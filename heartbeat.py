"""
SchnuBot.ai - Heartbeat (Memory-Kern)

Läuft alle 30 Minuten per Cron.
Zuständig für den Memory-Kern — Konsolidierung, Deduplizierung, Decay.

Kognition (Tagebuch, Introspection, Moltbook, Innerer Dialog, Autonome Reflexion)
wurde zu ORBIT migriert — läuft dort als heartbeat-Routinen.
"""

import os
import sys
import logging

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, PROJECT_DIR)
os.chdir(PROJECT_DIR)

from config import WAHA_API_KEY, USER_CONTEXTS
from core.datetime_utils import now_utc, now_berlin, safe_parse_dt, to_iso
from core.state import load_state, save_state
from core.database import get_connection
from core.whatsapp import init_waha

from memory.consolidator import consolidate_turns
from memory.merge import deduplicate_active
from memory.memory_store import get_stats
from decay import run_decay

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


# =============================================================================
# Hilfsfunktionen
# =============================================================================

def get_new_turns(user_id, since_iso, until_iso=None):
    """Holt neue Turns seit dem letzten Lauf."""
    conn = get_connection()
    cursor = conn.cursor()
    if since_iso:
        if until_iso:
            cursor.execute(
                "SELECT role, content FROM messages "
                "WHERE phone_number = ? AND timestamp > ? AND timestamp <= ? "
                "ORDER BY timestamp ASC, id ASC",
                (user_id, since_iso, until_iso),
            )
        else:
            cursor.execute(
                "SELECT role, content FROM messages "
                "WHERE phone_number = ? AND timestamp > ? "
                "ORDER BY timestamp ASC, id ASC",
                (user_id, since_iso),
            )
    else:
        cursor.execute(
            "SELECT role, content FROM messages "
            "WHERE phone_number = ? "
            "ORDER BY timestamp DESC, id DESC LIMIT 30",
            (user_id,),
        )
    rows = cursor.fetchall()
    conn.close()
    turns = [{"role": row["role"], "content": row["content"]} for row in rows]
    if not since_iso:
        turns.reverse()
    return turns


# =============================================================================
# Memory-Kern Jobs
# =============================================================================

def run_consolidation(user_id):
    """Neue Turns → Memory-Chunks (ChromaDB)."""
    state = load_state()
    last_run = state.get(f"{user_id}_last_consolidation")
    upper_bound = to_iso()
    turns = get_new_turns(user_id, last_run, until_iso=upper_bound)

    if not turns:
        logger.info("Konsolidierung: keine neuen Turns")
        return 0

    logger.info(f"Konsolidiere {len(turns)} neue Turns")
    chunk_count = consolidate_turns(turns)

    state = load_state()
    state[f"{user_id}_last_consolidation"] = upper_bound
    save_state(state)

    stats = get_stats()
    logger.info(
        f"Konsolidierung: {chunk_count} neue Chunks | "
        f"Gesamt: {stats['active_count']} aktiv, {stats['archive_count']} archiviert"
    )
    return chunk_count


def run_heartbeat(user_id, context_name):
    now = now_utc()
    logger.info(f"--- {context_name} ({user_id}) ---")

    from core.heartbeat_log import HeartbeatRun

    with HeartbeatRun(user_id) as hb_run:

        # 1. Konsolidierung — essenziell
        try:
            result = run_consolidation(user_id)
            detail = f"Konsol.: {result} neue Chunks" if result else "Konsol.: nichts Neues"
            hb_run.step("konsolidierung", "ok", detail)
        except Exception as e:
            logger.warning(f"Konsolidierung fehlgeschlagen: {e}")
            hb_run.step("konsolidierung", "error", str(e)[:80])

        # 2. Deduplizierung — nur einmal täglich (CPU-intensiv)
        try:
            from datetime import date
            state = load_state()  # State neu laden nach run_consolidation()
            last_dedup = state.get(f"{user_id}_last_dedup_date", "")
            today = date.today().isoformat()
            if last_dedup != today:
                dedup_count = deduplicate_active()
                state = load_state()
                state[f"{user_id}_last_dedup_date"] = today
                save_state(state)
                if dedup_count > 0:
                    logger.info(f"Deduplizierung: {dedup_count} Duplikate archiviert")
                    hb_run.step("deduplizierung", "ok", f"{dedup_count} archiviert")
                else:
                    hb_run.step("deduplizierung", "skip", "keine Duplikate")
            else:
                logger.debug("Deduplizierung: heute bereits gelaufen")
                hb_run.step("deduplizierung", "skip", "heute bereits gelaufen")
        except Exception as e:
            logger.warning(f"Deduplizierung fehlgeschlagen: {e}")
            hb_run.step("deduplizierung", "error", str(e)[:80])

        # 3. Decay
        try:
            decay_stats = run_decay()
            if decay_stats["decayed"] > 0 or decay_stats["archived"] > 0:
                logger.info(f"Decay: {decay_stats['decayed']} angepasst, {decay_stats['archived']} archiviert")
                hb_run.step("decay", "ok", f"{decay_stats['decayed']} angepasst, {decay_stats['archived']} archiviert")
            else:
                hb_run.step("decay", "skip", "")
        except Exception as e:
            logger.warning(f"Decay fehlgeschlagen: {e}")
            hb_run.step("decay", "error", str(e)[:80])

        # State aktualisieren
        state = load_state()
        state[f"{user_id}_last_run"] = to_iso(now)
        save_state(state)

    logger.info(f"--- fertig ---")


# =============================================================================
# Main
# =============================================================================

def main():
    logger.info("=== Heartbeat (Memory-Kern) gestartet ===")
    init_waha(WAHA_API_KEY)

    for user_id, context_name in USER_CONTEXTS.items():
        run_heartbeat(user_id, context_name)

    logger.info("=== Heartbeat abgeschlossen ===")


if __name__ == "__main__":
    main()
