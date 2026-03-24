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

    # --- Schritt 4: Output-Interpretation ---
    # Kimi Core verarbeitet Proposals, Todos, etc. aus dem Reply
    try:
        from core.kimi_output import process_kimi_output
        proc = process_kimi_output(
            source="chat",
            user_id=request.user_id,
            raw_text=reply,
            visibility="public",
            context=request.meta,
        )
        reply = proc.to_reply()
    except Exception as e:
        logger.warning(f"KimiCore: process_kimi_output fehlgeschlagen: {e}")

    # --- WP2: AWC nach Interaktion aktualisieren ---
    # Einfache Heuristik: last_decision aus Antwort ableiten, next_open_question setzen
    try:
        from active_working_context import get_active_context, update_active_context
        _awc_current = get_active_context(request.user_id)
        if _awc_current:
            # AWC existiert -- last_decision aus Reply-Kurzform aktualisieren
            _reply_short = (reply[:150] if reply else "").replace("\n", " ").strip()
            update_active_context(
                request.user_id,
                last_decision=f"Letzte Antwort: {_reply_short}",
            )
        else:
            # Kein AWC -- ersten aus Eingabe ableiten
            from active_working_context import set_active_context
            set_active_context(
                request.user_id,
                active_line=request.text[:80],
                active_goal="",
                active_document="",
                last_clean_state="",
                last_decision="",
                next_open_question=request.text[:80],
            )
            logger.debug("KimiCore: AWC initial aus Eingabe angelegt")
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
