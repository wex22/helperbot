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
    HABIT = "habit"


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


class ReminderSpec(BaseModel):
    """One reminder item inside a multi-reminder response."""
    title: str = ""
    remind_at: Optional[str] = None   # ISO 8601
    recurrence: Optional[str] = None  # APScheduler cron


class ClassificationResult(BaseModel):
    category: Category = Category.NOTE
    priority: Priority = Priority.NORMAL
    title: str = ""
    tags: list[str] = Field(default_factory=list)
    transcript: Optional[str] = None
    remind_at: Optional[str] = None
    recurrence: Optional[str] = None
    reminders: list[ReminderSpec] = Field(default_factory=list)
    is_close_task_command: bool = False
    close_task_id: Optional[int] = None
    is_conversational: bool = False
    reply: Optional[str] = None
    is_postpone: bool = False
    postpone_id: Optional[int] = None
    postpone_to: Optional[str] = None
    due_date: Optional[str] = None           # ISO 8601 deadline parsed from message
    multi_entries: list[dict] = Field(default_factory=list)   # batch from voice (validated later)
    is_habit_done: bool = False              # "выпил воду" → mark habit done today
    is_reply_append: bool = False            # user replying to add context to existing entry
    reply_entry_id: Optional[int] = None    # which entry to append to
    append_text: Optional[str] = None       # text to append


class MultiEntry(BaseModel):
    """One item in a batch save (multi-task voice message)."""
    title: str = ""
    content: str = ""
    category: str = "task"
    priority: str = "normal"
    due_date: Optional[str] = None


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
    due_date: Optional[datetime] = None
    reply_to_entry_id: Optional[int] = None  # if this is a comment on another entry


class Reminder(BaseModel):
    id: Optional[int] = None
    entry_id: int
    remind_at: Optional[datetime] = None
    recurrence: Optional[str] = None
    fired: bool = False
    content: Optional[str] = None  # join from entries.content for sending
