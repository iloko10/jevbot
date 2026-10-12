"""Paper exchange: real market data, simulated post-only orders and fills. No real money.

Fill rule (conservative by default): our resting bid fills only when an aggressive SELL trades
strictly BELOW our price (we assume we were at the back of the queue). Set FILL_ON_TOUCH=true
for the optimistic version. Fill size is capped by the trade size.

Fees follow Hyperliquid's real perps schedule: the tier is set once per UTC day from this
book's own 14-day volume, like on the real exchange.
"""
import datetime as dt
from collections import deque
from dataclasses import dataclass

# Hyperliquid perps fees by 14-day weighted volume in USD: (min volume, tier, maker bps, taker bps).
# Source: hyperliquid.gitbook.io/hyperliquid-docs/trading/fees (checked Oct 2026).
FEE_TIERS = [
    (7e9, 6, 0.0, 2.4),
    (2e9, 5, 0.0, 2.6),
    (5e8, 4, 0.0, 2.8),
    (1e8, 3, 0.4, 3.0),
    (2.5e7, 2, 0.8, 3.5),
    (5e6, 1, 1.2, 4.0),
    (0, 0, 1.5, 4.5),
]
FOURTEEN_DAYS = 14 * 86400


def fee_tier(volume_14d: float):
    """Returns (tier, maker_bps, taker_bps) before any staking discount."""
    for min_vol, tier, maker, taker in FEE_TIERS:
        if volume_14d > min_vol or min_vol == 0:
            return tier, maker, taker


@dataclass
class Order:
    side: str      # "bid" / "ask"
    px: float
    sz: float
    ts: float


@dataclass
class Fill:
    ts: float
    side: str
    px: float
    sz: float
    fee: float
    maker: bool
    mid_at_fill: float
    book: str = "main"


class PaperExchange:
    def __init__(self, cfg, start_cash=10_000.0, name="main", min_half_spread_bps=None):
        self.cfg = cfg
        self.name = name
        self.min_half_spread_bps = cfg.min_half_spread_bps if min_half_spread_bps is None else min_half_spread_bps
        self.cash = start_cash
        self.start_cash = start_cash
        self.inventory = 0.0
        self.fees = 0.0
        self.volume = 0.0                      # USD traded since start
        self.vol_log: deque = deque()          # (ts, notional) for the 14-day fee tier
        self.orders: dict[str, Order | None] = {"bid": None, "ask": None}
        self.position_since: float | None = None
        self.peak_equity = start_cash
        self.day = None
        self.day_start_equity = start_cash
        self.n_posted = self.n_cancels = self.n_rejects = self.n_fills = 0
        self.fills: deque[Fill] = deque(maxlen=200)   # recent only; the full log is in the database
        self.killed = False
        self.tier, self.maker_bps, self.taker_bps = 0, 1.5, 4.5
        self._update_fees(now=None)

    # ---------- fees ----------
    def seed_volume(self, rows):
        """rows = [(ts, notional)] from earlier runs, so the fee tier survives restarts."""
        for ts, notional in rows:
            self.vol_log.append((ts, notional))
        self._update_fees(now=max((ts for ts, _ in rows), default=None))

    def _update_fees(self, now):
        if now is not None:
            while self.vol_log and self.vol_log[0][0] < now - FOURTEEN_DAYS:
                self.vol_log.popleft()
        vol14 = sum(n for _, n in self.vol_log)
        tier, maker, taker = fee_tier(vol14)
        disc = 1 - self.cfg.fee_discount
        self.tier = tier
        self.maker_bps = float(self.cfg.maker_fee_override) if self.cfg.maker_fee_override else maker * disc
        self.taker_bps = taker * disc
        self.volume_14d = vol14

    # ---------- accounting ----------
    def equity(self, mid):
        return self.cash + self.inventory * mid

    def mark(self, mid, now):
        eq = self.equity(mid)
        self.peak_equity = max(self.peak_equity, eq)
        day = dt.datetime.fromtimestamp(now, dt.timezone.utc).date()
        if day != self.day:
            if self.day is not None:
                self._update_fees(now)   # like Hyperliquid: tier re-assessed once per UTC day
            self.day, self.day_start_equity = day, eq
            self.killed = False  # daily-loss kill resets at UTC midnight (drawdown kill may trip again)
        return eq

    def book_info(self, mid, now):
        eq = self.equity(mid)
        return {
            "inventory": self.inventory,
            "max_position": self.cfg.limits.max_position,
            "equity": eq,
            "pnl_total": eq - self.start_cash,
            "daily_pnl": eq - self.day_start_equity,
            "unrealized_pnl": eq - self.start_cash,  # simple: total PnL marked to mid
            "drawdown": self.peak_equity - eq,
            "position_age_s": (now - self.position_since) if self.position_since else 0.0,
            "fees": self.fees,
        }

    def summary(self, mid):
        """The fee check: per $ traded, how much did we earn before fees, pay in fees, keep after fees."""
        pnl = self.equity(mid) - self.start_cash
        v = self.volume
        bps = (lambda x: round(x / v * 1e4, 2)) if v else (lambda x: None)
        return {
            "book": self.name, "half_spread_bps": self.min_half_spread_bps,
            "pnl": round(pnl, 2), "pnl_before_fees": round(pnl + self.fees, 2), "fees": round(self.fees, 2),
            "volume": round(v, 0), "fills": self.n_fills,
            "earned_bps": bps(pnl + self.fees), "fee_bps": bps(self.fees), "net_bps": bps(pnl),
            "tier": self.tier, "maker_fee_bps": round(self.maker_bps, 3), "volume_14d": round(self.volume_14d, 0),
            "inventory": round(self.inventory, 5), "killed": self.killed,
        }

    def _apply(self, side, px, sz, now, maker, mid):
        notional = px * sz
        fee = notional * (self.maker_bps if maker else self.taker_bps) / 1e4
        before = self.inventory
        if side == "bid":
            self.cash -= notional
            self.inventory += sz
        else:
            self.cash += notional
            self.inventory -= sz
        self.cash -= fee
        self.fees += fee
        self.volume += notional
        self.vol_log.append((now, notional))
        self.inventory = round(self.inventory, 10)
        if before == 0 or (before > 0) != (self.inventory > 0):
            self.position_since = now if self.inventory != 0 else None
        if self.inventory == 0:
            self.position_since = None
        f = Fill(now, side, px, sz, fee, maker, mid, self.name)
        self.fills.append(f)
        self.n_fills += 1
        return f

    # ---------- orders ----------
    def cancel_all(self):
        for s in ("bid", "ask"):
            if self.orders[s]:
                self.n_cancels += 1
            self.orders[s] = None

    def set_quote(self, side, px, sz, now, best_bid, best_ask, mid):
        """Post-only. Keeps the old order if the new one is too close (saves churn)."""
        cur = self.orders[side]
        if px is None or sz <= 0:
            if cur:
                self.n_cancels += 1
            self.orders[side] = None
            return "cancelled" if cur else "none"
        # post-only reject: would cross the book
        if (side == "bid" and px >= best_ask) or (side == "ask" and px <= best_bid):
            self.n_rejects += 1
            return "rejected"
        if cur:
            moved_bps = abs(px - cur.px) / mid * 1e4
            rested_ms = (now - cur.ts) * 1000
            if cur.px == px and cur.sz == sz:
                return "kept"
            # Let quotes rest. Only replace when price moved enough, and not too often
            # (unless the move is big). Constant cancel/replace is what kills naive bots.
            if moved_bps < self.cfg.requote_bps:
                return "kept"
            if rested_ms < self.cfg.min_rest_ms and moved_bps < 5 * self.cfg.requote_bps:
                return "kept"
            self.n_cancels += 1
        self.orders[side] = Order(side, px, sz, now)
        self.n_posted += 1
        return "posted"

    def on_trades(self, trades, mid):
        out = []
        for t in trades:
            if t.side == "A" and self.orders["bid"]:
                o = self.orders["bid"]
                if t.px < o.px or (self.cfg.fill_on_touch and t.px <= o.px):
                    out.append(self._fill_order(o, t, mid))
            elif t.side == "B" and self.orders["ask"]:
                o = self.orders["ask"]
                if t.px > o.px or (self.cfg.fill_on_touch and t.px >= o.px):
                    out.append(self._fill_order(o, t, mid))
        return out

    def _fill_order(self, o: Order, t, mid):
        sz = min(o.sz, t.sz)
        f = self._apply(o.side, o.px, sz, t.ts, maker=True, mid=mid)
        o.sz = round(o.sz - sz, 10)
        if o.sz <= 0:
            self.orders[o.side] = None
        return f

    def flatten(self, best_bid, best_ask, now, mid):
        """Kill switch: cancel everything and close the position as a taker."""
        self.cancel_all()
        if self.inventory > 0:
            return self._apply("ask", best_bid, self.inventory, now, maker=False, mid=mid)
        if self.inventory < 0:
            return self._apply("bid", best_ask, -self.inventory, now, maker=False, mid=mid)
        return None

    def stats(self):
        return {"posted": self.n_posted, "cancels": self.n_cancels, "rejects": self.n_rejects,
                "fills": self.n_fills,
                "fill_ratio": round(self.n_fills / self.n_posted, 4) if self.n_posted else 0.0}
