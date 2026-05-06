import asyncio
import logging

from notion_client import Client as NotionSDK

from config import settings
from models import Entry

logger = logging.getLogger(__name__)
_notion = NotionSDK(auth=settings.NOTION_TOKEN)

_CAT_ICON = {"task": "📋", "thought": "💭", "idea": "💡", "reminder": "⏰", "note": "📝"}
_PRI_LABEL = {"urgent": "🔴 срочно", "normal": "🟡 обычно", "someday": "⚪ когда-нибудь"}
_MONTHS_RU = [
    "", "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря"
]


def _ru_date(dt) -> str:
    return f"{dt.day} {_MONTHS_RU[dt.month]} {dt.year}, {dt.hour:02d}:{dt.minute:02d}"


async def create_note(entry: Entry) -> None:
    title = entry.title or entry.content[:80]
    icon = _CAT_ICON.get(entry.category.value, "📝")

    properties = {
        "Title": {"title": [{"text": {"content": title}}]},
        "Category": {"select": {"name": entry.category.value}},
        "Priority": {"select": {"name": entry.priority.value}},
        "Tags": {"multi_select": [{"name": t} for t in entry.tags[:10]]},
        "Source": {"rich_text": [{"text": {"content": entry.source}}]},
    }
    if entry.created_at:
        properties["Date"] = {"date": {"start": entry.created_at.isoformat()}}

    children = []

    # Date + category callout at the top
    if entry.created_at:
        pri_label = _PRI_LABEL.get(entry.priority.value, "")
        date_line = f"{_ru_date(entry.created_at)}  ·  {entry.category.value}  ·  {pri_label}"
        children.append({
            "object": "block",
            "type": "callout",
            "callout": {
                "rich_text": [{"type": "text", "text": {"content": date_line}}],
                "icon": {"emoji": icon},
                "color": "gray_background",
            },
        })

    # Main content — split into chunks if long
    text = entry.content
    for i in range(0, max(len(text), 1), 1900):
        chunk = text[i:i + 1900]
        if not chunk:
            break
        children.append({
            "object": "block",
            "type": "paragraph",
            "paragraph": {
                "rich_text": [{"type": "text", "text": {"content": chunk}}],
            },
        })

    # Tags line at the bottom
    if entry.tags:
        tag_line = "  ".join(f"#{t}" for t in entry.tags)
        children.append({
            "object": "block",
            "type": "paragraph",
            "paragraph": {
                "rich_text": [{
                    "type": "text",
                    "text": {"content": tag_line},
                    "annotations": {"color": "gray", "italic": True},
                }],
            },
        })

    def _create():
        return _notion.pages.create(
            parent={"database_id": settings.NOTION_DB_ID},
            icon={"emoji": icon},
            properties=properties,
            children=children,
        )

    try:
        await asyncio.to_thread(_create)
    except Exception:
        logger.exception("Failed to create Notion page")
