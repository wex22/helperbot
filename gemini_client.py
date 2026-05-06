import base64
import json
import logging
from datetime import datetime
from typing import Optional

from groq import AsyncGroq

from config import settings
from models import ClassificationResult, Entry

TEXT_MODEL = "llama-3.3-70b-versatile"
VISION_MODEL = "llama-3.2-11b-vision-preview"
AUDIO_MODEL = "whisper-large-v3-turbo"

_client = AsyncGroq(api_key=settings.GROQ_API_KEY)
logger = logging.getLogger(__name__)

_SCHEMA_HINT = """Return ONLY valid JSON with these fields:
{
  "is_conversational": true | false,
  "reply": "<friendly reply in user's language if is_conversational=true, else omit>",
  "category": "task" | "thought" | "idea" | "reminder" | "note",
  "priority": "urgent" | "normal" | "someday",
  "title": "<short title ≤80 chars>",
  "tags": ["tag1", "tag2"],
  "transcript": "<if voice/photo: recognized text or image description, else omit>",
  "remind_at": "<ISO 8601 with tz offset, only for one-shot reminders, else omit>",
  "recurrence": "<APScheduler cron 'min hour day month dow', only for recurring, else omit>",
  "is_close_task_command": true | false,
  "close_task_id": <int or omit>
}"""


def _system_prompt(history: list[Entry], chat_buffer: list[dict]) -> str:
    now = datetime.now().astimezone().isoformat()
    history_block = "\n".join(
        f"- [{e.created_at:%Y-%m-%d %H:%M}] ({e.category.value}/{e.priority.value}/{e.status.value}) "
        f"#{e.id} {e.title or e.content[:80]}"
        for e in history if e.created_at
    ) or "(none)"

    buffer_block = ""
    if chat_buffer:
        buffer_block = "\nRecent conversation (last messages):\n" + "\n".join(
            f"  {'User' if m['role'] == 'user' else 'Bot'}: {m['text'][:200]}"
            for m in chat_buffer
        )

    return f"""You are a smart personal assistant. You process messages and return strict JSON.

Current local datetime: {now}
User timezone: {settings.TZ}
{buffer_block}
Recent saved entries (newest first):
{history_block}

{_SCHEMA_HINT}

CRITICAL RULES — read carefully:
- Return JSON only. No extra text.

WHEN TO SAVE (is_conversational=false):
  Save ONLY when the user EXPLICITLY wants to save something:
  • Uses save words: "добавь", "сохрани", "запомни", "запиши", "отметь", "add", "save", "note"
  • Clear task: "надо сделать X", "нужно X", "сделать X до Y"
  • Clear reminder: "напомни мне...", "remind me..."
  • Clear idea label: "идея:", "idea:"
  • Clear recurring reminder: "каждый день/неделю..."

WHEN NOT TO SAVE (is_conversational=true):
  • Casual chat, greetings, thanks, questions
  • Random words, links, IDs, numbers sent without context
  • Anything ambiguous — when in doubt, ask or reply conversationally
  • Short phrases that don't clearly express intent to save

When is_conversational=true: reply briefly and naturally in the user's language (RU/EN).
Use recent conversation context to understand what the user means.
Do NOT show full capability list unless user explicitly asks "что умеешь" / "what can you do".

- Russian "напомни/напомнить" = reminder. "надо/нужно/сделать" = task. "идея/мысль" = idea/thought.
- Urgency: "срочно/asap" => urgent. "когда-нибудь" => someday. Otherwise normal.
- Resolve relative time ("через 10 минут", "tomorrow at 9") against current datetime for remind_at.
- is_close_task_command=true when: "сделал", "done", "готово", "выполнено".

When user asks what you can do ("что ты умеешь", "что ты можешь", "как ты работаешь", "помощь", "help"):
Set is_conversational=true and reply with this (keep all points, friendly tone):

Я твой личный ассистент. Вот что я умею:
- Принимаю текст, голосовые, фото
- Сохраняю задачи, идеи, мысли, заметки, напоминания в Notion и базу
- Напоминания: разовые ("напомни через 2 часа позвонить Диме") и повторяющиеся ("каждый пн в 9")
- /задачи - список открытого, "сделал" / "сделал 5" - закрыть задачу
- /поиск слово - найти записи, /стат - статистика, /экспорт - все задачи
- /итоги - сводка, дайджест каждое утро в 9:00
- Могу просто поговорить, ответить на вопрос, помочь с текстом
Просто пиши как думаешь - разберусь сам."""


async def classify(
    content: str,
    history: list[Entry],
    chat_buffer: Optional[list[dict]] = None,
    audio_bytes: Optional[bytes] = None,
    image_bytes: Optional[bytes] = None,
) -> ClassificationResult:
    system = _system_prompt(history, chat_buffer or [])

    if audio_bytes:
        transcript = await _transcribe(audio_bytes)
        user_msg = f"Voice message (transcribed): {transcript}\n\nProcess it: put transcription in 'transcript' field."
    elif image_bytes:
        return await _classify_image(image_bytes, content, system, chat_buffer or [])
    else:
        user_msg = f"Message:\n{content}"

    resp = await _client.chat.completions.create(
        model=TEXT_MODEL,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user_msg},
        ],
        response_format={"type": "json_object"},
        temperature=0.2,
    )

    raw = resp.choices[0].message.content
    try:
        data = json.loads(raw)
    except Exception:
        logger.error("Groq returned non-JSON: %r", raw)
        raise

    return ClassificationResult.model_validate(data)


async def _transcribe(audio_bytes: bytes) -> str:
    transcription = await _client.audio.transcriptions.create(
        file=("audio.ogg", audio_bytes),
        model=AUDIO_MODEL,
        response_format="text",
    )
    return transcription.strip() if isinstance(transcription, str) else transcription.text.strip()


async def _classify_image(image_bytes: bytes, caption: str, system: str, chat_buffer: list[dict]) -> ClassificationResult:
    image_b64 = base64.b64encode(image_bytes).decode()
    caption_part = f"\nUser caption: {caption}" if caption else ""
    user_msg = [
        {
            "type": "image_url",
            "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"},
        },
        {
            "type": "text",
            "text": f"Describe what you see, put description in 'transcript', then classify.{caption_part}",
        },
    ]

    resp = await _client.chat.completions.create(
        model=VISION_MODEL,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user_msg},
        ],
        response_format={"type": "json_object"},
        temperature=0.2,
    )

    raw = resp.choices[0].message.content
    try:
        data = json.loads(raw)
    except Exception:
        logger.error("Groq vision returned non-JSON: %r", raw)
        raise

    return ClassificationResult.model_validate(data)


async def summarize(today_entries: list[Entry], week_entries: list[Entry]) -> str:
    def fmt(entries: list[Entry]) -> str:
        return "\n".join(
            f"- ({e.category.value}/{e.priority.value}/{e.status.value}) {e.title or e.content[:120]}"
            for e in entries
        ) or "(empty)"

    prompt = f"""Make a concise digest in Russian. Two sections:

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

    resp = await _client.chat.completions.create(
        model=TEXT_MODEL,
        messages=[{"role": "user", "content": prompt}],
        temperature=0.3,
    )
    return resp.choices[0].message.content.strip()
