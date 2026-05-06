import base64
import json
from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.parse import urlparse

from httpx import AsyncClient as HttpxClient, ConnectError

from config import settings
from models import Category, Entry, Priority, RawKind, Reminder, Status

def _project_ref_from_key(key: str) -> Optional[str]:
    parts = key.split(".")
    if len(parts) < 2:
        return None

    payload = parts[1].replace("-", "+").replace("_", "/")
    payload += "=" * (-len(payload) % 4)
    try:
        data = json.loads(base64.b64decode(payload).decode("utf-8"))
    except Exception:
        return None
    return data.get("ref")


def _supabase_url_from_settings() -> str:
    configured_url = settings.SUPABASE_URL.strip().rstrip("/")
    configured_host = urlparse(configured_url).hostname
    key_ref = _project_ref_from_key(settings.SUPABASE_KEY)

    if key_ref:
        expected_host = f"{key_ref}.supabase.co"
        if configured_host != expected_host:
            return f"https://{expected_host}"

    return configured_url


# Direct PostgREST calls — bypasses supabase-py auth init (which triggers sync httpx)
_SUPABASE_URL = _supabase_url_from_settings()
_SUPABASE_HOST = urlparse(_SUPABASE_URL).hostname or "(invalid SUPABASE_URL)"
_BASE = f"{_SUPABASE_URL}/rest/v1"
_HEADERS = {
    "apikey": settings.SUPABASE_KEY,
    "Authorization": f"Bearer {settings.SUPABASE_KEY}",
    "Content-Type": "application/json",
    "Prefer": "return=representation",
}


def supabase_host() -> str:
    return _SUPABASE_HOST


def _client() -> HttpxClient:
    return HttpxClient(base_url=_BASE, headers=_HEADERS, timeout=10)


async def check_connection() -> None:
    try:
        async with _client() as c:
            r = await c.get("/entries", params={"select": "id", "limit": 1})
            r.raise_for_status()
    except ConnectError as e:
        raise RuntimeError(
            f"Cannot resolve/reach Supabase host '{_SUPABASE_HOST}'. "
            "Check SUPABASE_URL in Render: it must be the Supabase Project URL "
            "from Project Settings → API, like https://<project-ref>.supabase.co"
        ) from e


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


async def insert_entry(entry: Entry) -> Entry:
    async with _client() as c:
        r = await c.post("/entries", json={
            "content": entry.content,
            "title": entry.title,
            "category": entry.category.value,
            "priority": entry.priority.value,
            "status": entry.status.value,
            "tags": entry.tags,
            "source": entry.source,
            "raw_kind": entry.raw_kind.value,
        })
        r.raise_for_status()
        return _to_entry(r.json()[0])


async def get_recent(limit: int = 20) -> list[Entry]:
    async with _client() as c:
        r = await c.get("/entries", params={
            "order": "created_at.desc",
            "limit": limit,
        })
        r.raise_for_status()
        return [_to_entry(row) for row in r.json()]


async def get_open_tasks() -> list[Entry]:
    async with _client() as c:
        r = await c.get("/entries", params={
            "category": f"eq.{Category.TASK.value}",
            "status": f"eq.{Status.OPEN.value}",
            "order": "created_at.desc",
        })
        r.raise_for_status()
        return [_to_entry(row) for row in r.json()]


async def close_task(task_id: int) -> Optional[Entry]:
    async with _client() as c:
        r = await c.patch(
            "/entries",
            params={"id": f"eq.{task_id}", "category": f"eq.{Category.TASK.value}"},
            json={"status": Status.DONE.value},
        )
        r.raise_for_status()
        data = r.json()
        return _to_entry(data[0]) if data else None


async def close_latest_open_task() -> Optional[Entry]:
    async with _client() as c:
        r = await c.get("/entries", params={
            "category": f"eq.{Category.TASK.value}",
            "status": f"eq.{Status.OPEN.value}",
            "order": "created_at.desc",
            "limit": 1,
        })
        r.raise_for_status()
        data = r.json()
        if not data:
            return None
        return await close_task(data[0]["id"])


async def insert_reminder(reminder: Reminder) -> Reminder:
    async with _client() as c:
        r = await c.post("/reminders", json={
            "entry_id": reminder.entry_id,
            "remind_at": reminder.remind_at.isoformat() if reminder.remind_at else None,
            "recurrence": reminder.recurrence,
            "fired": False,
        })
        r.raise_for_status()
        row = r.json()[0]
        row["entries"] = {"content": reminder.content} if reminder.content else None
        return _to_reminder(row)


async def get_pending_reminders() -> list[Reminder]:
    async with _client() as c:
        r = await c.get("/reminders", params={
            "fired": "eq.false",
            "select": "*,entries(content,title)",
        })
        r.raise_for_status()
        return [_to_reminder(row) for row in r.json()]


async def mark_reminder_fired(reminder_id: int) -> None:
    async with _client() as c:
        r = await c.patch("/reminders", params={"id": f"eq.{reminder_id}"}, json={"fired": True})
        r.raise_for_status()


async def get_today_entries() -> list[Entry]:
    async with _client() as c:
        start = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
        r = await c.get("/entries", params={
            "created_at": f"gte.{start}",
            "order": "created_at.desc",
        })
        r.raise_for_status()
        return [_to_entry(row) for row in r.json()]


async def get_week_entries() -> list[Entry]:
    async with _client() as c:
        start = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
        r = await c.get("/entries", params={
            "created_at": f"gte.{start}",
            "order": "created_at.desc",
        })
        r.raise_for_status()
        return [_to_entry(row) for row in r.json()]


async def get_reminders_firing_today() -> list[Reminder]:
    async with _client() as c:
        now = datetime.now(timezone.utc)
        end = (now + timedelta(hours=24)).isoformat()
        r = await c.get("/reminders", params={
            "fired": "eq.false",
            "remind_at": f"gte.{now.isoformat()}",
            "and": f"(remind_at.lte.{end})",
            "select": "*,entries(content,title)",
        })
        r.raise_for_status()
        return [_to_reminder(row) for row in r.json()]
