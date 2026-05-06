import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiohttp import web

import handlers
import scheduler
from config import settings

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)


async def health(_request: web.Request) -> web.Response:
    return web.Response(text="ok")


async def on_startup(bot: Bot) -> None:
    webhook_url = settings.WEBHOOK_URL.rstrip("/") + settings.WEBHOOK_PATH
    logger.info("Setting webhook to %s", webhook_url)

    # Retry webhook setup — Render sometimes has brief DNS lag on cold start
    for attempt in range(5):
        try:
            await bot.set_webhook(
                url=webhook_url,
                secret_token=settings.WEBHOOK_SECRET,
                drop_pending_updates=True,
            )
            logger.info("Webhook set successfully")
            break
        except Exception as e:
            logger.warning("Webhook attempt %d failed: %s", attempt + 1, e)
            if attempt < 4:
                await asyncio.sleep(3)
            else:
                logger.error("Could not set webhook after 5 attempts — bot may not receive messages")

    scheduler.init(bot)

    try:
        await scheduler.rehydrate()
    except Exception as e:
        logger.error("Reminder rehydration failed (non-fatal): %s", e)

    try:
        scheduler.schedule_daily_digest(
            lambda: asyncio.create_task(handlers.send_daily_digest(bot))
        )
    except Exception as e:
        logger.error("Daily digest scheduling failed (non-fatal): %s", e)

    logger.info("Bot started. TZ=%s", settings.TZ)


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
