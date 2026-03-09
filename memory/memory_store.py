"""
SchnuBot.ai - Memory Store
Referenz: Konzeptdokument V1.1, Abschnitte 1, 7, 8

ChromaDB-Wrapper: Zwei Collections (memory_active, memory_archive),
Chunk-CRUD, Archivierung, semantische Suche.
Embedding via nomic-embed-text-v1.5 (sentence-transformers).
"""

import os
import logging
import threading
from datetime import datetime, timezone

import chromadb
from chromadb.config import Settings
from sentence_transformers import SentenceTransformer

from memory.memory_config import (
    CHROMA_PERSIST_DIR,
    COLLECTION_ACTIVE,
    COLLECTION_ARCHIVE,
    EMBEDDING_MODEL,
    EMBEDDING_DIM,
    EMBEDDING_BATCH_SIZE,
    MERGE_SIMILARITY_THRESHOLD,
    MERGE_MAX_CANDIDATES,
)
from memory.chunk_schema import chunk_to_metadata, metadata_to_tags

logger = logging.getLogger(__name__)

# =============================================================================
# Globals (Lazy Init, Thread-Safe)
# =============================================================================
_lock = threading.RLock()
_client = None
_active_collection = None
_archive_collection = None
_embedder = None


# =============================================================================
# Initialisierung
# =============================================================================

def _ensure_persist_dir():
    """Erstellt das ChromaDB-Verzeichnis falls noetig."""
    os.makedirs(CHROMA_PERSIST_DIR, exist_ok=True)


def get_client():
    """Gibt den ChromaDB-Client zurueck (Singleton, thread-safe)."""
    global _client
    if _client is None:
        with _lock:
            if _client is None:
                _ensure_persist_dir()
                _client = chromadb.PersistentClient(
                    path=CHROMA_PERSIST_DIR,
                    settings=Settings(anonymized_telemetry=False),
                )
                logger.info(f"ChromaDB Client initialisiert: {CHROMA_PERSIST_DIR}")
    return _client


def get_active_collection():
    """Gibt die aktive Collection zurueck (Singleton, thread-safe)."""
    global _active_collection
    if _active_collection is None:
        with _lock:
            if _active_collection is None:
                client = get_client()
                _active_collection = client.get_or_create_collection(
                    name=COLLECTION_ACTIVE,
                    metadata={"hnsw:space": "cosine"},
                )
                count = _active_collection.count()
                logger.info(f"Collection '{COLLECTION_ACTIVE}' geladen: {count} Chunks")
    return _active_collection


def get_archive_collection():
    """Gibt die Archiv-Collection zurueck (Singleton, thread-safe)."""
    global _archive_collection
    if _archive_collection is None:
        with _lock:
            if _archive_collection is None:
                client = get_client()
                _archive_collection = client.get_or_create_collection(
                    name=COLLECTION_ARCHIVE,
                    metadata={"hnsw:space": "cosine"},
                )
                count = _archive_collection.count()
                logger.info(f"Collection '{COLLECTION_ARCHIVE}' geladen: {count} Chunks")
    return _archive_collection


def get_embedder():
    """Laedt das Embedding-Modell (Singleton, thread-safe, ~275 MB beim ersten Mal)."""
    global _embedder
    if _embedder is None:
        with _lock:
            if _embedder is None:
                logger.info(f"Lade Embedding-Modell: {EMBEDDING_MODEL} ...")
                _embedder = SentenceTransformer(EMBEDDING_MODEL, trust_remote_code=True)
                logger.info(f"Embedding-Modell geladen (Dim: {EMBEDDING_DIM})")
    return _embedder


# =============================================================================
# Embedding
# =============================================================================

def embed_text(text):
    """Erzeugt einen Embedding-Vektor fuer einen Text."""
    model = get_embedder()
    # nomic-embed-text erwartet task-prefix fuer beste Ergebnisse
    prefixed = f"search_document: {text}"
    vector = model.encode(prefixed, normalize_embeddings=True)
    return vector.tolist()


def embed_query(query):
    """Erzeugt einen Embedding-Vektor fuer eine Suchanfrage."""
    model = get_embedder()
    prefixed = f"search_query: {query}"
    vector = model.encode(prefixed, normalize_embeddings=True)
    return vector.tolist()


def embed_texts(texts, batch_size=None, prefix="search_document"):
    """
    Batch-Embedding fuer eine Liste von Texten.

    Zentrale Funktion fuer alle Embedding-Bedarfe ausser Queries:
    Dokument-Chunks, Voice-Transkripte, Mail-Inhalte, Web-Snippets.

    Args:
        texts:      Liste von Strings. Reihenfolge bleibt garantiert erhalten.
        batch_size: Chunks pro Batch. Default: EMBEDDING_BATCH_SIZE aus Config.
        prefix:     Task-Prefix fuer nomic-embed-text. Standard: search_document.

    Returns:
        Liste von Embedding-Vektoren (list[float]). Laenge == len(texts).
        Leere Liste [] bei Fehler eines einzelnen Chunks (kein Abbruch des Rests).
    """
    if not texts:
        return []

    if batch_size is None:
        batch_size = EMBEDDING_BATCH_SIZE

    embedder = get_embedder()
    total = len(texts)
    n_batches = (total + batch_size - 1) // batch_size
    logger.info(f"Batch-Embedding: {total} Texte in {n_batches} Batches (batch_size={batch_size})")

    # Prefixe vorbereiten — einmalig, konsistent
    prefixed = [f"{prefix}: {t}" for t in texts]

    results = [None] * total  # Reihenfolge-stabile Ergebnisliste
    t_start = __import__("time").time()

    for batch_idx in range(n_batches):
        start = batch_idx * batch_size
        end = min(start + batch_size, total)
        batch_texts = prefixed[start:end]

        try:
            batch_vectors = embedder.encode(
                batch_texts,
                batch_size=batch_size,
                normalize_embeddings=True,
                show_progress_bar=False,
            )
            for i, vec in enumerate(batch_vectors):
                results[start + i] = vec.tolist() if hasattr(vec, "tolist") else list(vec)
            logger.debug(f"Batch {batch_idx + 1}/{n_batches} fertig ({end - start} Chunks)")

        except Exception as e:
            logger.warning(f"Batch {batch_idx + 1}/{n_batches} fehlgeschlagen: {e} — Fallback auf Einzelverarbeitung")
            # Fallback: Chunk fuer Chunk, damit der Rest nicht verloren geht
            for i, text in enumerate(batch_texts):
                try:
                    vec = embedder.encode(text, normalize_embeddings=True)
                    results[start + i] = vec.tolist() if hasattr(vec, "tolist") else list(vec)
                except Exception as e2:
                    logger.error(f"Einzelembedding fehlgeschlagen (Chunk {start + i}): {e2}")
                    results[start + i] = []  # Leere Liste markiert Fehler

    elapsed = __import__("time").time() - t_start
    success = sum(1 for r in results if r)
    logger.info(f"Batch-Embedding abgeschlossen: {success}/{total} erfolgreich in {elapsed:.2f}s")

    return results


# =============================================================================
# CRUD: Chunks speichern / laden / loeschen
# =============================================================================

def store_chunk(chunk):
    """
    Speichert einen Chunk in der aktiven Collection.
    Erzeugt Embedding aus chunk['text'].
    """
    collection = get_active_collection()
    embedding = embed_text(chunk["text"])
    metadata = chunk_to_metadata(chunk)

    # Optionale Felder in Metadata aufnehmen
    if "supersedes" in chunk:
        metadata["supersedes"] = chunk["supersedes"]
    if "expires_at" in chunk:
        metadata["expires_at"] = chunk["expires_at"]
    if "last_confirmed_at" in chunk:
        metadata["last_confirmed_at"] = chunk["last_confirmed_at"]

    collection.upsert(
        ids=[chunk["id"]],
        documents=[chunk["text"]],
        embeddings=[embedding],
        metadatas=[metadata],
    )
    logger.info(
        f"Chunk gespeichert: {chunk['id'][:8]}... "
        f"[{chunk['chunk_type']}] [{chunk['epistemic_status']}] "
        f"conf={chunk['confidence']}"
    )
    return chunk["id"]


def get_chunk(chunk_id):
    """Holt einen einzelnen Chunk aus der aktiven Collection."""
    collection = get_active_collection()
    try:
        result = collection.get(ids=[chunk_id], include=["documents", "metadatas"])
        if not result["ids"]:
            return None
        return _result_to_chunk(result, 0)
    except Exception as e:
        logger.error(f"Fehler beim Laden von Chunk {chunk_id}: {e}")
        return None


def update_chunk(chunk):
    """
    Aktualisiert einen bestehenden Chunk (Metadata + ggf. Text).
    Re-embedded bei Textaenderung.
    """
    collection = get_active_collection()
    embedding = embed_text(chunk["text"])
    metadata = chunk_to_metadata(chunk)

    if "supersedes" in chunk:
        metadata["supersedes"] = chunk["supersedes"]
    if "expires_at" in chunk:
        metadata["expires_at"] = chunk["expires_at"]
    if "last_confirmed_at" in chunk:
        metadata["last_confirmed_at"] = chunk["last_confirmed_at"]

    collection.upsert(
        ids=[chunk["id"]],
        documents=[chunk["text"]],
        embeddings=[embedding],
        metadatas=[metadata],
    )
    logger.info(f"Chunk aktualisiert: {chunk['id'][:8]}...")
    return chunk["id"]


def delete_chunk(chunk_id):
    """Loescht einen Chunk aus der aktiven Collection (fuer Index-Hygiene)."""
    collection = get_active_collection()
    try:
        collection.delete(ids=[chunk_id])
        logger.info(f"Chunk geloescht: {chunk_id[:8]}...")
        return True
    except Exception as e:
        logger.error(f"Fehler beim Loeschen von Chunk {chunk_id}: {e}")
        return False


# =============================================================================
# Archivierung (Abschnitt 7.4 - Index-Hygiene)
# =============================================================================

def archive_chunk(chunk_id):
    """
    Verschiebt einen Chunk von active -> archive.
    1. Aus active lesen (inkl. Embedding)
    2. In archive schreiben
    3. Aus active loeschen
    """
    active = get_active_collection()
    archive = get_archive_collection()

    try:
        result = active.get(
            ids=[chunk_id],
            include=["documents", "metadatas", "embeddings"],
        )
        if not result["ids"]:
            logger.warning(f"Chunk {chunk_id[:8]}... nicht in active gefunden")
            return False

        # Status auf archived setzen
        metadata = result["metadatas"][0]
        metadata["status"] = "archived"

        # In Archive schreiben
        archive.upsert(
            ids=[chunk_id],
            documents=result["documents"],
            embeddings=result["embeddings"],
            metadatas=[metadata],
        )

        # Aus Active loeschen
        active.delete(ids=[chunk_id])

        logger.info(f"Chunk archiviert: {chunk_id[:8]}... [{metadata.get('chunk_type', '?')}]")
        return True

    except Exception as e:
        logger.error(f"Fehler bei Archivierung von {chunk_id}: {e}")
        return False


# =============================================================================
# Semantische Suche
# =============================================================================

def query_active(query_text, n_results=30, where_filter=None):
    """
    Semantische Suche in der aktiven Collection.

    Args:
        query_text:   Suchanfrage (wird embedded)
        n_results:    Max. Anzahl Ergebnisse
        where_filter: Optionaler ChromaDB where-Filter (z.B. {"chunk_type": "decision"})

    Returns:
        Liste von Dicts: [{chunk-felder}, ...] sortiert nach semantischer Naehe
    """
    collection = get_active_collection()
    query_embedding = embed_query(query_text)

    kwargs = {
        "query_embeddings": [query_embedding],
        "n_results": min(n_results, collection.count() or 1),
        "include": ["documents", "metadatas", "distances"],
    }
    if where_filter:
        kwargs["where"] = where_filter

    try:
        results = collection.query(**kwargs)
    except Exception as e:
        logger.error(f"Query-Fehler: {e}")
        return []

    chunks = []
    if results["ids"] and results["ids"][0]:
        for i in range(len(results["ids"][0])):
            chunk = _result_to_chunk_from_query(results, i)
            chunk["_semantic_distance"] = results["distances"][0][i]
            chunk["_semantic_similarity"] = 1.0 - results["distances"][0][i]
            chunks.append(chunk)

    # retrieved_count + last_retrieved_at aktualisieren (fire-and-forget)
    if chunks:
        now_iso = datetime.now(timezone.utc).isoformat()
        try:
            collection = get_active_collection()
            for chunk in chunks:
                old_meta = dict(chunk)
                new_count = int(old_meta.get("retrieved_count", 0)) + 1
                collection.update(
                    ids=[chunk["id"]],
                    metadatas=[{
                        **{k: v for k, v in old_meta.items() if not k.startswith("_")},
                        "retrieved_count": new_count,
                        "last_retrieved_at": now_iso,
                    }],
                )
                chunk["retrieved_count"] = new_count
                chunk["last_retrieved_at"] = now_iso
        except Exception as e:
            logger.debug(f"retrieved_count Update fehlgeschlagen: {e}")

    return chunks


def query_archive(query_text, n_results=10):
    """
    Semantische Suche in der Archiv-Collection (Historienmodus, Abschnitt 7.5).
    Nur bei expliziten Historienfragen.
    """
    collection = get_archive_collection()
    if collection.count() == 0:
        return []

    query_embedding = embed_query(query_text)

    try:
        results = collection.query(
            query_embeddings=[query_embedding],
            n_results=min(n_results, collection.count()),
            include=["documents", "metadatas", "distances"],
        )
    except Exception as e:
        logger.error(f"Archive-Query-Fehler: {e}")
        return []

    chunks = []
    if results["ids"] and results["ids"][0]:
        for i in range(len(results["ids"][0])):
            chunk = _result_to_chunk_from_query(results, i)
            chunk["_semantic_similarity"] = 1.0 - results["distances"][0][i]
            chunks.append(chunk)

    return chunks


# =============================================================================
# Merge-Kandidaten (Abschnitt 12.1, 13.11)
# =============================================================================

def find_merge_candidates(chunk_text, chunk_type, exclude_id=None):
    """
    Findet potenzielle Merge-Kandidaten: gleicher Typ + Aehnlichkeit >= Schwelle.
    Max MERGE_MAX_CANDIDATES Ergebnisse.
    """
    collection = get_active_collection()
    if collection.count() == 0:
        return []

    query_embedding = embed_query(chunk_text)

    try:
        results = collection.query(
            query_embeddings=[query_embedding],
            n_results=min(MERGE_MAX_CANDIDATES * 2, collection.count()),
            where={"chunk_type": chunk_type},
            include=["documents", "metadatas", "distances"],
        )
    except Exception as e:
        logger.error(f"Merge-Kandidaten-Fehler: {e}")
        return []

    candidates = []
    if results["ids"] and results["ids"][0]:
        for i in range(len(results["ids"][0])):
            cid = results["ids"][0][i]
            if cid == exclude_id:
                continue

            similarity = 1.0 - results["distances"][0][i]
            if similarity < MERGE_SIMILARITY_THRESHOLD:
                continue

            candidate = _result_to_chunk_from_query(results, i)
            candidate["_merge_similarity"] = similarity
            candidates.append(candidate)

            if len(candidates) >= MERGE_MAX_CANDIDATES:
                break

    return candidates


# =============================================================================
# Stats & Monitoring
# =============================================================================

def get_stats():
    """Gibt Statistiken ueber beide Collections zurueck."""
    active = get_active_collection()
    archive = get_archive_collection()

    return {
        "active_count": active.count(),
        "archive_count": archive.count(),
        "total_count": active.count() + archive.count(),
    }


# =============================================================================
# Hilfsfunktionen
# =============================================================================

def _result_to_chunk(result, index):
    """Konvertiert ein ChromaDB get-Ergebnis zurueck in ein Chunk-Dict."""
    metadata = result["metadatas"][index]
    return {
        "id": result["ids"][index],
        "text": result["documents"][index],
        "chunk_type": metadata.get("chunk_type"),
        "source": metadata.get("source"),
        "status": metadata.get("status", "active"),
        "weight": _safe_float(metadata.get("weight", 1.0), 1.0),
        "confidence": _safe_float(metadata.get("confidence", 0.5), 0.5),
        "epistemic_status": metadata.get("epistemic_status", "stated"),
        "created_at": metadata.get("created_at", ""),
        "tags": metadata_to_tags(metadata.get("tags", "")),
        # Optionale Felder
        **{k: metadata[k] for k in ("supersedes", "expires_at", "last_confirmed_at")
           if k in metadata},
    }


def _result_to_chunk_from_query(results, index):
    """Konvertiert ein ChromaDB query-Ergebnis zurueck in ein Chunk-Dict."""
    metadata = results["metadatas"][0][index]
    return {
        "id": results["ids"][0][index],
        "text": results["documents"][0][index],
        "chunk_type": metadata.get("chunk_type"),
        "source": metadata.get("source"),
        "status": metadata.get("status", "active"),
        "weight": _safe_float(metadata.get("weight", 1.0), 1.0),
        "confidence": _safe_float(metadata.get("confidence", 0.5), 0.5),
        "epistemic_status": metadata.get("epistemic_status", "stated"),
        "created_at": metadata.get("created_at", ""),
        "tags": metadata_to_tags(metadata.get("tags", "")),
        **{k: metadata[k] for k in ("supersedes", "expires_at", "last_confirmed_at")
           if k in metadata},
    }


def _safe_float(value, default):
    """Castet einen Wert sicher zu float. Fängt str-Altlasten aus Decay-Bug ab."""
    try:
        return float(value)
    except (ValueError, TypeError):
        return default


def get_all_active():
    """Gibt alle aktiven Chunks zurück (für Curiosity-Analyse)."""
    try:
        collection = get_active_collection()
        result = collection.get(include=["documents", "metadatas"])
        chunks = []
        for i, chunk_id in enumerate(result["ids"]):
            meta = result["metadatas"][i]
            chunks.append({
                "id": chunk_id,
                "text": result["documents"][i],
                "chunk_type": meta.get("chunk_type", ""),
                "confidence": _safe_float(meta.get("confidence", 0.5), 0.5),
                "tags": meta.get("tags", ""),
            })
        return chunks
    except Exception as e:
        logger.error(f"get_all_active Fehler: {e}")
        return []
