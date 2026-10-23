"""The server's API through the remote client: a real `acroscope serve` child on a free port with a scratch
database and an empty data dir, driven exactly as the CLI on another machine drives it."""
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from acroscope.sources import blackbox
from acroscope.store.remote import Remote, RemoteError


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def fake_arm(cache: Path, stem: str, length: float, rate: float, cols: dict):
    """A decoded arm in the server's cache, as if index_bbl had run on <stem>.bbl: `cols` maps a field to a function
    of the arm time."""
    import array
    n = int(length * rate)
    t = array.array("d", (i / rate for i in range(n)))
    meta = {"bbl": stem, "file": stem + ".bbl", "index": 1, "frames": n, "length": t[-1], "rate": rate, "fields": ["time", *cols]}
    (cache / "arms" / stem).mkdir(parents=True, exist_ok=True)
    blackbox._save(meta, t, {k: array.array("f", (f(x) for x in t)) for k, f in cols.items()}, cache / "arms" / stem / "arm-01.bin")
    (cache / "arms" / stem / "arms.json").write_text(json.dumps({"v": blackbox.INDEX_VERSION, "file": stem + ".bbl", "stem": stem, "date": None,
                                                                 "arms": [{k: v for k, v in meta.items() if k != "fields"}], "boots": []}))


LOG_BYTES = b"H Product:Blackbox flight data recorder by Nicholas Sherlock\n" + bytes(range(256)) * 4


class ApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        root = Path(cls.tmp.name)
        (root / "data" / "videos" / "2026-10-07-s").mkdir(parents=True)
        (root / "data" / "videos" / "2026-10-07-s" / "2026-10-07_001.mp4").write_bytes(b"")   # a clip that is not probeable
        (root / "data" / "videos" / "2026-10-05-tags").mkdir()                    # test_tags_api's own session
        (root / "data" / "videos" / "2026-10-05-tags" / "2026-10-05_001.mp4").write_bytes(b"")
        (root / "data" / "blackbox").mkdir()
        (root / "data" / "blackbox" / "x.bbl").write_bytes(b"")          # a log the server has (empty: no arms)
        (root / "data" / "blackbox" / "dump #2.bbl").write_bytes(LOG_BYTES)   # one to download (an odd name on purpose)
        (root / "data" / "blackbox" / "x.csv").write_bytes(b"time,gyro")     # what blackbox_decode leaves next to a log
        (root / "data" / "blackbox" / "db.bbl").symlink_to(root / "t.db")    # a link out of blackbox/
        (root / "elsewhere-logs").mkdir()
        (root / "elsewhere-logs" / "x.bbl").write_bytes(b"secret")
        (root / "data" / "blackbox" / "sub").symlink_to(root / "elsewhere-logs")   # a linked folder in blackbox/
        (root / "data" / "blackbox" / "LOUD.BBL").write_bytes(b"upper case")    # a card that shouts
        cls.port = free_port()
        env = dict(os.environ, ACROSCOPE_DB=str(root / "t.db"), ACROSCOPE_DATA=str(root / "data"),
                   ACROSCOPE_CACHE=str(root / "cache"), ACROSCOPE_URL="")
        cls.proc = subprocess.Popen([sys.executable, "-m", "acroscope.cli", "serve", "--host", "127.0.0.1", "--port", str(cls.port), "--no-warm"],
                                    env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        cls.url = f"http://127.0.0.1:{cls.port}"
        for _ in range(100):
            try:
                urllib.request.urlopen(cls.url + "/api/store", timeout=1).read()
                break
            except Exception:  # noqa: BLE001
                time.sleep(0.1)
        else:
            raise RuntimeError("server did not come up")
        cls.r = Remote(cls.url)

    @classmethod
    def tearDownClass(cls):
        cls.proc.terminate()
        cls.proc.wait(5)
        cls.tmp.cleanup()

    def test_cli_runs_as_a_job(self):
        # a long proxied command must not hit the client's read timeout (#9): the server answers with a job, the
        # client polls it
        j = self.r._call("POST", "/api/cli", {"args": ["sessions", "--json"], "async": True})
        self.assertIn("job", j)
        for _ in range(100):
            r = self.r._call("GET", "/api/cli/" + j["job"])
            if r["done"]:
                break
            time.sleep(0.1)
        self.assertEqual(r["code"], 0, r["stderr"])
        with self.assertRaises(RemoteError):                       # fetched once, then gone
            self.r._call("GET", "/api/cli/" + j["job"])
        self.assertEqual(self.r.cli(["sessions", "--json"])["code"], 0)
        with self.assertRaises(RemoteError):
            self.r._call("POST", "/api/cli", {"args": ["serve"], "async": True})

    def test_session_roundtrip(self):
        r = self.r
        s = r.refresh("2026-10-07-s", probe_videos=False)
        self.assertEqual([v["file"] for v in s["videos"]], ["2026-10-07_001.mp4"])
        m = r.tag("2026-10-07-s", "2026-10-07_001.mp4", 1, 2, "t", ["flip"], "n", {"k": 1})
        self.assertEqual(m["id"], "m01")
        self.assertEqual(r.set_match("2026-10-07-s", "2026-10-07_001.mp4", "x.bbl", 2, 3.5, "note"),   # the match, as Db gives it
                         {"video": "2026-10-07_001.mp4", "bbl": "x.bbl", "arm": 2, "offset": 3.5, "note": "note"})
        r.set_note("2026-10-07-s", "session note")
        r.set_note("2026-10-07-s", "clip note", "2026-10-07_001.mp4")
        d = r.load("2026-10-07-s")
        self.assertEqual((d["note"], d["videos"][0]["note"], d["blackbox"], d["matches"][0]["arm"], d["moments"][0]["metrics"]),
                         ("session note", "clip note", ["x.bbl"], 2, {"k": 1}))
        self.assertEqual(r.unmatch("2026-10-07-s", "2026-10-07_001.mp4", 2), 1)
        self.assertTrue(r.untag("2026-10-07-s", "m01"))
        self.assertEqual(next(s for s in r.sessions() if s["session"] == "2026-10-07-s")["moments"], 0)
        self.assertEqual(r.purge("2026-10-07-s", days=0), [])          # nothing is missing
        self.assertFalse(r.untag("2026-10-07-s", "m99"))
        with self.assertRaises(RemoteError):
            r.set_match("2026-10-07-s", "nope.mp4", "x.bbl", "not-an-int", 0)

    def test_session_editing(self):
        r = self.r
        root = Path(self.tmp.name)
        (root / "data" / "videos" / "2026-10-08-e").mkdir()
        (root / "data" / "videos" / "2026-10-08-e" / "2026-10-08_001.mp4").write_bytes(b"")
        r.refresh("2026-10-08-e", probe_videos=False)
        r.tag("2026-10-08-e", "2026-10-08_001.mp4", 1, 2, "t", ["poi"])
        s = r.rename_session("2026-10-08-e", "2026-10-09-renamed")
        self.assertEqual((s["session"], s["date"], len(s["moments"])), ("2026-10-09-renamed", "2026-10-09", 1))
        self.assertTrue((root / "data" / "videos" / "2026-10-09-renamed" / "2026-10-08_001.mp4").exists())
        s = r.move_clip("2026-10-09-renamed", "001", "2026-10-09-other")
        self.assertEqual(([v["file"] for v in s["videos"]], len(s["moments"])), (["2026-10-08_001.mp4"], 1))
        self.assertTrue((root / "data" / "videos" / "2026-10-09-other" / "2026-10-08_001.mp4").exists())
        s = r.merge_sessions("2026-10-09-other", "2026-10-09-renamed")
        self.assertEqual(len(s["videos"]), 1)
        self.assertFalse(r.exists("2026-10-09-other"))
        with self.assertRaises(RemoteError):
            r.rename_session("2026-10-09-renamed", "not a session name")

    def test_cli_as_client(self):
        """The CLI on another machine: no data dir of its own, everything through the server."""
        root = Path(self.tmp.name)
        env = dict(os.environ, ACROSCOPE_URL=self.url, ACROSCOPE_DATA=str(root / "nowhere"),
                   ACROSCOPE_CACHE=str(root / "client-cache"), ACROSCOPE_DB=str(root / "unused.db"))

        def cli(*args):
            return subprocess.run([sys.executable, "-m", "acroscope.cli", *args], env=env, capture_output=True, text=True)
        self.r.refresh("2026-10-07-s", probe_videos=False)
        r = cli("attach", "2026-10-07-s", "x.bbl")                             # the server resolves the log, not the client
        self.assertEqual(r.returncode, 0, r.stderr)
        r = cli("match", "2026-10-07-s", "001", "x.bbl", "1", "0")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout)[0]["arm"], 1)
        r = cli("sessions", "2026-10-07-s")                                    # runs on the server: the arms are in its cache
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("x.bbl", r.stdout)
        self.assertNotIn("file not found", r.stdout)
        self.assertEqual(self.r.unmatch("2026-10-07-s", "2026-10-07_001.mp4", 1), 1)

    def test_undecoded_log_is_not_decoded_in_the_request(self):
        """A match on a log that is not in the cache yet: events and series answer empty and the decode runs in a
        child, as CLAUDE.md demands (the decoder holds the GIL for ~30 s per file)."""
        root = Path(self.tmp.name)
        S = "2026-10-06-cold"
        (root / "data" / "videos" / S).mkdir()
        for n in range(1, 5):
            (root / "data" / "videos" / S / f"2026-10-06_00{n}.mp4").write_bytes(b"")
        for b in ("z1.bbl", "z2.bbl", "z3.bbl", "z4.bbl"):
            (root / "data" / "blackbox" / b).write_bytes(b"")
        self.r.refresh(S, probe_videos=False)
        for n in range(1, 5):
            self.r.set_match(S, f"2026-10-06_00{n}.mp4", f"z{n}.bbl", 1, 0)
        get = lambda what, clip, extra="": json.loads(urllib.request.urlopen(f"{self.url}/api/session/{S}/{what}?video={clip}{extra}").read())
        self.assertEqual(get("events", "2026-10-06_001.mp4"), [])
        self.assertEqual(get("series", "2026-10-06_002.mp4"), [])
        with self.assertRaises(urllib.error.HTTPError) as cm:                   # metrics: 503 until the log is decoded
            get("metrics", "2026-10-06_003.mp4", "&from=0&to=1")
        self.assertEqual(cm.exception.code, 503)
        (root / "data" / "blackbox" / "z4.bbl").unlink()                           # a log that is gone: nothing, not a 404
        self.assertEqual(get("events", "2026-10-06_004.mp4"), [])
        with self.assertRaises(urllib.error.HTTPError) as cm:                   # metrics: a 404, not "try again" forever
            get("metrics", "2026-10-06_004.mp4", "&from=0&to=1")
        self.assertEqual(cm.exception.code, 404)
        self.assertIn("z4.bbl", json.loads(cm.exception.read())["error"])
        for _ in range(50):                                                        # the child decode of z1 lands in the cache
            if (root / "cache" / "arms" / "z1" / "arms.json").exists():
                break
            time.sleep(0.1)
        self.assertTrue((root / "cache" / "arms" / "z1" / "arms.json").exists())

    def test_sticks(self):
        """The virtual sticks over a clip: every arm's sticks each 20 ms in the picture's terms, Mode 2, every axis +
        = stick right or up (left stick: yaw, throttle 0..100; right stick: roll, pitch). rcCommand[2] is + for the
        left stick LEFT (metrics.CONVENTIONS), so yaw comes out negated."""
        root = Path(self.tmp.name)
        S = "2026-10-08-sticks"
        (root / "data" / "videos" / S).mkdir()
        (root / "data" / "videos" / S / "2026-10-08_001.mp4").write_bytes(b"")
        (root / "data" / "videos" / S / "2026-10-08_002.mp4").write_bytes(b"")
        (root / "data" / "blackbox" / "sticks.bbl").write_bytes(b"")
        step = lambda a, b: lambda t: a if t < 1 else b                     # one value for the first second, one after
        fake_arm(root / "cache", "sticks", 2.0, 200, {"rcCommand[0]": step(250, -500), "rcCommand[1]": step(-100, 500),
                                                      "rcCommand[2]": step(500, -250), "rcCommand[3]": step(1250, 2000)})
        self.r.refresh(S, probe_videos=False)
        self.r.set_match(S, "2026-10-08_001.mp4", "sticks.bbl", 1, 3.0)
        get = lambda clip: json.loads(urllib.request.urlopen(f"{self.url}/api/session/{S}/sticks?video={clip}").read())
        [s] = get("2026-10-08_001.mp4")
        self.assertEqual((s["offset"], s["arm"], s["bbl"], s["step"]), (3.0, 1, "sticks.bbl", 0.02))
        self.assertEqual(len(s["roll"]), 100)                                  # 0 .. 1.995 s every 20 ms
        at = lambda i: {k: s[k][i] for k in ("throttle", "yaw", "pitch", "roll")}
        self.assertEqual(at(0), {"throttle": 25, "yaw": -100, "pitch": -20, "roll": 50})
        self.assertEqual(at(75), {"throttle": 100, "yaw": 50, "pitch": 100, "roll": -100})
        self.assertEqual(get("2026-10-08_002.mp4"), [])                       # no match, no sticks

    def test_a_clip_by_its_name_or_number(self):
        """#27: the clip-data calls take a clip as the CLI does, by file, readable name or number; `video=2026-10-14_010`
        gave [] where the file id gave the clip's suggestions. A clip that is not there is an error, not []."""
        root = Path(self.tmp.name)
        S = "2026-10-08-names"
        (root / "data" / "videos" / S).mkdir()
        (root / "data" / "videos" / S / "2026-10-08_001.mp4").write_bytes(b"")
        (root / "data" / "blackbox" / "names.bbl").write_bytes(b"")
        # what the detectors, the series, the sticks and the metrics read
        cols = {f"{f}[{i}]": (lambda t: 0.0) for f in ("accSmooth", "gyroADC", "gyroUnfilt") for i in range(3)}
        cols |= {f"rcCommand[{i}]": (lambda t: 0.0) for i in range(3)} | {"rcCommand[3]": lambda t: 1500.0, "vbatLatest": lambda t: 4.0}
        cols |= {f"imuQuaternion[{i}]": (lambda t: 0.0) for i in range(3)}
        fake_arm(root / "cache", "names", 2.0, 200, cols)
        self.r.refresh(S, probe_videos=False)
        self.r.set_match(S, "2026-10-08_001.mp4", "names.bbl", 1, 3.0)
        get = lambda what, clip: json.loads(urllib.request.urlopen(f"{self.url}/api/session/{S}/{what}?video={clip}").read())
        for what in ("events", "series", "sticks"):
            by_file = get(what, "2026-10-08_001.mp4")
            if what != "events":
                self.assertTrue(by_file, what)                                        # the arm is there
            self.assertEqual(get(what, "001"), by_file, what)
            self.assertEqual(get(what, "2026-10-08_001"), by_file, what)
        m = lambda clip: json.loads(urllib.request.urlopen(f"{self.url}/api/session/{S}/metrics?video={clip}&from=3.5&to=4.5").read())
        self.assertEqual(m("001"), m("2026-10-08_001.mp4"))
        for what in ("events", "series", "sticks"):
            with self.assertRaises(urllib.error.HTTPError, msg=what) as cm:
                urllib.request.urlopen(f"{self.url}/api/session/{S}/{what}?video=999")
            self.assertEqual(cm.exception.code, 404, what)

    def test_moment_times_are_checked(self):
        self.r.refresh("2026-10-07-s", probe_videos=False)
        for start, end in ((1e400, 2), (float("nan"), 2), (5, 1)):
            with self.assertRaises(RemoteError, msg=(start, end)):
                self.r.tag("2026-10-07-s", "2026-10-07_001.mp4", start, end, "bad", [])
        d = json.loads(urllib.request.urlopen(self.url + "/api/session/2026-10-07-s/data").read())
        self.assertEqual([m for m in d["moments"] if m["title"] == "bad"], [])
        req = urllib.request.Request(self.url + "/api/session/2026-10-07-s/moment", method="POST",
                                     data=b'{"video": "2026-10-07_001.mp4", "start": 5, "end": 1, "title": "bad"}')
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(req)
        self.assertEqual(cm.exception.code, 400)                                    # a client error, not a 500

    def test_head_sends_no_body(self):
        # a raw socket: http.client's buffered reader would swallow a stray body and hide the bug
        with socket.create_connection(("127.0.0.1", self.port), timeout=5) as c:
            c.sendall(b"HEAD /api/store HTTP/1.1\r\nHost: x\r\n\r\n")
            head = b""
            while b"\r\n\r\n" not in head:
                head += c.recv(4096)
            self.assertTrue(head.startswith(b"HTTP/1.1 200"), head[:40])
            self.assertEqual(head.split(b"\r\n\r\n", 1)[1], b"")
            c.settimeout(0.5)
            with self.assertRaises(TimeoutError):                            # nothing more arrives: no body
                c.recv(4096)

    def test_merge_a_cut_short_import(self):
        # #16: an import cut short came in as <date>-unsorted (two clips and the log), the restart under the right
        # name (one clip). Renaming onto the existing name refuses; merging takes clips, log, matches, moments, note.
        r = self.r
        root = Path(self.tmp.name)
        for sess, clips in (("2026-10-11-unsorted", ["2026-10-11_001.mp4", "2026-10-11_002.mp4"]), ("2026-10-11-bea", ["2026-10-11_005.mp4"])):
            (root / "data" / "videos" / sess).mkdir()
            for c in clips:
                (root / "data" / "videos" / sess / c).write_bytes(b"")
            r.refresh(sess, probe_videos=False)
        (root / "data" / "blackbox" / "BTFL_x.bbl").write_bytes(b"")
        r.attach("2026-10-11-unsorted", "BTFL_x.bbl")
        r.set_match("2026-10-11-unsorted", "2026-10-11_001.mp4", "BTFL_x.bbl", 1, 2.5)
        r.tag("2026-10-11-unsorted", "2026-10-11_002.mp4", 1, 2, "t", ["poi"])
        r.set_note("2026-10-11-unsorted", "cut short")
        with self.assertRaisesRegex(RemoteError, "exists"):
            r.rename_session("2026-10-11-unsorted", "2026-10-11-bea")
        s = r.merge_sessions("2026-10-11-unsorted", "2026-10-11-bea")
        self.assertEqual(sorted(v["file"] for v in s["videos"]), ["2026-10-11_001.mp4", "2026-10-11_002.mp4", "2026-10-11_005.mp4"])
        self.assertEqual(s["blackbox"], ["BTFL_x.bbl"])
        self.assertEqual([(m["video"], m["arm"]) for m in s["matches"]], [("2026-10-11_001.mp4", 1)])
        self.assertEqual(len(s["moments"]), 1)
        self.assertIn("cut short", s["note"])
        self.assertFalse(r.exists("2026-10-11-unsorted"))
        self.assertFalse((root / "data" / "videos" / "2026-10-11-unsorted").exists())

    def test_delete_a_clip(self):
        # a no-signal clip goes: the file (and its original) into trash/<today>/<session>/, its rows out; a refresh
        # does not bring it back
        r = self.r
        root = Path(self.tmp.name)
        S = "2026-10-12-del"
        (root / "data" / "videos" / S).mkdir()
        for c in ("2026-10-12_001.mp4", "2026-10-12_002.mp4"):
            (root / "data" / "videos" / S / c).write_bytes(b"x")
        r.refresh(S, probe_videos=False)
        r.set_match(S, "2026-10-12_002.mp4", "x.bbl", 1, 0.5)
        r.tag(S, "2026-10-12_002.mp4", 1, 2, "static", [])
        d = r.delete_clip(S, "002")
        self.assertEqual((d["video"], d["matches"], d["moments"]), ("2026-10-12_002.mp4", 1, 1))
        self.assertFalse((root / "data" / "videos" / S / "2026-10-12_002.mp4").exists())
        self.assertTrue((root / "data" / d["trash"]).is_file())
        self.assertRegex(d["trash"], rf"^trash/\d{{4}}-\d{{2}}-\d{{2}}/{S}/2026-10-12_002.mp4$")
        s = r.refresh(S, probe_videos=False)
        self.assertEqual([v["file"] for v in s["videos"]], ["2026-10-12_001.mp4"])
        self.assertEqual((s["matches"], s["moments"]), ([], []))
        with self.assertRaises(RemoteError):
            r.delete_clip(S, "002")                                  # gone already

    def test_no_session_from_nothing(self):
        # a session that is neither in the database nor on disk is not created by a refresh or a page asking for it:
        # a probe still running for a session merged or renamed away meanwhile brought it back empty (#16)
        r = self.r
        with self.assertRaises(RemoteError):
            r.refresh("2026-10-20-nothing", probe_videos=False)
        with self.assertRaises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(self.url + "/api/session/2026-10-20-nothing")
        self.assertEqual(e.exception.code, 404)
        self.assertNotIn("2026-10-20-nothing", [s["session"] for s in r.sessions()])
        # a folder not registered yet is registered, as before
        (Path(self.tmp.name) / "data" / "videos" / "2026-10-20-new").mkdir()
        urllib.request.urlopen(self.url + "/api/session/2026-10-20-new").read()
        self.assertTrue(r.exists("2026-10-20-new"))

    def test_cli_proxy(self):
        r = self.r
        out = r.cli(["tags"])
        self.assertEqual(out["code"], 0, out["stderr"])
        self.assertIn("tricks", out["stdout"])
        with self.assertRaises(RemoteError):
            r.cli(["serve"])                                   # not for clients
        # the file endpoint hands out cache files only, and a proxied command gets names, never paths
        outside = Path(self.tmp.name) / "outside.txt"
        outside.write_text("not for the client")
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(self.url + "/api/file?path=" + str(outside))
        self.assertEqual(cm.exception.code, 403)
        with self.assertRaises(RemoteError):
            r.cli(["frame", "2026-10-07-s", str(outside), "0"])
        # nor a bare file name that happens to sit in the server's working directory (the image's /app: Dockerfile)
        here = Path.cwd() / "cli-probe.bbl"
        here.write_bytes(b"H Product:Blackbox flight data recorder\n")
        self.addCleanup(here.unlink)
        out = r.cli(["arms", here.name])
        self.assertNotEqual(out["code"], 0)
        self.assertIn("0 blackbox files match", out["stderr"])
        out = r.cli(["frame", "2026-10-07-s", "README.md", "0"])
        self.assertIn("no such clip", out["stderr"])

    def test_unknown_session_is_404_and_edits_take_names_only(self):
        before = [s["session"] for s in self.r.sessions()]
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(self.url + "/api/session/not-a-session")
        self.assertEqual(cm.exception.code, 404)
        self.assertEqual([s["session"] for s in self.r.sessions()], before)          # a GET registers nothing
        outside = Path(self.tmp.name) / "outside_dir"
        outside.mkdir()
        with self.assertRaises(RemoteError):
            self.r.rename_session("../../outside_dir", "2026-01-01-pwn")
        self.assertTrue(outside.is_dir())                                           # nothing moved
        with self.assertRaises(RemoteError):
            self.r.refresh("../../outside_dir", probe_videos=False)

    def test_file_routes_stay_inside_their_dirs(self):
        # an encoded slash in a path segment must not walk out of videos/ or static/ (the database sits two up)
        for path in ("/video/..%2F../t.db", "/frame/..%2F../t.db/0", "/static/..%2F..%2Fpyproject.toml", "/blackbox/..%2F..%2Ft.db"):
            with self.assertRaises(urllib.error.HTTPError, msg=path) as cm:
                urllib.request.urlopen(self.url + path)
            self.assertEqual(cm.exception.code, 404, path)
        self.assertTrue((Path(self.tmp.name) / "t.db").exists())
        elsewhere = Path(self.tmp.name) / "elsewhere"                        # a session folder that is a symlink still serves
        elsewhere.mkdir()
        (elsewhere / "2026-10-04_001.mp4").write_bytes(b"")
        (Path(self.tmp.name) / "data" / "videos" / "2026-10-04-link").symlink_to(elsewhere)
        self.assertEqual(urllib.request.urlopen(self.url + "/video/2026-10-04-link/2026-10-04_001.mp4").status, 200)

    def test_a_log_downloads_as_itself(self):
        """The player's ⤓ on a log: the .bbl as it is, saved under its own name (for Blackbox Explorer)."""
        r = urllib.request.urlopen(self.url + "/blackbox/" + urllib.parse.quote("dump #2.bbl"))
        self.assertEqual((r.status, r.headers["Content-Type"], r.headers["Content-Length"], r.headers["Content-Disposition"]),
                         (200, "application/octet-stream", str(len(LOG_BYTES)), "attachment; filename*=UTF-8''dump%20%232.bbl"))
        self.assertEqual(r.read(), LOG_BYTES)

    def test_fetch_log_brings_a_log_to_the_client(self):
        """#21: `acroscope fetch-log <bbl>` saves the .bbl as it is on the client, through the same GET /blackbox/<file>
        the player's ⤓ uses: by its file name, or by a fragment unique among the logs the sessions know. Not a
        proxied command: it runs here, so it needs no deploy. Anything but a log in blackbox/ is refused."""
        import io
        from contextlib import redirect_stdout
        from unittest import mock
        from acroscope import cli
        from acroscope.store import sessions
        self.r.refresh("2026-10-07-s", probe_videos=False)
        self.r.attach("2026-10-07-s", "dump #2.bbl")
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "got.bbl"
            self.assertEqual(self.r.fetch_log("dump #2.bbl", dest), dest)
            self.assertEqual(dest.read_bytes(), LOG_BYTES)
            with self.assertRaises(RemoteError):
                self.r.fetch_log("x.csv", Path(tmp) / "no.csv")
            self.assertFalse((Path(tmp) / "no.csv").exists())
            out = io.StringIO()
            with mock.patch.object(sessions, "SERVER_URL", self.url), mock.patch.object(sessions, "_store", None), redirect_stdout(out):
                cli.main(["fetch-log", "dump #", "--to", tmp])                       # a fragment, resolved through the sessions
                cli.main(["fetch-log", "dump #2.bbl", "--to", tmp])                  # the exact name, no lookup
            got = Path(tmp) / "dump #2.bbl"
            self.assertEqual(out.getvalue().splitlines(), [str(got)] * 2)
            self.assertEqual(got.read_bytes(), LOG_BYTES)
            with mock.patch.object(sessions, "SERVER_URL", self.url), mock.patch.object(sessions, "_store", None), \
                    redirect_stdout(io.StringIO()), self.assertRaises(SystemExit):
                cli.main(["fetch-log", "nothing-like-it", "--to", tmp])
        self.r.detach("2026-10-07-s", "dump #2.bbl")

    def test_fetch_log_without_a_server(self):
        """Local store: the log is already here. Without --to its path is printed; with --to it is copied there."""
        import io
        from contextlib import redirect_stdout
        from unittest import mock
        from acroscope import cli
        from acroscope.store import sessions
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "logs").mkdir()
            (Path(tmp) / "logs" / "L_20261007.bbl").write_bytes(LOG_BYTES)
            out = io.StringIO()
            with mock.patch.object(sessions, "SERVER_URL", ""), mock.patch.object(sessions, "_store", None), \
                    mock.patch.object(blackbox, "BLACKBOX_DIR", Path(tmp) / "logs"), redirect_stdout(out):
                cli.main(["fetch-log", "20261007"])
                cli.main(["fetch-log", "20261007", "--to", str(Path(tmp) / "out")])
            self.assertEqual(out.getvalue().splitlines(), [str(Path(tmp) / "logs" / "L_20261007.bbl"), str(Path(tmp) / "out" / "L_20261007.bbl")])
            self.assertEqual((Path(tmp) / "out" / "L_20261007.bbl").read_bytes(), LOG_BYTES)
        # only logs, only in blackbox/ itself, never through a link: a missing one, a CSV beside them, a symlink
        # to the database, a subfolder
        self.assertEqual(urllib.request.urlopen(self.url + "/blackbox/LOUD.BBL").read(), b"upper case")
        for path in ("nope.bbl", "x.csv", "db.bbl", "sub%2Fx.bbl", "x.bbl/more"):
            with self.assertRaises(urllib.error.HTTPError, msg=path) as cm:
                urllib.request.urlopen(self.url + "/blackbox/" + path)
            self.assertEqual(cm.exception.code, 404, path)

    def session(self, name):
        d = Path(self.tmp.name) / "data" / "videos" / name
        d.mkdir()
        (d / f"{name[:10]}_001.mp4").write_bytes(b"")
        self.r.refresh(name, probe_videos=False)

    def test_move_a_log(self):
        """⇄ in one request: into an existing session only, out of this one with its matches."""
        for s in ("2026-10-08-a", "2026-10-08-b"):
            self.session(s)
        self.r.attach("2026-10-08-a", "x.bbl")
        self.r.set_match("2026-10-08-a", "001", "x.bbl", 1, 2.0)
        with self.assertRaises(RemoteError):
            self.r.move_log("2026-10-08-a", "x.bbl", "2026-10-08-typo")
        with self.assertRaises(RemoteError):                          # nor a session made by attaching to a typo
            self.r.attach("2026-10-08-typo", "x.bbl")
        self.assertFalse(self.r.exists("2026-10-08-typo"))
        self.assertEqual(self.r.move_log("2026-10-08-a", "x.bbl", "2026-10-08-b"), ["x.bbl"])
        a = self.r.load("2026-10-08-a")
        self.assertEqual((a["blackbox"], a["matches"]), ([], []))

    def test_overview_names_a_log_whose_file_is_gone(self):
        self.session("2026-10-06-gone")
        (Path(self.tmp.name) / "data" / "blackbox" / "gone.bbl").write_bytes(b"")
        self.r.attach("2026-10-06-gone", "gone.bbl")
        (Path(self.tmp.name) / "data" / "blackbox" / "gone.bbl").unlink()
        o = self.r._call("GET", "/api/session/2026-10-06-gone")
        self.assertEqual(o["blackbox_gone"], ["gone.bbl"])

    def test_free_text_may_hold_a_slash(self):
        """#32: the server refuses a '/' in what names a file (session, clip, log: a path would point ffmpeg or the
        decoder anywhere on the host), not in free text: a moment's title, --note, --tags ("725 deg/s")."""
        self.session("2026-10-06-slash")
        r = self.r._call("POST", "/api/cli", {"args": ["tag", "2026-10-06-slash", "001", "1", "2", "roll at 725 deg/s",
                                                      "--note", "pitch 2/3 of the way", "--tags", "roll,a/b"]})
        self.assertEqual(r["code"], 0, r["stderr"])
        titles = {m["title"]: (m["note"], m["tags"]) for m in self.r.load("2026-10-06-slash")["moments"]}
        self.assertEqual(titles["roll at 725 deg/s"], ("pitch 2/3 of the way", ["roll", "a/b"]))
        for args in (["sessions", "../2026-10-07-s"], ["tag", "../../x", "001", "1", "2", "t"], ["frame", "2026-10-07-s", "../../t.db", "1"],
                     ["arms", "/etc/passwd"], ["match", "2026-10-07-s", "001", "a/b.bbl", "1", "2"]):
            with self.assertRaises(RemoteError, msg=args) as cm:
                self.r._call("POST", "/api/cli", {"args": args})
            self.assertIn("paths are not accepted", str(cm.exception), args)
        for args in (["tag", "2026-10-07-s"], ["serve"], []):                            # not a command a client may run
            with self.assertRaises(RemoteError, msg=args):
                self.r._call("POST", "/api/cli", {"args": args})

    def test_tags_api(self):
        r = self.r
        t = r.tags()
        self.assertIn("tricks", [c["name"] for c in t["categories"]])
        r.set_category("cruise", "#00ff00")
        r.set_tag("hover", "cruise", "#010101")
        t = r.tags()
        self.assertIn({"name": "hover", "category": "cruise", "color": "#010101", "pos": max(x["pos"] for x in t["tags"])}, t["tags"])
        self.assertTrue(r.delete_tag("hover"))
        self.assertTrue(r.delete_category("cruise"))
        with self.assertRaises(RemoteError):
            r.set_tag("x", "no-such-category")
        r.refresh("2026-10-05-tags", probe_videos=False)
        r.tag("2026-10-05-tags", "2026-10-05_001.mp4", 1, 2, "t", ["flip", "custom"])
        self.assertEqual(r.tag_usage("2026-10-05-tags"), {"flip": 1, "custom": 1})

    def test_tricks_api(self):
        """#45: the tricks in force, and the pilot's layer in the database: read, replaced whole (checked first),
        cleared; the suggestions follow the layer on the next look."""
        r = self.r
        root = Path(self.tmp.name)
        S = "2026-10-09-tricks"
        (root / "data" / "videos" / S).mkdir()
        (root / "data" / "videos" / S / "2026-10-09_001.mp4").write_bytes(b"")
        (root / "data" / "blackbox" / "roll.bbl").write_bytes(b"")
        # a 250 deg right roll at 2.0-2.5 s with the stick behind it: a roll attempt by default, a roll to a beginner
        cols = {f"{f}[{i}]": (lambda t: 0.0) for f in ("accSmooth", "gyroADC", "gyroUnfilt") for i in range(3)}
        cols |= {"accSmooth[2]": lambda t: 2048.0, "gyroADC[0]": lambda t: 500.0 if 2.0 <= t < 2.5 else 0.0, "gyroUnfilt[0]": lambda t: 500.0 if 2.0 <= t < 2.5 else 0.0}
        cols |= {f"rcCommand[{i}]": (lambda t: 0.0) for i in range(3)} | {"rcCommand[0]": lambda t: 375.0 if 2.0 <= t < 2.5 else 0.0, "rcCommand[3]": lambda t: 1500.0, "vbatLatest": lambda t: 380.0}
        cols |= {f"imuQuaternion[{i}]": (lambda t: 0.0) for i in range(3)}
        fake_arm(root / "cache", "roll", 5.0, 400, cols)
        r.refresh(S, probe_videos=False)
        r.set_match(S, "2026-10-09_001.mp4", "roll.bbl", 1, 10.0)
        events = lambda: [e["title"] for e in json.loads(urllib.request.urlopen(f"{self.url}/api/session/{S}/events?video=001").read())]
        self.assertEqual(events(), ["right roll attempt, 249 deg"])
        d = r.tricks()
        self.assertEqual(d["params"]["flip_min_deg"], {"value": 270, "default": 270, "source": d["sources"][0]})
        self.assertFalse(d["database"])
        self.assertIn("roll", [t["id"] for t in d["tricks"]])
        self.assertIsNone(r.setting("tricks"))
        empty = json.loads(urllib.request.urlopen(self.url + "/api/tricks/layer").read())
        self.assertEqual((empty["layer"], empty["updated_at"], len(empty["etag"])), ({}, None, 12))
        # a bad layer is refused with the place named, and nothing changes
        with self.assertRaises(RemoteError) as cm:
            r.set_setting("tricks", {"version": 1, "params": {"flip_min_dg": 240}})
        self.assertIn("flip_min_dg", str(cm.exception))
        self.assertIsNone(r.setting("tricks"))
        with self.assertRaises(RemoteError):
            r.set_setting("tricks", [1, 2])
        # the pilot's layer lands and the suggestions follow it, no restart
        row = r.set_setting("tricks", {"version": 1, "skill": "beginner"})
        self.assertEqual(row["value"], {"version": 1, "skill": "beginner"})
        self.assertEqual(r.setting("tricks")["value"], {"version": 1, "skill": "beginner"})
        self.assertEqual(events(), ["right roll"])
        d = r.tricks()
        self.assertEqual((d["skill"], d["database"], d["params"]["flip_min_deg"]["value"], d["params"]["flip_min_deg"]["source"]), ("beginner", True, 240, "preset beginner"))
        d = r.tricks(craft="OttO")
        self.assertEqual(d["craft"], "OttO")
        self.assertTrue(r.delete_setting("tricks"))
        self.assertFalse(r.delete_setting("tricks"))
        self.assertEqual(events(), ["right roll attempt, 249 deg"])
        with self.assertRaises(RemoteError):
            r.setting("other")
        # the file is for sharing: the layer out as a tricks file, one in as the layer
        import tomllib
        r.set_setting("tricks", {"version": 1, "params": {"flip_min_deg": 240}})
        resp = urllib.request.urlopen(self.url + "/api/tricks/export")
        self.assertEqual(resp.headers["Content-Type"], "text/plain; charset=utf-8")
        self.assertIn('filename="acroscope-tricks.toml"', resp.headers["Content-Disposition"])
        text = resp.read().decode()
        self.assertTrue(text.startswith("# acroscope tricks, exported"))
        self.assertEqual(tomllib.loads(text), {"version": 1, "params": {"flip_min_deg": 240}})
        post = lambda body: json.loads(urllib.request.urlopen(urllib.request.Request(self.url + "/api/tricks/import", data=json.dumps(body).encode(), method="POST",
                                                                                      headers={"Content-Type": "application/json"})).read())
        self.assertEqual(post({"text": 'skill = "beginner"\n', "mode": "merge"})["layer"], {"version": 1, "params": {"flip_min_deg": 240}, "skill": "beginner"})
        self.assertEqual(post({"text": 'version = 1\nskill = "advanced"\n'})["layer"], {"version": 1, "skill": "advanced"})
        self.assertEqual(r.setting("tricks")["value"], {"version": 1, "skill": "advanced"})
        for bad in ({"text": "[params]\nflip_min_dg = 1\n"}, {"text": "not = [toml"}, {"text": 3}, {"text": "version = 1", "mode": "append"}):
            with self.assertRaises(urllib.error.HTTPError, msg=bad) as cm:
                post(bad)
            self.assertEqual(cm.exception.code, 400)
        self.assertEqual(r.setting("tricks")["value"], {"version": 1, "skill": "advanced"})
        r.delete_setting("tricks")
        # the review of #49: no files through the proxy; a write says what it started from; a layer a later release
        # broke leaves the shipped tricks in force and the suggestions answering
        for args in (["tricks", "export", "pyproject.toml"], ["tricks", "import", "compose.yaml"]):
            with self.assertRaises(RemoteError, msg=args):
                r.cli(args)
        layer = lambda: json.loads(urllib.request.urlopen(self.url + "/api/tricks/layer").read())
        etag0 = layer()["etag"]
        put = lambda body, etag=None: urllib.request.urlopen(urllib.request.Request(self.url + "/api/tricks/layer" + (f"?etag={etag}" if etag else ""),
                                                                                    data=json.dumps(body).encode(), method="POST", headers={"Content-Type": "application/json"}))
        first = json.loads(put({"version": 1, "skill": "beginner"}, etag0).read())
        with self.assertRaises(urllib.error.HTTPError) as cm:
            put({"version": 1, "params": {"flip_min_deg": 250}}, etag0)                      # from the layer before the first write
        self.assertEqual(cm.exception.code, 409)
        self.assertEqual(layer()["layer"], {"version": 1, "skill": "beginner"})                # the first write stands
        put({"version": 1, "params": {"flip_min_deg": 250}}, first["etag"])
        self.assertEqual(layer()["layer"], {"version": 1, "params": {"flip_min_deg": 250}})
        import sqlite3
        db = sqlite3.connect(root / "t.db")
        db.execute("UPDATE settings SET value = ? WHERE key = 'tricks'", (json.dumps({"version": 1, "params": {"roll_hold_share": 0.85}}),))
        db.commit(); db.close()
        d = r.tricks()
        self.assertIn("roll_hold_share", d["layer_error"])
        self.assertFalse(d["database"])
        self.assertEqual(events(), ["right roll attempt, 249 deg"])                             # the shipped tricks, no 500
        self.assertEqual(layer()["layer"], {"version": 1, "params": {"roll_hold_share": 0.85}})   # still there to repair
        r.delete_setting("tricks")
        self.assertIsNone(r.tricks()["layer_error"])


if __name__ == "__main__":
    unittest.main()


class JobTest(unittest.TestCase):
    """The same command started again while it runs (a reload of the player, then 🎯 again) is the same job: two
    `automatch --write` children on one session would race each other's matches."""
    def test_a_running_job_is_not_started_twice(self):
        import threading
        from unittest import mock
        from acroscope.server import jobs
        go, calls = threading.Event(), []

        def run(args, timeout=0):
            calls.append(args)
            go.wait(5)
            return {"stdout": "", "stderr": "", "code": 0}
        with mock.patch.object(jobs, "run_cli", run):
            args = ["automatch", "2026-10-15-x", "--unmatched", "--write"]
            a, b = jobs.start_job(args), jobs.start_job(list(args))
            c = jobs.start_job(["automatch", "2026-10-15-y", "--unmatched", "--write"])
            self.assertEqual(a, b)
            self.assertNotEqual(a, c)
            go.set()
            for _ in range(200):                                      # both children done
                if all(jobs._jobs[j]["done"] for j in (a, c)):
                    break
                time.sleep(0.01)
        self.assertEqual(len(calls), 2)
