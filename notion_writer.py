import asyncio
import logging

from notion_client import Client as NotionSDK

from config import settings
from models import Entry

logger = logging.getLogger(__name__)
_notion = NotionSDK(auth=settings.NOTION_TOKEN)


async def create_note(entry: Entry) -> None:
    title = entry.title or entry.content[:80]
    properties = {
        "Title": {"title": [{"text": {"content": title}}]},
        "Category": {"select": {"name": entry.category.value}},
        "Priority": {"select": {"name": entry.priority.value}},
        "Tags": {"multi_select": [{"name": t} for t in entry.tags[:10]]},
        "Source": {"rich_text": [{"text": {"content": entry.source}}]},
    }
    if entry.created_at:
        properties["Date"] = {"date": {"start": entry.created_at.isoformat()}}

    children = [
        {
            "object": "block",
            "type": "paragraph",
            "paragraph": {"rich_text": [{"type": "text", "text": {"content": entry.content[:1900]}}]},
        }
    ]

    def _create():
        return _notion.pages.create(
            parent={"database_id": settings.NOTION_DB_ID},
            properties=properties,
            children=children,
        )

    try:
        await asyncio.to_thread(_create)
    except Exception:
        logger.exception("Failed to create Notion page")
