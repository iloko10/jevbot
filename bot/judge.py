"""The judgment layer. Six atomic questions about the snapshot, asked in ONE call.

- JevJudge:  the real thing, POST https://api.typesafe.ai/v1/systemone (needs TYPESAFE_API_KEY)
- StubJudge: simple hand-written rules that return answers in the SAME shape as Jev.
             Used when you have no key yet, and as the fallback when Jev is down.

Both return: {"answers": {qid: {...}}, "model": str, "source": "jev"|"stub", "latency_ms": float}
Answer shapes match the TypeSafe API exactly:
  noul   -> {"type": "noul", "noul": 0.83}
  choice -> {"type": "choice", "choice": "up", "probabilities": {...}, "confidence": 0.7}
  score  -> {"type": "score", "score": 2.3, "probabilities": {"0":..}, "confidence": 0.85}
"""
import time

import httpx

# The battery. Raw TypeSafe API format (docs.typesafe.ai/api). Edit questions here.
BATTERY = {
    "regime": {
        "type": "choice",
        "instructions": "Which market regime does this order-book state describe right now?",
        "criteria": {
            "trending": "Price moving persistently in one direction (`price.ret_5m_bps` large vs volatility)",
            "mean_reverting": "Price oscillating around a level, no persistent direction",
            "high_vol": "Volatility well above its recent norm (`vol.rv_ratio_1m_5m` high)",
            "crisis": "Disorderly: very wide spread, thin book, violent moves",
        },
    },
    "direction": {
        "type": "choice",
        "instructions": "What is the most likely direction of the mid price over the next 30 seconds?",
        "criteria": {"up": None, "down": None, "neutral": "Moves less than the current spread"},
    },
    "toxic_flow": {
        "type": "noul",
        "instructions": "Is the recent aggressive flow (`flow_60s`) likely informed, i.e. will price keep moving in its direction?",
        "criteria": {"true": "Informed, one-sided flow likely to run over resting quotes",
                     "false": "Balanced or noise flow"},
    },
    "liquidity_stressed": {
        "type": "noul",
        "instructions": "Is the order book unusually thin or the spread unusually wide for a liquid BTC perpetual?",
    },
    "quote_environment": {
        "type": "score",
        "instructions": "How favorable is this state for a market maker to rest passive quotes on both sides?",
        "criteria": ["Do not quote", "Marginal", "Standard", "Excellent"],
    },
    "inventory_pressure": {
        "type": "score",
        "instructions": "Given `position` and current conditions, how urgent is it to reduce inventory?",
        "criteria": ["None", "Mild", "Skew hard", "Reduce now"],
    },
}


class JevError(Exception):
    pass


class JevJudge:
    def __init__(self, api_key: str, url: str, model: str, timeout_ms: int):
        self.url, self.model = url, model
        self.client = httpx.AsyncClient(
            timeout=timeout_ms / 1000,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            http2=False,
        )

    async def judge(self, snapshot: dict) -> dict:
        t0 = time.perf_counter()
        try:
            r = await self.client.post(self.url, json={"model": self.model, "state": snapshot, "questions": BATTERY})
        except httpx.HTTPError as e:
            raise JevError(f"network: {e!r}") from e
        latency = (time.perf_counter() - t0) * 1000
        if r.status_code != 200:
            raise JevError(f"HTTP {r.status_code}: {r.text[:200]}")
        body = r.json()
        return {"answers": body["answers"], "model": body.get("model", self.model), "source": "jev",
                "latency_ms": latency, "usage": body.get("usage", {})}

    async def close(self):
        await self.client.aclose()


def _clip(x, lo=0.0, hi=1.0):
    return max(lo, min(hi, x))


def _norm(d):
    s = sum(d.values()) or 1.0
    return {k: v / s for k, v in d.items()}


def _choice(probs):
    probs = _norm(probs)
    best = max(probs, key=probs.get)
    return {"type": "choice", "choice": best, "probabilities": probs, "confidence": probs[best]}


def _score(value, n_levels, confidence):
    value = _clip(value, 0, n_levels - 1)
    lo = int(value)
    hi = min(lo + 1, n_levels - 1)
    probs = {str(i): 0.0 for i in range(n_levels)}
    probs[str(lo)] += 1 - (value - lo)
    probs[str(hi)] += value - lo
    return {"type": "score", "score": value, "probabilities": probs, "confidence": confidence}


class StubJudge:
    """Hand-written rules. Same output shape as Jev. These are BASELINE rules, not an edge."""

    async def judge(self, snapshot: dict) -> dict:
        t0 = time.perf_counter()
        p, b, f, v, pos = (snapshot[k] for k in ("price", "book", "flow_60s", "vol", "position"))

        one_sided = abs(f["aggr_buy_ratio"] - 0.5) * 2                     # 0..1
        move = _clip(abs(p["ret_1m_bps"]) / 5)                              # 5 bps in a minute = big for BTC
        same_way = (f["aggr_buy_ratio"] > 0.5) == (p["ret_1m_bps"] > 0)
        toxic = _clip(one_sided * move * (1.0 if same_way else 0.5))

        stress = _clip((b["spread_bps"] - 1.0) / 4)                         # BTC on HL is usually ~0.1-1 bps

        ratio = v["rv_ratio_1m_5m"]
        trend = _clip(abs(p["ret_5m_bps"]) / (3 * max(v["rv_5m_bps_per_s"] * 17.3, 0.5)))  # 17.3 = sqrt(300)
        regime = _choice({
            "trending": 0.2 + trend,
            "mean_reverting": 0.2 + (1 - trend) * 0.6,
            "high_vol": 0.1 + _clip(ratio - 1.5),
            "crisis": 0.02 + stress * 0.5 * _clip(ratio - 2),
        })

        tilt = _clip(0.5 * b["imbalance"] + 0.1 * p["microprice_vs_mid_bps"], -0.5, 0.5)
        direction = _choice({"up": 0.33 + max(tilt, 0), "down": 0.33 + max(-tilt, 0), "neutral": 0.34})

        quote = 3 * (1 - max(toxic, stress, regime["probabilities"]["crisis"]))
        quote_env = _score(quote, 4, confidence=0.85 if max(toxic, stress) < 0.3 else 0.6)

        inv = abs(pos["inventory_vs_max"]) * 3 + _clip(pos["position_age_s"] / 600)
        inv_p = _score(inv, 4, confidence=0.9)

        answers = {
            "regime": regime, "direction": direction,
            "toxic_flow": {"type": "noul", "noul": toxic},
            "liquidity_stressed": {"type": "noul", "noul": stress},
            "quote_environment": quote_env, "inventory_pressure": inv_p,
        }
        return {"answers": answers, "model": "stub-rules-v1", "source": "stub",
                "latency_ms": (time.perf_counter() - t0) * 1000, "usage": {}}

    async def close(self):
        pass
