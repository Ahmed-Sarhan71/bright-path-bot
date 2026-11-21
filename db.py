import json
import os
import sqlite3
import time
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
        # Chats where a human has taken over: the bot stays quiet until paused_until.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS handover (
                session_id   TEXT PRIMARY KEY,
                paused_until REAL NOT NULL
            )
            """
        )
        # Ids of messages the bot sent itself, so they are never mistaken for a human reply.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS sent_messages (
                message_id TEXT PRIMARY KEY,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )


def pause_bot(session_id: str, hours: float) -> None:
    """Silence the bot in this chat for the next `hours` hours."""
    with closing(_connect()) as conn, conn:
        conn.execute(
            """
            INSERT INTO handover (session_id, paused_until) VALUES (?, ?)
            ON CONFLICT(session_id) DO UPDATE SET paused_until = excluded.paused_until
            """,
            (session_id, time.time() + hours * 3600),
        )


def is_paused(session_id: str) -> bool:
    with closing(_connect()) as conn:
        row = conn.execute(
            "SELECT paused_until FROM handover WHERE session_id = ?", (session_id,)
        ).fetchone()
    return row is not None and row[0] > time.time()


def record_sent(message_id: str) -> None:
    with closing(_connect()) as conn, conn:
        conn.execute("INSERT OR IGNORE INTO sent_messages (message_id) VALUES (?)", (message_id,))


def was_sent_by_bot(message_id: str) -> bool:
    with closing(_connect()) as conn:
        row = conn.execute(
            "SELECT 1 FROM sent_messages WHERE message_id = ?", (message_id,)
        ).fetchone()
    return row is not None


def add_message(session_id: str, role: str, content: str) -> None:
    """Append one message to a session's history without calling the AI."""
    session = get_session(session_id)
    session["messages"].append({"role": role, "content": content})
    save_session(session_id, session)


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