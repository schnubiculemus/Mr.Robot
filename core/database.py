import sqlite3
import json
import logging
from config import DB_PATH, MAX_CONTEXT_MESSAGES
from core.datetime_utils import to_iso

logger = logging.getLogger(__name__)

# SQLite Timeout: wartet bis zu 10s wenn die DB gelockt ist (statt sofort zu crashen).
# Relevant weil Webhook-Thread, Chat-Thread und Fast-Track-Thread gleichzeitig schreiben.
DB_TIMEOUT = 10


def get_connection():
    """Erstellt eine SQLite-Verbindung (thread-safe, mit Timeout)."""
    conn = sqlite3.connect(DB_PATH, timeout=DB_TIMEOUT)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    """Erstellt die Tabellen und aktiviert WAL-Modus."""
    conn = get_connection()
    cursor = conn.cursor()

    # WAL-Modus: erlaubt gleichzeitige Reads während ein Write läuft.
    # Ohne WAL blockiert jeder Write ALLE Reads. Mit WAL blockiert nur Write-Write.
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA busy_timeout=10000")

    # DEFAULTs als Fallback: strftime gibt UTC-ISO ohne Timezone-Suffix.
    # Alle expliziten Inserts nutzen Python-seitige to_iso() mit +00:00 (P1.6).
    # DEFAULTs greifen nur falls ein Insert ohne Timestamp-Feld durchkommt.
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            phone_number TEXT PRIMARY KEY,
            display_name TEXT,
            profile_file TEXT,
            created_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now')),
            updated_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now'))
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            phone_number TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            timestamp TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now')),
            FOREIGN KEY (phone_number) REFERENCES users(phone_number)
        )
    """)

    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_messages_phone
        ON messages(phone_number, timestamp DESC)
    """)

    conn.commit()
    conn.close()
    logger.info("Datenbank initialisiert (WAL-Modus)")


def get_or_create_user(phone_number, display_name=None):
    """Holt einen User oder erstellt ihn."""
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("SELECT * FROM users WHERE phone_number = ?", (phone_number,))
    user = cursor.fetchone()

    if not user:
        now = to_iso()
        cursor.execute(
            "INSERT INTO users (phone_number, display_name, created_at, updated_at) VALUES (?, ?, ?, ?)",
            (phone_number, display_name or "Unbekannt", now, now),
        )
        conn.commit()
        cursor.execute("SELECT * FROM users WHERE phone_number = ?", (phone_number,))
        user = cursor.fetchone()

    conn.close()
    return dict(user)


def save_message(phone_number, role, content):
    """Speichert eine Nachricht (role: 'user' oder 'assistant')."""
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute(
        "INSERT INTO messages (phone_number, role, content, timestamp) VALUES (?, ?, ?, ?)",
        (phone_number, role, content, to_iso()),
    )

    conn.commit()
    conn.close()


def get_chat_history(phone_number, limit=MAX_CONTEXT_MESSAGES):
    """Holt die letzten N Nachrichten für den Kontext."""
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute(
        """
        SELECT role, content FROM messages
        WHERE phone_number = ?
        ORDER BY timestamp DESC, id DESC
        LIMIT ?
        """,
        (phone_number, limit),
    )

    rows = cursor.fetchall()
    conn.close()

    # Umkehren, damit die älteste Nachricht zuerst kommt
    messages = [{"role": row["role"], "content": row["content"]} for row in reversed(rows)]
    return messages


def set_user_profile(phone_number, profile_file):
    """Verknüpft einen User mit einer Profil-Datei."""
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute(
        "UPDATE users SET profile_file = ?, updated_at = ? WHERE phone_number = ?",
        (profile_file, to_iso(), phone_number),
    )

    conn.commit()
    conn.close()
