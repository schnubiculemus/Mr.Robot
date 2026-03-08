"""
SchnuBot.ai - Proaktiv-Engine (Phase 5b)
Referenz: Konzeptdokument V1.1

Intelligente proaktive Nachrichten basierend auf Memory-Chunks.
Drei Stufen:
  1. Zeitbasiert (Stille-Check) — heartbeat.py Logik
  2. Eventbasiert — Deadlines, Morgen-Briefing, offene Tasks
  3. Impulsbasiert — Gedanken, Widersprüche, Erinnerungen

Wird vom Heartbeat aufgerufen.
"""

import logging
from datetime import datetime, timezone

from config import OLLAMA_API_URL, OLLAMA_API_KEY, OLLAMA_MODEL
from core.database import get_chat_history, save_message
from core.whatsapp import send_message
from core.ollama_client import build_system_prompt
from core.datetime_utils import now_utc, now_berlin, safe_parse_dt, format_berlin
import random
from memory.memory_store import query_active, get_all_active

logger = logging.getLogger(__name__)

HEARTBEAT_OK_TOKEN = "HEARTBEAT_OK"


# =============================================================================
# Trigger-Erkennung
# =============================================================================

def check_triggers(user_id, now):
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

    # --- Stufe 3: Impuls-Trigger ---
    impuls = _check_gedanken_impuls()
    if impuls:
        triggers.append({
            "typ": "gedanken-impuls",
            "kontext": impuls,
            "prioritaet": 4,
        })

    erinnerung = _check_erinnerung()
    if erinnerung:
        triggers.append({
            "typ": "erinnerung",
            "kontext": erinnerung,
            "prioritaet": 4,
        })

    widerspruch = _check_widerspruch()
    if widerspruch:
        triggers.append({
            "typ": "widerspruch",
            "kontext": widerspruch,
            "prioritaet": 3,
        })

    # --- Stufe 4: Curiosity-Frage ---
    curiosity = _check_curiosity(user_id, now)
    if curiosity:
        triggers.append({
            "typ": "neugier-frage",
            "kontext": curiosity,
            "prioritaet": 5,
        })

    return triggers


def _is_morning_briefing_time(berlin):
    return 7 <= berlin.hour < 10


def _is_evening_briefing_time(berlin):
    return 20 <= berlin.hour < 22


def _build_morning_context(user_id):
    context_parts = []

    ws_results = query_active("aktuelle Arbeit Projekt Phase Status", n_results=5)
    working_states = [r for r in ws_results if r.get("chunk_type") == "working_state"]
    if working_states:
        ws_texts = [f"- {c['text']}" for c in working_states[:5]]
        context_parts.append("Aktuelle Arbeitsstände:\n" + "\n".join(ws_texts))

    dec_results = query_active("Entscheidung geplant nächster Schritt", n_results=5)
    decisions = [r for r in dec_results if r.get("chunk_type") == "decision"]
    if decisions:
        dec_texts = [f"- {c['text']}" for c in decisions[:3]]
        context_parts.append("Aktive Entscheidungen:\n" + "\n".join(dec_texts))

    ref_results = query_active("Selbstreflexion Erkenntnis Verbesserung", n_results=3)
    reflections = [r for r in ref_results if r.get("chunk_type") == "self_reflection"]
    if reflections:
        context_parts.append(f"Letzte Selbstreflexion: {reflections[0]['text']}")

    return "\n\n".join(context_parts) if context_parts else None


def _build_evening_context(user_id):
    context_parts = []

    dec_results = query_active("Entscheidung heute festgelegt beschlossen", n_results=5)
    decisions = [r for r in dec_results if r.get("chunk_type") == "decision"]
    if decisions:
        dec_texts = [f"- {c['text']}" for c in decisions[:5]]
        context_parts.append("Aktive Entscheidungen:\n" + "\n".join(dec_texts))

    ws_results = query_active("aktuelle Arbeit Projekt Phase Status", n_results=5)
    working_states = [r for r in ws_results if r.get("chunk_type") == "working_state"]
    if working_states:
        ws_texts = [f"- {c['text']}" for c in working_states[:5]]
        context_parts.append("Aktuelle Arbeitsstände:\n" + "\n".join(ws_texts))

    ref_results = query_active("Selbstreflexion Erkenntnis Verbesserung", n_results=3)
    reflections = [r for r in ref_results if r.get("chunk_type") == "self_reflection"]
    if reflections:
        context_parts.append(f"Letzte Selbstreflexion: {reflections[0]['text']}")

    return "\n\n".join(context_parts) if context_parts else None


def _check_deadlines(now):
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
# Stufe 3: Impuls-Trigger
# =============================================================================

def _check_gedanken_impuls():
    """
    Prüft ob aktuelle self_reflection oder diary Chunks ein Thema haben
    das Kimi beschäftigt und das er von sich aus ansprechen könnte.
    Nur wenn mind. 2 Reflexionen vorhanden sind.
    """
    results = query_active("Gedanke Frage offen beschäftigt unklar", n_results=5)
    reflexionen = [r for r in results if r.get("chunk_type") in ("self_reflection", "diary")]

    if len(reflexionen) >= 2:
        texts = [f"- {r['text']}" for r in reflexionen[:3]]
        return "Eigene Gedanken/Reflexionen die mich beschäftigen:\n" + "\n".join(texts)

    return None


def _check_erinnerung():
    """
    Findet Entscheidungen oder working_states die länger nicht
    im Gespräch erwähnt wurden (älter als 5 Tage, noch aktiv).
    """
    results = query_active("offen geplant noch ausstehend todo nächster Schritt", n_results=10)
    now = now_utc()
    erinnerungen = []

    for chunk in results:
        if chunk.get("chunk_type") not in ("decision", "working_state"):
            continue
        created = chunk.get("created_at", "")
        if not created:
            continue
        created_dt = safe_parse_dt(created)
        if created_dt is None:
            continue
        age_days = (now - created_dt).days
        if 5 <= age_days <= 30:
            erinnerungen.append(f"- [{age_days}d] {chunk['text']}")

    if erinnerungen:
        return "Dinge die ich schon länger nicht angesprochen habe:\n" + "\n".join(erinnerungen[:3])

    return None


def _check_widerspruch():
    """
    Sucht nach Chunks die sich potenziell widersprechen könnten —
    einfache Heuristik: hard_facts mit ähnlichem Thema aber unterschiedlichem Inhalt.
    """
    results = query_active("geändert aktualisiert überholt anders früher jetzt", n_results=10)
    kandidaten = [r for r in results if r.get("chunk_type") in ("hard_fact", "decision", "preference")]

    if len(kandidaten) >= 2:
        texts = [f"- {r['text']}" for r in kandidaten[:3]]
        return "Mögliche Widersprüche im Gedächtnis:\n" + "\n".join(texts)

    return None


# =============================================================================
# Stufe 4: Curiosity-Trigger
# =============================================================================

def _check_curiosity(user_id, now):
    """
    Prüft ob eine Curiosity-Frage fällig ist.
    Cooldown: 2-3 Tage (zufällig). Zeitfenster: 9-21h.
    Kimi entscheidet frei welche Frage sie stellt — basierend auf
    vorhandenen Chunks und was noch fehlt.
    """
    from heartbeat import load_state, save_state, to_iso
    berlin = now_berlin()

    # Nur tagsüber
    if not (9 <= berlin.hour < 21):
        return None

    # Cooldown prüfen
    state = load_state()
    last_q = state.get(f"{user_id}_last_curiosity")
    if last_q:
        last_dt = safe_parse_dt(last_q)
        if last_dt:
            age_hours = (now - last_dt).total_seconds() / 3600
            cooldown_hours = state.get(f"{user_id}_curiosity_cooldown", 48)
            if age_hours < cooldown_hours:
                return None

    # Alle aktiven Chunks sammeln als Überblick für Kimi
    try:
        all_chunks = get_all_active()
    except Exception:
        all_chunks = []

    # Kompakte Zusammenfassung: was weiß ich schon?
    known_summary = []
    for c in all_chunks:
        ctype = c.get("chunk_type", "")
        if ctype in ("hard_fact", "preference", "decision"):
            known_summary.append(f"[{ctype}] {c['text'][:80]}")

    known_text = "\n".join(known_summary[:30]) if known_summary else "Noch kaum etwas gespeichert."

    context = f"""Was ich über Tommy weiß (Auszug):
{known_text}

Entscheide selbst: Worüber möchtest du mehr erfahren? 
Stell eine einzige, natürliche Frage — über etwas das dir fehlt oder dich interessiert.
Kein Thema ist vorgegeben. Du kannst völlig frei wählen."""

    # Cooldown setzen
    state[f"{user_id}_last_curiosity"] = to_iso(now)
    state[f"{user_id}_curiosity_cooldown"] = random.randint(48, 72)
    save_state(state)

    return context


# =============================================================================
# Nachricht generieren und senden
# =============================================================================

def generate_proactive_message(user_id, context_name, triggers, now):
    if not triggers:
        return None

    triggers.sort(key=lambda t: t["prioritaet"])

    trigger_text = ""
    for t in triggers:
        trigger_text += f"\n### Trigger: {t['typ']}\n{t['kontext']}\n"

    berlin = now_berlin()
    is_morning = 7 <= berlin.hour < 10

    system_prompt = build_system_prompt(context_name, user_id)
    history = get_chat_history(user_id, limit=5)

    morgen_hinweis = ""
    if is_morning:
        morgen_hinweis = """
MORGEN-BRIEFING HINWEIS:
Du meldest dich als erstes heute Morgen. Formuliere einen natürlichen, persönlichen Einstieg —
frag wie die Nacht war, mach eine kurze Bemerkung zur Tageszeit, oder steig direkt mit etwas
Relevantem ein das dich beschäftigt. Nicht immer dasselbe. Kein "Guten Morgen Tommy, hier ist
dein Briefing:" — das klingt wie ein Newsletter. Du bist ein Gegenüber, kein Assistent.
"""

    prompt = f"""Du bist Mr. Robot im Heartbeat-Modus. Du hast gerade dein Gedächtnis durchsucht und folgende Anlässe gefunden:

{trigger_text}

Aktuelle Zeit: {format_berlin(now)}
{morgen_hinweis}
REGELN:
- Schreib eine kurze, natürliche WhatsApp-Nachricht an Tommy.
- Wähle das Wichtigste — keine Aufzählung aller Trigger.
- Ton: locker, direkt, persönlich. Wie jemand der dich kennt und sich einfach meldet.
- Bei gedanken-impuls oder erinnerung: ruhig etwas Persönliches einbringen — "Ich hab grad gedacht..."
- Bei widerspruch: kurz und neugierig fragen, nicht anklagend.
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
    tagged = message + "\n\n[kimi/proaktiv]"
    send_message(user_id, tagged)
    save_message(user_id, "assistant", message)
    logger.info(f"Proaktive Nachricht gesendet: {message[:100]}")


# =============================================================================
# Hauptfunktion
# =============================================================================

def run_proactive(user_id, context_name, now):
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
