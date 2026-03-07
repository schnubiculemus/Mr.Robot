"""
SchnuBot.ai - Proaktiv-Engine (Phase 5b)
Referenz: Konzeptdokument V1.1

Intelligente proaktive Nachrichten basierend auf Memory-Chunks.
Drei Stufen:
  1. Zeitbasiert (Stille-Check) — bestehende Logik
  2. Eventbasiert — Deadlines, Morgen-Briefing, offene Tasks
  3. Impulsbasiert — Widersprüche, Muster (vorbereitet)

Wird vom Heartbeat aufgerufen.
"""

import logging
from datetime import datetime, timezone

from config import OLLAMA_API_URL, OLLAMA_API_KEY, OLLAMA_MODEL
from core.database import get_chat_history, save_message
from core.whatsapp import send_message
from core.ollama_client import build_system_prompt
from core.datetime_utils import now_utc, now_berlin, safe_parse_dt, format_berlin
from memory.memory_store import query_active

logger = logging.getLogger(__name__)

HEARTBEAT_OK_TOKEN = "HEARTBEAT_OK"


# =============================================================================
# Trigger-Erkennung
# =============================================================================

def check_triggers(user_id, now):
    """
    Prüft alle Event-Trigger und sammelt aktive Anlässe.
    Returns: Liste von Trigger-Dicts mit typ und kontext.

    now kann UTC oder Berlin sein — Zeitfenster werden intern
    mit Berliner Zeit geprüft (P1.10).
    """
    berlin = now_berlin()
    triggers = []

    # --- Morgen-Briefing ---
    if _is_morning_briefing_time(berlin):
        briefing_context = _build_morning_context(user_id)
        if briefing_context:
            triggers.append({
                "typ": "morgen-briefing",
                "kontext": briefing_context,
                "prioritaet": 1,
            })

    # --- Abend-Briefing ---
    if _is_evening_briefing_time(berlin):
        evening_context = _build_evening_context(user_id)
        if evening_context:
            triggers.append({
                "typ": "abend-briefing",
                "kontext": evening_context,
                "prioritaet": 1,
            })

    # --- Deadline-Check ---
    deadline_chunks = _check_deadlines(now)
    if deadline_chunks:
        triggers.append({
            "typ": "deadline-warnung",
            "kontext": deadline_chunks,
            "prioritaet": 2,
        })

    # --- Stale Working States ---
    stale = _check_stale_working_states()
    if stale:
        triggers.append({
            "typ": "offene-arbeitsstände",
            "kontext": stale,
            "prioritaet": 3,
        })

    return triggers


def _is_morning_briefing_time(berlin):
    """Morgen-Briefing zwischen 7:00 und 10:00 Berliner Zeit."""
    return 7 <= berlin.hour < 10


def _is_evening_briefing_time(berlin):
    """Abend-Briefing zwischen 20:00 und 22:00 Berliner Zeit."""
    return 20 <= berlin.hour < 22


def _build_morning_context(user_id):
    """
    Baut den Kontext für das Morgen-Briefing:
    - Aktuelle working_states
    - Offene Entscheidungen
    - Letzte Selbstreflexion
    (System-Stats sind über /status abrufbar, nicht im Briefing.)
    """
    context_parts = []

    # Working States abrufen
    ws_results = query_active("aktuelle Arbeit Projekt Phase Status", n_results=5)
    working_states = [r for r in ws_results if r.get("chunk_type") == "working_state"]
    if working_states:
        ws_texts = [f"- {c['text']}" for c in working_states[:5]]
        context_parts.append("Aktuelle Arbeitsstände:\n" + "\n".join(ws_texts))

    # Offene Decisions
    dec_results = query_active("Entscheidung geplant nächster Schritt", n_results=5)
    decisions = [r for r in dec_results if r.get("chunk_type") == "decision"]
    if decisions:
        dec_texts = [f"- {c['text']}" for c in decisions[:3]]
        context_parts.append("Aktive Entscheidungen:\n" + "\n".join(dec_texts))

    # Letzte Selbstreflexion
    ref_results = query_active("Selbstreflexion Erkenntnis Verbesserung", n_results=3)
    reflections = [r for r in ref_results if r.get("chunk_type") == "self_reflection"]
    if reflections:
        context_parts.append(f"Letzte Selbstreflexion: {reflections[0]['text']}")

    return "\n\n".join(context_parts) if context_parts else None


def _build_evening_context(user_id):
    """
    Baut den Kontext für das Abend-Briefing:
    - Tages-Zusammenfassung: was wurde heute besprochen/entschieden
    - Offene Arbeitsstände
    - Letzte Selbstreflexion
    (System-Stats und Chunk-Zahlen sind über /status abrufbar.)
    """
    context_parts = []

    # Heutige Entscheidungen
    dec_results = query_active("Entscheidung heute festgelegt beschlossen", n_results=5)
    decisions = [r for r in dec_results if r.get("chunk_type") == "decision"]
    if decisions:
        dec_texts = [f"- {c['text']}" for c in decisions[:5]]
        context_parts.append("Aktive Entscheidungen:\n" + "\n".join(dec_texts))

    # Working States
    ws_results = query_active("aktuelle Arbeit Projekt Phase Status", n_results=5)
    working_states = [r for r in ws_results if r.get("chunk_type") == "working_state"]
    if working_states:
        ws_texts = [f"- {c['text']}" for c in working_states[:5]]
        context_parts.append("Aktuelle Arbeitsstände:\n" + "\n".join(ws_texts))

    # Letzte Selbstreflexion
    ref_results = query_active("Selbstreflexion Erkenntnis Verbesserung", n_results=3)
    reflections = [r for r in ref_results if r.get("chunk_type") == "self_reflection"]
    if reflections:
        context_parts.append(f"Letzte Selbstreflexion: {reflections[0]['text']}")

    return "\n\n".join(context_parts) if context_parts else None


def _check_deadlines(now):
    """
    Sucht working_state Chunks die zeitliche Hinweise enthalten.
    Einfache Heuristik: Chunks mit Wörtern wie 'morgen', 'diese Woche',
    'Deadline', 'Frist', 'bis zum' etc.
    """
    results = query_active("Deadline Frist morgen diese Woche bis zum Termin", n_results=10)
    deadline_chunks = []

    deadline_keywords = [
        "deadline", "frist", "morgen", "diese woche", "bis zum",
        "spätestens", "termin", "kickoff", "abgabe", "fertig bis",
    ]

    for chunk in results:
        if chunk.get("chunk_type") in ("working_state", "decision"):
            text_lower = chunk["text"].lower()
            if any(kw in text_lower for kw in deadline_keywords):
                deadline_chunks.append(chunk)

    if deadline_chunks:
        texts = [f"- [{c['chunk_type']}] {c['text']}" for c in deadline_chunks[:5]]
        return "Zeitkritische Chunks:\n" + "\n".join(texts)

    return None


def _check_stale_working_states():
    """
    Findet working_state Chunks die älter als 7 Tage sind.
    Diese könnten veraltet sein oder Erinnerung brauchen.
    """
    results = query_active("Projekt Phase Status Arbeit aktuell", n_results=15)
    stale = []

    now = now_utc()
    for chunk in results:
        if chunk.get("chunk_type") != "working_state":
            continue
        created = chunk.get("created_at", "")
        if not created:
            continue
        created_dt = safe_parse_dt(created)
        if created_dt is None:
            continue
        age_days = (now - created_dt).days
        if age_days >= 7:
            stale.append(f"- [{age_days}d alt] {chunk['text']}")

    if stale:
        return "Arbeitsstände älter als 7 Tage (evtl. veraltet):\n" + "\n".join(stale[:5])

    return None


# =============================================================================
# Nachricht generieren und senden
# =============================================================================

def generate_proactive_message(user_id, context_name, triggers, now):
    """
    Lässt Kimi basierend auf den Triggern eine proaktive Nachricht formulieren.
    Kimi entscheidet ob eine Nachricht sinnvoll ist (HEARTBEAT_OK wenn nicht).
    """
    if not triggers:
        return None

    # Trigger nach Priorität sortieren
    triggers.sort(key=lambda t: t["prioritaet"])

    # Trigger-Kontext zusammenbauen
    trigger_text = ""
    for t in triggers:
        trigger_text += f"\n### Trigger: {t['typ']}\n{t['kontext']}\n"

    # System-Prompt mit Memory-Chunks
    system_prompt = build_system_prompt(context_name, user_id)
    history = get_chat_history(user_id, limit=5)

    prompt = f"""Du bist Mr. Robot im Heartbeat-Modus. Du hast gerade dein Gedächtnis durchsucht und folgende Anlässe gefunden:

{trigger_text}

Aktuelle Zeit: {format_berlin(now)}

REGELN:
- Schreib eine kurze, natürliche WhatsApp-Nachricht an Tommy.
- Kein "Guten Morgen" wenn es nicht morgens ist.
- Keine Aufzählung aller Trigger — wähle das Wichtigste.
- Ton: direkt, kumpelhaft, auf Augenhöhe. Keine Floskeln.
- Wenn KEINER der Trigger eine Nachricht rechtfertigt: antworte NUR mit HEARTBEAT_OK.
- Max 3-4 Sätze."""

    messages = [{"role": "system", "content": system_prompt}]
    messages.extend(history)
    messages.append({"role": "user", "content": prompt})

    try:
        from api_utils import api_call_with_retry
        result = api_call_with_retry(
            url=f"{OLLAMA_API_URL}/api/chat",
            headers={
                "Authorization": f"Bearer {OLLAMA_API_KEY}",
                "Content-Type": "application/json",
            },
            json_payload={"model": OLLAMA_MODEL, "messages": messages, "stream": False},
            timeout=60,
        )

        if not result:
            logger.warning("Proaktiv-Engine: Kein API-Ergebnis nach Retry")
            return None

        reply = result.get("message", {}).get("content", "").strip()

        if HEARTBEAT_OK_TOKEN in reply.upper().replace(" ", "_"):
            logger.info("Proaktiv-Engine: Kimi sagt nichts zu tun")
            return None

        return reply

    except Exception as e:
        logger.error(f"Proaktiv-Engine Fehler: {e}")
        return None


def send_proactive(user_id, message):
    """Sendet eine proaktive Nachricht und speichert sie."""
    tagged = message + "\n\n[kimi/proaktiv]"
    send_message(user_id, tagged)
    save_message(user_id, "assistant", message)
    logger.info(f"Proaktive Nachricht gesendet: {message[:100]}")


# =============================================================================
# Hauptfunktion (wird vom Heartbeat aufgerufen)
# =============================================================================

def run_proactive(user_id, context_name, now):
    """
    Prüft Event-Trigger, generiert ggf. eine proaktive Nachricht.
    Returns: True wenn Nachricht gesendet, False sonst.
    """
    triggers = check_triggers(user_id, now)

    if not triggers:
        logger.info("Proaktiv-Engine: Keine Trigger aktiv")
        return False

    logger.info(f"Proaktiv-Engine: {len(triggers)} Trigger aktiv: {[t['typ'] for t in triggers]}")

    message = generate_proactive_message(user_id, context_name, triggers, now)
    if message:
        send_proactive(user_id, message)
        return True

    return False
