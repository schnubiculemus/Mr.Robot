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

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, PROJECT_DIR)
os.chdir(PROJECT_DIR)

from config import OLLAMA_API_URL, OLLAMA_API_KEY, OLLAMA_MODEL, WAHA_API_KEY, USER_CONTEXTS
from core.datetime_utils import now_utc, now_berlin, safe_parse_dt, to_iso
from core.file_utils import atomic_write_json
from core.state import load_state, save_state
from core.database import get_connection, get_chat_history, save_message
from core.whatsapp import send_message, init_waha
from core.tasks import (
    get_pending_tasks, save_task, build_iteration_prompt, TASK_DONE_TOKEN,
    claim_task, release_task, is_claimed, generate_runner_id, deliver_task,
)
from memory.consolidator import consolidate_turns
from memory.merge import deduplicate_active
from memory.memory_store import get_stats
from proactive import run_proactive
from autonomy import run_autonomy
from decay import run_decay
from diary import run_diary

from logging.handlers import RotatingFileHandler as _RFH
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [HEARTBEAT] %(message)s",
    handlers=[
        _RFH(os.path.join(PROJECT_DIR, "logs", "heartbeat.log"), maxBytes=10*1024*1024, backupCount=5),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger(__name__)

# --- Konfiguration ---
ACTIVE_HOURS_START = 8
ACTIVE_HOURS_END = 22
SILENCE_THRESHOLD_HOURS = 3
HEARTBEAT_COOLDOWN_HOURS = 4



def get_last_message_time(user_id):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT timestamp FROM messages WHERE phone_number = ? ORDER BY timestamp DESC, id DESC LIMIT 1",
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
                ORDER BY timestamp ASC, id ASC
                """,
                (user_id, since_iso, until_iso),
            )
        else:
            cursor.execute(
                """
                SELECT role, content FROM messages
                WHERE phone_number = ? AND timestamp > ?
                ORDER BY timestamp ASC, id ASC
                """,
                (user_id, since_iso),
            )
    else:
        # Erster Lauf: letzte 30 Nachrichten
        cursor.execute(
            """
            SELECT role, content FROM messages
            WHERE phone_number = ?
            ORDER BY timestamp DESC, id DESC
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
    upper_bound = to_iso()

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
# Stille-Check (wird in run_heartbeat als Fallback außerhalb Briefing-Fenster genutzt)
# =============================================================================

def should_send_message(user_id, now):
    """Prüft ob eine proaktive Nachricht basierend auf Stille-Dauer sinnvoll ist."""
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


# =============================================================================
# Task-Verarbeitung
# =============================================================================

def process_tasks():
    """
    Verarbeitet offene Tasks — eine Iteration pro Heartbeat-Durchlauf.

    Ownership-Modell (P1.1):
    - Prüft ob ein Task bereits von app.py geclaimed ist (frischer Claim)
    - Übernimmt nur Tasks ohne Claim oder mit abgelaufenem (stale) Claim
    - Eigener runner_id verhindert Konflikte mit parallelen Heartbeat-Läufen
    """
    from core.ollama_client import build_system_prompt

    pending = get_pending_tasks()
    if not pending:
        return

    runner_id = generate_runner_id("heartbeat")

    for task in pending:
        task_id = task["id"]
        user_id = task["user_id"]
        context_name = task.get("context_name")
        iteration = task["current_iteration"]

        # --- Ownership-Check (P1.1) ---
        if is_claimed(task):
            logger.info(
                f"Task {task_id}: Bereits claimed von {task.get('runner_id')}, skip"
            )
            continue

        if iteration >= task["max_iterations"]:
            task["status"] = "done"
            save_task(task)
            deliver_task(task)
            continue

        # Task für diesen Heartbeat-Lauf claimen
        claim_task(task, runner_id)

        logger.info(f"Task {task_id}: Iteration {iteration + 1}/{task['max_iterations']} (runner={runner_id})")

        system_prompt = build_system_prompt(context_name, user_id, user_message=task.get("prompt", ""))
        iter_prompt = build_iteration_prompt(task)

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": iter_prompt},
        ]

        try:
            from api_utils import api_call_with_retry
            result_json = api_call_with_retry(
                url=f"{OLLAMA_API_URL}/api/chat",
                headers={
                    "Authorization": f"Bearer {OLLAMA_API_KEY}",
                    "Content-Type": "application/json",
                },
                json_payload={"model": OLLAMA_MODEL, "messages": messages, "stream": False},
                timeout=180,
            )

            if not result_json:
                logger.error(f"Task {task_id}: API nicht erreichbar nach Retry")
                release_task(task, runner_id)
                continue

            result = result_json.get("message", {}).get("content", "").strip()

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
                deliver_task(task)
            elif task["current_iteration"] >= task["max_iterations"]:
                logger.info(f"Task {task_id}: Max Iterationen erreicht")
                task["status"] = "done"
                save_task(task)
                deliver_task(task)
            else:
                # Claim freigeben — nächste Iteration beim nächsten Heartbeat
                release_task(task, runner_id)

        except Exception as e:
            logger.error(f"Task {task_id}: Fehler: {e}")
            release_task(task, runner_id)


# =============================================================================
# Main
# =============================================================================

def run_heartbeat(user_id, context_name):
    now = now_utc()
    berlin = now_berlin()
    logger.info(f"--- {context_name} ({user_id}) ---")

    from core.heartbeat_log import HeartbeatRun

    with HeartbeatRun(user_id) as hb_run:

        # 1. Konsolidierung
        try:
            result = run_consolidation(user_id)
            if isinstance(result, dict):
                new_c = result.get("created", 0)
                sup_c = result.get("superseded", 0)
                detail = f"Konsol.: {new_c} neu, {sup_c} ersetzt" if (new_c or sup_c) else "Konsol.: nichts"
            else:
                detail = "Konsol.: ok"
            hb_run.step("konsolidierung", "ok", detail)
        except Exception as e:
            logger.warning(f"Konsolidierung fehlgeschlagen: {e}")
            hb_run.step("konsolidierung", "error", str(e)[:80])

        # 2. Deduplizierung
        try:
            dedup_count = deduplicate_active()
            if dedup_count > 0:
                logger.info(f"Deduplizierung: {dedup_count} Duplikate archiviert")
                hb_run.step("deduplizierung", "ok", f"Dedup.: {dedup_count} archiviert")
            else:
                hb_run.step("deduplizierung", "skip", "")
        except Exception as e:
            logger.warning(f"Deduplizierung fehlgeschlagen: {e}")
            hb_run.step("deduplizierung", "error", str(e)[:80])

        # 3. Decay
        try:
            decay_stats = run_decay()
            if decay_stats["decayed"] > 0 or decay_stats["archived"] > 0:
                logger.info(f"Decay: {decay_stats['decayed']} angepasst, {decay_stats['archived']} archiviert")
                hb_run.step("decay", "ok", f"Decay: {decay_stats['decayed']} angepasst, {decay_stats['archived']} archiviert")
            else:
                hb_run.step("decay", "skip", "")
        except Exception as e:
            logger.warning(f"Decay fehlgeschlagen: {e}")
            hb_run.step("decay", "error", str(e)[:80])

        # 3b. Reflexion
        try:
            state = load_state()
            last_reflection = state.get(f"{user_id}_last_reflection")
            do_reflect = True
            if last_reflection:
                last_ref_dt = safe_parse_dt(last_reflection)
                if last_ref_dt:
                    age_hours = (now - last_ref_dt).total_seconds() / 3600
                    do_reflect = age_hours >= 12

            if do_reflect:
                from reflection import run_reflection
                chunk_id = run_reflection(user_id)
                if chunk_id:
                    state = load_state()
                    state[f"{user_id}_last_reflection"] = to_iso(now)
                    save_state(state)
                    logger.info(f"Reflexion erzeugt: {chunk_id[:8]}")
                    hb_run.step("reflexion", "ok", f"Reflexion: {chunk_id[:8]}")
                else:
                    hb_run.step("reflexion", "skip", "Kein Ergebnis")
            else:
                logger.info("Reflexion: Cooldown nicht erreicht, skip")
                hb_run.step("reflexion", "skip", "Cooldown")
        except Exception as e:
            logger.warning(f"Reflexion fehlgeschlagen: {e}")
            hb_run.step("reflexion", "error", str(e)[:80])

        # 3c. Tagebuch
        try:
            is_evening_for_diary = 20 <= berlin.hour < 23
            if is_evening_for_diary:
                result = run_diary(user_id)
                if result:
                    filepath, chunk_id = result
                    logger.info(f"Tagebuch geschrieben: {filepath}")
                    hb_run.step("tagebuch", "ok", "Tagebuch geschrieben")
                else:
                    hb_run.step("tagebuch", "skip", "Bereits heute geschrieben")
            else:
                logger.info("Tagebuch: Kein Abend-Fenster, skip")
                hb_run.step("tagebuch", "skip", "Kein Abend-Fenster")
        except Exception as e:
            logger.warning(f"Tagebuch fehlgeschlagen: {e}")
            hb_run.step("tagebuch", "error", str(e)[:80])

        # 3d. Introspection (MIRROR-basiert, datengetrieben)
        try:
            state = load_state()
            last_introspection = state.get(f"{user_id}_last_introspection")
            from introspection import run_introspection
            chunk_id = run_introspection(user_id, last_introspection)
            if chunk_id:
                state = load_state()
                state[f"{user_id}_last_introspection"] = to_iso(now)
                save_state(state)
                logger.info(f"Introspection erzeugt: {chunk_id[:8]}")
                hb_run.step("introspection", "ok", f"Introspection: {chunk_id[:8]}")
            else:
                hb_run.step("introspection", "skip", "")
        except Exception as e:
            logger.warning(f"Introspection fehlgeschlagen: {e}")
            hb_run.step("introspection", "error", str(e)[:80])

        # 3e. Moltbook Exploration (autonom, kuratorisch)
        try:
            state = load_state()
            last_moltbook = state.get(f"{user_id}_last_moltbook")
            from core.moltbook_explorer import run_moltbook_exploration
            chunk_id = run_moltbook_exploration(user_id, last_moltbook)
            if chunk_id:
                state = load_state()
                state[f"{user_id}_last_moltbook"] = to_iso(now)
                save_state(state)
                logger.info(f"MoltbookExplorer: Reflexion gespeichert: {chunk_id[:8]}")
                hb_run.step("moltbook", "ok", f"Exploration: {chunk_id[:8]}")
            else:
                hb_run.step("moltbook", "skip", "")
        except Exception as e:
            logger.warning(f"MoltbookExplorer fehlgeschlagen: {e}")
            hb_run.step("moltbook", "error", str(e)[:80])

        # 4. Proaktive Nachrichten
        try:
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
                        allow_proactive = True
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
                    hb_run.step("proaktiv", "ok", "Nachricht gesendet")
                else:
                    hb_run.step("proaktiv", "skip", "Kein Trigger")
            else:
                logger.info("Proaktive Nachricht: Cooldown/Zeitfenster nicht erfuellt, skip.")
                hb_run.step("proaktiv", "skip", "Cooldown")
        except Exception as e:
            logger.warning(f"Proaktiv-Engine fehlgeschlagen: {e}")
            hb_run.step("proaktiv", "error", str(e)[:80])

        # 5. Autonomie-Engine
        try:
            run_autonomy(user_id)
            hb_run.step("autonomie", "ok", "")
        except Exception as e:
            logger.warning(f"Autonomie-Engine fehlgeschlagen: {e}")
            hb_run.step("autonomie", "error", str(e)[:80])

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
