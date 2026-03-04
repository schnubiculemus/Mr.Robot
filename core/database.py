import sqlite3
import json
from datetime import datetime
from config import DB_PATH, MAX_CONTEXT_MESSAGES


def get_connection():
    """Erstellt eine SQLite-Verbindung."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    """Erstellt die Tabellen, falls sie noch nicht existieren."""
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            phone_number TEXT PRIMARY KEY,
            display_name TEXT,
            profile_file TEXT,
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now'))
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            phone_number TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            timestamp TEXT DEFAULT (datetime('now')),
            FOREIGN KEY (phone_number) REFERENCES users(phone_number)
        )
    """)

    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_messages_phone
        ON messages(phone_number, timestamp DESC)
    """)

    conn.commit()
    conn.close()


def get_or_create_user(phone_number, display_name=None):
    """Holt einen User oder erstellt ihn."""
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("SELECT * FROM users WHERE phone_number = ?", (phone_number,))
    user = cursor.fetchone()

    if not user:
        cursor.execute(
            "INSERT INTO users (phone_number, display_name) VALUES (?, ?)",
            (phone_number, display_name or "Unbekannt"),
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
        "INSERT INTO messages (phone_number, role, content) VALUES (?, ?, ?)",
        (phone_number, role, content),
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
        ORDER BY timestamp DESC
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
        "UPDATE users SET profile_file = ?, updated_at = datetime('now') WHERE phone_number = ?",
        (profile_file, phone_number),
    )

    conn.commit()
    conn.close()
