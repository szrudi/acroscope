"""Read the OSD timers off the footage and match clips to arms automatically.

Two OSD layouts have been flown (LAYOUTS). The current one puts two timers bottom right: the top one is the time
since arming (resets every arm), the bottom one the total armed time on the pack. The older one (AIR65 F until
2026-10-04) has a single timer on the bottom row: the total on the pack. The font is fixed and so are the cell
positions per layout (measured on the 720x480 DVR frames), so each digit cell is classified against templates
learned from a few frames labelled by eye (`scripts/osd-labels.json`, built with `acroscope osd-learn`), one
template set per layout. No general OCR needed. A clip's layout is picked by which one reads more digits.

Reading the timers at 2 fps gives every arm start in video time: with two timers an arm is a run where the top
timer counts up from 0; with the cumulative timer alone an arm is a stretch where the total counts (it stands
still while disarmed). Those runs are aligned to the log's arm lengths (and the total against the boot's
cumulative length), and each aligned pair yields a match with its own offset, which also carries across the
static stretches cobra-compress.py cuts out.
"""
import json
import math
import subprocess
from pathlib import Path

from . import blackbox, sessions, video
from .config import VIDEOS_DIR

# cell geometry on a 720x480 frame: digit cells d1 d2 : d3 d4 per timer row, 24 px pitch from x 559 in both layouts
# (glyph surveys of 2026-10-09 and 2026-10-10). `rows` are the y of each timer row top to bottom, `names` what the
# labels file calls them.
LAYOUTS = {
    "otto": {"x0": 559, "pitch": 24, "rows": [362, 398], "names": ["top", "bottom"], "cumulative": False},
    "air65f": {"x0": 559, "pitch": 24, "rows": [434], "names": ["total"], "cumulative": True},
    # same OSD one column further right (10-04 clips 005/006, after a settings change); same font, same templates
    "air65f-b": {"x0": 583, "pitch": 24, "rows": [434], "names": ["total"], "cumulative": True, "templates": "air65f"},
}
SHIFTS = [(dx, dy) for dx in (-1, 0, 1) for dy in (-1, 0, 1)]   # the analog picture jitters a pixel or so
H = 25
OSD_VERSION = 5     # bump when a layout's geometry or the reading format changes: it keys the per-clip cache
DIGIT_COLS = (0, 1, 3, 4)
TEMPLATES = Path(__file__).parent / "static" / "osd-templates.json"
BLANK_STD = 25.0                                   # glyph-box contrast below this = no digit


def _crop(layout: str) -> tuple[int, int, int, int]:
    """x, y, w, h of the strip holding every timer row of a layout (even height: ffmpeg pads odd crops)."""
    L = LAYOUTS[layout]
    h = L["rows"][-1] + H - L["rows"][0]
    return L["x0"], L["rows"][0], 5 * L["pitch"], h + h % 2


def _frames(path: Path, fps: float = 2.0, start: float = 0.0, end: float | None = None, layout: str = "otto"):
    """Yield (t, bytes) gray crops of the timer area at `fps`."""
    x, y, w, h = _crop(layout)
    cmd = ["ffmpeg", "-v", "error", "-nostdin"]
    if start:
        cmd += ["-ss", f"{start:.3f}"]
    cmd += ["-i", str(path)]
    if end is not None:
        cmd += ["-t", f"{end - start:.3f}"]
    cmd += ["-vf", f"fps={fps},crop={w}:{h}:{x}:{y}", "-pix_fmt", "gray", "-f", "rawvideo", "-"]
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


# the glyph sits in the left third of its 24 px cell: 8 px wide at x+5..x+13, 16 px tall at y+4..y+20
GLYPH_X, GLYPH_W, GLYPH_Y, GLYPH_H = 4, 10, 3, 18


def _cell(buf: bytes, col: int, row: int, layout: str = "otto", dx: int = 0, dy: int = 0) -> list[float]:
    """Normalised pixel vector of one digit glyph (GLYPH_W x GLYPH_H), shifted by (dx, dy); [] when blank."""
    L = LAYOUTS[layout]
    w = _crop(layout)[2]
    cx, cy = col * L["pitch"] + GLYPH_X + dx, (L["rows"][row] - L["rows"][0]) + GLYPH_Y + dy
    v = [float(buf[(cy + j) * w + cx + i]) for j in range(GLYPH_H) for i in range(GLYPH_W)]
    m = sum(v) / len(v)
    sd = math.sqrt(sum((a - m) ** 2 for a in v) / len(v))
    if sd < BLANK_STD:
        return []
    return [(a - m) / sd for a in v]


def _template_key(layout: str) -> str:
    return LAYOUTS[layout].get("templates", layout)


def _load_templates(layout: str = "otto") -> dict[str, list[float]]:
    all_ = json.loads(TEMPLATES.read_text()) if TEMPLATES.exists() else {}
    return all_.get(_template_key(layout), {})


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


def read_frame(buf: bytes, templates: dict, layout: str = "otto") -> tuple[int | None, int | None, float]:
    """(top seconds, bottom seconds, min confidence) from one crop; None when a timer is not readable. With the
    single cumulative timer, `top` is always None and `bottom` carries the total."""
    out = []
    conf = 1.0
    for row in range(len(LAYOUTS[layout]["rows"])):
        digits = []
        for col in DIGIT_COLS:
            d, c = _classify(buf, col, row, layout, templates)
            digits.append(d)
            if d is not None:
                conf = min(conf, c)
        if any(d is None for d in digits) or int(digits[2]) > 5:   # MM:SS, so the tens of seconds is 0-5
            out.append(None)
        else:
            out.append((int(digits[0]) * 10 + int(digits[1])) * 60 + int(digits[2]) * 10 + int(digits[3]))
    if LAYOUTS[layout]["cumulative"]:
        return None, out[0], conf
    return out[0], out[1], conf


def detect_layout(path: Path, seconds: float = 90.0) -> str:
    """The layout whose readings make sense: sampled at 1 fps over the clip's first `seconds`, score a layout by
    the consecutive readings of its total timer that stand still or count on (0..2 s apart). Wrong cells still
    read like digits, but they don't count."""
    best = ("otto", -1)
    for name in LAYOUTS:
        tpl = _load_templates(name)   # layouts sharing templates are told apart by where the digits are
        if not tpl:
            continue
        vals = [read_frame(buf, tpl, name)[1] for _, buf in _frames(path, 1.0, 0.0, seconds, name)]
        n = sum(1 for a, b in zip(vals, vals[1:]) if a is not None and b is not None and 0 <= b - a <= 2)
        if n > best[1]:
            best = (name, n)
    return best[0]


def read_timers(path: Path, fps: float = 2.0, layout: str | None = None) -> dict:
    """Read the timers through the clip: {layout, rows: [{t, top, bottom, conf}]} (cached per clip)."""
    st = path.stat()
    tst = TEMPLATES.stat() if TEMPLATES.exists() else None     # new templates (osd-learn) mean new readings
    tkey = f"{tst.st_size}-{int(tst.st_mtime)}" if tst else "none"
    cp = video.cache("osd", path.parent.name, f"{path.name}.{st.st_size}.{int(st.st_mtime)}.{fps:g}.v{OSD_VERSION}-{tkey}.json")
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
    in video seconds. The OSD shows floor(seconds); the OSD-implied start sits ~0.2 s before the real one."""
    # a reading of 0 (the arm's first second) says little about the start and biases the key: left out, as calibrated
    pts = sorted((r["t"] - r["top"], r["t"], r["top"], r["bottom"]) for r in rows if r["top"] and r["conf"] >= 0.01)
    arms = []
    for c in _clusters(pts):
        vs = [p[2] for p in c]
        if len(c) < min_readings or max(vs) - min(vs) < 1:      # the timer must have counted
            continue
        c.sort(key=lambda p: p[1])
        keys = sorted(p[0] for p in c)
        start = keys[len(keys) // 2] + 0.2     # calibrated on 10-09 008 against impact-refined offsets (spread ±0.4 s)
        vmax = max(vs)
        tb = [p[3] - p[2] for p in c if p[3] is not None]
        arms.append({"start": round(start, 2), "end": round(c[-1][1], 2), "length": round(vmax + 0.5, 2), "timer_max": vmax,
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
        arms.append({"start": round(key + v0 + 0.2, 2), "end": round(max(p[1] for p in c), 2), "length": round(v1 - v0 + 0.5, 2),
                     "timer_max": v1 - v0, "total_before": v0, "readings": len(c),
                     # the clip starts inside this arm: its length is only what the clip shows, the start is unknown
                     "partial_start": t0 < 1.5})
    return _drop_ghosts(arms)


def video_arms(rt: dict, fps: float = 2.0, min_readings: int = 3) -> list[dict]:
    """The arm runs of a read_timers() result, by its layout."""
    if LAYOUTS[rt["layout"]]["cumulative"]:
        return arms_from_cumulative(rt["rows"], min_readings)
    return arms_from_timers(rt["rows"], fps, min_readings)


def _elapsed(v: dict, l: dict) -> float | None:
    """For a run the clip starts inside: how far into log arm `l` the clip begins, from the totals (None if unknown)."""
    if v.get("partial_start") and v.get("total_before") is not None and l.get("cum_before") is not None:
        return v["total_before"] - l["cum_before"]
    return None


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
                if d <= 2 * tol and c + d < cost[i + 1][j + 1]:
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


def automatch(session: str, clip: str, write: bool = False, fps: float = 2.0, min_readings: int = 3, overwrite: bool = False) -> dict:
    """Propose (and with write=True record) matches for one clip from its OSD timers and the session's logs.
    Existing matches of the clip are kept unless overwrite=True (a hand-refined offset beats an OSD one)."""
    s = sessions.load(session)
    path = video.resolve_video(session, clip)
    rt = read_timers(path, fps)
    rows = rt["rows"]
    varms = [a for a in video_arms(rt, fps) if a["readings"] >= min_readings]
    # arms already matched to another clip of the session are taken: an arm is in one clip
    taken = {(m["bbl"], m["arm"]) for m in s["matches"] if m["video"] != path.name}
    # a clip comes from one flash dump, so align against each log separately and keep the best alignment
    best = None
    for b in s["blackbox"]:
        idx = blackbox.index_bbl(b)
        boot_of = {i: k for k, bt in enumerate(idx.get("boots", [])) for i in bt["arms"]}
        cum, logs = {}, []
        for a in idx["arms"]:
            if a.get("frames") and a.get("motors_spun", True):
                k = boot_of.get(a["index"])
                if (idx["file"], a["index"]) not in taken:
                    logs.append({"bbl": idx["file"], "index": a["index"], "length": a["length"], "vbat_start": a["vbat_start"],
                                 "cum_before": round(cum.get(k, 0.0), 1) if k is not None else None})
                if k is not None:
                    cum[k] = cum.get(k, 0.0) + a["length"]
        pairs = align(varms, logs)
        # ties (a run the clip starts inside fits an arm of several logs) go to the log the session already uses
        in_use = sum(1 for m in s["matches"] if m["bbl"] == idx["file"])
        score = (len(pairs), -round(sum(abs(varms[vi]["length"] - logs[li]["length"]) for vi, li in pairs), 1), in_use)
        if best is None or score > best[0]:
            best = (score, pairs, logs)
    _, pairs, logs = best if best else ((0, 0), [], [])
    matches = []
    for vi, li in pairs:
        v, l = varms[vi], logs[li]
        e = _elapsed(v, l)
        offset = round(v["start"] - max(0.0, e), 2) if e is not None else v["start"]
        matches.append({"video": path.name, "bbl": l["bbl"], "arm": l["index"], "offset": offset,
                        "video_start": v["start"], "video_length": v["length"], "log_length": l["length"],
                        "note": f"osd automatch: timer run {v['length']} s vs log {l['length']} s, total before {v['total_before']} vs {l['cum_before']} ({v['readings']} readings)"})
    unmatched = [v for k, v in enumerate(varms) if k not in {vi for vi, _ in pairs}]
    if write:
        have = {(m["bbl"], m["arm"]) for m in s["matches"] if m["video"] == path.name}
        for m in matches:
            if overwrite or (m["bbl"], m["arm"]) not in have:
                sessions.set_match(session, m["video"], m["bbl"], m["arm"], m["offset"], m["note"])
                m["written"] = True
    return {"clip": path.name, "layout": rt["layout"], "readings": len(rows),
            "readable": sum(1 for r in rows if (r["top"] if not LAYOUTS[rt["layout"]]["cumulative"] else r["bottom"]) is not None),
            "video_arms": varms, "matches": matches, "unmatched_video_arms": unmatched}


# ---- template learning --------------------------------------------------------------------------------------------

def learn(labels: list[dict]) -> dict:
    """labels: [{session, clip, t, layout, <row name>: 'MM:SS', ...}] (row names per LAYOUTS; layout defaults to
    otto). Takes the median cell features per digit, per layout, and writes every layout's templates."""
    acc: dict[str, dict[str, list[list[float]]]] = {}
    for lb in labels:
        layout = lb.get("layout", "otto")
        path = video.resolve_video(sessions.resolve_session(lb["session"]), lb["clip"])
        t, buf = next(_frames(path, fps=1, start=lb["t"], end=lb["t"] + 1, layout=layout))
        for row, key in enumerate(LAYOUTS[layout]["names"]):
            txt = lb.get(key)
            if not txt:
                continue
            digits = txt.replace(":", "")
            for col, d in zip(DIGIT_COLS, digits):
                f = _cell(buf, col, row, layout)
                if f:
                    acc.setdefault(_template_key(layout), {}).setdefault(d, []).append(f)
                else:
                    print(f"  {lb['clip']}@{lb['t']} {key} digit {d}: blank cell, skipped")
    out = {}
    for layout, by in acc.items():
        out[layout] = {}
        for d, fs in by.items():
            n = len(fs)
            out[layout][d] = [sorted(f[i] for f in fs)[n // 2] for i in range(len(fs[0]))]   # median: a mislabelled frame or two does no harm
    TEMPLATES.write_text(json.dumps(out))
    return {layout: {d: len(fs) for d, fs in by.items()} for layout, by in acc.items()}
