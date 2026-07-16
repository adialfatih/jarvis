import json
import sqlite3
import threading
import time
from pathlib import Path


DB_PATH = Path(__file__).resolve().parent.parent / "jarvis.db"

_lock = threading.Lock()
_conn: sqlite3.Connection | None = None


def _connection() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS chat_sessions (
                id TEXT PRIMARY KEY,
                project_path TEXT NOT NULL,
                project_name TEXT NOT NULL,
                claude_session_id TEXT,
                status TEXT NOT NULL,
                last_prompt TEXT,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS chat_events (
                session_id TEXT NOT NULL,
                seq INTEGER NOT NULL,
                data TEXT NOT NULL,
                PRIMARY KEY (session_id, seq)
            );
            CREATE TABLE IF NOT EXISTS push_subscriptions (
                endpoint TEXT PRIMARY KEY,
                data TEXT NOT NULL,
                created_at REAL NOT NULL
            );
            """
        )
        _conn.commit()
    return _conn


def upsert_chat_session(info: dict):
    with _lock:
        conn = _connection()
        conn.execute(
            """
            INSERT INTO chat_sessions (id, project_path, project_name, claude_session_id,
                                       status, last_prompt, created_at, updated_at)
            VALUES (:id, :project_path, :project_name, :claude_session_id,
                    :status, :last_prompt, :created_at, :updated_at)
            ON CONFLICT(id) DO UPDATE SET
                claude_session_id = excluded.claude_session_id,
                status = excluded.status,
                last_prompt = excluded.last_prompt,
                updated_at = excluded.updated_at
            """,
            {**info, "updated_at": time.time()},
        )
        conn.commit()


def append_chat_event(session_id: str, seq: int, event: dict):
    with _lock:
        conn = _connection()
        conn.execute(
            "INSERT OR REPLACE INTO chat_events (session_id, seq, data) VALUES (?, ?, ?)",
            (session_id, seq, json.dumps(event, ensure_ascii=False)),
        )
        conn.commit()


def load_chat_events(session_id: str) -> list[dict]:
    with _lock:
        rows = _connection().execute(
            "SELECT data FROM chat_events WHERE session_id = ? ORDER BY seq", (session_id,)
        ).fetchall()
    return [json.loads(row[0]) for row in rows]


def get_chat_session(session_id: str) -> dict | None:
    with _lock:
        row = _connection().execute(
            "SELECT id, project_path, project_name, claude_session_id, status, last_prompt, created_at, updated_at "
            "FROM chat_sessions WHERE id = ?",
            (session_id,),
        ).fetchone()
    return _row_to_dict(row) if row else None


def recent_chat_sessions(limit: int = 20) -> list[dict]:
    with _lock:
        rows = _connection().execute(
            "SELECT id, project_path, project_name, claude_session_id, status, last_prompt, created_at, updated_at "
            "FROM chat_sessions ORDER BY updated_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [_row_to_dict(row) for row in rows]


def delete_chat_session(session_id: str):
    with _lock:
        conn = _connection()
        conn.execute("DELETE FROM chat_sessions WHERE id = ?", (session_id,))
        conn.execute("DELETE FROM chat_events WHERE session_id = ?", (session_id,))
        conn.commit()


def save_push_subscription(subscription: dict):
    with _lock:
        conn = _connection()
        conn.execute(
            "INSERT OR REPLACE INTO push_subscriptions (endpoint, data, created_at) VALUES (?, ?, ?)",
            (subscription.get("endpoint", ""), json.dumps(subscription), time.time()),
        )
        conn.commit()


def delete_push_subscription(endpoint: str):
    with _lock:
        conn = _connection()
        conn.execute("DELETE FROM push_subscriptions WHERE endpoint = ?", (endpoint,))
        conn.commit()


def list_push_subscriptions() -> list[dict]:
    with _lock:
        rows = _connection().execute("SELECT data FROM push_subscriptions").fetchall()
    return [json.loads(row[0]) for row in rows]


def _row_to_dict(row) -> dict:
    return {
        "id": row[0],
        "project_path": row[1],
        "project": row[2],
        "claude_session_id": row[3],
        "status": row[4],
        "last_prompt": row[5],
        "created_at": row[6],
        "updated_at": row[7],
    }
