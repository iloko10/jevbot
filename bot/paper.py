"""Paper exchange: real market data, simulated post-only orders and fills. No real money.

Fill rule (conservative by default): our resting bid fills only when an aggressive SELL trades
strictly BELOW our price (we assume we were at the back of the queue). Set FILL_ON_TOUCH=true
for the optimistic version. Fill size is capped by the trade size.
"""
import datetime as dt
from dataclasses import dataclass

TAKER_FEE_BPS = 4.5


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


class PaperExchange:
    def __init__(self, cfg, start_cash=10_000.0):
        self.cfg = cfg
        self.cash = start_cash
        self.start_cash = start_cash
        self.inventory = 0.0
        self.fees = 0.0
        self.orders: dict[str, Order | None] = {"bid": None, "ask": None}
        self.position_since: float | None = None
        self.peak_equity = start_cash
        self.day = None
        self.day_start_equity = start_cash
        self.n_posted = self.n_cancels = self.n_rejects = self.n_fills = 0
        self.fills: list[Fill] = []
        self.killed = False

    # ---------- accounting ----------
    def equity(self, mid):
        return self.cash + self.inventory * mid

    def mark(self, mid, now):
        eq = self.equity(mid)
        self.peak_equity = max(self.peak_equity, eq)
        day = dt.datetime.fromtimestamp(now, dt.timezone.utc).date()
        if day != self.day:
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

    def _apply(self, side, px, sz, now, maker, mid):
        fee = px * sz * (self.cfg.maker_fee_bps if maker else TAKER_FEE_BPS) / 1e4
        before = self.inventory
        if side == "bid":
            self.cash -= px * sz
            self.inventory += sz
        else:
            self.cash += px * sz
            self.inventory -= sz
        self.cash -= fee
        self.fees += fee
        self.inventory = round(self.inventory, 10)
        if before == 0 or (before > 0) != (self.inventory > 0):
            self.position_since = now if self.inventory != 0 else None
        if self.inventory == 0:
            self.position_since = None
        f = Fill(now, side, px, sz, fee, maker, mid)
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
                    out.append(self._fill_order(o, t))
            elif t.side == "B" and self.orders["ask"]:
                o = self.orders["ask"]
                if t.px > o.px or (self.cfg.fill_on_touch and t.px >= o.px):
                    out.append(self._fill_order(o, t))
        return out

    def _fill_order(self, o: Order, t):
        sz = min(o.sz, t.sz)
        f = self._apply(o.side, o.px, sz, t.ts, maker=True, mid=t.px)
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
