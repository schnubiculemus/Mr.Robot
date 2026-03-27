#!/usr/bin/env python3
"""
scripts/chroma_healthcheck.py — W11: Chroma-Healthcheck

Prüft ob ein ChromaDB-Stand wirklich funktional lesbar ist.
get_stats() / count() reichen NICHT — die Embeddings und Queries müssen laufen.

Exit-Codes:
  0 = BACKUP_OK (alle Checks grün)
  1 = BACKUP_FAIL (mindestens ein Check fehlgeschlagen)

Optionaler Umgebungsvariable CHROMA_TEST_PATH:
  wenn gesetzt, wird dieser Pfad statt dem Standard verwendet
  (z.B. für Healthcheck auf entpacktem Backup-Inhalt)
"""

import sys
import os

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_DIR)
os.chdir(PROJECT_DIR)

# Optionaler Test-Pfad (für Backup-Prüfung)
test_path = os.environ.get("CHROMA_TEST_PATH")
if test_path:
    # Temporär den Chroma-Pfad überschreiben
    import memory.memory_config as _mc
    _mc.CHROMA_PERSIST_DIR = test_path

results = {}

def check(name, fn):
    try:
        fn()
        results[name] = "OK"
        print(f"  {name}: OK")
    except Exception as e:
        results[name] = f"FAIL: {e}"
        print(f"  {name}: FAIL — {e}")

print("=== Chroma Healthcheck ===")

# Check 1: Collections laden + Count
def check_count():
    from memory.memory_store import get_active_collection, get_archive_collection
    a = get_active_collection()
    r = get_archive_collection()
    active = a.count()
    archive = r.count()
    print(f"    active={active}, archive={archive}")
    if active == 0:
        raise ValueError("Keine aktiven Chunks — leere Collection")

check("count", check_count)

# Check 2: Docs + Metadaten lesbar
def check_docs():
    from memory.memory_store import get_active_collection
    a = get_active_collection()
    result = a.get(limit=3, include=["documents", "metadatas"])
    if not result["ids"]:
        raise ValueError("get() liefert keine IDs")
    print(f"    sample_ids: {result['ids'][:2]}")

check("docs_get", check_docs)

# Check 3: Embeddings lesbar
def check_embeddings():
    from memory.memory_store import get_active_collection
    a = get_active_collection()
    result = a.get(limit=1, include=["embeddings"])
    if not result["ids"]:
        raise ValueError("Keine IDs")
    emb = result["embeddings"][0]
    if not emb or len(emb) < 10:
        raise ValueError(f"Embedding zu kurz: len={len(emb) if emb else 0}")
    print(f"    embedding_len={len(emb)}")
    return emb

emb_vector = None
def check_embeddings_store():
    global emb_vector
    from memory.memory_store import get_active_collection
    a = get_active_collection()
    result = a.get(limit=1, include=["embeddings"])
    emb_vector = result["embeddings"][0]
    if len(emb_vector) < 10:
        raise ValueError("Embedding ungültig")
    print(f"    embedding_len={len(emb_vector)}")

check("embeddings_get", check_embeddings_store)

# Check 4: Query funktioniert
def check_query():
    from memory.memory_store import get_active_collection
    a = get_active_collection()
    if emb_vector is None:
        raise ValueError("Kein Embedding-Vektor verfügbar")
    result = a.query(
        query_embeddings=[emb_vector],
        n_results=3,
        include=["documents", "metadatas", "distances"]
    )
    if not result["ids"] or not result["ids"][0]:
        raise ValueError("Query liefert keine Ergebnisse")
    print(f"    query_ids: {result['ids'][0][:2]}")

check("query", check_query)

# Ergebnis
print("")
failed = [k for k, v in results.items() if not v.startswith("OK")]
if not failed:
    print("BACKUP_OK — alle Checks grün")
    sys.exit(0)
else:
    print(f"BACKUP_FAIL — fehlgeschlagen: {', '.join(failed)}")
    sys.exit(1)
