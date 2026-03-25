"""
coding_agent.py — WP7: Coding Agent als Worker

Der Coding Agent ist ein spezialisierter Worker für technische Arbeit.
Er wird von Kimi Core delegiert und liefert strukturierte Ergebnisse zurück.

Architekturregeln (WP7):
  - Worker, kein Orchestrator
  - Kein direkter Dialog mit dem Nutzer
  - Kein direktes Memory-Schreiben
  - Kein selbständiges Tool-Hopping
  - Keine ORBIT-Aktivierung
  - Arbeitet nur im explizit übergebenen Scope
  - Liefert immer zurück an Kimi Core

Modell: minimax-m2.7 (Coding-fokussiertes Modell, separater Call)
Kimi Core bleibt Kimi Core — minimax-m2.7 ist nur der Arbeiter im Hintergrund.

WP7-Bindungen:
  WP0: keine Hintergrundautonomie, keine Selbstaktivität
  WP1: Kimi Core bleibt führende Instanz
  WP2: Einsatz orientiert sich am aktiven Kontext
  WP3: kein direktes Memory-Schreiben
  WP4: arbeitet auf V2-Workspace, nicht Legacy
  WP5: aktiviert kein ORBIT
  WP6: ist Worker, kein Tool — getrennte Schicht
"""

import logging
import os
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# =============================================================================
# Modell-Konfiguration
# =============================================================================

CODING_AGENT_MODEL = os.getenv("CODING_AGENT_MODEL", "minimax-m2.7")
CODING_AGENT_TIMEOUT = int(os.getenv("CODING_AGENT_TIMEOUT", "180"))

# =============================================================================
# Modi
# =============================================================================

MODE_READ_ONLY   = "read_only_analysis"  # Lesen + verstehen, kein Schreiben
MODE_PATCH       = "patch"               # Gezielte Änderung einer Datei
MODE_REFACTOR    = "refactor"            # Refactoring innerhalb des Scopes
MODE_TESTS       = "tests"              # Tests schreiben
MODE_REVIEW      = "review"              # Code Review / technische Einschätzung
MODE_SCAFFOLD    = "scaffold"            # Neue Datei/Modul anlegen
MODE_EXPLAIN     = "explain_code"        # Code erklären

WRITE_MODES = {MODE_PATCH, MODE_REFACTOR, MODE_TESTS, MODE_SCAFFOLD}
READ_MODES  = {MODE_READ_ONLY, MODE_REVIEW, MODE_EXPLAIN}
ALL_MODES   = WRITE_MODES | READ_MODES

# =============================================================================
# Datenstrukturen
# =============================================================================

@dataclass
class CodingRequest:
    """Delegationsauftrag von Kimi Core an den Coding Agent."""
    owner_id: str               # User-ID
    mode: str                   # Einer der definierten Modi
    task: str                   # Was soll gemacht werden (klarer Auftrag)
    scope_files: list[str]      # Explizit erlaubte Dateien (Workspace doc_ids oder Pfade)
                                # Leer = neue Datei erlaubt (scaffold)
    active_line: str = ""       # Aktive Arbeitslinie aus AWC
    active_goal: str = ""       # Aktives Ziel aus AWC
    context_note: str = ""      # Optionaler Zusatzkontext
    return_format: str = "workspace"  # "workspace" (default) → code_file im V2-Workspace
                                      # "text" → nur als Chattext zurückgeben
    target_doc_id: str = ""     # Ziel-doc_id im V2-Workspace für neue Dateien


@dataclass
class CodingResult:
    """Rückgabe des Coding Agent an Kimi Core."""
    ok: bool
    mode: str
    output: str                          # Hauptergebnis (Code, Review, Erklärung)
    files_read: list[str] = field(default_factory=list)
    files_written: list[str] = field(default_factory=list)
    risk_note: str = ""                  # Hinweise auf Risiken / Unsicherheiten
    error: str = ""

    def to_kimi_context(self) -> str:
        """Formatiert das Ergebnis als Kontext-String für Kimi Core."""
        if not self.ok:
            return f"CODING AGENT FEHLER: {self.error}"

        parts = [f"CODING AGENT ERGEBNIS (Modus: {self.mode})\n"]

        if self.files_read:
            parts.append(f"Gelesen: {', '.join(self.files_read)}")
        if self.files_written:
            parts.append(f"Geschrieben: {', '.join(self.files_written)}")
        if self.risk_note:
            parts.append(f"Hinweis: {self.risk_note}")

        parts.append(f"\n{self.output}")
        parts.append(
            "\n\nIntegriere dieses Ergebnis in deine Antwort an Tommy. "
            "Erkläre kurz was gemacht wurde. Kein [CODE_AGENT:...] mehr schreiben."
        )
        return "\n".join(parts)


# =============================================================================
# Scope-Validierung
# =============================================================================

def _validate_scope(scope_files: list[str], mode: str = "") -> tuple[bool, str]:
    """
    Prüft ob der Scope gültig ist.

    Neue Datei (scaffold, leerer scope_files): erlaubt — target_doc_id wird als Ziel genutzt.
    Bestehende Datei: scope_files muss mindestens eine Datei enthalten.

    - Maximal 10 Dateien pro Auftrag
    - Keine absoluten Pfade außerhalb des Workspace
    """
    # scaffold darf mit leerem Scope starten — neue Datei anlegen
    if not scope_files:
        if mode == "scaffold":
            return True, ""
        return False, "Kein Scope angegeben — mindestens eine Datei erforderlich (oder mode=scaffold für neue Datei)"
    if len(scope_files) > 10:
        return False, f"Scope zu groß: {len(scope_files)} Dateien (max 10)"
    for f in scope_files:
        if f.startswith("/") and "/opt/whatsapp-bot/" not in f:
            return False, f"Datei außerhalb des erlaubten Bereichs: {f}"
    return True, ""


# =============================================================================
# Workspace-Zugriff (WP4-konform)
# =============================================================================

def _read_scope_files(owner_id: str, scope_files: list[str]) -> dict[str, str]:
    """
    Liest alle Scope-Dateien. Versucht zuerst V2-Workspace, dann direkte Pfade.
    Gibt {datei: inhalt} zurück.
    """
    from core.workspace_service import read_document
    result = {}
    for file_ref in scope_files:
        # V2-Workspace: doc_id ohne Pfad
        if not os.path.sep in file_ref and not file_ref.startswith("/"):
            content = read_document(owner_id, file_ref)
            if content:
                result[file_ref] = content
                continue

        # Direkter Pfad im Bot-Verzeichnis (nur /opt/whatsapp-bot/)
        if file_ref.startswith("/opt/whatsapp-bot/") and os.path.exists(file_ref):
            try:
                with open(file_ref, "r", encoding="utf-8") as f:
                    result[file_ref] = f.read()
            except Exception as e:
                logger.warning(f"_read_scope_files: {file_ref} nicht lesbar: {e}")
        else:
            logger.debug(f"_read_scope_files: {file_ref} nicht gefunden oder außerhalb Scope")
    return result


def _write_result_to_workspace(owner_id: str, doc_id: str, content: str) -> bool:
    """Schreibt Coding-Agent-Ergebnis in V2-Workspace (WP4-konform)."""
    from core.workspace_service import write_document, DOC_TYPE_CODE, WRITE_REASON_EXPLICIT
    return write_document(owner_id, doc_id, content,
                         doc_type=DOC_TYPE_CODE,
                         write_reason=WRITE_REASON_EXPLICIT)


# =============================================================================
# System-Prompt für den Coding Agent
# =============================================================================

def _build_system_prompt(request: CodingRequest) -> str:
    mode_instructions = {
        MODE_READ_ONLY:  "Analysiere den Code. Erkläre was er tut, wie er aufgebaut ist und wo mögliche Probleme liegen. Schreibe keinen neuen Code.",
        MODE_PATCH:      "Führe die beschriebene Änderung durch. Gib den vollständigen geänderten Code zurück. Nur die nötigsten Änderungen.",
        MODE_REFACTOR:   "Refactore den Code im angegebenen Scope. Behalte die Funktionalität bei. Erkläre kurz was du geändert hast und warum.",
        MODE_TESTS:      "Schreibe Tests für den beschriebenen Code. Verwende pytest. Decke die wichtigsten Fälle ab.",
        MODE_REVIEW:     "Führe ein Code Review durch. Fokus: Korrektheit, Lesbarkeit, mögliche Fehler, WP6/WP7-Konformität. Keine unnötigen Stilhinweise.",
        MODE_SCAFFOLD:   "Erstelle die neue Datei oder das neue Modul. Halte dich an den beschriebenen Scope und Stil.",
        MODE_EXPLAIN:    "Erkläre den Code verständlich. Keine Verbesserungsvorschläge außer wenn explizit gefragt.",
    }

    parts = [
        "Du bist ein spezialisierter Coding-Worker. Du arbeitest im Auftrag von Kimi Core.",
        "Regeln:",
        "- Antworte NUR mit dem Ergebnis deiner Arbeit (Code, Review, Erklärung)",
        "- Kein Smalltalk, keine Begrüßung, keine Zusammenfassung am Anfang",
        "- Kein direkter Dialog mit dem Nutzer",
        "- Arbeite nur im angegebenen Scope",
        "- Keine neuen Dateien außerhalb des Auftrags",
        "- Kein Markdown außer Code-Blöcken (keine Sternchen, keine Headers)",
        "",
        f"Modus: {request.mode}",
        f"Anweisung: {mode_instructions.get(request.mode, 'Führe den Auftrag aus.')}",
    ]

    if request.active_line:
        parts.append(f"Aktiver Kontext: {request.active_line}")
    if request.active_goal:
        parts.append(f"Ziel: {request.active_goal}")
    if request.context_note:
        parts.append(f"Zusatzkontext: {request.context_note}")

    return "\n".join(parts)


# =============================================================================
# Haupt-Entry-Point
# =============================================================================

def run(request: CodingRequest) -> CodingResult:
    """
    Führt einen Coding-Agent-Auftrag aus.

    Einziger Einstiegspunkt — wird nur von Kimi Core aufgerufen.
    Gibt immer CodingResult zurück — spricht nie direkt mit dem Nutzer.
    """
    # Scope validieren (scaffold darf leeren Scope haben)
    scope_ok, scope_err = _validate_scope(request.scope_files, mode=request.mode)
    if not scope_ok:
        logger.warning(f"CodingAgent: Scope-Fehler: {scope_err}")
        return CodingResult(ok=False, mode=request.mode, output="", error=scope_err)

    if request.mode not in ALL_MODES:
        return CodingResult(ok=False, mode=request.mode, output="",
                           error=f"Unbekannter Modus: {request.mode}")

    logger.info(f"CodingAgent: mode={request.mode}, scope={request.scope_files}, "
               f"model={CODING_AGENT_MODEL}")

    # Scope-Dateien lesen
    file_contents = _read_scope_files(request.owner_id, request.scope_files)
    files_read = list(file_contents.keys())

    if not file_contents and request.mode != MODE_SCAFFOLD:
        logger.warning(f"CodingAgent: Keine Scope-Dateien lesbar: {request.scope_files}")
        # Trotzdem versuchen — vielleicht ist der Auftrag datei-unabhängig

    # Prompt aufbauen
    system_prompt = _build_system_prompt(request)

    user_content_parts = [f"Auftrag: {request.task}"]
    if file_contents:
        user_content_parts.append("\nDateiinhalt(e):")
        for fname, content in file_contents.items():
            user_content_parts.append(f"\n--- {fname} ---\n{content}\n--- Ende {fname} ---")

    user_message = "\n".join(user_content_parts)

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_message},
    ]

    # Coding Agent Call (minimax-m2.7 — separater Call, kein Kimi-Kontext)
    try:
        from core.ollama_client import _call_ollama_with_model
        result = _call_ollama_with_model(messages, model=CODING_AGENT_MODEL,
                                         timeout=CODING_AGENT_TIMEOUT)
        if not result:
            return CodingResult(ok=False, mode=request.mode, output="",
                               error="Coding Agent: kein Ergebnis vom Modell")

        output = result.get("message", {}).get("content", "").strip()
        if not output:
            return CodingResult(ok=False, mode=request.mode, output="",
                               error="Coding Agent: leere Antwort")

    except Exception as e:
        logger.error(f"CodingAgent: Modell-Call fehlgeschlagen: {e}")
        return CodingResult(ok=False, mode=request.mode, output="", error=str(e))

    # Write-Modi: Ergebnis in V2-Workspace schreiben
    # Standard (return_format="workspace"): immer schreiben für alle Write-Modi
    # "text": nur als Chattext zurückgeben, kein Workspace-Write
    files_written = []
    if request.mode in WRITE_MODES and request.return_format != "text":
        # Ziel-doc_id bestimmen:
        # 1. Explizit als target_doc_id angegeben (neue Datei via scaffold)
        # 2. Aus erstem Scope-File abgeleitet (bestehende Datei)
        # 3. Fallback: coding_result
        if request.target_doc_id:
            result_doc_id = request.target_doc_id
        elif request.scope_files:
            first_scope = request.scope_files[0]
            base_name = os.path.basename(first_scope).replace(".py", "").replace(".md", "")
            result_doc_id = f"code_{base_name}"
        else:
            result_doc_id = "coding_result"

        if _write_result_to_workspace(request.owner_id, result_doc_id, output):
            files_written.append(result_doc_id)
            logger.info(f"CodingAgent: Ergebnis in Workspace geschrieben: {result_doc_id}")
        else:
            logger.warning(f"CodingAgent: Workspace-Write fehlgeschlagen für {result_doc_id}")

    logger.info(f"CodingAgent: fertig — {len(output)} Zeichen, "
               f"gelesen={files_read}, geschrieben={files_written}")

    return CodingResult(
        ok=True,
        mode=request.mode,
        output=output,
        files_read=files_read,
        files_written=files_written,
    )


# =============================================================================
# Marker-Parser (für Kimi Core)
# =============================================================================

def extract_coding_request(reply: str, owner_id: str,
                            awc: dict | None = None) -> tuple[str, "CodingRequest | None"]:
    """
    Extrahiert [CODE_AGENT: {...}] Block aus Kimis Antwort.
    Returns: (reply_cleaned, CodingRequest_or_None)

    Kimi Core ruft das auf um zu prüfen ob der Coding Agent gebraucht wird.
    """
    import re
    import json

    pattern = re.compile(r'\[CODE_AGENT:\s*(\{.*?\})\s*\]', re.DOTALL)
    match = pattern.search(reply)
    if not match:
        return reply, None

    reply_cleaned = pattern.sub("", reply).strip()
    reply_cleaned = re.sub(r'\n{3,}', '\n\n', reply_cleaned).strip()

    try:
        raw = match.group(1).replace('\n', ' ').replace('\r', '')
        payload = json.loads(raw)
    except json.JSONDecodeError as e:
        logger.warning(f"extract_coding_request: JSON-Fehler: {e}")
        return reply_cleaned, None

    mode = payload.get("mode", MODE_READ_ONLY)
    if mode not in ALL_MODES:
        logger.warning(f"extract_coding_request: Unbekannter Modus: {mode}")
        return reply_cleaned, None

    scope_files = payload.get("scope", [])
    if isinstance(scope_files, str):
        scope_files = [scope_files]

    req = CodingRequest(
        owner_id=owner_id,
        mode=mode,
        task=payload.get("task", ""),
        scope_files=scope_files,
        active_line=(awc or {}).get("active_line", ""),
        active_goal=(awc or {}).get("active_goal", ""),
        context_note=payload.get("context", ""),
        return_format=payload.get("return_format", "workspace"),
        target_doc_id=payload.get("target_doc_id", ""),
    )
    return reply_cleaned, req
