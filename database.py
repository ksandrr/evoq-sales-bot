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
                user_id INTEGER PRIMARY KEY,
                reminder_hour INTEGER NOT NULL DEFAULT 10,
                reminder_minute INTEGER NOT NULL DEFAULT 0
            )
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


def upsert_user(user_id: int):
    with _conn() as c:
        c.execute(
            "INSERT OR IGNORE INTO users (user_id) VALUES (?)",
            (user_id,),
        )


def get_user_time(user_id: int):
    with _conn() as c:
        row = c.execute(
            "SELECT reminder_hour, reminder_minute FROM users WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        return row if row else (10, 0)


def update_user_time(user_id: int, hour: int, minute: int):
    with _conn() as c:
        c.execute(
            """
            INSERT INTO users (user_id, reminder_hour, reminder_minute)
            VALUES (?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                reminder_hour = excluded.reminder_hour,
                reminder_minute = excluded.reminder_minute
            """,
            (user_id, hour, minute),
        )
