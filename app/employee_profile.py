from __future__ import annotations

import re
from datetime import datetime


FIELD_LABELS = {
    "员工编码": "employee_code",
    "员工编码（新）": "employee_code",
    "中文花名": "chinese_name",
    "花名": "chinese_name",
    "姓名/简历名": "resume_name",
    "简历名": "resume_name",
    "候选人姓名": "resume_name",
    "在职状态": "employment_status",
    "生效日期": "effective_date",
    "生效日期(入职日如2026-06-01)": "effective_date",
    "生效日期（入职日如2026-06-01）": "effective_date",
    "性别": "gender",
    "年龄区间": "age_range",
    "学历": "education",
    "国籍": "nationality",
    "生日月份": "birthday_month",
    "办公地区": "office_region",
    "办公地区（中国/其他国家）": "office_region",
    "工作TG": "work_tg",
    "工作TG（@TG名）": "work_tg",
    "私人联系方式": "private_contact",
    "私人联系方式（@TG名, 可同上）": "private_contact",
    "私人联系方式（@TG名，可同上）": "private_contact",
    "工作邮箱": "work_email",
}

DISPLAY_LABELS = {
    "employee_code": "员工编码", "chinese_name": "中文花名", "resume_name": "姓名/简历名",
    "employment_status": "在职状态", "effective_date": "生效日期", "gender": "性别",
    "age_range": "年龄区间", "education": "学历", "nationality": "国籍",
    "birthday_month": "生日月份", "office_region": "办公地区", "work_tg": "工作TG",
    "private_contact": "私人联系方式", "work_email": "工作邮箱",
}

PROFILE_FIELDS = tuple(DISPLAY_LABELS)
NEW_REQUIRED_FIELDS = ("employee_code", "chinese_name", "resume_name", "effective_date")


def _normalize_label(label: str) -> str:
    return re.sub(r"\s+", "", label.strip()).replace(":", "").replace("：", "")


NORMALIZED_LABELS = {_normalize_label(key): value for key, value in FIELD_LABELS.items()}


def parse_profile_message(text: str) -> tuple[str | None, dict[str, str]]:
    mode = "new" if "新人入职" in text[:60] else "update" if "字段变更" in text[:60] else None
    values: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip().replace("&#x20;", "")
        if not line or "：" not in line and ":" not in line:
            continue
        label, value = re.split(r"[：:]", line, maxsplit=1)
        key = NORMALIZED_LABELS.get(_normalize_label(label))
        value = value.strip()
        if key and value and not value.startswith("如"):
            values[key] = value
    if mode is None and values:
        mode = "update"
    return mode, values


def validate_profile(values: dict[str, str], is_new: bool) -> list[str]:
    errors: list[str] = []
    if is_new:
        for key in NEW_REQUIRED_FIELDS:
            if not values.get(key):
                errors.append(f"缺少{DISPLAY_LABELS[key]}")
    date_value = values.get("effective_date")
    if date_value:
        try:
            datetime.strptime(date_value, "%Y-%m-%d")
        except ValueError:
            errors.append("生效日期请使用 YYYY-MM-DD，例如 2026-06-01")
    allowed = {
        "employment_status": {"在职", "试用期", "调入", "调出", "停薪留职"},
        "gender": {"男", "女"},
        "age_range": {"25以下", "25 以下", "26-30", "31-35", "36-40", "41-45", "46-50", "51以上", "51 以上"},
        "education": {"本科", "博士", "大专", "高中及以下", "硕士"},
    }
    for key, options in allowed.items():
        if values.get(key) and values[key] not in options:
            errors.append(f"{DISPLAY_LABELS[key]}格式不正确")
    return errors


def clean_screenshot_values(values: dict[str, str]) -> dict[str, str]:
    """Drop uncertain OCR values so their sheet cells remain blank."""
    cleaned = {key: value for key, value in values.items() if value.strip()}
    date_value = cleaned.get("effective_date")
    if date_value:
        try:
            datetime.strptime(date_value, "%Y-%m-%d")
        except ValueError:
            cleaned.pop("effective_date", None)
    allowed = {
        "employment_status": {"在职", "试用期", "调入", "调出", "停薪留职"},
        "gender": {"男", "女"},
        "age_range": {"25以下", "25 以下", "26-30", "31-35", "36-40", "41-45", "46-50", "51以上", "51 以上"},
        "education": {"本科", "博士", "大专", "高中及以下", "硕士"},
    }
    for key, options in allowed.items():
        if cleaned.get(key) and cleaned[key] not in options:
            cleaned.pop(key, None)
    return cleaned


def profile_text(profile: dict[str, object]) -> str:
    lines = ["员工档案"]
    username = profile.get("telegram_username")
    lines.append(f"Telegram：@{username}" if username else f"Telegram ID：{profile['telegram_user_id']}")
    for key in PROFILE_FIELDS:
        value = profile.get(key)
        if value:
            lines.append(f"{DISPLAY_LABELS[key]}：{value}")
    lines.append(f"云表同步：{profile.get('sync_status') or 'pending'}")
    return "\n".join(lines)
