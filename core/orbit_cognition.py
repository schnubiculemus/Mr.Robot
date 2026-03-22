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
# Briefing
# =============================================================================

def _run_briefing(user_id: str, briefing_type: str, now) -> None:
    """
    Strukturiertes Morgen- oder Abend-Briefing.

    Ablauf:
    1. Kalender für heute + morgen abrufen (direkt via calendar_router)
    2. Offene Todos abrufen (direkt via todos.py)
    3. Kimi-eigene Todos (project='kimi', due_date=heute) einbeziehen
    4. Kimi formuliert aus echten Daten ein kompaktes Briefing
    5. Kimi-Todos nach dem Senden abhaken
    6. Eintrag in orbit_proactive_messages

    Kein Freitext-Prompt ohne Daten — Kimi bekommt immer konkrete Grundlage.
    """
    from config import OWNER_ID, WAHA_API_KEY
    from core.database import get_connection, save_message
    from core.whatsapp import send_message, init_waha
    from core.ollama_client import chat_internal
    from core.datetime_utils import now_utc, to_iso
    import uuid, json

    init_waha(WAHA_API_KEY)

    # ── Already-sent-Check ────────────────────────────────────────────────────
    today_str = now_utc().isoformat()[:10]
    conn = get_connection()
    try:
        already = conn.execute(
            "SELECT COUNT(*) FROM orbit_proactive_messages"
            " WHERE message_type = ? AND created_at LIKE ? AND release_state = 'sent'",
            (briefing_type, today_str + "%")
        ).fetchone()[0]
    finally:
        conn.close()

    if already:
        logger.info(f"Briefing: {briefing_type} heute bereits gesendet, skip")
        return

    is_morning = briefing_type == "morning_briefing"
    label = "Morgen-Briefing" if is_morning else "Abend-Briefing"

    # ── Morgens: orbit.log der letzten Nacht lesen ───────────────────────────
    log_context = ""
    if is_morning:
        try:
            from core.server_read import read_log
            raw_log = read_log("orbit", lines=40)
            if raw_log and "nicht gefunden" not in raw_log and "nicht verfügbar" not in raw_log:
                # Nur relevante Zeilen: Fehler, Kognition, gesendete Nachrichten
                import re as _re_log
                relevant = []
                for line in raw_log.split("\n"):
                    if any(k in line for k in ["ERROR", "WARNING", "Briefing gesendet", "Kognition", "DiaryNote", "Autonome Reflexion", "Chunk gespeichert", "fehlgeschlagen"]):
                        relevant.append(line.strip())
                if relevant:
                    log_context = "Relevante Ereignisse letzte Nacht (orbit.log):\n" + "\n".join(relevant[-15:])
            logger.info(f"Briefing: orbit.log gelesen ({len(log_context)} Zeichen)")
        except Exception as e:
            logger.debug(f"Briefing: Log-Lesen fehlgeschlagen (unkritisch): {e}")

    # ── Kalender abrufen ──────────────────────────────────────────────────────
    cal_text = ""
    try:
        from core.calendar.calendar_router import execute_calendar_action
        # Morgens: heute + morgen; abends: morgen
        if is_morning:
            heute = execute_calendar_action({"action": "list", "range": "today"})
            morgen = execute_calendar_action({"action": "list", "range": "tomorrow"})
            cal_text = f"Heute: {heute}\n\nMorgen: {morgen}"
        else:
            morgen = execute_calendar_action({"action": "list", "range": "tomorrow"})
            cal_text = f"Morgen: {morgen}"
        logger.info(f"Briefing: Kalender abgerufen ({len(cal_text)} Zeichen)")
    except Exception as e:
        cal_text = "(Kalender nicht verfügbar)"
        logger.warning(f"Briefing: Kalender fehlgeschlagen: {e}")

    # ── Todos abrufen ─────────────────────────────────────────────────────────
    todo_text = ""
    kimi_todos = []
    try:
        from core.todos import get_open_todos, get_overdue_todos, get_due_today, complete_todo

        overdue = get_overdue_todos(user_id)
        due_today = get_due_today(user_id)
        all_open = get_open_todos(user_id)

        # Kimi-eigene Todos (project='kimi') herausfiltern und merken zum Abhaken
        kimi_todos = [t for t in all_open if (t.get("project") or "").lower() == "kimi"]

        lines = []
        if overdue:
            lines.append("Überfällig: " + ", ".join(
                f"#{t['id']} {t['title']} (seit {t['due_date']})" for t in overdue[:3]
            ))
        if due_today:
            lines.append("Heute fällig: " + ", ".join(
                f"#{t['id']} {t['title']}" for t in due_today[:3]
            ))
        if kimi_todos:
            lines.append("Kimi-Vorhaben heute: " + ", ".join(
                f"#{t['id']} {t['title']}" for t in kimi_todos[:3]
            ))
        # Hochprio-Todos die noch keinen festen Tag haben
        high_prio = [t for t in all_open
                     if t.get("priority") == "hoch" and not t.get("due_date")
                     and t not in overdue and t not in due_today]
        if high_prio:
            lines.append("Hoch-Prio offen: " + ", ".join(
                f"#{t['id']} {t['title']}" for t in high_prio[:3]
            ))

        todo_text = "\n".join(lines) if lines else "Keine dringenden Todos."
        logger.info(f"Briefing: Todos abgerufen ({len(all_open)} offen, {len(kimi_todos)} Kimi)")
    except Exception as e:
        todo_text = "(Todos nicht verfügbar)"
        logger.warning(f"Briefing: Todos fehlgeschlagen: {e}")

    # ── Kimi formuliert Briefing aus echten Daten ─────────────────────────────
    doc_context = f"=== Kalender ===\n{cal_text}\n\n=== Todos ===\n{todo_text}"
    if log_context:
        doc_context += f"\n\n=== System (letzte Nacht) ===\n{log_context}"

    prompt = (
        f"Ich schicke Tommy jetzt sein {label}. "
        f"Ich habe die Kalender- und Todo-Daten vor mir. "
        + (f"Ich habe auch einen Blick in den orbit.log geworfen — wenn dort etwas Auffälliges steht, erwähne ich es kurz. " if log_context else "")
        + f"Ich fasse in 2-4 kurzen Sätzen zusammen was heute relevant ist — "
        f"Termine, fällige Aufgaben, eigene Vorhaben. "
        f"Wenn wirklich nichts relevant ist: NUR 'KEIN_BRIEFING' ausgeben. "
        f"Kein Markdown, kein Intro, kein 'Guten Morgen' — direkt zum Punkt. "
        f"Fließtext, max. 4 Sätze."
    )

    try:
        context_name = USER_CONTEXTS.get(user_id, "Tommy")
        reply, _ = chat_internal(
            user_id=user_id,
            message=prompt,
            chat_history=[],
            context_name=context_name,
            doc_context=doc_context,
        )
    except Exception as e:
        logger.warning(f"Briefing: chat_internal fehlgeschlagen: {e}")
        return

    if not reply or "KEIN_BRIEFING" in reply.upper():
        logger.info(f"Briefing: {briefing_type} — kein relevanter Inhalt, nicht gesendet")
        return

    content = reply.strip()
    if len(content) > 2000:
        content = content[:1997] + "..."

    # ── Senden ───────────────────────────────────────────────────────────────
    try:
        send_message(OWNER_ID, content)
        save_message(OWNER_ID, "assistant", content)
        logger.info(f"Briefing gesendet: {briefing_type} | {content[:80]}")
    except Exception as e:
        logger.warning(f"Briefing: send_message fehlgeschlagen: {e}")
        return

    # Kimi-Todos werden NICHT automatisch abgehakt — Kimi erledigt sie selbst.
    # Das Briefing erwähnt sie nur damit Kimi sie kennt und angehen kann.

    # ── orbit_proactive_messages Eintrag ──────────────────────────────────────
    try:
        mid = str(uuid.uuid4())
        conn = get_connection()
        try:
            conn.execute(
                "INSERT INTO orbit_proactive_messages"
                " (id, message_type, release_state, primary_origin,"
                " reason, channel_target, created_at, updated_at)"
                " VALUES (?, ?, 'sent', ?, ?, ?, ?, ?)",
                (mid, briefing_type, "orbit_cognition",
                 content[:200], OWNER_ID, to_iso(), to_iso())
            )
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        logger.warning(f"Briefing: proactive_messages Eintrag fehlgeschlagen: {e}")


# =============================================================================
# Kognitions-Run
# =============================================================================

def _get_gate_mode(user_id: str) -> dict:
    """6.x: Liest First-Line-Gate-Status. Steuert alle Meta-Module."""
    result = {"gate_active": False, "execution_required": False,
              "meta_cycle_count": 0, "todo_id": None, "todo_title": ""}
    try:
        from core.planner import get_planner_focus, META_CYCLE_THRESHOLD, META_CYCLE_HARD_LIMIT
        focus = get_planner_focus(user_id)
        if not focus or focus.get("primary_line_type") != "todo":
            return result
        todo_id = focus.get("primary_line_id")
        if not todo_id:
            return result
        from core.database import get_connection
        conn = get_connection()
        row = conn.execute(
            "SELECT first_line_gate_active, meta_cycle_count, first_meaningful_execution, title FROM todos WHERE id=?",
            (int(todo_id),)
        ).fetchone()
        conn.close()
        if not row or row["first_meaningful_execution"]:
            return result
        cycles = row["meta_cycle_count"] or 0
        result.update({
            "gate_active":        bool(row["first_line_gate_active"]) or cycles >= META_CYCLE_THRESHOLD,
            "execution_required": cycles >= META_CYCLE_HARD_LIMIT,
            "meta_cycle_count":   cycles,
            "todo_id":            todo_id,
            "todo_title":         (row["title"] or "")[:50],
        })
    except Exception as _ge:
        import logging; logging.getLogger(__name__).debug(f"_get_gate_mode: {_ge}")
    return result


def run_kognition(user_id: str, context_name: str):
    now = now_utc()
    berlin = now_berlin()
    state = load_state()
    _module_status = {}
    logger.info(f"--- Kognition: {context_name} ({user_id}) | {berlin.strftime("%H:%M")} ---")

    # 6.x: Gate-Modus -- steuert alle Meta-Module
    gate = _get_gate_mode(user_id)
    if gate["gate_active"]:
        logger.info(
            f"6.x Gate: {'EXECUTION_REQUIRED' if gate['execution_required'] else 'FIRST_LINE_GATE'} "
            f"-- Todo #{gate['todo_id']} '{gate['todo_title']}' ({gate['meta_cycle_count']} Zyklen)"
        )

    # ── Tagebuch (abends 20–23h) ─────────────────────────────────────────────
    try:
        is_evening = 20 <= berlin.hour < 23
        if is_evening:
            from diary import run_diary
            result = run_diary(user_id)
            if result:
                filepath, chunk_id = result
                logger.info(f"Tagebuch geschrieben: {filepath}")
                _module_status["diary"] = True
                _cognition_output(user_id, "diary", "Kimis Tagebucheintrag", "weak")
                state = load_state()
                state = _session_write(state, user_id, "diary", chunk_id, "Tagebucheintrag")
                save_state(state)
            else:
                logger.info("Tagebuch: bereits heute geschrieben")
            _module_status["diary"] = "skip"
        else:
            logger.debug("Tagebuch: kein Abend-Fenster")
            _module_status["diary"] = "window"
    except Exception as e:
        logger.warning(f"Tagebuch fehlgeschlagen: {e}")

    # ── Introspection ────────────────────────────────────────────────────────
    try:
        chunk_id = None
        if gate["execution_required"]:
            logger.info("6.x Gate: Introspection uebersprungen (execution_required)")
            _module_status["introspect"] = "gate_skip"
        else:
            if gate["gate_active"]:
                state = load_state()
                _ic = state.get(f"{user_id}_introspect_gate_count", 0) + 1
                state[f"{user_id}_introspect_gate_count"] = _ic
                save_state(state)
                if _ic % 2 != 0:
                    logger.info(f"6.x Gate: Introspection gedaempft (Lauf {_ic})")
                    _module_status["introspect"] = "gate_damped"
                else:
                    state = load_state()
                    last_introspection = state.get(f"{user_id}_last_introspection")
                    from introspection import run_introspection
                    chunk_id = run_introspection(user_id, last_introspection)
            else:
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
            _module_status["introspect"] = True
            _cognition_output(user_id, "introspection",
                              "Verhaltensreflexion aus MIRROR-Daten", "medium")
    except Exception as e:
        logger.warning(f"Introspection fehlgeschlagen: {e}")

    # ── Moltbook Exploration ─────────────────────────────────────────────────
    try:
        chunk_id = None
        if gate["gate_active"]:
            logger.info("6.x Gate: Moltbook uebersprungen (gate_active)")
            _module_status["moltbook"] = "gate_skip"
        else:
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
            _module_status["moltbook"] = True
            _cognition_output(user_id, "moltbook",
                              "Moltbook Exploration abgeschlossen", "weak")
    except Exception as e:
        logger.warning(f"Moltbook fehlgeschlagen: {e}")

    # ── Innerer Dialog ───────────────────────────────────────────────────────
    try:
        chunk_id = None
        if gate["execution_required"]:
            logger.info("6.x Gate: Innerer Dialog uebersprungen (execution_required)")
            _module_status["inner_dialogue"] = "gate_skip"
        else:
            state = load_state()
            last_inner = state.get(f"{user_id}_last_inner_dialogue")
            prior_context = _build_session_context(state, ["introspection", "moltbook"])
            if gate["gate_active"]:
                prior_context = (prior_context or "") + (
                    f"\n\n[6.x Gate] Todo '{gate['todo_title']}' wartet auf ersten echten Vollzug "
                    f"({gate['meta_cycle_count']} Meta-Zyklen). "
                    f"Konzentriere dich auf: Was ist der naechste konkrete Schritt?"
                )
            from inner_dialogue import run_inner_dialogue
            chunk_id = run_inner_dialogue(user_id, last_inner,
                                           session_context=prior_context or None)
        if chunk_id:
            state = load_state()
            state[f"{user_id}_last_inner_dialogue"] = to_iso(now)
            state = _session_write(state, user_id, "inner_dialogue", chunk_id,
                                   "Innerer Dialog mit früheren Reflexionen")
            save_state(state)
            logger.info(f"Innerer Dialog: {chunk_id[:8]}")
            _module_status["inner_dialogue"] = True
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

        if gate["execution_required"]:
            logger.info("6.x Gate: Autonome Reflexion uebersprungen (execution_required)")
            _module_status["auto_reflect"] = "gate_skip"
            chunk_id = None
        else:
            if gate["gate_active"]:
                prior_context = (prior_context or "") + (
                    f"\n\n[6.x Gate] Kein Platz fuer breite Reflexion. "
                    f"Todo '{gate['todo_title']}' braucht jetzt Vollzug, nicht weiteres Denken. "
                    f"Wenn ueberhaupt: nur naechster konkreter Schritt."
                )
            from autonomous_reflection import run_autonomous_reflection
            chunk_id = run_autonomous_reflection(
                user_id, last_autonomous, session_context=prior_context or None)
        if chunk_id:
            state = load_state()
            state[f"{user_id}_last_autonomous_reflection"] = to_iso(now)
            state = _session_write(state, user_id, "autonomous_reflection", chunk_id,
                                   "Autonome Reflexion über offene Fragen")
            save_state(state)
            logger.info(f"Autonome Reflexion: {chunk_id[:8]}")
            _module_status["auto_reflect"] = True
            _cognition_output(user_id, "autonomous_reflection",
                              "Autonome Reflexion über offene Fragen", "medium")

            # Starke Reflexion → spontane Tagebuch-Notiz (nicht nur abends)
            try:
                from memory.memory_store import get_chunk_by_id
                ref_chunk = get_chunk_by_id(chunk_id)
                if ref_chunk:
                    chunk_text = ref_chunk.get("text", "")
                    # Nur bei PROACTIVE oder langer Reflexion (substanziell genug)
                    if len(chunk_text) > 120:
                        from diary import run_diary_note
                        note_id = run_diary_note(user_id, chunk_text)
                        if note_id:
                            logger.info(f"DiaryNote aus Autonomer Reflexion: {note_id[:8]}")
            except Exception as _dn:
                logger.debug(f"DiaryNote-Trigger fehlgeschlagen (unkritisch): {_dn}")
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
            _module_status["tommy_model"] = True
            _cognition_output(user_id, "tommy_model",
                              "Neue Beobachtung ueber Tommy", "weak")
    except Exception as e:
        logger.warning(f"Tommy-Modell fehlgeschlagen: {e}")

    # ── Kalender-Awareness (abends 18-23h) ───────────────────────────────────
    # Checkt ob Tommy morgen Termine hat → cognition_output mit Kalender-Keywords
    # → ORBIT stuft Thread auf 'medium' → _maybe_autonomous_task → autonomer Task
    try:
        is_evening_cal = 18 <= berlin.hour < 23
        if is_evening_cal:
            from tommy_model import run_calendar_awareness
            cal_topic = run_calendar_awareness(user_id)
            if cal_topic:
                _cognition_output(user_id, "calendar_awareness", cal_topic, "medium")
                logger.info(f"CalendarAwareness: Trigger gefeuert — '{cal_topic}'")
    except Exception as e:
        logger.warning(f"Kalender-Awareness fehlgeschlagen: {e}")

    # ── Briefing (Morgen 7-10h, Abend 20-22h) ───────────────────────────────
    try:
        is_morning_b = 7 <= berlin.hour < 10
        is_evening_b = 20 <= berlin.hour < 22
        briefing_type = None
        if is_morning_b:
            briefing_type = "morning_briefing"
        elif is_evening_b:
            briefing_type = "evening_briefing"

        if briefing_type:
            _run_briefing(user_id, briefing_type, now)
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

    # ── Status-Summary ──────────────────────────────────────────────────────
    if _module_status:
        status_parts = [f"{k}={'✓' if v else '–'}" for k, v in _module_status.items()]
        logger.info(f"Kognition Summary: {' | '.join(status_parts)}")
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
