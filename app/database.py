from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import aiosqlite


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
                    employee_code TEXT NOT NULL UNIQUE,
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
                """
            )
            columns = {
                str(row[1])
                for row in await (await db.execute("PRAGMA table_info(feedback)")).fetchall()
            }
            if "topic" not in columns:
                await db.execute("ALTER TABLE feedback ADD COLUMN topic TEXT")
            await db.execute(
                "UPDATE feedback SET topic = '历史问题' WHERE topic IS NULL OR TRIM(topic) = ''"
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
