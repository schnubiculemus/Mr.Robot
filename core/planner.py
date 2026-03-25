"""
core/planner.py — LEGACY_COMPAT / TEMPORARY_COMPAT (WP8)

Status: temporary_compat — Führungslogik aus der Altarchitektur.
Nicht mehr der Hauptpfad. Wird noch referenziert von:
  - orbit.py _maybe_run_planner (selbst temporary_compat, WP5)
  - dashboard.py (Planner-Ansicht)
  - orbit_cognition.py

Delete-Kandidaten in diesem Modul:
  score_candidates()   — Führungslogik, kein V2-Pfad
  choose_worklines()   — Führungslogik, kein V2-Pfad
  should_replan()      — Führungslogik, kein V2-Pfad
  maybe_start_task()   — Bootstrap-Steps, ORBIT-nah

V2-Hauptpfad: Kimi Core + AWC (active_working_context.py)

--- Originaldokumentation ---
core/planner.py -- Kimi Planner V3

V3 vs V2x:
- Fokus als echte operative Entitaet (persistent in DB)
- Replan-Regeln statt Dauer-Neubewertung
- Blocker-Klassifizierung: hard/soft/waiting/needs_unblock
- Stagnationserkennung (loop_count, same_error, no_progress)
- Proposal als echte Planungsinstanz (5 Klassen)
- Arbeitslinien statt Einzelobjekte
- Entscheidungen werden erklaert und historisch gespeichert
- Dashboard-sichtbar
"""
import logging
import json
from core.datetime_utils import to_iso

logger = logging.getLogger(__name__)

MAX_INTERNAL_TASKS = 2
STAGNATION_LOOP_THRESHOLD = 3      # ab wann gilt eine Linie als stagnierende
STAGNATION_DAYS_THRESHOLD = 2.0    # Tage ohne Fortschritt
REPLAN_AFTER_HOURS_DEFAULT = 4     # Stunden bis naechster Replan-Check

# Entscheidungsklassen
CONTINUE_LINE  = "continue_line"
UNBLOCK_LINE   = "unblock_line"
START_LINE     = "start_line"
DEFER          = "defer"
DROP_OR_PAUSE  = "drop_or_pause"

# Blocker-Typen
BLOCKER_HARD              = "hard_blocked"
BLOCKER_SOFT              = "soft_blocked"
BLOCKER_WAITING_FEEDBACK  = "waiting_for_internal_feedback"
BLOCKER_WAITING_EXTERNAL  = "waiting_for_external_condition"
BLOCKER_NEEDS_UNBLOCK     = "needs_unblock_line"

# Neue Entscheidungsklassen 4.x
PROPOSAL_AS_LINE   = "proposal_as_line"    # Proposal ist aktive Hauptlinie
NOT_NOW            = "not_now"             # bewusste Nicht-Auswahl

# Waiting-Klassen 4.x (feinere Differenzierung)
BLOCKER_WAITING_USER      = "waiting_user_decision"
BLOCKER_WAITING_RETRY     = "waiting_retry_window"
BLOCKER_WAITING_FOLLOWUP  = "waiting_internal_followup"

# Fokusbindung -- Mindeststunden bevor Fokus gewechselt wird
FOCUS_MIN_HOURS = 1.5

# 6.x Konstanten
W_EXECUTION_PRESSURE      = 3.0   # Bonus fuer Linien unter Umsetzungsdruck
META_CYCLE_THRESHOLD      = 3     # Anzahl Meta-Zyklen bevor Gate aktiv
META_CYCLE_HARD_LIMIT     = 6     # Ab hier: execution_required -- erzwungener Vollzug-Bias
MEANINGFUL_EXEC_TOOLS  = {        # Tools die als first_meaningful_execution zaehlen
    "workspace.save", "workspace.delete",
    "todos.create", "todos.complete",
    "proposal.approve", "proposal.reject",
    "calendar.write", "calendar.change",
}

# Score-Gewichte
W_CONTINUITY        = 6.0
W_PROGRESS_NEAR     = 3.0
W_GOAL_RELEVANCE    = 3.0
W_ACTIONABLE        = 2.5
W_LEVERAGE          = 2.0
W_STALENESS         = 1.5
W_EFFORT_PENALTY    = 1.0
W_LOOP_PENALTY      = 2.5
W_STAGNATION_PENALTY= 3.0


# =============================================================================
# Persistenter Planner-State
# =============================================================================

def get_planner_focus(owner_id: str) -> dict | None:
    """Liest aktuellen Fokus aus DB."""
    try:
        from core.database import get_connection
        conn = get_connection()
        try:
            row = conn.execute(
                "SELECT * FROM planner_state WHERE owner_id=? ORDER BY updated_at DESC LIMIT 1",
                (owner_id,)
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()
    except Exception as e:
        logger.debug(f"get_planner_focus fehlgeschlagen: {e}")
        return None


def save_planner_focus(owner_id: str, primary_type: str, primary_id: int,
                       secondary_type: str = None, secondary_id: int = None,
                       reason: str = "", confidence: float = 1.0,
                       replan_after_hours: int = REPLAN_AFTER_HOURS_DEFAULT,
                       paused: bool = False) -> None:
    """Speichert oder aktualisiert Planner-Fokus."""
    try:
        from core.database import get_connection
        import datetime
        now = to_iso()
        replan_dt = (datetime.datetime.now(datetime.timezone.utc)
                     + datetime.timedelta(hours=replan_after_hours))
        replan_after = replan_dt.isoformat()
        status = "paused" if paused else "active"

        conn = get_connection()
        try:
            existing = conn.execute(
                "SELECT id, focus_since, primary_line_id, primary_line_type FROM planner_state WHERE owner_id=?",
                (owner_id,)
            ).fetchone()

            # Fokusdauer berechnen
            same_focus = (existing and
                          existing["primary_line_id"] == primary_id and
                          existing["primary_line_type"] == primary_type)
            focus_since = existing["focus_since"] if same_focus else now

            if existing:
                conn.execute(
                    """UPDATE planner_state SET
                       primary_line_type=?, primary_line_id=?,
                       secondary_line_type=?, secondary_line_id=?,
                       focus_since=?, focus_reason=?, replan_after=?,
                       focus_confidence=?, status=?, updated_at=?
                       WHERE owner_id=?""",
                    (primary_type, primary_id, secondary_type, secondary_id,
                     focus_since, reason[:300], replan_after,
                     confidence, status, now, owner_id)
                )
            else:
                conn.execute(
                    """INSERT INTO planner_state
                       (owner_id, primary_line_type, primary_line_id,
                        secondary_line_type, secondary_line_id,
                        focus_since, focus_reason, replan_after,
                        focus_confidence, status, updated_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                    (owner_id, primary_type, primary_id,
                     secondary_type, secondary_id,
                     focus_since, reason[:300], replan_after,
                     confidence, status, now)
                )
            conn.commit()
            logger.debug(f"Planner-Fokus: {primary_type}#{primary_id} "
                        f"({'gleich' if same_focus else 'neu'}, seit {focus_since[:10]}, "
                        f"confidence={confidence})")
        finally:
            conn.close()
    except Exception as e:
        logger.warning(f"save_planner_focus fehlgeschlagen: {e}")


def get_focus_duration_hours(owner_id: str) -> float:
    """Gibt an wie lange der aktuelle Fokus schon haelt (in Stunden)."""
    focus = get_planner_focus(owner_id)
    if not focus or not focus.get("focus_since"):
        return 0.0
    import datetime
    try:
        dt = datetime.datetime.fromisoformat(focus["focus_since"].replace("Z","+00:00"))
        return (datetime.datetime.now(datetime.timezone.utc) - dt).total_seconds() / 3600
    except Exception:
        return 0.0


def pause_focus(owner_id: str, reason: str = "") -> None:
    """Setzt Fokus auf pausiert -- Linie bewusst geparkt."""
    focus = get_planner_focus(owner_id)
    if focus:
        save_planner_focus(
            owner_id=owner_id,
            primary_type=focus.get("primary_line_type","todo"),
            primary_id=focus.get("primary_line_id",0),
            reason=f"Pausiert: {reason}",
            paused=True,
        )
        logger.info(f"Planner-Fokus pausiert: {reason}")


def get_line_stats(owner_id: str, line_id: int, line_type: str = "todo") -> dict:
    """
    4.4: Verlaufs-Stats fuer eine Arbeitslinie.
    Wie oft gestartet, wie oft stagniert, wie oft erfolgreich.
    """
    try:
        from core.database import get_connection
        conn = get_connection()
        try:
            rows = conn.execute(
                """SELECT action, decided_at FROM planner_decisions
                   WHERE owner_id=? AND primary_line_id=? AND primary_line_type=?
                   ORDER BY decided_at DESC LIMIT 20""",
                (owner_id, line_id, line_type)
            ).fetchall()
            decisions = [dict(r) for r in rows]

            stagnation_count = sum(1 for d in decisions if "stagniert" in (d.get("action") or ""))
            started_count = sum(1 for d in decisions if d.get("action") in (START_LINE, CONTINUE_LINE))
            return {
                "total_decisions": len(decisions),
                "started_count":   started_count,
                "stagnation_count": stagnation_count,
                "repeated_stagnation": stagnation_count >= 2,
            }
        finally:
            conn.close()
    except Exception:
        return {"total_decisions": 0, "started_count": 0, "stagnation_count": 0, "repeated_stagnation": False}


def record_decision(owner_id: str, action: str, primary_type: str = None,
                    primary_id: int = None, secondary_type: str = None,
                    secondary_id: int = None, reason: str = "",
                    replan_trigger: str = None,
                    deferred_ids: list = None, blocked_ids: list = None,
                    stagnation_flags: list = None) -> None:
    """Speichert Planner-Entscheidung historisch."""
    try:
        from core.database import get_connection
        conn = get_connection()
        try:
            conn.execute(
                """INSERT INTO planner_decisions
                   (owner_id, action, primary_line_type, primary_line_id,
                    secondary_line_type, secondary_line_id,
                    decision_reason, replan_trigger,
                    deferred_ids, blocked_ids, stagnation_flags, decided_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (owner_id, action, primary_type, primary_id,
                 secondary_type, secondary_id,
                 reason[:500], replan_trigger,
                 json.dumps(deferred_ids or []),
                 json.dumps(blocked_ids or []),
                 json.dumps(stagnation_flags or []),
                 to_iso())
            )
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        logger.debug(f"record_decision fehlgeschlagen: {e}")


# =============================================================================
# 1. Candidates sammeln
# =============================================================================

def collect_candidates(owner_id: str) -> dict:
    from core.database import get_connection
    conn = get_connection()
    try:
        goals = [dict(r) for r in conn.execute(
            "SELECT * FROM kimi_goals WHERE owner_id=? AND status='active' ORDER BY priority DESC, created_at DESC",
            (owner_id,)
        ).fetchall()]

        proposals = [dict(r) for r in conn.execute(
            "SELECT * FROM kimi_proposals WHERE owner_id=? AND status='pending' ORDER BY created_at ASC",
            (owner_id,)
        ).fetchall()]

        todos = [dict(r) for r in conn.execute(
            """SELECT * FROM todos
               WHERE user_id=? AND status IN ('open','in_progress','blocked')
               AND project='kimi'
               ORDER BY priority DESC, created_at ASC""",
            (owner_id,)
        ).fetchall()]

        # Owner-bezogene aktive Tasks
        _all_tasks = [dict(r) for r in conn.execute(
            """SELECT * FROM orbit_tasks
               WHERE mode IN ('internal','background')
               AND status NOT IN ('completed','failed','aborted')
               ORDER BY created_at DESC"""
        ).fetchall()]
        _owner_todo_ids = {
            row["id"] for row in conn.execute(
                "SELECT id FROM todos WHERE user_id=?", (owner_id,)
            ).fetchall()
        }
        active_tasks = [
            t for t in _all_tasks
            if t.get("primary_origin","").startswith(f"user:{owner_id}")
            or t.get("linked_todo_id") in _owner_todo_ids
        ]

        # Letzte Observations pro Task
        recent_obs = {}
        if active_tasks:
            task_ids = [t["id"] for t in active_tasks]
            placeholders = ",".join("?" * len(task_ids))
            rows = conn.execute(
                f"""SELECT task_id, type, content FROM kimi_observations
                    WHERE task_id IN ({placeholders})
                    ORDER BY created_at DESC""",
                task_ids
            ).fetchall()
            for r in rows:
                tid = r["task_id"]
                if tid not in recent_obs:
                    recent_obs[tid] = []
                if len(recent_obs[tid]) < 10:
                    recent_obs[tid].append({"type": r["type"], "content": r["content"]})

        # 7.5: Workspace-Artefaktstand pro Todo lesen -- inkl. Artefakttypen
        workspace_states = {}
        try:
            ws_rows = conn.execute(
                "SELECT line_id, latest_artifact_id, latest_materialized_artifact_id, last_workspace_write_at, artifact_count FROM line_workspace_state"
            ).fetchall()
            for ws in ws_rows:
                workspace_states[ws["line_id"]] = dict(ws)

            # Artefakt-Typen pro Linie ermitteln (welche Typen existieren aktiv)
            art_rows = conn.execute(
                "SELECT line_id, artifact_type, status FROM workspace_artifacts WHERE status IN ('active','final')"
            ).fetchall()
            for ar in art_rows:
                lid = ar["line_id"]
                if lid not in workspace_states:
                    workspace_states[lid] = {}
                types = workspace_states[lid].get("artifact_types", set())
                types.add(ar["artifact_type"])
                workspace_states[lid]["artifact_types"] = types

            # Letzten aktiven Artefakttyp pro Linie -- sauberer Subquery-Ansatz
            latest_rows = conn.execute(
                """SELECT wa.line_id, wa.artifact_type
                   FROM workspace_artifacts wa
                   INNER JOIN (
                       SELECT line_id, MAX(created_at) as max_ts
                       FROM workspace_artifacts
                       WHERE status='active'
                       GROUP BY line_id
                   ) latest ON wa.line_id = latest.line_id AND wa.created_at = latest.max_ts
                   WHERE wa.status='active'"""
            ).fetchall()
            for lr in latest_rows:
                lid = lr["line_id"]
                if lid not in workspace_states:
                    workspace_states[lid] = {}
                workspace_states[lid]["latest_active_type"] = lr["artifact_type"]
        except Exception:
            pass

        # 5.4: Offene Write-Requests als Linien
        write_request_lines = [dict(r) for r in conn.execute(
            """SELECT id, action_key, approval_status, origin_todo_id, line_status,
                      after_approve_action, after_reject_action, task_id, deferred_until,
                      preview_text, reason, created_at
               FROM write_requests
               WHERE owner_id=? AND approval_status IN ('pending','deferred')
               ORDER BY created_at DESC LIMIT 10""",
            (owner_id,)
        ).fetchall()]

        return {
            "goals":               goals,
            "proposals":           proposals,
            "todos":               todos,
            "active_tasks":        active_tasks,
            "recent_obs":          recent_obs,
            "write_request_lines": write_request_lines,
            "workspace_states":    workspace_states,
        }
    finally:
        conn.close()


# =============================================================================
# 2. Operatives Lagebild
# =============================================================================

def _build_situation(candidates: dict) -> dict:
    # workspace_states direkt aus candidates weitergeben
    if "workspace_states" not in candidates:
        candidates["workspace_states"] = {}
    active_task_by_todo = {}
    for t in candidates["active_tasks"]:
        if t.get("linked_todo_id"):
            active_task_by_todo[t["linked_todo_id"]] = t

    running_todos   = []
    waiting_todos   = []
    blocked_todos   = []
    startable_todos = []
    low_value_todos = []

    for todo in candidates["todos"]:
        tid = todo["id"]
        task = active_task_by_todo.get(tid)
        todo["_task"] = task

        if todo.get("status") == "blocked":
            blocked_todos.append(todo)
        elif task:
            ts = task.get("status","")
            if ts in ("active","new","planned"):
                running_todos.append(todo)
            elif ts == "waiting_user_decision":
                # 5.3: explizit wartet auf Freigabe -- eigene Kategorie
                todo["_wait_type"] = BLOCKER_WAITING_USER
                todo["_approval_pending"] = True
                waiting_todos.append(todo)
            elif ts == "waiting_feedback":
                todo["_wait_type"] = BLOCKER_WAITING_FEEDBACK
                waiting_todos.append(todo)
            elif ts == "waiting":
                loop = int(task.get("loop_count") or 0)
                max_l = int(task.get("max_loops") or 5)
                if loop >= max_l - 1:
                    todo["_wait_type"] = "soft_wait_near_limit"
                    blocked_todos.append(todo)
                else:
                    todo["_wait_type"] = BLOCKER_WAITING_EXTERNAL
                    waiting_todos.append(todo)
            elif ts in ("failed","aborted"):
                todo["_task"] = None
                if todo.get("execution_mode") in ("orbit_internal","orbit_chat"):
                    startable_todos.append(todo)
                else:
                    low_value_todos.append(todo)
            else:
                startable_todos.append(todo)
        elif todo.get("execution_mode") in ("orbit_internal","orbit_chat"):
            startable_todos.append(todo)
        else:
            low_value_todos.append(todo)

    return {
        "running":      running_todos,
        "waiting":      waiting_todos,
        "blocked":      blocked_todos,
        "startable":    startable_todos,
        "low_value":    low_value_todos,
        "task_by_todo": active_task_by_todo,
    }


# =============================================================================
# 3. Stagnationserkennung
# =============================================================================

def _detect_stagnation(todo: dict, task: dict | None,
                        recent_obs: dict) -> dict:
    """
    Erkennt ob eine Linie stagniert.
    Gibt {'stagnating': bool, 'reason': str, 'score': float} zurueck.
    """
    if not task:
        return {"stagnating": False, "reason": "", "score": 0.0}

    task_id = task["id"]
    obs = recent_obs.get(task_id, [])
    loop_count = int(task.get("loop_count") or 0)
    score = 0.0
    reasons = []

    # Schleifenzaehler
    if loop_count >= STAGNATION_LOOP_THRESHOLD:
        score += 2.0
        reasons.append(f"loop_count={loop_count}")

    # Wiederholte Fehler
    error_obs = [o for o in obs if o["type"] in ("error","blocker")]
    if len(error_obs) >= 2:
        score += 1.5
        reasons.append(f"{len(error_obs)} Fehler-Observations")

    # Gleiche Step-Muster (gleiche Content wiederholt)
    contents = [o["content"][:50] for o in obs if o["type"] == "tool_result"]
    if len(contents) >= 3 and len(set(contents)) <= 1:
        score += 2.0
        reasons.append("gleiche Step-Ergebnisse wiederholt")

    # Kein Fortschritt (waiting_feedback zu lange)
    if task.get("status") == "waiting_feedback":
        import datetime
        updated = task.get("updated_at","")
        if updated:
            try:
                dt = datetime.datetime.fromisoformat(updated.replace("Z","+00:00"))
                hours = (datetime.datetime.now(datetime.timezone.utc) - dt).total_seconds() / 3600
                if hours > 2:
                    score += 1.0
                    reasons.append(f"waiting_feedback seit {hours:.1f}h")
            except Exception:
                pass

    return {
        "stagnating": score >= 2.0,
        "reason": " | ".join(reasons),
        "score": score,
    }


# =============================================================================
# 4. Blocker klassifizieren
# =============================================================================

def _classify_blocker(todo: dict, task: dict | None,
                       recent_obs: dict) -> str:
    """
    4.2: Erweiterter Blocker-Klassifizierer.
    Unterscheidet: hard / soft / waiting_feedback / waiting_external /
                   waiting_user / waiting_retry / waiting_followup
    """
    wait_type = todo.get("_wait_type","")

    if wait_type == BLOCKER_WAITING_FEEDBACK:
        return BLOCKER_WAITING_FEEDBACK

    if wait_type == BLOCKER_WAITING_EXTERNAL:
        # Genauer unterscheiden
        if task:
            obs = recent_obs.get(task["id"], [])
            # Mehrfach gleicher Fehler → retry_window
            error_contents = [o["content"][:60] for o in obs if o["type"] == "error"]
            if len(error_contents) >= 2 and len(set(error_contents)) <= 1:
                return BLOCKER_WAITING_RETRY
            # Reflexion vorhanden → followup
            if any(o["type"] == "reflection" for o in obs):
                return BLOCKER_WAITING_FOLLOWUP
        return BLOCKER_WAITING_EXTERNAL

    if task:
        obs = recent_obs.get(task["id"], [])
        for o in obs:
            c = o.get("content","").lower()
            if "permission" in c or "zugriff" in c or "access denied" in c:
                return BLOCKER_HARD
            if "tommy" in c or "entscheidung" in c or "freigabe" in c:
                return BLOCKER_WAITING_USER
            if "nicht gefunden" in c or "missing" in c or "fehlt" in c:
                return BLOCKER_SOFT

    if todo.get("status") == "blocked":
        return BLOCKER_NEEDS_UNBLOCK

    return BLOCKER_SOFT


def _blocker_is_plannable(blocker_type: str) -> bool:
    """
    4.2: Entscheidet ob ein Blocker-Typ planerisch aktiv behandelt werden soll.
    waiting_feedback + waiting_followup → continue_line
    waiting_retry + waiting_external → bewusst warten
    waiting_user → melden, nicht weiter pushen
    hard → nicht anfassen
    """
    return blocker_type in (
        BLOCKER_WAITING_FEEDBACK,
        BLOCKER_WAITING_FOLLOWUP,
        BLOCKER_SOFT,
        BLOCKER_NEEDS_UNBLOCK,
    )


# =============================================================================
# 5. Proposal-Klassifizierung (5 Klassen)
# =============================================================================

def _classify_proposal(p: dict, active_goal_ids: set,
                        active_task_ids: set) -> str:
    # 5.5: bereits entschiedene Proposals nicht mehr aktiv planen
    status = p.get("status", "pending")
    if status in ("approved", "rejected"):
        return "proposal_low_value"  # bereits entschieden
    if status == "deferred":
        return "proposal_waiting_approval"  # bewusst verschoben
    """
    5 Proposal-Klassen:
    proposal_ready_for_work        -- konkret, Zielbezug, kleiner Aufwand
    proposal_waiting_approval      -- approved_todo_id schon gesetzt aber wartet
    proposal_needs_groundwork      -- Zielbezug aber noch konzeptionell
    proposal_depends_on_other_line -- haengt von laufender Arbeit ab
    proposal_low_value             -- kein Bezug, geringer Nutzen
    """
    has_desc = bool(p.get("description") and len(p.get("description","")) > 20)
    has_reason = bool(p.get("reason") and len(p.get("reason","")) > 10)
    is_concrete = has_desc or has_reason
    goal_relevant = bool(p.get("goal_id") and p["goal_id"] in active_goal_ids)
    effort_map = {"klein": 0, "mittel": 1, "gross": 2, "groß": 2}
    effort = effort_map.get((p.get("effort") or "mittel").lower(), 1)

    if p.get("approved_todo_id"):
        return "proposal_waiting_approval"

    if goal_relevant and is_concrete and effort <= 1:
        return "proposal_ready_for_work"

    if goal_relevant and not is_concrete:
        return "proposal_needs_groundwork"

    if goal_relevant and effort > 1:
        return "proposal_depends_on_other_line"

    return "proposal_low_value"


# =============================================================================
# 6. Scoring
# =============================================================================

def _staleness_days(obj: dict) -> float:
    import datetime
    ts = obj.get("status_updated_at") or obj.get("updated_at") or obj.get("created_at") or ""
    if not ts:
        return 0.0
    try:
        dt = datetime.datetime.fromisoformat(ts.replace("Z","+00:00"))
        now = datetime.datetime.now(datetime.timezone.utc)
        return max(0.0, (now - dt).total_seconds() / 86400)
    except Exception:
        return 0.0


# WP5: temporary_compat -- Scoring-/Priorisierungslogik (ORBIT-Führung)
def score_candidates(candidates: dict, situation: dict, owner_id: str = "") -> list:
    active_goal_ids = {g["id"] for g in candidates["goals"]}
    active_task_ids = {t["id"] for t in candidates["active_tasks"]}
    recent_obs = candidates.get("recent_obs", {})
    scored = []

    def _score_todo(todo: dict, base_decision: str) -> dict:
        task = todo.get("_task")
        details = {}

        # Stagnation
        stag = _detect_stagnation(todo, task, recent_obs)
        stag_penalty = W_STAGNATION_PENALTY if stag["stagnating"] else 0.0
        details["stagnation"] = stag

        # Kontinuitaet
        continuity = W_CONTINUITY if base_decision == CONTINUE_LINE else 0.0
        details["continuity"] = continuity

        # Progress-Naehe
        prog_near = 0.0
        if task and task.get("status") == "waiting_feedback":
            prog_near = W_PROGRESS_NEAR * 0.5
        if task:
            loop = int(task.get("loop_count",0))
            max_l = int(task.get("max_loops",5))
            if max_l > 0 and loop / max_l > 0.6:
                prog_near += W_PROGRESS_NEAR * 0.3
        details["progress_near"] = prog_near

        # Goal-Relevanz
        goal_rel = W_GOAL_RELEVANCE if todo.get("goal_id") and todo["goal_id"] in active_goal_ids else 0.0
        details["goal_relevance"] = goal_rel

        # Bearbeitbarkeit
        actionable_map = {
            CONTINUE_LINE: W_ACTIONABLE,
            START_LINE:    W_ACTIONABLE,
            UNBLOCK_LINE:  W_ACTIONABLE * 0.5,
            DEFER:         0.0,
        }
        actionable = actionable_map.get(base_decision, 0.0)
        details["actionable"] = actionable

        # Hebelwirkung
        leverage = 0.0
        if todo.get("proposal_id"):
            leverage += W_LEVERAGE * 0.3
        if goal_rel > 0 and actionable > 0:
            leverage += W_LEVERAGE * 0.7
        details["leverage"] = leverage

        # Vernachlaessigung
        staleness = min(W_STALENESS, _staleness_days(todo) * 0.1)
        details["staleness"] = staleness

        # Loop-Penalty
        loop_pen = 0.0
        if task:
            lc = int(task.get("loop_count") or 0)
            loop_pen = min(W_LOOP_PENALTY, lc * 0.3)
            obs = recent_obs.get(task["id"], [])
            err = sum(1 for o in obs if o["type"] in ("error","blocker"))
            loop_pen += min(W_LOOP_PENALTY * 0.5, err * 0.4)
        details["loop_penalty"] = loop_pen

        # Prioritaet
        prio_map = {"hoch": 1.5, "mittel": 0.5, "niedrig": -0.5, "keine": 0.0}
        prio = prio_map.get(todo.get("priority","keine"), 0.0)
        details["priority"] = prio

        score = (continuity + prog_near + goal_rel + actionable
                 + leverage + staleness + prio
                 - loop_pen - stag_penalty)

        # 4.4: Wiederholte Stagnation bestrafen (direkte DB-Abfrage, kein circular import)
        try:
            from core.database import get_connection as _gc
            _c = _gc()
            _stag_rows = _c.execute(
                "SELECT COUNT(*) as n FROM planner_decisions WHERE owner_id=? AND primary_line_id=? AND stagnation_flags!=?",
                (owner_id, todo["id"], "[]")
            ).fetchone()
            _c.close()
            stats = {"repeated_stagnation": (_stag_rows["n"] if _stag_rows else 0) >= 2}
        except Exception:
            stats = {}
        if stats.get("repeated_stagnation"):
            score -= W_STAGNATION_PENALTY
            details["repeated_stagnation_penalty"] = W_STAGNATION_PENALTY

        # Blocker-Typ
        blocker_type = _classify_blocker(todo, task, recent_obs) if base_decision in (UNBLOCK_LINE, DEFER) else None

        # 4.2: Nicht-planbare Blocker auf DEFER setzen
        if blocker_type and not _blocker_is_plannable(blocker_type):
            if base_decision == CONTINUE_LINE:
                base_decision = DEFER

        # 7.5: Workspace-Bonus -- Linie mit echtem Artefaktstand wird bevorzugt fortgesetzt
        line_id = f"todo:{todo.get('id','')}"
        ws_state = (candidates.get("workspace_states") or {}).get(line_id, {})
        has_materialized = bool(ws_state.get("latest_materialized_artifact_id"))
        has_artifacts = (ws_state.get("artifact_count") or 0) > 0
        artifact_types = ws_state.get("artifact_types", set())
        latest_active_type = ws_state.get("latest_active_type", "")

        # 7.5: Naechster empfohlener Artefakttyp aus Linienprogression
        # brief -> analysis -> plan -> implementation -> result -> report
        ARTIFACT_PROGRESSION = ["brief","analysis","plan","implementation","result","report"]
        next_artifact_type = "brief"
        for t in ARTIFACT_PROGRESSION:
            if t not in artifact_types:
                next_artifact_type = t
                break
        else:
            next_artifact_type = "report"  # alle da -- Bericht

        workspace_bonus = 0.0
        if has_materialized:
            workspace_bonus = 1.5  # Hat echten Vollzug
        elif has_artifacts:
            workspace_bonus = 0.5  # Hat Artefakte
        if workspace_bonus > 0:
            score += workspace_bonus

        # 6.3: execution_pressure -- Bonus fuer umsetzungsreife Linien
        ex_pressure = _get_execution_pressure(todo)
        if ex_pressure > 0:
            score += ex_pressure
            details["execution_pressure"] = ex_pressure

        # 6.1: First-Line-Gate + execution_required
        is_mature = _is_line_mature(todo, situation)
        first_line_gate = is_mature and not todo.get("first_meaningful_execution")
        execution_required = _is_execution_required(todo)

        # Bei execution_required: task_template auf implementation zwingen
        if execution_required and base_decision == CONTINUE_LINE:
            todo["task_template"] = "implementation"  # erzwinge Implementation

        return {
            "type":           "todo",
            "id":             todo["id"],
            "title":          todo.get("title",""),
            "score":          round(score, 2),
            "score_details":  details,
            "decision":       base_decision,
            "blocker_type":   blocker_type,
            "stagnating":     stag["stagnating"],
            "stagnation_reason": stag["reason"],
            "blocked":        base_decision in (UNBLOCK_LINE, DEFER),
            "has_task":       task is not None,
            "execution_mode": todo.get("execution_mode","none"),
            "release_mode":   todo.get("release_mode","manual"),
            "task_template":  todo.get("task_template"),
            "goal_id":        todo.get("goal_id"),
            "proposal_id":    todo.get("proposal_id"),
            "entblockbar":       blocker_type not in (BLOCKER_HARD,),
            "execution_pressure":  ex_pressure,
            "first_line_gate":     first_line_gate,
            "is_mature":           is_mature,
            "execution_required":  execution_required,
            "meta_cycle_count":    todo.get("meta_cycle_count") or 0,
            "first_meaningful_execution": todo.get("first_meaningful_execution"),
            "workspace_bonus":      workspace_bonus,
            "has_materialized":     has_materialized,
            "has_artifacts":        has_artifacts,
            "artifact_types":       list(artifact_types),
            "latest_active_type":   latest_active_type,
            "next_artifact_type":   next_artifact_type,
            "last_workspace_write": ws_state.get("last_workspace_write_at"),
        }

    for t in situation["running"]:
        scored.append(_score_todo(t, CONTINUE_LINE))
    for t in situation["waiting"]:
        scored.append(_score_todo(t, CONTINUE_LINE))
    for t in situation["blocked"]:
        dec = UNBLOCK_LINE if _classify_blocker(t, t.get("_task"), recent_obs) != BLOCKER_HARD else DEFER
        scored.append(_score_todo(t, dec))
    for t in situation["startable"]:
        scored.append(_score_todo(t, START_LINE))
    for t in situation["low_value"]:
        scored.append(_score_todo(t, DEFER))

    # Write-Requests -- 5.4: als echte Linienobjekte im Scoring
    import datetime as _dt54
    now_utc = _dt54.datetime.now(_dt54.timezone.utc)
    for wr in candidates.get("write_request_lines", []):
        ls = wr.get("line_status", "pending_approval")
        status = wr.get("approval_status", "pending")

        # Deferred: Wiedervorlage noch nicht erreicht -> score=0, DEFER
        if status == "deferred" and wr.get("deferred_until"):
            try:
                def_until = _dt54.datetime.fromisoformat(
                    wr["deferred_until"].replace("Z", "+00:00"))
                if now_utc < def_until:
                    scored.append({
                        "type": "write_request", "id": wr["id"],
                        "title": f"[Freigabe verschoben] {wr.get('action_key','')}",
                        "score": 0.0, "decision": DEFER,
                        "line_status": ls, "approval_status": status,
                        "blocked": False, "stagnating": False,
                        "blocker_type": BLOCKER_WAITING_USER,
                        "execution_mode": "none", "goal_id": wr.get("origin_goal_id"),
                        "origin_todo_id": wr.get("origin_todo_id"),
                        "task_id": wr.get("task_id"),
                        "after_approve": wr.get("after_approve_action","continue_line"),
                        "after_reject": wr.get("after_reject_action","replan"),
                        "deferred_until": wr.get("deferred_until"),
                        "decision_due_at": wr.get("decision_due_at"),
                    })
                    continue
            except Exception:
                pass

        # Überfällig (decision_due_at überschritten)
        overdue = False
        if wr.get("decision_due_at"):
            try:
                due = _dt54.datetime.fromisoformat(
                    wr["decision_due_at"].replace("Z", "+00:00"))
                overdue = now_utc > due
            except Exception:
                pass

        # Score: Goal-Relevanz + Überfälligkeit
        goal_rel = W_GOAL_RELEVANCE * 0.5 if wr.get("origin_goal_id") and                    wr["origin_goal_id"] in active_goal_ids else W_GOAL_RELEVANCE * 0.2
        overdue_bonus = W_STALENESS if overdue else 0.0
        score = goal_rel + overdue_bonus

        scored.append({
            "type":            "write_request",
            "id":              wr["id"],
            "title":           f"[Freigabe] {wr.get('action_key','')}",
            "score":           round(score, 2),
            "decision":        CONTINUE_LINE,  # aktive Approval-Linie
            "line_status":     ls,
            "approval_status": status,
            "blocked":         False,
            "stagnating":      False,
            "blocker_type":    BLOCKER_WAITING_USER,
            "execution_mode":  "none",
            "goal_id":         wr.get("origin_goal_id"),
            "origin_todo_id":  wr.get("origin_todo_id"),
            "task_id":         wr.get("task_id"),
            "after_approve":   wr.get("after_approve_action","continue_line"),
            "after_reject":    wr.get("after_reject_action","replan"),
            "overdue":         overdue,
            "decision_due_at": wr.get("decision_due_at"),
        })

    # Proposals -- 4.x: proposal_ready_for_work in dieselbe Auswahlmatrix wie Todos
    for p in candidates["proposals"]:
        cls = _classify_proposal(p, active_goal_ids, active_task_ids)
        goal_rel = W_GOAL_RELEVANCE * 0.4 if p.get("goal_id") and p["goal_id"] in active_goal_ids else 0.0
        effort_map2 = {"klein": 0.0, "mittel": W_EFFORT_PENALTY * 0.5,
                       "gross": W_EFFORT_PENALTY, "groß": W_EFFORT_PENALTY}
        effort_pen = effort_map2.get((p.get("effort") or "mittel").lower(), W_EFFORT_PENALTY * 0.5)
        staleness = min(W_STALENESS * 0.5, _staleness_days(p) * 0.05)

        # proposal_ready_for_work: voller Score wie ein startbares Todo
        if cls == "proposal_ready_for_work":
            full_goal_rel = W_GOAL_RELEVANCE if p.get("goal_id") and p["goal_id"] in active_goal_ids else W_GOAL_RELEVANCE * 0.3
            leverage = W_LEVERAGE if full_goal_rel > 0 else W_LEVERAGE * 0.3
            score = full_goal_rel + leverage + staleness - effort_pen
            decision = START_LINE  # kann gegen Todos konkurrieren
        else:
            score = goal_rel + staleness - effort_pen
            decision = DROP_OR_PAUSE if cls == "proposal_low_value" else DEFER

        scored.append({
            "type":           "proposal",
            "id":             p["id"],
            "title":          p.get("title",""),
            "score":          round(score, 2),
            "decision":       decision,
            "proposal_class": cls,
            "blocked":        False,
            "has_task":       False,
            "execution_mode": "orbit_internal" if cls == "proposal_ready_for_work" else "none",
            "release_mode":   "summarize",
            "task_template":  "analysis",
            "goal_id":        p.get("goal_id"),
            "entblockbar":    True,
            "stagnating":     False,
        })

    order = {CONTINUE_LINE: 0, UNBLOCK_LINE: 1, START_LINE: 2, DEFER: 3, DROP_OR_PAUSE: 4}
    scored.sort(key=lambda x: (x.get("stagnating", False),
                                order.get(x["decision"], 5),
                                -x["score"]))
    return scored


# =============================================================================
# 7. Replan-Entscheidung
# =============================================================================

# WP5: temporary_compat -- Replanning-Entscheidung
def should_replan(owner_id: str, situation: dict,
                  scored: list) -> tuple[bool, str]:
    """
    Prueft ob Replan noetig ist.
    Gibt (should_replan: bool, trigger: str) zurueck.
    """
    focus = get_planner_focus(owner_id)

    if not focus:
        return True, "kein_fokus"

    # Hauptlinie abgeschlossen?
    primary_id = focus.get("primary_line_id")
    primary_type = focus.get("primary_line_type","todo")
    if primary_type == "todo" and primary_id:
        # Ist das Todo noch aktiv?
        still_active = any(
            s["id"] == primary_id
            for s in scored
            if s["decision"] in (CONTINUE_LINE, UNBLOCK_LINE, START_LINE)
        )
        if not still_active:
            return True, "hauptlinie_abgeschlossen"

    # Hauptlinie blockiert?
    primary_blocked = any(
        s["id"] == primary_id and s["decision"] in (UNBLOCK_LINE, DEFER)
        and s.get("blocker_type") == BLOCKER_HARD
        for s in scored
    )
    if primary_blocked:
        return True, "hauptlinie_hart_blockiert"

    # Hauptlinie stagniert?
    primary_stagnating = any(
        s["id"] == primary_id and s.get("stagnating")
        for s in scored
    )
    if primary_stagnating:
        return True, "hauptlinie_stagniert"

    # 4.5: Fokusbindung -- Mindeststunden halten
    focus_hours = get_focus_duration_hours(owner_id)
    if focus_hours < FOCUS_MIN_HOURS:
        # Fokus ist noch jung -- nur bei hartem Trigger replanen
        if not primary_blocked and not primary_stagnating:
            return False, ""

    # Neue hochrelevante Linie -- aber nur wenn Fokus lang genug gehalten hat
    high_relevance_new = any(
        s["decision"] == START_LINE and s["score"] > 8.0
        and s["id"] != primary_id
        for s in scored
    )
    if high_relevance_new and focus_hours >= FOCUS_MIN_HOURS:
        return True, "neue_hochrelevante_linie"

    # Fokus zu lange ohne echten Fortschritt?
    focus_hours = get_focus_duration_hours(owner_id)
    if focus_hours > 12 and focus.get("focus_confidence", 1.0) < 0.5:
        return True, "fokus_zu_lange_ohne_fortschritt"

    # Fokus pausiert?
    if focus.get("status") == "paused":
        return True, "fokus_pausiert"

    # Replan-Zeitpunkt erreicht?
    import datetime
    replan_after = focus.get("replan_after","")
    if replan_after:
        try:
            ra_dt = datetime.datetime.fromisoformat(replan_after.replace("Z","+00:00"))
            if datetime.datetime.now(datetime.timezone.utc) > ra_dt:
                return True, "replan_zeitpunkt"
        except Exception:
            pass

    return False, ""


# =============================================================================
# 8. Arbeitslinien waehlen
# =============================================================================

# WP5: temporary_compat -- Linienauswahl-Führungslogik
def choose_worklines(candidates: dict, scored: list,
                     owner_id: str, force_replan: bool = False) -> dict:
    """
    V3: Fokus halten wenn moeglich, nur bei echten Triggern replanen.
    LLM nur fuer Feinauswahl.
    """
    active_internal = [t for t in candidates["active_tasks"]
                       if t["status"] not in ("completed","failed","aborted")]
    if len(active_internal) >= MAX_INTERNAL_TASKS:
        return {
            "primary_line":     None,
            "secondary_line":   None,
            "blocked_lines":    _format_blocked(scored),
            "deferred_lines":   _format_deferred(scored),
            "stagnation_flags": _format_stagnation(scored),
            "action":           "wait",
            "reasoning":        f"{len(active_internal)} interne Tasks aktiv.",
            "replan_trigger":   None,
            "chosen":           [],
        }

    do_replan, replan_trigger = should_replan(owner_id, _build_situation_from_scored(scored), scored)

    if not do_replan and not force_replan:
        # Fokus halten -- nur pruefen ob Hauptlinie noch laeuft
        focus = get_planner_focus(owner_id)
        if focus and focus.get("primary_line_id"):
            primary_candidate = next(
                (s for s in scored if s["id"] == focus["primary_line_id"]
                 and s["type"] == focus.get("primary_line_type","todo")),
                None
            )
            if primary_candidate and primary_candidate["decision"] in (CONTINUE_LINE, START_LINE):
                return _build_result(
                    primary_candidate, None, scored,
                    f"Fokus gehalten: {primary_candidate['title'][:50]}",
                    CONTINUE_LINE, replan_trigger=None
                )

    # Replan -- neue Linien waehlen
    continue_cands = [s for s in scored if s["decision"] == CONTINUE_LINE and not s.get("stagnating")]
    unblock_cands  = [s for s in scored if s["decision"] == UNBLOCK_LINE and s.get("entblockbar")]
    start_cands    = [s for s in scored if s["decision"] == START_LINE]

    # waiting feiner differenzieren:
    # 5.3: waiting_user_decision -- Linie haelt Fokus, aber Nebenlinie kann springen
    approval_waiting = [s for s in continue_cands
                        if s.get("blocker_type") == BLOCKER_WAITING_USER]
    feedback_waiting = [s for s in continue_cands
                        if s.get("blocker_type") == BLOCKER_WAITING_FEEDBACK]
    external_waiting = [s for s in continue_cands
                        if s.get("blocker_type") == BLOCKER_WAITING_EXTERNAL]
    active_waiting   = [s for s in continue_cands
                        if s.get("blocker_type") not in (BLOCKER_WAITING_EXTERNAL,
                                                          BLOCKER_WAITING_USER)]

    if approval_waiting:
        # 5.4: Write-Request als echte Hauptlinie -- Fokus halten, Nebenlinie springt ein
        primary = approval_waiting[0]
        secondary = (start_cands[0] if start_cands
                     else [s for s in feedback_waiting if s["id"] != primary["id"]][0]
                     if feedback_waiting else None)
        # Reason je nach Überfälligkeit
        reason = ("Freigabe überfällig -- bitte entscheiden." if primary.get("overdue")
                  else "Hauptlinie wartet auf Freigabe -- Nebenlinie aktiv.")
        return _build_result(primary, secondary, scored, reason, CONTINUE_LINE, replan_trigger)

    # 6.x: execution_required schlaegt alles -- harter Vollzugszwang
    required_cands = [s for s in continue_cands if s.get("execution_required")]
    if required_cands:
        primary = required_cands[0]
        # Keine Nebenlinie -- volle Konzentration auf Vollzug
        return _build_result(
            primary, None, scored,
            f"EXECUTION REQUIRED: {primary.get('meta_cycle_count',0)} Meta-Zyklen ohne Vollzug -- "
            f"nur noch Implementation erlaubt.",
            CONTINUE_LINE, replan_trigger
        )

    # 6.3: Linien mit execution_pressure bevorzugen
    pressure_cands = sorted(
        [s for s in continue_cands if s.get("execution_pressure", 0) > 0],
        key=lambda s: s.get("execution_pressure", 0), reverse=True
    )

    # Aktiv fortsetzbare Linien bevorzugen
    primary_pool = (pressure_cands if pressure_cands
                    else active_waiting if active_waiting
                    else (feedback_waiting or continue_cands))

    if primary_pool:
        primary = primary_pool[0]
        secondary_pool = [s for s in primary_pool[1:]] + start_cands
        secondary = secondary_pool[0] if secondary_pool else None

        # 6.4: Fokusgrund je nach Zustand
        if primary.get("execution_required"):
            reason = (f"EXECUTION REQUIRED: {primary.get('meta_cycle_count',0)} Meta-Zyklen "
                      f"-- nur noch Implementation.")
        elif primary.get("first_line_gate"):
            reason = (f"First-Line-Gate aktiv: {primary.get('meta_cycle_count',0)} Meta-Zyklen "
                      f"-- erster Vollzug noetig.")
        elif primary.get("execution_pressure", 0) > 0:
            reason = (f"Umsetzungsdruck ({primary['execution_pressure']:.1f}) -- "
                      f"Implementation bevorzugt.")
        elif primary.get("next_artifact_type"):
            reason = (f"Naechster Artefaktschritt: {primary['next_artifact_type']} "
                      f"(aktuell: {primary.get('latest_active_type') or 'keins'}).")
        elif external_waiting and not active_waiting:
            reason = "Warte auf externe Bedingung -- Nebenlinie starten falls moeglich."
        else:
            reason = "Laufende Arbeit fortsetzen."
        return _build_result(primary, secondary, scored, reason, CONTINUE_LINE, replan_trigger)

    if unblock_cands:
        primary = unblock_cands[0]
        return _build_result(primary, start_cands[0] if start_cands else None,
                            scored, "Blocker aufloesen hat Vorrang.", UNBLOCK_LINE, replan_trigger)

    if not start_cands:
        # Bewusste Nicht-Auswahl -- Proposals sind jetzt bereits in start_cands wenn ready
        return {
            "primary_line": None, "secondary_line": None,
            "blocked_lines": _format_blocked(scored),
            "deferred_lines": _format_deferred(scored),
            "stagnation_flags": _format_stagnation(scored),
            "action": NOT_NOW,
            "reasoning": "Keine sinnvolle Arbeitslinie gerade. Bewusst nichts starten.",
            "replan_trigger": replan_trigger, "chosen": [],
        }

    if len(start_cands) == 1:
        return _build_result(start_cands[0], None, scored,
                            "Einziger startbarer Kandidat.", START_LINE, replan_trigger)

    # LLM Feinauswahl aus Top-3
    top = start_cands[:3]
    goal_lines = "\n".join(
        f"- Goal #{g['id']}: {g['title'][:60]} ({g.get('progress',0)}%)"
        for g in candidates["goals"][:3]
    ) or "  (keine aktiven Goals)"

    cand_lines = "\n".join(
        f"[TODO #{s['id']}] {s['title'][:60]} | Score:{s['score']} | mode={s['execution_mode']}"
        for s in top
    )

    try:
        from core.ollama_client import chat_internal
        reply, _ = chat_internal(
            user_id=owner_id,
            message=(
                "Planungsentscheidung. Welche 1-2 Arbeitslinien jetzt?\n\n"
                "Aktive Goals:\n" + goal_lines + "\n\n"
                "Kandidaten:\n" + cand_lines + "\n\n"
                "Antworte NUR als JSON:\n"
                '{"hauptlinie_id": X, "nebenlinie_id": null_oder_Y, "reason": "..."}'
            ),
            chat_history=[],
            extra_system="Kurze interne Planungsentscheidung. Nur JSON.",
            retrieval_query="Planung Arbeitslinie",
        )
        import re
        match = re.search(r'\{.*\}', (reply or "").strip(), re.DOTALL)
        data = json.loads(match.group())
        primary = next((s for s in top if s["id"] == data.get("hauptlinie_id")), top[0])
        sec_id = data.get("nebenlinie_id")
        secondary = next((s for s in top if s["id"] == sec_id), None) if sec_id else None
        return _build_result(primary, secondary, scored,
                            data.get("reason",""), START_LINE, replan_trigger)
    except Exception as e:
        logger.debug(f"LLM Feinauswahl fehlgeschlagen, Fallback: {e}")
        return _build_result(top[0], top[1] if len(top) > 1 else None,
                            scored, "Fallback: Top-Kandidaten", START_LINE, replan_trigger)


def _is_line_mature(todo: dict, situation: dict) -> bool:
    """
    6.1: Prueft ob eine Linie umsetzungsreif ist.
    Umsetzungsreif = Voraussetzungen erfuellt, kein harter Blocker,
                     noch kein first_meaningful_execution,
                     mehrere Meta-Zyklen ohne echten Vollzug.
    """
    # Muss execution_mode gesetzt haben
    if todo.get("execution_mode", "none") == "none":
        return False
    # Kein harter Blocker
    if todo.get("status") == "blocked":
        return False
    # Noch kein erster echter Vollzug
    if todo.get("first_meaningful_execution"):
        return False
    # Mindestens META_CYCLE_THRESHOLD Meta-Zyklen ohne Vollzug
    meta_cycles = todo.get("meta_cycle_count") or 0
    if meta_cycles < META_CYCLE_THRESHOLD:
        return False
    return True


def _get_execution_pressure(todo: dict) -> float:
    """
    6.3: Berechnet execution_pressure fuer eine Linie.
    Ab META_CYCLE_HARD_LIMIT: Maximaldruck (execution_required).
    """
    if todo.get("first_meaningful_execution"):
        return 0.0
    meta_cycles = todo.get("meta_cycle_count") or 0
    if meta_cycles >= META_CYCLE_HARD_LIMIT:
        return W_EXECUTION_PRESSURE  # Maximaldruck
    base = min(meta_cycles * 0.5, W_EXECUTION_PRESSURE)
    if todo.get("goal_id"):
        base = min(base * 1.2, W_EXECUTION_PRESSURE)
    return round(base, 2)


def _is_execution_required(todo: dict) -> bool:
    """
    6.x: Harter Vollzugszwang -- ab META_CYCLE_HARD_LIMIT ohne Vollzug.
    Bei execution_required: Planner waehlt AUSSCHLIESSLICH Implementation.
    """
    if todo.get("first_meaningful_execution"):
        return False
    meta_cycles = todo.get("meta_cycle_count") or 0
    return meta_cycles >= META_CYCLE_HARD_LIMIT and (todo.get("execution_mode","none") != "none")


def record_meta_cycle(todo_id: int, owner_id: str) -> None:
    """6.2: Erhoehe Meta-Zyklus-Zaehler fuer ein Todo."""
    try:
        from core.database import get_connection
        conn = get_connection()
        conn.execute(
            "UPDATE todos SET meta_cycle_count = COALESCE(meta_cycle_count,0) + 1 WHERE id=?",
            (todo_id,)
        )
        conn.commit()
        conn.close()
    except Exception as e:
        logger.debug(f"record_meta_cycle fehlgeschlagen: {e}")


def record_meaningful_execution(todo_id: int, action_key: str) -> None:
    """6.1: Markiert ersten echten Vollzug fuer eine Linie."""
    try:
        from core.database import get_connection
        from core.datetime_utils import to_iso
        if action_key not in MEANINGFUL_EXEC_TOOLS:
            return
        conn = get_connection()
        # Nur setzen wenn noch nicht vorhanden
        conn.execute(
            """UPDATE todos SET
               first_meaningful_execution=?,
               first_line_gate_active=0,
               execution_pressure=0.0
               WHERE id=? AND first_meaningful_execution IS NULL""",
            (to_iso(), todo_id)
        )
        conn.commit()
        conn.close()
        logger.info(f"6.x: first_meaningful_execution fuer Todo #{todo_id} via {action_key}")
    except Exception as e:
        logger.debug(f"record_meaningful_execution fehlgeschlagen: {e}")


def _derive_followup_from_proposal(proposal: dict, candidates: dict,
                                    owner_id: str) -> dict | None:
    """
    4.1: Leitet aus einem proposal_ready_for_work ein Vorarbeit-Todo ab.
    Wenn noch kein Todo zum Proposal existiert: neues kimi-Todo erstellen.
    Gibt ein scored-artiges Dict zurueck oder None.
    """
    proposal_id = proposal["id"]
    # Gibt es schon ein Todo zum Proposal?
    for todo in candidates["todos"]:
        if todo.get("proposal_id") == proposal_id:
            return {
                "type":           "todo",
                "id":             todo["id"],
                "title":          todo.get("title",""),
                "decision":       START_LINE,
                "score":          proposal["score"],
                "execution_mode": todo.get("execution_mode","orbit_internal"),
                "release_mode":   todo.get("release_mode","summarize"),
                "task_template":  todo.get("task_template","analysis"),
                "goal_id":        todo.get("goal_id") or proposal.get("goal_id"),
                "proposal_id":    proposal_id,
                "has_task":       False,
                "stagnating":     False,
                "blocked":        False,
            }
    # Kein Todo vorhanden -- als Vorarbeit-Suggestion zurueckgeben (kein Auto-Create)
    # Auto-Create waere zu aggressiv ohne explizite Genehmigung
    return None


def _build_situation_from_scored(scored: list) -> dict:
    """Minimales Situation-Dict aus Scored-Liste fuer should_replan."""
    return {
        "running":   [s for s in scored if s["decision"] == CONTINUE_LINE],
        "blocked":   [s for s in scored if s["decision"] in (UNBLOCK_LINE, DEFER)],
        "startable": [s for s in scored if s["decision"] == START_LINE],
    }


def _build_result(primary, secondary, all_scored, reasoning, action_type,
                  replan_trigger=None) -> dict:
    return {
        "primary_line":     _format_line(primary),
        "secondary_line":   _format_line(secondary) if secondary else None,
        "blocked_lines":    _format_blocked(all_scored),
        "deferred_lines":   _format_deferred(all_scored),
        "stagnation_flags": _format_stagnation(all_scored),
        "action":           action_type,
        "reasoning":        reasoning,
        "replan_trigger":   replan_trigger,
        "chosen":           [primary] + ([secondary] if secondary else []),
    }


def _format_line(item: dict | None) -> dict | None:
    if not item:
        return None
    return {
        "kind":           item["type"],
        "id":             item["id"],
        "decision":       item["decision"],
        "reason":         item.get("reason",""),
        "title":          item.get("title",""),
        "goal_id":        item.get("goal_id"),
        "execution_mode": item.get("execution_mode","none"),
        "task_template":  item.get("task_template"),
        "stagnating":     item.get("stagnating", False),
        "blocker_type":   item.get("blocker_type"),
        "proposal_class": item.get("proposal_class"),
    }


def _format_blocked(scored: list) -> list:
    result = []
    for s in scored:
        if s["decision"] == UNBLOCK_LINE:
            result.append({
                "kind":         s["type"],
                "id":           s["id"],
                "title":        s.get("title",""),
                "blocker_type": s.get("blocker_type",""),
                "entblockbar":  s.get("entblockbar", True),
            })
        elif s["decision"] == DEFER and s.get("proposal_class") in (
                "proposal_ready_for_work", "proposal_waiting_approval"):
            result.append({
                "kind":           s["type"],
                "id":             s["id"],
                "title":          s.get("title",""),
                "proposal_class": s.get("proposal_class"),
                "blocker_type":   "proposal_waiting_approval",
            })
    return result[:5]


def _format_deferred(scored: list) -> list:
    return [
        {"kind": s["type"], "id": s["id"], "title": s.get("title",""),
         "reason": s.get("proposal_class","niedrige Prioritaet")}
        for s in scored
        if s["decision"] in (DEFER, DROP_OR_PAUSE)
        and s.get("proposal_class","") not in ("proposal_ready_for_work","proposal_waiting_approval")
    ][:5]


def _format_stagnation(scored: list) -> list:
    return [
        {"kind": s["type"], "id": s["id"], "title": s.get("title",""),
         "reason": s.get("stagnation_reason","")}
        for s in scored
        if s.get("stagnating")
    ]


# =============================================================================
# 9. Task starten
# =============================================================================

# WP5: temporary_compat -- Planner-Autostart (delete_candidate, Kimi Core soll steuern)
def maybe_start_task(chosen: list, owner_id: str) -> list:
    import orbit as _orbit
    from core.todo_service import start_todo

    started = []
    for line in chosen:
        # 4.x/5.5: Proposals als Linie starten
        if line["type"] == "proposal" and line["decision"] == START_LINE:
            proposal_class = line.get("proposal_class", "")

            # 5.5: proposal_ready_for_work -> direkten proposal_write-Task starten
            if proposal_class == "proposal_ready_for_work":
                try:
                    import json as _jp55
                    task_id_new = _orbit.create_task(
                        task_type="action",
                        goal=f"Proposal bewerten: {line['title'][:80]}",
                        primary_origin=f"planner:proposal:{line['id']}",
                        mode="internal",
                        release_mode="summarize",
                        priority="medium",
                        linked_todo_id=None,
                        goal_id=line.get("goal_id"),
                        proposal_id=line["id"],
                    )
                    # Step 1: Proposal-Kontext lesen
                    _orbit.create_step(
                        task_id=task_id_new,
                        step_type="todos_read",
                        description=_jp55.dumps({"action": "list", "project": "kimi"}),
                        tool_ref="todos_read",
                        interruptible=True,
                    )
                    # Step 2: proposal_write -- ID explizit rein
                    _orbit.create_step(
                        task_id=task_id_new,
                        step_type="proposal_write",
                        description=_jp55.dumps({"action": "approve", "id": line["id"],
                                                 "reason": "Planner: proposal_ready_for_work"}),
                        tool_ref="proposal_write",
                        interruptible=False,
                    )
                    started.append(line["id"])
                    logger.info(f"Planner 5.5: Proposal-Task fuer Proposal #{line['id']} angelegt")
                except Exception as _ep55:
                    logger.warning(f"Planner 5.5: Proposal-Task fehlgeschlagen: {_ep55}")
                continue

            # Vorarbeits-Todo suchen (alte Logik)
            followup = _derive_followup_from_proposal(line, {"todos": []}, owner_id)
            if followup:
                line = followup
            else:
                logger.info(f"Planner: Proposal #{line['id']} bereit aber kein Todo -- kein Auto-Start")
                continue
        if line["type"] != "todo":
            continue
        if line["decision"] in (CONTINUE_LINE, DEFER, DROP_OR_PAUSE):
            continue
        if line["has_task"]:
            continue
        if line["execution_mode"] not in ("orbit_internal","orbit_chat"):
            continue

        try:
            import json as _j
            orbit_mode = "internal" if line["execution_mode"] == "orbit_internal" else "chat"
            # 6.x + 7.5: execution_required oder next_artifact_type bestimmt Template
            if line.get("execution_required"):
                tmpl = "implementation"
            elif line.get("next_artifact_type") in ("implementation", "result"):
                tmpl = "implementation"
            elif line.get("next_artifact_type") in ("plan", "analysis"):
                tmpl = "analysis"
            else:
                tmpl = line.get("task_template") or "analysis"

            task_id = _orbit.create_task(
                task_type="action",
                goal=line["title"][:100],
                primary_origin=f"planner:{line['id']}",
                mode=orbit_mode,
                release_mode=line.get("release_mode","summarize"),
                priority="medium",
                linked_todo_id=line["id"],
                goal_id=line.get("goal_id"),
                proposal_id=line.get("proposal_id"),
            )

            template_steps = {
                "analysis":       ("todos_read", _j.dumps({"action": "list", "project": "kimi"})),
                "implementation": ("todos_read", _j.dumps({"action": "list"})),
                "review":         ("todos_read", _j.dumps({"action": "list", "project": "kimi"})),
                "unblock":        ("todos_read", _j.dumps({"action": "list", "status": "blocked"})),
                "maintenance":    ("workspace",  _j.dumps({"action": "list"})),
            }
            step_tool, step_desc = template_steps.get(tmpl, ("todos_read", _j.dumps({"action": "list"})))

            # Step 1: todos_read (Kontext)
            _orbit.create_step(
                task_id=task_id,
                step_type=step_tool,
                description=step_desc,
                tool_ref=step_tool,
                interruptible=True,
                preflight_required=False,
            )

            # Step 2: artifact_list -- Kimi sieht was auf der Linie existiert
            _line_id_bootstrap = f"todo:{line['id']}"
            _orbit.create_step(
                task_id=task_id,
                step_type="workspace",
                description=_j.dumps({
                    "action": "artifact_list",
                    "line_id": _line_id_bootstrap,
                }),
                tool_ref="workspace",
                interruptible=True,
                preflight_required=False,
            )

            # Step 3: artifact_bootstrap_brief -- WP0: im Safe Mode deaktiviert
            # ENABLE_AUTO_ARTIFACTS=False -> kein automatisches Workspace-Dokument beim Task-Start
            try:
                from orbit import ENABLE_AUTO_ARTIFACTS as _EAA
            except Exception:
                _EAA = True
            if _EAA:
                try:
                    from core.workspace_artifact_service import get_latest_line_artifact
                    _existing_brief = get_latest_line_artifact(_line_id_bootstrap, "brief")
                except Exception:
                    _existing_brief = None
                if _existing_brief:
                    _orbit.create_step(
                        task_id=task_id,
                        step_type="workspace",
                        description=_j.dumps({
                            "action": "artifact_read",
                            "line_id": _line_id_bootstrap,
                            "artifact_type": "brief",
                        }),
                        tool_ref="workspace",
                        interruptible=False,
                        preflight_required=False,
                    )
                    logger.info(f"Planner: Brief bereits vorhanden fuer Linie {_line_id_bootstrap} -- read statt create")
                else:
                    _orbit.create_step(
                        task_id=task_id,
                        step_type="workspace",
                        description=_j.dumps({
                            "action": "artifact_create",
                            "line_id": _line_id_bootstrap,
                            "artifact_type": "brief",
                            "format": "md",
                            "purpose": "line_bootstrap",
                            "content": f"# Brief: {line['title'][:80]}\n\n*Automatisch beim Start angelegt — Kimi fuellt aus.*",
                        }),
                        tool_ref="workspace",
                        interruptible=False,
                        preflight_required=False,
                    )
            else:
                logger.debug(f"WP0: Planner-Bootstrap-Dokument deaktiviert fuer Linie {_line_id_bootstrap}")

            _orbit.set_task_hot(task_id, True)
            start_todo(line["id"], task_id)
            started.append(task_id)
            logger.info(f"Planner: Task {task_id[:8]} ({tmpl}) fuer Todo #{line['id']} | 3 Bootstrap-Steps angelegt")
        except Exception as e:
            logger.warning(f"Planner: Task-Start fehlgeschlagen #{line['id']}: {e}")

    return started


# =============================================================================
# 10. Haupteinstieg
# =============================================================================

# WP5: temporary_compat -- Planner-Hauptlauf (nur noch von Kimi Core aufzurufen)
def run_planner(owner_id: str, force: bool = False) -> dict:
    try:
        logger.info("Planner V3: Start")
        candidates = collect_candidates(owner_id)
        situation  = _build_situation(candidates)
        scored     = score_candidates(candidates, situation, owner_id)

        logger.info(
            f"Lagebild: running={len(situation['running'])}, "
            f"waiting={len(situation['waiting'])}, "
            f"blocked={len(situation['blocked'])}, "
            f"startable={len(situation['startable'])}"
        )

        result = choose_worklines(candidates, scored, owner_id, force_replan=force)

        if result["action"] in (CONTINUE_LINE, UNBLOCK_LINE, START_LINE):
            started = maybe_start_task(result.get("chosen",[]), owner_id)
            result["started_tasks"] = started
        else:
            result["started_tasks"] = []

        # Fokus persistieren
        primary = result.get("primary_line")
        secondary = result.get("secondary_line")
        if primary:
            save_planner_focus(
                owner_id=owner_id,
                primary_type=primary.get("kind","todo"),
                primary_id=primary["id"],
                secondary_type=secondary.get("kind") if secondary else None,
                secondary_id=secondary["id"] if secondary else None,
                reason=result["reasoning"],
                confidence=0.9 if result["action"] == CONTINUE_LINE else 0.7,
            )

        # Entscheidung historisch speichern
        record_decision(
            owner_id=owner_id,
            action=result["action"],
            primary_type=primary.get("kind") if primary else None,
            primary_id=primary["id"] if primary else None,
            secondary_type=secondary.get("kind") if secondary else None,
            secondary_id=secondary["id"] if secondary else None,
            reason=result["reasoning"],
            replan_trigger=result.get("replan_trigger"),
            deferred_ids=[d["id"] for d in result.get("deferred_lines",[])],
            blocked_ids=[b["id"] for b in result.get("blocked_lines",[])],
            stagnation_flags=[s["id"] for s in result.get("stagnation_flags",[])],
        )

        # Als Observation speichern
        try:
            from core.todo_service import record_observation
            obs_text = (
                f"Planner V3: {result['action']} | "
                f"Fokus: {primary['title'][:40] if primary else 'keins'} | "
                f"Replan-Trigger: {result.get('replan_trigger') or 'keiner'} | "
                f"Gestartet: {len(result['started_tasks'])} Tasks"
            )
            record_observation(owner_id=owner_id, content=obs_text, obs_type="state_change")
        except Exception:
            pass

        # 6.2: Meta-Zyklen-Tracking -- inkrementieren fuer aktive Linien ohne Vollzug
        if primary:
            primary_todo_id = primary.get("id") if primary.get("type") == "todo" else None
            if primary_todo_id and not primary.get("first_meaningful_execution"):
                record_meta_cycle(primary_todo_id, owner_id)
                # First-Line-Gate in DB aktualisieren
                try:
                    from core.database import get_connection as _gc6x
                    _c6x = _gc6x()
                    is_gate = primary.get("first_line_gate", False)
                    _c6x.execute(
                        "UPDATE todos SET first_line_gate_active=?, execution_pressure=? WHERE id=?",
                        (1 if is_gate else 0, primary.get("execution_pressure", 0.0), primary_todo_id)
                    )
                    _c6x.commit()
                    _c6x.close()
                except Exception:
                    pass

        # 5.4: Deferred Write-Requests -- zugehoerige Todos nicht starten
        import datetime as _dt
        write_reqs = candidates.get("write_request_lines", [])
        deferred_todo_ids = set()
        for wr in write_reqs:
            if wr.get("approval_status") == "deferred" and wr.get("deferred_until"):
                try:
                    def_until = _dt.datetime.fromisoformat(
                        wr["deferred_until"].replace("Z", "+00:00"))
                    if _dt.datetime.now(_dt.timezone.utc) < def_until:
                        if wr.get("origin_todo_id"):
                            deferred_todo_ids.add(int(wr["origin_todo_id"]))
                except Exception:
                    pass

        if deferred_todo_ids:
            result["started_tasks"] = [
                t for t in result.get("started_tasks", [])
                if t not in deferred_todo_ids
            ]
            logger.info(f"Planner 5.4: {len(deferred_todo_ids)} Todos wegen Defer uebersprungen")

        logger.info(
            f"Planner: {result['action']} | "
            f"primary={primary['id'] if primary else None} | "
            f"trigger={result.get('replan_trigger')} | "
            f"started={result['started_tasks']} | "
            f"write_reqs={len(write_reqs)}"
        )
        return result

    except Exception as e:
        logger.warning(f"run_planner V3 fehlgeschlagen: {e}")
        return {"action": "error", "error": str(e), "chosen": [], "started_tasks": [],
                "primary_line": None, "secondary_line": None,
                "blocked_lines": [], "deferred_lines": [], "stagnation_flags": []}
