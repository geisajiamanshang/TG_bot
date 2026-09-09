"""Fixed HR business-rule defaults for new-hire roster entries, per the
company's "花名册填写规范" / "变更记录表填写规范" (2026-09-06 spec).

Everything here is deterministic (constants or simple date math) and is only
ever applied for NEW roster rows (is_new=True) — never for later field
updates — and only ever fills a gap, never overrides a value GPT/the admin
already extracted from the actual message.
"""

from __future__ import annotations

from datetime import date, timedelta


# 业务BP对口分工：部门/小组关键词 -> "姓名 @tg handle"
# Order matters only in that the first matching group wins; the keyword sets
# below don't overlap in practice.
HRBP_MAPPING: list[tuple[tuple[str, ...], str]] = [
    (("运营一部", "商务部", "渠道部"), "冯晚柠 @FWN066"),
    (("运营二部", "ACFan产品组", "ACFAN特战队", "app运营组", "品牌组"), "段奕宏 @wean4790"),
    (("技术部",), "星榆 @feang99568"),
    (("效能部", "内容组", "SEO组"), "林尔康 @linerkang"),
]

# Fixed defaults applied to a brand-new roster row when the field is still
# empty after GPT extraction + reference-fill. Keys are the same style of
# logical field name used elsewhere (see EXTRA_ORG_COLUMNS / EXTRA_HR_DEFAULT_COLUMNS
# in google_sheets_service.py for the column-letter mapping).
NEW_HIRE_STATIC_DEFAULTS: dict[str, str] = {
    "job_sequence": "派驻",       # 序列
    "service_entity": "恒睿",     # 服务单位
    "mgmt_sequence": "个人贡献者",  # 管理序列
    "is_key_position": "否",      # 是否关键岗位
    "mgmt_headcount": "0",        # 管理人数
    "work_mode": "远程",          # 办公方式
    "salary_basis": "月薪",       # 标准月薪口径
    "employment_type": "全职",    # 用工类型
    "establishment_attribute": "正编",  # 编制属性
    "establishment_status": "在编",     # 编制状态
    "probation_months": "2",      # 试用期（月）
    "tenure_years": "0.0",        # 司龄（年）
    "latest_change_type": "入职",  # 最近异动类型
    "latest_change_reason": "新人入职",  # 最近异动原因
}


def resolve_hrbp(*texts: str) -> str | None:
    """Match department/team/org_hint free text against the HRBP mapping
    table. Returns the mapped "姓名 @handle" string, or None if nothing
    matches — callers should fall back to reference-fill or leave blank,
    never guess.
    """
    haystack = " ".join(t for t in texts if t)
    if not haystack.strip():
        return None
    for keywords, hrbp in HRBP_MAPPING:
        if any(keyword in haystack for keyword in keywords):
            return hrbp
    return None


def normalize_region_like(value: str) -> str:
    """Shared normalization for 国籍/办公地区-style fields: 香港/台湾 (with or
    without "中国" already prefixed) become "中国香港"/"中国台湾"; anything
    else is returned unchanged (never invented).
    """
    text = (value or "").strip()
    if not text:
        return text
    if "香港" in text:
        return "中国香港"
    if "台湾" in text:
        return "中国台湾"
    return text


def default_nationality(value: str) -> str:
    text = normalize_region_like(value)
    return text or "中国"


def default_office_region(value: str) -> str:
    text = normalize_region_like(value)
    return text or "中国大陆"


def probation_end_date(effective_date: str) -> str:
    """入职日期 + 2 个月 - 1 天. Returns "" if effective_date isn't a valid
    YYYY-MM-DD string (never guesses a date)."""
    try:
        year, month, day = (int(part) for part in effective_date.split("-"))
        start = date(year, month, day)
    except (ValueError, AttributeError):
        return ""
    month_index = start.month - 1 + 2
    end_year = start.year + month_index // 12
    end_month = month_index % 12 + 1
    # Clamp day to the target month's last day (e.g. Jan 31 + 2mo -> Mar 31,
    # not an invalid Mar 31... but Jan 31 + 1mo would need clamping to Feb).
    if end_month == 12:
        next_month_first = date(end_year + 1, 1, 1)
    else:
        next_month_first = date(end_year, end_month + 1, 1)
    last_day_of_target_month = (next_month_first - timedelta(days=1)).day
    day = min(start.day, last_day_of_target_month)
    plus_two_months = date(end_year, end_month, day)
    return (plus_two_months - timedelta(days=1)).isoformat()
