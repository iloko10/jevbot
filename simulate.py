"""Offline test run on a FAKE market (random walk + bursts of one-way flow). No network needed.

Usage:  python simulate.py [hours] [db_path]     then:  python calibrate.py <db_path>
Good for checking the plumbing. Says nothing about real profitability.
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


async def main(hours=4.0, db="data/sim.db"):
    if os.path.exists(db):
        os.remove(db)
    cfg = Config()
    cfg.db_path = db
    fm = FakeMarket(seed=3)
    for _ in range(300):
        fm.step()
    eng = Engine(cfg, fm.md, PaperExchange(cfg), Store(db), StubJudge())
    acts = collections.Counter()
    for i in range(int(hours * 3600)):
        fm.drift_bps = 0.35 if (i % 1800) < 300 else 0.0   # 5-min one-way burst every 30 min
        acts[(await eng.tick(now=fm.step()))["action"]] += 1
    b = eng.paper.book_info(fm.mid, fm.t)
    print("actions:", dict(acts))
    print("orders:", eng.paper.stats())
    print(f"PnL ${b['pnl_total']:+.2f}  fees ${b['fees']:.2f}  inventory {b['inventory']:+.4f}  "
          f"max drawdown seen ${eng.paper.peak_equity - min(eng.paper.equity(fm.mid), eng.paper.peak_equity):.2f}")


if __name__ == "__main__":
    h = float(sys.argv[1]) if len(sys.argv) > 1 else 4.0
    asyncio.run(main(h, sys.argv[2] if len(sys.argv) > 2 else "data/sim.db"))
