from functools import lru_cache

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    telegram_bot_token: str = Field(min_length=10)
    openai_api_key: str = ""
    openai_model: str = "gpt-4o-mini"
    openai_vision_model: str = "gpt-4o-mini"
    admin_user_ids: frozenset[int]
    database_path: str = "/data/ssc_bot.db"
    company_name: str = "Your Company"
    support_contact: str = "SSC 人工客服"
    bot_language: str = "zh-CN"
    broadcast_messages_per_second: float = Field(default=20, gt=0, le=25)
    google_spreadsheet_id: str = "1OoNt1C0YyO4pBjle7eQjbuuepOpKk1gK8JlumdagGWM"
    google_roster_spreadsheet_id: str = "1DgnePrJOwA0sa8v4wJbv3RjFmdXvhGVN9f1TrTuzJng"
    google_service_account_file: str = "/run/secrets/google-service-account.json"
    wallet_spreadsheet_id: str = "1SrvH7LuKqlekXzUUxbMAY3VNT54ApXNqG6bki9bnhXQ"
    wallet_assistant_chat_id: int | None = None
    wallet_workflow_enabled: bool = False

    @field_validator("admin_user_ids", mode="before")
    @classmethod
    def parse_admin_ids(cls, value: object) -> frozenset[int]:
        if isinstance(value, str):
            values = [part.strip() for part in value.split(",") if part.strip()]
            return frozenset(int(part) for part in values)
        if isinstance(value, int):
            return frozenset({value})
        return frozenset(value)  # type: ignore[arg-type]


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
