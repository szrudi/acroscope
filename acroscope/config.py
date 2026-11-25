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
# set by the server for a client's command (/api/cli): clips and logs by name only, never a path on this host
NAMES_ONLY = os.environ.get("ACROSCOPE_NAMES_ONLY") == "1"

VIDEOS_DIR = DATA_DIR / "videos"
BLACKBOX_DIR = DATA_DIR / "blackbox"

# Shares (#57): a moment published as a static bundle, shares/<token>/, for a static web server to serve on a domain
# of its own (never this server). The public base URL the links are printed with, and two optional commands run
# after a bundle is rendered and when a share is revoked (a push to a host or a bucket, and the matching delete):
# split like a shell line, {dir} and {token} filled in per argument, run without a shell. The host and the domain
# are the operator's (set in the environment), not this repo's.
SHARES_DIR = Path(os.environ.get("ACROSCOPE_SHARES") or DATA_DIR / "shares").expanduser()
SHARE_URL = os.environ.get("ACROSCOPE_SHARE_URL", "").strip().rstrip("/")
SHARE_PUSH = os.environ.get("ACROSCOPE_SHARE_PUSH", "").strip()
SHARE_REVOKE = os.environ.get("ACROSCOPE_SHARE_REVOKE", "").strip()

# The drop (#75): where a paired device's batch is staged until it is whole (a sibling of inbox/ on the same
# filesystem: the commit is one rename), the bytes all devices together may have staged, and the room the data dir
# keeps free of staged files.
DROP_DIR = DATA_DIR / "drop"
# Where a device reaches the drop when that is not where the player is: a host of its own in front of /drop/, so the
# player's host keeps its login on every path. It goes into the pairing code, and a request under that host's name
# is served the drop and nothing else: never the player's own address. The host is the operator's (set in the
# environment), not this repo's; unset, the code names the address the configuration page was opened at.
DROP_URL = os.environ.get("ACROSCOPE_DROP_URL", "").strip().rstrip("/")
DROP_QUOTA = int(os.environ.get("ACROSCOPE_DROP_QUOTA") or 50 << 30)
DROP_MIN_FREE = int(os.environ.get("ACROSCOPE_DROP_MIN_FREE") or 5 << 30)

# Encoding the player relies on (Chrome on Linux has no HEVC): H.264, keyframe every 0.5 s at 60 fps.
H264_ARGS = ["-c:v", "libx264", "-preset", "medium", "-crf", "23", "-pix_fmt", "yuv420p",
             "-g", "30", "-keyint_min", "30", "-c:a", "aac", "-b:a", "96k", "-movflags", "+faststart"]

# The tag names a new database is seeded with (db.SEED_TAGS has the categories and colours); only for help text.
TAGS = ["flip", "roll", "powerloop", "split-s", "dive", "orbit", "milestone", "crash", "gyro-kick", "motor-loss", "poi"]


def cache(*parts: str) -> Path:
    p = CACHE_DIR.joinpath(*parts)
    p.parent.mkdir(parents=True, exist_ok=True)
    return p
