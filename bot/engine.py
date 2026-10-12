"""The 24/7 loop.

Every tick:
  trades -> paper fills -> snapshot -> judgment battery (one call, with deadline)
  -> policy action -> A-S prices -> hard risk veto -> place/cancel quotes -> log

Fallback ladder:
  healthy + high confidence -> normal quotes
  healthy + low confidence  -> wider, smaller (QUOTE_WIDE)
  decision late             -> HOLD: cancel quotes, never quote on stale state
  Jev erroring              -> stub rules only, wider + smaller
  hard limit breached       -> KILL: flatten, stop
"""
import asyncio
import logging
import time
from collections import deque

from . import risk as R
from .judge import JevError, StubJudge
from .policy import KILL, compose_action
from .pricing import as_quote
from .state import build_snapshot

log = logging.getLogger("engine")


class Engine:
    def __init__(self, cfg, md, paper, store, judge, fallback=None, tick_size=1.0):
        self.cfg, self.md, self.paper, self.store = cfg, md, paper, store
        self.judge = judge
        self.fallback = fallback or StubJudge()
        self.tick_size = tick_size
        self.latencies = deque(maxlen=10)
        self.errors_in_row = 0
        self.last_jev_try = 0.0
        self.status: dict = {"started": time.time(), "ticks": 0}

    async def _decide(self, snap):
        """Returns (result, mode). mode: 'primary' | 'fallback' | 'late'."""
        primary_is_fallback = isinstance(self.judge, StubJudge)
        jev_down = self.errors_in_row >= self.cfg.limits.max_api_errors
        now = time.time()
        if not primary_is_fallback and not (jev_down and now - self.last_jev_try < 30):
            self.last_jev_try = now
            deadline = min(self.cfg.tick_ms, self.cfg.limits.max_decision_latency_ms) / 1000
            try:
                res = await asyncio.wait_for(self.judge.judge(snap), timeout=deadline)
                if self.errors_in_row >= self.cfg.limits.max_api_errors:
                    self.store.event("jev_recovered")
                self.errors_in_row = 0
                return res, "primary"
            except asyncio.TimeoutError:
                return None, "late"
            except (JevError, KeyError, ValueError) as e:
                self.errors_in_row += 1
                if self.errors_in_row == self.cfg.limits.max_api_errors:
                    self.store.event("jev_down", repr(e))
                    log.error("Jev down after %d errors: %s", self.errors_in_row, e)
        res = await self.fallback.judge(snap)
        return res, ("primary" if primary_is_fallback else "fallback")

    async def tick(self, now=None):
        live = now is None
        now = time.time() if live else now
        md, paper, cfg = self.md, self.paper, self.cfg
        if not md.ready:
            return None

        bb, ba = md.bids[0][0], md.asks[0][0]
        mid = (bb + ba) / 2
        for f in paper.on_trades(md.take_new_trades(), mid):
            self.store.fill(f)
        paper.mark(mid, now)
        book = paper.book_info(mid, now)

        health = {
            "fill_ratio": paper.stats()["fill_ratio"], "rejects": paper.n_rejects,
            "decision_ms_avg": round(sum(self.latencies) / len(self.latencies), 1) if self.latencies else 0,
            "decision_ms_max": round(max(self.latencies), 1) if self.latencies else 0,
            "book_age_s": round(now - md.book_ts, 2),
        }
        snap, internal = build_snapshot(md, now, book, health)

        t0 = time.perf_counter()
        res, mode = await self._decide(snap)
        latency = (time.perf_counter() - t0) * 1000
        self.latencies.append(latency)
        now2 = time.time() if live else now + latency / 1000  # re-check staleness AFTER the decision

        row = {"ts": now, "source": res["source"] if res else "none", "model": res["model"] if res else "",
               "latency_ms": latency, "mid": mid, "snapshot": snap, "answers": res["answers"] if res else {},
               "bid_px": None, "ask_px": None, "inventory": paper.inventory, "equity": book["equity"]}

        rr = R.check(cfg.limits, now=now2, book_ts=md.book_ts, inventory=paper.inventory,
                     daily_pnl=book["daily_pnl"], drawdown=book["drawdown"],
                     position_age_s=book["position_age_s"],
                     decision_latency_ms=latency if mode != "late" else 1e9,
                     quote_size=cfg.quote_size)

        if rr.level == R.KILL or paper.killed:
            if not paper.killed:
                f = paper.flatten(bb, ba, now, mid)
                if f:
                    self.store.fill(f)
                paper.killed = True
                self.store.event("kill", "; ".join(rr.reasons))
                log.error("KILL SWITCH: %s", rr.reasons)
            paper.cancel_all()
            row.update(action=KILL, reason="; ".join(rr.reasons) or "killed earlier today", risk=R.KILL,
                       risk_reasons=rr.reasons)
        elif rr.level == R.HOLD or res is None:
            paper.cancel_all()
            reasons = rr.reasons or ["decision missed deadline"]
            row.update(action="HOLD", reason="; ".join(reasons), risk=R.HOLD, risk_reasons=reasons)
        else:
            act = compose_action(res["answers"], snap, cfg)
            if mode == "fallback":  # Jev unavailable: rules only, be more careful
                act.spread_mult *= cfg.wide_mult
                act.size_mult *= 0.5
                act.reason = "FALLBACK " + act.reason
            q = as_quote(mid, paper.inventory / cfg.quote_size, internal["sigma_px_per_s"], cfg.gamma, cfg.kappa, cfg.horizon_s,
                         cfg.min_half_spread_bps, skew_mult=act.skew_mult, spread_mult=act.spread_mult,
                         tick=self.tick_size)
            # strong inventory skew can push a quote across the book; clamp it to sit just inside
            q.bid = min(q.bid, ba - self.tick_size)
            q.ask = max(q.ask, bb + self.tick_size)
            size = round(rr.max_size * act.size_mult, 6)
            bid_px = q.bid if (act.bid and rr.allow_bid) else None
            ask_px = q.ask if (act.ask and rr.allow_ask) else None
            paper.set_quote("bid", bid_px, size, now, bb, ba, mid)
            paper.set_quote("ask", ask_px, size, now, bb, ba, mid)
            row.update(action=act.name, reason=act.reason, risk=rr.level, risk_reasons=rr.reasons,
                       bid_px=paper.orders["bid"].px if paper.orders["bid"] else None,
                       ask_px=paper.orders["ask"].px if paper.orders["ask"] else None)

        self.store.decision(row)
        self.status.update(ticks=self.status["ticks"] + 1, last=row | {"snapshot": None},
                           book=book, stats=paper.stats(), mode=mode, errors_in_row=self.errors_in_row,
                           snapshot=snap)
        return row

    async def run(self):
        period = self.cfg.tick_ms / 1000
        while True:
            start = time.time()
            try:
                await self.tick()
            except Exception:
                log.exception("tick failed")  # one bad tick must never kill a 24/7 loop
                self.paper.cancel_all()
            await asyncio.sleep(max(0.0, period - (time.time() - start)))
