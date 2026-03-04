"""
SchnuBot.ai – Task-System
Ermöglicht iteratives Nachdenken über komplexe Aufgaben.

Flow:
1. User schreibt /task <Auftrag> per WhatsApp
2. app.py erstellt Task via create_task()
3. Heartbeat erkennt offene Tasks und lässt Kimi iterieren
4. Pro Iteration: Kimi bekommt vorheriges Ergebnis + Verbesserungsauftrag
5. Kimi sagt TASK_DONE oder max. Runden erreicht → Ergebnis per WhatsApp
"""

import os
import json
import logging
from datetime import datetime

logger = logging.getLogger(__name__)

TASKS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tasks")
MAX_ITERATIONS = 10


def ensure_tasks_dir():
    os.makedirs(TASKS_DIR, exist_ok=True)


def create_task(user_id, prompt, context_name=None):
    """Erstellt einen neuen Task."""
    ensure_tasks_dir()

    task_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    task = {
        "id": task_id,
        "user_id": user_id,
        "context_name": context_name,
        "prompt": prompt,
        "status": "pending",  # pending → running → done → delivered
        "iterations": [],
        "current_iteration": 0,
        "max_iterations": MAX_ITERATIONS,
        "created_at": datetime.now().isoformat(),
        "updated_at": datetime.now().isoformat(),
    }

    path = os.path.join(TASKS_DIR, f"{task_id}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(task, f, ensure_ascii=False, indent=2)

    logger.info(f"📝 Task erstellt: {task_id} – {prompt[:80]}")
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
        except (json.JSONDecodeError, IOError):
            continue

    return tasks


def save_task(task):
    """Speichert einen Task."""
    task["updated_at"] = datetime.now().isoformat()
    path = os.path.join(TASKS_DIR, f"{task['id']}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(task, f, ensure_ascii=False, indent=2)


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
