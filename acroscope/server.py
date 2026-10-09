"""The player: a stdlib HTTP server for one HTML page, JSON endpoints over the session files, and the clips as
static files with Range support (seeking needs it). Writes go through the same functions as the CLI."""
import json
import mimetypes
import os
import re
import shutil
import subprocess
import sys
import threading
from functools import lru_cache
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from . import blackbox, events as ev, metrics, sessions, video
from .config import TAGS, VIDEOS_DIR

STATIC = Path(__file__).parent / "static"
_lock = threading.Lock()
_busy: set[str] = set()          # blackbox files being decoded / sessions being probed right now
_busy_lock = threading.Lock()


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
    subprocess.run([sys.executable, "-m", "acroscope.cli", *args], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _decode_async(bbl: str):
    _background(f"decode:{bbl}", lambda: _cli("arms", bbl))


def _probe_async(session: str):
    _background(f"probe:{session}", lambda: _cli("refresh", session))


def warm_up():
    """At start: decode every blackbox file and probe every clip the session files refer to, one child process at
    a time, so the first page open never waits on the Drive mount. Sessions without a file get one."""
    def run():
        for d in sessions.session_dirs():
            s = sessions.load(d.name)
            for b in s["blackbox"]:
                if blackbox.index_bbl(b, cached_only=True) is None:
                    _cli("arms", b)
            if not sessions.session_path(d.name).exists() or any(v.get("codec") is None for v in s["videos"]):
                _cli("refresh", d.name)
        print("warm-up done", flush=True)
    _background("warm-up", run)


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
                return self._file(STATIC / parts[1])
            if parts[0] == "video" and len(parts) == 3:
                return self._file(video.playable(VIDEOS_DIR / parts[1] / parts[2]), "video/mp4", "max-age=3600")
            if parts[0] == "frame" and len(parts) == 4:
                return self._file(video.frame(VIDEOS_DIR / parts[1] / parts[2], float(parts[3])), "image/jpeg", "max-age=86400")
            if parts[:2] == ["api", "sessions"]:
                return self._json(sessions.all_sessions())
            if parts[:2] == ["api", "tags"]:
                return self._json(TAGS)
            if parts[:2] == ["api", "session"] and len(parts) >= 3:
                name = parts[2]
                if len(parts) == 3:
                    if not sessions.session_path(name).exists():
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
            if parts[:2] == ["api", "session"] and len(parts) == 4 and parts[3] == "moment":
                b = self._body()
                with _lock:
                    m = sessions.tag(parts[2], b["video"], float(b["start"]), float(b["end"]), b.get("title", ""),
                                     [t for t in b.get("tags", []) if t], b.get("note", ""), b.get("metrics"), b.get("id"))
                return self._json(m)
            if parts[:2] == ["api", "session"] and len(parts) == 4 and parts[3] == "note":
                b = self._body()
                with _lock:
                    s = sessions.load(parts[2])
                    if "video" in b:
                        for v in s["videos"]:
                            if v["file"] == b["video"]:
                                v["note"] = b.get("note", "")
                    else:
                        s["note"] = b.get("note", "")
                    sessions.save(s)
                return self._json({"ok": True})
            return self._json({"error": "not found"}, 404)
        except Exception as e:  # noqa: BLE001
            return self._json({"error": f"{type(e).__name__}: {e}"}, 500)

    def do_DELETE(self):
        parts = [unquote(p) for p in urlparse(self.path).path.strip("/").split("/") if p]
        if parts[:2] == ["api", "session"] and len(parts) == 5 and parts[3] == "moment":
            with _lock:
                ok = sessions.untag(parts[2], parts[4])
            return self._json({"removed": ok})
        return self._json({"error": "not found"}, 404)


def serve(host="0.0.0.0", port=8070, warm: bool = True):
    srv = ThreadingHTTPServer((host, port), Handler)
    srv.daemon_threads = True
    print(f"acroscope player on http://{host}:{port}/  (data {VIDEOS_DIR.parent})")
    if warm:
        warm_up()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
