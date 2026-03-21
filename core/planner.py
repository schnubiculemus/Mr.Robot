"""
core/planner.py -- Kimi Planner v1

Entscheidet woran Kimi als naechstes arbeitet.
Nicht ORBIT (Ausfuehrung) -- sondern Auswahl der Arbeitslinie.

Vorgehen:
1. collect_candidates() -- Goals, Proposals, Todos, Tasks aus SQLite lesen
2. score_candidates()   -- regelbasierte Bewertung
3. choose_worklines()   -- Kimi waehlt intern 1-2 Linien aus den Top-Kandidaten
4. maybe_start_task()   -- optional Task starten wenn Linie klar ist

Trigger: idle_pulse wenn kein interner Task laeuft, oder nach Task-Abschluss.
"""
import logging
from core.datetime_utils import to_iso

logger = logging.getLogger(__name__)

# Maximale parallele interne Tasks
MAX_INTERNAL_TASKS = 2

# Score-Gewichte
W_GOAL_RELEVANCE  = 3.0
W_ACTIONABLE      = 2.5
W_LEVERAGE        = 2.0
W_CONTINUITY      = 2.0
W_STALENESS       = 1.5
W_EFFORT_PENALTY  = 1.0


# =============================================================================
# 1. Candidates sammeln
# =============================================================================

def collect_candidates(owner_id: str) -> dict:
    """
    Liest den vollstaendigen operativen Zustand aus SQLite.
    Gibt ein Dict mit goals, proposals, todos, active_tasks zurueck.
    """
    from core.database import get_connection
    conn = get_connection()
    try:
        # Aktive Goals
        goals = [dict(r) for r in conn.execute(
            "SELECT * FROM kimi_goals WHERE owner_id=? AND status='active' ORDER BY priority DESC, created_at DESC",
            (owner_id,)
        ).fetchall()]

        # Offene Proposals (pending)
        proposals = [dict(r) for r in conn.execute(
            "SELECT * FROM kimi_proposals WHERE owner_id=? AND status='pending' ORDER BY created_at ASC",
            (owner_id,)
        ).fetchall()]

        # Offene und in_progress Todos (kimi-Projekt)
        todos = [dict(r) for r in conn.execute(
            """SELECT * FROM todos
               WHERE user_id=? AND status IN ('open','in_progress')
               AND project='kimi'
               ORDER BY priority DESC, created_at ASC""",
            (owner_id,)
        ).fetchall()]

        # Laufende / wartende interne Tasks
        active_tasks = [dict(r) for r in conn.execute(
            """SELECT * FROM orbit_tasks
               WHERE mode IN ('internal','background')
               AND status NOT IN ('completed','failed','aborted')
               ORDER BY created_at DESC""",
        ).fetchall()]

        return {
            "goals":        goals,
            "proposals":    proposals,
            "todos":        todos,
            "active_tasks": active_tasks,
        }
    finally:
        conn.close()


# =============================================================================
# 2. Bewertung
# =============================================================================

def _goal_ids_active(candidates: dict) -> set:
    return {g["id"] for g in candidates["goals"]}


def _linked_todo_ids(candidates: dict) -> set:
    """Todo-IDs die bereits einen laufenden Task haben."""
    return {t["linked_todo_id"] for t in candidates["active_tasks"]
            if t.get("linked_todo_id")}


def _staleness_days(obj: dict) -> float:
    """Tage seit letzter Aktivitaet auf dem Objekt."""
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


def score_candidates(candidates: dict) -> list:
    """
    Bewertet Todos und Proposals als moegliche Arbeitslinien.
    Gibt sortierte Liste von Kandidaten-Dicts zurueck.
    Jedes Dict hat: type, id, title, score, score_details, blocked
    """
    active_goal_ids = _goal_ids_active(candidates)
    linked_todo_ids = _linked_todo_ids(candidates)
    scored = []

    # --- Todos bewerten ---
    for todo in candidates["todos"]:
        todo_id = todo["id"]
        blocked = False
        details = {}

        # Schon ein laufender Task?
        has_task = todo_id in linked_todo_ids
        continuity = W_CONTINUITY if has_task else 0.0
        details["continuity"] = continuity

        # Goal-Relevanz
        goal_rel = W_GOAL_RELEVANCE if todo.get("goal_id") and todo["goal_id"] in active_goal_ids else 0.0
        details["goal_relevance"] = goal_rel

        # Bearbeitbar jetzt?
        status = todo.get("status", "open")
        if status == "blocked":
            blocked = True
            actionable = 0.0
        elif status == "in_progress" and has_task:
            actionable = W_ACTIONABLE * 0.5  # laeuft schon, nicht nochmal starten
        elif todo.get("execution_mode", "none") in ("orbit_internal", "orbit_chat"):
            actionable = W_ACTIONABLE
        else:
            actionable = W_ACTIONABLE * 0.3  # kein orbit_mode gesetzt
        details["actionable"] = actionable

        # Hebelwirkung: hat Proposal-Bezug zu aktivem Goal?
        leverage = 0.0
        if todo.get("proposal_id"):
            leverage = W_LEVERAGE * 0.5
        if goal_rel > 0 and actionable > 0:
            leverage += W_LEVERAGE * 0.5
        details["leverage"] = leverage

        # Vernachlaessigung
        days = _staleness_days(todo)
        staleness = min(W_STALENESS, days * 0.1)
        details["staleness"] = staleness

        # Prioritaet
        prio_map = {"hoch": 1.5, "mittel": 0.5, "niedrig": -0.5, "keine": 0.0}
        prio_bonus = prio_map.get(todo.get("priority", "keine"), 0.0)
        details["priority"] = prio_bonus

        score = continuity + goal_rel + actionable + leverage + staleness + prio_bonus
        scored.append({
            "type":          "todo",
            "id":            todo_id,
            "title":         todo.get("title", ""),
            "score":         round(score, 2),
            "score_details": details,
            "blocked":       blocked,
            "has_task":      has_task,
            "execution_mode": todo.get("execution_mode", "none"),
            "release_mode":  todo.get("release_mode", "manual"),
            "task_template": todo.get("task_template"),
            "goal_id":       todo.get("goal_id"),
            "proposal_id":   todo.get("proposal_id"),
        })

    # --- Proposals bewerten (als potenzielle Folgearbeit) ---
    for proposal in candidates["proposals"]:
        blocked = False
        details = {}

        goal_rel = W_GOAL_RELEVANCE * 0.5 if proposal.get("goal_id") and proposal["goal_id"] in active_goal_ids else 0.0
        details["goal_relevance"] = goal_rel

        # Effort-Malus
        effort_map = {"klein": 0.0, "mittel": W_EFFORT_PENALTY * 0.5, "gross": W_EFFORT_PENALTY, "groß": W_EFFORT_PENALTY}
        effort_penalty = effort_map.get((proposal.get("effort") or "mittel").lower(), W_EFFORT_PENALTY * 0.5)
        details["effort_penalty"] = effort_penalty

        days = _staleness_days(proposal)
        staleness = min(W_STALENESS * 0.5, days * 0.05)
        details["staleness"] = staleness

        score = goal_rel + staleness - effort_penalty
        scored.append({
            "type":          "proposal",
            "id":            proposal["id"],
            "title":         proposal.get("title", ""),
            "score":         round(score, 2),
            "score_details": details,
            "blocked":       blocked,
            "has_task":      False,
            "execution_mode": "none",
            "goal_id":       proposal.get("goal_id"),
        })

    # Blockierte ans Ende, dann nach Score
    scored.sort(key=lambda x: (x["blocked"], -x["score"]))
    return scored


# =============================================================================
# 3. Kimi waehlt Arbeitslinien
# =============================================================================

def choose_worklines(candidates: dict, scored: list,
                     owner_id: str) -> dict:
    """
    Gibt scored[:5] an Kimi als internen Call -- sie waehlt 1-2 Arbeitslinien.
    Gibt ein Dict zurueck: {chosen: [...], deferred: [...], reasoning: str}
    """
    from core.ollama_client import chat_internal

    # Zu viele aktive Tasks?
    active_internal = [t for t in candidates["active_tasks"]
                       if t["status"] not in ("completed","failed","aborted")]
    if len(active_internal) >= MAX_INTERNAL_TASKS:
        logger.info(f"Planner: {len(active_internal)} aktive Tasks -- kein neuer Start")
        return {
            "chosen":    [],
            "deferred":  [s["id"] for s in scored[:3]],
            "reasoning": f"Bereits {len(active_internal)} interne Tasks aktiv -- nichts Neues starten.",
            "action":    "wait",
        }

    if not scored:
        return {"chosen": [], "deferred": [], "reasoning": "Keine Kandidaten.", "action": "idle"}

    # Top-5 nicht-blockierte Kandidaten
    top = [s for s in scored if not s["blocked"]][:5]
    if not top:
        return {"chosen": [], "deferred": [], "reasoning": "Alle Kandidaten blockiert.", "action": "idle"}

    # Goals als Kontext
    goal_lines = "\n".join(
        f"- Goal #{g['id']}: {g['title'][:60]} (Fortschritt: {g.get('progress',0)}%)"
        for g in candidates["goals"][:3]
    ) or "  (keine aktiven Goals)"

    # Kandidaten formatieren
    cand_lines = []
    for s in top:
        line = f"[{s['type'].upper()} #{s['id']}] {s['title'][:60]} | Score: {s['score']}"
        if s["has_task"]:
            line += " | LAEUFT BEREITS"
        if s["execution_mode"] in ("orbit_internal","orbit_chat"):
            line += f" | mode={s['execution_mode']}"
        cand_lines.append(line)
    cand_text = "\n".join(cand_lines)

    prompt = (
        "Ich bin im Planungsmodus. Welche 1-2 Arbeitslinien sind jetzt die sinnvollsten?\n\n"
        "Aktive Goals:\n" + goal_lines + "\n\n"
        "Kandidaten (nach Prioritaet):\n" + cand_text + "\n\n"
        "Laufende interne Tasks: " + str(len(active_internal)) + "\n\n"
        "Antworte NUR in diesem Format (JSON, kein Markdown):\n"
        '{"hauptlinie": {"type": "todo|proposal", "id": X, "reason": "..."}, '
        '"nebenlinie": null oder {"type": "todo|proposal", "id": Y, "reason": "..."}, '
        '"deferred_reason": "warum alles andere zurueckgestellt"}'
    )

    try:
        reply, _ = chat_internal(
            user_id=owner_id,
            message=prompt,
            chat_history=[],
            extra_system="Kurze interne Planungsentscheidung. Nur JSON, kein Kommentar.",
            retrieval_query="Planung naechste Arbeitslinie Kimi",
        )
        if not reply:
            raise ValueError("leere Antwort")

        import json, re
        # JSON aus Antwort extrahieren
        match = re.search(r'\{.*\}', reply.strip(), re.DOTALL)
        if not match:
            raise ValueError(f"kein JSON in Antwort: {reply[:80]}")
        data = json.loads(match.group())

        chosen = []
        for key in ("hauptlinie", "nebenlinie"):
            line = data.get(key)
            if line and isinstance(line, dict) and line.get("id"):
                # Kandidat aus scored suchen
                match_cand = next((s for s in top if s["id"] == line["id"] and s["type"] == line.get("type","todo")), None)
                if match_cand:
                    chosen.append({**match_cand, "reason": line.get("reason","")})

        deferred = [s["id"] for s in top if s["id"] not in {c["id"] for c in chosen}]

        return {
            "chosen":    chosen,
            "deferred":  deferred,
            "reasoning": data.get("deferred_reason",""),
            "action":    "start" if chosen else "idle",
        }

    except Exception as e:
        logger.warning(f"choose_worklines fehlgeschlagen: {e}")
        # Fallback: erstes nicht-blockiertes Todo nehmen
        if top:
            return {
                "chosen":    [top[0]],
                "deferred":  [s["id"] for s in top[1:]],
                "reasoning": "Fallback: erstes Kandidat",
                "action":    "start",
            }
        return {"chosen": [], "deferred": [], "reasoning": str(e), "action": "idle"}


# =============================================================================
# 4. Task starten wenn Linie klar
# =============================================================================

def maybe_start_task(chosen: list, owner_id: str) -> list:
    """
    Startet einen ORBIT-Task fuer jede gewaehlte Arbeitslinie die noch keinen Task hat.
    Gibt Liste gestarteter Task-IDs zurueck.
    """
    import orbit as _orbit
    from core.todo_service import start_todo

    started = []
    for line in chosen:
        if line["type"] != "todo":
            continue  # Proposals werden nicht direkt gestartet
        if line["has_task"]:
            logger.info(f"Planner: Todo #{line['id']} hat bereits Task -- skip")
            continue
        if line["execution_mode"] not in ("orbit_internal", "orbit_chat"):
            logger.info(f"Planner: Todo #{line['id']} hat execution_mode='{line['execution_mode']}' -- skip")
            continue

        try:
            orbit_mode = "internal" if line["execution_mode"] == "orbit_internal" else "chat"
            task_id = _orbit.create_task(
                task_type="action",
                goal=line["title"][:100],
                primary_origin=f"planner:{line['id']}",
                mode=orbit_mode,
                release_mode=line.get("release_mode", "summarize"),
                priority="medium",
                linked_todo_id=line["id"],
                goal_id=line.get("goal_id"),
                proposal_id=line.get("proposal_id"),
            )
            # Ersten Step je nach task_template
            tmpl = line.get("task_template", "general")
            if tmpl == "analysis":
                step_tool = "todos_read"
                step_desc = '{"action": "list", "project": "kimi"}'
            else:
                step_tool = "todos_read"
                step_desc = '{"action": "list"}'

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
            logger.info(f"Planner: Task {task_id[:8]} gestartet fuer Todo #{line['id']} -- {line['title'][:40]}")
        except Exception as e:
            logger.warning(f"Planner: Task-Start fehlgeschlagen fuer Todo #{line['id']}: {e}")

    return started


# =============================================================================
# 5. Haupteinstieg
# =============================================================================

def run_planner(owner_id: str, force: bool = False) -> dict:
    """
    Vollstaendiger Planner-Lauf.
    force=True: auch wenn Tasks laufen.
    Gibt Ergebnis-Dict zurueck.
    """
    try:
        logger.info("Planner: Start")
        candidates = collect_candidates(owner_id)
        logger.info(
            f"Planner: {len(candidates['goals'])} Goals, "
            f"{len(candidates['proposals'])} Proposals, "
            f"{len(candidates['todos'])} Todos, "
            f"{len(candidates['active_tasks'])} aktive Tasks"
        )

        scored = score_candidates(candidates)
        result = choose_worklines(candidates, scored, owner_id)

        if result["action"] == "start" and result["chosen"]:
            started = maybe_start_task(result["chosen"], owner_id)
            result["started_tasks"] = started
        else:
            result["started_tasks"] = []

        logger.info(
            f"Planner: {result['action']} | "
            f"chosen={[c['id'] for c in result['chosen']]} | "
            f"started={result.get('started_tasks',[])} | "
            f"reason={result['reasoning'][:60]}"
        )
        return result

    except Exception as e:
        logger.warning(f"run_planner fehlgeschlagen: {e}")
        return {"action": "error", "error": str(e), "chosen": [], "started_tasks": []}
