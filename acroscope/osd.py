"""Read the two OSD timers off the footage and match clips to arms automatically.

The Betaflight OSD puts two timers bottom right: the top one is the time since arming (resets every arm), the
bottom one the total armed time on the pack. The font and the cell positions are fixed (24 px pitch on the 720x480
DVR frames), so each digit cell is classified against templates learned from a few frames labelled by eye
(`scripts/osd-labels.json`, built with `acroscope osd-learn`). No general OCR needed.

Reading both timers at 2 fps gives every arm start in video time; an arm is a run where the top timer counts up
from 0. Those runs are aligned to the log's arm lengths, and each aligned pair yields a match with its own offset,
which also carries across the static stretches cobra-compress.py cuts out.
"""
import json
import math
import subprocess
from pathlib import Path

from . import blackbox, sessions, video
from .config import VIDEOS_DIR

# cell geometry on a 720x480 frame: digit cells d1 d2 : d3 d4 on two rows (see the glyph survey of 2026-10-09)
X0, PITCH, W = 559, 24, 24
TOP_Y, BOT_Y, H = 362, 398, 25
CROP = (X0, TOP_Y, 5 * PITCH, BOT_Y + H - TOP_Y + 1)   # x, y, w, h (even height: ffmpeg pads odd crops)
DIGIT_COLS = (0, 1, 3, 4)
FEAT = 12                                          # cell downsampled to FEAT x FEAT
TEMPLATES = Path(__file__).parent / "static" / "osd-templates.json"
BLANK_STD = 25.0                                   # glyph-box contrast below this = no digit


def _frames(path: Path, fps: float = 2.0, start: float = 0.0, end: float | None = None):
    """Yield (t, bytes) gray crops of the timer area at `fps`."""
    x, y, w, h = CROP
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


def _cell(buf: bytes, col: int, row: int) -> list[float]:
    """Normalised pixel vector of one digit glyph (GLYPH_W x GLYPH_H); [] when the cell is blank."""
    w = CROP[2]
    cx, cy = col * PITCH + GLYPH_X, (0 if row == 0 else BOT_Y - TOP_Y) + GLYPH_Y
    v = [float(buf[(cy + j) * w + cx + i]) for j in range(GLYPH_H) for i in range(GLYPH_W)]
    m = sum(v) / len(v)
    sd = math.sqrt(sum((a - m) ** 2 for a in v) / len(v))
    if sd < BLANK_STD:
        return []
    return [(a - m) / sd for a in v]


def _load_templates() -> dict[str, list[float]]:
    return json.loads(TEMPLATES.read_text()) if TEMPLATES.exists() else {}


def _classify(feat: list[float], templates: dict) -> tuple[str | None, float]:
    if not feat or not templates:
        return None, 0.0
    scores = sorted(((sum(a * b for a, b in zip(feat, t)) / len(feat), d) for d, t in templates.items()), reverse=True)
    best, second = scores[0], scores[1] if len(scores) > 1 else (0.0, "")
    return best[1], best[0] - second[0]


def read_frame(buf: bytes, templates: dict) -> tuple[int | None, int | None, float]:
    """(top seconds, bottom seconds, min confidence) from one crop; None when a timer is not readable."""
    out = []
    conf = 1.0
    for row in (0, 1):
        digits = []
        for col in DIGIT_COLS:
            d, c = _classify(_cell(buf, col, row), templates)
            digits.append(d)
            if d is not None:
                conf = min(conf, c)
        if any(d is None for d in digits):
            out.append(None)
        else:
            out.append((int(digits[0]) * 10 + int(digits[1])) * 60 + int(digits[2]) * 10 + int(digits[3]))
    return out[0], out[1], conf


def read_timers(path: Path, fps: float = 2.0) -> list[dict]:
    """Read both timers through the clip: [{t, top, bottom, conf}] (cached per clip in the cache dir)."""
    st = path.stat()
    cp = video.cache("osd", path.parent.name, f"{path.name}.{st.st_size}.{int(st.st_mtime)}.{fps:g}.json")
    if cp.exists():
        return json.loads(cp.read_text())
    tpl = _load_templates()
    rows = []
    for t, buf in _frames(path, fps):
        top, bot, conf = read_frame(buf, tpl)
        rows.append({"t": round(t, 2), "top": top, "bottom": bot, "conf": round(conf, 3)})
    cp.write_text(json.dumps(rows))
    return rows


def arms_from_timers(rows: list[dict], fps: float = 2.0, min_readings: int = 3) -> list[dict]:
    """Arms from the timer readings: every reading of the top timer implies a start time (t - timer), so the readings
    of one arm cluster at the same start while misread digits scatter. [{start, end, length, total_before}] in video
    seconds. The OSD shows floor(seconds); the OSD-implied start sits ~0.2 s before the real one."""
    pts = [(r["t"] - r["top"], r["t"], r["top"], r["bottom"]) for r in rows if r["top"] and r["conf"] >= 0.01]
    pts.sort()
    clusters, cur = [], []
    for p in pts:
        if cur and p[0] - cur[-1][0] > 1.0:
            clusters.append(cur)
            cur = []
        cur.append(p)
    if cur:
        clusters.append(cur)
    arms = []
    for c in clusters:
        if len(c) < min_readings:
            continue
        c.sort(key=lambda p: p[1])
        keys = sorted(p[0] for p in c)
        start = keys[len(keys) // 2] + 0.2     # calibrated on 10-09 008 against impact-refined offsets (spread ±0.4 s)
        vmax = max(p[2] for p in c)
        tb = [p[3] - p[2] for p in c if p[3] is not None]
        arms.append({"start": round(start, 2), "end": round(c[-1][1], 2), "length": round(vmax + 0.5, 2), "timer_max": vmax,
                     "total_before": sorted(tb)[len(tb) // 2] if tb else None, "readings": len(c)})
    arms.sort(key=lambda a: a["start"])
    # a cluster inside another arm's span (misreads sharing a key) is noise
    out = []
    for a in arms:
        if out and a["start"] < out[-1]["end"] - 0.5 and a["readings"] < out[-1]["readings"]:
            continue
        out.append(a)
    return out


def align(video_arms: list[dict], log_arms: list[dict], tol: float = 2.0, w_total: float = 0.3) -> list[tuple[int, int]]:
    """Order-preserving alignment of detected arms to log arms. Cost of a pair = |length difference| +
    w_total * |OSD total-before - log cumulative length in the boot| (when both known); skipping either side costs tol.
    Both lists are in time order. Returns (video index, log index) pairs."""
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
    rows = read_timers(path, fps)
    varms = [a for a in arms_from_timers(rows, fps) if a["readings"] >= min_readings]
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
        score = (len(pairs), -sum(abs(varms[vi]["length"] - logs[li]["length"]) for vi, li in pairs))
        if best is None or score > best[0]:
            best = (score, pairs, logs)
    _, pairs, logs = best if best else ((0, 0), [], [])
    matches = []
    for vi, li in pairs:
        v, l = varms[vi], logs[li]
        matches.append({"video": path.name, "bbl": l["bbl"], "arm": l["index"], "offset": v["start"],
                        "video_start": v["start"], "video_length": v["length"], "log_length": l["length"],
                        "note": f"osd automatch: timer run {v['length']} s vs log {l['length']} s, total before {v['total_before']} vs {l['cum_before']} ({v['readings']} readings)"})
    unmatched = [v for k, v in enumerate(varms) if k not in {vi for vi, _ in pairs}]
    if write:
        have = {(m["bbl"], m["arm"]) for m in s["matches"] if m["video"] == path.name}
        for m in matches:
            if overwrite or (m["bbl"], m["arm"]) not in have:
                sessions.set_match(session, m["video"], m["bbl"], m["arm"], m["offset"], m["note"])
                m["written"] = True
    return {"clip": path.name, "readings": len(rows), "readable": sum(1 for r in rows if r["top"] is not None),
            "video_arms": varms, "matches": matches, "unmatched_video_arms": unmatched}


# ---- template learning --------------------------------------------------------------------------------------------

def learn(labels: list[dict]) -> dict:
    """labels: [{session, clip, t, top: 'MM:SS', bottom: 'MM:SS'}]. Averages the cell features per digit."""
    acc: dict[str, list[list[float]]] = {}
    for lb in labels:
        path = video.resolve_video(sessions.resolve_session(lb["session"]), lb["clip"])
        t, buf = next(_frames(path, fps=1, start=lb["t"], end=lb["t"] + 1))
        for row, key in ((0, "top"), (1, "bottom")):
            txt = lb.get(key)
            if not txt:
                continue
            digits = txt.replace(":", "")
            for col, d in zip(DIGIT_COLS, digits):
                f = _cell(buf, col, row)
                if f:
                    acc.setdefault(d, []).append(f)
                else:
                    print(f"  {lb['clip']}@{lb['t']} {key} digit {d}: blank cell, skipped")
    tpl = {}
    for d, fs in acc.items():
        n = len(fs)
        tpl[d] = [sorted(f[i] for f in fs)[n // 2] for i in range(len(fs[0]))]   # median: a mislabelled frame or two does no harm
    TEMPLATES.write_text(json.dumps(tpl))
    return {d: len(fs) for d, fs in acc.items()}
