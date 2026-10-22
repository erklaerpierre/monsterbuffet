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
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.units import mm
    from reportlab.lib.utils import simpleSplit
    from reportlab.lib.colors import HexColor, white, black
    from reportlab.pdfbase.pdfmetrics import stringWidth, registerFont
    from reportlab.pdfbase.ttfonts import TTFont
    from svglib.svglib import svg2rlg
    from reportlab.graphics import renderPDF
    from reportlab.platypus import Flowable, Paragraph, Spacer, Table, TableStyle
    from reportlab.lib.styles import ParagraphStyle
    from xml.sax.saxutils import escape
    from reportlab.lib.utils import ImageReader
    from PIL import Image
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

def load_font(key, name, default):
    if not cfg["dateien"].get(key):
        return default
    fp = path(cfg["dateien"][key])
    if not os.path.exists(fp):
        fail(f"{key} nicht gefunden: {fp}")
    registerFont(TTFont(name, fp))
    return name


TITLE_FONT = load_font("titelschrift", "Titel", "Helvetica-Bold")
BODY_FONT = load_font("textschrift", "Text", "Helvetica")
QUOTE_FONT = load_font("spruchschrift", "Spruch", "Helvetica-Oblique")

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


AUTOR = cfg.get("autor") or {}
WEB_SHORT = re.sub(r"^https?://(www\.)?", "", AUTOR.get("web", "")).rstrip("/")
CREDIT = " · ".join(s for s in (AUTOR.get("name"), WEB_SHORT) if s)
_LOGOS = {}


def logo(white=False):
    """Logo als ImageReader; white = einfarbig weiße Fassung (aus der Transparenz)."""
    if not AUTOR.get("logo"):
        return None
    if white not in _LOGOS:
        src = os.path.join(ICON_DIR, AUTOR["logo"])
        if not os.path.exists(src):
            fail(f"Logo nicht gefunden: {src}")
        im = Image.open(src).convert("RGBA")
        if white:
            im = Image.merge("RGBA", (*[Image.new("L", im.size, 255)] * 3, im.getchannel("A")))
        _LOGOS[white] = ImageReader(im)
    return _LOGOS[white]


def draw_logo(c, x, y, h, white=False):
    """Zeichnet das Logo mit Höhe h, gibt die Breite zurück."""
    img = logo(white)
    if img is None:
        return 0
    iw, ih = img.getSize()
    c.drawImage(img, x, y, h * iw / ih, h, mask="auto")
    return h * iw / ih


def footer(c, text):
    c.setFont("Helvetica", 7)
    c.setFillColor(HexColor("#999999"))
    c.drawString(8 * mm, 4.5 * mm, text)
    if CREDIT:
        c.drawRightString(c._pagesize[0] - 8 * mm, 4.5 * mm, "© " + CREDIT)


def fit_text(c, text, cx, y, maxw, font=TITLE_FONT, size=13):
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
    c.setFont(BODY_FONT, 10)
    c.setFillColor(HexColor("#555555"))
    c.drawCentredString(x + CW / 2, y + 8 * mm,
                        TXT["zutat_zeile"].format(gruppe=z["gruppe"], farbe=display(z["farbe"]), anzahl=z["anzahl"]))


def level_frame(c, x, y, color):
    c.setFillColor(HexColor(color))
    c.rect(x - BLEED, y - BLEED, CW + 2 * BLEED, CH + 2 * BLEED, fill=1, stroke=0)
    c.setFillColor(white)
    c.rect(x + BAND, y + BAND, CW - 2 * BAND, CH - 2 * BAND, fill=1, stroke=0)


def level_border(c, x, y, level):
    level_frame(c, x, y, STUFEN[level]["farbe"])


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
    c.setFont(QUOTE_FONT, 9.2)
    c.setFillColor(HexColor("#444444"))
    yy = y + 28 * mm
    for ln in simpleSplit("„" + mo["spruch"] + "“", QUOTE_FONT, 9.2, CW - 2 * BAND - 6 * mm):
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


def badge(c, bx, by, ok, r=2.2 * mm):
    """Grüner Haken oder rotes Kreuz im Kreis (r = Radius)."""
    u = r / (2.2 * mm)
    c.setFillColor(HexColor(OK_COL if ok else NO_COL))
    c.setStrokeColor(white)
    c.setLineWidth(0.7 * u)
    c.circle(bx, by, r, fill=1, stroke=1)
    c.setLineWidth(1.0 * u)
    if ok:
        c.line(bx - 1.1 * mm * u, by - 0.1 * mm * u, bx - 0.3 * mm * u, by - 0.9 * mm * u)
        c.line(bx - 0.3 * mm * u, by - 0.9 * mm * u, bx + 1.2 * mm * u, by + 0.9 * mm * u)
    else:
        c.line(bx - 0.9 * mm * u, by - 0.9 * mm * u, bx + 0.9 * mm * u, by + 0.9 * mm * u)
        c.line(bx - 0.9 * mm * u, by + 0.9 * mm * u, bx + 0.9 * mm * u, by - 0.9 * mm * u)


def saying(c, x, y, text, color, width, size=7.8, lead=3.0 * mm):
    c.setFillColor(HexColor(color))
    c.circle(x, y + 0.9 * mm, 1.0 * mm, fill=1, stroke=0)
    c.setFont(QUOTE_FONT, size)
    c.setFillColor(HexColor("#333333"))
    for ln in simpleSplit(text, QUOTE_FONT, size, width):
        c.drawString(x + 2.6 * mm, y, ln)
        y -= lead
    return y


def monster_back(c, mo, x, y):
    level_border(c, x, y, mo["level"])
    cx = x + CW / 2
    # Name mit kleinem Monster-Icon davor, zusammen zentriert
    isz, gap = 6 * mm, 1.3 * mm
    maxw = CW - 2 * BAND - 4 * mm - isz - gap
    size = 13
    while stringWidth(mo["name"], TITLE_FONT, size) > maxw and size > 6.5:
        size -= 0.5
    x0 = cx - (isz + gap + stringWidth(mo["name"], TITLE_FONT, size)) / 2
    icon(c, mo["icon"], mo["color"], x0, y + 76.4 * mm, isz)
    c.setFillColor(HexColor(INK))
    c.setFont(TITLE_FONT, size)
    c.drawString(x0 + isz + gap, y + 77.5 * mm, mo["name"])
    if mo["plate"]:
        icon(c, TXT["teller_regel_icon"], "#777777", cx - 9.5 * mm, y + 47 * mm, 19 * mm)
        c.setFont(TITLE_FONT, 10)
        c.setFillColor(HexColor("#444444"))
        c.drawCentredString(cx, y + 40 * mm, TXT["teller_regel_titel"])
        c.setFont(BODY_FONT, 7.8)
        c.setFillColor(HexColor("#666666"))
        c.drawCentredString(cx, y + 35.6 * mm, TXT["teller_regel_zeile1"])
        c.drawCentredString(cx, y + 32.2 * mm, TXT["teller_regel_zeile2"])
    else:
        n = len(ingredients)
        rows = -(-n // 4)
        cell_w = 13.2 * mm
        cell_h = min(11.5 * mm, 45.5 * mm / rows)
        thumb = min(10.4 * mm, cell_h - 1.1 * mm)
        gx0 = x + (CW - 4 * cell_w) / 2
        gy0 = y + 73.5 * mm
        for k, z in enumerate(ingredients):
            col, row = k % 4, k // 4
            tcx = gx0 + col * cell_w + cell_w / 2
            tcy = gy0 - row * cell_h - cell_h / 2
            mini_card(c, z, tcx, tcy, thumb)
            badge(c, tcx + thumb / 2 - 0.5 * mm, tcy + thumb / 2 - 0.5 * mm, k in mo["table"],
                  r=2.2 * mm * thumb / (9.2 * mm))
    sx, sw = x + BAND + 3.5 * mm, CW - 2 * BAND - 9 * mm
    yy = saying(c, sx, y + 24.2 * mm, mo["mampf"], OK_COL, sw)
    saying(c, sx, yy - 0.8 * mm, mo["baeh"], NO_COL, sw)
    c.setStrokeColor(HexColor("#BBBBBB"))
    c.setLineWidth(0.5)
    c.line(x + BAND + 4 * mm, y + 13.4 * mm, x + CW - BAND - 4 * mm, y + 13.4 * mm)
    c.setFillColor(HexColor(RULE_COL))
    size = 8.6
    text = mo["regeltext"] or mo["regel"]
    lines = simpleSplit(text, BODY_FONT, size, CW - 2 * BAND - 6 * mm)
    while len(lines) > 2 and size > 6.5:
        size -= 0.5
        lines = simpleSplit(text, BODY_FONT, size, CW - 2 * BAND - 6 * mm)
    c.setFont(BODY_FONT, size)
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
    fit_text(c, t["untertitel"], cx, y + 12 * mm, CW - 10 * mm, font=BODY_FONT, size=10)


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
used = [m["icon"] for m in monsters] + [z["icon"] for z in ingredients] + [t["icon"] for t in MARKER] + [BR["icon"], cfg["teller"]["icon"]] + [b["icon"] for b in cfg["rueckseiten"].values()]
authors = sorted({author_of(u) for u in used})


def variant_monsters(v):
    if not v.get("positionen"):
        return monsters
    out = []
    for lv in sorted(STUFEN):
        group = [m for m in monsters if m["level"] == lv]
        for p in v["positionen"]:
            if 1 <= p <= len(group):
                out.append(group[p - 1])
            else:
                print(f"Hinweis: {v['name']}: Stufe {lv} hat kein Monster Nr. {p}.")
    return out


VARIANTS = cfg.get("monster_varianten") or [{"name": ""}]
PICK = next((v for v in VARIANTS if v.get("positionen")), None)
PH = {
    "titel": cfg["spiel"]["titel"],
    "untertitel": cfg["spiel"]["untertitel"],
    "anzahl_zutaten": len(ingredients),
    "anzahl_monster": len(monsters),
    "stufenfarben": ", ".join(f"{STUFEN[k]['name']} = {k}" for k in sorted(STUFEN)),
    "urheber": ", ".join(authors),
    "autor": AUTOR.get("name", ""),
    "autor_web": AUTOR.get("web", ""),
    "lizenz": AUTOR.get("lizenz", ""),
    "lizenz_url": AUTOR.get("lizenz_url", ""),
}
if PICK:
    PH["anzahl_auswahl"] = len(variant_monsters(PICK))
    PH["auswahl_je_stufe"] = len(PICK["positionen"])
for t in MARKER:
    PH[t["schluessel"]] = t["im_spiel"]
    PH[t["schluessel"] + "_reserve"] = t["reserve"]
    PH[t["schluessel"] + "_gesamt"] = t["im_spiel"] + t["reserve"]


def fill(s):
    return re.sub(r"\{(\w+)\}", lambda m: str(PH.get(m.group(1), m.group(0))), s)


def parse_anleitung(p):
    """# Abschnitt, ## Kasten, - Aufzählung, Leerzeile = neuer Absatz,
    [material] / [beispiel] = Abbildung an dieser Stelle, [autor] Text = Absatz mit Logo davor."""
    blocks, cur = [], None
    for line in open(p, encoding="utf-8").read().splitlines():
        s = line.strip()
        if s.startswith("#"):
            cur = dict(head=fill(s.lstrip("# ").strip()), box=s.startswith("##"), items=[])
            blocks.append(cur)
        elif cur is None:
            continue
        elif not s:
            cur["items"].append(("break", ""))
        elif s in ("[material]", "[beispiel]"):
            cur["items"].append((s[1:-1], ""))
        elif s.startswith("[autor]"):
            cur["items"].append(("autor", fill(s[7:].strip())))
        elif s.startswith("- "):
            cur["items"].append(("li", fill(s[2:].strip())))
        elif cur["items"] and cur["items"][-1][0] == "p":
            cur["items"][-1] = ("p", cur["items"][-1][1] + " " + fill(s))
        else:
            cur["items"].append(("p", fill(s)))
    for b in blocks:
        b["items"] = [it for it in b["items"] if it[0] != "break"]
    return blocks


# ------------------------------------------------------------------ Anleitung: Satz
ACC = cfg["farben"].get("anleitung", "#4B2E6B")
BOX = cfg["farben"].get("kasten", "#F2EDF7")


class Band(Flowable):
    """Überschrift auf farbigem Band."""
    keep_next = True

    def __init__(self, text, size):
        super().__init__()
        self.text, self.size = text, size

    def wrap(self, aw, ah):
        self.w, self.bh = aw, self.size * 1.6
        return aw, self.bh + self.size * 0.55

    def draw(self):
        c, s = self.canv, self.size
        c.setFillColor(HexColor(ACC))
        c.roundRect(0, 0, self.w, self.bh, s * 0.35, fill=1, stroke=0)
        size = s
        while stringWidth(self.text, TITLE_FONT, size) > self.w - s and size > 5:
            size -= 0.5
        c.setFillColor(white)
        c.setFont(TITLE_FONT, size)
        c.drawString(s * 0.5, s * 0.48, self.text)


class MaterialFig(Flowable):
    """Übersicht aller Kartensorten mit Anzahl."""

    def __init__(self, label_style):
        super().__init__()
        self.st = label_style
        mo = next(m for m in monsters if m["level"] == 2)
        self.items = [("zutat", ingredients[0], f"{len(ingredients)} Zutaten"),
                      ("monster", mo, f"{len(monsters)} Monster")]
        self.items += [("marker", t, f"{t['im_spiel'] + t['reserve']}× {t['titel']}") for t in MARKER]
        self.items.append(("teller", None, "1 Teller"))

    def wrap(self, aw, ah):
        self.w = aw
        self.cw = aw / 3
        self.isz = min(self.cw * 0.62, 18 * mm)
        self.paras = [Paragraph(lbl, self.st) for _, _, lbl in self.items]
        self.lh = max(p.wrap(self.cw - 1 * mm, ah)[1] for p in self.paras)
        self.rh = self.isz + self.lh + self.isz * 0.25
        self.rows = -(-len(self.items) // 3)
        return aw, self.rows * self.rh

    def draw(self):
        c, s = self.canv, self.isz
        for k, (kind, obj, _) in enumerate(self.items):
            col, row = k % 3, k // 3
            cx = col * self.cw + self.cw / 2
            top = (self.rows - row) * self.rh
            cy = top - s / 2
            if kind == "zutat":
                mini_card(c, obj, cx, cy, s * 0.95)
            elif kind == "monster":
                w_, h_ = s * 0.72, s
                c.setFillColor(HexColor(STUFEN[obj["level"]]["farbe"]))
                c.roundRect(cx - w_ / 2, cy - h_ / 2, w_, h_, s * 0.06, fill=1, stroke=0)
                c.setFillColor(white)
                b = s * 0.06
                c.rect(cx - w_ / 2 + b, cy - h_ / 2 + b, w_ - 2 * b, h_ - 2 * b, fill=1, stroke=0)
                icon(c, obj["icon"], obj["color"], cx - s * 0.28, cy - s * 0.2, s * 0.56)
            elif kind == "marker":
                icon(c, obj["icon"], obj["farbe"], cx - s * 0.42, cy - s * 0.42, s * 0.84)
            else:
                c.setFillColor(HexColor(cfg["teller"]["rand"]))
                c.setStrokeColor(HexColor(cfg["teller"]["linie"]))
                c.setLineWidth(0.6)
                c.ellipse(cx - s * 0.62, cy - s * 0.42, cx + s * 0.62, cy + s * 0.42, fill=1, stroke=1)
                c.setFillColor(HexColor(cfg["teller"]["mulde"]))
                c.ellipse(cx - s * 0.46, cy - s * 0.29, cx + s * 0.46, cy + s * 0.29, fill=1, stroke=1)
            p = self.paras[k]
            pw, ph = p.wrap(self.cw - 1 * mm, self.lh)
            p.drawOn(c, col * self.cw + 0.5 * mm, top - s - s * 0.12 - ph)


def example_setup():
    """Erstes Stufe-1-Monster ohne Teller-Regel, Teller: nein - ja - nein."""
    for mo in monsters:
        if mo["level"] == 1 and not mo["plate"]:
            yes = [k for k in range(len(ingredients)) if k in mo["table"]]
            no = [k for k in range(len(ingredients)) if k not in mo["table"]]
            if yes and len(no) >= 2:
                return mo, [no[0], yes[0], no[1]]
    return None, []


EX_MON, EX_PLATE = example_setup()


class PlateFig(Flowable):
    """Beispielteller mit drei Zutaten und den Reaktionen des Monsters."""

    def wrap(self, aw, ah):
        self.w = aw
        return aw, aw * 0.36

    def draw(self):
        c, w, h = self.canv, self.w, self.w * 0.36
        T = cfg["teller"]
        c.setStrokeColor(HexColor(T["linie"]))
        c.setLineWidth(0.6)
        c.setFillColor(HexColor(T["rand"]))
        c.ellipse(0, 0, w, h, fill=1, stroke=1)
        c.setFillColor(HexColor(T["mulde"]))
        c.ellipse(w * 0.07, h * 0.1, w * 0.93, h * 0.9, fill=1, stroke=1)
        s = w * 0.2
        for n, k in enumerate(EX_PLATE):
            cx = w * (0.27 + 0.23 * n)
            mini_card(c, ingredients[k], cx, h / 2, s)
            badge(c, cx + s / 2 - 0.5 * mm, h / 2 + s / 2 - 0.5 * mm, k in EX_MON["table"], r=s * 0.14)


class LogoPara(Flowable):
    """Absatz mit dem Autor-Logo links daneben."""

    def __init__(self, para, h):
        super().__init__()
        self.para, self.lh = para, h

    def wrap(self, aw, ah):
        img = logo()
        self.lw = self.lh * img.getSize()[0] / img.getSize()[1] if img else 0
        self.gap = 2.5 * mm if img else 0
        self.ph = self.para.wrap(aw - self.lw - self.gap, ah)[1]
        self.top = 0.12 * self.lh
        self.h = max(self.lh + self.top if img else 0, self.ph)
        return aw, self.h

    def draw(self):
        draw_logo(self.canv, 0, self.h - self.top - self.lh, self.lh)
        self.para.drawOn(self.canv, self.lw + self.gap, self.h - self.ph)


class Keep:
    """Abschnitt, der möglichst nicht auf zwei Rahmen verteilt wird."""

    def __init__(self, items):
        self.items = items

    def wrap(self, aw, ah):
        return aw, sum(g.wrap(aw, ah)[1] for g in self.items)


def rule_story(width, sc, keep=False):
    """Flowables für die Anleitung; sc = Skalierung (1 = Heft, kleiner = Karten),
    keep = Abschnitte zusammenhalten."""
    body = ParagraphStyle("b", fontName=BODY_FONT, fontSize=10 * sc, leading=13 * sc,
                          textColor=HexColor(INK))
    intro = ParagraphStyle("i", parent=body, fontName=QUOTE_FONT, fontSize=11 * sc, leading=14.2 * sc)
    bullet = ParagraphStyle("li", parent=body, leftIndent=4.5 * mm * sc, bulletIndent=1 * mm * sc,
                            bulletFontName=BODY_FONT, bulletFontSize=10 * sc, bulletColor=HexColor(ACC))
    small = ParagraphStyle("s", parent=body, fontSize=8.5 * sc, leading=10.5 * sc, alignment=1)
    cap = ParagraphStyle("c", parent=body, fontName=QUOTE_FONT, fontSize=9 * sc, leading=11.5 * sc,
                         alignment=1, textColor=HexColor("#555555"))
    boxhead = ParagraphStyle("bh", parent=body, fontName=TITLE_FONT, fontSize=12 * sc, leading=15 * sc,
                             textColor=HexColor(ACC))

    def para(text, st, li=False):
        text = escape(text)
        m = re.match(r"^([^:.]{2,30}):", text)
        if m:
            text = f'<font color="{ACC}">{m.group(1)}:</font>' + text[m.end():]
        return Paragraph(text, st, bulletText="•" if li else None)

    def figure(kind):
        if kind == "material":
            return [MaterialFig(small), Spacer(1, 2 * mm * sc)]
        if not EX_MON:
            return []
        n = [ingredients[k]["name"] for k in EX_PLATE]
        text = (f"Beispiel: {EX_MON['name']} ({EX_MON['regeltext'] or EX_MON['regel']}). "
                f"{n[1]} wird gegessen, {n[0]} und {n[2]} spuckt es aus: gemischt.")
        fig = [Spacer(1, 1 * mm * sc), PlateFig(), Spacer(1, 1.2 * mm * sc), Paragraph(escape(text), cap)]
        return ([Keep(fig)] if keep else fig) + [Spacer(1, 2 * mm * sc)]

    out = []
    for bi, b in enumerate(parse_anleitung(path(cfg["dateien"]["anleitung"]))):
        flow_ = []
        for kind, text in b["items"]:
            if kind in ("material", "beispiel"):
                flow_ += figure(kind)
            elif kind == "autor":
                flow_ += [LogoPara(para(text, body), 11 * mm * sc), Spacer(1, 1.6 * mm * sc)]
            else:
                st = intro if bi == 0 and kind == "p" else (bullet if kind == "li" else body)
                flow_ += [para(text, st, kind == "li"), Spacer(1, 1.6 * mm * sc)]
        if b["box"]:
            inner = [Paragraph(escape(b["head"]), boxhead), Spacer(1, 1 * mm * sc)] + flow_[:-1]
            t = Table([[inner]], colWidths=[width])
            t.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), HexColor(BOX)),
                                   ("LINEBEFORE", (0, 0), (0, -1), 2.5 * sc, HexColor(ACC)),
                                   ("LEFTPADDING", (0, 0), (-1, -1), 4 * mm * sc),
                                   ("RIGHTPADDING", (0, 0), (-1, -1), 3 * mm * sc),
                                   ("TOPPADDING", (0, 0), (-1, -1), 2.5 * mm * sc),
                                   ("BOTTOMPADDING", (0, 0), (-1, -1), 2 * mm * sc)]))
            sec = [t, Spacer(1, 3 * mm * sc)]
        else:
            sec = [Band(b["head"], 12 * sc)] + flow_ + [Spacer(1, 1.8 * mm * sc)]
        out += [Keep(sec)] if keep else sec
    return out


def flow(story, sizes):
    """Verteilt Flowables auf Rahmen. sizes liefert (w, h) je Rahmen.
    Ergebnis: je Rahmen eine Liste (flowable, y ab Rahmenunterkante); gezeichnet wird später."""
    story, frames = list(story), []
    while story:
        w, h = next(sizes)
        avail, placed = h, []
        frames.append(placed)
        while story:
            f = story[0]
            if isinstance(f, Spacer) and not placed:
                story.pop(0)
                continue
            if isinstance(f, Keep):
                need = sum(g.wrap(w, h)[1] for g in f.items if not isinstance(g, Spacer) or g is not f.items[-1])
                if placed and need > avail and (need <= h or avail < 0.45 * h):
                    break
                story[0:1] = f.items
                continue
            fh = f.wrap(w, avail)[1]
            if fh <= avail + 0.01:
                if getattr(f, "keep_next", False) and len(story) > 1 and placed:
                    g = story[1]
                    rest = avail - fh
                    if g.wrap(w, rest)[1] > rest and not g.split(w, rest):
                        break
                story.pop(0)
                avail -= fh
                placed.append((f, avail))
                continue
            parts = f.split(w, avail)
            if parts and parts[0].wrap(w, avail)[1] <= avail + 0.01:
                story[0:1] = parts
                continue
            if not placed:
                print("Hinweis: Element zu groß für den Rahmen, wird abgeschnitten.")
                story.pop(0)
                placed.append((f, avail - fh))
                continue
            break
    return frames


def draw_frame(c, placed, x, y):
    for f, fy in placed:
        f.drawOn(c, x, y + fy)


# ------------------------------------------------------------------ PDF
cv = canvas.Canvas(OUT, pagesize=A4)
cv.setTitle(cfg["spiel"]["titel"])

# Spielregeln als Heft: Kopfband, zwei Spalten
RM, RG = 15 * mm, 8 * mm
RCW = (W - 2 * RM - RG) / 2
R_BOTTOM, R_TOP1, R_TOP = 16 * mm, H - 12 * mm - 46 * mm - 7 * mm, H - 22 * mm


def cover_icons(n):
    """n Monster gleichmäßig über die Liste verteilt."""
    return [monsters[int(k * len(monsters) / n + len(monsters) / (2 * n))] for k in range(n)]


def book_page(c, page):
    if page == 1:
        bh, btop = 46 * mm, H - 12 * mm
        c.setFillColor(HexColor(ACC))
        c.roundRect(RM, btop - bh, W - 2 * RM, bh, 4 * mm, fill=1, stroke=0)
        isz, ig = 21 * mm, 3 * mm
        x0 = W - RM - 7 * mm - 3 * isz - 2 * ig
        for k, mo in enumerate(cover_icons(3)):
            icon(c, mo["icon"], "#FFFFFF", x0 + k * (isz + ig), btop - bh / 2 - isz / 2, isz)
        tmax = x0 - RM - 14 * mm
        size = 44
        while stringWidth(cfg["spiel"]["titel"], TITLE_FONT, size) > tmax:
            size -= 1
        c.setFillColor(white)
        c.setFont(TITLE_FONT, size)
        c.drawString(RM + 8 * mm, btop - 24 * mm, cfg["spiel"]["titel"])
        size = 12
        while stringWidth(cfg["spiel"]["untertitel"], BODY_FONT, size) > tmax and size > 7:
            size -= 0.5
        c.setFont(BODY_FONT, size)
        c.drawString(RM + 8 * mm, btop - 33 * mm, cfg["spiel"]["untertitel"])
    else:
        c.setFont(TITLE_FONT, 11)
        c.setFillColor(HexColor(ACC))
        c.drawString(RM, H - 14 * mm, cfg["spiel"]["titel"])
        c.setFont(BODY_FONT, 9)
        c.drawRightString(W - RM, H - 14 * mm, TXT["regeln_titel"])
        c.setStrokeColor(HexColor(ACC))
        c.setLineWidth(0.6)
        c.line(RM, H - 16 * mm, W - RM, H - 16 * mm)
    c.setFont(BODY_FONT, 9)
    c.setFillColor(HexColor("#777777"))
    c.drawCentredString(W / 2, 9 * mm, f"– {page} –")


def book_sizes():
    k = 0
    while True:
        yield RCW, (R_TOP1 if k < 2 else R_TOP) - R_BOTTOM
        k += 1


book = flow(rule_story(RCW, 1.0), book_sizes())
for p in range(0, len(book), 2):
    book_page(cv, p // 2 + 1)
    for col, placed in enumerate(book[p:p + 2]):
        draw_frame(cv, placed, RM + col * (RCW + RG), R_BOTTOM)
    footer(cv, TXT["fuss_anleitung"])
    cv.showPage()

# Spielregeln als Karten (63 x 88 mm): Karte 1 = Deckblatt, danach Vorder-/Rückseite im Wechsel
CPAD = 2.6 * mm
NUM_H = 3.2 * mm
CFW = CW - 2 * BAND - 2 * CPAD
CFH = CH - 2 * BAND - 2 * CPAD - NUM_H


def rule_card(c, x, y, placed, num, total):
    level_frame(c, x, y, ACC)
    draw_frame(c, placed, x + BAND + CPAD, y + BAND + CPAD + NUM_H)
    c.setFont(BODY_FONT, 7)
    c.setFillColor(HexColor(ACC))
    c.drawRightString(x + CW - BAND - CPAD, y + BAND + 2 * mm, f"{num}/{total}")


def cover_card(c, x, y):
    c.setFillColor(HexColor(ACC))
    c.rect(x - BLEED, y - BLEED, CW + 2 * BLEED, CH + 2 * BLEED, fill=1, stroke=0)
    c.setStrokeColor(HexColor(mix_white(ACC, 0.45)))
    c.setLineWidth(0.6)
    c.roundRect(x + 3 * mm, y + 3 * mm, CW - 6 * mm, CH - 6 * mm, 2.5 * mm, fill=0, stroke=1)
    cx = x + CW / 2
    center, *sides = cover_icons(3)
    icon(c, center["icon"], "#FFFFFF", cx - 13 * mm, y + 50 * mm, 26 * mm)
    for k, mo in enumerate(sides):
        icon(c, mo["icon"], mix_white(ACC, 0.55), x + (8 if k == 0 else CW / mm - 23) * mm, y + 46 * mm, 15 * mm)
    c.setFillColor(white)
    fit_text(c, cfg["spiel"]["titel"], cx, y + 35 * mm, CW - 10 * mm, size=24)
    c.setFont(BODY_FONT, 8.5)
    yy = y + 27 * mm
    for ln in simpleSplit(cfg["spiel"]["untertitel"], BODY_FONT, 8.5, CW - 14 * mm):
        c.drawCentredString(cx, yy, ln)
        yy -= 3.8 * mm
    if CREDIT:
        lh, gap = 6 * mm, 1.8 * mm
        img = logo(white=True)
        lw = lh * img.getSize()[0] / img.getSize()[1] if img else 0
        tw = stringWidth(CREDIT, BODY_FONT, 7.5)
        x0 = cx - (lw + (gap if lw else 0) + tw) / 2
        draw_logo(c, x0, y + 7 * mm, lh, white=True)
        c.setFillColor(white)
        c.setFont(BODY_FONT, 7.5)
        c.drawString(x0 + lw + (gap if lw else 0), y + 9.2 * mm, CREDIT)


def card_sizes():
    while True:
        yield CFW, CFH


CSC = 0.72
rcards = [None] + flow(rule_story(CFW, CSC, keep=True), card_sizes())
n_rule = len(rcards) - 1
sheets = [rcards[i:i + 2 * PER_PAGE] for i in range(0, len(rcards), 2 * PER_PAGE)]
for si, sheet in enumerate(sheets):
    base = si * 2 * PER_PAGE
    for side in (0, 1):
        for j in range(side, len(sheet), 2):
            i = j // 2
            if side:
                col, row = i % COLS, i // COLS
                i = row * COLS + (COLS - 1 - col)
            x, y = grid_pos(i)
            if base + j == 0:
                cover_card(cv, x, y)
            else:
                rule_card(cv, x, y, sheet[j], base + j, n_rule)
        crop_marks(cv)
        key = "fuss_regelkarten_hinten" if side else "fuss_regelkarten_vorne"
        footer(cv, TXT[key].format(seite=si + 1, seiten=len(sheets)))
        cv.showPage()

def mirrored_pos(i):
    """Position auf der Rückseite (Spalten gespiegelt, Duplex lange Kante)."""
    col, row = i % COLS, i // COLS
    return grid_pos(row * COLS + (COLS - 1 - col))


def card_back(c, x, y, kind):
    """Gemeinsame, dezente Rückseite für Zutaten- und Markerkarten."""
    b = cfg["rueckseiten"][kind]
    c.setFillColor(HexColor(mix_white(ACC, 0.9)))
    c.rect(x - BLEED, y - BLEED, CW + 2 * BLEED, CH + 2 * BLEED, fill=1, stroke=0)
    c.setStrokeColor(HexColor(mix_white(ACC, 0.62)))
    c.setLineWidth(0.6)
    c.roundRect(x + 3 * mm, y + 3 * mm, CW - 6 * mm, CH - 6 * mm, 2.5 * mm, fill=0, stroke=1)
    cx = x + CW / 2
    size = 30 * mm
    icon(c, b["icon"], mix_white(ACC, 0.55), cx - size / 2, y + 40 * mm, size)
    c.setFillColor(HexColor(mix_white(ACC, 0.25)))
    fit_text(c, b["titel"], cx, y + 28 * mm, CW - 12 * mm, size=22)
    c.setFillColor(HexColor(mix_white(ACC, 0.45)))
    c.setFont(BODY_FONT, 8)
    c.drawCentredString(cx, y + 9 * mm, cfg["spiel"]["titel"])


def backs_page(c, n, kind, text):
    for i in range(n):
        x, yb = mirrored_pos(i)
        card_back(c, x, yb, kind)
    crop_marks(c)
    footer(c, text)
    c.showPage()


# Zutaten
for p in range(0, len(ingredients), PER_PAGE):
    chunk = ingredients[p:p + PER_PAGE]
    for i, z in enumerate(chunk):
        x, yb = grid_pos(i)
        ingredient_card(cv, z, x, yb)
    crop_marks(cv)
    footer(cv, TXT["fuss_zutaten"].format(von=p + 1, bis=p + len(chunk), gesamt=len(ingredients)))
    cv.showPage()
    backs_page(cv, len(chunk), "zutat", TXT["fuss_zutaten_hinten"].format(von=p + 1, bis=p + len(chunk)))

# Monster: Vorder- und Rückseiten im Wechsel (Rückseiten gespiegelt für Duplex, lange Kante)
for v in VARIANTS:
    sel = variant_monsters(v)
    prefix = v["name"] + ": " if v["name"] else ""
    pages = [sel[i:i + PER_PAGE] for i in range(0, len(sel), PER_PAGE)]
    for pi, chunk in enumerate(pages):
        for i, mo in enumerate(chunk):
            x, yb = grid_pos(i)
            monster_front(cv, mo, x, yb)
        crop_marks(cv)
        footer(cv, prefix + TXT["fuss_monster_vorne"].format(seite=pi + 1, seiten=len(pages)))
        cv.showPage()
        for i, mo in enumerate(chunk):
            col, row = i % COLS, i // COLS
            x, yb = grid_pos(row * COLS + (COLS - 1 - col))
            monster_back(cv, mo, x, yb)
        crop_marks(cv)
        footer(cv, prefix + TXT["fuss_monster_hinten"].format(seite=pi + 1, seiten=len(pages)))
        cv.showPage()
    print(f"{prefix}{len(sel)} Monster, {2 * len(pages)} Seiten")

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
    backs_page(cv, len(chunk), "marker", TXT["fuss_marker_hinten"].format(seite=p // PER_PAGE + 1))

# Teller (quer): Innenfläche groß genug für 3 Karten nebeneinander
TL = cfg["teller"]
PW, PH_ = landscape(A4)
cv.setPageSize((PW, PH_))
tcx, tcy = PW / 2, PH_ / 2 + 1 * mm
cv.setLineWidth(1.2)
cv.setStrokeColor(HexColor(TL["linie"]))
cv.setFillColor(HexColor(TL["rand"]))
cv.ellipse(tcx - 141 * mm, tcy - 98 * mm, tcx + 141 * mm, tcy + 98 * mm, fill=1, stroke=1)
cv.setLineWidth(0.6)
cv.ellipse(tcx - 136 * mm, tcy - 93 * mm, tcx + 136 * mm, tcy + 93 * mm, fill=0, stroke=1)
cv.setFillColor(HexColor(TL["mulde"]))
cv.setLineWidth(1.0)
cv.ellipse(tcx - 121 * mm, tcy - 81 * mm, tcx + 121 * mm, tcy + 81 * mm, fill=1, stroke=1)
icon(cv, TL["icon"], TL["linie"], tcx - 4.5 * mm, tcy + 82.5 * mm, 9 * mm)
cv.setFont(BODY_FONT, 10)
cv.setFillColor(HexColor(TL["schrift"]))
for x, ang, txt in ((tcx - 130 * mm, 90, TXT["teller_links"]), (tcx + 130 * mm, -90, TXT["teller_rechts"])):
    cv.saveState()
    cv.translate(x, tcy)
    cv.rotate(ang)
    cv.drawCentredString(0, -1.2 * mm, txt)
    cv.restoreState()
footer(cv, TXT["fuss_teller"])
cv.showPage()
cv.setPageSize(A4)

# Bildnachweis
cv.setFont(TITLE_FONT, 14)
cv.setFillColor(HexColor(INK))
cv.drawString(15 * mm, H - 16 * mm, TXT["nachweis_titel"])
cv.setFont("Helvetica", 8)
cv.setFillColor(HexColor("#555555"))
for k, line in enumerate(TXT["nachweis_hinweis"]):
    cv.drawString(15 * mm, H - (20.5 + 4 * k) * mm, line)
rows = [(m["name"], m["icon"]) for m in sorted(monsters, key=lambda m: m["name"].lower())]
rows += [("Zutat " + z["name"], z["icon"]) for z in ingredients]
rows += [("Karte " + t["titel"], t["icon"]) for t in MARKER]
rows += [("Schwierigkeit", BR["icon"]), ("Teller", TL["icon"])]
rows += [("Rückseite " + b["titel"], b["icon"]) for b in cfg["rueckseiten"].values()]
per_col = 56
for i, (nm, rel) in enumerate(rows):
    col, row = i // per_col, i % per_col
    x = 15 * mm + col * 92 * mm
    yy = H - (23 + 4 * len(TXT["nachweis_hinweis"])) * mm - row * 4.55 * mm
    if yy < 8 * mm:
        print("Hinweis: Bildnachweis passt nicht komplett auf die Seite.")
        break
    cv.setFont("Helvetica", 7.3)
    cv.setFillColor(HexColor(INK))
    cv.drawString(x, yy, f"{nm}: {icon_title(rel)} - Icons made by {author_of(rel)}")
footer(cv, "")
cv.showPage()
cv.save()
print(f"Fertig: {OUT}  ({len(ingredients)} Zutaten, {len(monsters)} Monster)")
