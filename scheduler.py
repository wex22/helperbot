import asyncio
import logging
from datetime import datetime, timedelta
from typing import Optional

import pytz
from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger

import supabase_client as db
from config import settings
from models import Reminder

logger = logging.getLogger(__name__)

_scheduler: Optional[AsyncIOScheduler] = None
_bot: Optional[Bot] = None
_tz = pytz.timezone(settings.TZ)


def _fired_kb(reminder_id: int, recurring: bool) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(text="✅ Сделал", callback_data=f"rem_done:{reminder_id}"),
            InlineKeyboardButton(text="😴 +10м", callback_data=f"rem_snooze:{reminder_id}:10"),
            InlineKeyboardButton(text="⏰ +1ч", callback_data=f"rem_snooze:{reminder_id}:60"),
        ],
    ]
    if recurring:
        rows.append([InlineKeyboardButton(text="🔇 Прекратить серию", callback_data=f"rem_stop:{reminder_id}")])
    else:
        rows.append([InlineKeyboardButton(text="🗑️ Скрыть", callback_data=f"rem_hide:{reminder_id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def init(bot: Bot) -> AsyncIOScheduler:
    global _scheduler, _bot
    _bot = bot
    _scheduler = AsyncIOScheduler(timezone=_tz)
    _scheduler.start()
    return _scheduler


async def _send_reminder(reminder_id: int, content: str, recurring: bool) -> None:
    try:
        await _bot.send_message(
            settings.MY_CHAT_ID,
            f"⏰ {content}",
            reply_markup=_fired_kb(reminder_id, recurring),
        )
        if not recurring:
            await db.mark_reminder_fired(reminder_id)
    except Exception:
        logger.exception("Failed to send reminder %s", reminder_id)


async def snooze_reminder(reminder_id: int, minutes: int) -> Optional[datetime]:
    """Create a one-shot follow-up of an existing reminder N minutes from now."""
    if _scheduler is None:
        raise RuntimeError("scheduler not initialised")
    rem = await db.get_reminder(reminder_id)
    if rem is None:
        return None
    new_at = datetime.now(_tz) + timedelta(minutes=minutes)
    new_rem = await db.insert_reminder(Reminder(
        entry_id=rem.entry_id,
        remind_at=new_at,
        recurrence=None,
        content=rem.content,
    ))
    schedule_reminder(new_rem)
    return new_at


def schedule_reminder(reminder: Reminder) -> None:
    if _scheduler is None:
        raise RuntimeError("scheduler.init(bot) must be called before scheduling")

    job_id = f"reminder-{reminder.id}"
    content = reminder.content or "(no content)"

    if reminder.recurrence:
        trigger = CronTrigger.from_crontab(reminder.recurrence, timezone=_tz)
        _scheduler.add_job(
            _send_reminder,
            trigger=trigger,
            id=job_id,
            replace_existing=True,
            kwargs={"reminder_id": reminder.id, "content": content, "recurring": True},
        )
    elif reminder.remind_at:
        now = datetime.now(reminder.remind_at.tzinfo or _tz)
        if reminder.remind_at <= now:
            missed_by = (now - reminder.remind_at).total_seconds()
            # Fire missed one-shots up to 6h late, with a "(late)" mark.
            # Older than 6h is silently skipped — assume user already moved on.
            if missed_by < 6 * 3600:
                mins = int(missed_by // 60)
                late_note = f" <i>(опоздало на {mins} мин)</i>" if mins >= 2 else ""
                logger.info("Reminder %s missed by %.0fs — firing now", reminder.id, missed_by)
                asyncio.get_event_loop().create_task(
                    _send_reminder(reminder.id, content + late_note, False)
                )
            else:
                logger.info("Reminder %s too old (%.0fs) — skipping", reminder.id, missed_by)
                asyncio.get_event_loop().create_task(db.mark_reminder_fired(reminder.id))
            return
        _scheduler.add_job(
            _send_reminder,
            trigger=DateTrigger(run_date=reminder.remind_at),
            id=job_id,
            replace_existing=True,
            kwargs={"reminder_id": reminder.id, "content": content, "recurring": False},
        )
    else:
        logger.warning("Reminder %s has neither remind_at nor recurrence", reminder.id)


def schedule_daily_digest(send_digest_callback) -> None:
    if _scheduler is None:
        raise RuntimeError("scheduler.init(bot) must be called before scheduling")
    _scheduler.add_job(
        send_digest_callback,
        trigger=CronTrigger(hour=9, minute=0, timezone=_tz),
        id="daily-digest",
        replace_existing=True,
    )


def schedule_weekly_report(send_weekly_callback) -> None:
    if _scheduler is None:
        raise RuntimeError("scheduler.init(bot) must be called before scheduling")
    _scheduler.add_job(
        send_weekly_callback,
        trigger=CronTrigger(day_of_week="sun", hour=20, minute=0, timezone=_tz),
        id="weekly-report",
        replace_existing=True,
    )


def schedule_overdue_check(check_overdue_callback) -> None:
    if _scheduler is None:
        raise RuntimeError("scheduler.init(bot) must be called before scheduling")
    _scheduler.add_job(
        check_overdue_callback,
        trigger=CronTrigger(hour=10, minute=0, timezone=_tz),
        id="overdue-check",
        replace_existing=True,
    )


def cancel_reminder(reminder_id: int) -> None:
    if _scheduler is None:
        return
    job_id = f"reminder-{reminder_id}"
    try:
        _scheduler.remove_job(job_id)
        logger.info("Cancelled reminder job %s", job_id)
    except Exception:
        logger.warning("Job %s not found in scheduler (may have already fired)", job_id)


async def rehydrate() -> None:
    pending = await db.get_pending_reminders()
    for r in pending:
        schedule_reminder(r)
    logger.info("Rehydrated %d reminders", len(pending))
