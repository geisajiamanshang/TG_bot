from __future__ import annotations

import asyncio

from app.config import get_settings
from app.database import Database
from app.employee_profile import PROFILE_FIELDS
from app.google_sheets_service import GoogleSheetsService


async def main() -> None:
    settings = get_settings()
    database = Database(settings.database_path)
    sheets = GoogleSheetsService(
        settings.google_spreadsheet_id,
        settings.google_service_account_file,
        settings.wallet_spreadsheet_id,
    )
    if not sheets.configured:
        raise SystemExit("Google Sheets is not configured")
    pending = [
        profile for profile in await database.profiles()
        if profile.get("sync_status") != "synced"
    ]
    succeeded = 0
    failed = 0
    for profile in pending:
        changes = {
            key: ("", str(profile.get(key) or ""))
            for key in PROFILE_FIELDS if profile.get(key)
        }
        try:
            row = await sheets.sync_profile(
                profile,
                changes,
                is_new=profile.get("sheet_row") is None,
            )
            await database.set_profile_sync(int(profile["telegram_user_id"]), "synced", row)
            succeeded += 1
        except Exception as exc:
            await database.set_profile_sync(
                int(profile["telegram_user_id"]), f"error: {str(exc)}"
            )
            failed += 1
    print(f"pending={len(pending)} succeeded={succeeded} failed={failed}")


if __name__ == "__main__":
    asyncio.run(main())
