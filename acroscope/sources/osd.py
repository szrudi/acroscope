"""Read the OSD timers off the footage, and the arm runs they show.

A layout says where the timers are on the frame, what they count, and what their digits look like. Every layout is
derived from what Betaflight says (osdprofile, #10): a dump's OSD settings, the font on the OSD chip, and where the
DVR puts Betaflight's grid. The layouts flown on Rudi's quads ship with acroscope (osd-profiles.json, from each day's
dump; until 2026-10-23 they were measured by hand, with templates learned from labelled frames); a pilot's own are
stored in the database (PROFILE_SOURCE). layouts() is both, and a clip's layout is the one whose timers count on.

The flown ones: two timers bottom right, the arm timer (resets every arm) and the total armed time on the pack; the
oldest (AIR65 F until 2026-10-04) has the total alone, on the bottom row. Each digit cell is classified against
templates rendered from the font. No general OCR needed.

Reading the timers at 2 fps gives every arm start in video time: with two timers an arm is a run where the arm
timer counts up from 0; with the cumulative timer alone an arm is a stretch where the total counts (it stands
still while disarmed). Pairing those runs with a log's arms is the match phase's (match/: pairing, planner,
automatch).
"""
import json
import math
import subprocess
from pathlib import Path

from . import video


# A layout (osdprofile.derive): x0 per timer row and the pitch of the character cells, the y of each row, the format
# of each row ("dd:dd" = MM:SS, "dd:dd.d" = with tenths, "dd:dd.dd" = with hundredths; any other character is a cell
# to skip), the glyph box inside a cell (x, w, y, h), whether the one timer is the cumulative total, which row is the
# arm timer when there are two (arm_row), the digit templates (tpl), the STATS rows' labels (stats_scan), the frame
# size the grid is on.
SHIFTS = [(dx, dy) for dx in (-1, 0, 1) for dy in (-1, 0, 1)]   # the analog picture jitters a pixel or so
# The OSD shows a timer value about this long after the log reached it (OSD refresh, the analog link, the DVR):
# measured on 10-10 001 frame by frame against a yaw snap and a roll (#4, automatch 0.17-0.27 s late with the
# tenths timer), and consistent with the +0.2 s calibrated earlier on whole-second timers (where the floor of the
# seconds adds +0.5 on average: 0.5 - 0.25 = 0.25).
OSD_LAG = 0.25
OSD_VERSION = 9     # bump when a layout's geometry or the reading format changes: it keys the per-clip cache (9: the
                    # hand-measured layouts and the learned templates gone, every layout derived)
SHIPPED = Path(__file__).parent / "osd-profiles.json"
BLANK_STD = 25.0                                   # glyph-box contrast below this = no digit
STATS_SCORE = 0.6     # a STATS digit correlates 0.73-0.97 with its template; the % glyph and a frame without the
                      # STATS screen 0.25-0.5


# The pilot's own profiles: () -> (the stored profiles document, updated_at), set by the local CLI (cli.main) like
# tricks.LAYER_SOURCE, so this module never opens a database itself; tests set it explicitly.
PROFILE_SOURCE = None
SOURCE_EVERY = 1.0         # seconds between looks at PROFILE_SOURCE: every cell of a frame asks for its layout
RETRY_DERIVE = 60          # seconds before profiles that did not derive (a font not fetched yet) are tried again
_derived: dict = {"source": None, "checked": 0.0, "key": None, "at": 0.0, "layouts": {}, "errors": {}, "fp": "none", "names": ""}


def shipped() -> dict:
    """The profiles that ship with acroscope (osd-profiles.json): {name: profile}, fonts by name."""
    return json.loads(SHIPPED.read_text())["profiles"] if SHIPPED.exists() else {}


def builtin_names() -> list[str]:
    return sorted(shipped())


def layouts(fresh: bool = False) -> dict:
    """Every layout the reader knows, {name: layout} (the cache itself: not to be changed): the shipped profiles,
    then the pilot's (a name the shipped ones have is theirs). A profile that does not derive (its font could not be
    fetched, a stored one that is broken) is left out, named in derived_errors(), and tried again after RETRY_DERIVE
    s. The stored profiles are looked at once per SOURCE_EVERY s (`fresh`: now; the readers ask so at the start of a
    clip) and derived again only when the document changed, by its fingerprint (an updated_at has whole seconds: two
    saves within one would look alike): a frame's hundreds of cells do not each read the database.
    _templates_stamp() takes what these layouts came from, never a newer document."""
    import time
    from . import betaflight, osdprofile
    src, now = PROFILE_SOURCE, time.monotonic()
    if not fresh and _derived["source"] is src and _derived["key"] is not None and now - _derived["checked"] < SOURCE_EVERY:
        return _derived["layouts"]
    doc = src()[0] if src else None
    st = SHIPPED.stat() if SHIPPED.exists() else None
    key = (src, osdprofile.fingerprint(doc), st and (st.st_size, st.st_mtime_ns), betaflight.FONT_DIR)   # the source itself: a new one is new
    _derived.update(source=src, checked=now)
    if _derived["key"] != key or (_derived["errors"] and now - _derived["at"] > RETRY_DERIVE):
        out, errors = {}, {}
        for origin, profiles in (("shipped", shipped()), ("yours", (doc or {}).get("profiles") or {})):
            for name, p in profiles.items():
                if name in out:
                    continue
                try:
                    out[name] = {**osdprofile.derive(p, name), "origin": origin}
                except Exception as e:  # noqa: BLE001  (one broken profile is named, the others still read)
                    errors[name] = str(e) or type(e).__name__
        _derived.update(key=key, at=now, layouts=out, errors=errors, fp=key[1],
                        names=f"{st and (st.st_size, int(st.st_mtime))}|{','.join(sorted(out))}")
    return _derived["layouts"]


def derived_errors() -> dict:
    layouts()
    return dict(_derived["errors"])


def _layout(name) -> dict:
    L = layouts().get(name) if isinstance(name, str) else None
    if L is None:
        raise ValueError(f"{name}: no such OSD layout (a profile removed since this clip was read, or its font not to be had: "
                         f"{derived_errors().get(name, 'not a profile')})")
    return L


def _frame_w(layout: str) -> int:
    return _layout(layout).get("frame", [720])[0]


def _x0(layout: str, row: int) -> int:
    x = _layout(layout)["x0"]
    return x[row] if isinstance(x, list) else x


def _crop(layout: str) -> tuple[int, int, int, int]:
    """x, y, w, h of the strip holding every timer row of a layout (even height: ffmpeg pads odd crops), inside the
    frame: a timer on row 0 has its cell's top above the picture (the grid at y -4), one on the last row can end below
    it, and ffmpeg would move such a crop into the frame without saying so; _cell measures from the strip's real
    top."""
    L = _layout(layout)
    gx, gw, gy, gh = L["glyph"]
    left = max(0, min(_x0(layout, r) for r in range(len(L["formats"]))))
    top = max(0, L["rows"][0])
    right = max(_x0(layout, r) + len(f) * L["pitch"] + 2 for r, f in enumerate(L["formats"]))
    fh = L.get("frame", [720, 480])[1]
    bottom = min(L["rows"][-1] + gy + gh + 2, fh)       # nor below it: ffmpeg would move a crop past the bottom up
    h = bottom - top
    w = min(right, _frame_w(layout)) - left
    return left, top, w + w % 2, h + h % 2 if top + h + h % 2 <= fh else h - h % 2


def digit_cols(layout: str, row: int) -> list[int]:
    return [i for i, ch in enumerate(_layout(layout)["formats"][row]) if ch == "d"]


def _frames(path: Path, fps: float = 2.0, start: float = 0.0, end: float | None = None, layout: str = "otto-bold-v3"):
    """Yield (t, bytes) gray crops of the timer area at `fps`."""
    x, y, w, h = _crop(layout)
    cmd = ["ffmpeg", "-v", "error", "-nostdin"]
    if start:
        cmd += ["-ss", f"{start:.3f}"]
    cmd += ["-i", str(path)]
    if end is not None:
        cmd += ["-t", f"{end - start:.3f}"]
    fr = _layout(layout).get("frame")          # the frame scaled to the size its grid is on (a DVR that records smaller)
    cmd += ["-vf", (f"scale={fr[0]}:{fr[1]}," if fr else "") + f"fps={fps},crop={w}:{h}:{x}:{y}", "-pix_fmt", "gray", "-f", "rawvideo", "-"]
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE)
    n = w * h
    i = 0
    while True:
        buf = p.stdout.read(n)
        if len(buf) < n:
            break
        yield start + i / fps, buf
        i += 1
    p.wait()


def _cell(buf: bytes, col: int, row: int, layout: str = "otto-bold-v3", dx: int = 0, dy: int = 0) -> list[float]:
    """Normalised pixel vector of one digit glyph (the layout's glyph box), shifted by (dx, dy); [] when blank."""
    L = _layout(layout)
    left, top, w, _ = _crop(layout)
    return _glyph(buf, w, (_x0(layout, row) - left) + col * L["pitch"] + dx, (L["rows"][row] - top) + dy, L["glyph"])


def _glyph(buf: bytes, w: int, x: int, y: int, glyph: tuple) -> list[float]:
    """Normalised pixel vector of the glyph box of the cell at (x, y) in a gray image `w` wide; [] when blank."""
    gx, gw, gy, gh = glyph
    cx, cy = x + gx, y + gy
    if cx < 0 or cy < 0 or cx + gw > w or (cy + gh) * w > len(buf):      # a negative row would wrap to the bottom
        return []
    v = [float(buf[(cy + j) * w + cx + i]) for j in range(gh) for i in range(gw)]
    m = sum(v) / len(v)
    sd = math.sqrt(sum((a - m) ** 2 for a in v) / len(v))
    if sd < BLANK_STD:
        return []
    return [(a - m) / sd for a in v]


def _templates_stamp() -> str:
    """What the readings depend on besides the clip: the shipped profiles, the pilot's and which of them derive (a
    font fetched since), as the reader's layouts were derived from them (layouts()' own record, not the store's
    newest document: a reading is filed under the layouts it was made with). Any change means new readings, in every
    cache keyed on it."""
    import hashlib
    layouts()
    return hashlib.sha1(f"{_derived['names']}|{_derived['fp']}".encode()).hexdigest()[:12]


def _load_templates(layout: str) -> dict[str, list[float]]:
    """A layout's digit templates, rendered from its font when it was derived."""
    return _layout(layout)["tpl"]


def _classify(buf: bytes, col: int, row: int, layout: str, templates: dict) -> tuple[str | None, float]:
    """(digit, margin over the runner-up) for one cell: each template's best correlation over the SHIFTS."""
    if not templates:
        return None, 0.0
    best: dict[str, float] = {}
    blank = True
    for dx, dy in SHIFTS:
        feat = _cell(buf, col, row, layout, dx, dy)
        if not feat:
            continue
        blank = False
        for d, t in templates.items():
            s = sum(a * b for a, b in zip(feat, t)) / len(feat)
            if s > best.get(d, -9.0):
                best[d] = s
    if blank:
        return None, 0.0
    scores = sorted(((s, d) for d, s in best.items()), reverse=True)
    top, second = scores[0], scores[1] if len(scores) > 1 else (0.0, "")
    return top[1], top[0] - second[0]


def _score_at(buf: bytes, w: int, x: int, y: int, glyph: tuple, templates: dict) -> tuple[str | None, float]:
    """(digit, correlation) of the cell at (x, y), the best over the SHIFTS; (None, 0) when blank."""
    best = (None, 0.0)
    for dx, dy in SHIFTS:
        feat = _glyph(buf, w, x + dx, y + dy, glyph)
        for d, t in (templates.items() if feat else ()):
            sc = sum(a * b for a, b in zip(feat, t)) / len(feat)
            if sc > best[1]:
                best = (d, sc)
    return best


def stats_value(cells: list[tuple[str | None, float]], kind: str) -> float | None:
    """A STATS value from its cells' (digit, score): "mmss" = the four digits of MM:SS (seconds), "pct" = up to
    three digits followed by the % glyph (which scores low). None when it does not read as one."""
    ok = [d is not None and sc >= STATS_SCORE for d, sc in cells]
    if kind == "mmss":
        if not all(ok) or int(cells[0][0]) > 5 or int(cells[2][0]) > 5:     # MM:SS
            return None
        return int(cells[0][0] + cells[1][0]) * 60 + int(cells[2][0] + cells[3][0])
    n = ok.index(False) if False in ok else len(ok)
    if n == 0:
        return None
    v = int("".join(d for d, _ in cells[:n]))
    return v if v <= 100 else None


def read_stats_frame(buf: bytes, layout, templates: dict, w: int = 720) -> dict:
    """{total_arm, blackbox} off a full gray frame (the layout's frame size); both None when the frame shows no STATS
    screen. Its rows are found by their labels (osdprofile.stats_rows): which stats are on, and so where Betaflight
    centres them, is not counted."""
    from . import osdprofile
    L = layout if isinstance(layout, dict) else _layout(layout)
    g = L["glyph"]
    rows = osdprofile.stats_rows(buf, L)
    pitch = L.get("pitch", 24)
    for S in ([{"x0": L["stats_scan"]["x0"], **rows}] if len(rows) == 2 else []):
        ta = stats_value([_score_at(buf, w, S["x0"] + pitch * c, S["total_arm"], g, templates) for c in (0, 1, 3, 4)], "mmss")
        bb = stats_value([_score_at(buf, w, S["x0"] + pitch * c, S["blackbox"], g, templates) for c in (0, 1, 2)], "pct") if ta is not None else None
        if ta is not None and bb is not None:
            return {"total_arm": ta, "blackbox": bb}
    return {"total_arm": None, "blackbox": None}


def _gray_frame(path: Path, t: float, size=(720, 480)) -> bytes:
    cmd = ["ffmpeg", "-v", "error", "-nostdin", "-ss", f"{max(t, 0):.3f}", "-i", str(path), "-frames:v", "1",
           "-vf", f"scale={size[0]}:{size[1]}", "-pix_fmt", "gray", "-f", "rawvideo", "-"]
    return subprocess.run(cmd, capture_output=True).stdout


def read_stats(path: Path, runs: list[dict], layout: str) -> list[dict]:
    """The STATS screen after each run's disarm: [{total_arm, blackbox}] per run (None where not read). Sampled at
    1-4 s after the run's last reading (the screen stays up until the sticks move); the most common full reading
    wins. Cached per clip and run end."""
    L = _layout(layout)
    if not L.get("stats_scan"):
        return [{} for _ in runs]
    st = path.stat()
    cp = video.cache("osd", path.parent.name, f"{path.name}.{st.st_size}.{int(st.st_mtime)}.stats.v{OSD_VERSION}-{_templates_stamp()}.json")
    cache = json.loads(cp.read_text()) if cp.exists() else {}
    tpl = _load_templates(layout)
    out = []
    for v in runs:
        key = f"{layout}@{v['end']}"
        if key not in cache:
            size = L.get("frame", [720, 480])
            reads = [read_stats_frame(_gray_frame(path, v["end"] + dt, size), layout, tpl, size[0]) for dt in (1.0, 2.0, 3.0, 4.0)]
            full = [(r["total_arm"], r["blackbox"]) for r in reads if r["total_arm"] is not None and r["blackbox"] is not None]
            ta, bb = max(full, key=full.count) if full else (None, None)
            cache[key] = {"total_arm": ta, "blackbox": bb}
        out.append(cache[key])
    cp.write_text(json.dumps(cache))
    return out


def _value(digits: list) -> float | None:
    """Seconds from the digits of a "dd:dd" or "dd:dd.d" row; None when a digit is unreadable or impossible, or when
    there are not the four of MM:SS."""
    if len(digits) < 4 or any(d is None for d in digits):
        return None
    m, sec = digits[0] + digits[1], digits[2] + digits[3]
    if int(sec[0]) > 5:                   # MM:SS, so the tens of seconds is 0-5
        return None
    v = int(m) * 60 + int(sec)
    if len(digits) > 4:
        v += int(digits[4]) / 10
    if len(digits) > 5:
        v += int(digits[5]) / 100
    return v


def read_frame(buf: bytes, templates: dict, layout: str = "otto-bold-v3") -> tuple[float | None, float | None, float]:
    """(arm timer seconds, total seconds, min confidence) from one crop; None when a timer is not readable. With
    the single cumulative timer the arm timer is always None. Which row is which is the layout's `arm_row`."""
    L = _layout(layout)
    out = []
    conf = 1.0
    for row in range(len(L["formats"])):
        digits = []
        for col in digit_cols(layout, row):
            d, c = _classify(buf, col, row, layout, templates)
            digits.append(d)
            if d is not None:
                conf = min(conf, c)
        out.append(_value(digits))
    if L["cumulative"]:
        return None, out[0], conf
    if len(out) == 1:                      # an arm timer and no total (a derived layout can be that)
        return out[0], None, conf
    a = L.get("arm_row", 0)
    return out[a], out[1 - a], conf


MIN_COUNTING = 3                   # detect_layout: fewer counting pairs at 1 s/s than this, and other rates are tried
RATES = (0.5, 0.75, 1.25, 1.5, 1.75, 2.0, 2.25, 2.5, 2.75, 3.0)


def detect_layout(path: Path, fps: float = 0.5) -> str:
    """The layout whose readings make sense: sampled at `fps` over the whole clip, score a layout by the pairs of
    consecutive readings that count on at 1 s/s. Wrong cells read like digits too, and static text reads the same
    wrong digits every time, so standing still does not count; only counting does. A layout with two timers scores
    only the pairs where both count together (twice each, as two timers): while armed they always do, so a layout
    that reads one of another layout's rows as its own and nothing in its other row scores nothing, however well it
    reads that one row (2026-10-10 001: the v3 profile read the arm timer on row 11 as its total, in Impact more
    cleanly than the learned otto-bold templates, and won with a dead second row)."""
    step = 1 / fps
    layouts(fresh=True)

    def steps(vals, rate=1.0):
        s, tol = step * rate, 0.6 * max(1.0, rate)
        return [a is not None and b is not None and s - tol <= b - a <= s + tol for a, b in zip(vals, vals[1:])]
    if not layouts():
        raise ValueError("no OSD layout to read with: " + ("; ".join(f"{n}: {e}" for n, e in derived_errors().items()) or "no profiles"))
    reads = {}
    for name in layouts():
        tpl = _load_templates(name)
        if tpl:
            reads[name] = [read_frame(buf, tpl, name) for _, buf in _frames(path, fps, layout=name)]

    def score(name, rate):
        arm, total = steps([r[0] for r in reads[name]], rate), steps([r[1] for r in reads[name]], rate)
        L = layouts().get(name) or {}
        if len(L.get("formats", [])) == 2 and not L.get("cumulative"):
            return 2 * sum(1 for a, t in zip(arm, total) if a and t)
        return sum(arm) + sum(total)                     # one timer: the other reads None throughout
    # a tie goes to the shipped layout (then by name): a pilot's profile that reads a clip as well as the one it
    # was read with so far changes nothing about that clip
    rank = lambda n: (layouts()[n].get("origin") == "shipped", n)
    best = max(((score(n, 1.0), rank(n), n) for n in reads))[::2]
    if best[0] < MIN_COUNTING:
        # nothing counts on at 1 s/s: a clip whose clock runs off the OSD's (2026-10-14 007, a 2.2x mis-stamped frame
        # rate, where the right layout won 2 to 0 by chance); the layout counting at its own steady rate wins
        best = max(((max(score(n, r) for r in RATES), rank(n), n) for n in reads))[::2]
    # nothing counts at all (no OSD in the clip): the oldest two-timer layout, as before, if it is there
    return best[1] if best[0] > 0 or "otto" not in reads else "otto"


def _timers_cache(path: Path, fps: float, st=None) -> Path:
    st = st or path.stat()
    return video.cache("osd", path.parent.name, f"{path.name}.{st.st_size}.{int(st.st_mtime)}.{fps:g}.v{OSD_VERSION}-{_templates_stamp()}.json")


def carry_reading(was: Path, now: Path, fps: float = 2.0) -> None:
    """A clip moved from `was` to `now` unchanged (the move keeps size and mtime): its timer reading goes with it,
    so automatch does not read the OSD again (ingest reads it on the encode for the clock)."""
    st = now.stat()
    src, dst = _timers_cache(was, fps, st), _timers_cache(now, fps, st)
    if src.exists() and not dst.exists():
        dst.write_text(src.read_text())


def read_timers(path: Path, fps: float = 2.0, layout: str | None = None) -> dict:
    """Read the timers through the clip: {layout, rows: [{t, top, bottom, conf}]} (cached per clip)."""
    layouts(fresh=True)                    # the profiles as stored now, for the whole clip and the cache's stamp alike
    cp = _timers_cache(path, fps)
    if cp.exists():
        return json.loads(cp.read_text())
    layout = layout or detect_layout(path)
    tpl = _load_templates(layout)
    rows = []
    for t, buf in _frames(path, fps, layout=layout):
        top, bot, conf = read_frame(buf, tpl, layout)
        rows.append({"t": round(t, 2), "top": top, "bottom": bot, "conf": round(conf, 3)})
    out = {"layout": layout, "rows": rows}
    cp.write_text(json.dumps(out))
    return out


def _clusters(pts: list[tuple], spread: float = 1.5) -> list[list[tuple]]:
    """Group points sorted by key: a cluster's keys stay within `spread` of its first. A real arm's readings all
    imply the same start (the OSD floors seconds, so ±0.5 s); a frozen or misread timer drifts away."""
    out, cur = [], []
    for p in pts:
        if cur and p[0] - cur[0][0] > spread:
            out.append(cur)
            cur = []
        cur.append(p)
    if cur:
        out.append(cur)
    return out


def _drop_ghosts(arms: list[dict]) -> list[dict]:
    """A run overlapping in time with a run that has more readings is a ghost of it (the same arm read with a digit
    wrong gives a second, consistent key); two real arms never overlap."""
    kept = []
    for a in sorted(arms, key=lambda a: -a["readings"]):
        if any(a["start"] < k["end"] and a["end"] > k["start"] for k in kept):
            continue
        kept.append(a)
    kept.sort(key=lambda a: a["start"])
    return kept


def arms_from_timers(rows: list[dict], fps: float = 2.0, min_readings: int = 3) -> list[dict]:
    """Arms from the two-timer readings: every reading of the top timer implies a start time (t - timer), so the
    readings of one arm cluster at the same start while misread digits scatter. [{start, end, length, total_before}]
    in video seconds. The OSD shows floor(seconds); the OSD-implied start sits ~0.2 s before the real one.
    Three readings at 2 fps keeps the 2 s hops of an indoor session; the odd run of garbage read off static or
    the STATS screen that survives is a second or so long and matches no arm, so the aligner skips it."""
    # a reading of 0 (the arm's first second) says little about the start and biases the key: left out, as calibrated
    pts = sorted((r["t"] - r["top"], r["t"], r["top"], r["bottom"]) for r in rows if r["top"] and r["conf"] >= 0.01)
    arms = []
    for c in _clusters(pts):
        vs = [p[2] for p in c]
        c.sort(key=lambda p: p[1])
        # a frozen timer (the last arm's time shown while disarmed) drifts and is cut into pieces by the cluster
        # spread; each piece shows one value for more than a second, which a real arm's readings never do
        if len(c) < min_readings or (max(vs) == min(vs) and c[-1][1] - c[0][1] > 1.2):
            continue
        keys = sorted(p[0] for p in c)
        # the timer shows the floor of its last digit (half a unit early on average), and the picture shows it
        # OSD_LAG after the log did
        tenths = any(p[2] != int(p[2]) for p in c)
        start = keys[len(keys) // 2] + (0.05 if tenths else 0.5) - OSD_LAG
        vmax = max(vs)
        tb = [p[3] - p[2] for p in c if p[3] is not None]
        arms.append({"start": round(start, 2), "end": round(c[-1][1], 2), "length": round(vmax + (0.05 if tenths else 0.5), 2), "timer_max": vmax,
                     "total_before": sorted(tb)[len(tb) // 2] if tb else None, "readings": len(c)})
    return _drop_ghosts(arms)


def arms_from_cumulative(rows: list[dict], min_readings: int = 3) -> list[dict]:
    """Arms from a single cumulative timer: while armed the total counts at 1/s, so every reading implies the
    same pack start (t - total); while disarmed the total stands still and that key drifts. Readings with one key
    (within 1.5 s) whose total moved are an arm: its length is the total's span, total_before its first value,
    and its start the video time at which the total showed that first value."""
    pts = sorted((r["t"] - r["bottom"], r["t"], r["bottom"]) for r in rows if r["bottom"] is not None and r["conf"] >= 0.01)
    arms = []
    for c in _clusters(pts):
        vs = [p[2] for p in c]
        v0, v1 = min(vs), max(vs)
        if len(c) < min_readings or v1 - v0 < 1:          # a disarmed stretch, or noise
            continue
        keys = sorted(p[0] for p in c)
        key = keys[len(keys) // 2]
        t0 = min(p[1] for p in c)
        arms.append({"start": round(key + v0 + 0.5 - OSD_LAG, 2), "end": round(max(p[1] for p in c), 2), "length": round(v1 - v0 + 0.5, 2),
                     "timer_max": v1 - v0, "total_before": v0, "readings": len(c),
                     # the clip starts inside this arm: its length is only what the clip shows, the start is unknown
                     "partial_start": t0 < 1.5})
    return _drop_ghosts(arms)


def video_arms(rt: dict, fps: float = 2.0) -> list[dict]:
    """The arm runs of a read_timers() result, by its layout (each method has its own minimum of readings)."""
    if _layout(rt["layout"])["cumulative"]:
        return arms_from_cumulative(rt["rows"])
    return arms_from_timers(rt["rows"], fps)
