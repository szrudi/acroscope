"""What the server runs off the request threads. A client's CLI command (only the PROXIED ones, names checked: a
client names sessions, clips and logs, never a path here) runs in a child process, at once or as a job the client
polls. Decoding and the OSD reader are pure Python for seconds, so they run in a child too, never in a thread that
would hold the GIL against every request; ffprobe is a subprocess, so a thread is fine. And the background: the data
dir walked every SCAN_EVERY s, complete inbox batches ingested every INBOX_EVERY s, housekeeping once a day.
"""
import os
import subprocess
import sys
import threading
import time
import uuid

from .. import ingest
from ..sources import blackbox
from ..store import sessions


_busy: set[str] = set()          # blackbox files being decoded / sessions being probed right now
_busy_lock = threading.Lock()
SCAN_EVERY = 300                 # seconds between background walks of the data dir
# CLI commands a client may run here (they need the clips, the logs or the cache, which only the server has);
# each runs in a child process, so the reader's pure-Python work never holds this process's GIL
PROXIED = {"arms", "decode", "metrics", "events", "tricks", "frame", "sheet", "osd", "automatch", "tags", "sessions", "moments", "inbox", "match", "attach", "detach", "tag", "recut",
           "share", "unshare", "shares", "osd-profile"}        # osd-profile: `read` only (_check_proxied)
INBOX_EVERY = 20                 # seconds between looks at inbox/ for a complete batch


def _background(key: str, fn):
    """Run fn once in a thread, keyed so repeated requests don't start it twice."""
    with _busy_lock:
        if key in _busy:
            return
        _busy.add(key)

    def run():
        try:
            fn()
        except Exception as e:  # noqa: BLE001
            print(f"background {key}: {type(e).__name__}: {e}")
        finally:
            with _busy_lock:
                _busy.discard(key)
    threading.Thread(target=run, name=key, daemon=True).start()


def _cli(*args: str):
    """Run an acroscope CLI command in a child process. Decoding is pure Python and would hold the GIL for
    30 s per file inside this server; a child process keeps requests responsive and uses another core."""
    env = dict(os.environ, ACROSCOPE_URL="")      # the child writes the database directly, never through this server
    subprocess.run([sys.executable, "-m", "acroscope.cli", *args], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)


FREE_TEXT = {"title", "note", "tags", "text"}   # a client's command may say anything here; every other value names something


def _check_proxied(args: list[str]) -> None:
    """Only PROXIED commands, and only names (sessions, clips, logs, times) where a value names something: a path
    would let a client point ffmpeg or the decoder at any file on this host. Free text (a moment's title, a note,
    tags) may hold a '/' ("725 deg/s", #32): the command is parsed with the CLI's own parser to tell which is which."""
    if not args or args[0] not in PROXIED:
        raise ValueError(f"not a command a client may run here: {args[:1]}")
    from .. import cli
    try:
        ns = cli.parser().parse_args(args)
    except SystemExit:                                   # argparse's own error (or --help): not a command to run
        raise ValueError(f"not a valid command: {' '.join(args)}") from None
    for k, v in vars(ns).items():
        if k in FREE_TEXT:
            continue
        for x in v if isinstance(v, list) else [v]:
            if isinstance(x, str) and "/" in x:
                raise ValueError("paths are not accepted here: name the session, the clip and the log instead")
    if args[0] == "tricks" and len(args) > 1 and args[1] in ("export", "import"):
        raise ValueError("tricks export/import read and write a file on the client: not through the server")
    if args[0] == "osd-profile" and getattr(ns, "what", None) != "read":
        raise ValueError("osd-profile: only `read` runs here (a profile is made from a dump and a font on the client)")


def run_cli(args: list[str], timeout: float = 1800) -> dict:
    """A client's CLI command, run here: {stdout, stderr, code}."""
    _check_proxied(args)
    env = dict(os.environ, ACROSCOPE_URL="", ACROSCOPE_NAMES_ONLY="1")   # and a bare name never means a file in our cwd
    r = subprocess.run([sys.executable, "-m", "acroscope.cli", *args], capture_output=True, text=True, env=env, timeout=timeout)
    return {"stdout": r.stdout, "stderr": r.stderr, "code": r.returncode}


# A client's command as a job: POST /api/cli with "async" answers {job} at once, GET /api/cli/<job> gives {done}
# until it has finished, then the result (once). An automatch over a whole session takes longer than any HTTP
# read timeout the client can sensibly wait on (#9). The child is a subprocess, so a thread is fine.
_jobs: dict[str, dict] = {}
_jobs_lock = threading.Lock()
JOB_TIMEOUT = 3600               # seconds a job's child may run
JOB_KEEP = 3600                  # seconds a finished job nobody fetched is kept


def start_job(args: list[str]) -> str:
    """A new job, or the one already running the very same command (a player reloaded during its 🎯 presses it
    again: two `automatch --write` children on one session would race each other's matches)."""
    _check_proxied(args)
    jid = uuid.uuid4().hex[:12]
    now = time.time()
    with _jobs_lock:
        for k in [k for k, j in _jobs.items() if j["done"] and now - j["finished"] > JOB_KEEP]:
            del _jobs[k]
        same = next((k for k, j in _jobs.items() if not j["done"] and j["args"] == args), None)
        if same:
            return same
        _jobs[jid] = {"done": False, "args": args, "started": now}

    def run():
        try:
            r = run_cli(args, timeout=JOB_TIMEOUT)
        except Exception as e:  # noqa: BLE001  (the child timed out, ...)
            r = {"stdout": "", "stderr": f"{type(e).__name__}: {e}\n", "code": 1}
        with _jobs_lock:
            _jobs[jid] = {"done": True, "finished": time.time(), **r}
    threading.Thread(target=run, name=f"job:{jid}", daemon=True).start()
    return jid


def job(jid: str) -> dict | None:
    """{done: False, seconds} while running; the result once done (and the job is forgotten); None if unknown."""
    with _jobs_lock:
        j = _jobs.get(jid)
        if j is None:
            return None
        if not j["done"]:
            return {"done": False, "seconds": round(time.time() - j["started"])}
        del _jobs[jid]
    return {"done": True, "stdout": j["stdout"], "stderr": j["stderr"], "code": j["code"]}


def decode_async(bbl: str):
    _background(f"decode:{bbl}", lambda: _cli("arms", bbl))


def log_state(bbl: str) -> str:
    """"ready" when the log's arms are in the cache, "gone" when the log file is, else "decoding": the decode starts
    in a child, as a request thread must never decode (30 s with the GIL held freezes every other request)."""
    try:
        if blackbox.index_bbl(bbl, cached_only=True) is not None:
            return "ready"
    except FileNotFoundError:            # nothing to show, nothing to decode
        return "gone"
    decode_async(bbl)
    return "decoding"


def indexed(bbl: str) -> bool:
    return log_state(bbl) == "ready"


def probe_async(session: str):
    # ffprobe is a subprocess, so a thread is fine here (unlike decoding)
    _background(f"probe:{session}", lambda: sessions.refresh(session))


def scan_all(decode: bool = True) -> None:
    """Walk the data dir: register new session folders, refresh every session (new clips probed, gone clips marked
    missing), and decode blackbox files that are not in the cache yet (one child process at a time)."""
    if not sessions.session_dirs():
        print("scan: data dir not mounted, nothing to do", flush=True)
        return
    for row in sessions.scan():
        try:
            s = sessions.refresh(row["session"])
        except Exception as e:  # noqa: BLE001
            print(f"scan {row['session']}: {type(e).__name__}: {e}", flush=True)
            continue
        if decode:
            for b in s["blackbox"]:
                if blackbox.index_bbl(b, cached_only=True) is None:
                    _cli("arms", b)


def first_start() -> None:
    """An empty database next to a data dir with the old session.json files: import them once."""
    if not sessions.remote() and not sessions.store().sessions():
        done = sessions.import_json()
        if done:
            print(f"imported {len(done)} session file(s) into {sessions.store()}", flush=True)


def ingest_pending() -> None:
    """One batch at a time, in a child process (ffmpeg, the decoder and the OSD reader live there)."""
    for b in ingest.pending():
        print(f"ingest {b}", flush=True)
        _cli("ingest", b)


def background_scans():
    """Every INBOX_EVERY s: ingest complete batches. Every SCAN_EVERY s: walk the data dir. Once a day: originals
    past retention go, the database is backed up into the data dir."""
    def run():
        first_start()
        last_scan, last_day = 0.0, None
        while True:
            if time.time() - last_scan >= SCAN_EVERY:
                t = time.time()
                scan_all()
                print(f"scan done in {time.time() - t:.0f} s", flush=True)
                last_scan = time.time()
            try:
                ingest_pending()
            except Exception as e:  # noqa: BLE001
                print(f"ingest: {type(e).__name__}: {e}", flush=True)
            day = time.strftime("%Y-%m-%d")
            if day != last_day:
                _cli("housekeeping")
                last_day = day
            time.sleep(INBOX_EVERY)
    threading.Thread(target=run, name="scan", daemon=True).start()
