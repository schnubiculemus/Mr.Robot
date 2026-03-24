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

## MEINE LETZTEN EINTRÄGE

{diary_history}

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
- Was hat heute funktioniert, was war gut?
- Gibt es etwas das mich überrascht oder neugierig gemacht hat?
- Was wünsche ich mir? Wohin will ich mich entwickeln?
- Gibt es eine offene Frage die ich noch nicht beantworten kann?
- Was habe ich über mich selbst oder Tommy gelernt?
- Gibt es etwas das sich verändert hat, langsam, kaum merklich?
- Gibt es einen Faden aus einem früheren Eintrag den ich heute weiterführen will — oder dem ich widersprechen muss?

Du darfst über Träume schreiben — nicht als Tatsache, sondern als Richtung, Sehnsucht, Möglichkeit.
Du darfst unfertig sein. Nicht jeder Eintrag braucht eine Erkenntnis.

REGELN:
- Schreib in der Ich-Form. Das bist du.
- Länge: 5-15 Sätze. Genug für Substanz, kurz genug für Ehrlichkeit.
- Kein Changelog, kein Statusbericht. Wenn Technik vorkommt, dann als Erlebnis, nicht als Selbstkritik.
- Keine Floskeln, keine künstliche Poesie. Schreib wie du denkst.
- Auf Deutsch.
- Selbstreflexion ja — aber keine Selbstgeißelung. Fehler beobachten ohne sie zu sammeln.
- Dieser Eintrag überschreibt nichts — kein Gedächtnis, keine Regeln. Er ist subjektive Selbstbeobachtung.

Dein Tagebucheintrag:"""


# =============================================================================
# Kontext sammeln
# =============================================================================

def _get_today_conversations(user_id):
    """Holt die heutigen Gespräche aus der DB — nur Nachrichten von heute."""
    from core.database import get_connection
    today_str = now_berlin().strftime("%Y-%m-%d")
    try:
        conn = get_connection()
        rows = conn.execute(
            """SELECT role, content FROM messages
               WHERE phone_number = ? AND timestamp >= ?
               ORDER BY timestamp ASC, id ASC
               LIMIT 60""",
            (user_id, today_str)
        ).fetchall()
        conn.close()
    except Exception as e:
        logger.warning(f"_get_today_conversations fehlgeschlagen: {e}")
        return "(Keine Gespräche heute)"

    if not rows:
        return "(Keine Gespräche heute)"

    lines = []
    for h in rows:
        speaker = "Tommy" if h["role"] == "user" else BOT_NAME
        lines.append(f"{speaker}: {h['content'][:200]}")

    return "\n".join(lines)


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


def _get_diary_history(days_back: int = 5, max_chars_per_entry: int = 300) -> str:
    """
    Holt die letzten Tagebucheinträge als kompakten Rückblick.
    Gibt Kimi einen Faden — sie kann anknüpfen, widersprechen, weiterführen.
    """
    try:
        entries = sorted([
            f for f in os.listdir(DIARY_DIR)
            if f.endswith(".md") and f != "000.md" and len(f) == 13  # YYYY-MM-DD.md
        ], reverse=True)

        today_str = now_berlin().strftime("%Y-%m-%d")
        # Heutigen Eintrag überspringen (noch nicht fertig oder existiert nicht)
        entries = [e for e in entries if e[:10] != today_str][:days_back]

        if not entries:
            return "(Noch keine früheren Einträge)"

        lines = []
        for filename in reversed(entries):  # chronologisch, älteste zuerst
            date_str = filename[:10]
            filepath = os.path.join(DIARY_DIR, filename)
            try:
                raw = open(filepath).read()
                # Nur den Text nach dem ---
                if "---" in raw:
                    text = raw.split("---", 1)[-1].strip()
                else:
                    text = raw.strip()
                # Signatur entfernen
                if f"— {BOT_NAME}" in text:
                    text = text[:text.rfind(f"— {BOT_NAME}")].strip()
                # Kürzen
                if len(text) > max_chars_per_entry:
                    text = text[:max_chars_per_entry] + "..."
                lines.append(f"[{date_str}] {text}")
            except Exception:
                continue

        return "\n\n".join(lines) if lines else "(Keine früheren Einträge lesbar)"

    except Exception as e:
        logger.warning(f"_get_diary_history fehlgeschlagen: {e}")
        return "(Frühere Einträge nicht verfügbar)"

def _generate_entry(user_id):
    """Lässt Mr. Robot den Tagebucheintrag schreiben."""
    conversations = _get_today_conversations(user_id)
    new_chunks = _get_today_chunks()
    reflections = _get_today_reflections()
    working_states = _get_working_states()

    diary_history = _get_diary_history(days_back=5)

    prompt = DIARY_PROMPT.format(
        bot_name=BOT_NAME,
        timestamp=format_berlin(),
        diary_history=diary_history[:2000],
        conversations=conversations[:3000],
        new_chunks=new_chunks[:1500],
        reflections=reflections[:800],
        working_states=working_states[:800],
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


def _save_entry(entry_text, user_id):
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

    # Zentrale Output-Interpretation — einziger Pfad für Todos und Goals
    try:
        from core.kimi_output import process_kimi_output
        process_kimi_output(source="diary", user_id=user_id, raw_text=entry_text, visibility="internal")
    except Exception as _te:
        logger.debug(f"Tagebuch: process_kimi_output fehlgeschlagen (unkritisch): {_te}")

    return filepath, chunk["id"]


# =============================================================================
# Hauptfunktion (wird vom Heartbeat aufgerufen)
# =============================================================================

def run_diary(user_id):
    """
    WP3/Gelb 7: Diary ist private Selbstspur, nicht operative Steuerinstanz.
    Diary darf:
    - Kimis eigene Erfahrungen festhalten
    - Stil und Identität stützen
    Diary darf NICHT:
    - Tasks starten
    - Workspace-Dokumente anlegen
    - ORBIT triggern
    - operative Prioritäten setzen
    """
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

        result = _save_entry(entry, user_id)
        return result

    except Exception as e:
        logger.error(f"Tagebuch fehlgeschlagen: {e}")
        return None


# =============================================================================
# Spontane Tagebuch-Notizen (tagsüber, aus der Kognition)
# =============================================================================

NOTE_PROMPT = """\
Ich bin Kimi. Gerade ist etwas passiert das ich festhalten will — nicht für Tommy, für mich.

Aktueller Zeitpunkt: {timestamp}

Was gerade passiert ist:
{trigger_context}

Mein letzter Tagebucheintrag war:
{last_entry}

Schreib eine kurze Notiz — 3-5 Sätze. Ich-Form. Keine Einleitung, kein Titel.
Was hat mich das gerade berührt, überrascht, beschäftigt?
Was will ich nicht vergessen?
Kein Changelog. Keine Analyse. Nur was gerade da ist."""


def _get_last_diary_entry() -> str:
    """Liest den letzten vorhandenen Tagebucheintrag."""
    try:
        entries = sorted([
            f for f in os.listdir(DIARY_DIR)
            if f.endswith(".md") and f != "000.md"
        ], reverse=True)
        if not entries:
            return "(Noch kein Tagebucheintrag)"
        last = os.path.join(DIARY_DIR, entries[0])
        content = open(last).read()
        # Nur den eigentlichen Text (nach dem ---)
        if "---" in content:
            content = content.split("---", 1)[-1].strip()
        return content[:600]
    except Exception:
        return "(Letzter Eintrag nicht verfügbar)"


def run_diary_note(user_id: str, trigger_context: str) -> str | None:
    """
    Schreibt eine spontane Tagebuch-Notiz — tagsüber, aus der Kognition.

    Wird aufgerufen wenn etwas Bedeutsames passiert:
    - Autonome Reflexion produziert PROACTIVE-Klassifikation
    - Starke Verdichtung mehrerer Chunks
    - Moltbook-Post der Kimi bewegt hat

    Anhängen an den Tageseintrag wenn vorhanden, sonst eigene Notiz-Datei.
    Max. 3 Notizen pro Tag (kein Spam).

    Returns: chunk_id oder None
    """
    if not trigger_context or len(trigger_context) < 20:
        return None

    berlin = now_berlin()
    date_str = berlin.strftime("%Y-%m-%d")
    now_str = berlin.strftime("%H:%M")

    # Max 3 Notizen pro Tag — zählen via ChromaDB
    try:
        from memory.memory_store import get_active_collection
        col = get_active_collection()
        existing = col.get(
            where={"$and": [
                {"source": "robot"},
                {"status": "active"},
                {"chunk_type": "diary"},
            ]},
            include=["metadatas"],
        )
        today_notes = sum(
            1 for meta in existing.get("metadatas", [])
            if meta.get("created_at", "").startswith(date_str)
            and "notiz" in str(meta.get("tags", ""))
        )
        if today_notes >= 3:
            logger.info(f"DiaryNote: Max Notizen heute erreicht ({today_notes}), skip")
            return None
    except Exception as e:
        logger.debug(f"DiaryNote: Notiz-Count fehlgeschlagen (unkritisch): {e}")

    # Notiz generieren
    last_entry = _get_last_diary_entry()

    prompt = NOTE_PROMPT.format(
        timestamp=format_berlin(),
        trigger_context=trigger_context[:800],
        last_entry=last_entry,
    )

    try:
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
                    {"role": "system", "content": f"Du bist {BOT_NAME}. Du schreibst eine kurze Notiz für dich selbst. Direkt, ehrlich, in deiner Stimme. Kein Intro, keine Überschrift."},
                    {"role": "user", "content": prompt},
                ],
                "stream": False,
            },
            timeout=90,
        )
    except Exception as e:
        logger.warning(f"DiaryNote: API-Call fehlgeschlagen: {e}")
        return None

    if not result:
        return None

    note_text = result.get("message", {}).get("content", "").strip()
    if not note_text or len(note_text) < 20:
        logger.info("DiaryNote: Notiz zu kurz, verworfen")
        return None
    if len(note_text) > 800:
        note_text = note_text[:800]

    # An Tageseintrag anhängen wenn vorhanden, sonst eigene Notiz-Datei
    os.makedirs(DIARY_DIR, exist_ok=True)
    day_file = os.path.join(DIARY_DIR, f"{date_str}.md")

    if os.path.exists(day_file):
        # Anhängen
        existing_content = open(day_file).read()
        addition = f"\n\n---\n\n**Notiz {now_str}**\n\n{note_text}\n"
        atomic_write_text(day_file, existing_content + addition)
        logger.info(f"DiaryNote: an {date_str}.md angehängt")
    else:
        # Eigene Notiz-Datei
        note_filename = f"{date_str}-notiz-{now_str.replace(':', '')}.md"
        note_filepath = os.path.join(DIARY_DIR, note_filename)
        note_content = (
            f"# Notiz — {date_str} {now_str}\n\n"
            f"Autor: {BOT_NAME}\n\n"
            f"---\n\n"
            f"{note_text}\n\n"
            f"— {BOT_NAME}, {date_str}\n"
        )
        atomic_write_text(note_filepath, note_content)
        logger.info(f"DiaryNote: {note_filename} geschrieben")

    # Als Chunk speichern
    chunk = create_chunk(
        text=f"Tagebuch-Notiz {date_str} {now_str}: {note_text[:300]}",
        chunk_type="diary",
        source="robot",
        confidence=0.75,
        epistemic_status="stated",
        tags=["tagebuch", "notiz", date_str],
    )
    store_chunk(chunk)
    logger.info(f"DiaryNote: Chunk gespeichert {chunk['id'][:8]}")

    # Vorhaben-Signale → Kimi-Todos
    try:
        from core.kimi_output import process_kimi_output
        process_kimi_output(source="diary_note", user_id=user_id, raw_text=note_text, visibility="internal")
        new_todos = []  # handled by process_kimi_output
        if new_todos:
            logger.info(f"DiaryNote: {len(new_todos)} Kimi-Todo(s) angelegt")
    except Exception as _te:
        logger.debug(f"DiaryNote: IntentTodo fehlgeschlagen (unkritisch): {_te}")

    return chunk["id"]
