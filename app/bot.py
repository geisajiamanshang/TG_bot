from __future__ import annotations

import asyncio
import hashlib
import io
import logging
import random
import re
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any, Awaitable, Callable
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher, F, Router
from aiogram import BaseMiddleware
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    BotCommand,
    BotCommandScopeChat,
    BotCommandScopeDefault,
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from app.config import Settings
from app.database import Database, Employee
from app.employee_profile import (
    DISPLAY_LABELS,
    clean_screenshot_values,
    has_onboarding_keyword,
    parse_extra_profile_fields,
    parse_profile_message,
    profile_text,
    validate_profile,
)
from app.google_sheets_service import GoogleSheetsService, parse_sheet_date
from app.gpt_service import GPTService
from app.vision_service import VisionService
from app import hr_defaults
from app.onboarding_agreement import agreement_filename, build_onboarding_agreement, month_folder_name

logger = logging.getLogger(__name__)
ATTENDANCE_TIMEZONE = ZoneInfo("Asia/Shanghai")


class InboundAuditMiddleware(BaseMiddleware):
    def __init__(self, services: "Services") -> None:
        self.services = services

    async def __call__(
        self,
        handler: Callable[[Message, dict[str, Any]], Awaitable[Any]],
        event: Message,
        data: dict[str, Any],
    ) -> Any:
        user = event.from_user
        if user:
            content_type = str(event.content_type)
            content = event.text or event.caption
            if not content:
                content = f"[{content_type}]"
            await self.services.db.save_inbound_message(
                user.id, event.chat.id, event.message_id, user.username,
                user.full_name, content_type, content,
            )
            if _is_admin(user.id, self.services.settings):
                state: FSMContext | None = data.get("state")
                current_state = await state.get_state() if state else None
                # NOTE: event.content_type is an aiogram ContentType enum member; str() on it
                # yields "ContentType.PHOTO", not "photo", so comparing against the plain
                # string literal here always failed and silently swallowed every screenshot
                # sent during an attendance-check session. Check the actual payload instead.
                is_image = bool(event.photo) or bool(
                    event.document and (event.document.mime_type or "").startswith("image/")
                )
                is_cancel = bool(event.text and event.text.strip().casefold().startswith("/cancel"))
                # 截图识别不出来的时候，允许管理员直接发花名文字兜底（"张三 李四" /
                # "张三，李四"），只要不是斜杠命令就当作花名文字放行，交给下面的
                # attendance_check_text_names 处理；其它命令（/cancel 除外）仍然拦截提醒。
                is_name_list = bool(
                    event.text and event.text.strip() and not event.text.strip().startswith("/")
                )
                if (
                    current_state == AttendanceCheck.waiting_for_screenshot.state
                    and not is_image and not is_cancel and not is_name_list
                ):
                    await data["bot"].send_message(
                        event.chat.id,
                        "考勤抽查模式仍在运行，请继续发送截图；只有 /cancel 才会退出。",
                    )
                    return None
            if not _is_admin(user.id, self.services.settings):
                bot: Bot = data["bot"]
                now = datetime.now(UTC)
                is_screenshot = bool(event.photo) or bool(
                    event.document and (event.document.mime_type or "").startswith("image/")
                )
                attendance_result = await self.services.db.record_attendance_response(
                    user.id,
                    now.astimezone(ATTENDANCE_TIMEZONE).date().isoformat(),
                    now,
                    is_screenshot,
                )
                attendance_status = str(attendance_result.get("status") or "") if attendance_result else ""
                username = f"@{user.username}" if user.username else "无 username"
                header = (
                    f"📨 员工新消息\n发送人：{user.full_name} · {username}\n"
                    f"Telegram ID：{user.id}\n类型：{content_type}"
                )
                for admin_id in self.services.settings.admin_user_ids:
                    try:
                        await bot.send_message(admin_id, header)
                        await bot.copy_message(admin_id, event.chat.id, event.message_id)
                    except Exception:
                        logger.exception("Failed to mirror inbound message to admin %s", admin_id)
                if attendance_result and attendance_result.get("notify_late"):
                    elapsed_seconds = int(attendance_result.get("elapsed_seconds") or 0)
                    elapsed_minutes, seconds = divmod(elapsed_seconds, 60)
                    late_notice = (
                        f"{attendance_result['chinese_name']}-考勤消息慢回（超过20分钟），"
                        f"扣10 USD/次\n回复用时：{elapsed_minutes}分{seconds}秒"
                    )
                    for admin_id in self.services.settings.admin_user_ids:
                        try:
                            await bot.send_message(admin_id, late_notice)
                        except Exception:
                            logger.exception("Failed to notify admin %s about late reply", admin_id)
                if attendance_status == "screenshot_received":
                    await bot.send_message(
                        event.chat.id,
                        "后台审核中.....如未接到人工联系说明审核通过",
                    )
                    return None
                if attendance_status == "on_time":
                    await bot.send_message(event.chat.id, "✅ 按时响应，感谢配合")
                    return None
                if attendance_status == "late_waiting_screenshot":
                    await bot.send_message(
                        event.chat.id,
                        "⚠️超时回复，请发送接近抽查时间的带有时间戳的相关工作截图，后台会审核",
                    )
                    return None
        return await handler(event, data)


class Broadcast(StatesGroup):
    selecting_recipients = State()
    waiting_for_content = State()
    waiting_for_confirmation = State()


class AttendanceCheck(StatesGroup):
    waiting_for_screenshot = State()


class ChannelMessageSend(StatesGroup):
    waiting_for_confirmation = State()


class SupervisorNotify(StatesGroup):
    waiting_for_confirmation = State()


class OnboardingNotify(StatesGroup):
    waiting_for_confirmation = State()
    # 发送到群之前，管理员可以先修改【新人入职通知】/【入职信息同步】的内容（整段替换，不是
    # 增量编辑）。waiting_for_edit_choice 等管理员选要改哪一段，waiting_for_edit_text 等管理员
    # 发来替换后的新内容。
    waiting_for_edit_choice = State()
    waiting_for_edit_text = State()


class ProbationNotify(StatesGroup):
    # 1/2/2.1/2.2/3 可以按任意顺序、各自独立地回复，所以用同一个 state 一直等，直到 /cancel。
    waiting_for_action = State()


class EditSentMessage(StatesGroup):
    # 入职通知/入职信息同步/转正提醒/转正通知/转正信息同步 发到群之后，管理员点"✏️ 编辑这条消息"
    # 按钮进入这个状态，回复新内容就用 bot 自己的身份把群里那条消息编辑掉（普通用户改不了机器人发的
    # 消息，只有机器人自己能编辑）。
    waiting_for_new_text = State()


def _edit_sent_button(chat_id: int, message_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✏️ 编辑这条消息", callback_data=f"editsent:{chat_id}:{message_id}")
    ]])


def build_supervisor_groups(
    hires: list[dict[str, str]],
    supervisor_contacts: list[dict[str, str]],
) -> tuple[dict[str, dict[str, object]], list[str]]:
    """把今日入职新人按直接上级的工作TG分组。

    返回 (groups, issues)：groups 以上级工作TG（小写、不带@）为键，值含
    supervisor 花名、work_tg 和 hires 列表；issues 是无法通知的原因说明。
    """
    def key(value: object) -> str:
        return "".join(str(value or "").split()).casefold()

    contact_by_name = {key(item.get("requested_name")): item for item in supervisor_contacts}
    groups: dict[str, dict[str, object]] = {}
    issues: list[str] = []
    for hire in hires:
        supervisor = str(hire.get("supervisor") or "").strip().lstrip("@")
        label = f"{hire['chinese_name']}（第 {hire['row']} 行）"
        if not supervisor:
            issues.append(f"{label}：AA 列直接上级为空")
            continue
        contact = contact_by_name.get(key(supervisor), {"status": "not_found"})
        status = contact.get("status")
        if status == "not_found":
            issues.append(f"{label}：直接上级“{supervisor}”在花名册 C 列中找不到")
            continue
        if status == "missing_tg":
            issues.append(f"{label}：直接上级“{supervisor}”的 AE 列工作TG为空")
            continue
        if status == "ambiguous":
            issues.append(f"{label}：直接上级“{supervisor}”有多个同名行且 AE 列不一致，需人工核对")
            continue
        work_tg = str(contact.get("work_tg") or "")
        group = groups.setdefault(work_tg.lstrip("@").casefold(), {
            "supervisor": str(contact.get("chinese_name") or supervisor),
            "work_tg": work_tg,
            "hires": [],
        })
        group["hires"].append(hire)  # type: ignore[union-attr]
    return groups, issues


def supervisor_notice_text(supervisor: str, hires: list[dict[str, str]], today: str) -> str:
    lines = [
        "【今日入职新人通知】",
        f"{supervisor}您好！以下新同事今天（{today}）入职，直接上级为您，请知悉并及时联系：",
    ]
    for index, hire in enumerate(hires, 1):
        resume_name = hire.get("resume_name") or "简历名待补充"
        lines.append(f"{index}. {hire['chinese_name']}｜{resume_name}｜{hire.get('work_tg') or '工作TG待补充'}")
    return "\n".join(lines)


# ---- /onboarding_notify：今日入职 -> 【新人入职通知】【入职信息同步】两个群通知的生成规则 ----
# 规则来源：用户上传的模版文件（2026-10-17），不要凭记忆改动，以模版原文为准。

# 备注行 @ 顺序固定为：最先 DaBai10010 -> 直接/间接上级级别的人 -> HRBP -> 最后 wean4790。
ONBOARDING_MENTION_FIRST: tuple[str, ...] = ("DaBai10010",)
ONBOARDING_MENTION_LAST: tuple[str, ...] = ("wean4790",)
ONBOARDING_SUPERVISOR_HANDLES = frozenset(
    {"zlei1216", "xxs202215cz2025", "liyuanba666", "wdz999", "haohaoganhuoshaodapao"}
)
ONBOARDING_HRBP_HANDLES = frozenset({"FWN066", "feang99568", "kcin895"})

# (部门, 小组) 精确匹配优先于按部门匹配。
# 注意：这里的 linerkang 就是 linerkang，不是 haohaoganhuoshaodapao —— 之前因为"直接/间接
# 上级"名单里出现了 haohaoganhuoshaodapao 这个没见过的 handle，错误地把这里的 linerkang 当成
# 被替换掉了，实际上两者是完全不相关的两个人，haohaoganhuoshaodapao 只在 /probation_notify
# 的渠道拓展部/传统销售部规则里出现（那是按模版原文来的，没有问题）。
ONBOARDING_TEAM_MENTIONS: dict[tuple[str, str], tuple[str, ...]] = {
    ("ACFAN特战队", "内容组"): ("linerkang", "wean4790"),
    ("ACFAN特战队", "SEO组"): ("linerkang", "wean4790"),
    ("研发部", "前端组"): ("wdz999", "feang99568", "kcin895"),
    ("研发部", "后端组"): ("liyuanba666", "feang99568", "kcin895"),
}
ONBOARDING_DEPARTMENT_MENTIONS: dict[str, tuple[str, ...]] = {
    "效能部": ("linerkang", "wean4790"),
    "AIGC原创部": ("linerkang", "wean4790"),
    "运营一部": ("FWN066", "zlei1216"),
    "运营二部": ("xxs202215cz2025", "wean4790"),
    # 其他未命中具体小组规则的 ACFAN特战队。
    "ACFAN特战队": ("wean4790", "zlei1216"),
}
# 这两个部门：私人TG/工作TG/工作Gmail 三个字段都要写（没有就写"待补充"）。
# 其他部门：只写私人TG，工作TG 和 工作Gmail 这两行整行不写。
ONBOARDING_WORK_CONTACT_DEPARTMENTS = ("研发部", "效能部")

_ONBOARDING_NUMERAL_MAP = str.maketrans({"一": "1", "二": "2", "三": "3", "四": "4", "五": "5"})


def _onboarding_normalize(text: str) -> str:
    """部门/小组字段做模糊匹配前的归一化：去空格、中文数字转阿拉伯数字。"""
    return text.strip().translate(_ONBOARDING_NUMERAL_MAP)


def _onboarding_department_matches(department: str, key: str) -> bool:
    """real 花名册 department 字段经常是"部门+小组"连写、或中文数字写成阿拉伯数字
    （如 "运营1部产品运营组"），所以用归一化后的包含匹配，而不是精确相等。"""
    norm_department = _onboarding_normalize(department)
    norm_key = _onboarding_normalize(key)
    return norm_key in norm_department


def _onboarding_mentions_for(department: str, team: str) -> tuple[str, ...]:
    department = department.strip()
    team = team.strip()
    # 团队级规则优先于部门级规则，同样用归一化包含匹配。
    for (team_dept, team_name), handles in ONBOARDING_TEAM_MENTIONS.items():
        if _onboarding_department_matches(department, team_dept) and _onboarding_department_matches(team, team_name):
            return handles
    for dept_key, handles in ONBOARDING_DEPARTMENT_MENTIONS.items():
        if _onboarding_department_matches(department, dept_key):
            return handles
    return ()


def _onboarding_has_work_contact_fields(department: str) -> bool:
    return any(_onboarding_department_matches(department, key) for key in ONBOARDING_WORK_CONTACT_DEPARTMENTS)


def _onboarding_all_mentions(candidates: list[dict[str, str]]) -> list[str]:
    """所有候选人适用规则的并集去重，按固定顺序排列：
    最先 DaBai10010 -> 直接/间接上级级别的人 -> 其他未分类的人 -> HRBP -> 最后 wean4790。"""
    seen: set[str] = set(ONBOARDING_MENTION_FIRST) | set(ONBOARDING_MENTION_LAST)
    supervisor_tier: list[str] = []
    hrbp_tier: list[str] = []
    other_tier: list[str] = []

    def classify(handle: str) -> None:
        handle = handle.strip()
        if not handle or handle in seen:
            return
        seen.add(handle)
        if handle in ONBOARDING_SUPERVISOR_HANDLES:
            supervisor_tier.append(handle)
        elif handle in ONBOARDING_HRBP_HANDLES:
            hrbp_tier.append(handle)
        else:
            other_tier.append(handle)

    for candidate in candidates:
        for handle in _onboarding_mentions_for(
            str(candidate.get("department") or ""), str(candidate.get("team") or "")
        ):
            classify(handle)
    return [
        *ONBOARDING_MENTION_FIRST,
        *supervisor_tier,
        *other_tier,
        *hrbp_tier,
        *ONBOARDING_MENTION_LAST,
    ]


def _build_onboarding_notice_block(candidate: dict[str, str]) -> str:
    department = str(candidate.get("department") or "").strip()
    team = str(candidate.get("team") or "").strip()
    private_tg = str(candidate.get("private_contact") or "").strip()
    work_tg = str(candidate.get("work_tg") or "").strip()
    work_email = str(candidate.get("work_email") or "").strip()
    lines = [
        f"日期：{candidate.get('effective_date') or ''}",
        f"花名：{candidate.get('chinese_name') or ''}",
        f"简历名：{candidate.get('resume_name') or ''}",
        f"编号：{candidate.get('employee_code') or ''}",
        f"岗位：{candidate.get('position_title') or ''}",
        f"部门-小组：{department}-{team}",
        f"私人TG：{private_tg}",
    ]
    if _onboarding_has_work_contact_fields(department):
        same_as_private = bool(work_tg) and work_tg.lstrip("@").casefold() == private_tg.lstrip("@").casefold()
        work_tg_display = "待补充" if (not work_tg or same_as_private) else work_tg
        lines.append(f"工作TG: {work_tg_display}")
        lines.append(f"工作Gmail: {work_email or '待补充'}")
    return "\n".join(lines)


def build_onboarding_notice(candidates: list[dict[str, str]]) -> str:
    """【新人入职通知】：多人之间用 ——— 分隔，结尾一条合并去重的 @提醒 备注。"""
    blocks = [_build_onboarding_notice_block(candidate) for candidate in candidates]
    mentions = " ".join(f"@{handle}" for handle in _onboarding_all_mentions(candidates))
    body = "\n\n———\n\n".join(blocks)
    return f"【新人入职通知】\n\n{body}\n\n备注：已培训完成，{mentions} 请知悉"


def _build_onboarding_sync_block(candidate: dict[str, str]) -> str:
    org_unit = str(candidate.get("org_unit") or "").strip()
    service_entity = str(candidate.get("service_entity") or "").strip()
    department = str(candidate.get("department") or "").strip()
    team = str(candidate.get("team") or "").strip()
    private_tg = str(candidate.get("private_contact") or "").strip()
    work_tg = str(candidate.get("work_tg") or "").strip()
    work_email = str(candidate.get("work_email") or "").strip()
    lines = [
        f"入职日期：{candidate.get('hire_date') or candidate.get('effective_date') or ''}",
        f"编号：{candidate.get('employee_code') or ''}",
        f"花名：{candidate.get('chinese_name') or ''}",
        f"简历名：{candidate.get('resume_name') or ''}",
        f"部门：{org_unit}-{service_entity}",
        f"小组：{department}-{team}",
        f"直属上级：{candidate.get('direct_supervisor') or ''}",
        f"岗位：{candidate.get('position_title') or ''}",
        f"简历来源：{candidate.get('resume_source') or ''}",
        f"私人TG：{private_tg}",
    ]
    if _onboarding_has_work_contact_fields(department):
        same_as_private = bool(work_tg) and work_tg.lstrip("@").casefold() == private_tg.lstrip("@").casefold()
        work_tg_display = "待补充" if (not work_tg or same_as_private) else work_tg
        lines.append(f"工作TG：{work_tg_display}")
        lines.append(f"工作Gmail：{work_email or '待补充'}")
    return "\n".join(lines)


def build_onboarding_sync(candidates: list[dict[str, str]]) -> str:
    """【入职信息同步】：多人之间用 ——— 分隔，没有结尾备注行。"""
    blocks = [_build_onboarding_sync_block(candidate) for candidate in candidates]
    body = "\n\n———\n\n".join(blocks)
    return f"【入职信息同步】\n\n{body}"


# ---- /probation_notify：今日转正 -> 【新人预转正提醒】【转正申请】【转正通知】【转正信息同步】----
# 规则来源：用户上传的模版文件（2026-10-18），不要凭记忆改动，以模版原文为准。
# 注意：转正申请里的"直接上级"评语、"转正表单链接"花名册里没有对应字段，
# 模版本身就是要发给员工/领导手填的空白表单，所以这两处用"待补充"占位，不是漏做。

# (部门, 小组) 精确匹配优先于按部门匹配；值为 (hrbp handle, 领导 handle)。
PROBATION_TEAM_LEADS: dict[tuple[str, str], tuple[str, str]] = {
    ("ACFAN特战队", "内容组"): ("linerkang", "zlei1216"),
    ("ACFAN特战队", "SEO组"): ("linerkang", "zlei1216"),
    ("研发部", "前端组"): ("feang99568", "wdz999"),
    ("研发部", "后端组"): ("feang99568", "liyuanba666"),
}
PROBATION_DEPARTMENT_LEADS: dict[str, tuple[str, str]] = {
    "运营1部": ("FWN066", "zlei1216"),
    "运营2部": ("wean4790", "xxs202215cz2025"),
    "渠道拓展部": ("FWN066", "haohaoganhuoshaodapao"),
    "传统销售部": ("FWN066", "haohaoganhuoshaodapao"),
    "效能部": ("linerkang", "DaBai10010"),
    "AIGC原创部": ("linerkang", "DaBai10010"),
    # 其他未命中具体小组规则的 ACFAN特战队。
    "ACFAN特战队": ("wean4790", "zlei1216"),
}

# 花名册 P 部门 -> 转正通知要发的全员群 chat_id。
PROBATION_GROUP_CHAT_IDS: tuple[tuple[str, int], ...] = (
    ("运营1部", -1003663263859),
    ("运营2部", -1003950803307),
    ("研发部", -1003946619557),
    ("效能部", -1003946619557),
    ("ACFAN特战队", -1003553653887),
    ("AIGC原创部", -1003553653887),
    ("传统销售部", -1003872014182),
    ("渠道拓展部", -1003872014182),
)

# 直接上级是这些人时，/probation_notify 的"2.1"步骤不转发（这几位是公司高层，不需要走这个审批流程）。
PROBATION_SUPERVISOR_SKIP_NAMES = frozenset({"辰泽", "张雷", "王大壮", "李元霸", "万丰", "白一舟"})


def _probation_leads_for(department: str, team: str) -> tuple[str, str] | None:
    department = department.strip()
    team = team.strip()
    for (team_dept, team_name), pair in PROBATION_TEAM_LEADS.items():
        if _onboarding_department_matches(department, team_dept) and _onboarding_department_matches(team, team_name):
            return pair
    for dept_key, pair in PROBATION_DEPARTMENT_LEADS.items():
        if _onboarding_department_matches(department, dept_key):
            return pair
    return None


def _probation_group_chat_id(department: str) -> int | None:
    for dept_key, chat_id in PROBATION_GROUP_CHAT_IDS:
        if _onboarding_department_matches(department, dept_key):
            return chat_id
    return None


def _build_probation_reminder_block(candidate: dict[str, str]) -> str:
    department = str(candidate.get("department") or "").strip()
    team = str(candidate.get("team") or "").strip()
    leads = _probation_leads_for(department, team)
    hrbp, leader = leads if leads else ("待确认HRBP", "待确认领导")
    lines = [
        f"【{candidate.get('org_unit') or ''}-恒睿公司-新人预转正提醒】",
        "",
        f"花名：{candidate.get('chinese_name') or ''}",
        f"编号：{candidate.get('employee_code') or ''}",
        f"岗位：{candidate.get('position_title') or ''}",
        f"入职时间：{candidate.get('hire_date') or ''}",
        f"按期转正日：{candidate.get('probation_date') or ''}",
        "",
        f"以上员工试用期即将届满，请@{hrbp} 向领导 @{leader} 做转正评估，确认是否按期转正；",
    ]
    return "\n".join(lines)


def build_probation_reminder(candidates: list[dict[str, str]]) -> str:
    """【新人预转正提醒】：每人一段（含自己的编制中心抬头），多人之间用 ——— 分隔，
    结尾统一加一条延期/终止的说明（模版里这段只出现一次，不是每人重复）。"""
    blocks = [_build_probation_reminder_block(candidate) for candidate in candidates]
    body = "\n\n———\n\n".join(blocks)
    return (
        f"{body}\n\n"
        "如需延期，请告知预计转正日期，另请ODC同步当事员工知晓；如需终止，请提前告知SSC安排离职事宜，谢谢！"
    )


PROBATION_DEFAULT_EVALUATION = "同意按期转正"


async def _probation_resolve_form_link(services: "Services", candidate: dict[str, str]) -> str:
    """按转正日期所在的年月，去 Drive "<年>-<月>月转正明细" 子文件夹里按花名找表单链接。
    返回给预览/消息直接用的最终文案：真链接 / "待补充"（没找到）/
    "待补充（Drive读取失败：...）"（读取报错，比如文件夹没共享给服务账号）。"""
    probation_date = parse_sheet_date(str(candidate.get("probation_date") or ""))
    chinese_name = str(candidate.get("chinese_name") or "").strip()
    if not probation_date or not chinese_name:
        return "待补充"
    try:
        link = await services.sheets.find_probation_form_link(
            services.settings.probation_form_drive_folder_id,
            probation_date.year, probation_date.month, chinese_name,
        )
        return link or "待补充"
    except Exception as exc:
        logger.exception("Probation form link lookup failed for %s", chinese_name)
        return f"待补充（Drive读取失败：{str(exc)[:80]}）"


def _build_probation_application_block(candidate: dict[str, str], form_link: str | None = None) -> str:
    """单人【试用员工转正申请】，私信发给员工本人/其直接上级时用的就是这一份。
    "直接上级"评语默认写"同意按期转正"；"转正表单链接"按转正日期年月去 Drive 对应
    的"<年>-<月>月转正明细"子文件夹里按花名查找，找不到就留"待补充"。"""
    department = str(candidate.get("department") or "").strip()
    team = str(candidate.get("team") or "").strip()
    leads = _probation_leads_for(department, team)
    hrbp, leader = leads if leads else ("待确认HRBP", "待确认领导")
    job_level = candidate.get("job_level") or "待补充"
    lines = [
        f"【{candidate.get('org_unit') or ''}-试用员工转正申请】",
        "",
        f"花名：{candidate.get('chinese_name') or ''}",
        f"编号：{candidate.get('employee_code') or ''}",
        f"组别：{department}-{team}",
        f"岗位：{candidate.get('position_title') or ''}",
        f"入职日期：{candidate.get('hire_date') or ''}",
        f"转正日期：{candidate.get('probation_date') or ''}",
        f"试用期职级：{job_level}",
        f"转正职级：{job_level}",
        f"直接上级：{PROBATION_DEFAULT_EVALUATION}",
        "",
        f"转正表单链接：{form_link or '待补充'}",
        "",
        f"@{hrbp} 请odc填写HRBP考核意见，谢谢",
        f"@{leader} 请领导在审批表二级审批中填写该员工转正意见，谢谢",
    ]
    return "\n".join(lines)


def build_probation_notice_text(candidate: dict[str, str]) -> str:
    """单个员工的【转正通知】正文（随海报一起发到对应全员群）。"""
    work_tg = str(candidate.get("work_tg") or "").strip()
    mention = f" @{work_tg.lstrip('@')}" if work_tg else ""
    return (
        "【转正通知】\n\n\n"
        f"祝贺 {candidate.get('chinese_name') or ''}{mention}，表现优秀，通过试用期考核评估，"
        f"于 {candidate.get('probation_date') or ''} 起正式转正。\n\n"
        "愿你在未来的工作中继续保持热忱，持续成长，共创更多价值！\n\n"
        "恭喜转正，未来可期！"
    )


def _build_probation_sync_block(candidate: dict[str, str]) -> str:
    department = str(candidate.get("department") or "").strip()
    team = str(candidate.get("team") or "").strip()
    job_level = candidate.get("job_level") or "待补充"
    return "\n".join([
        f"北斗矩阵：{candidate.get('service_entity') or '恒睿'}",
        f"部门-小组：{department}-{team}",
        f"编号：{candidate.get('employee_code') or ''}",
        f"花名：{candidate.get('chinese_name') or ''}",
        f"岗位：{candidate.get('position_title') or ''}",
        f"入职日期：{candidate.get('hire_date') or ''}",
        f"转正日期：{candidate.get('probation_date') or ''}",
        "直属上级/BP评估：待补充",
        f"试用期职级：{job_level}",
        f"转正职级：{job_level}",
    ])


def build_probation_sync(candidates: list[dict[str, str]]) -> str:
    """【转正信息同步】：多人之间用 ——— 分隔，没有结尾备注行。"""
    blocks = [_build_probation_sync_block(candidate) for candidate in candidates]
    body = "\n\n———\n\n".join(blocks)
    return f"【转正信息同步】\n\n{body}"


async def _send_onboarding_agreements(
    bot: Bot, chat_id: int, candidates: list[dict[str, str]], services: "Services"
) -> None:
    """【新人入职通知】确认发送之后，逐人生成远程员工入职协议书、上传到 Drive 入职协议文件夹
    （按月份子文件夹存放，没有就自动新建），再把链接私信发给员工本人的私人TG，并附带
    '需要签字哦'。单人失败不影响其他人；最后给管理员发一条成功/失败汇总。"""
    settings = services.settings
    successful: list[str] = []
    failed: list[str] = []
    for candidate in candidates:
        name = str(candidate.get("chinese_name") or "")
        private_tg = str(candidate.get("private_contact") or "").strip().lstrip("@")
        if not private_tg:
            failed.append(f"{name}（花名册 AF 列没有私人TG）")
            continue
        try:
            content = build_onboarding_agreement(candidate)
            filename = agreement_filename(candidate)
            folder = month_folder_name(
                str(candidate.get("effective_date") or candidate.get("hire_date") or "")
            )
            link = await services.sheets.upload_onboarding_agreement(
                settings.onboarding_agreement_drive_folder_id, folder, filename, content
            )
        except Exception as exc:
            logger.exception("Onboarding agreement generate/upload failed for %s", name)
            failed.append(f"{name}（生成/上传入职协议失败：{str(exc)[:80]}）")
            continue
        try:
            target_chat = await bot.get_chat(f"@{private_tg}")
            await bot.send_message(target_chat.id, f"{link}\n\n需要签字哦")
            successful.append(name)
        except Exception as exc:
            logger.exception("Onboarding agreement DM failed for %s", name)
            failed.append(f"{name}（协议已生成，但私信 @{private_tg} 失败：{str(exc)[:80]}；链接：{link}）")
    lines = [f"【入职协议】处理完成。成功：{len(successful)}，失败：{len(failed)}"]
    if successful:
        lines.append("✅ " + "、".join(successful))
    if failed:
        lines.append("❌ " + "\n".join(failed))
    try:
        await bot.send_message(chat_id, "\n".join(lines)[:3900])
    except Exception:
        logger.exception("Failed to send onboarding agreement summary to admin")


async def run_probation_notify(bot: Bot, chat_id: int, state: FSMContext, services: "Services") -> None:
    """查花名册 BK 列生成 /probation_notify 预览并写入 FSM state。供手动命令和
    周一到周六 10:00 的定时任务共用（定时任务没有 Message，所以不依赖它）。"""
    if not services.sheets.configured:
        await bot.send_message(chat_id, "Google 花名册尚未配置，无法查询。")
        return
    settings = services.settings
    if not settings.probation_union_group_chat_id or not settings.probation_sync_group_chat_id:
        await bot.send_message(
            chat_id,
            "尚未配置 PROBATION_UNION_GROUP_CHAT_ID / PROBATION_SYNC_GROUP_CHAT_ID，"
            "请先在 .env 里配置这两个群的 chat_id 后再使用本命令。",
        )
        return
    today = datetime.now(ATTENDANCE_TIMEZONE).date()
    try:
        advance, due = await services.sheets.probation_notify_candidates(today, advance_days=4)
    except Exception as exc:
        logger.exception("Probation notify lookup failed")
        await bot.send_message(chat_id, f"花名册读取失败：{str(exc)[:160]}")
        return
    if not advance and not due:
        await bot.send_message(chat_id, f"今天（{today.isoformat()}）花名册 BK 列里没有需要处理的转正提醒/转正。")
        return

    reminder_text = build_probation_reminder(advance) if advance else ""
    sync_text = build_probation_sync(due) if due else ""

    await state.clear()
    await state.set_state(ProbationNotify.waiting_for_action)
    await state.update_data(
        probation_advance=advance,
        probation_due=due,
        probation_reminder_text=reminder_text,
        probation_sync_text=sync_text,
        probation_done={},
    )

    parts = [f"今天（{today.isoformat()}）："]
    if advance:
        adv_names = "、".join(str(c.get("chinese_name") or "") for c in advance)
        application_blocks = []
        for c in advance:
            form_link = await _probation_resolve_form_link(services, c)
            application_blocks.append(_build_probation_application_block(c, form_link))
        parts.append(
            f"距转正还有4天的有 {len(advance)} 人：{adv_names}\n\n"
            f"———【新人预转正提醒】预览（回复 1 转发到恒睿公司-联合管理群）———\n\n{reminder_text}\n\n"
            "———【转正申请】预览（每人一份，回复 2 私信发给本人工作TG；回复 2.1 私信发给其直接上级；"
            "回复 2.2 把所有人的转正申请一起发到恒睿公司-联合管理群）———\n\n"
            + "\n\n———\n\n".join(application_blocks)
        )
    if due:
        due_names = "、".join(str(c.get("chinese_name") or "") for c in due)
        parts.append(
            f"今天就是转正日的有 {len(due)} 人：{due_names}\n\n"
            "———【转正通知】预览（回复 3，逐人连同转正海报发到其部门对应的全员群；"
            "随后【转正信息同步】会在当天 16:45-19:00 之间随机发到人事数据同步-SSC3组）———\n\n"
            + "\n\n———\n\n".join(build_probation_notice_text(c) for c in due)
        )
    parts.append("完成需要的步骤后回复 /cancel 结束本轮；全部步骤做完会自动清理。")
    preview = "\n\n".join(parts)

    preview_message_ids: list[int] = []
    for start in range(0, len(preview), 3700):
        sent = await bot.send_message(chat_id, preview[start:start + 3700])
        preview_message_ids.append(sent.message_id)
    await state.update_data(probation_preview_message_ids=preview_message_ids)


async def probation_notify_scheduler_loop(bot: Bot, dp: Dispatcher, services: "Services") -> None:
    """周一到周六（Asia/Shanghai）10:00 之后，每天自动给每个管理员各发一次
    /probation_notify 预览（和手动执行命令效果一样，仍然要回复 1/2/2.1/3 才会真正发送）。"""
    last_run_date: date | None = None
    while True:
        try:
            now_local = datetime.now(ATTENDANCE_TIMEZONE)
            # Python weekday(): 周一=0 ... 周六=5，周日=6（周日不跑）。
            if now_local.weekday() <= 5 and now_local.hour >= 10 and last_run_date != now_local.date():
                last_run_date = now_local.date()
                for admin_id in services.settings.admin_user_ids:
                    try:
                        admin_state = dp.fsm.resolve_context(bot, chat_id=admin_id, user_id=admin_id)
                        await bot.send_message(admin_id, "【每日定时】正在查今天的转正提醒/转正……")
                        await run_probation_notify(bot, admin_id, admin_state, services)
                    except Exception:
                        logger.exception("Scheduled probation notify failed for admin %s", admin_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Probation notify scheduler loop failed")
        await asyncio.sleep(300)


class TemplateCreate(StatesGroup):
    waiting_for_name = State()
    waiting_for_content = State()
    waiting_for_auto_choice = State()


@dataclass
class Services:
    settings: Settings
    db: Database
    sheets: GoogleSheetsService
    vision: VisionService
    gpt: GPTService
    # /onboarding_notify：管理员 user_id -> 下午待发"入职信息同步"的后台任务，/cancel 时用来撤销。
    onboarding_sync_tasks: dict[int, asyncio.Task] = field(default_factory=dict)
    # /probation_notify：管理员 user_id -> 下午待发"转正信息同步"的后台任务，/cancel 时用来撤销。
    probation_sync_tasks: dict[int, asyncio.Task] = field(default_factory=dict)


def _is_admin(user_id: int, settings: Settings) -> bool:
    return user_id in settings.admin_user_ids


def _candidate_profile_id(candidate_name: str) -> int:
    """Stable negative SQLite id for candidates submitted by an administrator."""
    digest = hashlib.sha256(candidate_name.strip().casefold().encode("utf-8")).digest()
    return -(int.from_bytes(digest[:8], "big") & ((1 << 62) - 1)) - 1


def _is_wallet_completion(text: str) -> bool:
    normalized = re.sub(r"[\s_，,。.!！/\\-]+", "", text.casefold())
    has_wallet = "钱包" in normalized or "wallet" in normalized
    has_form_or_address = "地址" in normalized or "表单" in normalized
    has_done = any(word in normalized for word in ("完成", "填完", "提交", "好了", "已填", "搞定"))
    return has_wallet and has_form_or_address and has_done


def _is_new_hire_trigger(text: str) -> bool:
    """"入职信息确认"/"新人入职" mark a submission as being about a new hire who may not have
    an employee code yet -- the roster row should then be found by matching 候选人姓名/姓名/
    简历名 first, instead of only by employee code."""
    if not text:
        return False
    head = text[:80]
    return any(keyword in head for keyword in ("入职信息确认", "新人入职", "中文花名"))


def _apply_new_hire_defaults(
    values: dict[str, str], extra_fields: dict[str, str], org_hint: str
) -> None:
    """花名册/变更记录 填写规范 (2026-09-06): fixed HR business-rule defaults for a genuine
    new-hire submission. Only ever fills a gap or normalizes an already-given value -- never
    overrides anything explicitly present. Callers must only invoke this once the submission
    is confirmed to be a new hire (入职信息确认/新人入职), never for a plain 字段变更 update."""
    values["nationality"] = hr_defaults.default_nationality(values.get("nationality", ""))
    values["office_region"] = hr_defaults.default_office_region(values.get("office_region", ""))
    values.setdefault("employment_status", "试用期")
    # 2026-10-19 用户要求：收到"入职信息确认"（或同一触发条件下的"新人入职"/"中文花名"）时，
    # 以下几个字段如果本人没填，就统一套用固定默认值；工作TG 没填就直接沿用私人TG。
    for key, default_value in hr_defaults.NEW_HIRE_VALUE_DEFAULTS.items():
        values.setdefault(key, default_value)
    if not values.get("work_tg") and values.get("private_contact"):
        values["work_tg"] = values["private_contact"]
    for key, default_value in hr_defaults.NEW_HIRE_STATIC_DEFAULTS.items():
        extra_fields.setdefault(key, default_value)
    hr_defaults.apply_org_specific_defaults(extra_fields, org_hint)
    extra_fields.setdefault("salary_currency", "CNY")
    if not extra_fields.get("hrbp"):
        hrbp = hr_defaults.resolve_hrbp(
            extra_fields.get("department", ""), extra_fields.get("team", ""), org_hint
        )
        if hrbp:
            extra_fields["hrbp"] = hrbp
    effective_date = values.get("effective_date", "")
    if effective_date:
        extra_fields.setdefault("latest_change_date", effective_date)
        if not extra_fields.get("probation_end_date"):
            end_date = hr_defaults.probation_end_date(effective_date)
            if end_date:
                extra_fields["probation_end_date"] = end_date


def _wallet_packet(profile: dict[str, object], result: dict[str, str]) -> str:
    return (
        "💰 钱包地址表单已匹配\n"
        f"Telegram：@{profile.get('telegram_username') or '无'}（{profile['telegram_user_id']}）\n"
        f"中文花名：{profile.get('chinese_name') or ''}\n"
        f"简历名：{profile.get('resume_name') or ''}\n"
        f"员工编码：{result['employee_code']}\n"
        f"表格行：{result['row']}\n提交时间：{result['timestamp']}\n"
        f"提交类型：{result['submit_type']}\n当前地址：{result['current_address']}\n"
        f"当前二维码：{result['current_qr']}\n旧地址：{result['old_address']}\n"
        f"旧二维码：{result['old_qr']}\n更换原因：{result['reason']}\n确认：{result['confirmed']}"
    )


CONTACTS_PER_PAGE = 8


def _employee_label(employee: Employee) -> str:
    username = f"@{employee.username}" if employee.username else "无 username"
    return f"{employee.full_name} · {username} · {employee.telegram_user_id}"


async def _download_message_image(message: Message, bot: Bot) -> tuple[bytes, str]:
    file_id = message.photo[-1].file_id if message.photo else message.document.file_id
    mime_type = message.document.mime_type if message.document else "image/jpeg"
    buffer = io.BytesIO()
    await bot.download(file_id, destination=buffer)
    return buffer.getvalue(), mime_type or "image/jpeg"


# 考勤抽查允许管理员在截图识别不出来的时候，直接用文字发花名兜底。支持用英文逗号、
# 中文逗号、顿号或任意空白（包括全角空格）分隔多个花名，例如 "张三 李四" 或 "张三，李四"。
_ATTENDANCE_NAME_SPLIT_RE = re.compile(r"[,，、\s]+")


def _parse_attendance_name_list(text: str) -> list[str]:
    candidates = _ATTENDANCE_NAME_SPLIT_RE.split(text.strip())
    names: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        name = candidate.strip()
        if not name or name in seen:
            continue
        seen.add(name)
        names.append(name)
    return names


def _selection_keyboard(
    employees: list[Employee], selected_ids: set[int], page: int
) -> InlineKeyboardMarkup:
    page_count = max(1, (len(employees) + CONTACTS_PER_PAGE - 1) // CONTACTS_PER_PAGE)
    page = max(0, min(page, page_count - 1))
    start = page * CONTACTS_PER_PAGE
    rows: list[list[InlineKeyboardButton]] = []
    for employee in employees[start : start + CONTACTS_PER_PAGE]:
        mark = "✅" if employee.telegram_user_id in selected_ids else "⬜"
        username = f"@{employee.username}" if employee.username else employee.full_name
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{mark} {username}"[:60],
                    callback_data=f"broadcast:toggle:{employee.telegram_user_id}:{page}",
                )
            ]
        )
    navigation: list[InlineKeyboardButton] = []
    if page > 0:
        navigation.append(
            InlineKeyboardButton(text="⬅️ 上一页", callback_data=f"broadcast:page:{page - 1}")
        )
    navigation.append(
        InlineKeyboardButton(text=f"{page + 1}/{page_count}", callback_data="broadcast:noop")
    )
    if page + 1 < page_count:
        navigation.append(
            InlineKeyboardButton(text="下一页 ➡️", callback_data=f"broadcast:page:{page + 1}")
        )
    rows.append(navigation)
    rows.append(
        [
            InlineKeyboardButton(text="全选", callback_data="broadcast:select_all"),
            InlineKeyboardButton(text="清空", callback_data="broadcast:clear"),
        ]
    )
    rows.append(
        [
            InlineKeyboardButton(text="下一步：写通知", callback_data="broadcast:recipients_done"),
            InlineKeyboardButton(text="取消", callback_data="broadcast:cancel"),
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _render_recipient_selection(
    callback: CallbackQuery, services: Services, state: FSMContext, page: int
) -> None:
    employees = await services.db.active_employees()
    data = await state.get_data()
    selected_ids = {int(value) for value in data.get("selected_ids", [])}
    text = (
        "请选择通知接收人。点击联系人可以勾选或取消。\n\n"
        f"已选择：{len(selected_ids)} 人｜可选：{len(employees)} 人"
    )
    if callback.message:
        await callback.message.edit_text(
            text,
            reply_markup=_selection_keyboard(employees, selected_ids, page),
        )


async def _send_employee_list(message: Message, title: str, employees: list[Employee]) -> None:
    if not employees:
        return
    lines = [f"{index}. {_employee_label(employee)}" for index, employee in enumerate(employees, 1)]
    chunks: list[str] = []
    current = f"{title}\n"
    for line in lines:
        addition = f"\n{line}"
        if len(current) + len(addition) > 3800:
            chunks.append(current)
            current = f"{title}（续）\n{line}"
        else:
            current += addition
    chunks.append(current)
    for chunk in chunks:
        await message.answer(chunk)


async def _save_profile_values(
    message: Message,
    services: Services,
    values: dict[str, str],
    raw_message: str,
    require_existing: bool = False,
    allow_incomplete: bool = False,
    extra_fields: dict[str, str] | None = None,
    prefer_name_match: bool = False,
    is_new_hire_event: bool = False,
) -> bool:
    if not message.from_user:
        return False
    target_user_id = message.from_user.id
    target_username = message.from_user.username
    target_full_name = message.from_user.full_name
    existing = await services.db.profile(target_user_id)
    if require_existing and not existing:
        await message.answer("还没有找到你的员工档案。请先提交完整的新人入职信息。")
        return False
    # 花名册填写规范：忽略"候选人编码"，"姓名/简历名" == 候选人姓名才是唯一识别符。
    # 不管这次发消息/截图的是本人、HR/管理员代发，还是本人后续用别的账号继续确认/更新——
    # 只要候选人姓名和本地已有档案的姓名/简历名一致，就认定是同一个人，合并覆盖写入那条
    # 记录（保留其原有的 Telegram 归属信息，只更新字段），而不是新建一行；姓名对不上，就
    # 按新档案重新填写，不会误覆盖别人的记录。
    # Resume/candidate name is the stable identity used across 入职信息确认 and
    # 新人入职; flower names may be assigned later and can be completely different.
    candidate_name = str(values.get("resume_name") or values.get("chinese_name") or "").strip()
    if candidate_name:
        match = await services.db.profile_by_name(candidate_name)
        if match and int(match["telegram_user_id"]) != message.from_user.id:
            existing = match
            target_user_id = int(match["telegram_user_id"])
            target_username = str(match.get("telegram_username") or "") or target_username
            target_full_name = str(match.get("telegram_full_name") or "") or target_full_name
        elif (
            not match
            and is_new_hire_event
            and _is_admin(message.from_user.id, services.settings)
        ):
            # One SSC administrator submits many different candidates. Do not merge
            # all of them into the administrator's own single Telegram profile.
            target_user_id = _candidate_profile_id(candidate_name)
            target_username = None
            target_full_name = candidate_name
            existing = await services.db.profile(target_user_id)
    errors = validate_profile(values, is_new=existing is None and not allow_incomplete)
    if errors:
        await message.answer("信息暂未保存：\n- " + "\n- ".join(errors))
        return False
    try:
        saved, changes, is_new = await services.db.save_profile(
            target_user_id,
            target_username,
            target_full_name,
            values,
            raw_message,
        )
    except Exception as exc:
        logger.exception("Failed to save employee profile")
        await message.answer(f"保存失败：{str(exc)[:120]}")
        return False
    if not changes:
        # A previous attempt may have saved locally but only partially reached Google
        # Sheets. Reconcile every populated core field even when the local values did
        # not change. Empty local fields never clear sheet data, and an empty changes
        # dict means this repair does not create a change-record row.
        if services.sheets.configured:
            try:
                row = await services.sheets.sync_profile(
                    saved, {}, False, extra_fields, prefer_name_match,
                    reconcile_core=True,
                    onboarding_event=is_new_hire_event,
                )
                await services.db.set_profile_sync(target_user_id, "synced", row)
                saved["sync_status"] = "synced"
                saved["sheet_row"] = row
                await message.answer(
                    f"✅ 档案内容没有变化，已重新核对并同步到花名册第 {row} 行。\n\n"
                    + profile_text(saved)
                )
            except Exception as exc:
                logger.exception("Google Sheets reconciliation failed for %s", target_user_id)
                await services.db.set_profile_sync(target_user_id, f"error: {str(exc)}")
                await message.answer(
                    "云表重新核对失败，已保留记录供管理员处理。\n原因：" + _sheet_error_reason(exc)
                )
            return True
        if not services.sheets.configured:
            await message.answer(
                "档案内容没有变化，但仍在等待云表授权，暂时无法重试同步。\n\n"
                + profile_text(saved)
            )
            return True
    sync_note = "已保存在机器人中，等待云表授权后自动同步。"
    if services.sheets.configured:
        try:
            row = await services.sheets.sync_profile(
                saved, changes, is_new, extra_fields, prefer_name_match,
                onboarding_event=is_new_hire_event,
            )
            await services.db.set_profile_sync(target_user_id, "synced", row)
            saved["sync_status"] = "synced"
            sync_note = f"已同步到花名册第 {row} 行，并写入变更记录。"
        except Exception as exc:
            logger.exception("Google Sheets sync failed for %s", target_user_id)
            await services.db.set_profile_sync(target_user_id, f"error: {str(exc)}")
            sync_note = (
                "本地已保存，但云表同步失败，管理员可检查后重试。\n原因："
                + _sheet_error_reason(exc)
            )
    changed_labels = "、".join(DISPLAY_LABELS.get(key, key) for key in changes)
    await message.answer(
        f"✅ {'新人档案已建立' if is_new else '字段已更新'}\n"
        f"变更字段：{changed_labels}\n{sync_note}\n\n{profile_text(saved)}"
    )
    return True


def _vision_error_reason(exc: Exception) -> str:
    """把截图识别（OpenAI）失败的原因翻译成管理员能处理的一句话。"""
    text = str(exc)
    lowered = text.casefold()
    if "insufficient_quota" in lowered or "quota" in lowered:
        return "OpenAI 账户额度不足或欠费，请充值或更换 OPENAI_API_KEY。"
    if "invalid_api_key" in lowered or "incorrect api key" in lowered or "401" in lowered:
        return "OPENAI_API_KEY 无效或已失效。"
    if "rate limit" in lowered or "429" in lowered:
        return "OpenAI 请求过于频繁，请稍等一分钟再发。"
    if "model" in lowered and ("not found" in lowered or "does not exist" in lowered):
        return "OPENAI_VISION_MODEL 配置的模型不可用。"
    if "timeout" in lowered or "timed out" in lowered or "connection" in lowered:
        return "连接 OpenAI 超时或网络不通，请稍后重试。"
    return f"{exc.__class__.__name__}: {text[:200]}"


def _sheet_error_reason(exc: Exception) -> str:
    """把 Google 表格写入失败的原因翻译成管理员能直接处理的一句话。"""
    text = str(exc)
    lowered = text.casefold()
    if "not supported for this document" in lowered:
        return "目标表格是 Excel（.xlsx）文件，Google 接口无法写入；请在表格里“文件 → 另存为 Google 表格”，并把新表 ID 配置到 GOOGLE_SPREADSHEET_ID。"
    if "permission" in lowered or "403" in lowered:
        return "机器人服务账号没有该表格的编辑权限；请把表格共享给服务账号（编辑者）。"
    if "404" in lowered or "not found" in lowered:
        return "找不到目标表格或工作表；请检查 GOOGLE_SPREADSHEET_ID 和工作表名称（花名册 / 变更记录）。"
    if "unable to parse range" in lowered:
        return "目标表格里找不到“花名册”或“变更记录”工作表。"
    return text[:300] or exc.__class__.__name__


def build_dispatcher(services: Services) -> Dispatcher:
    router = Router()
    dp = Dispatcher()

    @router.message(CommandStart())
    async def start(message: Message, state: FSMContext) -> None:
        if not message.from_user or message.chat.type != ChatType.PRIVATE:
            return
        was_active = await services.db.is_active_employee(message.from_user.id)
        await services.db.upsert_employee(
            telegram_user_id=message.from_user.id,
            chat_id=message.chat.id,
            username=message.from_user.username,
            full_name=message.from_user.full_name,
        )
        await state.clear()
        await message.answer(
            f"你好，{message.from_user.full_name}。你已登记成功，可以接收公司通知。\n\n"
            "员工咨询请联系人工客服 @ffuuyao。\n\n"
            "常用命令：\n/help 使用帮助\n/human 联系人工\n/whoami 查看账号 ID"
            "\n/profile 查看我的员工档案"
        )
        if not was_active:
            for _, template_name, content, _ in await services.db.templates(auto_only=True):
                try:
                    await message.answer(f"【{template_name}】\n\n{content}")
                    await services.db.save_outbound_message(
                        message.from_user.id,
                        message.chat.id,
                        content,
                        message_kind="auto_template",
                        template_name=template_name,
                    )
                except Exception:
                    logger.exception(
                        "Failed to send newcomer template to user %s", message.from_user.id
                    )

    @router.message(Command("help"))
    async def help_command(message: Message) -> None:
        await message.answer(
            "本机器人用于接收公司通知和登记新人信息。\n\n"
            "发送“新人入职（按格式填写）”表单可建立档案；以后发送“字段变更”加需要修改的字段即可覆盖。\n"
            "也可以直接发送清晰、完整的员工信息截图，机器人会识别并校验后填写。\n"
            "使用 /profile 查看自己的档案，/profile_delete 删除自己的档案。\n\n"
            "员工咨询不由机器人自动回答，请发送 /human 联系人工客服 @ffuuyao。"
        )

    @router.message(Command("human"))
    async def human_command(message: Message) -> None:
        contact = services.settings.support_contact.strip()
        if contact.startswith("@") and len(contact) > 1:
            username = contact[1:]
            await message.answer(
                f'人工客服联系方式：<a href="https://t.me/{username}">{contact}</a>\n\n'
                "点击用户名即可打开私聊。",
                parse_mode="HTML",
            )
        else:
            await message.answer(f"人工客服联系方式：{contact}")

    @router.message(Command("whoami"))
    async def whoami(message: Message) -> None:
        if message.from_user:
            await message.answer(f"你的 Telegram user_id：`{message.from_user.id}`", parse_mode="Markdown")

    @router.message(Command("cancel"))
    async def cancel(message: Message, state: FSMContext) -> None:
        await state.clear()
        if message.from_user:
            # /onboarding_notify 下午待发送的【入职信息同步】后台任务也一并撤销。
            task = services.onboarding_sync_tasks.pop(message.from_user.id, None)
            if task and not task.done():
                task.cancel()
            # /probation_notify 下午待发送的【转正信息同步】后台任务也一并撤销。
            probation_task = services.probation_sync_tasks.pop(message.from_user.id, None)
            if probation_task and not probation_task.done():
                probation_task.cancel()
        await message.answer("操作已取消。")

    @router.callback_query(F.data.startswith("editsent:"))
    async def edit_sent_message_start(callback: CallbackQuery, state: FSMContext) -> None:
        if not callback.from_user or not _is_admin(callback.from_user.id, services.settings):
            await callback.answer("无权操作", show_alert=True)
            return
        parts = (callback.data or "").split(":", 2)
        if len(parts) != 3:
            await callback.answer("按钮数据已失效", show_alert=True)
            return
        try:
            target_chat_id = int(parts[1])
            target_message_id = int(parts[2])
        except ValueError:
            await callback.answer("按钮数据已失效", show_alert=True)
            return
        await state.set_state(EditSentMessage.waiting_for_new_text)
        await state.update_data(
            edit_target_chat_id=target_chat_id, edit_target_message_id=target_message_id
        )
        if callback.message:
            await callback.message.answer(
                "请回复要替换成的完整内容（会直接覆盖掉群里刚才那条消息）；回复 /cancel 取消。"
            )
        await callback.answer()

    @router.message(EditSentMessage.waiting_for_new_text, F.text)
    async def edit_sent_message_apply(message: Message, state: FSMContext, bot: Bot) -> None:
        if not message.from_user or not _is_admin(message.from_user.id, services.settings):
            await state.clear()
            return
        text = (message.text or "").strip()
        if text.casefold().startswith("/cancel"):
            return  # 交给全局 /cancel 命令处理
        data = await state.get_data()
        target_chat_id = data.get("edit_target_chat_id")
        target_message_id = data.get("edit_target_message_id")
        await state.clear()
        if target_chat_id is None or target_message_id is None:
            await message.answer("本次编辑已失效，请重新点击要编辑的消息下面的按钮。")
            return
        try:
            await bot.edit_message_text(text, chat_id=target_chat_id, message_id=target_message_id)
        except TelegramBadRequest:
            try:
                await bot.edit_message_caption(
                    chat_id=target_chat_id, message_id=target_message_id, caption=text
                )
            except Exception as exc:
                logger.exception("Edit sent message (caption) failed")
                await message.answer(f"编辑失败：{str(exc)[:160]}")
                return
        except Exception as exc:
            logger.exception("Edit sent message failed")
            await message.answer(f"编辑失败：{str(exc)[:160]}")
            return
        await message.answer("✅ 已更新那条消息。")

    @router.message(Command("stats"))
    async def stats(message: Message) -> None:
        if not message.from_user or not _is_admin(message.from_user.id, services.settings):
            await message.answer("无权使用此命令。")
            return
        count = await services.db.employee_count()
        await message.answer(f"当前已启用员工数：{count}")

    @router.message(Command("inbox"))
    async def inbox(message: Message) -> None:
        if not message.from_user or not _is_admin(message.from_user.id, services.settings):
            await message.answer("无权使用此命令。")
            return
        total = await services.db.inbound_message_count()
        items = await services.db.inbound_messages(limit=30)
        if not items:
            await message.answer("还没有收到员工消息。")
            return
        lines = [f"最近 30 条员工消息（总计 {total} 条）"]
        for item in items:
            username = f"@{item['username']}" if item.get("username") else str(item["telegram_user_id"])
            content = str(item.get("content") or "").replace("\n", " ")
            lines.append(f"#{item['id']} {item['full_name']} {username}｜{item['message_type']}｜{content[:120]}")
        text = "\n".join(lines)
        for start in range(0, len(text), 3800):
            await message.answer(text[start:start + 3800])

    @router.message(Command("sync"))
    async def sync_roster(message: Message) -> None:
        if not message.from_user or not _is_admin(message.from_user.id, services.settings):
            await message.answer("无权使用此命令。")
            return
        if not services.sheets.configured:
            await message.answer("Google 表格尚未配置，无法同步。")
            return
        await message.answer("正在从表格同步花名册…")
        try:
            rows = await services.sheets.read_roster()
            await services.db.replace_roster(rows)
        except Exception as exc:
            logger.exception("Roster sync failed")
            await message.answer(f"同步失败：{str(exc)[:200]}")
            return
        await message.answer(f"✅ 已同步花名册，共 {len(rows)} 条记录。使用 /roster 查看。")

    @router.message(Command("roster"))
    async def view_roster(message: Message) -> None:
        if not message.from_user or not _is_admin(message.from_user.id, services.settings):
            await message.answer("无权使用此命令。")
            return
        total = await services.db.roster_count()
        if not total:
            await message.answer("本地花名册为空，请先使用 /sync 同步。")
            return
        last_sync = await services.db.roster_last_sync()
        synced_at = str(last_sync["synced_at"]) if last_sync else "未知"
        rows = await services.db.roster(limit=500)
        lines = [f"本地花名册共 {total} 条（只读，最近同步：{synced_at}）"]
        for item in rows:
            code = item.get("employee_code") or "无编码"
            chinese_name = item.get("chinese_name") or ""
            resume_name = item.get("resume_name") or ""
            work_tg = item.get("work_tg") or "无TG"
            lines.append(f"{code}｜{chinese_name} · {resume_name} · {work_tg}")
        text = "\n".join(lines)
        for start in range(0, len(text), 3800):
            await message.answer(text[start:start + 3800])

    @router.message(Command("send_channel_msg"))
    async def send_channel_message_check(message: Message, state: FSMContext) -> None:
        if not message.from_user or not _is_admin(message.from_user.id, services.settings):
            await message.answer("无权使用此命令。")
            return
        if not services.sheets.configured:
            await message.answer("Google 花名册尚未配置，无法检查。")
            return
        await message.answer("正在只读检查花名册 AE、AR 列和机器人个人发送记录……")
        month_start = datetime.now(ATTENDANCE_TIMEZONE).date().replace(day=1)
        try:
            candidates = await services.sheets.channel_message_candidates(month_start)
        except Exception as exc:
            logger.exception("Channel-message candidate lookup failed")
            await message.answer(f"花名册读取失败：{str(exc)[:160]}")
            return
        employees = await services.db.active_employees()
        employee_by_username = {
            employee.username.strip().lstrip("@").casefold(): employee
            for employee in employees
            if employee.username and employee.username.strip().lstrip("@")
        }
        sent_ids = await services.db.channel_message_recipient_ids()
        activation_needed: dict[str, dict[str, str]] = {}
        channel_needed: dict[int, dict[str, object]] = {}
        for item in candidates:
            username = item["work_tg"].lstrip("@").casefold()
            employee = employee_by_username.get(username) if username else None
            if not username:
                key = item["chinese_name"].strip().casefold()
                activation_needed[key] = {**item, "reason": "AE列工作TG为空，未激活"}
            elif not employee:
                activation_needed[username] = {**item, "reason": "工作TG未激活机器人"}
            elif employee.telegram_user_id not in sent_ids:
                channel_needed[employee.telegram_user_id] = {
                    **item,
                    "telegram_user_id": employee.telegram_user_id,
                    "chat_id": employee.chat_id,
                }
        if not activation_needed and not channel_needed:
            await message.answer(
                f"✅ 检查完成：AR列入职日期从{month_start.isoformat()}起的员工，"
                "均已激活并有“关注恒睿频道”发送记录。"
            )
            return
        activation_template = await services.db.template_by_name("激活机器人")
        channel_template = await services.db.template_by_name("关注恒睿频道")
        if not activation_template or not channel_template:
            await message.answer("缺少启用中的“激活机器人”或“关注恒睿频道”模板。")
            return
        await state.clear()
        lines = [
            "【频道消息待处理名单】",
            f"范围：花名册 AR 入职日期 ≥ {month_start.isoformat()}（当月第一天）",
            f"未激活：{len(activation_needed)} 人",
            f"已激活但未发频道消息：{len(channel_needed)} 人",
        ]
        if activation_needed:
            lines.append("\n【未激活机器人｜请管理员人工发送】")
            for index, item in enumerate(activation_needed.values(), 1):
                username = str(item["work_tg"] or "").lstrip("@")
                contact = f"https://t.me/{username}" if username else "AE列无工作TG，需人工补充"
                lines.append(
                    f"{index}. {item['chinese_name']}｜{item['work_tg'] or '无工作TG'}｜"
                    f"{item['hire_date']}｜{contact}"
                )
            lines.append(f"\n激活机器人模板：\n{activation_template[2]}")
        if channel_needed:
            lines.append("\n【可由机器人直接发送频道消息】")
            for index, item in enumerate(channel_needed.values(), 1):
                lines.append(
                    f"{index}. {item['chinese_name']}｜{item['work_tg']}｜{item['hire_date']}"
                )
            lines.append(f"\n关注恒睿频道模板：\n{channel_template[2]}")
        text = "\n".join(lines)
        for start in range(0, len(text), 3700):
            await message.answer(text[start:start + 3700])
        if channel_needed:
            await state.set_state(ChannelMessageSend.waiting_for_confirmation)
            await state.update_data(
                channel_send_ids=list(channel_needed),
                channel_template_id=channel_template[0],
                channel_target_names={
                    str(user_id): str(item["chinese_name"])
                    for user_id, item in channel_needed.items()
                },
                channel_target_work_tg={
                    str(user_id): str(item["work_tg"])
                    for user_id, item in channel_needed.items()
                },
            )
            await message.answer(
                f"确认向以上 {len(channel_needed)} 名已激活员工发送“关注恒睿频道”模板吗？",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                    InlineKeyboardButton(text="确认直接发送", callback_data="channelmsg:confirm"),
                    InlineKeyboardButton(text="取消", callback_data="channelmsg:cancel"),
                ]]),
            )

    @router.message(Command("notify_supervisor"))
    async def notify_supervisor_check(message: Message, state: FSMContext) -> None:
        if not message.from_user or not _is_admin(message.from_user.id, services.settings):
            await message.answer("无权使用此命令。")
            return
        if not services.sheets.configured:
            await message.answer("Google 花名册尚未配置，无法查询。")
            return
        today = datetime.now(ATTENDANCE_TIMEZONE).date()
        await message.answer(f"正在只读查询花名册 G 列生效日期为 {today.isoformat()} 的新人……")
        try:
            hires = await services.sheets.today_hires(today)
            supervisor_names = sorted({
                str(item.get("supervisor") or "").strip().lstrip("@")
                for item in hires if str(item.get("supervisor") or "").strip()
            })
            contacts = (
                await services.sheets.attendance_contacts(supervisor_names)
                if supervisor_names else []
            )
        except Exception as exc:
            logger.exception("Supervisor notification lookup failed")
            await message.answer(f"花名册读取失败：{str(exc)[:160]}")
            return
        if not hires:
            await message.answer(f"花名册中没有 G 列生效日期为 {today.isoformat()} 的新人。")
            return
        groups, issues = build_supervisor_groups(hires, contacts)
        employees = await services.db.active_employees()
        employee_by_username = {
            employee.username.strip().lstrip("@").casefold(): employee
            for employee in employees
            if employee.username and employee.username.strip().lstrip("@")
        }
        sendable: dict[str, dict[str, object]] = {}
        manual: list[dict[str, object]] = []
        for username, group in groups.items():
            group["text"] = supervisor_notice_text(
                str(group["supervisor"]), list(group["hires"]), today.isoformat()  # type: ignore[arg-type]
            )
            employee = employee_by_username.get(username)
            if employee:
                sendable[str(employee.telegram_user_id)] = {**group, "chat_id": employee.chat_id}
            else:
                manual.append(group)

        lines = [
            "【今日入职新人 → 直接上级通知】",
            f"今日（{today.isoformat()}）入职：{len(hires)} 人；涉及直接上级：{len(groups)} 人",
        ]
        if sendable:
            lines.append("\n【可由机器人直接发送】")
            for index, group in enumerate(sendable.values(), 1):
                names = "、".join(str(h["chinese_name"]) for h in group["hires"])  # type: ignore[union-attr]
                lines.append(f"{index}. {group['supervisor']}｜{group['work_tg']} ← {names}")
        if manual:
            lines.append("\n【上级未激活机器人｜请管理员人工转发】")
            for index, group in enumerate(manual, 1):
                username = str(group["work_tg"]).lstrip("@")
                lines.append(f"{index}. {group['supervisor']}｜https://t.me/{username}\n{group['text']}")
        if issues:
            lines.append("\n【无法通知｜需人工处理】")
            lines.extend(f"· {item}" for item in issues)
        text = "\n".join(lines)
        for start in range(0, len(text), 3700):
            await message.answer(text[start:start + 3700])
        if not sendable:
            return
        await state.clear()
        await state.set_state(SupervisorNotify.waiting_for_confirmation)
        await state.update_data(supervisor_notices=sendable)
        sample = next(iter(sendable.values()))["text"]
        await message.answer(
            f"消息示例：\n\n{sample}\n\n确认向以上 {len(sendable)} 位直接上级发送吗？",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="确认发送", callback_data="supnotify:confirm"),
                InlineKeyboardButton(text="取消", callback_data="supnotify:cancel"),
            ]]),
        )

    @router.callback_query(F.data == "supnotify:cancel")
    async def notify_supervisor_cancel(callback: CallbackQuery, state: FSMContext) -> None:
        if not callback.from_user or not _is_admin(callback.from_user.id, services.settings):
            await callback.answer("无权操作", show_alert=True)
            return
        await state.clear()
        await callback.answer("已取消")
        if callback.message:
            await callback.message.edit_reply_markup(reply_markup=None)

    @router.callback_query(SupervisorNotify.waiting_for_confirmation, F.data == "supnotify:confirm")
    async def notify_supervisor_confirm(callback: CallbackQuery, state: FSMContext, bot: Bot) -> None:
        if not callback.from_user or not _is_admin(callback.from_user.id, services.settings):
            await callback.answer("无权操作", show_alert=True)
            return
        notices = dict((await state.get_data()).get("supervisor_notices") or {})
        await state.clear()
        await callback.answer("开始发送")
        if callback.message:
            await callback.message.edit_reply_markup(reply_markup=None)
        successful: list[str] = []
        failed: list[str] = []
        delay = 1 / services.settings.broadcast_messages_per_second
        for user_id, notice in notices.items():
            label = f"{notice['supervisor']} · {notice['work_tg']}"
            chat_id = int(notice["chat_id"])
            text = str(notice["text"])
            for attempt in range(2):
                try:
                    await bot.send_message(chat_id, text)
                    successful.append(label)
                    try:
                        await services.db.save_outbound_message(
                            int(user_id), chat_id, text, message_kind="supervisor_notice",
                        )
                    except Exception:
                        logger.exception("Failed to log supervisor notice for user %s", user_id)
                    break
                except TelegramRetryAfter as exc:
                    if attempt:
                        failed.append(label)
                        break
                    await asyncio.sleep(exc.retry_after + 0.5)
                except (TelegramForbiddenError, TelegramBadRequest):
                    await services.db.deactivate_employee(int(user_id))
                    failed.append(f"{label}（对方可能已停用机器人，已标记为未激活）")
                    break
                except Exception:
                    logger.exception("Supervisor notice send failed for user %s", user_id)
                    failed.append(label)
                    break
            await asyncio.sleep(delay)
        if callback.message:
            lines = [f"直接上级通知发送完成。成功：{len(successful)}，失败：{len(failed)}"]
            if successful:
                lines.append("\n✅ 成功：\n" + "\n".join(successful))
            if failed:
                lines.append("\n❌ 失败：\n" + "\n".join(failed))
            await callback.message.answer("\n".join(lines)[:3900])

    @router.message(Command("onboarding_notify"))
    async def onboarding_notify_start(message: Message, state: FSMContext) -> None:
        if not message.from_user or not _is_admin(message.from_user.id, services.settings):
            await message.answer("无权使用此命令。")
            return
        if not services.sheets.configured:
            await message.answer("Google 花名册尚未配置，无法查询。")
            return
        if not services.settings.onboarding_notify_group_chat_id or not services.settings.onboarding_sync_group_chat_id:
            await message.answer(
                "尚未配置 ONBOARDING_NOTIFY_GROUP_CHAT_ID / ONBOARDING_SYNC_GROUP_CHAT_ID，"
                "请先在 .env 里配置这两个群的 chat_id 后再使用本命令。"
            )
            return
        today = datetime.now(ATTENDANCE_TIMEZONE).date()
        await message.answer(f"正在只读查询花名册 G 列生效日期为 {today.isoformat()} 的新人……")
        try:
            candidates = await services.sheets.onboarding_notify_candidates(today)
        except Exception as exc:
            logger.exception("Onboarding notify lookup failed")
            await message.answer(f"花名册读取失败：{str(exc)[:160]}")
            return
        if not candidates:
            await message.answer(f"今天（{today.isoformat()}）花名册里没有生效日期是今天的新人。")
            return

        notice_text = build_onboarding_notice(candidates)
        sync_text = build_onboarding_sync(candidates)
        names = "、".join(str(candidate.get("chinese_name") or "") for candidate in candidates)

        await state.clear()
        await state.set_state(OnboardingNotify.waiting_for_confirmation)
        await state.update_data(
            onboarding_notice_text=notice_text,
            onboarding_sync_text=sync_text,
            onboarding_candidates=candidates,
            onboarding_names=names,
            onboarding_preview_message_ids=[],
        )
        await _onboarding_send_preview(message, state, notice_text, sync_text, names)

    async def _onboarding_send_preview(
        message: Message, state: FSMContext, notice_text: str, sync_text: str, names: str
    ) -> None:
        candidate_count = names.count("、") + 1 if names else 0
        preview = (
            f"今日入职 {candidate_count} 人：{names}\n\n"
            f"———【新人入职通知】预览（将发到恒睿公司-联合管理工作群）———\n\n{notice_text}\n\n"
            f"———【入职信息同步】预览（下午随机 15:15-18:00 之间发到人事数据同步-SSC3组）———\n\n{sync_text}\n\n"
            "回复 1 确认发送（发送后还会自动生成并私信入职协议）；回复 编辑 可以在发送前修改"
            "【新人入职通知】或【入职信息同步】的内容；回复 /cancel 取消本轮。"
        )
        data = await state.get_data()
        preview_message_ids = [int(v) for v in data.get("onboarding_preview_message_ids") or []]
        for start in range(0, len(preview), 3700):
            sent = await message.answer(preview[start:start + 3700])
            preview_message_ids.append(sent.message_id)
        await state.update_data(onboarding_preview_message_ids=preview_message_ids)

    @router.message(OnboardingNotify.waiting_for_confirmation, F.text)
    async def onboarding_notify_confirm(message: Message, state: FSMContext, bot: Bot) -> None:
        if not message.from_user or not _is_admin(message.from_user.id, services.settings):
            await state.clear()
            return
        text = (message.text or "").strip()
        if text.casefold().startswith("/cancel"):
            return  # 交给全局 /cancel 命令处理
        if text == "编辑":
            await state.set_state(OnboardingNotify.waiting_for_edit_choice)
            await message.answer(
                "要修改哪一段？回复 1 修改【新人入职通知】；回复 2 修改【入职信息同步】；"
                "回复 0 不改了，返回上一步；回复 /cancel 取消本轮。"
            )
            return
        if text != "1":
            await message.answer("回复 1 确认发送到群；回复 编辑 可以先修改内容；或回复 /cancel 取消本轮。")
            return

        data = await state.get_data()
        notice_text = str(data.get("onboarding_notice_text") or "")
        sync_text = str(data.get("onboarding_sync_text") or "")
        candidates: list[dict[str, str]] = data.get("onboarding_candidates") or []
        preview_message_ids = [int(value) for value in data.get("onboarding_preview_message_ids") or []]
        admin_id = message.from_user.id
        chat_id = message.chat.id
        notify_chat_id = services.settings.onboarding_notify_group_chat_id
        sync_chat_id = services.settings.onboarding_sync_group_chat_id
        await state.clear()

        if not notice_text or not sync_text or notify_chat_id is None or sync_chat_id is None:
            await message.answer("本轮预览已失效，请重新执行 /onboarding_notify。")
            return

        try:
            notice_sent = await bot.send_message(notify_chat_id, notice_text)
        except Exception as exc:
            logger.exception("Onboarding notice send failed")
            await message.answer(
                f"发送【新人入职通知】到群失败：{str(exc)[:160]}；本轮已终止，下午的入职信息同步也不会发送。"
            )
            return
        await message.answer(
            "✅ 【新人入职通知】已发送到恒睿公司-联合管理工作群。",
            reply_markup=_edit_sent_button(notify_chat_id, notice_sent.message_id),
        )

        if candidates:
            await _send_onboarding_agreements(bot, chat_id, candidates, services)

        now = datetime.now(ATTENDANCE_TIMEZONE)
        window_start = now.replace(hour=15, minute=15, second=0, microsecond=0)
        window_end = now.replace(hour=18, minute=0, second=0, microsecond=0)
        if now >= window_end:
            target = now + timedelta(seconds=5)
            await message.answer("现在已经过了 15:15-18:00 这个时间段，【入职信息同步】将在几秒后直接发送。")
        else:
            start_from = max(now, window_start)
            span_seconds = max(0, int((window_end - start_from).total_seconds()))
            target = start_from + timedelta(seconds=random.randint(0, span_seconds))
            await message.answer(f"已安排今天 {target.strftime('%H:%M')} 左右发送【入职信息同步】到人事数据同步-SSC3组。")

        async def _send_sync_later() -> None:
            try:
                delay = max(0.0, (target - datetime.now(ATTENDANCE_TIMEZONE)).total_seconds())
                await asyncio.sleep(delay)
                sync_sent = await bot.send_message(sync_chat_id, sync_text)
                await bot.send_message(
                    chat_id, "✅ 【入职信息同步】已发送到人事数据同步-SSC3组。本轮入职通知转发完成。",
                    reply_markup=_edit_sent_button(sync_chat_id, sync_sent.message_id),
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Onboarding sync send failed")
                try:
                    await bot.send_message(chat_id, "发送【入职信息同步】到群失败，请人工处理。")
                except Exception:
                    logger.exception("Failed to notify admin about onboarding sync failure")
            finally:
                services.onboarding_sync_tasks.pop(admin_id, None)
                for message_id in preview_message_ids:
                    try:
                        await bot.delete_message(chat_id, message_id)
                    except Exception:
                        logger.exception("Failed to delete onboarding preview message %s", message_id)

        existing = services.onboarding_sync_tasks.pop(admin_id, None)
        if existing and not existing.done():
            existing.cancel()
        services.onboarding_sync_tasks[admin_id] = asyncio.create_task(_send_sync_later())

    @router.message(OnboardingNotify.waiting_for_edit_choice, F.text)
    async def onboarding_notify_edit_choice(message: Message, state: FSMContext) -> None:
        if not message.from_user or not _is_admin(message.from_user.id, services.settings):
            await state.clear()
            return
        text = (message.text or "").strip()
        if text.casefold().startswith("/cancel"):
            return  # 交给全局 /cancel 命令处理
        if text == "0":
            data = await state.get_data()
            await state.set_state(OnboardingNotify.waiting_for_confirmation)
            await _onboarding_send_preview(
                message, state,
                str(data.get("onboarding_notice_text") or ""),
                str(data.get("onboarding_sync_text") or ""),
                str(data.get("onboarding_names") or ""),
            )
            return
        if text not in {"1", "2"}:
            await message.answer("回复 1 修改【新人入职通知】；回复 2 修改【入职信息同步】；回复 0 返回；或回复 /cancel 取消本轮。")
            return
        await state.update_data(onboarding_edit_target="notice" if text == "1" else "sync")
        await state.set_state(OnboardingNotify.waiting_for_edit_text)
        label = "【新人入职通知】" if text == "1" else "【入职信息同步】"
        await message.answer(f"请发送 {label} 的新内容（整段替换，不是在原文上增量修改）。")

    @router.message(OnboardingNotify.waiting_for_edit_text, F.text)
    async def onboarding_notify_edit_text(message: Message, state: FSMContext) -> None:
        if not message.from_user or not _is_admin(message.from_user.id, services.settings):
            await state.clear()
            return
        text = message.text or ""
        if text.strip().casefold().startswith("/cancel"):
            return  # 交给全局 /cancel 命令处理
        data = await state.get_data()
        target = data.get("onboarding_edit_target")
        notice_text = str(data.get("onboarding_notice_text") or "")
        sync_text = str(data.get("onboarding_sync_text") or "")
        if target == "notice":
            notice_text = text
        elif target == "sync":
            sync_text = text
        else:
            await message.answer("本轮编辑状态已失效，请重新执行 /onboarding_notify。")
            await state.clear()
            return
        await state.update_data(onboarding_notice_text=notice_text, onboarding_sync_text=sync_text)
        await state.set_state(OnboardingNotify.waiting_for_confirmation)
        await message.answer("已更新，预览如下：")
        await _onboarding_send_preview(message, state, notice_text, sync_text, str(data.get("onboarding_names") or ""))

    @router.message(Command("probation_notify"))
    async def probation_notify_start(message: Message, state: FSMContext, bot: Bot) -> None:
        if not message.from_user or not _is_admin(message.from_user.id, services.settings):
            await message.answer("无权使用此命令。")
            return
        await message.answer("正在只读查询花名册 BK 列（转正日期）……")
        await run_probation_notify(bot, message.chat.id, state, services)

    async def _probation_mark_done_and_maybe_cleanup(
        state: FSMContext, bot: Bot, chat_id: int, action: str
    ) -> None:
        data = await state.get_data()
        done = dict(data.get("probation_done") or {})
        done[action] = True
        await state.update_data(probation_done=done)
        advance = data.get("probation_advance") or []
        due = data.get("probation_due") or []
        required = set()
        if advance:
            required |= {"1", "2", "2.1"}
        if due:
            required.add("3")
        if required and required.issubset(set(k for k, v in done.items() if v)):
            preview_message_ids = [int(v) for v in data.get("probation_preview_message_ids") or []]
            for message_id in preview_message_ids:
                try:
                    await bot.delete_message(chat_id, message_id)
                except Exception:
                    logger.exception("Failed to delete probation preview message %s", message_id)
            await state.clear()

    @router.message(ProbationNotify.waiting_for_action, F.text)
    async def probation_notify_action(message: Message, state: FSMContext, bot: Bot) -> None:
        if not message.from_user or not _is_admin(message.from_user.id, services.settings):
            await state.clear()
            return
        text = (message.text or "").strip()
        if text.casefold().startswith("/cancel"):
            return  # 交给全局 /cancel 命令处理
        if text not in {"1", "2", "2.1", "2.2", "3"}:
            await message.answer("回复 1 / 2 / 2.1 / 2.2 / 3 执行对应步骤；或回复 /cancel 取消本轮。")
            return

        data = await state.get_data()
        advance: list[dict[str, str]] = data.get("probation_advance") or []
        due: list[dict[str, str]] = data.get("probation_due") or []
        reminder_text = str(data.get("probation_reminder_text") or "")
        sync_text = str(data.get("probation_sync_text") or "")
        admin_id = message.from_user.id
        chat_id = message.chat.id
        settings = services.settings

        if text == "1":
            if not advance or not reminder_text:
                await message.answer("没有需要发送的【新人预转正提醒】。")
                return
            try:
                reminder_sent = await bot.send_message(settings.probation_union_group_chat_id, reminder_text)
            except Exception as exc:
                logger.exception("Probation reminder send failed")
                await message.answer(f"发送【新人预转正提醒】到群失败：{str(exc)[:160]}")
                return
            await message.answer(
                "✅ 【新人预转正提醒】已发送到恒睿公司-联合管理群。",
                reply_markup=_edit_sent_button(settings.probation_union_group_chat_id, reminder_sent.message_id),
            )
            await _probation_mark_done_and_maybe_cleanup(state, bot, chat_id, "1")
            return

        if text == "2.2":
            if not advance:
                await message.answer("没有需要发送的【试用期转正申请】。")
                return
            blocks = []
            for candidate in advance:
                form_link = await _probation_resolve_form_link(services, candidate)
                blocks.append(_build_probation_application_block(candidate, form_link))
            application_text = "\n\n———\n\n".join(blocks)
            last_sent = None
            try:
                for start in range(0, len(application_text), 3900):
                    last_sent = await bot.send_message(
                        settings.probation_union_group_chat_id, application_text[start:start + 3900]
                    )
            except Exception as exc:
                logger.exception("Probation application group send failed")
                await message.answer(f"发送【试用期转正申请】到群失败：{str(exc)[:160]}")
                return
            reply_markup = (
                _edit_sent_button(settings.probation_union_group_chat_id, last_sent.message_id)
                if last_sent else None
            )
            await message.answer(
                "✅ 【试用期转正申请】已发送到恒睿公司-联合管理群。"
                + ("（内容太长分了多条，编辑按钮只对应最后一条）" if last_sent and len(application_text) > 3900 else ""),
                reply_markup=reply_markup,
            )
            await _probation_mark_done_and_maybe_cleanup(state, bot, chat_id, "2.2")
            return

        if text in {"2", "2.1"}:
            if not advance:
                await message.answer("没有需要处理的【转正申请】。")
                return
            successful: list[str] = []
            failed: list[str] = []
            skipped: list[str] = []
            for candidate in advance:
                name = str(candidate.get("chinese_name") or "")
                form_link = await _probation_resolve_form_link(services, candidate)
                block = _build_probation_application_block(candidate, form_link)
                if text == "2":
                    target_tg = str(candidate.get("work_tg") or "").strip().lstrip("@")
                    if not target_tg:
                        failed.append(f"{name}（花名册 AE 列没有工作TG）")
                        continue
                    caption = "填写个人部分并签字，请写详细一些"
                else:
                    supervisor_name = str(candidate.get("direct_supervisor") or "").strip()
                    if not supervisor_name:
                        failed.append(f"{name}（花名册 AA 列没有直接上级）")
                        continue
                    if supervisor_name in PROBATION_SUPERVISOR_SKIP_NAMES:
                        skipped.append(f"{name}（直接上级 {supervisor_name} 无需走此流程）")
                        continue
                    supervisor_tg = await services.sheets.resolve_work_tg_by_name(supervisor_name)
                    if not supervisor_tg:
                        failed.append(f"{name}（花名册里找不到直接上级「{supervisor_name}」自己的工作TG）")
                        continue
                    target_tg = supervisor_tg.lstrip("@")
                    caption = "请领导审批"
                try:
                    target_chat = await bot.get_chat(f"@{target_tg}")
                    await bot.send_message(target_chat.id, f"{block}\n\n{caption}")
                    successful.append(name)
                except Exception as exc:
                    logger.exception("Probation application send failed for %s", name)
                    failed.append(f"{name}（@{target_tg}，{str(exc)[:80]}）")
            lines = [f"步骤 {text} 完成。成功：{len(successful)}，跳过：{len(skipped)}，失败：{len(failed)}"]
            if successful:
                lines.append("✅ " + "、".join(successful))
            if skipped:
                lines.append("⏭️ " + "、".join(skipped))
            if failed:
                lines.append("❌ " + "\n".join(failed))
            await message.answer("\n".join(lines)[:3900])
            await _probation_mark_done_and_maybe_cleanup(state, bot, chat_id, text)
            return

        # text == "3"
        if not due:
            await message.answer("没有今天就转正的员工。")
            return
        successful = []
        failed = []
        sent_refs: list[tuple[str, int, int]] = []  # (花名, chat_id, message_id)，用来挂编辑按钮
        for index, candidate in enumerate(due):
            if index > 0:
                # 多人连续发转正海报到全员群时，每次发送间随机间隔 30 秒 - 1 分钟。
                await asyncio.sleep(random.uniform(30, 60))
            name = str(candidate.get("chinese_name") or "")
            department = str(candidate.get("department") or "")
            target_chat_id = _probation_group_chat_id(department)
            if target_chat_id is None:
                failed.append(f"{name}（部门「{department}」没有对应的全员群映射）")
                continue
            notice_text = build_probation_notice_text(candidate)
            try:
                poster = await services.sheets.find_probation_poster(
                    settings.probation_poster_drive_folder_id, name
                )
            except Exception:
                logger.exception("Probation poster lookup failed for %s", name)
                poster = None
            try:
                if poster:
                    content, _mime = poster
                    sent = await bot.send_photo(
                        target_chat_id,
                        BufferedInputFile(content, filename=f"{name}.jpg"),
                        caption=notice_text,
                    )
                else:
                    sent = await bot.send_message(target_chat_id, notice_text)
                successful.append(name)
                sent_refs.append((name, target_chat_id, sent.message_id))
            except Exception as exc:
                logger.exception("Probation notice send failed for %s", name)
                failed.append(f"{name}（{str(exc)[:80]}）")
        lines = [f"【转正通知】发送完成。成功：{len(successful)}，失败：{len(failed)}"]
        if successful:
            lines.append("✅ " + "、".join(successful))
        if failed:
            lines.append("❌ " + "\n".join(failed))
        edit_buttons = [
            [InlineKeyboardButton(text=f"✏️ 编辑 {name}", callback_data=f"editsent:{chat_id_}:{message_id_}")]
            for name, chat_id_, message_id_ in sent_refs
        ]
        await message.answer(
            "\n".join(lines)[:3900],
            reply_markup=InlineKeyboardMarkup(inline_keyboard=edit_buttons) if edit_buttons else None,
        )

        if not sync_text:
            await _probation_mark_done_and_maybe_cleanup(state, bot, chat_id, "3")
            return

        sync_chat_id = settings.probation_sync_group_chat_id
        now = datetime.now(ATTENDANCE_TIMEZONE)
        window_start = now.replace(hour=16, minute=45, second=0, microsecond=0)
        window_end = now.replace(hour=19, minute=0, second=0, microsecond=0)
        if now >= window_end:
            target = now + timedelta(seconds=5)
            await message.answer("现在已经过了 16:45-19:00 这个时间段，【转正信息同步】将在几秒后直接发送。")
        else:
            start_from = max(now, window_start)
            span_seconds = max(0, int((window_end - start_from).total_seconds()))
            target = start_from + timedelta(seconds=random.randint(0, span_seconds))
            await message.answer(f"已安排今天 {target.strftime('%H:%M')} 左右发送【转正信息同步】到人事数据同步-SSC3组。")

        async def _send_probation_sync_later() -> None:
            try:
                delay = max(0.0, (target - datetime.now(ATTENDANCE_TIMEZONE)).total_seconds())
                await asyncio.sleep(delay)
                probation_sync_sent = await bot.send_message(sync_chat_id, sync_text)
                await bot.send_message(
                    chat_id, "✅ 【转正信息同步】已发送到人事数据同步-SSC3组。",
                    reply_markup=_edit_sent_button(sync_chat_id, probation_sync_sent.message_id),
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Probation sync send failed")
                try:
                    await bot.send_message(chat_id, "发送【转正信息同步】到群失败，请人工处理。")
                except Exception:
                    logger.exception("Failed to notify admin about probation sync failure")
            finally:
                services.probation_sync_tasks.pop(admin_id, None)
                await _probation_mark_done_and_maybe_cleanup(state, bot, chat_id, "3")

        existing = services.probation_sync_tasks.pop(admin_id, None)
        if existing and not existing.done():
            existing.cancel()
        services.probation_sync_tasks[admin_id] = asyncio.create_task(_send_probation_sync_later())

    @router.callback_query(F.data == "channelmsg:cancel")
    async def channel_message_cancel(callback: CallbackQuery, state: FSMContext) -> None:
        if not callback.from_user or not _is_admin(callback.from_user.id, services.settings):
            await callback.answer("无权操作", show_alert=True)
            return
        await state.clear()
        await callback.answer("已取消")
        if callback.message:
            await callback.message.edit_reply_markup(reply_markup=None)

    @router.callback_query(ChannelMessageSend.waiting_for_confirmation, F.data == "channelmsg:confirm")
    async def channel_message_confirm(callback: CallbackQuery, state: FSMContext, bot: Bot) -> None:
        if not callback.from_user or not _is_admin(callback.from_user.id, services.settings):
            await callback.answer("无权操作", show_alert=True)
            return
        data = await state.get_data()
        user_ids = [int(value) for value in data.get("channel_send_ids", [])]
        target_names = dict(data.get("channel_target_names") or {})
        target_work_tg = dict(data.get("channel_target_work_tg") or {})
        template = await services.db.template(int(data.get("channel_template_id") or 0))
        recipients = await services.db.active_employees_by_ids(user_ids)
        if not template or not recipients:
            await state.clear()
            await callback.answer("模板或联系人已失效", show_alert=True)
            return
        await state.clear()
        await callback.answer("开始发送")
        if callback.message:
            await callback.message.edit_reply_markup(reply_markup=None)
            await callback.message.answer("已确认，正在发送“关注恒睿频道”消息……")
        successful: list[Employee] = []
        failed: list[Employee] = []
        delay = 1 / services.settings.broadcast_messages_per_second
        for employee in recipients:
            try:
                await bot.send_message(employee.chat_id, template[2])
                successful.append(employee)
                try:
                    await services.db.save_outbound_message(
                        employee.telegram_user_id, employee.chat_id, template[2],
                        message_kind="channel_message", template_name=template[1],
                    )
                except Exception:
                    logger.exception("Failed to log channel message for user %s", employee.telegram_user_id)
            except TelegramRetryAfter as exc:
                await asyncio.sleep(exc.retry_after + 0.5)
                try:
                    await bot.send_message(employee.chat_id, template[2])
                    successful.append(employee)
                    try:
                        await services.db.save_outbound_message(
                            employee.telegram_user_id, employee.chat_id, template[2],
                            message_kind="channel_message", template_name=template[1],
                        )
                    except Exception:
                        logger.exception("Failed to log channel message for user %s", employee.telegram_user_id)
                except Exception:
                    logger.exception("Channel-message retry failed for user %s", employee.telegram_user_id)
                    failed.append(employee)
            except (TelegramForbiddenError, TelegramBadRequest):
                await services.db.deactivate_employee(employee.telegram_user_id)
                failed.append(employee)
            except Exception:
                logger.exception("Channel-message send failed for user %s", employee.telegram_user_id)
                failed.append(employee)
            await asyncio.sleep(delay)
        await services.db.save_broadcast(
            callback.from_user.id, f"[关注恒睿频道]\n{template[2]}",
            len(successful), len(failed),
        )
        if callback.message:
            lines = [
                "“关注恒睿频道”发送完成。",
                f"成功：{len(successful)}",
                f"失败：{len(failed)}",
            ]
            if successful:
                lines.append("\n✅ 成功名单：\n" + "\n".join(
                    f"{target_names.get(str(item.telegram_user_id), item.full_name)} · "
                    f"{target_work_tg.get(str(item.telegram_user_id), 'AE列无工作TG')}"
                    for item in successful
                ))
            if failed:
                lines.append("\n❌ 失败名单：\n" + "\n".join(
                    f"{target_names.get(str(item.telegram_user_id), item.full_name)} · "
                    f"{target_work_tg.get(str(item.telegram_user_id), 'AE列无工作TG')}"
                    for item in failed
                ))
            await callback.message.answer("\n".join(lines)[:3900])

    @router.message(Command("profile"))
    async def my_profile(message: Message) -> None:
        if not message.from_user:
            return
        profile = await services.db.profile(message.from_user.id)
        if not profile:
            await message.answer("你还没有员工档案。请发送“新人入职（按格式填写）”完整表单。")
            return
        await message.answer(profile_text(profile))

    @router.message(Command("profiles"))
    async def all_profiles(message: Message) -> None:
        if not message.from_user or not _is_admin(message.from_user.id, services.settings):
            await message.answer("无权使用此命令。")
            return
        profiles = await services.db.profiles()
        if not profiles:
            await message.answer("目前没有员工档案。")
            return
        rows = []
        for item in profiles[:80]:
            label = f"{item['chinese_name']} · {item['resume_name']} · {item['employee_code']}"
            rows.append([InlineKeyboardButton(text=label[:60], callback_data=f"profile:view:{item['telegram_user_id']}")])
        await message.answer(f"员工档案共 {len(profiles)} 份：", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))

    @router.callback_query(F.data.startswith("profile:view:"))
    async def admin_view_profile(callback: CallbackQuery) -> None:
        if not callback.from_user or not _is_admin(callback.from_user.id, services.settings):
            await callback.answer("无权操作", show_alert=True)
            return
        user_id = int((callback.data or "").rsplit(":", 1)[-1])
        profile = await services.db.profile(user_id)
        await callback.answer()
        if callback.message and profile:
            keyboard = InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="删除这份档案", callback_data=f"profile:delete_ask:{user_id}")
            ]])
            await callback.message.answer(profile_text(profile), reply_markup=keyboard)

    @router.message(Command("profile_delete"))
    async def profile_delete_command(message: Message) -> None:
        if not message.from_user:
            return
        profile = await services.db.profile(message.from_user.id)
        if not profile:
            await message.answer("你目前没有可删除的员工档案。")
            return
        keyboard = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="确认删除", callback_data=f"profile:delete_confirm:{message.from_user.id}"),
            InlineKeyboardButton(text="取消", callback_data="profile:delete_cancel"),
        ]])
        await message.answer("确认删除你的员工档案吗？这会清除机器人管理的云表字段，并在变更记录中保留审计记录。", reply_markup=keyboard)

    @router.callback_query(F.data.startswith("profile:delete_ask:"))
    async def profile_delete_ask(callback: CallbackQuery) -> None:
        if not callback.from_user or not _is_admin(callback.from_user.id, services.settings):
            await callback.answer("无权操作", show_alert=True)
            return
        user_id = int((callback.data or "").rsplit(":", 1)[-1])
        if callback.message:
            await callback.message.answer(
                "确认删除这份档案并清除云表中由机器人管理的字段吗？",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                    InlineKeyboardButton(text="确认删除", callback_data=f"profile:delete_confirm:{user_id}"),
                    InlineKeyboardButton(text="取消", callback_data="profile:delete_cancel"),
                ]]),
            )
        await callback.answer()

    @router.callback_query(F.data == "profile:delete_cancel")
    async def profile_delete_cancel(callback: CallbackQuery) -> None:
        await callback.answer("已取消")
        if callback.message:
            await callback.message.edit_reply_markup(reply_markup=None)

    @router.callback_query(F.data.startswith("profile:delete_confirm:"))
    async def profile_delete_confirm(callback: CallbackQuery) -> None:
        if not callback.from_user:
            return
        user_id = int((callback.data or "").rsplit(":", 1)[-1])
        if callback.from_user.id != user_id and not _is_admin(callback.from_user.id, services.settings):
            await callback.answer("无权操作", show_alert=True)
            return
        profile = await services.db.profile(user_id)
        if not profile:
            await callback.answer("档案不存在", show_alert=True)
            return
        if services.sheets.configured:
            try:
                await services.sheets.clear_profile(profile)
            except Exception as exc:
                logger.exception("Failed to clear Google profile for %s", user_id)
                await callback.answer(f"云表清除失败，暂未删除：{str(exc)[:80]}", show_alert=True)
                return
        await services.db.delete_profile(user_id)
        await callback.answer("已删除")
        if callback.message:
            await callback.message.edit_reply_markup(reply_markup=None)
            await callback.message.answer("员工档案已删除。")

    @router.message(Command("template_add"))
    async def template_add(message: Message, state: FSMContext) -> None:
        if not message.from_user or not _is_admin(message.from_user.id, services.settings):
            await message.answer("无权使用此命令。")
            return
        await state.clear()
        await state.set_state(TemplateCreate.waiting_for_name)
        await message.answer(
            "请输入通知模板名称，例如“新人须知”。使用已有名称会更新该模板。\n"
            "发送 /cancel 可取消。"
        )

    @router.message(TemplateCreate.waiting_for_name, F.text)
    async def template_name(message: Message, state: FSMContext) -> None:
        name = message.text.strip()
        if len(name) > 40:
            await message.answer("模板名称请控制在 40 个字符以内。")
            return
        await state.update_data(template_name=name)
        await state.set_state(TemplateCreate.waiting_for_content)
        await message.answer("请输入模板通知正文。")

    @router.message(TemplateCreate.waiting_for_content, F.text)
    async def template_content(message: Message, state: FSMContext) -> None:
        content = message.text.strip()
        if len(content) > 3900:
            await message.answer("通知过长，请控制在 3900 个字符以内。")
            return
        await state.update_data(template_content=content)
        await state.set_state(TemplateCreate.waiting_for_auto_choice)
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="是，新人自动发送", callback_data="template:auto:yes"),
                    InlineKeyboardButton(text="否，仅手动复用", callback_data="template:auto:no"),
                ]
            ]
        )
        await message.answer("新人首次发送 /start 时，是否自动发送这个模板？", reply_markup=keyboard)

    @router.callback_query(TemplateCreate.waiting_for_auto_choice, F.data.startswith("template:auto:"))
    async def template_auto_choice(callback: CallbackQuery, state: FSMContext) -> None:
        if not callback.from_user or not _is_admin(callback.from_user.id, services.settings):
            await callback.answer("无权操作", show_alert=True)
            return
        data = await state.get_data()
        name = str(data.get("template_name", ""))
        content = str(data.get("template_content", ""))
        auto_send = (callback.data or "").endswith(":yes")
        template_id = await services.db.upsert_template(name, content, auto_send)
        await state.clear()
        await callback.answer("模板已保存")
        if callback.message:
            await callback.message.edit_reply_markup(reply_markup=None)
            await callback.message.answer(
                f"模板 #{template_id}“{name}”已保存。"
                f"新人自动发送：{'是' if auto_send else '否'}\n使用 /templates 可重复发送。"
            )

    @router.message(Command("templates"))
    async def templates_command(message: Message, state: FSMContext) -> None:
        if not message.from_user or not _is_admin(message.from_user.id, services.settings):
            await message.answer("无权使用此命令。")
            return
        await state.clear()
        templates = await services.db.templates()
        if not templates:
            await message.answer("还没有通知模板。使用 /template_add 创建。")
            return
        rows: list[list[InlineKeyboardButton]] = []
        for template_id, name, _, auto_send in templates:
            auto_mark = " · 新人自动" if auto_send else ""
            rows.append(
                [
                    InlineKeyboardButton(
                        text=f"发送：{name}{auto_mark}"[:60],
                        callback_data=f"template:use:{template_id}",
                    ),
                    InlineKeyboardButton(text="删除", callback_data=f"template:delete:{template_id}"),
                ]
            )
        await message.answer(
            "请选择要复用的通知模板。需要修改时，使用 /template_add 并输入相同名称。",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
        )

    @router.callback_query(F.data.startswith("template:delete:"))
    async def template_delete(callback: CallbackQuery) -> None:
        if not callback.from_user or not _is_admin(callback.from_user.id, services.settings):
            await callback.answer("无权操作", show_alert=True)
            return
        template_id = int((callback.data or "").rsplit(":", maxsplit=1)[-1])
        await services.db.delete_template(template_id)
        await callback.answer("模板已删除", show_alert=True)
        if callback.message:
            await callback.message.edit_reply_markup(reply_markup=None)

    @router.callback_query(F.data.startswith("template:use:"))
    async def template_use(callback: CallbackQuery, state: FSMContext) -> None:
        if not callback.from_user or not _is_admin(callback.from_user.id, services.settings):
            await callback.answer("无权操作", show_alert=True)
            return
        template_id = int((callback.data or "").rsplit(":", maxsplit=1)[-1])
        template = await services.db.template(template_id)
        employees = await services.db.active_employees()
        if not template or not employees:
            await callback.answer("模板不存在或没有联系人", show_alert=True)
            return
        _, name, content, _ = template
        await state.clear()
        await state.set_state(Broadcast.selecting_recipients)
        await state.update_data(selected_ids=[], content=content, template_name=name)
        await callback.answer()
        if callback.message:
            await callback.message.answer(
                f"模板“{name}”：请选择接收人。",
                reply_markup=_selection_keyboard(employees, set(), 0),
            )

    @router.message(Command("broadcast"))
    async def broadcast_start(message: Message, state: FSMContext) -> None:
        if not message.from_user or not _is_admin(message.from_user.id, services.settings):
            await message.answer("无权使用此命令。")
            return
        employees = await services.db.active_employees()
        if not employees:
            await message.answer("当前没有已登记的联系人。请让同事先向机器人发送 /start。")
            return
        await state.clear()
        await state.set_state(Broadcast.selecting_recipients)
        await state.update_data(selected_ids=[])
        await message.answer(
            f"请选择通知接收人。点击联系人可以勾选或取消。\n\n已选择：0 人｜可选：{len(employees)} 人",
            reply_markup=_selection_keyboard(employees, set(), 0),
        )

    @router.callback_query(Broadcast.selecting_recipients, F.data == "broadcast:noop")
    async def broadcast_noop(callback: CallbackQuery) -> None:
        await callback.answer()

    @router.callback_query(Broadcast.selecting_recipients, F.data.startswith("broadcast:page:"))
    async def broadcast_page(callback: CallbackQuery, state: FSMContext) -> None:
        if not callback.from_user or not _is_admin(callback.from_user.id, services.settings):
            await callback.answer("无权操作", show_alert=True)
            return
        page = int((callback.data or "").rsplit(":", maxsplit=1)[-1])
        await _render_recipient_selection(callback, services, state, page)
        await callback.answer()

    @router.callback_query(Broadcast.selecting_recipients, F.data.startswith("broadcast:toggle:"))
    async def broadcast_toggle(callback: CallbackQuery, state: FSMContext) -> None:
        if not callback.from_user or not _is_admin(callback.from_user.id, services.settings):
            await callback.answer("无权操作", show_alert=True)
            return
        try:
            _, _, raw_user_id, raw_page = (callback.data or "").split(":")
            user_id, page = int(raw_user_id), int(raw_page)
        except ValueError:
            await callback.answer("联系人数据无效", show_alert=True)
            return
        data = await state.get_data()
        selected_ids = {int(value) for value in data.get("selected_ids", [])}
        if user_id in selected_ids:
            selected_ids.remove(user_id)
        else:
            selected_ids.add(user_id)
        await state.update_data(selected_ids=sorted(selected_ids))
        await _render_recipient_selection(callback, services, state, page)
        await callback.answer()

    @router.callback_query(Broadcast.selecting_recipients, F.data == "broadcast:select_all")
    async def broadcast_select_all(callback: CallbackQuery, state: FSMContext) -> None:
        if not callback.from_user or not _is_admin(callback.from_user.id, services.settings):
            await callback.answer("无权操作", show_alert=True)
            return
        employees = await services.db.active_employees()
        await state.update_data(selected_ids=[employee.telegram_user_id for employee in employees])
        await _render_recipient_selection(callback, services, state, 0)
        await callback.answer(f"已选择 {len(employees)} 人")

    @router.callback_query(Broadcast.selecting_recipients, F.data == "broadcast:clear")
    async def broadcast_clear(callback: CallbackQuery, state: FSMContext) -> None:
        if not callback.from_user or not _is_admin(callback.from_user.id, services.settings):
            await callback.answer("无权操作", show_alert=True)
            return
        await state.update_data(selected_ids=[])
        await _render_recipient_selection(callback, services, state, 0)
        await callback.answer("已清空")

    @router.callback_query(Broadcast.selecting_recipients, F.data == "broadcast:recipients_done")
    async def broadcast_recipients_done(callback: CallbackQuery, state: FSMContext) -> None:
        if not callback.from_user or not _is_admin(callback.from_user.id, services.settings):
            await callback.answer("无权操作", show_alert=True)
            return
        data = await state.get_data()
        selected_ids = [int(value) for value in data.get("selected_ids", [])]
        if not selected_ids:
            await callback.answer("请至少选择一个联系人", show_alert=True)
            return
        preset_content = str(data.get("content", "")).strip()
        recipients = await services.db.active_employees_by_ids(selected_ids)
        if preset_content and recipients:
            await state.set_state(Broadcast.waiting_for_confirmation)
            keyboard = InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(text="确认发送", callback_data="broadcast:confirm"),
                        InlineKeyboardButton(text="取消", callback_data="broadcast:cancel"),
                    ]
                ]
            )
            await callback.answer()
            if callback.message:
                await callback.message.edit_text(
                    f"【模板群发预览】\n\n{preset_content}\n\n接收人数：{len(recipients)}",
                    reply_markup=keyboard,
                )
            return
        await state.set_state(Broadcast.waiting_for_content)
        await callback.answer()
        if callback.message:
            await callback.message.edit_text(
                f"已选择 {len(selected_ids)} 人。\n\n请输入要发送的通知正文，发送 /cancel 可取消。"
            )

    @router.message(Broadcast.waiting_for_content, F.text)
    async def broadcast_preview(message: Message, state: FSMContext) -> None:
        if not message.from_user or not _is_admin(message.from_user.id, services.settings):
            await state.clear()
            return
        content = message.text.strip()
        if len(content) > 3900:
            await message.answer("通知过长，请控制在 3900 个字符以内。")
            return
        await state.update_data(content=content)
        data = await state.get_data()
        selected_ids = [int(value) for value in data.get("selected_ids", [])]
        recipients = await services.db.active_employees_by_ids(selected_ids)
        if not recipients:
            await state.clear()
            await message.answer("所选联系人已经失效，请重新执行 /broadcast。")
            return
        await state.set_state(Broadcast.waiting_for_confirmation)
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="确认发送", callback_data="broadcast:confirm"),
                    InlineKeyboardButton(text="取消", callback_data="broadcast:cancel"),
                ]
            ]
        )
        await message.answer(
            f"【群发预览】\n\n{content}\n\n接收人数：{len(recipients)}",
            reply_markup=keyboard,
        )

    @router.callback_query(F.data == "broadcast:cancel")
    async def broadcast_cancel(callback: CallbackQuery, state: FSMContext) -> None:
        if not callback.from_user or not _is_admin(callback.from_user.id, services.settings):
            await callback.answer("无权操作", show_alert=True)
            return
        await state.clear()
        await callback.answer("已取消")
        if callback.message:
            await callback.message.edit_reply_markup(reply_markup=None)

    @router.callback_query(Broadcast.waiting_for_confirmation, F.data == "broadcast:confirm")
    async def broadcast_confirm(callback: CallbackQuery, state: FSMContext, bot: Bot) -> None:
        if not callback.from_user or not _is_admin(callback.from_user.id, services.settings):
            await callback.answer("无权操作", show_alert=True)
            return
        data = await state.get_data()
        content = str(data.get("content", "")).strip()
        template_name = str(data.get("template_name", "")).strip() or None
        selected_ids = [int(value) for value in data.get("selected_ids", [])]
        recipients = await services.db.active_employees_by_ids(selected_ids)
        if not content or not recipients:
            await state.clear()
            await callback.answer("通知内容或联系人已失效", show_alert=True)
            return

        await state.clear()
        await callback.answer("开始发送")
        if callback.message:
            await callback.message.edit_reply_markup(reply_markup=None)
            await callback.message.answer("正在发送，请勿重复操作……")

        successful: list[Employee] = []
        failed: list[Employee] = []
        delay = 1 / services.settings.broadcast_messages_per_second
        for employee in recipients:
            try:
                await bot.send_message(employee.chat_id, content)
                successful.append(employee)
                try:
                    await services.db.save_outbound_message(
                        employee.telegram_user_id, employee.chat_id, content,
                        message_kind="broadcast", template_name=template_name,
                    )
                except Exception:
                    logger.exception("Failed to log outbound message for user %s", employee.telegram_user_id)
            except TelegramRetryAfter as exc:
                await asyncio.sleep(exc.retry_after + 0.5)
                try:
                    await bot.send_message(employee.chat_id, content)
                    successful.append(employee)
                    try:
                        await services.db.save_outbound_message(
                            employee.telegram_user_id, employee.chat_id, content,
                            message_kind="broadcast", template_name=template_name,
                        )
                    except Exception:
                        logger.exception("Failed to log outbound message for user %s", employee.telegram_user_id)
                except Exception:
                    logger.exception(
                        "Broadcast retry failed for user %s", employee.telegram_user_id
                    )
                    failed.append(employee)
            except (TelegramForbiddenError, TelegramBadRequest):
                await services.db.deactivate_employee(employee.telegram_user_id)
                failed.append(employee)
            except Exception:
                logger.exception("Broadcast failed for user %s", employee.telegram_user_id)
                failed.append(employee)
            await asyncio.sleep(delay)

        broadcast_id = await services.db.save_broadcast(
            callback.from_user.id, content, len(successful), len(failed)
        )
        if callback.message:
            await callback.message.answer(
                f"群发完成。记录 #{broadcast_id}\n成功：{len(successful)}\n失败：{len(failed)}"
            )
            await _send_employee_list(callback.message, "✅ 发送成功名单", successful)
            await _send_employee_list(callback.message, "❌ 发送失败名单", failed)

    @router.message(Command("attendance_check"))
    async def attendance_check_start(message: Message, state: FSMContext) -> None:
        if not message.from_user or not _is_admin(message.from_user.id, services.settings):
            await message.answer("无权使用此命令。")
            return
        if not services.settings.openai_api_key or not services.sheets.configured:
            await message.answer("截图识别或 Google 花名册尚未配置，暂时无法执行考勤抽查。")
            return
        attendance_template = await services.db.template_by_name("考勤抽查")
        activation_template = await services.db.template_by_name("激活机器人")
        if not attendance_template or not activation_template:
            await message.answer("缺少启用中的“考勤抽查”或“激活机器人”模板，请先补充模板。")
            return
        await state.clear()
        await state.set_state(AttendanceCheck.waiting_for_screenshot)
        await state.update_data(
            attendance_template_id=attendance_template[0],
            activation_template_id=activation_template[0],
            attendance_sent_ids=[],
            attendance_reported_tg=[],
        )
        await message.answer(
            "请发送包含员工花名的考勤截图，可以连续发送多张。机器人会核对花名册（表格一、表格二）"
            "C 列花名和 AE 列工作 TG：\n"
            "· 已激活机器人的员工会立即收到考勤抽查通知；\n"
            "· 未激活的员工，机器人会把姓名、工作 TG 和激活提示语直接回复给您，方便手动转发。\n\n"
            "如果截图识别不出来，也可以直接发送花名文字兜底，支持空格或逗号分隔多个花名，"
            "例如“张三 李四”或“张三，李四”。\n\n"
            "发送 /cancel 结束本轮抽查，否则会一直等待下一张截图或花名文字。"
        )

    async def _process_attendance_names(
        message: Message, state: FSMContext, bot: Bot, names: list[str]
    ) -> None:
        """考勤抽查的核心比对/发送逻辑，截图识别和管理员直接发花名文字都走这里。"""
        try:
            sheet_results = await services.sheets.attendance_contacts(names)
        except Exception:
            logger.exception("Attendance roster lookup failed for %s", message.from_user.id)
            await message.answer("花名册核对失败，请重试；考勤抽查仍在等待中。")
            return

        state_data = await state.get_data()
        attendance_template = await services.db.template(int(state_data.get("attendance_template_id") or 0))
        activation_template = await services.db.template(int(state_data.get("activation_template_id") or 0))
        if not attendance_template or not activation_template:
            await state.clear()
            await message.answer("考勤相关模板已失效，请重新执行 /attendance_check。")
            return

        employees = await services.db.active_employees()
        employee_by_username = {
            employee.username.strip().lstrip("@").casefold(): employee
            for employee in employees if employee.username and employee.username.strip().lstrip("@")
        }
        # 同一轮 /attendance_check 里（直到 /cancel 之前）已经处理过的人不重复发送/重复上报，
        # 即便他们的花名又出现在后面发来的截图或文字里。
        sent_ids: set[int] = {int(value) for value in state_data.get("attendance_sent_ids") or []}
        reported_tg: set[str] = {str(value) for value in state_data.get("attendance_reported_tg") or []}

        sent_this_round: list[tuple[str, str]] = []
        activation_this_round: list[dict[str, str]] = []
        issues: list[str] = []
        delay = 1 / services.settings.broadcast_messages_per_second

        for result in sheet_results:
            name = str(result.get("chinese_name") or result.get("requested_name") or "")
            status = str(result.get("status") or "")
            if status == "not_found":
                issues.append(f"{name}：花名册 C 列未找到")
                continue
            if status == "missing_tg":
                issues.append(f"{name}：AE 列工作 TG 为空")
                continue
            if status == "ambiguous":
                issues.append(f"{name}：同名行的 AE 列不一致，需人工核对")
                continue
            work_tg = str(result.get("work_tg") or "")
            username_key = work_tg.lstrip("@").casefold()
            employee = employee_by_username.get(username_key)
            if employee:
                if employee.telegram_user_id in sent_ids:
                    continue
                try:
                    await bot.send_message(employee.chat_id, attendance_template[2])
                except TelegramRetryAfter as exc:
                    await asyncio.sleep(exc.retry_after + 0.5)
                    try:
                        await bot.send_message(employee.chat_id, attendance_template[2])
                    except Exception:
                        logger.exception("Attendance retry failed for user %s", employee.telegram_user_id)
                        issues.append(f"{name}：发送失败，请重试")
                        continue
                except (TelegramForbiddenError, TelegramBadRequest):
                    await services.db.deactivate_employee(employee.telegram_user_id)
                    issues.append(f"{name}：发送失败（对方可能已拉黑或停用机器人，已标记为未激活）")
                    continue
                except Exception:
                    logger.exception("Attendance send failed for user %s", employee.telegram_user_id)
                    issues.append(f"{name}：发送失败，请重试")
                    continue
                sent_ids.add(employee.telegram_user_id)
                sent_this_round.append((name, work_tg))
                sent_at = datetime.now(UTC)
                try:
                    await services.db.create_attendance_check(
                        employee.telegram_user_id, name, work_tg,
                        sent_at.astimezone(ATTENDANCE_TIMEZONE).date().isoformat(),
                        sent_at, sent_at + timedelta(minutes=20),
                    )
                except Exception:
                    logger.exception("Failed to persist attendance check for user %s", employee.telegram_user_id)
                await asyncio.sleep(delay)
            else:
                if username_key in reported_tg:
                    continue
                reported_tg.add(username_key)
                activation_this_round.append({"chinese_name": name, "work_tg": work_tg})

        await state.update_data(
            attendance_sent_ids=sorted(sent_ids),
            attendance_reported_tg=sorted(reported_tg),
        )

        lines = [f"本次识别到 {len(names)} 个花名。"]
        if sent_this_round:
            broadcast_id = await services.db.save_broadcast(
                message.from_user.id, f"[考勤抽查]\n{attendance_template[2]}",
                len(sent_this_round), 0,
            )
            lines.append(
                f"\n✅ 已直接发送考勤抽查通知（记录 #{broadcast_id}）：\n" + "\n".join(
                    f"{index}. {person_name} · {tg}"
                    for index, (person_name, tg) in enumerate(sent_this_round, 1)
                )
            )
        if activation_this_round:
            links = "\n".join(
                f"{item['chinese_name']}：{item['work_tg']}"
                f"（https://t.me/{str(item['work_tg']).lstrip('@')}）"
                for item in activation_this_round
            )
            lines.append(
                "\n⚠️ 以下人员尚未激活机器人（从未 /start），机器人无法主动私聊，"
                "请人工转发下面这条激活提示语：\n"
                f"{links}\n\n激活提示语（可直接复制粘贴）：\n{activation_template[2]}"
            )
        if issues:
            lines.append("\n未处理：\n" + "\n".join(dict.fromkeys(issues)))
        if not sent_this_round and not activation_this_round and not issues:
            lines.append("这批花名都已经在本轮之前处理过了。")
        lines.append("\n可以继续发送下一张截图或花名文字；发送 /cancel 结束本轮抽查。")
        await message.answer("\n".join(lines)[:3900])

    @router.message(
        AttendanceCheck.waiting_for_screenshot,
        F.photo | (F.document & F.document.mime_type.startswith("image/")),
    )
    async def attendance_check_screenshot(message: Message, state: FSMContext, bot: Bot) -> None:
        if not message.from_user or not _is_admin(message.from_user.id, services.settings):
            await state.clear()
            return
        if message.document and message.document.file_size and message.document.file_size > 10 * 1024 * 1024:
            await message.answer("图片文件过大，请压缩到 10 MB 以内后重试。")
            return
        await message.answer("正在识别花名并核对花名册，请稍候……")
        try:
            image, mime_type = await _download_message_image(message, bot)
            names = await services.vision.extract_attendance_names(image, mime_type)
        except Exception as exc:
            logger.exception("Attendance screenshot processing failed for %s", message.from_user.id)
            await message.answer(
                "截图识别失败，请重新发送这张截图；也可以直接发送花名文字兜底"
                "（例如“张三 李四”或“张三，李四”）。考勤抽查仍在等待中。\n"
                f"原因：{_vision_error_reason(exc)}"
            )
            return
        if not names:
            await message.answer(
                "没有识别到人员花名，请发送更清晰、完整的截图；"
                "如果截图始终识别不出来，也可以直接发送花名文字（例如“张三 李四”或“张三，李四”）。"
            )
            return
        await _process_attendance_names(message, state, bot, names)

    @router.message(AttendanceCheck.waiting_for_screenshot, F.text)
    async def attendance_check_text_names(message: Message, state: FSMContext, bot: Bot) -> None:
        if not message.from_user or not _is_admin(message.from_user.id, services.settings):
            await state.clear()
            return
        names = _parse_attendance_name_list(message.text or "")
        if not names:
            await message.answer(
                "没有识别到花名，请用空格或逗号分隔多个花名，例如“张三 李四”或“张三，李四”；"
                "也可以直接发送考勤截图。"
            )
            return
        await message.answer(f"正在核对花名册中的 {len(names)} 个花名，请稍候……")
        await _process_attendance_names(message, state, bot, names)

    @router.message(F.photo | (F.document & F.document.mime_type.startswith("image/")))
    async def profile_screenshot(message: Message, bot: Bot) -> None:
        if not message.from_user or message.chat.type != ChatType.PRIVATE:
            return
        if not await services.db.is_active_employee(message.from_user.id):
            await message.answer("请先发送 /start 完成登记。")
            return
        if not services.settings.openai_api_key:
            await message.answer("截图识别暂不可用，请改为发送文字版新人入职表单。")
            return
        if message.document and message.document.file_size and message.document.file_size > 10 * 1024 * 1024:
            await message.answer("图片文件过大，请压缩到 10 MB 以内后重试。")
            return
        await message.answer("正在识别员工信息截图，请稍候……")
        file_id = message.photo[-1].file_id if message.photo else message.document.file_id
        mime_type = message.document.mime_type if message.document else "image/jpeg"
        buffer = io.BytesIO()
        try:
            await bot.download(file_id, destination=buffer)
            values, image_keyword = await services.vision.extract_employee_profile(
                buffer.getvalue(), mime_type or "image/jpeg"
            )
        except Exception:
            logger.exception("Employee screenshot OCR failed for %s", message.from_user.id)
            await message.answer("截图识别失败，请发送更清晰的完整截图，或改为发送文字版表单。")
            return
        caption_keyword = has_onboarding_keyword(message.caption or "")
        if not image_keyword and not caption_keyword:
            await message.answer(
                "这张图片中没有识别到“新人入职”、“入职信息确认”或“中文花名”，因此没有写入花名册。"
                "如需录入，请发送包含其中一个关键词的完整截图。"
            )
            return
        if not values:
            await message.answer("没有从截图中识别到员工字段，请发送包含字段名称和填写内容的完整截图。")
            return
        org_hint = values.pop("org_hint", "")
        extracted_extras = {
            key: values.pop(key, "")
            for key in (
                "trial_salary", "confirmed_salary", "direct_supervisor",
                "indirect_supervisor",
                "org_unit", "job_sequence", "service_entity", "department",
                "team", "position_type", "position_title", "job_level",
                "job_grade", "mgmt_sequence", "work_mode",
                "recruitment_channel", "resume_source", "salary_currency",
                "candidate_code",
            )
        }
        values = clean_screenshot_values(values)
        if not values.get("employee_code") and not values.get("resume_name"):
            await message.answer("其他缺失字段可以留空，但必须能识别员工编码或候选人姓名/姓名/简历名，用于防止写错员工。请补充其中之一。")
            return
        extra_fields = {key: value for key, value in extracted_extras.items() if value}
        if extra_fields.get("trial_salary") or extra_fields.get("confirmed_salary"):
            extra_fields.setdefault("salary_currency", "CNY")
        reference_hint = org_hint or " ".join(
            str(extra_fields.get(key) or "") for key in ("org_unit", "department", "team")
        ).strip()
        if reference_hint and services.sheets.configured:
            try:
                reference_fields = await services.sheets.find_reference_org_fields(reference_hint)
                for key, value in reference_fields.items():
                    extra_fields.setdefault(key, value)
            except Exception:
                logger.exception("Reference org lookup failed for %s", message.from_user.id)
        # 管理员发送的截图始终是"入职信息确认"场景，按新人入职套用花名册/变更记录填写规范的固定默认值。
        _apply_new_hire_defaults(values, extra_fields, org_hint)
        raw = f"[截图识别：{image_keyword or '图片说明触发'}]\n" + "\n".join(
            f"{DISPLAY_LABELS.get(key, key)}：{value}" for key, value in values.items()
        )
        # 管理员发送的"入职信息确认"截图始终按新人处理：先按候选人姓名（即姓名/简历名）匹配花名册
        # 里已有的条目（例如 HR 提前建好但还没分配员工编码的占位行），找到就继续在同一行填写，
        # 而不是新建重复行。
        await _save_profile_values(
            message, services, values, raw, allow_incomplete=True,
            extra_fields=extra_fields, prefer_name_match=True, is_new_hire_event=True,
        )

    @router.message(F.text)
    async def redirect_to_human(message: Message, bot: Bot) -> None:
        if not message.from_user or message.chat.type != ChatType.PRIVATE:
            return
        if not await services.db.is_active_employee(message.from_user.id):
            await message.answer("请先发送 /start 完成登记。")
            return
        mode, values = parse_profile_message(message.text)
        if mode:
            extra_fields = parse_extra_profile_fields(message.text)
            org_hint = ""
            if services.settings.openai_api_key:
                try:
                    gpt_values = await services.gpt.extract_profile_from_text(message.text)
                except Exception:
                    logger.exception("GPT profile text extraction failed for %s", message.from_user.id)
                    gpt_values = {}
                org_hint = gpt_values.pop("org_hint", "")
                extracted_extras = {
                    key: gpt_values.pop(key, "")
                    for key in (
                        "trial_salary", "confirmed_salary", "direct_supervisor",
                        "indirect_supervisor",
                        "org_unit", "job_sequence", "service_entity", "department",
                        "team", "position_type", "position_title", "job_level",
                        "job_grade", "mgmt_sequence", "work_mode",
                        "recruitment_channel", "resume_source", "salary_currency",
                        "candidate_code",
                    )
                }
                gpt_values = clean_screenshot_values(gpt_values)
                if gpt_values.get("work_tg") and not gpt_values["work_tg"].startswith("@"):
                    gpt_values.pop("work_tg", None)
                for key, value in gpt_values.items():
                    values.setdefault(key, value)
                for key, value in extracted_extras.items():
                    if value:
                        extra_fields.setdefault(key, value)
                if extra_fields.get("trial_salary") or extra_fields.get("confirmed_salary"):
                    extra_fields.setdefault("salary_currency", "CNY")
                reference_hint = org_hint or " ".join(
                    str(extra_fields.get(key) or "") for key in ("org_unit", "department", "team")
                ).strip()
                if reference_hint and services.sheets.configured:
                    try:
                        reference_fields = await services.sheets.find_reference_org_fields(reference_hint)
                        for key, value in reference_fields.items():
                            extra_fields.setdefault(key, value)
                    except Exception:
                        logger.exception("Reference org lookup failed for %s", message.from_user.id)
            is_new_hire_event = mode == "new" or _is_new_hire_trigger(message.text)
            if is_new_hire_event:
                _apply_new_hire_defaults(values, extra_fields, org_hint)
            if values:
                prefer_name_match = is_new_hire_event
                await _save_profile_values(
                    message, services, values, message.text, require_existing=mode == "update",
                    allow_incomplete=is_new_hire_event,
                    extra_fields=extra_fields, prefer_name_match=prefer_name_match,
                    is_new_hire_event=is_new_hire_event,
                )
                return
        if services.settings.wallet_workflow_enabled and _is_wallet_completion(message.text):
            profile = await services.db.profile(message.from_user.id)
            if not profile:
                await message.answer("暂时无法核对钱包表单，因为还没有找到你的员工档案。请先提交新人入职信息或联系人工客服。")
                return
            if not services.sheets.configured:
                await message.answer("已收到完成通知，但云表授权尚未配置。我已通知管理员人工核对。")
                for admin_id in services.settings.admin_user_ids:
                    await bot.send_message(admin_id, f"⚠️ {profile.get('chinese_name')} 提交钱包完成通知，但 Google 服务账号尚未配置。")
                return
            try:
                result = await services.sheets.find_wallet_submission(profile)
            except Exception as exc:
                logger.exception("Wallet sheet lookup failed")
                await message.answer("钱包表单核对暂时失败，已通知管理员处理。")
                for admin_id in services.settings.admin_user_ids:
                    await bot.send_message(admin_id, f"⚠️ 钱包表单核对失败：{str(exc)[:200]}")
                return
            if not result:
                await message.answer("暂未在钱包表单中找到与你的员工编码、花名或简历名一致的记录，请检查表单后联系人工客服。")
                for admin_id in services.settings.admin_user_ids:
                    await bot.send_message(admin_id, f"⚠️ 未找到钱包表单：{profile.get('chinese_name')} / {profile.get('resume_name')} / {profile.get('employee_code')}")
                return
            packet = _wallet_packet(profile, result)
            delivered = False
            if services.settings.wallet_assistant_chat_id:
                try:
                    await bot.send_message(services.settings.wallet_assistant_chat_id, packet)
                    delivered = True
                except Exception:
                    logger.exception("Failed to send wallet packet to assistant")
            for admin_id in services.settings.admin_user_ids:
                await bot.send_message(
                    admin_id,
                    packet + ("\n\n✅ 已转交钱包地址助手。" if delivered else "\n\n⏳ 钱包地址助手尚未配置，等待管理员处理。"),
                )
            await message.answer("✅ 已在钱包表单中找到你的提交记录，管理员已收到通知。")
            return
        await message.answer(
            '员工咨询请联系人工客服：<a href="https://t.me/ffuuyao">@ffuuyao</a>\n\n'
            "点击用户名即可打开私聊。",
            parse_mode="HTML",
        )

    router.message.outer_middleware(InboundAuditMiddleware(services))
    dp.include_router(router)
    return dp


EMPLOYEE_COMMANDS = [
    BotCommand(command="start", description="开始或重新登记"),
    BotCommand(command="human", description="联系人工客服"),
]

ADMIN_COMMANDS = [
    BotCommand(command="start", description="开始或重新登记"),
    BotCommand(command="help", description="使用帮助"),
    BotCommand(command="human", description="联系人工客服"),
    BotCommand(command="whoami", description="查看 Telegram user ID"),
    BotCommand(command="profile", description="查看我的员工档案"),
    BotCommand(command="profile_delete", description="删除我的员工档案"),
    BotCommand(command="cancel", description="取消当前操作"),
    BotCommand(command="profiles", description="管理员查看员工档案"),
    BotCommand(command="broadcast", description="管理员群发通知"),
    BotCommand(command="attendance_check", description="管理员截图发起考勤抽查"),
    BotCommand(command="templates", description="管理员使用通知模板"),
    BotCommand(command="template_add", description="管理员创建或更新模板"),
    BotCommand(command="stats", description="管理员查看统计"),
    BotCommand(command="inbox", description="管理员查看员工消息"),
    BotCommand(command="sync", description="管理员同步花名册(只读)"),
    BotCommand(command="roster", description="管理员查看本地花名册"),
    BotCommand(command="send_channel_msg", description="检查频道消息待处理名单"),
    BotCommand(command="notify_supervisor", description="把今日入职新人发给其直接上级"),
    BotCommand(command="onboarding_notify", description="今日入职通知转发到群"),
    BotCommand(command="probation_notify", description="今日转正提醒/转正通知转发到群"),
]


async def set_commands(bot: Bot, admin_user_ids: "frozenset[int] | None" = None) -> None:
    # 默认菜单（所有人）：只包含普通员工命令
    await bot.set_my_commands(EMPLOYEE_COMMANDS, scope=BotCommandScopeDefault())

    # 管理员各自的私聊菜单：包含全部命令
    for admin_id in admin_user_ids or []:
        try:
            await bot.set_my_commands(
                ADMIN_COMMANDS, scope=BotCommandScopeChat(chat_id=admin_id)
            )
        except Exception:
            logger.exception("Failed to set admin command menu for %s", admin_id)


async def attendance_reminder_loop(bot: Bot, services: Services) -> None:
    """At/after 20:00 China time, notify admins once for today's unanswered checks."""
    while True:
        try:
            now_local = datetime.now(ATTENDANCE_TIMEZONE)
            absences = await services.db.attendance_absences_due(now_local)
            if absences:
                absence_text = "\n".join(
                    f"{item['chinese_name']}-工作时段失联满30分钟，按旷工0.5天计"
                    for item in absences
                )
                absence_delivered = False
                for admin_id in services.settings.admin_user_ids:
                    try:
                        await bot.send_message(admin_id, absence_text)
                        absence_delivered = True
                    except Exception:
                        logger.exception(
                            "Failed to notify admin %s about attendance absence", admin_id
                        )
                if absence_delivered:
                    await services.db.mark_attendance_absence_notified(
                        [int(item["id"]) for item in absences]
                    )
            cutoff_local = now_local.replace(hour=20, minute=0, second=0, microsecond=0)
            if now_local >= cutoff_local:
                unanswered = await services.db.unanswered_attendance(
                    now_local.date().isoformat(), cutoff_local
                )
                if unanswered:
                    lines = [
                        f"{item['chinese_name']}-至今未回复考勤抽查"
                        for item in unanswered
                    ]
                    text = "\n".join(lines)
                    delivered = False
                    for admin_id in services.settings.admin_user_ids:
                        try:
                            await bot.send_message(admin_id, text)
                            delivered = True
                        except Exception:
                            logger.exception(
                                "Failed to notify admin %s about unanswered attendance", admin_id
                            )
                    if delivered:
                        await services.db.mark_attendance_notified(
                            [int(item["id"]) for item in unanswered]
                        )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Attendance reminder loop failed")
        await asyncio.sleep(30)
