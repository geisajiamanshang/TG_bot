import unittest
from datetime import date

from app.bot import build_supervisor_groups, supervisor_notice_text
from app.google_sheets_service import GoogleSheetsService, parse_sheet_date


def roster_row(name, effective_date, supervisor, work_tg):
    # A..AE: C 花名(2), G 生效日期(6), AA 直接上级(26), AE 工作TG(30)
    row = [""] * 31
    row[2], row[6], row[26], row[30] = name, effective_date, supervisor, work_tg
    return row


class TodayHiresTests(unittest.TestCase):
    def test_reads_g_c_ae_aa_for_today_only(self):
        service = object.__new__(GoogleSheetsService)
        service.spreadsheet_id = "test"
        service._service = lambda: object()
        rows = [
            roster_row("昨日新人", "2026-10-15", "上级甲", "@old"),
            roster_row("今日甲", "2026-10-16", "上级甲", "@new_a"),
            roster_row("今日乙", "2026/10/16", "上级甲", "new_b"),
            roster_row("今日丙", "2026.10.16", "", ""),
        ]
        service._read_rows = lambda _svc, _sheet, range_, *_a, **_k: rows
        result = service._today_hires(date(2026, 10, 16))
        self.assertEqual(
            [(r["chinese_name"], r["work_tg"], r["supervisor"], r["row"]) for r in result],
            [("今日甲", "@new_a", "上级甲", "5"), ("今日乙", "@new_b", "上级甲", "6"), ("今日丙", "", "", "7")],
        )

    def test_parse_sheet_date(self):
        self.assertEqual(parse_sheet_date("2026年10月6日"), date(2026, 10, 6))
        self.assertIsNone(parse_sheet_date("待定"))


class SupervisorGroupingTests(unittest.TestCase):
    def test_merges_hires_per_supervisor_and_reports_issues(self):
        hires = [
            {"row": "5", "chinese_name": "今日甲", "work_tg": "@new_a", "supervisor": "上级甲"},
            {"row": "6", "chinese_name": "今日乙", "work_tg": "", "supervisor": "上级 甲"},
            {"row": "7", "chinese_name": "今日丙", "work_tg": "@c", "supervisor": ""},
            {"row": "8", "chinese_name": "今日丁", "work_tg": "@d", "supervisor": "无此人"},
        ]
        contacts = [
            {"requested_name": "上级甲", "chinese_name": "上级甲", "work_tg": "@boss_a", "status": "matched"},
            {"requested_name": "无此人", "status": "not_found"},
        ]
        groups, issues = build_supervisor_groups(hires, contacts)
        self.assertEqual(list(groups), ["boss_a"])
        self.assertEqual([h["chinese_name"] for h in groups["boss_a"]["hires"]], ["今日甲", "今日乙"])
        self.assertEqual(len(issues), 2)
        text = supervisor_notice_text("上级甲", groups["boss_a"]["hires"], "2026-10-16")
        self.assertIn("1. 今日甲｜@new_a", text)
        self.assertIn("2. 今日乙｜工作TG待补充", text)


if __name__ == "__main__":
    unittest.main()
