"""Editing sessions: rename one, change its date, move a clip to another, merge two, delete a clip, purge the clips
marked missing. The folders under videos/ and the database rows move together.
"""
import re
from pathlib import Path

from . import sessions
from ..config import DATA_DIR, VIDEOS_DIR
from .db import now


def purge(session: str, vid: str | None = None, days: float = 0) -> list[dict]:
    return sessions.store().purge(session, sessions.resolve_clip(session, vid) if vid else None, days)


# ---- editing sessions (folders and rows move together) -------------------------------------------------------------

SESSION_RE = re.compile(r"^\d{4}-\d{2}-\d{2}(-[A-Za-z0-9_-]+)?$")


def _mounted() -> None:
    """Folders and rows move together: with the data dir unmounted, an edit would move rows only."""
    if not sessions.session_dirs():
        raise ValueError("data dir not mounted (no session folders): nothing is moved")


def _move_dir(old: Path, new: Path) -> None:
    if old.is_dir():
        if new.exists():
            raise ValueError(f"{new} exists")
        new.parent.mkdir(parents=True, exist_ok=True)
        old.rename(new)


def rename_session(session: str, new: str) -> dict:
    """videos/<session> and originals/<session> become <new>; every row follows. The date is the new name's."""
    if sessions.remote():
        return sessions.store().rename_session(session, new)
    sessions._check_name(session)
    _mounted()
    if not SESSION_RE.match(new):
        raise ValueError(f"{new}: a session is named YYYY-MM-DD or YYYY-MM-DD-<name>")
    if (VIDEOS_DIR / new).exists() or (VIDEOS_DIR.parent / "originals" / new).exists() or sessions.store().exists(new):
        raise ValueError(f"{new} exists")
    _move_dir(VIDEOS_DIR / session, VIDEOS_DIR / new)
    _move_dir(VIDEOS_DIR.parent / "originals" / session, VIDEOS_DIR.parent / "originals" / new)
    sessions.store().rename_session(session, new)
    return sessions.load(new)


def set_date(session: str, date: str) -> dict:
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
        raise ValueError(f"{date}: not a date")
    rest = session[10:] if SESSION_RE.match(session) else "-" + session
    return rename_session(session, date + rest)


def move_clip(session: str, vid: str, to: str) -> dict:
    """A clip, its original, its matches and its moments go to session `to` (created if new)."""
    if sessions.remote():
        return sessions.store().move_clip(session, vid, to)
    sessions._check_name(session)
    _mounted()
    if not SESSION_RE.match(to):
        raise ValueError(f"{to}: a session is named YYYY-MM-DD or YYYY-MM-DD-<name>")
    file = sessions.resolve_clip(session, vid)
    v = next((x for x in sessions.load(session)["videos"] if x["file"] == file), None) or {"file": file}
    _check_moves(session, [v], to)
    _move_clip(session, v, to)
    return sessions.load(to)


def _check_moves(session: str, clips: list[dict], to: str) -> None:
    """Refuse before anything moves: a clip whose file, original or row is already in `to` (a rename onto an
    existing file replaces it without a word, and the row would clash halfway). Names every clash at once."""
    there = {v["file"] for v in sessions.load(to)["videos"]}
    clash = []
    for v in clips:
        if (VIDEOS_DIR / session / v["file"]).exists() and (VIDEOS_DIR / to / v["file"]).exists():
            clash.append(str(VIDEOS_DIR / to / v["file"]) + " exists")
        elif v["file"] in there:
            clash.append(f"{v['file']} is in {to} already")
        if v.get("original") and (DATA_DIR / v["original"]).exists() and (DATA_DIR / "originals" / to / Path(v["original"]).name).exists():
            clash.append(f"originals/{to}/{Path(v['original']).name} exists")
    if clash:
        raise ValueError("nothing moved: " + "; ".join(clash))


def _move_clip(session: str, v: dict, to: str) -> None:
    file = v["file"]
    src, dst = VIDEOS_DIR / session / file, VIDEOS_DIR / to / file
    if src.exists():
        dst.parent.mkdir(parents=True, exist_ok=True)
        src.rename(dst)
    if v.get("original"):
        o = DATA_DIR / v["original"]
        if o.exists():
            no = DATA_DIR / "originals" / to / o.name
            no.parent.mkdir(parents=True, exist_ok=True)
            o.rename(no)
            sessions.store().set_video_fields(session, file, original=str(no.relative_to(DATA_DIR)))
    sessions.store().move_clip(session, file, to)


def delete_clip(session: str, vid: str) -> dict:
    """A clip goes (a no-signal recording, a test): its file and its original into trash/<today>/<session>/, where
    housekeeping empties a day's folder after TRASH_DAYS (until then: move the file back and refresh), and its row
    with its matches and moments out of the database. {video, trash, matches, moments}."""
    if sessions.remote():
        return sessions.store().delete_clip(session, vid)
    file = sessions.resolve_clip(session, vid)
    v = next((x for x in sessions.load(session)["videos"] if x["file"] == file), None)
    if v is None:
        raise ValueError(f"{session}: no clip {vid}")
    trash = DATA_DIR / "trash" / now()[:10] / session
    dst = None
    for src in [VIDEOS_DIR / session / file] + ([DATA_DIR / v["original"]] if v.get("original") else []):
        if src.is_file():
            t = trash / ("originals" if src.parent.parent.name == "originals" else "") / src.name
            if t.exists():
                raise ValueError(f"{t} exists")
            t.parent.mkdir(parents=True, exist_ok=True)
            src.rename(t)
            dst = dst or t
    out = sessions.store().delete_clip(session, file)
    return {**out, "trash": str(dst.relative_to(DATA_DIR)) if dst else None}


def merge_sessions(session: str, into: str) -> dict:
    """Every clip and log of `session` goes to `into`; the emptied session is removed."""
    if sessions.remote():
        return sessions.store().merge_sessions(session, into)
    sessions._check_name(session)
    _mounted()
    if into == session:
        raise ValueError(f"{session}: cannot merge a session into itself")
    if not SESSION_RE.match(into):
        raise ValueError(f"{into}: a session is named YYYY-MM-DD or YYYY-MM-DD-<name>")
    s = sessions.load(session)
    _check_moves(session, s["videos"], into)            # all or nothing: a clash at clip 5 must not leave 1-4 moved
    for v in s["videos"]:
        _move_clip(session, v, into)
    for b in s["blackbox"]:
        sessions.store().attach(into, b)
    sessions.store().carry_detached(session, into)
    if s.get("note"):
        t = sessions.load(into).get("note", "")
        sessions.store().set_note(into, (t + "\n" + s["note"]).strip())
    sessions.store().delete_session(session)
    for d in (VIDEOS_DIR / session, VIDEOS_DIR.parent / "originals" / session):
        if d.is_dir() and not any(d.iterdir()):
            d.rmdir()
    return sessions.load(into)
