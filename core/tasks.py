"""
SchnuBot.ai – Task-System
Ermöglicht iteratives Nachdenken über komplexe Aufgaben.

Flow:
1. User schreibt /task <Auftrag> per WhatsApp
2. app.py erstellt Task via create_task() und claimed sofort via claim_task()
3. app.py iteriert im ThreadPool bis fertig oder API-Fehler
4. Heartbeat ist Fallback: übernimmt nur Tasks deren Claim abgelaufen ist
5. Kimi sagt TASK_DONE oder max. Runden erreicht → Ergebnis per WhatsApp

Ownership-Modell (Fix P1.1):
- Jeder Runner (app.py ThreadPool / Heartbeat) setzt runner_id + claimed_at
- Bevor ein Runner einen Task anfasst, prüft er ob der Claim noch frisch ist
- Stale-Timeout: CLAIM_STALE_MINUTES (default 15 Min)
- Verhindert doppelte Iterationen, doppelte API-Calls, Race Conditions

Fix 9.7:
- Task-IDs jetzt UUID-basiert (keine Sekunden-Kollision mehr)
- Atomisches Schreiben via Temp-Datei + os.replace()
- UTC-Timestamps via datetime_utils
"""

import os
import json
import uuid
import logging

from core.datetime_utils import to_iso, now_utc, safe_parse_dt
from core.file_utils import atomic_write_json

logger = logging.getLogger(__name__)

TASKS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tasks")
MAX_ITERATIONS = 10

# Wenn ein Claim älter als X Minuten ist, gilt er als stale (Runner abgestürzt).
# Muss länger sein als der längste erwartete API-Call (180s Timeout + Retry + Puffer).
CLAIM_STALE_MINUTES = 15


def ensure_tasks_dir():
    os.makedirs(TASKS_DIR, exist_ok=True)


def load_task(task_id):
    """Lädt einen Task von Disk. Returns: Task-Dict oder None."""
    path = os.path.join(TASKS_DIR, f"{task_id}.json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError, FileNotFoundError) as e:
        logger.error(f"Task {task_id}: Datei nicht lesbar: {e}")
        return None


def create_task(user_id, prompt, context_name=None):
    """Erstellt einen neuen Task mit UUID-basierter ID."""
    ensure_tasks_dir()

    now = to_iso()
    task_id = uuid.uuid4().hex[:12]
    task = {
        "id": task_id,
        "user_id": user_id,
        "context_name": context_name,
        "prompt": prompt,
        "status": "pending",  # pending → running → done → delivered
        "iterations": [],
        "current_iteration": 0,
        "max_iterations": MAX_ITERATIONS,
        "created_at": now,
        "updated_at": now,
        # Ownership-Felder (P1.1)
        "runner_id": None,
        "claimed_at": None,
    }

    path = os.path.join(TASKS_DIR, f"{task_id}.json")
    atomic_write_json(path, task)

    logger.info(f"Task erstellt: {task_id} – {prompt[:80]}")
    return task_id


# =============================================================================
# Task Ownership (P1.1 — Race-Prevention)
# =============================================================================

def claim_task(task, runner_id):
    """
    Beansprucht einen Task für einen bestimmten Runner.

    Setzt runner_id + claimed_at + status=running und speichert atomar.
    Muss VOR der ersten Iteration aufgerufen werden.

    Args:
        task: Task-Dict (wird in-place modifiziert)
        runner_id: Eindeutige ID des Runners (z.B. "app-<uuid>" oder "heartbeat")

    Returns:
        True wenn erfolgreich geclaimed
    """
    task["runner_id"] = runner_id
    task["claimed_at"] = to_iso()
    task["status"] = "running"
    save_task(task)
    logger.info(f"Task {task['id']}: claimed by {runner_id}")
    return True


def refresh_claim(task, runner_id):
    """
    Frischt den Claim-Timestamp auf (Heartbeat für laufende Iteration).

    Wird zwischen Iterationen aufgerufen, damit der Claim nicht
    während eines langen API-Calls als stale gilt.

    Returns:
        True wenn der Claim noch diesem Runner gehört, False wenn übernommen.
    """
    # Sicherheitscheck: gehört der Task noch uns?
    if task.get("runner_id") != runner_id:
        logger.warning(
            f"Task {task['id']}: Claim-Refresh verweigert — "
            f"Runner {runner_id} != Owner {task.get('runner_id')}"
        )
        return False

    task["claimed_at"] = to_iso()
    save_task(task)
    return True


def release_task(task, runner_id):
    """
    Gibt den Claim frei (z.B. bei API-Fehler, damit Heartbeat übernehmen kann).

    Setzt runner_id und claimed_at zurück, Status bleibt running
    damit get_pending_tasks() den Task noch findet.
    """
    if task.get("runner_id") != runner_id:
        logger.warning(
            f"Task {task['id']}: Release verweigert — "
            f"Runner {runner_id} != Owner {task.get('runner_id')}"
        )
        return

    task["runner_id"] = None
    task["claimed_at"] = None
    save_task(task)
    logger.info(f"Task {task['id']}: released by {runner_id}")


def is_claimed(task):
    """
    Prüft ob ein Task aktuell von einem aktiven Runner bearbeitet wird.

    Returns:
        True wenn ein frischer Claim existiert (nicht stale),
        False wenn frei oder Claim abgelaufen.
    """
    runner_id = task.get("runner_id")
    claimed_at = task.get("claimed_at")

    if not runner_id or not claimed_at:
        return False

    claimed_dt = safe_parse_dt(claimed_at)
    if claimed_dt is None:
        return False

    age_minutes = (now_utc() - claimed_dt).total_seconds() / 60

    if age_minutes > CLAIM_STALE_MINUTES:
        logger.warning(
            f"Task {task['id']}: Stale Claim von {runner_id} "
            f"({age_minutes:.1f} min > {CLAIM_STALE_MINUTES} min)"
        )
        return False

    return True


def generate_runner_id(prefix="runner"):
    """Erzeugt eine eindeutige Runner-ID."""
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


# =============================================================================
# Task Query & Persistence
# =============================================================================

def get_pending_tasks():
    """Gibt alle offenen Tasks zurück (pending oder running)."""
    ensure_tasks_dir()
    tasks = []

    for fn in sorted(os.listdir(TASKS_DIR)):
        if not fn.endswith(".json"):
            continue
        path = os.path.join(TASKS_DIR, fn)
        try:
            with open(path, "r", encoding="utf-8") as f:
                task = json.load(f)
            if task.get("status") in ("pending", "running"):
                tasks.append(task)
        except (json.JSONDecodeError, IOError) as e:
            logger.warning(f"Task-Datei nicht lesbar: {fn}: {e}")
            continue

    return tasks


def save_task(task):
    """Speichert einen Task atomar."""
    task["updated_at"] = to_iso()
    path = os.path.join(TASKS_DIR, f"{task['id']}.json")
    atomic_write_json(path, task)


# =============================================================================
# Task Delivery (zentral — wird von app.py UND heartbeat.py genutzt)
# =============================================================================

def deliver_task(task):
    """
    Sendet das Endergebnis per WhatsApp und markiert als delivered.

    Zentrale Funktion: verhindert doppelte Implementierung in app.py/heartbeat.py.
    Import von whatsapp/database hier statt top-level um zirkuläre Imports zu vermeiden.
    """
    from core.whatsapp import send_message
    from core.database import save_message

    user_id = task["user_id"]
    task_id = task["id"]

    if not task["iterations"]:
        return

    final_result = task["iterations"][-1]["result"]
    final_result = final_result.replace("TASK_DONE", "").replace("task_done", "").strip()

    iterations_count = len(task["iterations"])
    header = f"Task #{task_id} fertig ({iterations_count} Runden):\n\n"
    msg = header + final_result + "\n\n[kimi/task]"

    send_message(user_id, msg)
    save_message(user_id, "assistant", msg)
    task["status"] = "delivered"
    task["runner_id"] = None
    task["claimed_at"] = None
    save_task(task)
    logger.info(f"Task {task_id}: zugestellt ({iterations_count} Iterationen)")


# =============================================================================
# Prompt Builder
# =============================================================================

def build_iteration_prompt(task):
    """Baut den Prompt für die aktuelle Iteration."""
    iteration = task["current_iteration"]
    original_prompt = task["prompt"]

    if iteration == 0:
        # Erste Runde: Erstversion erstellen
        return f"""Du hast folgenden Auftrag bekommen. Erstelle eine erste Version.
Denke gründlich nach bevor du schreibst. Qualität vor Geschwindigkeit.

Auftrag:
{original_prompt}

Erstelle jetzt eine erste, solide Version. Am Ende deiner Antwort: bewerte selbst auf einer Skala 1-10 wie gut das Ergebnis ist und was noch fehlt.
Wenn du der Meinung bist, das Ergebnis ist bereits perfekt (9+/10), schreibe TASK_DONE am Ende.Und gib IMMER am Ende das finale Ergebnis aus. Zwischenschritte braucht der User nciht!"""

    else:
        # Folgerunden: Vorherige Version verbessern
        previous = task["iterations"][-1]["result"]
        prev_num = iteration
        return f"""Du arbeitest iterativ an einem Auftrag. Das ist Runde {iteration + 1} von maximal {task['max_iterations']}.

Ursprünglicher Auftrag:
{original_prompt}

Dein bisheriges Ergebnis (Runde {prev_num}):
---
{previous}
---

Deine Aufgabe jetzt:
1. Lies dein vorheriges Ergebnis kritisch
2. Was ist gut? Was fehlt? Was ist unpräzise oder schwach?
3. Überarbeite und verbessere das Ergebnis
4. Bewerte am Ende wieder 1-10 und benenne was noch besser werden könnte

Wenn das Ergebnis jetzt wirklich gut ist (9+/10), schreibe TASK_DONE am Ende.
Wenn nicht, liefere die verbesserte Version."""


TASK_DONE_TOKEN = "TASK_DONE"
