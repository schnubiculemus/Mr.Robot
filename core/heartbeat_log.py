"""
core/heartbeat_log.py — Strukturiertes Heartbeat-Run-Logging

Schreibt jeden Heartbeat-Run als JSON-Eintrag in die DB (Tabelle heartbeat_runs).
Wird von heartbeat.py genutzt um jeden Schritt zu protokollieren.

Schema pro Run:
  - run_id:     UUID
  - started_at: ISO-Timestamp
  - finished_at: ISO-Timestamp
  - user_id:    User
  - steps:      JSON-Array mit allen Schritten
  - summary:    Kurz-Text für die Timeline-Anzeige
  - had_error:  0/1

Schema pro Step:
  {
    "step":    "konsolidierung" | "deduplizierung" | "decay" | "reflexion"
             | "tagebuch" | "proaktiv" | "autonomie",
    "status":  "ok" | "skip" | "error",
    "detail":  "2 neue Chunks" | "Cooldown" | "Fehlermeldung" etc.,
    "ts":      ISO-Timestamp
  }
"""

import json
import logging
import uuid
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


class HeartbeatRun:
    """
    Context-Manager für einen Heartbeat-Run.

    Verwendung in heartbeat.py:
        with HeartbeatRun(user_id) as run:
            ...
            run.step("konsolidierung", "ok", "3 neue Chunks")
            run.step("decay", "skip", "Nichts zu tun")
    """

    def __init__(self, user_id: str):
        self.run_id = str(uuid.uuid4())
        self.user_id = user_id
        self.started_at = datetime.now(timezone.utc).isoformat()
        self.finished_at = None
        self.steps = []
        self.had_error = False

    def step(self, name: str, status: str, detail: str = ""):
        """Fügt einen Schritt zum aktuellen Run hinzu."""
        if status == "error":
            self.had_error = True
        self.steps.append({
            "step": name,
            "status": status,
            "detail": detail,
            "ts": datetime.now(timezone.utc).isoformat(),
        })

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.finished_at = datetime.now(timezone.utc).isoformat()
        if exc_type is not None:
            self.step("__crash__", "error", str(exc_val))
            self.had_error = True
        try:
            _save_run(self)
        except Exception as e:
            logger.warning(f"HeartbeatRun: Speichern fehlgeschlagen: {e}")
        return False  # Exception nicht unterdrücken


def _save_run(run: HeartbeatRun):
    """Schreibt den Run in die DB."""
    from core.database import get_connection
    summary = _build_summary(run.steps)
    with get_connection() as conn:
        conn.execute("""
            INSERT INTO heartbeat_runs
                (run_id, user_id, started_at, finished_at, steps_json, summary, had_error)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (
            run.run_id,
            run.user_id,
            run.started_at,
            run.finished_at,
            json.dumps(run.steps, ensure_ascii=False),
            summary,
            1 if run.had_error else 0,
        ))
        conn.commit()


def _build_summary(steps: list) -> str:
    """Baut einen kompakten Einzeiler aus den Steps."""
    parts = []
    for s in steps:
        name = s["step"]
        status = s["status"]
        detail = s.get("detail", "")

        if name == "__crash__":
            parts.append(f"💥 Crash: {detail[:60]}")
        elif status == "error":
            parts.append(f"⚠️ {name}: Fehler")
        elif status == "skip":
            label = {
                "konsolidierung": "Konsol.",
                "deduplizierung": "Dedup.",
                "decay": "Decay",
                "reflexion": "Reflexion",
                "introspection": "Introspection",
                "tagebuch": "Tagebuch",
                "proaktiv": "Proaktiv",
                "autonomie": "Autonomie",
            }.get(name, name)
            parts.append(f"{label}: –")
        elif status == "ok":
            if detail:
                parts.append(detail)

    return " · ".join(parts) if parts else "Kein Ergebnis"


def get_recent_runs(limit: int = 50) -> list:
    """Gibt die letzten N Heartbeat-Runs zurück."""
    from core.database import get_connection
    with get_connection() as conn:
        rows = conn.execute("""
            SELECT run_id, user_id, started_at, finished_at, steps_json, summary, had_error
            FROM heartbeat_runs
            ORDER BY started_at DESC
            LIMIT ?
        """, (limit,)).fetchall()

    result = []
    for row in rows:
        steps = []
        try:
            steps = json.loads(row[4]) if row[4] else []
        except Exception:
            pass
        result.append({
            "run_id":      row[0],
            "user_id":     row[1],
            "started_at":  row[2],
            "finished_at": row[3],
            "steps":       steps,
            "summary":     row[5],
            "had_error":   bool(row[6]),
        })
    return result
