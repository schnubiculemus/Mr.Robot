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
    try:
        from active_working_context import get_active_context, format_for_prompt
        awc = get_active_context(request.user_id)
        if awc:
            awc_extra = format_for_prompt(awc)
            delegations.append("awc")
            logger.debug(f"KimiCore: AWC gelesen: {awc.get('active_line','?')[:60]}")
    except Exception as _awc_e:
        logger.debug(f"KimiCore: AWC nicht verfügbar: {_awc_e}")

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

    # --- Schritt 5: Output-Interpretation ---
    # Kimi Core verarbeitet Proposals, Todos (Write), Calendar (Write) etc. aus dem Reply
    #
    # WP6 Write-Gate: write_allowed nur wenn explizite Nutzeranweisung erkannt.
    # Modell-Marker allein reichen nicht — Tommy muss es klar angefordert haben.
    _write_triggers = [
        # Todos
        "leg an", "anlegen", "erstell", "trag ein", "eintragen", "notier",
        "mach ein todo", "todo anlegen", "aufgabe anlegen", "aufgabe erstellen",
        "hak ab", "abhaken", "als erledigt", "erledigt markieren",
        "lösch das todo", "todo löschen",
        # Kalender
        "trag ein", "termin anlegen", "termin erstellen", "mach einen termin",
        "lösch den termin", "termin löschen", "termin ändern", "termin verschieben",
        "block", "blockiere",
    ]
    _text_lower = request.text.lower()
    _write_allowed = any(t in _text_lower for t in _write_triggers)
    if _write_allowed:
        logger.info(f"KimiCore: write_allowed=True (explizite Nutzeranweisung erkannt)")
    try:
        from core.kimi_output import process_kimi_output
        proc = process_kimi_output(
            source="chat",
            user_id=request.user_id,
            raw_text=reply,
            visibility="public",
            context=request.meta,
            write_allowed=_write_allowed,
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
