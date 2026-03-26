# Dieses Script auf dem Server ausführen:
# python3 /opt/whatsapp-bot/kimi_core_hint_patch.py

with open('/opt/whatsapp-bot/kimi_core.py', 'r') as f:
    content = f.read()

old_hint = '''        _coding_hint_lines = [
            "CODING-MODUS: Tommy hat /code verwendet. Nutze den Coding Agent.",
            "Schreibe einen [CODE_AGENT: {...}] Block:",
            "  mode: scaffold | patch | refactor | tests | review | read_only_analysis | explain_code",
            "  task: klarer Auftrag",
            "  scope: [] fuer neue Datei, oder doc_id fuer bestehende Datei",
            "  target_doc_id: Zieldateiname im Workspace (z.B. mein_skript)",
            "  return_format: workspace (Standard) oder text",
            "Beispiel neue Datei:",
            '[CODE_AGENT: {"mode": "scaffold", "task": "...", "scope": [], "target_doc_id": "skript", "return_format": "workspace"}]',
            "Beispiel bestehende Datei:",
            '[CODE_AGENT: {"mode": "patch", "task": "...", "scope": ["doc_id"], "return_format": "workspace"}]',
        ]'''

new_hint = '''        _coding_hint_lines = [
            "CODING-MODUS AKTIV. Tommy hat /code verwendet.",
            "PFLICHT: Deine Antwort MUSS exakt einen [CODE_AGENT: {...}] Block enthalten.",
            "Ohne diesen Block wird kein Code erzeugt. Kein Fliesstext ueber den Coding Agent.",
            "Direkt den Block ausgeben, dann kurz erklaeren was du beauftragt hast.",
            "",
            "Format:",
            '[CODE_AGENT: {"mode": "scaffold", "task": "AUFGABE", "scope": [], "target_doc_id": "DATEINAME", "return_format": "workspace"}]',
            "",
            "mode-Werte: scaffold (neue Datei) | patch | refactor | tests | review | read_only_analysis | explain_code",
            "scope: [] fuer neue Datei | [\"doc_id\"] fuer bestehende",
            "target_doc_id: kurzer snake_case Name ohne .py (z.B. hello_world, csv_parser)",
            "",
            "Beispiel fuer neue Datei:",
            '[CODE_AGENT: {"mode": "scaffold", "task": "Python-Skript das Hello World ausgibt", "scope": [], "target_doc_id": "hello_world", "return_format": "workspace"}]',
            "",
            "NOCHMAL: Zuerst den Block, dann maximal einen Satz Erklaerung. Kein anderer Text davor.",
        ]'''

if old_hint in content:
    content = content.replace(old_hint, new_hint)
    with open('/opt/whatsapp-bot/kimi_core.py', 'w') as f:
        f.write(content)
    print("OK — Hint ersetzt")
else:
    print("NICHT GEFUNDEN — manuell pruefen")
    idx = content.find("_coding_hint_lines")
    if idx >= 0:
        print(repr(content[idx:idx+500]))
