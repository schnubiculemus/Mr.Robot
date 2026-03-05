"""
SchnuBot.ai - Autonomie-Engine (Phase 7 + Soul-PR Handling)
Referenz: soul.md, Abschnitt Selbstreflexion und Entwicklung

Zwei Mechanismen:
1. Soul.md Pull-Requests (Tier 1): Mr. Robot schlägt Verfassungsänderungen vor.
   Tommy bekommt den Vorschlag per WhatsApp und bestätigt oder lehnt ab.
   Merge/Ablehnen wird über /merge und /ablehnen Commands in app.py gesteuert.
   Max 1 PR pro Tag, kein neuer PR solange einer offen ist.

2. Tier-2 Selbstmodifikation: Mr. Robot kann architecture.md eigenständig anpassen.
   Änderungen werden transparent geloggt.

Wird vom Heartbeat aufgerufen.
"""

import os
import json
import logging
import requests
import difflib
from datetime import datetime, timezone, timedelta

from config import OLLAMA_API_URL, OLLAMA_API_KEY, OLLAMA_MODEL, BOT_NAME
from core.whatsapp import send_message
from core.database import save_message

logger = logging.getLogger(__name__)

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
SOUL_PATH = os.path.join(PROJECT_DIR, "soul.md")
ARCHITECTURE_PATH = os.path.join(PROJECT_DIR, "architecture.md")
AUTONOMY_LOG_PATH = os.path.join(PROJECT_DIR, "logs", "autonomy.log")
SOUL_PR_PATH = os.path.join(PROJECT_DIR, "soul_pr_pending.json")

# Cooldown: Max 1 Soul-PR pro 24h
SOUL_PR_COOLDOWN_HOURS = 24


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
                    {"role": "system", "content": f"Du bist {BOT_NAME}. Du reflektierst über deine eigene Verfassung."},
                    {"role": "user", "content": prompt},
                ],
                "stream": False,
            },
            timeout=90,
        )

        if not result:
            return None

        reply = result.get("message", {}).get("content", "").strip()

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
    """
    Sendet den Soul.md-Vorschlag per WhatsApp an Tommy.
    Speichert den PR persistent als soul_pr_pending.json.
    NICHT in messages-DB speichern — beeinflusst sonst den Stille-Timer.
    """
    message = f"🔧 *Soul.md Pull-Request*\n\n{proposal}\n\n---\nAntworte mit */merge* zum Übernehmen oder */ablehnen* zum Verwerfen.\n\n[kimi/soul-pr]"
    send_message(user_id, message)
    # Bewusst KEIN save_message() — Soul-PRs sind System-Nachrichten,
    # kein Gespräch, und sollen den Stille-Timer nicht zurücksetzen.

    # PR persistent speichern
    _save_pending_pr(proposal, user_id)
    _log_autonomy("soul-proposal", proposal)
    logger.info(f"Soul-PR gesendet und gespeichert: {proposal[:100]}")


# =============================================================================
# Soul-PR State Management
# =============================================================================

def _save_pending_pr(proposal, user_id):
    """Speichert einen offenen Soul-PR als JSON."""
    pr_data = {
        "proposal": proposal,
        "user_id": user_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "status": "pending",
    }
    try:
        with open(SOUL_PR_PATH, "w", encoding="utf-8") as f:
            json.dump(pr_data, f, ensure_ascii=False, indent=2)
    except IOError as e:
        logger.error(f"Soul-PR speichern fehlgeschlagen: {e}")


def get_pending_pr():
    """Lädt den offenen Soul-PR. Returns: Dict oder None."""
    if not os.path.exists(SOUL_PR_PATH):
        return None
    try:
        with open(SOUL_PR_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError):
        return None


def _close_pending_pr(new_status):
    """
    Schließt den offenen Soul-PR mit neuem Status (merged/rejected).
    Datei wird NICHT gelöscht — der Timestamp bleibt für den Cooldown erhalten.
    Wird beim nächsten _can_send_new_pr() für den 24h-Check verwendet.
    """
    pending = get_pending_pr()
    if not pending:
        return
    pending["status"] = new_status
    pending["closed_at"] = datetime.now(timezone.utc).isoformat()
    try:
        with open(SOUL_PR_PATH, "w", encoding="utf-8") as f:
            json.dump(pending, f, ensure_ascii=False, indent=2)
    except IOError as e:
        logger.error(f"Soul-PR schließen fehlgeschlagen: {e}")


def _can_send_new_pr():
    """
    Prüft ob ein neuer Soul-PR gesendet werden darf.
    Nein wenn: PR offen ODER letzter PR < 24h her.
    """
    # Offener PR vorhanden?
    pending = get_pending_pr()
    if pending and pending.get("status") == "pending":
        logger.info("Soul-PR Skip: Offener PR vorhanden")
        return False

    # Cooldown prüfen (auch für abgeschlossene PRs)
    if pending:
        created = pending.get("created_at", "")
        if created:
            try:
                created_dt = datetime.fromisoformat(created)
                age_hours = (datetime.now(timezone.utc) - created_dt).total_seconds() / 3600
                if age_hours < SOUL_PR_COOLDOWN_HOURS:
                    logger.info(f"Soul-PR Skip: Cooldown ({age_hours:.1f}h < {SOUL_PR_COOLDOWN_HOURS}h)")
                    return False
            except (ValueError, TypeError):
                pass

    return True


# =============================================================================
# /merge Command Handler
# =============================================================================

def handle_merge(user_id):
    """
    Verarbeitet den /merge Command.
    1. Pending-PR laden
    2. Kimi die Änderung an soul.md anwenden lassen
    3. Diff als Nachricht senden
    4. Decision-Chunk ins Gedächtnis
    5. PR löschen

    Returns: Reply-Text für den User.
    """
    pending = get_pending_pr()
    if not pending or pending.get("status") != "pending":
        return "Kein offener Soul-PR vorhanden."

    proposal = pending["proposal"]

    # soul.md laden
    try:
        with open(SOUL_PATH, "r", encoding="utf-8") as f:
            soul_before = f.read()
    except FileNotFoundError:
        return "Fehler: soul.md nicht gefunden."

    # Kimi die Änderung anwenden lassen
    soul_after = _apply_proposal_via_llm(soul_before, proposal)
    if not soul_after:
        return "Merge fehlgeschlagen — Kimi konnte den Vorschlag nicht anwenden. PR bleibt offen."

    # Sicherheitscheck: soul.md sollte sich tatsächlich geändert haben
    if soul_after.strip() == soul_before.strip():
        return "Merge abgebrochen — keine Änderung erkannt. PR bleibt offen."

    # soul.md schreiben
    try:
        with open(SOUL_PATH, "w", encoding="utf-8") as f:
            f.write(soul_after)
    except IOError as e:
        logger.error(f"soul.md schreiben fehlgeschlagen: {e}")
        return f"Merge fehlgeschlagen — Dateifehler: {e}"

    # Diff berechnen und senden
    diff_text = _compute_diff(soul_before, soul_after)
    diff_msg = f"✅ *Soul.md gemerged*\n\n```\n{diff_text[:3000]}\n```\n\n[kimi/soul-merge]"
    send_message(user_id, diff_msg)
    # Auch hier bewusst kein save_message() für den Diff

    # Decision-Chunk ins Gedächtnis
    _store_merge_chunk(proposal)

    # PR aufräumen
    _log_autonomy("soul-merge", f"Proposal: {proposal[:200]}\nDiff: {diff_text[:300]}")
    _close_pending_pr("merged")

    logger.info(f"Soul-PR gemerged: {proposal[:100]}")
    return "Soul.md wurde aktualisiert. Diff kommt als separate Nachricht."


def _apply_proposal_via_llm(soul_content, proposal):
    """
    Lässt Kimi den Vorschlag konkret auf die soul.md anwenden.
    Returns: Neuer soul.md Inhalt oder None bei Fehler.
    """
    prompt = f"""Du bekommst die aktuelle soul.md und einen genehmigten Änderungsvorschlag.
Wende den Vorschlag an und gib die KOMPLETTE aktualisierte soul.md zurück.

REGELN:
- Gib NUR den soul.md Inhalt zurück, keine Erklärungen, kein Markdown-Fence.
- Ändere NUR was der Vorschlag verlangt. Alles andere bleibt EXAKT gleich.
- Behalte die bestehende Formatierung, Struktur und Markdown bei.
- {{{{BOT_NAME}}}} Placeholder beibehalten wo sie sind.

## AKTUELLE soul.md:
{soul_content}

## GENEHMIGTER VORSCHLAG:
{proposal}

Gib jetzt die komplette aktualisierte soul.md zurück:"""

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
                    {"role": "system", "content": f"Du bist ein präziser Texteditor. Du wendest Änderungen exakt an, ohne eigene Ergänzungen."},
                    {"role": "user", "content": prompt},
                ],
                "stream": False,
            },
            timeout=120,
        )

        if not result:
            logger.error("Soul-Merge: Kein API-Ergebnis")
            return None

        new_content = result.get("message", {}).get("content", "").strip()

        # Markdown-Fence entfernen falls Kimi einen zurückgibt
        if new_content.startswith("```"):
            lines = new_content.split("\n")
            new_content = "\n".join(lines[1:-1]).strip() if len(lines) > 2 else new_content

        # Minimale Plausibilitätsprüfung
        if "# soul.md" not in new_content and "## Wer ich bin" not in new_content:
            logger.warning("Soul-Merge: Ergebnis sieht nicht nach soul.md aus")
            return None

        if len(new_content) < len(soul_content) * 0.5:
            logger.warning("Soul-Merge: Ergebnis zu kurz (>50% Verlust)")
            return None

        return new_content

    except Exception as e:
        logger.error(f"Soul-Merge LLM-Fehler: {e}")
        return None


def _compute_diff(before, after):
    """Berechnet einen lesbaren Unified-Diff."""
    before_lines = before.splitlines(keepends=True)
    after_lines = after.splitlines(keepends=True)

    diff = difflib.unified_diff(
        before_lines, after_lines,
        fromfile="soul.md (vorher)",
        tofile="soul.md (nachher)",
        lineterm="",
    )
    return "".join(diff) or "(keine sichtbaren Änderungen)"


def _store_merge_chunk(proposal):
    """Speichert einen Decision-Chunk für den gemergten Soul-PR."""
    try:
        from memory.chunk_schema import create_chunk
        from memory.memory_store import store_chunk

        chunk = create_chunk(
            text=f"Soul.md Änderung gemerged: {proposal[:300]}",
            chunk_type="decision",
            source="shared",
            confidence=0.95,
            epistemic_status="confirmed",
            tags=["soul-pr", "merge"],
        )
        store_chunk(chunk)
        logger.info(f"Merge-Chunk gespeichert: {chunk['id'][:8]}")
    except Exception as e:
        logger.warning(f"Merge-Chunk Speicherfehler: {e}")


# =============================================================================
# /ablehnen Command Handler
# =============================================================================

def handle_reject(user_id):
    """
    Verarbeitet den /ablehnen Command.
    1. Pending-PR laden
    2. Ablehnungs-Chunk ins Gedächtnis (damit der Bot nicht nochmal dasselbe vorschlägt)
    3. PR löschen

    Returns: Reply-Text für den User.
    """
    pending = get_pending_pr()
    if not pending or pending.get("status") != "pending":
        return "Kein offener Soul-PR vorhanden."

    proposal = pending["proposal"]

    # Ablehnungs-Chunk ins Gedächtnis
    try:
        from memory.chunk_schema import create_chunk
        from memory.memory_store import store_chunk

        chunk = create_chunk(
            text=f"Soul.md Vorschlag ABGELEHNT: {proposal[:300]}",
            chunk_type="self_reflection",
            source="shared",
            confidence=0.90,
            epistemic_status="confirmed",
            tags=["soul-pr", "abgelehnt"],
        )
        store_chunk(chunk)
        logger.info(f"Ablehnungs-Chunk gespeichert: {chunk['id'][:8]}")
    except Exception as e:
        logger.warning(f"Ablehnungs-Chunk Speicherfehler: {e}")

    # Aufräumen
    _log_autonomy("soul-reject", proposal[:300])
    _close_pending_pr("rejected")

    logger.info(f"Soul-PR abgelehnt: {proposal[:100]}")
    return "Soul-PR abgelehnt und verworfen. Ich merk mir das für zukünftige Vorschläge."


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
                    {"role": "system", "content": f"Du bist {BOT_NAME}. Du prüfst deine eigene Dokumentation."},
                    {"role": "user", "content": prompt},
                ],
                "stream": False,
            },
            timeout=90,
        )

        if not result:
            return None

        reply = result.get("message", {}).get("content", "").strip()

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
    1. Soul.md Review → ggf. Pull-Request an Tommy (max 1x/Tag, nicht bei offenem PR)
    2. Architecture.md Review → ggf. autonome Aktualisierung
    """
    logger.info("Autonomie-Engine gestartet")

    # 1. Soul.md Pull-Request (Tier 1)
    try:
        if _can_send_new_pr():
            proposal = check_soul_proposal(user_id)
            if proposal:
                send_soul_proposal(user_id, proposal)
        else:
            logger.info("Soul-PR: Übersprungen (Cooldown oder offener PR)")
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
