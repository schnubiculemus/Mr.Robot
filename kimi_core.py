"""
kimi_core.py — Kimi Core V2

Die zentrale Orchestrierungsschicht von Kimi.
Kimi Core ist der einzige führende Einstiegspunkt für Nutzereingaben.

Verantwortlichkeiten:
  - Eingang verarbeiten
  - Modus / Kontextbezug bestimmen
  - Routing-Entscheidung: direkt / Memory / Workspace / Tool / Worker
  - Ergebnisse integrieren
  - finale Antwort erzeugen

Schichtenregeln (V2):
  - Kimi Core führt
  - Memory unterstützt (lesen/schreiben, kein Führungsrecht)
  - Workspace dient (Dokumente halten, kein Führungsrecht)
  - Tools werden genutzt (begrenzte Funktionalität, kein Führungsrecht)
  - Worker werden delegiert (Coding Agent etc., liefern zurück an Core)
  - ORBIT: temporäre Kompatibilitätsschicht, kein Führungsrecht

WP1: Kimi Core ist Subjekt, nicht Nebenprodukt.
"""

import logging
import re

logger = logging.getLogger(__name__)

# =============================================================================
# Routing-Signale
# =============================================================================
# Kimi Core entscheidet anhand dieser Signale ob delegiert wird.

ROUTE_DIRECT     = "direct"       # Kimi antwortet direkt
ROUTE_MEMORY     = "memory"       # Memory-Kontext nötig (Standard, immer aktiv)
ROUTE_WORKSPACE  = "workspace"    # Workspace-Kontext nötig
ROUTE_TOOL       = "tool"         # Tool-Aufruf nötig (websearch, introspect etc.)
ROUTE_WORKER     = "worker"       # Worker-Delegation (Coding Agent etc.)
ROUTE_ORBIT_COMPAT = "orbit_compat"  # temporary_compat: ORBIT-Kompatibilitätspfad


# =============================================================================
# KimiCoreRequest / KimiCoreResult
# =============================================================================

class KimiCoreRequest:
    """Eingehende Anfrage an Kimi Core."""
    def __init__(self, user_id: str, text: str, context_name: str,
                 chat_history: list = None, meta: dict = None):
        self.user_id = user_id
        self.text = text
        self.context_name = context_name
        self.chat_history = chat_history or []
        self.meta = meta or {}


class KimiCoreResult:
    """Ergebnis von Kimi Core."""
    def __init__(self, reply: str, route: str = ROUTE_DIRECT,
                 turn_meta: dict = None, delegations: list = None):
        self.reply = reply
        self.route = route
        self.turn_meta = turn_meta or {}
        self.delegations = delegations or []  # welche Schichten wurden genutzt

    def to_reply(self) -> str:
        return self.reply or ""


# =============================================================================
# Kimi Core — Hauptfunktion
# =============================================================================

def process(request: KimiCoreRequest) -> KimiCoreResult:
    """
    Haupteinstieg für alle Nutzernachrichten.

    Pipeline:
      1. Routing-Entscheidung
      2. Tool-Pre-Processing (websearch, introspect)
      3. Kimi-Antwort generieren (Memory immer aktiv)
      4. Tool-Post-Processing
      5. Output-Interpretation (proposals, todos etc.)
      6. Ergebnis zurückgeben

    Kimi Core führt — alles andere unterstützt.
    """
    from core.ollama_client import chat as ollama_chat

    delegations = []
    route = ROUTE_MEMORY  # Standard: Memory immer aktiv

    # --- WP2: Active Working Context als Primäranker VOR Memory lesen ---
    # WP3: AWC als extra_system (eigener Kanal), NICHT als doc_context
    # So verdrängt AWC nicht Fast-Track + Typed Memory
    awc_extra = ""
    awc = None
    try:
        from active_working_context import get_active_context, format_for_prompt
        awc = get_active_context(request.user_id)
        if awc:
            awc_extra = format_for_prompt(awc)
            delegations.append("awc")
            logger.debug(f"KimiCore: AWC gelesen: {awc.get('active_line','?')[:60]}")
    except Exception as _awc_e:
        logger.debug(f"KimiCore: AWC nicht verfügbar: {_awc_e}")

    # WP7: Coding-Modus erkennen — gesetzt von /code Command in app.py
    # Wenn coding_mode=True: Kimi bekommt expliziten Hinweis den Coding Agent zu nutzen.
    _coding_mode = request.meta.get("coding_mode", False)
    if _coding_mode:
        _coding_hint_lines = [
            "CODING-MODUS: Tommy hat /code verwendet. Nutze den Coding Agent.",
            "Schreibe einen [CODE_AGENT: {...}] Block:",
            "  mode: scaffold | patch | refactor | tests | review | read_only_analysis | explain_code",
            "  task: klarer Auftrag",
            "  scope: [] fuer neue Datei, oder doc_id fuer bestehende Datei",
            "  target_doc_id: Zieldateiname im Workspace (z.B. mein_skript)",
            "  return_format: workspace (Standard) oder text",
            "Beispiel neue Datei:",
            '[CODE_AGENT: {"mode": "scaffold", "task": "...", "scope": [], "target_doc_id": "skript", "return_format": "workspace"}]',
            "Beispiel bestehende Datei:",
            '[CODE_AGENT: {"mode": "patch", "task": "...", "scope": ["doc_id"], "return_format": "workspace"}]',
        ]
        _coding_hint = "\n".join(_coding_hint_lines)
        awc_extra = (_coding_hint + "\n\n" + awc_extra).strip()
        delegations.append("coding_mode")
        logger.info("KimiCore: Coding-Modus aktiv (/code)")

    # --- Schritt 1: Erste Antwort ---
    try:
        reply, turn_meta = ollama_chat(
            request.user_id,
            request.text,
            request.chat_history,
            request.context_name,
            extra_system=awc_extra if awc_extra else None,
        )
    except Exception as e:
        logger.error(f"KimiCore: Fehler beim Kimi-Call: {e}")
        return KimiCoreResult(
            reply="Es tut mir leid, ich konnte gerade keine Antwort generieren.",
            route=ROUTE_DIRECT,
        )

    delegations.append(ROUTE_MEMORY)

    # --- Schritt 2: Tool-Delegation (Web Search) ---
    # WP1/Gelb 6: core.tools statt app.py -- korrekte Abhängigkeitsrichtung
    try:
        from core.tools import handle_web_search
        reply, search_ctx = handle_web_search(reply, user_id=request.user_id,
                                               user_message=request.text)
        if search_ctx:
            delegations.append(ROUTE_TOOL)
            route = ROUTE_TOOL
            logger.info("KimiCore: WebSearch delegiert")
            try:
                search_reply, search_turn_meta = ollama_chat(
                    request.user_id, request.text,
                    request.chat_history, request.context_name,
                    doc_context=search_ctx,
                )
                search_reply = re.sub(r"\[SEARCH:\s*.+?\]", "", search_reply or "",
                                      flags=re.IGNORECASE).strip()
                if search_reply:
                    reply = search_reply
                    turn_meta = search_turn_meta
            except Exception as e:
                logger.error(f"KimiCore: WebSearch zweiter Call fehlgeschlagen: {e}")
    except Exception as e:
        logger.warning(f"KimiCore: WebSearch fehlgeschlagen (unkritisch): {e}")

    # --- Schritt 3: Tool-Delegation (Introspect) ---
    # WP1/Gelb 6: core.tools statt app.py
    try:
        from core.tools import handle_introspect
        reply, introspect_ctx = handle_introspect(reply)
        if introspect_ctx:
            delegations.append(ROUTE_TOOL)
            logger.info("KimiCore: Introspect delegiert")
            try:
                introspect_reply, introspect_turn_meta = ollama_chat(
                    request.user_id, request.text,
                    request.chat_history, request.context_name,
                    doc_context=introspect_ctx,
                )
                introspect_reply = re.sub(r"\[INTROSPECT\]", "", introspect_reply or "",
                                          flags=re.IGNORECASE).strip()
                if introspect_reply:
                    reply = introspect_reply
                    turn_meta = introspect_turn_meta
            except Exception as e:
                logger.error(f"KimiCore: Introspect zweiter Call fehlgeschlagen: {e}")
    except Exception as e:
        logger.warning(f"KimiCore: Introspect fehlgeschlagen (unkritisch): {e}")

    # --- Schritt 4: Tool-Delegation (Calendar Read) — WP6 ---
    # ACCESS_READ: [CALENDAR_ACTION: {"action": "list", ...}]
    # Nur list-Aktionen — create/update/delete laufen weiter über kimi_output.py (Schritt 5)
    try:
        from core.tools import handle_calendar_read
        reply, cal_ctx = handle_calendar_read(reply)
        if cal_ctx:
            delegations.append("calendar_read")
            route = ROUTE_TOOL
            logger.info("KimiCore: CalendarRead delegiert")
            try:
                cal_reply, cal_turn_meta = ollama_chat(
                    request.user_id, request.text,
                    request.chat_history, request.context_name,
                    doc_context=cal_ctx,
                )
                # Guard: kein [CALENDAR_ACTION:...] im zweiten Call
                cal_reply = re.sub(r"\[CALENDAR_ACTION:\s*\{.*?\}\s*\]", "",
                                   cal_reply or "", flags=re.IGNORECASE | re.DOTALL).strip()
                if cal_reply:
                    reply = cal_reply
                    turn_meta = cal_turn_meta
            except Exception as e:
                logger.error(f"KimiCore: CalendarRead zweiter Call fehlgeschlagen: {e}")
    except Exception as e:
        logger.warning(f"KimiCore: CalendarRead fehlgeschlagen (unkritisch): {e}")

    # --- Schritt 4b: Tool-Delegation (Todo Read) — WP6 ---
    # ACCESS_READ: [TODO_ACTION: {"action": "list", ...}]
    # Nur list-Aktionen — create/complete/delete laufen weiter über kimi_output.py (Schritt 5)
    try:
        from core.tools import handle_todo_read
        reply, todo_ctx = handle_todo_read(reply, user_id=request.user_id)
        if todo_ctx:
            delegations.append("todo_read")
            route = ROUTE_TOOL
            logger.info("KimiCore: TodoRead delegiert")
            try:
                todo_reply, todo_turn_meta = ollama_chat(
                    request.user_id, request.text,
                    request.chat_history, request.context_name,
                    doc_context=todo_ctx,
                )
                # Guard: kein [TODO_ACTION:...] im zweiten Call
                todo_reply = re.sub(r"\[TODO_ACTION:\s*\{.*?\}\s*\]", "",
                                    todo_reply or "", flags=re.IGNORECASE | re.DOTALL).strip()
                if todo_reply:
                    reply = todo_reply
                    turn_meta = todo_turn_meta
            except Exception as e:
                logger.error(f"KimiCore: TodoRead zweiter Call fehlgeschlagen: {e}")
    except Exception as e:
        logger.warning(f"KimiCore: TodoRead fehlgeschlagen (unkritisch): {e}")

    # --- Schritt 4c: Worker-Delegation (Coding Agent) — WP7 ---
    # Erkennt [CODE_AGENT: {...}] in Kimis Antwort.
    # Worker-Modell: minimax-m2.7 — separater Call, kein Kimi-Kontext.
    # Kimi Core führt — Coding Agent arbeitet im expliziten Scope.
    # Kein direkter Nutzer-Dialog, kein Memory-Write, kein ORBIT.
    try:
        from coding_agent import extract_coding_request, run as run_coding_agent
        _awc_for_agent = None
        try:
            from active_working_context import get_active_context
            _awc_for_agent = get_active_context(request.user_id)
        except Exception:
            pass

        reply, coding_req = extract_coding_request(reply, request.user_id, awc=_awc_for_agent)
        if coding_req:
            delegations.append("coding_agent")
            route = ROUTE_WORKER
            logger.info(f"KimiCore: CodingAgent delegiert — mode={coding_req.mode}, "
                       f"scope={coding_req.scope_files}")
            try:
                coding_result = run_coding_agent(coding_req)
                agent_ctx = coding_result.to_kimi_context()
                # Zweiter Kimi-Call: Kimi integriert das Ergebnis und antwortet Tommy
                agent_reply, agent_turn_meta = ollama_chat(
                    request.user_id, request.text,
                    request.chat_history, request.context_name,
                    doc_context=agent_ctx,
                )
                # Guard: kein [CODE_AGENT:...] im zweiten Call
                import re as _re
                agent_reply = _re.sub(r'\[CODE_AGENT:\s*\{.*?\}\s*\]', '',
                                      agent_reply or '', flags=_re.IGNORECASE | _re.DOTALL).strip()
                if agent_reply:
                    reply = agent_reply
                    turn_meta = agent_turn_meta
            except Exception as e:
                logger.error(f"KimiCore: CodingAgent fehlgeschlagen: {e}")
    except Exception as e:
        logger.warning(f"KimiCore: CodingAgent-Import fehlgeschlagen (unkritisch): {e}")

    # --- Schritt 5: Output-Interpretation ---
    # Kimi Core verarbeitet Proposals, Todos (Write), Calendar (Write) etc. aus dem Reply
    #
    # WP6 Write-Gate — präzise, klassenspezifisch:
    #   allow_todo_write     = True nur bei expliziter Todo-Anweisung
    #   allow_calendar_write = True nur bei expliziter Kalender-Anweisung
    #   allow_orbit_intent   = True nur wenn Nutzer ORBIT/Eigenbearbeitung explizit will
    #
    # Nicht ausreichend: "der Turn klingt irgendwie nach Schreiben"
    # Erforderlich: spezifische Anweisung für genau diese Write-Klasse
    _text_lower = request.text.lower()

    # Todo-Write: nur bei klar todogerichteter Anweisung
    _todo_write_triggers = [
        "leg an", "leg das an", "anlegen", "erstell ein todo", "erstelle ein todo",
        "mach ein todo", "todo anlegen", "aufgabe anlegen", "aufgabe erstellen",
        "trag das ein", "notier das", "hak ab", "abhaken",
        "als erledigt", "erledigt markieren", "todo löschen", "lösch das todo",
    ]
    allow_todo_write = any(t in _text_lower for t in _todo_write_triggers)

    # Kalender-Write: nur bei klar kalendergerichteter Anweisung
    _calendar_write_triggers = [
        "termin anlegen", "termin erstellen", "mach einen termin", "trag den termin ein",
        "termin eintragen", "termin löschen", "lösch den termin",
        "termin ändern", "termin verschieben", "blockiere", "block im kalender",
    ]
    allow_calendar_write = any(t in _text_lower for t in _calendar_write_triggers)

    # ORBIT-Intent: nur wenn Nutzer explizit Eigenbearbeitung signalisiert
    _orbit_intent_triggers = [
        "orbit", "arbeite selbst daran", "mach das intern", "bearbeite das eigenständig",
        "bearbeite das selbst", "kümmere dich selbst", "nimm dir das vor",
    ]
    allow_orbit_intent = any(t in _text_lower for t in _orbit_intent_triggers)

    if allow_todo_write:
        logger.info("KimiCore: allow_todo_write=True")
    if allow_calendar_write:
        logger.info("KimiCore: allow_calendar_write=True")
    if allow_orbit_intent:
        logger.info("KimiCore: allow_orbit_intent=True")

    try:
        from core.kimi_output import process_kimi_output
        proc = process_kimi_output(
            source="chat",
            user_id=request.user_id,
            raw_text=reply,
            visibility="public",
            context=request.meta,
            allow_todo_write=allow_todo_write,
            allow_calendar_write=allow_calendar_write,
            allow_orbit_intent=allow_orbit_intent,
        )
        reply = proc.to_reply()
    except Exception as e:
        logger.warning(f"KimiCore: process_kimi_output fehlgeschlagen: {e}")

    # --- WP4: Workspace-Routing ---
    # Kimi Core erkennt klare Schreibabsicht und routet an V2-Workspace
    # Freigabepunkt 1: Führendes Dokument hart
    # - active_document aus AWC = erste Wahl
    # - "hauptnotiz" NUR beim allerersten Schreiben (kein active_document gesetzt)
    # - kein stiller Rückfall auf "hauptnotiz" wenn Kontext läuft
    # Freigabepunkt 2: Hilfsdokumente real einbinden beim Lesen
    try:
        from core.workspace_service import (
            append_to_document, set_leading_document,
            get_leading_document, read_helper_documents,
            WRITE_REASON_IMPLICIT, DOC_TYPE_NOTE
        )
        _write_markers = [
            "halte das fest", "schreib das", "notiere", "füge hinzu",
            "ergänze", "aktualisiere die notiz", "schreibe in die notiz",
            "note:", "notiz:"
        ]
        _read_markers = [
            "was steht", "zeig mir die notiz", "lies die notiz",
            "was haben wir", "was habe ich", "öffne"
        ]
        _text_lower = request.text.lower()
        _has_write_intent = any(m in _text_lower for m in _write_markers)
        _has_read_intent = any(m in _text_lower for m in _read_markers)

        # Führendes Dokument aus AWC -- strikt: kein Fallback wenn Kontext läuft
        _current_leading = get_leading_document(request.user_id)
        if _has_write_intent:
            if _current_leading:
                # Kontext läuft -- konsequent ins führende Dokument
                _doc_id = _current_leading
            else:
                # Erstes Schreiben -- "hauptnotiz" als Einstieg, dann setzen
                _doc_id = "hauptnotiz"
            append_to_document(
                request.user_id, _doc_id,
                "---\n" + reply[:500],
                write_reason=WRITE_REASON_IMPLICIT,
            )
            set_leading_document(request.user_id, _doc_id)
            delegations.append(ROUTE_WORKSPACE)
            logger.info(f"KimiCore: Workspace-Write (implicit) → '{_doc_id}'")

        # Freigabepunkt 2: Hilfsdokumente beim Lesen einbinden
        elif _has_read_intent and _current_leading:
            helpers = read_helper_documents(request.user_id)
            if helpers:
                _helper_summary = "\n".join(
                    f"[{did}]: {txt[:200]}" for did, txt in helpers.items()
                )
                logger.debug(f"KimiCore: {len(helpers)} Hilfsdokument(e) gelesen")
                # Hilfsdokumente fließen in nächsten AWC-Kontext ein
                from active_working_context import update_active_context
                update_active_context(
                    request.user_id,
                    last_clean_state=f"Hilfsdokumente gelesen: {list(helpers.keys())}",
                )
    except Exception as _ws_e:
        logger.debug(f"KimiCore: Workspace-Routing fehlgeschlagen (unkritisch): {_ws_e}")

    # --- WP2: AWC nach Interaktion sinnvoll befüllen ---
    # Resthärtung: keine rohen Heuristiken mehr, sondern gezielte Felder
    try:
        from active_working_context import get_active_context, update_active_context, set_active_context
        _awc_current = get_active_context(request.user_id)

        # Felder aus Eingabe + Antwort ableiten
        _text = request.text.strip()
        _reply_clean = (reply or "").replace("\n", " ").strip()

        # last_decision: nur setzen wenn Entscheidung/Richtung erkennbar
        _decision_markers = ["entschieden", "wir machen", "ab jetzt", "ich werde",
                             "das ist", "fertig", "erledigt", "gut so", "ok"]
        _has_decision = any(m in _reply_clean.lower() for m in _decision_markers)
        _last_decision = _reply_clean[:120] if _has_decision else None

        # next_open_question: aus explizitem Fragezeichen oder offenem Ende
        _sentences = [s.strip() for s in _reply_clean.split(".") if s.strip()]
        _open_q = None
        for s in reversed(_sentences):
            if "?" in s or any(w in s.lower() for w in ["was ", "wie ", "wann ", "ob "]):
                _open_q = s[:120]
                break

        # last_clean_state: sachliche Verdichtung der letzten Aussage
        _last_clean = _reply_clean[:100] if _reply_clean else None

        if _awc_current:
            # AWC existiert -- nur gezielte Felder aktualisieren
            _updates = {}
            if _last_clean:
                _updates["last_clean_state"] = _last_clean
            if _last_decision:
                _updates["last_decision"] = _last_decision
            if _open_q:
                _updates["next_open_question"] = _open_q
            # active_line: nur aktualisieren wenn derzeit leer
            if not _awc_current.get("active_line") and _text:
                _updates["active_line"] = _text[:80]
            if _updates:
                update_active_context(request.user_id, **_updates)
                logger.debug(f"KimiCore: AWC aktualisiert: {list(_updates.keys())}")
        else:
            # Kein AWC -- ersten anlegen
            set_active_context(
                request.user_id,
                active_line=_text[:80],
                active_goal="",
                active_document="",
                last_clean_state=_last_clean or "",
                last_decision=_last_decision or "",
                next_open_question=_open_q or _text[:80],
            )
            logger.debug("KimiCore: AWC initial angelegt")
    except Exception as _awc_w:
        logger.debug(f"KimiCore: AWC-Update fehlgeschlagen (unkritisch): {_awc_w}")

    # --- WP9: Post-Interaction Cognition Request ---
    # Kimi Core schreibt nach relevanten Turns einen Request in cognition_requests.
    # Der Cognitive Heartbeat (schnubot-cognition.service) pollt diese Queue.
    # Kein direkter Service-Aufruf — sauber entkoppelt (Option A).
    _post_interaction_triggers = [
        "coding_agent", "calendar_read", "todo_read", ROUTE_TOOL, ROUTE_WORKER,
    ]
    _is_significant_turn = (
        any(d in delegations for d in _post_interaction_triggers)
        or route in (ROUTE_TOOL, ROUTE_WORKER)
        or bool(request.meta.get("coding_mode"))
    )
    if _is_significant_turn:
        try:
            from core.database import get_connection as _cog_gc
            from core.datetime_utils import to_iso as _cog_iso
            import json as _cog_json
            _conn = _cog_gc()
            try:
                _source_ctx = _cog_json.dumps({
                    "route": route,
                    "delegations": delegations,
                    "text_preview": request.text[:80],
                }, ensure_ascii=False)
                # source_turn_id: letzte Chat-Message-ID als Referenz
                _turn_id = ""
                try:
                    _last_msg = _conn.execute(
                        "SELECT id FROM messages WHERE phone_number=? "
                        "ORDER BY id DESC LIMIT 1",
                        (request.user_id,)
                    ).fetchone()
                    if _last_msg:
                        _turn_id = str(_last_msg["id"])
                except Exception:
                    pass

                _conn.execute(
                    """INSERT INTO cognition_requests
                       (user_id, request_type, priority, status,
                        source_turn_id, source_context, created_at)
                       VALUES (?, ?, 'light', 'pending', ?, ?, ?)""",
                    (request.user_id, "post_interaction",
                     _turn_id, _source_ctx, _cog_iso()),
                )
                _conn.commit()
                logger.debug("KimiCore: Post-Interaction Cognition Request erstellt")
            finally:
                _conn.close()
        except Exception as _cog_e:
            logger.debug(f"KimiCore: Cognition Request fehlgeschlagen (unkritisch): {_cog_e}")

    return KimiCoreResult(
        reply=reply,
        route=route,
        turn_meta=turn_meta,
        delegations=delegations,
    )


# =============================================================================
# Verantwortlichkeiten (V2 Architektur-Dokumentation)
# =============================================================================
#
# Kimi Core:
#   - Orchestrierung aller Eingaben
#   - Fokus halten
#   - Routing-Entscheidungen (Memory/Workspace/Tool/Worker)
#   - Ergebnisintegration
#   - Antwort erzeugen
#
# Memory (ChromaDB):
#   - Erinnern / Retrieval / Verdichtung
#   - Unterstützt Core, führt nicht
#
# Workspace:
#   - Dokumente lesen/schreiben/halten
#   - Unterstützt Core, führt nicht
#
# Tools (websearch, introspect, calendar etc.):
#   - klar begrenzte Funktionalität
#   - werden von Core delegiert, führen nicht
#
# Worker (Coding Agent etc.):
#   - spezialisierte Ausführung
#   - liefern an Core zurück, sprechen nicht direkt mit Nutzer
#   - schreiben nicht direkt in Memory
#
# Active Working Context (active_working_context.py):
#   - verbindlicher Primäranker für laufende Arbeit
#   - wird VOR Memory gelesen
#   - genau ein aktiver Kontext gleichzeitig
#   - Kontextwechsel nur nach Bestätigung
#
# ORBIT (temporary_compat):
#   - technische Kompatibilitätsschicht
#   - kein Führungsrecht mehr (WP0/WP1)
#   - delete_candidate für nicht-kritische Teile
#
