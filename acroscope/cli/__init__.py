"""acroscope command line: the agent side. Every command prints JSON unless it is a table for humans.

Times: video seconds or m:ss.s ('1:32.7'). Sessions and clips can be given by a unique fragment ('schammer', '007').
Matches, moments and notes live in the server's database: with ACROSCOPE_URL set, this CLI is a client of that
server; without it, it opens the local database (ACROSCOPE_DB) itself.
"""
import argparse
import sys

from . import analyse, ingest, match, review, share, sources, store
from .. import tricks
from ..sources import osd, osdprofile
from ..store import sessions
from ..config import CACHE_DIR, DATA_DIR, DB_PATH, SERVER_URL
from ..store.remote import RemoteError


def _layer_from_store():
    row = sessions.store().setting(tricks.LAYER_KEY)
    return (row["value"], row["updated_at"]) if row else (None, None)


def _profiles_from_store():
    row = sessions.store().setting(osdprofile.PROFILES_KEY)
    return (row["value"], row["updated_at"]) if row else (None, None)


def cmd_serve(a):
    from ..server import serve
    serve(a.host, a.port, warm=not a.no_warm)


# with ACROSCOPE_URL set these run on the server (they need the clips, the logs or the cache); frames and sheets
# come back as files into the local cache under the same relative path
PROXIED = {"arms", "decode", "metrics", "events", "tricks", "frame", "sheet", "osd", "automatch", "inbox", "sessions", "match", "attach", "detach", "tag", "recut",
           "share", "unshare", "shares", "osd-profile"}


def _proxy(argv: list[str]) -> None:
    r = sessions.store().cli(argv)
    if r["stderr"]:
        print(r["stderr"], end="", file=sys.stderr)
    if argv[0] in ("frame", "sheet") and r["code"] == 0:
        for line in r["stdout"].splitlines():
            if not line.strip():
                continue
            rel = line.strip().split("/.cache/acroscope/", 1)[-1] if "/.cache/acroscope/" in line else line.strip().split("/cache/", 1)[-1]
            dest = CACHE_DIR / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            sessions.store().fetch(line.strip(), dest)
            print(dest)
    else:
        print(r["stdout"], end="")
    sys.exit(r["code"])


def parser() -> argparse.ArgumentParser:
    """The CLI's arguments; the server parses a client's command with it too, to tell names from free text."""
    ap = argparse.ArgumentParser(prog="acroscope", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    class Version(argparse.Action):   # the store is opened only when asked: `--help` must not create a database
        def __call__(self, parser, ns, values, option_string=None):
            store = f"server {SERVER_URL}" if SERVER_URL else f"sqlite {DB_PATH}"      # named, not opened
            print(f"acroscope (data {DATA_DIR}, cache {CACHE_DIR}, store {store})"); parser.exit()
    ap.add_argument("--version", action=Version, nargs=0)
    sp = ap.add_subparsers(dest="cmd", required=True)

    for group in (store, sources, ingest, match, analyse, review, share):
        group.add(sp)
    p = sp.add_parser("serve", help="run the player (http)"); p.add_argument("--host", default="0.0.0.0"); p.add_argument("--port", type=int, default=8070)
    p.add_argument("--no-warm", action="store_true", help="don't decode/probe everything in the background at start"); p.set_defaults(f=cmd_serve)
    return ap


def main(argv=None):
    a = parser().parse_args(argv)
    args = list(sys.argv[1:] if argv is None else argv)
    if a.cmd in PROXIED and sessions.remote() and not (a.cmd == "tricks" and a.what in ("export", "import")) \
            and not (a.cmd == "osd-profile" and a.what != "read"):     # a profile is made from a local file; only reading needs the clips
        return _proxy(args)
    if not sessions.remote() and tricks.LAYER_SOURCE is None:          # the local database's layer for every command that detects
        tricks.LAYER_SOURCE = _layer_from_store
    if not sessions.remote() and osd.PROFILE_SOURCE is None:           # and its OSD profiles for every command that reads the OSD
        osd.PROFILE_SOURCE = _profiles_from_store
    try:
        a.f(a)
    except (FileNotFoundError, ValueError, RemoteError) as e:   # a name that is not there, a refused edit: one line
        print(f"acroscope: {e}", file=sys.stderr)
        sys.exit(1)
