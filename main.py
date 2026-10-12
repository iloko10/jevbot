"""Start the bot: market feed + 24/7 decision loop + dashboard."""
import asyncio
import logging
import threading

from bot.config import Config
from bot.dashboard import serve
from bot.engine import Engine
from bot.feed import HyperliquidFeed, MarketData
from bot.judge import JevJudge, StubJudge
from bot.paper import PaperExchange
from bot.store import Store

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("main")


async def main():
    cfg = Config()
    md = MarketData()
    store = Store(cfg.db_path)
    if cfg.use_jev:
        judge = JevJudge(cfg.typesafe_api_key, cfg.typesafe_url, cfg.jev_model, cfg.jev_timeout_ms)
        log.info("judge: Jev %s (pinned)", cfg.jev_model)
    else:
        judge = StubJudge()
        log.warning("judge: STUB RULES (set TYPESAFE_API_KEY to use Jev)")
    engine = Engine(cfg, md, PaperExchange(cfg), store, judge)
    store.event("start", f"judge={'jev ' + cfg.jev_model if cfg.use_jev else 'stub'} coin={cfg.coin}")

    threading.Thread(target=serve, args=(engine, cfg), daemon=True).start()
    log.info("dashboard on port %s", cfg.port)

    feed = HyperliquidFeed(cfg.ws_url, cfg.coin, md)
    await asyncio.gather(feed.run(), engine.run())


if __name__ == "__main__":
    asyncio.run(main())
