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

    # Observability: Fast-Track Events
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS fast_track_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            user_id TEXT NOT NULL,
            message_preview TEXT,
            trigger_pattern TEXT,
            chunk_type TEXT,
            tags TEXT,
            chunk_id TEXT,
            chunk_text TEXT,
            confidence REAL,
            stored INTEGER NOT NULL DEFAULT 0,
            skip_reason TEXT
        )
    """)

    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_ft_events_ts
        ON fast_track_events(timestamp DESC)
    """)

    # Observability: Konsolidierer Events
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS consolidator_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            run_id TEXT NOT NULL,
            block_index INTEGER NOT NULL DEFAULT 0,
            block_size INTEGER NOT NULL DEFAULT 0,
            turns_count INTEGER NOT NULL DEFAULT 0,
            actions_json TEXT,
            dropped_count INTEGER NOT NULL DEFAULT 0,
            retry_triggered INTEGER NOT NULL DEFAULT 0,
            null_result INTEGER NOT NULL DEFAULT 0,
            error TEXT
        )
    """)

    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_cons_events_ts
        ON consolidator_events(timestamp DESC)
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


def log_fast_track_event(
    user_id,
    message_preview,
    trigger_pattern,
    chunk_type=None,
    tags=None,
    chunk_id=None,
    chunk_text=None,
    confidence=None,
    stored=False,
    skip_reason=None,
):
    """Schreibt ein Fast-Track-Event in die Observability-Tabelle."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        INSERT INTO fast_track_events
            (timestamp, user_id, message_preview, trigger_pattern,
             chunk_type, tags, chunk_id, chunk_text, confidence, stored, skip_reason)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            to_iso(),
            user_id,
            (message_preview or "")[:200],
            trigger_pattern,
            chunk_type,
            ",".join(tags) if tags else None,
            chunk_id,
            (chunk_text or "")[:500],
            confidence,
            1 if stored else 0,
            skip_reason,
        ),
    )
    conn.commit()
    conn.close()


def get_fast_track_events(limit=50, user_id=None):
    """Holt die letzten Fast-Track-Events, optional gefiltert nach user_id."""
    conn = get_connection()
    cursor = conn.cursor()
    if user_id:
        cursor.execute(
            "SELECT * FROM fast_track_events WHERE user_id = ? ORDER BY timestamp DESC, id DESC LIMIT ?",
            (user_id, limit),
        )
    else:
        cursor.execute(
            "SELECT * FROM fast_track_events ORDER BY timestamp DESC, id DESC LIMIT ?",
            (limit,),
        )
    rows = cursor.fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_fast_track_stats():
    """Aggregierte Fast-Track-Statistiken."""
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("SELECT COUNT(*) FROM fast_track_events")
    total = cursor.fetchone()[0]

    cursor.execute("SELECT COUNT(*) FROM fast_track_events WHERE stored = 1")
    stored = cursor.fetchone()[0]

    today = to_iso()[:10]
    cursor.execute(
        "SELECT COUNT(*) FROM fast_track_events WHERE timestamp LIKE ?", (f"{today}%",)
    )
    today_total = cursor.fetchone()[0]

    cursor.execute(
        "SELECT COUNT(*) FROM fast_track_events WHERE stored = 1 AND timestamp LIKE ?",
        (f"{today}%",),
    )
    today_stored = cursor.fetchone()[0]

    cursor.execute(
        "SELECT chunk_type, COUNT(*) as cnt FROM fast_track_events WHERE stored = 1 GROUP BY chunk_type"
    )
    by_type = {row[0]: row[1] for row in cursor.fetchall() if row[0]}

    cursor.execute(
        "SELECT skip_reason, COUNT(*) as cnt FROM fast_track_events WHERE stored = 0 GROUP BY skip_reason"
    )
    by_skip = {row[0] or "unknown": row[1] for row in cursor.fetchall()}

    conn.close()
    return {
        "total": total,
        "stored": stored,
        "skipped": total - stored,
        "today_total": today_total,
        "today_stored": today_stored,
        "by_type": by_type,
        "by_skip": by_skip,
    }


def log_consolidator_event(
    run_id,
    block_index,
    block_size,
    turns_count,
    actions,
    dropped_count=0,
    retry_triggered=False,
    null_result=False,
    error=None,
):
    """Schreibt ein Konsolidierer-Event in die Observability-Tabelle."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        INSERT INTO consolidator_events
            (timestamp, run_id, block_index, block_size, turns_count,
             actions_json, dropped_count, retry_triggered, null_result, error)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            to_iso(),
            run_id,
            block_index,
            block_size,
            turns_count,
            json.dumps(actions, ensure_ascii=False),
            dropped_count,
            1 if retry_triggered else 0,
            1 if null_result else 0,
            error,
        ),
    )
    conn.commit()
    conn.close()


def get_consolidator_events(limit=30):
    """Holt die letzten Konsolidierer-Events."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT * FROM consolidator_events ORDER BY timestamp DESC, id DESC LIMIT ?",
        (limit,),
    )
    rows = cursor.fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_consolidator_stats():
    """Aggregierte Konsolidierer-Statistiken."""
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("SELECT COUNT(*) FROM consolidator_events")
    total_runs = cursor.fetchone()[0]

    today = to_iso()[:10]
    cursor.execute(
        "SELECT COUNT(*) FROM consolidator_events WHERE timestamp LIKE ?", (f"{today}%",)
    )
    today_runs = cursor.fetchone()[0]

    cursor.execute("SELECT COUNT(*) FROM consolidator_events WHERE null_result = 1")
    null_runs = cursor.fetchone()[0]

    cursor.execute("SELECT COUNT(*) FROM consolidator_events WHERE retry_triggered = 1")
    retry_runs = cursor.fetchone()[0]

    cursor.execute("SELECT SUM(dropped_count) FROM consolidator_events")
    total_dropped = cursor.fetchone()[0] or 0

    conn.close()
    return {
        "total_runs": total_runs,
        "today_runs": today_runs,
        "null_runs": null_runs,
        "retry_runs": retry_runs,
        "total_dropped": total_dropped,
    }
