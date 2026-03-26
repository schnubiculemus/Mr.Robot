#!/usr/bin/env python3
"""
MP3-Stream-Aufnahme CLI-Tool.
Nimmt Audio-Streams von URLs auf und speichert sie als MP3-Dateien.
"""

import argparse
import os
import sys
import time
import urllib.request
import urllib.error
from pathlib import Path
from datetime import datetime


STREAMS_DIR = Path.home() / ".local" / "radio_streams"


def setup_streams_dir():
    """Erstellt das Verzeichnis für gespeicherte Streams, falls es nicht existiert."""
    STREAMS_DIR.mkdir(parents=True, exist_ok=True)


def get_streams_db_path():
    """Gibt den Pfad zur Stream-Datenbank zurück."""
    return STREAMS_DIR / "streams.db"


def load_streams():
    """Lädt gespeicherte Stream-Informationen aus der Datenbank."""
    setup_streams_dir()
    db_path = get_streams_db_path()
    streams = {}
    
    if db_path.exists():
        try:
            with open(db_path, 'r') as f:
                for line in f:
                    line = line.strip()
                    if ':' in line:
                        name, url = line.split(':', 1)
                        streams[name] = url
        except (IOError, ValueError):
            pass
    
    return streams


def save_stream(name, url):
    """Speichert einen Stream-Eintrag in der Datenbank."""
    setup_streams_dir()
    db_path = get_streams_db_path()
    
    streams = load_streams()
    streams[name] = url
    
    try:
        with open(db_path, 'w') as f:
            for stream_name, stream_url in streams.items():
                f.write(f"{stream_name}:{stream_url}\n")
        return True
    except IOError as e:
        print(f"Fehler beim Speichern: {e}", file=sys.stderr)
        return False


def list_streams():
    """Zeigt alle gespeicherten Streams an."""
    streams = load_streams()
    
    if not streams:
        print("Keine gespeicherten Streams gefunden.")
        return
    
    print("Gespeicherte Streams:")
    print("-" * 50)
    for name, url in streams.items():
        print(f"  {name}")
        print(f"    URL: {url}")
        print()


def calculate_total_bytes(duration, blocksize, bitrate_kbps=128):
    """
    Schätzt die Gesamtzahl der Bytes basierend auf Dauer und Bitrate.
    
    Args:
        duration: Dauer in Sekunden
        blocksize: Blockgröße in Bytes
        bitrate_kbps: Geschätzte Bitrate in kbps
    
    Returns:
        Geschätzte Gesamtzahl der Bytes
    """
    bytes_per_second = (bitrate_kbps * 1000) // 8
    return duration * bytes_per_second


def record_stream(url, filename, duration, blocksize):
    """
    Zeichnet einen MP3-Stream auf.
    
    Args:
        url: Die URL des Streams
        filename: Name der Ausgabedatei
        duration: Aufnahmedauer in Sekunden
        blocksize: Größe jedes Datenblocks in Bytes
    
    Returns:
        True bei Erfolg, False bei Fehler
    """
    print(f"Starte Aufnahme von: {url}")
    print(f"Aufnahmedauer: {duration} Sekunden")
    print(f"Blockgröße: {blocksize} Bytes")
    print(f"Ausgabedatei: {filename}")
    print("-" * 50)
    
    output_path = Path(filename)
    
    if output_path.exists():
        response = input(f"Datei '{filename}' existiert bereits. Überschreiben? [j/N]: ")
        if response.lower() != 'j':
            print("Aufnahme abgebrochen.")
            return False
    
    start_time = time.time()
    total_bytes = 0
    connection_timeout = 30
    read_timeout = 60
    
    try:
        req = urllib.request.Request(
            url,
            headers={
                'User-Agent': 'Mozilla/5.0 (compatible; StreamRecorder/1.0)',
                'Accept': '*/*',
                'Icy-MetaData': '0'
            }
        )
        
        print(f"[{datetime.now().strftime('%H:%M:%S')}] Verbinde mit Stream...")
        
        with urllib.request.urlopen(req, timeout=connection_timeout) as response:
            print(f"[{datetime.now().strftime('%H:%M:%S')}] Verbindung hergestellt!")
            print(f"[{datetime.now().strftime('%H:%M:%S')}] Beginne Aufnahme...")
            
            estimated_total = calculate_total_bytes(duration, blocksize)
            
            with open(output_path, 'wb') as out_file:
                while True:
                    elapsed = time.time() - start_time
                    
                    if elapsed >= duration:
                        print(f"\n[{datetime.now().strftime('%H:%M:%S')}] Aufnahmedauer erreicht.")
                        break
                    
                    try:
                        chunk = response.read(blocksize)
                        
                        if not chunk:
                            remaining = duration - elapsed
                            if remaining > 5:
                                print(f"\n[{datetime.now().strftime('%H:%M:%S')}] Stream beendet, versuche erneut...")
                                time.sleep(2)
                                try:
                                    response = urllib.request.urlopen(req, timeout=connection_timeout)
                                    continue
                                except:
                                    print(f"[{datetime.now().strftime('%H:%M:%S')}] Stream nicht mehr verfügbar.")
                                    break
                            else:
                                print(f"\n[{datetime.now().strftime('%H:%M:%S')}] Stream beendet.")
                                break
                        
                        out_file.write(chunk)
                        total_bytes += len(chunk)
                        
                        if int(elapsed) % 5 == 0 and int(elapsed) > 0:
                            progress = (elapsed / duration) * 100
                            downloaded_mb = total_bytes / (1024 * 1024)
                            speed_mbps = downloaded_mb / elapsed if elapsed > 0 else 0
                            eta = (duration - elapsed) / speed_mbps if speed_mbps > 0 else 0
                            print(f"\r[{datetime.now().strftime('%H:%M:%S')}] "
                                  f"Fortschritt: {progress:.1f}% | "
                                  f"Geschrieben: {downloaded_mb:.2f} MB | "
                                  f"Speed: {speed_mbps:.2f} MB/s | "
                                  f"ETA: {eta:.0f}s", end='', flush=True)
                        
                    except socket.timeout:
                        remaining = duration - elapsed
                        if remaining > 10:
                            print(f"\n[{datetime.now().strftime('%H:%M:%S')}] Lese-Timeout, versuche erneut...")
                            continue
                        else:
                            print(f"\n[{datetime.now().strftime('%H:%M:%S')}] Timeout erreicht.")
                            break
                            
    except urllib.error.URLError as e:
        print(f"\n\nFehler bei der Verbindung: {e}", file=sys.stderr)
        return False
    except ConnectionResetError:
        print(f"\n\nFehler: Verbindung wurde vom Server zurückgesetzt.", file=sys.stderr)
        return False
    except ConnectionAbortedError:
        print(f"\n\nFehler: Verbindung wurde abgebrochen.", file=sys.stderr)
        return False
    except BrokenPipeError:
        print(f"\n\nFehler: Verbindung wurde unterbrochen (Broken Pipe).", file=sys.stderr)
        return False
    except OSError as e:
        if e.errno == 104:
            print(f"\n\nFehler: Verbindung zurückgesetzt (ECONNRESET).", file=sys.stderr)
        else:
            print(f"\n\nNetzwerkfehler: {e}", file=sys.stderr)
        return False
    except KeyboardInterrupt:
        print(f"\n\n[{datetime.now().strftime('%H:%M:%S')}] Aufnahme durch Benutzer abgebrochen.")
        if total_bytes > 0:
            print(f"Bis jetzt {total_bytes / (1024 * 1024):.2f} MB geschrieben.")
        return False
    finally:
        elapsed = time.time() - start_time
        print("\n" + "-" * 50)
        print(f"[{datetime.now().strftime('%H:%M:%S')}] Aufnahme beendet")
        print(f"Dauer: {elapsed:.1f} Sekunden")
        print(f"Geschriebene Daten: {total_bytes / (1024 * 1024):.2f} MB")
        print(f"Dateipfad: {output_path.absolute()}")
        
        if total_bytes == 0:
            print("\nWarnung: Keine Daten empfangen!", file=sys.stderr)
            if output_path.exists():
                output_path.unlink()
            return False
    
    return True


def main():
    parser = argparse.ArgumentParser(
        description='MP3-Stream-Aufnahme Tool',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Beispiele:
  %(prog)s http://stream.example.com/live.mp3
  %(prog)s http://stream.example.com/live.mp3 --duration 60 --filename mystream.mp3
  %(prog)s http://stream.example.com/live.mp3 --blocksize 4096 --duration 120
  %(prog)s --list
        """
    )
    
    parser.add_argument(
        'url',
        nargs='?',
        help='URL des MP3-Streams'
    )
    
    parser.add_argument(
        '--filename', '-f',
        default='myRadio.mp3',
        help='Dateiname für die Aufnahme (Standard: myRadio.mp3)'
    )
    
    parser.add_argument(
        '--duration', '-d',
        type=int,
        default=30,
        help='Aufnahmedauer in Sekunden (Standard: 30)'
    )
    
    parser.add_argument(
        '--blocksize', '-b',
        type=int,
        default=64,
        help='Blockgröße in Bytes (Standard: 64)'
    )
    
    parser.add_argument(
        '--list', '-l',
        action='store_true',
        help='Liste alle gespeicherten Streams auf'
    )
    
    parser.add_argument(
        '--save', '-s',
        metavar='NAME',
        help='Speichert die URL mit dem angegebenen Namen für später'
    )
    
    args = parser.parse_args()
    
    if args.list:
        list_streams()
        return 0
    
    if args.save:
        if args.url:
            if save_stream(args.save, args.url):
                print(f"Stream '{args.save}' gespeichert: {args.url}")
                return 0
            else:
                return 1
        else:
            print("Fehler: URL erforderlich zum Speichern.", file=sys.stderr)
            return 1
    
    if not args.url:
        parser.print_help()
        print("\nVerwende --list um gespeicherte Streams anzuzeigen.")
        return 1
    
    if args.duration <= 0:
        print("Fehler: Dauer muss größer als 0 sein.", file=sys.stderr)
        return 1
    
    if args.blocksize <= 0:
        print("Fehler: Blockgröße muss größer als 0 sein.", file=sys.stderr)
        return 1
    
    success = record_stream(args.url, args.filename, args.duration, args.blocksize)
    
    return 0 if success else 1


if __name__ == '__main__':
    import socket
    sys.exit(main())