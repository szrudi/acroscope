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
import os
import re
import secrets
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import clock, encode, intake
from ..sources import blackbox, osd, video
from ..store import sessions
from ..config import BLACKBOX_DIR, DATA_DIR, VIDEOS_DIR


INBOX_DIR = DATA_DIR / "inbox"
ORIGINALS_DIR = DATA_DIR / "originals"
ORIGINAL_DAYS = 90         # an original is kept this long after its compressed clip verified (Rudi, 2026-10-18: the
                           # cutting still changes, and a clip is recut from its original; it was 7, then 60; 90 keeps
                           # October's into January, past December's dev time)


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
            b = dict(b, files=len(files), clips=sum(1 for f in files if intake.CLIP_RE.match(f)), logs=sum(1 for f in files if f.lower().endswith(".bbl")))
            out.append(b)
    for b in rows.values():             # done or failed batches whose dir is gone: what they produced
        r = b.get("result") or {}
        out.append(dict(b, clips=len(r.get("clips", [])), logs=len(r.get("logs", []))))
    # in the order they came, whatever their state: the id is the push's timestamp (the player shows it newest first;
    # with the folders first, a new batch went to the bottom and jumped to the top once done and its folder gone)
    return sorted(out, key=lambda b: b["id"])


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
    d = INBOX_DIR / intake.plain_name(batch)
    if not d.is_dir() or d.is_symlink():
        raise FileNotFoundError(f"inbox/{batch}: no such batch")
    work = ORIGINALS_DIR / ".ingest" / intake.plain_name(batch)                  # root's side of the batch, until each clip is in place
    db.set_batch(batch, status="running", started_at=now().isoformat(), error=None)
    try:
        man, sc = intake.manifest(d), intake.sidecar(d)
        staged = {f.name: f for f in intake.batch_entries(work / "taken")} if (work / "taken").is_dir() else {}
        done_clips = db.batch_clips(batch)                               # {source name: file} from an earlier attempt
        intake.check_sizes(d, man, set(done_clips) | set(staged))
        date, session = batch_date_session(d, sc)
        db.ensure_session(session, date)
        db.set_batch(batch, session=session, date=date)
        entries = intake.batch_entries(d)
        inbox = {f.name for f in entries if intake.CLIP_RE.match(f.name)}
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
            taken = intake._take(src, work / "taken" / source) if source in inbox else staged[source]
            orig = ORIGINALS_DIR / session / f"{cid}{src.suffix.lower()}"
            part = work / "out" / f"{cid}.mp4"                           # not under videos/ until whole: the scan would take it
            dst = VIDEOS_DIR / session / f"{cid}.mp4"

            def pct(x, prog=prog):
                prog["pct"] = round(x * 100)
                db.set_batch(batch, progress=prog)
            try:
                res = encode.compress(taken, part, pct)
                # the clock on the encode, before the clip is recorded: a crash here is redone by the retry
                db.set_batch(batch, progress={**prog, "step": "clock"})
                k = clock.fix_clock(part, res["kept"], lambda m: log(f"{batch}: {m}"))
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
                dst = intake._take(f, _unique(dst))
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
        for f in intake.batch_entries(d):
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
