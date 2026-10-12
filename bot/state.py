"""State engine: turns raw market data into one small, numeric snapshot. Pure code, no AI.

Rule: every field uses only data with timestamp <= `now`. No peeking at the future.
"""
import math


def _price_at_or_before(mids, t):
    """Last mid at or before time t (mids is time-ordered)."""
    px = None
    for ts, m in mids:
        if ts > t:
            break
        px = m
    return px


def realized_vol_per_s(mids, now, window_s):
    """Std of 1-second log returns over the window (as a fraction, per sqrt-second)."""
    pts = [(ts, m) for ts, m in mids if now - window_s <= ts <= now]
    if len(pts) < 3:
        return 0.0
    # resample to 1s buckets (last value per second)
    buckets = {}
    for ts, m in pts:
        buckets[int(ts)] = m
    series = [buckets[k] for k in sorted(buckets)]
    rets = [math.log(b / a) for a, b in zip(series, series[1:]) if a > 0 and b > 0]
    if len(rets) < 2:
        return 0.0
    mu = sum(rets) / len(rets)
    return math.sqrt(sum((r - mu) ** 2 for r in rets) / (len(rets) - 1))


def build_snapshot(md, now: float, book: dict, health: dict) -> dict:
    """md = MarketData, book = paper exchange position info, health = execution/decision stats."""
    bids, asks = md.bids[:3], md.asks[:3]
    bb, ba = bids[0][0], asks[0][0]
    mid = (bb + ba) / 2
    bsz, asz = bids[0][1], asks[0][1]
    micro = (bb * asz + ba * bsz) / (bsz + asz) if (bsz + asz) > 0 else mid
    d_bid = sum(s for _, s in bids)
    d_ask = sum(s for _, s in asks)
    imb = (d_bid - d_ask) / (d_bid + d_ask) if (d_bid + d_ask) else 0.0

    mids = [x for x in md.mids if x[0] <= now]

    def ret_bps(sec):
        p = _price_at_or_before(mids, now - sec)
        return round((mid / p - 1) * 1e4, 2) if p else 0.0

    recent = [t for t in md.trades if now - 60 <= t.ts <= now]
    buy_v = sum(t.sz for t in recent if t.side == "B")
    sell_v = sum(t.sz for t in recent if t.side == "A")
    tot = buy_v + sell_v

    vol_1m = realized_vol_per_s(mids, now, 60)
    vol_5m = realized_vol_per_s(mids, now, 300)

    snap = {
        "price": {
            "mid": round(mid, 2),
            "microprice_vs_mid_bps": round((micro / mid - 1) * 1e4, 2),
            "ret_1m_bps": ret_bps(60), "ret_5m_bps": ret_bps(300), "ret_30m_bps": ret_bps(1800),
        },
        "book": {
            "spread_bps": round((ba - bb) / mid * 1e4, 3),
            "depth3_bid": round(d_bid, 4), "depth3_ask": round(d_ask, 4),
            "imbalance": round(imb, 3),
        },
        "flow_60s": {
            "aggr_buy_vol": round(buy_v, 4), "aggr_sell_vol": round(sell_v, 4),
            "aggr_buy_ratio": round(buy_v / tot, 3) if tot else 0.5,
            "trades": len(recent),
        },
        "vol": {
            "rv_1m_bps_per_s": round(vol_1m * 1e4, 3),
            "rv_5m_bps_per_s": round(vol_5m * 1e4, 3),
            "rv_ratio_1m_5m": round(vol_1m / vol_5m, 2) if vol_5m else 1.0,
        },
        "position": {
            "inventory": round(book["inventory"], 5),
            "inventory_vs_max": round(book["inventory"] / book["max_position"], 3) if book["max_position"] else 0,
            "unrealized_pnl": round(book["unrealized_pnl"], 2),
            "drawdown": round(book["drawdown"], 2),
            "position_age_s": round(book["position_age_s"], 1),
        },
        "health": health,
    }
    # numbers the code needs but Jev doesn't (kept out of the Jev payload to save tokens)
    internal = {"mid": mid, "best_bid": bb, "best_ask": ba, "sigma_px_per_s": vol_1m * mid, "ts": now}
    return snap, internal
