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
import urllib.request
from pathlib import Path

from acroscope.remote import Remote, RemoteError


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


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

    def test_session_roundtrip(self):
        r = self.r
        s = r.refresh("2026-10-07-s", probe_videos=False)
        self.assertEqual([v["file"] for v in s["videos"]], ["2026-10-07_001.mp4"])
        m = r.tag("2026-10-07-s", "2026-10-07_001.mp4", 1, 2, "t", ["flip"], "n", {"k": 1})
        self.assertEqual(m["id"], "m01")
        r.set_match("2026-10-07-s", "2026-10-07_001.mp4", "x.bbl", 2, 3.5, "note")
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
        for f in ("2026-10-06_001.mp4", "2026-10-06_002.mp4"):
            (root / "data" / "videos" / S / f).write_bytes(b"")
        for b in ("z1.bbl", "z2.bbl"):
            (root / "data" / "blackbox" / b).write_bytes(b"")
        self.r.refresh(S, probe_videos=False)
        self.r.set_match(S, "2026-10-06_001.mp4", "z1.bbl", 1, 0)
        self.r.set_match(S, "2026-10-06_002.mp4", "z2.bbl", 1, 0)
        get = lambda what, clip: json.loads(urllib.request.urlopen(f"{self.url}/api/session/{S}/{what}?video={clip}").read())
        self.assertEqual(get("events", "2026-10-06_001.mp4"), [])
        self.assertEqual(get("series", "2026-10-06_002.mp4"), [])

    def test_moment_times_are_checked(self):
        self.r.refresh("2026-10-07-s", probe_videos=False)
        for start, end in ((1e400, 2), (float("nan"), 2), (5, 1)):
            with self.assertRaises(RemoteError, msg=(start, end)):
                self.r.tag("2026-10-07-s", "2026-10-07_001.mp4", start, end, "bad", [])
        d = json.loads(urllib.request.urlopen(self.url + "/api/session/2026-10-07-s/data").read())
        self.assertEqual([m for m in d["moments"] if m["title"] == "bad"], [])

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

    def test_cli_proxy(self):
        r = self.r
        out = r.cli(["tags"])
        self.assertEqual(out["code"], 0, out["stderr"])
        self.assertIn("tricks", out["stdout"])
        with self.assertRaises(RemoteError):
            r.cli(["serve"])                                   # not for clients
        # the file endpoint hands out cache files only, and a proxied command gets names, never paths
        import urllib.error
        outside = Path(self.tmp.name) / "outside.txt"
        outside.write_text("not for the client")
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(self.url + "/api/file?path=" + str(outside))
        self.assertEqual(cm.exception.code, 403)
        with self.assertRaises(RemoteError):
            r.cli(["frame", "2026-10-07-s", str(outside), "0"])

    def test_unknown_session_is_404_and_edits_take_names_only(self):
        import urllib.error
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
        import urllib.error
        for path in ("/video/..%2F../t.db", "/frame/..%2F../t.db/0", "/static/..%2F..%2Fpyproject.toml"):
            with self.assertRaises(urllib.error.HTTPError, msg=path) as cm:
                urllib.request.urlopen(self.url + path)
            self.assertEqual(cm.exception.code, 404, path)
        self.assertTrue((Path(self.tmp.name) / "t.db").exists())

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
