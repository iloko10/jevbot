"""Live Hyperliquid market data: L2 order book + trades over WebSocket. Public data, no key needed."""
import asyncio
import json
import logging
import time
from collections import deque
from dataclasses import dataclass

import websockets

log = logging.getLogger("feed")


@dataclass
class Trade:
    ts: float      # seconds
    px: float
    sz: float
    side: str      # "B" = aggressive buy, "A" = aggressive sell


class MarketData:
    """Holds the latest book and recent trades. Pure data, easy to fake in tests."""

    def __init__(self, max_trades=5000, max_mids=4000):
        self.bids: list[tuple[float, float]] = []   # (px, sz) best first
        self.asks: list[tuple[float, float]] = []
        self.book_ts: float = 0.0
        self.trades: deque[Trade] = deque(maxlen=max_trades)
        self.mids: deque[tuple[float, float]] = deque(maxlen=max_mids)  # (ts, mid) one per book update
        self.new_trades: list[Trade] = []  # trades since the paper exchange last consumed them

    def on_book(self, bids, asks, ts):
        self.bids, self.asks, self.book_ts = bids, asks, ts
        if bids and asks:
            self.mids.append((ts, (bids[0][0] + asks[0][0]) / 2))

    def on_trade(self, t: Trade):
        self.trades.append(t)
        self.new_trades.append(t)

    def take_new_trades(self) -> list[Trade]:
        out, self.new_trades = self.new_trades, []
        return out

    @property
    def ready(self) -> bool:
        return bool(self.bids and self.asks)


def parse_book(msg: dict):
    levels = msg["levels"]
    bids = [(float(l["px"]), float(l["sz"])) for l in levels[0]]
    asks = [(float(l["px"]), float(l["sz"])) for l in levels[1]]
    return bids, asks, msg.get("time", time.time() * 1000) / 1000


def parse_trades(msg: list) -> list[Trade]:
    return [Trade(ts=t["time"] / 1000, px=float(t["px"]), sz=float(t["sz"]), side=t["side"]) for t in msg]


class HyperliquidFeed:
    def __init__(self, url: str, coin: str, md: MarketData):
        self.url, self.coin, self.md = url, coin, md
        self.connected = False
        self.reconnects = 0

    async def run(self):
        backoff = 1
        while True:
            try:
                async with websockets.connect(self.url, ping_interval=20, max_size=2**22) as ws:
                    for sub in ({"type": "l2Book", "coin": self.coin}, {"type": "trades", "coin": self.coin}):
                        await ws.send(json.dumps({"method": "subscribe", "subscription": sub}))
                    self.connected, backoff = True, 1
                    log.info("connected to %s for %s", self.url, self.coin)
                    async for raw in ws:
                        self._handle(json.loads(raw))
            except Exception as e:  # network drops are normal; reconnect forever
                self.connected = False
                self.reconnects += 1
                log.warning("feed dropped (%s), reconnecting in %ss", e, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30)

    def _handle(self, m: dict):
        ch = m.get("channel")
        if ch == "l2Book":
            self.md.on_book(*parse_book(m["data"]))
        elif ch == "trades":
            for t in parse_trades(m["data"]):
                self.md.on_trade(t)
