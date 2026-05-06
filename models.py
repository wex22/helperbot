from datetime import datetime
from enum import Enum
from typing import Optional
from pydantic import BaseModel, Field


class Category(str, Enum):
    TASK = "task"
    THOUGHT = "thought"
    IDEA = "idea"
    REMINDER = "reminder"
    NOTE = "note"


class Priority(str, Enum):
    URGENT = "urgent"
    NORMAL = "normal"
    SOMEDAY = "someday"


class Status(str, Enum):
    OPEN = "open"
    DONE = "done"


class RawKind(str, Enum):
    TEXT = "text"
    VOICE = "voice"
    PHOTO = "photo"


class ClassificationResult(BaseModel):
    category: Category
    priority: Priority = Priority.NORMAL
    title: str
    tags: list[str] = Field(default_factory=list)
    transcript: Optional[str] = None  # filled when input was voice/photo
    remind_at: Optional[str] = None   # ISO 8601 with TZ, e.g. 2026-05-07T09:00:00+03:00
    recurrence: Optional[str] = None  # APScheduler cron expression, e.g. "0 9 * * 1"
    is_close_task_command: bool = False
    close_task_id: Optional[int] = None


class Entry(BaseModel):
    id: Optional[int] = None
    created_at: Optional[datetime] = None
    content: str
    title: Optional[str] = None
    category: Category
    priority: Priority = Priority.NORMAL
    status: Status = Status.OPEN
    tags: list[str] = Field(default_factory=list)
    source: str = "telegram"
    raw_kind: RawKind = RawKind.TEXT


class Reminder(BaseModel):
    id: Optional[int] = None
    entry_id: int
    remind_at: Optional[datetime] = None
    recurrence: Optional[str] = None
    fired: bool = False
    content: Optional[str] = None  # join from entries.content for sending
