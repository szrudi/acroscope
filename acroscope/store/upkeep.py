"""Keeping the data dir tidy (`acroscope housekeeping`): a deleted clip's trash/<date>/ emptied after TRASH_DAYS, and
the database copied into state/ so clips and index restore from one snapshot.
"""
import shutil
from datetime import datetime
from pathlib import Path

from . import sessions
from .. import ingest
from ..config import DATA_DIR


STATE_DIR = DATA_DIR / "state"
TRASH_DAYS = 7             # a deleted clip waits this long in trash/<date>/ (sessions.delete_clip)


def cleanup_trash(root: Path | None = None, today: str | None = None, days: int = TRASH_DAYS, log=print) -> list[str]:
    """Delete the trash/<date>/ folders older than `days` (what sessions.delete_clip put there). Returns the dates."""
    root = root or DATA_DIR / "trash"
    today_d = datetime.fromisoformat(today).date() if today else ingest.now().date()
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
    dst = STATE_DIR / f"acroscope-{ingest.now().strftime('%Y-%m-%d')}.db"
    db.backup_to(dst)
    old = sorted(STATE_DIR.glob("acroscope-*.db"))[:-keep]
    for p in old:
        p.unlink()
    return dst
