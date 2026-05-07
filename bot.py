import asyncio
import logging
from pathlib import Path

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import MenuButtonWebApp, WebAppInfo
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiohttp import web

import handlers
import notion_writer as notion
import scheduler
import supabase_client as db
from config import settings
from models import Category, Entry, Priority, RawKind, Status

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)
APP_VERSION = "2026-05-06-webhook-fix"


async def health(_request: web.Request) -> web.Response:
    return web.Response(text="ok")


async def version(_request: web.Request) -> web.Response:
    return web.Response(text=APP_VERSION)


# ── Mini App routes ────────────────────────────────────────────────────────────

def _check_token(request: web.Request) -> bool:
    return request.query.get("token") == settings.WEBHOOK_SECRET


async def serve_app(request: web.Request) -> web.Response:
    if not _check_token(request):
        return web.Response(text="Forbidden", status=403)
    html_path = Path(__file__).parent / "static" / "app.html"
    try:
        html = html_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return web.Response(text="App not found", status=404)
    base_url = settings.WEBHOOK_URL.rstrip("/")
    html = html.replace("__TOKEN__", settings.WEBHOOK_SECRET)
    html = html.replace("__BASE_URL__", base_url)
    return web.Response(text=html, content_type="text/html", charset="utf-8")


async def api_tasks(request: web.Request) -> web.Response:
    if not _check_token(request):
        return web.json_response({"error": "forbidden"}, status=403)
    try:
        entries = await db.get_all_open()
        data = [
            {
                "id": e.id,
                "title": e.title or e.content[:80],
                "category": e.category.value,
                "priority": e.priority.value,
                "created_at": e.created_at.isoformat() if e.created_at else None,
            }
            for e in entries
        ]
        return web.json_response(data)
    except Exception:
        logger.exception("api_tasks failed")
        return web.json_response({"error": "db error"}, status=500)


def _entry_dict(e: Entry) -> dict:
    return {
        "id": e.id,
        "title": e.title or (e.content[:80] if e.content else "(без текста)"),
        "content": e.content,
        "category": e.category.value,
        "priority": e.priority.value,
        "status": e.status.value,
        "tags": e.tags,
        "created_at": e.created_at.isoformat() if e.created_at else None,
        "due_date": e.due_date.isoformat() if e.due_date else None,
    }


async def api_entries(request: web.Request) -> web.Response:
    if not _check_token(request):
        return web.json_response({"error": "forbidden"}, status=403)
    try:
        category = request.query.get("category") or None
        status = request.query.get("status") or "open"
        q = (request.query.get("q") or "").strip() or None
        limit = int(request.query.get("limit") or 200)
        entries = await db.list_entries(category=category, status=status, q=q, limit=limit)
        return web.json_response([_entry_dict(e) for e in entries])
    except Exception:
        logger.exception("api_entries failed")
        return web.json_response({"error": "db error"}, status=500)


async def api_reminders(request: web.Request) -> web.Response:
    if not _check_token(request):
        return web.json_response({"error": "forbidden"}, status=403)
    try:
        rems = await db.get_pending_reminders()
        data = [
            {
                "id": r.id,
                "entry_id": r.entry_id,
                "content": r.content or "",
                "remind_at": r.remind_at.isoformat() if r.remind_at else None,
                "recurrence": r.recurrence,
            }
            for r in rems
        ]
        # Sort: time first (asc), recurring last
        data.sort(key=lambda x: (x["remind_at"] is None, x["remind_at"] or ""))
        return web.json_response(data)
    except Exception:
        logger.exception("api_reminders failed")
        return web.json_response({"error": "db error"}, status=500)


async def api_reminder_cancel(request: web.Request) -> web.Response:
    if not _check_token(request):
        return web.json_response({"error": "forbidden"}, status=403)
    try:
        rem_id = int(request.match_info["id"])
        await db.mark_reminder_fired(rem_id)
        scheduler.cancel_reminder(rem_id)
        return web.json_response({"ok": True})
    except Exception:
        logger.exception("api_reminder_cancel failed")
        return web.json_response({"error": "error"}, status=500)


async def api_reminder_snooze(request: web.Request) -> web.Response:
    if not _check_token(request):
        return web.json_response({"error": "forbidden"}, status=403)
    try:
        rem_id = int(request.match_info["id"])
        body = await request.json()
        minutes = int(body.get("minutes", 10))
        new_at = await scheduler.snooze_reminder(rem_id, minutes)
        if new_at:
            return web.json_response({"ok": True, "remind_at": new_at.isoformat()})
        return web.json_response({"error": "not found"}, status=404)
    except Exception:
        logger.exception("api_reminder_snooze failed")
        return web.json_response({"error": "error"}, status=500)


async def api_edit(request: web.Request) -> web.Response:
    if not _check_token(request):
        return web.json_response({"error": "forbidden"}, status=403)
    try:
        entry_id = int(request.match_info["id"])
        body = await request.json()
        allowed = {"title", "content", "priority", "category", "status", "tags"}
        fields = {k: v for k, v in body.items() if k in allowed and v is not None}
        # validate enums
        if "priority" in fields:
            fields["priority"] = Priority(fields["priority"]).value
        if "category" in fields:
            fields["category"] = Category(fields["category"]).value
        if "status" in fields:
            fields["status"] = Status(fields["status"]).value
        updated = await db.update_entry(entry_id, fields)
        if not updated:
            return web.json_response({"error": "not found"}, status=404)
        return web.json_response({"ok": True, "entry": _entry_dict(updated)})
    except Exception:
        logger.exception("api_edit failed")
        return web.json_response({"error": "error"}, status=500)


async def api_stats(request: web.Request) -> web.Response:
    if not _check_token(request):
        return web.json_response({"error": "forbidden"}, status=403)
    try:
        s = await db.get_stats()
        return web.json_response(s)
    except Exception:
        logger.exception("api_stats failed")
        return web.json_response({"error": "error"}, status=500)


async def api_close(request: web.Request) -> web.Response:
    if not _check_token(request):
        return web.json_response({"error": "forbidden"}, status=403)
    try:
        entry_id = int(request.match_info["id"])
        await db.close_entry(entry_id)
        return web.json_response({"ok": True})
    except Exception:
        logger.exception("api_close failed")
        return web.json_response({"error": "error"}, status=500)


async def api_delete(request: web.Request) -> web.Response:
    if not _check_token(request):
        return web.json_response({"error": "forbidden"}, status=403)
    try:
        entry_id = int(request.match_info["id"])
        await db.delete_entry(entry_id)
        return web.json_response({"ok": True})
    except Exception:
        logger.exception("api_delete failed")
        return web.json_response({"error": "error"}, status=500)


async def api_add(request: web.Request) -> web.Response:
    if not _check_token(request):
        return web.json_response({"error": "forbidden"}, status=403)
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "bad json"}, status=400)

    content = (body.get("content") or "").strip()
    if not content:
        return web.json_response({"error": "empty content"}, status=400)

    try:
        priority = Priority(body.get("priority", "normal"))
    except ValueError:
        priority = Priority.NORMAL

    try:
        category = Category(body.get("category", "task"))
    except ValueError:
        category = Category.TASK

    title = (body.get("title") or content[:80]).strip()[:80]
    remind_at_str = body.get("remind_at")  # ISO string, optional
    recurrence = body.get("recurrence")     # cron string, optional

    try:
        entry = await db.insert_entry(Entry(
            content=content,
            title=title,
            category=category,
            priority=priority,
            status=Status.OPEN,
            tags=[],
            raw_kind=RawKind.TEXT,
            source="miniapp",
        ))
        asyncio.create_task(notion.create_note(entry))

        rem_id = None
        if remind_at_str or recurrence:
            from datetime import datetime as _dt
            from models import Reminder
            remind_at = None
            if remind_at_str:
                try:
                    remind_at = _dt.fromisoformat(remind_at_str.replace("Z", "+00:00"))
                except Exception:
                    pass
            if remind_at or recurrence:
                rem = await db.insert_reminder(Reminder(
                    entry_id=entry.id,
                    remind_at=remind_at,
                    recurrence=recurrence,
                    content=title or content[:120],
                ))
                scheduler.schedule_reminder(rem)
                rem_id = rem.id

        return web.json_response({"ok": True, "id": entry.id, "reminder_id": rem_id})
    except Exception:
        logger.exception("api_add failed")
        return web.json_response({"error": "db error"}, status=500)


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
        app_url = f"{settings.WEBHOOK_URL.rstrip('/')}/app?token={settings.WEBHOOK_SECRET}"
        await bot.set_chat_menu_button(
            menu_button=MenuButtonWebApp(text="📋 Задачи", web_app=WebAppInfo(url=app_url))
        )
        logger.info("Menu button set to %s", app_url)
    except Exception as e:
        logger.warning("Failed to set menu button: %s", e)

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
        scheduler.schedule_weekly_report(
            lambda: asyncio.create_task(handlers.send_weekly_report(bot))
        )
        scheduler.schedule_overdue_check(
            lambda: asyncio.create_task(handlers.check_overdue_tasks(bot))
        )
        logger.info("Scheduled: daily digest, weekly report, overdue check. TZ=%s", settings.TZ)
    except Exception as e:
        logger.error("Schedule setup failed (non-fatal): %s", e)


async def on_startup(bot: Bot) -> None:
    # Fire-and-forget: server binds to port immediately, init happens in background
    asyncio.create_task(_background_init(bot))


async def on_shutdown(bot: Bot) -> None:
    logger.info("Shutdown: keeping Telegram webhook registered")


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
    app.router.add_get("/version", version)
    app.router.add_get("/", health)

    # Mini App
    app.router.add_get("/app", serve_app)
    app.router.add_get("/api/tasks", api_tasks)
    app.router.add_get("/api/entries", api_entries)
    app.router.add_get("/api/reminders", api_reminders)
    app.router.add_get("/api/stats", api_stats)
    app.router.add_post("/api/close/{id}", api_close)
    app.router.add_post("/api/delete/{id}", api_delete)
    app.router.add_post("/api/edit/{id}", api_edit)
    app.router.add_post("/api/add", api_add)
    app.router.add_post("/api/reminder/cancel/{id}", api_reminder_cancel)
    app.router.add_post("/api/reminder/snooze/{id}", api_reminder_snooze)

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
