#!/usr/bin/env python3
"""Monsterbuffet - Kartengenerator.

Aufruf:
    python3 build.py                  # PDF erzeugen (liest config.yaml)
    python3 build.py --pruefen        # nur Daten prüfen und Regeln anzeigen
    python3 build.py -c andere.yaml   # andere Konfiguration verwenden

Alles Veränderliche steht in config.yaml, zutaten.csv, monster.csv und anleitung.txt.
"""
import argparse, copy, csv, itertools, os, re, sys

try:
    import yaml
    from reportlab.pdfgen import canvas
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import mm
    from reportlab.lib.utils import simpleSplit
    from reportlab.lib.colors import HexColor, white, black
    from reportlab.pdfbase.pdfmetrics import stringWidth
    from svglib.svglib import svg2rlg
    from reportlab.graphics import renderPDF
except ImportError as e:
    sys.exit(f"Fehlendes Paket: {e.name}. Installieren mit: pip install -r requirements.txt")

HERE = os.path.dirname(os.path.abspath(__file__))


def fail(msg):
    sys.exit("FEHLER: " + msg)


# ------------------------------------------------------------------ Konfiguration laden
ap = argparse.ArgumentParser()
ap.add_argument("-c", "--config", default=os.path.join(HERE, "config.yaml"))
ap.add_argument("--pruefen", action="store_true", help="nur prüfen, kein PDF")
args = ap.parse_args()
CFG_PATH = os.path.abspath(args.config)
BASE = os.path.dirname(CFG_PATH)
cfg = yaml.safe_load(open(CFG_PATH, encoding="utf-8"))


def path(p):
    return p if os.path.isabs(p) else os.path.join(BASE, p)


ICON_DIR = path(cfg["dateien"]["icon_ordner"])
OUT = path(cfg["dateien"]["ausgabe"])
CACHE = path(cfg["dateien"].get("cache_ordner", ".cache"))
os.makedirs(CACHE, exist_ok=True)
os.makedirs(os.path.dirname(OUT) or ".", exist_ok=True)

LAY = cfg["layout"]
W, H = A4
CW, CH = 63 * mm, 88 * mm
G = LAY["abstand_mm"] * mm
BLEED = LAY["anschnitt_mm"] * mm
BAND = LAY["randbreite_mm"] * mm
PM = LAY["seitenrand_mm"] * mm
COLS = int((W - 2 * PM + G) // (CW + G))
ROWS = int((H - 2 * PM + G) // (CH + G))
PER_PAGE = COLS * ROWS
if PER_PAGE < 1:
    fail("Seitenrand/Abstand so groß, dass keine Karte auf die Seite passt.")
GX = (W - (COLS * CW + (COLS - 1) * G)) / 2
GY = (H - (ROWS * CH + (ROWS - 1) * G)) / 2

COLORS = cfg["farben"]["zutaten"]              # Farbname -> Hex
DISPLAY = cfg["farben"].get("anzeigenamen", {})  # Farbname -> Beschriftung
STUFEN = {int(k): v for k, v in cfg["stufen"].items()}
TXT = cfg["texte"]
INK = cfg["farben"].get("text", "#222222")
RULE_COL = cfg["farben"].get("regeltext", "#B0302A")
OK_COL = cfg["farben"].get("mampf", "#2E9E4F")
NO_COL = cfg["farben"].get("baeh", "#D7263D")


def display(color):
    return DISPLAY.get(color, color)


# ------------------------------------------------------------------ CSV laden
def read_csv(name, needed):
    p = path(cfg["dateien"][name])
    with open(p, encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f, delimiter=";"))
    if rows:
        missing = [c for c in needed if c not in rows[0]]
        if missing:
            fail(f"{os.path.basename(p)}: Spalte(n) fehlen: {', '.join(missing)}")
    return [{k.strip(): (v or "").strip() for k, v in r.items() if k} for r in rows]


ingredients = read_csv("zutaten", ["name", "gruppe", "farbe", "anzahl", "icon"])
monster_rows = read_csv("monster", ["name", "stufe", "regel", "regeltext", "spruch", "mampf", "baeh", "icon", "farbe"])
for z in ingredients:
    z["anzahl"] = int(z["anzahl"])
    if z["farbe"] not in COLORS:
        fail(f"Zutat {z['name']}: Farbe '{z['farbe']}' fehlt in config.yaml (farben > zutaten).")
    if z["anzahl"] not in (1, 2, 3):
        fail(f"Zutat {z['name']}: Anzahl muss 1, 2 oder 3 sein.")
    if not os.path.exists(os.path.join(ICON_DIR, z["icon"])):
        fail(f"Zutat {z['name']}: Icon nicht gefunden: {os.path.join(ICON_DIR, z['icon'])}")

# ------------------------------------------------------------------ Regelsprache
# Wörter: Gruppen, Farben und Anzahlen (1/2/3) aus zutaten.csv, plus  links, rechts, groesse:N
# Operatoren: und, oder, nicht, Klammern
VOCAB = {}
for z in ingredients:
    for v in (z["gruppe"], z["farbe"], display(z["farbe"]), str(z["anzahl"])):
        VOCAB[v.lower()] = v
    VOCAB[z["gruppe"].lower()] = z["gruppe"]
    VOCAB[z["farbe"].lower()] = z["farbe"]
    VOCAB[display(z["farbe"]).lower()] = z["farbe"]
    VOCAB[str(z["anzahl"])] = str(z["anzahl"])
KEYWORDS = {"und", "oder", "nicht"}


def tokenize(s):
    return re.findall(r"\(|\)|[^\s()]+", s)


class Parser:
    def __init__(self, text):
        self.t, self.i, self.text = tokenize(text), 0, text

    def peek(self):
        return self.t[self.i].lower() if self.i < len(self.t) else None

    def take(self):
        self.i += 1
        return self.t[self.i - 1]

    def parse(self):
        e = self.p_or()
        if self.i != len(self.t):
            raise ValueError(f"unerwartet: '{self.t[self.i]}'")
        return e

    def p_or(self):
        e = self.p_and()
        while self.peek() == "oder":
            self.take(); e = ("or", e, self.p_and())
        return e

    def p_and(self):
        e = self.p_not()
        while self.peek() == "und":
            self.take(); e = ("and", e, self.p_not())
        return e

    def p_not(self):
        if self.peek() == "nicht":
            self.take(); return ("not", self.p_not())
        return self.p_atom()

    def p_atom(self):
        w = self.peek()
        if w is None:
            raise ValueError("Ausdruck endet zu früh")
        if w == "(":
            self.take(); e = self.p_or()
            if self.peek() != ")":
                raise ValueError("Klammer nicht geschlossen")
            self.take(); return e
        if w in KEYWORDS or w == ")":
            raise ValueError(f"unerwartet: '{w}'")
        self.take()
        if w in ("links", "rechts"):
            return ("pos", w)
        m = re.match(r"^(groesse|größe):(\d+)$", w)
        if m:
            return ("size", int(m.group(2)))
        if w not in VOCAB:
            raise ValueError(f"unbekanntes Wort '{w}' (bekannt: {', '.join(sorted(set(VOCAB.values())))}, links, rechts, groesse:N)")
        return ("val", VOCAB[w])


def has_plate(e):
    if e[0] in ("pos", "size"):
        return True
    if e[0] == "val":
        return False
    return any(has_plate(x) for x in e[1:])


def ev(e, card, pos, size):
    k = e[0]
    if k == "val":
        return e[1] in (card["gruppe"], card["farbe"], str(card["anzahl"]))
    if k == "pos":
        return pos == e[1]
    if k == "size":
        return size == e[1]
    if k == "not":
        return not ev(e[1], card, pos, size)
    a = ev(e[1], card, pos, size)
    b = ev(e[2], card, pos, size)
    return (a and b) if k == "and" else (a or b)


monsters = []
problems = []
for r in monster_rows:
    if not r["name"]:
        continue
    try:
        ast = Parser(r["regel"]).parse()
    except ValueError as e:
        fail(f"Monster {r['name']}: Regel '{r['regel']}': {e}")
    lv = int(r["stufe"])
    if lv not in STUFEN:
        fail(f"Monster {r['name']}: Stufe {lv} fehlt in config.yaml (stufen).")
    if not os.path.exists(os.path.join(ICON_DIR, r["icon"])):
        fail(f"Monster {r['name']}: Icon nicht gefunden: {os.path.join(ICON_DIR, r['icon'])}")
    m = dict(r, level=lv, ast=ast, plate=has_plate(ast))
    if not m["plate"]:
        m["table"] = frozenset(i for i, c in enumerate(ingredients) if ev(ast, c, None, None))
        n = len(m["table"])
        if n == 0 or n == len(ingredients):
            problems.append(f"{m['name']}: Regel trifft auf {n} von {len(ingredients)} Zutaten zu (unspielbar).")
    monsters.append(m)
names = [m["name"] for m in monsters]
for n in {x for x in names if names.count(x) > 1}:
    problems.append(f"Name doppelt: {n}")
seen = {}
for m in monsters:
    if not m["plate"]:
        if m["table"] in seen:
            problems.append(f"{m['name']} und {seen[m['table']]} haben dieselbe Wirkung auf die Zutaten.")
        seen[m["table"]] = m["name"]

pal = itertools.cycle(cfg["farben"]["monsterfarben"])
for m in monsters:
    m["color"] = m["farbe"] or next(pal)
monsters.sort(key=lambda m: (m["level"], m["name"].lower()))

if args.pruefen:
    print(f"{len(ingredients)} Zutaten, {len(monsters)} Monster, {COLS}x{ROWS} Karten pro Seite")
    for lv in sorted(STUFEN):
        print(f"  Stufe {lv}: {sum(1 for m in monsters if m['level'] == lv)} Monster")
    print()
    for m in monsters:
        if m["plate"]:
            info = "Teller-Regel"
        else:
            info = f"{len(m['table'])}/{len(ingredients)} Zutaten gegessen"
        print(f"  [{m['level']}] {m['name']:<28} {m['regeltext'] or m['regel']:<50} {info}")
    print()
    print("Hinweise:" if problems else "Keine Auffälligkeiten.")
    for p in problems:
        print("  -", p)
    sys.exit(0)
for p in problems:
    print("Hinweis:", p)

# ------------------------------------------------------------------ Zeichnen
_CACHE = {}


def tinted(rel, color):
    key = (rel, color)
    if key not in _CACHE:
        src_path = os.path.join(ICON_DIR, rel)
        src = open(src_path, encoding="utf-8").read()
        new = re.sub(r'fill="(#000(000)?|black)"', f'fill="{color}"', src)
        out = os.path.join(CACHE, re.sub(r"[^A-Za-z0-9]+", "_", rel) + color.strip("#") + ".svg")
        open(out, "w", encoding="utf-8").write(new)
        d = svg2rlg(out)
        if d is None:
            fail(f"Icon nicht lesbar: {src_path}")
        _CACHE[key] = d
    return copy.deepcopy(_CACHE[key])


def draw_svg(c, d, x, y, size):
    sc = size / d.width
    d.width *= sc; d.height *= sc
    d.scale(sc, sc)
    renderPDF.draw(d, c, x, y)


def icon(c, rel, color, x, y, size):
    draw_svg(c, tinted(rel, color), x, y, size)


def mix_white(hexcol, t=0.86):
    h = hexcol.lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
    return "#%02X%02X%02X" % tuple(int(v + (255 - v) * t) for v in (r, g, b))


BR = cfg["gehirn"]


def brains(c, n, cx, y, size=6.5 * mm, gap=1.2 * mm):
    total = n * size + (n - 1) * gap
    x0 = cx - total / 2
    for k in range(n):
        icon(c, BR["icon"], BR["farbe"], x0 + k * (size + gap), y, size)


def grid_pos(i):
    col, row = i % COLS, i // COLS
    return GX + col * (CW + G), H - GY - (row + 1) * CH - row * G


def crop_marks(c):
    if not LAY["schnittmarken"]:
        return
    c.saveState()
    c.setStrokeColor(black)
    c.setLineWidth(0.3)
    top, bottom = H - GY, GY
    for col in range(COLS):
        for x in (GX + col * (CW + G), GX + col * (CW + G) + CW):
            c.line(x, top + 2.5 * mm, x, top + 5.5 * mm)
            c.line(x, bottom - 2.5 * mm, x, bottom - 5.5 * mm)
    left, right = GX, GX + COLS * CW + (COLS - 1) * G
    for row in range(ROWS):
        yt = H - GY - row * (CH + G)
        for yy in (yt, yt - CH):
            c.line(left - 2.5 * mm, yy, left - 5.5 * mm, yy)
            c.line(right + 2.5 * mm, yy, right + 5.5 * mm, yy)
    c.restoreState()


def footer(c, text):
    c.setFont("Helvetica", 7)
    c.setFillColor(HexColor("#999999"))
    c.drawString(8 * mm, 4.5 * mm, text)


def wrapped(c, text, x, y, width, font="Helvetica", size=10, lead=13, color="#222222"):
    c.setFont(font, size)
    c.setFillColor(HexColor(color))
    for line in simpleSplit(text, font, size, width):
        c.drawString(x, y, line)
        y -= lead
    return y


def fit_text(c, text, cx, y, maxw, font="Helvetica-Bold", size=13):
    while stringWidth(text, font, size) > maxw and size > 6.5:
        size -= 0.5
    c.setFont(font, size)
    c.drawCentredString(cx, y, text)


def ingredient_card(c, z, x, y):
    col = COLORS[z["farbe"]]
    c.setFillColor(HexColor(INK))
    fit_text(c, z["name"], x + CW / 2, y + CH - 14 * mm, CW - 10 * mm, size=16)
    cx, cy = x + CW / 2, y + CH / 2 - 2 * mm
    n = z["anzahl"]
    if n == 1:
        pts, size = [(cx, cy)], 36 * mm
    elif n == 2:
        pts, size = [(cx - 14.5 * mm, cy), (cx + 14.5 * mm, cy)], 26 * mm
    else:
        pts, size = [(cx, cy + 12 * mm), (cx - 13.5 * mm, cy - 11.5 * mm), (cx + 13.5 * mm, cy - 11.5 * mm)], 25 * mm
    for (px, py) in pts:
        icon(c, z["icon"], col, px - size / 2, py - size / 2, size)
    c.setFont("Helvetica", 9)
    c.setFillColor(HexColor("#555555"))
    c.drawCentredString(x + CW / 2, y + 8 * mm,
                        TXT["zutat_zeile"].format(gruppe=z["gruppe"], farbe=display(z["farbe"]), anzahl=z["anzahl"]))


def level_border(c, x, y, level):
    c.setFillColor(HexColor(STUFEN[level]["farbe"]))
    c.rect(x - BLEED, y - BLEED, CW + 2 * BLEED, CH + 2 * BLEED, fill=1, stroke=0)
    c.setFillColor(white)
    c.rect(x + BAND, y + BAND, CW - 2 * BAND, CH - 2 * BAND, fill=1, stroke=0)


def monster_front(c, mo, x, y):
    level_border(c, x, y, mo["level"])
    cx = x + CW / 2
    cy = y + 60 * mm
    c.setFillColor(HexColor(mix_white(mo["color"])))
    c.circle(cx, cy, 20 * mm, fill=1, stroke=0)
    size = 31 * mm
    icon(c, mo["icon"], mo["color"], cx - size / 2, cy - size / 2, size)
    c.setFillColor(HexColor(INK))
    fit_text(c, mo["name"], cx, y + 34 * mm, CW - 2 * BAND - 4 * mm, size=13)
    c.setFont("Helvetica-Oblique", 8.5)
    c.setFillColor(HexColor("#444444"))
    yy = y + 28 * mm
    for ln in simpleSplit("„" + mo["spruch"] + "“", "Helvetica-Oblique", 8.5, CW - 2 * BAND - 6 * mm):
        c.drawCentredString(cx, yy, ln)
        yy -= 3.9 * mm
    brains(c, mo["level"], cx, y + 8 * mm)


def mini_card(c, z, cx, cy, size):
    col = COLORS[z["farbe"]]
    c.setStrokeColor(HexColor("#C8C8C8"))
    c.setLineWidth(0.5)
    c.setFillColor(white)
    c.roundRect(cx - size / 2, cy - size / 2, size, size, 1.2 * mm, fill=1, stroke=1)
    n = z["anzahl"]
    u = size / 11.0
    if n == 1:
        pts, sz = [(0, 0)], 8.2 * u
    elif n == 2:
        pts, sz = [(-2.8 * u, 0), (2.8 * u, 0)], 5.2 * u
    else:
        pts, sz = [(0, 2.5 * u), (-2.7 * u, -2.1 * u), (2.7 * u, -2.1 * u)], 4.7 * u
    for dx, dy in pts:
        icon(c, z["icon"], col, cx + dx - sz / 2, cy + dy - sz / 2, sz)


def saying(c, x, y, text, color, width, size=7.2, lead=3.0 * mm):
    c.setFillColor(HexColor(color))
    c.circle(x, y + 0.9 * mm, 1.0 * mm, fill=1, stroke=0)
    c.setFont("Helvetica-Oblique", size)
    c.setFillColor(HexColor("#333333"))
    for ln in simpleSplit(text, "Helvetica-Oblique", size, width):
        c.drawString(x + 2.6 * mm, y, ln)
        y -= lead
    return y


def monster_back(c, mo, x, y):
    level_border(c, x, y, mo["level"])
    cx = x + CW / 2
    c.setFillColor(HexColor(INK))
    fit_text(c, mo["name"], cx, y + 77.5 * mm, CW - 2 * BAND - 4 * mm, size=10)
    brains(c, mo["level"], cx, y + 69.6 * mm, size=4.8 * mm, gap=1 * mm)
    if mo["plate"]:
        icon(c, TXT["teller_regel_icon"], "#777777", cx - 8.5 * mm, y + 44 * mm, 17 * mm)
        c.setFont("Helvetica-Bold", 9.5)
        c.setFillColor(HexColor("#444444"))
        c.drawCentredString(cx, y + 38 * mm, TXT["teller_regel_titel"])
        c.setFont("Helvetica", 7)
        c.setFillColor(HexColor("#666666"))
        c.drawCentredString(cx, y + 34.2 * mm, TXT["teller_regel_zeile1"])
        c.drawCentredString(cx, y + 31 * mm, TXT["teller_regel_zeile2"])
    else:
        n = len(ingredients)
        rows = -(-n // 4)
        cell_w = 13 * mm
        cell_h = min(10 * mm, 40 * mm / rows)
        thumb = min(9.2 * mm, cell_h - 0.8 * mm)
        gx0 = x + (CW - 4 * cell_w) / 2
        gy0 = y + 67.8 * mm
        for k, z in enumerate(ingredients):
            col, row = k % 4, k // 4
            tcx = gx0 + col * cell_w + cell_w / 2
            tcy = gy0 - row * cell_h - cell_h / 2
            mini_card(c, z, tcx, tcy, thumb)
            ok = k in mo["table"]
            bx, by = tcx + thumb / 2 - 0.5 * mm, tcy + thumb / 2 - 0.5 * mm
            c.setFillColor(HexColor(OK_COL if ok else NO_COL))
            c.setStrokeColor(white)
            c.setLineWidth(0.7)
            c.circle(bx, by, 2.2 * mm, fill=1, stroke=1)
            c.setLineWidth(1.0)
            if ok:
                c.line(bx - 1.1 * mm, by - 0.1 * mm, bx - 0.3 * mm, by - 0.9 * mm)
                c.line(bx - 0.3 * mm, by - 0.9 * mm, bx + 1.2 * mm, by + 0.9 * mm)
            else:
                c.line(bx - 0.9 * mm, by - 0.9 * mm, bx + 0.9 * mm, by + 0.9 * mm)
                c.line(bx - 0.9 * mm, by + 0.9 * mm, bx + 0.9 * mm, by - 0.9 * mm)
    sx, sw = x + BAND + 3.5 * mm, CW - 2 * BAND - 9 * mm
    yy = saying(c, sx, y + 24.2 * mm, mo["mampf"], OK_COL, sw)
    saying(c, sx, yy - 0.8 * mm, mo["baeh"], NO_COL, sw)
    c.setStrokeColor(HexColor("#BBBBBB"))
    c.setLineWidth(0.5)
    c.line(x + BAND + 4 * mm, y + 13.4 * mm, x + CW - BAND - 4 * mm, y + 13.4 * mm)
    c.setFillColor(HexColor(RULE_COL))
    size = 7.8
    text = mo["regeltext"] or mo["regel"]
    lines = simpleSplit(text, "Helvetica-Bold", size, CW - 2 * BAND - 6 * mm)
    while len(lines) > 2 and size > 6.5:
        size -= 0.5
        lines = simpleSplit(text, "Helvetica-Bold", size, CW - 2 * BAND - 6 * mm)
    c.setFont("Helvetica-Bold", size)
    yy = y + 9.4 * mm
    for ln in lines:
        c.drawCentredString(cx, yy, ln)
        yy -= 3.2 * mm


def token_card(c, t, x, y):
    cx = x + CW / 2
    c.setFillColor(HexColor(INK))
    fit_text(c, t["titel"], cx, y + CH - 16 * mm, CW - 10 * mm, size=18)
    size = 38 * mm
    icon(c, t["icon"], t["farbe"], cx - size / 2, y + CH / 2 - size / 2 - 2 * mm, size)
    c.setFillColor(HexColor("#555555"))
    fit_text(c, t["untertitel"], cx, y + 12 * mm, CW - 10 * mm, font="Helvetica", size=9)


# ------------------------------------------------------------------ Platzhalter für Anleitung
def author_of(rel):
    folder = os.path.basename(os.path.dirname(rel))
    return cfg.get("urheber", {}).get(folder, folder.replace("-", " ").title())


def icon_title(rel):
    return os.path.splitext(os.path.basename(rel))[0].replace("-", " ").capitalize()


MARKER = cfg["marker"]
for t in MARKER:
    if not os.path.exists(os.path.join(ICON_DIR, t["icon"])):
        fail(f"Marker {t['titel']}: Icon nicht gefunden.")
used = [m["icon"] for m in monsters] + [z["icon"] for z in ingredients] + [t["icon"] for t in MARKER] + [BR["icon"]]
authors = sorted({author_of(u) for u in used})
PH = {
    "titel": cfg["spiel"]["titel"],
    "untertitel": cfg["spiel"]["untertitel"],
    "anzahl_zutaten": len(ingredients),
    "anzahl_monster": len(monsters),
    "stufenfarben": ", ".join(f"{STUFEN[k]['name']} = {k}" for k in sorted(STUFEN)),
    "urheber": ", ".join(authors),
}
for t in MARKER:
    PH[t["schluessel"]] = t["im_spiel"]
    PH[t["schluessel"] + "_reserve"] = t["reserve"]
    PH[t["schluessel"] + "_gesamt"] = t["im_spiel"] + t["reserve"]


def fill(s):
    return re.sub(r"\{(\w+)\}", lambda m: str(PH.get(m.group(1), m.group(0))), s)


def parse_anleitung(p):
    blocks, cur = [], None
    for line in open(p, encoding="utf-8").read().splitlines():
        if line.startswith("#"):
            cur = dict(head=fill(line.lstrip("# ").strip()), body=[], bullets=[])
            blocks.append(cur)
        elif cur is None or not line.strip():
            continue
        elif line.lstrip().startswith("- "):
            cur["bullets"].append(fill(line.lstrip()[2:].strip()))
        else:
            cur["body"].append(fill(line.strip()))
    return blocks


# ------------------------------------------------------------------ PDF
cv = canvas.Canvas(OUT, pagesize=A4)
cv.setTitle(cfg["spiel"]["titel"])
M_ = 18 * mm
S, L = 9.6, 12.4
state = {"y": 0}


def new_rules_page(first=False):
    if not first:
        footer(cv, TXT["fuss_anleitung"])
        cv.showPage()
    state["y"] = H - 18 * mm
    if first:
        cv.setFont("Helvetica-Bold", 22)
        cv.setFillColor(HexColor(INK))
        cv.drawString(M_, state["y"], cfg["spiel"]["titel"])
        state["y"] -= 6.5 * mm
        cv.setFont("Helvetica", 10)
        cv.setFillColor(HexColor("#555555"))
        cv.drawString(M_, state["y"], cfg["spiel"]["untertitel"])
        state["y"] -= 9 * mm


def block(head, body, bullets):
    n = len(simpleSplit(body, "Helvetica", S, W - 2 * M_)) + sum(
        len(simpleSplit(b, "Helvetica", S, W - 2 * M_ - 8 * mm)) for b in bullets)
    if state["y"] - (n + 2) * L - 8 * mm < 20 * mm:
        new_rules_page()
    cv.setFont("Helvetica-Bold", 11.5)
    cv.setFillColor(HexColor(RULE_COL))
    cv.drawString(M_, state["y"], head)
    state["y"] -= 5 * mm
    if body:
        state["y"] = wrapped(cv, body, M_, state["y"], W - 2 * M_, size=S, lead=L) - 1.5 * mm
    for b in bullets:
        cv.setFont("Helvetica-Bold", S)
        cv.setFillColor(HexColor(INK))
        cv.drawString(M_ + 3 * mm, state["y"], "•")
        state["y"] = wrapped(cv, b, M_ + 8 * mm, state["y"], W - 2 * M_ - 8 * mm, size=S, lead=L) - 1.0 * mm
    state["y"] -= 1.8 * mm


new_rules_page(first=True)
for b in parse_anleitung(path(cfg["dateien"]["anleitung"])):
    block(b["head"], " ".join(b["body"]), b["bullets"])
footer(cv, TXT["fuss_anleitung"])
cv.showPage()

# Zutaten
for p in range(0, len(ingredients), PER_PAGE):
    chunk = ingredients[p:p + PER_PAGE]
    for i, z in enumerate(chunk):
        x, yb = grid_pos(i)
        ingredient_card(cv, z, x, yb)
    crop_marks(cv)
    footer(cv, TXT["fuss_zutaten"].format(von=p + 1, bis=p + len(chunk), gesamt=len(ingredients)))
    cv.showPage()

# Monster: Vorder- und Rückseiten im Wechsel (Rückseiten gespiegelt für Duplex, lange Kante)
pages = [monsters[i:i + PER_PAGE] for i in range(0, len(monsters), PER_PAGE)]
for pi, chunk in enumerate(pages):
    for i, mo in enumerate(chunk):
        x, yb = grid_pos(i)
        monster_front(cv, mo, x, yb)
    crop_marks(cv)
    footer(cv, TXT["fuss_monster_vorne"].format(seite=pi + 1, seiten=len(pages)))
    cv.showPage()
    for i, mo in enumerate(chunk):
        col, row = i % COLS, i // COLS
        x, yb = grid_pos(row * COLS + (COLS - 1 - col))
        monster_back(cv, mo, x, yb)
    crop_marks(cv)
    footer(cv, TXT["fuss_monster_hinten"].format(seite=pi + 1, seiten=len(pages)))
    cv.showPage()

# Marker
tokens = [t for t in MARKER for _ in range(t["im_spiel"] + t["reserve"])]
for p in range(0, len(tokens), PER_PAGE):
    chunk = tokens[p:p + PER_PAGE]
    for i, t in enumerate(chunk):
        x, yb = grid_pos(i)
        token_card(cv, t, x, yb)
    crop_marks(cv)
    footer(cv, TXT["fuss_marker"].format(seite=p // PER_PAGE + 1))
    cv.showPage()

# Bildnachweis
cv.setFont("Helvetica-Bold", 14)
cv.setFillColor(HexColor(INK))
cv.drawString(15 * mm, H - 16 * mm, TXT["nachweis_titel"])
cv.setFont("Helvetica", 8)
cv.setFillColor(HexColor("#555555"))
for k, line in enumerate(TXT["nachweis_hinweis"]):
    cv.drawString(15 * mm, H - (20.5 + 4 * k) * mm, line)
rows = [(m["name"], m["icon"]) for m in sorted(monsters, key=lambda m: m["name"].lower())]
rows += [("Zutat " + z["name"], z["icon"]) for z in ingredients]
rows += [("Karte " + t["titel"], t["icon"]) for t in MARKER]
rows += [("Schwierigkeit", BR["icon"])]
per_col = 56
for i, (nm, rel) in enumerate(rows):
    col, row = i // per_col, i % per_col
    x = 15 * mm + col * 92 * mm
    yy = H - 31 * mm - row * 4.55 * mm
    if yy < 8 * mm:
        print("Hinweis: Bildnachweis passt nicht komplett auf die Seite.")
        break
    cv.setFont("Helvetica", 7.3)
    cv.setFillColor(HexColor(INK))
    cv.drawString(x, yy, f"{nm}: {icon_title(rel)} - Icons made by {author_of(rel)}")
cv.showPage()
cv.save()
print(f"Fertig: {OUT}  ({len(ingredients)} Zutaten, {len(monsters)} Monster)")
