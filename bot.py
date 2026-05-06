import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiohttp import web

import handlers
import scheduler
import supabase_client as db
from config import settings

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)


async def health(_request: web.Request) -> web.Response:
    return web.Response(text="ok")


async def _background_init(bot: Bot) -> None:
    """All network-dependent startup — runs after server is bound to port."""
    # Give the event loop a moment before hitting external APIs
    await asyncio.sleep(2)

    webhook_url = settings.WEBHOOK_URL.rstrip("/") + settings.WEBHOOK_PATH
    for attempt in range(5):
        try:
            await bot.set_webhook(
                url=webhook_url,
                secret_token=settings.WEBHOOK_SECRET,
                drop_pending_updates=True,
            )
            logger.info("Webhook set to %s", webhook_url)
            break
        except Exception as e:
            logger.warning("Webhook attempt %d/5 failed: %s", attempt + 1, e)
            await asyncio.sleep(4)

    scheduler.init(bot)

    try:
        await db.check_connection()
        logger.info("Supabase reachable: %s", db.supabase_host())
        await scheduler.rehydrate()
        logger.info("Reminders rehydrated")
    except Exception as e:
        logger.error("Supabase startup check/rehydrate failed (non-fatal): %s", e)

    try:
        scheduler.schedule_daily_digest(
            lambda: asyncio.create_task(handlers.send_daily_digest(bot))
        )
        logger.info("Daily digest scheduled. TZ=%s", settings.TZ)
    except Exception as e:
        logger.error("Digest schedule failed (non-fatal): %s", e)


async def on_startup(bot: Bot) -> None:
    # Fire-and-forget: server binds to port immediately, init happens in background
    asyncio.create_task(_background_init(bot))


async def on_shutdown(bot: Bot) -> None:
    try:
        await bot.delete_webhook()
    except Exception:
        pass


def build_app() -> web.Application:
    bot = Bot(
        token=settings.BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher()
    dp.include_router(handlers.router)
    dp.startup.register(on_startup)
    dp.shutdown.register(on_shutdown)

    app = web.Application()
    app.router.add_get("/health", health)
    app.router.add_get("/", health)

    SimpleRequestHandler(
        dispatcher=dp,
        bot=bot,
        secret_token=settings.WEBHOOK_SECRET,
    ).register(app, path=settings.WEBHOOK_PATH)
    setup_application(app, dp, bot=bot)
    return app


def main() -> None:
    app = build_app()
    web.run_app(app, host="0.0.0.0", port=settings.PORT)


if __name__ == "__main__":
    main()
