from __future__ import annotations

import asyncio
import json
import logging
from collections import Counter
from datetime import datetime
from pathlib import Path
from threading import Lock

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

from app.employee_profile import DISPLAY_LABELS, person_name_keys

logger = logging.getLogger(__name__)

ROSTER_SHEET = "花名册"
CHANGE_SHEET = "变更记录"
ROSTER_SHEET_ID = 139264850
CHANGE_SHEET_ID = 310862563
ROSTER_START_ROW = 4
CHANGE_START_ROW = 3
ROSTER_COLUMNS = {
    "employee_code": "B", "chinese_name": "C", "resume_name": "D",
    "employment_status": "E", "effective_date": "F", "gender": "G",
    "age_range": "H", "education": "I", "nationality": "J", "birthday_month": "K",
    "office_region": "AB", "work_tg": "AD", "private_contact": "AE", "work_email": "AF",
}

# Organizational fields that are consistent within a team/department, safe to copy from
# an existing roster row that matches a department/team name mentioned in the inbound message.
EXTRA_ORG_COLUMNS = {
    "org_unit": "L", "job_sequence": "M", "service_entity": "N",
    "department": "O", "team": "P", "hrbp": "R",
    "position_type": "S", "position_title": "T", "job_level": "U",
    "job_grade": "V", "direct_supervisor": "Z", "indirect_supervisor": "AA",
    "work_mode": "AC", "recruitment_channel": "AO", "resume_source": "AP",
}
# Salary fields extracted directly from the message when explicitly stated (never inferred
# from other rows). Currency is always CNY per company policy.
EXTRA_SALARY_COLUMNS = {
    "salary_currency": "AI", "trial_salary": "AJ", "confirmed_salary": "AK",
}
# Fixed HR business-rule defaults (see app/hr_defaults.py) applied only to brand-new
# roster rows (is_new=True) -- never inferred from other rows, never used on updates.
EXTRA_HR_DEFAULT_COLUMNS = {
    "mgmt_headcount": "Y",
    "work_mode": "AC", "salary_basis": "AL",
    "employment_type": "AV", "establishment_attribute": "AW", "establishment_status": "AX",
    "probation_months": "AY", "probation_end_date": "AZ",
    "tenure_years": "BB",
    "latest_change_reason": "BF",
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
    "E": {"在职", "试用期", "调入", "调出", "停薪留职"},
    "G": {"男", "女"},
    "H": {"25 以下", "26-30", "31-35", "36-40", "41-45", "46-50", "51 以上"},
    "I": {"本科", "博士", "大专", "高中及以下", "硕士"},
    "K": {f"{month}月" for month in range(1, 13)},
    "M": {"派驻", "直属"},
    "S": {"财务", "策划", "产品", "法务", "管理", "技术", "内容", "其他", "渠道", "人事", "商务", "设计", "市场", "销售", "行政", "研发", "运营", "质检", "HRBP", "SSC"},
    "U": {*(f"P{i}" for i in range(1, 9)), *(f"M{i}" for i in range(1, 8))},
    "V": {"1", "2", "3"},
    "AC": {"远程", "集中办公室", "混合"},
    "AI": {"CNY", "USD", "USDT", "HKD", "SGD", "TWD", "GBP", "EUR", "JPY", "KRW", "AED"},
    "AO": {"员工内推", "中诚渠道", "猎头", "二次入职", "卓亚渠道", "万天渠道", "宏景渠道", "燎原社渠道", "星探队", "其他", "寻英渠道", "牧星渠道"},
    "AV": {"全职", "外包", "实习", "劳务派遣"},
    "AW": {"正编", "外包", "顾问"},
    "AX": {"空缺", "冻结", "在编"},
}

# These columns must retain the roster's native dropdown validation. W, X and BE
# are formula columns: their validation is preserved, but their values/formulas are
# never written by this bot.
DROPDOWN_STRUCTURE_COLUMNS = (
    "H", "I", "K", "M", "S", "U", "V", "W", "X", "AC", "AI", "AO", "AV", "AW", "AX", "BA", "BC", "BE",
)

FORMULA_COLUMNS = ("W", "X", "BB", "BE", "BG", "BJ")


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

    def _service(self):
        credentials = Credentials.from_service_account_file(
            self.credentials_file,
            scopes=["https://www.googleapis.com/auth/spreadsheets"],
        )
        return build("sheets", "v4", credentials=credentials, cache_discovery=False)

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
                ranges=[f"'{ROSTER_SHEET}'!H{ROSTER_START_ROW}:BE{target_row}"],
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
                relative_index = absolute_index - self._column_index("H")
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
            ranges=[f"'{ROSTER_SHEET}'!W{ROSTER_START_ROW}:BJ{target_row}"],
            includeGridData=True,
            fields="sheets.data.rowData.values(userEnteredValue)",
        ).execute()
        grid_rows = (
            result.get("sheets", [{}])[0]
            .get("data", [{}])[0]
            .get("rowData", [])
        )
        requests = []
        base_index = self._column_index("W")
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
                    "sheetId": ROSTER_SHEET_ID,
                    "startRowIndex": source_index,
                    "endRowIndex": source_index + 1,
                    "startColumnIndex": absolute_index,
                    "endColumnIndex": absolute_index + 1,
                },
                "destination": {
                    "sheetId": ROSTER_SHEET_ID,
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
        # Read through BG so idempotency checks can see salary, recruiting,
        # employment and latest-change fields, not only the core profile through AF.
        rows = self._read_rows(service, ROSTER_SHEET, "A4:BG")
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
                person_name_keys(str(stored_values[3] if len(stored_values) > 3 else ""))
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
                        person_name_keys(str(row[3] if len(row) > 3 else ""))
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
            self._ensure_rows(service, ROSTER_SHEET_ID, target_row)

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
            updates.append({"range": f"'{ROSTER_SHEET}'!AQ{target_row}", "values": [[str(profile.get('effective_date') or '')]]})
        written_extra_fields: dict[str, str] = {}
        if extra_fields:
            for extra_key, extra_value in extra_fields.items():
                # Fixed HR business-rule defaults (app/hr_defaults.py) are only ever meant
                # for a brand-new roster row -- never let them overwrite a field HR has
                # since edited on an existing employee's row.
                extra_column = (
                    EXTRA_ORG_COLUMNS.get(extra_key)
                    or EXTRA_SALARY_COLUMNS.get(extra_key)
                    or EXTRA_HR_DEFAULT_COLUMNS.get(extra_key)
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
            grade_index = self._column_index("V")
            current_grade = str(current_row[grade_index]).strip() if len(current_row) > grade_index else ""
            incoming_grade = str((extra_fields or {}).get("job_grade") or "").strip()
            dropdown_exclusions = {"V"} if not (incoming_grade or current_grade) else set()
            self._copy_row_rules(
                service, ROSTER_SHEET, ROSTER_SHEET_ID, source_row, target_row,
                self._column_index("BJ") + 1,
                dropdown_exclusions,
            )
            if effective_new or reconcile_core:
                self._copy_roster_formulas(service, target_row)
            service.spreadsheets().values().batchUpdate(
                spreadsheetId=self.spreadsheet_id,
                body={"valueInputOption": "USER_ENTERED", "data": updates},
            ).execute()

        if changes or written_extra_fields:
            self._append_change(
                service, profile, changes, is_new or onboarding_event,
                written_extra_fields,
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
        if is_new:
            employee_code = str(profile.get("employee_code") or "").strip().casefold()
            # Column D is the sole employee identifier for onboarding changes.
            # Without it, do not create an unidentifiable/undedupeable 入职 row.
            if not employee_code:
                logger.warning("Skipped onboarding change record without employee code")
                return
            for existing in rows:
                change_type = str(existing[1] if len(existing) > 1 else "").strip()
                if change_type != "入职":
                    continue
                existing_code = str(existing[3] if len(existing) > 3 else "").strip().casefold()
                if existing_code == employee_code:
                    logger.info(
                        "Skipped duplicate onboarding change record for employee %s (%s)",
                        employee_code,
                        str(profile.get("chinese_name") or ""),
                    )
                    return
        row = self._last_used_row(rows, CHANGE_START_ROW) + 1
        self._ensure_rows(service, CHANGE_SHEET_ID, row)
        sequence_values = [int(str(item[0])) for item in rows if item and str(item[0]).isdigit()]
        effective_date = str(profile.get("effective_date") or datetime.now().date().isoformat())
        details = "；".join(
            f"{DISPLAY_LABELS.get(key, key)}：{old or '空'} → {new or '空'}"
            for key, (old, new) in changes.items()
        )
        if is_new:
            details = ""
        values = [[
            max(sequence_values, default=0) + 1,
            "入职" if is_new else "其他",
            effective_date,
            str(profile.get("employee_code") or ""),
            str(profile.get("chinese_name") or ""),
            "", unit_after, "", position_level_after, "", "", "", "",
            details,
            "新人入职" if is_new else "员工字段变更（机器人）",
            int(effective_date[5:7]) if len(effective_date) >= 7 and effective_date[5:7].isdigit() else "",
        ]]
        source_row = max(CHANGE_START_ROW, row - 1)
        self._copy_row_rules(service, CHANGE_SHEET, CHANGE_SHEET_ID, source_row, row, 16)
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
        rows = self._read_rows(service, ROSTER_SHEET, "A4:AF")
        code = str(profile.get("employee_code") or "")
        target = next((ROSTER_START_ROW + i for i, row in enumerate(rows) if len(row) > 1 and str(row[1]).strip() == code), 0)
        if not target:
            return
        ranges = [f"'{ROSTER_SHEET}'!{column}{target}" for column in ROSTER_COLUMNS.values()]
        ranges.append(f"'{ROSTER_SHEET}'!AQ{target}")
        service.spreadsheets().values().batchClear(
            spreadsheetId=self.spreadsheet_id, body={"ranges": ranges}
        ).execute()
        changes = {key: (str(profile.get(key) or ""), "") for key in ROSTER_COLUMNS}
        self._append_change(service, profile, changes, False)

    async def read_roster(self) -> list[dict[str, str]]:
        return await asyncio.to_thread(self._read_roster)

    def _read_roster(self) -> list[dict[str, str]]:
        service = self._service()
        rows = self._read_rows(service, ROSTER_SHEET, "A4:AF", self.roster_spreadsheet_id)
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
        rows = self._read_rows(service, ROSTER_SHEET, "A4:AA")
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
