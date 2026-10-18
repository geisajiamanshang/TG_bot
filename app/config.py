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
    google_spreadsheet_id: str = "1NuEckgwIEjzegpktBx4O6UxjFXl32LruM1j9JsYVOGw"
    google_roster_spreadsheet_id: str = "1DgnePrJOwA0sa8v4wJbv3RjFmdXvhGVN9f1TrTuzJng"
    google_service_account_file: str = "/run/secrets/google-service-account.json"
    wallet_spreadsheet_id: str = "1SrvH7LuKqlekXzUUxbMAY3VNT54ApXNqG6bki9bnhXQ"
    wallet_assistant_chat_id: int | None = None
    wallet_workflow_enabled: bool = False
    # /onboarding_notify 用：恒睿公司-联合管理工作群（新人入职通知）、人事数据同步-SSC3组（入职信息同步）。
    # 机器人必须已经是这两个群的成员才能发消息；群 chat_id 通常是负数。
    onboarding_notify_group_chat_id: int | None = None
    onboarding_sync_group_chat_id: int | None = None
    # /probation_notify 用：恒睿公司-联合管理群（预转正提醒，和上面的 onboarding_notify_group_chat_id
    # 是同一个群，这里保留一个独立字段方便以后拆开配置）、人事数据同步-SSC3组（转正信息同步，注意这是
    # 另一个和上面 onboarding_sync_group_chat_id 不同的群）。
    probation_union_group_chat_id: int | None = None
    probation_sync_group_chat_id: int | None = None
    # 转正海报所在的 Google Drive 文件夹 ID（服务账号必须被额外共享该文件夹的查看权限）。
    probation_poster_drive_folder_id: str = "1ow3D3vRgUSQ6PctbcoAmbdNkjzh3kF83"

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
