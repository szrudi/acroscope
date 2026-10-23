"""OSD profiles derived from what Betaflight says, not measured by hand (#10 step 1).

A profile is a pilot's OSD as Betaflight has it: the OSD settings of a CLI dump (where the timers are, what they
count, at what precision, where the flight counter is), the font uploaded to the OSD chip, and where the DVR puts
Betaflight's character grid in the recorded picture (`grid`: the top-left of column 0, row 0, and the cell size, on
the frame scaled to 720 x 480 for NTSC, 720 x 576 for PAL). From those, `derive` builds a layout in the shape of
`osd.LAYOUTS` (cells, formats, the glyph box, which row is the arm timer) with digit templates rendered from the
font, and the STATS screen's rows are found by their labels rendered from the same font. Nobody surveys a frame or
labels digits: a new pilot pastes a dump, names a font, and checks the grid on one frame (the configuration page's
OSD tab, or `acroscope osd-profile`).

The grid is the one thing that is the DVR's, not Betaflight's. On the goggles flown here (720 x 480 analog DVR,
2026-10) column c starts at x = 3 + 24 c and row r at y = -4 + 36 r: fitted (`fit`) on the 10-09 and 10-11 timers (x = 4 on
the 09-23 dump's footage: the reader's one-pixel shifts take either).
Profiles live in the database (settings row `osd`, PROFILES_KEY), one document {version, profiles: {name: ...}};
osd.layouts() adds them to the hand-measured layouts, and detect_layout picks among all of them by which counts.
"""
import hashlib
import json
import math
import re
import sys

from . import betaflight

PROFILES_KEY = "osd"
GRID_NTSC = {"x": 3, "y": -4, "cw": 24, "ch": 36, "w": 720, "h": 480}
GRID_PAL = {**GRID_NTSC, "h": 576}
TRANSPARENT = 128         # a template's transparent pixel: the camera picture behind it, mid grey on average
STATS_LABELS = {"total_arm": "TOTAL ARM", "blackbox": "BLACKBOX"}
STATS_LABEL_MIN = 0.4     # a STATS row's label: 0.50-0.54 on its row in the stock font's thin strokes (10-07 007), 0.87 in
                          # Impact (10-14 005); other rows 0.22-0.45
STATS_LABEL_MARGIN = 0.15 # the row that wins must win by this much over the next best row (0.25-0.5 on the real frames)
STATS_INK = 0.6           # and the two cells after the label must not read as a letter: TOTAL FLIGHTS, TOTAL FLIGHT TIME and
                          # TOTAL DISTANCE begin like TOTAL ARM (0.73 in a crisp font) and go on where it stops
EDGE_KEEP = 0.75          # a digit cell partly outside the picture is read on what is left when this much of its box is in
NAME_RE = re.compile(r"[a-z0-9][a-z0-9._-]{0,39}")


GRID_RANGE = {"x": (-200, 200), "y": (-200, 200), "cw": (8, 64), "ch": (8, 96), "w": (160, 1920), "h": (120, 1088)}


def default_grid(video_system: str) -> dict:
    return dict(GRID_PAL if video_system == "PAL" else GRID_NTSC)


def check_grid(g: dict) -> dict:
    """A grid of whole pixels in sensible ranges (GRID_RANGE): a frame of 100000 x 100000 is nobody's DVR."""
    for k, (lo, hi) in GRID_RANGE.items():
        v = g.get(k)
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v != int(v) or not lo <= v <= hi:
            raise ValueError(f"grid {k} = {v!r}: whole pixels from {lo} to {hi}")
    return {k: int(g[k]) for k in GRID_RANGE}


def check_types(profile) -> None:
    """A profile's shape, before anything reads it (a request's or a stored one): ValueError naming what is wrong, so
    a client's bad input is a 400 and a broken stored profile is one named error, never a crash."""
    if not isinstance(profile, dict):
        raise ValueError("a profile is {settings, font, grid}")
    if not isinstance(profile.get("settings", {}), dict) or \
            any(not isinstance(k, str) or isinstance(v, bool) or not isinstance(v, (str, int, float)) for k, v in profile.get("settings", {}).items()):
        raise ValueError("settings: the dump's `set name = value` lines, {name: value}")
    f = profile.get("font", {})
    if not isinstance(f, dict) or any(not isinstance(f.get(k, ""), str) for k in ("name", "data")):
        raise ValueError("font: {name, data}, both text")
    if not isinstance(profile.get("grid", {}), dict):
        raise ValueError("grid: {x, y, cw, ch, w, h}")
    for k in ("version", "note"):
        if profile.get(k) is not None and not isinstance(profile[k], str):
            raise ValueError(f"{k}: text")


def _norm(v: list[float]) -> list[float]:
    m = sum(v) / len(v)
    sd = math.sqrt(sum((a - m) ** 2 for a in v) / len(v)) or 1.0
    return [(a - m) / sd for a in v]


def glyph_box(chars, grid: dict, text: str = "0123456789") -> tuple[int, int, int, int]:
    """(x, w, y, h) inside a cell of the opaque pixels of `text`'s characters together, in frame pixels."""
    opq = [(r, c) for ch in text for r, row in enumerate(chars[ord(ch)]) for c, p in enumerate(row) if p in ("00", "10")]
    if not opq:
        raise ValueError(f"the font has no glyphs for {text!r}")
    sx, sy = grid["cw"] / betaflight.CHAR_W, grid["ch"] / betaflight.CHAR_H
    r0, r1 = min(r for r, _ in opq), max(r for r, _ in opq)
    c0, c1 = min(c for _, c in opq), max(c for _, c in opq)
    gx, gy = math.floor(c0 * sx), math.floor(r0 * sy)
    return gx, math.ceil((c1 + 1) * sx) - gx, gy, math.ceil((r1 + 1) * sy) - gy


def render(chars, text: str, grid: dict, box: tuple) -> list[float]:
    """`text` side by side as it shows on the frame, each character over the columns and rows `box` (x, w, y, h)
    takes of its cell: black 0, white 255, transparent TRANSPARENT, normalised (the vector the reader correlates the
    same pixels of a frame with). A digit takes the digits' glyph box; a label takes whole cells."""
    bx, bw, by, bh = box
    sx, sy = grid["cw"] / betaflight.CHAR_W, grid["ch"] / betaflight.CHAR_H
    val = {"00": 0.0, "10": 255.0}
    out = []
    for j in range(bh):
        fy = min(betaflight.CHAR_H - 1, int((by + j) / sy))
        for ch in text:
            row = chars[ord(ch)][fy]
            out += [val.get(row[min(betaflight.CHAR_W - 1, int((bx + i) / sx))], TRANSPARENT) for i in range(bw)]
    return _norm(out)


def _font(profile: dict):
    f = profile.get("font") or {}
    if f.get("data"):
        return betaflight.unpack_font(f["data"])
    if f.get("name"):
        return betaflight.parse_mcm(betaflight.fetch_font(f["name"]))
    raise ValueError("a profile names a font (a Configurator font, or the .mcm of your own)")


def derive(profile: dict, name: str = "profile") -> dict:
    """A layout for the OSD reader from a profile {settings, font, grid}: the shape of osd.LAYOUTS, with its own
    digit templates (`tpl`), the STATS labels to find the STATS rows by (`stats_scan`), the flight counter's cells
    (`counter`) and what it was derived from (`derived`, `warnings`). ValueError when it cannot be read with."""
    check_types(profile)
    el = betaflight.osd_elements(profile.get("settings") or {})
    grid = check_grid({**default_grid(el["video_system"]), **(profile.get("grid") or {})})
    chars = _font(profile)
    X = lambda col: grid["x"] + grid["cw"] * col
    Y = lambda row: grid["y"] + grid["ch"] * row
    warnings = []
    box = glyph_box(chars, grid)
    timers = [t for t in el["timers"] if t["source"] in ("last", "total")]
    for t in el["timers"]:
        if t not in timers:
            warnings.append(f"timer {t['n']} counts {t['source']!r} time: not read (only the arm timer and the total armed time say when an arm was)")
    if not timers:
        raise ValueError("no timer to read: the dump shows neither an arm timer (source last armed) nor the total armed time "
                         "(osd_tim_1_pos / osd_tim_2_pos with the profile bit, osd_tim1 / osd_tim2)")
    gx, gw, gy, gh = box
    formats = []
    for t in timers:
        f = list(t["format"])
        for k, ch in enumerate(f):                   # a cell past the picture's right edge: read on what is left, or skipped
            left = grid["w"] - (X(t["col"] + 1 + k) + gx)
            if ch == "d" and left < gw * EDGE_KEEP:
                if k < 5:                            # MM:SS itself: a timer without its seconds says nothing
                    raise ValueError(f"timer {t['n']}: its digit at column {t['col'] + 1 + k} is outside the picture (x {X(t['col'] + 1 + k)} "
                                     f"on a {grid['w']} px frame): the grid is off, or the timer sits past the edge the DVR records")
                f[k] = "x"                           # a tenth or a hundredth: read without it
                warnings.append(f"timer {t['n']}: the cell at column {t['col'] + 1 + k} is outside the picture; read without it")
        formats.append("".join(f))
    right = max(X(t["col"] + 1 + k) + gx + gw for t, f in zip(timers, formats) for k, ch in enumerate(f) if ch == "d")
    if right > grid["w"]:
        gw -= right - grid["w"]                       # every cell's box narrowed alike: the templates stay one set
    # the top edge likewise: a cell on row 0 starts above the picture (the grid at y -4); the rows the DVR did not
    # record are left out of every box, or, when too much is gone, the counter is not read and a timer is refused
    counter = el["counter"]
    tops = [Y(t["row"]) + gy for t in timers] + ([Y(counter["row"]) + gy] if counter else [])
    if min(tops) < 0:
        cut = -min(tops)
        if cut > gh * (1 - EDGE_KEEP):
            if min(Y(t["row"]) + gy for t in timers) < 0:
                raise ValueError(f"a timer's digits start {cut} px above the picture: the grid is off (y {grid['y']})")
            warnings.append(f"the flight counter's digits start {cut} px above the picture: not read")
            counter = None
        else:
            gy, gh = gy + cut, gh - cut
    box = (gx, gw, gy, gh)
    arm = [i for i, t in enumerate(timers) if t["source"] == "last"]
    if len([t for t in timers if t["source"] == "total"]) > 1 or len(arm) > 1:
        raise ValueError("two timers count the same thing: keep one arm timer and one total")
    layout = {"x0": [X(t["col"] + 1) for t in timers], "pitch": grid["cw"], "rows": [Y(t["row"]) for t in timers],
              "names": ["top", "bottom"][:len(timers)] if len(timers) == 2 else ["arm" if arm else "total"],
              "formats": formats, "glyph": box, "cumulative": not arm, "tpl": {d: render(chars, d, grid, box) for d in "0123456789"},
              "frame": [grid["w"], grid["h"]], "grid": grid,
              "derived": {"profile": name, "font": (profile.get("font") or {}).get("name"), "craft": el["craft"],
                          "version": profile.get("version"), "timers": el["timers"]}}
    if arm:
        layout["arm_row"] = arm[0]
    # the STATS screen: value column at midCol + 7, a row found by its label at midCol - 13 (STATS_LABELS)
    mid = el["cols"] // 2
    lbox = glyph_box(chars, grid, "TOTALRMBCKX")
    cell = (0, grid["cw"], lbox[2], lbox[3])
    letters = [c for c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ" if any(p in ("00", "10") for row in chars[ord(c)] for p in row)]
    layout["stats_scan"] = {"x0": X(mid + betaflight.STATS_VALUE_COL), "label_x": X(mid + betaflight.STATS_LABEL_COL),
                            "rows": [Y(r) for r in range(el["rows"])], "label_box": [lbox[2], lbox[3]],
                            "labels": {k: render(chars, s, grid, cell) for k, s in STATS_LABELS.items()},
                            "letters": [render(chars, c, grid, cell) for c in letters]}
    if counter:
        layout["counter"] = {"x0": X(counter["col"] + 1), "y": Y(counter["row"]), "cells": min(5, el["cols"] - counter["col"] - 1)}
    if el["displayport"] and el["displayport"].upper() not in ("MAX7456", "AUTO"):
        warnings.append(f"osd_displayport_device = {el['displayport']}: an analog grid assumed (digital goggles record the OSD as a file of its own)")
    layout["warnings"] = warnings
    return layout


def osd_settings(dump: dict) -> dict:
    """The part of a parsed dump a profile keeps: the OSD's own settings, the video system, the stats switches and
    the craft name (nothing else of the quad's setup)."""
    keep = re.compile(r"(osd_.*|vcd_video_system|stats_.*|craft_name)")
    return {k: v for k, v in dump["settings"].items() if keep.fullmatch(k)}


def check_profiles(doc) -> dict:
    """The stored document, checked as it is stored: every profile derives (a font that parses, a dump with a
    timer). Raises ValueError naming the profile."""
    if not isinstance(doc, dict) or not isinstance(doc.get("profiles", {}), dict):
        raise ValueError("the OSD profiles are {version: 1, profiles: {name: {settings, font, grid}}}")
    from . import osd
    for name, p in doc.get("profiles", {}).items():
        if not NAME_RE.fullmatch(name) or name in osd.LAYOUTS:
            raise ValueError(f"{name!r}: a profile name is lower case letters, digits, '.', '_' and '-' (40 at most), and not a built-in layout's")
        if not isinstance(p, dict) or not isinstance(p.get("settings"), dict):
            raise ValueError(f"{name}: a profile has the dump's settings")
        try:
            check_types(p)
        except ValueError as e:
            raise ValueError(f"{name}: {e}") from None
        if not (p.get("font") or {}).get("data"):
            raise ValueError(f"{name}: a stored profile carries its font (font.data), so reading never fetches anything")
        try:
            derive(p, name)
        except ValueError as e:
            raise ValueError(f"{name}: {e}") from None
    return doc


def fingerprint(doc) -> str:
    return hashlib.sha1(json.dumps(doc, sort_keys=True).encode()).hexdigest()[:12] if doc else "none"


def make(dump_text: str, font: str | None = None, mcm: str | None = None, grid: dict | None = None, note: str = "") -> dict:
    """A profile from a dump's text and a font (a Configurator name, or the .mcm text of your own), font packed in."""
    d = betaflight.parse_dump(dump_text)
    if mcm:
        chars, fname = betaflight.parse_mcm(mcm), font or "custom"
    elif font:
        chars, fname = betaflight.parse_mcm(betaflight.fetch_font(font)), font
    else:
        raise ValueError("name a font (one of " + ", ".join(betaflight.FONTS) + ") or give the .mcm of your own")
    settings = osd_settings(d)
    p = {"settings": settings, "version": d["version"], "font": {"name": fname, "data": betaflight.pack_font(chars)},
         "grid": {**default_grid(betaflight.video_system(settings)), **(grid or {})}}
    if note:
        p["note"] = note
    return p


def describe(doc) -> dict:
    """What GET /api/osd answers: every stored profile derived (its cells, formats, warnings; not the templates),
    the ones that do not derive with the reason, the Configurator fonts, the built-in layouts."""
    from . import osd
    out, errors = {}, {}
    for name, p in ((doc or {}).get("profiles") or {}).items():
        try:
            lay = derive(p, name)
        except Exception as e:  # noqa: BLE001  (a broken stored profile is listed, never the page's 500)
            errors[name] = str(e) or type(e).__name__
            continue
        out[name] = {"font": (p.get("font") or {}).get("name"), "version": p.get("version"), "note": p.get("note", ""),
                     "grid": lay["grid"], "layout": _public(lay)}
    return {"profiles": out, "errors": errors, "fonts": betaflight.FONTS, "builtin": sorted(osd.LAYOUTS),
            "grid": {"NTSC": GRID_NTSC, "PAL": GRID_PAL}}


def glyphs(profile: dict, text: str = "0123456789:.#") -> dict:
    """{char: 18 rows of 'b' / 'w' / '.'} of the profile's font, for the page to draw."""
    chars = _font(profile)
    return {c: ["".join({"00": "b", "10": "w"}.get(px, ".") for px in row) for row in chars[ord(c)]] for c in text}


# ---- one frame: what the reader sees (the configuration page's OSD tab) ------------------------------------------

def _gray(path, t: float, frame: list) -> bytes:
    import subprocess
    cmd = ["ffmpeg", "-v", "error", "-nostdin", "-ss", f"{max(t, 0):.3f}", "-i", str(path), "-frames:v", "1",
           "-vf", f"scale={frame[0]}:{frame[1]}", "-pix_fmt", "gray", "-f", "rawvideo", "-"]
    return subprocess.run(cmd, capture_output=True).stdout


def probe(buf: bytes, layout: dict) -> dict:
    """Every cell the layout reads on one full frame, with what it reads: {cells, values, stats, counter}. A cell is
    {kind, row, k, x, y, fmt, digit, score, margin}."""
    from . import osd
    w = layout["frame"][0]
    g = layout["glyph"]
    tpl = layout["tpl"]
    cells, digits = [], []
    for row, f in enumerate(layout["formats"]):
        x0 = layout["x0"][row]
        ds = []
        for k, ch in enumerate(f):
            c = {"kind": "timer", "row": row, "k": k, "x": x0 + k * layout["pitch"], "y": layout["rows"][row], "fmt": ch}
            if ch == "d":
                d, sc, mg = _best(buf, w, c["x"], c["y"], g, tpl)
                c.update(digit=d, score=round(sc, 3), margin=round(mg, 3))
                ds.append(d)
            cells.append(c)
        digits.append(ds)
    vals = [osd._value(ds) for ds in digits]
    if layout["cumulative"]:
        values = {"arm": None, "total": vals[0]}
    else:
        a = layout.get("arm_row", 0)
        values = {"arm": vals[a], "total": vals[1 - a] if len(vals) > 1 else None}
    out = {"cells": cells, "values": values, "counter": None, "stats": None}
    if layout.get("counter"):
        c = layout["counter"]
        n, ds = [], []
        for k in range(c["cells"]):                    # "#N" left-aligned: digits until a cell reads as none
            d, sc, mg = _best(buf, w, c["x0"] + k * layout["pitch"], c["y"], g, tpl)
            if d is None or sc < osd.STATS_SCORE:
                break
            cells.append({"kind": "counter", "row": None, "k": k, "x": c["x0"] + k * layout["pitch"], "y": c["y"], "fmt": "d",
                          "digit": d, "score": round(sc, 3), "margin": round(mg, 3)})
            n.append(d)
        out["counter"] = int("".join(n)) if n else None
    rows = stats_rows(buf, layout)
    if rows:
        out["stats"] = {**rows, **osd.read_stats_frame(buf, layout, tpl, w)}
        for k in ("total_arm", "blackbox"):
            if rows.get(k) is not None:
                cells.append({"kind": "stats", "row": k, "k": 0, "x": layout["stats_scan"]["label_x"], "y": rows[k],
                              "w": layout["stats_scan"]["x0"] + 5 * layout["pitch"] - layout["stats_scan"]["label_x"], "fmt": "label"})
    return out


def _best(buf, w, x, y, glyph, tpl):
    """(digit, score, margin over the runner-up) of the cell at (x, y), the best over osd.SHIFTS."""
    from . import osd
    best = {}
    for dx, dy in osd.SHIFTS:
        feat = osd._glyph(buf, w, x + dx, y + dy, glyph)
        for d, t in (tpl.items() if feat else ()):
            s = sum(a * b for a, b in zip(feat, t)) / len(feat)
            if s > best.get(d, -9.0):
                best[d] = s
    if not best:
        return None, 0.0, 0.0
    sc = sorted(((s, d) for d, s in best.items()), reverse=True)
    return sc[0][1], sc[0][0], sc[0][0] - (sc[1][0] if len(sc) > 1 else 0.0)


def stats_rows(buf: bytes, layout: dict) -> dict:
    """{total_arm: y, blackbox: y} of the STATS screen's rows found by their labels on a full frame; {} when the
    frame shows no STATS screen (no row's label reaches STATS_LABEL_MIN)."""
    from . import osd
    S = layout.get("stats_scan")
    if not S:
        return {}
    w = layout["frame"][0]
    gy, gh = S["label_box"]

    def corr(t, n, x0, y0):
        if x0 < 0 or y0 < 0 or x0 + n > w or (y0 + gh) * w > len(buf):
            return -1.0
        v = _norm([float(buf[(y0 + j) * w + x0 + i]) for j in range(gh) for i in range(n)])
        return sum(a * b for a, b in zip(v, t)) / len(v)
    cw = layout["grid"]["cw"]

    def ink(x, y0):
        """How much the cell at (x, y0) reads as a capital of the font (the best correlation)."""
        if x + cw > w or not S.get("letters"):
            return 0.0
        v = [float(buf[(y0 + j) * w + x + i]) for j in range(gh) for i in range(cw)] if y0 >= 0 and (y0 + gh) * w <= len(buf) else []
        if not v or max(v) - min(v) < 1:
            return 0.0
        v = _norm(v)
        return max(sum(a * b for a, b in zip(v, t)) / len(v) for t in S["letters"])
    out = {}
    for key, t in S["labels"].items():
        n = len(t) // gh
        # every row at the grid's place with the analog jitter (osd.SHIFTS); the best row must also beat the next
        per = sorted(max((corr(t, n, S["label_x"] + dx, y + gy + dy), dx, dy) for dx, dy in osd.SHIFTS) + (y,) for y in S["rows"])[::-1]
        (s, dx, dy, y), runner = per[0], (per[1][0] if len(per) > 1 else -1.0)
        if s < STATS_LABEL_MIN or s - runner < STATS_LABEL_MARGIN:
            continue
        # a longer label that begins the same goes on in the next two cells; this one leaves them blank
        after = S["label_x"] + dx + len(STATS_LABELS[key]) * cw
        if max(ink(after + k * cw, y + gy + dy) for k in (0, 1)) >= STATS_INK:
            continue
        out[key] = y
    return out


def fit(bufs: list[bytes], profile: dict, reach: int = 12) -> dict:
    """The grid origin that reads the timers best on these frames: every (x, y) within `reach` px of the profile's,
    scored by the digit cells' best template correlation (coarse 2 px steps, then 1 px around the best). The frames
    should show the timers. {x, y, score, was: {x, y, score}}."""
    from . import osd
    g0 = derive(profile)["grid"]

    def score(x, y):
        try:
            lay = derive({**profile, "grid": {**g0, "x": x, "y": y}})
        except ValueError:                             # a grid that puts a timer's seconds past the edge reads nothing
            return -1.0
        w, g, tpl = lay["frame"][0], lay["glyph"], lay["tpl"]
        tot, n = 0.0, 0
        for buf in bufs:
            for row, f in enumerate(lay["formats"]):
                for k, ch in enumerate(f):
                    if ch != "d":
                        continue
                    feat = osd._glyph(buf, w, lay["x0"][row] + k * lay["pitch"], lay["rows"][row], g)
                    tot += max((sum(a * b for a, b in zip(feat, t)) / len(feat) for t in tpl.values()), default=0.0) if feat else 0.0
                    n += 1
        return tot / max(1, n)
    was = score(g0["x"], g0["y"])
    best = (was, g0["x"], g0["y"])
    for y in range(g0["y"] - reach, g0["y"] + reach + 1, 2):
        for x in range(g0["x"] - reach, g0["x"] + reach + 1, 2):
            s = score(x, y)
            if s > best[0]:
                best = (s, x, y)
    _, bx, by = best
    for y in range(by - 1, by + 2):
        for x in range(bx - 1, bx + 2):
            s = score(x, y)
            if s > best[0]:
                best = (s, x, y)
    return {"x": best[1], "y": best[2], "score": round(best[0], 3), "was": {"x": g0["x"], "y": g0["y"], "score": round(was, 3)}}


def rank_fonts(bufs: list[bytes], profile: dict, fonts: list[str] | None = None) -> list[dict]:
    """The Configurator fonts by how well their digits read the timers on these frames, best first:
    [{font, score, values}] (the mean of each digit cell's best correlation; the values read on the first frame)."""
    out = []
    for f in fonts or betaflight.FONTS:
        try:
            p = {**profile, "font": {"name": f, "data": betaflight.pack_font(betaflight.parse_mcm(betaflight.fetch_font(f)))}}
            lay = derive(p)
        except (ValueError, OSError) as e:
            out.append({"font": f, "score": None, "error": str(e)})
            continue
        scores, values = [], None
        for buf in bufs:
            pr = probe(buf, lay)
            values = values or pr["values"]
            scores += [c["score"] for c in pr["cells"] if c["kind"] == "timer" and c.get("digit") is not None]
        out.append({"font": f, "score": round(sum(scores) / len(scores), 3) if scores else 0.0, "values": values})
    return sorted(out, key=lambda r: -(r["score"] or 0))


def main() -> None:
    """A child process for the server (pure Python for seconds: never in a request thread). Reads {op, profile,
    path, times} as JSON on stdin, writes the answer as JSON ({error} when it cannot): op probe (one frame), fit (the
    grid), fonts (the ranking)."""
    try:
        req = json.load(sys.stdin)
        lay = derive(req["profile"])
        bufs = [_gray(req["path"], t, lay["frame"]) for t in req.get("times", [0.0])]
        if any(len(b) != lay["frame"][0] * lay["frame"][1] for b in bufs):
            raise ValueError("no frame at that time (past the clip's end?)")
        if req["op"] == "probe":
            out = {"layout": _public(lay), **probe(bufs[0], lay)}
        elif req["op"] == "fit":
            out = fit(bufs, req["profile"])
        elif req["op"] == "fonts":
            out = {"fonts": rank_fonts(bufs, req["profile"])}
        else:
            raise ValueError(f"no op {req['op']!r}")
    except (ValueError, KeyError, OSError) as e:
        out = {"error": str(e)}
    json.dump(out, sys.stdout)


def _public(layout: dict) -> dict:
    """A layout without its templates (what the page draws: the cells, not the vectors)."""
    out = {k: v for k, v in layout.items() if k not in ("tpl", "stats_scan")}
    if "stats_scan" in layout:
        out["stats_scan"] = {k: v for k, v in layout["stats_scan"].items() if k != "labels"}
    return out


if __name__ == "__main__":
    main()
