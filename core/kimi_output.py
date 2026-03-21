"""
core/kimi_output.py — Zentrale Kimi Output-Interpretation

DIE einzige Schicht die aus Kimi-Text echte Systemhandlungen macht.

Drei Phasen:
    A. Extraktion  — Text → strukturierte Actions (keine Side Effects)
    B. Ausführung  — Actions → ActionResults (echte Writes)
    C. Verifikation — ActionResults → verifizierte Zustandsänderungen

Alle Module laufen über process_kimi_output():
    app.py, orbit.py, autonomous_reflection.py, inner_dialogue.py, diary.py

Kein Modul darf eigene operative Parser haben.
"""

from __future__ import annotations
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


# =============================================================================
# Datenstrukturen
# =============================================================================

@dataclass
class Action:
    """Eine strukturierte Aktion aus Kimis Output."""
    type: str                        # todo.create | todo.complete | proposal.create | moltbook.* | calendar.* | send.message
    payload: dict                    # Aktions-spezifische Daten
    source: str                      # chat | idle_pulse | autonomous_reflection | ...
    raw_fragment: str | None = None  # Original-Text aus dem die Aktion extrahiert wurde
    confidence: float = 1.0          # 1.0 = expliziter Block, <1.0 = Intent-Erkennung


@dataclass
class ActionResult:
    """Ergebnis einer ausgeführten Aktion — mit Verifikation."""
    ok: bool
    type: str
    object_type: str | None = None      # todo | proposal | moltbook_post | ...
    object_id: str | int | None = None  # ID des erzeugten Objekts
    message: str | None = None          # Text für Tommy (nur bei ok=True)
    error: str | None = None            # Fehlertext (nur bei ok=False)
    dashboard_visible: bool = False     # Soll im Dashboard erscheinen?
    links: dict | None = None           # Verknüpfungen zu anderen Objekten


@dataclass
class ProcessResult:
    """Gesamtergebnis eines process_kimi_output() Aufrufs."""
    cleaned_text: str
    actions: list[Action] = field(default_factory=list)
    results: list[ActionResult] = field(default_factory=list)
    public_appendix: list[str] = field(default_factory=list)
    internal_events: list[dict] = field(default_factory=list)

    @property
    def success_count(self) -> int:
        return sum(1 for r in self.results if r.ok)

    @property
    def failure_count(self) -> int:
        return sum(1 for r in self.results if not r.ok)

    def to_reply(self) -> str:
        """Baut den finalen Text für Tommy zusammen."""
        parts = [self.cleaned_text] if self.cleaned_text else []
        parts.extend(self.public_appendix)
        return "\n\n".join(p for p in parts if p).strip()


# =============================================================================
# Phase A — Extraktion
# =============================================================================

def extract_actions(text: str, source: str) -> tuple[str, list[Action]]:
    """
    Extrahiert alle strukturierten Aktionen aus Kimis Text.
    Gibt (bereinigter_text, actions) zurück.
    Keine Side Effects — nur Daten.
    """
    actions = []
    cleaned = text

    # 1. TODO_ACTION Blöcke
    cleaned, todo_actions = _extract_todo_actions(cleaned, source)
    actions.extend(todo_actions)

    # 2. PROPOSAL Blöcke
    cleaned, proposal_actions = _extract_proposal_actions(cleaned, source)
    actions.extend(proposal_actions)

    # 3. CALENDAR_ACTION Blöcke
    cleaned, cal_actions = _extract_calendar_actions(cleaned, source)
    actions.extend(cal_actions)

    # 4. MOLTBOOK Blöcke
    cleaned, mb_actions = _extract_moltbook_actions(cleaned, source)
    actions.extend(mb_actions)

    # 5. SEARCH Blöcke (nur markieren, werden separat verarbeitet)
    # Nicht hier ausführen — app.py macht den zweiten Call

    # 6. [gespeichert] Signal entfernen
    cleaned = re.sub(r'\s*\[gespeichert\]\s*', ' ', cleaned).strip()
    cleaned = re.sub(r'\n{3,}', '\n\n', cleaned).strip()

    return cleaned, actions


def _extract_todo_actions(text: str, source: str) -> tuple[str, list[Action]]:
    pattern = re.compile(r'\[TODO_ACTION:\s*(\{.*?\})\s*\]', re.DOTALL)
    matches = list(pattern.finditer(text))
    if not matches:
        return text, []

    cleaned = pattern.sub("", text).strip()
    cleaned = re.sub(r'\n{3,}', '\n\n', cleaned).strip()

    actions = []
    for match in matches:
        try:
            raw = match.group(1).replace('\n', ' ').replace('\r', '')
            payload = json.loads(raw)
            actions.append(Action(
                type=f"todo.{payload.get('action', 'create')}",
                payload=payload,
                source=source,
                raw_fragment=match.group(0),
                confidence=1.0,
            ))
        except json.JSONDecodeError as e:
            logger.warning(f"extract_actions: TODO_ACTION JSON-Fehler: {e}")

    return cleaned, actions


def _extract_proposal_actions(text: str, source: str) -> tuple[str, list[Action]]:
    pattern = re.compile(r'\[PROPOSAL:\s*(\{.*?\})\s*\]', re.DOTALL)
    matches = list(pattern.finditer(text))
    if not matches:
        return text, []

    cleaned = pattern.sub("", text).strip()
    cleaned = re.sub(r'\n{3,}', '\n\n', cleaned).strip()

    actions = []
    for match in matches:
        try:
            raw = match.group(1).replace('\n', ' ').replace('\r', '')
            payload = json.loads(raw)
            actions.append(Action(
                type="proposal.create",
                payload=payload,
                source=source,
                raw_fragment=match.group(0),
                confidence=1.0,
            ))
        except json.JSONDecodeError as e:
            logger.warning(f"extract_actions: PROPOSAL JSON-Fehler: {e}")

    return cleaned, actions


def _extract_calendar_actions(text: str, source: str) -> tuple[str, list[Action]]:
    try:
        from core.calendar.calendar_router import extract_calendar_action
        cleaned, cal_action = extract_calendar_action(text)
        if cal_action:
            return cleaned, [Action(
                type=f"calendar.{cal_action.get('action', 'list')}",
                payload=cal_action,
                source=source,
                confidence=1.0,
            )]
        return text, []
    except Exception:
        return text, []


def _extract_moltbook_actions(text: str, source: str) -> tuple[str, list[Action]]:
    try:
        from core.moltbook import extract_moltbook_action
        cleaned, mb_action = extract_moltbook_action(text)
        if mb_action:
            return cleaned, [Action(
                type=f"moltbook.{mb_action.get('action', 'unknown')}",
                payload=mb_action,
                source=source,
                confidence=1.0,
            )]
        return text, []
    except Exception:
        return text, []


# =============================================================================
# Phase B — Ausführung
# =============================================================================

def execute_actions(
    actions: list[Action],
    user_id: str,
    source: str,
    context: dict | None = None,
) -> list[ActionResult]:
    """
    Führt alle Actions aus. Jede Action geht durch einen dedizierten Executor.
    Gibt Liste von ActionResults zurück — jedes mit Verifikation.
    """
    results = []
    for action in actions:
        result = _execute_single(action, user_id, context)
        results.append(result)
        if result.ok:
            logger.info(f"execute_actions [{source}]: {action.type} → {result.object_type}:{result.object_id}")
        else:
            logger.warning(f"execute_actions [{source}]: {action.type} FAILED → {result.error}")
    return results


def _execute_single(action: Action, user_id: str, context: dict | None) -> ActionResult:
    """Dispatcht eine einzelne Action an den richtigen Executor."""
    try:
        if action.type.startswith("todo."):
            return _run_todo(action, user_id, context)
        elif action.type == "proposal.create":
            return _run_proposal(action, user_id, context)
        elif action.type.startswith("calendar."):
            return _run_calendar(action, user_id, context)
        elif action.type.startswith("moltbook."):
            return _run_moltbook(action, user_id, context)
        else:
            return ActionResult(ok=False, type=action.type, error=f"Unbekannter Action-Typ: {action.type}")
    except Exception as e:
        return ActionResult(ok=False, type=action.type, error=str(e))


def _run_todo(action: Action, user_id: str, context: dict | None) -> ActionResult:
    from core.todos import execute_todo_action
    payload = action.payload
    act = payload.get("action", "create")

    result_text = execute_todo_action(user_id, payload)

    # Verifikation: bei create prüfen ob ID in der Antwort
    if act == "create":
        if result_text and "#" in result_text:
            # ID aus Antwort extrahieren
            m = re.search(r'#(\d+)', result_text)
            todo_id = int(m.group(1)) if m else None
            return ActionResult(
                ok=True,
                type=action.type,
                object_type="todo",
                object_id=todo_id,
                message=result_text,
                dashboard_visible=True,
            )
        else:
            return ActionResult(ok=False, type=action.type, error=f"Todo-Anlage fehlgeschlagen: {result_text}")
    elif act == "complete":
        ok = result_text is not None and "abgehakt" in (result_text or "").lower()
        return ActionResult(ok=ok, type=action.type, object_type="todo",
                           object_id=payload.get("id"), message=result_text)
    else:
        return ActionResult(ok=result_text is not None, type=action.type,
                           object_type="todo", message=result_text)


def _run_proposal(action: Action, user_id: str, context: dict | None) -> ActionResult:
    from core.proposals import save_proposal
    payload = action.payload

    proposal_id = save_proposal(payload, source=action.source, user_id=user_id)

    if proposal_id is not None:
        title = payload.get("title", "Vorschlag")
        return ActionResult(
            ok=True,
            type=action.type,
            object_type="proposal",
            object_id=proposal_id,
            message=f"Vorschlag eingereicht: {title}",
            dashboard_visible=True,
        )
    else:
        return ActionResult(
            ok=False,
            type=action.type,
            error="Proposal konnte nicht gespeichert werden",
        )


def _run_calendar(action: Action, user_id: str, context: dict | None) -> ActionResult:
    from core.calendar.calendar_router import execute_calendar_action
    result_text = execute_calendar_action(action.payload)
    ok = result_text is not None and len(result_text.strip()) > 0
    return ActionResult(ok=ok, type=action.type, object_type="calendar_event",
                       message=result_text if ok else None,
                       error=None if ok else "Kalender-Aktion fehlgeschlagen")


def _run_moltbook(action: Action, user_id: str, context: dict | None) -> ActionResult:
    from core.moltbook import execute_moltbook_action
    result_text = execute_moltbook_action(action.payload)
    ok = result_text is not None and len(result_text.strip()) > 0
    return ActionResult(ok=ok, type=action.type, object_type="moltbook",
                       message=result_text if ok else None,
                       error=None if ok else "Moltbook-Aktion fehlgeschlagen")


# =============================================================================
# Phase C — Verifikation + Rückgabe
# =============================================================================

def build_public_appendix(results: list[ActionResult], visibility: str) -> list[str]:
    """
    Baut die öffentlichen Anhänge für Tommys Antwort.
    Nur ok=True Actions dürfen Bestätigungen erzeugen.
    Nur bei visibility="public".
    """
    if visibility != "public":
        return []

    appendix = []
    for result in results:
        if result.ok and result.message:
            appendix.append(result.message)
    return appendix


def build_internal_events(
    results: list[ActionResult],
    source: str,
    user_id: str,
    context: dict | None = None,
) -> list[dict]:
    """Baut interne Event-Einträge für das Log und ORBIT."""
    from core.datetime_utils import to_iso
    events = []
    for result in results:
        event = {
            "event": f"{result.object_type or 'unknown'}_{'created' if result.ok else 'failed'}",
            "source": source,
            "user_id": user_id,
            "action_type": result.type,
            "object_type": result.object_type,
            "object_id": result.object_id,
            "ok": result.ok,
            "error": result.error,
            "timestamp": to_iso(),
        }
        if context:
            event["context"] = {k: v for k, v in context.items()
                               if k in ("task_id", "thread_id", "display_name")}
        events.append(event)
    return events


def link_related_objects(results: list[ActionResult], context: dict | None = None) -> None:
    """
    Verknüpft erzeugte Objekte miteinander.
    Z.B. proposal → todo wenn approve, oder todo → task.
    Aktuell: Basis-Verknüpfung proposal → todo aus context.
    """
    try:
        proposal_id = next((r.object_id for r in results if r.ok and r.object_type == "proposal"), None)
        todo_id = next((r.object_id for r in results if r.ok and r.object_type == "todo"), None)

        if proposal_id and todo_id:
            from core.database import get_connection
            conn = get_connection()
            try:
                conn.execute(
                    "UPDATE kimi_proposals SET approved_todo_id=? WHERE id=?",
                    (todo_id, proposal_id)
                )
                conn.execute(
                    "UPDATE todos SET description = description || ' [proposal:' || ? || ']' WHERE id=? AND description NOT LIKE '%[proposal:%'",
                    (str(proposal_id), todo_id)
                )
                conn.commit()
            finally:
                conn.close()
    except Exception as e:
        logger.debug(f"link_related_objects: fehlgeschlagen (unkritisch): {e}")


# =============================================================================
# Logging
# =============================================================================

def _log_to_db(source: str, user_id: str, actions: list[Action], results: list[ActionResult]) -> None:
    """Schreibt Aktionen und Ergebnisse ins kimi_output_log."""
    try:
        from core.database import get_connection
        from core.datetime_utils import to_iso
        conn = get_connection()
        try:
            conn.execute(
                """INSERT INTO kimi_output_log
                   (source, user_id, actions_found, actions_executed, actions_failed, created_at)
                   VALUES (?,?,?,?,?,?)""",
                (
                    source, user_id,
                    json.dumps([a.type for a in actions]),
                    json.dumps([r.type for r in results if r.ok]),
                    json.dumps([{"type": r.type, "error": r.error} for r in results if not r.ok]),
                    to_iso(),
                )
            )
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        logger.debug(f"_log_to_db fehlgeschlagen (unkritisch): {e}")


# =============================================================================
# Haupt-Einstiegspunkt
# =============================================================================

def _extract_proposals_via_llm(text: str, user_id: str) -> list[dict]:
    """
    Zweiter strukturierter Extraction-Call:
    Kimi selbst extrahiert aus ihrem eigenen Text einen Proposal-JSON.
    Nur wenn kein expliziter Block vorhanden war.
    Confidence=0.8 — robuster als Regex, schlechter als expliziter Block.
    """
    if not text or len(text) < 15:
        return []

    # Schnell-Check: enthält der Text überhaupt Wunsch-Signale?
    import re as _re
    wish_signals = [
        "wünsch", "hätte gerne", "würde ich", "fehlt mir",
        "ich will", "ich brauche", "wäre gut", "proposal", "vorschlag",
        "eigenen ordner", "schreibzugriff", "workspace", "git"
    ]
    text_lower = text.lower()
    if not any(s in text_lower for s in wish_signals):
        return []

    try:
        from core.ollama_client import chat_internal
        from config import USER_CONTEXTS, OWNER_ID

        extraction_prompt = (
            f"Hier ist eine Antwort von mir auf eine Wunsch-Frage:\n\n"
            f"{text}\n\n"
            f"Extrahiere daraus genau einen konkreten, baubaren Wunsch als JSON.\n"
            f"Format (NUR dieses JSON, nichts anderes):\n"
            f'{{"title": "kurzer Titel", "description": "was genau", '
            f'"effort": "klein|mittel|groß", "reason": "warum"}}'
            f"\n\nWenn kein konkreter baubarer Wunsch erkennbar ist: antworte mit dem Wort LEER."
        )

        reply, _ = chat_internal(
            user_id=user_id or OWNER_ID,
            message=extraction_prompt,
            chat_history=[],
            context_name=USER_CONTEXTS.get(user_id or OWNER_ID, "tommy"),
            extra_system=(
                "Du bist ein strukturierter JSON-Extraktor. "
                "Antworte NUR mit dem JSON-Objekt oder dem Wort LEER. "
                "Kein erklärender Text, keine Backticks, kein Markdown."
            ),
        )

        if not reply or "LEER" in reply.upper():
            return []

        # JSON aus Antwort extrahieren
        reply = reply.strip()
        # Backticks entfernen falls trotzdem dabei
        reply = _re.sub(r"^```(?:json)?\n?", "", reply).strip()
        reply = _re.sub(r"\n?```$", "", reply).strip()

        import json as _json
        proposal = _json.loads(reply)

        # Validierung
        if not proposal.get("title") or len(proposal["title"]) < 5:
            return []
        if proposal.get("effort", "").lower() not in ("klein", "mittel", "groß", "gross"):
            proposal["effort"] = "mittel"

        logger.info(f"_extract_proposals_via_llm: Proposal extrahiert: '{proposal['title'][:50]}'")
        return [proposal]

    except Exception as e:
        logger.debug(f"_extract_proposals_via_llm fehlgeschlagen (unkritisch): {e}")
        return []


def process_kimi_output(
    *,
    source: str,
    user_id: str,
    raw_text: str,
    visibility: str = "public",
    context: dict | None = None,
) -> ProcessResult:
    """
    DIE einzige Funktion die Kimi-Ausgaben interpretiert.

    Args:
        source:     "chat" | "idle_pulse" | "autonomous_reflection" | "inner_dialogue" | "diary" | ...
        user_id:    User-ID
        raw_text:   Kimis roher Antwort-Text
        visibility: "public" → Antwort geht an Tommy | "internal" → nur Memory/ORBIT
        context:    Optional: task_id, thread_id, display_name etc.

    Returns:
        ProcessResult mit cleaned_text, actions, results, public_appendix, internal_events
    """
    if not raw_text or not raw_text.strip():
        return ProcessResult(cleaned_text="")

    # Phase A: Extraktion
    cleaned_text, actions = extract_actions(raw_text, source)

    # Phase B: Ausführung
    results = execute_actions(actions, user_id, source, context)

    # Phase B.5: LLM-Extraction wenn kein expliziter Proposal-Block vorhanden
    # Kimi extrahiert strukturiert aus ihrem eigenen Text — robuster als Regex
    proposal_results = [r for r in results if r.ok and r.object_type == "proposal"]
    if not proposal_results and visibility == "public":
        try:
            llm_proposals = _extract_proposals_via_llm(raw_text, user_id)
            if llm_proposals:
                llm_actions = [
                    Action(type="proposal.create", payload=p, source=f"{source}:llm_extraction",
                           raw_fragment=None, confidence=0.8)
                    for p in llm_proposals
                ]
                llm_results = execute_actions(llm_actions, user_id, source, context)
                results.extend(llm_results)
                for r in llm_results:
                    if r.ok:
                        logger.info(f"process_kimi_output: LLM-Proposal gespeichert #{r.object_id}")
        except Exception as _llm_e:
            logger.debug(f"process_kimi_output: LLM-Extraction fehlgeschlagen (unkritisch): {_llm_e}")

    # Phase C: Verifikation + Rückgabe
    public_appendix = build_public_appendix(results, visibility)
    internal_events = build_internal_events(results, source, user_id, context)

    # Objekte verknüpfen
    if results:
        link_related_objects(results, context)

    # Logging
    if actions or any(not r.ok for r in results):
        _log_to_db(source, user_id, actions, results)

    proc_result = ProcessResult(
        cleaned_text=cleaned_text,
        actions=actions,
        results=results,
        public_appendix=public_appendix,
        internal_events=internal_events,
    )

    if proc_result.failure_count > 0:
        logger.warning(
            f"process_kimi_output [{source}]: {proc_result.failure_count} Fehler von {len(actions)} Aktionen"
        )

    return proc_result
