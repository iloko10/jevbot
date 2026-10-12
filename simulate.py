"""Offline test run on a FAKE market (random walk + big orders with price impact + bursts of one-way flow).
No network needed.

Usage:  python simulate.py [hours] [db_path]     then:  python calibrate.py <db_path>
Good for checking the plumbing. Says nothing about real profitability: only real data can.
"""
import asyncio
import collections
import os
import sys

from bot.config import Config
from bot.engine import Engine
from bot.judge import StubJudge
from bot.paper import PaperExchange
from bot.store import Store
from tests.fakes import FakeMarket


async def main(hours=4.0, db="data/sim.db", seed=3):
    if os.path.exists(db):
        os.remove(db)
    cfg = Config()
    cfg.db_path = db
    fm = FakeMarket(seed=seed)
    for _ in range(300):
        fm.step()
    eng = Engine(cfg, fm.md, PaperExchange(cfg), Store(db), StubJudge())
    acts = collections.Counter()
    for i in range(int(hours * 3600)):
        fm.drift_bps = 0.35 if (i % 1800) < 300 else 0.0   # 5-min one-way burst every 30 min
        acts[(await eng.tick(now=fm.step()))["action"]] += 1
    print("main book actions:", dict(acts))
    print("main book orders:", eng.paper.stats())
    print("\nFees check (per $ traded; bps = cents per $100):")
    print("  book              spread  fills    volume$   earned   fees    net     PnL$")
    for b in eng.books:
        s = b.summary(fm.mid)
        fmt = lambda x: f"{x:+.2f}" if x is not None else "  -  "
        print(f"  {s['book']:<17} {s['half_spread_bps']:>4g}bp {s['fills']:>6} {s['volume']:>11,.0f}  "
              f"{fmt(s['earned_bps'])}  {fmt(s['fee_bps'])}  {fmt(s['net_bps'])}  {s['pnl']:>+7.2f}"
              f"{'  (kill switch hit)' if s['killed'] else ''}")


if __name__ == "__main__":
    h = float(sys.argv[1]) if len(sys.argv) > 1 else 4.0
    asyncio.run(main(h, sys.argv[2] if len(sys.argv) > 2 else "data/sim.db"))
