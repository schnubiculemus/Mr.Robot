"""
core/planner.py -- Kimi Planner v2

V2 vs V1:
- Operatives Lagebild zuerst (laufend / blockiert / startbar / niedrig)
- Kontinuitaet stark bevorzugt (nicht neu anfangen wenn gutes Laufendes da)
- Entscheidungsklassen: continue_line | unblock_line | start_line | defer
- Blocker differenziert: direkt / entblockbar / unvermeidbar
- Progress-Naehe beruecksichtigt
- Schleifenschutz / Fehler-Penalty
- LLM nur fuer Feinauswahl aus bereits guten Kandidaten
- Kleiner Planner-Zustand fuer Kontinuitaet
"""
import logging
from core.datetime_utils import to_iso

logger = logging.getLogger(__name__)

# Maximale parallele interne Tasks
MAX_INTERNAL_TASKS = 2

# Entscheidungsklassen
CONTINUE_LINE  = "continue_line"
UNBLOCK_LINE   = "unblock_line"
START_LINE     = "start_line"
DEFER          = "defer"
DROP_OR_PAUSE  = "drop_or_pause"

# Task-Templates
TEMPLATES = ("analysis", "implementation", "review", "unblock", "maintenance")

# Score-Gewichte V2
W_CONTINUITY        = 5.0   # Hoch -- laufende Arbeit bevorzugen
W_PROGRESS_NEAR     = 3.0   # Nahe am Abschluss
W_GOAL_RELEVANCE    = 3.0
W_ACTIONABLE        = 2.5
W_LEVERAGE          = 2.0
W_STALENESS         = 1.5
W_EFFORT_PENALTY    = 1.0
W_LOOP_PENALTY      = 2.0   # Schleife / Fehler bestrafen


# =============================================================================
# Planner-Zustand (in-memory, wird pro Lauf aktualisiert)
# =============================================================================

_planner_state = {
    "current_focus_todo_id":   None,
    "current_focus_goal_id":   None,
    "last_planner_run_at":     None,
    "last_planner_choice":     None,
    "deferred_ids":            set(),   # bewusst zurueckgestellt
}


def get_planner_state() -> dict:
    return dict(_planner_state)


def _update_state(focus_todo_id=None, focus_goal_id=None, choice=None,
                  deferred_ids: set = None):
    _planner_state["last_planner_run_at"] = to_iso()
    if focus_todo_id is not None:
        _planner_state["current_focus_todo_id"] = focus_todo_id
    if focus_goal_id is not None:
        _planner_state["current_focus_goal_id"] = focus_goal_id
    if choice is not None:
        _planner_state["last_planner_choice"] = choice
    if deferred_ids is not None:
        _planner_state["deferred_ids"] = deferred_ids


# =============================================================================
# 1. Candidates sammeln
# =============================================================================

def collect_candidates(owner_id: str) -> dict:
    """
    Liest vollstaendigen operativen Zustand aus SQLite.
    Gibt goals, proposals, todos, active_tasks, recent_observations zurueck.
    """
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

        active_tasks = [dict(r) for r in conn.execute(
            """SELECT * FROM orbit_tasks
               WHERE mode IN ('internal','background')
               AND status NOT IN ('completed','failed','aborted')
               ORDER BY created_at DESC"""
        ).fetchall()]

        # Letzte Observations pro Task (Fehler/Schleifen erkennen)
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
                if len(recent_obs[tid]) < 5:
                    recent_obs[tid].append({"type": r["type"], "content": r["content"]})

        return {
            "goals":           goals,
            "proposals":       proposals,
            "todos":           todos,
            "active_tasks":    active_tasks,
            "recent_obs":      recent_obs,
        }
    finally:
        conn.close()


# =============================================================================
# 2. Operatives Lagebild
# =============================================================================

def _build_situation(candidates: dict) -> dict:
    """
    Zerlegt Kandidaten in klare Bereiche:
    running, waiting_feedback, blocked_tasks, startable_todos, low_value_todos
    """
    active_task_by_todo = {}
    for t in candidates["active_tasks"]:
        if t.get("linked_todo_id"):
            active_task_by_todo[t["linked_todo_id"]] = t

    running_todos       = []  # laufende Arbeit
    waiting_todos       = []  # waiting_feedback / waiting
    blocked_todos       = []  # blockiert
    startable_todos     = []  # offen, kein Task, execution_mode gesetzt
    low_value_todos     = []  # offen, kein execution_mode

    for todo in candidates["todos"]:
        tid = todo["id"]
        task = active_task_by_todo.get(tid)
        todo["_task"] = task

        if todo.get("status") == "blocked":
            blocked_todos.append(todo)
        elif task:
            task_status = task.get("status","")
            if task_status in ("active","new","planned"):
                running_todos.append(todo)
            elif task_status in ("waiting_feedback","waiting"):
                waiting_todos.append(todo)
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
# 3. Hilfsfunktionen
# =============================================================================

def _staleness_days(obj: dict) -> float:
    import datetime
    ts = obj.get("status_updated_at") or obj.get("updated_at") or obj.get("created_at") or ""
    if not ts:
        return 0.0
    try:
        dt = datetime.datetime.fromisoformat(ts.replace("Z", "+00:00"))
        now = datetime.datetime.now(datetime.timezone.utc)
        return max(0.0, (now - dt).total_seconds() / 86400)
    except Exception:
        return 0.0


def _loop_penalty(task: dict | None, recent_obs: dict) -> float:
    """Bestraft Schleifen und Fehler-Haeufen."""
    if not task:
        return 0.0
    loop_count = int(task.get("loop_count") or 0)
    penalty = min(W_LOOP_PENALTY, loop_count * 0.3)
    # Fehler-Observations
    obs = recent_obs.get(task["id"], [])
    error_count = sum(1 for o in obs if o["type"] in ("error","blocker"))
    penalty += min(W_LOOP_PENALTY, error_count * 0.5)
    return penalty


def _progress_near_bonus(todo: dict, task: dict | None) -> float:
    """Bonus wenn Arbeit kurz vor Abschluss steht."""
    if task and task.get("status") == "waiting_feedback":
        return W_PROGRESS_NEAR * 0.5  # Nahe dran
    loop = int(task.get("loop_count", 0)) if task else 0
    max_l = int(task.get("max_loops", 5)) if task else 5
    if max_l > 0 and loop / max_l > 0.6:
        return W_PROGRESS_NEAR * 0.5
    return 0.0


def _is_entblockbar(todo: dict, candidates: dict) -> bool:
    """Heuristik: ist der Blocker ueberwindbar?"""
    task = todo.get("_task")
    if not task:
        return True  # kein Task = kein bekannter Blocker
    obs = candidates.get("recent_obs", {}).get(task["id"], [])
    for o in obs:
        c = o.get("content","").lower()
        if "permission" in c or "zugriff" in c:
            return False  # Systemrechte -- nicht einfach lösbar
    return True


# =============================================================================
# 4. Scoring
# =============================================================================

def score_candidates(candidates: dict, situation: dict) -> list:
    """
    Bewertet Todos als Arbeitslinien.
    Gibt sortierte Liste mit decision-Klasse zurueck.
    """
    active_goal_ids = {g["id"] for g in candidates["goals"]}
    recent_obs = candidates.get("recent_obs", {})
    scored = []

    def _score_todo(todo: dict, base_decision: str) -> dict:
        task = todo.get("_task")
        details = {}

        # Kontinuitaet
        continuity = W_CONTINUITY if base_decision == CONTINUE_LINE else 0.0
        details["continuity"] = continuity

        # Progress-Naehe
        prog_near = _progress_near_bonus(todo, task)
        details["progress_near"] = prog_near

        # Goal-Relevanz
        goal_rel = W_GOAL_RELEVANCE if todo.get("goal_id") and todo["goal_id"] in active_goal_ids else 0.0
        details["goal_relevance"] = goal_rel

        # Bearbeitbarkeit
        if base_decision == CONTINUE_LINE:
            actionable = W_ACTIONABLE
        elif base_decision == START_LINE:
            actionable = W_ACTIONABLE
        elif base_decision == UNBLOCK_LINE:
            actionable = W_ACTIONABLE * 0.5
        else:
            actionable = 0.0
        details["actionable"] = actionable

        # Hebelwirkung
        leverage = 0.0
        if todo.get("proposal_id"):
            leverage += W_LEVERAGE * 0.3
        if goal_rel > 0 and actionable > 0:
            leverage += W_LEVERAGE * 0.7
        details["leverage"] = leverage

        # Vernachlaessigung
        days = _staleness_days(todo)
        staleness = min(W_STALENESS, days * 0.1)
        details["staleness"] = staleness

        # Schleife / Fehler
        loop_pen = _loop_penalty(task, recent_obs)
        details["loop_penalty"] = loop_pen

        # Prioritaet
        prio_map = {"hoch": 1.5, "mittel": 0.5, "niedrig": -0.5, "keine": 0.0}
        prio_bonus = prio_map.get(todo.get("priority","keine"), 0.0)
        details["priority"] = prio_bonus

        score = continuity + prog_near + goal_rel + actionable + leverage + staleness + prio_bonus - loop_pen
        return {
            "type":           "todo",
            "id":             todo["id"],
            "title":          todo.get("title",""),
            "score":          round(score, 2),
            "score_details":  details,
            "decision":       base_decision,
            "blocked":        base_decision in (UNBLOCK_LINE, DEFER),
            "has_task":       task is not None,
            "execution_mode": todo.get("execution_mode","none"),
            "release_mode":   todo.get("release_mode","manual"),
            "task_template":  todo.get("task_template"),
            "goal_id":        todo.get("goal_id"),
            "proposal_id":    todo.get("proposal_id"),
            "entblockbar":    _is_entblockbar(todo, candidates),
        }

    # Laufende Arbeit
    for t in situation["running"]:
        scored.append(_score_todo(t, CONTINUE_LINE))

    # Wartende Arbeit (waiting_feedback)
    for t in situation["waiting"]:
        scored.append(_score_todo(t, CONTINUE_LINE))

    # Blockierte Arbeit
    for t in situation["blocked"]:
        dec = UNBLOCK_LINE if _is_entblockbar(t, candidates) else DEFER
        scored.append(_score_todo(t, dec))

    # Startbare Arbeit
    for t in situation["startable"]:
        scored.append(_score_todo(t, START_LINE))

    # Niedrig relevante Arbeit
    for t in situation["low_value"]:
        scored.append(_score_todo(t, DEFER))

    # Proposals als potenzielle Arbeitslinien
    for p in candidates["proposals"]:
        goal_rel = W_GOAL_RELEVANCE * 0.4 if p.get("goal_id") and p["goal_id"] in active_goal_ids else 0.0
        effort_map = {"klein": 0.0, "mittel": W_EFFORT_PENALTY * 0.5, "gross": W_EFFORT_PENALTY, "groß": W_EFFORT_PENALTY}
        effort_pen = effort_map.get((p.get("effort") or "mittel").lower(), W_EFFORT_PENALTY * 0.5)
        days = _staleness_days(p)
        staleness = min(W_STALENESS * 0.5, days * 0.05)
        score = goal_rel + staleness - effort_pen
        # Proposals werden nie direkt gestartet -- nur als Referenz
        scored.append({
            "type":      "proposal",
            "id":        p["id"],
            "title":     p.get("title",""),
            "score":     round(score, 2),
            "decision":  DEFER,  # Proposal braucht explizite Genehmigung
            "blocked":   False,
            "has_task":  False,
            "execution_mode": "none",
            "goal_id":   p.get("goal_id"),
        })

    # Sortierung: CONTINUE/UNBLOCK vor START vor DEFER, dann nach Score
    order = {CONTINUE_LINE: 0, UNBLOCK_LINE: 1, START_LINE: 2, DEFER: 3, DROP_OR_PAUSE: 4}
    scored.sort(key=lambda x: (order.get(x["decision"], 5), -x["score"]))
    return scored


# =============================================================================
# 5. Kimi waehlt Arbeitslinien (LLM nur Feinauswahl)
# =============================================================================

def choose_worklines(candidates: dict, scored: list, owner_id: str) -> dict:
    """
    Stufe 1 deterministisch: continue/unblock pruefen.
    Stufe 2 LLM: nur fuer Feinauswahl aus Top-3 startbaren Kandidaten.
    """
    from core.ollama_client import chat_internal

    active_internal = [t for t in candidates["active_tasks"]
                       if t["status"] not in ("completed","failed","aborted")]
    if len(active_internal) >= MAX_INTERNAL_TASKS:
        logger.info(f"Planner: {len(active_internal)} aktive Tasks -- kein neuer Start")
        return {
            "primary_line":   None,
            "secondary_line": None,
            "blocked_lines":  [],
            "action":         "wait",
            "reasoning":      f"{len(active_internal)} interne Tasks aktiv.",
        }

    # Stufe 1: Gibt es gute continue_line?
    continue_candidates = [s for s in scored if s["decision"] == CONTINUE_LINE and not s.get("blocked")]
    unblock_candidates  = [s for s in scored if s["decision"] == UNBLOCK_LINE and s.get("entblockbar")]
    start_candidates    = [s for s in scored if s["decision"] == START_LINE]

    # Wenn gutes Laufendes da -- einfach fortsetzen
    if continue_candidates:
        primary = continue_candidates[0]
        secondary = continue_candidates[1] if len(continue_candidates) > 1 else (
            start_candidates[0] if start_candidates else None
        )
        return _build_result(primary, secondary, scored, "Laufende Arbeit fortsetzen.", CONTINUE_LINE)

    # Wenn wichtige Entblockung moeglich
    if unblock_candidates and (not start_candidates or unblock_candidates[0]["score"] > (start_candidates[0]["score"] if start_candidates else 0)):
        primary = unblock_candidates[0]
        return _build_result(primary, start_candidates[0] if start_candidates else None,
                            scored, "Blocker aufloesen hat Vorrang.", UNBLOCK_LINE)

    # Stufe 2: LLM fuer Feinauswahl aus startbaren Kandidaten
    if not start_candidates:
        return {
            "primary_line":   None,
            "secondary_line": None,
            "blocked_lines":  _format_blocked(scored),
            "action":         "idle",
            "reasoning":      "Keine startbaren Kandidaten.",
        }

    top_start = start_candidates[:3]
    if len(top_start) == 1:
        # Nur einer -- kein LLM noetig
        return _build_result(top_start[0], None, scored, "Einziger startbarer Kandidat.", START_LINE)

    # LLM Feinauswahl
    goal_lines = "\n".join(
        f"- Goal #{g['id']}: {g['title'][:60]} ({g.get('progress',0)}%)"
        for g in candidates["goals"][:3]
    ) or "  (keine aktiven Goals)"

    cand_lines = "\n".join(
        f"[TODO #{s['id']}] {s['title'][:60]} | Score: {s['score']} | mode={s['execution_mode']}"
        for s in top_start
    )

    prompt = (
        "Planungsentscheidung. Welche 1-2 Arbeitslinien jetzt?\n\n"
        "Aktive Goals:\n" + goal_lines + "\n\n"
        "Startbare Kandidaten:\n" + cand_lines + "\n\n"
        "Antworte NUR als JSON (kein Markdown):\n"
        '{"hauptlinie_id": X, "nebenlinie_id": null_oder_Y, "reason": "..."}'
    )

    try:
        reply, _ = chat_internal(
            user_id=owner_id,
            message=prompt,
            chat_history=[],
            extra_system="Kurze interne Planungsentscheidung. Nur JSON.",
            retrieval_query="Planung Arbeitslinie",
        )
        import json, re
        match = re.search(r'\{.*\}', (reply or "").strip(), re.DOTALL)
        if not match:
            raise ValueError("kein JSON")
        data = json.loads(match.group())

        primary = next((s for s in top_start if s["id"] == data.get("hauptlinie_id")), top_start[0])
        secondary_id = data.get("nebenlinie_id")
        secondary = next((s for s in top_start if s["id"] == secondary_id), None) if secondary_id else None
        return _build_result(primary, secondary, scored, data.get("reason",""), START_LINE)

    except Exception as e:
        logger.debug(f"choose_worklines LLM fehlgeschlagen, Fallback: {e}")
        return _build_result(top_start[0], top_start[1] if len(top_start) > 1 else None,
                            scored, "Fallback: Top-Kandidaten", START_LINE)


def _build_result(primary: dict, secondary: dict | None,
                  all_scored: list, reasoning: str, action_type: str) -> dict:
    chosen_ids = {primary["id"]}
    if secondary:
        chosen_ids.add(secondary["id"])

    blocked = _format_blocked(all_scored)

    return {
        "primary_line":   _format_line(primary),
        "secondary_line": _format_line(secondary) if secondary else None,
        "blocked_lines":  blocked,
        "action":         action_type,
        "reasoning":      reasoning,
        # Flache chosen-Liste fuer Rueckwaertskompatibilitaet mit maybe_start_task
        "chosen":         [primary] + ([secondary] if secondary else []),
    }


def _format_line(item: dict | None) -> dict | None:
    if not item:
        return None
    return {
        "kind":     item["type"],
        "id":       item["id"],
        "decision": item["decision"],
        "reason":   item.get("reason", ""),
        "title":    item.get("title",""),
    }


def _format_blocked(scored: list) -> list:
    return [
        {"kind": s["type"], "id": s["id"], "decision": s["decision"],
         "reason": "blockiert oder niedrige Prioritaet"}
        for s in scored
        if s["decision"] in (UNBLOCK_LINE, DEFER) and not s.get("entblockbar", True)
    ][:3]


# =============================================================================
# 6. Task starten
# =============================================================================

def maybe_start_task(chosen: list, owner_id: str) -> list:
    """
    Startet ORBIT-Task nur fuer start_line Entscheidungen mit execution_mode gesetzt.
    continue_line und unblock_line werden nicht neu gestartet.
    """
    import orbit as _orbit
    from core.todo_service import start_todo

    started = []
    for line in chosen:
        if line["type"] != "todo":
            continue
        # Laufende Arbeit nicht nochmal starten
        if line["decision"] in (CONTINUE_LINE, DEFER, DROP_OR_PAUSE):
            continue
        if line["has_task"]:
            continue
        if line["execution_mode"] not in ("orbit_internal","orbit_chat"):
            continue

        try:
            orbit_mode = "internal" if line["execution_mode"] == "orbit_internal" else "chat"
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

            # Ersten Step je nach Template
            template_steps = {
                "analysis":       ("todos_read",  '{"action": "list", "project": "kimi"}'),
                "implementation": ("todos_read",  '{"action": "list"}'),
                "review":         ("todos_read",  '{"action": "list"}'),
                "unblock":        ("todos_read",  '{"action": "list"}'),
                "maintenance":    ("todos_read",  '{"action": "list"}'),
            }
            step_tool, step_desc = template_steps.get(tmpl, ("todos_read", '{"action": "list"}'))

            _orbit.create_step(
                task_id=task_id,
                step_type=step_tool,
                description=step_desc,
                tool_ref=step_tool,
                interruptible=True,
                preflight_required=False,
            )
            _orbit.set_task_hot(task_id, True)
            start_todo(line["id"], task_id)
            started.append(task_id)
            logger.info(f"Planner: Task {task_id[:8]} gestartet ({tmpl}) fuer Todo #{line['id']}")
        except Exception as e:
            logger.warning(f"Planner: Task-Start fehlgeschlagen fuer #{line['id']}: {e}")

    return started


# =============================================================================
# 7. Haupteinstieg
# =============================================================================

def run_planner(owner_id: str, force: bool = False) -> dict:
    """
    Vollstaendiger Planner-Lauf V2.
    """
    try:
        logger.info("Planner V2: Start")
        candidates = collect_candidates(owner_id)
        logger.info(
            f"Planner: {len(candidates['goals'])} Goals, "
            f"{len(candidates['proposals'])} Proposals, "
            f"{len(candidates['todos'])} Todos, "
            f"{len(candidates['active_tasks'])} aktive Tasks"
        )

        situation = _build_situation(candidates)
        logger.info(
            f"Lagebild: running={len(situation['running'])}, "
            f"waiting={len(situation['waiting'])}, "
            f"blocked={len(situation['blocked'])}, "
            f"startable={len(situation['startable'])}"
        )

        scored = score_candidates(candidates, situation)
        result = choose_worklines(candidates, scored, owner_id)

        if result["action"] in (START_LINE, UNBLOCK_LINE, CONTINUE_LINE):
            chosen = result.get("chosen", [])
            started = maybe_start_task(chosen, owner_id)
            result["started_tasks"] = started
        else:
            result["started_tasks"] = []

        # Zustand aktualisieren
        primary = result.get("primary_line")
        _update_state(
            focus_todo_id=primary["id"] if primary and primary.get("kind") == "todo" else None,
            focus_goal_id=primary.get("goal_id") if primary else None,
            choice=result["action"],
            deferred_ids={s["id"] for s in scored if s["decision"] == DEFER},
        )

        logger.info(
            f"Planner V2: {result['action']} | "
            f"primary={primary['id'] if primary else None} | "
            f"started={result['started_tasks']} | "
            f"reason={result['reasoning'][:60]}"
        )
        return result

    except Exception as e:
        logger.warning(f"run_planner V2 fehlgeschlagen: {e}")
        return {"action": "error", "error": str(e), "chosen": [], "started_tasks": [],
                "primary_line": None, "secondary_line": None, "blocked_lines": []}
