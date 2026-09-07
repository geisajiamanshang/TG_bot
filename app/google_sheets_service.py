from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime
from pathlib import Path

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

from app.employee_profile import DISPLAY_LABELS

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
    "position_title": "T", "job_level": "U", "job_grade": "V", "direct_supervisor": "Z",
}
# Salary fields extracted directly from the message when explicitly stated (never inferred
# from other rows). Currency is always CNY per company policy.
EXTRA_SALARY_COLUMNS = {
    "salary_currency": "AI", "trial_salary": "AJ", "confirmed_salary": "AK",
}
# Fixed HR business-rule defaults (see app/hr_defaults.py) applied only to brand-new
# roster rows (is_new=True) -- never inferred from other rows, never used on updates.
EXTRA_HR_DEFAULT_COLUMNS = {
    "mgmt_sequence": "W", "is_key_position": "X", "mgmt_headcount": "Y",
    "work_mode": "AC", "salary_basis": "AL",
    "employment_type": "AV", "establishment_attribute": "AW", "establishment_status": "AX",
    "probation_months": "AY", "probation_end_date": "AZ",
    "tenure_years": "BB",
    "latest_change_type": "BE", "latest_change_reason": "BF", "latest_change_date": "BG",
}


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
    ) -> int:
        return await asyncio.to_thread(
            self._sync_profile, profile, changes, is_new, extra_fields, prefer_name_match
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

    def _copy_row_rules(self, service, sheet_id: int, source_row: int, target_row: int, end_column: int) -> None:
        if source_row < 1 or source_row == target_row:
            return
        requests = []
        for paste_type in ("PASTE_FORMAT", "PASTE_DATA_VALIDATION"):
            requests.append({"copyPaste": {
                "source": {"sheetId": sheet_id, "startRowIndex": source_row - 1, "endRowIndex": source_row, "startColumnIndex": 0, "endColumnIndex": end_column},
                "destination": {"sheetId": sheet_id, "startRowIndex": target_row - 1, "endRowIndex": target_row, "startColumnIndex": 0, "endColumnIndex": end_column},
                "pasteType": paste_type,
                "pasteOrientation": "NORMAL",
            }})
        service.spreadsheets().batchUpdate(
            spreadsheetId=self.spreadsheet_id, body={"requests": requests}
        ).execute()

    def _sync_profile(
        self,
        profile: dict[str, object],
        changes: dict[str, tuple[str, str]],
        is_new: bool,
        extra_fields: dict[str, str] | None = None,
        prefer_name_match: bool = False,
    ) -> int:
        service = self._service()
        rows = self._read_rows(service, ROSTER_SHEET, "A4:AF")
        employee_code = str(profile.get("employee_code") or "")
        target_row = 0
        matched_by_name = False
        # For "入职信息确认"/"新人入职" submissions (prefer_name_match=True), HR may already have
        # a placeholder row keyed only by candidate name (姓名/简历名，即候选人姓名) before an
        # employee code exists -- e.g. created earlier during offer approval. Look that up FIRST
        # so we keep filling the same row instead of creating a duplicate. Only act on a single
        # unambiguous match; never guess between same-name rows.
        if prefer_name_match:
            resume_name = str(profile.get("resume_name") or "").strip()
            if resume_name:
                name_matches = [
                    ROSTER_START_ROW + offset
                    for offset, row in enumerate(rows)
                    if len(row) > 3 and str(row[3]).strip().casefold() == resume_name.casefold()
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
        effective_new = is_new and (not row_already_exists or matched_by_name)
        if is_new and not row_already_exists:
            sequence_values = [int(str(row[0])) for row in rows if row and str(row[0]).isdigit()]
            updates.append({"range": f"'{ROSTER_SHEET}'!A{target_row}", "values": [[max(sequence_values, default=0) + 1]]})
        keys = ROSTER_COLUMNS.keys() if effective_new else changes.keys()
        for key in keys:
            column = ROSTER_COLUMNS.get(key)
            if column:
                value = str(profile.get(key) or "")
                if key == "age_range":
                    value = value.replace("25以下", "25 以下").replace("51以上", "51 以上")
                updates.append({"range": f"'{ROSTER_SHEET}'!{column}{target_row}", "values": [[value]]})
        if is_new or "effective_date" in changes:
            updates.append({"range": f"'{ROSTER_SHEET}'!AQ{target_row}", "values": [[str(profile.get('effective_date') or '')]]})
        if extra_fields:
            for extra_key, extra_value in extra_fields.items():
                extra_column = (
                    EXTRA_ORG_COLUMNS.get(extra_key)
                    or EXTRA_SALARY_COLUMNS.get(extra_key)
                    or EXTRA_HR_DEFAULT_COLUMNS.get(extra_key)
                )
                if extra_column and extra_value:
                    updates.append({"range": f"'{ROSTER_SHEET}'!{extra_column}{target_row}", "values": [[extra_value]]})
        if updates:
            service.spreadsheets().values().batchUpdate(
                spreadsheetId=self.spreadsheet_id,
                body={"valueInputOption": "USER_ENTERED", "data": updates},
            ).execute()

        self._append_change(service, profile, changes, effective_new, extra_fields)
        return target_row

    def _append_change(
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
        level_after = (
            "/".join(
                part for part in (extra.get("job_level", ""), extra.get("job_grade", ""))
                if part
            )
            if is_new
            else ""
        )
        rows = self._read_rows(service, CHANGE_SHEET, "A3:P")
        row = self._last_used_row(rows, CHANGE_START_ROW) + 1
        self._ensure_rows(service, CHANGE_SHEET_ID, row)
        sequence_values = [int(str(item[0])) for item in rows if item and str(item[0]).isdigit()]
        effective_date = str(profile.get("effective_date") or datetime.now().date().isoformat())
        details = "；".join(
            f"{DISPLAY_LABELS.get(key, key)}：{old or '空'} → {new or '空'}"
            for key, (old, new) in changes.items()
        )
        values = [[
            max(sequence_values, default=0) + 1,
            "入职" if is_new else "其他",
            effective_date,
            str(profile.get("employee_code") or ""),
            str(profile.get("chinese_name") or ""),
            "", unit_after, "", level_after, "", "", "", "",
            details,
            "新人入职（机器人）" if is_new else "员工字段变更（机器人）",
            int(effective_date[5:7]) if len(effective_date) >= 7 and effective_date[5:7].isdigit() else "",
        ]]
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
        matches: list[dict[str, str]] = []
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
            is_match = bool(haystack.strip()) and org_hint in haystack
            if not is_match:
                for field_value in fields:
                    if field_value and (field_value in org_hint or org_hint in field_value):
                        is_match = True
                        break
            if is_match:
                matches.append(record)
        if not matches:
            return {}
        result: dict[str, str] = {}
        for key in EXTRA_ORG_COLUMNS:
            values = {match[key] for match in matches if match.get(key)}
            if len(values) == 1:
                result[key] = next(iter(values))
        return result
