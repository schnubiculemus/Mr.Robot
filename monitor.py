"""
SchnuBot.ai - System Monitor (Phase 6b)
CLI-Dashboard und Status-Report für Proaktiv-Engine.

Usage:
  python monitor.py          # Vollständiges Dashboard
  python monitor.py --json   # JSON-Output für Proaktiv-Engine
"""

import os
import sys
import json
import logging
from datetime import datetime, timezone, timedelta

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, PROJECT_DIR)
os.environ["HF_HUB_OFFLINE"] = "1"

from core.datetime_utils import now_utc, to_iso
from memory.memory_store import get_stats, get_active_collection, get_archive_collection

logger = logging.getLogger(__name__)


def get_chunk_distribution():
    """
    Chunk-Verteilung nach Typ und Source.
    Zählt heute erstellte Chunks gleich mit (P1.11: ein Scan statt zwei).
    """
    collection = get_active_collection()
    all_data = collection.get(include=["metadatas"])

    by_type = {}
    by_source = {}
    by_epistemic = {}
    oldest = None
    newest = None
    today_count = 0

    today_str = now_utc().strftime("%Y-%m-%d")

    for meta in all_data["metadatas"]:
        # Nach Typ
        ct = meta.get("chunk_type", "unknown")
        by_type[ct] = by_type.get(ct, 0) + 1

        # Nach Source
        src = meta.get("source", "unknown")
        by_source[src] = by_source.get(src, 0) + 1

        # Nach Epistemic
        ep = meta.get("epistemic_status", "unknown")
        by_epistemic[ep] = by_epistemic.get(ep, 0) + 1

        # Alter + Heute-Zähler
        created = meta.get("created_at", "")
        if created:
            if oldest is None or created < oldest:
                oldest = created
            if newest is None or created > newest:
                newest = created
            if created[:10] == today_str:
                today_count += 1

    return {
        "by_type": by_type,
        "by_source": by_source,
        "by_epistemic": by_epistemic,
        "oldest_chunk": oldest,
        "newest_chunk": newest,
        "today_count": today_count,
    }


def get_heartbeat_state():
    """Letzter Heartbeat-Status."""
    state_path = os.path.join(PROJECT_DIR, "heartbeat_state.json")
    if not os.path.exists(state_path):
        return {}
    try:
        with open(state_path) as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError) as e:
        logger.error(f"heartbeat_state.json kaputt: {e}")
        return {}


def get_log_errors(hours=24):
    """Zählt Fehler in den Logs der letzten N Stunden."""
    errors = {"schnubot": 0, "heartbeat": 0}
    cutoff = now_utc() - timedelta(hours=hours)

    for logname in ["schnubot", "heartbeat"]:
        logpath = os.path.join(PROJECT_DIR, "logs", f"{logname}.log")
        if not os.path.exists(logpath):
            continue
        try:
            with open(logpath, "r") as f:
                for line in f:
                    if "[ERROR]" in line or "[WARNING]" in line:
                        # Einfacher Timestamp-Check
                        try:
                            ts_str = line[:19]
                            ts = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S")
                            ts = ts.replace(tzinfo=timezone.utc)
                            if ts >= cutoff:
                                errors[logname] += 1
                        except (ValueError, IndexError):
                            pass
        except IOError:
            pass

    return errors


def get_chromadb_disk_size():
    """ChromaDB Größe auf Disk in MB."""
    db_path = os.path.join(PROJECT_DIR, "data", "chromadb")
    total = 0
    if os.path.exists(db_path):
        for dirpath, _, filenames in os.walk(db_path):
            for f in filenames:
                fp = os.path.join(dirpath, f)
                total += os.path.getsize(fp)
    return round(total / (1024 * 1024), 2)


def get_system_resources():
    """RAM, CPU und Disk-Auslastung."""
    import subprocess
    resources = {}

    # RAM
    try:
        with open("/proc/meminfo") as f:
            meminfo = f.read()
        total = int([l for l in meminfo.split("\n") if "MemTotal" in l][0].split()[1]) // 1024
        available = int([l for l in meminfo.split("\n") if "MemAvailable" in l][0].split()[1]) // 1024
        used = total - available
        resources["ram_total_mb"] = total
        resources["ram_used_mb"] = used
        resources["ram_available_mb"] = available
        resources["ram_percent"] = round(used / total * 100, 1)
    except Exception:
        resources["ram_total_mb"] = 0
        resources["ram_used_mb"] = 0
        resources["ram_percent"] = 0

    # CPU Load (1, 5, 15 min)
    try:
        with open("/proc/loadavg") as f:
            parts = f.read().split()
        resources["cpu_load_1m"] = float(parts[0])
        resources["cpu_load_5m"] = float(parts[1])
        resources["cpu_load_15m"] = float(parts[2])
    except Exception:
        resources["cpu_load_1m"] = 0

    # Disk
    try:
        result = subprocess.run(["df", "-h", "/opt/whatsapp-bot"], capture_output=True, text=True)
        line = result.stdout.strip().split("\n")[-1].split()
        resources["disk_total"] = line[1]
        resources["disk_used"] = line[2]
        resources["disk_available"] = line[3]
        resources["disk_percent"] = line[4]
    except Exception:
        resources["disk_percent"] = "?"

    return resources


def get_bot_uptime():
    """Prüft ob der Bot-Prozess läuft (Gunicorn oder direkt python app.py)."""
    import subprocess
    try:
        # Primär: Gunicorn (produktiv via systemd)
        result = subprocess.run(
            ["pgrep", "-f", "gunicorn.*app:app"],
            capture_output=True, text=True
        )
        pids = [p for p in result.stdout.strip().split("\n") if p.strip()]
        if pids:
            return True
        # Fallback: direkt python app.py (Entwicklung)
        result2 = subprocess.run(
            ["pgrep", "-f", "python app.py"],
            capture_output=True, text=True
        )
        pids2 = [p for p in result2.stdout.strip().split("\n") if p.strip()]
        return len(pids2) > 0
    except Exception:
        return False


def build_full_report():
    """Baut den vollständigen System-Report."""
    stats = get_stats()
    dist = get_chunk_distribution()
    hb_state = get_heartbeat_state()
    errors = get_log_errors(24)
    disk = get_chromadb_disk_size()
    bot_running = get_bot_uptime()

    return {
        "timestamp": to_iso(),
        "bot_running": bot_running,
        "resources": get_system_resources(),
        "memory": {
            "active_chunks": stats["active_count"],
            "archived_chunks": stats["archive_count"],
            "total_chunks": stats["total_count"],
            "disk_mb": disk,
        },
        "distribution": dist,
        "heartbeat": hb_state,
        "errors_24h": errors,
    }


def format_status_for_briefing():
    """
    Formatiert einen kompakten Status-String für das Morgen-Briefing.
    Wird von der Proaktiv-Engine aufgerufen.
    """
    report = build_full_report()
    mem = report["memory"]
    dist = report["distribution"]
    errors = report["errors_24h"]
    res = report["resources"]

    lines = []
    lines.append(f"System-Status: {'ONLINE' if report['bot_running'] else 'OFFLINE'}")
    lines.append(f"RAM: {res.get('ram_used_mb', '?')}MB / {res.get('ram_total_mb', '?')}MB ({res.get('ram_percent', '?')}%) | CPU Load: {res.get('cpu_load_1m', '?')} | Disk: {res.get('disk_percent', '?')} belegt")
    lines.append(f"ChromaDB: {mem['active_chunks']} aktiv, {mem['archived_chunks']} archiviert ({mem['disk_mb']} MB)")

    # Chunk-Verteilung
    type_parts = []
    for t in ["hard_fact", "preference", "decision", "working_state", "knowledge", "self_reflection"]:
        count = dist["by_type"].get(t, 0)
        if count > 0:
            type_parts.append(f"{t}={count}")
    if type_parts:
        lines.append(f"Verteilung: {', '.join(type_parts)}")

    # Letzter Heartbeat
    for key, val in report["heartbeat"].items():
        if "last_consolidation" in key:
            lines.append(f"Letzte Konsolidierung: {val[:19]}")
        elif "last_run" in key:
            lines.append(f"Letzter Heartbeat: {val[:19]}")

    # Fehler
    total_errors = sum(errors.values())
    if total_errors > 0:
        lines.append(f"Fehler/Warnungen (24h): {errors}")
    else:
        lines.append("Keine Fehler in den letzten 24h.")

    return "\n".join(lines)


# =============================================================================
# CLI Dashboard
# =============================================================================

def print_dashboard():
    """Gibt ein formatiertes CLI-Dashboard aus."""
    report = build_full_report()
    mem = report["memory"]
    dist = report["distribution"]
    errors = report["errors_24h"]

    print("=" * 60)
    print(f"  SchnuBot.ai System Monitor")
    print(f"  {report['timestamp'][:19]}")
    print("=" * 60)

    print(f"\n  Bot-Prozess:  {'RUNNING' if report['bot_running'] else 'DOWN'}")

    res = report["resources"]
    print(f"\n  Server-Ressourcen")
    print(f"  ├─ RAM:       {res.get('ram_used_mb', '?')} / {res.get('ram_total_mb', '?')} MB ({res.get('ram_percent', '?')}%)")
    print(f"  ├─ CPU Load:  {res.get('cpu_load_1m', '?')} / {res.get('cpu_load_5m', '?')} / {res.get('cpu_load_15m', '?')} (1/5/15 min)")
    print(f"  └─ Disk:      {res.get('disk_used', '?')} / {res.get('disk_total', '?')} ({res.get('disk_percent', '?')})")

    print(f"\n  Memory")
    print(f"  ├─ Aktiv:     {mem['active_chunks']} Chunks")
    print(f"  ├─ Archiv:    {mem['archived_chunks']} Chunks")
    print(f"  ├─ Gesamt:    {mem['total_chunks']} Chunks")
    print(f"  └─ Disk:      {mem['disk_mb']} MB")

    print(f"\n  Chunk-Typen")
    for t, count in sorted(dist["by_type"].items(), key=lambda x: -x[1]):
        bar = "█" * min(count, 40)
        print(f"  ├─ {t:20s} {count:3d}  {bar}")

    print(f"\n  Sources")
    for s, count in sorted(dist["by_source"].items(), key=lambda x: -x[1]):
        print(f"  ├─ {s:20s} {count:3d}")

    print(f"\n  Epistemic Status")
    for e, count in sorted(dist["by_epistemic"].items(), key=lambda x: -x[1]):
        print(f"  ├─ {e:20s} {count:3d}")

    if dist["oldest_chunk"]:
        print(f"\n  Ältester Chunk: {dist['oldest_chunk'][:19]}")
    if dist["newest_chunk"]:
        print(f"  Neuester Chunk: {dist['newest_chunk'][:19]}")

    print(f"\n  Heartbeat State")
    for key, val in report["heartbeat"].items():
        print(f"  ├─ {key}: {val[:19] if isinstance(val, str) else val}")

    print(f"\n  Fehler/Warnungen (24h)")
    for log, count in errors.items():
        status = "OK" if count == 0 else f"{count} Einträge"
        print(f"  ├─ {log:20s} {status}")

    print("\n" + "=" * 60)


if __name__ == "__main__":
    if "--json" in sys.argv:
        print(json.dumps(build_full_report(), indent=2, default=str))
    else:
        print_dashboard()
