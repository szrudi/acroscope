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

from . import blackbox, video
from .config import DATA_DIR, DB_PATH, SERVER_URL, VIDEOS_DIR
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
    vids = load(session)["videos"]
    by_file = {v["file"]: v for v in vids}
    if file in by_file:
        return file
    if Path(file).name in by_file:
        return Path(file).name
    hits = [v["file"] for v in vids if v.get("name") == file or Path(v["file"]).stem == file]
    if file.isdigit():
        hits += [v["file"] for v in vids if (v.get("name") or "").endswith(f"_{int(file):03d}")]
    if len(set(hits)) == 1:
        return hits[0]
    if len(set(hits)) > 1:
        raise FileNotFoundError(f"{file}: {len(set(hits))} clips match (need exactly 1)")
    return video.resolve_video(session, file).name     # a file not registered yet, or a FileNotFoundError


def clip_path(session: str, file: str) -> Path:
    """The clip's file on disk, from anything resolve_clip takes (an ingested clip's number or readable name is
    only known to the database, not to the folder), or a path as given."""
    p = Path(file).expanduser()
    if p.is_file():
        return p
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
                             "vbat_end", "vbat_min", "motors_spun", "disarm_reason", "complete")},
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
                db.upsert_video(session, f, info["duration"], info["codec"])
    if not s["blackbox"]:
        db.set_blackbox(session, [b.name for b in blackbox.all_bbls() if blackbox.bbl_date(b) == session[:10] and b.stat().st_size])
    return db.load(session)


# ---- writing ------------------------------------------------------------------------------------------------------

def set_match(session: str, vid: str, bbl: str, arm: int, offset: float, note: str = "", boot: bool = False) -> dict:
    """Record video <-> arm with `offset` (video = arm time + offset). With boot=True, also record every other arm
    of the same power cycle, their offsets derived from the FC uptime (video = uptime + boot offset)."""
    vid = resolve_clip(session, vid)
    bbl_name = blackbox.resolve_bbl(bbl).name
    new = [(arm, round(offset, 2), note)]
    if boot:
        idx = blackbox.index_bbl(bbl_name)
        meta = {a["index"]: a for a in idx["arms"]}
        b = next((b for b in idx.get("boots", []) if arm in b["arms"]), None)
        if b:
            boot_offset = offset - meta[arm]["uptime_start"]
            dur = next((v.get("duration") for v in load(session)["videos"] if v["file"] == vid), None)
            new = []
            for i in b["arms"]:
                off = round(boot_offset + meta[i]["uptime_start"], 2)
                # an arm of the boot that lies outside this clip belongs to another clip (or to none): leave it
                if i != arm and dur and (off > dur or off + meta[i]["length"] < 0):
                    continue
                new.append((i, off, note if i == arm else f"derived from arm {arm} via uptime (boot {b['first']}-{b['last']})"))
    for i, off, n in new:
        store().set_match(session, vid, bbl_name, i, off, n)
    return load(session)


def unmatch(session: str, vid: str, arm: int | None = None) -> int:
    """Remove the matches of a clip (all, or one arm). Returns how many were removed."""
    return store().unmatch(session, resolve_clip(session, vid), arm)


def tag(session: str, vid: str, start: float, end: float, title: str, tags: list[str], note: str = "",
        metrics: dict | None = None, mid: str | None = None) -> dict:
    """Add a moment, or replace the one with id `mid`."""
    return store().tag(session, resolve_clip(session, vid), start, end, title, tags, note, metrics, mid)


def untag(session: str, mid: str) -> bool:
    return store().untag(session, mid)


def set_note(session: str, note: str, vid: str | None = None) -> None:
    store().set_note(session, note, resolve_clip(session, vid) if vid else None)


def attach(session: str, bbl: str) -> list[str]:
    return store().attach(session, blackbox.resolve_bbl(bbl).name)


def detach(session: str, bbl: str) -> list[str]:
    return store().detach(session, blackbox.resolve_bbl(bbl).name)


def purge(session: str, vid: str | None = None, days: float = 0) -> list[dict]:
    return store().purge(session, resolve_clip(session, vid) if vid else None, days)


# ---- editing sessions (folders and rows move together) -------------------------------------------------------------

SESSION_RE = re.compile(r"^\d{4}-\d{2}-\d{2}(-[A-Za-z0-9_-]+)?$")


def _mounted() -> None:
    """Folders and rows move together: with the data dir unmounted, an edit would move rows only."""
    if not session_dirs():
        raise ValueError("data dir not mounted (no session folders): nothing is moved")


def _move_dir(old: Path, new: Path) -> None:
    if old.is_dir():
        if new.exists():
            raise ValueError(f"{new} exists")
        new.parent.mkdir(parents=True, exist_ok=True)
        old.rename(new)


def rename_session(session: str, new: str) -> dict:
    """videos/<session> and originals/<session> become <new>; every row follows. The date is the new name's."""
    if remote():
        return store().rename_session(session, new)
    _check_name(session)
    _mounted()
    if not SESSION_RE.match(new):
        raise ValueError(f"{new}: a session is named YYYY-MM-DD or YYYY-MM-DD-<name>")
    if (VIDEOS_DIR / new).exists() or (VIDEOS_DIR.parent / "originals" / new).exists() or store().exists(new):
        raise ValueError(f"{new} exists")
    _move_dir(VIDEOS_DIR / session, VIDEOS_DIR / new)
    _move_dir(VIDEOS_DIR.parent / "originals" / session, VIDEOS_DIR.parent / "originals" / new)
    store().rename_session(session, new)
    return load(new)


def set_date(session: str, date: str) -> dict:
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
        raise ValueError(f"{date}: not a date")
    rest = session[10:] if SESSION_RE.match(session) else "-" + session
    return rename_session(session, date + rest)


def move_clip(session: str, vid: str, to: str) -> dict:
    """A clip, its original, its matches and its moments go to session `to` (created if new)."""
    if remote():
        return store().move_clip(session, vid, to)
    _check_name(session)
    _mounted()
    if not SESSION_RE.match(to):
        raise ValueError(f"{to}: a session is named YYYY-MM-DD or YYYY-MM-DD-<name>")
    file = resolve_clip(session, vid)
    v = next((x for x in load(session)["videos"] if x["file"] == file), None)
    src, dst = VIDEOS_DIR / session / file, VIDEOS_DIR / to / file
    if src.exists():
        if dst.exists():
            raise ValueError(f"{dst} exists")
        dst.parent.mkdir(parents=True, exist_ok=True)
        src.rename(dst)
    if v and v.get("original"):
        o = DATA_DIR / v["original"]
        if o.exists():
            no = DATA_DIR / "originals" / to / o.name
            no.parent.mkdir(parents=True, exist_ok=True)
            o.rename(no)
            store().set_video_fields(session, file, original=str(no.relative_to(DATA_DIR)))
    store().move_clip(session, file, to)
    return load(to)


def merge_sessions(session: str, into: str) -> dict:
    """Every clip and log of `session` goes to `into`; the emptied session is removed."""
    if remote():
        return store().merge_sessions(session, into)
    _check_name(session)
    _mounted()
    if into == session:
        raise ValueError(f"{session}: cannot merge a session into itself")
    s = load(session)
    for v in s["videos"]:
        move_clip(session, v["file"], into)
    for b in s["blackbox"]:
        store().attach(into, b)
    if s.get("note"):
        t = load(into).get("note", "")
        store().set_note(into, (t + "\n" + s["note"]).strip())
    store().delete_session(session)
    for d in (VIDEOS_DIR / session, VIDEOS_DIR.parent / "originals" / session):
        if d.is_dir() and not any(d.iterdir()):
            d.rmdir()
    return load(into)


def import_json(session: str | None = None) -> list[dict]:
    """Import the pre-database videos/<session>/session.json files (one session, or every one found)."""
    if remote():
        return store().import_json(session)
    paths = [session_path(session)] if session else [session_path(d.name) for d in session_dirs()]
    return [store().import_json(p) for p in paths if p.exists()]
