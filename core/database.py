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

    # MIRROR: Turn-Logging
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS mirror_turns (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            turn_id TEXT UNIQUE NOT NULL,
            timestamp TEXT NOT NULL,
            user_id TEXT NOT NULL,
            user_message_preview TEXT,
            active_chunks_json TEXT,
            rule_stack_json TEXT,
            response_profile_json TEXT,
            preflight_status TEXT,
            preflight_issues_json TEXT,
            pattern_flags_json TEXT
        )
    """)

    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_mirror_turns_ts
        ON mirror_turns(timestamp DESC)
    """)

    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_mirror_turns_user
        ON mirror_turns(user_id, timestamp DESC)
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


# =============================================================================
# Soul Proposals
# =============================================================================

def init_soul_proposals_table():
    """Erstellt die soul_proposals Tabelle falls nicht vorhanden."""
    conn = get_connection()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS soul_proposals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            proposal TEXT NOT NULL,
            reflections_used INTEGER NOT NULL DEFAULT 0,
            diary_entries_used INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'open',
            status_changed_at TEXT
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_soul_proposals_ts ON soul_proposals(timestamp DESC)")

    # Heartbeat-Timeline
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS heartbeat_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL UNIQUE,
            user_id TEXT NOT NULL,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            steps_json TEXT,
            summary TEXT,
            had_error INTEGER NOT NULL DEFAULT 0
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_hb_runs_ts ON heartbeat_runs(started_at DESC)")

    # Search Log
    conn.execute("""
        CREATE TABLE IF NOT EXISTS search_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            user_id TEXT NOT NULL,
            query TEXT NOT NULL,
            success INTEGER NOT NULL DEFAULT 1,
            result_length INTEGER,
            user_message_preview TEXT
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_search_log_ts ON search_log(timestamp DESC)")

    conn.execute("""
        CREATE TABLE IF NOT EXISTS moltbook_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            user_id TEXT NOT NULL,
            query TEXT NOT NULL,
            result_count INTEGER NOT NULL DEFAULT 0,
            post_titles TEXT,
            reflection_preview TEXT
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_moltbook_log_ts ON moltbook_log(timestamp DESC)")

    # Proposed Patterns: Kimi-eigene Verhaltenshypothesen
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS proposed_patterns (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chunk_id TEXT UNIQUE NOT NULL,
            created_at TEXT NOT NULL,
            name TEXT NOT NULL,
            description TEXT NOT NULL,
            evidence TEXT NOT NULL,
            occurrences INTEGER NOT NULL DEFAULT 1,
            last_seen TEXT NOT NULL,
            confidence REAL NOT NULL DEFAULT 0.5,
            status TEXT NOT NULL DEFAULT 'open',
            status_changed_at TEXT,
            promoted_to TEXT
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_proposed_patterns_status ON proposed_patterns(status)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_proposed_patterns_ts ON proposed_patterns(created_at DESC)")

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS moltbook_posts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            post_id TEXT UNIQUE NOT NULL,
            created_at TEXT NOT NULL,
            title TEXT NOT NULL,
            content TEXT,
            submolt TEXT DEFAULT 'general',
            triggered_by TEXT,
            upvotes INTEGER DEFAULT 0,
            comment_count INTEGER DEFAULT 0,
            last_checked TEXT
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_moltbook_posts_ts ON moltbook_posts(created_at DESC)")

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS moltbook_inbox (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            received_at TEXT NOT NULL,
            post_id TEXT NOT NULL,
            post_title TEXT,
            author TEXT NOT NULL,
            content TEXT NOT NULL,
            comment_id TEXT,
            processed INTEGER DEFAULT 0,
            direction TEXT DEFAULT 'in'
        )
    """)
    # Migration: add direction column if missing
    try:
        conn.execute("ALTER TABLE moltbook_inbox ADD COLUMN direction TEXT DEFAULT 'in'")
        conn.commit()
    except Exception:
        pass
    conn.execute("CREATE INDEX IF NOT EXISTS idx_moltbook_inbox_ts ON moltbook_inbox(received_at DESC)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_moltbook_inbox_post ON moltbook_inbox(post_id)")

    # Todos
    from core.todos import init_todos_table
    init_todos_table(conn)

    conn.commit()
    conn.close()


def save_soul_proposal(proposal, reflections_used=0, diary_entries_used=0):
    """Speichert einen neuen Soul-Vorschlag."""
    conn = get_connection()
    conn.execute(
        """INSERT INTO soul_proposals
           (timestamp, proposal, reflections_used, diary_entries_used, status)
           VALUES (?, ?, ?, ?, 'open')""",
        (to_iso(), proposal, reflections_used, diary_entries_used),
    )
    conn.commit()
    conn.close()


def get_soul_proposals(limit=20, status=None):
    """Lädt Soul-Vorschläge, optional nach Status gefiltert."""
    conn = get_connection()
    if status:
        rows = conn.execute(
            "SELECT * FROM soul_proposals WHERE status = ? ORDER BY timestamp DESC LIMIT ?",
            (status, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM soul_proposals ORDER BY timestamp DESC LIMIT ?",
            (limit,),
        ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def update_soul_proposal_status(proposal_id, status):
    """Ändert den Status eines Vorschlags (open/adopted/rejected)."""
    conn = get_connection()
    conn.execute(
        "UPDATE soul_proposals SET status = ?, status_changed_at = ? WHERE id = ?",
        (status, to_iso(), proposal_id),
    )
    conn.commit()
    conn.close()


# =============================================================================
# MIRROR: Turn-Logging
# =============================================================================

def save_mirror_turn(turn: dict) -> None:
    """Speichert ein MIRROR Turn-Objekt."""
    import json
    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("""
            INSERT OR REPLACE INTO mirror_turns (
                turn_id, timestamp, user_id, user_message_preview,
                active_chunks_json, rule_stack_json, response_profile_json,
                preflight_status, preflight_issues_json, pattern_flags_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            turn["turn_id"],
            turn["timestamp"],
            turn["user_id"],
            turn.get("user_message_preview", ""),
            json.dumps(turn.get("active_chunks", []), ensure_ascii=False),
            json.dumps(turn.get("rule_stack", []), ensure_ascii=False),
            json.dumps(turn.get("response_profile", {}), ensure_ascii=False),
            turn.get("preflight", {}).get("status", "green"),
            json.dumps(turn.get("preflight", {}).get("issues", []), ensure_ascii=False),
            json.dumps(turn.get("pattern_flags", []), ensure_ascii=False),
        ))
        conn.commit()
    except Exception as e:
        logger.warning(f"save_mirror_turn Fehler: {e}")
    finally:
        conn.close()


def get_mirror_turns(limit: int = 50, user_id: str = None) -> list:
    """Holt die letzten N Turn-Objekte, optional gefiltert nach User."""
    import json
    conn = get_connection()
    cursor = conn.cursor()
    if user_id:
        cursor.execute(
            "SELECT * FROM mirror_turns WHERE user_id = ? ORDER BY timestamp DESC LIMIT ?",
            (user_id, limit)
        )
    else:
        cursor.execute(
            "SELECT * FROM mirror_turns ORDER BY timestamp DESC LIMIT ?",
            (limit,)
        )
    rows = cursor.fetchall()
    conn.close()

    result = []
    for row in rows:
        result.append({
            "turn_id": row["turn_id"],
            "timestamp": row["timestamp"],
            "user_id": row["user_id"],
            "user_message_preview": row["user_message_preview"],
            "active_chunks": json.loads(row["active_chunks_json"] or "[]"),
            "rule_stack": json.loads(row["rule_stack_json"] or "[]"),
            "response_profile": json.loads(row["response_profile_json"] or "{}"),
            "preflight": {
                "status": row["preflight_status"],
                "issues": json.loads(row["preflight_issues_json"] or "[]"),
            },
            "pattern_flags": json.loads(row["pattern_flags_json"] or "[]"),
        })
    return result


def get_mirror_stats(days: int = 7) -> dict:
    """
    Aggregierte Pattern-Statistiken der letzten N Tage.
    Enthält zusätzlich:
    - trend: Vergleich dieser Woche vs. vorherige Woche (bad_pct delta)
    - topic_correlation: Keywords aus user_message_preview bei schlechten Turns
    """
    import json
    from datetime import timedelta
    from core.datetime_utils import now_utc
    now = now_utc()
    since = (now - timedelta(days=days)).isoformat()
    prev_since = (now - timedelta(days=days * 2)).isoformat()

    conn = get_connection()
    cursor = conn.cursor()

    # Aktuelle Periode
    cursor.execute(
        "SELECT pattern_flags_json, preflight_status, user_message_preview FROM mirror_turns WHERE timestamp >= ? ORDER BY timestamp DESC",
        (since,)
    )
    rows = cursor.fetchall()

    # Vorherige Periode (für Trend)
    cursor.execute(
        "SELECT preflight_status FROM mirror_turns WHERE timestamp >= ? AND timestamp < ?",
        (prev_since, since)
    )
    prev_rows = cursor.fetchall()
    conn.close()

    pattern_counts = {}
    status_counts = {"green": 0, "yellow": 0, "orange": 0, "red": 0}
    total = len(rows)

    # Themen-Korrelation: Keywords aus schlechten Turns sammeln
    bad_turn_words = {}
    STOPWORDS = {"ich", "du", "er", "sie", "es", "wir", "ihr", "die", "der", "das",
                 "ein", "eine", "und", "oder", "aber", "mit", "bei", "für", "von",
                 "zu", "in", "an", "auf", "ist", "bin", "hat", "hab", "kann", "wie",
                 "was", "wo", "wann", "bitte", "noch", "mal", "auch", "dann", "schon",
                 "nicht", "kein", "keine", "mein", "dein", "sein", "ihren", "haben"}

    for row in rows:
        status = row["preflight_status"] or "green"
        status_counts[status] = status_counts.get(status, 0) + 1
        flags = json.loads(row["pattern_flags_json"] or "[]")
        for flag in flags:
            pid = flag.get("type", "?")
            pattern_counts[pid] = pattern_counts.get(pid, 0) + 1

        # Keywords aus schlechten Turns
        if status in ("orange", "red") and row["user_message_preview"]:
            words = row["user_message_preview"].lower().split()
            for w in words:
                w = w.strip(".,!?:;\"'()[]")
                if len(w) > 3 and w not in STOPWORDS:
                    bad_turn_words[w] = bad_turn_words.get(w, 0) + 1

    # Top-Keywords sortiert
    topic_correlation = sorted(bad_turn_words.items(), key=lambda x: -x[1])[:10]

    # Trend berechnen
    prev_total = len(prev_rows)
    prev_bad = sum(1 for r in prev_rows if (r["preflight_status"] or "green") in ("orange", "red"))
    curr_bad = status_counts.get("orange", 0) + status_counts.get("red", 0)
    curr_bad_pct = round(curr_bad / max(total, 1) * 100)
    prev_bad_pct = round(prev_bad / max(prev_total, 1) * 100)
    trend_delta = curr_bad_pct - prev_bad_pct  # positiv = schlechter, negativ = besser

    return {
        "total_turns": total,
        "days": days,
        "preflight_distribution": status_counts,
        "pattern_counts": pattern_counts,
        "trend": {
            "current_bad_pct": curr_bad_pct,
            "prev_bad_pct": prev_bad_pct,
            "delta": trend_delta,
            "prev_total_turns": prev_total,
            "direction": "worse" if trend_delta > 5 else "better" if trend_delta < -5 else "stable",
        },
        "topic_correlation": [{"word": w, "count": c} for w, c in topic_correlation],
    }


def get_chunk_genealogy() -> list:
    """
    Memory Genealogy: Aggregiert alle Chunks aus mirror_turns.
    Gibt pro Chunk zurück:
      - wie oft gezogen (appearances)
      - in wie vielen Turns mit Flags (flagged_turns)
      - in wie vielen Turns mit Preflight rot/orange (bad_turns)
      - zuletzt gesehen (last_seen)
      - zuerst gesehen (first_seen)
      - preview + type
    """
    import json
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT active_chunks_json, rule_stack_json, pattern_flags_json, preflight_status, timestamp FROM mirror_turns ORDER BY timestamp ASC"
    )
    rows = cursor.fetchall()
    conn.close()

    chunk_stats = {}  # chunk_id -> stats dict

    for row in rows:
        ts = row["timestamp"]
        status = row["preflight_status"] or "green"
        flags = json.loads(row["pattern_flags_json"] or "[]")
        has_flags = len(flags) > 0
        is_bad = status in ("orange", "red")

        chunks = json.loads(row["active_chunks_json"] or "[]")
        rules = json.loads(row["rule_stack_json"] or "[]")

        for c in chunks + rules:
            cid = c.get("id", "?")
            if cid == "?":
                continue
            if cid not in chunk_stats:
                chunk_stats[cid] = {
                    "id": cid,
                    "type": c.get("type", "?"),
                    "preview": c.get("preview", ""),
                    "appearances": 0,
                    "flagged_turns": 0,
                    "bad_turns": 0,
                    "first_seen": ts,
                    "last_seen": ts,
                    "scores": [],
                }
            s = chunk_stats[cid]
            s["appearances"] += 1
            if has_flags:
                s["flagged_turns"] += 1
            if is_bad:
                s["bad_turns"] += 1
            if ts > s["last_seen"]:
                s["last_seen"] = ts
            score = c.get("score", 0.0)
            if score:
                s["scores"].append(score)

    result = []
    for s in chunk_stats.values():
        scores = s.pop("scores", [])
        s["avg_score"] = round(sum(scores) / len(scores), 3) if scores else 0.0
        s["flag_rate"] = round(s["flagged_turns"] / max(s["appearances"], 1), 2)
        result.append(s)

    result.sort(key=lambda x: x["appearances"], reverse=True)
    return result


def get_chunk_trust_scores() -> dict:
    """
    Berechnet einen Trust-Score pro Chunk-ID basierend auf Turn-History.

    Trust = Anteil der Turns in denen der Chunk aktiv war und das Preflight
    grün war. Chunks die oft in roten/orangen Turns auftauchen bekommen
    einen niedrigen Trust-Score.

    Returns:
        Dict: chunk_id → trust_score (0.0 - 1.0)
        Nur Chunks mit mindestens 3 Appearances werden berücksichtigt.
    """
    import json
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT active_chunks_json, preflight_status FROM mirror_turns ORDER BY timestamp DESC LIMIT 500"
    )
    rows = cursor.fetchall()
    conn.close()

    stats = {}  # chunk_id → {total: int, good: int}

    for row in rows:
        chunks = json.loads(row["active_chunks_json"] or "[]")
        status = row["preflight_status"] or "green"
        is_good = status == "green"

        for c in chunks:
            cid = c.get("id")
            if not cid:
                continue
            if cid not in stats:
                stats[cid] = {"total": 0, "good": 0}
            stats[cid]["total"] += 1
            if is_good:
                stats[cid]["good"] += 1

    result = {}
    for cid, s in stats.items():
        if s["total"] < 3:
            continue  # zu wenig Daten
        result[cid] = round(s["good"] / s["total"], 3)

    return result


def save_search_log(user_id: str, query: str, success: bool, result_length: int = 0, user_message_preview: str = "") -> None:
    """Speichert eine Web-Search-Anfrage von Schnubot."""
    from core.datetime_utils import now_utc
    conn = get_connection()
    conn.execute(
        """INSERT INTO search_log (timestamp, user_id, query, success, result_length, user_message_preview)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (
            now_utc().isoformat(),
            user_id,
            query[:500],
            1 if success else 0,
            result_length,
            user_message_preview[:120],
        )
    )
    conn.commit()
    conn.close()


def get_search_log(limit: int = 100) -> list:
    """Gibt die letzten N Search-Log-Einträge zurück."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT * FROM search_log ORDER BY timestamp DESC LIMIT ?",
        (limit,)
    )
    rows = cursor.fetchall()
    conn.close()
    return [
        {
            "id": row["id"],
            "timestamp": row["timestamp"],
            "user_id": row["user_id"],
            "query": row["query"],
            "success": bool(row["success"]),
            "result_length": row["result_length"],
            "user_message_preview": row["user_message_preview"],
        }
        for row in rows
    ]


def save_moltbook_log(user_id: str, query: str, result_count: int, post_titles: list, reflection_preview: str = "") -> None:
    """Speichert einen Moltbook-Exploration-Lauf."""
    from core.datetime_utils import now_utc
    import json
    ts = now_utc().isoformat()
    titles_json = json.dumps(post_titles, ensure_ascii=False)
    with get_connection() as conn:
        conn.execute(
            """INSERT INTO moltbook_log (timestamp, user_id, query, result_count, post_titles, reflection_preview)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (ts, user_id, query, result_count, titles_json, reflection_preview),
        )
        conn.commit()


def get_moltbook_log(limit: int = 100) -> list:
    """Gibt Moltbook-Exploration-Logs zurueck."""
    import json
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM moltbook_log ORDER BY timestamp DESC LIMIT ?",
            (limit,)
        ).fetchall()
        result = []
        for row in rows:
            d = dict(row)
            try:
                d["post_titles"] = json.loads(d.get("post_titles") or "[]")
            except Exception:
                d["post_titles"] = []
            result.append(d)
        return result


# =============================================================================
# Proposed Patterns: Kimi-eigene Verhaltenshypothesen
# =============================================================================

def save_proposed_pattern(chunk_id: str, name: str, description: str, evidence: str,
                           occurrences: int, confidence: float) -> None:
    """Speichert ein neues proposed_pattern aus der Introspection."""
    conn = get_connection()
    try:
        conn.execute(
            """INSERT OR IGNORE INTO proposed_patterns
               (chunk_id, created_at, name, description, evidence, occurrences, last_seen, confidence, status)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'open')""",
            (chunk_id, to_iso(), name, description, evidence, occurrences, to_iso(), confidence),
        )
        conn.commit()
    except Exception as e:
        logger.warning(f"save_proposed_pattern Fehler: {e}")
    finally:
        conn.close()


def get_proposed_patterns(status: str = None, limit: int = 50) -> list:
    """Gibt proposed_patterns zurück, optional nach Status gefiltert."""
    conn = get_connection()
    try:
        if status:
            rows = conn.execute(
                "SELECT * FROM proposed_patterns WHERE status = ? ORDER BY created_at DESC LIMIT ?",
                (status, limit)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM proposed_patterns ORDER BY created_at DESC LIMIT ?",
                (limit,)
            ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def update_proposed_pattern_status(pattern_id: int, status: str, promoted_to: str = None) -> None:
    """
    Ändert den Status eines proposed_pattern.
    status: 'open' | 'dismissed' | 'working_state' | 'promoted'
    promoted_to: chunk_id des neuen working_state/decision Chunks (optional)
    """
    conn = get_connection()
    try:
        conn.execute(
            "UPDATE proposed_patterns SET status = ?, status_changed_at = ?, promoted_to = ? WHERE id = ?",
            (status, to_iso(), promoted_to, pattern_id),
        )
        conn.commit()
    except Exception as e:
        logger.warning(f"update_proposed_pattern_status Fehler: {e}")
    finally:
        conn.close()


def save_moltbook_post(post_id, title, content_text="", submolt="general", triggered_by=""):
    conn = get_connection()
    try:
        conn.execute("INSERT OR IGNORE INTO moltbook_posts (post_id, created_at, title, content, submolt, triggered_by) VALUES (?, ?, ?, ?, ?, ?)",
            (post_id, to_iso(), title, content_text, submolt, triggered_by))
        conn.commit()
    except Exception as e:
        logger.warning(f"save_moltbook_post: {e}")
    finally:
        conn.close()

def get_moltbook_posts(limit=50):
    conn = get_connection()
    try:
        rows = conn.execute("SELECT * FROM moltbook_posts ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()

def save_moltbook_inbox(post_id, post_title, author, content_text, comment_id="", direction="in"):
    conn = get_connection()
    try:
        conn.execute("INSERT INTO moltbook_inbox (received_at, post_id, post_title, author, content, comment_id, direction) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (to_iso(), post_id, post_title, author, content_text, comment_id, direction))
        conn.commit()
    except Exception as e:
        logger.warning(f"save_moltbook_inbox: {e}")
    finally:
        conn.close()

def get_moltbook_inbox(limit=100, unread_only=False):
    conn = get_connection()
    try:
        if unread_only:
            rows = conn.execute("SELECT * FROM moltbook_inbox WHERE processed = 0 ORDER BY received_at DESC LIMIT ?", (limit,)).fetchall()
        else:
            rows = conn.execute("SELECT * FROM moltbook_inbox ORDER BY received_at DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()

def update_moltbook_post_stats(post_id, upvotes, comment_count):
    conn = get_connection()
    try:
        conn.execute("UPDATE moltbook_posts SET upvotes = ?, comment_count = ?, last_checked = ? WHERE post_id = ?",
            (upvotes, comment_count, to_iso(), post_id))
        conn.commit()
    except Exception as e:
        logger.warning(f"update_moltbook_post_stats: {e}")
    finally:
        conn.close()
