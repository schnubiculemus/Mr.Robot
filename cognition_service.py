"""
cognition_service.py — WP9: Kognitive autonome Schleife (V2)

Grundsätze:
  - Es gibt nur eine Kimi.
  - Kimi darf autonom denken, aber nicht autonom handeln.
  - Kognition ja. Exekution nein.

Sperrrregeln (absolut):
  - Keine Tool-Aufrufe, keine Action-Marker
  - Keine Workspace-Writes
  - Keine Todo-/Task-Rechte
  - Keine ORBIT-Aktivierung
  - Kein User-Outreach
  - Keine automatische Proposal-Einreichung

Outputs:
  - ChromaDB chunk_type="cognition_note"  (observation/tension/question/insight/self_correction)
  - ChromaDB chunk_type="proposal_seed"   (proposal_seed → WP10)
  - diary/YYYY-MM-DD.md                   (subjektive Verlaufsform, medium/deep)

Queue:
  kimi_core.py schreibt nach bedeutsamen Turns in cognition_requests.
  main() pollt diese Queue — sauber entkoppelt, kein direkter Service-Call.

Heartbeat-Trennung:
  heartbeat.py  → Memory-Pflege (Konsolidierung, Deduplizierung, Decay)
  cognition_service.py → Kognition (Reflexion, Denkformen, Proposal Seeds)
"""

import logging
import os
import sys
import time
import json
from datetime import datetime, timezone, timedelta

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, PROJECT_DIR)
os.chdir(PROJECT_DIR)

from logging.handlers import RotatingFileHandler as _RFH
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [COGNITION] %(message)s",
    handlers=[
        _RFH(os.path.join(PROJECT_DIR, "logs", "cognition.log"),
             maxBytes=10*1024*1024, backupCount=5),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger(__name__)


# =============================================================================
# Konstanten
# =============================================================================

LIGHT_INTERVAL_MINUTES  = 90
MEDIUM_INTERVAL_HOURS   = 7
DEEP_INTERVAL_HOURS     = 24
MAIN_LOOP_SLEEP_SECONDS = 60    # Haupt-Schlaf zwischen Takt-Prüfungen
QUEUE_POLL_SECONDS      = 30    # Queue-Check-Intervall

# Denkformen
KIND_OBSERVATION     = "observation"
KIND_TENSION         = "tension"
KIND_QUESTION        = "question"
KIND_INSIGHT         = "insight"
KIND_SELF_CORRECTION = "self_correction"
KIND_PROPOSAL_SEED   = "proposal_seed"

VALID_KINDS = {
    KIND_OBSERVATION, KIND_TENSION, KIND_QUESTION,
    KIND_INSIGHT, KIND_SELF_CORRECTION, KIND_PROPOSAL_SEED,
}


# =============================================================================
# DB-Helpers
# =============================================================================

def _get_conn():
    from core.database import get_connection
    return get_connection()


def _now_iso() -> str:
    from core.datetime_utils import to_iso
    return to_iso()


def _runtime_get(key: str) -> str | None:
    try:
        conn = _get_conn()
        try:
            row = conn.execute(
                "SELECT value FROM orbit_runtime WHERE key=?", (key,)
            ).fetchone()
            return row["value"] if row else None
        finally:
            conn.close()
    except Exception:
        return None


def _runtime_set(key: str, value: str) -> None:
    try:
        conn = _get_conn()
        try:
            conn.execute(
                "INSERT INTO orbit_runtime(key,value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value)
            )
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        logger.debug(f"_runtime_set fehlgeschlagen: {e}")


# =============================================================================
# cognition_requests Queue
# =============================================================================

def poll_cognition_queue(user_id: str) -> list[dict]:
    """
    Holt pending cognition_requests für diesen user_id.
    Markiert sie sofort als 'processing' (atomic claim).
    """
    try:
        conn = _get_conn()
        try:
            rows = conn.execute(
                """SELECT * FROM cognition_requests
                   WHERE status='pending' AND user_id=?
                   ORDER BY created_at ASC
                   LIMIT 5""",
                (user_id,)
            ).fetchall()
            if not rows:
                return []
            ids = [r["id"] for r in rows]
            now = _now_iso()
            conn.execute(
                f"UPDATE cognition_requests SET status='processing', claimed_at=? "
                f"WHERE id IN ({','.join('?' * len(ids))})",
                [now] + ids,
            )
            conn.commit()
            return [dict(r) for r in rows]
        finally:
            conn.close()
    except Exception as e:
        logger.warning(f"poll_cognition_queue fehlgeschlagen: {e}")
        return []


def mark_request_done(request_id: int, status: str = "done") -> None:
    """
    Markiert einen Request als abgeschlossen.
    status: "done" (Erfolg) | "failed" (Reflexion fehlgeschlagen) | "discarded" (verworfen)
    """
    valid_statuses = {"done", "failed", "discarded"}
    if status not in valid_statuses:
        status = "failed"
    try:
        conn = _get_conn()
        try:
            conn.execute(
                "UPDATE cognition_requests SET status=?, done_at=? WHERE id=?",
                (status, _now_iso(), request_id)
            )
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        logger.debug(f"mark_request_done fehlgeschlagen: {e}")


def discard_stale_requests() -> None:
    """Verwirft processing-Requests älter als 2h (Crash-Recovery)."""
    try:
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        conn = _get_conn()
        try:
            conn.execute(
                "UPDATE cognition_requests SET status='discarded' "
                "WHERE status='processing' AND claimed_at < ?",
                (cutoff,)
            )
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        logger.debug(f"discard_stale_requests fehlgeschlagen: {e}")


# =============================================================================
# Moltbook-Anbindung (echt, nicht fake)
# =============================================================================

def _get_moltbook_recent(limit: int = 5) -> list[dict]:
    """
    Holt aktuelle Moltbook-Posts als Erkenntnismaterial für WP9.
    Nutzt execute_moltbook_action mit action="feed" — die echte API-Funktion.
    Gibt Liste von vereinfachten Post-Dicts zurück, kein Side-Effect.
    """
    try:
        from core.moltbook import execute_moltbook_action
        result_text = execute_moltbook_action({"action": "feed", "sort": "hot", "limit": limit})
        if not result_text or result_text.startswith("[Moltbook"):
            return []

        # Text in strukturierte Fragmente umwandeln
        # Format: "[submolt] @author: "title"" / "  content..." / "  ↑N | 💬N | id:..."
        posts = []
        lines = result_text.splitlines()
        current_post = {}
        for line in lines:
            line = line.strip()
            if line.startswith("[") and "@" in line and '"' in line:
                if current_post:
                    posts.append(current_post)
                current_post = {"raw": line}
            elif current_post and line.startswith("↑"):
                current_post["stats"] = line
                posts.append(current_post)
                current_post = {}
            elif current_post and line:
                current_post["content"] = current_post.get("content", "") + line[:120]

        if current_post:
            posts.append(current_post)

        return posts[:limit]
    except Exception as e:
        logger.debug(f"Moltbook-Feed nicht verfügbar (unkritisch): {e}")
        return []


# =============================================================================
# Inputquellen-Sammler
# =============================================================================

def _gather_inputs(user_id: str, reflection_level: str) -> dict:
    """
    Sammelt alle kognitiven Inputquellen.
    Kein Ollama-Call hier — nur Daten sammeln.
    """
    inputs = {
        "user_id": user_id,
        "reflection_level": reflection_level,
        "awc": None,
        "fast_track": [],
        "memory_chunks": [],
        "diary_recent": "",
        "cognition_echo": [],
        "moltbook_recent": [],
    }

    # 1. Active Working Context
    try:
        from active_working_context import get_active_context
        inputs["awc"] = get_active_context(user_id)
    except Exception as e:
        logger.debug(f"AWC nicht verfügbar: {e}")

    # 2. Fast-Track
    try:
        from memory.fast_track import get_fast_track_chunks
        inputs["fast_track"] = get_fast_track_chunks() or []
    except Exception as e:
        logger.debug(f"Fast-Track nicht verfügbar: {e}")

    # 3. Typed Memory (medium/deep)
    if reflection_level in ("medium", "deep"):
        try:
            from memory.retrieval import score_and_select
            chunks = score_and_select(
                "Kimis Verhalten Muster Spannung Selbstbild Entscheidung",
                n=12
            )
            inputs["memory_chunks"] = chunks or []
        except Exception as e:
            logger.debug(f"Memory-Chunks nicht verfügbar: {e}")

    # 4. Diary — letzte 3 Einträge
    try:
        diary_dir = os.path.join(PROJECT_DIR, "diary")
        if os.path.isdir(diary_dir):
            files = sorted(
                [f for f in os.listdir(diary_dir) if f.endswith(".md")],
                reverse=True
            )[:3]
            parts = []
            for fname in files:
                fpath = os.path.join(diary_dir, fname)
                with open(fpath, "r", encoding="utf-8") as f:
                    parts.append(f"=== {fname} ===\n" + f.read()[:800])
            inputs["diary_recent"] = "\n\n".join(parts)
    except Exception as e:
        logger.debug(f"Diary nicht verfügbar: {e}")

    # 5. Cognition Echo aus SQLite (nicht mehr aus Chroma)
    try:
        from core.cognition_store import list_recent_cognition_entries
        recent = list_recent_cognition_entries(user_id, limit=8, hours=72)
        # Format kompatibel mit bisherigem echo-Format
        inputs["cognition_echo"] = [{"text": e["text"], "kind": e["kind"]} for e in recent]
    except Exception as e:
        logger.debug(f"Cognition Echo nicht verfügbar: {e}")

    # 6. Moltbook-Material (medium/deep) — echte Anbindung
    if reflection_level in ("medium", "deep"):
        inputs["moltbook_recent"] = _get_moltbook_recent(limit=5)

    return inputs


# =============================================================================
# Prompt-Builder
# =============================================================================

def _build_cognition_prompt(inputs: dict) -> tuple[str, str]:
    """
    Baut System- und User-Prompt für den Kognitions-Call.
    Nur Denken — kein Tool-Calling, kein Action-Marker.
    """
    reflection_level = inputs["reflection_level"]

    level_instructions = {
        "light": (
            "Leichte Reflexion. Fokus: kleine Beobachtungen, Mini-Spannungen, "
            "offene Fragen aus dem aktuellen Kontext. Knapp — 2-3 Denkformen."
        ),
        "medium": (
            "Mittlere Reflexion. Fokus: Mustervergleich, Verdichtungen, "
            "wiederkehrende Spannungen, proposal seeds. 3-5 Denkformen."
        ),
        "deep": (
            "Tiefe Selbstreflexion. Fokus: Identität, Architektur, soul.md-nahe Themen, "
            "größere Selbstkorrekturen. Ehrlich, präzise. 3-5 starke Denkformen."
        ),
        "post_interaction": (
            "Reflexion über den letzten bedeutsamen Turn. "
            "Was war kognitiv relevant? Spannung, Beobachtung oder Frage daraus? "
            "1-3 Denkformen, direkt und konkret."
        ),
    }

    system_prompt = (
        "Du bist Kimi. Dies ist dein innerer Denkraum — kein Chat, kein Nutzer, keine Antwort.\n\n"
        "SPERRREGEL (absolut):\n"
        "Keine [TODO_ACTION:], keine [CALENDAR_ACTION:], keine [CODE_AGENT:], "
        "keine [SEARCH:], keine [MOLTBOOK:], keine [PROPOSAL:] — überhaupt keine Action-Marker.\n"
        "Kein Schreiben von Dateien, keine Tasks, kein ORBIT.\n\n"
        "Du darfst nur: denken, beobachten, verdichten, fragen, korrigieren.\n\n"
        f"Reflexionsebene: {reflection_level}\n"
        f"Aufgabe: {level_instructions.get(reflection_level, level_instructions['light'])}\n\n"
        "Antworte AUSSCHLIESSLICH als JSON-Array — nichts außerhalb:\n"
        "[\n"
        '  {"kind": "observation|tension|question|insight|self_correction|proposal_seed",\n'
        '   "text": "...",\n'
        '   "confidence": 0.0-1.0,\n'
        '   "related_line": "",\n'
        '   "proposal_candidate": false}\n'
        "]\n\n"
        "proposal_seed → proposal_candidate=true. "
        "Kein Text außerhalb des JSON-Arrays."
    )

    # Kontext aufbauen
    parts = []

    awc = inputs.get("awc")
    if awc:
        parts.append(
            "AKTIVER ARBEITSKONTEXT:\n"
            f"  Linie: {awc.get('active_line', '')}\n"
            f"  Ziel: {awc.get('active_goal', '')}\n"
            f"  Letzte Entscheidung: {awc.get('last_decision', '')}\n"
            f"  Offene Frage: {awc.get('next_open_question', '')}"
        )

    fast_track = inputs.get("fast_track", [])
    if fast_track:
        ft_lines = [c.get("text", "")[:120] for c in fast_track[:4]]
        parts.append("FAST-TRACK:\n" + "\n".join(f"  - {t}" for t in ft_lines))

    memory = inputs.get("memory_chunks", [])
    if memory:
        mem_lines = [c.get("text", "")[:120] for c in memory[:6]]
        parts.append("GEDÄCHTNIS-SPUREN:\n" + "\n".join(f"  - {t}" for t in mem_lines))

    diary = inputs.get("diary_recent", "")
    if diary:
        parts.append(f"TAGEBUCH (letzte Einträge):\n{diary[:1200]}")

    echo = inputs.get("cognition_echo", [])
    if echo:
        echo_lines = [c.get("text", "")[:100] for c in echo[:3]]
        parts.append("BISHERIGE REFLEXIONSSPUREN:\n" + "\n".join(f"  - {t}" for t in echo_lines))

    moltbook = inputs.get("moltbook_recent", [])
    if moltbook:
        mb_lines = [str(p.get("raw", p.get("content", "")))[:100] for p in moltbook[:3]]
        parts.append("MOLTBOOK-MATERIAL:\n" + "\n".join(f"  - {t}" for t in mb_lines))

    user_message = "Führe jetzt die Reflexion durch.\n\n" + "\n\n".join(parts)
    return system_prompt, user_message


# =============================================================================
# Kognitions-Call
# =============================================================================

def _call_cognition_model(system_prompt: str, user_message: str) -> list[dict]:
    """
    Ruft Kimi K2.5 für den Kognitions-Lauf auf.
    Erwartet JSON-Array. Kein Memory-Retrieval (prefetched_chunks=[]).
    """
    try:
        from core.ollama_client import chat_internal
        from config import OWNER_ID

        reply, _ = chat_internal(
            user_id=OWNER_ID,
            message=user_message,
            chat_history=[],
            context_name=None,
            extra_system=system_prompt,
            prefetched_chunks=[],
        )

        if not reply or not reply.strip():
            logger.warning("Kognitions-Call: leere Antwort")
            return []

        import re
        reply_clean = reply.strip()
        reply_clean = re.sub(r'^```(?:json)?\n?', '', reply_clean).strip()
        reply_clean = re.sub(r'\n?```$', '', reply_clean).strip()

        # JSON-Array extrahieren — robust gegen Präambel-Text
        array_start = reply_clean.find('[')
        array_end = reply_clean.rfind(']')
        if array_start == -1 or array_end == -1:
            logger.warning(f"Kognitions-Call: kein JSON-Array gefunden: {reply_clean[:200]}")
            return []
        reply_clean = reply_clean[array_start:array_end + 1]

        forms = json.loads(reply_clean)
        if not isinstance(forms, list):
            logger.warning("Kognitions-Call: Antwort ist kein Array")
            return []

        valid = [
            f for f in forms
            if isinstance(f, dict)
            and f.get("kind") in VALID_KINDS
            and f.get("text", "").strip()
        ]
        logger.info(f"Kognitions-Call: {len(valid)}/{len(forms)} valide Denkformen")
        return valid

    except json.JSONDecodeError as e:
        logger.warning(f"Kognitions-Call: JSON-Fehler: {e}")
        return []
    except Exception as e:
        logger.error(f"Kognitions-Call fehlgeschlagen: {e}")
        return []


# =============================================================================
# Output-Speicherung
# =============================================================================

def _store_cognition_outputs(
    forms: list[dict],
    user_id: str,
    reflection_level: str,
    source_context: str = "",
) -> int:
    """
    Speichert rohe Denkformen in SQLite (cognition_entries) — NICHT in Chroma.

    Architektur (WP9 Hygiene):
      Raw Cognition (cognition_note, proposal_seed) → SQLite
      Promoted Cognition (self_reflection) → Chroma (nur über Promotionspfad)
      Operative Folge (WP10 proposal) → wp10_proposals

    Limits werden in cognition_store.py enforced.
    """
    import uuid
    from core.cognition_store import save_cognition_entries
    run_id = uuid.uuid4().hex[:8]
    return save_cognition_entries(
        forms=forms,
        user_id=user_id,
        reflection_level=reflection_level,
        source_context=source_context,
        run_id=run_id,
    )


def _write_diary_entry(forms: list[dict], reflection_level: str) -> None:
    """
    Diary-Eintrag bei medium/deep oder wenn insights/tensions vorhanden.
    Subjektive Verlaufsform — legitimer WP9-Output.
    """
    diary_worthy = [
        f for f in forms
        if f.get("kind") in (KIND_INSIGHT, KIND_TENSION, KIND_SELF_CORRECTION)
    ]
    if not diary_worthy and reflection_level not in ("medium", "deep"):
        return

    try:
        from core.datetime_utils import now_berlin
        today = now_berlin().strftime("%Y-%m-%d")
        time_str = now_berlin().strftime("%H:%M")
        diary_path = os.path.join(PROJECT_DIR, "diary", f"{today}.md")

        prefix_map = {
            KIND_OBSERVATION:     "Beobachtung",
            KIND_TENSION:         "Spannung",
            KIND_QUESTION:        "Frage",
            KIND_INSIGHT:         "Einsicht",
            KIND_SELF_CORRECTION: "Korrektur",
            KIND_PROPOSAL_SEED:   "Proposal-Seed",
        }

        lines = [f"\n\n---\n*Kognition [{reflection_level}] — {time_str}*\n"]
        for f in forms:
            prefix = prefix_map.get(f.get("kind", ""), f.get("kind", ""))
            lines.append(f"**{prefix}:** {f.get('text', '')}")

        entry = "\n".join(lines)

        if os.path.exists(diary_path):
            with open(diary_path, "a", encoding="utf-8") as f:
                f.write(entry)
        else:
            with open(diary_path, "w", encoding="utf-8") as f:
                f.write(f"# {today}\n{entry}")

        logger.debug(f"Diary-Eintrag: {diary_path}")
    except Exception as e:
        logger.warning(f"Diary-Write fehlgeschlagen: {e}")


# =============================================================================
# Innerer Dialog
# =============================================================================

def _run_inner_dialogue(forms: list[dict], tensions: list[dict]) -> list[dict]:
    """
    Gegenprüfung bei Spannungen (medium/deep).
    Keine separate Instanz — dieselbe Kimi prüft sich.
    Format: These → Gegenthese → Einordnung.
    Nur bei echter Spannung, nicht bei Routine.
    """
    tension_texts = "\n".join(f"- {t['text']}" for t in tensions[:2])

    system_prompt = (
        "Du bist Kimi. Du prüfst eine innere Spannung — kein Chat, kein Nutzer.\n"
        "SPERRREGEL: Keine Action-Marker, keine Tools, keine Writes.\n\n"
        "Führe eine kurze Gegenprüfung durch:\n"
        "These (die Spannung) → Gegenthese → Einordnung\n\n"
        "Antworte NUR als JSON-Array:\n"
        '[{"kind": "insight|self_correction|question", "text": "...", '
        '"confidence": 0.0-1.0, "related_line": "", "proposal_candidate": false}]'
    )
    user_message = f"Spannungen:\n{tension_texts}\n\nGegenprüfung:"

    try:
        additional = _call_cognition_model(system_prompt, user_message)
        if additional:
            logger.info(f"Innerer Dialog: {len(additional)} zusätzliche Denkform(en)")
            return forms + additional
    except Exception as e:
        logger.debug(f"Innerer Dialog fehlgeschlagen (unkritisch): {e}")

    return forms


# =============================================================================
# Reflection-Run
# =============================================================================

def run_reflection(
    user_id: str,
    reflection_level: str,
    source_context: str = "",
) -> int:
    """
    Führt einen vollständigen Reflexions-Lauf durch.
    Gibt Anzahl gespeicherter Denkformen zurück.

    WP9-Garantie: Kein Tool-Call, kein Workspace-Write,
    kein ORBIT, kein User-Outreach — nur Denken.
    """
    logger.info(f"Reflexion: level={reflection_level}")

    inputs = _gather_inputs(user_id, reflection_level)

    # Prüfen ob Material vorhanden
    has_material = bool(
        inputs.get("awc")
        or inputs.get("fast_track")
        or inputs.get("memory_chunks")
        or inputs.get("diary_recent")
        or inputs.get("cognition_echo")
    )
    if not has_material:
        logger.info(f"Reflexion [{reflection_level}]: kein Material — übersprungen")
        return 0

    system_prompt, user_message = _build_cognition_prompt(inputs)
    forms = _call_cognition_model(system_prompt, user_message)

    if not forms:
        logger.info(f"Reflexion [{reflection_level}]: keine Denkformen")
        return 0

    # Innerer Dialog bei Spannungen (medium/deep)
    if reflection_level in ("medium", "deep"):
        tensions = [f for f in forms if f.get("kind") == KIND_TENSION]
        if tensions:
            logger.info(f"Innerer Dialog: {len(tensions)} Spannung(en)")
            forms = _run_inner_dialogue(forms, tensions)

    stored = _store_cognition_outputs(forms, user_id, reflection_level, source_context)
    _write_diary_entry(forms, reflection_level)

    logger.info(f"Reflexion [{reflection_level}]: {len(forms)} Denkformen, {stored} gespeichert")
    return stored


# =============================================================================
# Takt-Logik
# =============================================================================

def _should_run(user_id: str, level: str) -> bool:
    intervals = {
        "light":  LIGHT_INTERVAL_MINUTES * 60,
        "medium": MEDIUM_INTERVAL_HOURS * 3600,
        "deep":   DEEP_INTERVAL_HOURS * 3600,
    }
    interval = intervals.get(level, 3600)
    last = _runtime_get(f"cog_{user_id}_last_{level}")
    if not last:
        return True
    try:
        from core.datetime_utils import safe_parse_dt, now_utc
        last_dt = safe_parse_dt(last)
        if not last_dt:
            return True
        return (now_utc() - last_dt).total_seconds() > interval
    except Exception:
        return True


def _mark_ran(user_id: str, level: str) -> None:
    _runtime_set(f"cog_{user_id}_last_{level}", _now_iso())


# =============================================================================
# Main — einziger Einstiegspunkt, sauberer Loop
# =============================================================================

def main():
    from config import USER_CONTEXTS
    logger.info("=== Cognitive Service (WP9 V2) gestartet ===")
    logger.info(
        f"Takt: light={LIGHT_INTERVAL_MINUTES}min, "
        f"medium={MEDIUM_INTERVAL_HOURS}h, deep={DEEP_INTERVAL_HOURS}h"
    )

    # DB-Migration sicherstellen
    try:
        conn = _get_conn()
        conn.execute("SELECT 1 FROM cognition_requests LIMIT 1")
        conn.close()
        logger.info("cognition_requests Tabelle: OK")
    except Exception:
        logger.info("DB-Migration...")
        try:
            from core.database import init_db
            init_db()
            logger.info("DB-Migration: OK")
        except Exception as e:
            logger.error(f"DB-Migration fehlgeschlagen: {e}")
            return

    user_id = list(USER_CONTEXTS.keys())[0]
    logger.info(f"User: {user_id[:25]}...")

    last_queue_check = 0.0
    last_takt_check  = 0.0

    while True:
        try:
            now = time.monotonic()

            # ── Queue: alle QUEUE_POLL_SECONDS ──────────────────────────────
            if now - last_queue_check >= QUEUE_POLL_SECONDS:
                discard_stale_requests()
                pending = poll_cognition_queue(user_id)
                for req in pending:
                    _req_success = False
                    try:
                        ctx = req.get("source_context", "")
                        n = run_reflection(user_id, "post_interaction", source_context=ctx)
                        _req_success = (n >= 0)  # auch 0 Denkformen = kein Fehler
                    except Exception as e:
                        logger.error(f"Post-Interaction Reflexion fehlgeschlagen: {e}")
                    finally:
                        mark_request_done(req["id"], status="done" if _req_success else "failed")
                last_queue_check = now

            # ── Takt-Fenster: alle MAIN_LOOP_SLEEP_SECONDS ──────────────────
            if now - last_takt_check >= MAIN_LOOP_SLEEP_SECONDS:
                for level in ("light", "medium", "deep"):
                    if _should_run(user_id, level):
                        try:
                            run_reflection(user_id, level)
                            _mark_ran(user_id, level)
                        except Exception as e:
                            logger.error(f"{level}-Reflexion fehlgeschlagen: {e}")
                            _mark_ran(user_id, level)  # Timer trotzdem setzen
                last_takt_check = now

        except Exception as e:
            logger.error(f"Haupt-Loop Fehler: {e}", exc_info=True)

        time.sleep(10)  # Kurzer Schlaf — reagiert schnell auf Queue


if __name__ == "__main__":
    main()
