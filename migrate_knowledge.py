"""
SchnuBot.ai - Phase 5a: Wissensaufnahme
Migriert tommy.facts und bim.md als vordefinierte Chunks in ChromaDB.
Deterministisch, kein LLM-Call.
"""

import sys
import os
sys.path.insert(0, "/opt/whatsapp-bot")
os.environ["HF_HUB_OFFLINE"] = "1"

import logging
from memory.chunk_schema import create_chunk, validate_chunk
from memory.memory_store import store_chunk, get_active_collection, get_stats

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


# =============================================================================
# tommy.facts → hard_fact + preference Chunks
# =============================================================================

TOMMY_CHUNKS = [
    # --- Identität ---
    {
        "text": "Tommy Schnurrpfeil, geboren am 13.11.1987 in Schkeuditz, wohnhaft in Leipzig. Deutsch, Muttersprache Deutsch, Englisch auf Basisniveau.",
        "chunk_type": "hard_fact",
        "source": "tommy",
        "confidence": 0.99,
        "epistemic_status": "confirmed",
        "tags": ["identitaet", "persoenlich"],
    },
    # --- Beziehungen ---
    {
        "text": "Partnerin: Julia Oehme, geboren 01.04.1987. Selbstständige Fotografin (Babyfotografie), Website julia-oehme.de, Fotografin des Jahres 2021. Interessen: Disney, Romcoms, Schokolade, Ed Sheeran, Filmmusik.",
        "chunk_type": "hard_fact",
        "source": "tommy",
        "confidence": 0.98,
        "epistemic_status": "confirmed",
        "tags": ["beziehung", "julia", "persoenlich"],
    },
    {
        "text": "Drei Britisch-Langhaar-Katzen: Melody, Lilo und Odette.",
        "chunk_type": "hard_fact",
        "source": "tommy",
        "confidence": 0.99,
        "epistemic_status": "confirmed",
        "tags": ["haustiere", "katzen", "persoenlich"],
    },
    # --- Beruf ---
    {
        "text": "Tommy ist BIM-Manager am Universitätsklinikum Leipzig (UKL), Bereich 5, Abteilung Gebäudemanagement. Projekte: SIR, NUK, Süd 4, Energiezentrale ZKM, BIM im Betrieb.",
        "chunk_type": "hard_fact",
        "source": "tommy",
        "confidence": 0.99,
        "epistemic_status": "confirmed",
        "tags": ["beruf", "bim", "ukl"],
    },
    # --- Bildung ---
    {
        "text": "Abgeschlossene Abschlüsse: Master of Science Bauingenieurwesen, Bachelor of Engineering Bauingenieurwesen. Aktuell: Medieninformatik B.Sc. an der TH Brandenburg (Fernstudium), Pause bis April 2026, nächstes Semester 5.",
        "chunk_type": "hard_fact",
        "source": "tommy",
        "confidence": 0.98,
        "epistemic_status": "confirmed",
        "tags": ["bildung", "studium"],
    },
    {
        "text": "Studiennoten Medieninformatik: Grundlagen der Programmierung 1 (1.0), Grundlagen der Programmierung 2 (1.0), Web-Programmierung (1.0), Rechnernetze Grundlagen (1.0), Mediendesign 1 (1.0), Mediendesign 2 (1.0), Computergrafik (1.0), Projektmanagement (1.0), Kommunikation/Führung/Selbstmanagement (1.3), Datenbanken (1.7), Relationen und Funktionen (1.7), Einführung in die Informatik (2.0), Computerarchitektur und Betriebssysteme (2.0), BWL (2.0), Grundlagen der Mathematik (2.7), Mensch-Computer-Interaktion (2.7), Theoretische Informatik (4.0).",
        "chunk_type": "hard_fact",
        "source": "tommy",
        "confidence": 0.97,
        "epistemic_status": "confirmed",
        "tags": ["studium", "noten", "medieninformatik"],
    },
    {
        "text": "Geplante Module: Netzwerksicherheit, IT-Forensik, Objektorientierte Skriptsprachen, Rechnernetze Vertiefung. Studien-Tools: MiroBoard, Anki, KI-Tools, IntelliJ, Illustrator.",
        "chunk_type": "hard_fact",
        "source": "tommy",
        "confidence": 0.95,
        "epistemic_status": "stated",
        "tags": ["studium", "planung"],
    },
    # --- Technik ---
    {
        "text": "Programmiersprachen: Java (bevorzugt), JavaScript, HTML, CSS, SQL, Swift, Basic. Paradigma: OOP mit sauberer Struktur.",
        "chunk_type": "hard_fact",
        "source": "tommy",
        "confidence": 0.97,
        "epistemic_status": "confirmed",
        "tags": ["technik", "programmierung"],
    },
    # --- Fitness & Gesundheit ---
    {
        "text": "Gravelbike: Rose Backroad FF. Radziel: Harz Halo Orbit 360. Aktuell nicht aktiv wegen Abszess (gluteal, post-operativ entfernt). Indoor: Rollentrainer mit Zwift, 2024/2025 viel gefahren. Ebike: Lemmo One MK2 (grau) für den Arbeitsweg. Überlegung: Wahoo Bolt als Fahrradcomputer.",
        "chunk_type": "hard_fact",
        "source": "tommy",
        "confidence": 0.96,
        "epistemic_status": "confirmed",
        "tags": ["fitness", "fahrrad", "gesundheit"],
    },
    # --- Persönlichkeit ---
    {
        "text": "MBTI: INFP (Introvertiert, Intuitiv, Fühlend, Wahrnehmend). Stärken: Auffassungsgabe, Empathie, Neugier, Begeisterungsfähigkeit, Leidenschaft. Schwächen: Konfliktscheu, harmoniebedürftig, leicht ablenkbar, manchmal chaotisch.",
        "chunk_type": "hard_fact",
        "source": "tommy",
        "confidence": 0.97,
        "epistemic_status": "confirmed",
        "tags": ["persoenlichkeit", "mbti"],
    },
    {
        "text": "Verhaltensmuster: Träumend, introspektiv, bevorzugt tiefgehende Gespräche. Strebt nach Authentizität. Kreative Ausdrucksformen. Denkweise: Systemdenken, Fortschritt am eigenen Maßstab, Neugiergetrieben.",
        "chunk_type": "preference",
        "source": "tommy",
        "confidence": 0.93,
        "epistemic_status": "stated",
        "tags": ["persoenlichkeit", "verhalten"],
    },
    {
        "text": "Tagesablauf: Ruhe, Sonne, entspannter Start. Routinen: Latte Macchiato, aufgeräumtes Umfeld, funktionierende Technik. Chronotyp: Nachtmensch. Grundstimmung: ruhig, lebensfroh, optimistisch, manchmal nachdenklich.",
        "chunk_type": "preference",
        "source": "tommy",
        "confidence": 0.95,
        "epistemic_status": "stated",
        "tags": ["routine", "chronotyp", "persoenlich"],
    },
    {
        "text": "Arbeitsstil: entspannt minimalistisch, unter Druck kreativ-chaotisch. Starkes Reinsteigern in Themen. Hadernd mit Disziplin, aber langfristig konsequent. Sammlertyp: Bücher, Schallplatten, Projektideen.",
        "chunk_type": "preference",
        "source": "tommy",
        "confidence": 0.94,
        "epistemic_status": "stated",
        "tags": ["arbeitsstil", "persoenlichkeit"],
    },
    # --- Lebensstil ---
    {
        "text": "Fahrzeug: Renault Captur (staubig, chaotisch, reines Transportmittel). Ernährung: Vegetarisch. Lieblingsgerichte: Tiramisu, Salamipizza, Nudeln mit Spinat, Rotes Curry, Crispy Chicken. Essgewohnheit: Zu viel McDonalds (wird reduziert), EveryFood als Alternative. Kochen: Brot backen, Pizza im Holzbackofen.",
        "chunk_type": "hard_fact",
        "source": "tommy",
        "confidence": 0.95,
        "epistemic_status": "stated",
        "tags": ["lebensstil", "ernaehrung"],
    },
    # --- Interessen ---
    {
        "text": "Lieblingsfilme: Dune, Interstellar, Schindlers Liste, Inception, Matrix, Herr der Ringe, Der Pate, Batman Trilogie (Nolan), Der Marsianer. Lieblingsserien: Stranger Things, Mr. Robot, Scrubs, Breaking Bad, Better Call Saul, Silo. Musik: Enno Bunger, Moby, Ed Sheeran, Filmmusik. Lieblingsfarbe: Rot.",
        "chunk_type": "preference",
        "source": "tommy",
        "confidence": 0.97,
        "epistemic_status": "confirmed",
        "tags": ["interessen", "filme", "musik"],
    },
    {
        "text": "Privates Projekt: Tischkicker-Bau (in Arbeit).",
        "chunk_type": "working_state",
        "source": "tommy",
        "confidence": 0.90,
        "epistemic_status": "stated",
        "tags": ["projekt", "privat"],
    },
    # --- SchnuBot-Anweisungen → decisions ---
    {
        "text": "Kommunikationsstil: direkt, auf Augenhöhe, kumpelhaft. Tabu: Lobhudelei und rhetorische Floskeln. Textlänge anfangs kurz halten, stückweise steigern.",
        "chunk_type": "decision",
        "source": "tommy",
        "confidence": 0.97,
        "epistemic_status": "stated",
        "tags": ["kommunikation", "anweisungen"],
    },
    {
        "text": "Kommunikationsregeln BIM: Keine erfundenen Fakten oder Zahlen, nur verifizierte Daten. Bei Unsicherheit offene Nachfrage statt Interpolation. Transparente Nachfragen statt impliziter Datennutzung. Textbausteine kritisch hinterfragen bevor sie ausgegeben werden.",
        "chunk_type": "decision",
        "source": "tommy",
        "confidence": 0.97,
        "epistemic_status": "stated",
        "tags": ["kommunikation", "bim", "anweisungen"],
    },
]


# =============================================================================
# bim.md → knowledge Chunks (gruppiert, verdichtet)
# =============================================================================

BIM_CHUNKS = [
    # --- Überblick ---
    {
        "text": "Das UKL verfolgt einen open BIM-Ansatz mit IFC als produktneutralem Austauschformat. Als öffentlicher Auftraggeber mit kleinteiliger, mittelständischer Auftragnehmerschaft soll keine Software-Abhängigkeit entstehen.",
        "chunk_type": "knowledge",
        "source": "tommy",
        "confidence": 0.97,
        "epistemic_status": "confirmed",
        "tags": ["bim", "ukl", "strategie"],
    },
    {
        "text": "Tommy ist BIM-Informationsmanager in den Projekten SIR, NUK und Süd 4. Er definiert Informationsbedürfnisse und Modellanforderungen des Bauherrn (AIA/BAP), ist NICHT für operative Modellierung zuständig. Im Projekt Energiezentrale ZKM übernimmt er BIM-Management und BIM-Gesamtkoordination ohne externe Beratung.",
        "chunk_type": "knowledge",
        "source": "tommy",
        "confidence": 0.98,
        "epistemic_status": "confirmed",
        "tags": ["bim", "rolle", "ukl"],
    },
    {
        "text": "BIM-Ziele UKL: Kollisionsreduktion, bessere Planungskommunikation, Einbeziehung Nutzer/Betreiber, Fehlervermeidung, konsistente Datenquelle über alle LPH, nahtloser Übergang Bau→Betrieb (CAFM: SAP-PM, Famos), hohe Dokumentationsqualität für Krankenhausbetrieb.",
        "chunk_type": "knowledge",
        "source": "tommy",
        "confidence": 0.96,
        "epistemic_status": "confirmed",
        "tags": ["bim", "ziele", "ukl"],
    },
    {
        "text": "Projektlandschaft CDE/Issues: SIR und NUK nutzen WWBau (PlanTeamSpace) + BIMcollab. Süd 4 nutzt UKL FileSync + BIMcollab. Energiezentrale ZKM nutzt Autodesk Construction Cloud für beides.",
        "chunk_type": "knowledge",
        "source": "tommy",
        "confidence": 0.97,
        "epistemic_status": "confirmed",
        "tags": ["bim", "cde", "projekte"],
    },
    # --- SIR Projekt ---
    {
        "text": "Projekt SIR: Neubau Klinikgebäude (Gebäude 4334) für Strahlenmedizin, Radiologie, Rheumatologie/Endokrinologie, Neuro-Neurochirurgische Frühreha. BAP V1.7 (25.07.2024). PL: Anja Reinker, stellv. Bettina Szabo. BIM-Management: Formitas AG (Kalin Bozhanov). BIM-Gesamtkoordination: Formitas AG (Larissa Hecht).",
        "chunk_type": "knowledge",
        "source": "tommy",
        "confidence": 0.97,
        "epistemic_status": "confirmed",
        "tags": ["bim", "sir", "projekt"],
    },
    {
        "text": "SIR Beteiligte: Objektplanung=Wörner Traxler Richter (Allplan v22), Tragwerk=MVD (Allplan v24), TGA=ZWP (Microstation 16 Tricad, 17+ Fachmodelle), Medizintechnik=mtp (Allplan 21), Vermesser=Kunze und Schmidt (Revit 2024), Küche=Nau (WinDelta), Bekleidungsautomat=Elis (Revit 2023).",
        "chunk_type": "knowledge",
        "source": "tommy",
        "confidence": 0.96,
        "epistemic_status": "confirmed",
        "tags": ["bim", "sir", "beteiligte"],
    },
    {
        "text": "SIR BIM-Rollen: BIM-Informationsmanagement (Tommy) = AG-Ansprechpartner, definiert Informationsbedürfnisse. BIM-Management (Formitas/Bozhanov) = AIA aufstellen, Modelle prüfen. BIM-Gesamtkoordination (Formitas/Hecht) = BAP fortschreiben, Koordinationsmodell, Kollisionsprüfung, Issue-Management, QS-Besprechungen.",
        "chunk_type": "knowledge",
        "source": "tommy",
        "confidence": 0.96,
        "epistemic_status": "confirmed",
        "tags": ["bim", "sir", "rollen"],
    },
    {
        "text": "SIR Austausch: IFC 2x3 Standard, Standards VDI 2552 + DIN EN ISO 19650. Datenübergabe: IFC, DWG, PDF, Excel + native zum Projektende. CDE=WWBau (PlanTeamSpace), Issue-Management=BIMcollab (BCF-basiert). DataDrop 4-wöchentlich, zum LPH-Ende mind. 2x komplett.",
        "chunk_type": "knowledge",
        "source": "tommy",
        "confidence": 0.96,
        "epistemic_status": "confirmed",
        "tags": ["bim", "sir", "austausch", "ifc"],
    },
    {
        "text": "SIR Modellstruktur: IFC-Hierarchie IfcSite→IfcBuilding(4334)→IfcBuildingStorey(-2 bis 06 + KOORDINATIONSEBENE). Geschosszuordnung OKRF bis OKRF. TGA nach IfcSystem aufteilen. Koordinatensystem UTM 33 ETRS89. Projekt-Nullpunkt auf Achse-L, 5m vor Achse-1.",
        "chunk_type": "knowledge",
        "source": "tommy",
        "confidence": 0.95,
        "epistemic_status": "confirmed",
        "tags": ["bim", "sir", "modellstruktur"],
    },
    {
        "text": "SIR Georeferenzierung: Vermessungspunkt VP 3356318901 (x=318029.3511, y=5689741.6590, z=118.616). Projektbasispunkt (x=318188.1528, y=5689789.6935, z=118.5). Winkel ggn. Norden: -16.6833°. Koordinationspyramiden (0.50×0.75×1.00m) an beiden Punkten.",
        "chunk_type": "knowledge",
        "source": "tommy",
        "confidence": 0.95,
        "epistemic_status": "confirmed",
        "tags": ["bim", "sir", "georeferenzierung"],
    },
    {
        "text": "SIR LOD: LPH3 (Entwurf) LOD 200/1:100 — detaillierte Elemente, Trennung Rohbau/Ausbau, TGA fest installiert (keine Kabel ELT/NT/GA). LPH5 (Ausführung) LOD 300/1:50 — endgültige Geometrien, Schichten einzeln in IFC, Oberflächen als Parameter.",
        "chunk_type": "knowledge",
        "source": "tommy",
        "confidence": 0.95,
        "epistemic_status": "confirmed",
        "tags": ["bim", "sir", "lod", "detaillierung"],
    },
    {
        "text": "SIR Modellierungsvorgaben (Kern): Stabile GUIDs, korrekte IFC Property Sets, keine IfcBuildingElementProxy, geschossweise Modellierung, keine Duplikate/Kollisionen, Rohbauöffnungen als IfcOpening, HLSK/ELT-Punkte als IfcPort, herstellerneutral, Räume direkt an Bauteile grenzend, Brandschutz an Wänden, Schallschutz an Räumen/Türen.",
        "chunk_type": "knowledge",
        "source": "tommy",
        "confidence": 0.94,
        "epistemic_status": "confirmed",
        "tags": ["bim", "sir", "modellierung", "vorgaben"],
    },
    {
        "text": "SIR Besprechungsrhythmus: BIM-QS-Besprechung 4-wöchentlich, Donnerstags gerade Wochen 13:30 (nach Planer-JF). BIM-M-Besprechung anfangs 2-wöchentlich, später 4-wöchentlich. QS-Kollisionstoleranzen LPH3: 5cm.",
        "chunk_type": "knowledge",
        "source": "tommy",
        "confidence": 0.95,
        "epistemic_status": "confirmed",
        "tags": ["bim", "sir", "besprechungen"],
    },
    {
        "text": "SIR Schlitz- und Durchbruchsplanung: Modellbasiert LPH3 (statisch relevant) + LPH5 (alle). Anpassung Juni 2024: SuD in LPH3 auf relevante Bereiche reduziert, geschossgleiche Durchbrüche 1 Geschoss prüfen→übertragen. Statisch relevant: Sperrzonen, Schachtausfädelungen UG/EG/1.OG, >1m Seitenlänge.",
        "chunk_type": "knowledge",
        "source": "tommy",
        "confidence": 0.93,
        "epistemic_status": "confirmed",
        "tags": ["bim", "sir", "sud"],
    },
    {
        "text": "SIR Anwendungsfälle: AwF1=Objektorientierte Modellierung (IFC 2x3 zertifiziert), AwF2=Planungs-/Baubesprechungen (Viewer mit BIMcollab-Anbindung), AwF3=Koordination & QS (Fachmodell+Koordinationsmodellprüfung), AwF4=Virtuelle Inbetriebnahme (BIM→CAFM: SAP-PM, Famos).",
        "chunk_type": "knowledge",
        "source": "tommy",
        "confidence": 0.95,
        "epistemic_status": "confirmed",
        "tags": ["bim", "sir", "anwendungsfaelle"],
    },
    {
        "text": "SIR Ausschreibung Bauleistungen LPH8: Low Level BIM = W+M-Planung in 3D (DWG/DGN), Parametrik über vorbereitete Excel-Dateien, BIM-Gedanke eingeschränkt auf Kollisionsprüfung. High Level BIM = IFC-Modelle mit vollständiger Parametrik, BuildingSMART-zertifiziertes Autorensystem, open BIM vollständig bedient.",
        "chunk_type": "knowledge",
        "source": "tommy",
        "confidence": 0.95,
        "epistemic_status": "confirmed",
        "tags": ["bim", "sir", "lph8", "ausschreibung"],
    },
    # --- NUK Projekt ---
    {
        "text": "Projekt NUK: Erweiterungsneubau Nuklearmedizin Haus 3 (Bestand 4254, Neubau 4259). Ca. 1300m² HNF Neubau + 1000m² Bestand. Umfasst: aktiver/inaktiver Bereich, Ganzkörperzähler, Zyklotron, Schilddrüsenambulanz, Therapiestation. BAP V1.1. PL: Alexandra Voigt-Kölzsch. BIM-Management: Formitas (Dr. Ramirez Brey). BIM-Gesamtkoordination: Formitas (Mehandzhiev).",
        "chunk_type": "knowledge",
        "source": "tommy",
        "confidence": 0.97,
        "epistemic_status": "confirmed",
        "tags": ["bim", "nuk", "projekt"],
    },
    {
        "text": "NUK Unterschiede zu SIR: Architekt=SWECO (Revit 23 statt Allplan), Tragwerk=Mathes Ingenieure (Strakon statt Allplan), ELT=PGMM als eigene Disziplin (bei SIR in ZWP), Medizintechnik=SWECO. Kein Vermesser, kein Küchen-/Bekleidungsplaner. Zusätzlicher AwF: Bestandserfassung (AwF1). 21 Fachmodelle inkl. Bestand.",
        "chunk_type": "knowledge",
        "source": "tommy",
        "confidence": 0.96,
        "epistemic_status": "confirmed",
        "tags": ["bim", "nuk", "unterschiede"],
    },
    {
        "text": "NUK Georeferenzierung: Gleicher Vermessungspunkt wie SIR. ANDERER Projektbasispunkt (x=317718.178, y=5689868.162, z=118.5). Winkel 8.154° (SIR: -16.683°). Geschosse: FU, -2, -1, 00, 01, 02, DA. Geschosszuordnung OKRD statt OKRF. Custom Property Sets: UKL und UKL-SUD.",
        "chunk_type": "knowledge",
        "source": "tommy",
        "confidence": 0.95,
        "epistemic_status": "confirmed",
        "tags": ["bim", "nuk", "georeferenzierung"],
    },
    {
        "text": "NUK Besprechungsrhythmus: BIM-QS Donnerstags ungerade Wochen 14:00 (SIR: gerade 13:30). Ab KW37/2024 3-wöchentlich (SIR: 4-wöchentlich). BIM-M anfangs 2-wöchentlich, später 3-wöchentlich (SIR: 4-wöchentlich).",
        "chunk_type": "knowledge",
        "source": "tommy",
        "confidence": 0.95,
        "epistemic_status": "confirmed",
        "tags": ["bim", "nuk", "besprechungen"],
    },
    # --- BIM im Betrieb ---
    {
        "text": "BIM im Betrieb Konzept: Überführung der BIM-Methodik von Planung/Bau in den Betrieb. Konsistente, aktuelle Datenbasis für Reparatur, Wartung, Instandhaltung, Umzüge. Open BIM (IFC) als Grundlage für softwareunabhängige Nutzung über den Lebenszyklus. Pilotprojekte: SIR und NUK.",
        "chunk_type": "knowledge",
        "source": "tommy",
        "confidence": 0.96,
        "epistemic_status": "confirmed",
        "tags": ["bim", "betrieb", "konzept"],
    },
    {
        "text": "BIM im Betrieb Systemintegration: SAP PM (Ticketsystem, Wartung), Famos (Flächenmanagement, Spindverwaltung) + CAD Flow, Maqsima (Arbeitsschutz), RegIS (Betreiberpflichten), Aedifion (Energiemanagement). Zielbild: Integrationsplattform als zentraler Datenhub mit bidirektionalen Schnittstellen, IFC-Modellserver, modellbasiertes Ticketsystem, DMS, BIM-Viewer.",
        "chunk_type": "knowledge",
        "source": "tommy",
        "confidence": 0.95,
        "epistemic_status": "confirmed",
        "tags": ["bim", "betrieb", "integration", "systeme"],
    },
    {
        "text": "BIM im Betrieb Organisation: BIM-Champions (methodische Gesamtverantwortung), BIM-Lenkungskreis (strategische Entscheidungen, quartalsweise), BIM-Arbeitskreis (technische Umsetzung). Stufenweise Einführung mit Pilotprojekten, iterative Prototypenentwicklung (MVP), Schulungsplan für alle Beteiligten. KRITIS-Anforderungen als Entscheidungsgröße (Cloud vs On-Premise).",
        "chunk_type": "knowledge",
        "source": "tommy",
        "confidence": 0.94,
        "epistemic_status": "confirmed",
        "tags": ["bim", "betrieb", "organisation"],
    },
]


# =============================================================================
# Migration ausführen
# =============================================================================

def run_migration(dry_run=False):
    all_chunks = TOMMY_CHUNKS + BIM_CHUNKS
    
    logger.info(f"=== Phase 5a: Wissensaufnahme ===")
    logger.info(f"tommy.facts: {len(TOMMY_CHUNKS)} Chunks")
    logger.info(f"bim.md: {len(BIM_CHUNKS)} Chunks")
    logger.info(f"Gesamt: {len(all_chunks)} Chunks")
    logger.info(f"Modus: {'DRY-RUN' if dry_run else 'LIVE'}")
    
    if not dry_run:
        stats_before = get_stats()
        logger.info(f"Vorher: {stats_before['active_count']} aktiv, {stats_before['archive_count']} archiviert")
    
    stored = 0
    skipped = 0
    
    for i, cdef in enumerate(all_chunks):
        chunk = create_chunk(
            text=cdef["text"],
            chunk_type=cdef["chunk_type"],
            source=cdef["source"],
            confidence=cdef["confidence"],
            epistemic_status=cdef["epistemic_status"],
            tags=cdef.get("tags", []),
        )
        
        valid, error = validate_chunk(chunk)
        if not valid:
            logger.warning(f"Chunk {i+1} ungültig: {error}")
            skipped += 1
            continue
        
        if dry_run:
            logger.info(f"[DRY] [{cdef['chunk_type']}] [{cdef['source']}] conf={cdef['confidence']} | {cdef['text'][:80]}")
            stored += 1
        else:
            try:
                store_chunk(chunk)
                logger.info(f"[{stored+1}/{len(all_chunks)}] [{cdef['chunk_type']}] conf={cdef['confidence']} | {cdef['text'][:70]}")
                stored += 1
            except Exception as e:
                logger.error(f"Speicherfehler Chunk {i+1}: {e}")
                skipped += 1
    
    if not dry_run:
        stats_after = get_stats()
        logger.info(f"Nachher: {stats_after['active_count']} aktiv, {stats_after['archive_count']} archiviert")
    
    logger.info(f"=== Ergebnis: {stored} gespeichert, {skipped} übersprungen ===")
    return stored


if __name__ == "__main__":
    dry = "--dry-run" in sys.argv
    run_migration(dry_run=dry)
