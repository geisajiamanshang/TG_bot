from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from aiogram import Bot

from app.bot import Services, build_dispatcher, set_commands
from app.config import get_settings
from app.database import Database
from app.google_sheets_service import GoogleSheetsService
from app.gpt_service import GPTService
from app.vision_service import VisionService


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    settings = get_settings()
    db = Database(settings.database_path)
    await db.initialize()

    bot = Bot(token=settings.telegram_bot_token)
    sheets = GoogleSheetsService(
        settings.google_spreadsheet_id,
        settings.google_service_account_file,
        settings.wallet_spreadsheet_id,
        settings.google_roster_spreadsheet_id,
    )
    if not sheets.configured:
        logging.warning("Google Sheets service account is not configured; profile sync will remain pending")
    vision = VisionService(settings)
    gpt = GPTService(settings)
    dp = build_dispatcher(Services(settings=settings, db=db, sheets=sheets, vision=vision, gpt=gpt))

    await bot.delete_webhook(drop_pending_updates=False)
    await set_commands(bot, settings.admin_user_ids)
    Path(settings.database_path).parent.joinpath("healthy").touch()
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        Path(settings.database_path).parent.joinpath("healthy").unlink(missing_ok=True)
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
