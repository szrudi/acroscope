"""Ingest: batches the laptop drops into inbox/ become numbered, compressed clips and attached logs.

A batch is `inbox/<batch>/` with the Cobra's `VID*.mov` as they came off the card, any `*.bbl` the laptop pulled
off a quad, an optional `import.json` sidecar ({date, session, imported_at, host}, every field optional) and a
`.done` marker written last. `.done` may carry a manifest ({"files": {name: size}}); when it does, the sizes are
checked before anything is touched, so a transfer cut short is never consumed.

Per batch: the date and session (sidecar, else the import time, else the batch's time; session "<date>-unsorted"
when unnamed), the clips numbered <date>_NNN continuing the day's sequence over all its sessions in card order,
each original moved to originals/<session>/<id>.mov and compressed (no-signal stretches of 5 s or more cut: flat and
changing like noise; H.264 as the player wants it) into videos/<session>/<id>.mp4, verified against the kept stretches,
re-timed when its OSD clock runs off its own (fix_clock), and kept for
ORIGINAL_DAYS after that; the logs moved to blackbox/ and attached; then the arms decoded and automatch run on the
new clips, and when the batch brought a log, on every clip of the session without a match (a log pushed after its
clips). The batch row in the database carries the status, the progress and any error; a failed batch stays in
inbox/ for a retry. Everything heavy (ffmpeg, the decoder, the OSD reader) runs in child processes when the server
drives this, so requests keep flowing.

The inbox is hostile ground: the push user owns everything in a batch dir (names, modes, links, the manifest) and
this runs as root. So: only regular files with one link and a plain basename count, symlinks and directories fail
the batch, the marker and the sidecar are opened without following links, a file is looked at again once it sits
in a root-owned dir (the push user cannot reach it there), and what is filed is owned by root.
"""
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import blackbox, osd, sessions, video
from .config import BLACKBOX_DIR, CACHE_DIR, DATA_DIR, H264_ARGS, VIDEOS_DIR

INBOX_DIR = DATA_DIR / "inbox"
ORIGINALS_DIR = DATA_DIR / "originals"
STATE_DIR = DATA_DIR / "state"
ORIGINAL_DAYS = 60         # an original is kept this long after its compressed clip verified (Rudi, 2026-10-18: the
                           # cutting still changes, and a clip is recut from its original; it was 7)
TRASH_DAYS = 7             # a deleted clip waits this long in trash/<date>/ (sessions.delete_clip)
RATE = 2                   # no-signal analysis samples per second
THRESHOLD = 35             # luma range (16x12 frame, 10th-90th percentile) below this = flat: static, or a dark/flat picture
CHANGE = 8.0               # and frame-to-frame change (mean |difference|, 64x48) above this = static (snow 15-21; a flat
                           # live picture 0.3-3: dusk, a grey sky with the OSD on, 2026-10-14 schammer 018 and 022)
MIN_GAP = 5.0              # only cut no-signal runs at least this long (brief breakups mid-flight are kept)
PAD = 1.0                  # seconds of context kept around each cut
CLOCK_TOL = 0.05           # an OSD clock off the clip's by more than this = a mis-stamped frame rate (2026-10-14 007:
                           # the DVR wrote ~27 fps stamped 60, the OSD ran 2.20x the clip): the clip is re-timed
CLIP_RE = re.compile(r"^vid.*\.(mov|mp4|avi)$", re.I)


def now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


# ---- the inbox ------------------------------------------------------------------------------------------------

def list_inbox() -> list[dict]:
    """Every batch dir in inbox/: complete (has .done) or still arriving, with what the database knows about it."""
    rows = {b["id"]: b for b in sessions.store().batches()}
    out = []
    if INBOX_DIR.is_dir():
        for d in sorted(p for p in INBOX_DIR.iterdir() if p.is_dir()):
            files = [f.name for f in d.iterdir() if f.is_file() and not f.name.startswith(".")]
            b = rows.pop(d.name, None) or {"id": d.name, "status": "pending" if (d / ".done").exists() else "arriving"}
            b = dict(b, files=len(files), clips=sum(1 for f in files if CLIP_RE.match(f)), logs=sum(1 for f in files if f.lower().endswith(".bbl")))
            out.append(b)
    for b in rows.values():             # done or failed batches whose dir is gone: what they produced
        r = b.get("result") or {}
        out.append(dict(b, clips=len(r.get("clips", [])), logs=len(r.get("logs", []))))
    # in the order they came, whatever their state: the id is the push's timestamp (the player shows it newest first;
    # with the folders first, a new batch went to the bottom and jumped to the top once done and its folder gone)
    return sorted(out, key=lambda b: b["id"])


def plain_name(name: str) -> str:
    """A file name as the push user may give one: one path component, nothing hidden behind it."""
    if not name or name in (".", "..") or "/" in name or "\\" in name or "\0" in name or name != os.path.basename(name):
        raise ValueError(f"{name!r}: not a plain file name")
    return name


def _read_nofollow(p: Path, limit: int = 1 << 20) -> str:
    """The text of a small file, opened without following a link (a link there fails the batch)."""
    try:
        fd = os.open(p, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError as e:
        raise ValueError(f"{p.name}: {e.strerror}") from None
    with os.fdopen(fd, "rb") as f:
        st = os.fstat(fd)
        if not os.path.isfile(p) or st.st_size > limit:
            raise ValueError(f"{p.name}: not a small regular file")
        return f.read().decode("utf-8", "replace")


def batch_entries(d: Path) -> list[Path]:
    """The regular files of a batch dir. Anything else there (a link, a directory, a file with several links, a
    name that is not plain) fails the batch: the push user put it there, and this runs as root."""
    out = []
    with os.scandir(d) as it:
        for e in it:
            plain_name(e.name)
            st = os.lstat(e.path)
            import stat as _st
            if not _st.S_ISREG(st.st_mode):
                raise ValueError(f"{e.name}: not a regular file")
            if st.st_nlink > 1:
                raise ValueError(f"{e.name}: has {st.st_nlink} links")
            out.append(Path(e.path))
    return sorted(out)


def sidecar(d: Path) -> dict:
    p = d / "import.json"
    if not os.path.lexists(p):
        return {}
    try:
        sc = json.loads(_read_nofollow(p))
    except json.JSONDecodeError as e:
        raise ValueError(f"import.json: {e}") from None
    if not isinstance(sc, dict):
        raise ValueError("import.json: not an object")
    return {k: v for k, v in sc.items() if isinstance(v, str)}


def manifest(d: Path) -> dict:
    p = d / ".done"
    if not os.path.lexists(p):
        raise ValueError("no .done marker: the batch is still arriving")
    try:
        txt = _read_nofollow(p).strip()
        man = json.loads(txt) if txt else {}
    except json.JSONDecodeError as e:
        raise ValueError(f".done: {e}") from None
    if not isinstance(man, dict) or not isinstance(man.get("files", {}), dict):
        raise ValueError(".done: not a manifest")
    for name in man.get("files") or {}:
        plain_name(name)
    return man


def check_sizes(d: Path, man: dict, taken: set[str] = frozenset()) -> None:
    """The manifest against the batch dir; `taken` are clips an earlier attempt already took out of it."""
    for name, size in (man.get("files") or {}).items():
        if plain_name(name) in taken and not os.path.lexists(d / name):
            continue
        f = d / name
        if not os.path.lexists(f):
            raise ValueError(f"{name}: in the manifest, not in the batch")
        st = os.lstat(f)
        if size is not None and st.st_size != size:
            raise ValueError(f"{name}: {st.st_size} bytes, the manifest says {size}")


def _take(src: Path, dst: Path) -> Path:
    """Move a batch file into a root-owned place, then make sure it is still a plain file there (the push user
    could have swapped it for a link between the look and the move), and give it to root."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    os.rename(src, dst)                                   # same filesystem; renames a link as a link, never follows
    st = os.lstat(dst)
    import stat as _st
    if not _st.S_ISREG(st.st_mode) or st.st_nlink > 1:
        dst.unlink()
        raise ValueError(f"{src.name}: was swapped for a link or hard-linked while being taken")
    if os.geteuid() == 0:
        os.chown(dst, 0, 0)
    os.chmod(dst, 0o644)
    return dst


def batch_date_session(d: Path, sc: dict) -> tuple[str, str]:
    date = sc.get("date") or (sc.get("imported_at") or "")[:10]
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date or ""):
        date = datetime.fromtimestamp(d.stat().st_mtime).strftime("%Y-%m-%d")
    name = re.sub(r"[^A-Za-z0-9_-]+", "-", sc.get("session") or "").strip("-")
    return date, f"{date}-{name}" if name else f"{date}-unsorted"


def next_numbers(date: str, count: int) -> list[str]:
    """<date>_NNN names continuing the day's sequence across all its sessions."""
    used = [int(m.group(1)) for n in sessions.store().names_on(date) if (m := re.search(r"_(\d{3})$", n))]
    start = max(used, default=0) + 1
    return [f"{date}_{start + i:03d}" for i in range(count)]


def new_id() -> str:
    return "c" + secrets.token_hex(5)


# ---- the compressor (cobra-compress.py as functions) ---------------------------------------------------------------

def signal_stats(path: Path) -> tuple[list[float], list[float]]:
    """Per RATE-th of a second: the luma spread of the frame shrunk to 16x12 (analog static averages to flat grey,
    ~15; footage mostly stays above 50, but dusk or a sky-filled frame drops to 22-34), and the mean change from one
    frame to the next at 64x48 over that stretch (static is new noise every frame, 15-21; a flat live picture with
    its OSD is nearly still). One decode, two branches, each printing its own stats to a file."""
    with tempfile.TemporaryDirectory() as tmp:
        a, b = Path(tmp) / "range", Path(tmp) / "change"
        cmd = ["ffmpeg", "-hide_banner", "-nostats", "-nostdin", "-i", str(path), "-filter_complex",
               f"[0:v]split[x][y];[x]fps={RATE},scale=16:12:flags=area,signalstats,metadata=print:file={a}[ra];"
               f"[y]scale=64:48:flags=area,tblend=all_mode=difference,signalstats,metadata=print:file={b}[rb]",
               "-map", "[ra]", "-f", "null", "-", "-map", "[rb]", "-f", "null", "-"]
        try:
            subprocess.run(cmd, capture_output=True, text=True, check=True, timeout=900)
        except subprocess.TimeoutExpired:   # ffmpeg hung here once (2026-10-09, main thread spinning); a rerun passed
            subprocess.run(cmd, capture_output=True, text=True, check=True, timeout=900)
        ra, rb = a.read_text(), b.read_text()
    lo = [float(x) for x in re.findall(r"YLOW=([\d.]+)", ra)]
    hi = [float(x) for x in re.findall(r"YHIGH=([\d.]+)", ra)]
    ranges = [h - l for l, h in zip(lo, hi)]
    sums, counts = [0.0] * len(ranges), [0] * len(ranges)
    for t, d in zip(re.findall(r"pts_time:([\d.]+)", rb), re.findall(r"YAVG=([\d.]+)", rb)):
        i = int(float(t) * RATE)
        if i < len(ranges):
            sums[i] += float(d); counts[i] += 1
    return ranges, [s / c if c else CHANGE for s, c in zip(sums, counts)]


def keep_segments(ranges: list[float], duration: float, changes: list[float] | None = None) -> list[tuple[float, float]]:
    """[(start, end)] seconds worth keeping: signal runs, gaps shorter than MIN_GAP bridged, PAD around each. A
    sample is no signal when it is flat (range below THRESHOLD) and, where `changes` are given, changes like noise
    (above CHANGE): flat and still is a picture (dusk, a grey sky) and is kept."""
    live = [r >= THRESHOLD or (changes is not None and i < len(changes) and changes[i] <= CHANGE) for i, r in enumerate(ranges)]
    segs, start = [], None
    for i, ok in enumerate(live + [False]):
        t = i / RATE
        if ok and start is None:
            start = t
        elif not ok and start is not None:
            segs.append([start, t])
            start = None
    merged: list[list[float]] = []
    for s, e in segs:
        if merged and s - merged[-1][1] < MIN_GAP:
            merged[-1][1] = e
        else:
            merged.append([s, e])
    return [(max(0.0, s - PAD), min(duration, e + PAD)) for s, e in merged]


def encode(src: Path, dst: Path, segs: list[tuple[float, float]], progress=None) -> None:
    """The kept stretches of src as one H.264 clip (config.H264_ARGS: what the player wants), niced."""
    sel = "+".join(f"between(t,{s:.2f},{e:.2f})" for s, e in segs)
    kept = sum(e - s for s, e in segs) or 1
    part = dst.with_name(dst.name + ".part.mp4")
    cmd = ["nice", "-n", "10", "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostats", "-nostdin", "-progress", "pipe:1", "-y",
           "-i", str(src), "-vf", f"select='{sel}',setpts=N/FRAME_RATE/TB", "-af", f"aselect='{sel}',asetpts=N/SR/TB",
           *H264_ARGS, str(part)]
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, text=True)
    for line in p.stdout:
        if progress and line.startswith("out_time_us=") and line[12:].strip().isdigit():
            progress(min(1.0, int(line[12:]) / 1e6 / kept))
    if p.wait():
        part.unlink(missing_ok=True)
        raise subprocess.CalledProcessError(p.returncode, "ffmpeg")
    part.replace(dst)


def compress(src: Path, dst: Path, progress=None) -> dict:
    """Analyse, cut and encode src into dst. Returns {kept: [[s, e]...], source_duration, duration, verified}.
    A clip without any signal is encoded whole (nothing cut), so nothing is silently dropped."""
    dur = video.probe(src)["duration"]
    ranges, changes = signal_stats(src)
    segs = keep_segments(ranges, dur, changes) or [(0.0, dur)]
    encode(src, dst, segs, progress)
    info = video.probe(dst)
    kept = sum(e - s for s, e in segs)
    return {"kept": [[round(s, 2), round(e, 2)] for s, e in segs], "source_duration": round(dur, 2),
            "duration": info["duration"], "codec": info["codec"], "verified": abs(info["duration"] - kept) <= 1.5}


def _to_source(t: float, cuts: list) -> float:
    """A time in a compressed clip -> the same moment in its original, through the kept stretches."""
    acc = 0.0
    for s, e in cuts:
        if t <= acc + (e - s):
            return s + (t - acc)
        acc += e - s
    return cuts[-1][1]


def _to_clip(o: float, cuts: list) -> float:
    """A time in an original -> the compressed clip (a moment inside a cut lands where the next stretch starts)."""
    acc = 0.0
    for s, e in cuts:
        if o <= e:
            return acc + max(0.0, o - s)
        acc += e - s
    return acc


def move_time(t: float, old: list, k_old: float, new: list, k_new: float) -> float:
    """A time in a clip as it was (kept stretches `old`, re-timed by k_old) -> the same moment in the clip as it is
    now (`new`, k_new). Cuts are in the original's own (stamped) time; a re-timed clip runs k times that."""
    return round(_to_clip(_to_source(t / k_old, old), new) * k_new, 2)


def _stretch(t: float, cuts: list) -> int:
    """Which kept stretch a compressed clip's time t falls in."""
    acc = 0.0
    for i, (s, e) in enumerate(cuts):
        acc += e - s
        if t < acc:
            return i
    return len(cuts) - 1


CLOCK_EVEN = 0.03          # clock_fit: each third's slope within this of the whole fit's (frames dropped evenly; 007's
                           # thirds are within 1.3 % of its 2.2001)


def clock_fit(rows: list[dict], kept: list | None = None) -> float | None:
    """How fast the OSD arm clock runs against the clip's own time, from read_timers rows: only readings where both
    timers agree (a second arm's total and arm timers differ) and that advanced since the last one (a disarmed timer
    stands still), misreads trimmed. Fitted in the original's time (each reading back through the kept stretches): a
    cut inside an arm is not a fast clock. None (cannot tell) on less than 20 readings over 15 s, a fit worse than
    1 s rms, a third with too few readings, or a third whose rate is not the whole fit's: an off clock is only fixed when the
    frames were dropped evenly (2026-10-14 007: 2.200 / 2.183 / 2.207), not for one DVR stall."""
    pts, prev = [], None
    for x in rows:
        top, bot = x.get("top"), x.get("bottom")
        if top is None or bot is None or abs(top - bot) > 1.2 or top <= 1:
            continue
        if prev is not None and top > prev:
            pts.append((_to_source(x["t"], kept) if kept else x["t"], top))
        prev = top

    def fit(p):
        n = len(p)
        mx, my = sum(t for t, _ in p) / n, sum(y for _, y in p) / n
        sxx = sum((t - mx) ** 2 for t, _ in p)
        a = sum((t - mx) * (y - my) for t, y in p) / sxx if sxx else 0.0
        return a, my - a * mx

    for lim in (10.0, 4.0, 2.0, 1.5):
        if len(pts) < 20:
            return None
        a, b = fit(pts)
        pts = [(t, y) for t, y in pts if abs(y - a * t - b) < lim]
    if len(pts) < 20 or pts[-1][0] - pts[0][0] < 15:
        return None
    a, b = fit(pts)
    rms = (sum((y - a * t - b) ** 2 for t, y in pts) / len(pts)) ** 0.5
    if not (rms < 1.0 and a > 0):
        return None
    t0, t1 = pts[0][0], pts[-1][0]
    thirds = [[p for p in pts if t0 + k * (t1 - t0) / 3 <= p[0] <= t0 + (k + 1) * (t1 - t0) / 3] for k in range(3)]
    if any(len(th) < 5 for th in thirds):
        return None
    # each third runs at the whole fit's rate: a stall between two thirds leaves them all at 1.0 against a fit of 1.06
    return a if all(abs(fit(th)[0] - a) <= CLOCK_EVEN * a for th in thirds) else None


def retime(path: Path, k: float) -> None:
    """Stretch a clip's clock by k: the video's timestamps scaled, no re-encode. The audio goes (the DVR stamped it as
    wrongly). Built beside the clip as .part (not a video suffix: the scan leaves it alone), then put in place."""
    tmp = path.with_name(path.name + ".retime.part")
    try:
        subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-y", "-itsscale", f"{k:.5f}", "-i", str(path), "-map", "0:v", "-c", "copy",
                        "-movflags", "+faststart", "-f", "mp4", str(tmp)], check=True)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    os.replace(tmp, path)


def fix_clock(path: Path, kept: list, log=print) -> float | None:
    """1.0 (on time), the factor a clip was re-timed by, or None when it cannot tell (an OSD the reader cannot read, no
    arm long enough, an uneven clock): the clip then stays as it is. The OSD's arm clock against the clip's, in the
    original's time (`kept`: the stretches compress kept), read at automatch's 2 fps."""
    try:
        k = clock_fit(osd.read_timers(path, 2.0)["rows"], kept)
    except Exception:  # noqa: BLE001  (an OSD the reader has no layout for: nothing to go by)
        return None
    if k is None:
        return None
    if abs(k - 1) <= CLOCK_TOL:
        return 1.0
    k = round(k, 4)
    log(f"{path.name}: the OSD clock runs {k}x the clip's (a mis-stamped frame rate): re-timed to {video.probe(path)['duration'] * k:.1f} s")
    retime(path, k)
    return k


def recut(session: str, vid: str, log=print) -> dict:
    """Compress a clip again from its kept original, under the same id: for a cut that took real footage (the
    detector before 2026-10-14 took dusk and a grey sky for static, and 3.5 s of flight each in 2026-10-14
    schammer 018 and 022). Moments and matches move with the footage: old clip time -> original -> new clip time.
    A match whose arm spans an old cut held on one side of it only, so it is left as it is and listed in `check`
    for a look. The clip's cached frames and sheets go; the original stays until its date.
    {video, old_cuts, cuts, duration, moments, matches, check}"""
    db = sessions.store()
    file = sessions.resolve_clip(session, vid)
    s = db.load(session)
    v = next((x for x in s["videos"] if x["file"] == file), None)
    if v is None:
        raise ValueError(f"{session}: no clip {vid}")
    src = DATA_DIR / v["original"] if v.get("original") else None
    if not src or not src.is_file():
        raise ValueError(f"{v.get('name') or file}: no original kept (they go {ORIGINAL_DAYS} days after the import)")
    k_old = v.get("timescale") or 1.0
    old = v.get("cuts") or [[0.0, v["duration"] / k_old]]
    work = ORIGINALS_DIR / ".ingest" / f"recut-{file}"                     # not under videos/: the scan would take it
    work.parent.mkdir(parents=True, exist_ok=True)
    log(f"{session}/{v.get('name') or file}: compressing again from {v['original']}")
    try:
        res = compress(src, work)
        if not res["verified"]:
            raise ValueError(f"{file}: compressed clip is {res['duration']} s against {sum(e - s for s, e in res['kept']):.1f} s kept: not replaced")
    except Exception:
        work.unlink(missing_ok=True)
        raise
    new = res["kept"]
    k_new = fix_clock(work, new, log)
    if k_new is None:                       # cannot tell from the new encode: the clock is the original's, as it was
        k_new = k_old
        if k_old != 1.0:
            retime(work, k_old)
    move = lambda t: move_time(t, old, k_old, new, k_new)
    reclocked = abs(k_new / k_old - 1) > 0.01
    moments, matches, check = [], [], []
    for m in (m for m in s["moments"] if m["video"] == file):
        a, b = move(m["start"]), move(m["end"])
        if (a, b) != (m["start"], m["end"]):
            db.tag(session, file, a, b, m["title"], m.get("tags") or [], m.get("note") or "", mid=m["id"])
            moments.append({"id": m["id"], "from": [m["start"], m["end"]], "to": [a, b]})
    for m in (m for m in s["matches"] if m["video"] == file):
        idx = blackbox.index_bbl(m["bbl"], cached_only=True) if (BLACKBOX_DIR / m["bbl"]).is_file() else None
        length = next((a["length"] for a in (idx or {}).get("arms", []) if a["index"] == m["arm"]), None)
        if length is None or reclocked or _stretch(m["offset"] / k_old, old) != _stretch((m["offset"] + length) / k_old, old):
            why = ("arm length unknown" if length is None else f"the clip's clock changed ({k_old} -> {k_new}): the offset was measured on the old one"
                   if reclocked else "the arm spans an old cut: its offset held on one side only")
            check.append({"bbl": m["bbl"], "arm": m["arm"], "offset": m["offset"], "why": why})
            continue
        o = move(m["offset"])
        if o != m["offset"]:
            db.set_match(session, file, m["bbl"], m["arm"], o, m.get("note") or "")
            matches.append({"bbl": m["bbl"], "arm": m["arm"], "from": m["offset"], "to": o})
    os.replace(work, VIDEOS_DIR / session / file)
    duration = video.probe(VIDEOS_DIR / session / file)["duration"]
    db.set_video_fields(session, file, cuts=new, duration=duration, codec=res["codec"], timescale=k_new if k_new != 1.0 else None)
    for f in (CACHE_DIR / "frames" / session).glob(f"{Path(file).stem}_*"):
        f.unlink()
    return {"video": file, "old_cuts": old, "cuts": new, "timescale": k_new, "duration": duration, "moments": moments,
            "matches": matches, "check": check}


# ---- a batch -----------------------------------------------------------------------------------------------------

def _cli(*args: str) -> int:
    env = dict(os.environ, ACROSCOPE_URL="")
    return subprocess.run([sys.executable, "-m", "acroscope.cli", *args], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode


def _unique(dst: Path) -> Path:
    """dst, or dst with a counter when a different file already sits there (the same bytes: dst itself)."""
    if not dst.exists():
        return dst
    i = 2
    while True:
        cand = dst.with_name(f"{dst.stem}-{i}{dst.suffix}")
        if not cand.exists():
            return cand
        i += 1


def _place(taken: Path, part: Path, dst: Path, orig: Path) -> None:
    """A recorded clip's files into place: the encode under videos/, the source under originals/. Each step is a
    rename, and a retry after a crash in between does what is left."""
    if part.exists():
        shutil.move(part, dst)
    if taken.exists():
        os.rename(taken, orig)


def _tidy(work: Path) -> None:
    """The batch's work dir goes once nothing is left in it."""
    for sub in (work / "out", work / "taken", work):
        if sub.is_dir() and not any(sub.iterdir()):
            sub.rmdir()


def process_batch(batch: str, log=print) -> dict:
    """Consume inbox/<batch>/. Idempotent: a clip already recorded for this batch is not imported twice."""
    db = sessions.store()
    d = INBOX_DIR / plain_name(batch)
    if not d.is_dir() or d.is_symlink():
        raise FileNotFoundError(f"inbox/{batch}: no such batch")
    work = ORIGINALS_DIR / ".ingest" / plain_name(batch)                  # root's side of the batch, until each clip is in place
    db.set_batch(batch, status="running", started_at=now().isoformat(), error=None)
    try:
        man, sc = manifest(d), sidecar(d)
        staged = {f.name: f for f in batch_entries(work / "taken")} if (work / "taken").is_dir() else {}
        done_clips = db.batch_clips(batch)                               # {source name: file} from an earlier attempt
        check_sizes(d, man, set(done_clips) | set(staged))
        date, session = batch_date_session(d, sc)
        db.ensure_session(session, date)
        db.set_batch(batch, session=session, date=date)
        entries = batch_entries(d)
        inbox = {f.name for f in entries if CLIP_RE.match(f.name)}
        logs = [f for f in entries if f.name.lower().endswith(".bbl")]
        (VIDEOS_DIR / session).mkdir(parents=True, exist_ok=True)
        (ORIGINALS_DIR / session).mkdir(parents=True, exist_ok=True)
        # an earlier attempt died after recording a clip and before its files were in place: put them there now
        recorded = {v["file"]: v for v in db.load(session)["videos"]}
        for source, file in done_clips.items():
            if source in staged and file in recorded:
                _place(staged.pop(source), work / "out" / file, VIDEOS_DIR / session / file, DATA_DIR / recorded[file]["original"])
        shutil.rmtree(work / "out", ignore_errors=True)                  # half an encode from a run that died
        (work / "out").mkdir(parents=True)
        todo = sorted((inbox | set(staged)) - set(done_clips))
        names = next_numbers(date, len(todo))
        imported = []
        for i, source in enumerate(todo):
            cid, name = new_id(), names.pop(0)
            prog = {"clip": name, "source": source, "step": "compress", "done": i, "total": len(todo), "pct": 0}
            db.set_batch(batch, progress=prog)
            log(f"{batch}: {source} -> {session}/{cid}.mp4 ({name})")
            src = d / source
            # out of the push user's reach first (a new push of a staged clip replaces the staged copy)
            taken = _take(src, work / "taken" / source) if source in inbox else staged[source]
            orig = ORIGINALS_DIR / session / f"{cid}{src.suffix.lower()}"
            part = work / "out" / f"{cid}.mp4"                           # not under videos/ until whole: the scan would take it
            dst = VIDEOS_DIR / session / f"{cid}.mp4"

            def pct(x, prog=prog):
                prog["pct"] = round(x * 100)
                db.set_batch(batch, progress=prog)
            try:
                res = compress(taken, part, pct)
                # the clock on the encode, before the clip is recorded: a crash here is redone by the retry
                db.set_batch(batch, progress={**prog, "step": "clock"})
                k = fix_clock(part, res["kept"], lambda m: log(f"{batch}: {m}"))
                k = None if k in (None, 1.0) else k
                duration = video.probe(part)["duration"] if k else res["duration"]
                if os.geteuid() == 0:
                    os.chown(part, 0, 0)
                db.record_ingested(batch, source, session, dst.name, name, duration, res["codec"], cuts=res["kept"], timescale=k,
                                   original=str(orig.relative_to(DATA_DIR)),
                                   original_until=(now() + timedelta(days=ORIGINAL_DAYS)).isoformat() if res["verified"] else None)
            except Exception:
                part.unlink(missing_ok=True)
                if not os.path.lexists(src):
                    os.rename(taken, src)           # back into the batch: the retry takes it again (or a good copy pushed over it)
                raise
            _place(taken, part, dst, orig)
            if not k:
                osd.carry_reading(part, dst)        # the reading was taken on the encode: automatch reuses it there
            if not res["verified"]:
                log(f"{batch}: {name}: compressed clip is {res['duration']} s, kept stretches add up to {sum(e - s for s, e in res['kept']):.1f} s: original kept")
            imported.append(dst.name)
        _tidy(work)
        BLACKBOX_DIR.mkdir(parents=True, exist_ok=True)
        attached = []
        for f in logs:
            dst = BLACKBOX_DIR / f.name
            if dst.exists() and dst.stat().st_size == os.lstat(f).st_size:
                f.unlink()                                               # the same dump again
            else:
                dst = _take(f, _unique(dst))
            db.attach(session, dst.name)
            attached.append(dst.name)
        db.set_batch(batch, progress={"step": "decode", "logs": attached})
        for b in attached:
            if blackbox.index_bbl(b, cached_only=True) is None:
                _cli("arms", b)
        # this batch's clips, and with a log: every clip of the session without a match yet (the log may come in a
        # batch of its own after its clips, as on 2026-10-14; a clip already matched keeps its matches)
        now_in = db.load(session)
        matched = {m["video"] for m in now_in["matches"]}
        to_match = [v["file"] for v in now_in["videos"] if v["file"] in imported
                    or (attached and v["file"] not in matched and not v.get("missing_since"))]
        db.set_batch(batch, progress={"step": "automatch", "clips": len(to_match)})
        if to_match:                         # in one run: the clips are planned together, not each bounded by the last (#26)
            _cli("automatch", session, *to_match, "--write")
        for f in batch_entries(d):
            if f.name.startswith(".") or f.name == "import.json":
                f.unlink()
        if not any(d.iterdir()):
            d.rmdir()
        names = {v["file"]: v.get("name") for v in db.load(session)["videos"]}
        result = {"clips": [names.get(c, c) for c in imported], "logs": attached}
        db.set_batch(batch, status="done", finished_at=now().isoformat(), progress=None, result=result)
        return {"batch": batch, "session": session, "clips": imported, "logs": attached}
    except Exception as e:  # noqa: BLE001
        _tidy(work)
        db.set_batch(batch, status="failed", finished_at=now().isoformat(), error=f"{type(e).__name__}: {e}")
        raise


def pending() -> list[str]:
    """Batch dirs with a .done marker that are pending (a failed one waits for a retry, which sets it pending)."""
    return [b["id"] for b in list_inbox() if b.get("status") == "pending" and (INBOX_DIR / b["id"] / ".done").exists()]


# ---- housekeeping ---------------------------------------------------------------------------------------------------

def cleanup_originals(log=print) -> list[str]:
    """Delete originals past their retention date (and clear the fields)."""
    db = sessions.store()
    gone = []
    for session, file, original in db.expired_originals(now().isoformat()):
        p = DATA_DIR / original
        if p.exists():
            p.unlink()
        db.set_video_fields(session, file, original=None, original_until=None)
        gone.append(original)
        log(f"original removed: {original}")
    return gone


def cleanup_trash(root: Path | None = None, today: str | None = None, days: int = TRASH_DAYS, log=print) -> list[str]:
    """Delete the trash/<date>/ folders older than `days` (what sessions.delete_clip put there). Returns the dates."""
    root = root or DATA_DIR / "trash"
    today_d = datetime.fromisoformat(today).date() if today else now().date()
    gone = []
    for d in sorted(root.iterdir()) if root.is_dir() else []:
        try:
            day = datetime.fromisoformat(d.name).date()
        except ValueError:
            continue                                   # not one of ours
        if d.is_dir() and (today_d - day).days > days:
            shutil.rmtree(d)
            gone.append(d.name)
            log(f"trash emptied: {d.name}")
    return gone


def backup_db(keep: int = 7) -> Path | None:
    """A consistent copy of the database into the data dir's state/, so clips and index restore together."""
    db = sessions.store()
    if not hasattr(db, "backup_to"):
        return None
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    dst = STATE_DIR / f"acroscope-{now().strftime('%Y-%m-%d')}.db"
    db.backup_to(dst)
    old = sorted(STATE_DIR.glob("acroscope-*.db"))[:-keep]
    for p in old:
        p.unlink()
    return dst
