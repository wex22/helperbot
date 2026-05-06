import asyncio
import io
import logging
import re
from datetime import datetime
from typing import Optional

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.types import ErrorEvent
from aiogram.types import Message
from dateutil import parser as dateparser

import gemini_client
import notion_writer as notion
import scheduler
import supabase_client as db
from config import settings
from models import Category, ClassificationResult, Entry, RawKind, Reminder, Status

logger = logging.getLogger(__name__)
router = Router()

TAG_RE = re.compile(r"#([\wа-яА-ЯёЁ\-]+)", re.UNICODE)
DONE_RE = re.compile(r"^\s*(сделал|сделано|готово|done)\b\s*(\d+)?", re.IGNORECASE)


def _is_authorized(message: Message) -> bool:
    return message.chat.id == settings.MY_CHAT_ID


@router.message(Command("start"))
async def cmd_start(message: Message) -> None:
    await message.answer(
        f"Hi. Your chat id is `{message.chat.id}`.\n"
        f"Authorized: {'yes' if _is_authorized(message) else 'no'}",
        parse_mode="Markdown",
    )


@router.message(Command("ping"))
async def cmd_ping(message: Message) -> None:
    if not _is_authorized(message):
        return
    await message.answer("pong")


@router.errors()
async def on_error(event: ErrorEvent, bot: Bot) -> None:
    logger.exception("Unhandled update error", exc_info=event.exception)
    try:
        await bot.send_message(
            settings.MY_CHAT_ID,
            f"⚠️ Ошибка обработки сообщения: {type(event.exception).__name__}: {event.exception}",
        )
    except Exception:
        logger.exception("Failed to report update error")


@router.message(Command("задачи"))
async def cmd_tasks(message: Message) -> None:
    if not _is_authorized(message):
        return
    tasks = await db.get_open_tasks()
    if not tasks:
        await message.answer("Открытых задач нет.")
        return
    lines = [
        f"[{t.id}] {t.priority.value} · {t.title or t.content[:80]}"
        for t in tasks
    ]
    await message.answer("\n".join(lines))


@router.message(Command("done"))
async def cmd_done(message: Message) -> None:
    if not _is_authorized(message):
        return
    parts = (message.text or "").split(maxsplit=1)
    closed: Optional[Entry]
    if len(parts) > 1 and parts[1].strip().isdigit():
        closed = await db.close_task(int(parts[1].strip()))
    else:
        closed = await db.close_latest_open_task()
    if closed:
        await message.answer(f"✅ Закрыта: [{closed.id}] {closed.title or closed.content[:80]}")
    else:
        await message.answer("Не нашёл открытую задачу.")


@router.message(Command("итоги"))
async def cmd_summary(message: Message) -> None:
    if not _is_authorized(message):
        return
    today = await db.get_today_entries()
    week = await db.get_week_entries()
    text = await gemini_client.summarize(today, week)
    await message.answer(text, parse_mode="Markdown")


@router.message(F.voice)
async def on_voice(message: Message, bot: Bot) -> None:
    if not _is_authorized(message):
        return
    file = await bot.get_file(message.voice.file_id)
    buf = io.BytesIO()
    await bot.download_file(file.file_path, destination=buf)
    await _process(message, raw_kind=RawKind.VOICE, content="", audio=buf.getvalue())


@router.message(F.photo)
async def on_photo(message: Message, bot: Bot) -> None:
    if not _is_authorized(message):
        return
    photo = message.photo[-1]
    file = await bot.get_file(photo.file_id)
    buf = io.BytesIO()
    await bot.download_file(file.file_path, destination=buf)
    caption = message.caption or ""
    await _process(message, raw_kind=RawKind.PHOTO, content=caption, image=buf.getvalue())


@router.message(F.text)
async def on_text(message: Message) -> None:
    if not _is_authorized(message):
        return
    text = message.text or ""
    m = DONE_RE.match(text)
    if m:
        task_id = int(m.group(2)) if m.group(2) else None
        closed = await db.close_task(task_id) if task_id else await db.close_latest_open_task()
        if closed:
            await message.answer(f"✅ Закрыта: [{closed.id}] {closed.title or closed.content[:80]}")
        else:
            await message.answer("Не нашёл открытую задачу.")
        return
    await _process(message, raw_kind=RawKind.TEXT, content=text)


async def _process(
    message: Message,
    *,
    raw_kind: RawKind,
    content: str,
    audio: Optional[bytes] = None,
    image: Optional[bytes] = None,
) -> None:
    try:
        history = await db.get_recent(20)
    except Exception:
        logger.exception("Supabase history lookup failed")
        history = []

    try:
        result: ClassificationResult = await gemini_client.classify(
            content=content, history=history, audio_bytes=audio, image_bytes=image
        )
    except Exception:
        logger.exception("Gemini classification failed")
        await message.answer("⚠️ Не получилось обработать сообщение.")
        return

    if result.is_close_task_command:
        closed = (
            await db.close_task(result.close_task_id)
            if result.close_task_id
            else await db.close_latest_open_task()
        )
        if closed:
            await message.answer(f"✅ Закрыта: [{closed.id}] {closed.title or closed.content[:80]}")
        else:
            await message.answer("Не нашёл открытую задачу.")
        return

    final_content = result.transcript or content
    if not final_content:
        final_content = result.title

    hashtags = [t.lstrip("#") for t in TAG_RE.findall(final_content)]
    tags = list(dict.fromkeys([*result.tags, *hashtags]))

    try:
        entry = await db.insert_entry(
            Entry(
                content=final_content,
                title=result.title,
                category=result.category,
                priority=result.priority,
                status=Status.OPEN,
                tags=tags,
                raw_kind=raw_kind,
            )
        )
    except Exception:
        logger.exception("Supabase insert failed")
        await message.answer(
            "⚠️ Я понял сообщение, но не смог сохранить его в Supabase.\n"
            "Проверь `SUPABASE_URL` в Render: должен быть Project URL вида "
            "`https://<project-ref>.supabase.co`.",
            parse_mode="Markdown",
        )
        return

    asyncio.create_task(notion.create_note(entry))

    reminder_msg = ""
    if result.category == Category.REMINDER:
        remind_at: Optional[datetime] = None
        if result.remind_at:
            try:
                remind_at = dateparser.isoparse(result.remind_at)
            except Exception:
                logger.warning("Bad remind_at: %r", result.remind_at)
        if remind_at or result.recurrence:
            reminder = await db.insert_reminder(
                Reminder(
                    entry_id=entry.id,
                    remind_at=remind_at,
                    recurrence=result.recurrence,
                    content=entry.title or entry.content[:120],
                )
            )
            scheduler.schedule_reminder(reminder)
            if remind_at:
                reminder_msg = f" · ⏰ {remind_at:%Y-%m-%d %H:%M}"
            elif result.recurrence:
                reminder_msg = f" · 🔁 {result.recurrence}"

    transcript_preview = ""
    if raw_kind in (RawKind.VOICE, RawKind.PHOTO) and result.transcript:
        transcript_preview = f"\n📝 _{result.transcript[:200]}_"

    await message.answer(
        f"✅ {result.category.value} · {result.priority.value} · "
        f"[{entry.id}] {entry.title}{reminder_msg}{transcript_preview}",
        parse_mode="Markdown",
    )


async def send_daily_digest(bot: Bot) -> None:
    today = await db.get_today_entries()
    week = await db.get_week_entries()
    open_tasks = await db.get_open_tasks()
    today_reminders = await db.get_reminders_firing_today()

    parts = ["☀️ *Доброе утро. Дайджест:*"]
    if open_tasks:
        parts.append("\n*Открытые задачи*")
        parts.extend(
            f"  [{t.id}] {t.priority.value} · {t.title or t.content[:80]}" for t in open_tasks[:15]
        )
    if today_reminders:
        parts.append("\n*Напоминания на сегодня*")
        parts.extend(
            f"  ⏰ {r.remind_at:%H:%M} — {r.content or ''}" for r in today_reminders if r.remind_at
        )
    try:
        summary = await gemini_client.summarize(today, week)
        parts.append("\n" + summary)
    except Exception:
        logger.exception("Digest summary failed")

    await bot.send_message(settings.MY_CHAT_ID, "\n".join(parts), parse_mode="Markdown")
