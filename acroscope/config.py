"""Where the data, the database and the cache live.

The data dir is the Google Drive "FPV drone" folder (videos/, blackbox/): clips and logs only. Matches, moments and
notes live in a SQLite database owned by the server (ACROSCOPE_DB). A CLI on another machine talks to the server
instead (ACROSCOPE_URL). The cache holds decoded arms, extracted frames and H.264 proxies, and is never synced.
"""
import os
from pathlib import Path

DATA_DIR = Path(os.environ.get("ACROSCOPE_DATA", "~/gdrive/fpv")).expanduser()
CACHE_DIR = Path(os.environ.get("ACROSCOPE_CACHE", "~/.cache/acroscope")).expanduser()
DB_PATH = Path(os.environ.get("ACROSCOPE_DB", "~/.local/share/acroscope/acroscope.db")).expanduser()


def _server_url() -> str:
    """ACROSCOPE_URL, else the one line of ~/.config/acroscope/url (the durable way to point a machine's CLI at
    the server: shells started by an agent do not carry exported variables). Empty = the local database."""
    env = os.environ.get("ACROSCOPE_URL")
    if env is not None:
        return env.rstrip("/")
    f = Path("~/.config/acroscope/url").expanduser()
    return f.read_text().strip().rstrip("/") if f.is_file() else ""


SERVER_URL = _server_url()   # set: the CLI is a client of that server's database

VIDEOS_DIR = DATA_DIR / "videos"
BLACKBOX_DIR = DATA_DIR / "blackbox"

# Encoding the player relies on (Chrome on Linux has no HEVC): H.264, keyframe every 0.5 s at 60 fps.
H264_ARGS = ["-c:v", "libx264", "-preset", "medium", "-crf", "23", "-pix_fmt", "yuv420p",
             "-g", "30", "-keyint_min", "30", "-c:a", "aac", "-b:a", "96k", "-movflags", "+faststart"]

# The tag names a new database is seeded with (db.SEED_TAGS has the categories and colours); only for help text.
TAGS = ["flip", "roll", "powerloop", "split-s", "dive", "orbit", "milestone", "crash", "gyro-kick", "motor-loss", "poi"]


def cache(*parts: str) -> Path:
    p = CACHE_DIR.joinpath(*parts)
    p.parent.mkdir(parents=True, exist_ok=True)
    return p
