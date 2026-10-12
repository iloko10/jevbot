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

Spread tests: extra paper books that quote wider than the main book. They see the same trades
and use the same judgments (one Jev call per tick, no extra cost), but each has its own position,
fees, risk checks and kill switch. They answer one question: which spread beats the fees?
"""
import asyncio
import logging
import time
from collections import deque

from . import risk as R
from .judge import JevError, StubJudge
from .paper import FOURTEEN_DAYS, PaperExchange
from .policy import KILL, compose_action
from .pricing import as_quote
from .state import build_snapshot

log = logging.getLogger("engine")
STATS_EVERY_S = 60


def parse_spread_tests(text, main_bps):
    out = []
    for part in (text or "").split(","):
        part = part.strip()
        if part:
            v = float(part)
            if v > 0 and v != main_bps and v not in out:
                out.append(v)
    return out


class Engine:
    def __init__(self, cfg, md, paper, store, judge, fallback=None, tick_size=1.0, tests=None):
        self.cfg, self.md, self.paper, self.store = cfg, md, paper, store
        self.judge = judge
        self.fallback = fallback or StubJudge()
        self.tick_size = tick_size
        if tests is None:
            tests = [PaperExchange(cfg, name=f"spread_{b:g}bps", min_half_spread_bps=b)
                     for b in parse_spread_tests(cfg.spread_tests_bps, paper.min_half_spread_bps)]
        self.tests = tests
        self.books = [paper] + tests
        since = time.time() - FOURTEEN_DAYS
        for b in self.books:  # keep the 14-day fee tier across restarts
            b.seed_volume(store.volume_since(b.name, since))
        self.latencies = deque(maxlen=10)
        self.errors_in_row = 0
        self.last_jev_try = 0.0
        self.last_stats = 0.0
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

    def _answers_for_test(self, book, answers, now):
        """Test books share the market judgments, but 'inventory pressure' must describe THEIR position."""
        age = (now - book.position_since) if book.position_since else 0.0
        pressure = min(3.0, abs(book.inventory) / self.cfg.limits.max_position * 3 + min(1.0, age / 600))
        a = dict(answers)
        a["inventory_pressure"] = {"type": "score", "score": pressure, "confidence": 0.9}
        return a

    def _trade(self, book, answers, mode, latency, now, now2, mid, bb, ba, sigma):
        """Risk check, then quote (or hold / kill) for one book. Returns the fields to log."""
        cfg = self.cfg
        info = book.book_info(mid, now)
        rr = R.check(cfg.limits, now=now2, book_ts=self.md.book_ts, inventory=book.inventory,
                     daily_pnl=info["daily_pnl"], drawdown=info["drawdown"],
                     position_age_s=info["position_age_s"],
                     decision_latency_ms=latency if mode != "late" else 1e9,
                     quote_size=cfg.quote_size)

        if rr.level == R.KILL or book.killed:
            if not book.killed:
                f = book.flatten(bb, ba, now, mid)
                if f:
                    self.store.fill(f)
                book.killed = True
                self.store.event("kill", f"{book.name}: " + "; ".join(rr.reasons))
                log.error("KILL SWITCH [%s]: %s", book.name, rr.reasons)
            book.cancel_all()
            return dict(action=KILL, reason="; ".join(rr.reasons) or "killed earlier today", risk=R.KILL,
                        risk_reasons=rr.reasons)

        if rr.level == R.HOLD or answers is None:
            book.cancel_all()
            reasons = rr.reasons or ["decision missed deadline"]
            return dict(action="HOLD", reason="; ".join(reasons), risk=R.HOLD, risk_reasons=reasons)

        act = compose_action(answers, {"position": {"inventory": book.inventory}}, cfg)
        if mode == "fallback":  # Jev unavailable: rules only, be more careful
            act.spread_mult *= cfg.wide_mult
            act.size_mult *= 0.5
            act.reason = "FALLBACK " + act.reason
        q = as_quote(mid, book.inventory / cfg.quote_size, sigma, cfg.gamma, cfg.kappa, cfg.horizon_s,
                     book.min_half_spread_bps, skew_mult=act.skew_mult, spread_mult=act.spread_mult,
                     tick=self.tick_size)
        # strong inventory skew can push a quote across the book; clamp it to sit just inside
        q.bid = min(q.bid, ba - self.tick_size)
        q.ask = max(q.ask, bb + self.tick_size)
        size = round(rr.max_size * act.size_mult, 6)
        book.set_quote("bid", q.bid if (act.bid and rr.allow_bid) else None, size, now, bb, ba, mid)
        book.set_quote("ask", q.ask if (act.ask and rr.allow_ask) else None, size, now, bb, ba, mid)
        return dict(action=act.name, reason=act.reason, risk=rr.level, risk_reasons=rr.reasons,
                    bid_px=book.orders["bid"].px if book.orders["bid"] else None,
                    ask_px=book.orders["ask"].px if book.orders["ask"] else None)

    async def tick(self, now=None):
        live = now is None
        now = time.time() if live else now
        md, paper = self.md, self.paper
        if not md.ready:
            return None

        bb, ba = md.bids[0][0], md.asks[0][0]
        mid = (bb + ba) / 2
        trades = md.take_new_trades()
        for b in self.books:            # every book sees the same real trades
            for f in b.on_trades(trades, mid):
                self.store.fill(f)
            b.mark(mid, now)
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
        answers = res["answers"] if res else None
        sigma = internal["sigma_px_per_s"]

        row = {"ts": now, "source": res["source"] if res else "none", "model": res["model"] if res else "",
               "latency_ms": latency, "mid": mid, "snapshot": snap, "answers": answers or {},
               "bid_px": None, "ask_px": None, "inventory": paper.inventory, "equity": book["equity"]}
        row.update(self._trade(paper, answers, mode, latency, now, now2, mid, bb, ba, sigma))
        for b in self.tests:
            self._trade(b, self._answers_for_test(b, answers, now) if answers else None,
                        mode, latency, now, now2, mid, bb, ba, sigma)

        self.store.decision(row)
        books = [b.summary(mid) for b in self.books]
        if now - self.last_stats >= STATS_EVERY_S:
            self.store.book_stats(now, books)
            self.last_stats = now
        self.status.update(ticks=self.status["ticks"] + 1, last=row | {"snapshot": None},
                           book=book, stats=paper.stats(), mode=mode, errors_in_row=self.errors_in_row,
                           snapshot=snap, books=books)
        return row

    async def run(self):
        period = self.cfg.tick_ms / 1000
        while True:
            start = time.time()
            try:
                await self.tick()
            except Exception:
                log.exception("tick failed")  # one bad tick must never kill a 24/7 loop
                for b in self.books:
                    b.cancel_all()
            await asyncio.sleep(max(0.0, period - (time.time() - start)))
