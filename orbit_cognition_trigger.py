"""
SchnuBot.ai — Kognitions-Trigger (orbit_cognition_trigger.py)

Wird alle 2h per Cron aufgerufen.
Schreibt NUR einen cognition_run Trigger in die SQLite — kein ChromaDB-Zugriff.

ORBIT liest den Trigger im nächsten Tick (max. 20s) und führt die Kognition
im selben Prozess wie den Bot aus — kein Parallelzugriff auf ChromaDB möglich.

Ersetzt den direkten Cron-Aufruf von orbit_cognition.py.
"""

import os
import sys

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, PROJECT_DIR)
os.chdir(PROJECT_DIR)

from config import USER_CONTEXTS
from core.datetime_utils import to_iso

# Nur SQLite-Zugriff — kein ChromaDB, kein Ollama, kein Memory
from core.database import get_connection


def write_cognition_trigger(user_id: str) -> str:
    """Schreibt einen cognition_run Trigger direkt in orbit_triggers."""
    import uuid
    tid = str(uuid.uuid4())
    import json
    conn = get_connection()
    try:
        conn.execute(
            """INSERT INTO orbit_triggers
               (id, trigger_type, source, payload, processed, created_at)
               VALUES (?, ?, ?, ?, 0, ?)""",
            (
                tid,
                "cognition_run",
                "cron",
                json.dumps({"user_id": user_id}, ensure_ascii=False),
                to_iso(),
            )
        )
        conn.commit()
        print(f"[TRIGGER] cognition_run → {tid[:8]} für {user_id[:20]}")
        return tid
    finally:
        conn.close()


def main():
    for user_id in USER_CONTEXTS.keys():
        write_cognition_trigger(user_id)
    print("[TRIGGER] Kognitions-Trigger geschrieben — ORBIT führt aus im nächsten Tick")


if __name__ == "__main__":
    main()
