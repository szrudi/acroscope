"""Ingest: batches the laptop drops into inbox/ become numbered, compressed clips and attached logs.

A batch is `inbox/<batch>/` with the Cobra's `VID*.mov` as they came off the card, any `*.bbl` the laptop pulled
off a quad, an optional `import.json` sidecar ({date, session, imported_at, host}, every field optional) and a
`.done` marker written last. `.done` may carry a manifest ({"files": {name: size}}); when it does, the sizes are
checked before anything is touched, so a transfer cut short is never consumed.

Per batch: the date and session (sidecar, else the import time, else the batch's time; session "<date>-unsorted"
when unnamed), the clips numbered <date>_NNN continuing the day's sequence over all its sessions in card order,
each original moved to originals/<session>/<id>.mov and compressed (no-signal stretches of 5 s or more cut, H.264
as the player wants it) into videos/<session>/<id>.mp4, verified against the kept stretches, and kept for
ORIGINAL_DAYS after that; the logs moved to blackbox/ and attached; then the arms decoded and automatch run on the
new clips. The batch row in the database carries the status, the progress and any error; a failed batch stays in
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
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import blackbox, sessions, video
from .config import BLACKBOX_DIR, DATA_DIR, H264_ARGS, VIDEOS_DIR

INBOX_DIR = DATA_DIR / "inbox"
ORIGINALS_DIR = DATA_DIR / "originals"
STATE_DIR = DATA_DIR / "state"
ORIGINAL_DAYS = 7          # an original is kept this long after its compressed clip verified
RATE = 2                   # no-signal analysis samples per second
THRESHOLD = 35             # luma range (16x12 frame, 10th-90th percentile) below this = no signal
MIN_GAP = 5.0              # only cut no-signal runs at least this long (brief breakups mid-flight are kept)
PAD = 1.0                  # seconds of context kept around each cut
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
    out += list(rows.values())          # done or failed batches whose dir is gone
    return out


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


def check_sizes(d: Path, man: dict) -> None:
    for name, size in (man.get("files") or {}).items():
        f = d / plain_name(name)
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

def luma_ranges(path: Path) -> list[float]:
    """Per RATE-th of a second, the luma spread of the frame shrunk to 16x12: analog static averages to flat grey
    (~15), black is 0, footage stays above 50."""
    cmd = ["ffmpeg", "-hide_banner", "-nostats", "-nostdin", "-i", str(path), "-an", "-vf",
           f"fps={RATE},scale=16:12:flags=area,signalstats,metadata=print:file=-", "-f", "null", "-"]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, check=True, timeout=900).stdout
    except subprocess.TimeoutExpired:   # ffmpeg hung here once (2026-10-09, main thread spinning); a rerun passed
        out = subprocess.run(cmd, capture_output=True, text=True, check=True, timeout=900).stdout
    lo = [float(x) for x in re.findall(r"YLOW=([\d.]+)", out)]
    hi = [float(x) for x in re.findall(r"YHIGH=([\d.]+)", out)]
    return [h - l for l, h in zip(lo, hi)]


def keep_segments(ranges: list[float], duration: float) -> list[tuple[float, float]]:
    """[(start, end)] seconds worth keeping: signal runs, gaps shorter than MIN_GAP bridged, PAD around each."""
    segs, start = [], None
    for i, r in enumerate(ranges + [0]):
        t = i / RATE
        if r >= THRESHOLD and start is None:
            start = t
        elif r < THRESHOLD and start is not None:
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
    segs = keep_segments(luma_ranges(src), dur) or [(0.0, dur)]
    encode(src, dst, segs, progress)
    info = video.probe(dst)
    kept = sum(e - s for s, e in segs)
    return {"kept": [[round(s, 2), round(e, 2)] for s, e in segs], "source_duration": round(dur, 2),
            "duration": info["duration"], "codec": info["codec"], "verified": abs(info["duration"] - kept) <= 1.5}


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


def process_batch(batch: str, log=print) -> dict:
    """Consume inbox/<batch>/. Idempotent: a clip already recorded for this batch is not imported twice."""
    db = sessions.store()
    d = INBOX_DIR / plain_name(batch)
    if not d.is_dir() or d.is_symlink():
        raise FileNotFoundError(f"inbox/{batch}: no such batch")
    db.set_batch(batch, status="running", started_at=now().isoformat(), error=None)
    try:
        man, sc = manifest(d), sidecar(d)
        check_sizes(d, man)
        date, session = batch_date_session(d, sc)
        db.ensure_session(session, date)
        db.set_batch(batch, session=session, date=date)
        entries = batch_entries(d)
        clips = [f for f in entries if CLIP_RE.match(f.name)]
        logs = [f for f in entries if f.name.lower().endswith(".bbl")]
        done_clips = db.batch_clips(batch)                               # {source name: file} from an earlier attempt
        names = next_numbers(date, len([c for c in clips if c.name not in done_clips]))
        imported = []
        (VIDEOS_DIR / session).mkdir(parents=True, exist_ok=True)
        (ORIGINALS_DIR / session).mkdir(parents=True, exist_ok=True)
        for i, src in enumerate(clips):
            if src.name in done_clips:
                continue
            cid, name = new_id(), names.pop(0)
            prog = {"clip": name, "source": src.name, "step": "compress", "done": i, "total": len(clips), "pct": 0}
            db.set_batch(batch, progress=prog)
            log(f"{batch}: {src.name} -> {session}/{cid}.mp4 ({name})")
            orig = _take(src, ORIGINALS_DIR / session / f"{cid}{src.suffix.lower()}")
            dst = VIDEOS_DIR / session / f"{cid}.mp4"

            def pct(x, prog=prog):
                prog["pct"] = round(x * 100)
                db.set_batch(batch, progress=prog)
            res = compress(orig, dst, pct)
            if os.geteuid() == 0:
                os.chown(dst, 0, 0)
            db.upsert_video(session, dst.name, res["duration"], res["codec"], name=name)
            db.set_video_fields(session, dst.name, cuts=res["kept"], original=str(orig.relative_to(DATA_DIR)),
                                original_until=(now() + timedelta(days=ORIGINAL_DAYS)).isoformat() if res["verified"] else None)
            db.add_batch_clip(batch, src.name, dst.name, session)
            if not res["verified"]:
                log(f"{batch}: {name}: compressed clip is {res['duration']} s, kept stretches add up to {sum(e - s for s, e in res['kept']):.1f} s: original kept")
            imported.append(dst.name)
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
        db.set_batch(batch, progress={"step": "automatch", "clips": len(imported)})
        for c in imported:
            _cli("automatch", session, c, "--write")
        for f in batch_entries(d):
            if f.name.startswith(".") or f.name == "import.json":
                f.unlink()
        if not any(d.iterdir()):
            d.rmdir()
        db.set_batch(batch, status="done", finished_at=now().isoformat(), progress=None)
        return {"batch": batch, "session": session, "clips": imported, "logs": attached}
    except Exception as e:  # noqa: BLE001
        db.set_batch(batch, status="failed", finished_at=now().isoformat(), error=f"{type(e).__name__}: {e}")
        raise


def pending() -> list[str]:
    """Batch dirs with a .done marker that are not done or running."""
    return [b["id"] for b in list_inbox() if b.get("status") in ("pending", "failed") and (INBOX_DIR / b["id"] / ".done").exists()
            and b.get("status") != "failed"]


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
