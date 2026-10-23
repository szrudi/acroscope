"""Sessions: what the database knows about a flying session, plus the folder scan that feeds it.

A session is videos/<YYYY-MM-DD-name>/ in the data dir. The database (db.Db, or remote.Remote when the CLI talks to
a server) holds its clips (with duration, codec, note, and `missing_since` once the file is gone), blackbox files,
matches (video <-> arm + offset) and moments. The clips and the logs themselves stay in the data dir.

Times are video seconds. video time = arm time + offset (offset holds within one arm; the clips have static cut
out, so a clip's timeline is not continuous across arms). Arm details come from the blackbox cache, not from here.
"""
import re
import subprocess
import sys
from pathlib import Path

from ..sources import blackbox, video
from ..config import DB_PATH, SERVER_URL, VIDEOS_DIR
from .db import now


VIDEO_EXT = (".mp4", ".mov", ".mkv")
_store = None


def store():
    """The database (default), or a client of the server named by ACROSCOPE_URL."""
    global _store
    if _store is None:
        if SERVER_URL:
            from .remote import Remote
            _store = Remote(SERVER_URL)
        else:
            from .db import Db
            if not DB_PATH.exists():
                print(f"acroscope: new local database {DB_PATH} (set ACROSCOPE_URL to use a server's instead)", file=sys.stderr)
            _store = Db(DB_PATH)
    return _store


def remote() -> bool:
    return bool(SERVER_URL)


# ---- folders ----------------------------------------------------------------------------------------------------

def session_dirs() -> list[Path]:
    if not VIDEOS_DIR.is_dir():   # data dir not mounted (yet): no sessions rather than a crash
        return []
    return sorted(d for d in VIDEOS_DIR.iterdir() if d.is_dir() and re.match(r"\d{4}-\d{2}-\d{2}", d.name))


def session_path(session: str) -> Path:
    """The pre-database session file (only read by import_json now)."""
    return VIDEOS_DIR / session / "session.json"


def _check_name(session: str) -> None:
    """A session is a folder name; anything with a slash or a '..' would reach outside videos/ on disk."""
    if not session or "/" in session or session in (".", "..") or "\\" in session:
        raise ValueError(f"{session!r}: not a session name")


def resolve_session(name: str) -> str:
    """Accept the full name or a unique fragment ('10-07', 'schammer'); folders and database sessions both count."""
    names = {d.name for d in session_dirs()} | {s["session"] for s in store().sessions()}
    if name in names:
        return name
    hits = sorted(n for n in names if name in n)
    if len(hits) != 1:
        raise FileNotFoundError(f"{name}: {len(hits)} sessions match (need exactly 1)")
    return hits[0]


def resolve_clip(session: str, file: str) -> str:
    """A clip's file name (its id on disk) from its file name, its readable name ('2026-10-07_007'), its number
    ('007'/'7') or a path; a clip that is only in the database (file gone) still resolves."""
    _check_name(session)
    vids = load(session)["videos"]
    by_file = {v["file"]: v for v in vids}
    if file in by_file:
        return file
    if Path(file).name in by_file:
        return Path(file).name
    hits = [v["file"] for v in vids if v.get("name") == file or Path(v["file"]).stem == file]
    if file.isdigit():
        hits += [v["file"] for v in vids if (v.get("name") or "").endswith(f"_{int(file):03d}")]
    hits = sorted(set(hits), key=lambda f: Path(f).suffix.lower() != ".mp4")   # the compressed .mp4 before an original .mov beside it
    if hits and len({Path(f).stem for f in hits}) == 1:
        return hits[0]
    if len(hits) > 1:
        raise FileNotFoundError(f"{file}: {len(hits)} clips match (need exactly 1)")
    return video.resolve_video(session, file).name     # a file not registered yet, or a FileNotFoundError


def clip_path(session: str, file: str) -> Path:
    """The file of a clip given by file name, readable name, number or path."""
    return VIDEOS_DIR / session / resolve_clip(session, file)


def clip_files(d: Path) -> list[str]:
    return sorted(f.name for f in d.iterdir() if f.suffix.lower() in VIDEO_EXT)


# ---- reading ----------------------------------------------------------------------------------------------------

def load(session: str) -> dict:
    return store().load(session)


def all_sessions() -> list[dict]:
    return store().sessions()


def match_for(s: dict, vid: str, t: float | None = None) -> dict | None:
    """The match covering video `vid` (at video time t if given, else the first)."""
    ms = [m for m in s["matches"] if m["video"] == vid]
    if t is None or not ms:
        return ms[0] if ms else None
    best = None
    for m in ms:
        a = t - m["offset"]
        length = arm_length(m)
        if a >= -1 and (length is None or a <= length + 1):
            best = m
    return best or ms[0]


def arm_length(m: dict) -> float | None:
    try:
        idx = blackbox.index_bbl(m["bbl"])
    except FileNotFoundError:
        return None
    for a in idx["arms"]:
        if a["index"] == m["arm"]:
            return a["length"]
    return None


def overview(session: str, with_arms: bool = True, cached_only: bool = False) -> dict:
    """The session plus the arm index of its blackbox files and coverage (which arms have a clip).
    With cached_only, blackbox files not decoded yet are listed in `arms_pending` instead of decoded here."""
    s = load(session)
    out = dict(s)
    if with_arms:
        arms = []
        out["arms_pending"] = []
        out["boots"] = {}
        for b in s["blackbox"]:
            try:
                idx = blackbox.index_bbl(b, cached_only=cached_only)
            except FileNotFoundError:
                continue
            if idx is None:
                out["arms_pending"].append(b)
                continue
            out["boots"][idx["file"]] = [{k: bt[k] for k in ("first", "last", "arms", "length")} for bt in idx.get("boots", [])]
            for a in idx["arms"]:
                matched = [m for m in s["matches"] if m["bbl"] == idx["file"] and m["arm"] == a["index"]]
                arms.append({"bbl": idx["file"], **{k: a.get(k) for k in ("index", "length", "rate", "vbat_start",
                             "vbat_end", "vbat_min", "motors_spun", "disarm_reason", "complete", "frames", "error")},
                             "videos": [{"video": m["video"], "offset": m["offset"]} for m in matched]})
        out["arms"] = arms
    return out


# ---- the folder scan ----------------------------------------------------------------------------------------------

def scan() -> list[dict]:
    """Register every session folder in the database (no probing). Returns the session list."""
    if remote():
        return store().scan()
    for d in session_dirs():
        store().ensure_session(d.name, d.name[:10])
    return store().sessions()


def refresh(session: str, probe_videos: bool = True) -> dict:
    """Bring a session in line with its folder: new clips are added (and probed for duration/codec), clips whose
    file is gone are marked `missing_since` (never removed: purge() does that), clips that are back are unmarked,
    and the blackbox files of that date are attached when none are. A folder that cannot be listed, or lists no
    clip at all, marks nothing: an unmounted or half-synced Drive must not look like a deletion."""
    if remote():
        return store().refresh(session, probe_videos)
    _check_name(session)
    db = store()
    d = VIDEOS_DIR / session
    if not d.is_dir() and not db.exists(session):
        # nothing to bring in line: a session is not made from a name alone (a probe still running for a session
        # merged or renamed away meanwhile, a page asking for the old name, brought it back empty)
        raise FileNotFoundError(f"{session}: no such session")
    db.ensure_session(session, session[:10])
    s = db.load(session)
    present = clip_files(d) if d.is_dir() else None
    known = {v["file"]: v for v in s["videos"]}
    if present:
        for f in present:
            if f not in known:
                db.upsert_video(session, f)
                known[f] = {"file": f, "duration": None, "codec": None}
    for f, v in known.items():
        if present is None or not present:
            continue                                    # folder unreadable or empty: leave every mark as it is
        if f in present:
            if v.get("missing_since"):
                db.upsert_video(session, f, missing_since=None)
                print(f"{f}: back", file=sys.stderr)
        elif not v.get("missing_since"):
            db.upsert_video(session, f, missing_since=now())
            print(f"{f}: file gone, marked missing (purge removes it)", file=sys.stderr)
    if probe_videos and present:
        for f, v in known.items():
            if (v.get("duration") is None or v.get("codec") is None) and f in present:
                try:
                    info = video.probe(d / f)
                except subprocess.CalledProcessError:   # still syncing / half-written: try again next refresh
                    print(f"{f}: ffprobe failed (still syncing?), skipped", file=sys.stderr)
                    continue
                # an update, not an upsert: a merge or a move may have taken the clip (and the session) while ffprobe
                # ran, and the result must not make either again (#16)
                db.set_video_fields(session, f, duration=info["duration"], codec=info["codec"])
    if not s["blackbox"] and db.exists(session):
        out = db.detached(session)                      # taken out by hand: not back by their date
        db.set_blackbox(session, [b.name for b in blackbox.all_bbls() if blackbox.bbl_date(b) == session[:10] and b.stat().st_size and b.name not in out])
    return db.load(session)


def import_json(session: str | None = None) -> list[dict]:
    """Import the pre-database videos/<session>/session.json files (one session, or every one found)."""
    if remote():
        return store().import_json(session)
    if session:
        _check_name(session)
    paths = [session_path(session)] if session else [session_path(d.name) for d in session_dirs()]
    return [store().import_json(p) for p in paths if p.exists()]
