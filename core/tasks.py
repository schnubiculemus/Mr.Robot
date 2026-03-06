"""
SchnuBot.ai – Task-System
Ermöglicht iteratives Nachdenken über komplexe Aufgaben.

Flow:
1. User schreibt /task <Auftrag> per WhatsApp
2. app.py erstellt Task via create_task()
3. Heartbeat erkennt offene Tasks und lässt Kimi iterieren
4. Pro Iteration: Kimi bekommt vorheriges Ergebnis + Verbesserungsauftrag
5. Kimi sagt TASK_DONE oder max. Runden erreicht → Ergebnis per WhatsApp

Fix 9.7:
- Task-IDs jetzt UUID-basiert (keine Sekunden-Kollision mehr)
- Atomisches Schreiben via Temp-Datei + os.replace()
- UTC-Timestamps via datetime_utils
"""

import os
import json
import uuid
import tempfile
import logging

from core.datetime_utils import to_iso

logger = logging.getLogger(__name__)

TASKS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tasks")
MAX_ITERATIONS = 10


def ensure_tasks_dir():
    os.makedirs(TASKS_DIR, exist_ok=True)


def _atomic_write_json(path, data):
    """
    Schreibt JSON atomar: erst in Temp-Datei, dann os.replace().
    Verhindert halb-geschriebene Dateien bei Crashes oder parallelen Zugriffen.
    """
    dir_name = os.path.dirname(path)
    try:
        fd, tmp_path = tempfile.mkstemp(dir=dir_name, suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, path)
    except Exception as e:
        # Temp-Datei aufräumen falls replace fehlschlägt
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise e


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
    }

    path = os.path.join(TASKS_DIR, f"{task_id}.json")
    _atomic_write_json(path, task)

    logger.info(f"Task erstellt: {task_id} – {prompt[:80]}")
    return task_id


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
    _atomic_write_json(path, task)


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
