# Jev Paper Market-Maker (Hyperliquid BTC)

A 24/7 market-making bot built from the "How to Use Jev to Build a 24/7 HFT Trading System" blueprint.
It runs on **real live Hyperliquid data** but trades **fake money** (paper trading).

## How it works (simple version)

Every second:

1. **Read the market** – live BTC order book and trades from Hyperliquid (free, no account needed).
2. **Make a snapshot** – plain code turns that into ~30 numbers (price, spread, flow, volatility, your position).
3. **Ask 6 questions in one call** – regime? direction? is the flow dangerous? is the book thin? is it a good time to quote? should I cut my position?
   - With a TypeSafe key: Jev answers.
   - Without a key: simple built-in rules answer, in exactly the same format.
4. **Policy** – your rules turn the answers into one action (quote, quote wide, pull quotes, reduce only, stand down).
5. **Pricing** – Avellaneda-Stoikov math sets the bid and ask.
6. **Risk check** – hard limits always win (max position, daily loss, drawdown, stale data, slow decision).
7. **Paper orders** – post-only quotes; they "fill" when a real trade goes through your price.
8. **Log everything** – so you can check later whether the answers were actually right.

Safety ladder: slow decision → cancel quotes and wait. Jev errors → use the rules, quote wider and smaller.
Limit broken → close everything and stop (kill switch).

## Files

| File | What it does |
|---|---|
| `bot/feed.py` | Hyperliquid WebSocket, reconnects forever |
| `bot/state.py` | Snapshot (only uses past data) |
| `bot/judge.py` | The 6 questions; Jev client + stand-in rules |
| `bot/policy.py` | Answers → action. **Thresholds live in `config.py`** |
| `bot/pricing.py` | Avellaneda-Stoikov bid/ask |
| `bot/risk.py` | Hard limits |
| `bot/paper.py` | Fake exchange: fills, fees, PnL |
| `bot/engine.py` | The loop + safety ladder |
| `bot/dashboard.py` | Web page with PnL, fills, latest answers |
| `calibrate.py` | Checks if the answers are trustworthy |
| `simulate.py` | Offline test on a fake market |

## Run on your computer

```bash
pip install -r requirements-dev.txt
pytest                      # 12 tests
python simulate.py 4        # 4 fake hours, offline
python calibrate.py data/sim.db
python main.py              # LIVE data, paper money -> open http://localhost:8080
```

## Put it on Railway

1. Push this folder to a GitHub repo, then in Railway: **New Project → Deploy from GitHub repo**.
2. Add a **Volume** mounted at `/data` (so logs survive redeploys).
3. In **Variables** set `DB_PATH=/data/jevbot.db` (see `.env.example` for the rest).
4. In **Settings → Networking**, generate a domain. That's your dashboard.

`railway.json` already sets the start command, health check (`/healthz`) and auto-restart.

## Switching to Jev

When TypeSafe approves you, add `TYPESAFE_API_KEY` in Railway Variables. Nothing else changes.

- The model is **pinned** (`JEV_MODEL=jev-1.13.0`). Don't use `jev-latest`: your thresholds are tuned to one version.
- Cost: each call is roughly 700 tokens. At $0.042 per million tokens that is about **$75/month at 1 tick/second**,
  $37 at 2 s, $15 at 5 s (`TICK_MS`). Higher than the article's "$10–25" claim.
- Want to try before the waitlist? Jev is also on Vercel AI Gateway (`typesafe-ai/jev`), but that adds latency
  and needs a small change in `bot/judge.py`.

## Honest notes (read these)

- **Fees decide everything.** In the 4-hour fake-market test the bot earned about +$11 from spreads but paid $61
  in fees (1.5 bps per maker fill) and hit the $50 daily kill switch. Real data will differ, but this is the
  main thing to watch on the dashboard.
- **The stand-in rules have no edge.** On fake data their "is the flow dangerous" answer was *worse* than guessing
  a constant. That's expected. The point of running them now is to collect data and have a baseline to beat.
- **Compare before you trust.** Run 1–2 weeks on stub rules, then 1–2 weeks with Jev, and compare with
  `calibrate.py` (Brier score, ECE, markout) and the PnL. Jev only matters if it beats the rules.
- **Paper fills are approximate.** By default a quote only fills if price trades *through* it (we assume we're last
  in the queue). Real fills can be better or worse.
- **No real-money trading code yet.** Going live needs a Hyperliquid API wallet, order signing, and real order
  tracking. Only add that after paper results are clearly positive *after fees*.
- The AgenKit / "DM me" parts of the article are ads and aren't needed.
