from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import aiosqlite

from app.employee_profile import person_name_keys


@dataclass(frozen=True)
class Employee:
    telegram_user_id: int
    chat_id: int
    username: str | None
    full_name: str
    active: bool


class Database:
    def __init__(self, path: str) -> None:
        self.path = path

    async def initialize(self) -> None:
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self.path) as db:
            await db.executescript(
                """
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS employees (
                    telegram_user_id INTEGER PRIMARY KEY,
                    chat_id INTEGER NOT NULL,
                    username TEXT,
                    full_name TEXT NOT NULL,
                    active INTEGER NOT NULL DEFAULT 1,
                    joined_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS broadcasts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    admin_user_id INTEGER NOT NULL,
                    content TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    success_count INTEGER NOT NULL DEFAULT 0,
                    failed_count INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS feedback (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    telegram_user_id INTEGER NOT NULL,
                    question TEXT NOT NULL,
                    answer TEXT NOT NULL,
                    topic TEXT,
                    helpful INTEGER,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS topic_answers (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    topic TEXT NOT NULL,
                    answer TEXT NOT NULL,
                    admin_user_id INTEGER NOT NULL,
                    recipient_count INTEGER NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS notification_templates (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL UNIQUE,
                    content TEXT NOT NULL,
                    auto_send_to_new INTEGER NOT NULL DEFAULT 0,
                    active INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS employee_profiles (
                    telegram_user_id INTEGER PRIMARY KEY,
                    telegram_username TEXT,
                    telegram_full_name TEXT NOT NULL,
                    employee_code TEXT NOT NULL DEFAULT '',
                    chinese_name TEXT NOT NULL,
                    resume_name TEXT NOT NULL,
                    employment_status TEXT,
                    effective_date TEXT NOT NULL,
                    gender TEXT,
                    age_range TEXT,
                    education TEXT,
                    nationality TEXT,
                    birthday_month TEXT,
                    office_region TEXT,
                    work_tg TEXT,
                    private_contact TEXT,
                    work_email TEXT,
                    raw_message TEXT NOT NULL,
                    sheet_row INTEGER,
                    sync_status TEXT NOT NULL DEFAULT 'pending',
                    deleted INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS employee_profile_changes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    telegram_user_id INTEGER NOT NULL,
                    change_type TEXT NOT NULL,
                    changes TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS inbound_messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    telegram_user_id INTEGER NOT NULL,
                    chat_id INTEGER NOT NULL,
                    message_id INTEGER NOT NULL,
                    username TEXT,
                    full_name TEXT NOT NULL,
                    message_type TEXT NOT NULL,
                    content TEXT,
                    created_at TEXT NOT NULL,
                    UNIQUE(chat_id, message_id)
                );
                  CREATE TABLE IF NOT EXISTS roster (
                      id INTEGER PRIMARY KEY AUTOINCREMENT,
                      employee_code TEXT,
                      chinese_name TEXT,
                      resume_name TEXT,
                      employment_status TEXT,
                      effective_date TEXT,
                      gender TEXT,
                      age_range TEXT,
                      education TEXT,
                      nationality TEXT,
                      birthday_month TEXT,
                      office_region TEXT,
                      work_tg TEXT,
                      private_contact TEXT,
                      work_email TEXT
                  );
                CREATE TABLE IF NOT EXISTS roster_sync_log (
                      id INTEGER PRIMARY KEY AUTOINCREMENT,
                      synced_at TEXT NOT NULL,
                      row_count INTEGER NOT NULL
                  );
                CREATE TABLE IF NOT EXISTS attendance_checks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    telegram_user_id INTEGER NOT NULL,
                    chinese_name TEXT NOT NULL,
                    work_tg TEXT NOT NULL,
                    check_date TEXT NOT NULL,
                    sent_at TEXT NOT NULL,
                    deadline_at TEXT NOT NULL,
                    replied_at TEXT,
                    response_status TEXT NOT NULL DEFAULT 'pending',
                    admin_notified INTEGER NOT NULL DEFAULT 0,
                    absence_notified INTEGER NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS idx_attendance_user_date
                    ON attendance_checks (telegram_user_id, check_date, id);
                CREATE INDEX IF NOT EXISTS idx_attendance_pending_date
                    ON attendance_checks (check_date, response_status, admin_notified);
                """
            )
            # 花名册填写规范更新：候选人编码不再是唯一识别符（改为按"姓名/简历名"匹配），
            # 所以本地表也不应该再对 employee_code 强制 UNIQUE —— 否则不同候选人恰好编码
            # 相同/都留空时会在写入时报 "UNIQUE constraint failed" 而丢失数据。已经存在的
            # 旧库（建表时还带着 UNIQUE 约束）在这里做一次性迁移去掉它，保留全部数据。
            existing_sql_row = await (await db.execute(
                "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'employee_profiles'"
            )).fetchone()
            existing_sql = str(existing_sql_row[0]) if existing_sql_row and existing_sql_row[0] else ""
            if "employee_code" in existing_sql and "UNIQUE" in existing_sql:
                await db.executescript(
                    """
                    ALTER TABLE employee_profiles RENAME TO employee_profiles_pre_unique_migration;
                    CREATE TABLE employee_profiles (
                        telegram_user_id INTEGER PRIMARY KEY,
                        telegram_username TEXT,
                        telegram_full_name TEXT NOT NULL,
                        employee_code TEXT NOT NULL DEFAULT '',
                        chinese_name TEXT NOT NULL,
                        resume_name TEXT NOT NULL,
                        employment_status TEXT,
                        effective_date TEXT NOT NULL,
                        gender TEXT,
                        age_range TEXT,
                        education TEXT,
                        nationality TEXT,
                        birthday_month TEXT,
                        office_region TEXT,
                        work_tg TEXT,
                        private_contact TEXT,
                        work_email TEXT,
                        raw_message TEXT NOT NULL,
                        sheet_row INTEGER,
                        sync_status TEXT NOT NULL DEFAULT 'pending',
                        deleted INTEGER NOT NULL DEFAULT 0,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );
                    INSERT INTO employee_profiles SELECT * FROM employee_profiles_pre_unique_migration;
                    DROP TABLE employee_profiles_pre_unique_migration;
                    """
                )
            columns = {
                str(row[1])
                for row in await (await db.execute("PRAGMA table_info(feedback)")).fetchall()
            }
            if "topic" not in columns:
                await db.execute("ALTER TABLE feedback ADD COLUMN topic TEXT")
            attendance_columns = {
                str(row[1])
                for row in await (await db.execute(
                    "PRAGMA table_info(attendance_checks)"
                )).fetchall()
            }
            if "absence_notified" not in attendance_columns:
                await db.execute(
                    "ALTER TABLE attendance_checks ADD COLUMN absence_notified INTEGER NOT NULL DEFAULT 0"
                )
            await db.execute(
                "UPDATE feedback SET topic = '历史问题' WHERE topic IS NULL OR TRIM(topic) = ''"
            )
            await db.commit()

    async def create_attendance_check(
        self,
        telegram_user_id: int,
        chinese_name: str,
        work_tg: str,
        check_date: str,
        sent_at: datetime,
        deadline_at: datetime,
    ) -> int:
        async with aiosqlite.connect(self.path) as db:
            await db.execute("BEGIN IMMEDIATE")
            await db.execute(
                """
                UPDATE attendance_checks SET response_status = 'superseded'
                WHERE telegram_user_id = ? AND check_date = ?
                  AND response_status IN ('pending', 'late_waiting_screenshot')
                """,
                (telegram_user_id, check_date),
            )
            cursor = await db.execute(
                """
                INSERT INTO attendance_checks
                    (telegram_user_id, chinese_name, work_tg, check_date,
                     sent_at, deadline_at, response_status, admin_notified)
                VALUES (?, ?, ?, ?, ?, ?, 'pending', 0)
                """,
                (
                    telegram_user_id, chinese_name, work_tg, check_date,
                    sent_at.astimezone(UTC).isoformat(),
                    deadline_at.astimezone(UTC).isoformat(),
                ),
            )
            await db.commit()
            return int(cursor.lastrowid or 0)

    async def record_attendance_response(
        self,
        telegram_user_id: int,
        check_date: str,
        replied_at: datetime,
        is_screenshot: bool,
    ) -> dict[str, object] | None:
        """Atomically classify one response against the latest open check."""
        now_utc = replied_at.astimezone(UTC)
        async with aiosqlite.connect(self.path) as db:
            await db.execute("BEGIN IMMEDIATE")
            row = await (await db.execute(
                """
                SELECT id, chinese_name, work_tg, sent_at, deadline_at, response_status
                FROM attendance_checks
                WHERE telegram_user_id = ? AND check_date = ?
                  AND response_status IN ('pending', 'late_waiting_screenshot')
                ORDER BY id DESC LIMIT 1
                """,
                (telegram_user_id, check_date),
            )).fetchone()
            if not row:
                await db.commit()
                return None
            check_id = int(row[0])
            chinese_name, work_tg = str(row[1]), str(row[2])
            sent_at = datetime.fromisoformat(str(row[3]))
            deadline_text, old_status = str(row[4]), str(row[5])
            deadline = datetime.fromisoformat(deadline_text)
            is_late = now_utc > deadline
            if is_screenshot:
                status = "screenshot_received"
            elif old_status == "late_waiting_screenshot":
                status = "late_waiting_screenshot"
            else:
                status = "on_time" if now_utc <= deadline else "late_waiting_screenshot"
            await db.execute(
                """
                UPDATE attendance_checks
                SET replied_at = ?, response_status = ?
                WHERE id = ?
                """,
                (now_utc.isoformat(), status, check_id),
            )
            await db.commit()
            return {
                "id": check_id,
                "status": status,
                "chinese_name": chinese_name,
                "work_tg": work_tg,
                "sent_at": sent_at.isoformat(),
                "replied_at": now_utc.isoformat(),
                "elapsed_seconds": max(0, int((now_utc - sent_at).total_seconds())),
                "notify_late": is_late and old_status == "pending",
            }

    async def attendance_absences_due(self, now: datetime) -> list[dict[str, object]]:
        cutoff = now.astimezone(UTC) - timedelta(minutes=30)
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            rows = await (await db.execute(
                """
                SELECT id, telegram_user_id, chinese_name, work_tg, sent_at
                FROM attendance_checks
                WHERE response_status = 'pending' AND absence_notified = 0
                  AND sent_at <= ?
                ORDER BY id
                """,
                (cutoff.isoformat(),),
            )).fetchall()
            return [dict(row) for row in rows]

    async def mark_attendance_absence_notified(self, check_ids: list[int]) -> None:
        if not check_ids:
            return
        placeholders = ",".join("?" for _ in check_ids)
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                f"UPDATE attendance_checks SET absence_notified = 1 WHERE id IN ({placeholders})",
                tuple(check_ids),
            )
            await db.commit()

    async def unanswered_attendance(
        self, check_date: str, cutoff_at: datetime
    ) -> list[dict[str, object]]:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            rows = await (await db.execute(
                """
                SELECT id, telegram_user_id, chinese_name, work_tg, sent_at
                FROM attendance_checks
                WHERE check_date = ? AND response_status = 'pending'
                  AND admin_notified = 0 AND sent_at <= ?
                ORDER BY id
                """,
                (check_date, cutoff_at.astimezone(UTC).isoformat()),
            )).fetchall()
            return [dict(row) for row in rows]

    async def mark_attendance_notified(self, check_ids: list[int]) -> None:
        if not check_ids:
            return
        placeholders = ",".join("?" for _ in check_ids)
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                f"UPDATE attendance_checks SET admin_notified = 1 WHERE id IN ({placeholders})",
                tuple(check_ids),
            )
            await db.commit()

    async def save_inbound_message(
        self,
        telegram_user_id: int,
        chat_id: int,
        message_id: int,
        username: str | None,
        full_name: str,
        message_type: str,
        content: str | None,
    ) -> int | None:
        async with aiosqlite.connect(self.path) as db:
            cursor = await db.execute(
                """
                INSERT OR IGNORE INTO inbound_messages
                    (telegram_user_id, chat_id, message_id, username, full_name,
                     message_type, content, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (telegram_user_id, chat_id, message_id, username, full_name,
                 message_type, content, datetime.now(UTC).isoformat()),
            )
            await db.commit()
            return int(cursor.lastrowid) if cursor.rowcount else None

    async def inbound_messages(self, limit: int = 50, offset: int = 0) -> list[dict[str, object]]:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            rows = await (await db.execute(
                "SELECT * FROM inbound_messages ORDER BY id DESC LIMIT ? OFFSET ?",
                (limit, offset),
            )).fetchall()
            return [dict(row) for row in rows]

    async def inbound_message_count(self) -> int:
        async with aiosqlite.connect(self.path) as db:
            row = await (await db.execute("SELECT COUNT(*) FROM inbound_messages")).fetchone()
            return int(row[0]) if row else 0

    async def profile(self, telegram_user_id: int, include_deleted: bool = False) -> dict[str, object] | None:
        clause = "" if include_deleted else " AND deleted = 0"
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            row = await (await db.execute(
                f"SELECT * FROM employee_profiles WHERE telegram_user_id = ?{clause}",
                (telegram_user_id,),
            )).fetchone()
            return dict(row) if row else None

    async def profiles(self) -> list[dict[str, object]]:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            rows = await (await db.execute(
                "SELECT * FROM employee_profiles WHERE deleted = 0 ORDER BY updated_at DESC"
            )).fetchall()
            return [dict(row) for row in rows]

    async def profile_by_employee_code(self, employee_code: str) -> dict[str, object] | None:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            row = await (await db.execute(
                "SELECT * FROM employee_profiles WHERE employee_code = ? AND deleted = 0",
                (employee_code,),
            )).fetchone()
            return dict(row) if row else None

    async def profile_by_name(self, name: str) -> dict[str, object] | None:
        """花名册填写规范：忽略候选人编码，"姓名/简历名" == 候选人姓名 才是唯一识别符。
        Matches an existing (non-deleted) profile whose stored chinese_name or
        resume_name equals the given candidate name (trimmed, case-insensitive).
        Returns the most recently updated match if more than one row somehow ties."""
        lookup_keys = person_name_keys(name)
        if not lookup_keys:
            return None
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            rows = await (await db.execute(
                "SELECT * FROM employee_profiles WHERE deleted = 0"
            )).fetchall()
        matches = [
            dict(row) for row in rows
            if lookup_keys & (
                person_name_keys(str(row["resume_name"] or ""))
                | person_name_keys(str(row["chinese_name"] or ""))
            )
        ]
        if not matches:
            return None
        # Prefer a real Telegram-owned profile carrying a real employee code;
        # synthetic SSC placeholders remain a fallback only.
        matches.sort(
            key=lambda item: (
                bool(str(item.get("employee_code") or "")),
                int(item.get("telegram_user_id") or 0) > 0,
                bool(item.get("sheet_row")),
                str(item.get("updated_at") or ""),
            ),
            reverse=True,
        )
        return matches[0]

    async def save_profile(
        self,
        telegram_user_id: int,
        telegram_username: str | None,
        telegram_full_name: str,
        values: dict[str, str],
        raw_message: str,
    ) -> tuple[dict[str, object], dict[str, tuple[str, str]], bool]:
        old = await self.profile(telegram_user_id, include_deleted=True)
        is_new = old is None or bool(old.get("deleted"))
        merged = {key: str(old.get(key) or "") if old else "" for key in values}
        merged.update(values)
        changes = {
            key: (str(old.get(key) or "") if old else "", value)
            for key, value in values.items()
            if not old or str(old.get(key) or "") != value
        }
        now = datetime.now(UTC).isoformat()
        all_fields = (
            "employee_code", "chinese_name", "resume_name", "employment_status",
            "effective_date", "gender", "age_range", "education", "nationality",
            "birthday_month", "office_region", "work_tg", "private_contact", "work_email",
        )
        complete = {key: (values.get(key) if key in values else str(old.get(key) or "") if old else "") for key in all_fields}
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                f"""
                INSERT INTO employee_profiles
                    (telegram_user_id, telegram_username, telegram_full_name, {', '.join(all_fields)},
                     raw_message, sync_status, deleted, created_at, updated_at)
                VALUES ({', '.join('?' for _ in range(4 + len(all_fields)))}, 'pending', 0, ?, ?)
                ON CONFLICT(telegram_user_id) DO UPDATE SET
                    telegram_username=excluded.telegram_username,
                    telegram_full_name=excluded.telegram_full_name,
                    {', '.join(f'{field}=excluded.{field}' for field in all_fields)},
                    raw_message=excluded.raw_message,
                    sync_status='pending', deleted=0, updated_at=excluded.updated_at
                """,
                (telegram_user_id, telegram_username, telegram_full_name,
                 *(complete[field] for field in all_fields), raw_message, now, now),
            )
            if changes:
                import json
                await db.execute(
                    "INSERT INTO employee_profile_changes (telegram_user_id, change_type, changes, created_at) VALUES (?, ?, ?, ?)",
                    (telegram_user_id, "入职" if is_new else "字段变更", json.dumps(changes, ensure_ascii=False), now),
                )
            await db.commit()
        saved = await self.profile(telegram_user_id)
        assert saved is not None
        return saved, changes, is_new

    async def set_profile_sync(self, telegram_user_id: int, status: str, sheet_row: int | None = None) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "UPDATE employee_profiles SET sync_status = ?, sheet_row = COALESCE(?, sheet_row), updated_at = ? WHERE telegram_user_id = ?",
                (status[:500], sheet_row, datetime.now(UTC).isoformat(), telegram_user_id),
            )
            await db.commit()

    async def delete_profile(self, telegram_user_id: int) -> dict[str, object] | None:
        profile = await self.profile(telegram_user_id)
        if not profile:
            return None
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "UPDATE employee_profiles SET deleted = 1, sync_status = 'delete_pending', updated_at = ? WHERE telegram_user_id = ?",
                (datetime.now(UTC).isoformat(), telegram_user_id),
            )
            await db.commit()
        return profile

    async def upsert_employee(
        self,
        telegram_user_id: int,
        chat_id: int,
        username: str | None,
        full_name: str,
    ) -> None:
        now = datetime.now(UTC).isoformat()
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                """
                INSERT INTO employees
                    (telegram_user_id, chat_id, username, full_name, active, joined_at, last_seen_at)
                VALUES (?, ?, ?, ?, 1, ?, ?)
                ON CONFLICT(telegram_user_id) DO UPDATE SET
                    chat_id=excluded.chat_id,
                    username=excluded.username,
                    full_name=excluded.full_name,
                    active=1,
                    last_seen_at=excluded.last_seen_at
                """,
                (telegram_user_id, chat_id, username, full_name, now, now),
            )
            await db.commit()

    async def is_active_employee(self, telegram_user_id: int) -> bool:
        async with aiosqlite.connect(self.path) as db:
            cursor = await db.execute(
                "SELECT active FROM employees WHERE telegram_user_id = ?",
                (telegram_user_id,),
            )
            row = await cursor.fetchone()
            return bool(row and row[0])

    async def active_employees(self) -> list[Employee]:
        async with aiosqlite.connect(self.path) as db:
            cursor = await db.execute(
                """
                SELECT telegram_user_id, chat_id, username, full_name, active
                FROM employees
                WHERE active = 1
                ORDER BY full_name COLLATE NOCASE, telegram_user_id
                """
            )
            return [
                Employee(
                    telegram_user_id=int(row[0]),
                    chat_id=int(row[1]),
                    username=row[2],
                    full_name=str(row[3]),
                    active=bool(row[4]),
                )
                for row in await cursor.fetchall()
            ]

    async def active_employees_by_ids(self, user_ids: list[int]) -> list[Employee]:
        if not user_ids:
            return []
        placeholders = ",".join("?" for _ in user_ids)
        async with aiosqlite.connect(self.path) as db:
            cursor = await db.execute(
                f"""
                SELECT telegram_user_id, chat_id, username, full_name, active
                FROM employees
                WHERE active = 1 AND telegram_user_id IN ({placeholders})
                ORDER BY full_name COLLATE NOCASE, telegram_user_id
                """,
                user_ids,
            )
            return [
                Employee(
                    telegram_user_id=int(row[0]),
                    chat_id=int(row[1]),
                    username=row[2],
                    full_name=str(row[3]),
                    active=bool(row[4]),
                )
                for row in await cursor.fetchall()
            ]

    async def deactivate_employee(self, telegram_user_id: int) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "UPDATE employees SET active = 0 WHERE telegram_user_id = ?",
                (telegram_user_id,),
            )
            await db.commit()

    async def employee_count(self) -> int:
        async with aiosqlite.connect(self.path) as db:
            cursor = await db.execute("SELECT COUNT(*) FROM employees WHERE active = 1")
            row = await cursor.fetchone()
            return int(row[0]) if row else 0

    async def save_broadcast(
        self, admin_user_id: int, content: str, success_count: int, failed_count: int
    ) -> int:
        async with aiosqlite.connect(self.path) as db:
            cursor = await db.execute(
                """
                INSERT INTO broadcasts
                    (admin_user_id, content, created_at, success_count, failed_count)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    admin_user_id,
                    content,
                    datetime.now(UTC).isoformat(),
                    success_count,
                    failed_count,
                ),
            )
            await db.commit()
            return int(cursor.lastrowid or 0)

    async def save_feedback(
        self,
        telegram_user_id: int,
        question: str,
        answer: str,
        topic: str,
        helpful: bool | None,
    ) -> int:
        async with aiosqlite.connect(self.path) as db:
            cursor = await db.execute(
                """
                INSERT INTO feedback
                    (telegram_user_id, question, answer, topic, helpful, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    telegram_user_id,
                    question,
                    answer,
                    topic,
                    None if helpful is None else int(helpful),
                    datetime.now(UTC).isoformat(),
                ),
            )
            await db.commit()
            return int(cursor.lastrowid or 0)

    async def topic_stats(self, limit: int = 20) -> list[tuple[str, int, int]]:
        async with aiosqlite.connect(self.path) as db:
            cursor = await db.execute(
                """
                SELECT topic, COUNT(*), COUNT(DISTINCT telegram_user_id)
                FROM feedback
                WHERE topic IS NOT NULL AND TRIM(topic) != ''
                GROUP BY topic
                ORDER BY COUNT(*) DESC, MAX(id) DESC
                LIMIT ?
                """,
                (limit,),
            )
            return [(str(row[0]), int(row[1]), int(row[2])) for row in await cursor.fetchall()]

    async def questions_for_topic(self, topic: str, limit: int = 10) -> list[str]:
        async with aiosqlite.connect(self.path) as db:
            cursor = await db.execute(
                """
                SELECT question FROM feedback
                WHERE topic = ?
                ORDER BY id DESC LIMIT ?
                """,
                (topic, limit),
            )
            return [str(row[0]) for row in await cursor.fetchall()]

    async def employees_for_topic(self, topic: str) -> list[Employee]:
        async with aiosqlite.connect(self.path) as db:
            cursor = await db.execute(
                """
                SELECT DISTINCT e.telegram_user_id, e.chat_id, e.username, e.full_name, e.active
                FROM employees e
                JOIN feedback f ON f.telegram_user_id = e.telegram_user_id
                WHERE e.active = 1 AND f.topic = ?
                ORDER BY e.full_name COLLATE NOCASE, e.telegram_user_id
                """,
                (topic,),
            )
            return [
                Employee(int(row[0]), int(row[1]), row[2], str(row[3]), bool(row[4]))
                for row in await cursor.fetchall()
            ]

    async def save_topic_answer(
        self, topic: str, answer: str, admin_user_id: int, recipient_count: int
    ) -> int:
        async with aiosqlite.connect(self.path) as db:
            cursor = await db.execute(
                """
                INSERT INTO topic_answers
                    (topic, answer, admin_user_id, recipient_count, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (topic, answer, admin_user_id, recipient_count, datetime.now(UTC).isoformat()),
            )
            await db.commit()
            return int(cursor.lastrowid or 0)

    async def upsert_template(self, name: str, content: str, auto_send: bool) -> int:
        now = datetime.now(UTC).isoformat()
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                """
                INSERT INTO notification_templates
                    (name, content, auto_send_to_new, active, created_at, updated_at)
                VALUES (?, ?, ?, 1, ?, ?)
                ON CONFLICT(name) DO UPDATE SET
                    content=excluded.content,
                    auto_send_to_new=excluded.auto_send_to_new,
                    active=1,
                    updated_at=excluded.updated_at
                """,
                (name, content, int(auto_send), now, now),
            )
            await db.commit()
            cursor = await db.execute(
                "SELECT id FROM notification_templates WHERE name = ?", (name,)
            )
            row = await cursor.fetchone()
            return int(row[0])

    async def templates(self, auto_only: bool = False) -> list[tuple[int, str, str, bool]]:
        sql = """
            SELECT id, name, content, auto_send_to_new
            FROM notification_templates
            WHERE active = 1
        """
        if auto_only:
            sql += " AND auto_send_to_new = 1"
        sql += " ORDER BY name COLLATE NOCASE"
        async with aiosqlite.connect(self.path) as db:
            cursor = await db.execute(sql)
            return [
                (int(row[0]), str(row[1]), str(row[2]), bool(row[3]))
                for row in await cursor.fetchall()
            ]

    async def template(self, template_id: int) -> tuple[int, str, str, bool] | None:
        async with aiosqlite.connect(self.path) as db:
            cursor = await db.execute(
                """
                SELECT id, name, content, auto_send_to_new
                FROM notification_templates WHERE id = ? AND active = 1
                """,
                (template_id,),
            )
            row = await cursor.fetchone()
            if not row:
                return None
            return int(row[0]), str(row[1]), str(row[2]), bool(row[3])

    async def template_by_name(self, name: str) -> tuple[int, str, str, bool] | None:
        async with aiosqlite.connect(self.path) as db:
            cursor = await db.execute(
                """
                SELECT id, name, content, auto_send_to_new
                FROM notification_templates
                WHERE name = ? AND active = 1
                """,
                (name,),
            )
            row = await cursor.fetchone()
            if not row:
                return None
            return int(row[0]), str(row[1]), str(row[2]), bool(row[3])

    async def delete_template(self, template_id: int) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "UPDATE notification_templates SET active = 0, updated_at = ? WHERE id = ?",
                (datetime.now(UTC).isoformat(), template_id),
            )
            await db.commit()

    async def set_feedback(self, feedback_id: int, helpful: bool) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "UPDATE feedback SET helpful = ? WHERE id = ?",
                (int(helpful), feedback_id),
            )
            await db.commit()

    async def replace_roster(self, rows: list[dict[str, str]]) -> None:
        now = datetime.now(UTC).isoformat()
        fields = (
            "employee_code", "chinese_name", "resume_name", "employment_status",
            "effective_date", "gender", "age_range", "education", "nationality",
            "birthday_month", "office_region", "work_tg", "private_contact", "work_email",
        )
        async with aiosqlite.connect(self.path) as db:
            await db.execute("DELETE FROM roster")
            await db.executemany(
                f"INSERT INTO roster ({', '.join(fields)}) VALUES ({', '.join('?' for _ in fields)})",
                [tuple(row.get(field, "") for field in fields) for row in rows],
            )
            await db.execute(
                "INSERT INTO roster_sync_log (synced_at, row_count) VALUES (?, ?)",
                (now, len(rows)),
            )
            await db.commit()

    async def roster(self, limit: int = 500, offset: int = 0) -> list[dict[str, object]]:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            rows = await (await db.execute(
                "SELECT * FROM roster ORDER BY employee_code COLLATE NOCASE LIMIT ? OFFSET ?",
                (limit, offset),
            )).fetchall()
            return [dict(row) for row in rows]

    async def roster_count(self) -> int:
        async with aiosqlite.connect(self.path) as db:
            row = await (await db.execute("SELECT COUNT(*) FROM roster")).fetchone()
            return int(row[0]) if row else 0

    async def roster_last_sync(self) -> dict[str, object] | None:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            row = await (await db.execute(
                "SELECT * FROM roster_sync_log ORDER BY id DESC LIMIT 1"
            )).fetchone()
            return dict(row) if row else None
