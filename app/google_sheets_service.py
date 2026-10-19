from __future__ import annotations

import asyncio
import io
import json
import re
import logging
from collections import Counter
from datetime import date, datetime, timedelta
from pathlib import Path
from threading import Lock

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload

from app.employee_profile import DISPLAY_LABELS, person_name_keys
from app.hr_defaults import normalize_department, default_office_region

logger = logging.getLogger(__name__)

ROSTER_SHEET = "花名册"
CHANGE_SHEET = "变更记录"
ROSTER_START_ROW = 4
CHANGE_START_ROW = 3
ROSTER_COLUMNS = {
    "employee_code": "B", "chinese_name": "C", "resume_name": "E",
    "employment_status": "F", "effective_date": "G", "gender": "H",
    "age_range": "I", "education": "J", "nationality": "K", "birthday_month": "L",
    "office_region": "AC", "work_tg": "AE", "private_contact": "AF", "work_email": "AG",
}

# Organizational fields that are consistent within a team/department, safe to copy from
# an existing roster row that matches a department/team name mentioned in the inbound message.
EXTRA_ORG_COLUMNS = {
    "org_unit": "M", "job_sequence": "N", "service_entity": "O",
    "department": "P", "team": "Q", "hrbp": "S",
    "position_type": "T", "position_title": "U", "job_level": "V",
    "job_grade": "W", "direct_supervisor": "AA", "indirect_supervisor": "AB",
    "work_mode": "AD", "recruitment_channel": "AP", "resume_source": "AQ",
}
# 候选人编码（D列）：只从消息/截图中明确的“候选人编码”标签原样提取，绝不从其他行参照。
EXTRA_IDENTITY_COLUMNS = {
    "candidate_code": "D",
}
# Salary fields extracted directly from the message when explicitly stated (never inferred
# from other rows). Currency is always CNY per company policy.
EXTRA_SALARY_COLUMNS = {
    "salary_currency": "AJ", "trial_salary": "AK", "confirmed_salary": "AL",
}
# Fixed HR business-rule defaults (see app/hr_defaults.py) applied only to brand-new
# roster rows (is_new=True) -- never inferred from other rows, never used on updates.
EXTRA_HR_DEFAULT_COLUMNS = {
    "mgmt_headcount": "Z",
    "work_mode": "AD", "salary_basis": "AM",
    "employment_type": "AW", "establishment_attribute": "AX", "establishment_status": "AY",
    "probation_months": "AZ", "probation_end_date": "BA",
    "tenure_years": "BC",
    "latest_change_reason": "BG",
}

# Fixed HR business-rule defaults (app/hr_defaults.py) that must never be written onto a
# row this call didn't just create or first-fill (see effective_new below). This is every
# EXTRA_HR_DEFAULT_COLUMNS key, plus "job_sequence"/"service_entity" (序列/服务单位) --
# those two are classified under EXTRA_ORG_COLUMNS instead (so find_reference_org_fields
# can still copy them between existing rows), but hr_defaults also fixes them to constant
# values for a brand-new hire, so they need the same never-overwrite-an-existing-row
# protection: without it, a duplicate "新人入职"/"入职信息确认" resend that happens to
# match an already-filled existing employee row (by employee_code) would clobber that
# row's 序列/服务单位 with the blind defaults "派驻"/"恒睿".
NEW_HIRE_ONLY_EXTRA_KEYS = frozenset(EXTRA_HR_DEFAULT_COLUMNS) | {"job_sequence", "service_entity"}

DROPDOWN_ALLOWED = {
    "F": {"在职", "试用期", "调入", "调出", "停薪留职"},
    "H": {"男", "女"},
    "I": {"25 以下", "26-30", "31-35", "36-40", "41-45", "46-50", "51 以上"},
    "J": {"本科", "博士", "大专", "高中及以下", "硕士"},
    "L": {f"{month}月" for month in range(1, 13)},
    "N": {"派驻", "直属"},
    "T": {"财务", "策划", "产品", "法务", "管理", "技术", "内容", "其他", "渠道", "人事", "商务", "设计", "市场", "销售", "行政", "研发", "运营", "质检", "HRBP", "SSC"},
    "V": {*(f"P{i}" for i in range(1, 9)), *(f"M{i}" for i in range(1, 8))},
    "W": {"1", "2", "3"},
    "AD": {"远程", "集中办公室", "混合"},
    "AJ": {"CNY", "USD", "USDT", "HKD", "SGD", "TWD", "GBP", "EUR", "JPY", "KRW", "AED"},
    "AP": {"员工内推", "中诚渠道", "猎头", "二次入职", "卓亚渠道", "万天渠道", "宏景渠道", "燎原社渠道", "星探队", "其他", "寻英渠道", "牧星渠道"},
    "AW": {"全职", "外包", "实习", "劳务派遣"},
    "AX": {"正编", "外包", "顾问"},
    "AY": {"空缺", "冻结", "在编"},
}

# These columns must retain the roster's native dropdown validation. X, Y and BF
# are formula columns: their validation is preserved, but their values/formulas are
# never written by this bot.
DROPDOWN_STRUCTURE_COLUMNS = (
    "I", "J", "L", "N", "T", "V", "W", "X", "Y", "AD", "AJ", "AP", "AW", "AX", "AY", "BB", "BD", "BF",
)

FORMULA_COLUMNS = ("X", "Y", "BC", "BF", "BH", "BK")
# 入职日期（AR）：新人入职时写入生效日期。
HIRE_DATE_COLUMN = "AR"
# 花名册最后一列（转正日期（含延期））。
ROSTER_LAST_COLUMN = "BK"


def parse_sheet_date(value: str) -> date | None:
    """解析花名册里的日期文本：2026-10-16 / 2026/10/16 / 2026.10.16 / 2026年10月16日。"""
    text = str(value or "").strip()
    match = re.match(r"^(\d{4})\s*[-/.年]\s*(\d{1,2})\s*[-/.月]\s*(\d{1,2})", text)
    if not match:
        return None
    try:
        return date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
    except ValueError:
        return None


def onboarding_identity_keys(profile: dict[str, object]) -> set[str]:
    """Stable keys used to keep one 入职 row per person, even before a code exists."""
    keys: set[str] = set()
    for field in ("employee_code", "resume_name", "chinese_name"):
        value = str(profile.get(field) or "").strip()
        if value:
            keys.add(value.casefold())
            keys.update(person_name_keys(value))
    return keys


class GoogleSheetsService:
    def __init__(
        self,
        spreadsheet_id: str,
        credentials_file: str,
        wallet_spreadsheet_id: str = "",
        roster_spreadsheet_id: str = "",
    ) -> None:
        self.spreadsheet_id = spreadsheet_id
        self.credentials_file = credentials_file
        self.wallet_spreadsheet_id = wallet_spreadsheet_id
        self.roster_spreadsheet_id = roster_spreadsheet_id or spreadsheet_id
        # Prevent two Telegram updates from passing the new-hire dedupe check at
        # the same time and both appending an onboarding record.
        self._change_lock = Lock()

    @property
    def configured(self) -> bool:
        return bool(self.spreadsheet_id and Path(self.credentials_file).is_file())

    def service_account_email(self) -> str | None:
        if not self.configured:
            return None
        try:
            return str(json.loads(Path(self.credentials_file).read_text())["client_email"])
        except Exception:
            return None

    def _sheet_id(self, service, title: str) -> int:
        """Resolve a tab's numeric sheetId by its title (gid differs between files)."""
        cache = self.__dict__.setdefault("_sheet_id_cache", {})
        key = (self.spreadsheet_id, title)
        if key not in cache:
            metadata = service.spreadsheets().get(
                spreadsheetId=self.spreadsheet_id, fields="sheets.properties(sheetId,title)"
            ).execute()
            for sheet in metadata.get("sheets", []):
                props = sheet.get("properties", {})
                cache[(self.spreadsheet_id, props.get("title"))] = int(props.get("sheetId", 0))
            if key not in cache:
                raise ValueError(f"Google 表格中找不到工作表：{title}")
        return cache[key]

    def _assert_roster_layout(self, service) -> None:
        """Refuse to write unless the roster uses the V1.4.0 layout (D=候选人编码, E=姓名/简历名).

        Every column letter in this module assumes that layout; writing into an older
        roster (where D is 姓名/简历名) would shift every value one column to the left.
        """
        checked = self.__dict__.setdefault("_layout_checked", set())
        if self.spreadsheet_id in checked:
            return
        header = (self._read_rows(service, ROSTER_SHEET, "A3:E3") or [[]])[0]
        header_d = str(header[3] if len(header) > 3 else "").strip()
        header_e = str(header[4] if len(header) > 4 else "").strip()
        if header_d != "候选人编码" or not header_e.startswith("姓名"):
            raise ValueError(
                "花名册表头与机器人列位置不一致（D3 应为“候选人编码”、E3 应为“姓名/简历名”），"
                "已停止写入。请检查 GOOGLE_SPREADSHEET_ID 是否指向新版花名册。"
            )
        checked.add(self.spreadsheet_id)

    def _service(self):
        credentials = Credentials.from_service_account_file(
            self.credentials_file,
            scopes=["https://www.googleapis.com/auth/spreadsheets"],
        )
        return build("sheets", "v4", credentials=credentials, cache_discovery=False)

    def _drive_service(self):
        """Drive 客户端：/probation_notify 用来按花名在指定文件夹里找转正海报/表单（只读），
        入职协议功能还要用它往"入职协议"文件夹里新建月份子文件夹、上传协议文件（需要写权限），
        所以这里用的是完整的 drive scope，不是 drive.readonly。服务账号需要额外被授予相应 Drive
        文件夹的权限——只读用途（海报/表单）"查看者"就够，入职协议文件夹需要"编辑者"。"""
        credentials = Credentials.from_service_account_file(
            self.credentials_file,
            scopes=["https://www.googleapis.com/auth/drive"],
        )
        return build("drive", "v3", credentials=credentials, cache_discovery=False)

    async def find_wallet_submission(self, profile: dict[str, object]) -> dict[str, str] | None:
        return await asyncio.to_thread(self._find_wallet_submission, profile)

    def _find_wallet_submission(self, profile: dict[str, object]) -> dict[str, str] | None:
        if not self.wallet_spreadsheet_id:
            return None
        service = self._service()
        result = service.spreadsheets().values().get(
            spreadsheetId=self.wallet_spreadsheet_id,
            range="'第 1 张表单回复'!A2:N244",
        ).execute()
        rows = result.get("values", [])
        names = {
            str(profile.get("chinese_name") or "").strip().casefold(),
            str(profile.get("resume_name") or "").strip().casefold(),
        } - {""}
        employee_code = str(profile.get("employee_code") or "").strip().casefold()
        matches: list[tuple[int, list[object]]] = []
        for index, row in enumerate(rows, start=2):
            flower_name = str(row[2] if len(row) > 2 else "").strip().casefold()
            code = str(row[3] if len(row) > 3 else "").strip().casefold()
            if (employee_code and code == employee_code) or flower_name in names:
                matches.append((index, row))
        if not matches:
            return None
        row_number, row = matches[-1]
        def value(index: int) -> str:
            return str(row[index]).strip() if len(row) > index else ""
        return {
            "row": str(row_number), "timestamp": value(0), "submit_date": value(1),
            "flower_name": value(2), "employee_code": value(3), "company": value(4),
            "submit_type": value(5), "current_address": value(6), "current_qr": value(7),
            "old_address": value(8), "old_qr": value(9), "reason": value(10),
            "confirmed": value(11), "email": value(13),
        }

    async def sync_profile(
        self,
        profile: dict[str, object],
        changes: dict[str, tuple[str, str]],
        is_new: bool,
        extra_fields: dict[str, str] | None = None,
        prefer_name_match: bool = False,
        reconcile_core: bool = False,
        onboarding_event: bool = False,
    ) -> int:
        return await asyncio.to_thread(
            self._sync_profile, profile, changes, is_new, extra_fields,
            prefer_name_match, reconcile_core, onboarding_event
        )

    def _read_rows(self, service, sheet: str, range_: str, spreadsheet_id: str | None = None) -> list[list[object]]:
        result = service.spreadsheets().values().get(
            spreadsheetId=spreadsheet_id or self.spreadsheet_id, range=f"'{sheet}'!{range_}"
        ).execute()
        return result.get("values", [])

    async def attendance_contacts(self, chinese_names: list[str]) -> list[dict[str, str]]:
        """Read-only lookup: match 花名册 C 花名 and return AE 工作TG."""
        return await asyncio.to_thread(self._attendance_contacts, chinese_names)

    def _attendance_contacts(self, chinese_names: list[str]) -> list[dict[str, str]]:
        service = self._service()
        # 花名可能只登记在其中一张表：表格一（可编辑，self.spreadsheet_id）用于新人信息
        # 录入，表格二（只读，self.roster_spreadsheet_id，/sync 同步用）是正式花名册。两张
        # 表结构一致（C 花名、AE 工作TG），考勤抽查需要两张表都查，找不到再换另一张。
        rows = list(self._read_rows(service, ROSTER_SHEET, "C4:AE5000", self.spreadsheet_id))
        if self.roster_spreadsheet_id and self.roster_spreadsheet_id != self.spreadsheet_id:
            rows.extend(self._read_rows(service, ROSTER_SHEET, "C4:AE5000", self.roster_spreadsheet_id))

        def key(value: object) -> str:
            return "".join(str(value or "").split()).casefold()

        by_name: dict[str, list[tuple[int, str, str]]] = {}
        for row_number, row in enumerate(rows, start=ROSTER_START_ROW):
            flower_name = str(row[0] if row else "").strip()
            if not flower_name:
                continue
            # Read starts at C, so AE 工作TG is offset AE - C.
            tg_offset = self._column_index(ROSTER_COLUMNS["work_tg"]) - self._column_index("C")
            work_tg = str(row[tg_offset] if len(row) > tg_offset else "").strip()
            by_name.setdefault(key(flower_name), []).append(
                (row_number, flower_name, work_tg)
            )

        results: list[dict[str, str]] = []
        seen: set[str] = set()
        for requested_name in chinese_names:
            normalized = key(requested_name)
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            candidates = by_name.get(normalized, [])
            if not candidates:
                results.append({"requested_name": requested_name, "status": "not_found"})
                continue
            usernames = {
                work_tg.lstrip("@").strip().casefold()
                for _, _, work_tg in candidates if work_tg.lstrip("@").strip()
            }
            if not usernames:
                results.append({
                    "requested_name": requested_name,
                    "chinese_name": candidates[-1][1],
                    "status": "missing_tg",
                })
                continue
            if len(usernames) > 1:
                results.append({
                    "requested_name": requested_name,
                    "chinese_name": candidates[-1][1],
                    "status": "ambiguous",
                })
                continue
            username = next(iter(usernames))
            matching = next(item for item in reversed(candidates) if item[2].lstrip("@").strip().casefold() == username)
            results.append({
                "requested_name": requested_name,
                "chinese_name": matching[1],
                "work_tg": f"@{username}",
                "row": str(matching[0]),
                "status": "matched",
            })
        return results

    async def channel_message_candidates(
        self, since_date: date
    ) -> list[dict[str, str]]:
        """Read-only C/AE/AR lookup for employees hired on or after since_date."""
        return await asyncio.to_thread(self._channel_message_candidates, since_date)

    def _channel_message_candidates(self, since_date: date) -> list[dict[str, str]]:
        service = self._service()
        rows = self._read_rows(service, ROSTER_SHEET, f"C4:{HIRE_DATE_COLUMN}5000")
        tg_offset = self._column_index(ROSTER_COLUMNS["work_tg"]) - self._column_index("C")
        hire_offset = self._column_index(HIRE_DATE_COLUMN) - self._column_index("C")
        results: list[dict[str, str]] = []
        for row_number, row in enumerate(rows, start=ROSTER_START_ROW):
            chinese_name = str(row[0] if row else "").strip()
            work_tg = str(row[tg_offset] if len(row) > tg_offset else "").strip()
            hire_date_text = str(row[hire_offset] if len(row) > hire_offset else "").strip()
            if not chinese_name or not hire_date_text:
                continue
            normalized_date = hire_date_text.replace("/", "-").replace(".", "-")
            try:
                hire_date = date.fromisoformat(normalized_date)
            except ValueError:
                logger.warning(
                    "Skipped unparseable AR hire date at roster row %s: %r",
                    row_number, hire_date_text,
                )
                continue
            if hire_date < since_date:
                continue
            username = work_tg.lstrip("@").strip()
            results.append({
                "row": str(row_number),
                "chinese_name": chinese_name,
                "work_tg": f"@{username}" if username else "",
                "hire_date": hire_date.isoformat(),
            })
        return results

    async def today_hires(self, today: date) -> list[dict[str, str]]:
        """Read-only: 表格一花名册中 G 列（生效日期）等于 today 的员工的 C 花名、E 简历名、
        AE 工作TG、AA 直接上级。"""
        return await asyncio.to_thread(self._today_hires, today)

    async def onboarding_notify_candidates(self, today: date) -> list[dict[str, str]]:
        """Read-only，供 /onboarding_notify 用：花名册 G 列生效日期等于 today 的所有员工，取
        【新人入职通知】【入职信息同步】两个模版需要的全部字段。"""
        return await asyncio.to_thread(self._onboarding_notify_candidates, today)

    def _onboarding_notify_candidates(self, today: date) -> list[dict[str, str]]:
        service = self._service()
        last = HIRE_DATE_COLUMN  # AR，覆盖到最后需要读取的列
        rows = self._read_rows(service, ROSTER_SHEET, f"A{ROSTER_START_ROW}:{last}5000")

        def idx(column: str) -> int:
            return self._column_index(column)

        fields = {
            "employee_code": idx(ROSTER_COLUMNS["employee_code"]),       # B 编号
            "chinese_name": idx(ROSTER_COLUMNS["chinese_name"]),         # C 花名
            "resume_name": idx(ROSTER_COLUMNS["resume_name"]),           # E 简历名
            "effective_date": idx(ROSTER_COLUMNS["effective_date"]),     # G 日期
            "org_unit": idx(EXTRA_ORG_COLUMNS["org_unit"]),              # M 编制组织
            "department": idx(EXTRA_ORG_COLUMNS["department"]),          # P 部门
            "team": idx(EXTRA_ORG_COLUMNS["team"]),                      # Q 小组
            "position_title": idx(EXTRA_ORG_COLUMNS["position_title"]),  # U 岗位
            "direct_supervisor": idx(EXTRA_ORG_COLUMNS["direct_supervisor"]),  # AA 直属上级
            "service_entity": idx(EXTRA_ORG_COLUMNS["service_entity"]),  # O 派驻公司
            "resume_source": idx(EXTRA_ORG_COLUMNS["resume_source"]),    # AQ 简历来源
            "work_tg": idx(ROSTER_COLUMNS["work_tg"]),                   # AE 工作TG
            "private_contact": idx(ROSTER_COLUMNS["private_contact"]),   # AF 私人TG
            "work_email": idx(ROSTER_COLUMNS["work_email"]),             # AG 工作Gmail
            "trial_salary": idx(EXTRA_SALARY_COLUMNS["trial_salary"]),       # AK 试用期薪资
            "confirmed_salary": idx(EXTRA_SALARY_COLUMNS["confirmed_salary"]),  # AL 转正薪资
            "hire_date": idx(HIRE_DATE_COLUMN),                          # AR 入职日期
        }

        def cell(row: list[object], index: int) -> str:
            return str(row[index]).strip() if len(row) > index else ""

        date_index = fields["effective_date"]
        name_index = fields["chinese_name"]
        results: list[dict[str, str]] = []
        for row_number, row in enumerate(rows, start=ROSTER_START_ROW):
            if parse_sheet_date(cell(row, date_index)) != today:
                continue
            name = cell(row, name_index)
            if not name:
                continue
            record = {"row": str(row_number)}
            for key, index in fields.items():
                record[key] = cell(row, index)
            results.append(record)
        return results

    async def probation_notify_candidates(
        self, today: date, advance_days: int = 4
    ) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
        """Read-only，供 /probation_notify 用：花名册 BK 列（转正日期，含延期）。
        返回 (advance, due) 两个列表：advance 是 BK == today + advance_days 的员工
        （提前 N 天预转正提醒），due 是 BK == today 的员工（转正当天）。"""
        return await asyncio.to_thread(self._probation_notify_candidates, today, advance_days)

    def _probation_notify_candidates(
        self, today: date, advance_days: int
    ) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
        service = self._service()
        last = ROSTER_LAST_COLUMN  # BK，覆盖到最后需要读取的列
        rows = self._read_rows(service, ROSTER_SHEET, f"A{ROSTER_START_ROW}:{last}5000")

        def idx(column: str) -> int:
            return self._column_index(column)

        fields = {
            "employee_code": idx(ROSTER_COLUMNS["employee_code"]),       # B 编号
            "chinese_name": idx(ROSTER_COLUMNS["chinese_name"]),         # C 花名
            "resume_name": idx(ROSTER_COLUMNS["resume_name"]),           # E 简历名
            "org_unit": idx(EXTRA_ORG_COLUMNS["org_unit"]),              # M 编制组织
            "department": idx(EXTRA_ORG_COLUMNS["department"]),          # P 部门
            "team": idx(EXTRA_ORG_COLUMNS["team"]),                      # Q 小组
            "position_title": idx(EXTRA_ORG_COLUMNS["position_title"]),  # U 岗位
            "job_level": idx(EXTRA_ORG_COLUMNS["job_level"]),            # V 职级
            "direct_supervisor": idx(EXTRA_ORG_COLUMNS["direct_supervisor"]),  # AA 直属上级
            "service_entity": idx(EXTRA_ORG_COLUMNS["service_entity"]),  # O 派驻公司
            "work_tg": idx(ROSTER_COLUMNS["work_tg"]),                   # AE 工作TG
            "hire_date": idx(HIRE_DATE_COLUMN),                          # AR 入职日期
            "probation_date": idx(ROSTER_LAST_COLUMN),                   # BK 转正日期（含延期）
        }

        def cell(row: list[object], index: int) -> str:
            return str(row[index]).strip() if len(row) > index else ""

        name_index = fields["chinese_name"]
        date_index = fields["probation_date"]
        advance_target = today + timedelta(days=advance_days)

        advance: list[dict[str, str]] = []
        due: list[dict[str, str]] = []
        for row_number, row in enumerate(rows, start=ROSTER_START_ROW):
            name = cell(row, name_index)
            if not name:
                continue
            probation_date = parse_sheet_date(cell(row, date_index))
            if probation_date is None:
                continue
            record = {"row": str(row_number)}
            for key, index in fields.items():
                record[key] = cell(row, index)
            if probation_date == advance_target:
                advance.append(record)
            elif probation_date == today:
                due.append(record)
        return advance, due

    async def resolve_work_tg_by_name(self, name: str) -> str | None:
        """按花名/简历名在整张花名册里找这个人自己的工作TG（AE列）。
        用于"直接上级"只是个名字时，查出这位上级自己的工作TG 以便私信转发。"""
        return await asyncio.to_thread(self._resolve_work_tg_by_name, name)

    def _resolve_work_tg_by_name(self, name: str) -> str | None:
        name = str(name or "").strip()
        if not name:
            return None
        service = self._service()
        last = ROSTER_COLUMNS["work_tg"]
        rows = self._read_rows(service, ROSTER_SHEET, f"A{ROSTER_START_ROW}:{last}5000")
        name_index = self._column_index(ROSTER_COLUMNS["chinese_name"])
        resume_index = self._column_index(ROSTER_COLUMNS["resume_name"])
        tg_index = self._column_index(ROSTER_COLUMNS["work_tg"])

        def cell(row: list[object], index: int) -> str:
            return str(row[index]).strip() if len(row) > index else ""

        target = name.casefold()
        for row in rows:
            chinese_name = cell(row, name_index)
            resume_name = cell(row, resume_index)
            if target in (chinese_name.casefold(), resume_name.casefold()):
                username = cell(row, tg_index).lstrip("@").strip()
                return f"@{username}" if username else None
        return None

    async def find_probation_poster(self, folder_id: str, chinese_name: str) -> tuple[bytes, str] | None:
        """在指定 Drive 文件夹里按花名找转正海报文件，返回 (文件内容, mime_type)。
        服务账号必须已被共享该文件夹的查看权限，否则这里会抛出 HttpError。"""
        return await asyncio.to_thread(self._find_probation_poster, folder_id, chinese_name)

    def _find_probation_poster(self, folder_id: str, chinese_name: str) -> tuple[bytes, str] | None:
        chinese_name = str(chinese_name or "").strip()
        if not chinese_name or not folder_id:
            return None
        drive = self._drive_service()
        safe_name = chinese_name.replace("'", "\\'")
        query = f"'{folder_id}' in parents and trashed = false and name contains '{safe_name}'"
        response = drive.files().list(
            q=query, fields="files(id, name, mimeType)", pageSize=20,
            supportsAllDrives=True, includeItemsFromAllDrives=True,
        ).execute()
        files = response.get("files", [])
        if not files:
            return None
        # 按花名精确匹配文件名（去掉扩展名）优先，否则取第一个包含花名的文件。
        exact = [f for f in files if Path(f["name"]).stem.strip() == chinese_name]
        chosen = exact[0] if exact else files[0]
        content = drive.files().get_media(fileId=chosen["id"], supportsAllDrives=True).execute()
        return content, chosen.get("mimeType", "application/octet-stream")

    async def find_probation_form_link(
        self, root_folder_id: str, year: int, month: int, chinese_name: str
    ) -> str | None:
        """转正表单链接：在"<root_folder_id>/<年>-<月>月转正明细"（文件夹名格式不完全统一，
        按"包含年份数字 + 月份数字+月"模糊匹配）里按花名找该员工的表单文件，返回可分享链接。"""
        return await asyncio.to_thread(
            self._find_probation_form_link, root_folder_id, year, month, chinese_name
        )

    def _find_probation_form_link(
        self, root_folder_id: str, year: int, month: int, chinese_name: str
    ) -> str | None:
        chinese_name = str(chinese_name or "").strip()
        if not chinese_name or not root_folder_id:
            return None
        drive = self._drive_service()
        folder_query = (
            f"'{root_folder_id}' in parents and trashed = false "
            "and mimeType = 'application/vnd.google-apps.folder'"
        )
        folder_response = drive.files().list(
            q=folder_query, fields="files(id, name)", pageSize=50,
            supportsAllDrives=True, includeItemsFromAllDrives=True,
        ).execute()
        month_folders = [
            f for f in folder_response.get("files", [])
            if str(year) in f["name"] and f"{month}月" in f["name"]
        ]
        if not month_folders:
            return None
        subfolder_id = month_folders[0]["id"]
        safe_name = chinese_name.replace("'", "\\'")
        file_query = f"'{subfolder_id}' in parents and trashed = false and name contains '{safe_name}'"
        file_response = drive.files().list(
            q=file_query, fields="files(id, name, webViewLink)", pageSize=20,
            supportsAllDrives=True, includeItemsFromAllDrives=True,
        ).execute()
        files = file_response.get("files", [])
        if not files:
            return None
        exact = [f for f in files if Path(f["name"]).stem.strip() == chinese_name]
        chosen = exact[0] if exact else files[0]
        return chosen.get("webViewLink") or f"https://drive.google.com/file/d/{chosen['id']}/view"

    async def upload_onboarding_agreement(
        self, root_folder_id: str, month_folder: str, filename: str, content: bytes
    ) -> str:
        """把填好的入职协议上传到 "<root_folder_id>/<month_folder>" 下（月份子文件夹不存在
        就自动新建），文件设成"知道链接的人可查看"，返回可分享链接。服务账号需要已经被共享
        该根文件夹的【编辑者】权限，否则这里会抛出 HttpError。"""
        return await asyncio.to_thread(
            self._upload_onboarding_agreement, root_folder_id, month_folder, filename, content
        )

    def _upload_onboarding_agreement(
        self, root_folder_id: str, month_folder: str, filename: str, content: bytes
    ) -> str:
        drive = self._drive_service()
        safe_folder = month_folder.replace("'", "\\'")
        folder_query = (
            f"'{root_folder_id}' in parents and trashed = false "
            f"and mimeType = 'application/vnd.google-apps.folder' and name = '{safe_folder}'"
        )
        folder_response = drive.files().list(
            q=folder_query, fields="files(id, name)", pageSize=10,
            supportsAllDrives=True, includeItemsFromAllDrives=True,
        ).execute()
        existing_folders = folder_response.get("files", [])
        if existing_folders:
            subfolder_id = existing_folders[0]["id"]
        else:
            created_folder = drive.files().create(
                body={
                    "name": month_folder,
                    "mimeType": "application/vnd.google-apps.folder",
                    "parents": [root_folder_id],
                },
                fields="id",
                supportsAllDrives=True,
            ).execute()
            subfolder_id = created_folder["id"]

        media = MediaIoBaseUpload(
            io.BytesIO(content),
            mimetype="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            resumable=False,
        )
        uploaded = drive.files().create(
            body={"name": filename, "parents": [subfolder_id]},
            media_body=media,
            fields="id, webViewLink",
            supportsAllDrives=True,
        ).execute()
        file_id = uploaded["id"]
        try:
            drive.permissions().create(
                fileId=file_id,
                body={"role": "reader", "type": "anyone"},
                supportsAllDrives=True,
            ).execute()
        except Exception:
            logger.exception("Failed to set anyone-with-link permission for uploaded agreement %s", file_id)
        return uploaded.get("webViewLink") or f"https://drive.google.com/file/d/{file_id}/view"

    def _today_hires(self, today: date) -> list[dict[str, str]]:
        service = self._service()
        last = ROSTER_COLUMNS["work_tg"]
        rows = self._read_rows(service, ROSTER_SHEET, f"A{ROSTER_START_ROW}:{last}5000")
        name_index = self._column_index(ROSTER_COLUMNS["chinese_name"])
        resume_name_index = self._column_index(ROSTER_COLUMNS["resume_name"])
        date_index = self._column_index(ROSTER_COLUMNS["effective_date"])
        tg_index = self._column_index(ROSTER_COLUMNS["work_tg"])
        supervisor_index = self._column_index(EXTRA_ORG_COLUMNS["direct_supervisor"])

        def cell(row: list[object], index: int) -> str:
            return str(row[index]).strip() if len(row) > index else ""

        results: list[dict[str, str]] = []
        for row_number, row in enumerate(rows, start=ROSTER_START_ROW):
            if parse_sheet_date(cell(row, date_index)) != today:
                continue
            name = cell(row, name_index)
            if not name:
                continue
            username = cell(row, tg_index).lstrip("@").strip()
            results.append({
                "row": str(row_number),
                "chinese_name": name,
                "resume_name": cell(row, resume_name_index),
                "work_tg": f"@{username}" if username else "",
                "supervisor": cell(row, supervisor_index),
            })
        return results

    @staticmethod
    def _last_used_row(rows: list[list[object]], start_row: int) -> int:
        last = start_row - 1
        for offset, row in enumerate(rows):
            if any(str(value).strip() for value in row):
                last = start_row + offset
        return last

    def _ensure_rows(self, service, sheet_id: int, required_row: int) -> None:
        metadata = service.spreadsheets().get(
            spreadsheetId=self.spreadsheet_id, fields="sheets.properties"
        ).execute()
        props = next(s["properties"] for s in metadata["sheets"] if s["properties"]["sheetId"] == sheet_id)
        count = int(props["gridProperties"]["rowCount"])
        if required_row > count:
            service.spreadsheets().batchUpdate(
                spreadsheetId=self.spreadsheet_id,
                body={"requests": [{"appendDimension": {"sheetId": sheet_id, "dimension": "ROWS", "length": max(50, required_row - count)}}]},
            ).execute()

    def _copy_row_rules(
        self, service, sheet: str, sheet_id: int, source_row: int,
        target_row: int, end_column: int,
        dropdown_exclusions: set[str] | None = None,
    ) -> None:
        """Copy only format and validation; never copy values or formulas."""
        if source_row < 1 or source_row == target_row:
            return
        result = service.spreadsheets().get(
            spreadsheetId=self.spreadsheet_id,
            ranges=[f"'{sheet}'!A{source_row}:{self._index_to_column(end_column - 1)}{source_row}"],
            includeGridData=True,
            fields="sheets.data.rowData.values(userEnteredFormat,dataValidation)",
        ).execute()

        source_values = (
            result.get("sheets", [{}])[0]
            .get("data", [{}])[0]
            .get("rowData", [{}])[0]
            .get("values", [])
        )
        if not source_values:
            return
        cells = [
            {
                **({"userEnteredFormat": cell["userEnteredFormat"]} if cell.get("userEnteredFormat") else {}),
                **({"dataValidation": cell["dataValidation"]} if cell.get("dataValidation") else {}),
            }
            for cell in source_values
        ]
        # Keep the copied row structure exactly as wide as the target range. Empty
        # cells stay empty; this only carries format and dropdown validation.
        cells.extend({} for _ in range(max(0, end_column - len(cells))))
        excluded = dropdown_exclusions or set()
        if sheet == ROSTER_SHEET:
            # A single neighbouring row is not reliable: some rows are missing V or
            # other validation cells. Find the first live rule for every required
            # dropdown column and apply that rule to the destination row.
            validation_result = service.spreadsheets().get(
                spreadsheetId=self.spreadsheet_id,
                ranges=[f"'{ROSTER_SHEET}'!I{ROSTER_START_ROW}:BF{target_row}"],
                includeGridData=True,
                fields="sheets.data.rowData.values(dataValidation)",
            ).execute()
            grid_rows = (
                validation_result.get("sheets", [{}])[0]
                .get("data", [{}])[0]
                .get("rowData", [])
            )
            for column in DROPDOWN_STRUCTURE_COLUMNS:
                absolute_index = self._column_index(column)
                if column in excluded:
                    # The roster specifically requires a truly blank job-grade cell
                    # to have no dropdown validation at all.
                    cells[absolute_index].pop("dataValidation", None)
                    continue
                relative_index = absolute_index - self._column_index("I")
                rule = next(
                    (
                        row.get("values", [])[relative_index].get("dataValidation")
                        for row in grid_rows
                        if len(row.get("values", [])) > relative_index
                        and row["values"][relative_index].get("dataValidation")
                    ),
                    None,
                )
                if rule:
                    cells[absolute_index]["dataValidation"] = rule
                else:
                    logger.warning("No live dropdown rule found for roster column %s", column)
        service.spreadsheets().batchUpdate(
            spreadsheetId=self.spreadsheet_id,
            body={"requests": [{"updateCells": {
                "range": {"sheetId": sheet_id, "startRowIndex": target_row - 1, "endRowIndex": target_row, "startColumnIndex": 0, "endColumnIndex": end_column},
                "rows": [{"values": cells}],
                "fields": "userEnteredFormat,dataValidation",
            }}]},
        ).execute()

    def _copy_roster_formulas(self, service, target_row: int) -> None:
        """Copy only established per-row formulas, adjusted by Google Sheets."""
        result = service.spreadsheets().get(
            spreadsheetId=self.spreadsheet_id,
            ranges=[f"'{ROSTER_SHEET}'!X{ROSTER_START_ROW}:{ROSTER_LAST_COLUMN}{target_row}"],
            includeGridData=True,
            fields="sheets.data.rowData.values(userEnteredValue)",
        ).execute()
        grid_rows = (
            result.get("sheets", [{}])[0]
            .get("data", [{}])[0]
            .get("rowData", [])
        )
        requests = []
        roster_sheet_id = self._sheet_id(service, ROSTER_SHEET)
        base_index = self._column_index("X")
        for column in FORMULA_COLUMNS:
            absolute_index = self._column_index(column)
            relative_index = absolute_index - base_index
            source_offset = next(
                (
                    offset for offset, row in enumerate(grid_rows)
                    if len(row.get("values", [])) > relative_index
                    and row["values"][relative_index].get("userEnteredValue", {}).get("formulaValue")
                ),
                None,
            )
            if source_offset is None:
                logger.warning("No live formula template found for roster column %s", column)
                continue
            source_index = ROSTER_START_ROW - 1 + source_offset
            requests.append({"copyPaste": {
                "source": {
                    "sheetId": roster_sheet_id,
                    "startRowIndex": source_index,
                    "endRowIndex": source_index + 1,
                    "startColumnIndex": absolute_index,
                    "endColumnIndex": absolute_index + 1,
                },
                "destination": {
                    "sheetId": roster_sheet_id,
                    "startRowIndex": target_row - 1,
                    "endRowIndex": target_row,
                    "startColumnIndex": absolute_index,
                    "endColumnIndex": absolute_index + 1,
                },
                "pasteType": "PASTE_FORMULA",
                "pasteOrientation": "NORMAL",
            }})
        if requests:
            service.spreadsheets().batchUpdate(
                spreadsheetId=self.spreadsheet_id,
                body={"requests": requests},
            ).execute()

    @staticmethod
    def _index_to_column(index: int) -> str:
        value = index + 1
        result = ""
        while value:
            value, remainder = divmod(value - 1, 26)
            result = chr(65 + remainder) + result
        return result

    @staticmethod
    def _dropdown_value(column: str, value: str) -> str | None:
        allowed = DROPDOWN_ALLOWED.get(column)
        if not allowed:
            return value
        return value if value in allowed else None

    def _sync_profile(
        self,
        profile: dict[str, object],
        changes: dict[str, tuple[str, str]],
        is_new: bool,
        extra_fields: dict[str, str] | None = None,
        prefer_name_match: bool = False,
        reconcile_core: bool = False,
        onboarding_event: bool = False,
    ) -> int:
        service = self._service()
        self._assert_roster_layout(service)
        # Read through BH so idempotency checks can see salary, recruiting,
        # employment and latest-change fields, not only the core profile through AG.
        rows = self._read_rows(service, ROSTER_SHEET, "A4:BH")
        resume_index = self._column_index(ROSTER_COLUMNS["resume_name"])
        employee_code = str(profile.get("employee_code") or "")
        if employee_code and len(employee_code) != 6:
            raise ValueError("员工编码必须为6位，候选人编码不能写入员工编码列")
        target_row = 0
        matched_by_name = False
        stored_row = int(profile.get("sheet_row") or 0)
        profile_name_keys = (
            person_name_keys(str(profile.get("resume_name") or ""))
            | person_name_keys(str(profile.get("chinese_name") or ""))
        )
        if stored_row >= ROSTER_START_ROW and stored_row - ROSTER_START_ROW < len(rows):
            stored_values = rows[stored_row - ROSTER_START_ROW]
            stored_name_keys = (
                person_name_keys(str(stored_values[resume_index] if len(stored_values) > resume_index else ""))
                | person_name_keys(str(stored_values[2] if len(stored_values) > 2 else ""))
            )
            if profile_name_keys & stored_name_keys:
                target_row = stored_row
                matched_by_name = True
        # For "入职信息确认"/"新人入职" submissions (prefer_name_match=True), HR may already have
        # a placeholder row keyed only by candidate name (姓名/简历名，即候选人姓名) before an
        # employee code exists -- e.g. created earlier during offer approval. Look that up FIRST
        # so we keep filling the same row instead of creating a duplicate. Only act on a single
        # unambiguous match; never guess between same-name rows.
        if prefer_name_match and not target_row:
            if profile_name_keys:
                name_matches = [
                    ROSTER_START_ROW + offset
                    for offset, row in enumerate(rows)
                    if profile_name_keys & (
                        person_name_keys(str(row[resume_index] if len(row) > resume_index else ""))
                        | person_name_keys(str(row[2] if len(row) > 2 else ""))
                    )
                ]
                if len(name_matches) == 1:
                    target_row = name_matches[0]
                    matched_by_name = True
        if not target_row and employee_code:
            for offset, row in enumerate(rows):
                if len(row) > 1 and str(row[1]).strip() == employee_code:
                    target_row = ROSTER_START_ROW + offset
                    break
        row_already_exists = bool(target_row)
        if not target_row:
            target_row = self._last_used_row(rows, ROSTER_START_ROW) + 1
            self._ensure_rows(service, self._sheet_id(service, ROSTER_SHEET), target_row)

        updates: list[dict[str, object]] = []
        # A brand-new row and an existing placeholder row located purely by candidate name both
        # need every field written; only a genuine employee-code match (an existing, already
        # filled-in employee) should stay a diff-only update. Only a truly new row (not a
        # name-matched placeholder) gets a freshly assigned serial number in column A.
        # The local database can retain an old sheet_row after someone deletes the
        # Google Sheets row manually. If no live row matches, rebuild a complete
        # row regardless of the local is_new flag; never create an extras-only row.
        effective_new = not row_already_exists or (is_new and matched_by_name)
        if not row_already_exists:
            sequence_values = [int(str(row[0])) for row in rows if row and str(row[0]).isdigit()]
            updates.append({"range": f"'{ROSTER_SHEET}'!A{target_row}", "values": [[max(sequence_values, default=0) + 1]]})
        if effective_new:
            keys = ROSTER_COLUMNS.keys()
        elif reconcile_core:
            # Re-send all locally known core values without clearing sheet-only data.
            # This repairs a prior partial sync while keeping the change log untouched.
            keys = [key for key in ROSTER_COLUMNS if str(profile.get(key) or "").strip()]
        else:
            keys = changes.keys()
        for key in keys:
            column = ROSTER_COLUMNS.get(key)
            if column:
                value = str(profile.get(key) or "")
                if key == "age_range":
                    value = value.replace("25以下", "25 以下").replace("51以上", "51 以上")
                elif key == "office_region":
                    value = default_office_region(value)
                elif key == "birthday_month":
                    month = "".join(character for character in value if character.isdigit())
                    if month and 1 <= int(month) <= 12:
                        value = f"{int(month)}月"
                value = self._dropdown_value(column, value)
                if value is not None:
                    updates.append({"range": f"'{ROSTER_SHEET}'!{column}{target_row}", "values": [[value]]})
        if str(profile.get("effective_date") or "") and (
            effective_new or is_new or reconcile_core or "effective_date" in changes
        ):
            updates.append({"range": f"'{ROSTER_SHEET}'!{HIRE_DATE_COLUMN}{target_row}", "values": [[str(profile.get('effective_date') or '')]]})
        written_extra_fields: dict[str, str] = {}
        if extra_fields:
            for extra_key, extra_value in extra_fields.items():
                if extra_key == "department":
                    extra_value = normalize_department(str(extra_value))
                # Fixed HR business-rule defaults (app/hr_defaults.py) are only ever meant
                # for a brand-new roster row -- never let them overwrite a field HR has
                # since edited on an existing employee's row.
                extra_column = (
                    EXTRA_ORG_COLUMNS.get(extra_key)
                    or EXTRA_SALARY_COLUMNS.get(extra_key)
                    or EXTRA_HR_DEFAULT_COLUMNS.get(extra_key)
                    or EXTRA_IDENTITY_COLUMNS.get(extra_key)
                )
                if extra_key in NEW_HIRE_ONLY_EXTRA_KEYS and not effective_new:
                    current_row = rows[target_row - ROSTER_START_ROW] if target_row >= ROSTER_START_ROW and target_row - ROSTER_START_ROW < len(rows) else []
                    column_index = self._column_index(extra_column) if extra_column else -1
                    current_value = str(current_row[column_index]).strip() if column_index >= 0 and len(current_row) > column_index else ""
                    if current_value:
                        continue
                if extra_column and extra_value:
                    if extra_key == "hrbp":
                        extra_value = str(extra_value).split("@", 1)[0].strip()
                    dropdown_value = self._dropdown_value(extra_column, str(extra_value))
                    if dropdown_value is None:
                        logger.warning("Skipped invalid dropdown value %s=%r", extra_key, extra_value)
                        continue
                    extra_value = dropdown_value
                    current_row = rows[target_row - ROSTER_START_ROW] if target_row >= ROSTER_START_ROW and target_row - ROSTER_START_ROW < len(rows) else []
                    column_index = self._column_index(extra_column)
                    current_value = str(current_row[column_index]).strip() if len(current_row) > column_index else ""
                    if current_value == str(extra_value).strip():
                        continue
                    updates.append({"range": f"'{ROSTER_SHEET}'!{extra_column}{target_row}", "values": [[extra_value]]})
                    written_extra_fields[extra_key] = str(extra_value)
        if updates:
            source_row = max(
                (ROSTER_START_ROW + index for index, item in enumerate(rows[: max(0, target_row - ROSTER_START_ROW)]) if len(item) > 1 and str(item[1]).strip()),
                default=max(ROSTER_START_ROW, target_row - 1),
            )
            current_row = (
                rows[target_row - ROSTER_START_ROW]
                if target_row >= ROSTER_START_ROW
                and target_row - ROSTER_START_ROW < len(rows)
                else []
            )
            grade_index = self._column_index(EXTRA_ORG_COLUMNS["job_grade"])
            current_grade = str(current_row[grade_index]).strip() if len(current_row) > grade_index else ""
            incoming_grade = str((extra_fields or {}).get("job_grade") or "").strip()
            dropdown_exclusions = {EXTRA_ORG_COLUMNS["job_grade"]} if not (incoming_grade or current_grade) else set()
            self._copy_row_rules(
                service, ROSTER_SHEET, self._sheet_id(service, ROSTER_SHEET), source_row, target_row,
                self._column_index(ROSTER_LAST_COLUMN) + 1,
                dropdown_exclusions,
            )
            # Reconfirmed onboarding rows may already exist as HR placeholders.
            # Refresh every protected formula column (including BB) from a live
            # template so BB evaluates to the roster-standard 0.0 instead of
            # being left blank or carrying a hard-coded value.
            if effective_new or reconcile_core or onboarding_event:
                self._copy_roster_formulas(service, target_row)
            service.spreadsheets().values().batchUpdate(
                spreadsheetId=self.spreadsheet_id,
                body={"valueInputOption": "USER_ENTERED", "data": updates},
            ).execute()

        # An earlier sync may have written the roster but skipped the onboarding
        # record (for example, before a six-character employee code was assigned).
        # Reconciliation must therefore also retry the one-time 入职 record.
        if changes or written_extra_fields or onboarding_event:
            # When an onboarding submission matches a pre-existing roster row by
            # name, use that live row as the source of truth for the change log.
            # The inbound message may legitimately omit the already-assigned code
            # and organization fields.
            change_profile = dict(profile)
            change_extra_fields = dict(written_extra_fields)
            live_row = (
                rows[target_row - ROSTER_START_ROW]
                if target_row >= ROSTER_START_ROW
                and target_row - ROSTER_START_ROW < len(rows)
                else []
            )

            def live_value(column: str) -> str:
                index = self._column_index(column)
                return str(live_row[index]).strip() if len(live_row) > index else ""

            if onboarding_event and live_row:
                for key, column in (
                    ("employee_code", ROSTER_COLUMNS["employee_code"]),
                    ("chinese_name", ROSTER_COLUMNS["chinese_name"]),
                    ("resume_name", ROSTER_COLUMNS["resume_name"]),
                    ("effective_date", ROSTER_COLUMNS["effective_date"]),
                ):
                    if not str(change_profile.get(key) or "").strip():
                        change_profile[key] = live_value(column)
                for key in (
                    "org_unit", "service_entity", "department",
                    "position_title", "job_level", "job_grade",
                ):
                    change_extra_fields.setdefault(key, live_value(EXTRA_ORG_COLUMNS[key]))
            self._append_change(
                service, change_profile, changes, is_new or onboarding_event,
                change_extra_fields,
            )
        return target_row

    def _append_change(
        self,
        service,
        profile: dict[str, object],
        changes: dict[str, tuple[str, str]],
        is_new: bool,
        extra_fields: dict[str, str] | None = None,
    ) -> None:
        with self._change_lock:
            self._append_change_once(service, profile, changes, is_new, extra_fields)

    def _append_change_once(
        self,
        service,
        profile: dict[str, object],
        changes: dict[str, tuple[str, str]],
        is_new: bool,
        extra_fields: dict[str, str] | None = None,
    ) -> None:
        extra = extra_fields or {}
        unit_after = (
            "/".join(
                part for part in (
                    extra.get("org_unit", ""),
                    extra.get("service_entity", "") or "恒睿",
                    extra.get("department", ""),
                )
                if part
            )
            if is_new
            else ""
        )
        level = str(extra.get("job_level", ""))
        grade = str(extra.get("job_grade", ""))
        level_grade = f"{level}-{grade}" if level and grade else level or grade
        position_level_after = "/".join(
            part for part in (str(extra.get("position_title", "")), level_grade) if part
        ) if is_new else ""
        rows = self._read_rows(service, CHANGE_SHEET, "A3:P")
        matched_row = 0
        matched_existing: list[object] = []
        if is_new:
            identity_keys = onboarding_identity_keys(profile)
            identifier = str(
                profile.get("employee_code")
                or profile.get("resume_name")
                or profile.get("chinese_name")
                or ""
            ).strip()
            if not identifier or not identity_keys:
                logger.warning("Skipped onboarding change record without a stable identity")
                return
            for offset, existing in enumerate(rows):
                change_type = str(existing[1] if len(existing) > 1 else "").strip()
                if change_type != "入职":
                    continue
                existing_profile = {
                    "employee_code": str(existing[3] if len(existing) > 3 else ""),
                    "chinese_name": str(existing[4] if len(existing) > 4 else ""),
                }
                if identity_keys & onboarding_identity_keys(existing_profile):
                    matched_row = CHANGE_START_ROW + offset
                    matched_existing = existing
                    logger.info(
                        "Updating existing onboarding change record for employee %s (%s)",
                        identifier,
                        str(profile.get("chinese_name") or ""),
                    )
                    break
        row = matched_row or self._last_used_row(rows, CHANGE_START_ROW) + 1
        if not matched_row:
            self._ensure_rows(service, self._sheet_id(service, CHANGE_SHEET), row)
        sequence_values = [int(str(item[0])) for item in rows if item and str(item[0]).isdigit()]
        effective_date = str(profile.get("effective_date") or datetime.now().date().isoformat())
        details = "；".join(
            f"{DISPLAY_LABELS.get(key, key)}：{old or '空'} → {new or '空'}"
            for key, (old, new) in changes.items()
        )
        if is_new:
            details = ""
        values = [[
            (
                matched_existing[0]
                if matched_existing and str(matched_existing[0]).strip()
                else max(sequence_values, default=0) + 1
            ),
            "入职" if is_new else "其他",
            effective_date,
            str(
                profile.get("employee_code")
                or profile.get("resume_name")
                or profile.get("chinese_name")
                or ""
            ),
            str(profile.get("chinese_name") or ""),
            "", unit_after, "", position_level_after, "", "", "", "",
            details,
            "新人入职" if is_new else "员工字段变更（机器人）",
            int(effective_date[5:7]) if len(effective_date) >= 7 and effective_date[5:7].isdigit() else "",
        ]]
        if not matched_row:
            source_row = max(CHANGE_START_ROW, row - 1)
            self._copy_row_rules(service, CHANGE_SHEET, self._sheet_id(service, CHANGE_SHEET), source_row, row, 16)
        service.spreadsheets().values().update(
            spreadsheetId=self.spreadsheet_id,
            range=f"'{CHANGE_SHEET}'!A{row}:P{row}",
            valueInputOption="USER_ENTERED",
            body={"values": values},
        ).execute()

    async def clear_profile(self, profile: dict[str, object]) -> None:
        await asyncio.to_thread(self._clear_profile, profile)

    def _clear_profile(self, profile: dict[str, object]) -> None:
        service = self._service()
        self._assert_roster_layout(service)
        rows = self._read_rows(service, ROSTER_SHEET, "A4:AG")
        code = str(profile.get("employee_code") or "")
        target = next((ROSTER_START_ROW + i for i, row in enumerate(rows) if len(row) > 1 and str(row[1]).strip() == code), 0)
        if not target:
            return
        ranges = [f"'{ROSTER_SHEET}'!{column}{target}" for column in ROSTER_COLUMNS.values()]
        ranges.append(f"'{ROSTER_SHEET}'!{HIRE_DATE_COLUMN}{target}")
        service.spreadsheets().values().batchClear(
            spreadsheetId=self.spreadsheet_id, body={"ranges": ranges}
        ).execute()
        changes = {key: (str(profile.get(key) or ""), "") for key in ROSTER_COLUMNS}
        self._append_change(service, profile, changes, False)

    async def read_roster(self) -> list[dict[str, str]]:
        return await asyncio.to_thread(self._read_roster)

    def _read_roster(self) -> list[dict[str, str]]:
        service = self._service()
        rows = self._read_rows(service, ROSTER_SHEET, "A4:AG", self.roster_spreadsheet_id)
        indices = {key: self._column_index(column) for key, column in ROSTER_COLUMNS.items()}
        roster: list[dict[str, str]] = []
        for row in rows:
            if not any(str(value).strip() for value in row):
                continue
            record = {
                key: (str(row[index]).strip() if len(row) > index else "")
                for key, index in indices.items()
            }
            roster.append(record)
        return roster

    @staticmethod
    def _column_index(column: str) -> int:
        result = 0
        for ch in column:
            result = result * 26 + (ord(ch.upper()) - ord("A") + 1)
        return result - 1

    async def find_reference_org_fields(self, org_hint: str) -> dict[str, str]:
        """Best-effort: look up organizational fields from other roster rows whose
        department/team/org-unit text matches a hint mentioned in the inbound message.
        Only returns a field when every matching row agrees on its value; never guesses.
        """
        if not org_hint or not org_hint.strip():
            return {}
        return await asyncio.to_thread(self._find_reference_org_fields, org_hint.strip())

    def _find_reference_org_fields(self, org_hint: str) -> dict[str, str]:
        service = self._service()
        rows = self._read_rows(service, ROSTER_SHEET, "A4:AB")
        indices = {key: self._column_index(column) for key, column in EXTRA_ORG_COLUMNS.items()}
        scored_matches: list[tuple[int, dict[str, str]]] = []
        for row in rows:
            record = {
                key: (str(row[index]).strip() if len(row) > index else "")
                for key, index in indices.items()
            }
            fields = [record.get(key, "") for key in ("department", "team", "org_unit")]
            haystack = " ".join(fields)
            # org_hint from GPT may bundle multiple concepts with punctuation the sheet
            # doesn't use (e.g. "市场部-华东团队"), so also match if an individual
            # department/team/org_unit value is itself a substring of the hint (or vice
            # versa), not only when the whole hint appears verbatim in the joined text.
            # Prefer the most specific match. For a hint such as
            # "ACFAN特战队 app运营组", an app运营组 row scores on both department
            # and team, while another team in ACFAN only scores on department.
            score = sum(
                len(field_value)
                for field_value in fields
                if field_value and (field_value in org_hint or org_hint in field_value)
            )
            if org_hint in haystack:
                score += len(org_hint)
            if score:
                scored_matches.append((score, record))
        if not scored_matches:
            return {}
        best_score = max(score for score, _ in scored_matches)
        matches = [record for score, record in scored_matches if score == best_score]
        result: dict[str, str] = {}
        for key in EXTRA_ORG_COLUMNS:
            # 职等 must only come from the submitted employee information. Never
            # infer it from colleagues in the same organization.
            if key == "job_grade":
                continue
            populated = [match[key] for match in matches if match.get(key)]
            values = set(populated)
            if len(values) == 1:
                result[key] = next(iter(values))
            elif key in {"direct_supervisor", "indirect_supervisor"} and populated:
                ranked = Counter(populated).most_common()
                # Use a supervisor only when one value has a clear, repeated lead.
                if ranked[0][1] >= 2 and (len(ranked) == 1 or ranked[0][1] > ranked[1][1]):
                    result[key] = ranked[0][0]
        return result
