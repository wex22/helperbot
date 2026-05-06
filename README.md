# Helperbot — Personal AI Assistant Telegram Bot

A single-user Telegram bot that acts as your AI second brain.

Send it text, voice notes, or screenshots. Every message is classified by **Gemini 2.5 Pro**, stored in **Supabase**, mirrored to **Notion** as a structured note, and — if it's a reminder — scheduled with **APScheduler** to ping you back at the right time. You also get a daily 09:00 digest, task management commands, and contextual memory (the last 20 records are fed back into the LLM on every reply).

Deployed on **Render** in webhook mode and kept warm with **UptimeRobot**.

---

## Features

1. Reads text, voice (`.ogg` → Gemini Audio natively), and photos/screenshots (Gemini Vision)
2. Classifies every message: `task` / `thought` / `idea` / `reminder` / `note`
3. Saves to Supabase: `content`, `category`, `priority` (urgent/normal/someday), `status` (open/done), `tags`
4. Reminders: parses date/time from natural language (RU/EN), stored in Supabase, APScheduler delivers at the right time
5. Recurring reminders (`каждый понедельник`, `every day at 9`)
6. Mirrors structured notes to Notion automatically
7. Daily digest at **09:00** — open tasks + reminders firing today + Gemini summary
8. `/итоги` — today + this week summary
9. `/задачи` — list of open tasks with IDs
10. Close a task by saying "сделал", "сделал 42", "done", or `/done [id]`
11. Custom tags via `#tag` in any message
12. Context memory — last 20 entries are injected into every Gemini call
13. Priority auto-detection by Gemini

---

## Architecture

```
Telegram ──webhook──▶ aiohttp (bot.py)
                       │
                       ▼
                  handlers.py ──▶ gemini_client.py ──▶ Gemini 2.5 Pro
                       │
                       ├──▶ supabase_client.py ──▶ Supabase
                       ├──▶ notion_writer.py   ──▶ Notion DB
                       └──▶ scheduler.py       ──▶ APScheduler
```

| File | Role |
|---|---|
| `bot.py` | aiohttp + aiogram entrypoint, sets webhook, boots scheduler |
| `config.py` | Env var loading via pydantic-settings |
| `models.py` | Pydantic models + enums |
| `gemini_client.py` | All Gemini calls (classify, summarize) — JSON-schema responses |
| `supabase_client.py` | DB CRUD |
| `notion_writer.py` | Notion page creation (renamed from `notion_client.py` to avoid collision with the installed `notion-client` SDK) |
| `scheduler.py` | APScheduler — reminders + daily digest, rehydrates from Supabase on boot |
| `handlers.py` | aiogram routers for text/voice/photo/commands |

---

## 1. Local setup

```bash
git clone <your-repo>
cd helperbot
python -m venv .venv
# Windows
.venv\Scripts\activate
# macOS/Linux
source .venv/bin/activate

pip install -r requirements.txt
cp .env.example .env
# fill in .env with your real keys
python bot.py
```

For local webhook testing use `ngrok http 8080` and put the HTTPS URL into `WEBHOOK_URL`.

---

## 2. Supabase

1. Create a project at [supabase.com](https://supabase.com).
2. In the SQL editor, run:

```sql
create type category as enum ('task','thought','idea','reminder','note');
create type priority as enum ('urgent','normal','someday');
create type status   as enum ('open','done');

create table entries (
  id          bigserial primary key,
  created_at  timestamptz not null default now(),
  content     text not null,
  title       text,
  category    category not null,
  priority    priority not null default 'normal',
  status      status   not null default 'open',
  tags        text[]   not null default '{}',
  source      text     not null default 'telegram',
  raw_kind    text     not null default 'text'
);
create index on entries (created_at desc);
create index on entries (category, status);

create table reminders (
  id          bigserial primary key,
  entry_id    bigint references entries(id) on delete cascade,
  remind_at   timestamptz,
  recurrence  text,
  fired       boolean not null default false,
  created_at  timestamptz not null default now()
);
create index on reminders (fired, remind_at);
```

3. Project Settings → API → copy:
   - `Project URL` → `SUPABASE_URL`
   - `service_role` key → `SUPABASE_KEY`

> The bot uses the service role key. Keep it server-side only.

---

## 3. Notion

1. Go to <https://www.notion.so/my-integrations>, create an internal integration. Copy the secret → `NOTION_TOKEN`.
2. Create a new database in Notion with these properties:
   - `Title` — Title (default)
   - `Category` — Select (options: `task`, `thought`, `idea`, `reminder`, `note`)
   - `Priority` — Select (options: `urgent`, `normal`, `someday`)
   - `Tags` — Multi-select
   - `Date` — Date
   - `Source` — Text
3. Open the database as a full page → `•••` → `Connections` → add your integration.
4. Copy the database id from the URL (the 32-char hex between the last `/` and `?v=`) → `NOTION_DB_ID`.

---

## 4. Gemini

Get an API key from [Google AI Studio](https://aistudio.google.com/app/apikey) → `GEMINI_API_KEY`.

The bot uses model `gemini-2.5-pro` while the Pro subscription is active.

### Switching to Flash after September 2026

When the Pro subscription expires, edit one line in `gemini_client.py`:

```python
# Switch to "gemini-2.0-flash" after September 2026 when Gemini Pro subscription ends.
MODEL = "gemini-2.5-pro"   # ← change to "gemini-2.0-flash"
```

That's it. No other code changes needed.

---

## 5. Telegram bot

1. Talk to [@BotFather](https://t.me/BotFather) → `/newbot` → copy the token → `BOT_TOKEN`.
2. To find your `MY_CHAT_ID`: deploy first, send `/start` to the bot, the reply contains your chat id. Put it in `MY_CHAT_ID` and redeploy.

---

## 6. Render deploy

1. Push the repo to GitHub.
2. On [Render](https://render.com): **New → Web Service** → connect repo.
3. Settings:
   - **Environment**: Python 3
   - **Build command**: `pip install -r requirements.txt`
   - **Start command**: `python bot.py`
   - **Health check path**: `/health`
4. Add environment variables:

| Var | Value |
|---|---|
| `BOT_TOKEN` | from BotFather |
| `GEMINI_API_KEY` | from AI Studio |
| `SUPABASE_URL` | from Supabase |
| `SUPABASE_KEY` | service-role key |
| `NOTION_TOKEN` | integration secret |
| `NOTION_DB_ID` | 32-char hex |
| `WEBHOOK_URL` | `https://<your-service>.onrender.com` (no trailing slash) |
| `MY_CHAT_ID` | your Telegram numeric id |
| `TZ` | `Europe/Warsaw` (or your IANA tz) |
| `WEBHOOK_SECRET` | any long random string |
| `PORT` | leave unset — Render injects it |

5. Deploy. On boot you'll see in logs: `Webhook set to https://....onrender.com/telegram`.

---

## 7. UptimeRobot (keep Render free tier alive)

Render free services sleep after 15 min of inactivity. To keep the bot warm:

1. Sign up at [uptimerobot.com](https://uptimerobot.com).
2. **Add New Monitor** → **HTTP(s)**.
3. URL: `https://<your-service>.onrender.com/health`
4. Monitoring interval: **5 minutes**.
5. Save.

The `/health` endpoint returns `200 ok`. UptimeRobot pinging every 5 min keeps the dyno from sleeping.

---

## Commands cheat sheet

| Command | What it does |
|---|---|
| `/start` | Health check, prints your chat id |
| `/задачи` | List open tasks with IDs |
| `/done` | Close most recent open task |
| `/done 42` | Close task #42 |
| `сделал` / `сделал 42` / `done` | Same as `/done` (plain text) |
| `/итоги` | Gemini-summarized digest of today + this week |

Plus: any text, voice, or photo message gets classified, stored, mirrored to Notion, and (if it's a reminder) scheduled.

---

## Verification checklist

- [ ] Send a text msg — confirm row in `entries` and Notion page
- [ ] Send a voice note — confirm `transcript` populated
- [ ] Send a photo — confirm Gemini description in `content`
- [ ] `напомни через 2 минуты ...` — reminder fires within ~2 min
- [ ] `каждый день в 9:30 ...` — recurring job persists across restart (rehydrate)
- [ ] `сделал` — most recent task flips to `done`
- [ ] Wait for 09:00 digest, or temporarily set the cron earlier in `scheduler.py`
- [ ] `/health` returns 200 from the public URL

---

## Troubleshooting

- **No replies at all**: check the bot logs in Render for `Webhook set to ...`. Confirm `MY_CHAT_ID` matches the chat id printed by `/start`.
- **Supabase says `Name or service not known`**: check Render's `SUPABASE_URL`. It must be the Supabase **Project URL** from Project Settings → API, like `https://<project-ref>.supabase.co`.
- **Gemini returns non-JSON** error: usually a model overload / safety block. The bot logs the raw response — retry the message.
- **Reminders don't fire after a redeploy**: rehydrate runs on startup. Check that the row in `reminders` has `fired=false` and a future `remind_at`.
- **Render keeps sleeping**: confirm UptimeRobot is hitting `/health` with status 200 every 5 min.
