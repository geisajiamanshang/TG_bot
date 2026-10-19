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
    # 转正表单链接所在的 Google Drive 根文件夹 ID（"2-试用期管理"，下面按"<年>-<月>月转正明细"
    # 这样的子文件夹分月存放，服务账号也要被额外共享这个文件夹的查看权限）。
    probation_form_drive_folder_id: str = "17mscwGo9mdwQg59AJRkIwSxM-M_Sgchz"
    # 入职协议上传的 Google Drive 根文件夹 ID（北斗-恒睿/人事/入职协议，下面按"<年>年-<月>月"这样
    # 的子文件夹分月存放，没有就自动新建）。服务账号必须被额外共享这个文件夹的【编辑者】权限
    # （不是查看者），因为这个功能需要往里面写文件，不只是读。
    onboarding_agreement_drive_folder_id: str = "1tmNkfhM35ZbYYXu1UncC7dZCsbx_BUJq"
    # /training_notify 用：人事数据同步-SSC3组（次日培训信息同步）。注意这里不单独起一个字段，
    # 直接复用 probation_sync_group_chat_id——这就是"转正信息同步"实际在用、已经验证能发送成功
    # 的那个群 chat_id。之前单独给了一个硬编码默认值，结果和用户 .env 里真正配置的
    # PROBATION_SYNC_GROUP_CHAT_ID 不一致，导致 "chat not found"；复用同一个字段就不会再对不上。

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
