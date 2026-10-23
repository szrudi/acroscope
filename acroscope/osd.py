"""Read the OSD timers off the footage and match clips to arms automatically.

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
still while disarmed). Those runs are aligned to the log's arm lengths (and the total against the boot's
cumulative length), and each aligned pair yields a match with its own offset, which also carries across the
static stretches cobra-compress.py cuts out.
"""
import json
import math
import re
import subprocess
from pathlib import Path

from . import blackbox, sessions, video
from .config import VIDEOS_DIR

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
# The two clocks (DVR and FC) differ by a few tenths of a percent (#4: 0.3 %). A boot's line beyond this is pairs
# that are not one boot in one clip (duration-only matches on the cumulative timer, #9), not a clock.
MAX_CLOCK = 0.01
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
    frame: a timer on row 0 has its cell's top above the picture (the grid at y -4), and ffmpeg would move a crop
    there down to 0 without saying so; _cell measures from the strip's real top."""
    L = _layout(layout)
    gx, gw, gy, gh = L["glyph"]
    left = max(0, min(_x0(layout, r) for r in range(len(L["formats"]))))
    top = max(0, L["rows"][0])
    right = max(_x0(layout, r) + len(f) * L["pitch"] + 2 for r, f in enumerate(L["formats"]))
    h = L["rows"][-1] + gy + gh + 2 - top
    w = min(right, _frame_w(layout)) - left
    return left, top, w + w % 2, h + h % 2


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


def _elapsed(v: dict, l: dict) -> float | None:
    """For a run the clip starts inside: how far into log arm `l` the clip begins, from the totals (None if unknown)."""
    if v.get("partial_start") and v.get("total_before") is not None and l.get("cum_before") is not None:
        return v["total_before"] - l["cum_before"]
    return None


def _may(v: dict, l: dict) -> bool:
    """Whether run v may be log arm l at all: not after the flash filled (no_log), and the run at which it filled
    only the log's cut-off last arm (only_arm)."""
    return not v.get("no_log") and v.get("only_arm", l["index"]) == l["index"]


def align(video_arms: list[dict], log_arms: list[dict], tol: float = 2.0, w_total: float = 0.3) -> list[tuple[int, int]]:
    """Order-preserving alignment of detected arms to log arms. Cost of a pair = |length difference| +
    w_total * |OSD total-before - log cumulative length in the boot| (when both known); skipping either side costs tol.
    A run the clip starts inside (partial_start) is compared against what is left of the log arm after the point
    the totals say the clip begins. Both lists are in time order. Returns (video index, log index) pairs."""
    n, m = len(video_arms), len(log_arms)
    INF = float("inf")
    cost = [[INF] * (m + 1) for _ in range(n + 1)]
    back = [[None] * (m + 1) for _ in range(n + 1)]
    cost[0][0] = 0.0
    for i in range(n + 1):
        for j in range(m + 1):
            c = cost[i][j]
            if c == INF:
                continue
            if i < n and j < m:
                v, l = video_arms[i], log_arms[j]
                e = _elapsed(v, l)
                if e is not None:
                    d = abs(v["length"] - (l["length"] - e)) if e >= -1 else INF
                else:
                    d = abs(v["length"] - l["length"])
                    if v.get("total_before") is not None and l.get("cum_before") is not None:
                        d += w_total * abs(v["total_before"] - l["cum_before"])
                if d <= 2 * tol and _may(v, l) and c + d < cost[i + 1][j + 1]:
                    cost[i + 1][j + 1], back[i + 1][j + 1] = c + d, ("m", i, j)
            if i < n and c + tol < cost[i + 1][j]:
                cost[i + 1][j], back[i + 1][j] = c + tol, ("v", i, j)
            if j < m and c + tol < cost[i][j + 1]:
                cost[i][j + 1], back[i][j + 1] = c + tol, ("l", i, j)
    pairs, i, j = [], n, m
    while back[i][j]:
        kind, pi, pj = back[i][j]
        if kind == "m":
            pairs.append((pi, pj))
        i, j = pi, pj
    return pairs[::-1]


def consistent_pairs(video_arms: list[dict], log_arms: list[dict], pairs: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Drop pairs that break the one thing a boot guarantees: the log's time field is the FC uptime, so within a
    boot `video start - uptime start` is the boot's offset, and it can only go DOWN along the boot (the compressor
    cuts video out, never adds any). A stray run that pairs with a short arm far from where the boot puts it
    makes that offset jump up; of two such pairs the one with fewer readings goes."""
    kept: list[tuple[int, int]] = []
    last: dict = {}                                   # boot -> (boot offset, index into kept)
    for vi, li in pairs:
        v, l = video_arms[vi], log_arms[li]
        if l.get("boot") is None or l.get("uptime_start") is None:
            kept.append((vi, li))
            continue
        off = v["start"] - l["uptime_start"]
        prev = last.get(l["boot"])
        if prev and off > prev[0] + 2.0:          # the starts read off a whole-second timer jitter by about a second
            pv = video_arms[kept[prev[1]][0]]
            if v["readings"] <= pv["readings"]:
                continue                              # the newcomer is the stray
            kept.pop(prev[1])                         # the earlier pair was the stray
            last = {b: (o, k if k < prev[1] else k - 1) for b, (o, k) in last.items() if k != prev[1]}
        kept.append((vi, li))
        last[l["boot"]] = (off, len(kept) - 1)
    return kept


def fit_boots(video_arms: list[dict], log_arms: list[dict], pairs: list[tuple[int, int]]) -> dict:
    """Per boot, video time as a line in the FC uptime: video = a * uptime + b, from the pairs. The two clocks
    differ by a few tenths of a percent (#4: about 0.3 % on the Cobra SD against this FC), half a second over a
    four-minute clip, so one offset per boot is not enough. Least squares when three or more pairs span a minute;
    the median offset with a = 1 otherwise. {boot: {a, b, n}}, with rejected=True when no clock explains the pairs
    (a line beyond MAX_CLOCK, or offsets further apart than the timer jitter and MAX_CLOCK allow): such a fit
    places and derives nothing."""
    pts: dict = {}
    for vi, li in pairs:
        l = log_arms[li]
        if l.get("boot") is not None and l.get("uptime_start") is not None:
            pts.setdefault(l["boot"], []).append((l["uptime_start"], video_arms[vi]["start"]))
    out = {}
    for b, xy in pts.items():
        xs, ys = [x for x, _ in xy], [y for _, y in xy]
        if len(xy) >= 3 and max(xs) - min(xs) >= 60:
            mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
            sxx = sum((x - mx) ** 2 for x in xs)
            a = sum((x - mx) * (y - my) for x, y in xy) / sxx
            out[b] = {"a": round(a, 5), "b": round(my - a * mx, 3), "n": len(xy)}
            if abs(a - 1) > MAX_CLOCK:
                out[b]["rejected"] = True
        else:
            offs = sorted(y - x for x, y in xy)
            out[b] = {"a": 1.0, "b": round(offs[len(offs) // 2], 3), "n": len(xy)}
            if offs[-1] - offs[0] > 2.0 + MAX_CLOCK * (max(xs) - min(xs)):     # whole-second timers jitter by a second
                out[b]["rejected"] = True
    return out


def _at(fit: dict, uptime: float) -> float:
    return fit["a"] * uptime + fit["b"]


def place_by_boot(video_arms: list[dict], log_arms: list[dict], pairs: list[tuple[int, int]], tol: float = 2.0) -> list[tuple[int, int]]:
    """Second pass: the pairs give each boot's line (fit_boots), which puts every other arm of the boot at a known
    place in the video. An unmatched run whose start sits within `tol` of exactly one unmatched arm's place is that
    arm, however long the run reads: a run's length is only what the timer showed, and the end of an arm is often
    unreadable (a crash, a broken-up picture, the STATS screen)."""
    fits = {b: f for b, f in fit_boots(video_arms, log_arms, pairs).items() if not f.get("rejected")}
    used_v, used_l = {vi for vi, _ in pairs}, {li for _, li in pairs}
    out = list(pairs)
    for vi, v in enumerate(video_arms):
        if vi in used_v or v.get("partial_start"):
            continue
        hits = [li for li, l in enumerate(log_arms) if li not in used_l and l.get("boot") in fits and _may(v, l)
                and abs(v["start"] - _at(fits[l["boot"]], l["uptime_start"])) <= tol and v["length"] <= l["length"] + tol]
        if len(hits) == 1:
            out.append((vi, hits[0]))
            used_v.add(vi); used_l.add(hits[0])
    out.sort()
    return out


def derive_unread(log_arms: list[dict], pairs: list[tuple[int, int]], fits: dict, duration: float | None) -> list[tuple[int, float]]:
    """Arms of a fitted boot that no run was read for (too short for the reader, or unreadable), placed by the
    boot's line when they fall inside the clip: [(log index, video start)]. Marked as derived in the match note."""
    used_l = {li for _, li in pairs}
    out = []
    for li, l in enumerate(log_arms):
        if li in used_l or l.get("boot") not in fits or l.get("uptime_start") is None:
            continue
        f = fits[l["boot"]]
        if f["n"] < 2 or f.get("rejected"):
            continue
        start = _at(f, l["uptime_start"])
        if start < -0.5 or (duration is not None and start + l["length"] > duration + 0.5):
            continue
        out.append((li, round(start, 2)))
    return out


def in_order(video_arms: list[dict], log_arms: list[dict], pairs: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """The pairs that keep a clip's arms in the log's order: a clip is one stretch of time, so a later run is a
    later arm. The alignment preserves order, but placing by a boot's line and partial runs need not. Keeps the
    order-preserving subset with the most readings (a longest increasing subsequence, weighted)."""
    ps = sorted(pairs, key=lambda p: (video_arms[p[0]]["start"], p[1]))
    best, prev = [], []
    for k, (vi, li) in enumerate(ps):
        w = video_arms[vi].get("readings", 1)
        cands = [(best[j], j) for j in range(k) if log_arms[ps[j][1]]["index"] < log_arms[li]["index"]]
        b, j = max(cands, default=(0, None))
        best.append(b + w)
        prev.append(j)
    if not ps:
        return []
    k = max(range(len(ps)), key=lambda i: best[i])
    out = []
    while k is not None:
        out.append(ps[k])
        k = prev[k]
    return sorted(out)


def clip_order(name: str | None) -> str | None:
    """A clip's place in time: its readable name `<date>_NNN` sorts chronologically as a string. Anything else (a
    clip without a name falls back to its file name, an id) has no known place: None, and the order rules skip it."""
    return name if name and re.fullmatch(r"\d{4}-\d{2}-\d{2}_\d+", name) else None


def arm_window(key: str | None, others: list[tuple[str, int]]) -> tuple[int | None, int | None]:
    """Clips are chronological (their readable names sort by date and number) and so are a log's arms: a clip's
    arms come after every arm matched to an earlier clip and before every arm matched to a later one. `others`:
    (clip name, arm) of the log's matches in the other clips. (lo, hi): the arms must lie strictly between. A clip
    or a match without a place in time (clip_order) bounds nothing."""
    key = clip_order(key)
    if key is None:
        return None, None
    lo = max((a for k, a in others if clip_order(k) and k < key), default=None)
    hi = min((a for k, a in others if clip_order(k) and k > key), default=None)
    return lo, hi


PLAN_TOL = 2.0                     # session plan: seconds a run or an arm left without a partner costs
PLAN_MARGIN = 1.0                  # a pick whose best alternative costs less than this more is ambiguous: nothing written
PLAN_W_TOTAL = 0.3                 # weight of the OSD's total-before against the arm's place in its boot (as align())
PLAN_SLACK = 1                     # a clip's window reaches this many arms into its neighbours' (the clip's own pass,
                                   # with its boot fit, settles a boundary the lengths put one arm off: 2026-10-11 004)


def _pair_cost(r: dict, a: dict) -> float:
    """A run against an arm: the length misfit, plus (on a cumulative-timer OSD, which shows the boot's flying time
    before the run) the misfit of that total against the arm's place in its boot, as align() weighs it."""
    c = abs(r["length"] - a["length"])
    if r.get("total_before") is not None and a.get("cum_before") is not None:
        c += PLAN_W_TOTAL * abs(r["total_before"] - a["cum_before"])
    return c


def _plan_cost(runs: list[dict], arms: list[dict], forbid: tuple[int, int] | None = None, tol: float = PLAN_TOL):
    """Order-preserving alignment of every run of a session's clips (card order) to its arms (log order): a pair
    costs _pair_cost, a run or an arm without a partner costs `tol`. (cost, pairs)."""
    n, m = len(runs), len(arms)
    INF = float("inf")
    D = [[0.0] * (m + 1) for _ in range(n + 1)]
    for i in range(1, n + 1):
        D[i][0] = i * tol
    for j in range(1, m + 1):
        D[0][j] = j * tol
    for i in range(1, n + 1):
        for j in range(1, m + 1):
            pair = INF if forbid == (i - 1, j - 1) else D[i - 1][j - 1] + _pair_cost(runs[i - 1], arms[j - 1])
            D[i][j] = min(pair, D[i - 1][j] + tol, D[i][j - 1] + tol)
    pairs, i, j = [], n, m
    while i and j:
        if forbid != (i - 1, j - 1) and D[i][j] == D[i - 1][j - 1] + _pair_cost(runs[i - 1], arms[j - 1]):
            pairs.append((i - 1, j - 1)); i -= 1; j -= 1
        elif D[i][j] == D[i - 1][j] + tol:
            i -= 1
        else:
            j -= 1
    return D[n][m], pairs[::-1]


def plan_session_pairs(runs: list[dict], arms: list[dict], margin: float = PLAN_MARGIN) -> tuple[list[tuple[int, int]], set]:
    """The session's runs (each with its `clip`) against its arms in one go (#26): clip by clip, one locally best pick
    (2026-10-14 009 took arm 17, 0.7 s nearer than arm 7) bounded every later clip out of its arms. Returns (pairs,
    ambiguous clips), the ambiguous clips' pairs left out. A runner-up within `margin` makes clips ambiguous when it
    gives another clip than the one whose pair it drops other arms (two clips moving together count, #41): then where
    their windows lie is a coin toss. A close runner-up inside one clip (a dozen short hops of near equal length,
    2026-10-16) changes no other clip; which run is which arm is the clip's own pass to settle."""
    cost, pairs = _plan_cost(runs, arms)

    def arms_of(ps):
        out = {}
        for ri, ai in ps:
            out.setdefault(runs[ri].get("clip"), set()).add(ai)
        return out
    best = arms_of(pairs)
    ambiguous = set()
    for ri, ai in pairs:
        alt_cost, alt = _plan_cost(runs, arms, forbid=(ri, ai))
        if alt_cost - cost >= margin:
            continue
        other = arms_of(alt)
        changed = {c for c in set(best) | set(other) if best.get(c, set()) != other.get(c, set())}
        if changed - {runs[ri].get("clip")}:
            ambiguous |= changed
    return [(ri, ai) for ri, ai in pairs if runs[ri].get("clip") not in ambiguous], ambiguous


def _log_order(bbl: str) -> str:
    """A log's place in time: the dump's date and time in its name (BTFL_..._YYYYMMDD_HHMMSS_...), else the name."""
    m = re.search(r"_(\d{8})_(\d{6})_", bbl)
    return m.group(1) + m.group(2) if m else bbl


def _craft(bbl: str) -> str:
    """The quad a log is from: the craft name in BTFL_BLACKBOX_LOG_<craft>_<date>_<time>_..., else the file itself."""
    m = re.match(r"BTFL_BLACKBOX_LOG_(.+?)_\d{8}_\d{6}_", bbl)
    return m.group(1) if m else bbl


def session_plan(session: str, fps: float = 2.0) -> dict:
    """{clip file: {"windows": {bbl: (lo, hi)}, "planned": [(bbl, arm)], "ambiguous": bool}} for every named,
    probed, present clip: the arms each clip may hold, from one alignment of the whole session (clips in card order,
    a quad's logs' arms in dump order) instead of from the picks of the clips before it. One alignment per quad (the
    craft in the log's name): two quads flown in turn and dumped at the end interleave in the clips, not end to end
    (#41). Matches already in the session are fixed points (the clips between two of them align against the arms
    between); arms another session holds, or a clip of this one the plan leaves out (a missing one), are left out."""
    s = sessions.load(session)
    clips = sorted((v for v in s["videos"] if clip_order(v.get("name")) and v.get("duration") and not v.get("missing_since")),
                   key=lambda v: v["name"])
    if not clips:
        return {}
    files = {v["file"] for v in clips}
    mine = {v.get("name") for v in s["videos"]} | {v["file"] for v in s["videos"]}
    other = {b: [(k, a) for k, a in ms if k not in mine] for b, ms in _log_matches(s, None, []).items()}
    elsewhere = {(b, a) for b, ms in other.items() for _, a in ms}
    elsewhere |= {(m["bbl"], m["arm"]) for m in s["matches"] if m["video"] not in files}   # a missing clip keeps its arm
    # a day's clips are numbered across all its sessions, so another session's matches bound this one's arms too: an
    # arm held by an earlier-named clip comes before them, one held by a later-named clip after (2026-10-02: a log
    # holding the day's first session's arms too)
    first, last = clips[0]["name"], clips[-1]["name"]
    bound = {b: (max((a for k, a in ms if clip_order(k) and k < first), default=None),
                 min((a for k, a in ms if clip_order(k) and k > last), default=None)) for b, ms in other.items()}
    chains: dict[str, list[dict]] = {}
    for b in sorted(s["blackbox"], key=_log_order):
        try:
            idx = blackbox.index_bbl(b)
        except FileNotFoundError:
            continue
        boot_of = {i: k for k, bt in enumerate(idx.get("boots", [])) for i in bt["arms"]}
        cum, chain = {}, chains.setdefault(_craft(idx["file"]), [])
        lo_b, hi_b = bound.get(idx["file"], (None, None))
        for a in idx["arms"]:
            if not (a.get("frames") and a.get("motors_spun", True)):
                continue
            k = boot_of.get(a["index"])
            if (idx["file"], a["index"]) not in elsewhere and (lo_b is None or a["index"] > lo_b) and (hi_b is None or a["index"] < hi_b):
                chain.append({"bbl": idx["file"], "index": a["index"], "length": a["length"],
                              "cum_before": round(cum.get(k, 0.0), 1) if k is not None else None})
            if k is not None:
                cum[k] = cum.get(k, 0.0) + a["length"]
    chains = {c: arms for c, arms in chains.items() if arms}
    out = {v["file"]: {"windows": {}, "ambiguous": False, "planned": []} for v in clips}
    if not chains:
        return out
    held_any = {m["video"] for m in s["matches"]}
    runs_of = {}
    for v in clips:                                          # a clip that cannot be read has no runs, the rest plan on
        if v["file"] in held_any:
            continue
        try:
            runs_of[v["file"]] = [dict(r, clip=v["file"]) for r in video_arms(read_timers(sessions.clip_path(session, v["file"]), fps), fps)]
        except Exception:  # noqa: BLE001  (gone but not marked missing yet, still syncing)
            runs_of[v["file"]] = []
    for arms in chains.values():
        pos = {(a["bbl"], a["index"]): k for k, a in enumerate(arms)}
        held = {}                                            # clip -> positions of its matches in this chain
        for m in s["matches"]:
            if m["video"] in files and (m["bbl"], m["arm"]) in pos:
                held.setdefault(m["video"], []).append(pos[(m["bbl"], m["arm"])])
        fixed = {m["video"] for m in s["matches"] if m["video"] in files}   # matched in any chain: not planned again
        assigned = {v["file"]: list(held.get(v["file"], [])) for v in clips}
        ambiguous = set()
        seg, lo = [], -1
        for v in clips + [None]:                             # segments of clips without matches, between fixed points
            if v is not None and v["file"] not in held:
                if v["file"] not in fixed:
                    seg.append(v)
                continue
            hi = min(held[v["file"]]) if v is not None else len(arms)
            if seg:
                runs = [r for c in seg for r in runs_of.get(c["file"], [])]
                pairs, amb = plan_session_pairs(runs, arms[lo + 1:hi] if hi > lo else [])
                for ri, ai in pairs:
                    assigned[runs[ri]["clip"]].append(lo + 1 + ai)
                ambiguous |= amb
            seg = []
            if v is not None:
                lo = max(lo, max(held[v["file"]]))
        for k, v in enumerate(clips):
            before = [p for c in clips[:k] for p in assigned[c["file"]]]
            after = [p for c in clips[k + 1:] for p in assigned[c["file"]]]
            lo_p, hi_p = max(before, default=-1) - PLAN_SLACK, min(after, default=len(arms)) + PLAN_SLACK
            for b in {a["bbl"] for a in arms}:
                below = [a["index"] for p, a in enumerate(arms) if a["bbl"] == b and p <= lo_p]
                above = [a["index"] for p, a in enumerate(arms) if a["bbl"] == b and p >= hi_p]
                out[v["file"]]["windows"][b] = (max(below) if below else None, min(above) if above else None)
            out[v["file"]]["planned"] += [(arms[p]["bbl"], arms[p]["index"]) for p in sorted(assigned[v["file"]]) if v["file"] not in held]
            out[v["file"]]["ambiguous"] |= v["file"] in ambiguous
    for v in clips:
        if v["file"] in fixed:
            out[v["file"]]["planned"] = sorted({(m["bbl"], m["arm"]) for m in s["matches"] if m["video"] == v["file"]})
    return out


def full_point(s: dict, name: str, after: str | None, fps: float, here: tuple) -> dict | None:
    """Where a log that ended with the flash full ends: the first run, in the session's clips after `after` (the last
    clip holding arms of the log) up to this one (`here` = (read_timers result, runs, path)), whose STATS screen
    shows BLACKBOX 100 %. {clip, run, at, total_arm} (at = the run's start), or None.

    A 100 % only counts when it is this fill: after a clip holding arms of the log, or after a reading below 100 %.
    A flash is erased between fills (10-07: 001 shows the previous fill at 100 %, the erase is in 003, this log
    starts after it), and an erase leaves no trace in the footage the reader sees."""
    if clip_order(name) is None:
        return None
    clips = [(clip_order(v.get("name")), v) for v in s["videos"] if v.get("duration")]
    clips = sorted((k, v) for k, v in clips if k is not None and k < name and (after is None or k > after))
    seen_below = after is not None
    for key, v in clips + [(name, None)]:
        try:
            if v is None:
                rt, runs, path = here
            else:
                path = sessions.clip_path(s["session"], v["file"])
                rt = read_timers(path, fps)
                runs = video_arms(rt, fps)
            stats = read_stats(path, runs, rt["layout"])
        except Exception:  # noqa: BLE001  (a clip gone or unreadable says nothing)
            continue
        for k, st in enumerate(stats):
            bb = st.get("blackbox")
            if bb is not None and bb < 100:
                seen_below = True
            elif bb == 100 and seen_below:
                return {"clip": key, "run": k, "at": runs[k]["start"], "total_arm": st.get("total_arm")}
    return None


def _log_matches(session: dict, video: str, pending: list[dict]) -> dict[str, list[tuple[str, int]]]:
    """{bbl: [(clip name, arm)]} of every match in every session except this clip's own, plus the proposals of
    clips earlier in this run (pending: [{session, video, name, bbl, arm}]). A log can be attached to several
    sessions (one flash dump over several days), so an arm taken in another session is taken here too."""
    out: dict[str, list[tuple[str, int]]] = {}
    for row in sessions.all_sessions():
        if not row.get("matches"):
            continue
        s = session if row["session"] == session["session"] else sessions.load(row["session"])
        names = {v["file"]: v.get("name") or v["file"] for v in s["videos"]}
        for m in s["matches"]:
            if s["session"] == session["session"] and m["video"] == video:
                continue
            out.setdefault(m["bbl"], []).append((names.get(m["video"], m["video"]), m["arm"]))
    for m in pending:
        if not (m["session"] == session["session"] and m["video"] == video):
            out.setdefault(m["bbl"], []).append((m["name"], m["arm"]))
    return out


def automatch(session: str, clip: str, write: bool = False, fps: float = 2.0, overwrite: bool = False,
              pending: list[dict] | None = None, windows: dict | None = None) -> dict:
    """Propose (and with write=True record) matches for one clip from its OSD timers and the session's logs.
    Existing matches of the clip are kept unless overwrite=True (a hand-refined offset beats an OSD one).
    `pending`: proposals of clips before this one in the same run (not written), taken like written ones."""
    s = sessions.load(session)
    path = sessions.clip_path(session, clip)
    name = next((v.get("name") or v["file"] for v in s["videos"] if v["file"] == path.name), path.name)
    rt = read_timers(path, fps)
    rows = rt["rows"]
    varms = video_arms(rt, fps)
    # arms matched to another clip (of any session) are taken: an arm is in one clip; and the clips before and
    # after this one bound its arms (#9: duration-only matches on the cumulative timer went anywhere in the log)
    others = _log_matches(s, path.name, pending or [])
    taken = {(b, a) for b, ms in others.items() for _, a in ms}
    bounds, fulls = {}, {}
    # a clip comes from one flash dump, so align against each log separately and keep the best alignment
    best = None
    for b in s["blackbox"]:
        idx = blackbox.index_bbl(b)
        # the arms this clip may hold: from the session plan when there is one (#26), else bounded by the other clips'
        # matches (#9), which a wrong pick before this clip cascades from
        lo, hi = arm_window(name, others.get(idx["file"], []))
        if windows and idx["file"] in windows:
            # the plan's window, narrowed by what the clips before this one took in this run: the plan reaches one arm
            # into each neighbour (PLAN_SLACK), and an arm a clip before this one took bounds it as before
            plo, phi = windows[idx["file"]]
            lo = max(x for x in (lo, plo) if x is not None) if (lo is not None or plo is not None) else None
            hi = min(x for x in (hi, phi) if x is not None) if (hi is not None or phi is not None) else None
        bounds[idx["file"]] = (lo, hi)
        # a log whose last arm is cut off ended with the flash full (#9): once a STATS screen shows BLACKBOX 100 %,
        # no later run is in it, and the run at which it shows may only be that last arm
        vs = varms
        if idx["arms"] and idx["arms"][-1].get("truncated"):
            last = idx["arms"][-1]["index"]
            before = max((k for k, a in others.get(idx["file"], []) if clip_order(k) and clip_order(name) and k < name), default=None)
            f = full_point(s, name, before, fps, (rt, varms, path))
            if f:
                fulls[idx["file"]] = f
                if f["clip"] != name:
                    vs = [dict(v, no_log=True) for v in varms]
                else:
                    vs = [dict(v, no_log=True) if v["start"] > f["at"] else dict(v, only_arm=last) if k == f["run"] else v
                          for k, v in enumerate(varms)]
        boot_of = {i: k for k, bt in enumerate(idx.get("boots", [])) for i in bt["arms"]}
        cum, logs = {}, []
        for a in idx["arms"]:
            if a.get("frames") and a.get("motors_spun", True):
                k = boot_of.get(a["index"])
                if (idx["file"], a["index"]) not in taken and (lo is None or a["index"] > lo) and (hi is None or a["index"] < hi):
                    logs.append({"bbl": idx["file"], "index": a["index"], "length": a["length"], "vbat_start": a["vbat_start"],
                                 "cum_before": round(cum.get(k, 0.0), 1) if k is not None else None,
                                 "boot": k, "uptime_start": a.get("uptime_start")})
                if k is not None:
                    cum[k] = cum.get(k, 0.0) + a["length"]
        pairs = consistent_pairs(vs, logs, align(vs, logs))
        pairs = in_order(vs, logs, place_by_boot(vs, logs, pairs))
        # ties (a run the clip starts inside fits an arm of several logs) go to the log the session already uses
        in_use = sum(1 for m in s["matches"] if m["bbl"] == idx["file"])
        score = (len(pairs), -round(sum(abs(varms[vi]["length"] - logs[li]["length"]) for vi, li in pairs), 1), in_use)
        if best is None or score > best[0]:
            best = (score, pairs, logs, idx["file"])
    _, pairs, logs, bbl = best if best else ((0, 0), [], [], None)
    full = fulls.get(bbl)
    fits = fit_boots(varms, logs, pairs)
    duration = next((v.get("duration") for v in s["videos"] if v["file"] == path.name), None)
    matches = []
    for vi, li in pairs:
        v, l = varms[vi], logs[li]
        e = _elapsed(v, l)
        offset = round(v["start"] - max(0.0, e), 2) if e is not None else v["start"]
        matches.append({"video": path.name, "bbl": l["bbl"], "arm": l["index"], "offset": offset,
                        "video_start": v["start"], "video_length": v["length"], "log_length": l["length"],
                        "note": f"osd automatch: timer run {v['length']} s vs log {l['length']} s, total before {v['total_before']} vs {l['cum_before']} ({v['readings']} readings)"
                                + ("" if abs(v["length"] - l["length"]) <= 2 else "; placed by the boot's offset, the run reads shorter than the arm")})
    real = [(varms[vi]["start"], logs[li]["index"]) for vi, li in pairs]
    for li, start in derive_unread(logs, pairs, fits, duration):
        l = logs[li]
        if any((t < start) != (i < l["index"]) for t, i in real):     # out of order with what was read
            continue
        if full and (full["clip"] != name or start > full["at"]):     # placed after the flash filled
            continue
        f = fits[l["boot"]]
        matches.append({"video": path.name, "bbl": l["bbl"], "arm": l["index"], "offset": start, "video_start": start,
                        "video_length": None, "log_length": l["length"], "derived": True,
                        "note": f"derived from the boot's fit (video = {f['a']:.5f} x uptime + {f['b']:.2f}, {f['n']} arms read), the arm's timer was not read"})
    matches.sort(key=lambda m: m["offset"])
    unmatched = [v for k, v in enumerate(varms) if k not in {vi for vi, _ in pairs}]
    if write:
        # the session as it is now, not as it was before the minutes of reading: a match set by hand meanwhile is
        # kept, and a log taken out meanwhile gets none (the write checks it again in the same statement, and never
        # attaches the log the way set_match does)
        now_s = sessions.load(session)
        have = {(m["bbl"], m["arm"]) for m in now_s["matches"] if m["video"] == path.name}
        attached = set(now_s["blackbox"])
        for m in matches:
            if m["bbl"] in attached and (overwrite or (m["bbl"], m["arm"]) not in have):
                m["written"] = sessions.store().add_match_if_attached(session, m["video"], m["bbl"], m["arm"], m["offset"], m["note"], replace=overwrite)
    return {"clip": path.name, "name": name, "layout": rt["layout"], "readings": len(rows), "boot_fits": fits,
            "bbl": bbl, "arm_window": bounds.get(bbl, (None, None)), "flash_full": full,
            "readable": sum(1 for r in rows if (r["top"] if not _layout(rt["layout"])["cumulative"] else r["bottom"]) is not None),
            "video_arms": varms, "matches": matches, "unmatched_video_arms": unmatched}
