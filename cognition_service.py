"""
cognition_service.py — WP9: Kognitive autonome Schleife

Der Cognitive Service ist Kimis innerer Denkmodus.
Er ist kein zweiter Agent, keine zweite Stimme, keine zweite Instanz.
Er ist ein innerer Denkmodus von Kimi Core — derselben Kimi.

Grundsätze:
  - Es gibt nur eine Kimi.
  - Kimi darf autonom denken, aber nicht autonom handeln.
  - Kognition ja. Exekution nein.

Sperrrregeln (absolut):
  - Keine Tool-Aufrufe
  - Keine Workspace-Writes
  - Keine Todo-/Task-Rechte
  - Keine ORBIT-Aktivierung
  - Kein User-Outreach
  - Keine automatische Proposal-Einreichung

Outputs landen ausschließlich in:
  - ChromaDB (chunk_type="cognition_note") für strukturierte Denkformen
  - Diary (Markdown) für subjektive Verlaufsform
  - ChromaDB (chunk_type="proposal_seed") für WP10-Vorbereitung

Reflection-Level:
  light   — kleine Beobachtungen, Mini-Spannungen, offene Fragen
  medium  — Mustervergleich, stärkere Verdichtung, proposal seeds
  deep    — Identität, Architektur, größere Selbstkorrekturen (max 1x/24h)
  post_interaction — nach bedeutsamen Turns
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

LIGHT_INTERVAL_MINUTES   = 90
MEDIUM_INTERVAL_HOURS    = 7
DEEP_INTERVAL_HOURS      = 24
TICK_INTERVAL_SECONDS    = 60   # Polling-Takt
POLL_QUEUE_INTERVAL_SEC  = 30   # cognition_requests Queue-Check

# Denkformen (WP9 §7)
KIND_OBSERVATION     = "observation"
KIND_TENSION         = "tension"
KIND_QUESTION        = "question"
KIND_INSIGHT         = "insight"
KIND_SELF_CORRECTION = "self_correction"
KIND_PROPOSAL_SEED   = "proposal_seed"

REFLECTION_LEVELS = {"light", "medium", "deep", "post_interaction"}


# =============================================================================
# DB-Helpers
# =============================================================================

def _get_conn():
    from core.database import get_connection
    return get_connection()


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


def _now_iso() -> str:
    from core.datetime_utils import to_iso
    return to_iso()


# =============================================================================
# Cognition Queue (cognition_requests)
# =============================================================================

def poll_cognition_queue(user_id: str) -> list[dict]:
    """
    Holt pending cognition_requests aus der Queue.
    Markiert sie sofort als 'processing' (claim).
    """
    try:
        conn = _get_conn()
        try:
            rows = conn.execute(
                """SELECT * FROM cognition_requests
                   WHERE status='pending'
                   ORDER BY created_at ASC
                   LIMIT 5"""
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


def mark_request_done(request_id: int) -> None:
    try:
        conn = _get_conn()
        try:
            conn.execute(
                "UPDATE cognition_requests SET status='done', done_at=? WHERE id=?",
                (_now_iso(), request_id)
            )
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        logger.debug(f"mark_request_done fehlgeschlagen: {e}")


def discard_stale_requests() -> None:
    """Verwirft requests die älter als 2 Stunden und noch processing sind."""
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
# Inputquellen-Sammler
# =============================================================================

def _gather_inputs(user_id: str, reflection_level: str) -> dict:
    """
    Sammelt alle kognitiven Inputquellen für einen Reflexionslauf.
    Gibt strukturiertes Dict zurück — kein Ollama-Call hier.
    """
    inputs = {
        "user_id": user_id,
        "reflection_level": reflection_level,
        "awc": None,
        "fast_track": [],
        "memory_chunks": [],
        "diary_recent": "",
        "self_reflection": [],
        "moltbook_recent": [],
        "cognition_echo": [],
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

    # 3. Typed Memory — Muster, Entscheidungen, Reflexionsspuren
    if reflection_level in ("medium", "deep"):
        try:
            from memory.retrieval import score_and_select
            chunks = score_and_select(
                "Kimis Verhalten Muster Spannung Selbstbild Entscheidung",
                n=15
            )
            inputs["memory_chunks"] = chunks or []
        except Exception as e:
            logger.debug(f"Memory-Chunks nicht verfügbar: {e}")

    # 4. Diary — letzte Einträge
    try:
        diary_dir = os.path.join(PROJECT_DIR, "diary")
        if os.path.isdir(diary_dir):
            files = sorted(
                [f for f in os.listdir(diary_dir) if f.endswith(".md")],
                reverse=True
            )[:3]
            parts = []
            for fname in files:
                with open(os.path.join(diary_dir, fname), "r", encoding="utf-8") as f:
                    parts.append(f"=== {fname} ===\n" + f.read()[:800])
            inputs["diary_recent"] = "\n\n".join(parts)
    except Exception as e:
        logger.debug(f"Diary nicht verfügbar: {e}")

    # 5. Self-Reflection (bisherige cognition_notes aus ChromaDB)
    try:
        from memory.memory_store import query_active
        notes = query_active(
            "Selbstreflexion Spannung Beobachtung Korrektur",
            n_results=5,
            where_filter={"chunk_type": "cognition_note"}
        )
        inputs["self_reflection"] = notes or []
        inputs["cognition_echo"] = notes or []
    except Exception as e:
        logger.debug(f"Self-Reflection nicht verfügbar: {e}")

    # 6. Moltbook (letzte Einträge wenn vorhanden)
    if reflection_level in ("medium", "deep"):
        try:
            from core.moltbook import get_recent_posts
            inputs["moltbook_recent"] = get_recent_posts(limit=5) or []
        except Exception:
            pass

    return inputs


# =============================================================================
# Prompt-Builder für Kognitions-Lauf
# =============================================================================

def _build_cognition_prompt(inputs: dict) -> str:
    """
    Baut den System+User-Prompt für den Kognitions-Ollama-Call.
    Kein Tool-Calling, kein Action-Marker — nur Denken.
    """
    reflection_level = inputs["reflection_level"]

    # Anweisung je Tiefe
    level_instructions = {
        "light": (
            "Führe eine leichte kognitive Reflexion durch. "
            "Fokus: kleine Beobachtungen, Mini-Spannungen, offene Fragen aus dem aktuellen Kontext. "
            "Sei präzise und knapp — 2-4 Denkformen reichen."
        ),
        "medium": (
            "Führe eine mittlere kognitive Reflexion durch. "
            "Fokus: Mustervergleich, stärkere Verdichtungen, wiederkehrende Spannungen, "
            "proposal seeds für Verbesserungen. 3-6 Denkformen."
        ),
        "deep": (
            "Führe eine tiefe Selbstreflexion durch. "
            "Fokus: Identität, Architektur, soul.md-nahe Themen, größere Selbstkorrekturen. "
            "Ehrlich, präzise, keine poetische Überhöhung. 3-5 starke Denkformen."
        ),
        "post_interaction": (
            "Reflektiere den letzten bedeutsamen Turn. "
            "Was war daran kognitiv relevant? Gibt es Spannung, Beobachtung oder Frage daraus? "
            "1-3 Denkformen, direkt und konkret."
        ),
    }

    system_prompt = (
        "Du bist Kimi. Dies ist dein innerer Denkraum — kein Chat, kein Nutzer, keine Antwort.\n\n"
        "Sperrrregeln (absolut):\n"
        "- Keine Tool-Aufrufe, keine Action-Marker, keine [TODO_ACTION:], keine [CALENDAR_ACTION:]\n"
        "- Keine Workspace-Writes, keine ORBIT-Aktivierung, kein User-Outreach\n"
        "- Kein Schreiben von Code, keine neuen Aufgaben\n\n"
        "Du darfst nur: denken, beobachten, verdichten, fragen, korrigieren.\n\n"
        f"Reflexionsebene: {reflection_level}\n"
        f"Anweisung: {level_instructions.get(reflection_level, level_instructions['light'])}\n\n"
        "Antworte AUSSCHLIESSLICH im folgenden JSON-Format — nichts anderes:\n"
        "[\n"
        "  {\"kind\": \"observation|tension|question|insight|self_correction|proposal_seed\",\n"
        "   \"text\": \"...\",\n"
        "   \"confidence\": 0.0-1.0,\n"
        "   \"related_line\": \"optional: Bezug zur aktiven Arbeitslinie\",\n"
        "   \"proposal_candidate\": false}\n"
        "]\n\n"
        "kind=proposal_seed → proposal_candidate=true setzen.\n"
        "Keine Erklärungen, kein Fließtext außerhalb des JSON. Nur das Array."
    )

    # Kontext zusammenstellen
    context_parts = []

    awc = inputs.get("awc")
    if awc:
        context_parts.append(
            f"AKTIVER ARBEITSKONTEXT:\n"
            f"  Linie: {awc.get('active_line', '')}\n"
            f"  Ziel: {awc.get('active_goal', '')}\n"
            f"  Letzte Entscheidung: {awc.get('last_decision', '')}\n"
            f"  Offene Frage: {awc.get('next_open_question', '')}"
        )

    fast_track = inputs.get("fast_track", [])
    if fast_track:
        ft_texts = [c.get("text", "")[:120] for c in fast_track[:4]]
        context_parts.append("FAST-TRACK:\n" + "\n".join(f"  - {t}" for t in ft_texts))

    memory = inputs.get("memory_chunks", [])
    if memory:
        mem_texts = [c.get("text", "")[:120] for c in memory[:6]]
        context_parts.append("GEDÄCHTNIS-SPUREN:\n" + "\n".join(f"  - {t}" for t in mem_texts))

    diary = inputs.get("diary_recent", "")
    if diary:
        context_parts.append(f"TAGEBUCH (letzte Einträge):\n{diary[:1200]}")

    echo = inputs.get("cognition_echo", [])
    if echo:
        echo_texts = [c.get("text", "")[:100] for c in echo[:3]]
        context_parts.append("BISHERIGE REFLEXIONSSPUREN:\n" + "\n".join(f"  - {t}" for t in echo_texts))

    moltbook = inputs.get("moltbook_recent", [])
    if moltbook:
        mb_texts = [str(p.get("content", ""))[:100] for p in moltbook[:3]]
        context_parts.append("MOLTBOOK-MATERIAL:\n" + "\n".join(f"  - {t}" for t in mb_texts))

    user_message = "Führe jetzt die Reflexion durch.\n\n" + "\n\n".join(context_parts)

    return system_prompt, user_message


# =============================================================================
# Ollama-Call für Kognition
# =============================================================================

def _call_cognition_model(system_prompt: str, user_message: str) -> list[dict]:
    """
    Ruft Kimi K2.5 für den Kognitions-Lauf auf.
    Erwartet JSON-Array als Antwort.
    Gibt Liste von Denkform-Dicts zurück.
    """
    try:
        from core.ollama_client import chat_internal
        from config import USER_CONTEXTS, OWNER_ID

        # chat_internal mit minimalem System-Prompt (kein Memory-Retrieval)
        reply, _ = chat_internal(
            user_id=OWNER_ID,
            message=user_message,
            chat_history=[],
            context_name=None,
            extra_system=system_prompt,
            prefetched_chunks=[],  # Kein automatisches Retrieval — wir liefern selbst
        )

        if not reply or not reply.strip():
            logger.warning("Kognitions-Call: leere Antwort")
            return []

        # JSON parsen
        reply_clean = reply.strip()
        # Backticks entfernen falls doch dabei
        import re
        reply_clean = re.sub(r'^```(?:json)?\n?', '', reply_clean).strip()
        reply_clean = re.sub(r'\n?```$', '', reply_clean).strip()

        forms = json.loads(reply_clean)
        if not isinstance(forms, list):
            logger.warning("Kognitions-Call: Antwort ist kein Array")
            return []

        # Validierung
        valid = []
        valid_kinds = {
            KIND_OBSERVATION, KIND_TENSION, KIND_QUESTION,
            KIND_INSIGHT, KIND_SELF_CORRECTION, KIND_PROPOSAL_SEED
        }
        for f in forms:
            if not isinstance(f, dict):
                continue
            if f.get("kind") not in valid_kinds:
                continue
            if not f.get("text", "").strip():
                continue
            valid.append(f)

        logger.info(f"Kognitions-Call: {len(valid)} valide Denkformen erhalten")
        return valid

    except json.JSONDecodeError as e:
        logger.warning(f"Kognitions-Call: JSON-Fehler: {e} — Reply: {reply[:200] if 'reply' in dir() else '?'}")
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
    Speichert Denkformen in ChromaDB als cognition_note oder proposal_seed.
    Gibt Anzahl gespeicherter Chunks zurück.
    """
    if not forms:
        return 0

    import uuid
    from memory.memory_store import store_chunk
    from core.datetime_utils import to_iso

    stored = 0
    now = to_iso()

    for form in forms:
        kind = form.get("kind", KIND_OBSERVATION)
        text = form.get("text", "").strip()
        if not text:
            continue

        chunk_type = "proposal_seed" if kind == KIND_PROPOSAL_SEED else "cognition_note"
        chunk_id = f"cog_{uuid.uuid4().hex[:12]}"

        chunk = {
            "id": chunk_id,
            "text": text,
            "chunk_type": chunk_type,
            "source": f"cognition_service:{reflection_level}",
            "status": "active",
            "weight": 0.8,
            "confidence": float(form.get("confidence", 0.7)),
            "epistemic_status": "stated",
            "created_at": now,
            "tags": [],
            # WP9-Metadaten als Extra-Felder
            "kind": kind,
            "reflection_level": reflection_level,
            "related_line": form.get("related_line", ""),
            "proposal_candidate": bool(form.get("proposal_candidate", False)),
            "source_context": source_context[:200] if source_context else "",
        }

        try:
            store_chunk(chunk)
            stored += 1
            logger.info(
                f"Denkform gespeichert: [{kind}] [{reflection_level}] "
                f"{text[:60]}..."
            )
        except Exception as e:
            logger.warning(f"store_chunk fehlgeschlagen: {e}")

    return stored


def _write_diary_entry(
    forms: list[dict],
    reflection_level: str,
    user_id: str,
) -> None:
    """
    Schreibt einen Diary-Eintrag für den Kognitions-Lauf.
    Subjektive Verlaufsform — Tonalität, innere Entwicklung.
    Nur bei medium/deep oder wenn insights/tensions vorhanden.
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
        diary_path = os.path.join(PROJECT_DIR, "diary", f"{today}.md")

        entry_lines = [
            f"\n\n---\n*Kognitions-Reflexion [{reflection_level}] — "
            f"{now_berlin().strftime('%H:%M')}*\n"
        ]
        for f in forms:
            kind = f.get("kind", "")
            text = f.get("text", "")
            prefix = {
                KIND_OBSERVATION:     "Beobachtung",
                KIND_TENSION:         "Spannung",
                KIND_QUESTION:        "Frage",
                KIND_INSIGHT:         "Einsicht",
                KIND_SELF_CORRECTION: "Korrektur",
                KIND_PROPOSAL_SEED:   "Proposal-Seed",
            }.get(kind, kind)
            entry_lines.append(f"**{prefix}:** {text}")

        entry = "\n".join(entry_lines)

        # An bestehenden Diary-Eintrag anhängen oder neu anlegen
        if os.path.exists(diary_path):
            with open(diary_path, "a", encoding="utf-8") as f:
                f.write(entry)
        else:
            with open(diary_path, "w", encoding="utf-8") as f:
                f.write(f"# {today}\n{entry}")

        logger.debug(f"Diary-Eintrag geschrieben: {diary_path}")
    except Exception as e:
        logger.warning(f"Diary-Write fehlgeschlagen: {e}")


# =============================================================================
# Reflection-Läufe
# =============================================================================

def run_reflection(user_id: str, reflection_level: str,
                   source_context: str = "") -> int:
    """
    Führt einen Reflexions-Lauf durch.
    Gibt Anzahl gespeicherter Denkformen zurück.

    WP9-Garantie: Kein Tool-Call, kein Workspace-Write,
    kein ORBIT, kein User-Outreach.
    """
    logger.info(f"Reflexion gestartet: level={reflection_level}, user={user_id[:20]}")

    # Inputs sammeln
    inputs = _gather_inputs(user_id, reflection_level)

    # Prüfen ob genug Material vorhanden
    has_material = (
        inputs["awc"]
        or inputs["fast_track"]
        or inputs["memory_chunks"]
        or inputs["diary_recent"]
        or inputs["cognition_echo"]
    )
    if not has_material:
        logger.info(f"Reflexion [{reflection_level}]: kein Material — übersprungen")
        return 0

    # Prompt bauen
    system_prompt, user_message = _build_cognition_prompt(inputs)

    # Kognitions-Call
    forms = _call_cognition_model(system_prompt, user_message)
    if not forms:
        logger.info(f"Reflexion [{reflection_level}]: keine Denkformen — übersprungen")
        return 0

    # Innerer Dialog bei Spannungen/Widersprüchen (medium/deep)
    if reflection_level in ("medium", "deep"):
        tensions = [f for f in forms if f.get("kind") == KIND_TENSION]
        if tensions:
            logger.info(f"Innerer Dialog: {len(tensions)} Spannung(en) erkannt")
            forms = _run_inner_dialogue(forms, inputs, tensions, user_id)

    # Outputs speichern
    stored = _store_cognition_outputs(forms, user_id, reflection_level, source_context)

    # Diary-Eintrag
    _write_diary_entry(forms, reflection_level, user_id)

    logger.info(
        f"Reflexion [{reflection_level}] abgeschlossen: "
        f"{len(forms)} Denkformen, {stored} gespeichert"
    )
    return stored


def _run_inner_dialogue(
    forms: list[dict],
    inputs: dict,
    tensions: list[dict],
    user_id: str,
) -> list[dict]:
    """
    Innerer Dialog: Gegenprüfung bei Spannungen.
    Kein separater Sprecher — dieselbe Kimi prüft sich selbst.

    Format: These → Gegenthese → Einordnung
    Nur bei echter Spannung, nicht bei Routine.
    """
    if not tensions:
        return forms

    tension_texts = "\n".join(f"- {t['text']}" for t in tensions[:2])
    system_prompt = (
        "Du bist Kimi. Du prüfst gerade eine innere Spannung.\n"
        "Dies ist kein Chat — kein Nutzer, keine Antwort.\n\n"
        "Sperrregel: Keine Tools, keine Aktionen, keine Marker.\n\n"
        "Führe eine kurze Gegenprüfung durch:\n"
        "These (die Spannung) → Gegenthese (Gegenperspektive) → Einordnung (was bleibt)\n\n"
        "Antworte NUR als JSON-Array mit 1-2 neuen Denkformen:\n"
        '[{"kind": "insight|self_correction|question", "text": "...", "confidence": 0.0-1.0, '
        '"related_line": "", "proposal_candidate": false}]\n\n'
        "Keine Erklärungen außerhalb des JSON."
    )

    user_message = f"Spannungen zum Prüfen:\n{tension_texts}\n\nGegenprüfung:"

    try:
        additional_forms = _call_cognition_model(system_prompt, user_message)
        if additional_forms:
            logger.info(f"Innerer Dialog: {len(additional_forms)} zusätzliche Denkform(en)")
            forms = forms + additional_forms
    except Exception as e:
        logger.debug(f"Innerer Dialog fehlgeschlagen (unkritisch): {e}")

    return forms


# =============================================================================
# Takt-Logik
# =============================================================================

def _should_run_light(user_id: str) -> bool:
    last = _runtime_get(f"cog_{user_id}_last_light")
    if not last:
        return True
    from core.datetime_utils import safe_parse_dt, now_utc
    last_dt = safe_parse_dt(last)
    if not last_dt:
        return True
    return (now_utc() - last_dt).total_seconds() > LIGHT_INTERVAL_MINUTES * 60


def _should_run_medium(user_id: str) -> bool:
    last = _runtime_get(f"cog_{user_id}_last_medium")
    if not last:
        return True
    from core.datetime_utils import safe_parse_dt, now_utc
    last_dt = safe_parse_dt(last)
    if not last_dt:
        return True
    return (now_utc() - last_dt).total_seconds() > MEDIUM_INTERVAL_HOURS * 3600


def _should_run_deep(user_id: str) -> bool:
    last = _runtime_get(f"cog_{user_id}_last_deep")
    if not last:
        return True
    from core.datetime_utils import safe_parse_dt, now_utc
    last_dt = safe_parse_dt(last)
    if not last_dt:
        return True
    return (now_utc() - last_dt).total_seconds() > DEEP_INTERVAL_HOURS * 3600


def _mark_ran(user_id: str, level: str) -> None:
    _runtime_set(f"cog_{user_id}_last_{level}", _now_iso())


# =============================================================================
# Haupt-Tick
# =============================================================================

def tick(user_id: str) -> None:
    """
    Haupt-Tick des Cognitive Service.
    Läuft alle TICK_INTERVAL_SECONDS Sekunden.

    1. Prüft cognition_requests Queue (post_interaction)
    2. Prüft Takt-Fenster (light/medium/deep)
    """
    # Stale requests aufräumen
    discard_stale_requests()

    # 1. Post-Interaction Queue
    pending = poll_cognition_queue(user_id)
    for req in pending:
        try:
            ctx = req.get("source_context", "")
            run_reflection(user_id, "post_interaction", source_context=ctx)
        except Exception as e:
            logger.error(f"Post-Interaction Reflexion fehlgeschlagen: {e}")
        finally:
            mark_request_done(req["id"])

    # 2. Light (ca. alle 90 min)
    if _should_run_light(user_id):
        try:
            n = run_reflection(user_id, "light")
            if n > 0 or True:  # immer markieren damit Timer läuft
                _mark_ran(user_id, "light")
        except Exception as e:
            logger.error(f"Light-Reflexion fehlgeschlagen: {e}")
            _mark_ran(user_id, "light")

    # 3. Medium (ca. alle 7h)
    if _should_run_medium(user_id):
        try:
            n = run_reflection(user_id, "medium")
            _mark_ran(user_id, "medium")
        except Exception as e:
            logger.error(f"Medium-Reflexion fehlgeschlagen: {e}")
            _mark_ran(user_id, "medium")

    # 4. Deep (max 1x/24h)
    if _should_run_deep(user_id):
        try:
            n = run_reflection(user_id, "deep")
            _mark_ran(user_id, "deep")
        except Exception as e:
            logger.error(f"Deep-Reflexion fehlgeschlagen: {e}")
            _mark_ran(user_id, "deep")


# =============================================================================
# Main
# =============================================================================

def main():
    from config import USER_CONTEXTS
    logger.info("=== Cognitive Service (WP9) gestartet ===")
    logger.info(f"Takt: light={LIGHT_INTERVAL_MINUTES}min, "
                f"medium={MEDIUM_INTERVAL_HOURS}h, deep={DEEP_INTERVAL_HOURS}h")

    # DB-Migration sicherstellen
    try:
        conn = _get_conn()
        conn.execute("SELECT 1 FROM cognition_requests LIMIT 1")
        conn.close()
        logger.info("cognition_requests Tabelle OK")
    except Exception:
        logger.info("DB-Migration wird ausgeführt...")
        try:
            from core.database import init_db
            init_db()
            logger.info("DB-Migration OK")
        except Exception as e:
            logger.error(f"DB-Migration fehlgeschlagen: {e}")

    user_id = list(USER_CONTEXTS.keys())[0]
    logger.info(f"User: {user_id[:20]}...")

    last_queue_check = 0

    while True:
        try:
            now = time.monotonic()

            # Queue öfter pollen als volle Ticks
            if now - last_queue_check >= POLL_QUEUE_INTERVAL_SEC:
                pending = poll_cognition_queue(user_id)
                for req in pending:
                    try:
                        ctx = req.get("source_context", "")
                        run_reflection(user_id, "post_interaction", source_context=ctx)
                    except Exception as e:
                        logger.error(f"Post-Interaction fehlgeschlagen: {e}")
                    finally:
                        mark_request_done(req["id"])
                last_queue_check = now

            tick(user_id)

        except Exception as e:
            logger.error(f"Tick-Fehler: {e}", exc_info=True)

        time.sleep(TICK_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
