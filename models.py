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
    category: Category = Category.NOTE
    priority: Priority = Priority.NORMAL
    title: str = ""
    tags: list[str] = Field(default_factory=list)
    transcript: Optional[str] = None
    remind_at: Optional[str] = None
    recurrence: Optional[str] = None
    is_close_task_command: bool = False
    close_task_id: Optional[int] = None
    is_conversational: bool = False
    reply: Optional[str] = None


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
