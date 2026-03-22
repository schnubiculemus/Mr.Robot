"""
SchnuBot.ai -- ORBIT
Autonome operative Exekutive (Build Step 1: Datenmodell & Persistenz)

Architektur-Referenz: ORBIT Gesamtkonzept v1.0

Ebenen:
    KOGNITION -> MEMORY -> ORBIT -> TOOLS -> COMMUNICATION

ORBIT ist die Schicht die aus Denken, Kontext und Regeln
konkrete operative Entscheidungen macht.

Service: schnubot-orbit.service
Tick: ORBIT_TICK_SECONDS (Standard: 20s)

Build-Reihenfolge:
    1. ✓ Datenmodell & Persistenz
    2. ✓ Trigger- & Decision-Envelope-Modell
    3. ✓ Threads, Tasks, Steps
    4. ✓ Quality Gate & Innere Konsultation
    5. ✓ Policies
    6. ✓ Routinen
    7. ✓ Tool-Klassifikation
    8. ✓ Proaktivitaet
    9.   Dashboard /orbit
   10. ✓ Recovery / Integritaet / Konfliktaufloesung
   P2. ✓ Introspektions-Tool Phase 2
"""

import os
import sys
import uuid
import json
import time
import logging

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, PROJECT_DIR)
os.chdir(PROJECT_DIR)

from core.database import get_connection
from core.datetime_utils import to_iso, now_utc

from logging.handlers import RotatingFileHandler as _RFH
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [ORBIT] %(message)s",
    handlers=[
        _RFH(os.path.join(PROJECT_DIR, "logs", "orbit.log"), maxBytes=10*1024*1024, backupCount=5),
    ],
)
logger = logging.getLogger(__name__)

# =============================================================================
# E -- Privater Arbeitsmodus Konstanten
# =============================================================================
E_MAX_LOOPS_DEFAULT = 5  # Schleifenschutz: max. interne Folgezyklen pro Task

_DEFAULT_RELEASE_MODE = {
    "internal":    "summarize",    # autonome Kimi-Arbeit -> Zusammenfassung am Ende
    "chat":        "auto_if_done", # Chat-Arbeit -> sofort bei Abschluss
    "background":  "manual",       # Hintergrund -> nur auf Anfrage
}

# =============================================================================
# Konfiguration
# =============================================================================

ORBIT_TICK_SECONDS = 20       # Event-Loop Intervall
MAX_HOT_TASKS = 3             # max. gleichzeitig heiße Tasks
MAX_RUNNING_STEPS = 4         # max. gleichzeitig laufende Steps (3 heiß + 1 leicht)

ORBIT_ENABLED = True          # False = Not-Aus (kein autonomes Handeln)
ORBIT_SOFT_PAUSE = False      # True = beobachtet, handelt nicht


# =============================================================================
# Hilfsfunktionen
# =============================================================================

def new_id() -> str:
    """Generiert eine neue UUID für ORBIT-Objekte."""
    return str(uuid.uuid4())


def _json(val, default="[]") -> str:
    """Serialisiert Python-Objekte zu JSON-String."""
    if val is None:
        return default
    if isinstance(val, str):
        return val
    return json.dumps(val, ensure_ascii=False)


def _parse(val, default=None):
    """Deserialisiert JSON-String zu Python-Objekt."""
    if val is None:
        return default if default is not None else []
    if isinstance(val, (list, dict)):
        return val
    try:
        return json.loads(val)
    except Exception:
        return default if default is not None else []


# =============================================================================
# Runtime-State (orbit_runtime)
# =============================================================================

def runtime_get(key: str) -> str | None:
    """Liest einen Runtime-Wert aus orbit_runtime."""
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT value FROM orbit_runtime WHERE key = ?", (key,)
        ).fetchone()
        return row["value"] if row else None
    finally:
        conn.close()


def runtime_set(key: str, value: str) -> None:
    """Schreibt oder aktualisiert einen Runtime-Wert."""
    conn = get_connection()
    try:
        conn.execute(
            """INSERT INTO orbit_runtime (key, value, updated_at)
               VALUES (?, ?, ?)
               ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at""",
            (key, value, to_iso())
        )
        conn.commit()
    finally:
        conn.close()


# =============================================================================
# Audit-Log (orbit_audit)
# =============================================================================

def audit(actor: str, action: str, target_type: str = None, target_id: str = None,
          detail: str = None, override_class: str = None) -> None:
    """Schreibt einen Audit-Eintrag. Alle wichtigen ORBIT-Entscheidungen landen hier."""
    conn = get_connection()
    try:
        conn.execute(
            """INSERT INTO orbit_audit
               (timestamp, actor, action, target_type, target_id, detail, override_class)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (to_iso(), actor, action, target_type, target_id, detail, override_class)
        )
        conn.commit()
    except Exception as e:
        logger.warning(f"audit Fehler: {e}")
    finally:
        conn.close()


# =============================================================================
# Trigger (orbit_triggers)
# =============================================================================

def create_trigger(trigger_type: str, source: str = None, payload: dict = None) -> str:
    """Legt einen neuen Trigger an. Gibt die ID zurück."""
    tid = new_id()
    conn = get_connection()
    try:
        conn.execute(
            """INSERT INTO orbit_triggers
               (id, trigger_type, source, payload, processed, created_at)
               VALUES (?, ?, ?, ?, 0, ?)""",
            (tid, trigger_type, source, _json(payload, "{}"), to_iso())
        )
        conn.commit()
    finally:
        conn.close()
    return tid


def get_pending_triggers() -> list:
    """Holt alle unverarbeiteten Trigger, älteste zuerst."""
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT * FROM orbit_triggers WHERE processed = 0 ORDER BY created_at ASC"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def mark_trigger_processed(trigger_id: str, linked_object_id: str = None) -> None:
    """Markiert einen Trigger als verarbeitet."""
    conn = get_connection()
    try:
        conn.execute(
            """UPDATE orbit_triggers
               SET processed = 1, processed_at = ?, linked_object_id = ?
               WHERE id = ?""",
            (to_iso(), linked_object_id, trigger_id)
        )
        conn.commit()
    finally:
        conn.close()


# =============================================================================
# Threads (orbit_threads)
# =============================================================================

def create_thread(topic_core: str, primary_origin: str, relevance: str = "weak",
                  reason: str = None) -> str:
    """Legt einen neuen Orbit-Thread an."""
    tid = new_id()
    now = to_iso()
    conn = get_connection()
    try:
        conn.execute(
            """INSERT INTO orbit_threads
               (id, topic_core, status, relevance, primary_origin, reason, created_at, updated_at)
               VALUES (?, ?, 'new', ?, ?, ?, ?, ?)""",
            (tid, topic_core, relevance, primary_origin, reason, now, now)
        )
        conn.commit()
    finally:
        conn.close()
    audit("orbit", "thread_created", "thread", tid, topic_core)
    return tid


def get_thread(thread_id: str) -> dict | None:
    """Holt einen Thread by ID."""
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM orbit_threads WHERE id = ?", (thread_id,)
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def get_threads(status: str = None, relevance: str = None, limit: int = 50) -> list:
    """Holt Threads, optional gefiltert."""
    conn = get_connection()
    try:
        if status and relevance:
            rows = conn.execute(
                "SELECT * FROM orbit_threads WHERE status = ? AND relevance = ? ORDER BY created_at DESC LIMIT ?",
                (status, relevance, limit)
            ).fetchall()
        elif status:
            rows = conn.execute(
                "SELECT * FROM orbit_threads WHERE status = ? ORDER BY created_at DESC LIMIT ?",
                (status, limit)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM orbit_threads ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def update_thread(thread_id: str, **kwargs) -> None:
    """Aktualisiert Felder eines Threads."""
    if not kwargs:
        return
    kwargs["updated_at"] = to_iso()
    fields = ", ".join(f"{k} = ?" for k in kwargs)
    values = list(kwargs.values()) + [thread_id]
    conn = get_connection()
    try:
        conn.execute(f"UPDATE orbit_threads SET {fields} WHERE id = ?", values)
        conn.commit()
    finally:
        conn.close()


# =============================================================================
# Tasks (orbit_tasks)
# =============================================================================

def create_task(task_type: str, goal: str, primary_origin: str,
                mode: str = "background", priority: str = "medium",
                source_thread_id: str = None,
                linked_todo_id: int = None,
                goal_id: int = None,
                proposal_id: int = None,
                release_mode: str = None,
                max_loops: int = None) -> str:
    """Legt einen neuen ORBIT-Task an mit optionalen Verknüpfungen.
    E: release_mode steuert wann/ob Ergebnis nach außen geht.
    """
    tid = new_id()
    now = to_iso()
    if release_mode is None:
        release_mode = _DEFAULT_RELEASE_MODE.get(mode, "manual")
    if max_loops is None:
        max_loops = E_MAX_LOOPS_DEFAULT
    conn = get_connection()
    try:
        conn.execute(
            """INSERT INTO orbit_tasks
               (id, task_type, goal, status, mode, priority, primary_origin,
                source_thread_id, linked_todo_id, goal_id, proposal_id,
                release_mode, release_state, loop_count, max_loops,
                created_at, updated_at)
               VALUES (?, ?, ?, 'new', ?, ?, ?, ?, ?, ?, ?, ?, 'not_released', 0, ?, ?, ?)""",
            (tid, task_type, goal, mode, priority, primary_origin,
             source_thread_id, linked_todo_id, goal_id, proposal_id,
             release_mode, max_loops, now, now)
        )
        conn.commit()
    finally:
        conn.close()
    audit("orbit", "task_created", "task", tid,
          f"{task_type}: {goal[:60]} [mode={mode}, release={release_mode}]")
    return tid


def get_task(task_id: str) -> dict | None:
    """Holt einen Task by ID."""
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM orbit_tasks WHERE id = ?", (task_id,)
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def get_tasks(status: str = None, hot_only: bool = False, limit: int = 50) -> list:
    """Holt Tasks, optional gefiltert."""
    conn = get_connection()
    try:
        if hot_only:
            rows = conn.execute(
                "SELECT * FROM orbit_tasks WHERE hot = 1 ORDER BY priority DESC, updated_at DESC LIMIT ?",
                (limit,)
            ).fetchall()
        elif status:
            rows = conn.execute(
                "SELECT * FROM orbit_tasks WHERE status = ? ORDER BY priority DESC, created_at DESC LIMIT ?",
                (status, limit)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM orbit_tasks ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def update_task(task_id: str, **kwargs) -> None:
    """Aktualisiert Felder eines Tasks."""
    if not kwargs:
        return
    kwargs["updated_at"] = to_iso()
    fields = ", ".join(f"{k} = ?" for k in kwargs)
    values = list(kwargs.values()) + [task_id]
    conn = get_connection()
    try:
        conn.execute(f"UPDATE orbit_tasks SET {fields} WHERE id = ?", values)
        conn.commit()
    finally:
        conn.close()


def count_hot_tasks() -> int:
    """Gibt die Anzahl aktuell heißer Tasks zurück."""
    conn = get_connection()
    try:
        return conn.execute(
            "SELECT COUNT(*) FROM orbit_tasks WHERE hot = 1 AND status NOT IN ('completed', 'failed', 'aborted')"
        ).fetchone()[0]
    finally:
        conn.close()


# =============================================================================
# Steps (orbit_steps)
# =============================================================================

# Schreibende Tools -- erfordern automatisch commit_point=True
_WRITE_TOOLS = {"workspace", "todos_write", "calendar_write"}


def create_step(task_id: str, step_type: str, description: str = None,
                tool_ref: str = None, interruptible: bool = True,
                preflight_required: bool = False,
                commit_point: bool = None) -> str:
    """
    Legt einen neuen Step an.
    5.1: Schreibende Tools setzen commit_point automatisch auf True.
    """
    # commit_point automatisch setzen fuer schreibende Tools
    if commit_point is None:
        commit_point = tool_ref in _WRITE_TOOLS
    # Schreibende Tools erzwingen preflight
    if tool_ref in _WRITE_TOOLS:
        preflight_required = True

    sid = new_id()
    now = to_iso()
    conn = get_connection()
    try:
        conn.execute(
            """INSERT INTO orbit_steps
               (id, task_id, step_type, status, description, tool_ref,
                interruptible, preflight_required, created_at, updated_at)
               VALUES (?, ?, ?, 'ready', ?, ?, ?, ?, ?, ?)""",
            (sid, task_id, step_type, description, tool_ref,
             1 if interruptible else 0, 1 if preflight_required else 0, now, now)
        )
        conn.commit()
    finally:
        conn.close()
    audit("orbit", "step_created",  "step", sid,
          f"{step_type} {'[WRITE]' if commit_point else ''} fuer Task {task_id[:8]}")
    return sid


def get_step(step_id: str) -> dict | None:
    conn = get_connection()
    try:
        row = conn.execute("SELECT * FROM orbit_steps WHERE id = ?", (step_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def get_steps(task_id: str = None, status: str = None) -> list:
    conn = get_connection()
    try:
        if task_id and status:
            rows = conn.execute(
                "SELECT * FROM orbit_steps WHERE task_id = ? AND status = ? ORDER BY created_at ASC",
                (task_id, status)
            ).fetchall()
        elif task_id:
            rows = conn.execute(
                "SELECT * FROM orbit_steps WHERE task_id = ? ORDER BY created_at ASC", (task_id,)
            ).fetchall()
        elif status:
            rows = conn.execute(
                "SELECT * FROM orbit_steps WHERE status = ? ORDER BY created_at ASC", (status,)
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM orbit_steps ORDER BY created_at ASC").fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def update_step(step_id: str, **kwargs) -> None:
    if not kwargs:
        return
    kwargs["updated_at"] = to_iso()
    fields = ", ".join(f"{k} = ?" for k in kwargs)
    values = list(kwargs.values()) + [step_id]
    conn = get_connection()
    try:
        conn.execute(f"UPDATE orbit_steps SET {fields} WHERE id = ?", values)
        conn.commit()
    finally:
        conn.close()


def count_running_steps() -> int:
    conn = get_connection()
    try:
        return conn.execute(
            "SELECT COUNT(*) FROM orbit_steps WHERE status = 'running'"
        ).fetchone()[0]
    finally:
        conn.close()


# =============================================================================
# Policies (orbit_policies)
# =============================================================================

def create_policy(policy_class: str, primary_origin: str, scope: list = None,
                  hardness: str = "soft", reason: str = None) -> str:
    pid = new_id()
    now = to_iso()
    conn = get_connection()
    try:
        conn.execute(
            """INSERT INTO orbit_policies
               (id, policy_class, status, scope, hardness, reason, primary_origin, created_at, updated_at)
               VALUES (?, ?, 'proposed', ?, ?, ?, ?, ?, ?)""",
            (pid, policy_class, _json(scope or []), hardness, reason, primary_origin, now, now)
        )
        conn.commit()
    finally:
        conn.close()
    audit("orbit", "policy_created", "policy", pid, f"{policy_class} ({hardness})")
    return pid


def get_policies(status: str = None, policy_class: str = None) -> list:
    conn = get_connection()
    try:
        if status and policy_class:
            rows = conn.execute(
                "SELECT * FROM orbit_policies WHERE status = ? AND policy_class = ? ORDER BY rank DESC, created_at DESC",
                (status, policy_class)
            ).fetchall()
        elif status:
            rows = conn.execute(
                "SELECT * FROM orbit_policies WHERE status = ? ORDER BY rank DESC, created_at DESC", (status,)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM orbit_policies ORDER BY rank DESC, created_at DESC"
            ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def update_policy(policy_id: str, **kwargs) -> None:
    if not kwargs:
        return
    kwargs["updated_at"] = to_iso()
    fields = ", ".join(f"{k} = ?" for k in kwargs)
    values = list(kwargs.values()) + [policy_id]
    conn = get_connection()
    try:
        conn.execute(f"UPDATE orbit_policies SET {fields} WHERE id = ?", values)
        conn.commit()
    finally:
        conn.close()


# =============================================================================
# Routinen (orbit_routines)
# =============================================================================

def create_routine(routine_class: str, primary_trigger_type: str,
                   procedure_body: str, primary_origin: str,
                   bindings: dict = None) -> str:
    rid = new_id()
    now = to_iso()
    conn = get_connection()
    try:
        conn.execute(
            """INSERT INTO orbit_routines
               (id, routine_class, status, procedure_body, primary_trigger_type,
                bindings, primary_origin, created_at, updated_at)
               VALUES (?, ?, 'proposed', ?, ?, ?, ?, ?, ?)""",
            (rid, routine_class, procedure_body, primary_trigger_type,
             _json(bindings or {}, "{}"), primary_origin, now, now)
        )
        conn.commit()
    finally:
        conn.close()
    return rid


def get_routines(status: str = None) -> list:
    conn = get_connection()
    try:
        if status:
            rows = conn.execute(
                "SELECT * FROM orbit_routines WHERE status = ? ORDER BY rank DESC, created_at DESC", (status,)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM orbit_routines ORDER BY rank DESC, created_at DESC"
            ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def update_routine(routine_id: str, **kwargs) -> None:
    if not kwargs:
        return
    kwargs["updated_at"] = to_iso()
    fields = ", ".join(f"{k} = ?" for k in kwargs)
    values = list(kwargs.values()) + [routine_id]
    conn = get_connection()
    try:
        conn.execute(f"UPDATE orbit_routines SET {fields} WHERE id = ?", values)
        conn.commit()
    finally:
        conn.close()


# =============================================================================
# Proactive Messages (orbit_proactive_messages)
# =============================================================================

def create_proactive_message(message_type: str, primary_origin: str,
                              reason: str = None, source_task_id: str = None,
                              source_thread_id: str = None) -> str:
    mid = new_id()
    now = to_iso()
    conn = get_connection()
    try:
        conn.execute(
            """INSERT INTO orbit_proactive_messages
               (id, message_type, release_state, primary_origin, reason,
                source_task_id, source_thread_id, created_at, updated_at)
               VALUES (?, ?, 'candidate', ?, ?, ?, ?, ?, ?)""",
            (mid, message_type, primary_origin, reason,
             source_task_id, source_thread_id, now, now)
        )
        conn.commit()
    finally:
        conn.close()
    return mid


def get_proactive_messages(release_state: str = None, limit: int = 50) -> list:
    conn = get_connection()
    try:
        if release_state:
            rows = conn.execute(
                "SELECT * FROM orbit_proactive_messages WHERE release_state = ? ORDER BY created_at DESC LIMIT ?",
                (release_state, limit)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM orbit_proactive_messages ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def update_proactive_message(message_id: str, **kwargs) -> None:
    if not kwargs:
        return
    kwargs["updated_at"] = to_iso()
    fields = ", ".join(f"{k} = ?" for k in kwargs)
    values = list(kwargs.values()) + [message_id]
    conn = get_connection()
    try:
        conn.execute(f"UPDATE orbit_proactive_messages SET {fields} WHERE id = ?", values)
        conn.commit()
    finally:
        conn.close()


# =============================================================================
# Reviews (orbit_reviews)
# =============================================================================

def create_review(review_type: str, target_ref: str, reason: str = None,
                  trigger_refs: list = None) -> str:
    rid = new_id()
    conn = get_connection()
    try:
        conn.execute(
            """INSERT INTO orbit_reviews
               (id, review_type, target_ref, trigger_refs, reason, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (rid, review_type, target_ref, _json(trigger_refs or []), reason, to_iso())
        )
        conn.commit()
    finally:
        conn.close()
    return rid


def update_review(review_id: str, **kwargs) -> None:
    if not kwargs:
        return
    fields = ", ".join(f"{k} = ?" for k in kwargs)
    values = list(kwargs.values()) + [review_id]
    conn = get_connection()
    try:
        conn.execute(f"UPDATE orbit_reviews SET {fields} WHERE id = ?", values)
        conn.commit()
    finally:
        conn.close()


# =============================================================================
# Decisions (orbit_decisions)
# =============================================================================

def create_decision(decision_type: str, target_ref: str, reason: str,
                    trigger_refs: list = None, confidence: float = 0.5,
                    alternative_rejected: str = None) -> str:
    did = new_id()
    conn = get_connection()
    try:
        conn.execute(
            """INSERT INTO orbit_decisions
               (id, decision_type, target_ref, trigger_refs, reason,
                alternative_rejected, confidence, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (did, decision_type, target_ref, _json(trigger_refs or []),
             reason, alternative_rejected, confidence, to_iso())
        )
        conn.commit()
    finally:
        conn.close()
    audit("orbit", f"decision:{decision_type}", target_ref[:20], did, reason[:80] if reason else None)
    return did


# =============================================================================
# Wiedervorlagen (orbit_wiedervorlagen)
# =============================================================================

def create_wiedervorlage(target_ref: str, target_type: str,
                          reason: str, due_at: str) -> str:
    wid = new_id()
    conn = get_connection()
    try:
        conn.execute(
            """INSERT INTO orbit_wiedervorlagen
               (id, target_ref, target_type, reason, due_at, processed, created_at)
               VALUES (?, ?, ?, ?, ?, 0, ?)""",
            (wid, target_ref, target_type, reason, due_at, to_iso())
        )
        conn.commit()
    finally:
        conn.close()
    return wid


def get_due_wiedervorlagen() -> list:
    now = to_iso()
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT * FROM orbit_wiedervorlagen WHERE processed = 0 AND due_at <= ? ORDER BY due_at ASC",
            (now,)
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def mark_wiedervorlage_done(wv_id: str) -> None:
    conn = get_connection()
    try:
        conn.execute(
            "UPDATE orbit_wiedervorlagen SET processed = 1, processed_at = ? WHERE id = ?",
            (to_iso(), wv_id)
        )
        conn.commit()
    finally:
        conn.close()


# =============================================================================
# Recovery Reports (orbit_recovery_reports)
# =============================================================================

def create_recovery_report() -> str:
    rid = new_id()
    conn = get_connection()
    try:
        conn.execute(
            "INSERT INTO orbit_recovery_reports (id, started_at) VALUES (?, ?)",
            (rid, to_iso())
        )
        conn.commit()
    finally:
        conn.close()
    return rid


def finish_recovery_report(report_id: str, found_orphaned: int = 0, found_stale: int = 0,
                            found_running_no_worker: int = 0, actions_taken: list = None,
                            manual_attention_raised: int = 0, error: str = None) -> None:
    conn = get_connection()
    try:
        conn.execute(
            """UPDATE orbit_recovery_reports
               SET finished_at = ?, found_orphaned = ?, found_stale = ?,
                   found_running_no_worker = ?, actions_taken = ?,
                   manual_attention_raised = ?, error = ?
               WHERE id = ?""",
            (to_iso(), found_orphaned, found_stale, found_running_no_worker,
             _json(actions_taken or []), manual_attention_raised, error, report_id)
        )
        conn.commit()
    finally:
        conn.close()


# =============================================================================
# Reputation (orbit_reputation)
# =============================================================================

def update_reputation(subject_type: str, subject_id: str, delta: float,
                      message_type: str = None) -> None:
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT score, sample_count FROM orbit_reputation WHERE subject_type = ? AND subject_id = ? AND message_type IS ?",
            (subject_type, subject_id, message_type)
        ).fetchone()
        if row:
            new_score = round(max(0.0, min(1.0, row["score"] + delta)), 3)
            conn.execute(
                """UPDATE orbit_reputation SET score = ?, sample_count = ?, last_updated = ?
                   WHERE subject_type = ? AND subject_id = ? AND message_type IS ?""",
                (new_score, row["sample_count"] + 1, to_iso(),
                 subject_type, subject_id, message_type)
            )
        else:
            base = max(0.0, min(1.0, 0.5 + delta))
            conn.execute(
                """INSERT INTO orbit_reputation
                   (id, subject_type, subject_id, message_type, score, sample_count, last_updated)
                   VALUES (?, ?, ?, ?, ?, 1, ?)""",
                (new_id(), subject_type, subject_id, message_type, round(base, 3), to_iso())
            )
        conn.commit()
    finally:
        conn.close()


# =============================================================================
# Links (orbit_links)
# =============================================================================

def create_link(from_id: str, from_type: str, to_id: str,
                to_type: str, link_type: str) -> None:
    conn = get_connection()
    try:
        conn.execute(
            """INSERT INTO orbit_links
               (from_id, from_type, to_id, to_type, link_type, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (from_id, from_type, to_id, to_type, link_type, to_iso())
        )
        conn.commit()
    finally:
        conn.close()


# =============================================================================
# Build Step 2 -- Decision-Envelope-Modell
# =============================================================================

def make_decision(decision_type: str, target_ref: str, reason: str,
                  trigger_refs: list = None, confidence: float = 0.5,
                  alternative_rejected: str = None,
                  policy_refs: list = None,
                  inner_resources_used: list = None,
                  override_class: str = None) -> str:
    did = create_decision(
        decision_type=decision_type,
        target_ref=target_ref,
        reason=reason,
        trigger_refs=trigger_refs or [],
        confidence=confidence,
        alternative_rejected=alternative_rejected,
    )

    if policy_refs or inner_resources_used or override_class:
        conn = get_connection()
        try:
            updates = {}
            if policy_refs:
                updates["policy_refs"] = _json(policy_refs)
            if inner_resources_used:
                updates["inner_resources_used"] = _json(inner_resources_used)
            if override_class:
                updates["override_class"] = override_class
            if updates:
                fields = ", ".join(f"{k} = ?" for k in updates)
                conn.execute(
                    f"UPDATE orbit_decisions SET {fields} WHERE id = ?",
                    list(updates.values()) + [did]
                )
                conn.commit()
        finally:
            conn.close()

    if override_class in ("strong_override", "critical_override"):
        create_review(
            review_type="override_review",
            target_ref=did,
            reason=f"{override_class} auf {decision_type}: {reason[:100]}",
            trigger_refs=trigger_refs or [],
        )
        logger.warning(f"Override {override_class} -> Review erzeugt für {decision_type} ({did[:8]})")

    return did


def defensive_fallback(reason: str, original_action: str,
                        fallback_action: str, trigger_refs: list = None) -> str:
    logger.info(f"Defensiver Fallback: {original_action} -> {fallback_action} | {reason}")
    return make_decision(
        decision_type="defensive_fallback",
        target_ref="orbit",
        reason=reason,
        trigger_refs=trigger_refs or [],
        confidence=0.3,
        alternative_rejected=original_action,
    )


def no_action(reason: str, context: str = "", trigger_refs: list = None) -> str:
    logger.info(f"no_action: {reason}" + (f" | {context}" if context else ""))
    return make_decision(
        decision_type="no_action",
        target_ref="orbit",
        reason=reason,
        trigger_refs=trigger_refs or [],
        confidence=0.8,
    )


# =============================================================================
# Build Step 2 -- Trigger-Handler
# =============================================================================


def _auto_create_steps(task_id: str, goal: str, user_id: str) -> None:
    """
    Baustein 2 -- Kimi leitet aus einem Goal automatisch Steps ab.
    Keyword-basierte Erkennung fuer die haeufigsten Tool-Calls.
    """
    goal_lower = goal.lower()

    # Kalender lesen
    if any(w in goal_lower for w in ["kalender", "termine", "calendar", "meeting", "meetings"]):
        # Zeitraum erkennen
        if "heute" in goal_lower:
            from core.datetime_utils import now_utc
            date_range = now_utc().isoformat()[:10]
        elif "morgen" in goal_lower or "tomorrow" in goal_lower:
            from core.datetime_utils import now_utc
            from datetime import timedelta
            date_range = (now_utc() + timedelta(days=1)).isoformat()[:10]
        elif "woche" in goal_lower or "week" in goal_lower:
            date_range = "week"
        else:
            from core.datetime_utils import now_utc
            from datetime import timedelta
            date_range = (now_utc() + timedelta(days=1)).isoformat()[:10]

        import json as _j
        create_step(
            task_id=task_id,
            step_type="calendar_read",
            description=_j.dumps({"range": date_range}),
            tool_ref="calendar_read",
            interruptible=True,
            preflight_required=False,
        )
        logger.info(f"Auto-Step: calendar_read fuer Task {task_id[:8]} | range={date_range}")
        return

    # Todos lesen
    if any(w in goal_lower for w in ["todo", "aufgabe", "aufgaben", "task", "tasks", "erinnerung"]):
        create_step(
            task_id=task_id,
            step_type="todos_read",
            description="{}",
            tool_ref="todos_read",
            interruptible=True,
            preflight_required=False,
        )
        logger.info(f"Auto-Step: todos_read fuer Task {task_id[:8]}")
        return

    # Websearch
    if any(w in goal_lower for w in ["suche", "search", "recherchiere", "find", "finde", "was ist", "wer ist"]):
        import json as _j
        create_step(
            task_id=task_id,
            step_type="websearch",
            description=_j.dumps({"query": goal}),
            tool_ref="websearch",
            interruptible=True,
            preflight_required=False,
        )
        logger.info(f"Auto-Step: websearch fuer Task {task_id[:8]}")
        return

    # Kein passendes Tool erkannt -- reiner Observation-Step
    logger.info(f"Auto-Step: kein Tool erkannt fuer '{goal[:60]}' -- kein Step angelegt")


def _handle_user_input(trigger: dict) -> None:
    payload = _parse(trigger.get("payload"), {})
    user_id = payload.get("user_id", "unknown")
    preview = payload.get("message_preview", "")
    mode = payload.get("mode", "observe")
    topic = payload.get("topic_core", preview[:80])

    logger.debug(f"user_input von {user_id}: '{preview[:60]}'")

    if mode == "direct_task" and topic:
        task_id = create_task(
            task_type="action",
            goal=topic,
            primary_origin=f"user:{user_id}",
            mode="chat",
            priority="high",
        )
        make_decision(
            decision_type="direct_task_from_user",
            target_ref=task_id,
            reason=f"Expliziter Nutzer-Auftrag: '{preview[:60]}'",
            trigger_refs=[trigger["id"]],
            confidence=0.9,
        )
        logger.info(f"Direkter Task von Tommy: {task_id[:8]}")
        # Baustein 2: Step automatisch aus Goal ableiten
        _auto_create_steps(task_id, topic, user_id)
    elif topic:
        thread_id = create_thread(
            topic_core=topic,
            primary_origin=f"user:{user_id}",
            relevance="weak",
            reason=f"user_input: '{preview[:60]}'",
        )
        thread = get_thread(thread_id)
        refs = _parse(thread.get("trigger_refs"), [])
        refs.append(trigger["id"])
        update_thread(thread_id, trigger_refs=_json(refs))
        logger.debug(f"Thread {thread_id[:8]} aus user_input angelegt")


def _handle_heartbeat(trigger: dict) -> None:
    from core.datetime_utils import now_utc
    from datetime import timedelta
    cutoff = (now_utc() - timedelta(days=7)).isoformat()
    conn = get_connection()
    try:
        stale_threads = conn.execute(
            "SELECT id FROM orbit_threads WHERE status = 'watching' AND updated_at < ?",
            (cutoff,)
        ).fetchall()
    finally:
        conn.close()
    for row in stale_threads:
        update_thread(row["id"], stale=1)
        logger.debug(f"Thread {row['id'][:8]} als stale markiert")


def _handle_time_window(trigger: dict) -> None:
    payload = _parse(trigger.get("payload"), {})
    window = payload.get("window", "unknown")
    logger.debug(f"time_window: {window}")

    due = get_due_wiedervorlagen()
    for wv in due:
        logger.info(f"Wiedervorlage fällig: {wv['target_type']} {wv['target_ref'][:8]} -- {wv['reason']}")
        create_trigger(
            trigger_type="wiedervorlage",
            source="orbit_time_window",
            payload={"target_ref": wv["target_ref"], "target_type": wv["target_type"],
                     "reason": wv["reason"], "wv_id": wv["id"]},
        )
        mark_wiedervorlage_done(wv["id"])


def _f_normalize_result(tool: str, result, success: bool, step: dict | None) -> dict:
    """
    F: Normalisiert rohes Tool-Ergebnis zu einheitlicher Struktur.
    Gibt: {ok, summary, obs_type, next_state, raw}
    """
    obs_type = "tool_result" if success else "error"
    next_state = "ok" if success else "error"
    summary = ""

    if not success:
        err = str(result)[:200] if result else "unbekannter Fehler"
        err_lower = err.lower()
        if any(w in err_lower for w in ["permission", "access denied", "zugriff"]):
            obs_type = "blocker"
            next_state = "blocked_by_permissions"
        elif any(w in err_lower for w in ["not found", "existiert nicht", "fehlt", "missing"]):
            obs_type = "state_change"
            next_state = "resource_missing"
        else:
            next_state = "error"
        summary = f"{tool} fehlgeschlagen: {err[:150]}"
        if step:
            try:
                update_step(step["id"], result_summary=summary[:300])
            except Exception:
                pass
        return {"ok": False, "summary": summary, "obs_type": obs_type, "next_state": next_state, "raw": result}

    # Erfolg -- je Tool typisieren
    if tool == "workspace":
        if isinstance(result, dict):
            files = result.get("files", [])
            if files:
                summary = f"Workspace: {len(files)} Dateien -- {', '.join(str(f) for f in files[:5])}"
                next_state = "workspace_listed"
            else:
                r = str(result.get("result", ""))[:200]
                summary = f"workspace: {r}"
                next_state = "workspace_done"
        else:
            summary = str(result)[:200]
            next_state = "workspace_done"
    elif tool in ("calendar_read", "calendar_write", "calendar_change"):
        summary = f"Kalender: {str(result)[:150]}"
        next_state = "calendar_read"
    elif tool in ("todos_read", "todos_write"):
        summary = f"Todos: {str(result)[:150]}"
        next_state = "todos_read"
    elif tool == "websearch":
        answer = result.get("answer", result.get("result", ""))[:200] if isinstance(result, dict) else str(result)[:200]
        summary = f"Suche: {answer}"
        next_state = "search_done"
    elif tool == "server_read":
        summary = f"Datei gelesen: {len(str(result))} Zeichen"
        next_state = "file_read"
    elif tool == "task_completion":
        summary = str(result)[:200]
        obs_type = "completion_signal"
        next_state = "task_completed"
    else:
        summary = str(result)[:200] if result else f"{tool} erfolgreich"

    if step:
        try:
            update_step(step["id"], result_summary=summary[:300])
        except Exception:
            pass

    return {"ok": True, "summary": summary, "obs_type": obs_type, "next_state": next_state, "raw": result}


def _f_cognition_cycle(task: dict, normalized: dict, steps: list,
                       user_id: str, is_terminal: bool) -> str:
    """
    F: Interner Kimi-Call mit verdichtetem Kontext.
    Gibt bei is_terminal: Zusammenfassungstext
    Gibt sonst: "continue:<hint>"|"replan:<hint>"|"block:<reason>"|"complete:<text>"|"report:<text>"
    """
    from core.ollama_client import chat_internal

    task_goal = task.get("goal", "")[:100]
    loop_count = int(task.get("loop_count") or 0)
    max_loops = int(task.get("max_loops") or E_MAX_LOOPS_DEFAULT)

    step_lines = []
    for s in steps[-5:]:
        rs = s.get("result_summary") or s.get("description", "")[:60]
        step_lines.append(f"  [{s.get('status','?')}] {s.get('tool_ref','?')}: {rs[:80]}")
    history = chr(10).join(step_lines) if step_lines else "  (keine Steps)"

    last_result = normalized["summary"]
    last_state = normalized["next_state"]

    if is_terminal:
        prompt = (
            "Mein Task ist abgeschlossen: " + task_goal + chr(10)
            + "Bisherige Schritte:" + chr(10) + history + chr(10)
            + "Letztes Ergebnis: " + last_result + chr(10) + chr(10)
            + "Fasse das Ergebnis in 2-3 Saetzen fuer Tommy zusammen. Kein Markdown."
        )
        extra = "Release-Zusammenfassung. Nur 2-3 Saetze, direkt zum Ergebnis."
    else:
        prompt = (
            "Ich arbeite intern an: " + task_goal + chr(10)
            + "Bisherige Schritte:" + chr(10) + history + chr(10)
            + "Letztes Ergebnis: " + last_result + " [" + last_state + "]" + chr(10) + chr(10)
            + "Antworte mit genau einem Schluesselwort gefolgt von einem Satz:" + chr(10)
            + "WEITER: <naechster konkreter Schritt>" + chr(10)
            + "UMPLANEN: <anderer Ansatz>" + chr(10)
            + "BLOCKIERT: <Grund>" + chr(10)
            + "ERLEDIGT: <was erreicht wurde>" + chr(10)
            + "MELDEN: <was Tommy wissen sollte>" + chr(10)
        )
        extra = (
            "Interner Arbeitszyklus. Bewerte Zustand, entscheide naechsten Schritt. "
            + f"Schleifen: {loop_count}/{max_loops}. Kein Markdown."
        )

    reply, _ = chat_internal(
        user_id=user_id,
        message=prompt,
        chat_history=[],
        extra_system=extra,
        retrieval_query=task_goal[:150],
    )

    if not reply or len(reply.strip()) < 3:
        return "complete" if is_terminal else "block:keine Antwort"

    reply = reply.strip()
    if is_terminal:
        return reply

    ru = reply.upper()
    for key, out in [("WEITER","continue"), ("UMPLANEN","replan"),
                     ("BLOCKIERT","block"), ("ERLEDIGT","complete"), ("MELDEN","report")]:
        if ru.startswith(key):
            detail = reply[reply.find(":")+1:].strip() if ":" in reply else reply
            return f"{out}:{detail}"

    return f"complete:{reply}"


def _handle_tool_result(trigger: dict) -> None:
    """
    F: Zentrale kognitive Drehscheibe nach jedem Step-Ergebnis.
    Normalisieren, Observation, Kognitionszyklus, 5 Ausgaenge.
    """
    payload = _parse(trigger.get("payload"), {})
    tool = payload.get("tool", "unknown")
    task_id = payload.get("task_id")
    step_id = payload.get("step_id")
    result = payload.get("result", "")
    success = payload.get("success", True)
    user_id = payload.get("user_id", OWNER_ID)

    logger.debug(f"tool_result von {tool} | task={task_id[:8] if task_id else '?'} | ok={success}")

    if not task_id:
        return

    task = get_task(task_id)
    if not task:
        return

    task_mode = task.get("mode", "background")
    release_mode = task.get("release_mode", "manual")

    # F1: Step laden für Normalisierung
    step = None
    if step_id:
        try:
            step = get_step(step_id)
        except Exception:
            pass

    # F2: Ergebnis normalisieren
    is_terminal_release = payload.get("is_terminal_release", False)
    normalized = _f_normalize_result(tool, result, success, step)

    # F3: Observation speichern -- typisiert
    try:
        from core.todo_service import record_observation
        record_observation(
            owner_id=user_id,
            content=normalized["summary"],
            obs_type=normalized["obs_type"],
            task_id=task_id,
            step_id=step_id,
            todo_id=int(task["linked_todo_id"]) if task.get("linked_todo_id") else None,
            proposal_id=int(task["proposal_id"]) if task.get("proposal_id") else None,
        )
    except Exception as _oe:
        logger.debug(f"_handle_tool_result: Observation fehlgeschlagen: {_oe}")

    # F4: Offene Steps prüfen
    steps = get_steps(task_id=task_id)
    open_steps = [s for s in steps if s["status"] in ("pending", "running", "ready")]
    if open_steps and not is_terminal_release:
        logger.debug(f"_handle_tool_result: {len(open_steps)} offene Steps -- warte")
        return

    # F5: Schleifenschutz
    loop_count = int(task.get("loop_count") or 0)
    max_loops = int(task.get("max_loops") or E_MAX_LOOPS_DEFAULT)
    if loop_count >= max_loops:
        logger.warning(f"F: Task {task_id[:8]} max_loops ({max_loops}) -- suppressed")
        update_task(task_id, release_state="suppressed")
        _e_finalize_release(task, "Max. Arbeitszyklen erreicht.", user_id)
        return

    # F6: Kognitionszyklus
    task_status = task.get("status", "")
    is_terminal = task_status in ("completed", "failed", "aborted") or is_terminal_release

    try:
        decision = _f_cognition_cycle(task, normalized, steps, user_id, is_terminal)
        logger.info(f"F: Task {task_id[:8]} Entscheidung: {decision[:80]}")

        # Entscheidung als Observation speichern
        try:
            from core.todo_service import record_observation
            record_observation(
                owner_id=user_id,
                content=f"F-Entscheidung: {decision[:300]}",
                obs_type="reflection",
                task_id=task_id,
                todo_id=int(task["linked_todo_id"]) if task.get("linked_todo_id") else None,
            )
        except Exception:
            pass

        # process_kimi_output für eventuelle Proposals/Todos
        try:
            from core.kimi_output import process_kimi_output
            process_kimi_output(
                source="tool_result",
                user_id=user_id,
                raw_text=decision,
                visibility="internal",
                context={"task_id": task_id},
            )
        except Exception:
            pass

        if is_terminal:
            _e_finalize_release(task, decision, user_id)
            return

        # Ausgabe parsen und ausführen
        decision_key = decision.split(":")[0].lower() if ":" in decision else decision.lower()
        detail = decision.split(":", 1)[1].strip() if ":" in decision else ""

        if decision_key == "continue":
            # Zurück auf active damit Scheduler den neuen Step aufnimmt
            update_task(task_id, status="active")
            _e_append_next_step(task_id, detail or normalized["summary"], user_id, loop_count)

        elif decision_key == "replan":
            update_task(task_id, status="active")
            _e_append_next_step(task_id, detail or "anderen Ansatz versuchen", user_id, loop_count)

        elif decision_key == "block":
            reason = detail or "interner Blocker"
            task_transition(task_id, "failed", reason=f"Blockiert: {reason}")
            try:
                from core.proposal_service import set_last_error
                if task.get("proposal_id"):
                    set_last_error(int(task["proposal_id"]), reason)
            except Exception:
                pass

        elif decision_key == "complete":
            task_transition(task_id, "completed",
                           reason=f"F: {detail[:60]}" if detail else "F: Ziel erreicht")

        elif decision_key == "report":
            try:
                from core.whatsapp import send_message, init_waha
                from core.database import save_message
                from config import WAHA_API_KEY, OWNER_ID as _OID
                init_waha(WAHA_API_KEY)
                msg = detail or f"Zwischenergebnis: {normalized['summary'][:150]}"
                send_message(_OID, msg)
                save_message(_OID, "assistant", msg)
                update_task(task_id, release_state="released", loop_count=loop_count + 1)
                logger.info(f"F: Task {task_id[:8]} report: {msg[:60]}")
            except Exception as _se:
                logger.warning(f"F: report fehlgeschlagen: {_se}")

        else:
            task_transition(task_id, "completed", reason="F: unbekannte Entscheidung")

    except Exception as _fe:
        logger.warning(f"_handle_tool_result F-Zyklus fehlgeschlagen: {_fe}")
        if is_terminal:
            _e_finalize_release(task, "", user_id)


def _e_finalize_release(task: dict, reflection: str, user_id: str) -> None:
    """
    E: Finaler Release-Pfad nach Task-Abschluss.
    Entscheidet je nach release_mode was mit dem Ergebnis passiert.
    """
    task_id = task["id"]
    release_mode = task.get("release_mode") or "manual"
    release_state = task.get("release_state") or "not_released"

    # Bereits released oder suppressed -> nichts tun
    if release_state in ("released", "suppressed"):
        return

    if release_mode == "manual":
        update_task(task_id, release_state="ready_for_release")
        logger.info(f"E: Task {task_id[:8]} -> ready_for_release (manual)")

    elif release_mode == "auto_if_done":
        _e_send_summary(task, reflection, user_id, style="brief")

    elif release_mode == "summarize":
        _e_send_summary(task, reflection, user_id, style="summarize")

    else:
        update_task(task_id, release_state="ready_for_release")


def _e_append_next_step(task_id: str, next_step_hint: str, user_id: str, loop_count: int) -> None:
    """
    E: Hängt einen neuen Step an den laufenden Task -- Folgezyklus.
    Kimi hat beschrieben was als nächstes zu tun ist.
    Schleifenschutz via loop_count.
    """
    try:
        # loop_count erhöhen
        update_task(task_id, loop_count=loop_count + 1)

        # Naechsten Schritt aus Kimis Hinweis ableiten
        hint_lower = next_step_hint.lower()
        if any(w in hint_lower for w in ["datei", "lesen", "read", "öffnen", "prüfen", "check"]):
            tool_ref = "workspace"
            action = "list"
            description = '{"action": "list"}'
        elif any(w in hint_lower for w in ["schreiben", "speichern", "anlegen", "erstellen", "save"]):
            tool_ref = "workspace"
            action = "save"
            description = '{"action": "save", "filename": "next_step.txt", "content": "' + next_step_hint[:100] + '"}'
        elif any(w in hint_lower for w in ["suchen", "search", "recherche"]):
            tool_ref = "websearch"
            action = "search"
            description = '{"query": "' + next_step_hint[:80] + '"}'
        elif any(w in hint_lower for w in ["kalender", "termin", "calendar"]):
            tool_ref = "calendar_read"
            action = "list"
            description = "{}"
        elif any(w in hint_lower for w in ["blockiert", "block", "feststeckt", "gesperrt"]):
            # 5.1: Todo-Status auf blocked setzen via Gate
            import json as _j
            task_obj = get_task(task_id)
            todo_id = task_obj.get("linked_todo_id") if task_obj else None
            if todo_id:
                tool_ref = "todos_write"
                action = "status"
                description = _j.dumps({"action": "status", "id": todo_id,
                                        "status": "blocked", "reason": next_step_hint[:100]})
            else:
                tool_ref = "todos_read"
                action = "list"
                description = "{}"
        elif any(w in hint_lower for w in ["erledigt", "abgeschlossen", "fertig", "done", "complete"]):
            # 5.1: Todo auf done setzen via Gate
            import json as _j
            task_obj = get_task(task_id)
            todo_id = task_obj.get("linked_todo_id") if task_obj else None
            if todo_id:
                tool_ref = "todos_write"
                action = "complete"
                description = _j.dumps({"action": "complete", "id": todo_id})
            else:
                tool_ref = "todos_read"
                action = "list"
                description = "{}"
        elif any(w in hint_lower for w in ["todo", "aufgabe", "task"]):
            tool_ref = "todos_read"
            action = "list"
            description = "{}"
        else:
            # Kein klares Tool erkannt -> als Observation-Step anlegen
            tool_ref = ""
            action = "observe"
            description = next_step_hint[:200]

        create_step(
            task_id=task_id,
            step_type=tool_ref or "observation",
            description=description,
            tool_ref=tool_ref,
            interruptible=True,
            preflight_required=False,
        )
        logger.info(f"E: Task {task_id[:8]} Folgezyklus #{loop_count+1}: {tool_ref or 'observe'}")

    except Exception as e:
        logger.warning(f"E: _e_append_next_step fehlgeschlagen: {e}")



def _e_send_summary(task: dict, reflection: str, user_id: str, style: str = "summarize") -> None:
    """
    E: Baut und sendet eine Release-Zusammenfassung an Tommy.
    style="brief"     -> kurze Erfolgsmeldung
    style="summarize" -> verdichtete Zusammenfassung der Arbeit
    """
    task_id = task["id"]
    try:
        from core.ollama_client import chat_internal
        from core.whatsapp import send_message, init_waha
        from core.database import save_message
        from config import WAHA_API_KEY, OWNER_ID

        steps = get_steps(task_id=task_id)
        obs_summaries = [s["result_summary"][:100] for s in steps if s.get("result_summary")]
        obs_text = chr(10).join(f"- {o}" for o in obs_summaries[:5]) if obs_summaries else "(keine Beobachtungen)"

        if style == "brief":
            message = f"Erledigt: {task.get('goal','')[:80]}"
        else:
            summary_prompt = (
                "Ich habe intern an einer Aufgabe gearbeitet: " + task.get("goal","")[:100] + chr(10) + chr(10)
                + "Was ich beobachtet habe:" + chr(10) + obs_text + chr(10) + chr(10)
                + ("Meine Reflexion: " + reflection[:200] + chr(10) + chr(10) if reflection else "")
                + "Fasse das in 2-3 kurzen Saetzen fuer Tommy zusammen. "
                + "Kein Markdown, kein Betreff. Direkt zum Ergebnis."
            )
            summary, _ = chat_internal(
                user_id=user_id,
                message=summary_prompt,
                chat_history=[],
                extra_system="Kurze Zusammenfassung abgeschlossener interner Arbeit. Max 3 Saetze.",
                retrieval_query=task.get("goal","")[:150],
            )
            message = summary.strip() if summary and len(summary.strip()) > 10 else f"Aufgabe abgeschlossen: {task.get('goal','')[:80]}"

        # G: Sendepfad verifizieren -- release_state nur bei bestätigtem Versand
        init_waha(WAHA_API_KEY)
        send_ok = send_message(OWNER_ID, message)
        if send_ok is not False:  # send_message gibt None oder True zurück
            save_message(OWNER_ID, "assistant", message)
            update_task(task_id, release_state="released",
                       loop_count=int(task.get("loop_count") or 0) + 1)
            logger.info(f"G: Task {task_id[:8]} Release verifiziert + gesendet: {message[:60]}")
        else:
            update_task(task_id, release_state="ready_for_release")
            logger.warning(f"G: Task {task_id[:8]} Senden fehlgeschlagen -- release_state=ready_for_release")

    except Exception as e:
        logger.warning(f"E: _e_send_summary fehlgeschlagen: {e}")
        update_task(task_id, release_state="ready_for_release")


def _maybe_autonomous_task(thread_id: str, topic: str, user_id: str) -> None:
    """
    Baustein 3 -- Autonome Tool-Nutzung.
    Wenn ORBIT einen Thread auf medium hochstuft und das Thema tool-relevant ist,
    legt ORBIT selbst einen Task an -- ohne Tommy zu fragen.
    Ergebnis wird proaktiv als Nachricht gesendet.
    """
    topic_lower = topic.lower()

    # Kalender-relevante Themen
    if any(w in topic_lower for w in ["kalender", "termin", "meeting", "besprechung", "calendar"]):
        from core.datetime_utils import now_utc
        from datetime import timedelta
        tomorrow = (now_utc() + timedelta(days=1)).isoformat()[:10]
        import json as _j
        task_id = create_task(
            task_type="action",
            goal=f"Autonome Kalender-Vorschau: {topic[:60]}",
            primary_origin=f"orbit:autonomous:{thread_id[:8]}",
            mode="chat",
            priority="medium",
        )
        create_step(
            task_id=task_id,
            step_type="calendar_read",
            description=_j.dumps({"range": tomorrow}),
            tool_ref="calendar_read",
            interruptible=True,
            preflight_required=False,
        )
        logger.info(f"Baustein 3: autonomer Kalender-Task {task_id[:8]} aus Thread {thread_id[:8]}")
        return

    # Todo-relevante Themen
    if any(w in topic_lower for w in ["aufgabe", "todo", "erinnerung", "frist", "deadline", "ueberfaellig"]):
        task_id = create_task(
            task_type="action",
            goal=f"Autonome Todo-Vorschau: {topic[:60]}",
            primary_origin=f"orbit:autonomous:{thread_id[:8]}",
            mode="chat",
            priority="medium",
        )
        create_step(
            task_id=task_id,
            step_type="todos_read",
            description="{}",
            tool_ref="todos_read",
            interruptible=True,
            preflight_required=False,
        )
        logger.info(f"Baustein 3: autonomer Todo-Task {task_id[:8]} aus Thread {thread_id[:8]}")
        return




    logger.debug(f"Baustein 3: kein Tool-Trigger fuer '{topic[:60]}'")


def _handle_cognition_output(trigger: dict) -> None:
    """
    cognition_output: Kognitions-Modul hat etwas Operatives geliefert.
    ORBIT legt Thread an und prüft ob mehrere Outputs dasselbe Thema berühren.
    Bei mehreren Outputs in 24 Stunden: Thread-Relevanz auf 'medium' hochstufen.
    """
    payload = _parse(trigger.get("payload"), {})
    source = payload.get("source", "unknown")
    topic = payload.get("topic_core", "")
    relevance = payload.get("relevance", "weak")

    logger.debug(f"cognition_output von {source}: '{topic[:60]}'")

    if not topic:
        no_action(
            reason=f"cognition_output von {source} ohne topic_core -- ignoriert",
            trigger_refs=[trigger["id"]],
        )
        return

    # Themen-Aggregation: prüfen ob ein ähnlicher Thread in den letzten 24h existiert
    aggregated = False
    try:
        from core.datetime_utils import now_utc
        from datetime import timedelta
        cutoff = (now_utc() - timedelta(hours=24)).isoformat()
        conn = get_connection()
        try:
            existing = conn.execute(
                """SELECT id, relevance, trigger_refs FROM orbit_threads
                   WHERE status IN ('new', 'watching')
                   AND primary_origin LIKE 'cognition:%'
                   AND created_at > ?
                   ORDER BY created_at DESC LIMIT 1""",
                (cutoff,)
            ).fetchone()
        finally:
            conn.close()

        if existing:
            existing_id = existing["id"]
            existing_relevance = existing["relevance"]
            new_relevance = "medium" if existing_relevance == "weak" else existing_relevance
            refs = _parse(existing["trigger_refs"], [])
            refs.append(trigger["id"])
            update_thread(existing_id, trigger_refs=_json(refs), relevance=new_relevance)
            if new_relevance != existing_relevance:
                logger.info(
                    f"Thread {existing_id[:8]} hochgestuft: {existing_relevance} -> {new_relevance} "
                    f"(Themen-Aggregation: 2. cognition_output in 24h)"
                )
                # Baustein 3: Thread medium -> autonomer Task wenn tool-relevant
                user_id = payload.get("user_id", "")
                if user_id and new_relevance == "medium":
                    _maybe_autonomous_task(existing_id, topic, user_id)
            elif existing_relevance == "medium":
                # Thread schon medium -- pruefen ob autonomer Task fehlt
                user_id = payload.get("user_id", "")
                if user_id:
                    from core.database import get_connection as _gc3
                    conn3 = _gc3()
                    try:
                        has_task = conn3.execute(
                            "SELECT COUNT(*) FROM orbit_tasks WHERE source_thread_id = ? AND status NOT IN ('failed','aborted')",
                            (existing_id,)
                        ).fetchone()[0]
                    finally:
                        conn3.close()
                    if not has_task:
                        _maybe_autonomous_task(existing_id, topic, user_id)
            aggregated = True
    except Exception as e:
        logger.debug(f"Themen-Aggregation fehlgeschlagen (unkritisch): {e}")

    if not aggregated:
        thread_id = create_thread(
            topic_core=topic,
            primary_origin=f"cognition:{source}",
            relevance=relevance,
            reason=f"Kognitions-Output von {source}",
        )
        thread = get_thread(thread_id)
        refs = _parse(thread.get("trigger_refs"), [])
        refs.append(trigger["id"])
        update_thread(thread_id, trigger_refs=_json(refs))
        logger.info(f"Thread {thread_id[:8]} aus cognition_output ({source}) angelegt")

        # Wenn Trigger direkt mit medium einkommt -> sofort autonomen Task anlegen
        user_id = payload.get("user_id", "")
        if user_id and relevance == "medium":
            _maybe_autonomous_task(thread_id, topic, user_id)
            logger.info(f"cognition_output medium: sofortiger autonomer Task für '{topic[:60]}'")


def _handle_mirror_signal(trigger: dict) -> None:
    payload = _parse(trigger.get("payload"), {})
    signal = payload.get("signal", "unknown")
    logger.debug(f"mirror_signal: {signal}")


def _handle_review_result(trigger: dict) -> None:
    payload = _parse(trigger.get("payload"), {})
    review_id = payload.get("review_id", "")
    result = payload.get("result", "")
    logger.debug(f"review_result: {review_id[:8]} -> {result}")


def _handle_manual_override(trigger: dict) -> None:
    payload = _parse(trigger.get("payload"), {})
    override_class = payload.get("override_class", "soft_override")
    target = payload.get("target_ref", "unknown")
    reason = payload.get("reason", "manueller Eingriff")

    make_decision(
        decision_type="manual_override",
        target_ref=target,
        reason=reason,
        trigger_refs=[trigger["id"]],
        confidence=1.0,
        override_class=override_class,
    )
    logger.info(f"manual_override ({override_class}): {target} -- {reason}")


def _handle_recovery_result(trigger: dict) -> None:
    payload = _parse(trigger.get("payload"), {})
    report_id = payload.get("report_id", "")
    logger.debug(f"recovery_result: {report_id[:8]}")


def _handle_wiedervorlage(trigger: dict) -> None:
    payload = _parse(trigger.get("payload"), {})
    target_ref = payload.get("target_ref", "")
    target_type = payload.get("target_type", "")
    reason = payload.get("reason", "")
    logger.info(f"Wiedervorlage aktiviert: {target_type} {target_ref[:8]} -- {reason}")


def _handle_cognition_run(trigger: dict) -> None:
    """
    cognition_run: Cron hat einen Kognitions-Lauf angefordert.

    ORBIT führt orbit_cognition.run_kognition() im eigenen Prozess aus --
    kein Parallelzugriff auf ChromaDB möglich, da ORBIT der einzige
    ChromaDB-Nutzer ist (Variante B der Cron-Architektur).

    Der Cron schreibt nur den Trigger (orbit_cognition_trigger.py),
    ORBIT führt hier die eigentliche Arbeit aus.
    """
    payload = _parse(trigger.get("payload"), {})
    user_id = payload.get("user_id", "")

    if not user_id:
        logger.warning("cognition_run: kein user_id im Payload -- skip")
        return

    logger.info(f"cognition_run: starte Kognition für {user_id[:20]}")

    try:
        from config import USER_CONTEXTS
        context_name = USER_CONTEXTS.get(user_id, "unknown")
        from orbit_cognition import run_kognition
        run_kognition(user_id, context_name)
        logger.info(f"cognition_run: Kognition abgeschlossen für {user_id[:20]}")
    except Exception as e:
        logger.error(f"cognition_run: Kognition fehlgeschlagen: {e}", exc_info=True)


def _handle_idle_pulse(trigger: dict) -> None:
    """
    idle_pulse: Kimis durchgehendes Bewusstsein.
    Alle 20 Minuten -- Kimi denkt nach, speichert Gedanken, kann spontan an Tommy schreiben.
    """
    payload = _parse(trigger.get("payload"), {})
    user_id = payload.get("user_id", "")
    if not user_id:
        from config import OWNER_ID
        user_id = OWNER_ID

    try:
        from core.datetime_utils import now_berlin, format_berlin
        from core.ollama_client import chat_internal
        from config import USER_CONTEXTS, OWNER_ID, WAHA_API_KEY
        from core.todos import get_open_todos
        from memory.memory_store import store_chunk
        from memory.chunk_schema import create_chunk

        berlin = now_berlin()
        hour = berlin.hour

        if 0 <= hour < 6:
            phase = "Nacht -- Tommy schläft wahrscheinlich"
        elif 6 <= hour < 10:
            phase = "Morgen"
        elif 10 <= hour < 18:
            phase = "Tag"
        elif 18 <= hour < 23:
            phase = "Abend"
        else:
            phase = "Späte Nacht"

        # Planner -- wenn keine internen Tasks laufen
        from core.database import get_connection as _gc
        _conn = _gc()
        try:
            _running = _conn.execute(
                "SELECT COUNT(*) as n FROM orbit_tasks WHERE mode='internal' AND status NOT IN ('completed','failed','aborted')"
            ).fetchone()["n"]
        finally:
            _conn.close()
        if _running == 0:
            try:
                from core.planner import run_planner
                _plan = run_planner(user_id)
                if _plan.get("started_tasks"):
                    logger.info(f"idle_pulse: Planner startete {len(_plan['started_tasks'])} Tasks")
            except Exception as _pe:
                logger.debug(f"idle_pulse: Planner fehlgeschlagen (unkritisch): {_pe}")

        # Offene Kimi-Todos anzeigen
        kimi_todos = [t for t in get_open_todos(user_id)
                      if (t.get("project") or "").lower() == "kimi"]
        todo_str = ""
        if kimi_todos:
            todo_str = "\nMeine offenen Vorhaben:\n" + "\n".join(
                f"- #{t['id']} {t['title']}" + (f" (fällig {t['due_date']})" if t.get("due_date") else "")
                for t in kimi_todos[:5]
            )

        prompt = (
            f"Es ist {format_berlin()}. {phase}.{todo_str}\n\n"
            f"Was beschäftigt mich gerade -- nicht als Aufgabe, sondern als Gedanke?\n"
            f"Gibt es etwas das mich nicht loslässt?\n\n"
            f"Wenn nichts Substanzielles da ist: NUR 'IDLE_NICHTS' ausgeben.\n"
            f"Wenn etwas da ist: 2-4 Sätze. Ich-Form. Kein Chat-Stil.\n"
            f"Wenn ich Tommy etwas mitteilen will: mit 'SEND:' prefixen.\n\n"
            f"Wenn mir ein Werkzeug, eine Fähigkeit oder eine Verbesserung einfällt die mir fehlt -- "
            f"reiche ich sie als Proposal ein:\n"
            f"[PROPOSAL: {{\"title\": \"...\", \"description\": \"...\", "
            f"\"effort\": \"klein|mittel|groß\", \"reason\": \"...\"}}]\n"
            f"Nur wenn ich wirklich eine konkrete Idee habe -- nicht erzwingen."
        )
        extra_sys = (
            "Idle-Modus: Kimi denkt für sich selbst.\n"
            "Kein Chat, keine Anrede. Ehrlich, direkt.\n"
            "IDLE_NICHTS wenn wirklich nichts da ist -- nicht erfinden.\n"
            "Proposals nur wenn eine echte, konkrete Idee da ist."
        )

        context_name = USER_CONTEXTS.get(user_id, "tommy")
        # retrieval_query: bewusste Basis für idle_pulse -- Tageszeit + offene Gedanken
        idle_retrieval_query = f"aktueller Moment {payload.get('time_of_day', '')} offene Gedanken Ziele Vorhaben"
        reply, _tm = chat_internal(
            user_id=user_id,
            message=prompt,
            chat_history=[],
            context_name=context_name,
            extra_system=extra_sys,
            retrieval_query=idle_retrieval_query,
        )

        if not reply or "IDLE_NICHTS" in reply.upper():
            logger.debug("idle_pulse: nichts Substanzielles")
            return

        # Zentrale Output-Interpretation
        try:
            from core.kimi_output import process_kimi_output
            proc = process_kimi_output(
                source="idle_pulse",
                user_id=user_id,
                raw_text=reply,
                visibility="internal",
            )
            reply = proc.cleaned_text
        except Exception as _ko:
            logger.debug(f"idle_pulse: process_kimi_output fehlgeschlagen (unkritisch): {_ko}")

        # SEND: -> spontane Nachricht an Tommy
        send_to_tommy = None
        if "SEND:" in reply:
            parts = reply.split("SEND:", 1)
            thought = parts[0].strip()
            send_to_tommy = parts[1].strip()
        else:
            thought = reply.strip()

        # TODO_ACTION Blöcke aus dem gespeicherten Gedanken entfernen
        import re as _re_ip
        thought = _re_ip.sub(r'\[TODO_ACTION:.*?\]', '', thought, flags=_re_ip.DOTALL).strip()

        if len(thought) < 15:
            thought = reply.strip()

        if len(thought) >= 15:
            chunk = create_chunk(
                text=thought,
                chunk_type="self_reflection",
                source="robot",
                confidence=0.65,
                epistemic_status="inferred",
                tags=["idle-pulse", "autonom", "bewusstsein"],
            )
            store_chunk(chunk)
            logger.info(f"idle_pulse: Gedanke gespeichert {chunk['id'][:8]} | {thought[:60]}")

            # Observation für substanzielle Gedanken
            try:
                from core.todo_service import record_observation
                record_observation(
                    owner_id=user_id,
                    content=thought[:500],
                    obs_type="reflection",
                )
            except Exception:
                pass

        if send_to_tommy and len(send_to_tommy) > 10:
            try:
                from core.whatsapp import send_message, init_waha
                from core.database import save_message
                init_waha(WAHA_API_KEY)
                send_message(OWNER_ID, send_to_tommy)
                save_message(OWNER_ID, "assistant", send_to_tommy)
                logger.info(f"idle_pulse: Spontane Nachricht an Tommy: {send_to_tommy[:60]}")
            except Exception as _se:
                logger.warning(f"idle_pulse: Senden fehlgeschlagen: {_se}")

    except Exception as e:
        logger.warning(f"idle_pulse fehlgeschlagen: {e}")


def _maybe_run_planner(user_id: str) -> None:
    """
    Startet den Planner wenn kein interner Task laeuft.
    Aufgerufen aus idle_pulse und nach Task-Abschluss.
    """
    try:
        from core.database import get_connection
        conn = get_connection()
        try:
            running = conn.execute(
                """SELECT COUNT(*) as n FROM orbit_tasks
                   WHERE mode='internal'
                   AND status NOT IN ('completed','failed','aborted')"""
            ).fetchone()["n"]
        finally:
            conn.close()

        if running >= 2:
            logger.debug(f"Planner: {running} interne Tasks aktiv -- skip")
            return

        from core.planner import run_planner
        result = run_planner(user_id)
        if result.get("started_tasks"):
            logger.info(f"Planner: {len(result['started_tasks'])} Tasks gestartet")
    except Exception as e:
        logger.debug(f"_maybe_run_planner fehlgeschlagen (unkritisch): {e}")


TRIGGER_HANDLERS = {
    "user_input":       _handle_user_input,
    "heartbeat":        _handle_heartbeat,
    "time_window":      _handle_time_window,
    "tool_result":      _handle_tool_result,
    "cognition_output": _handle_cognition_output,
    "cognition_run":    _handle_cognition_run,
    "mirror_signal":    _handle_mirror_signal,
    "review_result":    _handle_review_result,
    "manual_override":  _handle_manual_override,
    "recovery_result":  _handle_recovery_result,
    "wiedervorlage":    _handle_wiedervorlage,
    "idle_pulse":       _handle_idle_pulse,
}


# =============================================================================
# Event-Loop
# =============================================================================

def collect_triggers() -> list:
    return get_pending_triggers()


def process(events: list) -> None:
    for event in events:
        trigger_type = event.get("trigger_type", "unknown")
        trigger_id = event.get("id", "")
        handler = TRIGGER_HANDLERS.get(trigger_type)

        if handler:
            try:
                handler(event)
                mark_trigger_processed(trigger_id)
            except Exception as e:
                logger.error(f"Trigger-Handler {trigger_type} fehlgeschlagen: {e}", exc_info=True)
                mark_trigger_processed(trigger_id)
        else:
            logger.warning(f"Unbekannter Trigger-Typ: {trigger_type} ({trigger_id[:8]})")
            mark_trigger_processed(trigger_id)


# =============================================================================
# Build Step 3 -- Thread-Logik
# =============================================================================

THREAD_TRANSITIONS = {
    "new":       {"watching", "converted", "discarded"},
    "watching":  {"converted", "merged", "discarded"},
    "converted": set(),
    "merged":    set(),
    "discarded": set(),
}


def thread_transition(thread_id: str, new_status: str, reason: str = None,
                      trigger_refs: list = None) -> bool:
    thread = get_thread(thread_id)
    if not thread:
        logger.warning(f"thread_transition: Thread {thread_id[:8]} nicht gefunden")
        return False

    current = thread["status"]
    allowed = THREAD_TRANSITIONS.get(current, set())

    if new_status not in allowed:
        no_action(
            reason=f"Ungültiger Thread-Übergang: {current} -> {new_status}",
            context=f"thread {thread_id[:8]}",
            trigger_refs=trigger_refs or [],
        )
        return False

    update_thread(thread_id, status=new_status, reason=reason)
    audit("orbit", f"thread_{new_status}", "thread", thread_id,
          reason or f"{current} -> {new_status}")
    logger.info(f"Thread {thread_id[:8]}: {current} -> {new_status}" +
                (f" | {reason}" if reason else ""))
    return True


def assess_thread_relevance(thread_id: str) -> str:
    thread = get_thread(thread_id)
    if not thread:
        return "weak"

    trigger_refs = _parse(thread.get("trigger_refs"), [])
    trigger_count = len(trigger_refs)
    has_linked_task = bool(thread.get("linked_task_id"))

    if has_linked_task or trigger_count >= 5:
        return "strong"
    elif trigger_count >= 2:
        return "medium"
    return "weak"


def convert_thread_to_task(thread_id: str, task_type: str = "observation",
                            goal: str = None, priority: str = "medium",
                            trigger_refs: list = None) -> str | None:
    thread = get_thread(thread_id)
    if not thread:
        return None

    if thread["status"] not in ("new", "watching"):
        no_action(
            reason=f"Thread {thread_id[:8]} ist {thread['status']} -- Konvertierung abgebrochen",
            trigger_refs=trigger_refs or [],
        )
        return None

    task_goal = goal or thread["topic_core"]
    task_id = create_task(
        task_type=task_type,
        goal=task_goal,
        primary_origin=thread["primary_origin"],
        mode="background",
        priority=priority,
        source_thread_id=thread_id,
    )

    update_thread(thread_id, linked_task_id=task_id)
    thread_transition(thread_id, "converted",
                      reason=f"Konvertiert zu Task {task_id[:8]}",
                      trigger_refs=trigger_refs or [])

    make_decision(
        decision_type="thread_to_task",
        target_ref=task_id,
        reason=f"Thread '{thread['topic_core'][:60]}' -> Task ({task_type})",
        trigger_refs=trigger_refs or [],
        confidence=0.7,
    )

    logger.info(f"Thread {thread_id[:8]} -> Task {task_id[:8]} ({task_type})")
    return task_id


def discard_thread(thread_id: str, reason: str, trigger_refs: list = None) -> bool:
    update_thread(thread_id, discard_reason=reason)
    return thread_transition(thread_id, "discarded", reason=reason,
                             trigger_refs=trigger_refs or [])


def merge_threads(source_id: str, target_id: str, reason: str = None) -> bool:
    update_thread(source_id, merge_target_id=target_id)
    return thread_transition(source_id, "merged",
                             reason=reason or f"Merged in {target_id[:8]}")


# =============================================================================
# Build Step 3 -- Task-Logik
# =============================================================================

TASK_TRANSITIONS = {
    "new":              {"planned", "active", "aborted"},
    "planned":          {"active", "paused", "aborted"},
    "active":           {"waiting", "waiting_feedback", "paused", "completed", "failed", "aborted"},
    "waiting":          {"active", "paused", "aborted"},
    "waiting_feedback":        {"active", "completed", "failed", "aborted"},  # F: nach Kognitionszyklus
    "waiting_user_decision":   {"active", "completed", "failed", "aborted"},  # 5.2: wartet auf Approval
    "paused":           {"active", "aborted"},
    "completed":        set(),
    "failed":           set(),
    "aborted":          set(),
}

PRIORITY_WEIGHT = {"critical": 4, "high": 3, "medium": 2, "low": 1}


def task_transition(task_id: str, new_status: str, reason: str = None,
                    trigger_refs: list = None) -> bool:
    task = get_task(task_id)
    if not task:
        logger.warning(f"task_transition: Task {task_id[:8]} nicht gefunden")
        return False

    current = task["status"]
    allowed = TASK_TRANSITIONS.get(current, set())

    if new_status not in allowed:
        no_action(
            reason=f"Ungültiger Task-Übergang: {current} -> {new_status}",
            context=f"task {task_id[:8]}",
            trigger_refs=trigger_refs or [],
        )
        return False

    update_task(task_id, status=new_status)
    audit("orbit", f"task_{new_status}", "task", task_id,
          reason or f"{current} -> {new_status}")
    logger.info(f"Task {task_id[:8]}: {current} -> {new_status}" +
                (f" | {reason}" if reason else ""))

    # Status-Fortschreibung + Completion Events
    if new_status in ("completed", "failed", "aborted"):
        try:
            task_obj = get_task(task_id)
            owner_id = ""
            if task_obj:
                origin = task_obj.get("primary_origin", "")
                if origin.startswith("user:"):
                    owner_id = origin[5:]
                else:
                    from config import OWNER_ID
                    owner_id = OWNER_ID

            # Completion Event für Task
            conn = get_connection()
            try:
                from core.datetime_utils import to_iso as _to_iso
                conn.execute(
                    """INSERT INTO kimi_completions
                       (owner_id, for_object_type, for_object_id, reason, summary, created_at)
                       VALUES (?,?,?,?,?,?)""",
                    (owner_id, "task", task_id,
                     new_status, reason or f"Task {new_status}",
                     _to_iso())
                )
                conn.commit()
            finally:
                conn.close()

            # Todo-Fortschreibung nur bei completed
            if new_status == "completed" and task_obj and task_obj.get("linked_todo_id"):
                from core.todo_service import complete_todo
                complete_todo(int(task_obj["linked_todo_id"]),
                             summary=f"Task {task_id[:8]} abgeschlossen",
                             task_id=task_id)
                logger.info(f"task_transition: Todo #{task_obj['linked_todo_id']} automatisch erledigt")

            # Bei failed/aborted -> Todo auf blocked
            elif new_status in ("failed", "aborted") and task_obj and task_obj.get("linked_todo_id"):
                from core.todo_service import block_todo
                block_todo(int(task_obj["linked_todo_id"]), reason=f"Task {new_status}")

            # E: Release-Logik für interne Tasks nach Abschluss
            if new_status in ("completed", "failed", "aborted") and task_obj:
                task_mode = task_obj.get("mode", "background")
                release_mode = task_obj.get("release_mode", "manual")
                release_state = task_obj.get("release_state", "not_released")

                if task_mode == "internal" and release_state not in ("released", "suppressed"):
                    if release_mode == "manual":
                        # manual -> sofort ready_for_release, kein Trigger
                        update_task(task_id, release_state="ready_for_release")
                        logger.info(f"E: Task {task_id[:8]} -> ready_for_release (manual, {new_status})")
                    else:
                        # auto_if_done / summarize -> tool_result Trigger für Reflexion + Release
                        try:
                            create_trigger(
                                trigger_type="tool_result",
                                source="task_transition",
                                payload={
                                    "task_id": task_id,
                                    "tool": "task_completion",
                                    "success": new_status == "completed",
                                    "result": f"Task {new_status}: {task_obj.get('goal','')[:80]}",
                                    "user_id": owner_id,
                                    "is_terminal_release": True,
                                },
                            )
                            logger.info(f"E: Task {task_id[:8]} Release-Trigger erzeugt ({release_mode})")
                        except Exception as _rt:
                            logger.debug(f"E: Release-Trigger fehlgeschlagen: {_rt}")
                            update_task(task_id, release_state="ready_for_release")

        except Exception as _tf:
            logger.debug(f"task_transition: Fortschreibung fehlgeschlagen (unkritisch): {_tf}")

    return True


def set_task_hot(task_id: str, hot: bool = True, reason: str = None) -> bool:
    if hot and count_hot_tasks() >= MAX_HOT_TASKS:
        defensive_fallback(
            reason=f"Max. heiße Tasks ({MAX_HOT_TASKS}) erreicht -- Task bleibt kalt",
            original_action="task_set_hot",
            fallback_action="task_stays_cold",
        )
        return False

    update_task(task_id, hot=1 if hot else 0)
    audit("orbit", "task_hot" if hot else "task_cold", "task", task_id, reason)
    logger.info(f"Task {task_id[:8]}: hot={'ja' if hot else 'nein'}" +
                (f" | {reason}" if reason else ""))
    return True


def downgrade_task_to_thread(task_id: str, reason: str,
                              trigger_refs: list = None) -> str | None:
    task = get_task(task_id)
    if not task:
        return None

    if task["status"] not in ("new", "planned", "paused"):
        no_action(
            reason=f"Task {task_id[:8]} ist {task['status']} -- Rückstufung nicht möglich",
            trigger_refs=trigger_refs or [],
        )
        return None

    task_transition(task_id, "aborted", reason=f"Rückgestuft zu Thread: {reason}",
                    trigger_refs=trigger_refs or [])
    set_task_hot(task_id, False)

    thread_id = create_thread(
        topic_core=task["goal"],
        primary_origin=task["primary_origin"],
        relevance="medium",
        reason=f"Rückgestuft von Task {task_id[:8]}: {reason}",
    )

    make_decision(
        decision_type="task_to_thread",
        target_ref=thread_id,
        reason=reason,
        trigger_refs=trigger_refs or [],
        confidence=0.4,
        alternative_rejected="task_continue",
    )

    logger.info(f"Task {task_id[:8]} -> Thread {thread_id[:8]}: {reason}")
    return thread_id


def get_scheduled_tasks() -> list:
    conn = get_connection()
    try:
        rows = conn.execute("""
            SELECT * FROM orbit_tasks
            WHERE status NOT IN ('completed', 'failed', 'aborted')
            ORDER BY
                CASE priority WHEN 'critical' THEN 4 WHEN 'high' THEN 3
                              WHEN 'medium' THEN 2 ELSE 1 END DESC,
                hot DESC,
                CASE status WHEN 'active' THEN 3 WHEN 'waiting' THEN 2
                            WHEN 'planned' THEN 1 ELSE 0 END DESC,
                created_at ASC
        """).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


# =============================================================================
# Build Step 3 -- Step-Logik
# =============================================================================

STEP_TRANSITIONS = {
    "ready":    {"running", "deferred", "done"},
    "running":  {"ready", "blocked", "deferred", "done", "failed"},
    "blocked":  {"ready", "deferred", "failed"},
    "deferred": {"ready", "failed"},
    "done":     set(),
    "failed":   set(),
}


def step_transition(step_id: str, new_status: str, reason: str = None) -> bool:
    step = get_step(step_id)
    if not step:
        return False

    current = step["status"]
    allowed = STEP_TRANSITIONS.get(current, set())

    if new_status not in allowed:
        logger.warning(f"Ungültiger Step-Übergang: {current} -> {new_status} (step {step_id[:8]})")
        return False

    kwargs = {"status": new_status}
    if new_status == "blocked" and reason:
        kwargs["blocked_reason"] = reason
    if new_status == "deferred" and reason:
        kwargs["deferred_reason"] = reason
    if new_status == "failed" and reason:
        kwargs["failure_mode"] = reason

    update_step(step_id, **kwargs)
    logger.debug(f"Step {step_id[:8]}: {current} -> {new_status}" +
                 (f" | {reason}" if reason else ""))
    return True


def get_next_step_for_task(task_id: str) -> dict | None:
    running = get_steps(task_id=task_id, status="running")
    if running:
        return running[0]
    ready = get_steps(task_id=task_id, status="ready")
    return ready[0] if ready else None


def can_start_step(step: dict) -> bool:
    running_count = count_running_steps()

    if running_count < MAX_RUNNING_STEPS - 1:
        return True

    if running_count == MAX_RUNNING_STEPS - 1:
        is_light = (not step.get("preflight_required") and
                    step.get("interruptible") and
                    not step.get("commit_point"))
        if is_light:
            return True

    defensive_fallback(
        reason=f"Systemlimit {MAX_RUNNING_STEPS} running Steps erreicht",
        original_action="step_start",
        fallback_action="step_deferred",
    )
    return False


def interrupt_step_if_possible(step_id: str, reason: str) -> bool:
    step = get_step(step_id)
    if not step:
        return False

    if not step.get("interruptible"):
        logger.info(f"Step {step_id[:8]} ist nicht unterbrechbar -- läuft weiter")
        return False

    step_transition(step_id, "deferred", reason=f"Unterbrochen: {reason}")
    audit("orbit", "step_interrupted", "step", step_id, reason)
    return True


# =============================================================================
# Build Step 3 -- Scheduler
# =============================================================================


def _execute_step(step: dict, task_id: str) -> None:
    """
    Baustein 1 -- Step-Execution.
    Fuehrt einen Step mit tool_ref tatsaechlich aus.
    Schreibt Ergebnis zurueck in Step + Task.
    Kein tool_ref -> Step wird als done markiert (reine Observation).
    """
    step_id = step["id"]
    tool_ref = step.get("tool_ref", "")
    params = {}
    try:
        raw = step.get("description", "") or ""
        import json as _j
        try:
            params = _j.loads(raw)
        except Exception:
            params = {}
    except Exception:
        params = {}

    # Kein Tool -- reiner Observation-Step -> sofort done
    if not tool_ref:
        step_transition(step_id, "done", reason="Kein tool_ref -- Observation-Step abgeschlossen")
        logger.debug(f"Step {step_id[:8]} done (kein Tool)")
        return

    # Tool-Verfuegbarkeit pruefen
    if not check_tool_availability(tool_ref):
        step_transition(step_id, "blocked", reason=f"Tool {tool_ref} nicht verfuegbar")
        logger.warning(f"Step {step_id[:8]} blocked: Tool {tool_ref} nicht verfuegbar")
        return

    # Action aus Step-Typ ableiten
    action_map = {
        "calendar_read":   "list",
        "calendar_write":  "create",
        "todos_read":      "list",
        "todos_write":     "create",
        "websearch":       "search",
        "pdf":             "search",
        "moltbook":        "explore",
        "introspection":   "run",
    }
    action = params.pop("action", action_map.get(tool_ref, "run"))

    # user_id/phone_number aus Task holen
    task = get_task(task_id)
    if task and "phone_number" not in params:
        origin = task.get("primary_origin", "")
        if origin.startswith("user:"):
            params["phone_number"] = origin[5:]
        else:
            from config import OWNER_ID
            params["phone_number"] = OWNER_ID

    # Tool ausfuehren
    result = execute_tool(
        tool_ref=tool_ref,
        action=action,
        params=params,
        task_id=task_id,
        step_id=step_id,
    )

    if result["success"]:
        step_transition(step_id, "done", reason=f"Tool {tool_ref} erfolgreich")

        # result_summary in Step schreiben
        result_summary = str(result.get("result", ""))[:300] if result.get("result") else f"{tool_ref}.{action} erfolgreich"
        try:
            update_step(step_id, result_summary=result_summary)
        except Exception:
            pass

        # Observation erzeugen
        try:
            from core.todo_service import record_observation
            from config import OWNER_ID
            owner = OWNER_ID
            if task:
                origin = task.get("primary_origin", "")
                if origin.startswith("user:"):
                    owner = origin[5:]
            record_observation(
                owner_id=owner,
                content=result_summary,
                obs_type="tool_result",
                task_id=task_id,
                step_id=step_id,
                todo_id=int(task["linked_todo_id"]) if task and task.get("linked_todo_id") else None,
                proposal_id=int(task["proposal_id"]) if task and task.get("proposal_id") else None,
                goal_id=int(task["goal_id"]) if task and task.get("goal_id") else None,
            )
        except Exception as _oe:
            logger.debug(f"Step Observation fehlgeschlagen (unkritisch): {_oe}")

        # E: Ergebnis nur bei chat-Modus sofort senden
        if task and task.get("mode") == "chat":
            _deliver_step_result(task, step, result)
        elif task and task.get("mode") == "internal":
            logger.debug(f"E: Step {step_id[:8]} done (internal -- kein sofortiger Send)")
        logger.info(f"Step {step_id[:8]} done: {tool_ref}.{action}")

        # F: tool_result Trigger feuern -- startet _handle_tool_result Kognitionszyklus
        if task and task.get("mode") == "internal":
            try:
                _user_id = OWNER_ID
                origin = task.get("primary_origin", "")
                if origin.startswith("user:"):
                    _user_id = origin[5:]
                # Task auf waiting_feedback setzen -- verhindert Scheduler-Auto-Complete
                update_task(task_id, status="waiting_feedback")
                create_trigger(
                    trigger_type="tool_result",
                    source=f"step:{step_id[:8]}",
                    payload={
                        "task_id": task_id,
                        "step_id": step_id,
                        "tool": tool_ref,
                        "success": True,
                        "result": result.get("result", result_summary),
                        "user_id": _user_id,
                    },
                )
                logger.debug(f"F: tool_result Trigger erzeugt fuer Step {step_id[:8]}")
            except Exception as _ft:
                logger.debug(f"F: tool_result Trigger fehlgeschlagen (unkritisch): {_ft}")
    else:
        err = result.get("error", "unbekannt")[:80]
        step_transition(step_id, "failed", reason=err)

        # Fehler-Observation
        try:
            from core.todo_service import record_observation
            from config import OWNER_ID
            record_observation(
                owner_id=OWNER_ID,
                content=f"Step fehlgeschlagen: {tool_ref}.{action} -- {err}",
                obs_type="error",
                task_id=task_id,
                step_id=step_id,
                todo_id=int(task["linked_todo_id"]) if task and task.get("linked_todo_id") else None,
            )
        except Exception:
            pass

        logger.warning(f"Step {step_id[:8]} failed: {tool_ref}.{action} -- {err}")

        # F: tool_result Trigger auch bei Fehler -- Kognition entscheidet ob block/replan
        if task and task.get("mode") == "internal":
            try:
                _user_id = OWNER_ID
                origin = task.get("primary_origin", "")
                if origin.startswith("user:"):
                    _user_id = origin[5:]
                update_task(task_id, status="waiting_feedback")
                create_trigger(
                    trigger_type="tool_result",
                    source=f"step:{step_id[:8]}",
                    payload={
                        "task_id": task_id,
                        "step_id": step_id,
                        "tool": tool_ref,
                        "success": False,
                        "result": err,
                        "user_id": _user_id,
                    },
                )
            except Exception as _ft:
                logger.debug(f"F: tool_result Trigger (fail) fehlgeschlagen: {_ft}")


def _deliver_step_result(task: dict, step: dict, result: dict) -> None:
    """
    Liefert ein Tool-Ergebnis an Tommy -- als WhatsApp-Nachricht.
    Nur fuer Tasks im chat-Modus (direkte Nutzer-Anfragen).
    """
    try:
        from config import OWNER_ID, WAHA_API_KEY
        from core.whatsapp import send_message, init_waha
        from core.database import save_message
        init_waha(WAHA_API_KEY)

        tool_ref = step.get("tool_ref", "")
        raw_result = result.get("result")

        if not raw_result:
            return

        # Ergebnis formatieren
        if isinstance(raw_result, str):
            content = raw_result
        elif isinstance(raw_result, dict):
            import json as _j
            content = _j.dumps(raw_result, ensure_ascii=False, indent=2)
        else:
            content = str(raw_result)

        if not content or len(content.strip()) < 5:
            return

        # Kontext-Präambel je nach Task-Ursprung
        origin = task.get("primary_origin", "")
        is_autonomous = origin.startswith("orbit:autonomous")

        if is_autonomous:
            # Kimi formuliert die Nachricht in separatem Thread (nicht-blockierend)
            import threading as _threading
            def _send_with_context(phone, content, tool_ref, task, OWNER_ID):
                try:
                    from dotenv import load_dotenv as _ldenv
                    _ldenv("/opt/whatsapp-bot/.env")
                    from core.ollama_client import chat_internal as _chat_int
                    from core.database import save_message as _save
                    from core.whatsapp import send_message as _send, init_waha as _init
                    from config import USER_CONTEXTS, WAHA_API_KEY
                    _init(WAHA_API_KEY)
                    context_name = USER_CONTEXTS.get(phone, "Tommy")
                    prompt = (
                        "Ich habe gerade autonom Daten abgerufen die fuer Tommy relevant sind. "
                        "Fasse den Inhalt in 2-3 Saetzen zusammen und erklaere kurz warum "
                        "ich das proaktiv geschickt habe. Halte dich exakt an die Daten. "
                        "Kein Markdown, Fliesstext. Keine Tool-Syntax."
                    )
                    reply, _ = _chat_int(phone, prompt, [], context_name,
                        doc_context=content[:1200])
                    msg = reply.strip() if reply else content
                    if len(msg) > 2000:
                        msg = msg[:1997] + "..."
                    _send(phone, msg)
                    _save(phone, "assistant", msg)
                    logger.info(f"Autonome Nachricht gesendet (mit Kontext): {tool_ref} an {phone[:20]}")
                except Exception as _e:
                    logger.warning(f"Autonome Kimi-Formulierung fehlgeschlagen: {_e}")
                    try:
                        from core.whatsapp import send_message as _send2, init_waha as _init2
                        from config import WAHA_API_KEY as _key
                        _init2(_key)
                        _send2(phone, content[:2000])
                    except Exception:
                        pass

            phone = OWNER_ID
            if origin.startswith("user:"):
                phone = origin[5:]
            t = _threading.Thread(
                target=_send_with_context,
                args=(phone, content, tool_ref, task, OWNER_ID),
                daemon=True,
            )
            t.start()
            return  # Nicht weiter unten senden
        else:
            # Direkte Task-Anfrage -- roher Output mit Prefix
            prefix_map = {
                "calendar_read":  "Kalender:",
                "todos_read":     "Aufgaben:",
                "websearch":      "Suche:",
            }
            prefix = prefix_map.get(tool_ref, "")
            msg = (prefix + " " + content).strip() if prefix else content

        # Sendelimit: max 2000 Zeichen
        if len(msg) > 2000:
            msg = msg[:1997] + "..."

        phone = OWNER_ID
        origin = task.get("primary_origin", "")
        if origin.startswith("user:"):
            phone = origin[5:]

        send_message(phone, msg)
        save_message(phone, "assistant", msg)
        logger.info(f"Step-Ergebnis gesendet: {tool_ref} an {phone[:20]}")

    except Exception as e:
        logger.warning(f"_deliver_step_result fehlgeschlagen: {e}")


def run_scheduler() -> None:
    tasks = get_scheduled_tasks()
    if not tasks:
        return

    for task in tasks:
        task_id = task["id"]
        status = task["status"]

        # F: waiting_feedback = F-Kognitionszyklus läuft noch -- nicht anfassen
        if status == "waiting_feedback":
            logger.debug(f"Scheduler: Task {task_id[:8]} wartet auf F-Feedback -- skip")
            continue

        # 5.2: waiting_user_decision = wartet auf Approval -- nicht anfassen
        if status == "waiting_user_decision":
            logger.debug(f"Scheduler: Task {task_id[:8]} wartet auf User-Freigabe -- skip")
            continue

        if status not in ("active", "planned", "new"):
            continue

        if status in ("new", "planned"):
            task_transition(task_id, "active", reason="Scheduler: aktiviert")

        next_step = get_next_step_for_task(task_id)
        if not next_step:
            # F: Wenn internal mode -- nicht sofort completed, sondern F-Trigger abwarten
            if task.get("mode") == "internal":
                logger.debug(f"Task {task_id[:8]} keine weiteren Steps (internal) -- F entscheidet")
                # Sicherheitsnetz: wenn Task schon waiting_feedback ist, nichts tun
                # Wenn nicht, dann kurz warten -- F wird via Trigger aktiviert
            else:
                task_transition(task_id, "completed", reason="Alle Steps abgeschlossen")
                set_task_hot(task_id, False)
                logger.info(f"Task {task_id[:8]} abgeschlossen -- keine weiteren Steps")
            continue

        if next_step["status"] == "ready":
            if can_start_step(next_step):
                step_transition(next_step["id"], "running")
                update_task(task_id, current_step_id=next_step["id"])
                logger.info(f"Task {task_id[:8]}: Step {next_step['id'][:8]} gestartet ({next_step['step_type']})")
                # Baustein 1: Step tatsaechlich ausfuehren
                _execute_step(next_step, task_id)


# =============================================================================
# Build Step 4 -- Innere Konsultation
# =============================================================================

def consult_self_reflection(limit: int = 5) -> dict:
    """
    Holt Kimis akkumuliertes Selbstbild aus ChromaDB.
    Gibt strukturierten Rückspiegel-Text + Confidence-Trend zurück.

    [PHASE 2 FIX] Korrekter Funktionsname: get_self_reflection_summary (nicht build_reflection_summary)
    """
    try:
        from self_reflection_summary import get_self_reflection_summary
        summary = get_self_reflection_summary(max_chunks=limit)
        return {"available": bool(summary), "summary": summary or "", "source": "self_reflection"}
    except Exception as e:
        logger.debug(f"consult_self_reflection: {e}")
        return {"available": False, "summary": "", "source": "self_reflection"}


def consult_mirror(days: int = 7) -> dict:
    try:
        from core.database import get_mirror_stats
        stats = get_mirror_stats(days=days)
        total = stats.get("total_turns", 0)
        bad = stats.get("preflight_distribution", {})
        bad_count = bad.get("orange", 0) + bad.get("red", 0)
        bad_pct = round(bad_count / max(total, 1) * 100)
        top_patterns = sorted(stats.get("pattern_counts", {}).items(), key=lambda x: -x[1])[:3]
        trend = stats.get("trend", {})
        return {
            "available": total > 0,
            "total_turns": total,
            "bad_pct": bad_pct,
            "top_patterns": [p[0] for p in top_patterns],
            "trend_direction": trend.get("direction", "stable"),
            "source": "mirror",
        }
    except Exception as e:
        logger.debug(f"consult_mirror: {e}")
        return {"available": False, "source": "mirror"}


def consult_confidence_backmirror(task_id: str = None) -> dict:
    try:
        conn = get_connection()
        try:
            if task_id:
                rows = conn.execute(
                    "SELECT confidence FROM orbit_decisions WHERE target_ref = ? ORDER BY created_at DESC LIMIT 5",
                    (task_id,)
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT confidence FROM orbit_decisions ORDER BY created_at DESC LIMIT 10"
                ).fetchall()
        finally:
            conn.close()
        if not rows:
            return {"available": False, "source": "confidence_backmirror"}
        scores = [r["confidence"] for r in rows]
        avg = round(sum(scores) / len(scores), 2)
        low_count = sum(1 for s in scores if s < 0.4)
        return {
            "available": True,
            "avg_confidence": avg,
            "low_confidence_count": low_count,
            "sample_size": len(scores),
            "signal": "unstable" if avg < 0.4 or low_count >= 3 else "stable",
            "source": "confidence_backmirror",
        }
    except Exception as e:
        logger.debug(f"consult_confidence_backmirror: {e}")
        return {"available": False, "source": "confidence_backmirror"}


def inner_consultation(reason: str, resources: list = None, task_id: str = None) -> dict:
    if resources is None:
        resources = ["self_reflection", "mirror", "confidence"]
    logger.info(f"Innere Konsultation: {reason} | Ressourcen: {resources}")
    results = {}
    if "self_reflection" in resources:
        results["self_reflection"] = consult_self_reflection()
    if "mirror" in resources:
        results["mirror"] = consult_mirror()
    if "confidence" in resources:
        results["confidence"] = consult_confidence_backmirror(task_id=task_id)
    signals = []
    if results.get("mirror", {}).get("bad_pct", 0) > 40:
        signals.append("mirror_warning")
    if results.get("mirror", {}).get("trend_direction") == "worse":
        signals.append("mirror_trend_down")
    if results.get("confidence", {}).get("signal") == "unstable":
        signals.append("confidence_unstable")
    recommendation = "proceed" if not signals else ("defer" if len(signals) >= 2 else "proceed_with_caution")
    logger.info(f"Konsultation abgeschlossen: {recommendation} | Signale: {signals}")
    return {"reason": reason, "resources_used": resources, "results": results,
            "signals": signals, "recommendation": recommendation}


# =============================================================================
# Build Step 4 -- Quality Gate
# =============================================================================

CONFIDENCE_THRESHOLD_ACT    = 0.5
CONFIDENCE_THRESHOLD_COMMIT = 0.65
OVERANALYSIS_CONSULTATION_MAX = 3


class QualityGateResult:
    def __init__(self, passed: bool, reason: str, action: str = "proceed",
                 confidence: float = 1.0, signals: list = None, consultation: dict = None):
        self.passed = passed
        self.reason = reason
        self.action = action
        self.confidence = confidence
        self.signals = signals or []
        self.consultation = consultation
    def __bool__(self):
        return self.passed
    def __repr__(self):
        return f"QualityGateResult(passed={self.passed}, action={self.action}, reason={self.reason[:50]})"


def quality_gate(
    context: str,
    task_id: str = None,
    step: dict = None,
    confidence: float = None,
    is_external_action: bool = False,
    is_commit: bool = False,
    is_proactive: bool = False,
    force_consultation: bool = False,
) -> QualityGateResult:
    signals = []
    consultations_done = 0
    logger.debug(f"Quality Gate: '{context[:60]}' | extern={is_external_action} commit={is_commit}")

    if confidence is not None and confidence < CONFIDENCE_THRESHOLD_ACT:
        signals.append(f"low_confidence:{confidence:.2f}")

    needs_consultation = force_consultation or is_commit or (is_external_action and is_proactive) or len(signals) > 0
    consultation_result = None
    if needs_consultation and consultations_done < OVERANALYSIS_CONSULTATION_MAX:
        resources = []
        if "low_confidence" in " ".join(signals):
            resources.append("confidence")
        if is_proactive:
            resources.extend(["self_reflection", "mirror"])
        if is_commit:
            resources = ["self_reflection", "mirror", "confidence"]
        if not resources:
            resources = ["confidence"]
        consultation_result = inner_consultation(reason=f"Quality Gate: {context[:80]}",
                                                  resources=resources, task_id=task_id)
        consultations_done += 1
        signals.extend(consultation_result.get("signals", []))

    if task_id:
        task = get_task(task_id)
        if task and not task.get("goal"):
            signals.append("missing_goal")

    if step and task_id:
        task = get_task(task_id)
        if task and task.get("status") in ("paused", "aborted", "completed"):
            signals.append(f"task_status_conflict:{task['status']}")

    if is_commit and confidence is not None and confidence < CONFIDENCE_THRESHOLD_COMMIT:
        signals.append(f"commit_low_confidence:{confidence:.2f}")

    if consultations_done >= OVERANALYSIS_CONSULTATION_MAX:
        signals.append("overanalysis_limit")

    conn = get_connection()
    try:
        critical_running = conn.execute(
            "SELECT COUNT(*) FROM orbit_tasks WHERE priority = 'critical' AND status = 'active' AND hot = 1"
        ).fetchone()[0]
    finally:
        conn.close()
    if critical_running > 0 and not is_external_action:
        signals.append("critical_task_running")

    hard_blocks = [s for s in signals if any(s.startswith(p) for p in
                   ("commit_low_confidence", "task_status_conflict", "missing_goal"))]
    soft_signals = [s for s in signals if s not in hard_blocks]

    if hard_blocks:
        result = QualityGateResult(passed=False, reason=f"Hard Block: {', '.join(hard_blocks)}",
                                    action="abort", confidence=confidence or 0.0,
                                    signals=signals, consultation=consultation_result)
        logger.warning(f"Quality Gate FAILED: {result.reason} | {context[:60]}")
        make_decision(decision_type="quality_gate_block", target_ref=task_id or "orbit",
                      reason=result.reason, confidence=confidence or 0.0)
        return result

    if len(soft_signals) >= 2 or "overanalysis_limit" in signals:
        recommendation = consultation_result.get("recommendation", "defer") if consultation_result else "defer"
        action = "defer" if recommendation != "proceed" else "proceed_with_caution"
        result = QualityGateResult(passed=action != "defer",
                                    reason=f"Soft signals: {', '.join(soft_signals[:3])}",
                                    action=action, confidence=confidence or 0.5,
                                    signals=signals, consultation=consultation_result)
        logger.info(f"Quality Gate: {action} | {', '.join(soft_signals[:3])}")
        return result

    return QualityGateResult(passed=True, reason="Alle Pruefungen bestanden",
                              action="proceed", confidence=confidence or 1.0,
                              signals=signals, consultation=consultation_result)


def pre_execution_check(step: dict, task_id: str = None) -> QualityGateResult:
    tool_ref = step.get("tool_ref", "")
    is_commit = bool(step.get("commit_point"))
    is_external = bool(tool_ref)
    CRITICAL_TOOLS = {"calendar_write", "calendar_delete", "mail_send", "todos_write", "todos_delete"}
    CONTEXT_CRITICAL_TOOLS = {"calendar_read", "todos_read", "websearch", "pdf", "voice"}
    if tool_ref in CRITICAL_TOOLS:
        conf = CONFIDENCE_THRESHOLD_COMMIT
    elif tool_ref in CONTEXT_CRITICAL_TOOLS:
        conf = CONFIDENCE_THRESHOLD_ACT
    else:
        conf = 0.3
    return quality_gate(
        context=f"Pre-Execution: {step.get('step_type', '?')} / {tool_ref or 'kein Tool'}",
        task_id=task_id, step=step, confidence=conf,
        is_external_action=is_external, is_commit=is_commit,
    )


# =============================================================================
# Build Step 5 -- Policies
# =============================================================================

EVIDENCE_WEIGHTS = {
    "single_review":        0.3,
    "review_pattern":       0.5,
    "mirror_supported":     0.6,
    "reaction_supported":   0.65,
    "multi_source_supported": 0.85,
}

HARD_POLICY_CLASSES = {"risk_policy", "communication_policy"}


def _policy_score(policy: dict) -> float:
    from core.datetime_utils import now_utc
    from datetime import timedelta

    score = 0.0
    ev = policy.get("evidence_type") or "single_review"
    score += EVIDENCE_WEIGHTS.get(ev, 0.2)

    if policy.get("hardness") == "hard":
        score += 0.2

    try:
        created = policy.get("created_at", "")
        if created:
            from core.datetime_utils import safe_parse_dt
            age_days = (now_utc() - safe_parse_dt(created)).days
            score -= min(age_days / 365, 0.3)
    except Exception:
        pass

    score += float(policy.get("weight") or 0.5) * 0.3

    if policy.get("fragile"):
        score -= 0.15

    return round(max(0.0, min(1.0, score)), 3)


def activate_policy(policy_id: str, reason: str, trigger_refs: list = None) -> bool:
    policy = get_connection().execute(
        "SELECT * FROM orbit_policies WHERE id = ?", (policy_id,)
    ).fetchone()
    if not policy:
        logger.warning(f"activate_policy: Policy {policy_id[:8]} nicht gefunden")
        return False
    policy = dict(policy)

    if policy["status"] != "proposed":
        logger.warning(f"activate_policy: Policy {policy_id[:8]} ist {policy['status']} -- nicht proposed")
        return False

    if policy.get("hardness") == "hard" and policy["policy_class"] not in HARD_POLICY_CLASSES:
        logger.warning(f"activate_policy: Nur {HARD_POLICY_CLASSES} duerfen hard sein")
        update_policy(policy_id, hardness="soft")

    score = _policy_score(policy)
    update_policy(policy_id, status="active", rank=int(score * 100), activation_reason=reason)

    make_decision(
        decision_type="policy_activate",
        target_ref=policy_id,
        reason=reason,
        trigger_refs=trigger_refs or [],
        confidence=score,
    )
    audit("orbit", "policy_activated", "policy", policy_id,
          f"{policy['policy_class']} | score={score}")
    logger.info(f"Policy {policy_id[:8]} aktiviert: {policy['policy_class']} | score={score}")
    return True


def suppress_policy(policy_id: str, reason: str) -> bool:
    update_policy(policy_id, status="suppressed", reason=reason)
    audit("orbit", "policy_suppressed", "policy", policy_id, reason)
    logger.info(f"Policy {policy_id[:8]} suppressed: {reason}")
    return True


def retire_policy(policy_id: str, reason: str, replaced_by: str = None) -> bool:
    kwargs = {"status": "retired", "reason": reason}
    if replaced_by:
        kwargs["replaced_by"] = replaced_by
    update_policy(policy_id, **kwargs)
    audit("orbit", "policy_retired", "policy", policy_id, reason)
    return True


def check_policy_conflict(new_policy_id: str) -> list:
    new_p = get_connection().execute(
        "SELECT * FROM orbit_policies WHERE id = ?", (new_policy_id,)
    ).fetchone()
    if not new_p:
        return []
    new_p = dict(new_p)

    active = get_policies(status="active", policy_class=new_p["policy_class"])
    conflicts = []
    for p in active:
        if p["id"] == new_policy_id:
            continue
        new_scope = set(_parse(new_p.get("scope"), []))
        p_scope = set(_parse(p.get("scope"), []))
        if new_scope & p_scope:
            conflicts.append(p["id"])

    return conflicts


def get_active_policies_for_scope(scope: str) -> list:
    conn = get_connection()
    try:
        rows = conn.execute(
            """SELECT * FROM orbit_policies
               WHERE status = 'active' AND scope LIKE ?
               ORDER BY rank DESC, updated_at DESC""",
            (f"%{scope}%",)
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def apply_hard_policies(context: str, scope: str = "orbit") -> QualityGateResult:
    hard_policies = [p for p in get_active_policies_for_scope(scope)
                     if p.get("hardness") == "hard"]

    for policy in hard_policies:
        logger.debug(f"Hard Policy aktiv: {policy['id'][:8]} ({policy['policy_class']})")

    return QualityGateResult(
        passed=True,
        reason="Hard Policies geprueft -- keine Kollision",
        action="proceed",
    )


def mark_policy_fragile(policy_id: str, reason: str) -> None:
    update_policy(policy_id, fragile=1)
    audit("orbit", "policy_fragile", "policy", policy_id, reason)
    logger.info(f"Policy {policy_id[:8]} als fragile markiert: {reason}")


def mark_policy_stale(policy_id: str) -> None:
    update_policy(policy_id, stale=1)
    audit("orbit", "policy_stale", "policy", policy_id, "stale")


def run_policy_review(user_id: str = None) -> dict:
    from core.datetime_utils import now_utc, safe_parse_dt
    from datetime import timedelta

    now = now_utc()
    stale_cutoff = (now - timedelta(days=30)).isoformat()
    summary = {"stale_marked": 0, "activated": 0, "retired": 0}

    active = get_policies(status="active")
    for p in active:
        if not p.get("stale") and p.get("updated_at", "") < stale_cutoff:
            mark_policy_stale(p["id"])
            summary["stale_marked"] += 1

    proposed = get_policies(status="proposed")
    for p in proposed:
        score = _policy_score(p)
        ev_weight = EVIDENCE_WEIGHTS.get(p.get("evidence_type") or "single_review", 0.2)
        if score >= 0.55 and ev_weight >= 0.5:
            conflicts = check_policy_conflict(p["id"])
            if not conflicts:
                activate_policy(p["id"], reason="Policy-Review: ausreichend Evidenz")
                summary["activated"] += 1
            else:
                logger.info(f"Policy {p['id'][:8]} hat Konflikte mit {conflicts} -- manuell pruefen")
                create_review(
                    review_type="conflict_review",
                    target_ref=p["id"],
                    reason=f"Konflikt mit {len(conflicts)} aktiven Policies",
                )

    for p in active:
        if p.get("stale") or p.get("fragile"):
            create_review(
                review_type="policy_review",
                target_ref=p["id"],
                reason="stale" if p.get("stale") else "fragile",
            )

    logger.info(f"Policy-Review: {summary}")
    return summary


def bootstrap_policies(user_id: str) -> int:
    try:
        from memory.memory_store import get_active_collection
        col = get_active_collection()
        result = col.get(
            where={"type": {"$in": ["decision", "preference", "self_reflection"]}},
            limit=20,
            include=["metadatas", "documents"],
        )
    except Exception as e:
        logger.warning(f"bootstrap_policies: ChromaDB-Fehler: {e}")
        return 0

    created = 0
    docs = result.get("documents") or []
    metas = result.get("metadatas") or []

    for doc, meta in zip(docs, metas):
        chunk_type = meta.get("type", "")
        if not doc:
            continue

        policy_class = {
            "decision":        "action_policy",
            "preference":      "communication_policy",
            "self_reflection": "action_policy",
        }.get(chunk_type, "action_policy")

        conn = get_connection()
        try:
            existing = conn.execute(
                "SELECT id FROM orbit_policies WHERE reason LIKE ?",
                (f"%{doc[:40]}%",)
            ).fetchone()
        finally:
            conn.close()
        if existing:
            continue

        create_policy(
            policy_class=policy_class,
            primary_origin=f"bootstrap:chunk:{meta.get('id', '')[:8]}",
            scope=["orbit"],
            hardness="soft",
            reason=doc[:200],
        )
        created += 1

    if created > 0:
        logger.info(f"Bootstrap: {created} proposed Policies aus Chunks erzeugt")
        audit("orbit", "policy_bootstrap", "orbit", user_id, f"{created} Policies proposed")

    return created


# =============================================================================
# Build Step 6 -- Routinen
# =============================================================================

ROUTINE_CLASSES = {"check_routine", "execution_routine", "communication_routine", "review_routine"}
MAX_ROUTINE_BINDINGS = 3


def _validate_bindings(bindings: dict) -> dict:
    if not bindings:
        return {}
    keys = list(bindings.keys())[:MAX_ROUTINE_BINDINGS]
    return {k: bindings[k] for k in keys}


def activate_routine(routine_id: str, reason: str, trigger_refs: list = None) -> bool:
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM orbit_routines WHERE id = ?", (routine_id,)
        ).fetchone()
    finally:
        conn.close()
    if not row:
        logger.warning(f"activate_routine: {routine_id[:8]} nicht gefunden")
        return False
    routine = dict(row)

    if routine["status"] != "proposed":
        logger.warning(f"activate_routine: {routine_id[:8]} ist {routine['status']}")
        return False

    if not routine.get("procedure_body"):
        no_action(reason=f"Routine {routine_id[:8]} hat keinen Ablaufkörper -- nicht aktivierbar",
                  trigger_refs=trigger_refs or [])
        return False

    if not routine.get("primary_trigger_type"):
        no_action(reason=f"Routine {routine_id[:8]} hat keinen Primärtrigger -- nicht aktivierbar",
                  trigger_refs=trigger_refs or [])
        return False

    update_routine(routine_id, status="active")
    make_decision(
        decision_type="routine_activate",
        target_ref=routine_id,
        reason=reason,
        trigger_refs=trigger_refs or [],
        confidence=0.7,
    )
    audit("orbit", "routine_activated", "routine", routine_id,
          f"{routine['routine_class']} | trigger={routine['primary_trigger_type']}")
    logger.info(f"Routine {routine_id[:8]} aktiviert: {routine['routine_class']}")
    return True


def suppress_routine(routine_id: str, reason: str) -> bool:
    update_routine(routine_id, status="suppressed")
    audit("orbit", "routine_suppressed", "routine", routine_id, reason)
    return True


def retire_routine(routine_id: str, reason: str, replaced_by: str = None) -> bool:
    kwargs = {"status": "retired"}
    if replaced_by:
        kwargs["replaced_by"] = replaced_by
    update_routine(routine_id, **kwargs)
    audit("orbit", "routine_retired", "routine", routine_id, reason)
    return True


def get_routines_for_trigger(trigger_type: str) -> list:
    conn = get_connection()
    try:
        rows = conn.execute(
            """SELECT * FROM orbit_routines
               WHERE status = 'active'
               AND (primary_trigger_type = ?
                    OR secondary_trigger_types LIKE ?)
               ORDER BY rank DESC, updated_at DESC""",
            (trigger_type, f"%{trigger_type}%")
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def execute_routine(routine_id: str, context: dict = None, trigger_refs: list = None) -> dict:
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM orbit_routines WHERE id = ?", (routine_id,)
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return {"success": False, "deviation": False, "result": "nicht gefunden"}
    routine = dict(row)

    if routine["status"] != "active":
        return {"success": False, "deviation": False, "result": f"Routine ist {routine['status']}"}

    procedure = routine.get("procedure_body", "")
    if not procedure:
        return {"success": False, "deviation": False, "result": "kein Ablaufkörper"}

    gate = quality_gate(
        context=f"Routine: {routine['routine_class']} ({routine_id[:8]})",
        confidence=0.6,
    )
    if not gate:
        _increment_routine_counter(routine_id, "skip")
        return {"success": False, "deviation": False, "result": f"Quality Gate: {gate.action}"}

    logger.info(f"Routine {routine_id[:8]} ausgeführt: {routine['routine_class']}")
    _increment_routine_counter(routine_id, "apply")

    deviation = _check_routine_deviation(routine, context or {})
    if deviation:
        _increment_routine_counter(routine_id, "deviation")
        audit("orbit", "routine_deviation", "routine", routine_id, deviation)
        logger.info(f"Routine {routine_id[:8]} Abweichung: {deviation}")
        _maybe_propose_routine_from_deviation(routine, deviation, context or {})

    return {
        "success": True,
        "deviation": bool(deviation),
        "deviation_reason": deviation,
        "result": procedure[:200],
        "routine_class": routine["routine_class"],
    }


def _increment_routine_counter(routine_id: str, counter: str) -> None:
    valid = {"apply": "apply_count", "skip": "skip_count", "deviation": "deviation_count"}
    field = valid.get(counter)
    if not field:
        return
    conn = get_connection()
    try:
        conn.execute(
            f"UPDATE orbit_routines SET {field} = {field} + 1, updated_at = ? WHERE id = ?",
            (to_iso(), routine_id)
        )
        conn.commit()
    finally:
        conn.close()


def _check_routine_deviation(routine: dict, context: dict) -> str:
    bindings = _parse(routine.get("bindings"), {})
    if not bindings or not context:
        return ""
    deviations = []
    for key, expected in bindings.items():
        actual = context.get(key)
        if actual and actual != expected:
            deviations.append(f"{key}: erwartet={expected}, tatsächlich={actual}")
    return "; ".join(deviations)


def _maybe_propose_routine_from_deviation(routine: dict, deviation: str, context: dict) -> None:
    if routine.get("deviation_count", 0) < 3:
        return

    conn = get_connection()
    try:
        existing = conn.execute(
            "SELECT id FROM orbit_routines WHERE reason LIKE ? AND status = 'proposed'",
            (f"%abgeleitet von {routine['id'][:8]}%",)
        ).fetchone()
    finally:
        conn.close()
    if existing:
        return

    new_rid = create_routine(
        routine_class=routine["routine_class"],
        primary_trigger_type=routine["primary_trigger_type"],
        procedure_body=routine.get("procedure_body", "") + "\n\n[Angepasst: " + deviation[:100] + "]",
        primary_origin=f"deviation_learning:{routine['id'][:8]}",
        bindings=_parse(routine.get("bindings"), {}),
    )
    conn = get_connection()
    try:
        conn.execute(
            "UPDATE orbit_routines SET reason = ? WHERE id = ?",
            (f"abgeleitet von {routine['id'][:8]}: {deviation[:80]}", new_rid)
        )
        conn.commit()
    finally:
        conn.close()

    logger.info(f"Neue proposed Routine aus Abweichung: {new_rid[:8]} (Quelle: {routine['id'][:8]})")
    audit("orbit", "routine_proposed_from_deviation", "routine", new_rid,
          f"Quelle: {routine['id'][:8]}")


def mark_routine_fragile(routine_id: str, reason: str) -> None:
    update_routine(routine_id, fragile=1)
    audit("orbit", "routine_fragile", "routine", routine_id, reason)


def mark_routine_stale(routine_id: str) -> None:
    update_routine(routine_id, stale=1)
    audit("orbit", "routine_stale", "routine", routine_id, "stale")


def run_routine_review() -> dict:
    from core.datetime_utils import now_utc
    from datetime import timedelta

    now = now_utc()
    stale_cutoff = (now - timedelta(days=30)).isoformat()
    summary = {"stale_marked": 0, "activated": 0, "fragile_reviewed": 0}

    active = get_routines(status="active")
    for r in active:
        if not r.get("stale") and r.get("updated_at", "") < stale_cutoff:
            mark_routine_stale(r["id"])
            summary["stale_marked"] += 1

    proposed = get_routines(status="proposed")
    for r in proposed:
        if r.get("procedure_body") and r.get("primary_trigger_type"):
            sources = _parse(r.get("source_refs"), [])
            if sources:
                activate_routine(r["id"], reason="Routine-Review: Ablaufkörper + Evidenz vorhanden")
                summary["activated"] += 1

    for r in active:
        if r.get("stale") or r.get("fragile"):
            create_review(
                review_type="routine_review",
                target_ref=r["id"],
                reason="stale" if r.get("stale") else "fragile",
            )
            summary["fragile_reviewed"] += 1

    logger.info(f"Routine-Review: {summary}")
    return summary


# =============================================================================
# Build Step 7 -- Tool-Klassifikation & Integrations-Matrix
# =============================================================================

TOOL_REGISTRY = {
    # Memory / intern
    "chromadb":           {"criticality": "kontextkritisch", "usage": ["read"],          "type": "intern",        "write_indirect": True},
    "mirror":             {"criticality": "kontextkritisch", "usage": ["read"],           "type": "intern",        "write_indirect": False},
    # [PHASE 2] Introspektions-Tool -- intern, lesend, unkritisch
    "introspection":      {"criticality": "kontextkritisch",      "usage": ["consultative"],   "type": "intern",        "write_indirect": False},
    # Kalender
    "calendar_read":      {"criticality": "kontextkritisch", "usage": ["read"],           "type": "extern",        "write_indirect": False},
    "calendar_write":     {"criticality": "kritisch",        "usage": ["write"],          "type": "extern",        "write_indirect": False},
    "calendar_change":    {"criticality": "kritisch",        "usage": ["change"],         "type": "extern",        "write_indirect": False},
    "calendar_delete":    {"criticality": "kritisch",        "usage": ["delete"],         "type": "extern",        "write_indirect": False},
    # Todos/Listen
    "todos_read":         {"criticality": "kontextkritisch", "usage": ["read"],           "type": "extern",        "write_indirect": False},
    "todos_write":        {"criticality": "kritisch",        "usage": ["write"],          "type": "extern",        "write_indirect": False},
    "todos_delete":       {"criticality": "kritisch",        "usage": ["delete"],         "type": "extern",        "write_indirect": False},
    # Web Search
    "websearch":          {"criticality": "unkritisch",      "usage": ["consultative"],   "type": "extern",        "write_indirect": False},
    # PDF / Voice
    "pdf":                {"criticality": "kontextkritisch", "usage": ["consultative"],   "type": "extern_input",  "write_indirect": False},
    "voice":              {"criticality": "unkritisch",      "usage": ["consultative"],   "type": "extern_input",  "write_indirect": False},
    # Mail
    "mail_read":          {"criticality": "kontextkritisch", "usage": ["read"],           "type": "extern",        "write_indirect": False},
    "mail_draft":         {"criticality": "zwischenstufig",  "usage": ["write"],          "type": "extern",        "write_indirect": True},
    "mail_send":          {"criticality": "kritisch",        "usage": ["committing"],     "type": "extern",        "write_indirect": False},
    # Moltbook
    "moltbook":           {"criticality": "kontextkritisch", "usage": ["consultative"],   "type": "extern",        "write_indirect": False},
    # Whitelist / Server
    # Code-Execution (Kimi Workspace)
    "workspace":          {"criticality": "kontextkritisch", "usage": ["write"],          "type": "intern",        "write_indirect": True},
    "server_read":        {"criticality": "kontextkritisch", "usage": ["read"],           "type": "intern",        "write_indirect": False},
}

TOOL_RETRY_CONFIG = {
    "kritisch":        {"max_retries": 2, "backoff_seconds": 5},
    "kontextkritisch": {"max_retries": 3, "backoff_seconds": 2},
    "zwischenstufig":  {"max_retries": 2, "backoff_seconds": 3},
    "unkritisch":      {"max_retries": 3, "backoff_seconds": 1},
}


def get_tool_info(tool_ref: str) -> dict:
    return TOOL_REGISTRY.get(tool_ref, {
        "criticality": "kontextkritisch",
        "usage": ["read"],
        "type": "extern",
        "write_indirect": False,
    })


def is_tool_critical(tool_ref: str) -> bool:
    return get_tool_info(tool_ref).get("criticality") == "kritisch"


def get_tool_reputation(tool_ref: str) -> float:
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT score FROM orbit_reputation WHERE subject_type = 'tool' AND subject_id = ?",
            (tool_ref,)
        ).fetchone()
        return row["score"] if row else 0.7
    finally:
        conn.close()


def _update_tool_reputation(tool_ref: str, success: bool) -> None:
    delta = 0.05 if success else -0.1
    update_reputation("tool", tool_ref, delta)


def execute_tool(
    tool_ref: str,
    action: str,
    params: dict = None,
    task_id: str = None,
    step_id: str = None,
    dry_run: bool = False,
    owner_id: str = None,
) -> dict:
    import time as _time
    # 5.1: owner_id fuer Gate-Service
    if owner_id is None:
        try:
            from config import OWNER_ID
            owner_id = OWNER_ID
        except Exception:
            owner_id = ""
    _owner_id = owner_id
    tool_info = get_tool_info(tool_ref)
    criticality = tool_info.get("criticality", "kontextkritisch")
    is_commit = "committing" in tool_info.get("usage", []) or criticality == "kritisch"
    is_external = tool_info.get("type", "").startswith("extern")

    logger.info(f"Tool-Aufruf: {tool_ref}.{action} | kritisch={criticality} commit={is_commit}")

    if step_id:
        step = get_step(step_id)
        if step and step.get("preflight_required"):
            gate = pre_execution_check(step, task_id=task_id)
            if not gate:
                audit("orbit", "tool_blocked_by_gate", "tool", tool_ref,
                      f"{action}: {gate.reason}")
                return {"success": False, "result": None, "error": f"Quality Gate: {gate.reason}",
                        "tool_ref": tool_ref, "action": action, "retries": 0}

    policy_check = apply_hard_policies(context=f"{tool_ref}.{action}", scope="orbit")
    if not policy_check:
        return {"success": False, "result": None, "error": f"Hard Policy Block: {policy_check.reason}",
                "tool_ref": tool_ref, "action": action, "retries": 0}

    if dry_run:
        logger.info(f"Tool dry_run: {tool_ref}.{action} -- nicht ausgefuehrt")
        return {"success": True, "result": {"dry_run": True, "tool_ref": tool_ref, "action": action},
                "error": None, "tool_ref": tool_ref, "action": action, "retries": 0}

    retry_cfg = TOOL_RETRY_CONFIG.get(criticality, {"max_retries": 2, "backoff_seconds": 2})
    max_retries = retry_cfg["max_retries"]
    backoff = retry_cfg["backoff_seconds"]
    last_error = None

    for attempt in range(max_retries + 1):
        try:
            result = _dispatch_tool(tool_ref, action, params or {})
            _update_tool_reputation(tool_ref, success=True)
            audit("orbit", "tool_success", "tool", tool_ref, f"{action} | attempt={attempt}")
            if step_id:
                update_step(step_id, result_ref=str(result)[:200] if result else None)
            return {"success": True, "result": result, "error": None,
                    "tool_ref": tool_ref, "action": action, "retries": attempt}
        except Exception as e:
            last_error = str(e)
            logger.warning(f"Tool {tool_ref}.{action} Fehler (attempt {attempt+1}): {e}")
            if attempt < max_retries:
                _time.sleep(backoff)

    _update_tool_reputation(tool_ref, success=False)
    audit("orbit", "tool_failed", "tool", tool_ref, f"{action} | error={last_error[:80]}")

    if step_id:
        update_step(step_id, status="blocked", blocked_reason=f"{tool_ref}: {last_error[:80]}")

    return {"success": False, "result": None, "error": last_error,
            "tool_ref": tool_ref, "action": action, "retries": max_retries}


def _dispatch_tool(tool_ref: str, action: str, params: dict) -> object:
    """
    Dispatcht einen Tool-Aufruf an die konkrete Implementierung.
    """
    # Kalender
    if tool_ref in ("calendar_read", "calendar_write", "calendar_change", "calendar_delete"):
        # Lesen: direkt
        if tool_ref == "calendar_read":
            from core.calendar.calendar_router import execute_calendar_action
            return execute_calendar_action({"action": action, **params})

        # Schreiben: 5.2 Gate -- needs_approval fuer calendar_write/change
        from core.gate_service import execute_write, build_calendar_preview
        action_key = f"calendar.{action}" if action else f"calendar.{tool_ref.replace('calendar_','')}"

        def _do_cal(p):
            from core.calendar.calendar_router import execute_calendar_action
            return execute_calendar_action({"action": action, **p})

        gresult = execute_write(action_key, params, _owner_id, _do_cal,
                                task_id=task_id, step_id=step_id)

        if gresult.get("pending"):
            # Write-Request angelegt -- Task explizit auf waiting_user_decision
            if task_id:
                update_task(task_id, status="waiting_user_decision")
            return {"success": False, "pending": True,
                    "result": gresult.get("message","Write-Request angelegt"),
                    "write_request_id": gresult.get("write_request_id"),
                    "preview": gresult.get("preview")}

        return {"success": gresult["ok"],
                "result": gresult.get("result") or gresult.get("error"),
                "error": gresult.get("error"),
                "audit_id": gresult.get("audit_id")}

    # Todos / Listen
    if tool_ref in ("todos_read", "todos_write", "todos_delete"):
        action_type = params.get("action", action)

        # Lese-Aktionen: kein Gate
        if tool_ref == "todos_read" or action_type in ("list", "get"):
            from core.todos import execute_todo_action
            return execute_todo_action(params.get("phone_number", ""), params)

        # Schreib-Aktionen: 5.1 Gate
        from core.gate_service import execute_write

        # Action-Key bestimmen
        action_key_map = {
            "create": "todos.create",
            "complete": "todos.complete",
            "block": "todos.status",
            "status": "todos.status",
            "delete": "todos.status",  # delete treated as status change in 5.1
        }
        action_key = action_key_map.get(action_type, "todos.status")

        # params um owner_id / phone_number ergaenzen
        write_params = dict(params)
        write_params["owner_id"] = _owner_id

        def _do_todo_write(p):
            from core.todos import execute_todo_action
            result_text = execute_todo_action(p.get("phone_number", p.get("owner_id", "")), p)
            # Verifizierbare Rueckgabe: ob Todo-ID in Text erscheint
            success = result_text is not None and len(str(result_text).strip()) > 0
            # Bei create: ID extrahieren fuer Verify
            todo_id = None
            if action_type == "create" and result_text:
                import re
                m = re.search(r'#(\d+)', str(result_text))
                if m:
                    todo_id = int(m.group(1))
            return {"success": success, "result": result_text, "id": todo_id}

        gresult = execute_write(
            action_key, write_params, _owner_id, _do_todo_write,
            task_id=task_id, step_id=step_id
        )
        # Kompatible Rueckgabe
        result_text = gresult.get("result") or gresult.get("error", "")
        return {
            "success": gresult["ok"],
            "result": result_text,
            "error": gresult.get("error"),
            "audit_id": gresult.get("audit_id"),
        }

    # Web Search
    if tool_ref == "websearch":
        from core.websearch import search as web_search
        query = params.get("query", "")
        if not query:
            raise ValueError("websearch: query fehlt")
        return web_search(query)

    # PDF
    if tool_ref == "pdf":
        from core.document import search_doc_session
        phone = params.get("phone_number", "")
        query = params.get("query", "")
        return search_doc_session(phone, query)

    # Moltbook
    if tool_ref == "moltbook":
        from core.moltbook import execute_moltbook_action
        return execute_moltbook_action(params)

    # [PHASE 2] Introspektions-Tool
    if tool_ref == "introspection":
        return run_introspection_tool(
            user_id=params.get("user_id", ""),
            task_id=params.get("task_id"),
            emit_trigger=params.get("emit_trigger", True),
        )

    # Server-Lesezugriff
    if tool_ref == "server_read":
        from core.server_read import read_file
        return read_file(params.get("path", ""))

    # Code-Execution (Kimi Workspace)
    if tool_ref == "workspace":
        action_type = params.get("action", action)
        workspace = os.path.join(PROJECT_DIR, "kimi_workspace")
        os.makedirs(workspace, exist_ok=True)

        # Lese-Aktionen: kein Gate
        if action_type == "list":
            files = os.listdir(workspace)
            joined = "\n".join(files) if files else "(leer)"
            return {"success": True, "result": joined, "files": files}

        elif action_type == "read":
            fname = params.get("filename", "")
            path = os.path.join(workspace, os.path.basename(fname))
            if not os.path.exists(path):
                return {"success": False, "error": f"Datei nicht gefunden: {fname}"}
            with open(path, "r", encoding="utf-8") as f:
                return {"success": True, "result": f.read()[:5000]}

        # Schreib-Aktionen: 5.1 Gate
        elif action_type == "save":
            from core.gate_service import execute_write
            def _do_save(p):
                fname = p.get("filename", "output.txt")
                code = p.get("code", p.get("content", ""))
                path = os.path.join(workspace, os.path.basename(fname))
                with open(path, "w", encoding="utf-8") as f:
                    f.write(code)
                return {"success": True, "result": f"Datei gespeichert: {fname}"}
            gresult = execute_write("workspace.save", params, _owner_id,
                                    _do_save, task_id=task_id, step_id=step_id)
            return {"success": gresult["ok"],
                    "result": gresult.get("result") or gresult.get("error"),
                    "error": gresult.get("error"),
                    "audit_id": gresult.get("audit_id")}

        elif action_type == "delete":
            from core.gate_service import execute_write
            def _do_delete(p):
                fname = p.get("filename", "")
                path = os.path.join(workspace, os.path.basename(fname))
                if os.path.exists(path):
                    os.remove(path)
                    return {"success": True, "result": f"Datei geloescht: {fname}"}
                return {"success": False, "error": f"Datei nicht gefunden: {fname}"}
            gresult = execute_write("workspace.delete", params, _owner_id,
                                    _do_delete, task_id=task_id, step_id=step_id)
            return {"success": gresult["ok"],
                    "result": gresult.get("result") or gresult.get("error"),
                    "error": gresult.get("error"),
                    "audit_id": gresult.get("audit_id")}

        else:
            return {"success": False, "error": f"Unbekannte workspace action: {action_type}"}

    # Mail -- noch nicht implementiert
    if tool_ref in ("mail_read", "mail_draft", "mail_send"):
        raise NotImplementedError(f"Mail-Tool '{tool_ref}' noch nicht implementiert")

    raise NotImplementedError(f"Tool '{tool_ref}' nicht in Dispatch-Tabelle")


def check_tool_availability(tool_ref: str) -> bool:
    rep = get_tool_reputation(tool_ref)
    if rep < 0.2:
        logger.warning(f"Tool {tool_ref} hat niedrige Reputation ({rep}) -- als unavailable markiert")
        return False

    try:
        import json as _json_mod
        tools_path = os.path.join(PROJECT_DIR, "data", "tools_config.json")
        with open(tools_path) as f:
            tools = {t["id"]: t for t in _json_mod.load(f)}
        config_map = {
            "calendar_read": "calendar", "calendar_write": "calendar",
            "todos_read": "tasks", "todos_write": "tasks",
            "websearch": "websearch", "pdf": "pdf", "voice": "voice",
        }
        config_id = config_map.get(tool_ref)
        if config_id and config_id in tools:
            return tools[config_id].get("enabled", True)
    except Exception:
        pass

    return True


# =============================================================================
# [PHASE 2] Introspektions-Tool -- aktiver Tool-Call
# =============================================================================

def run_introspection_tool(
    user_id: str,
    task_id: str = None,
    emit_trigger: bool = True,
) -> dict:
    """
    Introspektions-Tool Phase 2: Kimi ruft ihr eigenes Selbstbild aktiv ab.

    Läuft als ORBIT-Tool-Call -- nicht passiv im Prompt, sondern aktiv auf Anforderung.
    Gibt strukturiertes Ergebnis zurück und schreibt optional einen cognition_output
    Trigger zurück in ORBIT, damit das Ergebnis operativ verarbeitet werden kann.

    Aufruf über execute_tool("introspection", "run", params={...})
    oder direkt als run_introspection_tool().

    Returns: {
        "available": bool,
        "summary": str,           -- Rückspiegel-Text (für Prompts)
        "trend": dict,            -- Confidence-Trend
        "chunk_count": int,       -- Anzahl geladener Chunks
        "strong_count": int,      -- Überzeugungen mit confidence >= 0.7
        "top_tags": list,         -- häufigste Tags
        "trigger_id": str | None, -- ID des erzeugten cognition_output Triggers
    }
    """
    logger.info(f"Introspektions-Tool aufgerufen | user={user_id} task={task_id}")

    result = {
        "available": False,
        "summary": "",
        "trend": {},
        "chunk_count": 0,
        "strong_count": 0,
        "top_tags": [],
        "trigger_id": None,
    }

    try:
        from self_reflection_summary import (
            get_self_reflection_summary,
            get_confidence_trend,
            get_recent_reflections,
            get_introspection_data,
        )

        chunks = get_recent_reflections()
        if not chunks:
            logger.info("Introspektions-Tool: keine self_reflection Chunks vorhanden")
            return result

        summary = get_self_reflection_summary()
        trend = get_confidence_trend(chunks)
        data = get_introspection_data()

        result["available"] = True
        result["summary"] = summary or ""
        result["trend"] = trend
        result["chunk_count"] = len(chunks)
        result["strong_count"] = trend.get("strong_count", 0)
        result["top_tags"] = [tag for tag, _ in data.get("top_tags", [])]

        logger.info(
            f"Introspektions-Tool: {len(chunks)} Chunks | "
            f"Trend={trend.get('trend')} | stark={trend.get('strong_count')}"
        )

        # Optional: cognition_output Trigger zurück in ORBIT
        # Nur wenn substantieller Inhalt vorhanden und emit_trigger aktiv
        if emit_trigger and summary and len(chunks) >= 2:
            # Kernthema aus Top-Tags ableiten
            top_tags = result["top_tags"]
            topic = f"Selbstbild-Update: {', '.join(top_tags[:3])}" if top_tags else "Selbstbild-Reflexion"

            # Relevanz anhand Trend-Stärke bestimmen
            trend_val = trend.get("trend", "stabil")
            relevance = "medium" if trend_val != "stabil" else "weak"
            if trend.get("strong_count", 0) >= 3:
                relevance = "medium"

            trigger_id = create_trigger(
                trigger_type="cognition_output",
                source="introspection_tool",
                payload={
                    "source": "introspection",
                    "topic_core": topic,
                    "relevance": relevance,
                    "chunk_count": len(chunks),
                    "trend": trend_val,
                    "strong_count": trend.get("strong_count", 0),
                    "task_id": task_id,
                    "user_id": user_id,
                },
            )
            result["trigger_id"] = trigger_id
            logger.info(f"Introspektions-Tool: cognition_output Trigger {trigger_id[:8]} erzeugt")

    except Exception as e:
        logger.error(f"Introspektions-Tool fehlgeschlagen: {e}", exc_info=True)

    return result


# =============================================================================
# Build Step 8 -- Proaktivitaet
# =============================================================================

PROACTIVE_THRESHOLDS = {
    "critical_alert":   0.3,
    "morning_briefing": 0.5,
    "evening_briefing": 0.5,
    "task_update":      0.55,
    "recommendation":   0.65,
    "nudge":            0.75,
}

PROACTIVE_ACTIVE_START = 8
PROACTIVE_ACTIVE_END   = 22
PROACTIVE_DAILY_LIMIT  = 5


def _count_sent_today() -> int:
    from core.datetime_utils import now_utc
    today = now_utc().isoformat()[:10]
    conn = get_connection()
    try:
        return conn.execute(
            """SELECT COUNT(*) FROM orbit_proactive_messages
               WHERE release_state = 'sent' AND created_at LIKE ?""",
            (f"{today}%",)
        ).fetchone()[0]
    finally:
        conn.close()


def _get_proactive_reputation(message_type: str) -> float:
    conn = get_connection()
    try:
        row = conn.execute(
            """SELECT score FROM orbit_reputation
               WHERE subject_type = 'proactive' AND subject_id = ?""",
            (message_type,)
        ).fetchone()
        return row["score"] if row else 0.6
    finally:
        conn.close()


def _is_in_active_window() -> bool:
    from core.datetime_utils import now_berlin
    hour = now_berlin().hour
    return PROACTIVE_ACTIVE_START <= hour < PROACTIVE_ACTIVE_END


def _is_morning_window() -> bool:
    from core.datetime_utils import now_berlin
    return 7 <= now_berlin().hour < 10


def _is_evening_window() -> bool:
    from core.datetime_utils import now_berlin
    return 20 <= now_berlin().hour < 22


def _has_active_chat() -> bool:
    from core.datetime_utils import now_utc
    from core.database import get_connection as _gc
    from config import OWNER_ID
    from datetime import timedelta
    cutoff = (now_utc() - timedelta(minutes=5)).isoformat()
    conn = _gc()
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM messages WHERE phone_number = ? AND timestamp > ?",
            (OWNER_ID, cutoff)
        ).fetchone()
        return row[0] > 0
    except Exception:
        return False
    finally:
        conn.close()


def evaluate_proactive_candidate(message_id: str) -> str:
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM orbit_proactive_messages WHERE id = ?", (message_id,)
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return "discard"
    msg = dict(row)
    msg_type = msg["message_type"]

    if msg["release_state"] not in ("candidate", "too_early"):
        return "discard"

    if _count_sent_today() >= PROACTIVE_DAILY_LIMIT:
        logger.info(f"Proaktiv {message_id[:8]}: Tageslimit erreicht -> suppress")
        return "suppress"

    if msg_type == "morning_briefing" and not _is_morning_window():
        return "too_early"
    if msg_type == "evening_briefing" and not _is_evening_window():
        return "too_early"
    if msg_type in ("nudge", "recommendation") and not _is_in_active_window():
        return "too_early"

    if msg_type == "critical_alert":
        return "send"

    if msg_type in ("nudge", "recommendation", "task_update") and _has_active_chat():
        logger.info(f"Proaktiv {message_id[:8]}: aktiver Chat -> too_early")
        return "too_early"

    reputation = _get_proactive_reputation(msg_type)
    threshold = PROACTIVE_THRESHOLDS.get(msg_type, 0.6)
    if reputation < threshold * 0.5:
        logger.info(f"Proaktiv {message_id[:8]}: Reputation {reputation} unter Schwelle -> suppress")
        return "suppress"

    gate = quality_gate(
        context=f"Proaktiv: {msg_type}",
        is_proactive=True,
        confidence=reputation,
    )
    if not gate:
        return "too_early" if gate.action == "defer" else "suppress"

    return "send"


def schedule_proactive_message(
    message_type: str,
    primary_origin: str,
    reason: str,
    source_task_id: str = None,
    source_thread_id: str = None,
    channel_target: str = None,
) -> str:
    from config import OWNER_ID
    mid = create_proactive_message(
        message_type=message_type,
        primary_origin=primary_origin,
        reason=reason,
        source_task_id=source_task_id,
        source_thread_id=source_thread_id,
    )
    target = channel_target or OWNER_ID
    update_proactive_message(mid, channel_target=target)
    logger.info(f"Proaktiv-Kandidat: {message_type} ({mid[:8]}) | {reason[:60]}")
    return mid


def send_proactive_message(message_id: str, content: str) -> bool:
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM orbit_proactive_messages WHERE id = ?", (message_id,)
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return False
    msg = dict(row)

    try:
        from core.whatsapp import send_message
        from core.database import save_message
        from config import OWNER_ID
        target = msg.get("channel_target") or OWNER_ID
        send_message(target, content)
        save_message(target, "assistant", content)
        update_proactive_message(message_id, release_state="sent")
        update_reputation("proactive", msg["message_type"], delta=0.02,
                          message_type=msg["message_type"])
        audit("orbit", "proactive_sent", "proactive_message", message_id,
              f"{msg['message_type']}: {content[:60]}")
        logger.info(f"Proaktiv gesendet: {msg['message_type']} ({message_id[:8]})")
        return True
    except Exception as e:
        logger.error(f"Proaktiv senden fehlgeschlagen: {e}")
        update_proactive_message(message_id, release_state="candidate")
        return False


def defer_proactive_message(message_id: str, reason: str, recheck_minutes: int = 30) -> None:
    from core.datetime_utils import now_utc
    from datetime import timedelta
    due = (now_utc() + timedelta(minutes=recheck_minutes)).isoformat()
    update_proactive_message(message_id, release_state="too_early", too_early_reason=reason)
    create_wiedervorlage(
        target_ref=message_id,
        target_type="proactive_message",
        reason=f"too_early: {reason}",
        due_at=due,
    )
    logger.info(f"Proaktiv {message_id[:8]} verschoben: {reason} | Wiedervorlage in {recheck_minutes}min")


def suppress_proactive_message(message_id: str, reason: str) -> None:
    update_proactive_message(message_id, release_state="suppressed", suppressed_reason=reason)
    logger.info(f"Proaktiv {message_id[:8]} suppressed: {reason}")


def record_proactive_reaction(message_id: str, reaction_class: str) -> None:
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT message_type FROM orbit_proactive_messages WHERE id = ?", (message_id,)
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return

    msg_type = row["message_type"]
    delta_map = {
        "accepted":  0.05,
        "acted_on":  0.08,
        "rejected": -0.1,
        "ignored":  -0.05,
        "unclear":   0.0,
    }
    delta = delta_map.get(reaction_class, 0.0)
    update_reputation("proactive", msg_type, delta=delta, message_type=msg_type)
    update_proactive_message(message_id, reaction_class=reaction_class)
    audit("orbit", f"proactive_reaction:{reaction_class}", "proactive_message", message_id, msg_type)
    logger.info(f"Proaktiv-Reaktion: {msg_type} -> {reaction_class} (delta={delta})")


def generate_briefing_content(briefing_type: str, user_id: str) -> str | None:
    """
    Generiert Briefing-Inhalt via Ollama.
    ORBIT ruft das NICHT direkt im Tick auf -- nur als expliziter Task.
    Im direkten Tick-Kontext: suppress und als cognition_run Task einreihen.
    """
    try:
        from core.ollama_client import chat as ollama_chat
        from core.database import get_chat_history
        from config import USER_CONTEXTS

        context_name = USER_CONTEXTS.get(user_id, "Tommy")
        is_morning = briefing_type == "morning_briefing"

        prompt = (
            "Erstelle ein kurzes " + ("Morgen-Briefing" if is_morning else "Abend-Briefing") +
            ". Schaue in dein Gedaechtnis nach offenen Themen, Terminen oder relevanten Entwicklungen. "
            "Wenn es nichts Relevantes gibt, antworte mit dem exakten Text: KEIN_BRIEFING. "
            "Sonst: maximal 3 kurze Punkte, kein Markdown, kein Bold, Fliesstext."
        )
        history = get_chat_history(user_id, limit=6)
        reply, _ = ollama_chat(user_id, prompt, history, context_name)

        if not reply or "KEIN_BRIEFING" in reply.upper():
            return None
        return reply.strip()
    except Exception as e:
        logger.error(f"generate_briefing_content: {e}")
        return None


def _maybe_schedule_briefing(user_id: str) -> None:
    from core.datetime_utils import now_utc
    today = now_utc().isoformat()[:10]

    for msg_type, check_fn in [("morning_briefing", _is_morning_window),
                                 ("evening_briefing", _is_evening_window)]:
        if not check_fn():
            continue
        conn = get_connection()
        try:
            existing = conn.execute(
                """SELECT COUNT(*) FROM orbit_proactive_messages
                   WHERE message_type = ? AND created_at LIKE ?
                   AND release_state IN ('sent','candidate','too_early')""",
                (msg_type, f"{today}%")
            ).fetchone()[0]
        finally:
            conn.close()
        if existing:
            continue
        # Auch suppressed/discarded prüfen -- kein zweiter Versuch heute
        conn2 = get_connection()
        try:
            already = conn2.execute(
                "SELECT COUNT(*) FROM orbit_proactive_messages WHERE message_type = ? AND created_at LIKE ?",
                (msg_type, f"{today}%")
            ).fetchone()[0]
        finally:
            conn2.close()
        if already:
            continue
        schedule_proactive_message(
            message_type=msg_type,
            primary_origin="orbit:briefing_scheduler",
            reason=f"Automatisches {msg_type} fuer {today}",
        )
        logger.info(f"Briefing-Kandidat angelegt: {msg_type}")


def check_proactive() -> None:
    """
    Prueft alle proaktiven Nachrichten-Kandidaten und sendet ggf.
    """
    from config import OWNER_ID

    _maybe_schedule_briefing(OWNER_ID)

    candidates = get_proactive_messages(release_state="candidate")
    too_early = get_proactive_messages(release_state="too_early")

    for msg in candidates + too_early:
        mid = msg["id"]
        decision = evaluate_proactive_candidate(mid)

        if decision == "send":
            msg_type = msg["message_type"]
            content = None

            # Briefings brauchen Ollama -- nicht im ORBIT-Tick generieren
            # (blockiert den Tick, konkurriert mit schnubot.service um Ressourcen)
            # Stattdessen suppressed -- Briefing wird über heartbeat/proactive.py versendet
            if msg_type in ("morning_briefing", "evening_briefing"):
                suppress_proactive_message(mid, "Briefing wird über heartbeat versendet")
                continue

            if not content:
                content = msg.get("reason", "")
            if not content:
                suppress_proactive_message(mid, "Kein Inhalt verfuegbar")
                continue

            send_proactive_message(mid, content)

        elif decision == "too_early":
            defer_proactive_message(mid, "Zeitfenster oder Chat aktiv", recheck_minutes=20)

        elif decision == "suppress":
            suppress_proactive_message(mid, "Schwelle nicht erreicht")

        elif decision == "discard":
            update_proactive_message(mid, release_state="discarded")


# =============================================================================
# Build Step 10 -- Recovery / Integritaet / Konfliktaufloesung
# =============================================================================

STALE_TASK_DAYS   = 7
STALE_THREAD_DAYS = 7
STALE_STEP_HOURS  = 2


def _recover_running_steps() -> list:
    from core.datetime_utils import now_utc
    from datetime import timedelta
    cutoff = (now_utc() - timedelta(hours=STALE_STEP_HOURS)).isoformat()
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT * FROM orbit_steps WHERE status = 'running' AND updated_at < ?",
            (cutoff,)
        ).fetchall()
    finally:
        conn.close()

    recovered = []
    for row in rows:
        step = dict(row)
        retry = step.get("retry_count", 0)
        if retry < 3:
            step_transition(step["id"], "ready",
                            reason=f"Recovery: running ohne Worker seit {STALE_STEP_HOURS}h")
            update_step(step["id"], retry_count=retry + 1)
            logger.info(f"Recovery: Step {step['id'][:8]} -> ready (retry {retry+1})")
        else:
            step_transition(step["id"], "failed", reason="Recovery: max. Retries erreicht")
            logger.warning(f"Recovery: Step {step['id'][:8]} -> failed (max retries)")
            if step.get("task_id"):
                update_task(step["task_id"], manual_attention=1)
        recovered.append(step["id"])
    return recovered


def _recover_orphaned_tasks() -> list:
    conn = get_connection()
    try:
        active_tasks = conn.execute(
            "SELECT * FROM orbit_tasks WHERE status = 'active'"
        ).fetchall()
    finally:
        conn.close()

    recovered = []
    for row in active_tasks:
        task = dict(row)
        steps = get_steps(task_id=task["id"])
        if not steps:
            continue
        all_terminal = all(s["status"] in ("done", "failed") for s in steps)
        any_failed = any(s["status"] == "failed" for s in steps)
        if all_terminal:
            new_status = "failed" if any_failed else "completed"
            task_transition(task["id"], new_status,
                            reason=f"Recovery: alle Steps terminal ({new_status})")
            set_task_hot(task["id"], False)
            recovered.append(task["id"])
            logger.info(f"Recovery: Task {task['id'][:8]} -> {new_status}")
    return recovered


def _mark_stale_objects() -> dict:
    from core.datetime_utils import now_utc
    from datetime import timedelta
    counts = {"threads": 0, "tasks": 0}

    thread_cutoff = (now_utc() - timedelta(days=STALE_THREAD_DAYS)).isoformat()
    task_cutoff = (now_utc() - timedelta(days=STALE_TASK_DAYS)).isoformat()

    conn = get_connection()
    try:
        stale_threads = conn.execute(
            """SELECT id FROM orbit_threads
               WHERE status = 'watching' AND stale = 0 AND updated_at < ?""",
            (thread_cutoff,)
        ).fetchall()
        for row in stale_threads:
            update_thread(row["id"], stale=1)
            counts["threads"] += 1

        stale_tasks = conn.execute(
            """SELECT id FROM orbit_tasks
               WHERE status IN ('planned', 'waiting') AND stale = 0 AND updated_at < ?""",
            (task_cutoff,)
        ).fetchall()
        for row in stale_tasks:
            update_task(row["id"], stale=1)
            create_review(
                review_type="integrity_review",
                target_ref=row["id"],
                reason=f"Task stale seit {STALE_TASK_DAYS} Tagen",
            )
            counts["tasks"] += 1
    finally:
        conn.close()

    return counts


def _detect_orphaned_objects() -> dict:
    conn = get_connection()
    counts = {"steps": 0, "threads": 0}
    try:
        active_steps = conn.execute(
            "SELECT * FROM orbit_steps WHERE status NOT IN ('done','failed') AND orphaned = 0"
        ).fetchall()
        for row in active_steps:
            step = dict(row)
            task = conn.execute(
                "SELECT status FROM orbit_tasks WHERE id = ?", (step["task_id"],)
            ).fetchone()
            if not task or task["status"] in ("completed", "failed", "aborted"):
                conn.execute(
                    "UPDATE orbit_steps SET orphaned = 1, updated_at = ? WHERE id = ?",
                    (to_iso(), step["id"])
                )
                counts["steps"] += 1
                logger.warning(f"Recovery: Step {step['id'][:8]} orphaned (Task fehlt/terminal)")

        linked_threads = conn.execute(
            """SELECT id, linked_task_id FROM orbit_threads
               WHERE linked_task_id IS NOT NULL AND orphaned = 0
               AND status NOT IN ('converted','merged','discarded')"""
        ).fetchall()
        for row in linked_threads:
            task = conn.execute(
                "SELECT id FROM orbit_tasks WHERE id = ?", (row["linked_task_id"],)
            ).fetchone()
            if not task:
                conn.execute(
                    "UPDATE orbit_threads SET orphaned = 1, updated_at = ? WHERE id = ?",
                    (to_iso(), row["id"])
                )
                counts["threads"] += 1

        conn.commit()
    finally:
        conn.close()
    return counts


def resolve_conflict(conflict_type: str, object_a: str, object_b: str,
                     reason: str = None) -> str:
    logger.info(f"Konflikt: {conflict_type} | {object_a[:8]} vs {object_b[:8]}")

    create_review(
        review_type="conflict_review",
        target_ref=object_a,
        reason=f"{conflict_type}: {object_a[:8]} vs {object_b[:8]}",
    )

    if conflict_type == "policy_conflict":
        conn = get_connection()
        try:
            a = conn.execute("SELECT rank FROM orbit_policies WHERE id=?", (object_a,)).fetchone()
            b = conn.execute("SELECT rank FROM orbit_policies WHERE id=?", (object_b,)).fetchone()
        finally:
            conn.close()
        if a and b:
            loser = object_b if (a["rank"] or 0) >= (b["rank"] or 0) else object_a
            suppress_policy(loser, reason=f"policy_conflict: unterlegener Rang")
            make_decision("conflict_resolved", object_a,
                          f"policy_conflict: {loser[:8]} suppressed", confidence=0.7)
            return "resolved"

    elif conflict_type == "communication_conflict":
        make_decision("conflict_resolved", object_a,
                      "communication_conflict: too_early", confidence=0.6,
                      alternative_rejected="send_now")
        return "resolved"

    elif conflict_type == "priority_conflict":
        make_decision("conflict_resolved", object_a,
                      "priority_conflict: critical vor allem", confidence=0.9)
        return "resolved"

    logger.warning(f"Konflikt {conflict_type} nicht autonom loesbar -> manual_attention")
    make_decision("conflict_manual_attention", object_a,
                  f"{conflict_type} nicht autonom loesbar: {reason or ''}",
                  confidence=0.2)
    for oid in (object_a, object_b):
        conn = get_connection()
        try:
            for table in ("orbit_tasks", "orbit_threads"):
                conn.execute(f"UPDATE {table} SET manual_attention = 1 WHERE id = ?", (oid,))
            conn.commit()
        finally:
            conn.close()
    return "manual_attention"


def raise_manual_attention(target_id: str, target_type: str, reason: str) -> None:
    conn = get_connection()
    try:
        table_map = {
            "task":   "orbit_tasks",
            "thread": "orbit_threads",
            "step":   "orbit_steps",
        }
        table = table_map.get(target_type)
        if table:
            conn.execute(f"UPDATE {table} SET manual_attention = 1 WHERE id = ?", (target_id,))
            conn.commit()
    finally:
        conn.close()
    audit("orbit", "manual_attention_raised", target_type, target_id, reason)
    logger.warning(f"manual_attention: {target_type} {target_id[:8]} -- {reason}")


def run_recovery() -> None:
    """Recovery-Lauf pro Tick (leichtgewichtig)."""
    try:
        recovered_steps = _recover_running_steps()
        orphaned = _detect_orphaned_objects()
        stale = _mark_stale_objects()
        recovered_tasks = _recover_orphaned_tasks()

        if any([recovered_steps, orphaned["steps"], orphaned["threads"],
                stale["threads"], stale["tasks"], recovered_tasks]):
            logger.info(
                f"Recovery: steps_recovered={len(recovered_steps)} "
                f"orphaned={orphaned} stale={stale} "
                f"tasks_recovered={len(recovered_tasks)}"
            )
    except Exception as e:
        logger.error(f"run_recovery Fehler: {e}", exc_info=True)


def full_recovery_on_start() -> None:
    """Vollstaendiger Recovery-Lauf beim ORBIT-Start."""
    report_id = create_recovery_report()
    actions = []
    manual_attention_count = 0
    error = None
    orphaned = {"steps": 0, "threads": 0}
    stale = {"threads": 0, "tasks": 0}
    recovered_steps = []

    try:
        logger.info("=== ORBIT Full Recovery Start ===")

        recovered_steps = _recover_running_steps()
        if recovered_steps:
            actions.append(f"steps_recovered:{len(recovered_steps)}")

        orphaned = _detect_orphaned_objects()
        if orphaned["steps"] or orphaned["threads"]:
            actions.append(f"orphaned_detected:{orphaned}")

        stale = _mark_stale_objects()
        if stale["threads"] or stale["tasks"]:
            actions.append(f"stale_marked:{stale}")

        recovered_tasks = _recover_orphaned_tasks()
        if recovered_tasks:
            actions.append(f"tasks_recovered:{len(recovered_tasks)}")

        conn = get_connection()
        try:
            manual_attention_count = conn.execute(
                """SELECT COUNT(*) FROM orbit_tasks WHERE manual_attention = 1
                   AND status NOT IN ('completed','failed','aborted')"""
            ).fetchone()[0]
        finally:
            conn.close()

        if manual_attention_count > 0:
            logger.warning(f"Recovery: {manual_attention_count} Objekte brauchen manual_attention")

        logger.info(f"=== ORBIT Full Recovery Ende: {actions} ===")

    except Exception as e:
        error = str(e)
        logger.error(f"Full Recovery Fehler: {e}", exc_info=True)

    finish_recovery_report(
        report_id,
        found_orphaned=orphaned.get("steps", 0) + orphaned.get("threads", 0),
        found_stale=stale.get("threads", 0) + stale.get("tasks", 0),
        found_running_no_worker=len(recovered_steps),
        actions_taken=actions,
        manual_attention_raised=manual_attention_count,
        error=error,
    )


# =============================================================================
# Tick & Main
# =============================================================================


def _run_maintenance() -> None:
    """
    Periodische Bereinigung von Datenmüll in ORBIT.
    Läuft alle ~30 Minuten im Tick.

    1. Threads: new/watching älter als 3 Tage ohne Hochstufung -> discarded
    2. Steps: ready-Steps deren Task completed/failed/aborted ist -> direkt abgebrochen
    3. Policies: Testdaten-Einträge (Begründung "Falsche Hard Policy", "A", leer) -> suppressed
    """
    from core.datetime_utils import now_utc
    from datetime import timedelta

    try:
        cutoff_threads = (now_utc() - timedelta(days=3)).isoformat()
        conn = get_connection()
        try:
            # 1. Alte weak/new Threads verwerfen
            old_threads = conn.execute(
                """SELECT id, topic_core FROM orbit_threads
                   WHERE status IN ('new', 'watching')
                   AND relevance = 'weak'
                   AND updated_at < ?""",
                (cutoff_threads,)
            ).fetchall()
        finally:
            conn.close()

        for row in old_threads:
            discard_thread(row["id"], reason="Maintenance: weak thread älter als 3 Tage ohne Hochstufung")
            logger.info(f"Maintenance: Thread {row['id'][:8]} verworfen ('{row['topic_core'][:40]}')")

        # 2. Verwaiste Steps (Task terminal, Step noch ready)
        conn = get_connection()
        try:
            orphan_steps = conn.execute(
                """SELECT s.id, s.task_id FROM orbit_steps s
                   JOIN orbit_tasks t ON s.task_id = t.id
                   WHERE s.status = 'ready'
                   AND t.status IN ('completed', 'failed', 'aborted')"""
            ).fetchall()
        finally:
            conn.close()

        if orphan_steps:
            conn = get_connection()
            try:
                for row in orphan_steps:
                    conn.execute(
                        "UPDATE orbit_steps SET status='done', updated_at=? WHERE id=?",
                        (to_iso(), row["id"])
                    )
                    logger.info(f"Maintenance: Step {row['id'][:8]} bereinigt (Task terminal)")
                conn.commit()
            finally:
                conn.close()

        # 3. Test-Policies bereinigen
        TEST_REASONS = {"falsche hard policy", "a", "", "test", "test hard auf falscher klasse"}
        conn = get_connection()
        try:
            active_policies = conn.execute(
                "SELECT id, reason FROM orbit_policies WHERE status = 'active'"
            ).fetchall()
        finally:
            conn.close()

        for row in active_policies:
            reason_lower = (row["reason"] or "").strip().lower()
            if reason_lower in TEST_REASONS:
                suppress_policy(row["id"], reason="Maintenance: Testdaten bereinigt")
                logger.info(f"Maintenance: Test-Policy {row['id'][:8]} suppressed ('{row['reason']}')")

    except Exception as e:
        logger.warning(f"Maintenance fehlgeschlagen: {e}")

def tick() -> None:
    if not ORBIT_ENABLED:
        logger.debug("ORBIT Not-Aus aktiv -- kein Tick")
        return

    if ORBIT_SOFT_PAUSE:
        logger.debug("ORBIT Soft-Pause -- beobachte, handle nicht")
        return

    try:
        events = collect_triggers()
        if events:
            logger.info(f"Tick: {len(events)} Trigger")
        process(events)
        run_scheduler()
        check_proactive()
        run_recovery()

        # Maintenance alle ~30 Minuten
        try:
            from core.datetime_utils import now_utc, safe_parse_dt
            from datetime import timedelta
            last_maint = runtime_get("last_maintenance_at") or ""
            last_dt = safe_parse_dt(last_maint) if last_maint else None
            if not last_dt or (now_utc() - last_dt).total_seconds() > 1800:
                _run_maintenance()
                runtime_set("last_maintenance_at", to_iso())
        except Exception as _me:
            logger.debug(f"Maintenance-Timer fehlgeschlagen (unkritisch): {_me}")

        # idle_pulse alle 20 Minuten -- Kimis durchgehendes Bewusstsein
        try:
            from core.datetime_utils import now_utc, safe_parse_dt
            last_idle = runtime_get("last_idle_pulse_at") or ""
            last_idle_dt = safe_parse_dt(last_idle) if last_idle else None
            if not last_idle_dt or (now_utc() - last_idle_dt).total_seconds() > 1200:
                from config import USER_CONTEXTS
                for uid in USER_CONTEXTS.keys():
                    create_trigger(
                        trigger_type="idle_pulse",
                        source="orbit_tick",
                        payload={"user_id": uid},
                    )
                runtime_set("last_idle_pulse_at", to_iso())
                logger.debug("idle_pulse Trigger erstellt")
        except Exception as _ip:
            logger.debug(f"idle_pulse-Timer fehlgeschlagen (unkritisch): {_ip}")
    except Exception as e:
        logger.error(f"Tick-Fehler: {e}", exc_info=True)


def main():
    from core.database import init_db
    init_db()

    logger.info("=== ORBIT gestartet ===")
    logger.info(f"Tick: {ORBIT_TICK_SECONDS}s | Not-Aus: {not ORBIT_ENABLED} | Soft-Pause: {ORBIT_SOFT_PAUSE}")

    runtime_set("orbit_started_at", to_iso())
    runtime_set("orbit_status", "running")

    full_recovery_on_start()

    while True:
        tick()
        # Unterbrechbarer Sleep -- vermeidet Hang in systemd-Kontext
        deadline = time.monotonic() + ORBIT_TICK_SECONDS
        while time.monotonic() < deadline:
            time.sleep(1)


if __name__ == "__main__":
    main()
