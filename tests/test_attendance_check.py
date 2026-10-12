from app.google_sheets_service import GoogleSheetsService


def test_attendance_contacts_reads_c_and_ae_without_writing(monkeypatch):
    service = object.__new__(GoogleSheetsService)
    service.spreadsheet_id = service.roster_spreadsheet_id = "test"
    monkeypatch.setattr(service, "_service", lambda: object())
    rows = [
        ["春雨"] + [""] * 27 + ["@SpringRain"],
        ["空谷"] + [""] * 28,
    ]
    monkeypatch.setattr(service, "_read_rows", lambda *_args, **_kwargs: rows)

    result = service._attendance_contacts(["春雨", "空谷", "未找到", " 春 雨 "])

    assert result == [
        {
            "requested_name": "春雨",
            "chinese_name": "春雨",
            "work_tg": "@springrain",
            "row": "4",
            "status": "matched",
        },
        {"requested_name": "空谷", "chinese_name": "空谷", "status": "missing_tg"},
        {"requested_name": "未找到", "status": "not_found"},
    ]


def test_attendance_contacts_rejects_conflicting_duplicate_names(monkeypatch):
    service = object.__new__(GoogleSheetsService)
    service.spreadsheet_id = service.roster_spreadsheet_id = "test"
    monkeypatch.setattr(service, "_service", lambda: object())
    rows = [
        ["夜阑雨"] + [""] * 27 + ["@first"],
        ["夜阑雨"] + [""] * 27 + ["@second"],
    ]
    monkeypatch.setattr(service, "_read_rows", lambda *_args, **_kwargs: rows)

    assert service._attendance_contacts(["夜阑雨"])[0]["status"] == "ambiguous"
