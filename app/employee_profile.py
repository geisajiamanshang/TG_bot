from __future__ import annotations

import re
from datetime import datetime


FIELD_LABELS = {
    "员工编码": "employee_code",
    "员工编码（新）": "employee_code",
    "编号": "employee_code",
    "中文花名": "chinese_name",
    "花名": "chinese_name",
    "姓名/简历名": "resume_name",
    "简历名": "resume_name",
    "候选人姓名": "resume_name",
    "在职状态": "employment_status",
    "生效日期": "effective_date",
    "日期": "effective_date",
    "入职日期": "effective_date",
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
    "工作tg": "work_tg",
    "工作TG（@TG名）": "work_tg",
    "私人联系方式": "private_contact",
    "私人TG": "private_contact",
    "私人tg": "private_contact",
    "候选人联系方式": "private_contact",
    "私人联系方式（@TG名, 可同上）": "private_contact",
    "私人联系方式（@TG名，可同上）": "private_contact",
    "工作邮箱": "work_email",
    "工作gmail": "work_email",
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
    return re.sub(r"\s+", "", label.strip()).replace(":", "").replace("：", "").casefold()


NORMALIZED_LABELS = {_normalize_label(key): value for key, value in FIELD_LABELS.items()}


def normalize_date(value: str) -> str:
    """Normalize common HR message date formats to YYYY-MM-DD."""
    raw = value.strip()
    match = re.search(
        r"(?<!\d)(\d{4})\s*(?:年|[-/.])\s*(\d{1,2})\s*(?:月|[-/.])\s*(\d{1,2})(?:\s*日)?",
        raw,
    )
    if not match:
        short_match = re.search(
            r"(?<!\d)(\d{1,2})\s*(?:月|[-/.])\s*(\d{1,2})(?:\s*日)?",
            raw,
        )
        if not short_match:
            return raw
        year = datetime.now().year
        month, day = (int(part) for part in short_match.groups())
    else:
        year, month, day = (int(part) for part in match.groups())
    try:
        return datetime(year, month, day).strftime("%Y-%m-%d")
    except ValueError:
        return raw


def parse_profile_message(text: str) -> tuple[str | None, dict[str, str]]:
    mode = "new" if has_onboarding_keyword(text) else "update" if "字段变更" in text else None
    values: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip().replace("&#x20;", "")
        if not line or "：" not in line and ":" not in line:
            continue
        label, value = re.split(r"[：:]", line, maxsplit=1)
        key = NORMALIZED_LABELS.get(_normalize_label(label))
        value = value.strip()
        # Birthday submissions commonly keep the template prefix, e.g.
        # "生日月份：如11月". Treat that as an explicit month; other "如..."
        # values remain examples and are ignored.
        if key and value and (not value.startswith("如") or key == "birthday_month"):
            if key == "effective_date":
                value = normalize_date(value)
            elif key == "birthday_month":
                month_match = re.search(r"(1[0-2]|[1-9])", value)
                value = f"{int(month_match.group(1))}月" if month_match else value
            values[key] = value
    return mode, values


def has_onboarding_keyword(text: str) -> bool:
    return any(keyword in text for keyword in ("新人入职", "入职信息确认"))


def person_name_keys(value: str) -> set[str]:
    """Comparable full-name and Chinese-name keys for bilingual aliases."""
    normalized = "".join(
        char.casefold() for char in value.strip()
        if char.isalnum() or "\u4e00" <= char <= "\u9fff"
    )
    chinese = "".join(char for char in normalized if "\u4e00" <= char <= "\u9fff")
    return {key for key in (normalized, chinese) if len(key) >= 2}


EXTRA_FIELD_LABELS = {
    "入职编制组织": "org_unit",
    "编制组织": "org_unit",
    "入职服务单位": "service_entity",
    "服务单位": "service_entity",
    "人员性质": "job_sequence",
    "入职部门": "department",
    "部门": "department",
    "入职小组": "team",
    "小组": "team",
    "岗位类型": "position_type",
    "职位": "position_title",
    "管理序列": "mgmt_sequence",
    "办公方式": "work_mode",
    "招聘渠道": "recruitment_channel",
    "简历来源": "resume_source",
    "直属上级": "direct_supervisor",
    "直接上级": "direct_supervisor",
    "薪资货币": "salary_currency",
    "试用薪资": "trial_salary",
    "试用期薪资": "trial_salary",
    "转正薪资": "confirmed_salary",
    "转正后薪资": "confirmed_salary",
}
NORMALIZED_EXTRA_LABELS = {
    _normalize_label(key): value for key, value in EXTRA_FIELD_LABELS.items()
}


def _normalize_salary(value: str) -> str:
    match = re.search(r"(\d+(?:\.\d+)?)\s*([kK万]?)", value.replace(",", ""))
    if not match:
        return ""
    amount = float(match.group(1))
    if match.group(2).casefold() == "k":
        amount *= 1000
    elif match.group(2) == "万":
        amount *= 10000
    return str(int(amount))


def parse_extra_profile_fields(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip().replace("\xa0", " ")
        if "：" not in line and ":" not in line:
            continue
        label, value = re.split(r"[：:]", line, maxsplit=1)
        normalized_label = _normalize_label(re.sub(r"^[\d️⃣]+", "", label))
        value = value.strip()
        key = NORMALIZED_EXTRA_LABELS.get(normalized_label)
        if key and value:
            values[key] = value
        if normalized_label == _normalize_label("建议职级") and value:
            level = re.match(r"\s*([A-Za-z]+\d+)(?:[-－](\d+))?", value)
            if level:
                values["job_level"] = level.group(1).upper()
                if level.group(2):
                    values["job_grade"] = level.group(2)
    for salary_key in ("trial_salary", "confirmed_salary"):
        if salary_key in values:
            values[salary_key] = _normalize_salary(values[salary_key])
    if values.get("salary_currency", "").upper() in {"RMB", "人民币"}:
        values["salary_currency"] = "CNY"
    if values.get("mgmt_sequence") == "个人":
        values["mgmt_sequence"] = "个人贡献者"
    channel = values.get("recruitment_channel", "")
    if channel.endswith("招聘部"):
        values["recruitment_channel"] = channel[:-3] + "渠道"
    department = values.get("department", "")
    if "-" in department or "－" in department:
        parts = [part.strip() for part in re.split(r"[-－]", department, maxsplit=1)]
        if len(parts) == 2 and all(parts):
            values["department"] = re.sub(r"(?i)acfan", "ACFAN", parts[0])
            values.setdefault("team", parts[1])
    return {key: value for key, value in values.items() if value}


def validate_profile(values: dict[str, str], is_new: bool) -> list[str]:
    errors: list[str] = []
    if values.get("employee_code") and len(values["employee_code"].strip()) != 6:
        errors.append("员工编码必须为6位；候选人编码请勿填写到员工编码")
    if values.get("birthday_month") and values["birthday_month"] not in {
        f"{month}月" for month in range(1, 13)
    }:
        errors.append("生日月份必须为1月到12月")
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
    if cleaned.get("birthday_month"):
        month_match = re.search(r"(1[0-2]|[1-9])", cleaned["birthday_month"])
        if month_match:
            cleaned["birthday_month"] = f"{int(month_match.group(1))}月"
        else:
            cleaned.pop("birthday_month", None)
    if cleaned.get("employee_code") and len(cleaned["employee_code"].strip()) != 6:
        cleaned.pop("employee_code", None)
    date_value = cleaned.get("effective_date")
    if date_value:
        date_value = normalize_date(date_value)
        cleaned["effective_date"] = date_value
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
