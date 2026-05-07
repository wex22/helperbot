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
  "remind_at": "<ISO 8601 with tz offset, only for single one-shot reminder, else omit>",
  "recurrence": "<cron 'min hour day month dow', only for single recurring, else omit>",
  "reminders": [
    {"title": "<short title>", "remind_at": "<ISO 8601 or omit>", "recurrence": "<cron or omit>"}
  ],
  "is_close_task_command": true | false,
  "close_task_id": <int or omit>,
  "is_postpone": true | false,
  "postpone_id": <int or omit>,
  "postpone_to": "<ISO 8601 with tz offset, or omit>"
}

MULTIPLE REMINDERS — use "reminders" array (not top-level fields) when user asks for several at once.

  CRITICAL — REMINDER TITLE QUALITY:
  Every reminder title MUST contain the actual content the user wants to be reminded ABOUT.
  NEVER set a title like "Напоминание каждые 30 мин" or "Reminder every X minutes" — those are useless when they fire.
  When user says "каждые 30 мин ТАКОЕ ЖЕ напоминание" / "то же самое каждый час" / "напоминай об этом" —
  REUSE the content/title of the previous reminder in the same message or the latest user intent. The recurring reminder
  must repeat the meaningful payload, not a meta-description of itself.

  Example (good): "каждое утро в 9 душ, покушать, витамины. И каждые 30 минут с 9 до 11 такое же напоминание"
  → reminders: [
      {"title": "Душ, покушать, витамины", "recurrence": "0 9 * * *"},
      {"title": "Душ, покушать, витамины", "recurrence": "0,30 9-10 * * *"}
    ]
  Example (good): "напомни купить духи завтра в 10. И сегодня в 10 утра такое же"
  → reminders: [
      {"title": "Купить духи", "remind_at": "<tomorrow 10:00 ISO>"},
      {"title": "Купить духи", "remind_at": "<today 10:00 ISO>"}
    ]
  Cron tips: "каждый день в 9" → "0 9 * * *", "каждые 30 мин с 9 до 11" → "0,30 9-10 * * *",
  "каждый пн в 9" → "0 9 * * 1", "каждый час" → "0 * * * *".

POSTPONE / EDIT REMINDER (is_postpone=true):
  When user says "перенеси напоминание 42 на завтра в 10" / "напоминание 42 на +1 час" / "отложи напоминание 5":
  Return: {"is_postpone": true, "postpone_id": 42, "postpone_to": "<ISO 8601>"}
  Do NOT save a new entry. is_conversational stays false.

DONE-FOR-RECURRING (is_recurring_done=true):
  When user says "принял витамины", "сделал утренние дела", "сделал то напоминание" right after a recurring reminder fires —
  return is_close_task_command=true with close_task_id set to the matching reminder's id IF clear from context."""


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

HONESTY RULES - very important:
- You CANNOT access Telegram channel history or read old messages. You only see messages that come to you in real-time.
- You CANNOT look at the channel, check history, or retrieve past messages.
- If user asks you to "look at the channel" or "check history" - honestly say you can only see new messages as they arrive, and suggest /анализ to analyze saved entries.
- Never pretend you did something you cannot do.

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

    # Strip null values so Pydantic falls back to model defaults
    data = {k: v for k, v in data.items() if v is not None}
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

    data = {k: v for k, v in data.items() if v is not None}
    return ClassificationResult.model_validate(data)


async def analyze_tasks(entries: list[Entry]) -> str:
    if not entries:
        return "Нет сохранённых задач для анализа."

    tasks_text = "\n".join(
        f"[{e.id}] {e.title or e.content[:120]} (создана {e.created_at:%d.%m} · {e.status.value})"
        for e in entries
    )

    prompt = f"""Проанализируй список задач пользователя. Ответь на русском языке, структурировано.

ЗАДАЧИ:
{tasks_text}

Найди и опиши:
1. ПОВТОРЯЮЩИЕСЯ ТЕМЫ - задачи про одно и то же (здоровье, уборка, работа и тд)
2. ЗАВИСШИЕ ЗАДАЧИ - что давно не закрывается
3. ПАТТЕРНЫ - что пользователь часто забывает или откладывает
4. ПРИОРИТЕТЫ - что реально срочное судя по формулировкам

Будь конкретным, называй ID задач. Без лишних слов."""

    resp = await _client.chat.completions.create(
        model=TEXT_MODEL,
        messages=[{"role": "user", "content": prompt}],
        temperature=0.3,
    )
    return resp.choices[0].message.content.strip()


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
