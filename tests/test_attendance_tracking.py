import tempfile
import unittest
from datetime import UTC, datetime, timedelta

from app.database import Database


class AttendanceTrackingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        handle = tempfile.NamedTemporaryFile(suffix=".sqlite3")
        self.addCleanup(handle.close)
        self.handle = handle
        self.db = Database(handle.name)
        await self.db.initialize()

    async def test_twenty_minutes_inclusive_is_on_time(self):
        sent = datetime(2026, 9, 9, 4, 0, tzinfo=UTC)
        await self.db.create_attendance_check(
            1, "春雨", "@spring", "2026-09-09", sent, sent + timedelta(minutes=20)
        )
        result = await self.db.record_attendance_response(
            1, "2026-09-09", sent + timedelta(minutes=20), False
        )
        self.assertEqual(result["status"], "on_time")
        self.assertFalse(result["notify_late"])

    async def test_late_reply_waits_for_screenshot(self):
        sent = datetime(2026, 9, 9, 4, 0, tzinfo=UTC)
        await self.db.create_attendance_check(
            2, "夏风", "@summer", "2026-09-09", sent, sent + timedelta(minutes=20)
        )
        result = await self.db.record_attendance_response(
            2, "2026-09-09", sent + timedelta(minutes=20, seconds=1), False
        )
        self.assertEqual(result["status"], "late_waiting_screenshot")
        self.assertTrue(result["notify_late"])
        screenshot = await self.db.record_attendance_response(
            2, "2026-09-09", sent + timedelta(minutes=21), True
        )
        self.assertEqual(screenshot["status"], "screenshot_received")

    async def test_only_completely_unanswered_checks_are_reported(self):
        sent = datetime(2026, 9, 9, 4, 0, tzinfo=UTC)
        for user_id, name in ((3, "秋叶"), (4, "冬雪")):
            await self.db.create_attendance_check(
                user_id, name, f"@u{user_id}", "2026-09-09", sent,
                sent + timedelta(minutes=20),
            )
        await self.db.record_attendance_response(
            4, "2026-09-09", sent + timedelta(minutes=21), False
        )
        rows = await self.db.unanswered_attendance(
            "2026-09-09", datetime(2026, 9, 9, 12, 0, tzinfo=UTC)
        )
        self.assertEqual([row["chinese_name"] for row in rows], ["秋叶"])

    async def test_thirty_minutes_without_reply_is_absence(self):
        sent = datetime(2026, 9, 9, 4, 0, tzinfo=UTC)
        check_id = await self.db.create_attendance_check(
            5, "星河", "@star", "2026-09-09", sent, sent + timedelta(minutes=20)
        )
        before = await self.db.attendance_absences_due(sent + timedelta(minutes=29, seconds=59))
        self.assertEqual(before, [])
        due = await self.db.attendance_absences_due(sent + timedelta(minutes=30))
        self.assertEqual([row["id"] for row in due], [check_id])
        await self.db.mark_attendance_absence_notified([check_id])
        self.assertEqual(await self.db.attendance_absences_due(sent + timedelta(minutes=31)), [])


if __name__ == "__main__":
    unittest.main()
