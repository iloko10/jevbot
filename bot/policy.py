"""Policy engine: turns the judgments into ONE action. Thresholds live here, in code, never in the model."""
from dataclasses import dataclass

KILL, PULL, STAND_DOWN, WIDEN, QUOTE, QUOTE_WIDE, REDUCE_ONLY = (
    "KILL", "PULL_QUOTES", "STAND_DOWN", "WIDEN", "QUOTE", "QUOTE_WIDE", "REDUCE_ONLY")


@dataclass
class Action:
    name: str
    bid: bool = False          # post a bid?
    ask: bool = False          # post an ask?
    spread_mult: float = 1.0
    skew_mult: float = 1.0
    size_mult: float = 1.0
    reason: str = ""


def compose_action(ans: dict, snap: dict, cfg) -> Action:
    pos = snap["position"]
    inv = pos["inventory"]

    toxic = ans["toxic_flow"]["noul"]
    stress = ans["liquidity_stressed"]["noul"]
    q = ans["quote_environment"]
    pressure = ans["inventory_pressure"]["score"]
    skew = 1.0 + pressure  # 0 -> normal A-S skew, 3 -> 4x skew

    # Inventory first: if we must cut, only quote the side that reduces it.
    if pressure >= 2.5 and inv != 0:
        return Action(REDUCE_ONLY, bid=inv < 0, ask=inv > 0, skew_mult=skew, reason=f"inventory_pressure {pressure:.2f}")

    if toxic > cfg.toxic_pull:
        return Action(PULL, reason=f"toxic_flow {toxic:.2f}")
    if stress > cfg.stress_widen:
        return Action(WIDEN, bid=True, ask=True, spread_mult=cfg.wide_mult * 1.5, skew_mult=skew,
                      size_mult=0.5, reason=f"liquidity_stressed {stress:.2f}")

    if q["score"] >= cfg.quote_score_min and q["confidence"] >= cfg.quote_conf_min:
        return Action(QUOTE, bid=True, ask=True, skew_mult=skew,
                      reason=f"quote_env {q['score']:.2f} conf {q['confidence']:.2f}")
    if q["score"] >= cfg.wide_score_min:
        # fallback ladder: healthy but unsure -> wider and smaller
        return Action(QUOTE_WIDE, bid=True, ask=True, spread_mult=cfg.wide_mult, skew_mult=skew, size_mult=0.5,
                      reason=f"quote_env {q['score']:.2f} conf {q['confidence']:.2f}")
    return Action(STAND_DOWN, reason=f"quote_env {q['score']:.2f}")


def kelly_fraction(p: float, cap: float = 0.25) -> float:
    """Fractional Kelly for an even-odds bet: (2p - 1), capped. Only use after calibration is verified."""
    return max(0.0, min(cap, 2 * p - 1))
