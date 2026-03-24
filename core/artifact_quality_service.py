"""
7.5.8: Artefakt-Qualitätsgate
Bewertet ob ein Artefakt als workspace-sichtbar gilt oder system/hidden bleibt.
"""
import re

# Mindest-Zeichenlänge pro Typ
CONTENT_MIN_LENGTH = {
    "brief":          150,
    "analysis":       200,
    "plan":           150,
    "implementation": 100,
    "result":         100,
    "report":         150,
    "worklog":        30,
}

# Harte Negativsignale -- wenn enthalten -> kein workspace-Artefakt
NEGATIVE_SIGNALS = [
    r"automatisch beim start angelegt",
    r"automatisch angelegt",
    r"\*automatisch",
    r"task:\s+[0-9a-f]{8}",
    r"alle steps terminal",
    r"task completed",
    r"recovery:",
    r"\btbd\b",
    r"^#\s+abschluss:.*\n\n\*\*task:\*\*",  # reines recovery-result
]

# Recovery-artige Herkunft
RECOVERY_ORIGINS = {"recovery", "auto_trigger"}
RECOVERY_PURPOSES = {"recovery_summary", "diagnostics"}


def evaluate_artifact_quality(
    *,
    artifact_type: str,
    content: str,
    content_origin: str = "manual",
    purpose: str = None,
) -> dict:
    """
    Bewertet Artefakt-Qualität.
    Gibt dict zurück mit:
      allow_workspace_visibility: bool
      visibility_class: workspace | system | hidden
      quality_state: raw | draft | reviewable | publishable | rejected
      reasons: list[str]
      signals: dict
    """
    reasons = []
    content_lower = (content or "").lower().strip()
    content_len = len(content_lower)

    # Signal: Placeholder
    is_placeholder = any(re.search(sig, content_lower) for sig in NEGATIVE_SIGNALS)

    # Signal: Recovery-ähnlich
    is_recovery_like = (
        content_origin in RECOVERY_ORIGINS
        or (purpose or "") in RECOVERY_PURPOSES
        or re.search(r"recovery|diagnostics|stale|trigger.note", content_lower) is not None
    )

    # Signal: Mindestsubstanz
    min_len = CONTENT_MIN_LENGTH.get(artifact_type, 50)
    has_minimum_substance = content_len >= min_len

    # Entscheidung
    allow_workspace = True
    visibility_class = "workspace"
    quality_state = "draft"

    if is_placeholder:
        reasons.append("Placeholder-Text erkannt")
        quality_state = "raw"
        if artifact_type in ("result", "report"):
            allow_workspace = False
            visibility_class = "system"
            quality_state = "rejected"

    if is_recovery_like and artifact_type in ("result", "report"):
        reasons.append("Recovery-Herkunft für result/report nicht erlaubt")
        allow_workspace = False
        visibility_class = "system"
        quality_state = "rejected"

    if not has_minimum_substance:
        reasons.append(f"Inhalt zu kurz ({content_len} < {min_len} Zeichen)")
        quality_state = "raw"
        if artifact_type in ("result", "report"):
            allow_workspace = False
            visibility_class = "system"

    if allow_workspace and not reasons:
        quality_state = "reviewable"

    return {
        "allow_workspace_visibility": allow_workspace,
        "visibility_class": visibility_class,
        "quality_state": quality_state,
        "reasons": reasons,
        "signals": {
            "is_placeholder": is_placeholder,
            "is_recovery_like": is_recovery_like,
            "has_minimum_substance": has_minimum_substance,
        }
    }
