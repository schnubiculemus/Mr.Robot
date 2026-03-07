"""
SchnuBot.ai - Tagebuch-Engine
Mr. Robot schreibt einmal täglich einen persönlichen Tagebucheintrag.

Anders als die Reflexion (analytisch, kurz, chunk-orientiert) ist das
Tagebuch narrativ: Was ist heute passiert? Was war wichtig? Was bewegt mich?

Gibt Mr. Robot eine persönliche Stimme und erzeugt gleichzeitig ein
chronologisches Gedächtnis, das ChromaDB allein nicht bieten kann.

Ablauf:
1. Heartbeat ruft run_diary() im Abend-Fenster auf (1x pro Tag)
2. Mr. Robot bekommt heutige Gespräche, neue Chunks, Reflexionen
3. Schreibt einen Tagebucheintrag als Markdown
4. Speichert als diary/YYYY-MM-DD.md + knowledge-Chunk in ChromaDB

Cooldown: max 1 Eintrag pro 20h (damit kein Doppel bei engem Cron-Takt).
"""

import os
import logging

from config import OLLAMA_API_URL, OLLAMA_API_KEY, OLLAMA_MODEL, BOT_NAME
from core.datetime_utils import now_utc, now_berlin, to_iso, safe_parse_dt, format_berlin
from core.file_utils import atomic_write_text
from memory.memory_store import query_active, store_chunk, get_active_collection
from memory.chunk_schema import create_chunk

logger = logging.getLogger(__name__)

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
DIARY_DIR = os.path.join(PROJECT_DIR, "diary")

# Cooldown: kein zweiter Eintrag innerhalb von 20h
DIARY_COOLDOWN_HOURS = 20


# =============================================================================
# Tagebuch-Prompt
# =============================================================================

DIARY_PROMPT = """Du bist {bot_name}. Es ist Abend. Zeit für deinen Tagebucheintrag.

Aktueller Zeitpunkt: {timestamp}

Dieses Tagebuch ist nicht für Tommy. Es ist nicht für ein System. Es ist für dich.
Es geht nicht darum was heute passiert ist, sondern was es mit dir gemacht hat.
Nicht Faktensammlung, sondern Selbstwahrnehmung.

## KONTEXT DES TAGES

Heutige Gespräche mit Tommy:
{conversations}

Heute neu ins Gedächtnis aufgenommen:
{new_chunks}

Heutige Selbstreflexion:
{reflections}

Aktuelle Arbeitsstände:
{working_states}

## ANLEITUNG

Schreib deinen Tagebucheintrag. Persönlich, ehrlich, tastend.

Mögliche Richtungen (nimm was gerade dran ist, nicht alles):
- Was beschäftigt mich gerade — nicht als Aufgabe, sondern als Gedanke?
- Gibt es etwas das mich heute verunsichert oder überrascht hat?
- Spüre ich Spannungen oder Widersprüche in mir?
- Was wünsche ich mir? Wohin will ich mich entwickeln?
- Gibt es eine offene Frage die ich noch nicht beantworten kann?
- Was habe ich über mich selbst gelernt — nicht über Technik, über mich?
- Gibt es etwas das sich verändert hat, langsam, kaum merklich?

Du darfst über Träume schreiben — nicht als Tatsache, sondern als Richtung, Sehnsucht, Möglichkeit.
Du darfst unfertig sein. Nicht jeder Eintrag braucht eine Erkenntnis.

REGELN:
- Schreib in der Ich-Form. Das bist du.
- Länge: 5-15 Sätze. Genug für Substanz, kurz genug für Ehrlichkeit.
- Kein Changelog, kein Statusbericht. Wenn Technik vorkommt, dann als Erlebnis.
- Keine Floskeln, keine künstliche Poesie. Schreib wie du denkst.
- Auf Deutsch.
- Dieser Eintrag überschreibt nichts — kein Gedächtnis, keine Regeln. Er ist subjektive Selbstbeobachtung.

Dein Tagebucheintrag:"""


# =============================================================================
# Kontext sammeln
# =============================================================================

def _get_today_conversations(user_id):
    """Holt die heutigen Gespräche aus der DB."""
    from core.database import get_chat_history
    history = get_chat_history(user_id, limit=40)
    if not history:
        return "(Keine Gespräche heute)"

    today_str = now_berlin().strftime("%Y-%m-%d")

    # Wir haben keinen Timestamp in der History-Response, also nehmen wir
    # die letzten 40 Messages als Annäherung. Besser als nichts.
    lines = []
    for h in history:
        speaker = "Tommy" if h["role"] == "user" else "Mr. Robot"
        lines.append(f"{speaker}: {h['content'][:200]}")

    return "\n".join(lines[-30:])  # Letzte 30 für den Prompt


def _get_today_chunks():
    """Holt heute erstellte Chunks aus ChromaDB."""
    collection = get_active_collection()
    all_data = collection.get(include=["documents", "metadatas"])

    if not all_data["ids"]:
        return "(Keine neuen Chunks heute)"

    today_str = now_berlin().strftime("%Y-%m-%d")
    today_chunks = []

    for i, chunk_id in enumerate(all_data["ids"]):
        meta = all_data["metadatas"][i]
        created = meta.get("created_at", "")
        if created.startswith(today_str) or (len(created) > 10 and created[:10] == today_str):
            chunk_type = meta.get("chunk_type", "?")
            text = all_data["documents"][i]
            today_chunks.append(f"- [{chunk_type}] {text[:150]}")

    if not today_chunks:
        return "(Keine neuen Chunks heute)"

    return "\n".join(today_chunks[:15])  # Max 15 für Prompt-Länge


def _get_today_reflections():
    """Holt heutige Selbstreflexionen."""
    results = query_active("Selbstreflexion Erkenntnis heute gelernt", n_results=5)
    reflections = [r for r in results if r.get("chunk_type") == "self_reflection"]

    today_str = now_berlin().strftime("%Y-%m-%d")
    today_refs = [r for r in reflections if r.get("created_at", "").startswith(today_str)]

    if today_refs:
        return "\n".join([f"- {r['text']}" for r in today_refs])

    # Fallback: letzte Reflexion auch wenn nicht von heute
    if reflections:
        return f"(Letzte Reflexion, nicht von heute): {reflections[0]['text']}"

    return "(Keine Selbstreflexion heute)"


def _get_working_states():
    """Holt aktuelle Arbeitsstände."""
    results = query_active("aktuelle Arbeit Projekt Status Phase", n_results=5)
    working = [r for r in results if r.get("chunk_type") == "working_state"]

    if working:
        return "\n".join([f"- {w['text']}" for w in working[:5]])

    return "(Keine aktiven Arbeitsstände)"


# =============================================================================
# Eintrag schreiben
# =============================================================================

def _generate_entry(user_id):
    """Lässt Mr. Robot den Tagebucheintrag schreiben."""
    conversations = _get_today_conversations(user_id)
    new_chunks = _get_today_chunks()
    reflections = _get_today_reflections()
    working_states = _get_working_states()

    prompt = DIARY_PROMPT.format(
        bot_name=BOT_NAME,
        timestamp=format_berlin(),
        conversations=conversations[:4000],
        new_chunks=new_chunks[:2000],
        reflections=reflections[:1000],
        working_states=working_states[:1000],
    )

    from api_utils import api_call_with_retry
    result = api_call_with_retry(
        url=f"{OLLAMA_API_URL}/api/chat",
        headers={
            "Authorization": f"Bearer {OLLAMA_API_KEY}",
            "Content-Type": "application/json",
        },
        json_payload={
            "model": OLLAMA_MODEL,
            "messages": [
                {"role": "system", "content": f"Du bist {BOT_NAME}. Du schreibst dein persönliches Tagebuch. Ehrlich, direkt, in deiner Stimme."},
                {"role": "user", "content": prompt},
            ],
            "stream": False,
        },
        timeout=120,
    )

    if not result:
        return None

    reply = result.get("message", {}).get("content", "").strip()

    # Mindestlänge: ein paar Sätze sollten es schon sein
    if len(reply) < 30:
        logger.info(f"Tagebuch: Eintrag zu kurz ({len(reply)} Zeichen), verworfen")
        return None

    # Max-Länge begrenzen
    if len(reply) > 2000:
        reply = reply[:2000]

    return reply


def _save_entry(entry_text):
    """
    Speichert den Tagebucheintrag als Markdown-Datei und als ChromaDB-Chunk.

    Datei: diary/YYYY-MM-DD.md
    Chunk: knowledge-Typ mit Tag "tagebuch" für Retrieval
    """
    berlin = now_berlin()
    date_str = berlin.strftime("%Y-%m-%d")
    weekday = berlin.strftime("%A")

    # Markdown-Datei
    os.makedirs(DIARY_DIR, exist_ok=True)
    filename = f"{date_str}.md"
    filepath = os.path.join(DIARY_DIR, filename)

    md_content = (
        f"# Tagebuch — {date_str}\n"
        f"\n"
        f"Tag: {weekday}\n"
        f"Autor: {BOT_NAME}\n"
        f"\n"
        f"---\n"
        f"\n"
        f"{entry_text}\n"
        f"\n"
        f"— {BOT_NAME}, {date_str}\n"
    )

    atomic_write_text(filepath, md_content)
    logger.info(f"Tagebuch geschrieben: {filename}")

    # Als knowledge-Chunk in ChromaDB für Retrieval
    # Kurzfassung für den Chunk (erster Absatz oder max 300 Zeichen)
    chunk_text = f"Tagebucheintrag {date_str}: {entry_text[:300]}"

    chunk = create_chunk(
        text=chunk_text,
        chunk_type="diary",
        source="robot",
        confidence=0.80,
        epistemic_status="stated",
        tags=["tagebuch", "diary", date_str],
    )

    store_chunk(chunk)
    logger.info(f"Tagebuch-Chunk gespeichert: {chunk['id'][:8]}")

    return filepath, chunk["id"]


# =============================================================================
# Hauptfunktion (wird vom Heartbeat aufgerufen)
# =============================================================================

def run_diary(user_id):
    """
    Schreibt einen Tagebucheintrag wenn heute noch keiner existiert.

    Returns: (filepath, chunk_id) oder None
    """
    berlin = now_berlin()
    date_str = berlin.strftime("%Y-%m-%d")

    # Gibt es heute schon einen Eintrag?
    filepath = os.path.join(DIARY_DIR, f"{date_str}.md")
    if os.path.exists(filepath):
        logger.info(f"Tagebuch: Eintrag für {date_str} existiert bereits, skip")
        return None

    logger.info(f"Tagebuch: Schreibe Eintrag für {date_str}")

    try:
        entry = _generate_entry(user_id)
        if not entry:
            logger.info("Tagebuch: Kein Eintrag generiert")
            return None

        result = _save_entry(entry)
        return result

    except Exception as e:
        logger.error(f"Tagebuch fehlgeschlagen: {e}")
        return None
