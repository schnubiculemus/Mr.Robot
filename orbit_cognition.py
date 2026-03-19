"""
SchnuBot.ai - Kognitions-Heartbeat (orbit_cognition.py)

Wird von Cron alle 2h ausgeführt.
Führt alle Kognitions-Module aus und meldet Ergebnisse an ORBIT zurück.

Module:
- Tagebuch (abends 20-23h)
- Introspection (MIRROR-basiert)
- Moltbook Exploration (autonom)
- Innerer Dialog (autonom)
- Autonome Reflexion (autonom)

Nach jedem erfolgreichen Lauf:
- cognition_output Trigger in ORBIT
- cognition_session Eintrag in heartbeat_state.json
  → wird von ollama_client.load_cognition_echo() gelesen
  → Kimi im Chat-Prompt sieht was sie zuletzt gedacht hat

Innere Vielstimmigkeit: jedes Modul liest den cognition_session State
des vorherigen Moduls und kann darauf aufbauen.
"""

import os
import sys
import logging

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, PROJECT_DIR)
os.chdir(PROJECT_DIR)

from config import USER_CONTEXTS
from core.datetime_utils import now_utc, now_berlin, to_iso
from core.state import load_state, save_state
import orbit

from logging.handlers import RotatingFileHandler as _RFH
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [KOGNITION] %(message)s",
    handlers=[
        _RFH(os.path.join(PROJECT_DIR, "logs", "cognition.log"), maxBytes=5*1024*1024, backupCount=3),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger(__name__)


# =============================================================================
# Session-Tracking
# =============================================================================

def _session_write(state: dict, user_id: str, module: str, chunk_id: str, topic: str) -> dict:
    """
    Schreibt ein Modul-Ergebnis in die cognition_session im State.
    Wird von load_cognition_echo() gelesen um den Chat-Prompt zu befüllen.
    """
    if "cognition_session" not in state:
        state["cognition_session"] = {}
    state["cognition_session"][module] = {
        "chunk_id": chunk_id,
        "topic": topic,
        "timestamp": to_iso(),
        "user_id": user_id,
    }
    return state


def _session_read(state: dict, module: str) -> dict | None:
    """Liest den letzten Eintrag eines Moduls aus der cognition_session."""
    return state.get("cognition_session", {}).get(module)


def _build_session_context(state: dict, modules: list[str]) -> str:
    """
    Baut einen kompakten Kontext-String aus den Ergebnissen der angegebenen Module.
    Wird als extra_system an nachfolgende Module übergeben.
    """
    session = state.get("cognition_session", {})
    lines = []
    module_labels = {
        "diary":                 "Tagebuch",
        "introspection":         "Mirror-Analyse",
        "moltbook":              "Moltbook",
        "inner_dialogue":        "Innerer Dialog",
        "autonomous_reflection": "Autonome Reflexion",
    }
    for module in modules:
        entry = session.get(module)
        if not entry:
            continue
        label = module_labels.get(module, module)
        topic = entry.get("topic", "")
        if topic:
            lines.append(f"- {label}: {topic[:120]}")

    if not lines:
        return ""
    return "Was ich in dieser Kognitions-Session bereits gedacht habe:\n" + "\n".join(lines)


# =============================================================================
# ORBIT-Trigger
# =============================================================================

def _cognition_output(user_id: str, source: str, topic: str, relevance: str = "weak"):
    """Meldet ein Kognitions-Ergebnis an ORBIT."""
    try:
        orbit.create_trigger(
            trigger_type="cognition_output",
            source=f"cognition:{source}",
            payload={
                "user_id": user_id,
                "source": source,
                "topic_core": topic,
                "relevance": relevance,
            },
        )
        logger.debug(f"cognition_output gemeldet: {source} — '{topic[:60]}'")
    except Exception as e:
        logger.warning(f"cognition_output Trigger fehlgeschlagen: {e}")


# =============================================================================
# Kognitions-Run
# =============================================================================

def run_kognition(user_id: str, context_name: str):
    now = now_utc()
    berlin = now_berlin()
    state = load_state()
    logger.info(f"--- Kognition: {context_name} ({user_id}) ---")

    # ── Tagebuch (abends 20–23h) ─────────────────────────────────────────────
    try:
        is_evening = 20 <= berlin.hour < 23
        if is_evening:
            from diary import run_diary
            result = run_diary(user_id)
            if result:
                filepath, chunk_id = result
                logger.info(f"Tagebuch geschrieben: {filepath}")
                _cognition_output(user_id, "diary", "Kimis Tagebucheintrag", "weak")
                state = load_state()
                state = _session_write(state, user_id, "diary", chunk_id, "Tagebucheintrag")
                save_state(state)
            else:
                logger.info("Tagebuch: bereits heute geschrieben")
        else:
            logger.debug("Tagebuch: kein Abend-Fenster")
    except Exception as e:
        logger.warning(f"Tagebuch fehlgeschlagen: {e}")

    # ── Introspection ────────────────────────────────────────────────────────
    try:
        state = load_state()
        last_introspection = state.get(f"{user_id}_last_introspection")
        from introspection import run_introspection
        chunk_id = run_introspection(user_id, last_introspection)
        if chunk_id:
            state = load_state()
            state[f"{user_id}_last_introspection"] = to_iso(now)
            state = _session_write(state, user_id, "introspection", chunk_id,
                                   "Verhaltensreflexion aus MIRROR-Daten")
            save_state(state)
            logger.info(f"Introspection: {chunk_id[:8]}")
            _cognition_output(user_id, "introspection",
                              "Verhaltensreflexion aus MIRROR-Daten", "medium")
    except Exception as e:
        logger.warning(f"Introspection fehlgeschlagen: {e}")

    # ── Moltbook Exploration ─────────────────────────────────────────────────
    try:
        state = load_state()
        last_moltbook = state.get(f"{user_id}_last_moltbook")
        from core.moltbook_explorer import run_moltbook_exploration
        chunk_id = run_moltbook_exploration(user_id, last_moltbook)
        if chunk_id:
            state = load_state()
            state[f"{user_id}_last_moltbook"] = to_iso(now)
            state = _session_write(state, user_id, "moltbook", chunk_id,
                                   "Moltbook Exploration abgeschlossen")
            save_state(state)
            logger.info(f"Moltbook: {chunk_id[:8]}")
            _cognition_output(user_id, "moltbook",
                              "Moltbook Exploration abgeschlossen", "weak")
    except Exception as e:
        logger.warning(f"Moltbook fehlgeschlagen: {e}")

    # ── Innerer Dialog ───────────────────────────────────────────────────────
    # Liest was Introspection und Moltbook herausgefunden haben
    try:
        state = load_state()
        last_inner = state.get(f"{user_id}_last_inner_dialogue")

        # Session-Kontext der Vorgänger-Module aufbauen
        prior_context = _build_session_context(state, ["introspection", "moltbook"])

        from inner_dialogue import run_inner_dialogue
        chunk_id = run_inner_dialogue(
            user_id,
            last_inner,
            session_context=prior_context or None,
        )
        if chunk_id:
            state = load_state()
            state[f"{user_id}_last_inner_dialogue"] = to_iso(now)
            state = _session_write(state, user_id, "inner_dialogue", chunk_id,
                                   "Innerer Dialog mit früheren Reflexionen")
            save_state(state)
            logger.info(f"Innerer Dialog: {chunk_id[:8]}")
            _cognition_output(user_id, "inner_dialogue",
                              "Innerer Dialog mit früheren Reflexionen", "weak")
    except Exception as e:
        logger.warning(f"Innerer Dialog fehlgeschlagen: {e}")

    # ── Autonome Reflexion ───────────────────────────────────────────────────
    # Liest was alle Vorgänger-Module herausgefunden haben
    try:
        state = load_state()
        last_autonomous = state.get(f"{user_id}_last_autonomous_reflection")

        # Session-Kontext aller bisherigen Module aufbauen
        prior_context = _build_session_context(
            state, ["introspection", "moltbook", "inner_dialogue"]
        )

        from autonomous_reflection import run_autonomous_reflection
        chunk_id = run_autonomous_reflection(
            user_id,
            last_autonomous,
            session_context=prior_context or None,
        )
        if chunk_id:
            state = load_state()
            state[f"{user_id}_last_autonomous_reflection"] = to_iso(now)
            state = _session_write(state, user_id, "autonomous_reflection", chunk_id,
                                   "Autonome Reflexion über offene Fragen")
            save_state(state)
            logger.info(f"Autonome Reflexion: {chunk_id[:8]}")
            _cognition_output(user_id, "autonomous_reflection",
                              "Autonome Reflexion über offene Fragen", "medium")
    except Exception as e:
        logger.warning(f"Autonome Reflexion fehlgeschlagen: {e}")

    # ── Kognitions-Feedback-Schleife ───────────────────────────────────────────
    # Hat sich ein Gedanke von vor 14-60 Tagen bewahrheitet?
    try:
        state = load_state()
        last_feedback = state.get(f"{user_id}_last_cognition_feedback")
        from cognition_feedback import run_cognition_feedback
        count = run_cognition_feedback(user_id, last_feedback)
        if count > 0:
            state = load_state()
            state[f"{user_id}_last_cognition_feedback"] = to_iso(now)
            save_state(state)
            logger.info(f"Kognitions-Feedback: {count} Chunks verarbeitet")
            _cognition_output(user_id, "cognition_feedback",
                              f"Feedback fuer {count} frueheren Gedanken", "weak")
    except Exception as e:
        logger.warning(f"Kognitions-Feedback fehlgeschlagen: {e}")

    # ── Tommy-Modell ─────────────────────────────────────────────────────────
    # Läuft nach allen anderen Modulen — hat den vollständigen Session-Kontext
    try:
        state = load_state()
        last_tommy = state.get(f"{user_id}_last_tommy_observation")
        from tommy_model import run_tommy_observation
        chunk_id = run_tommy_observation(user_id, last_tommy)
        if chunk_id:
            state = load_state()
            state[f"{user_id}_last_tommy_observation"] = to_iso(now)
            state = _session_write(state, user_id, "tommy_model", chunk_id,
                                   "Neue Beobachtung ueber Tommy")
            save_state(state)
            logger.info(f"Tommy-Modell: {chunk_id[:8]}")
            _cognition_output(user_id, "tommy_model",
                              "Neue Beobachtung ueber Tommy", "weak")
    except Exception as e:
        logger.warning(f"Tommy-Modell fehlgeschlagen: {e}")

    # ── Briefing (Morgen 7-10h, Abend 20-22h) ───────────────────────────────
    # Ollama laeuft hier sowieso — daher Briefings hier generieren, nicht im ORBIT-Tick
    try:
        is_morning_b = 7 <= berlin.hour < 10
        is_evening_b = 20 <= berlin.hour < 22
        briefing_type = None
        if is_morning_b:
            briefing_type = "morning_briefing"
        elif is_evening_b:
            briefing_type = "evening_briefing"

        if briefing_type:
            today_str = now_utc().isoformat()[:10]
            from core.database import get_connection as _gc2
            conn_b = _gc2()
            try:
                already_sent = conn_b.execute(
                    "SELECT COUNT(*) FROM orbit_proactive_messages" +
                    " WHERE message_type = ? AND created_at LIKE ? AND release_state = 'sent'",
                    (briefing_type, today_str + "%")
                ).fetchone()[0]
            finally:
                conn_b.close()

            if already_sent:
                logger.info(f"Briefing: {briefing_type} heute bereits gesendet, skip")
            else:
                from core.ollama_client import chat as _ollama_chat
                from core.database import get_chat_history as _get_history, save_message as _save_msg
                from config import OWNER_ID as _OWNER_ID

                context_name_b = USER_CONTEXTS.get(user_id, "Tommy")
                is_morning_flag = briefing_type == "morning_briefing"
                prompt_b = (
                    "Erstelle ein kurzes " +
                    ("Morgen-Briefing" if is_morning_flag else "Abend-Briefing") +
                    ". Schaue in dein Gedaechtnis nach offenen Themen, Terminen " +
                    "oder relevanten Entwicklungen. " +
                    "Wenn es nichts Relevantes gibt: KEIN_BRIEFING. " +
                    "Sonst: maximal 3 kurze Punkte, kein Markdown, Fliesstext."
                )
                history_b = _get_history(user_id, limit=6)
                reply_b, _ = _ollama_chat(user_id, prompt_b, history_b, context_name_b)

                if reply_b and "KEIN_BRIEFING" not in reply_b.upper():
                    content_b = reply_b.strip()
                    from core.whatsapp import send_message as _send
                    _send(_OWNER_ID, content_b)
                    _save_msg(_OWNER_ID, "assistant", content_b)

                    import uuid as _uuid2
                    mid_b = str(_uuid2.uuid4())
                    from core.database import get_connection as _gc3
                    conn_b2 = _gc3()
                    try:
                        conn_b2.execute(
                            "INSERT INTO orbit_proactive_messages" +
                            " (id, message_type, release_state, primary_origin," +
                            " reason, channel_target, created_at, updated_at)" +
                            " VALUES (?, ?, 'sent', ?, ?, ?, ?, ?)",
                            (mid_b, briefing_type, "orbit_cognition",
                             content_b[:200], _OWNER_ID, to_iso(), to_iso())
                        )
                        conn_b2.commit()
                    finally:
                        conn_b2.close()

                    logger.info(f"Briefing gesendet: {briefing_type} | {content_b[:60]}")
                else:
                    logger.info(f"Briefing: kein Inhalt fuer {briefing_type}")
    except Exception as e:
        logger.warning(f"Briefing fehlgeschlagen: {e}")

    # Echo-Cache invalidieren damit der nächste Chat-Prompt frische Daten sieht
    try:
        from core.ollama_client import invalidate_cognition_echo_cache
        invalidate_cognition_echo_cache()
        logger.debug("Kognitions-Echo Cache invalidiert")
    except Exception as e:
        logger.debug(f"Cache-Invalidierung fehlgeschlagen (unkritisch): {e}")

    # ORBIT heartbeat-Trigger
    try:
        orbit.create_trigger(
            trigger_type="heartbeat",
            source="orbit_cognition",
            payload={"user_id": user_id, "source": "cognition_run"},
        )
    except Exception as e:
        logger.warning(f"ORBIT heartbeat-Trigger fehlgeschlagen: {e}")

    # Kognitions-Run in Heartbeat-Timeline loggen
    try:
        from core.heartbeat_log import log_cognition_run
        state_final = load_state()
        session = state_final.get("cognition_session", {})
        today = to_iso(now)[:10]  # YYYY-MM-DD
        cognition_results = {}
        for module in ["diary", "introspection", "moltbook", "inner_dialogue",
                       "autonomous_reflection", "tommy_model", "cognition_feedback"]:
            entry = session.get(module)
            if entry and entry.get("timestamp", "")[:10] == today:
                chunk_id = entry.get("chunk_id", "ok")
                cognition_results[module] = chunk_id if chunk_id else "ok"
            else:
                cognition_results[module] = "skip"
        log_cognition_run(user_id, cognition_results)
    except Exception as e:
        logger.debug(f"Timeline-Log fehlgeschlagen (unkritisch): {e}")

    logger.info(f"--- Kognition fertig ---")


# =============================================================================
# Main
# =============================================================================

def main():
    logger.info("=== Kognitions-Heartbeat gestartet ===")

    for user_id, context_name in USER_CONTEXTS.items():
        run_kognition(user_id, context_name)

    logger.info("=== Kognitions-Heartbeat abgeschlossen ===")


if __name__ == "__main__":
    main()
