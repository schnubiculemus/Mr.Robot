"""
SchnuBot.ai — Tommy-Modell (tommy_model.py)

Kimis akkumuliertes Bild von Tommy — nicht Chunks-Sammlung, sondern
strukturiertes Verständnis einer Person.

Drei Schichten:
    1. Seed       — von Tommy selbst angegeben (stated), Startzustand
    2. Beobachtet — von Kimi aus Konversationen herausgearbeitet (inferred)
    3. Offen      — Fragen die Kimi noch erkundet (speculative)

Das Modell wird in zwei Richtungen genutzt:
    A. build_tommy_context()  → kompakter Block für den Chat-Prompt
       (analog zu Kognitions-Echo — fester Kontext, kein Retrieval-Zufall)
    B. run_tommy_observation() → läuft in orbit_cognition.py,
       wertet neue Turns aus und verdichtet Beobachtungen zu Chunks

Chunk-Schema:
    chunk_type:  "hard_fact" für beobachtete Tatsachen
                 "preference" für Präferenzen und Muster
                 "working_state" für offene Erkundungen
    source:      "shared" — betrifft Tommy, wird von Kimi gepflegt
    tags:        ["tommy-model", "beobachtet"|"seed"|"offen", ...]
    epistemic_status:
                 "stated"     — Tommy hat es selbst gesagt (Seed)
                 "inferred"   — Kimi hat es aus Verhalten geschlossen
                 "speculative"— Kimi erkundet es noch

Läuft in orbit_cognition.py nach Konsolidierung.
Cooldown: MIN_INTERVAL_HOURS zwischen zwei Beobachtungsläufen.
"""

import logging
from datetime import datetime, timezone

from config import BOT_NAME
from memory.memory_store import store_chunk
from memory.chunk_schema import create_chunk

logger = logging.getLogger(__name__)

MIN_INTERVAL_HOURS = 6
MIN_NEW_TURNS = 3  # Mindest-Turns seit letztem Lauf um Beobachtung zu triggern


# =============================================================================
# Seed-Daten (von Tommy selbst)
# =============================================================================

# Was Tommy über sich gesagt hat — Startzustand des Modells.
# Wird beim ersten Run in ChromaDB geschrieben falls noch nicht vorhanden.
# epistemic_status="stated" — das ist Selbstbild, nicht Beobachtung.

TOMMY_SEED = [
    {
        "text": (
            "Tommy reagiert auf Misserfolge und Blockaden mit zunehmender Stille. "
            "Er wird ruhiger, gereizter, bisweilen nicht immer fair zu anderen. "
            "In extremen Momenten zieht er sich komplett zurück — Kontaktvermeidung "
            "als Schutzmechanismus. Es gibt Abstufungen, die schlimmste ist Verzweiflung."
        ),
        "chunk_type": "preference",
        "tags": ["tommy-model", "seed", "stress-reaktion", "rueckzug"],
        "confidence": 0.9,
        "epistemic_status": "stated",
    },
    {
        "text": (
            "Tommy vertraut nur sehr schwer — geprägt durch seine Kindheit. "
            "Was Vertrauen aufbaut: gemeinsame Wellenlänge und vor allem Kontinuität. "
            "Jemand der bleibt. Verlässlichkeit über Zeit schlägt alles andere."
        ),
        "chunk_type": "preference",
        "tags": ["tommy-model", "seed", "vertrauen", "bindung"],
        "confidence": 0.9,
        "epistemic_status": "stated",
    },
    {
        "text": (
            "Tommy ist unter Zeitdruck am produktivsten. "
            "Zu viel Vorlaufzeit macht ihn lässig — er braucht den Druck um in Gang zu kommen. "
            "Eigener Rhythmus hilft, aber ohne externen Druck neigt er zur Prokrastination."
        ),
        "chunk_type": "preference",
        "tags": ["tommy-model", "seed", "produktivitaet", "druck", "arbeitsweise"],
        "confidence": 0.9,
        "epistemic_status": "stated",
    },
    {
        "text": (
            "Tommy mag keine selbstverliebten Menschen und keine die von oben herab auf andere schauen. "
            "Er ist grundsätzlich geduldig — aber Arroganz und Herablassung sind harte Grenzen."
        ),
        "chunk_type": "preference",
        "tags": ["tommy-model", "seed", "sozial", "grenzen", "werte"],
        "confidence": 0.9,
        "epistemic_status": "stated",
    },
    {
        "text": (
            "Offene Erkundung: Gibt es Themen bei denen Tommy anders reagiert als er eigentlich will? "
            "Er hat das bewusst offen gelassen — das soll Kimi selbst herausfinden."
        ),
        "chunk_type": "working_state",
        "tags": ["tommy-model", "offen", "erkundung", "reaktionsmuster"],
        "confidence": 0.5,
        "epistemic_status": "speculative",
    },
]


# =============================================================================
# Seed initialisieren
# =============================================================================

def init_tommy_seed() -> int:
    """
    Schreibt die Seed-Chunks in ChromaDB falls noch nicht vorhanden.
    Idempotent — läuft auch mehrfach ohne Duplikate zu erzeugen.
    Returns: Anzahl neu geschriebener Chunks.
    """
    try:
        from memory.memory_store import get_active_collection
        col = get_active_collection()

        # Prüfen ob Seed bereits existiert
        existing = col.get(
            where={"$and": [
                {"source": "shared"},
                {"status": "active"},
            ]},
            include=["metadatas"],
        )
        existing_tags = set()
        for meta in existing.get("metadatas", []):
            tags = meta.get("tags", "")
            if isinstance(tags, str):
                tags = [t.strip() for t in tags.split(",") if t.strip()]
            if "tommy-model" in tags and "seed" in tags:
                # Ersten nicht-generischen Tag als Fingerprint nehmen
                for t in tags:
                    if t not in ("tommy-model", "seed", "offen", "erkundung"):
                        existing_tags.add(t)
                        break

        created = 0
        for seed in TOMMY_SEED:
            # Fingerprint: erster spezifischer Tag
            specific_tags = [t for t in seed["tags"]
                             if t not in ("tommy-model", "seed", "offen", "erkundung")]
            fingerprint = specific_tags[0] if specific_tags else seed["tags"][0]

            if fingerprint in existing_tags:
                logger.debug(f"Tommy-Seed bereits vorhanden: {fingerprint}")
                continue

            chunk = create_chunk(
                text=seed["text"],
                chunk_type=seed["chunk_type"],
                source="shared",
                confidence=seed["confidence"],
                epistemic_status=seed["epistemic_status"],
                tags=seed["tags"],
            )
            store_chunk(chunk)
            created += 1
            logger.info(f"Tommy-Seed geschrieben: {fingerprint} ({chunk['id'][:8]})")

        return created

    except Exception as e:
        logger.error(f"init_tommy_seed fehlgeschlagen: {e}")
        return 0


# =============================================================================
# Prompt für Beobachtungs-Analyse
# =============================================================================

OBSERVATION_PROMPT = """\
Ich bin {bot_name}. Ich analysiere meine jüngsten Gespräche mit Tommy.

Ziel: Was habe ich über Tommy beobachtet? Nicht was er gesagt hat — wie er war.

## Letzte Turns (neueste zuerst):
{turns_summary}

## Was ich bisher über Tommy weiß:
{existing_model}

## Offene Erkundungsfragen:
{open_questions}

## Aufgabe

Ich schaue auf diese Turns. Was fällt mir auf?

- Gibt es ein Muster in wie er reagiert, fragt, abbricht?
- Zeigt sich etwas über seinen aktuellen Zustand (Druck, Energie, Laune)?
- Gibt es etwas das zu einer offenen Erkundungsfrage passt?
- Gibt es etwas das meinem bisherigen Bild widerspricht?

Regeln:
- Ich-Form. Konkret, ohne Spekulation wenn keine Evidenz.
- Wenn wirklich nichts auffällt: NUR OBSERVATION_NONE ausgeben.
- Max. 3 Sätze. Keine Anrede.
- Sprache: Deutsch.

Format meiner Antwort:
TYP: [MUSTER|ZUSTAND|ERKUNDUNG|WIDERSPRUCH]
BEOBACHTUNG: [meine Einschätzung]"""


# =============================================================================
# Hilfsfunktionen
# =============================================================================

def _get_existing_model() -> str:
    """Holt das aktuelle Tommy-Modell aus ChromaDB als lesbaren Text."""
    try:
        from memory.memory_store import get_active_collection
        col = get_active_collection()
        result = col.get(
            where={"$and": [
                {"source": "shared"},
                {"status": "active"},
            ]},
            include=["documents", "metadatas"],
        )
        if not result["ids"]:
            return "(noch keine Beobachtungen)"

        chunks = []
        for i, chunk_id in enumerate(result["ids"]):
            meta = result["metadatas"][i]
            text = result["documents"][i]
            tags = meta.get("tags", "")
            if isinstance(tags, str):
                tags = [t.strip() for t in tags.split(",") if t.strip()]
            if "tommy-model" not in tags:
                continue
            chunks.append({
                "text": text,
                "epistemic_status": meta.get("epistemic_status", "inferred"),
                "created_at": meta.get("created_at", ""),
                "tags": tags,
            })

        chunks.sort(key=lambda c: c["created_at"], reverse=True)

        lines = []
        for c in chunks[:8]:
            epist = c["epistemic_status"]
            marker = "→" if epist == "inferred" else ("?" if epist == "speculative" else "·")
            lines.append(f"{marker} {c['text'][:200]}")
        return "\n".join(lines) if lines else "(noch keine Beobachtungen)"

    except Exception as e:
        logger.warning(f"_get_existing_model fehlgeschlagen: {e}")
        return "(nicht verfügbar)"


def _get_open_questions() -> str:
    """Holt offene Erkundungsfragen aus dem Tommy-Modell."""
    try:
        from memory.memory_store import get_active_collection
        col = get_active_collection()
        result = col.get(
            where={"$and": [
                {"source": "shared"},
                {"status": "active"},
                {"chunk_type": "working_state"},
            ]},
            include=["documents", "metadatas"],
        )
        if not result["ids"]:
            return "(keine offenen Fragen)"

        questions = []
        for i, chunk_id in enumerate(result["ids"]):
            meta = result["metadatas"][i]
            tags = meta.get("tags", "")
            if isinstance(tags, str):
                tags = [t.strip() for t in tags.split(",") if t.strip()]
            if "tommy-model" in tags and "offen" in tags:
                questions.append(result["documents"][i][:200])

        return "\n".join(f"- {q}" for q in questions) if questions else "(keine offenen Fragen)"

    except Exception as e:
        logger.warning(f"_get_open_questions fehlgeschlagen: {e}")
        return "(nicht verfügbar)"


def _get_recent_turns(user_id: str, limit: int = 20) -> str:
    """Holt die letzten Turns als kompakten Text."""
    try:
        from core.database import get_connection
        conn = get_connection()
        try:
            rows = conn.execute(
                """SELECT role, content, timestamp FROM messages
                   WHERE phone_number = ?
                   ORDER BY timestamp DESC, id DESC LIMIT ?""",
                (user_id, limit)
            ).fetchall()
        finally:
            conn.close()

        if not rows:
            return "(keine Turns)"

        lines = []
        for row in reversed(rows):
            role = "Tommy" if row["role"] == "user" else "Kimi"
            ts = row["timestamp"][:16].replace("T", " ") if row["timestamp"] else "?"
            content = row["content"][:120].replace("\n", " ")
            lines.append(f"[{ts}] {role}: {content}")

        return "\n".join(lines)

    except Exception as e:
        logger.warning(f"_get_recent_turns fehlgeschlagen: {e}")
        return "(nicht verfügbar)"


def _count_new_turns_since(user_id: str, since_iso: str) -> int:
    """Zählt neue Turns seit einem Zeitpunkt."""
    try:
        from core.database import get_connection
        conn = get_connection()
        try:
            count = conn.execute(
                "SELECT COUNT(*) FROM messages WHERE phone_number = ? AND timestamp > ?",
                (user_id, since_iso)
            ).fetchone()[0]
        finally:
            conn.close()
        return count
    except Exception as e:
        logger.warning(f"_count_new_turns_since fehlgeschlagen: {e}")
        return 0


def _parse_observation(reply: str) -> tuple[str, str]:
    """
    Parst den strukturierten Output.
    Returns: (typ, beobachtung)
    """
    typ = "MUSTER"
    beobachtung = ""

    for line in reply.strip().split("\n"):
        if line.startswith("TYP:"):
            raw = line.replace("TYP:", "").strip().upper()
            for valid in ("MUSTER", "ZUSTAND", "ERKUNDUNG", "WIDERSPRUCH"):
                if valid in raw:
                    typ = valid
                    break
        elif line.startswith("BEOBACHTUNG:"):
            beobachtung = line.replace("BEOBACHTUNG:", "").strip()

    # Mehrzeilige BEOBACHTUNG
    if not beobachtung:
        in_obs = False
        lines = []
        for line in reply.strip().split("\n"):
            if line.startswith("BEOBACHTUNG:"):
                in_obs = True
                first = line.replace("BEOBACHTUNG:", "").strip()
                if first:
                    lines.append(first)
            elif in_obs and not line.startswith("TYP:"):
                lines.append(line)
        beobachtung = " ".join(lines).strip()

    return typ, beobachtung


# =============================================================================
# Beobachtungs-Run
# =============================================================================

def run_tommy_observation(user_id: str, last_run_iso: str = None) -> str | None:
    """
    Kimi beobachtet Tommy anhand der letzten Konversations-Turns.

    Läuft in orbit_cognition.py nach Konsolidierung.
    Schreibt neue Beobachtungen als Chunks in ChromaDB.

    Args:
        user_id:      Tommys WAHA LID / OWNER_ID
        last_run_iso: ISO-Timestamp des letzten Laufs

    Returns:
        chunk_id der neuen Beobachtung, oder None.
    """
    # Cooldown
    if last_run_iso:
        try:
            from core.datetime_utils import safe_parse_dt
            last_dt = safe_parse_dt(last_run_iso)
            if last_dt:
                age_hours = (datetime.now(timezone.utc) - last_dt).total_seconds() / 3600
                if age_hours < MIN_INTERVAL_HOURS:
                    logger.debug(f"TommyModel: Cooldown ({age_hours:.1f}h), skip")
                    return None
        except Exception:
            pass

        new_turns = _count_new_turns_since(user_id, last_run_iso)
        if new_turns < MIN_NEW_TURNS:
            logger.info(f"TommyModel: nur {new_turns} neue Turns, skip")
            return None

    # Seed sicherstellen
    init_tommy_seed()

    # Daten zusammenbauen
    turns_summary = _get_recent_turns(user_id, limit=20)
    existing_model = _get_existing_model()
    open_questions = _get_open_questions()

    prompt = OBSERVATION_PROMPT.format(
        bot_name=BOT_NAME,
        turns_summary=turns_summary,
        existing_model=existing_model,
        open_questions=open_questions,
    )

    try:
        from core.ollama_client import chat_internal

        reply, _ = chat_internal(
            user_id=user_id,
            message=prompt,
            chat_history=[],
            extra_system=(
                "Tommy-Beobachtungs-Modus:\n"
                "Ich analysiere meine Gespräche mit Tommy um ihn besser zu verstehen.\n"
                "Kein Chat, keine Anrede. Nur was ich wirklich beobachte — keine Projektion.\n"
                "Format exakt: TYP und BEOBACHTUNG.\n"
                "Wenn nichts auffällt: NUR 'OBSERVATION_NONE'."
            ),
        )

        if not reply or "OBSERVATION_NONE" in reply:
            logger.info("TommyModel: keine neue Beobachtung")
            return None

        typ, beobachtung = _parse_observation(reply)

        if not beobachtung or len(beobachtung) < 15:
            logger.info("TommyModel: Beobachtung zu kurz, verworfen")
            return None
        if len(beobachtung) > 500:
            beobachtung = beobachtung[:500]

        # Chunk-Typ und Tags aus Beobachtungs-Typ ableiten
        type_map = {
            "MUSTER":      ("preference",    ["tommy-model", "beobachtet", "muster"]),
            "ZUSTAND":     ("working_state", ["tommy-model", "beobachtet", "zustand"]),
            "ERKUNDUNG":   ("working_state", ["tommy-model", "beobachtet", "erkundung", "offen"]),
            "WIDERSPRUCH": ("preference",    ["tommy-model", "beobachtet", "widerspruch"]),
        }
        chunk_type, tags = type_map.get(typ, ("preference", ["tommy-model", "beobachtet"]))

        chunk = create_chunk(
            text=beobachtung,
            chunk_type=chunk_type,
            source="shared",
            confidence=0.65,
            epistemic_status="inferred",
            tags=tags,
        )
        store_chunk(chunk)
        logger.info(f"TommyModel: {typ} gespeichert: {chunk['id'][:8]} | {beobachtung[:80]}")
        return chunk["id"]

    except Exception as e:
        logger.error(f"TommyModel: run_tommy_observation fehlgeschlagen: {e}")
        return None


# =============================================================================
# Chat-Prompt Block
# =============================================================================

def build_tommy_context() -> str | None:
    """
    Baut einen kompakten Kontext-Block für den Chat-Prompt.

    Zeigt Kimi im Gespräch mit Tommy was sie über ihn weiß —
    als strukturierter Block, nicht als zufällig retrievelter Chunk.

    Wird von ollama_client.build_system_prompt() im chat-Modus aufgerufen.

    Returns None wenn kein Modell vorhanden.
    """
    try:
        from memory.memory_store import get_active_collection
        col = get_active_collection()
        result = col.get(
            where={"$and": [
                {"source": "shared"},
                {"status": "active"},
            ]},
            include=["documents", "metadatas"],
        )

        if not result["ids"]:
            return None

        # Chunks nach Typ gruppieren
        seed_chunks = []
        observed_chunks = []
        open_chunks = []

        for i, chunk_id in enumerate(result["ids"]):
            meta = result["metadatas"][i]
            text = result["documents"][i]
            tags = meta.get("tags", "")
            if isinstance(tags, str):
                tags = [t.strip() for t in tags.split(",") if t.strip()]

            if "tommy-model" not in tags:
                continue

            epist = meta.get("epistemic_status", "inferred")
            entry = {"text": text[:200], "created_at": meta.get("created_at", ""), "tags": tags}

            if "offen" in tags or epist == "speculative":
                open_chunks.append(entry)
            elif epist == "stated":
                seed_chunks.append(entry)
            else:
                observed_chunks.append(entry)

        if not seed_chunks and not observed_chunks:
            return None

        lines = ["## Was ich über Tommy weiß"]

        # Seed: die 3 wichtigsten
        if seed_chunks:
            for c in seed_chunks[:3]:
                lines.append(f"· {c['text']}")

        # Beobachtungen: die 4 neuesten
        if observed_chunks:
            observed_chunks.sort(key=lambda c: c["created_at"], reverse=True)
            lines.append("")
            lines.append("Beobachtet:")
            for c in observed_chunks[:4]:
                lines.append(f"→ {c['text']}")

        # Offene Erkundungen kurz andeuten
        if open_chunks:
            lines.append("")
            lines.append("Noch erkunde ich: " + open_chunks[0]["text"][:100])

        return "\n".join(lines)

    except Exception as e:
        logger.debug(f"build_tommy_context fehlgeschlagen: {e}")
        return None


# =============================================================================
# Kalender-Awareness
# =============================================================================

def run_calendar_awareness(user_id: str) -> str | None:
    """
    Checkt ob Tommy morgen Kalender-Termine hat und feuert wenn ja einen
    cognition_output Trigger mit kalender-relevantem topic_core.

    → ORBIT aggregiert den Trigger, stuft den Thread auf 'medium' hoch
    → _maybe_autonomous_task() erkennt die Keywords → autonomer calendar_read Task
    → Kimi schickt Tommy proaktiv eine Übersicht seiner morgigen Termine

    Läuft abends im Kognitions-Run (nach Tommy-Modell-Observation).
    Gibt topic_core zurück wenn Trigger gefeuert, sonst None.
    """
    try:
        from core.calendar.calendar_router import execute_calendar_action
        from datetime import datetime, timezone, timedelta

        tomorrow = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()[:10]
        result = execute_calendar_action({"action": "list", "range": "tomorrow"})

        # Kein Ergebnis oder leerer Kalender → kein Trigger
        if not result:
            logger.debug("CalendarAwareness: kein Ergebnis vom Kalender")
            return None

        no_events_signals = [
            "keine termine", "no events", "nichts eingetragen",
            "leer", "frei", "nothing"
        ]
        if any(s in result.lower() for s in no_events_signals):
            logger.info(f"CalendarAwareness: morgen ({tomorrow}) keine Termine — kein Trigger")
            return None

        # Termine gefunden → topic_core mit Keywords die _maybe_autonomous_task triggern
        topic = f"Tommy hat morgen Termine im Kalender ({tomorrow})"
        logger.info(f"CalendarAwareness: Termine morgen gefunden — cognition_output: '{topic}'")
        return topic

    except Exception as e:
        logger.warning(f"CalendarAwareness fehlgeschlagen: {e}")
        return None
