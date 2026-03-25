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

Write-Sicherheitsregeln (WP7-Freigabefix):
  - Write-Modi (scaffold, patch, refactor, tests) geben NUR reinen Dateiinhalt zurück
  - Keine Markdown-Fences, keine Erklärungstexte in der Datei
  - _extract_file_content() bereinigt den Output vor dem Workspace-Write
  - scaffold ohne target_doc_id wird blockiert

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
import re
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
MODE_REFACTOR    = "refactor"            # Refactoring — gibt reinen Dateiinhalt zurück
MODE_TESTS       = "tests"               # Tests schreiben
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
    return_format: str = "workspace"  # "workspace" → code_file im V2-Workspace (Standard)
                                      # "text"      → nur als Chattext, kein Workspace-Write
    target_doc_id: str = ""     # Ziel-doc_id im V2-Workspace (Pflicht bei scaffold)


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

def _validate_scope(scope_files: list[str], mode: str = "",
                    target_doc_id: str = "") -> tuple[bool, str]:
    """
    Prüft ob der Scope gültig ist.

    scaffold darf leeren Scope haben — aber target_doc_id muss dann gesetzt sein.
    Alle anderen Write-Modi: mindestens eine scope_file erforderlich.
    Maximal 10 Dateien pro Auftrag.
    Keine absoluten Pfade außerhalb von /opt/whatsapp-bot/.
    """
    if not scope_files:
        if mode == MODE_SCAFFOLD:
            if not target_doc_id:
                return False, (
                    "scaffold ohne scope erfordert target_doc_id "
                    "(Name der neuen Datei im Workspace)"
                )
            return True, ""
        return False, (
            "Kein Scope angegeben — mindestens eine Datei erforderlich "
            "(oder mode=scaffold + target_doc_id fuer neue Datei)"
        )
    if len(scope_files) > 10:
        return False, f"Scope zu gross: {len(scope_files)} Dateien (max 10)"
    for f in scope_files:
        if f.startswith("/") and "/opt/whatsapp-bot/" not in f:
            return False, f"Datei ausserhalb des erlaubten Bereichs: {f}"
    return True, ""


# =============================================================================
# Output-Bereinigung für Write-Modi
# =============================================================================

def _extract_file_content(raw_output: str, mode: str) -> str:
    """
    Bereinigt den Modelloutput für Write-Modi.

    Strategie:
    1. Wenn ein einzelner Codeblock vorhanden: nur dessen Inhalt nehmen
    2. Wenn mehrere Codeblöcke: alle zusammenführen
    3. Wenn kein Codeblock: raw_output als reinen Text nehmen, Fences entfernen

    Schützt vor:
    - Markdown-Codefences (```python ... ```)
    - Einleitungssätzen vor dem Code
    - Erklärungstexten nach dem Code
    """
    if mode not in WRITE_MODES:
        # Read-Modi: kein Bereinigen — Erklärungstext ist gewünscht
        return raw_output

    # Alle Codeblöcke extrahieren
    fence_pattern = re.compile(r'```[a-zA-Z0-9_]*\n?(.*?)```', re.DOTALL)
    blocks = fence_pattern.findall(raw_output)

    if blocks:
        # Codeblöcke gefunden: nur deren Inhalt, zusammengeführt
        extracted = "\n\n".join(b.strip() for b in blocks if b.strip())
        logger.debug(f"_extract_file_content: {len(blocks)} Codeblock(e) extrahiert")
        return extracted

    # Kein Codeblock — Fences ohne Sprache entfernen, Rest nehmen
    cleaned = re.sub(r'```[a-zA-Z0-9_]*\n?', '', raw_output)
    cleaned = re.sub(r'```', '', cleaned)
    cleaned = cleaned.strip()

    # Typische Einleitungszeilen entfernen (erste Zeile wenn kein Code-Zeichen)
    lines = cleaned.splitlines()
    if lines:
        first = lines[0].strip()
        # Einleitung erkennen: kein Code-Zeichen, Satz endet mit Doppelpunkt oder ist kurz prosa
        is_intro = (
            not any(c in first for c in ('=', '(', 'def ', 'class ', 'import ', '#!'))
            and (first.endswith(':') or (len(first) < 80 and not first.startswith('#')))
            and not first.startswith('def ')
            and not first.startswith('class ')
        )
        if is_intro and len(lines) > 1:
            cleaned = "\n".join(lines[1:]).strip()
            logger.debug(f"_extract_file_content: Einleitungszeile entfernt: {first[:60]}")

    return cleaned


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
        if os.path.sep not in file_ref and not file_ref.startswith("/"):
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
            logger.debug(f"_read_scope_files: {file_ref} nicht gefunden oder ausserhalb Scope")
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

# Gemeinsame Grundregel für alle Write-Modi — hart formuliert
_WRITE_MODE_OUTPUT_RULE = (
    "WICHTIG: Antworte NUR mit dem finalen Dateiinhalt. "
    "Kein Einleitungssatz. Kein Erklaerungstext. Keine Markdown-Fences (keine ```). "
    "Nur der Code/Text der direkt in die Datei geschrieben werden soll."
)

def _build_system_prompt(request: CodingRequest) -> str:
    # Modus-spezifische Anweisungen
    # Write-Modi: nur Dateiinhalt, keine Erklaerung
    # Read-Modi: Erklaerung/Analyse erwuenscht
    mode_instructions = {
        MODE_READ_ONLY: (
            "Analysiere den Code. Erklaere was er tut, wie er aufgebaut ist "
            "und wo moegliche Probleme liegen. Schreibe keinen neuen Code."
        ),
        MODE_PATCH: (
            "Fuehre die beschriebene Aenderung durch. "
            "Gib den vollstaendigen geaenderten Dateiinhalt zurueck. "
            "Nur die noetigen Aenderungen. " + _WRITE_MODE_OUTPUT_RULE
        ),
        MODE_REFACTOR: (
            "Refactore den Code im angegebenen Scope. Behalte die Funktionalitaet bei. "
            "Gib den vollstaendigen refactorten Dateiinhalt zurueck. "
            "Keine Erklaerung des Refactorings im Output — nur der fertige Code. "
            + _WRITE_MODE_OUTPUT_RULE
        ),
        MODE_TESTS: (
            "Schreibe Tests fuer den beschriebenen Code. Verwende pytest. "
            "Decke die wichtigsten Faelle ab. "
            + _WRITE_MODE_OUTPUT_RULE
        ),
        MODE_REVIEW: (
            "Fuehre ein Code Review durch. "
            "Fokus: Korrektheit, Lesbarkeit, moegliche Fehler. "
            "Keine unnötigen Stilhinweise."
        ),
        MODE_SCAFFOLD: (
            "Erstelle die neue Datei. Halte dich an den beschriebenen Stil und Auftrag. "
            + _WRITE_MODE_OUTPUT_RULE
        ),
        MODE_EXPLAIN: (
            "Erklaere den Code verstaendlich. "
            "Keine Verbesserungsvorschlaege ausser wenn explizit gefragt."
        ),
    }

    parts = [
        "Du bist ein spezialisierter Coding-Worker. Du arbeitest im Auftrag von Kimi Core.",
        "Basisregeln:",
        "- Kein Smalltalk, keine Begruessung",
        "- Kein direkter Dialog mit dem Nutzer",
        "- Arbeite nur im angegebenen Scope",
        "- Keine neuen Dateien ausserhalb des Auftrags",
        "",
        f"Modus: {request.mode}",
        f"Anweisung: {mode_instructions.get(request.mode, 'Fuehre den Auftrag aus.')}",
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
    Fuehrt einen Coding-Agent-Auftrag aus.

    Einziger Einstiegspunkt — wird nur von Kimi Core aufgerufen.
    Gibt immer CodingResult zurueck — spricht nie direkt mit dem Nutzer.
    """
    # Scope validieren (scaffold ohne Scope nur mit target_doc_id)
    scope_ok, scope_err = _validate_scope(
        request.scope_files, mode=request.mode, target_doc_id=request.target_doc_id
    )
    if not scope_ok:
        logger.warning(f"CodingAgent: Scope-Fehler: {scope_err}")
        return CodingResult(ok=False, mode=request.mode, output="", error=scope_err)

    if request.mode not in ALL_MODES:
        return CodingResult(ok=False, mode=request.mode, output="",
                            error=f"Unbekannter Modus: {request.mode}")

    logger.info(f"CodingAgent: mode={request.mode}, scope={request.scope_files}, "
                f"target={request.target_doc_id}, model={CODING_AGENT_MODEL}")

    # Scope-Dateien lesen
    file_contents = _read_scope_files(request.owner_id, request.scope_files)
    files_read = list(file_contents.keys())

    if not file_contents and request.mode != MODE_SCAFFOLD:
        logger.warning(f"CodingAgent: Keine Scope-Dateien lesbar: {request.scope_files}")

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

        raw_output = result.get("message", {}).get("content", "").strip()
        if not raw_output:
            return CodingResult(ok=False, mode=request.mode, output="",
                                error="Coding Agent: leere Antwort")

    except Exception as e:
        logger.error(f"CodingAgent: Modell-Call fehlgeschlagen: {e}")
        return CodingResult(ok=False, mode=request.mode, output="", error=str(e))

    # Write-Modi: Output bereinigen BEVOR er in den Workspace geht
    if request.mode in WRITE_MODES:
        output = _extract_file_content(raw_output, request.mode)
        logger.debug(f"CodingAgent: Output bereinigt: {len(raw_output)} → {len(output)} Zeichen")
    else:
        output = raw_output

    # Workspace-Write für Write-Modi (Standard: return_format="workspace")
    files_written = []
    if request.mode in WRITE_MODES and request.return_format != "text":
        # Ziel-doc_id bestimmen:
        # 1. Explizit als target_doc_id (neue Datei via scaffold — bereits validiert)
        # 2. Aus erstem Scope-File ableiten (bestehende Datei)
        if request.target_doc_id:
            result_doc_id = request.target_doc_id
        elif request.scope_files:
            first_scope = request.scope_files[0]
            base_name = os.path.basename(first_scope).replace(".py", "").replace(".md", "")
            result_doc_id = f"code_{base_name}"
        else:
            # Sollte durch Validierung abgefangen sein — defensiver Fallback
            result_doc_id = "coding_result"
            logger.warning("CodingAgent: Fallback auf 'coding_result' — target_doc_id fehlt")

        if output:
            if _write_result_to_workspace(request.owner_id, result_doc_id, output):
                files_written.append(result_doc_id)
                logger.info(f"CodingAgent: code_file geschrieben: '{result_doc_id}' "
                            f"({len(output)} Zeichen)")
            else:
                logger.warning(f"CodingAgent: Workspace-Write fehlgeschlagen: {result_doc_id}")
        else:
            logger.warning("CodingAgent: Output nach Bereinigung leer — kein Workspace-Write")

    logger.info(f"CodingAgent: fertig — gelesen={files_read}, geschrieben={files_written}")

    return CodingResult(
        ok=True,
        mode=request.mode,
        output=output,
        files_read=files_read,
        files_written=files_written,
    )


# =============================================================================
# Marker-Parser (fuer Kimi Core)
# =============================================================================

def extract_coding_request(reply: str, owner_id: str,
                             awc: dict | None = None) -> tuple[str, "CodingRequest | None"]:
    """
    Extrahiert [CODE_AGENT: {...}] Block aus Kimis Antwort.
    Returns: (reply_cleaned, CodingRequest_or_None)

    Kimi Core ruft das auf um zu pruefen ob der Coding Agent gebraucht wird.
    """
    pattern = re.compile(r'\[CODE_AGENT:\s*(\{.*?\})\s*\]', re.DOTALL)
    match = pattern.search(reply)
    if not match:
        return reply, None

    reply_cleaned = pattern.sub("", reply).strip()
    reply_cleaned = re.sub(r'\n{3,}', '\n\n', reply_cleaned).strip()

    import json
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
