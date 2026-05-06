import asyncio
import io
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Optional

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.types import (
    ErrorEvent,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    WebAppInfo,
)
from dateutil import parser as dateparser

import gemini_client
import notion_writer as notion
import scheduler
import supabase_client as db
from config import settings
from models import Category, ClassificationResult, Entry, RawKind, Reminder, ReminderSpec, Status

logger = logging.getLogger(__name__)
router = Router()

TAG_RE = re.compile(r"#([\wа-яА-ЯёЁ\-]+)", re.UNICODE)
DONE_RE = re.compile(r"^\s*(сделал|сделано|готово|выполнено|done)\b\s*(\d+)?", re.IGNORECASE)


def _md(s: str) -> str:
    """Escape Markdown special chars so Telegram doesn't choke on titles."""
    return (s or "").replace("_", "\\_").replace("*", "\\*").replace("[", "\\[").replace("`", "\\`")

# Short-term conversation memory (last 8 messages = 4 exchanges)
_chat_buffer: list[dict] = []
_MAX_BUFFER = 8


def _buf_add(role: str, text: str) -> None:
    _chat_buffer.append({"role": role, "text": text[:300]})
    if len(_chat_buffer) > _MAX_BUFFER:
        _chat_buffer.pop(0)


def _is_authorized(message: Message) -> bool:
    return message.chat.id == settings.MY_CHAT_ID


@router.message(Command("start"))
async def cmd_start(message: Message) -> None:
    await message.answer(
        f"Привет! Я твой личный ассистент.\n\n"
        f"Просто пиши мне что угодно — задачи, идеи, напоминания, вопросы.\n"
        f"Или отправь голосовое / фото.\n\n"
        f"Команды:\n"
        f"/меню — визуальный менеджер задач\n"
        f"/задачи — открытые задачи\n"
        f"/анализ — найти повторы и паттерны\n"
        f"/итоги — сводка за неделю\n"
        f"/поиск <слово> — поиск по записям\n"
        f"/стат — твоя статистика\n"
        f"/экспорт — все задачи текстом\n"
        f"/done [id] — закрыть задачу\n\n"
        f"Chat id: `{message.chat.id}`",
        parse_mode="Markdown",
    )


@router.message(Command("меню"))
async def cmd_menu(message: Message) -> None:
    if not _is_authorized(message):
        return
    url = f"{settings.WEBHOOK_URL.rstrip('/')}/app?token={settings.WEBHOOK_SECRET}"
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="📋 Открыть задачи", web_app=WebAppInfo(url=url))
    ]])
    await message.answer("Менеджер задач:", reply_markup=kb)


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
            f"⚠️ Ошибка: {type(event.exception).__name__}: {event.exception}",
        )
    except Exception:
        logger.exception("Failed to report update error")


@router.message(Command("задачи"))
async def cmd_tasks(message: Message) -> None:
    if not _is_authorized(message):
        return
    tasks = await db.get_open_tasks()
    if not tasks:
        await message.answer("Открытых задач нет. Всё сделано 💪")
        return
    lines = [f"📋 *Открытые задачи ({len(tasks)}):*\n"]
    for t in tasks:
        age = ""
        if t.created_at:
            days = (datetime.now(timezone.utc) - t.created_at).days
            if days > 0:
                age = f" _{days}д_"
        priority_icon = {"urgent": "🔴", "normal": "🟡", "someday": "⚪"}.get(t.priority.value, "")
        lines.append(f"{priority_icon} [{t.id}] {_md(t.title or t.content[:80])}{age}")
    await message.answer("\n".join(lines), parse_mode="Markdown")


@router.message(Command("done"))
async def cmd_done(message: Message) -> None:
    if not _is_authorized(message):
        return
    parts = (message.text or "").split(maxsplit=1)
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


@router.message(Command("поиск"))
async def cmd_search(message: Message) -> None:
    if not _is_authorized(message):
        return
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        await message.answer("Использование: /поиск <слово>")
        return
    query = parts[1].strip()
    results = await db.search_entries(query)
    if not results:
        await message.answer(f"По запросу «{query}» ничего не найдено.")
        return
    lines = [f"🔍 *Результаты по «{query}»:*\n"]
    for e in results:
        date_str = e.created_at.strftime("%d.%m") if e.created_at else ""
        lines.append(f"[{e.id}] {e.category.value} · {_md(e.title or e.content[:80])} _{date_str}_")
    await message.answer("\n".join(lines), parse_mode="Markdown")


@router.message(Command("стат"))
async def cmd_stats(message: Message) -> None:
    if not _is_authorized(message):
        return
    stats = await db.get_stats()
    cat_icons = {"task": "📋", "thought": "💭", "idea": "💡", "reminder": "⏰", "note": "📝"}
    lines = [f"📊 *Статистика:*\n", f"Всего записей: *{stats['total']}*\n"]
    for cat, count in sorted(stats["by_category"].items(), key=lambda x: -x[1]):
        icon = cat_icons.get(cat, "•")
        lines.append(f"{icon} {cat}: {count}")
    lines.append(f"\n✅ Задач закрыто: {stats['done_tasks']}")
    lines.append(f"🔓 Задач открыто: {stats['open_tasks']}")
    await message.answer("\n".join(lines), parse_mode="Markdown")


@router.message(Command("анализ"))
async def cmd_analyze(message: Message) -> None:
    if not _is_authorized(message):
        return
    await message.answer("🔍 Анализирую задачи...")
    all_tasks = await db.get_open_tasks()
    done = await db.get_week_entries()
    all_entries = list({e.id: e for e in [*all_tasks, *done]}.values())
    if not all_entries:
        await message.answer("Нет задач для анализа. Сначала добавь несколько.")
        return
    try:
        result = await gemini_client.analyze_tasks(all_entries)
        await message.answer(result, parse_mode="Markdown")
    except Exception:
        logger.exception("Analyze failed")
        await message.answer("⚠️ Не удалось проанализировать.")


@router.message(Command("экспорт"))
async def cmd_export(message: Message) -> None:
    if not _is_authorized(message):
        return
    tasks = await db.get_open_tasks()
    if not tasks:
        await message.answer("Открытых задач нет.")
        return
    lines = ["ОТКРЫТЫЕ ЗАДАЧИ\n"]
    for t in tasks:
        date_str = t.created_at.strftime("%Y-%m-%d") if t.created_at else ""
        lines.append(f"[{t.id}] [{t.priority.value.upper()}] {t.title or t.content[:120]}  ({date_str})")
    await message.answer("```\n" + "\n".join(lines) + "\n```", parse_mode="Markdown")


@router.channel_post(F.text)
async def on_channel_text(message: Message, bot: Bot) -> None:
    # Discovery: if CHANNEL_ID not set, report which channel the bot was added to
    if settings.CHANNEL_ID == 0:
        await bot.send_message(
            settings.MY_CHAT_ID,
            f"📡 Бот добавлен в канал!\n"
            f"Название: {message.chat.title}\n"
            f"ID канала: `{message.chat.id}`\n\n"
            f"Скинь этот ID — подключу канал.",
            parse_mode="Markdown",
        )
        return
    if message.chat.id != settings.CHANNEL_ID:
        return
    text = (message.text or "").strip()
    if len(text) < 5:  # ignore dots, single chars, empty
        return
    await _process_channel(message, bot, raw_kind=RawKind.TEXT, content=text)


@router.channel_post(F.photo)
async def on_channel_photo(message: Message, bot: Bot) -> None:
    if settings.CHANNEL_ID == 0 or message.chat.id != settings.CHANNEL_ID:
        return
    photo = message.photo[-1]
    file = await bot.get_file(photo.file_id)
    buf = io.BytesIO()
    await bot.download_file(file.file_path, destination=buf)
    caption = message.caption or ""
    await _process_channel(message, bot, raw_kind=RawKind.PHOTO, content=caption, image=buf.getvalue())


@router.channel_post(F.voice)
async def on_channel_voice(message: Message, bot: Bot) -> None:
    if settings.CHANNEL_ID == 0 or message.chat.id != settings.CHANNEL_ID:
        return
    file = await bot.get_file(message.voice.file_id)
    buf = io.BytesIO()
    await bot.download_file(file.file_path, destination=buf)
    await _process_channel(message, bot, raw_kind=RawKind.VOICE, content="", audio=buf.getvalue())


async def _process_channel(
    message: Message,
    bot: Bot,
    *,
    raw_kind: RawKind,
    content: str,
    audio: Optional[bytes] = None,
    image: Optional[bytes] = None,
) -> None:
    try:
        history = await db.get_recent(20)
    except Exception:
        history = []

    try:
        result: ClassificationResult = await gemini_client.classify(
            content=content, history=history, audio_bytes=audio, image_bytes=image
        )
    except Exception:
        logger.exception("Groq classification failed (channel)")
        return

    if result.is_close_task_command or result.is_conversational:
        return

    final_content = result.transcript or content or result.title or ""
    if not final_content.strip():
        return
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
                source="channel",
                raw_kind=raw_kind,
            )
        )
    except Exception:
        logger.exception("Supabase insert failed (channel)")
        return

    asyncio.create_task(notion.create_note(entry))

    category_icons = {"task": "📋", "thought": "💭", "idea": "💡", "reminder": "⏰", "note": "📝"}
    priority_icons = {"urgent": "🔴", "normal": "🟡", "someday": "⚪"}
    cat_icon = category_icons.get(result.category.value, "✅")
    pri_icon = priority_icons.get(result.priority.value, "")

    await bot.send_message(
        settings.MY_CHAT_ID,
        f"{cat_icon} Канал · {pri_icon} {result.priority.value} · "
        f"[{entry.id}] _{_md(entry.title)}_",
        parse_mode="Markdown",
    )


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

    _buf_add("user", content or "[voice/photo]")

    try:
        result: ClassificationResult = await gemini_client.classify(
            content=content, history=history, chat_buffer=list(_chat_buffer),
            audio_bytes=audio, image_bytes=image
        )
    except Exception:
        logger.exception("Groq classification failed")
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

    if result.is_conversational and result.reply:
        _buf_add("bot", result.reply)
        await message.answer(result.reply)
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
        await message.answer("⚠️ Понял, но не смог сохранить. Проверь Supabase.")
        return

    asyncio.create_task(notion.create_note(entry))

    reminder_msg = ""
    if result.category == Category.REMINDER:
        # Multiple reminders from one message
        specs: list[ReminderSpec] = result.reminders or []
        if not specs:
            # Fall back to top-level fields (single reminder)
            specs = [ReminderSpec(title=result.title, remind_at=result.remind_at, recurrence=result.recurrence)]

        scheduled = []
        for spec in specs:
            remind_at = None
            if spec.remind_at:
                try:
                    remind_at = dateparser.isoparse(spec.remind_at)
                except Exception:
                    logger.warning("Bad remind_at: %r", spec.remind_at)
            if remind_at or spec.recurrence:
                reminder = await db.insert_reminder(
                    Reminder(
                        entry_id=entry.id,
                        remind_at=remind_at,
                        recurrence=spec.recurrence,
                        content=spec.title or entry.title or entry.content[:120],
                    )
                )
                scheduler.schedule_reminder(reminder)
                if spec.recurrence:
                    scheduled.append(f"🔁 {spec.title or spec.recurrence}")
                elif remind_at:
                    scheduled.append(f"⏰ {remind_at:%d.%m %H:%M}")

        if len(scheduled) == 1:
            reminder_msg = f" · {scheduled[0]}"
        elif scheduled:
            reminder_msg = f" · {len(scheduled)} напоминания"

    transcript_preview = ""
    if raw_kind in (RawKind.VOICE, RawKind.PHOTO) and result.transcript:
        transcript_preview = f"\n📝 _{result.transcript[:200]}_"

    category_icons = {"task": "📋", "thought": "💭", "idea": "💡", "reminder": "⏰", "note": "📝"}
    priority_icons = {"urgent": "🔴", "normal": "🟡", "someday": "⚪"}
    cat_icon = category_icons.get(result.category.value, "✅")
    pri_icon = priority_icons.get(result.priority.value, "")

    reply_text = (
        f"{cat_icon} Сохранено · {pri_icon} {result.priority.value} · "
        f"[{entry.id}] _{_md(entry.title)}_{reminder_msg}{transcript_preview}"
    )
    _buf_add("bot", reply_text)
    await message.answer(reply_text, parse_mode="Markdown")


async def send_daily_digest(bot: Bot) -> None:
    today = await db.get_today_entries()
    week = await db.get_week_entries()
    open_tasks = await db.get_open_tasks()
    today_reminders = await db.get_reminders_firing_today()

    parts = ["☀️ *Доброе утро. Дайджест:*"]
    if open_tasks:
        parts.append(f"\n*Открытые задачи ({len(open_tasks)})*")
        for t in open_tasks[:15]:
            priority_icon = {"urgent": "🔴", "normal": "🟡", "someday": "⚪"}.get(t.priority.value, "")
            parts.append(f"  {priority_icon} [{t.id}] {t.title or t.content[:80]}")
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


async def send_weekly_report(bot: Bot) -> None:
    try:
        stats = await db.get_stats()
        week = await db.get_week_entries()
        open_tasks = await db.get_open_tasks()

        cat_icons = {"task": "📋", "thought": "💭", "idea": "💡", "reminder": "⏰", "note": "📝"}
        lines = ["📅 *Итоги недели:*\n"]
        lines.append(f"Записей за неделю: *{len(week)}*")
        lines.append(f"✅ Задач закрыто всего: {stats['done_tasks']}")
        lines.append(f"🔓 Задач ещё открыто: {stats['open_tasks']}\n")

        if week:
            by_cat: dict[str, int] = {}
            for e in week:
                by_cat[e.category.value] = by_cat.get(e.category.value, 0) + 1
            for cat, cnt in sorted(by_cat.items(), key=lambda x: -x[1]):
                lines.append(f"{cat_icons.get(cat,'•')} {cat}: {cnt}")

        if open_tasks:
            urgent = [t for t in open_tasks if t.priority.value == "urgent"]
            if urgent:
                lines.append(f"\n🔴 *Срочные задачи:*")
                for t in urgent[:5]:
                    lines.append(f"  [{t.id}] {t.title or t.content[:60]}")

        await bot.send_message(settings.MY_CHAT_ID, "\n".join(lines), parse_mode="Markdown")
    except Exception:
        logger.exception("Weekly report failed")


async def check_overdue_tasks(bot: Bot) -> None:
    try:
        tasks = await db.get_open_tasks()
        overdue = [
            t for t in tasks
            if t.created_at and (datetime.now(timezone.utc) - t.created_at).days >= 7
            and t.priority.value != "someday"
        ]
        if not overdue:
            return
        lines = [f"⚠️ *{len(overdue)} задач висит больше недели:*\n"]
        for t in overdue[:10]:
            days = (datetime.now(timezone.utc) - t.created_at).days
            lines.append(f"  [{t.id}] {t.title or t.content[:60]} — {days} дней")
        await bot.send_message(settings.MY_CHAT_ID, "\n".join(lines), parse_mode="Markdown")
    except Exception:
        logger.exception("Overdue check failed")
