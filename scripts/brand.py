"""Generate the acroscope mark + wordmark SVGs. Hand-tuned knots, Hermite -> cubic Bezier."""
import sys, math
sys.path.insert(0, sys.argv[1])  # pylib
from fontTools.ttLib import TTFont
from fontTools.pens.svgPathPen import SVGPathPen
from fontTools.pens.transformPen import TransformPen

OUT = sys.argv[2]
AMBER = "#f5a524"; FG = "#e6e8ec"; DIM = "#9aa3b2"; BG = "#14161a"; PANEL = "#1c1f25"; LINE = "#2b3039"
BLUE = "#4cc2ff"; GREEN = "#5fd68b"; RED = "#ff6b6b"

def f(x):
    s = f"{x:.1f}"
    return s[:-2] if s.endswith(".0") else s

def hermite_path(knots):
    """knots: list of (x, y, tx, ty, hout, hin): tangent dir (unit-ish) and handle lengths out/in."""
    d = [f"M{f(knots[0][0])} {f(knots[0][1])}"]
    for (x0, y0, tx0, ty0, ho, _), (x1, y1, tx1, ty1, _, hi) in zip(knots, knots[1:]):
        n0 = math.hypot(tx0, ty0); n1 = math.hypot(tx1, ty1)
        c1 = (x0 + tx0 / n0 * ho, y0 + ty0 / n0 * ho)
        c2 = (x1 - tx1 / n1 * hi, y1 - ty1 / n1 * hi)
        d.append(f"C{f(c1[0])} {f(c1[1])} {f(c2[0])} {f(c2[1])} {f(x1)} {f(y1)}")
    return "".join(d)

def loop_trace(base=45, cross=35, top=14, side=24, loopw=11, ang=45, x0=2, x1=62, hin=12, hcross=9, hl=6.5, tilt=0):
    """A powerloop seen from the side: level entry from the left, pull up through the crossing into a round loop,
    back through the crossing, level exit to the right. `tilt` (degrees, negative = leaning back/left) rotates the
    loop about the crossing so the entry is a gentle climb and the exit a steeper drop."""
    a = math.radians(ang); c, s = math.cos(a), math.sin(a)
    loop = [
        (32, cross, c, -s, hl, hcross),      # crossing, heading up-right
        (32+loopw, side, 0, -1, hl, hl),     # loop right
        (32, top, -1, 0, hl, hl),            # loop top
        (32-loopw, side, 0, 1, hl, hl),      # loop left
        (32, cross, c, s, hcross, hl),       # crossing, heading down-right
    ]
    t = math.radians(tilt); ct, st = math.cos(t), math.sin(t)
    def rot(x, y, tx, ty, ho, hi):
        dx, dy = x-32, y-cross
        return (32 + dx*ct - dy*st, cross + dx*st + dy*ct, tx*ct - ty*st, tx*st + ty*ct, ho, hi)
    return [(x0, base, 1, 0, hin, 0)] + [rot(*k) for k in loop] + [(x1, base, 1, 0, 0, hin)]

def ring_arcs(r, base, gap_deg):
    """Ring of radius r about (32,32), broken where the trace (at height `base` near the edges) crosses it."""
    import math as m
    dy = base - 32
    ang = m.degrees(m.atan2(dy, m.sqrt(r*r - dy*dy)))   # right gap centre; left is 180-ang
    g = gap_deg/2
    if gap_deg <= 0:
        return f"M{f(32-r)} 32A{r} {r} 0 1 1 {f(32+r)} 32A{r} {r} 0 1 1 {f(32-r)} 32"
    def pt(deg):
        t = m.radians(deg); return (32 + r*m.cos(t), 32 + r*m.sin(t))
    arcs = []
    for a0, a1 in ((180 - ang + g, 360 + ang - g), (ang + g, 180 - ang - g)):
        (xa, ya), (xb, yb) = pt(a0), pt(a1)
        arcs.append(f"M{f(xa)} {f(ya)}A{r} {r} 0 {1 if a1-a0 > 180 else 0} 1 {f(xb)} {f(yb)}")
    return "".join(arcs)

def mark(sw_ring=4, sw_trace=4, ticks=True, r=25, gap=26, tick=5, trace_kw=None, color=AMBER):
    kw = dict(trace_kw or {})
    trace = hermite_path(loop_trace(**kw))
    ring = ring_arcs(r, kw.get("base", 42), gap)
    parts = [f'<path d="{ring}" fill="none" stroke="{color}" stroke-width="{sw_ring}" stroke-linecap="round" stroke-opacity="1"/>']
    if ticks:
        t = f"M32 {f(32-r-tick)}v{f(2*tick)}M32 {f(32+r-tick)}v{f(2*tick)}"
        parts.append(f'<path d="{t}" stroke="{color}" stroke-width="{sw_ring}" stroke-opacity="1"/>')
    parts.append(f'<path d="{trace}" fill="none" stroke="{color}" stroke-width="{sw_trace}" stroke-linecap="round" stroke-linejoin="round" stroke-opacity="1"/>')
    return "\n  ".join(parts)

def svg(w, h, body, vb=None, extra=""):
    vb = vb or f"0 0 {w} {h}"
    return f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{vb}" width="{w}" height="{h}"{extra}>\n  {body}\n</svg>\n'

# ---- wordmark via fontTools ----
def text_paths(text, fontfile, size, tracking=0.0, x=0, y=0):
    font = TTFont(fontfile); gs = font.getGlyphSet(); cmap = font.getBestCmap(); upem = font['head'].unitsPerEm
    kern = {}
    if 'kern' in font:
        for st in font['kern'].kernTables:
            kern.update(st.kernTable)
    scale = size/upem; pen_d = []; pen_x = x; prev = None
    for ch in text:
        g = cmap[ord(ch)]
        if prev is not None:
            pen_x += kern.get((prev, g), 0)*scale + tracking
        p = SVGPathPen(gs, ntos=lambda v: f(v))
        tp = TransformPen(p, (scale, 0, 0, -scale, pen_x, y))
        gs[g].draw(tp)
        pen_d.append(p.getCommands())
        pen_x += gs[g].width*scale; prev = g
    return "".join(pen_d), pen_x - x

BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
BOOK = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"

def write(name, s):
    open(f"{OUT}/{name}", "w").write(s)
    print(name, len(s.encode()), "bytes")

# ---- files ----
ICON = dict(gap=22, trace_kw=dict(base=42, cross=39, top=13, side=25, loopw=14, ang=55, hin=14, hcross=5, hl=7.2))
write("icon.svg", svg(64, 64, mark(**ICON)))
FAV = dict(sw_ring=5.5, sw_trace=5.5, ticks=False, r=26.5, gap=30,
           trace_kw=dict(base=42, cross=39, top=12, side=24.5, loopw=14, ang=55, hin=12, hcross=5, hl=7.5))
write("favicon.svg", svg(64, 64, mark(**FAV)))

# logo: mark 48px + wordmark
wm, wmw = text_paths("acroscope", BOLD, 34, tracking=-0.6)
mark_s = 48/64
mh = 48; gapx = 12
body = f'<g transform="scale({mark_s:.4f})">{mark(**ICON)}</g>\n  <path transform="translate({mh+gapx},{f(mh/2+12)})" fill="{FG}" d="{wm}"/>'
write("logo.svg", svg(round(mh+gapx+wmw+2), mh, body))

# banner 1200x400
tag, tagw = text_paths("find, measure and tag the tricks in your FPV sessions", BOOK, 26, tracking=0.2)
wm2, wm2w = text_paths("acroscope", BOLD, 92, tracking=-1.5)
# layout: mark 176px at x=120,y=92 ; text block to its right
mx, my, ms = 120, 100, 176/64
tx = mx + 176 + 44
blocks = []  # timeline motif: moments as coloured blocks on a strip at the bottom
strip_y, strip_h = 318, 26
import random
rnd = random.Random(7)
xx = 0
cols = [AMBER, BLUE, GREEN, AMBER, RED, AMBER, BLUE]
i = 0
while xx < 1200:
    w = rnd.randint(28, 90); g = rnd.randint(30, 110)
    c = cols[i % len(cols)]; i += 1
    blocks.append(f'<rect x="{xx+g}" y="{strip_y}" width="{w}" height="{strip_h}" rx="3" fill="{c}" opacity="0.28"/>')
    xx += g + w
# throttle trace: a quiet line with bumps where blocks are
pts = []
for x in range(0, 1201, 12):
    base = strip_y + strip_h - 4
    v = 0.35 + 0.12*math.sin(x/37) + 0.08*math.sin(x/11)
    # bumps
    for b in blocks:
        bx = float(b.split('x="')[1].split('"')[0]); bw = float(b.split('width="')[1].split('"')[0])
        if bx < x < bx + bw:
            v += 0.45*math.sin((x-bx)/bw*math.pi)
    pts.append(f"{x} {f(base - v*(strip_h-2)*1.3)}")
trace = "M" + "L".join(pts)
banner = [
    f'<rect width="1200" height="400" fill="{BG}"/>',
    f'<rect y="{strip_y-10}" width="1200" height="{strip_h+20}" fill="{PANEL}"/>',
    f'<path d="M0 {strip_y-10.5}h1200M0 {strip_y+strip_h+10.5}h1200" stroke="{LINE}" stroke-width="1" stroke-opacity="1"/>',
    *blocks,
    f'<path d="{trace}" fill="none" stroke="{DIM}" stroke-width="1.5" stroke-opacity="0.7"/>',
    f'<path d="M662.5 {strip_y-10}v{strip_h+20}" stroke="{AMBER}" stroke-width="2" stroke-opacity="1"/>',
    f'<path d="M656 {strip_y-10}h13l-6.5 7z" fill="{AMBER}"/>',
    f'<g transform="translate({mx},{my}) scale({ms:.4f})">{mark(**ICON)}</g>',
    f'<path transform="translate({tx},{my+110})" fill="{FG}" d="{wm2}"/>',
    f'<path transform="translate({tx+4},{my+156})" fill="{DIM}" d="{tag}"/>',
]
write("banner.svg", svg(1200, 400, "\n  ".join(banner)))
