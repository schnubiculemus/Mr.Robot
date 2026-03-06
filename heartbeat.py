"""
SchnuBot.ai - Heartbeat (Neu)
Referenz: Konzeptdokument V1.1, Abschnitt 13, 18, 19

Periodischer Hintergrundprozess. Konsolidiert neue Turns zu Memory-Chunks.
Ersetzt den alten Heartbeat (legacy/heartbeat.py) schrittweise.

Tasks:
1. Neue Turns aus bot.db laden
2. Konsolidierer aufrufen -> Chunks in ChromaDB
3. Proaktive Nachrichten (unveraendert aus legacy)
4. Task-Verarbeitung (unveraendert aus legacy)
"""

import os
import sys
import json
import logging
from datetime import datetime, timezone

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, PROJECT_DIR)
os.chdir(PROJECT_DIR)

from config import OLLAMA_API_URL, OLLAMA_API_KEY, OLLAMA_MODEL, WAHA_API_KEY
from core.datetime_utils import now_utc, now_berlin, safe_parse_dt, to_iso
from core.database import get_connection, get_chat_history, save_message
from core.whatsapp import send_message, init_waha
from core.tasks import get_pending_tasks, save_task, build_iteration_prompt, TASK_DONE_TOKEN
from memory.consolidator import consolidate_turns
from memory.merge import deduplicate_active
from memory.memory_store import get_stats
from proactive import run_proactive
from autonomy import run_autonomy
from decay import run_decay

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [HEARTBEAT] %(message)s",
    handlers=[
        logging.FileHandler(os.path.join(PROJECT_DIR, "logs", "heartbeat.log")),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger(__name__)

# --- Konfiguration ---
ACTIVE_HOURS_START = 8
ACTIVE_HOURS_END = 22
SILENCE_THRESHOLD_HOURS = 12
HEARTBEAT_COOLDOWN_HOURS = 18
HEARTBEAT_OK_TOKEN = "HEARTBEAT_OK"

USER_CONTEXTS = {
    "221152228159675@lid": "tommy",
}

HEARTBEAT_STATE_PATH = os.path.join(PROJECT_DIR, "heartbeat_state.json")


# =============================================================================
# State
# =============================================================================

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
    return safe_parse_dt(row["timestamp"]) if row else None


# =============================================================================
# Task 1: Konsolidierung (NEU - ersetzt alten Facts-Merge)
# =============================================================================

def get_new_turns(user_id, since_iso, until_iso=None):
    """
    Holt alle Nachrichten seit dem letzten Heartbeat-Lauf bis upper_bound.

    Args:
        user_id: Phone-Number / LID
        since_iso: ISO-Timestamp des letzten Laufs
        until_iso: Upper-Bound (exklusiv). Verhindert Doppel-Konsolidierung.

    Returns:
        Liste von Dicts mit 'role' und 'content'
    """
    conn = get_connection()
    cursor = conn.cursor()

    if since_iso:
        if until_iso:
            cursor.execute(
                """
                SELECT role, content FROM messages
                WHERE phone_number = ? AND timestamp > ? AND timestamp <= ?
                ORDER BY timestamp ASC
                """,
                (user_id, since_iso, until_iso),
            )
        else:
            cursor.execute(
                """
                SELECT role, content FROM messages
                WHERE phone_number = ? AND timestamp > ?
                ORDER BY timestamp ASC
                """,
                (user_id, since_iso),
            )
    else:
        # Erster Lauf: letzte 30 Nachrichten
        cursor.execute(
            """
            SELECT role, content FROM messages
            WHERE phone_number = ?
            ORDER BY timestamp DESC
            LIMIT 30
            """,
            (user_id,),
        )

    rows = cursor.fetchall()
    conn.close()

    turns = [{"role": row["role"], "content": row["content"]} for row in rows]

    # Bei DESC-Query umkehren (chronologisch)
    if not since_iso:
        turns.reverse()

    return turns


def run_consolidation(user_id):
    """
    Holt neue Turns und konsolidiert sie zu Memory-Chunks.
    upper_bound wird VOR dem Query gesetzt UND im Query als Obergrenze verwendet,
    damit keine Turns doppelt konsolidiert werden.
    """
    state = load_state()
    last_run = state.get(f"{user_id}_last_consolidation")

    # Upper bound JETZT setzen — alles was danach reinkommt wird beim nächsten Lauf geholt
    upper_bound = datetime.now(timezone.utc).isoformat()

    turns = get_new_turns(user_id, last_run, until_iso=upper_bound)

    if not turns:
        logger.info(f"Keine neuen Turns seit letztem Lauf")
        return 0

    logger.info(f"Konsolidiere {len(turns)} neue Turns")

    # Konsolidierer aufrufen
    chunk_count = consolidate_turns(turns)

    # State auf upper_bound setzen (nicht datetime.now!)
    state[f"{user_id}_last_consolidation"] = upper_bound
    save_state(state)

    # Stats loggen
    stats = get_stats()
    logger.info(
        f"Konsolidierung fertig: {chunk_count} neue Chunks | "
        f"Gesamt: {stats['active_count']} aktiv, {stats['archive_count']} archiviert"
    )

    return chunk_count


# =============================================================================
# Task 2: Proaktive Nachricht (LEGACY — nicht mehr verwendet)
# Ersetzt durch run_proactive() aus proactive.py
# TODO Roadmap: Diesen Block entfernen nach Bestätigung dass run_proactive stabil läuft.
# =============================================================================

def should_send_message(user_id, now):
    berlin = now_berlin()
    if berlin.hour < ACTIVE_HOURS_START or berlin.hour >= ACTIVE_HOURS_END:
        return False

    last_msg = get_last_message_time(user_id)
    if not last_msg:
        return False

    silence = (now_utc() - last_msg).total_seconds() / 3600
    if silence < SILENCE_THRESHOLD_HOURS:
        return False

    state = load_state()
    last_hb = state.get(f"{user_id}_message")
    if last_hb:
        last_hb_dt = safe_parse_dt(last_hb)
        if last_hb_dt:
            cooldown = (now_utc() - last_hb_dt).total_seconds() / 3600
            if cooldown < HEARTBEAT_COOLDOWN_HOURS:
                return False

    return True


def maybe_send_message(user_id, context_name, now):
    if not should_send_message(user_id, now):
        logger.info("Proaktive Nachricht: Bedingungen nicht erfuellt, skip.")
        return

    heartbeat_path = os.path.join(PROJECT_DIR, "heartbeat.md")
    try:
        with open(heartbeat_path, "r", encoding="utf-8") as f:
            heartbeat_prompt = f.read()
    except FileNotFoundError:
        logger.info("Keine heartbeat.md, skip.")
        return

    logger.info("Pruefe ob proaktive Nachricht sinnvoll...")

    import requests
    from core.ollama_client import build_system_prompt
    system_prompt = build_system_prompt(context_name, user_id)
    history = get_chat_history(user_id, limit=10)

    prompt = f"""{heartbeat_prompt}

---
Aktuelle Uhrzeit: {now_berlin().strftime('%A, %d. %B %Y, %H:%M Uhr')}

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
            logger.info("Kimi: nichts zu tun.")
        else:
            logger.info(f"Sende: {reply[:100]}")
            send_message(user_id, reply + "\n\n[kimi/heartbeat]")
            save_message(user_id, "assistant", reply)

        state = load_state()
        state[f"{user_id}_message"] = to_iso()
        save_state(state)

    except Exception as e:
        logger.error(f"Proaktive Nachricht Fehler: {e}")


# =============================================================================
# Task 3: Iterative Task-Verarbeitung (aus legacy uebernommen)
# =============================================================================

def process_tasks():
    """Verarbeitet offene Tasks — eine Iteration pro Heartbeat-Durchlauf."""
    import requests
    from core.ollama_client import build_system_prompt

    pending = get_pending_tasks()
    if not pending:
        return

    for task in pending:
        task_id = task["id"]
        user_id = task["user_id"]
        context_name = task.get("context_name")
        iteration = task["current_iteration"]

        if iteration >= task["max_iterations"]:
            task["status"] = "done"
            save_task(task)
            _deliver_task(task)
            continue

        logger.info(f"Task {task_id}: Iteration {iteration + 1}/{task['max_iterations']}")
        task["status"] = "running"

        system_prompt = build_system_prompt(context_name, user_id)
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
                timeout=180,
            )
            response.raise_for_status()
            result = response.json().get("message", {}).get("content", "").strip()

            task["iterations"].append({
                "iteration": iteration + 1,
                "result": result,
                "timestamp": to_iso(),
            })
            task["current_iteration"] = iteration + 1

            if TASK_DONE_TOKEN in result.upper().replace(" ", "_"):
                logger.info(f"Task {task_id}: DONE nach Iteration {iteration + 1}")
                task["status"] = "done"
                save_task(task)
                _deliver_task(task)
            elif task["current_iteration"] >= task["max_iterations"]:
                logger.info(f"Task {task_id}: Max Iterationen erreicht")
                task["status"] = "done"
                save_task(task)
                _deliver_task(task)
            else:
                save_task(task)

        except Exception as e:
            logger.error(f"Task {task_id}: Fehler: {e}")
            save_task(task)


def _deliver_task(task):
    """Sendet das Endergebnis per WhatsApp."""
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
    save_task(task)
    logger.info(f"Task {task_id}: zugestellt ({iterations_count} Iterationen)")


# =============================================================================
# Main
# =============================================================================

def run_heartbeat(user_id, context_name):
    now = now_utc()
    berlin = now_berlin()
    logger.info(f"--- {context_name} ({user_id}) ---")

    # 1. Konsolidierung
    run_consolidation(user_id)

    # 2. Deduplizierung (nach Konsolidierung)
    try:
        dedup_count = deduplicate_active()
        if dedup_count > 0:
            logger.info(f"Deduplizierung: {dedup_count} Duplikate archiviert")
    except Exception as e:
        logger.warning(f"Deduplizierung fehlgeschlagen: {e}")

    # 3. Decay (Gewichts- und Confidence-Alterung)
    try:
        decay_stats = run_decay()
        if decay_stats["decayed"] > 0 or decay_stats["archived"] > 0:
            logger.info(f"Decay: {decay_stats['decayed']} angepasst, {decay_stats['archived']} archiviert")
    except Exception as e:
        logger.warning(f"Decay fehlgeschlagen: {e}")

    # 3b. Reflexion (Mr. Robot denkt eigenständig nach)
    try:
        state = load_state()
        last_reflection = state.get(f"{user_id}_last_reflection")
        do_reflect = True

        if last_reflection:
            last_ref_dt = safe_parse_dt(last_reflection)
            if last_ref_dt:
                age_hours = (now - last_ref_dt).total_seconds() / 3600
                do_reflect = age_hours >= 12
            else:
                do_reflect = True  # Kaputtes Datum → reflektieren

        if do_reflect:
            from reflection import run_reflection
            chunk_id = run_reflection(user_id)
            if chunk_id:
                state = load_state()
                state[f"{user_id}_last_reflection"] = to_iso(now)
                save_state(state)
                logger.info(f"Reflexion erzeugt: {chunk_id[:8]}")
        else:
            logger.info("Reflexion: Cooldown nicht erreicht, skip")
    except Exception as e:
        logger.warning(f"Reflexion fehlgeschlagen: {e}")

    # 4. Proaktive Nachrichten (Event-basiert)
    try:
        # Briefing-Fenster dürfen den Stille-Check umgehen — sie sollen kommen
        # wenn Tommy aktiv war, nicht nur bei langer Stille.
        # Aber: max 1 Briefing pro Fenster (eigener Cooldown).
        is_morning = 7 <= berlin.hour < 10
        is_evening = 20 <= berlin.hour < 22
        is_briefing_window = is_morning or is_evening
        allow_proactive = False

        if is_briefing_window:
            state = load_state()
            cooldown_key = f"{user_id}_last_briefing_morning" if is_morning else f"{user_id}_last_briefing_evening"
            last_briefing = state.get(cooldown_key)
            if last_briefing:
                last_br_dt = safe_parse_dt(last_briefing)
                if last_br_dt:
                    age_hours = (now - last_br_dt).total_seconds() / 3600
                    allow_proactive = age_hours >= 20
                else:
                    allow_proactive = True  # Kaputtes Datum → erlauben
            else:
                allow_proactive = True
        else:
            allow_proactive = should_send_message(user_id, now)

        if allow_proactive:
            sent = run_proactive(user_id, context_name, now)
            if sent:
                state = load_state()
                state[f"{user_id}_message"] = to_iso(now)
                if is_morning:
                    state[f"{user_id}_last_briefing_morning"] = to_iso(now)
                elif is_evening:
                    state[f"{user_id}_last_briefing_evening"] = to_iso(now)
                save_state(state)
        else:
            logger.info("Proaktive Nachricht: Cooldown/Zeitfenster nicht erfuellt, skip.")
    except Exception as e:
        logger.warning(f"Proaktiv-Engine fehlgeschlagen: {e}")

    # 5. Autonomie-Engine (Soul-PR + Tier-2 Selbstmodifikation)
    try:
        run_autonomy(user_id)
    except Exception as e:
        logger.warning(f"Autonomie-Engine fehlgeschlagen: {e}")

    # State
    state = load_state()
    state[f"{user_id}_last_run"] = to_iso(now)
    save_state(state)

    logger.info(f"--- fertig ---")


def main():
    logger.info("=== Heartbeat gestartet ===")
    init_waha(WAHA_API_KEY)

    for user_id, context_name in USER_CONTEXTS.items():
        run_heartbeat(user_id, context_name)

    # Tasks verarbeiten (user-uebergreifend)
    process_tasks()

    logger.info("=== Heartbeat abgeschlossen ===")


if __name__ == "__main__":
    main()
