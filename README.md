# Monsterbuffet – Kartengenerator

Erzeugt aus Textdateien ein druckfertiges PDF (Anleitung, Zutatenkarten, Monster vorn/hinten, Markerkarten, Bildnachweis).

## Start
```
pip install -r requirements.txt
python3 build.py              # PDF -> output/Monsterbuffet.pdf
python3 build.py --pruefen    # nur Daten prüfen, Regeln und Trefferzahlen anzeigen
python3 build.py -c meine.yaml   # andere Konfiguration
```

## Wo ändere ich was?

| Ich will … | Datei |
|---|---|
| Titel, Beschriftungen, Fußzeilen ändern | `config.yaml` → `spiel`, `texte` |
| Farben ändern (Zutaten, Schwierigkeitsränder, Haken/Kreuz, Gehirne, Monsterfarben) | `config.yaml` → `farben`, `stufen`, `gehirn` |
| Layout (Abstand, Anschnitt, Randbreite, Schnittmarken) | `config.yaml` → `layout` |
| Anzahl Geduld-/Sättigungs-/Schnupperteller-Karten, deren Icon und Text | `config.yaml` → `marker` |
| Spielregeln (Text) ändern | `anleitung.txt` (Platzhalter wie `{geduld}` siehe Kopf der Datei) |
| Zutaten ändern (Name, Gruppe, Farbe, Anzahl 1–3, Icon) | `zutaten.csv` |
| Monster ergänzen oder ändern | `monster.csv` (eine Zeile pro Monster) |
| Icons austauschen | SVG in den Icon-Ordner legen, Pfad in der CSV anpassen |
| Icon-Ordner verlegen | `config.yaml` → `dateien.icon_ordner` (relativ oder absolut) |

CSV-Dateien sind mit Semikolon getrennt und in UTF-8 gespeichert (Excel/LibreOffice öffnen sie direkt).

## Icons
- Alle Icons liegen unter `icons/`. In der CSV steht der Pfad relativ zum Icon-Ordner, z. B. `monster/lorc/ghost.svg`.
- Der **Ordner direkt über der Datei ist der Urheber** (`lorc`, `delapouite` …) und erscheint im Bildnachweis. Anzeigenamen stehen in `config.yaml` → `urheber`.
- Neue Icons bei game-icons.net als SVG laden (Variante schwarz auf transparent), z. B. nach `icons/monster/<urheber>/<name>.svg` kopieren.
- Icons werden beim Druck automatisch eingefärbt (schwarze Füllung `#000` wird ersetzt).
- Lizenz CC BY 3.0: Urheber nennen (macht Anleitung + Bildnachweis automatisch).

## zutaten.csv
`name;gruppe;farbe;anzahl;icon`. Die Farbe muss in `config.yaml` unter `farben.zutaten` stehen. Anzahl = Zahl der Symbole auf der Karte (1, 2 oder 3). Wenn du Zutaten hinzufügst, wird die Bildtabelle auf den Monsterrückseiten automatisch größer (bis ca. 20 Zutaten gut lesbar).

## monster.csv
| Spalte | Bedeutung |
|---|---|
| `name` | Name auf der Karte (eindeutig) |
| `stufe` | Schwierigkeit 1–4 (Randfarbe + Gehirne, Definition in `config.yaml` → `stufen`; weitere Stufen möglich) |
| `regel` | die Regel als Ausdruck (siehe unten) |
| `regeltext` | Regel in Worten, so wie sie unten auf der Rückseite steht |
| `spruch` | leichter Hinweis auf der Vorderseite |
| `mampf` / `baeh` | Sprüche auf der Rückseite |
| `icon` | Pfad zum Icon |
| `farbe` | optional, Hex wie `#5B3A8C`; leer = automatisch |

Sortierung beim Druck: nach Stufe, dann Name.

### Regelsprache (`regel`)
Wörter sind die Werte aus `zutaten.csv`: Gruppen (`Gemüse`, `Süßes` …), Farben (`rot`, `blau`, `gelb`, `gruen`/`grün`) und Anzahlen (`1`, `2`, `3`).
Verknüpfen mit `und`, `oder`, `nicht` und Klammern; `nicht` bindet am stärksten, dann `und`, dann `oder`.

Beispiele:
- `Süßes` – mag Süßes
- `nicht rot` – mag nichts Rotes
- `rot oder 3` – Rotes ODER Dreiergruppen
- `(rot oder blau) und nicht Gemüse` – Rotes oder Blaues, aber kein Gemüse

**Teller-Regeln** (gelten pro Karte auf dem gelegten Teller):
- `links` / `rechts` – nur die zuerst / zuletzt gelegte Karte (eine einzelne Karte ist beides)
- `groesse:2` – nur Teller mit genau 2 Karten
- kombinierbar: `blau und links`, `Süßes und groesse:3`, `(rot oder blau) und rechts`

Monster mit Teller-Regel bekommen auf der Rückseite keine Bildtabelle und sind für Solo ungeeignet.

### Prüfung
`python3 build.py --pruefen` meldet Tippfehler in Regeln (mit Liste der erlaubten Wörter), unspielbare Regeln (trifft auf alle oder keine Zutat), doppelte Namen und Monster mit identischer Wirkung, und listet pro Monster, wie viele Zutaten gegessen werden.

## Hinweis
Die Kartengröße ist fest 63 × 88 mm; Layout der Karteninhalte ist darauf abgestimmt.

### anleitung.txt

'# Format: Zeilen mit "#" sind Überschriften, "- " sind Aufzählungspunkte, alles andere ist Fließtext.
'# Platzhalter: {titel} {untertitel} {anzahl_zutaten} {anzahl_monster} {stufenfarben} {urheber}
'#   und je Marker aus config.yaml: {geduld} {geduld_reserve} {geduld_gesamt} (analog saettigung, schnupperteller)
'# Diese erste Zeile (Kommentar) wird nicht ausgegeben, weil sie vor der ersten Überschrift steht.
