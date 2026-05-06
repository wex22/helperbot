import asyncio
from datetime import datetime, timedelta, timezone
from typing import Optional

from supabase import create_client, Client

from config import settings
from models import Category, Entry, Priority, RawKind, Reminder, Status

_client: Client = create_client(settings.SUPABASE_URL, settings.SUPABASE_KEY)


def _to_entry(row: dict) -> Entry:
    return Entry(
        id=row["id"],
        created_at=datetime.fromisoformat(row["created_at"].replace("Z", "+00:00")),
        content=row["content"],
        title=row.get("title"),
        category=Category(row["category"]),
        priority=Priority(row["priority"]),
        status=Status(row["status"]),
        tags=row.get("tags") or [],
        source=row.get("source") or "telegram",
        raw_kind=RawKind(row.get("raw_kind") or "text"),
    )


def _to_reminder(row: dict) -> Reminder:
    remind_at = row.get("remind_at")
    return Reminder(
        id=row["id"],
        entry_id=row["entry_id"],
        remind_at=datetime.fromisoformat(remind_at.replace("Z", "+00:00")) if remind_at else None,
        recurrence=row.get("recurrence"),
        fired=row.get("fired", False),
        content=(row.get("entries") or {}).get("content"),
    )


async def _run(fn):
    return await asyncio.to_thread(fn)


async def insert_entry(entry: Entry) -> Entry:
    payload = {
        "content": entry.content,
        "title": entry.title,
        "category": entry.category.value,
        "priority": entry.priority.value,
        "status": entry.status.value,
        "tags": entry.tags,
        "source": entry.source,
        "raw_kind": entry.raw_kind.value,
    }
    res = await _run(lambda: _client.table("entries").insert(payload).execute())
    return _to_entry(res.data[0])


async def get_recent(limit: int = 20) -> list[Entry]:
    res = await _run(
        lambda: _client.table("entries").select("*").order("created_at", desc=True).limit(limit).execute()
    )
    return [_to_entry(r) for r in res.data]


async def get_open_tasks() -> list[Entry]:
    res = await _run(
        lambda: _client.table("entries")
        .select("*")
        .eq("category", Category.TASK.value)
        .eq("status", Status.OPEN.value)
        .order("created_at", desc=True)
        .execute()
    )
    return [_to_entry(r) for r in res.data]


async def close_task(task_id: int) -> Optional[Entry]:
    res = await _run(
        lambda: _client.table("entries")
        .update({"status": Status.DONE.value})
        .eq("id", task_id)
        .eq("category", Category.TASK.value)
        .execute()
    )
    return _to_entry(res.data[0]) if res.data else None


async def close_latest_open_task() -> Optional[Entry]:
    res = await _run(
        lambda: _client.table("entries")
        .select("*")
        .eq("category", Category.TASK.value)
        .eq("status", Status.OPEN.value)
        .order("created_at", desc=True)
        .limit(1)
        .execute()
    )
    if not res.data:
        return None
    return await close_task(res.data[0]["id"])


async def insert_reminder(reminder: Reminder) -> Reminder:
    payload = {
        "entry_id": reminder.entry_id,
        "remind_at": reminder.remind_at.isoformat() if reminder.remind_at else None,
        "recurrence": reminder.recurrence,
        "fired": False,
    }
    res = await _run(lambda: _client.table("reminders").insert(payload).execute())
    row = res.data[0]
    row["entries"] = {"content": reminder.content} if reminder.content else None
    return _to_reminder(row)


async def get_pending_reminders() -> list[Reminder]:
    res = await _run(
        lambda: _client.table("reminders")
        .select("*, entries(content, title)")
        .eq("fired", False)
        .execute()
    )
    return [_to_reminder(r) for r in res.data]


async def mark_reminder_fired(reminder_id: int) -> None:
    await _run(
        lambda: _client.table("reminders").update({"fired": True}).eq("id", reminder_id).execute()
    )


async def get_today_entries() -> list[Entry]:
    start = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    res = await _run(
        lambda: _client.table("entries")
        .select("*")
        .gte("created_at", start)
        .order("created_at", desc=True)
        .execute()
    )
    return [_to_entry(r) for r in res.data]


async def get_week_entries() -> list[Entry]:
    start = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    res = await _run(
        lambda: _client.table("entries")
        .select("*")
        .gte("created_at", start)
        .order("created_at", desc=True)
        .execute()
    )
    return [_to_entry(r) for r in res.data]


async def get_reminders_firing_today() -> list[Reminder]:
    now = datetime.now(timezone.utc)
    end = (now + timedelta(hours=24)).isoformat()
    res = await _run(
        lambda: _client.table("reminders")
        .select("*, entries(content, title)")
        .eq("fired", False)
        .gte("remind_at", now.isoformat())
        .lte("remind_at", end)
        .execute()
    )
    return [_to_reminder(r) for r in res.data]
