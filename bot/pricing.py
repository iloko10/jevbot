"""Avellaneda-Stoikov quotes. Classic market-making math, all in code.

  reservation r = mid - q * gamma * sigma^2 * (T - t)
  total spread  = gamma * sigma^2 * (T - t) + (2/gamma) * ln(1 + gamma/kappa)

q in lots (inventory / quote size), sigma in price units per sqrt(second), T - t in seconds (rolling horizon).
"""
import math
from dataclasses import dataclass


@dataclass
class Quote:
    bid: float
    ask: float
    reservation: float
    half_spread: float


def as_quote(mid, inventory, sigma, gamma, kappa, horizon_s, min_half_spread_bps,
             skew_mult=1.0, spread_mult=1.0, tick=1.0) -> Quote:
    var_t = sigma ** 2 * horizon_s
    r = mid - inventory * gamma * var_t * skew_mult
    total = gamma * var_t + (2 / gamma) * math.log(1 + gamma / kappa)
    half = max(total / 2, mid * min_half_spread_bps / 1e4) * spread_mult
    bid = math.floor((r - half) / tick) * tick
    ask = math.ceil((r + half) / tick) * tick
    return Quote(bid=bid, ask=ask, reservation=r, half_spread=half)
