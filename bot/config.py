"""All settings in one place. Every value can be changed with an environment variable."""
import os
from dataclasses import dataclass, field


def _f(name, default):
    return float(os.getenv(name, default))


def _i(name, default):
    return int(os.getenv(name, default))


def _b(name, default):
    return os.getenv(name, str(default)).lower() in ("1", "true", "yes")


@dataclass
class Limits:
    """Hard risk limits. The risk engine checks these before every order. Jev never touches them."""
    max_position: float = field(default_factory=lambda: _f("MAX_POSITION", 0.02))        # coin units (BTC)
    max_order_size: float = field(default_factory=lambda: _f("MAX_ORDER_SIZE", 0.005))
    max_daily_loss: float = field(default_factory=lambda: _f("MAX_DAILY_LOSS", 50))      # USD
    max_drawdown: float = field(default_factory=lambda: _f("MAX_DRAWDOWN", 75))          # USD from peak equity
    max_inventory_age_s: float = field(default_factory=lambda: _f("MAX_INVENTORY_AGE_S", 600))
    max_stale_data_s: float = field(default_factory=lambda: _f("MAX_STALE_DATA_S", 3))
    max_decision_latency_ms: float = field(default_factory=lambda: _f("MAX_DECISION_LATENCY_MS", 800))
    max_api_errors: int = field(default_factory=lambda: _i("MAX_API_ERRORS", 20))       # Jev errors in a row before it is declared "down" (stub-only mode)


@dataclass
class Config:
    coin: str = field(default_factory=lambda: os.getenv("COIN", "BTC"))
    ws_url: str = field(default_factory=lambda: os.getenv("HL_WS_URL", "wss://api.hyperliquid.xyz/ws"))

    # Loop speed. One decision per tick. 1000 ms = ~86k Jev calls/day.
    tick_ms: int = field(default_factory=lambda: _i("TICK_MS", 1000))

    # Jev
    typesafe_api_key: str = field(default_factory=lambda: os.getenv("TYPESAFE_API_KEY", ""))
    typesafe_url: str = field(default_factory=lambda: os.getenv("TYPESAFE_URL", "https://api.typesafe.ai/v1/systemone"))
    jev_model: str = field(default_factory=lambda: os.getenv("JEV_MODEL", "jev-1.13.0"))  # pinned, never "latest" in prod
    jev_timeout_ms: int = field(default_factory=lambda: _i("JEV_TIMEOUT_MS", 700))

    # Avellaneda-Stoikov pricing
    # Units: inventory in LOTS (inventory / QUOTE_SIZE), sigma in USD per sqrt(second).
    # With BTC ~0.8 bps/s vol these defaults give ~2 bps half-spread and ~0.6 bps skew per lot held.
    gamma: float = field(default_factory=lambda: _f("AS_GAMMA", 0.01))          # risk aversion
    kappa: float = field(default_factory=lambda: _f("AS_KAPPA", 1.5))           # order arrival intensity
    horizon_s: float = field(default_factory=lambda: _f("AS_HORIZON_S", 10))    # T - t, rolling
    min_half_spread_bps: float = field(default_factory=lambda: _f("MIN_HALF_SPREAD_BPS", 2.0))
    wide_mult: float = field(default_factory=lambda: _f("WIDE_MULT", 2.0))

    # Paper exchange
    quote_size: float = field(default_factory=lambda: _f("QUOTE_SIZE", 0.002))
    maker_fee_bps: float = field(default_factory=lambda: _f("MAKER_FEE_BPS", 1.5))
    fill_on_touch: bool = field(default_factory=lambda: _b("FILL_ON_TOUCH", False))  # False = only fill when price trades THROUGH us
    min_rest_ms: int = field(default_factory=lambda: _i("MIN_REST_MS", 2000))  # don't cancel/replace faster than this unless price moved
    requote_bps: float = field(default_factory=lambda: _f("REQUOTE_BPS", 1.0))

    # Policy thresholds (one per action, edit here, not in prompts)
    toxic_pull: float = field(default_factory=lambda: _f("TH_TOXIC_PULL", 0.6))
    stress_widen: float = field(default_factory=lambda: _f("TH_STRESS_WIDEN", 0.7))
    quote_score_min: float = field(default_factory=lambda: _f("TH_QUOTE_SCORE", 2.0))
    quote_conf_min: float = field(default_factory=lambda: _f("TH_QUOTE_CONF", 0.8))
    wide_score_min: float = field(default_factory=lambda: _f("TH_WIDE_SCORE", 1.0))

    db_path: str = field(default_factory=lambda: os.getenv("DB_PATH", "data/jevbot.db"))
    port: int = field(default_factory=lambda: _i("PORT", 8080))

    limits: Limits = field(default_factory=Limits)

    @property
    def use_jev(self) -> bool:
        return bool(self.typesafe_api_key)
