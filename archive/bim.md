# BIM.md – Wissensbasis {{BOT_NAME}}
# Universitätsklinikum Leipzig AöR

> Diese Datei enthält das kondensierte BIM-Wissen für den Chatbot-Kontext.

---

## 1. ÜBERBLICK – BIM AM UKL

### 1.1 Grundsatz
Das UKL verfolgt einen **open BIM-Ansatz** mit IFC als produktneutralem Austauschformat.
Hintergrund: Als öffentlicher Auftraggeber arbeitet das UKL mit einer kleinteiligen, mittelständisch geprägten Auftragnehmerschaft – open BIM stellt sicher, dass keine Software-Abhängigkeiten entstehen.

### 1.2 Tommys Rolle
Tommy Schnurrpfeil ist in den Projeken SIR, NUk und Süd 4  **BIM-Informationsmanager** am UKL (Bereich 5, Abt. Gebäudemanagement).
Er definiert die Informationsbedürfnisse und Modellanforderungen des Bauherrn, die in AIA und BAP einfließen. Er ist NICHT für die operative Modellierung zuständig, sondern für die strategische Steuerung der BIM-Anforderungen aus AG-Sicht.
In dem Projekt Energiezentrale ZKM übernimmt er die Rolle des BIM-Managements und BIM-Gesamtkoordination (keine extrne Beratung)

### 1.3 BIM-Ziele UKL
- Kollisionsreduktion in der Planung
- Kommunikation zwischen Planungsbeteiligten verbessern
- Einbeziehung zukünftiger Nutzer und Betreiber in den Planungsprozess
- Fehler und Missverständnisse vermeiden
- Konsistente Datenquelle über alle Leistungsphasen
- Nahtloser Übergang von Bau in Betrieb (CAFM-Integration: SAP-PM, Famos)
- Hohe Dokumentationsqualität zur Betreibung eines Krankenhauses

### 1.4 Projektlandschaft & Systeme

| Projekt | CDE (Datenaustausch) | Issue-Management |
|---|---|---|
| **SIR** (Strahlenschutz, Innere Medizin, Radiologie) | WWBau (PlanTeamSpace) | BIMcollab |
| **NUK** (Nuklearmedizin) | WWBau (PlanTeamSpace) | BIMcollab |
| **Süd 4** | UKL FileSync | BIMcollab |
| **Energiezentrale ZKM** | Autodesk Construction Cloud | Autodesk Construction Cloud |

---

## 2. PROJEKT SIR – BAP-KERNINHALTE

### 2.1 Projektdaten
- **Projektname**: SIR – Strahlenschutz, Innere Medizin und Radiologie
- **Gebäudenummer**: 4334
- **Beschreibung**: Neubau Klinikgebäude mit Ambulanz-/Stationsflächen für Strahlenmedizin, Radiologie, Rheumatologie/Endokrinologie, Neurologische-Neurochirurgische Frühreha
- **Auftraggeber**: Universitätsklinikum Leipzig AöR
- **Projektleitung**: Anja Reinker (UKL, Bereich 5, Abt. Bau), stellv. Betinna Szabo
- **BIM-Informationsmanagement**: Tommy Schnurrpfeil (UKL, Bereich 5, Abt. GBM)
- **BAP-Version**: 1.7 (Stand 25.07.2024)
- **BIM-Management**: Formitas AG (Kalin Bozhanov)
- **BIM-Gesamtkoordination**: Formitas AG (Larissa Hecht)

### 2.2 BIM-Rollen & Verantwortlichkeiten

**BIM-Informationsmanagement (Tommy)**
UKL-interner Ansprechpartner für BIM-Inhalte. Definiert Informationsbedürfnisse und Modellanforderungen des Bauherrn.

**BIM-Management (Formitas – Kalin Bozhanov)**
Führt Informationsbedarfe aus allen Lebenszyklusphasen zusammen, stellt AIA auf, überprüft BIM-Modelle (AG-seitige Qualitätssicherung).

**BIM-Gesamtkoordination (Formitas – Larissa Hecht)**
Entwicklung/Fortschreibung BAP, Koordinationsmodell zusammenstellen, gewerkeübergreifende Kollisionsprüfungen, Issue-Management organisieren, QS-Besprechungen leiten.
Hinweis: Die fachliche Koordination obliegt weiterhin dem Objektplaner.

**BIM-Fachkoordination (je Fachplaner)**
Verantworten Erstellung und Qualität ihrer Fach-/Teilmodelle.

**RACI-Matrix (Kurzform):**
- AIA: Tommy=Accountable, BIM-Mgmt=Responsible
- BAP: BIM-Gesamtkoord=Responsible/Accountable
- Kollisionsprüfung inkl. Issues: BIM-Gesamtkoord=R/A
- BIM-M-Besprechung: BIM-Mgmt=R/A (mit PL und GKO)
- BIM-QS-Besprechung: BIM-Gesamtkoord=R/A (mit Fachkoord.)

### 2.3 Projektbeteiligte & Software

| Disziplin | Firma | Software | Fachmodelle |
|---|---|---|---|
| **Objektplanung** | Wörner Traxler Richter | Allplan v.22 | Architektur, Gebäudehülle |
| **Tragwerk** | Mayer-Vorfelder Dinkelacker (MVD) | Allplan v.24 | Tragwerk, Tabuzonen |
| **TGA** | ZWP Ingenieur-AG | Microstation 16 Tricad | 17+ Fachmodelle (Heizung, Kälte, Lüftung, Trinkwasser, Abwasser, ELT, Installation, Trassen, SuD, Grundleitung, Förderanlagen, Rohrpost, Medienversorgung, EDV, Arbeitsraum Unrein) |
| **Medizintechnik** | mtp Planungsgesellschaft | Allplan 21 | Befestigungsfelder, Objekte, techn. Angaben |
| **Vermesser** | Kunze und Schmidt | Revit 2024 | Bestand Hochbau+Tunnel, Geländehöhen |
| **Küchenplanung** | Nau Grossküchentechnik | WinDelta PMS 2022 | Küchentechnik |
| **Bekleidungsautomat** | Elis Group Services | Revit 2023 | Bekleidungsautomat, Ausgabeschacht |

Alle nutzen **BIMCollab ZOOM** als Viewer und/oder **Solibri** zur Fachkoordination.

### 2.4 Datenumgebung (CDE)

**PKMS / Datenraum**: WWBau (PlanTeamSpace von WeltWeitBau)
- Einzige verbindliche Austauschplattform für Modelle, Pläne, Dokumente
- Verteilung außerhalb (E-Mail, USB) gilt als nicht dokumentiert
- Verpflichtend für alle Projektbeteiligten

**Issue-Management**: BIMcollab
- BCF-basiert (BIM Collaboration Format)
- Issues enthalten: Titel, Kommentar, Screenshot, Filteroptionen (Etiketten, Typ, Priorität, Meilenstein, Besprechung, Frist)
- Zuweisung: 1 Person oder Projektmailadresse
- Etiketten = Gewerke + Geschoss + Thema (Pflichtfeld zum Filtern)
- Besprechungs-Feld = Pflichtfeld (Koordinationsbesprechung / Planungsbesprechung / BIM-QS / Direkte Kommunikation)
- Prioritäten: Normal, Aufgeschoben (mit Meilenstein-Verschiebung)
- Workflow: Ersteller → Empfänger → Lösen ODER Kommentar ODER Weiterleiten → Schließen

### 2.5 Austauschformat & Standards

- **IFC 2x3** als Standard-Austauschformat (abweichend nur nach Abstimmung mit BIM-Management)
- Standards: VDI 2552, DIN EN ISO 19650 (08/2019), Baukomponenten-Katalog LOD v3.1
- Datenübergabe: IFC, DWG, PDF, Excel + native Dateien zum Projektende

### 2.6 Modellstruktur

**Modelltypen:**
- Fachmodell (FM) = komplette Planung einer Disziplin
- Teilmodell (TM) = z.B. einzelnes Geschoss einer Disziplin
- Koordinationsmodell (KM) = zusammengeführte Fachmodelle

**IFC-Hierarchie (Pflicht):**
```
IfcSite (Grundstück: UKL)
  └─ IfcBuilding (Gebäude: 4334)
       └─ IfcBuildingStorey (-2, -1, 00, 01, 02, 03, 04, 05, 06, KOORDINATIONSEBENE)
```

**TGA-Modelle** müssen zusätzlich nach **IfcSystem** aufgeteilt sein (z.B. Fortluft, Zuluft, Heizung VL/RL, Kaltwasser, Warmwasser, Schmutzwasser, Starkstrom, Schwachstrom, Funktionserhalt).

**Geschosszuordnung**: OKRF bis OKRF (Oberkante Rohbau bis Oberkante Rohbau). Fundamente, Bühnen und Dach in separaten Geschossen. Übergreifende Bauteile möglichst komplett einem Geschoss zuordnen.

### 2.7 Detaillierungstiefe (LOD)

**LPH 3 (Entwurf) – LOD 200, Maßstab 1:100:**
- Detaillierte Elemente mit ausreichendem Informationsgehalt für Kennwerte
- Klare Trennung Rohbau-/Ausbauschicht
- TGA: alle fest installierten Komponenten nach Gewerken vollständig (KEINE Kabel/Leitungen ELT+NT+GA, KEINE Blitzschutzkomponenten außer Dachaufsicht)
- Medizintechnik: neutral, optisch identifizierbar; Geometrie bleibt, Attribute steigen
- Bodenabläufe bereits modellieren (Kollision mit Tabuzonen möglich)

**LPH 5 (Ausführung) – LOD 300, Maßstab 1:50:**
- Endgültige Geometrien und Platzbedarfe
- Schichten einzeln in IFC auslesbar
- Oberflächen als Parameter am Bauteil
- Medizintechnik: herstellerneutral, Maximalgröße als Geometrie-Grundlage

### 2.8 Modellierungsvorgaben (Kurzfassung)

- Einheitliche, konsistente Bauteilbezeichnungen
- Stabile GUIDs (gleiches Element = gleiche GUID bei erneutem Export)
- Korrekte IFC Property Sets; keine Doppelbenennungen bei Attributen
- Korrekte Klassifizierung nach ISO/PAS 16739:2018; IfcBuildingElementProxy vermeiden
- Mehrschichtige Elemente: Schichten im IFC klar erkennbar
- Raumhöhe: OKRF bis UKRD; lichte Höhen als Attribut
- Schachträume geschossweise modellieren, einander berührend
- Decken separat je Höhenlage (nicht durchgängig)
- Zusammenhängende Räume über "ZoneName" clustern (max. 1 Cluster je IfcSpace)
- Geschossweise Modellierung, gleiche Geschossaufteilung in allen Fachmodellen
- Keine Duplikate/Überschneidungen (= Kollisionen)
- Rohbauöffnungen als IfcOpening
- HLSK/ELT Ein-/Austrittspunkte als IfcPort modellieren
- Herstellerneutral, performant, keine überflüssigen Informationen
- Platzhalter: Text="tbd", Boolean=kein Export (Ausnahme: false), Zahl=kein Export (Ausnahme: 0)

**Raum-Anforderungen:**
- Direkt an umgebende Bauteile grenzend
- Keine offenen Volumina oder Überschneidungen
- Schächte (technisch, Licht, Aufzug) als Räume je Ebene
- Raumnamen/-nummern konsistent gem. Raumprogramm
- Alle Räume müssen enthalten sein

**Spezifisch:**
- Brandschutzanforderungen an Wänden pflegen
- Schallschutz: LPH3 an Räumen + Türen; LPH5 an weiteren Bauteilen
- TGA: Wartungs-/Einbringungsflächen als Volumenkörper

### 2.9 Koordinatensystem & Georeferenzierung

**Bezugssystem**: UTM 33 ETRS89

**Vermessungspunkt VP 3356318901:**
- O/W (x): 318029,3511 m
- N/S (y): 5689741,6590 m
- Höhe (z): 118,6160 m
- Winkel ggn. Norden: -16,6833°

**Projektbasispunkt:**
- O/W (x): 318188,1528 m
- N/S (y): 5689789,6935 m
- Höhe (z): 118,5000 m
- Winkel ggn. Norden: -16,6833°

**Lokale Position Vermesserpunkt im Projektkoordinatensystem:**
- Global X: -138,3273 m
- Global Y: -91,6016 m
- Global Z: 0,116 m

**Projekt-Nullpunkt**: Auf Achse-L, 5m vor Achse-1

**Koordinationspyramiden** (alle modellierenden Parteien):
- An Projektbasispunkt UND Vermesserpunkt je eine unregelmäßige Pyramide
- Maße: x=0,50m, y=0,75m, z=1,00m
- Koordinationselemente auf Ebene "KOORDINATIONSEBENE"
- Achssystem + Ausrichtung = Verantwortung Architekt → alle anderen übernehmen

### 2.10 DataDrops & Besprechungszyklen

**DataDrop-Turnus**: 4-wöchentlich über WWBau (Datenraum)
- Während LPH auch Teilmodelle möglich
- Zum LPH-Ende: mindestens 2x Fachmodelle komplett (alle Geschosse, 1 IFC-Datei)
- Zu jedem DataDrop ALLE Fachmodelle/Teilmodelle hochladen (auch ohne Änderungen)
- Zum Projektende: auch native Modelle übergeben

**BIM-QS-Besprechung** (Qualitätssicherung):
- 4-wöchentlich, Donnerstags gerade Wochen 13:30, direkt nach Planer-JF
- Teilnehmer: BIM-Gesamtkoordination + BIM-Fachkoordinatoren
- Inhalte: Terminsituation, technische BIM-Konflikte, QS-Ergebnisse, BAP-Fortschreibung

**BIM-M-Besprechung** (Management):
- Anfangs 2-wöchentlich, später 4-wöchentlich
- Teilnehmer: BIM-Management + BIM-Gesamtkoordination + Projektleitung UKL
- Inhalte: Projektfortschritt, strategische Vorausschau, BIM-Meilensteine

### 2.11 Qualitätssicherung

**Stufe 1 – Fachmodellprüfung (durch Fachkoordinator vor DataDrop):**
Checkliste: Projektinfos gepflegt? Modell richtig verortet? Nullpunktpyramiden korrekt? Geschosse richtig? Objekttypen korrekt? Keine überkomplexen Geometrien? Attribute vollständig? Keine kritischen Fachmodell-internen Kollisionen? Keine fremden Disziplinen mit exportiert?

**Stufe 2 – Koordinationsmodellprüfung (durch BIM-Gesamtkoordination):**
- Min. 4-wöchentlich
- Alle Fachmodelle in Prüfsoftware überlagert
- Prüfung gegen AIA/BAP
- LPH3: Kollisionstoleranzen 5cm
- Fokus: geometrische Überschneidungen (gem. Kollisionsmatrix), Informationsanforderungen, Anschlusspunkte Prozessanlagen
- Ergebnisse → BIM-QS-Besprechung → Lösungen mit Verantwortlichkeiten

### 2.12 Schlitz- und Durchbruchsplanung (SuD)

- Modellbasiert in LPH3 (statisch relevante) UND LPH5 (alle Durchbrüche)
- Kernbohrungen mit unklarer Position → rechteckige "Kernbohrzonen" modellieren
- Workflow gem. Anhang 3

**Anpassung LPH3 (Stand Juni 2024):**
- SuD auf relevante Bereiche reduziert (weniger Neubewertung bei LPH5-Änderungen)
- Schächte bereits abgestimmt
- Geschossgleiche Durchbrüche: 1 Geschoss genau prüfen → Freigaben auf andere übertragen
- Statisch relevant (TWP bewertet): Durchbrüche in Sperrzonen, Schachtausfädelungen tragende Bauteile UG/EG/1.OG, Durchbrüche >1m Seitenlänge im Rohbau
- ARC bewertet: alle oben genannten + Schachtausfädelungen Rohbau/Mauerwerk restliche Geschosse
- Ablage WWBau: geschossweise
- Wanddurchbrüche: bündig ohne Überstand modellieren
- LPH5: regulärer SuD-Prozess wird wieder aufgenommen

### 2.13 BIM-Anwendungsfälle SIR

**AwF 1 – Objektorientierte Modellierung:**
Planung in BIM-fähiger CAD, Pläne/Schnitte/Ansichten aus 3D-Modell ableiten, IFC 2x3 zertifiziert nach buildingSMART, Datenübergabe: IFC + native + DWG + PDF + Excel.

**AwF 2 – Planungs-/Baubesprechungen:**
Gesamtmodell sichtbar anzeigen, navigieren, BCF-Kommentare erstellen. Viewer muss BIMcollab-Anbindung haben. Zuständigkeit: Gesamtkoordinator.

**AwF 3 – Koordination & Qualitätssicherung:**
Fachmodellprüfung (AN) + Koordinationsmodellprüfung (GKO). Ergebnisse als Issues + Prüfregeln.

**AwF 4 – Virtuelle Inbetriebnahme:**
BIM-Modelle vor Abschluss LPH8 in CAFM-Systeme SAP-PM und Famos überführen. Anwendungsfall wird bauherrenseitig in LPH1–4 erarbeitet.

### 2.14 Vorgaben für die Ausschreibung von Bauleistungen (W+M-Planung LPH 8)

**Grundsatz:** Ausführende Gewerke liefern die Werk- und Montageplanung (W+M) in LPH 8 je nach BIM-Fähigkeit in zwei Stufen: Low Level BIM oder High Level BIM.

#### Low Level BIM

**Geometrie (verpflichtend):**
Die ausführenden Firmen erstellen die W+M-Planung in 3D in einem Autorensystem eigener Wahl, damit diese in das Koordinationsmodell integriert werden können. Austauschformate beschränkt auf .dwg und .dgn. Der durchgängige BIM-Gedanke wird hierbei verlassen – es wird lediglich die Kollisionsprüfung hinsichtlich grafischer Repräsentanz verfolgt. Relevante Teile der W+M-Planung werden durch Dritte in das BIM-Modell überführt.

**Parametrik:**
Ausführende Firmen füllen vorbereitete Excel-Dateien aus, um die geforderte Parametrik der LPH 8 in die Fachmodelle der LPH 5 zu importieren. Die Excel-Dateien werden durch den Planer bereitgestellt, die Ergebnisse durch den entsprechenden Planer importiert. Fachliche Prüfung obliegt den verantwortlichen Fachplanern bzw. ausführenden Gewerken.

#### High Level BIM

**Geometrie:**
Ausführende Firmen stellen die W+M-Planung im IFC-Datenformat bereit und dokumentieren damit die BIM-Fähigkeit des eigenen Autorenwerkzeuges. Modelle werden in das Koordinationsmodell integriert und zur Kollisionsprüfung herangezogen. Autorensystem muss BuildingSMART-zertifiziert sein. Open-BIM-Gedanke wird verfolgt: sowohl Parametrik als auch grafische Repräsentanz werden bedient.

**Parametrik:**
Wie Geometrie – ausführende Firmen liefern IFC-Modelle mit vollständiger Parametrik. Integration in das Koordinationsmodell und Kollisionsprüfung durch die BIM-Gesamtkoordination.

#### Änderungsmanagement in der Ausführungsphase

Bei maßgeblichen Änderungen während der W+M-Planung ist ein strukturierter Freigabe- und Kommunikationsprozess erforderlich. Ergebnis ist entweder die Übernahme in das BIM-Modell oder Rückbau auf der Baustelle. Übernahme muss zeitlich strukturiert geplant werden, um Datenverluste zu vermeiden. Empfohlen werden feste monatliche Abstimmungen zur Freigabe und Übernahme, gemeinsam mit der BIM-Gesamtkoordination LPH 8 abgestimmt und im BAP festgehalten.

#### Plattformzugang für ausführende Gewerke

Empfohlen wird ein Zugang für alle an der Planung beteiligten Projektteilnehmer des AN. Die Plattform wird durch den AG bereitgestellt und durch den BIM-Gesamtkoordinator administriert. Zugang ist kostenfrei. Neue Nutzer registrieren sich eigenständig mit E-Mail-Adresse und erhalten personalisierten Zugang (keine Mehrfachnutzung eines Accounts). Einführung in die Nutzung erfolgt zu Beginn der Beauftragung per Videokonferenz durch die Planungskoordinatoren und kann bei Bedarf wiederholt werden.

---

## 3. PROJEKT NUK – BAP-KERNINHALTE

> Quelle: BAP NUK V1.1 (04.09.2024)
> Grundsatz wie SIR: Open BIM, IFC 2x3, CDE=WWBau, Issue-Management=BIMcollab.
> Nachfolgend nur die **Unterschiede und Ergänzungen** gegenüber SIR.

### 3.1 Projektdaten
- **Projektname**: NUK Haus 3 – Zentralisierung Nuklearmedizin Haus 3
- **Gebäude Bestand**: 4254 (Haus 2/3/B)
- **Gebäude Neubau**: 4259
- **Beschreibung**: Erweiterungsneubau zur Zentralisierung der Nuklearmedizin. Erweiterung der Ambulanz Haus 3 um stationäre Nuklearmedizin aus Haus 5.2 (Stephanstraße 9). Ca. 1.300 m² HNF Neubau + ca. 1.000 m² HNF Bestand. Umfasst: aktiver/inaktiver Bereich, Ganzkörperzähler, Zyklotron mit Heißzellen, Schilddrüsenambulanz, Therapiestation, Personalumkleiden, Lagerhaltung, Güterumschlag. Baumaßnahme im laufenden Klinikbetrieb.
- **Projektleitung**: Alexandra Voigt-Kölzsch (UKL), stellv. Christoph Lieber
- **BIM-Informationsmanagement**: Tommy Schnurrpfeil
- **BIM-Management**: Formitas AG (Dr. Joaquin Ramirez Brey)
- **BIM-Gesamtkoordination**: Formitas AG (Ivaylo Mehandzhiev)

### 3.2 Projektbeteiligte & Software (Unterschiede zu SIR)

| Disziplin | Firma | Software | Fachmodelle |
|---|---|---|---|
| **Objektplanung** | SWECO | Revit 23 | Architektur Bestand, Architektur Neubau, Rohbau Neubau |
| **Tragwerk** | Mathes Ingenieure | Strakon | Tragwerk, Tabuzonen |
| **TGA** | ZWP Ingenieur-AG | Microstation 16 Tricad | Heizung, Kälte, Trinkwasser, Schmutzwasser, Lüftung, Med. Gas, Gebäudeautomation, SuD Wand, SuD Boden |
| **Elektrotechnik** | PGMM | Revit 23 | Starkstrom, Schwachstrom, Rohrpost, Trassen (ELT+NT+GA), SuD ELT |
| **Medizintechnik** | SWECO | Revit 23 | Medizin-/Labortechnik, SuD Medizintechnik |

**Wesentliche Unterschiede zu SIR:**
- Architekt = SWECO (statt Wörner Traxler Richter), nutzt **Revit 23** (statt Allplan)
- Tragwerk = Mathes Ingenieure (statt MVD), nutzt **Strakon** (statt Allplan)
- Elektrotechnik = **PGMM** als eigene Disziplin mit eigenem Fachkoordinator (bei SIR in ZWP integriert)
- Medizintechnik = SWECO (statt mtp), gleiche Firma wie Objektplanung
- **Kein Vermesser** als separater Fachplaner gelistet
- **Kein Küchentechnik-/Bekleidungsautomaten-Planer**

### 3.3 Rollenbesetzung

| Rolle | Name | Firma |
|---|---|---|
| BIM-Informationsmanagement | Tommy Schnurrpfeil | UKL |
| BIM-Management | Dr. Joaquin Ramirez Brey | Formitas AG |
| BIM-Gesamtkoordination | Ivaylo Mehandzhiev | Formitas AG |
| Fachkoord. Architektur | Corinna Schulz | SWECO |
| Fachkoord. Tragwerk | Claudia Neumann | Mathes Ingenieure |
| Fachkoord. TGA | Susan Vahldieck | ZWP |
| Fachkoord. Elektrotechnik | Richard Günther | PGMM |
| Fachkoord. Medizintechnik | Corinna Schulz | SWECO |

RACI-Matrix identisch zu SIR.

### 3.4 Besonderheiten NUK

**Zusätzlicher Anwendungsfall: Bestandserfassung (AwF 1)**
NUK hat 5 Anwendungsfälle (SIR hat 4). Zusätzlich:
- AwF 1: Bestandserfassung – Objektplaner erstellt digitales Bauwerksmodell der Bestandssituation. TGA-Planer modelliert relevante TGA-Schnittstellen zum Bestand auf Grundlage bereitgestellter Bestandspläne.
- AwF 2–5 entsprechen AwF 1–4 bei SIR (Objektorientierte Modellierung, Besprechungen, Koordination/QS, Virtuelle Inbetriebnahme).

**Datenverantwortung (expliziter als bei SIR):**
- Kein Planer darf Modelle anderer Gewerke ohne Zustimmung ändern
- Inhaltliche + urheberrechtliche Verantwortung liegt beim Ersteller
- AG darf IFC + native Dateien ohne Zustimmung des Planers verwenden

**Custom Property Sets:**
- Allgemeine UKL-Attribute: Property Set `UKL`
- SuD-Attribute: separates Property Set `UKL-SUD`
(Bei SIR nicht explizit so benannt)

**Maßeinheiten je Fachmodell (NUK-spezifisch):**
- Architektur: Meter [m]
- Tragwerksplanung: Meter [m]
- TGA: Millimeter [mm]

### 3.5 Modellstruktur NUK

**Geschosse**: FU, -2, -1, 00, 01, 02 (+ DA)
(SIR: -2, -1, 00, 01, 02, 03, 04, 05, 06, KOORDINATIONSEBENE)

**Ebenenbezeichnungen**: FU, -2…, -1, 00, 01, 02…, DA – einheitlich in allen Modellen.

**Geschosszuordnung**: OKRD bis OKRD (Oberkante Rohdecke zu Oberkante Rohdecke)
(SIR nutzt OKRF – Oberkante Rohfußboden. Leicht abweichende Terminologie, gleiche Logik.)

**Fachmodelle NUK (21 Stück):**
Architektur Neubau, Architektur Bestand, Rohbau Neubau, Tragwerk, Tabuzonen, Heizung, Kälte, Trinkwasser, Schmutzwasser, Lüftung, Med. Gas, Gebäudeautomation, Starkstrom, Schwachstrom, Rohrpost, Trassen (ELT+NT+GA), SuD ELT, SuD HLSK Wand, SuD HLSK Boden, Medizin-/Labortechnik, SuD Medizintechnik.

### 3.6 Koordinatensystem NUK

**Vermessungspunkt (gleich wie SIR – selber Punkt auf dem UKL-Gelände):**
- O/W (x): 318029,35110004 m
- N/S (y): 5689741,65900000 m
- Höhe (z): 118,6160 m

**Projektbasispunkt (ANDERS als SIR!):**
- O/W (x): 317718,17753979 m
- N/S (y): 5689868,16203443 m
- Höhe (z): 118,5000 m
- Winkel ggn. geographischen Norden: **8,15429281°** (SIR: -16,6833°)

**Lokale Position Vermesserpunkt im Projektkoordinatensystem:**
- Global X: 325,971 m (SIR: -138,3273 m)
- Global Y: -81,087 m (SIR: -91,6016 m)
- Global Z: 0,116 m (identisch)

**Pyramiden**: Gleiche Vorgabe wie SIR (x=0,50m, y=0,75m, z=1,00m).

### 3.7 Besprechungsrhythmus NUK

**BIM-QS-Besprechung:**
- Donnerstags an **ungeraden** Wochen, **14:00 Uhr** (SIR: gerade Wochen, 13:30)
- Ab KW 37/2024 bis Ende LP3: **3-wöchentlich** (statt 4-wöchentlich)

**BIM-M-Besprechung:**
- Anfangs 2-wöchentlich, später **3-wöchentlich** (SIR: später 4-wöchentlich)

### 3.8 SuD NUK
- Modellbasiert in LPH3 (statisch relevant) und LPH5 (alle Durchbrüche) – wie SIR
- Keine LPH3-Sonderregelung dokumentiert (SIR hatte Juni-2024-Anpassung)
- SuD-Attribute in eigenem Property Set `UKL-SUD`

---

## 4. BIM IM BETRIEB – IMPLEMENTIERUNGSKONZEPT

> Quelle: Implementierungskonzept „BIM im Betrieb“ (UKL / medfacilities GmbH / Formitas AG, 13.12.2024).  
> Ziel: Überführung der BIM-Methodik aus Planung/Bau in den Betrieb – mit konsistenter, stets aktueller Datenbasis und Systemintegration (u. a. SAP PM, Famos). fileciteturn6file13

### 4.1 Präambel (Kernaussage)
BIM im Betrieb bedeutet am UKL: Gebäude- und Anlagendaten werden so strukturiert bereitgestellt und gepflegt, dass Betriebsprozesse (Reparatur, Wartung, Instandhaltung, Umzüge/Anpassungen) auf einer verlässlichen, zentral verfügbaren Datenbasis arbeiten können. open BIM (IFC) ist dabei die Grundlage für softwareunabhängige Nutzung über den Lebenszyklus. fileciteturn6file13

### 4.2 Zielstellung und Nutzen (Betrieb)
Ziel ist die Umstellung der Betriebsprozesse auf eine zukunftsorientierte, datenbasierte Arbeitsweise – mit einer konsistenten, stets aktuellen Datenbasis, die Bau- und Betriebsprozesse gemeinsam optimiert. Dazu gehört explizit die Integration in bestehende Systeme (SAP PM, Famos) sowie die frühzeitige Bereitstellung vollständiger Dokumentationsunterlagen und ein reibungsloser Übergang von Bau in Betrieb. fileciteturn6file13

Die im Implementierungskonzept beschriebenen Nutzenfelder:
- **A. Zentraler Zugang zu Betriebsdaten**: Betriebs- und Wartungsinformationen digital und zentral verfügbar. fileciteturn6file15  
- **B. Standardisierte und konsistente Dokumentation**: Redundanzen und uneinheitliche Dokumentationen vermeiden. fileciteturn6file15  
- **C. Effiziente Systemintegration**: Verknüpfung von Betriebs- und Wartungsdaten mit bestehenden IT-Systemen; automatisierte Datenflüsse. fileciteturn6file15  
- **D. Verbesserte Wartungsplanung**: Präzise/aktuelle Daten ermöglichen gezielte Wartungen und minimieren ungeplante Ausfälle. fileciteturn6file15  

### 4.3 Zielgruppen
Die BIM-Strategie richtet sich intern u. a. an Instandhaltungskoordinatoren, technische und kaufmännische Abteilungen, Bauprojektteams sowie die Leitungsebene; extern an Dienstleister/Partner, die in Planungs- und Betriebsprozesse eingebunden sind. fileciteturn6file15

### 4.4 Anwendungsbereich
Der Anwendungsbereich umfasst den gesamten Lebenszyklus (Planung → Bau → Betrieb). BIM wird gezielt in Facility-Management-Prozesse integriert, um technische Gebäudeausstattung, infrastrukturelle Maßnahmen und betriebliche Anforderungen konsistent im digitalen Modell abzubilden. fileciteturn6file15

### 4.5 Strategischer Rahmen der Umsetzung
Die Umsetzung erfolgt stufenweise, beginnend mit Pilotprojekten (u. a. SIR und NUK). Die Einführung wird strategisch über einen **BIM-Lenkungskreis** gesteuert; die technische Umsetzung verantwortet ein **BIM-Arbeitskreis**. Parallel werden Softwaretools integriert und Schulungsmaßnahmen durchgeführt; die Strategie wird kontinuierlich evaluiert und angepasst. fileciteturn6file11

### 4.6 Technisches Grobkonzept (IT-Umgebung & Systemintegration)

#### 4.6.1 Bestehende IT-Infrastruktur / Softwareumgebung (Auszug)
Im Implementierungskonzept ist eine Integration in bestehende UKL-Systeme vorgesehen, u. a.:
- **SAP PM** (Ticketsystem, Wartungsplanung)  
- **Famos** (Flächenmanagement, Spindverwaltung) inkl. **CAD Flow** (2D-CAD-Modul)  
- **Maqsima** (Arbeitsschutz)  
- Weitere relevante Systeme wie **RegIS** (Betreiberpflichten) und **Aedifion** (Energiemanagement) werden geprüft und bei Bedarf integriert. fileciteturn6file2turn6file9

Hinweis aus dem Konzept:
- Famos soll künftig durch BIM-Modelle bei Flächeninformationen aktualisiert/erweitert werden. fileciteturn6file2  
- Für SAP PM ist eine Schnittstelle zu BIM-Modellen vorgesehen, um **bidirektionale Integration** sicherzustellen. fileciteturn6file2  

#### 4.6.2 Integrationsplattform & BIM-Viewer (Zielbild)
Langfristig ist eine **Integrationsplattform als zentraler Datenhub** geplant. Sie soll:
- Zugriff auf Gebäudedaten über eine benutzerfreundliche Oberfläche ermöglichen,
- relevante Betriebssoftwarelösungen über **bidirektionale Schnittstellen** verknüpfen,
- einen **Modellserver für IFC-Daten** enthalten,
- ein **modellbasiertes Ticketsystem** für Issue-Management bereitstellen,
- ein **Dokumentenmanagementsystem** (DMS) bereitstellen,
- ergänzt werden durch einen leistungsfähigen **BIM-Viewer** (browserbasiert und Desktop) für 3D-Navigation und Informationsbereitstellung im täglichen Betrieb. fileciteturn6file2turn6file9

### 4.7 Organisatorisches Grobkonzept (Rollen & Gremien)

#### 4.7.1 BIM-Champions
BIM-Champions tragen die methodische Gesamtverantwortung für die Einführung, koordinieren den Prozess, moderieren die Gremien und stellen sicher, dass Betrieb, Planung und Bau berücksichtigt werden. fileciteturn6file9

#### 4.7.2 BIM-Lenkungskreis
Der BIM-Lenkungskreis trifft Entscheidungen zur inhaltlichen Implementierung von BIM und wird durch BIM-Champions moderiert. Zentrale Steuerungselemente sind **Implementierungsprojekte** (z. B. Einführung Viewer, Integration BIM in Famos und SAP PM). Nach einer Impulsphase soll der Lenkungskreis **quartalsweise** tagen (Monitoring, neue Implementierungsprojekte). fileciteturn6file0turn6file4

Beispielhafte Besetzung (Betrieb):
- Abteilungsleitung
- BIM-Champion (Moderation)
- Vertreter aus anderen Digitalisierungsprojekten
- bei Bedarf: Vertreter aus Abteilungen und/oder externe Unterstützung fileciteturn6file0

#### 4.7.3 BIM-Arbeitskreis
Der BIM-Arbeitskreis verantwortet technisch ausgerichtete Aufgaben der BIM-Einführung/-Umsetzung und ist mit den Personen besetzt, die BIM-Anwendungsfälle implementieren und durchführen, z. B.:
- Vertreter der BIM-Anwendungsfälle (technisches Personal; z. B. Instandhaltungskoordinatoren TEC)
- BIM-Champion (Moderation)
- IT
- CAD-Abteilung
- bei Bedarf externe Unterstützung fileciteturn6file0

#### 4.7.4 Aufgabenmatrix (aus dem Implementierungskonzept – Kurzübernahme)
Legende: **V = Verantwortlich | M = Mitwirkend | T = Teilnehmend** fileciteturn6file4

- Koordination der BIM-Implementierung: BIM-Champion (V), Lenkungskreis (M), Arbeitskreis (M)
- Moderation der Kreise: BIM-Champion (V)
- Kommunikation der Fortschritte: BIM-Champion (V), Lenkungskreis (M), Arbeitskreis (T)
- Erstellung/Pflege BIM-Managementdokumente (z. B. AIA/BAP/Leistungsbilder): BIM-Champion (V), Lenkungskreis (M), Arbeitskreis (M)
- Lessons-Learned-Workshops: BIM-Champion (V), Lenkungskreis (T), Arbeitskreis (T)
- Identifikation/Definition BIM-Ziele: Lenkungskreis (V), BIM-Champion (M), Arbeitskreis (M)
- Auswahl/Beschaffung IT-Infrastruktur: Lenkungskreis (V), BIM-Champion (M), Arbeitskreis (M)
- Definition BIM-Anwendungsfälle & Implementierungsprojekte: Lenkungskreis (V), BIM-Champion (M), Arbeitskreis (M)
- Monitoring Fortschritt: Lenkungskreis (V), BIM-Champion (M), Arbeitskreis (M)
- Durchführung Implementierungsprojekte: Arbeitskreis (V), BIM-Champion (M), Lenkungskreis (M)
- Verwaltungsinterne Daten-/Schnittstellenabstimmungen: Arbeitskreis (V), BIM-Champion (M), Lenkungskreis (M)
- Austausch zu technischen Lösungen/Problemen: Arbeitskreis (V), BIM-Champion (M), Lenkungskreis (M) fileciteturn6file4

### 4.8 Personelle Anforderungen & Schulungsbedarf
Für die Umsetzung ist ein umfassender Schulungsplan erforderlich:
- Schulung aller Beteiligten in BIM-Grundlagen und Nutzung spezifischer Tools,
- vertiefte Schulungen für Schlüsselrollen und die Personen, die BIM-Anwendungsfälle im Betrieb umsetzen. fileciteturn6file4

### 4.9 IST-Analyse & Entwicklung über Prototypen (Arbeitsweise)
BIM im Betrieb wird über eine iterative Vorgehensweise weiterentwickelt: Anforderungen werden erhoben, prototypisch umgesetzt, in den Betriebsabteilungen getestet und iterativ angepasst. Dabei werden Handlungsfelder identifiziert und über Implementierungsprojekte bearbeitet. fileciteturn6file8

### 4.10 Handlungsfelder: Schnittstellen, IT-Sicherheit (KRITIS) & Verantwortung
Im Implementierungskonzept werden als zentrale Handlungsfelder beschrieben:
- Identifikation softwareübergreifender Schnittstellen (SAP, Famos, Maqsima etc.) inkl. Datensynchronisation und Prozesskompatibilität,
- Klärung der Verantwortlichkeiten für Management und Administration der IT-Softwarelandschaft,
- IT-Sicherheit und Rahmenbedingungen unter **KRITIS** (u. a. Cloud vs On-Premise) als wesentliche Entscheidungsgröße. fileciteturn6file8turn6file14

### 4.11 Prototypentwicklung Integrationsplattform (MVP)
Der Prototyp der Integrationsplattform wird als **Minimum Viable Product (MVP)** entwickelt und kontinuierlich mit den Betriebsabteilungen getestet. Kernfunktionen: fileciteturn6file8
- **Viewer & Modellinteraktion**: Drehen, Verschieben, Schnittwerkzeuge, Maßnehmen, Filtern
- **Attributbaum**: Eigenschaften/semantische Informationen inkl. kundenspezifischer Attribute
- **Issue-Management (BCF)**: modellbasiertes Themenmanagement zur Kommunikation und Nachverfolgung
- **Dokumentenmanagement**: Ablage und Verknüpfung von Dokumenten mit BIM-Elementen (zentral verfügbar)

### 4.12 Betriebsnahe Zielthemen (aus dem Konzept)
- Entwicklung digitaler Wartungspläne und modellgestützter Instandhaltungsstrategien
- Schulung der Betriebsteams zur Nutzung der Modelle
- Definition von Standards/Prozessen, damit Modellinformationen Betreiberpflichten-Anforderungen erfüllen fileciteturn6file5

### 4.13 Status / Fortschreibung
Das Implementierungskonzept wird fortgeschrieben, um Erkenntnisse aus den Pilotprojekten und aus der laufenden Erarbeitung der Anforderungen für BIM im Betrieb zu erfassen. fileciteturn6file5

---

## 5. ABKÜRZUNGSVERZEICHNIS

| Kürzel | Bedeutung |
|---|---|
| AIA | Auftraggeber-Informationsanforderungen |
| ARC | Architekt / Objektplanung |
| BAP | BIM-Abwicklungsplan |
| BCF | BIM Collaboration Format |
| BIM | Building Information Modeling |
| CAFM | Computer-Aided Facility Management |
| CDE | Common Data Environment |
| FM | Fachmodell |
| GKO | Gesamtkoordinator |
| GUID | Globally Unique Identifier |
| IFC | Industry Foundation Classes |
| KM | Koordinationsmodell |
| LOIN | Level of Information Need |
| LOD | Level of Development / Detail |
| LoG | Level of Geometry |
| LoI | Level of Information |
| LPH | HOAI-Leistungsphase |
| MVD | Model View Definition |
| OKFF | Oberkante Fertigfußboden |
| OKRF | Oberkante Rohfußboden |
| PfV | Provision for Voids (Durchbruchsvorhaltungen) |
| PKMS | Projekt-Kommunikations-Management-System |
| Pset | Property-Set / Eigenschaftssatz (IFC) |
| SuD | Schlitz- und Durchbruchsplanung |
| TM | Teilmodell |
| TWP | Tragwerksplanung |
| UKFD | Unterkante Fertigdecke |
| UKRD | Unterkante Rohdecke |
| UKUZ | Unterkante Unterzug |
| UKL | Universitätsklinikum Leipzig |
