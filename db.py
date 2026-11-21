import json
import os
import sqlite3
from contextlib import closing


def _db_path() -> str:
    # On Railway, point this at the mounted volume, e.g. /data/sessions.db
    return os.getenv("DB_PATH", "sessions.db")


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(_db_path(), timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")  # lets reads and writes overlap
    return conn


def init_db() -> None:
    """Create the sessions table if it does not exist yet."""
    folder = os.path.dirname(_db_path())
    if folder:
        os.makedirs(folder, exist_ok=True)
    with closing(_connect()) as conn, conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS sessions (
                session_id TEXT PRIMARY KEY,
                messages   TEXT NOT NULL,
                saved      INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        # WhatsApp message ids we already handled (Meta retries webhooks).
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS processed_messages (
                message_id TEXT PRIMARY KEY,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )


def claim_message(message_id: str) -> bool:
    """Return True the first time a message id is seen, False for repeats."""
    with closing(_connect()) as conn, conn:
        cursor = conn.execute(
            "INSERT OR IGNORE INTO processed_messages (message_id) VALUES (?)", (message_id,)
        )
        return cursor.rowcount == 1


def get_session(session_id: str) -> dict:
    """Load a session, or return a new empty one."""
    with closing(_connect()) as conn:
        row = conn.execute(
            "SELECT messages, saved FROM sessions WHERE session_id = ?", (session_id,)
        ).fetchone()
    if row is None:
        return {"messages": [], "saved": False}
    return {"messages": json.loads(row[0]), "saved": bool(row[1])}


def save_session(session_id: str, session: dict) -> None:
    """Insert or update a session."""
    with closing(_connect()) as conn, conn:
        conn.execute(
            """
            INSERT INTO sessions (session_id, messages, saved, updated_at)
            VALUES (?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(session_id) DO UPDATE SET
                messages = excluded.messages,
                saved = excluded.saved,
                updated_at = CURRENT_TIMESTAMP
            """,
            (session_id, json.dumps(session["messages"], ensure_ascii=False), int(session["saved"])),
        )