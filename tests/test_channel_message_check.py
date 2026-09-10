import tempfile
import unittest
from datetime import date

from app.database import Database
from app.google_sheets_service import GoogleSheetsService


class ChannelSheetCandidateTests(unittest.TestCase):
    def test_reads_c_ad_aq_and_filters_from_august(self):
        service = object.__new__(GoogleSheetsService)
        service.spreadsheet_id = "test"
        service._service = lambda: object()
        rows = [
            ["七月员工"] + [""] * 26 + ["@july"] + [""] * 12 + ["2026-07-31"],
            ["八月员工"] + [""] * 26 + ["@aug"] + [""] * 12 + ["2026-08-01"],
            ["无TG员工"] + [""] * 27 + [""] * 12 + ["2026/09/10"],
        ]
        service._read_rows = lambda *_args, **_kwargs: rows

        result = service._channel_message_candidates(date(2026, 8, 1))

        self.assertEqual(
            result,
            [
                {"row": "5", "chinese_name": "八月员工", "work_tg": "@aug", "hire_date": "2026-08-01"},
                {"row": "6", "chinese_name": "无TG员工", "work_tg": "", "hire_date": "2026-09-10"},
            ],
        )


class OutboundMessageLogTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.handle = tempfile.NamedTemporaryFile(suffix=".sqlite3")
        self.addCleanup(self.handle.close)
        self.db = Database(self.handle.name)
        await self.db.initialize()

    async def test_channel_template_is_tracked_per_employee(self):
        await self.db.save_outbound_message(
            101, 101, "频道正文", message_kind="broadcast", template_name="关注恒睿频道"
        )
        await self.db.save_outbound_message(
            102, 102, "普通通知", message_kind="broadcast", template_name="其他模板"
        )
        self.assertEqual(await self.db.channel_message_recipient_ids(), {101})


if __name__ == "__main__":
    unittest.main()
