import asyncio
import io
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Optional

import aiohttp
from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery,
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
from models import Category, ClassificationResult, Entry, Priority, RawKind, Reminder, ReminderSpec, Status

logger = logging.getLogger(__name__)
router = Router()

TAG_RE = re.compile(r"#([\wа-яА-ЯёЁ\-]+)", re.UNICODE)
DONE_RE = re.compile(r"^\s*(сделал|сделано|готово|выполнено|done)\b\s*(.*)?", re.IGNORECASE)
URL_RE  = re.compile(r"^https?://\S+$", re.IGNORECASE)

# Mapping bot message_id → entry_id (to support reply-to-entry)
_bot_msg_to_entry: dict[int, int] = {}

# Project context: if set, all new entries get this tag
_active_project: Optional[str] = None

# Titles that are useless when a reminder fires — we replace them with real content
_GENERIC_TITLE_RE = re.compile(
    r"^\s*(напомин\w*|reminder|каждые?|every|repeat|повтор\w*)\b.*$",
    re.IGNORECASE | re.UNICODE,
)


def _is_generic_reminder_title(title: str) -> bool:
    if not title or len(title.strip()) < 4:
        return True
    return bool(_GENERIC_TITLE_RE.match(title.strip()))


def _best_reminder_title(specs: list, fallback: str) -> str:
    """Pick the most meaningful title from a batch — used to repair generic siblings."""
    for s in specs:
        if s.title and not _is_generic_reminder_title(s.title):
            return s.title
    return fallback


def _make_action_kb(entry_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Готово", callback_data=f"close:{entry_id}"),
        InlineKeyboardButton(text="🗑️ Удалить", callback_data=f"del:{entry_id}"),
    ]])


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
        f"Или отправь голосовое / фото / ссылку.\n\n"
        f"*Задачи:*\n"
        f"/меню — визуальный менеджер задач\n"
        f"/фокус — срочные задачи на сегодня\n"
        f"/задачи — все открытые задачи\n"
        f"/done [id] — закрыть задачу\n"
        f"сделал [слово/id] — закрыть по названию\n\n"
        f"*Напоминания:*\n"
        f"/сегодня — план на сегодня\n"
        f"/завтра — план на завтра\n"
        f"/напоминания — список активных\n"
        f"/отмена [id] — отменить напоминание\n"
        f"перенеси напоминание N на завтра 10 — переносит\n\n"
        f"*Привычки и проекты:*\n"
        f"/привычки — трекер привычек + streak\n"
        f"/проект <название> — тегать всё в один проект\n"
        f"/проект stop — снять контекст проекта\n\n"
        f"*Поиск и статистика:*\n"
        f"/поиск <слово> — поиск по записям\n"
        f"/стат — статистика\n"
        f"/анализ — паттерны и повторы\n"
        f"/итоги — сводка за неделю\n"
        f"/экспорт — все задачи текстом\n\n"
        f"Chat id: `{message.chat.id}`",
        parse_mode="Markdown",
    )


# ── Callback кнопки ────────────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("close:"))
async def cb_close(callback: CallbackQuery) -> None:
    entry_id = int(callback.data.split(":")[1])
    await db.close_entry(entry_id)
    await callback.answer("✅ Закрыто!")
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass


@router.callback_query(F.data.startswith("del:"))
async def cb_delete(callback: CallbackQuery) -> None:
    entry_id = int(callback.data.split(":")[1])
    await db.delete_entry(entry_id)
    await callback.answer("🗑️ Удалено")
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass


@router.callback_query(F.data.startswith("cancel_rem:"))
async def cb_cancel_reminder(callback: CallbackQuery) -> None:
    rem_id = int(callback.data.split(":")[1])
    await db.mark_reminder_fired(rem_id)
    scheduler.cancel_reminder(rem_id)
    await callback.answer("⏰ Напоминание отменено")
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass


@router.callback_query(F.data.startswith("rem_done:"))
async def cb_rem_done(callback: CallbackQuery) -> None:
    rem_id = int(callback.data.split(":")[1])
    rem = await db.get_reminder(rem_id)
    if rem and rem.entry_id:
        await db.close_entry(rem.entry_id)
    if rem and not rem.recurrence:
        await db.mark_reminder_fired(rem_id)
    await callback.answer("✅ Сделано!")
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass


@router.callback_query(F.data.startswith("rem_snooze:"))
async def cb_rem_snooze(callback: CallbackQuery) -> None:
    parts = callback.data.split(":")
    rem_id, minutes = int(parts[1]), int(parts[2])
    new_at = await scheduler.snooze_reminder(rem_id, minutes)
    if new_at:
        await callback.answer(f"😴 Отложено до {new_at:%H:%M}")
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
    else:
        await callback.answer("Не нашёл напоминание", show_alert=True)


@router.callback_query(F.data.startswith("rem_stop:"))
async def cb_rem_stop(callback: CallbackQuery) -> None:
    rem_id = int(callback.data.split(":")[1])
    await db.mark_reminder_fired(rem_id)
    scheduler.cancel_reminder(rem_id)
    await callback.answer("🔇 Серия остановлена")
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass


@router.callback_query(F.data.startswith("rem_hide:"))
async def cb_rem_hide(callback: CallbackQuery) -> None:
    await callback.answer()
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass


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


@router.message(Command("фокус"))
async def cmd_focus(message: Message) -> None:
    if not _is_authorized(message):
        return
    tasks = await db.get_open_tasks()
    urgent = [t for t in tasks if t.priority.value == "urgent"]
    show = urgent[:5] or [t for t in tasks if t.priority.value == "normal"][:3]
    if not show:
        await message.answer("🎯 Нет задач. Свободен!")
        return
    lines = [f"🎯 *Фокус {'срочное' if urgent else 'на сейчас'}:*\n"]
    for t in show:
        icon = "🔴" if t.priority.value == "urgent" else "🟡"
        days = (datetime.now(timezone.utc) - t.created_at).days if t.created_at else 0
        age = f" _{days}д_" if days > 0 else ""
        lines.append(f"{icon} [{t.id}] {_md(t.title or t.content[:80])}{age}")
    await message.answer("\n".join(lines), parse_mode="Markdown")


@router.message(Command("напоминания"))
async def cmd_reminders_list(message: Message) -> None:
    if not _is_authorized(message):
        return
    pending = await db.get_pending_reminders()
    if not pending:
        await message.answer("Нет активных напоминаний.")
        return
    lines = [f"⏰ *Активные напоминания ({len(pending)}):*\n"]
    buttons = []
    for r in pending[:15]:
        time_str = f"🔁 повтор" if r.recurrence else (f"⏰ {r.remind_at:%d.%m %H:%M}" if r.remind_at else "?")
        lines.append(f"[{r.id}] {time_str} — {_md(r.content or '')}")
        buttons.append([InlineKeyboardButton(
            text=f"❌ Отменить [{r.id}]", callback_data=f"cancel_rem:{r.id}"
        )])
    kb = InlineKeyboardMarkup(inline_keyboard=buttons)
    await message.answer("\n".join(lines), parse_mode="Markdown", reply_markup=kb)


async def _day_view(message: Message, day_offset: int, label: str) -> None:
    import pytz
    tz = pytz.timezone(settings.TZ)
    today_local = datetime.now(tz).replace(hour=0, minute=0, second=0, microsecond=0)
    start = today_local + timedelta(days=day_offset)
    end = start + timedelta(days=1)
    rems = await db.get_reminders_in_range(start.astimezone(timezone.utc), end.astimezone(timezone.utc))

    open_tasks = []
    if day_offset == 0:
        open_tasks = await db.get_open_tasks()
        urgent = [t for t in open_tasks if t.priority.value == "urgent"]
        normal = [t for t in open_tasks if t.priority.value == "normal"]
        open_tasks = (urgent + normal)[:8]

    lines = [f"📅 *{label} ({start:%d.%m, %a})*\n"]

    if rems:
        lines.append(f"⏰ *Напоминания ({len(rems)}):*")
        for r in rems[:20]:
            t_str = r.remind_at.astimezone(tz).strftime("%H:%M") if r.remind_at else "?"
            lines.append(f"  {t_str} · [{r.id}] {_md(r.content or '')[:80]}")
    else:
        lines.append("⏰ Напоминаний нет")

    if day_offset == 0 and open_tasks:
        lines.append(f"\n📋 *Задачи (топ {len(open_tasks)} из {len(await db.get_open_tasks())}):*")
        for t in open_tasks:
            icon = "🔴" if t.priority.value == "urgent" else "🟡"
            lines.append(f"  {icon} [{t.id}] {_md(t.title or t.content[:80])}")

    await message.answer("\n".join(lines), parse_mode="Markdown")


@router.message(Command("сегодня"))
async def cmd_today(message: Message) -> None:
    if not _is_authorized(message):
        return
    await _day_view(message, 0, "Сегодня")


@router.message(Command("завтра"))
async def cmd_tomorrow(message: Message) -> None:
    if not _is_authorized(message):
        return
    await _day_view(message, 1, "Завтра")


@router.message(Command("отмена"))
async def cmd_cancel_reminder(message: Message) -> None:
    if not _is_authorized(message):
        return
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip().isdigit():
        await message.answer("Использование: /отмена <id>  (id из /напоминания)")
        return
    rem_id = int(parts[1].strip())
    await db.mark_reminder_fired(rem_id)
    scheduler.cancel_reminder(rem_id)
    await message.answer(f"✅ Напоминание #{rem_id} отменено.")


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


@router.message(Command("проект"))
async def cmd_project(message: Message) -> None:
    global _active_project
    if not _is_authorized(message):
        return
    parts = (message.text or "").split(maxsplit=1)
    arg = parts[1].strip() if len(parts) > 1 else ""
    if not arg or arg.lower() in ("stop", "стоп", "off", "выкл"):
        _active_project = None
        await message.answer("🔵 Контекст проекта снят. Записи идут без проекта.")
    else:
        _active_project = arg.lower().replace(" ", "_")
        await message.answer(
            f"📁 Проект: *{_active_project}*\n"
            f"Все новые записи будут помечены тегом `#{_active_project}`.\n"
            f"Чтобы снять — `/проект stop`",
            parse_mode="Markdown",
        )


@router.message(Command("привычки"))
async def cmd_habits(message: Message) -> None:
    if not _is_authorized(message):
        return
    habits = await db.get_habits()
    if not habits:
        await message.answer("Привычек нет. Напиши «трекай выпить воду каждый день» — добавлю.")
        return
    lines = [f"🔁 *Привычки ({len(habits)}):*\n"]
    for h in habits:
        streak = await db.get_habit_streak(h.id)
        streak_str = f" 🔥{streak}" if streak > 1 else ""
        lines.append(f"[{h.id}] {_md(h.title or h.content[:60])}{streak_str}")
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"✅ {_md(h.title or h.content[:30])}", callback_data=f"habit_done:{h.id}")]
        for h in habits[:8]
    ])
    await message.answer("\n".join(lines), parse_mode="Markdown", reply_markup=kb)


@router.callback_query(F.data.startswith("habit_done:"))
async def cb_habit_done(callback: CallbackQuery) -> None:
    habit_id = int(callback.data.split(":")[1])
    habit = await db.close_entry(habit_id)  # mark today's check as done
    # Re-open the habit itself so it persists
    await db.reopen_entry(habit_id)
    streak = await db.get_habit_streak(habit_id)
    await callback.answer(f"✅ Отмечено! 🔥{streak} дней подряд" if streak > 1 else "✅ Отмечено!")
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass


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
    text = (message.text or "").strip()

    # ── "сделал [id или ключевое слово]" ──────────────────────────────────────
    m = DONE_RE.match(text)
    if m:
        rest = (m.group(2) or "").strip()
        if rest.isdigit():
            closed = await db.close_task(int(rest))
        elif rest:
            results = await db.search_entries(rest, limit=5)
            open_match = next((e for e in results if e.status == Status.OPEN), None)
            closed = await db.close_entry(open_match.id) if open_match else None
        else:
            closed = await db.close_latest_open_task()

        if closed:
            await message.answer(f"✅ Закрыта: [{closed.id}] {_md(closed.title or closed.content[:80])}", parse_mode="Markdown")
        else:
            await message.answer("Не нашёл открытую задачу.")
        return

    # ── Ссылка → сохранить с заголовком ───────────────────────────────────────
    if URL_RE.match(text):
        await _save_url(message, text)
        return

    # ── Cancel pending dedup via typed response ────────────────────────────────
    if _pending_saves:
        _CANCEL = {"нет", "не", "no", "отмена", "cancel", "н", "нет.", "не."}
        _CONFIRM = {"да", "yes", "ок", "ok", "д", "да.", "добавить", "сохранить"}
        tl = text.lower().strip()
        if tl in _CANCEL:
            _pending_saves.clear()
            await message.answer("Отменено.")
            return
        if tl in _CONFIRM:
            _, (pend_result, pend_content, pend_raw_kind) = next(reversed(_pending_saves.items()))
            _pending_saves.clear()
            hashtags = [t.lstrip("#") for t in TAG_RE.findall(pend_content or "")]
            tags = list(dict.fromkeys([*pend_result.tags, *hashtags]))
            if _active_project:
                tags = list(dict.fromkeys([_active_project, *tags]))
            due_date = None
            if pend_result.due_date:
                try:
                    due_date = dateparser.isoparse(pend_result.due_date)
                except Exception:
                    pass
            await _do_save(message, pend_result, pend_content, tags, pend_raw_kind, due_date)
            return

    # ── Detect reply-to-entry ──────────────────────────────────────────────────
    reply_entry_id: Optional[int] = None
    if message.reply_to_message:
        reply_entry_id = _bot_msg_to_entry.get(message.reply_to_message.message_id)

    await _process(message, raw_kind=RawKind.TEXT, content=text, reply_entry_id=reply_entry_id)


async def _save_url(message: Message, url: str) -> None:
    title = url[:80]
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=6)) as session:
            async with session.get(url, headers={"User-Agent": "Mozilla/5.0"}, allow_redirects=True) as resp:
                if resp.status == 200:
                    html = await resp.text(errors="ignore")
                    m = re.search(r"<title[^>]*>(.*?)</title>", html, re.IGNORECASE | re.DOTALL)
                    if m:
                        title = re.sub(r"\s+", " ", m.group(1)).strip()[:80]
    except Exception:
        pass

    try:
        entry = await db.insert_entry(Entry(
            content=url,
            title=title,
            category=Category.NOTE,
            priority=Priority.NORMAL,
            status=Status.OPEN,
            tags=["link"],
            raw_kind=RawKind.TEXT,
        ))
        asyncio.create_task(notion.create_note(entry))
        await message.answer(
            f"🔗 Сохранено: _{_md(title)}_",
            parse_mode="Markdown",
            reply_markup=_make_action_kb(entry.id),
        )
    except Exception:
        logger.exception("URL save failed")
        await message.answer("⚠️ Не смог сохранить ссылку.")


async def _process(
    message: Message,
    *,
    raw_kind: RawKind,
    content: str,
    audio: Optional[bytes] = None,
    image: Optional[bytes] = None,
    reply_entry_id: Optional[int] = None,
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
            audio_bytes=audio, image_bytes=image,
            reply_entry_id=reply_entry_id,
        )
    except Exception:
        logger.exception("Groq classification failed")
        await message.answer("⚠️ Не получилось обработать сообщение.")
        return

    # ── Reply/append to existing entry ────────────────────────────────────────
    if result.is_reply_append and result.reply_entry_id and result.append_text:
        updated = await db.append_to_entry(result.reply_entry_id, result.append_text)
        if updated:
            await message.answer(
                f"✏️ Добавлено к [{updated.id}] _{_md(updated.title or updated.content[:80])}_",
                parse_mode="Markdown",
            )
        else:
            await message.answer(f"Не нашёл запись #{result.reply_entry_id}.")
        return

    # ── Close task ─────────────────────────────────────────────────────────────
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

    # ── Postpone reminder ──────────────────────────────────────────────────────
    if result.is_postpone and result.postpone_id and result.postpone_to:
        try:
            new_at = dateparser.isoparse(result.postpone_to)
        except Exception:
            await message.answer("⚠️ Не разобрал новое время.")
            return
        scheduler.cancel_reminder(result.postpone_id)
        updated = await db.update_reminder_time(result.postpone_id, new_at)
        if not updated:
            await message.answer(f"Не нашёл напоминание #{result.postpone_id}.")
            return
        scheduler.schedule_reminder(updated)
        await message.answer(
            f"📅 Перенёс напоминание #{updated.id} на {new_at:%d.%m %H:%M}\n"
            f"_{_md(updated.content or '')[:120]}_",
            parse_mode="Markdown",
        )
        return

    # ── Conversational ─────────────────────────────────────────────────────────
    if result.is_conversational and result.reply:
        _buf_add("bot", result.reply)
        await message.answer(result.reply)
        return

    final_content = result.transcript or content
    if not final_content:
        final_content = result.title

    # ── Multi-entry batch (voice with several items) ───────────────────────────
    if result.multi_entries and len(result.multi_entries) >= 2:
        await _save_batch(message, result.multi_entries, raw_kind)
        return

    # ── Habit done check-in ────────────────────────────────────────────────────
    if result.is_habit_done:
        habits = await db.get_habits()
        matched = None
        if habits:
            q_lower = (final_content or "").lower()
            for h in habits:
                hw = (h.title or h.content or "").lower()
                if any(w in q_lower for w in hw.split()[:3] if len(w) > 3):
                    matched = h
                    break
        if matched:
            streak = await db.get_habit_streak(matched.id)
            await message.answer(
                f"✅ *{_md(matched.title or matched.content[:60])}* отмечено!\n"
                f"{'🔥 ' + str(streak + 1) + ' дней подряд' if streak >= 1 else 'Начинаем серию 🚀'}",
                parse_mode="Markdown",
            )
            return

    # ── Dedup guard ────────────────────────────────────────────────────────────
    if result.category != Category.REMINDER:
        dup = await db.find_duplicate(result.title or "", final_content or "")
        if dup:
            kb = InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="Да, добавить", callback_data=f"dedup_save:{dup.id}"),
                InlineKeyboardButton(text="Отмена",       callback_data="dedup_cancel"),
            ]])
            await message.answer(
                f"⚠️ Похожая запись уже есть:\n[{dup.id}] _{_md(dup.title or dup.content[:80])}_\n\nВсё равно сохранить?",
                parse_mode="Markdown", reply_markup=kb,
            )
            # Stash the pending save context in buffer for the callback
            _pending_saves[message.message_id] = (result, final_content, raw_kind)
            return

    hashtags = [t.lstrip("#") for t in TAG_RE.findall(final_content or "")]
    tags = list(dict.fromkeys([*result.tags, *hashtags]))
    if _active_project:
        tags = list(dict.fromkeys([_active_project, *tags]))

    due_date = None
    if result.due_date:
        try:
            due_date = dateparser.isoparse(result.due_date)
        except Exception:
            logger.warning("Bad due_date: %r", result.due_date)

    await _do_save(message, result, final_content, tags, raw_kind, due_date)


# Pending saves waiting for dedup confirmation
_pending_saves: dict[int, tuple] = {}


@router.callback_query(F.data.startswith("dedup_save:"))
async def cb_dedup_save(callback: CallbackQuery) -> None:
    msg_id = callback.message.reply_to_message.message_id if callback.message.reply_to_message else None
    save_data = _pending_saves.pop(msg_id, None) if msg_id else None
    await callback.answer()
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    if save_data:
        result, final_content, raw_kind = save_data
        await _do_save(callback.message, result, final_content, result.tags, raw_kind, None)
    else:
        await callback.message.answer("Нажми кнопку сразу после сообщения.")


@router.callback_query(F.data == "dedup_cancel")
async def cb_dedup_cancel(callback: CallbackQuery) -> None:
    await callback.answer("Отменено")
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass


async def _save_batch(message: Message, items: list[dict], raw_kind: RawKind) -> None:
    """Save multiple entries from one voice/text message."""
    saved = []
    for item in items[:10]:
        try:
            cat = Category(item.get("category", "task"))
        except ValueError:
            cat = Category.TASK
        try:
            pri = Priority(item.get("priority", "normal"))
        except ValueError:
            pri = Priority.NORMAL
        due = None
        if item.get("due_date"):
            try:
                due = dateparser.isoparse(item["due_date"])
            except Exception:
                pass
        tags = [_active_project] if _active_project else []
        entry = await db.insert_entry(Entry(
            content=item.get("content") or item.get("title", ""),
            title=item.get("title", "")[:80],
            category=cat, priority=pri, status=Status.OPEN,
            tags=tags, raw_kind=raw_kind, due_date=due,
        ))
        asyncio.create_task(notion.create_note(entry))
        saved.append(entry)
    cat_icons = {"task": "📋", "thought": "💭", "idea": "💡", "reminder": "⏰", "note": "📝", "habit": "🔁"}
    lines = [f"📦 Сохранено {len(saved)} записей:\n"]
    for e in saved:
        icon = cat_icons.get(e.category.value, "•")
        lines.append(f"{icon} [{e.id}] {_md(e.title or e.content[:60])}")
    await message.answer("\n".join(lines), parse_mode="Markdown")


async def _do_save(
    message: Message,
    result: ClassificationResult,
    final_content: str,
    tags: list[str],
    raw_kind: RawKind,
    due_date,
) -> None:
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
                due_date=due_date,
            )
        )
    except Exception:
        logger.exception("Supabase insert failed")
        await message.answer("⚠️ Понял, но не смог сохранить. Проверь Supabase.")
        return

    asyncio.create_task(notion.create_note(entry))

    # Track bot message → entry_id for reply-to-entry
    reminder_msg = ""
    if result.category == Category.REMINDER or result.category == Category.HABIT:
        specs: list[ReminderSpec] = result.reminders or []
        if not specs:
            specs = [ReminderSpec(title=result.title, remind_at=result.remind_at, recurrence=result.recurrence)]

        good_title = _best_reminder_title(specs, result.title or final_content[:80])
        for spec in specs:
            if _is_generic_reminder_title(spec.title):
                spec.title = good_title

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
            reminder_msg = f" · {len(scheduled)} напомин."

    due_str = ""
    if due_date:
        due_str = f" · 📅 до {due_date:%d.%m}"

    transcript_preview = ""
    if raw_kind in (RawKind.VOICE, RawKind.PHOTO) and result.transcript:
        transcript_preview = f"\n📝 _{result.transcript[:200]}_"

    category_icons = {"task": "📋", "thought": "💭", "idea": "💡", "reminder": "⏰", "note": "📝", "habit": "🔁"}
    priority_icons = {"urgent": "🔴", "normal": "🟡", "someday": "⚪"}
    cat_icon = category_icons.get(result.category.value, "✅")
    pri_icon = priority_icons.get(result.priority.value, "")

    reply_text = (
        f"{cat_icon} Сохранено · {pri_icon} {result.priority.value} · "
        f"[{entry.id}] _{_md(entry.title)}_{reminder_msg}{due_str}{transcript_preview}"
    )
    _buf_add("bot", reply_text)
    kb = _make_action_kb(entry.id) if result.category not in (Category.REMINDER, Category.HABIT) else None
    sent = await message.answer(reply_text, parse_mode="Markdown", reply_markup=kb)
    # Store mapping for reply-to-entry
    _bot_msg_to_entry[sent.message_id] = entry.id


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
            due = f" 📅{t.due_date:%d.%m}" if t.due_date else ""
            parts.append(f"  {priority_icon} [{t.id}] {t.title or t.content[:80]}{due}")
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
