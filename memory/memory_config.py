"""
SchnuBot.ai - Neurales Gedaechtnis: Zentrale Konfiguration
Referenz: SchnuBot_Konzept_Neurales_Gedaechtnis_V1.1

Alle Parameter fuer Retrieval, Konsolidierung, Decay und Merge.
Bestehende config.py bleibt unangetastet - diese Datei ergaenzt sie.
"""

# =============================================================================
# Embedding
# =============================================================================
EMBEDDING_MODEL = "nomic-ai/nomic-embed-text-v1.5"
EMBEDDING_DIM = 768

# =============================================================================
# ChromaDB
# =============================================================================
CHROMA_PERSIST_DIR = "/opt/whatsapp-bot/data/chromadb"
COLLECTION_ACTIVE = "memory_active"
COLLECTION_ARCHIVE = "memory_archive"

# =============================================================================
# Konsolidierungsmodell
# =============================================================================
CONSOLIDATION_MODEL = "qwen3.5:122b"
CONSOLIDATION_SPEC_VERSION = "1.1"

# =============================================================================
# Chunk-Typen
# =============================================================================
CHUNK_TYPES = [
    "hard_fact",
    "preference",
    "decision",
    "working_state",
    "self_reflection",
    "knowledge",
]

# =============================================================================
# Quellen (Source)
# =============================================================================
VALID_SOURCES = ["tommy", "robot", "shared"]

# =============================================================================
# Epistemic Status - Belastbarkeit der Information (Abschnitt 6.4)
# =============================================================================
EPISTEMIC_STATUS = {
    "confirmed":   1.00,
    "stated":      0.80,
    "inferred":    0.55,
    "speculative": 0.30,
    "outdated":    0.10,
}

# =============================================================================
# Confidence-Schwellen - Mindestsicherheit pro Typ (Abschnitt 6.3)
# =============================================================================
CONFIDENCE_THRESHOLDS = {
    "decision":        0.85,
    "hard_fact":       0.75,
    "knowledge":       0.70,
    "preference":      0.70,
    "working_state":   0.60,
    "self_reflection": 0.40,
}

CONFIDENCE_GLOBAL_MIN = 0.40
CONFIDENCE_MAX = 0.99

# =============================================================================
# Retrieval-Score - Gewichte der 6 Faktoren (Abschnitt 8.2)
# =============================================================================
RETRIEVAL_WEIGHTS = {
    "semantic":    0.45,
    "epistemic":   0.15,
    "weight":      0.13,
    "recency":     0.12,
    "confidence":  0.08,
    "type_factor": 0.07,
}

# =============================================================================
# Retrieval - Caps (Abschnitt 8.1)
# =============================================================================
GLOBAL_MAX_CHUNKS = 30
MIN_CHUNKS_IF_AVAILABLE = 3

TYPE_CAPS = {
    "knowledge":       25,
    "hard_fact":       14,
    "decision":         8,
    "working_state":    8,
    "preference":       7,
    "self_reflection":  4,
}

# =============================================================================
# Statische Typ-Faktoren (Abschnitt 8.4)
# =============================================================================
TYPE_FACTORS = {
    "decision":        1.00,
    "knowledge":       0.95,
    "hard_fact":       0.90,
    "preference":      0.80,
    "working_state":   0.70,
    "self_reflection": 0.60,
}

# =============================================================================
# Recency - Aktualitaetsfaktor (Abschnitt 8.3)
# =============================================================================
RECENCY_HORIZON_DAYS = 90
RECENCY_MINIMUM = 0.10

# =============================================================================
# Type Decay - typspezifische Alterung (Abschnitt 8.5)
# =============================================================================
TYPE_DECAY = {
    "working_state":   {"horizon_days": 21,  "minimum": 0.10},
    "self_reflection": {"horizon_days": 60,  "minimum": 0.20},
    "preference":      {"horizon_days": 180, "minimum": 0.40},
}

# =============================================================================
# Weight-Modell (Abschnitt 10)
# =============================================================================
WEIGHT_BASELINES = {
    "decision":        1.30,
    "knowledge":       1.20,
    "hard_fact":       1.15,
    "preference":      1.00,
    "working_state":   0.90,
    "self_reflection": 0.85,
}

WEIGHT_ADJUSTMENTS = {
    "confirm": 0.05,
    "update":  0.02,
}

WEIGHT_MAX = 2.0

# =============================================================================
# Confidence-Dynamik (Abschnitt 11)
# =============================================================================
CONFIDENCE_ADJUSTMENTS = {
    "confirm": 0.03,
    "update_blend": {"old": 0.7, "new": 0.3},
}

# =============================================================================
# Merge/Update (Abschnitt 12)
# =============================================================================
MERGE_SIMILARITY_THRESHOLD = 0.84
MERGE_MAX_CANDIDATES = 5

# =============================================================================
# Konsolidierer - Buffer und Limits (Abschnitt 13)
# =============================================================================
BUFFER_MAX_TURNS_PER_BLOCK = 20
CONSOLIDATION_MAX_ACTIONS_PER_BLOCK = 10

# Lazy Consolidation (Abschnitt 13.3)
LAZY_CONSOLIDATION_THRESHOLD = 50
LAZY_MIN_WORD_COUNT = 20
LAZY_FALLBACK_HOURS = 48

# Fast-Track (Abschnitt 13.3.1)
FAST_TRACK_MAX_PER_CHAT = 3
FAST_TRACK_CONFIDENCE_PENALTY = 0.05

# Confidence-Sofortkorrektur (Abschnitt 13.3.2)
CONFIDENCE_CORRECTION_MAX = 0.20

# =============================================================================
# Tags (Abschnitt 14)
# =============================================================================
TAGS_MAX_PER_CHUNK = 5

# =============================================================================
# Prompt-Einbindung - Reihenfolge (Abschnitt 9)
# =============================================================================
PROMPT_TYPE_ORDER = [
    "decision",
    "knowledge",
    "working_state",
    "hard_fact",
    "preference",
    "self_reflection",
]

# =============================================================================
# Archivierung (Abschnitt 7.3)
# =============================================================================
ARCHIVE_WORKING_STATE_DAYS = 30

# =============================================================================
# Index-Hygiene (Abschnitt 7.4)
# =============================================================================
INDEX_REBUILD_THRESHOLD = 5000
INDEX_TIGHTEN_THRESHOLD = 10000

# =============================================================================
# Soul-Vorschlaege (Abschnitt 17.3)
# =============================================================================
SOUL_MAX_OPEN_PROPOSALS = 1
SOUL_MAX_OPERATIONAL_PER_WEEK = 2
SOUL_PROPOSAL_REMINDER_HOURS = 48

# =============================================================================
# Autonome Task-Initiierung (Abschnitt 18)
# =============================================================================
TASK_MAX_PROPOSALS_PER_DAY = 2

# =============================================================================
# Backups (Abschnitt 19)
# =============================================================================
BACKUP_DAILY_RETENTION = 7
BACKUP_WEEKLY_RETENTION = 4

# =============================================================================
# Retrieval Fallback (Abschnitt 8.6)
# =============================================================================
FALLBACK_THRESHOLD_REDUCTION = 0.10

# =============================================================================
# Prompt-Texte (Abschnitt 9)
# =============================================================================
PROMPT_MEMORY_HEADER = "# Gedächtnis"
PROMPT_REFLECTION_HEADER = "# Interne Reflexionen von Mr. Robot"
PROMPT_REFLECTION_HINT = "(Das sind deine eigenen Gedanken, keine Aussagen von Tommy)"
