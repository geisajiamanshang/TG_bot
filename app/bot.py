from __future__ import annotations

import asyncio
import hashlib
import io
import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
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
from app.google_sheets_service import GoogleSheetsService
from app.gpt_service import GPTService
from app.vision_service import VisionService
from app import hr_defaults

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
                is_image = content_type == "photo" or (
                    content_type == "document"
                    and bool(event.document and (event.document.mime_type or "").startswith("image/"))
                )
                is_cancel = bool(event.text and event.text.strip().casefold().startswith("/cancel"))
                if (
                    current_state == AttendanceCheck.waiting_for_screenshot.state
                    and not is_image and not is_cancel
                ):
                    await data["bot"].send_message(
                        event.chat.id,
                        "考勤抽查模式仍在运行，请继续发送截图；只有 /cancel 才会退出。",
                    )
                    return None
            if not _is_admin(user.id, self.services.settings):
                bot: Bot = data["bot"]
                now = datetime.now(UTC)
                is_screenshot = content_type == "photo" or (
                    content_type == "document"
                    and bool(event.document and (event.document.mime_type or "").startswith("image/"))
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
    for key, default_value in hr_defaults.NEW_HIRE_STATIC_DEFAULTS.items():
        extra_fields.setdefault(key, default_value)
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
                await message.answer("云表重新核对失败，已保留记录供管理员处理。")
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
            sync_note = "本地已保存，但云表同步失败，管理员可检查后重试。"
    changed_labels = "、".join(DISPLAY_LABELS.get(key, key) for key in changes)
    await message.answer(
        f"✅ {'新人档案已建立' if is_new else '字段已更新'}\n"
        f"变更字段：{changed_labels}\n{sync_note}\n\n{profile_text(saved)}"
    )
    return True


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
        await message.answer("操作已取消。")

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
        await message.answer("正在只读检查花名册 AD、AQ 列和机器人个人发送记录……")
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
                activation_needed[key] = {**item, "reason": "AD列工作TG为空，未激活"}
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
                f"✅ 检查完成：AQ列入职日期从{month_start.isoformat()}起的员工，"
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
            f"范围：花名册 AQ 入职日期 ≥ {month_start.isoformat()}（当月第一天）",
            f"未激活：{len(activation_needed)} 人",
            f"已激活但未发频道消息：{len(channel_needed)} 人",
        ]
        if activation_needed:
            lines.append("\n【未激活机器人｜请管理员人工发送】")
            for index, item in enumerate(activation_needed.values(), 1):
                username = str(item["work_tg"] or "").lstrip("@")
                contact = f"https://t.me/{username}" if username else "AD列无工作TG，需人工补充"
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
                    f"{target_work_tg.get(str(item.telegram_user_id), 'AD列无工作TG')}"
                    for item in successful
                ))
            if failed:
                lines.append("\n❌ 失败名单：\n" + "\n".join(
                    f"{target_names.get(str(item.telegram_user_id), item.full_name)} · "
                    f"{target_work_tg.get(str(item.telegram_user_id), 'AD列无工作TG')}"
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
            attendance_targets=[],
            activation_targets=[],
            attendance_issues=[],
            attendance_names=[],
        )
        await message.answer(
            "请发送包含员工花名的考勤截图。机器人会核对花名册 C 列花名和 AD 列工作 TG。\n\n"
            "识别后只生成发送预览；在您确认前不会发送任何消息。发送 /cancel 可取消。"
        )

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
            if not names:
                await message.answer("没有识别到人员花名，请发送更清晰、完整的截图。")
                return
            sheet_results = await services.sheets.attendance_contacts(names)
        except Exception:
            logger.exception("Attendance screenshot processing failed for %s", message.from_user.id)
            await message.answer("截图识别或花名册核对失败；当前未发送任何消息。")
            return

        employees = await services.db.active_employees()
        employee_by_username = {
            employee.username.strip().lstrip("@").casefold(): employee
            for employee in employees if employee.username and employee.username.strip().lstrip("@")
        }
        state_data = await state.get_data()
        attendance_by_id = {
            int(item["telegram_user_id"]): item
            for item in list(state_data.get("attendance_targets") or [])
        }
        activation_by_tg = {
            str(item["work_tg"]).lstrip("@").casefold(): item
            for item in list(state_data.get("activation_targets") or [])
        }
        issues = list(state_data.get("attendance_issues") or [])
        recognized_names = list(state_data.get("attendance_names") or [])
        recognized_keys = {"".join(str(item).split()).casefold() for item in recognized_names}
        for name in names:
            normalized_name = "".join(name.split()).casefold()
            if normalized_name not in recognized_keys:
                recognized_keys.add(normalized_name)
                recognized_names.append(name)
        for result in sheet_results:
            name = str(result.get("chinese_name") or result.get("requested_name") or "")
            status = str(result.get("status") or "")
            if status == "not_found":
                issues.append(f"{name}：花名册 C 列未找到")
                continue
            if status == "missing_tg":
                issues.append(f"{name}：AD 列工作 TG 为空")
                continue
            if status == "ambiguous":
                issues.append(f"{name}：同名行的 AD 列不一致，需人工核对")
                continue
            work_tg = str(result.get("work_tg") or "")
            employee = employee_by_username.get(work_tg.lstrip("@").casefold())
            if employee:
                attendance_by_id[employee.telegram_user_id] = {
                    "telegram_user_id": employee.telegram_user_id,
                    "chinese_name": name,
                    "work_tg": work_tg,
                }
                activation_by_tg.pop(work_tg.lstrip("@").casefold(), None)
            else:
                activation_by_tg[work_tg.lstrip("@").casefold()] = {
                    "chinese_name": name, "work_tg": work_tg
                }

        attendance_targets = list(attendance_by_id.values())
        activation_targets = list(activation_by_tg.values())
        issues = list(dict.fromkeys(issues))
        attendance_template = await services.db.template(int(state_data.get("attendance_template_id") or 0))
        activation_template = await services.db.template(int(state_data.get("activation_template_id") or 0))
        if not attendance_template or not activation_template:
            await state.clear()
            await message.answer("考勤相关模板已失效；当前未发送任何消息。")
            return
        await state.update_data(
            attendance_targets=attendance_targets,
            activation_targets=activation_targets,
            attendance_issues=issues,
            attendance_names=recognized_names,
        )
        lines = [
            "【考勤抽查发送预览】",
            f"本张识别：{len(names)} 人｜本批累计：{len(recognized_names)} 人",
            f"已登记，可发送考勤模板：{len(attendance_targets)} 人",
            f"未登记，需发送激活模板：{len(activation_targets)} 人",
            f"异常：{len(issues)} 人",
        ]
        if attendance_targets:
            lines.append("\n考勤抽查名单：\n" + "\n".join(
                f"{index}. {item['chinese_name']} · {item['work_tg']}"
                for index, item in enumerate(attendance_targets, 1)
            ))
            lines.append(f"\n考勤模板：\n{attendance_template[2]}")
        if activation_targets:
            lines.append("\n未登记名单：\n" + "\n".join(
                f"{index}. {item['chinese_name']} · {item['work_tg']}"
                for index, item in enumerate(activation_targets, 1)
            ))
            lines.append(
                f"\n激活模板：\n{activation_template[2]}\n"
                "提示：Telegram 不允许机器人主动私聊从未 /start 的用户；确认后会生成可点击的人工转发清单。"
            )
        if issues:
            lines.append("\n未进入发送范围：\n" + "\n".join(issues))
        if not attendance_targets and not activation_targets:
            await state.set_state(AttendanceCheck.waiting_for_screenshot)
            await message.answer("\n".join(lines)[:3900] + "\n\n没有可处理对象，请核对后重新发送截图。")
            return
        # Keep the attendance session open so the next image is handled here instead
        # of falling through to the onboarding screenshot handler. /cancel is the
        # only operation that exits the session.
        await state.set_state(AttendanceCheck.waiting_for_screenshot)
        await message.answer(
            "\n".join(lines)[:3700] + "\n\n可以继续发送下一张图片；名单会自动累计去重。",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="确认执行", callback_data="attendance:confirm"),
                InlineKeyboardButton(text="取消", callback_data="attendance:cancel"),
            ]]),
        )

    @router.callback_query(F.data == "attendance:cancel")
    async def attendance_check_cancel(callback: CallbackQuery, state: FSMContext) -> None:
        if not callback.from_user or not _is_admin(callback.from_user.id, services.settings):
            await callback.answer("无权操作", show_alert=True)
            return
        await state.clear()
        await callback.answer("已取消")
        if callback.message:
            await callback.message.edit_reply_markup(reply_markup=None)

    @router.callback_query(AttendanceCheck.waiting_for_screenshot, F.data == "attendance:confirm")
    async def attendance_check_confirm(callback: CallbackQuery, state: FSMContext, bot: Bot) -> None:
        if not callback.from_user or not _is_admin(callback.from_user.id, services.settings):
            await callback.answer("无权操作", show_alert=True)
            return
        data = await state.get_data()
        targets = list(data.get("attendance_targets") or [])
        activation_targets = list(data.get("activation_targets") or [])
        attendance_template = await services.db.template(int(data.get("attendance_template_id") or 0))
        activation_template = await services.db.template(int(data.get("activation_template_id") or 0))
        if not attendance_template or not activation_template:
            await state.clear()
            await callback.answer("模板已经失效", show_alert=True)
            return
        if not targets and not activation_targets:
            await callback.answer("当前批次已经处理或名单为空，请继续发送图片。", show_alert=True)
            return
        # Clear only the completed batch. Keep template ids and the attendance
        # state so the administrator can immediately send more screenshots.
        await state.set_state(AttendanceCheck.waiting_for_screenshot)
        await state.update_data(
            attendance_targets=[],
            activation_targets=[],
            attendance_issues=[],
            attendance_names=[],
        )
        await callback.answer("开始执行")
        if callback.message:
            await callback.message.edit_reply_markup(reply_markup=None)
            await callback.message.answer("已获得您的确认，正在发送考勤抽查……")

        recipients = await services.db.active_employees_by_ids(
            [int(item["telegram_user_id"]) for item in targets]
        )
        target_by_id = {int(item["telegram_user_id"]): item for item in targets}
        successful: list[Employee] = []
        failed: list[Employee] = []
        delay = 1 / services.settings.broadcast_messages_per_second
        for employee in recipients:
            try:
                await bot.send_message(employee.chat_id, attendance_template[2])
                successful.append(employee)
                sent_at = datetime.now(UTC)
                target = target_by_id[employee.telegram_user_id]
                try:
                    await services.db.create_attendance_check(
                        employee.telegram_user_id,
                        str(target["chinese_name"]),
                        str(target["work_tg"]),
                        sent_at.astimezone(ATTENDANCE_TIMEZONE).date().isoformat(),
                        sent_at,
                        sent_at + timedelta(minutes=20),
                    )
                except Exception:
                    logger.exception("Failed to persist attendance check for user %s", employee.telegram_user_id)
            except TelegramRetryAfter as exc:
                await asyncio.sleep(exc.retry_after + 0.5)
                try:
                    await bot.send_message(employee.chat_id, attendance_template[2])
                    successful.append(employee)
                    sent_at = datetime.now(UTC)
                    target = target_by_id[employee.telegram_user_id]
                    try:
                        await services.db.create_attendance_check(
                            employee.telegram_user_id,
                            str(target["chinese_name"]),
                            str(target["work_tg"]),
                            sent_at.astimezone(ATTENDANCE_TIMEZONE).date().isoformat(),
                            sent_at,
                            sent_at + timedelta(minutes=20),
                        )
                    except Exception:
                        logger.exception("Failed to persist attendance check for user %s", employee.telegram_user_id)
                except Exception:
                    logger.exception("Attendance retry failed for user %s", employee.telegram_user_id)
                    failed.append(employee)
            except (TelegramForbiddenError, TelegramBadRequest):
                await services.db.deactivate_employee(employee.telegram_user_id)
                failed.append(employee)
            except Exception:
                logger.exception("Attendance send failed for user %s", employee.telegram_user_id)
                failed.append(employee)
            await asyncio.sleep(delay)

        broadcast_id = await services.db.save_broadcast(
            callback.from_user.id, f"[考勤抽查]\n{attendance_template[2]}", len(successful), len(failed)
        )
        if callback.message:
            report = [
                f"考勤抽查执行完成。记录 #{broadcast_id}",
                f"考勤模板发送成功：{len(successful)}",
                f"发送失败：{len(failed)}",
            ]
            if successful:
                report.append("\n✅ 发送成功：\n" + "\n".join(
                    f"{target_by_id[item.telegram_user_id]['chinese_name']} · "
                    f"{target_by_id[item.telegram_user_id]['work_tg']}" for item in successful
                ))
            if failed:
                report.append("\n❌ 发送失败：\n" + "\n".join(
                    f"{target_by_id[item.telegram_user_id]['chinese_name']} · "
                    f"{target_by_id[item.telegram_user_id]['work_tg']}" for item in failed
                ))
            if activation_targets:
                links = "\n".join(
                    f"{item['chinese_name']}：https://t.me/{str(item['work_tg']).lstrip('@')}"
                    for item in activation_targets
                )
                report.append(
                    f"\n⚠️ 以下员工从未激活机器人，机器人无法主动私聊。请人工转发：\n"
                    f"{activation_template[2]}\n\n{links}"
                )
            await callback.message.answer(
                "\n".join(report)[:3700]
                + "\n\n考勤抽查仍在等待下一张图片；发送 /cancel 才会退出。"
            )

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
