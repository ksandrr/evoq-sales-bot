import os
import sqlite3
from datetime import datetime
from contextlib import contextmanager

DB_PATH = os.environ.get("DB_PATH", "ideas.db")


@contextmanager
def _conn():
    conn = sqlite3.connect(DB_PATH)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _table_columns(c, table: str):
    return [row[1] for row in c.execute(f"PRAGMA table_info({table})").fetchall()]


def init_db():
    parent = os.path.dirname(DB_PATH)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with _conn() as c:
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS ideas (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                brief TEXT NOT NULL,
                details TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY
            )
            """
        )

        user_cols = _table_columns(c, "users")
        if "timezone" not in user_cols:
            c.execute("ALTER TABLE users ADD COLUMN timezone TEXT NOT NULL DEFAULT 'UTC'")

        c.execute(
            """
            CREATE TABLE IF NOT EXISTS reminders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                hour INTEGER NOT NULL,
                minute INTEGER NOT NULL DEFAULT 0,
                UNIQUE(user_id, hour, minute)
            )
            """
        )

        c.execute(
            """
            CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                text TEXT NOT NULL,
                done INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            )
            """
        )

        # Миграция: если в старой схеме были reminder_hour/reminder_minute —
        # перенесём их в таблицу reminders, чтобы существующие напоминания не пропали.
        user_cols = _table_columns(c, "users")
        if "reminder_hour" in user_cols and "reminder_minute" in user_cols:
            c.execute(
                """
                INSERT OR IGNORE INTO reminders (user_id, hour, minute)
                SELECT user_id, reminder_hour, reminder_minute
                FROM users
                WHERE reminder_hour IS NOT NULL
                """
            )


def add_idea(user_id: int, brief: str, details: str) -> int:
    with _conn() as c:
        cur = c.execute(
            "INSERT INTO ideas (user_id, brief, details, created_at) VALUES (?, ?, ?, ?)",
            (user_id, brief, details, datetime.utcnow().isoformat()),
        )
        return cur.lastrowid


def get_user_ideas(user_id: int):
    with _conn() as c:
        return c.execute(
            "SELECT id, brief, details FROM ideas WHERE user_id = ? ORDER BY id",
            (user_id,),
        ).fetchall()


def get_idea(idea_id: int, user_id: int):
    with _conn() as c:
        return c.execute(
            "SELECT id, brief, details FROM ideas WHERE id = ? AND user_id = ?",
            (idea_id, user_id),
        ).fetchone()


def delete_idea(idea_id: int, user_id: int):
    with _conn() as c:
        c.execute(
            "DELETE FROM ideas WHERE id = ? AND user_id = ?",
            (idea_id, user_id),
        )


def get_all_user_ids():
    with _conn() as c:
        rows = c.execute("SELECT user_id FROM users").fetchall()
        return [r[0] for r in rows]


def upsert_user(user_id: int, default_hour: int = 10, default_minute: int = 0):
    """Создаёт пользователя, если его нет, и сразу одно дефолтное напоминание."""
    with _conn() as c:
        c.execute(
            "INSERT OR IGNORE INTO users (user_id) VALUES (?)",
            (user_id,),
        )
        # Если у пользователя нет ни одного напоминания — добавляем дефолтное.
        has = c.execute(
            "SELECT 1 FROM reminders WHERE user_id = ? LIMIT 1", (user_id,)
        ).fetchone()
        if not has:
            c.execute(
                "INSERT OR IGNORE INTO reminders (user_id, hour, minute) VALUES (?, ?, ?)",
                (user_id, default_hour, default_minute),
            )


def get_user_timezone(user_id: int) -> str:
    with _conn() as c:
        row = c.execute(
            "SELECT timezone FROM users WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        return row[0] if row and row[0] else "UTC"


def set_user_timezone(user_id: int, tz: str):
    with _conn() as c:
        c.execute(
            "INSERT INTO users (user_id, timezone) VALUES (?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET timezone = excluded.timezone",
            (user_id, tz),
        )


def get_user_reminders(user_id: int):
    with _conn() as c:
        return c.execute(
            "SELECT id, hour, minute FROM reminders WHERE user_id = ? ORDER BY hour, minute",
            (user_id,),
        ).fetchall()


def add_reminder(user_id: int, hour: int, minute: int) -> bool:
    """Возвращает True, если добавлено; False, если уже есть такое же время."""
    with _conn() as c:
        try:
            c.execute(
                "INSERT INTO reminders (user_id, hour, minute) VALUES (?, ?, ?)",
                (user_id, hour, minute),
            )
            return True
        except sqlite3.IntegrityError:
            return False


def delete_reminder(reminder_id: int, user_id: int):
    with _conn() as c:
        c.execute(
            "DELETE FROM reminders WHERE id = ? AND user_id = ?",
            (reminder_id, user_id),
        )


def add_task(user_id: int, text: str) -> int:
    with _conn() as c:
        cur = c.execute(
            "INSERT INTO tasks (user_id, text, done, created_at) VALUES (?, ?, 0, ?)",
            (user_id, text, datetime.utcnow().isoformat()),
        )
        return cur.lastrowid


def get_user_tasks(user_id: int):
    with _conn() as c:
        return c.execute(
            "SELECT id, text, done FROM tasks WHERE user_id = ? ORDER BY done, id",
            (user_id,),
        ).fetchall()


def get_task(task_id: int, user_id: int):
    with _conn() as c:
        return c.execute(
            "SELECT id, text, done FROM tasks WHERE id = ? AND user_id = ?",
            (task_id, user_id),
        ).fetchone()


def set_task_done(task_id: int, user_id: int, done: bool):
    with _conn() as c:
        c.execute(
            "UPDATE tasks SET done = ? WHERE id = ? AND user_id = ?",
            (1 if done else 0, task_id, user_id),
        )


def delete_task(task_id: int, user_id: int):
    with _conn() as c:
        c.execute(
            "DELETE FROM tasks WHERE id = ? AND user_id = ?",
            (task_id, user_id),
        )
