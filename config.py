from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    BOT_TOKEN: str
    GEMINI_API_KEY: str
    SUPABASE_URL: str
    SUPABASE_KEY: str
    NOTION_TOKEN: str
    NOTION_DB_ID: str
    WEBHOOK_URL: str
    MY_CHAT_ID: int
    TZ: str = "Europe/Moscow"
    PORT: int = 8080
    WEBHOOK_SECRET: str = "change-me"
    WEBHOOK_PATH: str = "/telegram"


settings = Settings()
