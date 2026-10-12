"""Hard risk engine. Plain checks on measured numbers. Absolute veto. Never asks Jev anything."""
from dataclasses import dataclass, field

OK, HOLD, KILL = "OK", "HOLD", "KILL"


@dataclass
class RiskResult:
    level: str                          # OK / HOLD (cancel quotes, wait) / KILL (flatten, stop)
    reasons: list = field(default_factory=list)
    allow_bid: bool = True
    allow_ask: bool = True
    max_size: float = 0.0


def check(limits, *, now, book_ts, inventory, daily_pnl, drawdown, position_age_s,
          decision_latency_ms, quote_size) -> RiskResult:
    reasons = []

    # KILL: money limits
    if daily_pnl <= -limits.max_daily_loss:
        reasons.append(f"daily loss {daily_pnl:.2f} <= -{limits.max_daily_loss}")
    if drawdown >= limits.max_drawdown:
        reasons.append(f"drawdown {drawdown:.2f} >= {limits.max_drawdown}")
    if abs(inventory) > limits.max_position * 1.5:
        reasons.append(f"position {inventory} way over max {limits.max_position}")
    if reasons:
        return RiskResult(KILL, reasons, False, False, 0.0)

    # HOLD: data or decision not trustworthy right now
    if now - book_ts > limits.max_stale_data_s:
        reasons.append(f"stale book {now - book_ts:.1f}s")
    if decision_latency_ms is not None and decision_latency_ms > limits.max_decision_latency_ms:
        reasons.append(f"decision latency {decision_latency_ms:.0f}ms")
    if reasons:
        return RiskResult(HOLD, reasons, False, False, 0.0)

    # Per-order limits
    size = min(quote_size, limits.max_order_size)
    allow_bid = inventory + size <= limits.max_position
    allow_ask = inventory - size >= -limits.max_position
    if position_age_s > limits.max_inventory_age_s and inventory != 0:
        # old inventory: only allow the side that reduces it
        allow_bid, allow_ask = inventory < 0, inventory > 0
        reasons.append(f"inventory age {position_age_s:.0f}s -> reduce only")
    if not allow_bid:
        reasons.append("bid blocked by max position")
    if not allow_ask:
        reasons.append("ask blocked by max position")
    return RiskResult(OK, reasons, allow_bid, allow_ask, size)
