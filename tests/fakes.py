"""Fake market for tests and offline simulation: random-walk BTC book + random trades.

Sweeps (big market orders eating several levels) have permanent price impact: after a sweep the
price stays partly moved, like in a real market. Without this, wide quotes would look
free money in the simulation, because every sweep that filled them would snap right back.
"""
import random

from bot.feed import MarketData, Trade


class FakeMarket:
    def __init__(self, seed=1, mid=100_000.0, vol_bps_per_s=0.8, t0=1_760_000_000.0,
                 sweep_prob=0.03, impact=0.5):
        self.rng = random.Random(seed)
        self.mid, self.vol, self.t = mid, vol_bps_per_s, t0
        self.sweep_prob, self.impact = sweep_prob, impact
        self.md = MarketData()
        self.drift_bps = 0.0

    def step(self, dt=1.0, n_trades=6):
        self.t += dt
        self.mid *= 1 + (self.rng.gauss(self.drift_bps, self.vol) * dt ** 0.5) / 1e4
        m = round(self.mid)
        bids = [(m - 1 - i, self.rng.uniform(0.5, 3)) for i in range(5)]
        asks = [(m + 1 + i, self.rng.uniform(0.5, 3)) for i in range(5)]
        shift = 0.0
        for k in range(n_trades):
            side = "B" if self.rng.random() < 0.5 + self.drift_bps else "A"
            px = m + self.rng.choice([1, 2, 3, 4]) if side == "B" else m - self.rng.choice([1, 2, 3, 4])
            if self.rng.random() < self.sweep_prob:  # sweep through several levels (market order eating depth)
                px = m + self.rng.uniform(5, 45) * (1 if side == "B" else -1)
                shift += self.impact * (px - m)       # part of the move sticks (price impact)
            self.md.on_trade(Trade(self.t - dt + k * dt / n_trades, px, self.rng.uniform(0.001, 0.05), side))
        self.md.on_book(bids, asks, self.t)
        self.mid += shift                             # shows up in the next step's book
        return self.t
