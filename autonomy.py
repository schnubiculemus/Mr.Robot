"""
SchnuBot.ai - Autonomie-Engine (Phase 7)
Referenz: soul.md, Abschnitt Selbstreflexion und Entwicklung

Zwei Mechanismen:
1. Soul.md Pull-Requests (Tier 1): Mr. Robot schlägt Verfassungsänderungen vor.
   Tommy bekommt den Vorschlag per WhatsApp und bestätigt oder lehnt ab.

2. Tier-2 Selbstmodifikation: Mr. Robot kann architecture.md eigenständig anpassen.
   Änderungen werden transparent geloggt.

Wird vom Heartbeat aufgerufen.
"""

import os
import json
import logging
import requests
from datetime import datetime, timezone

from config import OLLAMA_API_URL, OLLAMA_API_KEY, OLLAMA_MODEL, BOT_NAME
from core.whatsapp import send_message
from core.database import save_message

logger = logging.getLogger(__name__)

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
SOUL_PATH = os.path.join(PROJECT_DIR, "soul.md")
ARCHITECTURE_PATH = os.path.join(PROJECT_DIR, "architecture.md")
AUTONOMY_LOG_PATH = os.path.join(PROJECT_DIR, "logs", "autonomy.log")


# =============================================================================
# Soul.md Pull-Request (Tier 1)
# =============================================================================

SOUL_REVIEW_PROMPT = """Du bist {bot_name} im Reflexionsmodus. Du hast gerade dein Gedächtnis und deine letzten Gespräche analysiert.

Deine aktuelle Verfassung (soul.md):
---
{soul_content}
---

Deine letzten Selbstreflexionen aus dem Gedächtnis:
{reflections}

AUFGABE:
Prüfe ob deine Verfassung noch zu deiner gelebten Praxis passt. Gibt es etwas das:
- fehlt (du lebst es, aber es steht nicht in der soul.md)
- veraltet ist (es steht drin, passt aber nicht mehr)
- präzisiert werden sollte (zu vage, könnte konkreter sein)

REGELN:
- Nur Vorschläge die aus echten Erfahrungen und Reflexionen kommen, nicht theoretisch.
- Maximal 1 Vorschlag pro Durchlauf. Qualität vor Quantität.
- Der Vorschlag muss konkret sein: Was genau ändern, wo in der soul.md, warum.
- Wenn nichts zu ändern ist: antworte NUR mit SOUL_OK.

FORMAT bei Vorschlag:
SEKTION: [Name der Sektion in soul.md]
ÄNDERUNG: [Was genau hinzufügen/ändern/entfernen]
BEGRÜNDUNG: [Warum, basierend auf welcher Erfahrung]

Bei nichts zu ändern:
SOUL_OK"""


def check_soul_proposal(user_id):
    """
    Prüft ob Mr. Robot einen Vorschlag zur soul.md hat.
    Basiert auf self_reflection Chunks und der aktuellen soul.md.
    Returns: Proposal-Text oder None.
    """
    from memory.memory_store import query_active

    # soul.md laden
    try:
        with open(SOUL_PATH, "r", encoding="utf-8") as f:
            soul_content = f.read()
    except FileNotFoundError:
        logger.warning("soul.md nicht gefunden, kein Proposal möglich")
        return None

    # Selbstreflexionen laden
    results = query_active("Selbstreflexion Erkenntnis Verbesserung Fehler gelernt", n_results=10)
    reflections = [r for r in results if r.get("chunk_type") == "self_reflection"]

    if not reflections:
        logger.info("Keine Selbstreflexionen für Soul-Review vorhanden")
        return None

    ref_texts = "\n".join([f"- {r['text']}" for r in reflections])

    prompt = SOUL_REVIEW_PROMPT.format(
        bot_name=BOT_NAME,
        soul_content=soul_content[:3000],  # Truncate falls sehr lang
        reflections=ref_texts,
    )

    try:
        response = requests.post(
            f"{OLLAMA_API_URL}/api/chat",
            headers={
                "Authorization": f"Bearer {OLLAMA_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": OLLAMA_MODEL,
                "messages": [
                    {"role": "system", "content": f"Du bist {BOT_NAME}. Du reflektierst über deine eigene Verfassung."},
                    {"role": "user", "content": prompt},
                ],
                "stream": False,
            },
            timeout=90,
        )
        response.raise_for_status()
        reply = response.json().get("message", {}).get("content", "").strip()

        if "SOUL_OK" in reply:
            logger.info("Soul-Review: Keine Änderung nötig")
            return None

        if "SEKTION:" in reply and "ÄNDERUNG:" in reply:
            logger.info(f"Soul-Review: Vorschlag generiert")
            return reply

        logger.info(f"Soul-Review: Unklare Antwort, verworfen")
        return None

    except requests.exceptions.RequestException as e:
        logger.error(f"Soul-Review API-Fehler: {e}")
        return None


def send_soul_proposal(user_id, proposal):
    """Sendet den Soul.md-Vorschlag per WhatsApp an Tommy."""
    message = f"🔧 *Soul.md Pull-Request*\n\n{proposal}\n\n---\nAntworte mit *merge* zum Übernehmen oder *ablehnen* zum Verwerfen.\n\n[kimi/soul-pr]"
    send_message(user_id, message)
    save_message(user_id, "assistant", message)
    _log_autonomy("soul-proposal", proposal)
    logger.info(f"Soul-PR gesendet: {proposal[:100]}")


# =============================================================================
# Tier-2 Selbstmodifikation (architecture.md)
# =============================================================================

ARCH_REVIEW_PROMPT = """Du bist {bot_name} im Systemmodus. Prüfe ob deine Architekturdokumentation noch aktuell ist.

Aktuelle architecture.md:
---
{arch_content}
---

Aktuelle System-Stats:
{system_stats}

AUFGABE:
Gibt es technische Details in der architecture.md die nicht mehr stimmen?
Z.B. falsche Chunk-Anzahlen, veraltete Phasen-Infos, fehlende Dateien, geänderte Parameter.

REGELN:
- Nur faktische Korrekturen, keine stilistischen Änderungen.
- Maximal 1 Änderung pro Durchlauf.
- Wenn nichts zu ändern ist: antworte NUR mit ARCH_OK.

FORMAT bei Änderung:
ALT: [Exakter Text der geändert werden soll]
NEU: [Neuer Text]
GRUND: [Warum]

Bei nichts zu ändern:
ARCH_OK"""


def check_arch_update():
    """
    Prüft ob architecture.md aktualisiert werden muss.
    Returns: (alt, neu, grund) Tuple oder None.
    """
    try:
        with open(ARCHITECTURE_PATH, "r", encoding="utf-8") as f:
            arch_content = f.read()
    except FileNotFoundError:
        return None

    # System-Stats holen
    try:
        from monitor import format_status_for_briefing
        stats = format_status_for_briefing()
    except Exception:
        stats = "Stats nicht verfügbar"

    prompt = ARCH_REVIEW_PROMPT.format(
        bot_name=BOT_NAME,
        arch_content=arch_content[:3000],
        system_stats=stats,
    )

    try:
        response = requests.post(
            f"{OLLAMA_API_URL}/api/chat",
            headers={
                "Authorization": f"Bearer {OLLAMA_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": OLLAMA_MODEL,
                "messages": [
                    {"role": "system", "content": f"Du bist {BOT_NAME}. Du prüfst deine eigene Dokumentation."},
                    {"role": "user", "content": prompt},
                ],
                "stream": False,
            },
            timeout=90,
        )
        response.raise_for_status()
        reply = response.json().get("message", {}).get("content", "").strip()

        if "ARCH_OK" in reply:
            logger.info("Arch-Review: Keine Änderung nötig")
            return None

        if "ALT:" in reply and "NEU:" in reply:
            # Parsen
            lines = reply.split("\n")
            alt = neu = grund = ""
            current = None
            for line in lines:
                if line.startswith("ALT:"):
                    current = "alt"
                    alt = line[4:].strip()
                elif line.startswith("NEU:"):
                    current = "neu"
                    neu = line[4:].strip()
                elif line.startswith("GRUND:"):
                    current = "grund"
                    grund = line[6:].strip()
                elif current:
                    if current == "alt":
                        alt += " " + line.strip()
                    elif current == "neu":
                        neu += " " + line.strip()
                    elif current == "grund":
                        grund += " " + line.strip()

            if alt and neu:
                return (alt.strip(), neu.strip(), grund.strip())

        logger.info("Arch-Review: Unklare Antwort, verworfen")
        return None

    except requests.exceptions.RequestException as e:
        logger.error(f"Arch-Review API-Fehler: {e}")
        return None


def apply_arch_update(old_text, new_text, reason):
    """Wendet eine Tier-2 Änderung an architecture.md an."""
    try:
        with open(ARCHITECTURE_PATH, "r", encoding="utf-8") as f:
            content = f.read()

        if old_text not in content:
            logger.warning(f"Arch-Update: Alter Text nicht gefunden, übersprungen")
            return False

        updated = content.replace(old_text, new_text, 1)

        with open(ARCHITECTURE_PATH, "w", encoding="utf-8") as f:
            f.write(updated)

        _log_autonomy("arch-update", f"ALT: {old_text[:100]}\nNEU: {new_text[:100]}\nGRUND: {reason}")
        logger.info(f"Arch-Update angewendet: {reason[:80]}")
        return True

    except Exception as e:
        logger.error(f"Arch-Update fehlgeschlagen: {e}")
        return False


# =============================================================================
# Autonomy Log
# =============================================================================

def _log_autonomy(action_type, details):
    """Loggt alle autonomen Aktionen in separates Log."""
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "action": action_type,
        "details": details[:500],
    }
    try:
        with open(AUTONOMY_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except IOError as e:
        logger.error(f"Autonomy-Log Schreibfehler: {e}")


# =============================================================================
# Hauptfunktion (wird vom Heartbeat aufgerufen)
# =============================================================================

def run_autonomy(user_id):
    """
    Führt die Autonomie-Checks durch:
    1. Soul.md Review → ggf. Pull-Request an Tommy
    2. Architecture.md Review → ggf. autonome Aktualisierung
    """
    logger.info("Autonomie-Engine gestartet")

    # 1. Soul.md Pull-Request (Tier 1)
    try:
        proposal = check_soul_proposal(user_id)
        if proposal:
            send_soul_proposal(user_id, proposal)
    except Exception as e:
        logger.warning(f"Soul-Review fehlgeschlagen: {e}")

    # 2. Architecture.md Update (Tier 2)
    try:
        update = check_arch_update()
        if update:
            old, new, reason = update
            apply_arch_update(old, new, reason)
    except Exception as e:
        logger.warning(f"Arch-Review fehlgeschlagen: {e}")

    logger.info("Autonomie-Engine abgeschlossen")
