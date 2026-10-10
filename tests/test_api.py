"""The server's API through the remote client: a real `acroscope serve` child on a free port with a scratch
database and an empty data dir, driven exactly as the CLI on another machine drives it."""
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
        (root / "data" / "blackbox").mkdir()
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
        self.assertEqual(r.sessions()[0]["moments"], 0)
        self.assertEqual(r.purge("2026-10-07-s", days=0), [])          # nothing is missing
        self.assertFalse(r.untag("2026-10-07-s", "m99"))
        with self.assertRaises(RemoteError):
            r.set_match("2026-10-07-s", "nope.mp4", "x.bbl", "not-an-int", 0)

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
        r.tag("2026-10-07-s", "2026-10-07_001.mp4", 1, 2, "t", ["flip", "custom"])
        self.assertEqual(r.tag_usage("2026-10-07-s"), {"flip": 1, "custom": 1})


if __name__ == "__main__":
    unittest.main()
