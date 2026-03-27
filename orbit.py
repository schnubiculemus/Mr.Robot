"""
SchnuBot.ai -- ORBIT V2

WP5: ORBIT ist technischer Executor, nicht Führungsinstanz.

Rolle in V2:
    Kimi Core führt.
    ORBIT dient als technischer Executor für explizite Ausführungsschritte.

Was ORBIT noch darf:
    - explizite Ausführungseinheiten (Tasks/Steps) abarbeiten
    - technische Restläufe (Recovery, Maintenance)
    - Kompatibilitätsschicht für Altpfade (temporary_compat)
    - Status und Fehler zurückmelden an Kimi Core

Was ORBIT nicht mehr darf:
    - autonome Arbeitslogik lostreten
    - Priorisierung von Linien/Tasks entscheiden
    - Dokumente/Artefakte aus eigenem Antrieb erzeugen
    - Trigger als Primärsteuerung
    - Führungsrolle neben oder über Kimi Core

Architekturebenen V2:
    Kimi Core --> Memory --> Workspace --> Tools
                        --> ORBIT (Executor, wenn delegiert)

Altbezeichnung "Autonome operative Exekutive" ist aufgehoben (WP5).
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
# =============================================================================
# WP0 – Safe Mode Feature Gates
# =============================================================================
SAFE_MODE = True                  # Globaler Safe-Mode-Schalter

ENABLE_MULTI_HOT_TASKS        = False   # WP0: max 1 heißer Task
ENABLE_RECOVERY_WORKSPACE_OUT = False   # WP0: Recovery schreibt nicht in Workspace
# WP8: ENABLE_IDLE_PULSE, ENABLE_AUTONOMOUS_COGNITION, ENABLE_AUTO_ARTIFACTS entfernt (delete_candidates physisch gelöscht)

MAX_HOT_TASKS = 1 if not ENABLE_MULTI_HOT_TASKS else 3  # WP0: max 1
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
               (id, trigger_type, source, payload, processed, processing, created_at)
               VALUES (?, ?, ?, ?, 0, 0, ?)""",
            (tid, trigger_type, source, _json(payload, "{}"), to_iso())
        )
        conn.commit()
    finally:
        conn.close()
    return tid


def get_pending_triggers() -> list:
    """7.5.3: Trigger priorisiert laden -- tool_result vor cognition vor idle_pulse."""
    try:
        from core.database import get_connection
        conn = get_connection()
        try:
            # 7.5.7: Nur IDs holen, noch nicht claimen -- claim-on-dispatch
            # LIMIT 1 pro Tick: kein Batch-Claiming mehr
            rows = conn.execute(
                """SELECT * FROM orbit_triggers WHERE processed = 0 AND processing = 0
                   ORDER BY
                     CASE trigger_type
                       WHEN 'tool_result'  THEN 1
                       WHEN 'cognition'    THEN 2
                       ELSE 3
                     END ASC,
                     created_at ASC
                   LIMIT 1"""
            ).fetchall()
            # Noch KEIN Claiming hier -- passiert in process() kurz vor Verarbeitung
            return [dict(r) for r in rows]
        finally:
            conn.close()
    except Exception as e:
        logger.warning(f"get_pending_triggers fehlgeschlagen: {e}")
        return []
    finally:
        conn.close()


def mark_trigger_processed(trigger_id: str, linked_object_id: str = None) -> None:
    """Markiert einen Trigger als verarbeitet."""
    conn = get_connection()
    try:
        conn.execute(
            """UPDATE orbit_triggers
               SET processed = 1, processed_at = ?, processing = 0, linked_object_id = ?
               WHERE id = ?""",
            (to_iso(), linked_object_id, trigger_id)
        )
        conn.commit()
    finally:
        conn.close()


# =============================================================================
# Threads (orbit_threads)
# =============================================================================

# WP5: temporary_compat -- Linien-Container der alten Autonomie (delete_candidate nach ORBIT-Rückbau)
# WP5: temporary_compat -- Thread-System (delete_candidate nach ORBIT-Rückbau)
# Threads waren Linien-Container der alten Autonomie. Nicht mehr neue Arbeitsstruktur.
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
_WRITE_TOOLS = {"workspace", "todos_write", "calendar_write", "proposal_write"}


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


# WP5: delete_candidate -- 7.4: Brief-Auto-Trigger
    payload = _parse(trigger.get("payload"), {})
    window = payload.get("window", "unknown")
    logger.debug(f"time_window: {window}")
    # Wiedervorlagen sind echter Nutzwert -- bleiben aktiv
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
    from config import OWNER_ID as _OWNER_ID
    user_id = payload.get("user_id", _OWNER_ID)

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
        # 7.5.2: Deadlock-Fix -- Task wieder aktivieren statt einfrieren
        # waiting_feedback + offene Steps = Scheduler skippt -> nichts laeuft weiter
        update_task(task_id, status="active")
        set_task_hot(task_id, True)
        logger.debug(f"_handle_tool_result: {len(open_steps)} offene Steps -- Task wieder active+hot")
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
                # WP10: set_last_error entfernt (legacy)
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
        import json as _jstep, re as _restep
        hint_lower = next_step_hint.lower()

        # 7.5.1: Artefakt-native Pfade -- vor Legacy-Fallbacks pruefen
        # Artefakt-Typ aus Hinweis ableiten
        _artifact_type = None
        if any(w in hint_lower for w in ["brief", "orientierungsrahmen", "einordnung", "grundlage", "einstieg"]):
            _artifact_type = "brief"
        elif any(w in hint_lower for w in ["analyse", "analysis", "mechanismen", "untersuchen", "strukturieren", "untersuchung"]):
            _artifact_type = "analysis"
        elif any(w in hint_lower for w in ["plan", "nächste schritte", "umsetzung", "umsetzungsskizze", "roadmap"]):
            _artifact_type = "plan"
        elif any(w in hint_lower for w in ["implementierung", "implementation", "umsetzung", "code", "bauen"]):
            _artifact_type = "implementation"
        elif any(w in hint_lower for w in ["ergebnis", "result", "fazit", "abschluss", "zusammenfassung"]):
            _artifact_type = "result"
        elif any(w in hint_lower for w in ["bericht", "report", "abschlussbericht"]):
            _artifact_type = "report"

        # line_id aus Task ableiten
        _line_id = None
        try:
            _task_obj = get_task(task_id)
            if _task_obj and _task_obj.get("linked_todo_id"):
                _line_id = f"todo:{_task_obj['linked_todo_id']}"
        except Exception:
            pass

        # 7.5.1: Lesen/Laden eines Artefakts
        if _artifact_type and _line_id and any(w in hint_lower for w in [
            "laden", "lesen", "öffnen", "fortsetzen", "weiter", "lad", "lies", "read"
        ]):
            tool_ref = "workspace"
            action = "artifact_read"
            description = _jstep.dumps({
                "action": "artifact_read",
                "line_id": _line_id,
                "artifact_type": _artifact_type,
            })

        # 7.5.1: Artefakt anlegen
        elif _artifact_type and _line_id and any(w in hint_lower for w in [
            "anlegen", "erstellen", "schreiben", "erzeugen", "neue", "neues", "erstell", "anleg"
        ]):
            tool_ref = "workspace"
            action = "artifact_create"
            description = _jstep.dumps({
                "action": "artifact_create",
                "line_id": _line_id,
                "artifact_type": _artifact_type,
                "format": "md",
                "purpose": "working_state",
                "content": f"# {_artifact_type.capitalize()}\n\n{next_step_hint[:300]}",
            })

        # 7.5.1: Artefakt aktualisieren
        elif _artifact_type and _line_id and any(w in hint_lower for w in [
            "ergänzen", "erweitern", "aktualisieren", "update", "fortschreiben", "hinzufügen",
            "vervollständigen", "befüllen", "ausformulieren", "weiter ausarbeiten",
            "fertig schreiben", "überarbeiten", "ausarbeiten", "fertigstellen"
        ]):
            tool_ref = "workspace"
            action = "artifact_update"
            description = _jstep.dumps({
                "action": "artifact_update",
                "line_id": _line_id,
                "artifact_type": _artifact_type,
                "content": next_step_hint[:300],
            })

        # 7.5.1: Artefakt-Liste der Linie lesen (statt generisches workspace.list)
        elif _line_id and any(w in hint_lower for w in [
            "datei", "prüfen", "check", "überblick", "liste", "was gibt es", "vorhanden"
        ]):
            tool_ref = "workspace"
            action = "artifact_list"
            description = _jstep.dumps({
                "action": "artifact_list",
                "line_id": _line_id,
            })

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
        elif any(w in hint_lower for w in ["proposal", "vorschlag", "genehmigen", "ablehnen"]):
            # 5.5: Proposal-Statusaenderung via Gate -- Proposal-ID aus Task-Kontext
            import json as _j55, re as _re55
            tool_ref = "proposal_write"
            action = "approve"
            if any(w in hint_lower for w in ["ablehnen", "reject"]):
                action = "reject"
            elif any(w in hint_lower for w in ["verschieben", "defer", "spaeter"]):
                action = "defer"

            # Proposal-ID aus Hinweistext extrahieren (#N oder "Proposal N")
            proposal_id = None
            m = _re55.search(r'(?:proposal|vorschlag)\s*[#:]?\s*(\d+)', next_step_hint.lower())
            if m:
                proposal_id = int(m.group(1))
            else:
                # Aus verknuepftem Todo ableiten
                try:
                    task_obj = get_task(task_id)
                    linked_todo_id = task_obj.get("linked_todo_id") if task_obj else None
                    if linked_todo_id:
                        from core.database import get_connection as _gc_p55
                        _cp55 = _gc_p55()
                        _row = _cp55.execute(
                            "SELECT proposal_id FROM todos WHERE id=?", (linked_todo_id,)
                        ).fetchone()
                        _cp55.close()
                        if _row and _row["proposal_id"]:
                            proposal_id = _row["proposal_id"]
                except Exception:
                    pass

            if proposal_id:
                description = _j55.dumps({"action": action, "id": proposal_id})
            else:
                # Kein Proposal-Bezug gefunden -- fallback auf Observation
                tool_ref = ""
                action = "observe"
                description = next_step_hint[:200]
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


# WP5: delete_candidate -- autonome Task-Anbahnung (ORBIT führt nicht mehr)
def _handle_cognition_output(trigger: dict) -> None:
    """WP8: temporary_compat — Safe Mode no-op. Thread-Logik entfernt."""
    if SAFE_MODE:
        logger.debug("WP5/WP8: _handle_cognition_output no-op im Safe Mode")
        return
    # WP8: Thread-System entfernt — cognition_output nur noch loggen
    payload = _parse(trigger.get("payload"), {})
    source = payload.get("source", "unknown")
    topic = payload.get("topic", "")
    logger.info(f"cognition_output empfangen von {source}: '{topic[:60]}' — kein Thread mehr (WP8)")


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


def collect_triggers() -> list:
    return get_pending_triggers()


# Trigger-Typen die in separatem Thread mit Timeout laufen muessen
# um den Tick-Loop nicht zu blockieren (ChromaDB, Ollama-Calls)
_ASYNC_TRIGGER_TYPES = {"tool_result"}  # WP8: idle_pulse + cognition entfernt
_ASYNC_TRIGGER_TIMEOUT = 90  # Sekunden

def _claim_trigger(trigger_id: str) -> bool:
    """7.5.7: Claim-on-dispatch -- Trigger kurz vor Verarbeitung claimen."""
    try:
        import datetime as _dt_c
        _now = _dt_c.datetime.now(_dt_c.timezone.utc).isoformat()
        from core.database import get_connection as _gc_c
        conn = _gc_c()
        try:
            cur = conn.execute(
                """UPDATE orbit_triggers SET processing=1, claimed_at=?
                   WHERE id=? AND processed=0 AND processing=0""",
                (_now, trigger_id)
            )
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()
    except Exception as _ce:
        logger.warning(f"_claim_trigger fehlgeschlagen: {_ce}")
        return False


def process(events: list) -> None:
    import threading as _threading
    for event in events:
        trigger_type = event.get("trigger_type", "unknown")
        trigger_id = event.get("id", "")
        handler = TRIGGER_HANDLERS.get(trigger_type)

        if handler:
            # 7.5.7: Claim-on-dispatch
            if not _claim_trigger(trigger_id):
                logger.debug(f"Trigger {trigger_id[:8]} bereits geclaimt -- skip")
                continue

            if trigger_type in _ASYNC_TRIGGER_TYPES:
                _fire_and_forget = False  # WP8: idle_pulse/cognition entfernt
                def _run(h=handler, e=event, tid=trigger_id, tt=trigger_type):
                    try:
                        h(e)
                        mark_trigger_processed(tid)
                    except Exception as _ex:
                        logger.error(f"Trigger-Handler {tt} fehlgeschlagen: {_ex}", exc_info=True)
                        mark_trigger_processed(tid)
                t = _threading.Thread(target=_run, daemon=True)
                t.start()
                if _fire_and_forget:
                    logger.debug(f"Trigger {trigger_type} fire-and-forget gestartet")
                else:
                    # 7.5.7: 15s Timeout fuer tool_result -- kurz genug um Tick nicht zu blockieren
                    t.join(timeout=15)
                    if t.is_alive():
                        logger.debug(f"Trigger {trigger_type} laeuft im Hintergrund (>15s)")
            else:
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
                # 7.4 + 7.5.8: Result + Report bei Abschluss
                # Recovery-Übergänge erzeugen KEINE sichtbaren result/report
                try:
                    linked = task_obj.get("linked_todo_id")
                    _owner = task_obj.get("owner_id","")
                    if not _owner:
                        from config import OWNER_ID
                        _owner = OWNER_ID
                    _line_id = f"todo:{linked}"
                    _goal = task_obj.get("goal","")
                    # WP8: _auto_trigger_result/_report entfernt (delete_candidate)
                    logger.debug(f"task_transition: kein auto-artifact fuer Task {task_id[:8]} (WP8)")
                except Exception:
                    pass

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

    # WP8: Thread-System entfernt — downgrade loggt nur noch
    logger.info(f"Task {task_id[:8]} downgraded: {reason} (WP8: kein Thread-System mehr)")
    return None


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

    # owner_id fuer _execute_step -- benoetigt von 7.4 Triggern
    try:
        from config import OWNER_ID as _OWNER_ID_ES
        _owner_id = _OWNER_ID_ES
        if task:
            origin = task.get("primary_origin", "")
            if origin.startswith("user:"):
                _owner_id = origin[5:]
    except Exception:
        _owner_id = ""

    # Tool ausfuehren
    result = execute_tool(
        tool_ref=tool_ref,
        action=action,
        params=params,
        task_id=task_id,
        step_id=step_id,
        owner_id=_owner_id,
    )

    if result["success"]:
        step_transition(step_id, "done", reason=f"Tool {tool_ref} erfolgreich")

        # WP8: _auto_trigger_analysis/_auto_trigger_plan entfernt

        # WP8: _auto_trigger_worklog_stagnation entfernt

        # 6.1 + 7.5: first_meaningful_execution tracken + Artefakt materialisieren
        if result.get("audit_id"):
            try:
                action_key = f"{tool_ref}.{action}"
                step_type = step.get("step_type", "") if step else ""
                from core.planner import record_meaningful_execution, MEANINGFUL_EXEC_TOOLS, MEANINGFUL_EXEC_STEPS
                is_meaningful = (action_key in MEANINGFUL_EXEC_TOOLS or step_type in MEANINGFUL_EXEC_STEPS)
                if is_meaningful:
                    t_obj = get_task(task_id) if task_id else None
                    linked_todo = t_obj.get("linked_todo_id") if t_obj else None
                    if linked_todo:
                        record_meaningful_execution(int(linked_todo), action_key, step_type)
                        # 7.5: Ergebnis als Execution-Artefakt -- ueber execute_write (5.x-Disziplin)
                        try:
                            result_text = str(result.get("result",""))[:2000]
                            if result_text and result_text.strip():
                                from core.gate_service import execute_write
                                line_id = f"todo:{linked_todo}"
                                mat_params = {
                                    "action": "materialize_execution",
                                    "line_id": line_id,
                                    "content": f"# Erster Vollzug\n\n**Aktion:** {action_key}\n\n**Ergebnis:**\n{result_text}",
                                    "format": "md",
                                }
                                def _do_materialize(p):
                                    # WP4: temporary_compat -- delete_candidate
                                    # materialize_execution ist Legacy. Im Safe Mode blockiert.
                                    try:
                                        from orbit import SAFE_MODE as _SM_MAT
                                    except Exception:
                                        _SM_MAT = False
                                    if _SM_MAT:
                                        logger.debug("WP4: materialize_execution im Safe Mode blockiert")
                                        return {"success": False, "error": "WP4: Legacy materialize_execution im Safe Mode deaktiviert"}
                                    from core.workspace_artifact_service import materialize_execution_artifact
                                    art = materialize_execution_artifact(
                                        owner_id=_owner_id,
                                        line_id=p["line_id"],
                                        content=p["content"],
                                        format=p.get("format","md"),
                                        task_id=task_id, step_id=step_id,
                                    )
                                    return {"success": bool(art),
                                            "result": f"Artifact #{art['id']}" if art else "Fehler",
                                            "artifact_id": art["id"] if art else None}
                                execute_write(
                                    "workspace.materialize_execution",
                                    mat_params, _owner_id, _do_materialize,
                                    task_id=task_id, step_id=step_id,
                                )
                        except Exception:
                            pass
            except Exception:
                pass

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
                from config import OWNER_ID as _OWNER_ID
                _user_id = _OWNER_ID
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
                from config import OWNER_ID as _OWNER_ID
                _user_id = _OWNER_ID
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


# WP5: temporary_compat -- Policy/Routine war ORBIT-Führungslogik (delete_candidate)
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


# WP5: temporary_compat -- Policy/Routine war ORBIT-Führungslogik (delete_candidate)
def suppress_policy(policy_id: str, reason: str) -> bool:
    update_policy(policy_id, status="suppressed", reason=reason)
    audit("orbit", "policy_suppressed", "policy", policy_id, reason)
    logger.info(f"Policy {policy_id[:8]} suppressed: {reason}")
    return True


# WP5: temporary_compat -- Policy-Retirement (delete_candidate)
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


# WP5: temporary_compat -- Policy/Routine war ORBIT-Führungslogik (delete_candidate)
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


# WP5: temporary_compat -- Policy/Routine war ORBIT-Führungslogik (delete_candidate)
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


# WP5: temporary_compat -- Policy/Routine war ORBIT-Führungslogik (delete_candidate)
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


# WP5: temporary_compat -- Routine-Retirement (delete_candidate)
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


# WP5: temporary_compat -- Policy/Routine war ORBIT-Führungslogik (delete_candidate)
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


# WP5: temporary_compat -- Policy/Routine war ORBIT-Führungslogik (delete_candidate)
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
    "proposal_write":     {"criticality": "kritisch",        "usage": ["write"],          "type": "intern",        "write_indirect": False},  # WP10: deaktiviert
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
            result = _dispatch_tool(tool_ref, action, params or {},
                                   _owner_id=_owner_id, task_id=task_id, step_id=step_id)
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


def _dispatch_tool(tool_ref: str, action: str, params: dict,
                   _owner_id: str = None, task_id: str = None, step_id: str = None) -> object:
    """
    Dispatcht einen Tool-Aufruf an die konkrete Implementierung.
    """
    # owner_id sicherstellen
    if _owner_id is None:
        try:
            from config import OWNER_ID
            _owner_id = OWNER_ID
        except Exception:
            _owner_id = ""
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


    # WP10: proposal_write deaktiviert — Proposals sind Vorschläge, keine ORBIT-Exekution
    if tool_ref == "proposal_write":
        return {"success": False,
                "error": "WP10: proposal_write ist deaktiviert. Proposals laufen über [WP10_PROPOSAL:] im Chat."}

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

        # 7.x: Semantische Artifact-Actions via Gate
        if action_type in ("artifact_create", "artifact_update", "worklog_append",
                           "materialize_execution", "artifact_delete"):
            from core.gate_service import execute_write

            action_key = f"workspace.{action_type}"

            def _do_artifact(p):
                # WP4: Safe Mode Gate -- neue normale Arbeit schreibt nicht in Legacy-Artefaktwelt
                # artifact_create / artifact_update / worklog_append / materialize_execution
                # sind Legacy-Pfade (temporary_compat). Im Safe Mode blockiert.
                try:
                    from orbit import SAFE_MODE as _SM_ART
                except Exception:
                    _SM_ART = False
                if _SM_ART:
                    import logging as _lart
                    _lart.getLogger(__name__).debug(
                        "WP4: Legacy-Artifact-Write im Safe Mode blockiert -- "
                        "neue normale Arbeit nutzt V2-Workspace"
                    )
                    return {"success": False,
                            "error": "WP4 Safe Mode: Legacy-Artifact-Writes deaktiviert. "
                                     "Neue Arbeit läuft über V2-Workspace (workspace_service.py)."}
                _at = p.get("action", action_type)
                if _at == "artifact_create":
                    from core.workspace_artifact_service import create_artifact
                    lid = p.get("line_id") or (f"todo:{task_id}" if task_id else "general")
                    art = create_artifact(
                        owner_id=_owner_id, line_id=lid,
                        artifact_type=p.get("artifact_type","analysis"),
                        format=p.get("format","md"),
                        content=p.get("content",""),
                        purpose=p.get("purpose","working_state"),
                        task_id=task_id, step_id=step_id,
                    )
                    if art:
                        return {"success": True, "result": f"Artifact #{art['id']} erstellt",
                                "artifact_id": art["id"]}
                    return {"success": False, "error": "Artifact-Erstellung fehlgeschlagen"}
                elif _at == "artifact_update":
                    from core.workspace_artifact_service import update_artifact_content, get_latest_line_artifact
                    # 7.5.3: Fallback -- artifact_id optional, line_id+artifact_type reichen
                    _art_id = p.get("artifact_id")
                    if not _art_id:
                        _lid_u = p.get("line_id") or (f"todo:{task_id}" if task_id else None)
                        _atype_u = p.get("artifact_type", "brief")
                        if _lid_u:
                            _latest = get_latest_line_artifact(_lid_u, _atype_u)
                            if _latest:
                                _art_id = _latest["id"]
                                logger.debug(f"artifact_update: Fallback auf #{_art_id} ({_atype_u}) fuer Linie {_lid_u}")
                    if not _art_id:
                        return {"success": False, "error": "artifact_update: keine artifact_id und kein Artefakt auf Linie gefunden"}
                    ok = update_artifact_content(
                        artifact_id=int(_art_id),
                        content=p.get("content",""), status=p.get("status"))
                    return {"success": ok, "result": "Artifact aktualisiert" if ok else "Fehler",
                            "artifact_id": _art_id}
                elif _at == "worklog_append":
                    from core.workspace_artifact_service import append_worklog_entry
                    lid = p.get("line_id") or (f"todo:{task_id}" if task_id else "general")
                    ok = append_worklog_entry(lid, p.get("content",""), task_id=task_id)
                    return {"success": ok, "result": "Worklog aktualisiert" if ok else "Fehler",
                            "artifact_id": None}
                elif _at == "materialize_execution":
                    from core.workspace_artifact_service import materialize_execution_artifact
                    lid = p.get("line_id") or (f"todo:{task_id}" if task_id else "general")
                    art = materialize_execution_artifact(
                        owner_id=_owner_id, line_id=lid,
                        content=p.get("content",""),
                        format=p.get("format","md"),
                        task_id=task_id, step_id=step_id,
                    )
                    return {"success": bool(art),
                            "result": f"Materialisiert: Artifact #{art['id']}" if art else "Fehler",
                            "artifact_id": art["id"] if art else None}
                elif _at == "artifact_delete":
                    from core.workspace_artifact_service import delete_artifact
                    ok = delete_artifact(int(p.get("artifact_id",0)))
                    return {"success": ok, "result": "Artifact geloescht" if ok else "Fehler",
                            "artifact_id": p.get("artifact_id")}
                return {"success": False, "error": f"Unbekannte Artifact-Aktion: {_at}"}

            write_params = dict(params)
            write_params["action"] = action_type
            gresult = execute_write(action_key, write_params, _owner_id, _do_artifact,
                                    task_id=task_id, step_id=step_id)
            return {"success": gresult["ok"],
                    "result": gresult.get("result") or gresult.get("error"),
                    "error": gresult.get("error"),
                    "artifact_id": (gresult.get("result") or {}).get("artifact_id") if isinstance(gresult.get("result"), dict) else None,
                    "audit_id": gresult.get("audit_id")}

        elif action_type == "artifact_read":
            from core.workspace_artifact_service import get_artifact, read_artifact_content
            art_id = params.get("artifact_id")
            if art_id:
                content_text = read_artifact_content(int(art_id))
                return {"success": bool(content_text),
                        "result": (content_text or "")[:5000]}
            # Letztes Artefakt der Linie
            line_id = params.get("line_id", "")
            from core.workspace_artifact_service import get_latest_line_artifact
            art = get_latest_line_artifact(line_id, params.get("artifact_type"))
            if not art:
                return {"success": False, "error": "Kein Artifact gefunden"}
            content_text = read_artifact_content(art["id"])
            return {"success": True, "result": (content_text or "")[:5000],
                    "artifact_id": art["id"]}

        elif action_type == "artifact_list":
            from core.workspace_artifact_service import list_line_artifacts, build_line_manifest
            line_id = params.get("line_id", "")
            manifest = build_line_manifest(line_id)
            return {"success": True, "result": str(manifest)[:2000], "manifest": manifest}

        elif action_type == "worklog_append":
            # WP4: temporary_compat -- delete_candidate
            # worklog_append ist Legacy-Schreibpfad. Im Safe Mode blockiert.
            try:
                from orbit import SAFE_MODE as _SM_WL
            except Exception:
                _SM_WL = False
            if _SM_WL:
                logger.debug("WP4: worklog_append im Safe Mode blockiert")
                return {"success": False,
                        "error": "WP4 Safe Mode: worklog_append ist Legacy -- kein V2-Schreibpfad"}
            from core.workspace_artifact_service import append_worklog_entry
            line_id = params.get("line_id") or (f"todo:{task_id}" if task_id else "general")
            ok = append_worklog_entry(line_id, params.get("content", ""),
                                       task_id=task_id)
            return {"success": ok, "result": "Worklog aktualisiert" if ok else "Fehler"}

        elif action_type == "materialize_execution":
            # WP4: temporary_compat -- delete_candidate
            # materialize_execution ist Legacy-Schreibpfad. Im Safe Mode blockiert.
            try:
                from orbit import SAFE_MODE as _SM_ME
            except Exception:
                _SM_ME = False
            if _SM_ME:
                logger.debug("WP4: materialize_execution im Safe Mode blockiert")
                return {"success": False,
                        "error": "WP4 Safe Mode: materialize_execution ist Legacy -- kein V2-Schreibpfad"}
            from core.workspace_artifact_service import materialize_execution_artifact
            line_id = params.get("line_id") or (f"todo:{task_id}" if task_id else "general")
            art = materialize_execution_artifact(
                owner_id=_owner_id, line_id=line_id,
                content=params.get("content", ""),
                format=params.get("format", "md"),
                task_id=task_id, step_id=step_id,
            )
            return {"success": bool(art),
                    "result": f"Execution materialisiert: Artifact #{art['id']}" if art else "Fehler",
                    "artifact_id": art["id"] if art else None}

        # Legacy-Aktionen: kein Gate fuer Lesen
        elif action_type == "list":
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
    WP0: im Safe Mode deaktiviert -- kein proaktives Altverhalten (temporary_compat)
    """
    if SAFE_MODE:
        logger.debug("WP0: check_proactive im Safe Mode deaktiviert")
        return
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
            # WP8: update_thread entfernt — direkt SQL
            conn.execute("UPDATE orbit_threads SET stale=1 WHERE id=?", (row["id"],))
            counts["threads"] += 1
        conn.commit()

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


# WP5-Cleanup: Recovery = technische Restfunktion, keine Systemintelligenz
def run_recovery() -> None:
    """Recovery-Lauf pro Tick (leichtgewichtig).
    WP0/WP5: temporary_compat -- technische Reparatur (Steps, Orphans, Stale).
    Kein Workspace-Output, kein Führungsrecht, kein Nutzpfad.
    Langfristig: nur auf Start-Recovery (full_recovery_on_start) reduzieren.
    """
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

        # 7.5.5: Stale-Trigger-Recovery -- geclaimte aber nie verarbeitete Trigger freigeben
        # Trigger die älter als 10 Minuten auf processing=1 stehen werden zurückgesetzt
        try:
            import datetime as _dt_rec
            _stale_cutoff = (_dt_rec.datetime.now(_dt_rec.timezone.utc)
                             - _dt_rec.timedelta(minutes=10)).isoformat()
            _conn_rec = get_connection()
            try:
                _stale = _conn_rec.execute(
                    """SELECT COUNT(*) FROM orbit_triggers
                       WHERE processed=0 AND processing=1
                       AND (claimed_at IS NULL OR claimed_at < ?)""",
                    (_stale_cutoff,)
                ).fetchone()[0]
                if _stale:
                    _conn_rec.execute(
                        """UPDATE orbit_triggers SET processing=0, claimed_at=NULL
                           WHERE processed=0 AND processing=1
                           AND (claimed_at IS NULL OR claimed_at < ?)""",
                        (_stale_cutoff,)
                    )
                    _conn_rec.commit()
                    logger.info(f"Recovery: {_stale} stale Trigger freigegeben (processing=0)")
                    actions.append(f"stale_triggers_released:{_stale}")
            finally:
                _conn_rec.close()
        except Exception as _rec_e:
            logger.warning(f"Recovery: Stale-Trigger-Reset fehlgeschlagen: {_rec_e}")

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

# WP5: technischer Infrastrukturpuls -- keine führende Arbeitslogik mehr
def tick() -> None:
    if not ORBIT_ENABLED:
        logger.debug("ORBIT Not-Aus aktiv -- kein Tick")
        return

    if ORBIT_SOFT_PAUSE:
        logger.debug("ORBIT Soft-Pause -- beobachte, handle nicht")
        return

    try:
        import time as _time_tick
        _t0 = _time_tick.monotonic()

        # 7.5.7: Scheduler ZUERST -- neue/heiße Tasks vor Trigger-Backlog
        run_scheduler()
        _t1 = _time_tick.monotonic()

        # Trigger holen (LIMIT 1 pro Tick -- kein Batch-Claiming)
        events = collect_triggers()
        if events:
            logger.info(f"Tick: {len(events)} Trigger")



        process(events)
        _t2 = _time_tick.monotonic()
        logger.debug(f"Tick: scheduler={_t1-_t0:.1f}s | triggers={_t2-_t1:.1f}s")

        # WP5: temporary_compat -- check_proactive() (Safe Mode gibt early return)
        check_proactive()
        # WP5: temporary_compat -- run_recovery() (technische Reparatur, kein Nutzpfad)
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

        # WP8: idle_pulse entfernt
    except Exception as e:
        logger.error(f"Tick-Fehler: {e}", exc_info=True)


def main():
    from core.database import init_db
    init_db()

    # 7.5.4: Migration -- processing-Spalte zu orbit_triggers hinzufügen falls nicht vorhanden
    try:
        from core.database import get_connection as _gc_mig
        _conn_mig = _gc_mig()
        try:
            _conn_mig.execute("ALTER TABLE orbit_triggers ADD COLUMN processing INTEGER NOT NULL DEFAULT 0")
            _conn_mig.commit()
            logger.info("Migration: orbit_triggers.processing Spalte hinzugefuegt")
        except Exception:
            pass
        try:
            _conn_mig.execute("ALTER TABLE orbit_triggers ADD COLUMN claimed_at TEXT")
            _conn_mig.commit()
            logger.info("Migration: orbit_triggers.claimed_at Spalte hinzugefuegt")
        except Exception:
            pass  # Spalte existiert bereits
        finally:
            _conn_mig.close()
    except Exception as _mig_e:
        logger.warning(f"Migration orbit_triggers fehlgeschlagen: {_mig_e}")

    # 7.5.8: Migration -- neue Felder in workspace_artifacts
    try:
        from core.database import get_connection as _gc_758
        _c758 = _gc_758()
        try:
            for _col, _default in [
                ("visibility_class", "'workspace'"),
                ("quality_state", "'draft'"),
                ("content_origin", "'manual'"),
                ("quality_notes", "NULL"),
            ]:
                try:
                    _c758.execute(f"ALTER TABLE workspace_artifacts ADD COLUMN {_col} TEXT DEFAULT {_default}")
                    _c758.commit()
                    logger.info(f"Migration: workspace_artifacts.{_col} hinzugefuegt")
                except Exception:
                    pass  # Spalte existiert bereits
        finally:
            _c758.close()
    except Exception as _m758:
        logger.warning(f"Migration 7.5.8 fehlgeschlagen: {_m758}")

    logger.info("=== ORBIT gestartet ===")

    # WP0: Safe Mode Status loggen + Runtime bereinigen
    if SAFE_MODE:
        logger.info("WP0: SAFE MODE aktiv")
        logger.info(f"WP0: Safe Mode aktiv | multi_hot={ENABLE_MULTI_HOT_TASKS}")
        # Überzählige heiße Tasks kalt stellen (max 1)
        try:
            from core.database import get_connection as _gc_wp0
            _c_wp0 = _gc_wp0()
            try:
                hot_tasks = _c_wp0.execute(
                    "SELECT id FROM orbit_tasks WHERE hot=1 AND status NOT IN ('completed','failed','aborted') ORDER BY updated_at DESC"
                ).fetchall()
                if len(hot_tasks) > 1:
                    for row in hot_tasks[1:]:
                        _c_wp0.execute("UPDATE orbit_tasks SET hot=0 WHERE id=?", (row["id"],))
                    _c_wp0.commit()
                    logger.info(f"WP0: {len(hot_tasks)-1} überzählige heiße Tasks kalt gestellt")
            finally:
                _c_wp0.close()
        except Exception as _wp0e:
            logger.warning(f"WP0: Runtime-Bereinigung fehlgeschlagen: {_wp0e}")
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
