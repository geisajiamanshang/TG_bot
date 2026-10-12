import unittest

from app.employee_profile import parse_extra_profile_fields
from app.google_sheets_service import GoogleSheetsService


class _Exec:
    def __init__(self, sink, body):
        self.sink, self.body = sink, body

    def execute(self):
        self.sink.append(self.body)
        return {}


class _Values:
    def __init__(self, sink):
        self.sink = sink

    def batchUpdate(self, spreadsheetId, body):
        return _Exec(self.sink, body)


class _Sheets:
    def __init__(self, sink):
        self.sink = sink

    def values(self):
        return _Values(self.sink)


class _Service:
    def __init__(self, sink):
        self.sink = sink

    def spreadsheets(self):
        return _Sheets(self.sink)


def _service_with_rows(rows):
    service = object.__new__(GoogleSheetsService)
    service.spreadsheet_id = "test"
    writes = []
    service._service = lambda: _Service(writes)
    header = ["序号", "员工编码（新）", "花名*", "候选人编码", "姓名/简历名（选填）"]
    service._read_rows = lambda _svc, _sheet, range_, *_a, **_k: [header] if range_ == "A3:E3" else rows
    service._sheet_id = lambda *_a, **_k: 1
    service._ensure_rows = lambda *_a, **_k: None
    service._copy_row_rules = lambda *_a, **_k: None
    service._copy_roster_formulas = lambda *_a, **_k: None
    service._append_change = lambda *_a, **_k: None
    return service, writes


class CandidateCodeTests(unittest.TestCase):
    def test_text_form_extracts_candidate_code(self):
        fields = parse_extra_profile_fields("入职信息确认\n候选人编码：HJHT0000126\n候选人姓名：测试甲")
        self.assertEqual(fields["candidate_code"], "HJHT0000126")

    def test_onboarding_writes_candidate_code_to_column_d(self):
        # Existing placeholder row 4: A 序号, B 编码, C 花名, D 候选人编码, E 姓名/简历名
        rows = [["1", "", "", "", "测试甲"]]
        service, writes = _service_with_rows(rows)
        target = service._sync_profile(
            {"resume_name": "测试甲", "effective_date": "2026-10-12"},
            {}, True, {"candidate_code": "HJHT0000126"},
            prefer_name_match=True, onboarding_event=True,
        )
        self.assertEqual(target, 4)
        data = {item["range"]: item["values"][0][0] for item in writes[0]["data"]}
        self.assertEqual(data["'花名册'!D4"], "HJHT0000126")
        self.assertEqual(data["'花名册'!E4"], "测试甲")
        self.assertEqual(data["'花名册'!AR4"], "2026-10-12")


    def test_refuses_to_write_into_old_layout(self):
        service, writes = _service_with_rows([])
        service._read_rows = lambda _svc, _sheet, range_, *_a, **_k: (
            [["序号", "员工编码（新）", "花名*", "姓名/简历名（选填）", "在职状态*"]] if range_ == "A3:E3" else []
        )
        with self.assertRaises(ValueError):
            service._sync_profile({"resume_name": "测试乙"}, {}, True, {}, prefer_name_match=True)
        self.assertEqual(writes, [])


if __name__ == "__main__":
    unittest.main()
