import asyncio
import json
import math

import httpx
import pytest

from bot import risk as R
from bot.config import Config
from bot.engine import Engine
from bot.feed import Trade, parse_book, parse_trades
from bot.judge import BATTERY, JevJudge, StubJudge
from bot.paper import PaperExchange
from bot.policy import KILL, PULL, QUOTE, QUOTE_WIDE, REDUCE_ONLY, STAND_DOWN, compose_action, kelly_fraction
from bot.pricing import as_quote
from bot.store import Store

from .fakes import FakeMarket


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    c = Config()
    c.db_path = str(tmp_path / "t.db")
    return c


# ---------------- feed parsing (Hyperliquid message shapes) ----------------
def test_parse_hyperliquid_messages():
    bids, asks, ts = parse_book({"coin": "BTC", "time": 1700000000000,
                                 "levels": [[{"px": "100", "sz": "1.5", "n": 2}], [{"px": "101", "sz": "2", "n": 1}]]})
    assert bids == [(100.0, 1.5)] and asks == [(101.0, 2.0)] and ts == 1700000000.0
    tr = parse_trades([{"coin": "BTC", "side": "A", "px": "99", "sz": "0.1", "time": 1700000000500,
                        "hash": "x", "tid": 1, "users": ["a", "b"]}])
    assert tr[0].side == "A" and tr[0].px == 99.0 and tr[0].ts == 1700000000.5


# ---------------- pricing ----------------
def test_as_quote_skews_against_inventory():
    flat = as_quote(100_000, 0.0, 5.0, 0.1, 1.5, 60, 2.0)
    long = as_quote(100_000, 0.02, 5.0, 0.1, 1.5, 60, 2.0)
    assert flat.bid < 100_000 < flat.ask
    assert long.reservation < flat.reservation        # long -> lower prices to sell inventory
    assert flat.half_spread >= 100_000 * 2 / 1e4       # min spread floor respected
    wide = as_quote(100_000, 0.0, 5.0, 0.1, 1.5, 60, 2.0, spread_mult=2)
    assert wide.ask - wide.bid > flat.ask - flat.bid


# ---------------- risk ----------------
def _risk(cfg, **kw):
    base = dict(now=100, book_ts=99.5, inventory=0.0, daily_pnl=0, drawdown=0, position_age_s=0,
                decision_latency_ms=50, quote_size=cfg.quote_size)
    base.update(kw)
    return R.check(cfg.limits, **base)


def test_risk_vetoes(cfg):
    assert _risk(cfg).level == R.OK
    assert _risk(cfg, daily_pnl=-1000).level == R.KILL
    assert _risk(cfg, drawdown=1000).level == R.KILL
    assert _risk(cfg, book_ts=0).level == R.HOLD                      # stale data
    assert _risk(cfg, decision_latency_ms=5000).level == R.HOLD       # late decision
    r = _risk(cfg, inventory=cfg.limits.max_position)                 # at max long
    assert r.level == R.OK and not r.allow_bid and r.allow_ask
    r = _risk(cfg, inventory=-0.01, position_age_s=10_000)            # old short -> buy only
    assert r.allow_bid and not r.allow_ask


# ---------------- policy ----------------
def _ans(toxic=0.1, stress=0.1, q=2.5, qc=0.9, inv=0.0):
    return {"toxic_flow": {"noul": toxic}, "liquidity_stressed": {"noul": stress},
            "quote_environment": {"score": q, "confidence": qc}, "inventory_pressure": {"score": inv}}


def _snap(inv=0.0):
    return {"position": {"inventory": inv}}


def test_policy(cfg):
    assert compose_action(_ans(), _snap(), cfg).name == QUOTE
    assert compose_action(_ans(toxic=0.9), _snap(), cfg).name == PULL
    assert compose_action(_ans(qc=0.5), _snap(), cfg).name == QUOTE_WIDE      # unsure -> wide & small
    assert compose_action(_ans(q=0.3), _snap(), cfg).name == STAND_DOWN
    a = compose_action(_ans(inv=3), _snap(inv=0.01), cfg)
    assert a.name == REDUCE_ONLY and a.ask and not a.bid
    assert kelly_fraction(0.5) == 0 and kelly_fraction(0.9) == 0.25


# ---------------- paper exchange ----------------
def test_paper_fills_and_post_only(cfg):
    p = PaperExchange(cfg)
    assert p.set_quote("bid", 101, 0.01, 0, best_bid=100, best_ask=101, mid=100.5) == "rejected"
    assert p.set_quote("bid", 99, 0.01, 0, 100, 101, 100.5) == "posted"
    p.on_trades([Trade(1, 99, 1.0, "A")], 100)          # touch only -> no fill (conservative)
    assert p.inventory == 0
    p.on_trades([Trade(2, 98, 0.004, "A")], 99)         # through -> fill capped by trade size
    assert p.inventory == pytest.approx(0.004) and p.orders["bid"].sz == pytest.approx(0.006)
    assert p.fees > 0
    f = p.flatten(98, 99, 3, 98.5)
    assert p.inventory == 0 and not f.maker


def test_requote_throttle(cfg):
    p = PaperExchange(cfg)
    p.set_quote("bid", 99_990, 0.01, 0.0, 99_999, 100_001, 100_000)
    assert p.set_quote("bid", 99_991, 0.01, 9.0, 99_999, 100_001, 100_000) == "kept"    # tiny move: keep resting
    assert p.set_quote("bid", 99_975, 0.01, 0.5, 99_999, 100_001, 100_000) == "kept"    # 1.5 bps but too soon
    assert p.set_quote("bid", 99_975, 0.01, 5.0, 99_999, 100_001, 100_000) == "posted"  # moved + rested
    assert p.set_quote("bid", 99_900, 0.01, 5.1, 99_999, 100_001, 100_000) == "posted"  # big move: replace now


# ---------------- judges ----------------
def _snapshot_from_fake(cfg):
    fm = FakeMarket()
    for _ in range(120):
        fm.step()
    from bot.state import build_snapshot
    p = PaperExchange(cfg)
    snap, internal = build_snapshot(fm.md, fm.t, p.book_info(fm.mid, fm.t), {})
    return snap, internal


async def test_stub_matches_api_shape(cfg):
    snap, internal = _snapshot_from_fake(cfg)
    assert len(json.dumps(snap)) < 1600               # compact state (~<400 tokens)
    assert internal["sigma_px_per_s"] > 0
    res = await StubJudge().judge(snap)
    for qid, q in BATTERY.items():
        a = res["answers"][qid]
        assert a["type"] == q["type"]
        if a["type"] == "choice":
            assert set(a["probabilities"]) == set(q["criteria"])
            assert math.isclose(sum(a["probabilities"].values()), 1, abs_tol=1e-9)
        if a["type"] == "score":
            assert 0 <= a["score"] <= len(q["criteria"]) - 1
        if a["type"] == "noul":
            assert 0 <= a["noul"] <= 1


async def test_jev_request_format(cfg):
    seen = {}

    def handler(req: httpx.Request):
        seen["body"] = json.loads(req.content)
        seen["auth"] = req.headers["authorization"]
        ans = {qid: ({"type": "noul", "noul": 0.2} if q["type"] == "noul" else
                     {"type": "choice", "choice": list(q["criteria"])[0],
                      "probabilities": {k: 1 / len(q["criteria"]) for k in q["criteria"]}, "confidence": 0.5}
                     if q["type"] == "choice" else
                     {"type": "score", "score": 2.4, "probabilities": {"2": 0.6, "3": 0.4}, "confidence": 0.9})
               for qid, q in seen["body"]["questions"].items()}
        return httpx.Response(200, json={"model": "jev-1.13.0", "answers": ans,
                                         "usage": {"input_tokens": 400, "output_tokens": 30}})

    j = JevJudge("k", "https://api.typesafe.ai/v1/systemone", "jev-1.13.0", 500)
    j.client = httpx.AsyncClient(transport=httpx.MockTransport(handler), headers=j.client.headers)
    snap, _ = _snapshot_from_fake(cfg)
    res = await j.judge(snap)
    assert seen["auth"] == "Bearer k"
    assert seen["body"]["model"] == "jev-1.13.0" and seen["body"]["state"] == snap
    assert seen["body"]["questions"]["quote_environment"]["criteria"][0] == "Do not quote"
    assert res["source"] == "jev" and res["answers"]["quote_environment"]["score"] == 2.4


# ---------------- engine: full loop + fallback ladder ----------------
class SlowJudge:
    async def judge(self, snap):
        await asyncio.sleep(5)


class BrokenJudge:
    async def judge(self, snap):
        from bot.judge import JevError
        raise JevError("HTTP 529")


def _engine(cfg, judge=None):
    fm = FakeMarket(seed=7)
    for _ in range(60):
        fm.step()
    store = Store(cfg.db_path)
    return fm, Engine(cfg, fm.md, PaperExchange(cfg), store, judge or StubJudge())


async def test_engine_runs_and_logs(cfg):
    fm, eng = _engine(cfg)
    actions = []
    for _ in range(600):
        row = await eng.tick(now=fm.step())
        actions.append(row["action"])
    assert eng.status["ticks"] == 600
    assert any(a in (QUOTE, QUOTE_WIDE) for a in actions)
    n = eng.store.db.execute("SELECT COUNT(*) FROM decisions").fetchone()[0]
    assert n == 600
    assert abs(eng.paper.inventory) <= cfg.limits.max_position + 1e-9


async def test_late_decision_holds(cfg):
    cfg.tick_ms = 200
    fm, eng = _engine(cfg, SlowJudge())
    eng.paper.orders["bid"] = None
    row = await eng.tick(now=fm.step())
    assert row["action"] == "HOLD" and eng.paper.orders["bid"] is None


async def test_jev_down_uses_fallback(cfg):
    fm, eng = _engine(cfg, BrokenJudge())
    row = await eng.tick(now=fm.step())
    assert row["source"] == "stub" and eng.errors_in_row == 1
    if row["action"] not in ("HOLD", KILL, PULL, STAND_DOWN):
        assert row["reason"].startswith("FALLBACK")


async def test_kill_switch(cfg):
    fm, eng = _engine(cfg)
    await eng.tick(now=fm.step())
    eng.paper.inventory = 0.01
    eng.paper.cash -= 0.01 * fm.mid
    eng.paper.position_since = fm.t
    fm.mid *= 0.98                       # 2% crash while long -> ~20 USD loss... make it bigger:
    eng.paper.peak_equity += 500         # simulate big drawdown from peak
    row = await eng.tick(now=fm.step())
    assert row["action"] == KILL and eng.paper.inventory == 0 and eng.paper.killed
    row = await eng.tick(now=fm.step())
    assert row["action"] == KILL         # stays off
    ev = eng.store.db.execute("SELECT kind FROM events").fetchall()
    assert ("kill",) in ev


# ---------------- fees: tiers, discount, earned vs fees ----------------
from bot.engine import parse_spread_tests
from bot.paper import fee_tier


def test_fee_tiers_match_hyperliquid_schedule():
    assert fee_tier(0) == (0, 1.5, 4.5)
    assert fee_tier(5e6) == (0, 1.5, 4.5)          # must be MORE than $5M
    assert fee_tier(6e6) == (1, 1.2, 4.0)
    assert fee_tier(3e7) == (2, 0.8, 3.5)
    assert fee_tier(2e8) == (3, 0.4, 3.0)
    assert fee_tier(6e8)[1] == 0.0                   # tier 4+: zero maker fee


def test_fee_tier_changes_once_per_day_and_discount(cfg):
    cfg.fee_discount = 0.05                          # >10 HYPE staked
    p = PaperExchange(cfg)
    assert p.maker_bps == pytest.approx(1.425)
    day1 = 1_760_000_000.0
    p.mark(100_000, day1)
    p.seed_volume([(day1 - 3600, 6e6)])              # $6M traded in the last 14 days
    assert p.tier == 1                               # seeding (restart) applies right away
    p2 = PaperExchange(cfg)
    p2.mark(100_000, day1)
    p2._apply("bid", 100_000, 30.0, day1, maker=True, mid=100_000)   # $3M
    p2._apply("ask", 100_000, 30.0, day1, maker=True, mid=100_000)   # $3M
    assert p2.tier == 0                              # same day: no change yet
    p2.mark(100_000, day1 + 86400)                   # next UTC day: re-assessed
    assert p2.tier == 1 and p2.maker_bps == pytest.approx(1.2 * 0.95)
    p2.mark(100_000, day1 + 16 * 86400)              # volume older than 14 days drops out
    assert p2.tier == 0


def test_summary_splits_earned_and_fees(cfg):
    p = PaperExchange(cfg)
    p._apply("bid", 99_980, 0.01, 1, maker=True, mid=100_000)        # buy 2 bps under mid
    p._apply("ask", 100_020, 0.01, 2, maker=True, mid=100_000)       # sell 2 bps over mid
    s = p.summary(100_000)
    assert s["fills"] == 2 and s["volume"] == pytest.approx(2000, abs=1)
    assert s["pnl_before_fees"] == pytest.approx(0.40, abs=1e-6)     # $0.40 spread captured
    assert s["fee_bps"] == pytest.approx(1.5, abs=0.01)
    assert s["earned_bps"] == pytest.approx(2.0, abs=0.01)           # 0.40 / 2000
    assert s["net_bps"] == pytest.approx(0.5, abs=0.01)


def test_parse_spread_tests():
    assert parse_spread_tests("3, 5,8", 2.0) == [3.0, 5.0, 8.0]
    assert parse_spread_tests("2,3,3", 2.0) == [3.0]                 # skips the main spread and repeats
    assert parse_spread_tests("", 2.0) == []


async def test_spread_test_books_share_data(cfg):
    cfg.spread_tests_bps = "4,8"
    fm, eng = _engine(cfg)
    assert [b.name for b in eng.books] == ["main", "spread_4bps", "spread_8bps"]
    for _ in range(900):
        await eng.tick(now=fm.step())
    main, w4, w8 = eng.books
    # wider book quotes further from the price (when both sides are up)
    if main.orders["bid"] and w8.orders["bid"]:
        assert w8.orders["bid"].px < main.orders["bid"].px
    rows = eng.store.db.execute("SELECT book, COUNT(*) FROM fills GROUP BY book").fetchall()
    assert dict(rows).get("main", 0) > 0
    stats = eng.store.db.execute("SELECT DISTINCT book FROM book_stats").fetchall()
    assert {r[0] for r in stats} == {"main", "spread_4bps", "spread_8bps"}
    assert len(eng.status["books"]) == 3
    # only ONE judgment per tick, whatever the number of books
    assert eng.store.db.execute("SELECT COUNT(*) FROM decisions").fetchone()[0] == 900


def test_old_database_gets_book_column(tmp_path):
    import sqlite3
    path = str(tmp_path / "old.db")
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE fills (ts REAL, side TEXT, px REAL, sz REAL, fee REAL, maker INTEGER, mid REAL)")
    db.execute("INSERT INTO fills VALUES (1, 'bid', 100, 1, 0.01, 1, 100)")
    db.commit()
    db.close()
    s = Store(path)
    assert s.volume_since("main", 0) == [(1.0, 100.0)]              # old rows count as the main book
