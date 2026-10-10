"""The player and the API: a stdlib HTTP server for one HTML page, JSON endpoints over the database, and the clips
as static files with Range support (seeking needs it). The server owns the database; the CLI on another machine
uses these endpoints (remote.Remote). Writes go through the same functions as the CLI.

The data dir is only walked in the background (a scan at start and every SCAN_EVERY seconds, and on request): a
page open never waits on the Drive mount."""
import json
import mimetypes
import os
import re
import subprocess
import sys
import threading
import time
from functools import lru_cache
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from . import blackbox, events as ev, ingest, metrics, sessions, video
from .config import CACHE_DIR, VIDEOS_DIR

STATIC = Path(__file__).parent / "static"
_lock = threading.Lock()
_busy: set[str] = set()          # blackbox files being decoded / sessions being probed right now
_busy_lock = threading.Lock()
SCAN_EVERY = 300                 # seconds between background walks of the data dir
# CLI commands a client may run here (they need the clips, the logs or the cache, which only the server has);
# each runs in a child process, so the reader's pure-Python work never holds this process's GIL
PROXIED = {"arms", "decode", "metrics", "events", "frame", "sheet", "osd", "automatch", "tags", "sessions", "moments", "inbox"}
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


def run_cli(args: list[str]) -> dict:
    """A client's CLI command, run here: {stdout, stderr, code}. Only PROXIED commands, and only names (sessions,
    clips, logs, times): a path would let a client point ffmpeg or the decoder at any file on this host."""
    if not args or args[0] not in PROXIED:
        raise ValueError(f"not a command a client may run here: {args[:1]}")
    if any("/" in a for a in args[1:]):
        raise ValueError("paths are not accepted here: name the session, the clip and the log instead")
    env = dict(os.environ, ACROSCOPE_URL="")
    r = subprocess.run([sys.executable, "-m", "acroscope.cli", *args], capture_output=True, text=True, env=env, timeout=1800)
    return {"stdout": r.stdout, "stderr": r.stderr, "code": r.returncode}


def _decode_async(bbl: str):
    _background(f"decode:{bbl}", lambda: _cli("arms", bbl))


def _probe_async(session: str):
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


def _under(root: Path, *parts: str) -> Path:
    """root/parts, refused when it would leave root (a '..' or an encoded slash inside a path segment)."""
    p = root.joinpath(*parts)
    if not p.resolve().is_relative_to(root.resolve()):
        raise FileNotFoundError(f"{'/'.join(parts)}: not under {root.name}")
    return p


@lru_cache(maxsize=64)
def _events(bbl: str, arm: int) -> list[dict]:
    return ev.detect(blackbox.load_arm(bbl, arm))


@lru_cache(maxsize=16)
def _series(bbl: str, arm: int, step: float) -> dict:
    """Downsampled lanes for the strip under the video: per `step` s, throttle %, tilt deg, vbat V, |gyro| max."""
    a = blackbox.load_arm(bbl, arm)
    rc3, vb = a.col("rcCommand[3]"), a.col("vbatLatest")
    g = [a.col(f"gyroADC[{k}]") for k in range(3)]
    n = int(a.length / step) + 1
    rows = []
    for b in range(n):
        i0, i1 = a.window(b * step, (b + 1) * step)
        if i1 <= i0:
            rows.append(None)
            continue
        mid = (i0 + i1) // 2
        rows.append([round((rc3[mid] - 1000) / 10), round(a.tilt(mid)), round(vb[mid] / 100, 2),
                     round(max(max(abs(g[k][i]) for k in range(3)) for i in range(i0, i1, max(1, (i1 - i0) // 20))))])
    return {"step": step, "length": a.length, "fields": ["throttle", "tilt", "vbat", "gyro_max"], "rows": rows}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # quieter: only non-200s
        if args and str(args[1]) not in ("200", "206", "304"):
            super().log_message(fmt, *args)

    # ---- helpers
    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _file(self, path: Path, ctype=None, cache="no-store"):
        if not path.is_file():
            return self._json({"error": f"{path.name}: not found"}, 404)
        size = path.stat().st_size
        ctype = ctype or mimetypes.guess_type(str(path))[0] or "application/octet-stream"
        start, end = 0, size - 1
        rng = self.headers.get("Range")
        m = re.fullmatch(r"bytes=(\d*)-(\d*)", rng or "")
        if m and (m.group(1) or m.group(2)):
            if m.group(1):
                start = int(m.group(1))
                end = int(m.group(2)) if m.group(2) else size - 1
            else:  # suffix range
                start = max(0, size - int(m.group(2)))
            if start >= size:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            end = min(end, size - 1)
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        else:
            self.send_response(200)
        length = end - start + 1
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Cache-Control", cache)
        self.end_headers()
        if self.command == "HEAD":
            return
        with open(path, "rb") as f:
            f.seek(start)
            left = length
            while left > 0:
                chunk = f.read(min(1 << 20, left))
                if not chunk:
                    break
                try:
                    self.wfile.write(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    return
                left -= len(chunk)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    # ---- routes
    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        u = urlparse(self.path)
        parts = [unquote(p) for p in u.path.strip("/").split("/") if p]
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        try:
            if not parts:
                return self._file(STATIC / "index.html", "text/html; charset=utf-8")
            if parts[0] == "static" and len(parts) == 2:
                return self._file(_under(STATIC, parts[1]))
            if parts[0] == "video" and len(parts) == 3:
                return self._file(video.playable(_under(VIDEOS_DIR, parts[1], parts[2])), "video/mp4", "max-age=3600")
            if parts[0] == "frame" and len(parts) == 4:
                return self._file(video.frame(_under(VIDEOS_DIR, parts[1], parts[2]), float(parts[3])), "image/jpeg", "max-age=86400")
            if parts[:2] == ["api", "sessions"]:
                return self._json(sessions.all_sessions())             # the database: never the Drive mount
            if parts == ["api", "inbox"]:
                return self._json(ingest.list_inbox())
            if parts == ["api", "file"]:                      # a file the CLI made here (a frame, a sheet): cache only
                p = Path(q.get("path", "")).resolve()
                if not p.is_relative_to(CACHE_DIR.resolve()):
                    return self._json({"error": "not a cache file"}, 403)
                return self._file(p, cache="max-age=86400")
            if parts[:2] == ["api", "store"]:
                return self._json({"store": repr(sessions.store()), "data": str(VIDEOS_DIR.parent)})
            if parts == ["api", "tags"]:
                return self._json(sessions.store().tags())
            if parts == ["api", "tag-usage"]:
                return self._json(sessions.store().tag_usage(q.get("session")))
            if parts[:2] == ["api", "session"] and len(parts) >= 3:
                name = parts[2]
                if len(parts) == 3:
                    if not sessions.store().exists(name):
                        if name not in {d.name for d in sessions.session_dirs()}:   # a GET registers nothing
                            return self._json({"error": f"{name}: no such session"}, 404)
                        sessions.refresh(name, probe_videos=False)
                    o = sessions.overview(name, cached_only=True)
                    for b in o["arms_pending"]:
                        _decode_async(b)
                    for v in o["videos"]:
                        p = VIDEOS_DIR / name / v["file"]
                        v["playable"] = video.playable(p).name if p.exists() else None
                        if v["playable"] != v["file"]:
                            v["codec"] = "h264"          # the proxy
                        elif v.get("codec") is None and p.exists():
                            info = video.probe(p, cached_only=True)
                            v["codec"] = info["codec"] if info else None
                    if any(v.get("duration") is None or v.get("codec") is None for v in o["videos"]):
                        o["probe_pending"] = True
                        _probe_async(name)
                    return self._json(o)
                if parts[3] == "data":
                    return self._json(sessions.load(name))
                if parts[3] == "events":
                    s = sessions.load(name)
                    rows = []
                    for m in s["matches"]:
                        if q.get("video") and m["video"] != q["video"]:
                            continue
                        for e in _events(m["bbl"], m["arm"]):
                            rows.append({**e, "video": m["video"], "start": round(e["arm_from"] + m["offset"], 2), "end": round(e["arm_to"] + m["offset"], 2)})
                    return self._json(rows)
                if parts[3] == "series":
                    s = sessions.load(name)
                    out = []
                    for m in s["matches"]:
                        if m["video"] == q.get("video"):
                            out.append({"offset": m["offset"], "arm": m["arm"], "bbl": m["bbl"], **_series(m["bbl"], m["arm"], float(q.get("step", 0.1)))})
                    return self._json(out)
                if parts[3] == "metrics":
                    s = sessions.load(name)
                    t0, t1 = float(q["from"]), float(q["to"])
                    m = sessions.match_for(s, q["video"], t0)
                    if not m:
                        return self._json({"error": "no match"}, 404)
                    a = blackbox.load_arm(m["bbl"], m["arm"])
                    return self._json({"summary": metrics.summary(a, t0 - m["offset"], t1 - m["offset"]),
                                       "segments": metrics.rotation_segments(a, t0 - m["offset"], t1 - m["offset"])})
            return self._json({"error": "not found"}, 404)
        except FileNotFoundError as e:
            return self._json({"error": str(e)}, 404)
        except Exception as e:  # noqa: BLE001
            return self._json({"error": f"{type(e).__name__}: {e}"}, 500)

    def do_POST(self):
        parts = [unquote(p) for p in urlparse(self.path).path.strip("/").split("/") if p]
        try:
            if parts == ["api", "scan"]:
                return self._json(sessions.scan())
            if parts == ["api", "cli"]:
                return self._json(run_cli([str(x) for x in self._body().get("args", [])]))
            if parts == ["api", "import"]:
                return self._json(sessions.import_json(self._body().get("session")))
            if parts[:2] == ["api", "inbox"] and len(parts) == 4 and parts[3] == "retry":
                sessions.store().set_batch(parts[2], status="pending", error=None)
                return self._json({"ok": True})
            if parts == ["api", "tags"]:
                b = self._body()
                return self._json(sessions.store().set_tag(b["name"], b["category"], b.get("color")))
            if parts == ["api", "tag-categories"]:
                b = self._body()
                return self._json(sessions.store().set_category(b["name"], b.get("color")))
            if parts[:2] != ["api", "session"] or len(parts) != 4:
                return self._json({"error": "not found"}, 404)
            name, what, b = parts[2], parts[3], self._body()
            with _lock:
                if what == "moment":
                    return self._json(sessions.tag(name, b["video"], float(b["start"]), float(b["end"]), b.get("title", ""),
                                                   [t for t in b.get("tags", []) if t], b.get("note", ""), b.get("metrics"), b.get("id")))
                if what == "note":
                    sessions.set_note(name, b.get("note", ""), b.get("video"))
                    return self._json({"ok": True})
                if what == "match":
                    return self._json(sessions.store().set_match(name, b["video"], b["bbl"], int(b["arm"]), float(b["offset"]), b.get("note", "")))
                if what == "blackbox":
                    for f in b.get("add", []):
                        sessions.attach(name, f)
                    for f in b.get("remove", []):
                        sessions.detach(name, f)
                    return self._json(sessions.load(name)["blackbox"])
                if what == "purge":
                    return self._json(sessions.purge(name, b.get("video"), float(b.get("days") or 0)))
                if what == "rename":
                    return self._json(sessions.rename_session(name, b["name"]))
                if what == "date":
                    return self._json(sessions.set_date(name, b["date"]))
                if what == "move":
                    return self._json(sessions.move_clip(name, b["video"], b["to"]))
                if what == "merge":
                    return self._json(sessions.merge_sessions(name, b["into"]))
            if what == "refresh":                      # walks the mount: outside the lock, it can take a while
                return self._json(sessions.refresh(name, probe_videos=b.get("probe", True)))
            return self._json({"error": "not found"}, 404)
        except FileNotFoundError as e:
            return self._json({"error": str(e)}, 404)
        except (ValueError, KeyError) as e:
            return self._json({"error": f"{type(e).__name__}: {e}"}, 400)
        except Exception as e:  # noqa: BLE001
            return self._json({"error": f"{type(e).__name__}: {e}"}, 500)

    def do_DELETE(self):
        u = urlparse(self.path)
        parts = [unquote(p) for p in u.path.strip("/").split("/") if p]
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        try:
            if parts[:2] == ["api", "session"] and len(parts) == 5 and parts[3] == "moment":
                with _lock:
                    ok = sessions.untag(parts[2], parts[4])
                return self._json({"removed": ok})
            if parts[:2] == ["api", "session"] and len(parts) == 4 and parts[3] == "match":
                with _lock:
                    n = sessions.unmatch(parts[2], q["video"], int(q["arm"]) if q.get("arm") else None)
                return self._json({"removed": n})
            if parts[:2] == ["api", "tags"] and len(parts) == 3:
                return self._json({"removed": sessions.store().delete_tag(parts[2])})
            if parts[:2] == ["api", "tag-categories"] and len(parts) == 3:
                return self._json({"removed": sessions.store().delete_category(parts[2])})
            return self._json({"error": "not found"}, 404)
        except FileNotFoundError as e:
            return self._json({"error": str(e)}, 404)
        except ValueError as e:
            return self._json({"error": str(e)}, 400)
        except Exception as e:  # noqa: BLE001
            return self._json({"error": f"{type(e).__name__}: {e}"}, 500)


def serve(host="0.0.0.0", port=8070, warm: bool = True):
    if sessions.remote():
        sys.exit("acroscope serve: unset ACROSCOPE_URL, the server owns the database itself")
    srv = ThreadingHTTPServer((host, port), Handler)
    srv.daemon_threads = True
    print(f"acroscope player on http://{host}:{port}/  (data {VIDEOS_DIR.parent}, {sessions.store()})")
    if warm:
        background_scans()
    else:
        first_start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
