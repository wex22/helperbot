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


def _parse_dt(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def _to_entry(row: dict) -> Entry:
    return Entry(
        id=row["id"],
        created_at=_parse_dt(row["created_at"]),
        content=row["content"],
        title=row.get("title"),
        category=Category(row["category"]),
        priority=Priority(row["priority"]),
        status=Status(row["status"]),
        tags=row.get("tags") or [],
        source=row.get("source") or "telegram",
        raw_kind=RawKind(row.get("raw_kind") or "text"),
        due_date=_parse_dt(row.get("due_date")),
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
    payload: dict = {
        "content": entry.content,
        "title": entry.title,
        "category": entry.category.value,
        "priority": entry.priority.value,
        "status": entry.status.value,
        "tags": entry.tags,
        "source": entry.source,
        "raw_kind": entry.raw_kind.value,
    }
    if entry.due_date:
        payload["due_date"] = entry.due_date.isoformat()
    async with _client() as c:
        r = await c.post("/entries", json=payload)
        r.raise_for_status()
        return _to_entry(r.json()[0])


async def append_to_entry(entry_id: int, extra_text: str) -> Optional[Entry]:
    """Append extra_text to existing entry's content (used for reply-to-entry)."""
    async with _client() as c:
        # First fetch current content
        r = await c.get("/entries", params={"id": f"eq.{entry_id}", "select": "id,content"})
        r.raise_for_status()
        rows = r.json()
        if not rows:
            return None
        old = rows[0]["content"] or ""
        new_content = f"{old}\n\n— {extra_text}"
        return await update_entry(entry_id, {"content": new_content})


async def find_duplicate(title: str, content: str) -> Optional[Entry]:
    """Look for a very similar entry saved in last hour (dedup guard)."""
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    # Use title match first (exact), then content prefix
    q = title[:60] if title else content[:60]
    async with _client() as c:
        r = await c.get("/entries", params={
            "or": f"(title.ilike.*{q[:40]}*,content.ilike.*{q[:40]}*)",
            "created_at": f"gte.{cutoff}",
            "order": "created_at.desc",
            "limit": 5,
        })
        r.raise_for_status()
        rows = r.json()
        return _to_entry(rows[0]) if rows else None


async def get_habits() -> list[Entry]:
    async with _client() as c:
        r = await c.get("/entries", params={
            "category": "eq.habit",
            "status": "eq.open",
            "order": "created_at.desc",
        })
        r.raise_for_status()
        return [_to_entry(row) for row in r.json()]


async def get_habit_streak(entry_id: int) -> int:
    """Count how many consecutive days (backwards from today) a habit was marked done.
    Relies on 'done' entries with reply_to_entry_id matching this habit.
    Fallback: just return 0 if column doesn't exist yet."""
    try:
        async with _client() as c:
            r = await c.get("/entries", params={
                "reply_to_entry_id": f"eq.{entry_id}",
                "status": "eq.done",
                "order": "created_at.desc",
                "limit": 60,
                "select": "created_at",
            })
            r.raise_for_status()
            rows = r.json()
        if not rows:
            return 0
        dates = set()
        for row in rows:
            dt = _parse_dt(row["created_at"])
            if dt:
                dates.add(dt.date())
        streak = 0
        from datetime import date, timedelta as td
        d = date.today()
        while d in dates:
            streak += 1
            d -= td(days=1)
        return streak
    except Exception:
        return 0


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


async def get_all_open(limit: int = 200) -> list[Entry]:
    async with _client() as c:
        r = await c.get("/entries", params={
            "status": f"eq.{Status.OPEN.value}",
            "order": "created_at.desc",
            "limit": limit,
        })
        r.raise_for_status()
        return [_to_entry(row) for row in r.json()]


async def close_entry(entry_id: int) -> Optional[Entry]:
    async with _client() as c:
        r = await c.patch(
            "/entries",
            params={"id": f"eq.{entry_id}"},
            json={"status": Status.DONE.value},
        )
        r.raise_for_status()
        data = r.json()
        return _to_entry(data[0]) if data else None


async def reopen_entry(entry_id: int) -> Optional[Entry]:
    async with _client() as c:
        r = await c.patch(
            "/entries",
            params={"id": f"eq.{entry_id}"},
            json={"status": Status.OPEN.value},
        )
        r.raise_for_status()
        data = r.json()
        return _to_entry(data[0]) if data else None


async def update_entry(entry_id: int, fields: dict) -> Optional[Entry]:
    """Patch arbitrary subset of entry fields. Caller must pass only known columns."""
    if not fields:
        return None
    async with _client() as c:
        r = await c.patch(
            "/entries",
            params={"id": f"eq.{entry_id}"},
            json=fields,
        )
        r.raise_for_status()
        data = r.json()
        return _to_entry(data[0]) if data else None


async def list_entries(
    *,
    category: Optional[str] = None,
    status: Optional[str] = None,
    q: Optional[str] = None,
    limit: int = 200,
) -> list[Entry]:
    params: dict = {"order": "created_at.desc", "limit": str(limit)}
    if category:
        params["category"] = f"eq.{category}"
    if status:
        params["status"] = f"eq.{status}"
    if q:
        params["or"] = f"(content.ilike.*{q}*,title.ilike.*{q}*)"
    async with _client() as c:
        r = await c.get("/entries", params=params)
        r.raise_for_status()
        return [_to_entry(row) for row in r.json()]


async def delete_entry(entry_id: int) -> None:
    async with _client() as c:
        r = await c.delete("/entries", params={"id": f"eq.{entry_id}"})
        r.raise_for_status()


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


async def get_reminder(reminder_id: int) -> Optional[Reminder]:
    async with _client() as c:
        r = await c.get("/reminders", params={
            "id": f"eq.{reminder_id}",
            "select": "*,entries(content,title)",
            "limit": 1,
        })
        r.raise_for_status()
        rows = r.json()
        return _to_reminder(rows[0]) if rows else None


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


async def search_entries(query: str, limit: int = 10) -> list[Entry]:
    async with _client() as c:
        r = await c.get("/entries", params={
            "or": f"(content.ilike.*{query}*,title.ilike.*{query}*)",
            "order": "created_at.desc",
            "limit": limit,
        })
        r.raise_for_status()
        return [_to_entry(row) for row in r.json()]


async def get_stats() -> dict:
    async with _client() as c:
        r_total = await c.get("/entries", params={"select": "category,status", "limit": 1000})
        r_total.raise_for_status()
        rows = r_total.json()

    total = len(rows)
    by_category: dict[str, int] = {}
    open_tasks = 0
    done_tasks = 0
    for row in rows:
        cat = row.get("category", "note")
        by_category[cat] = by_category.get(cat, 0) + 1
        if cat == "task":
            if row.get("status") == "open":
                open_tasks += 1
            else:
                done_tasks += 1
    return {
        "total": total,
        "by_category": by_category,
        "open_tasks": open_tasks,
        "done_tasks": done_tasks,
    }


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


async def get_reminders_in_range(start: datetime, end: datetime) -> list[Reminder]:
    async with _client() as c:
        r = await c.get("/reminders", params={
            "fired": "eq.false",
            "remind_at": f"gte.{start.isoformat()}",
            "and": f"(remind_at.lt.{end.isoformat()})",
            "select": "*,entries(content,title)",
            "order": "remind_at.asc",
        })
        r.raise_for_status()
        return [_to_reminder(row) for row in r.json()]


async def update_reminder_time(reminder_id: int, new_time: datetime) -> Optional[Reminder]:
    async with _client() as c:
        r = await c.patch(
            "/reminders",
            params={"id": f"eq.{reminder_id}"},
            json={"remind_at": new_time.isoformat(), "fired": False},
        )
        r.raise_for_status()
        rows = r.json()
        if not rows:
            return None
        return await get_reminder(reminder_id)
