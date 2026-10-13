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
import urllib.request
from pathlib import Path

from acroscope import blackbox
from acroscope.remote import Remote, RemoteError


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
        for path in ("/video/..%2F../t.db", "/frame/..%2F../t.db/0", "/static/..%2F..%2Fpyproject.toml"):
            with self.assertRaises(urllib.error.HTTPError, msg=path) as cm:
                urllib.request.urlopen(self.url + path)
            self.assertEqual(cm.exception.code, 404, path)
        self.assertTrue((Path(self.tmp.name) / "t.db").exists())
        elsewhere = Path(self.tmp.name) / "elsewhere"                        # a session folder that is a symlink still serves
        elsewhere.mkdir()
        (elsewhere / "2026-10-04_001.mp4").write_bytes(b"")
        (Path(self.tmp.name) / "data" / "videos" / "2026-10-04-link").symlink_to(elsewhere)
        self.assertEqual(urllib.request.urlopen(self.url + "/video/2026-10-04-link/2026-10-04_001.mp4").status, 200)

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


if __name__ == "__main__":
    unittest.main()
