#!/usr/bin/env python3
"""
SchnuBot.ai – Heartbeat System
Autonomer Hintergrundprozess. SchnuBots eigene Denkzeit.

Tasks:
1. Memory → .facts appenden (neue Fakten als Zeilen anhängen)
2. Memory aufräumen (gemergte Fakten entfernen)
3. Optional: proaktive Nachricht senden
4. Changelog an User senden
"""

import os
import sys
import json
import logging
import requests
from datetime import datetime

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, PROJECT_DIR)
os.chdir(PROJECT_DIR)

from config import OLLAMA_API_URL, OLLAMA_API_KEY, OLLAMA_MODEL, WAHA_API_KEY
from core.database import get_connection, get_chat_history, save_message
from legacy.memory import (
    get_unmerged_facts,
    get_unmerged_by_category,
    mark_facts_as_merged,
    cleanup_memory,
    format_memory_for_prompt,
    _fact_text,
)
from core.whatsapp import send_message, init_waha
from core.tasks import get_pending_tasks, save_task, build_iteration_prompt, TASK_DONE_TOKEN

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [HEARTBEAT] %(message)s",
    handlers=[
        logging.FileHandler(os.path.join(PROJECT_DIR, "logs", "heartbeat.log")),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger(__name__)

# ─── Konfiguration ───────────────────────────────────────────────
SILENCE_THRESHOLD_HOURS = 12
ACTIVE_HOURS_START = 8
ACTIVE_HOURS_END = 22
HEARTBEAT_COOLDOWN_HOURS = 18
HEARTBEAT_OK_TOKEN = "HEARTBEAT_OK"
CONTEXT_DIR = os.path.join(PROJECT_DIR, "context")

USER_CONTEXTS = {
    "221152228159675@lid": "tommy",
}

HEARTBEAT_STATE_PATH = os.path.join(PROJECT_DIR, "heartbeat_state.json")


# ─── State ────────────────────────────────────────────────────────

def load_state():
    if os.path.exists(HEARTBEAT_STATE_PATH):
        try:
            with open(HEARTBEAT_STATE_PATH, "r") as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            pass
    return {}


def save_state(state):
    with open(HEARTBEAT_STATE_PATH, "w") as f:
        json.dump(state, f, indent=2)


def get_last_message_time(user_id):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT timestamp FROM messages WHERE phone_number = ? ORDER BY timestamp DESC LIMIT 1",
        (user_id,),
    )
    row = cursor.fetchone()
    conn.close()
    return datetime.fromisoformat(row["timestamp"]) if row else None


# ─── Task 1: Memory → .facts Append ─────────────────────────────

def merge_memory_to_context(user_id, context_name):
    """Appendet ungemergte Fakten an die .facts-Datei. Simpel und robust."""
    grouped = get_unmerged_by_category(user_id)

    # Nur persoenlich + kommunikation gehen in die Context-Datei
    context_facts = grouped.get("persoenlich", []) + grouped.get("kommunikation", [])
    knowledge_facts = grouped.get("knowledge", [])

    if not context_facts and not knowledge_facts:
        logger.info(f"📋 Keine ungemergten Fakten für {context_name}.")
        return False, []

    all_merged = []

    # Knowledge-Fakten → bim.facts
    if knowledge_facts:
        bim_facts_path = os.path.join(CONTEXT_DIR, "bim.facts")
        if os.path.exists(bim_facts_path):
            try:
                now = datetime.now().strftime("%Y-%m-%d")
                new_lines = [_fact_text(f).strip() for f in knowledge_facts if _fact_text(f).strip()]
                if new_lines:
                    with open(bim_facts_path, "a", encoding="utf-8") as f:
                        f.write(f"\n# Heartbeat {now}\n")
                        for line in new_lines:
                            f.write(f"{line}\n")
                    logger.info(f"📚 {len(new_lines)} Knowledge-Fakten an bim.facts angehängt")
                    mark_facts_as_merged(user_id, knowledge_facts)
                    all_merged.extend([_fact_text(f) for f in knowledge_facts])
            except IOError as e:
                logger.error(f"❌ Fehler beim Appenden an bim.facts: {e}")
        else:
            logger.warning(f"⚠️ bim.facts nicht gefunden, Knowledge-Fakten übersprungen")

    # Persönliche Fakten → tommy.facts (oder andere user.facts)
    if not context_facts:
        if all_merged:
            return True, all_merged
        logger.info(f"📋 Keine persönlichen Fakten zum Appenden.")
        return False, []

    # .facts-Datei finden
    facts_path = os.path.join(CONTEXT_DIR, f"{context_name}.facts")

    if not os.path.exists(facts_path):
        logger.error(f"❌ Keine .facts-Datei gefunden: {facts_path}")
        return len(all_merged) > 0, all_merged

    # Backup
    try:
        with open(facts_path, "r", encoding="utf-8") as f:
            current_content = f.read()
        backup_path = facts_path + ".backup"
        with open(backup_path, "w", encoding="utf-8") as f:
            f.write(current_content)
    except IOError as e:
        logger.error(f"❌ Backup fehlgeschlagen: {e}")
        return len(all_merged) > 0, all_merged

    # Neue Fakten als Zeilen formatieren und appenden
    now = datetime.now().strftime("%Y-%m-%d")
    new_lines = []

    for fact in context_facts:
        text = _fact_text(fact)
        if text.strip():
            new_lines.append(text.strip())

    if not new_lines:
        logger.info(f"📋 Keine gültigen Fakten zum Appenden.")
        return len(all_merged) > 0, all_merged

    try:
        with open(facts_path, "a", encoding="utf-8") as f:
            f.write(f"\n# Heartbeat {now}\n")
            for line in new_lines:
                f.write(f"{line}\n")

        logger.info(f"✅ {len(new_lines)} Fakten an {context_name}.facts angehängt")
        mark_facts_as_merged(user_id, context_facts)
        all_merged.extend([_fact_text(f) for f in context_facts])
        return True, all_merged

    except IOError as e:
        logger.error(f"❌ Fehler beim Appenden: {e}")
        return False, []


# ─── Task 2: Cleanup ─────────────────────────────────────────────

def run_cleanup(user_id):
    removed = cleanup_memory(user_id)
    if removed:
        logger.info(f"🧹 {removed} Fakten aus Memory entfernt")
    return removed


# ─── Task 3: Proaktive Nachricht ─────────────────────────────────

def should_send_message(user_id, now):
    if now.hour < ACTIVE_HOURS_START or now.hour >= ACTIVE_HOURS_END:
        return False

    last_msg = get_last_message_time(user_id)
    if not last_msg:
        return False

    silence = (now - last_msg).total_seconds() / 3600
    if silence < SILENCE_THRESHOLD_HOURS:
        return False

    state = load_state()
    last_hb = state.get(f"{user_id}_message")
    if last_hb:
        cooldown = (now - datetime.fromisoformat(last_hb)).total_seconds() / 3600
        if cooldown < HEARTBEAT_COOLDOWN_HOURS:
            return False

    return True


def maybe_send_message(user_id, context_name, now):
    if not should_send_message(user_id, now):
        logger.info("💬 Proaktive Nachricht: Bedingungen nicht erfüllt, skip.")
        return

    heartbeat_path = os.path.join(PROJECT_DIR, "heartbeat.md")
    try:
        with open(heartbeat_path, "r", encoding="utf-8") as f:
            heartbeat_prompt = f.read()
    except FileNotFoundError:
        logger.info("💬 Keine heartbeat.md, skip.")
        return

    logger.info("💬 Prüfe ob proaktive Nachricht sinnvoll...")

    from core.ollama_client import build_system_prompt
    system_prompt = build_system_prompt(context_name, user_id)
    history = get_chat_history(user_id, limit=10)

    memory_text = format_memory_for_prompt(user_id)
    memory_section = f"\n\nDein Langzeitgedächtnis:\n{memory_text}" if memory_text else ""

    prompt = f"""{heartbeat_prompt}

---
Aktuelle Uhrzeit: {now.strftime('%A, %d. %B %Y, %H:%M Uhr')}
{memory_section}

Schreib eine kurze Nachricht ODER antworte mit HEARTBEAT_OK."""

    messages = [{"role": "system", "content": system_prompt}]
    messages.extend(history)
    messages.append({"role": "user", "content": prompt})

    try:
        response = requests.post(
            f"{OLLAMA_API_URL}/api/chat",
            headers={
                "Authorization": f"Bearer {OLLAMA_API_KEY}",
                "Content-Type": "application/json",
            },
            json={"model": OLLAMA_MODEL, "messages": messages, "stream": False},
            timeout=60,
        )
        response.raise_for_status()
        reply = response.json().get("message", {}).get("content", "").strip()

        if HEARTBEAT_OK_TOKEN in reply.upper().replace(" ", "_"):
            logger.info("💬 Kimi: nichts zu tun. ✓")
        else:
            logger.info(f"💬 Sende: {reply[:100]}")
            send_message(user_id, reply + "\n\n[kimi/heartbeat]")
            save_message(user_id, "assistant", reply)

        state = load_state()
        state[f"{user_id}_message"] = now.isoformat()
        save_state(state)

    except Exception as e:
        logger.error(f"💬 Fehler: {e}")


# ─── Task 4: Changelog ───────────────────────────────────────────

def send_changelog(user_id, merged_facts, now):
    if not merged_facts:
        return

    if now.hour < ACTIVE_HOURS_START or now.hour >= ACTIVE_HOURS_END:
        return

    facts_list = ", ".join(merged_facts[:5])
    if len(merged_facts) > 5:
        facts_list += f" (+{len(merged_facts) - 5} weitere)"

    msg = f"🧠 Profil aktualisiert: {facts_list}\n\n[ministral→heartbeat]"
    send_message(user_id, msg)
    save_message(user_id, "assistant", msg)
    logger.info(f"📨 Changelog gesendet")


# ─── Task 5: Iterative Task-Verarbeitung ─────────────────────────

def process_tasks():
    """Verarbeitet offene Tasks – eine Iteration pro Heartbeat-Durchlauf."""
    pending = get_pending_tasks()
    if not pending:
        return

    for task in pending:
        task_id = task["id"]
        user_id = task["user_id"]
        context_name = task.get("context_name")
        iteration = task["current_iteration"]

        if iteration >= task["max_iterations"]:
            # Max erreicht → abschließen
            task["status"] = "done"
            save_task(task)
            _deliver_task(task)
            continue

        logger.info(f"📝 Task {task_id}: Iteration {iteration + 1}/{task['max_iterations']}")
        task["status"] = "running"

        # System-Prompt aufbauen (mit vollem Kontext)
        from core.ollama_client import build_system_prompt
        system_prompt = build_system_prompt(context_name, user_id)

        # Iterations-Prompt
        iter_prompt = build_iteration_prompt(task)

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": iter_prompt},
        ]

        try:
            response = requests.post(
                f"{OLLAMA_API_URL}/api/chat",
                headers={
                    "Authorization": f"Bearer {OLLAMA_API_KEY}",
                    "Content-Type": "application/json",
                },
                json={"model": OLLAMA_MODEL, "messages": messages, "stream": False},
                timeout=180,  # Tasks dürfen länger dauern
            )
            response.raise_for_status()
            result = response.json().get("message", {}).get("content", "").strip()

            # Iteration speichern
            task["iterations"].append({
                "iteration": iteration + 1,
                "result": result,
                "timestamp": datetime.now().isoformat(),
            })
            task["current_iteration"] = iteration + 1

            # Prüfen ob Kimi fertig ist
            if TASK_DONE_TOKEN in result.upper().replace(" ", "_"):
                logger.info(f"📝 Task {task_id}: Kimi sagt DONE nach Iteration {iteration + 1}")
                task["status"] = "done"
                save_task(task)
                _deliver_task(task)
            elif task["current_iteration"] >= task["max_iterations"]:
                logger.info(f"📝 Task {task_id}: Max Iterationen erreicht ({task['max_iterations']})")
                task["status"] = "done"
                save_task(task)
                _deliver_task(task)
            else:
                logger.info(f"📝 Task {task_id}: Iteration {iteration + 1} abgeschlossen, weiter im nächsten Heartbeat")
                save_task(task)

        except requests.exceptions.RequestException as e:
            logger.error(f"📝 Task {task_id}: API-Fehler in Iteration {iteration + 1}: {e}")
            save_task(task)
        except Exception as e:
            logger.error(f"📝 Task {task_id}: Fehler: {e}")
            save_task(task)


def _deliver_task(task):
    """Sendet das Endergebnis per WhatsApp."""
    user_id = task["user_id"]
    task_id = task["id"]

    if not task["iterations"]:
        return

    # Letztes Ergebnis nehmen
    final_result = task["iterations"][-1]["result"]

    # TASK_DONE Token entfernen
    final_result = final_result.replace("TASK_DONE", "").replace("task_done", "").strip()

    iterations_count = len(task["iterations"])
    header = f"📝 Task #{task_id} fertig ({iterations_count} Runden):\n\n"
    msg = header + final_result + "\n\n[kimi/task]"

    send_message(user_id, msg)
    save_message(user_id, "assistant", msg)
    task["status"] = "delivered"
    save_task(task)
    logger.info(f"📝 Task {task_id}: Ergebnis zugestellt ({iterations_count} Iterationen)")


# ─── Main ─────────────────────────────────────────────────────────

def run_heartbeat(user_id, context_name):
    now = datetime.now()
    logger.info(f"💓 ─── {context_name} ({user_id}) ───")

    # 1. Append an .facts
    merged_ok, merged_facts = merge_memory_to_context(user_id, context_name)

    # 2. Cleanup
    if merged_ok:
        run_cleanup(user_id)

    # 3. Proaktive Nachricht
    maybe_send_message(user_id, context_name, now)

    # 4. Changelog
    send_changelog(user_id, merged_facts, now)

    # State
    state = load_state()
    state[f"{user_id}_last_run"] = now.isoformat()
    save_state(state)

    logger.info(f"💓 ─── fertig ───")


def main():
    logger.info("💓 ═══ Heartbeat gestartet ═══")
    init_waha(WAHA_API_KEY)

    for user_id, context_name in USER_CONTEXTS.items():
        run_heartbeat(user_id, context_name)

    # Tasks verarbeiten (user-übergreifend)
    process_tasks()

    logger.info("💓 ═══ Heartbeat abgeschlossen ═══")


if __name__ == "__main__":
    main()
