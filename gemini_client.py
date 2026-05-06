import json
import logging
from datetime import datetime
from typing import Optional

from google import genai
from google.genai import types

from config import settings
from models import ClassificationResult, Entry

# Switch to "gemini-2.0-flash" after September 2026 when Gemini Pro subscription ends.
MODEL = "gemini-2.5-pro"

_client = genai.Client(api_key=settings.GEMINI_API_KEY)
logger = logging.getLogger(__name__)

CLASSIFICATION_SCHEMA = types.Schema(
    type=types.Type.OBJECT,
    properties={
        "category": types.Schema(type=types.Type.STRING, enum=["task", "thought", "idea", "reminder", "note"]),
        "priority": types.Schema(type=types.Type.STRING, enum=["urgent", "normal", "someday"]),
        "title": types.Schema(type=types.Type.STRING),
        "tags": types.Schema(type=types.Type.ARRAY, items=types.Schema(type=types.Type.STRING)),
        "transcript": types.Schema(type=types.Type.STRING),
        "remind_at": types.Schema(type=types.Type.STRING),
        "recurrence": types.Schema(type=types.Type.STRING),
        "is_close_task_command": types.Schema(type=types.Type.BOOLEAN),
        "close_task_id": types.Schema(type=types.Type.INTEGER),
    },
    required=["category", "priority", "title"],
)


def _system_prompt(history: list[Entry]) -> str:
    now = datetime.now().astimezone().isoformat()
    history_block = "\n".join(
        f"- [{e.created_at:%Y-%m-%d %H:%M}] ({e.category.value}/{e.priority.value}/{e.status.value}) "
        f"#{e.id} {e.title or e.content[:80]}"
        for e in history if e.created_at
    ) or "(no recent context)"

    return f"""You are a personal assistant that processes one incoming message at a time and returns strict JSON.

Current local datetime: {now}
User timezone: {settings.TZ}

Recent context (last entries by user, newest first):
{history_block}

Your job: read the new message and produce JSON matching this schema:
- category: one of task, thought, idea, reminder, note
- priority: urgent | normal | someday (decide from urgency cues, deadlines, language)
- title: short (<=80 chars) human title in the language of the message
- tags: list of short tags (no leading #); may be empty
- transcript: ONLY when the input was voice/photo — put the recognized text / image description here; otherwise omit
- remind_at: ISO 8601 with timezone offset, ONLY if the user clearly wants a one-shot reminder at a specific time. Resolve relative phrases ("через 10 минут", "tomorrow at 9", "завтра вечером") against current datetime. Omit otherwise.
- recurrence: APScheduler cron expression "min hour day month dow" (e.g. "0 9 * * 1" for every Monday 09:00). Use ONLY for recurring reminders ("каждый день в 9", "every monday"). Omit otherwise.
- is_close_task_command: true if the user is signalling completion of a previous task ("сделал", "done", "готово"). When true, set close_task_id to the referenced task id if explicit (e.g. "сделал 42"); otherwise omit.

Rules:
- Be decisive. Do not ask the user anything; produce JSON only.
- Russian "напомни/напомнить" = reminder. "идея/мысль" = idea/thought. "надо/сделать/нужно" usually = task.
- If the message is just venting / journal-like, category = thought.
- Urgency cues: "срочно", "asap", "сегодня до", explicit deadline within 24h => urgent. Vague "когда-нибудь" => someday. Otherwise normal.
"""


async def classify(
    content: str,
    history: list[Entry],
    audio_bytes: Optional[bytes] = None,
    image_bytes: Optional[bytes] = None,
) -> ClassificationResult:
    parts = [_system_prompt(history)]

    if audio_bytes:
        parts.append(types.Part.from_bytes(data=audio_bytes, mime_type="audio/ogg"))
        parts.append("New message: <voice attached above — transcribe into the 'transcript' field, then classify it>")
    elif image_bytes:
        parts.append(types.Part.from_bytes(data=image_bytes, mime_type="image/jpeg"))
        caption = f"\nUser caption: {content}" if content else ""
        parts.append(f"New message: <image attached above — describe its meaning into 'transcript', then classify>.{caption}")
    else:
        parts.append(f"New message:\n{content}")

    resp = await _client.aio.models.generate_content(
        model=MODEL,
        contents=parts,
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=CLASSIFICATION_SCHEMA,
            temperature=0.2,
        ),
    )

    try:
        data = json.loads(resp.text)
    except Exception:
        logger.error("Gemini returned non-JSON: %r", resp.text)
        raise

    return ClassificationResult.model_validate(data)


async def summarize(today_entries: list[Entry], week_entries: list[Entry]) -> str:
    def fmt(entries: list[Entry]) -> str:
        return "\n".join(
            f"- ({e.category.value}/{e.priority.value}/{e.status.value}) {e.title or e.content[:120]}"
            for e in entries
        ) or "(empty)"

    prompt = f"""Make a concise bilingual-friendly digest in Russian. Two sections:

=== СЕГОДНЯ ===
{fmt(today_entries)}

=== ЗА НЕДЕЛЮ ===
{fmt(week_entries)}

Return Markdown:
*Сегодня*
- 1-2 line summary highlighting urgent items, open tasks, key thoughts
*Неделя*
- 2-4 lines summarizing patterns and progress
Keep it tight. No filler."""

    resp = await _client.aio.models.generate_content(
        model=MODEL,
        contents=prompt,
        config=types.GenerateContentConfig(temperature=0.3),
    )
    return resp.text.strip()
