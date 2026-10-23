"""The OSD profiles through the server (#10): a real `acroscope serve` child on a scratch data dir holding one
synthetic clip with a Betaflight OSD drawn in a made-up font (tests/osd_synthetic.py). Derive a profile from a dump
and a font, look at a frame with it (the configuration page's OSD tab), store it, and see the reader use it."""
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from acroscope.remote import Remote

try:
    from tests import osd_synthetic as syn
    from tests.test_api import free_port
except ImportError:
    import osd_synthetic as syn
    from test_api import free_port

S = "2026-10-04-motor-jitter-testing"


class OsdApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        root = Path(cls.tmp.name)
        cls.chars = syn.font_chars()
        cls.mcm = syn.mcm_text(cls.chars)
        syn.make_clip(root / "data" / "videos" / S / "2026-10-04_001.mp4", cls.chars, {"x": 3, "y": -4, "cw": 24, "ch": 36})
        (root / "data" / "blackbox").mkdir(parents=True)
        cls.port = free_port()
        (root / "fonts").mkdir()                    # no Configurator font: the shipped profiles stay out, nothing is fetched
        cls.env = dict(os.environ, ACROSCOPE_DB=str(root / "t.db"), ACROSCOPE_DATA=str(root / "data"), ACROSCOPE_CACHE=str(root / "cache"), ACROSCOPE_URL="",
                       ACROSCOPE_FONTS=str(root / "fonts"))
        cls.proc = subprocess.Popen([sys.executable, "-m", "acroscope.cli", "serve", "--host", "127.0.0.1", "--port", str(cls.port), "--no-warm"],
                                    env=cls.env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
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
        cls.r.refresh(S)

    @classmethod
    def tearDownClass(cls):
        cls.proc.terminate()
        cls.proc.wait(5)
        cls.tmp.cleanup()

    def call(self, method, path, body=None, code=200):
        req = urllib.request.Request(self.url + path, data=json.dumps(body).encode() if body is not None else None, method=method,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                self.assertEqual(r.status, code)
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            body = e.read()
            self.assertEqual(e.code, code, body[:300])
            return json.loads(body or b"{}")

    def derive(self):
        return self.call("POST", "/api/osd/derive", {"dump": syn.DUMP, "mcm": self.mcm, "font": "synth"})

    def test_1_derive(self):
        d = self.call("GET", "/api/osd")
        self.assertEqual(d["profiles"], {})
        self.assertIn("impact", d["fonts"])
        self.assertIn("otto-bold-v3", d["builtin"])
        r = self.derive()
        self.assertEqual(r["layout"]["formats"], ["dd:dd", "dd:dd.d"])
        self.assertEqual(r["layout"]["x0"], [3 + 24 * 23, 3 + 24 * 21])
        self.assertNotIn("tpl", r["layout"])                                  # the page draws cells, not vectors
        self.assertEqual(len(r["glyphs"]["0"]), 18)
        self.assertTrue(r["profile"]["font"]["data"])
        self.assertNotIn("pilot_name", r["profile"]["settings"])
        # the settings kept and another grid: derived again without the dump
        r2 = self.call("POST", "/api/osd/derive", {"settings": r["profile"]["settings"], "mcm": self.mcm, "font": "synth", "grid": {"x": 5, "y": -2}})
        self.assertEqual(r2["layout"]["x0"][0], 5 + 24 * 23)
        self.assertIn("paste the dump", self.call("POST", "/api/osd/derive", {"mcm": self.mcm}, 400)["error"])
        self.assertIn("MAX7456", self.call("POST", "/api/osd/derive", {"dump": syn.DUMP, "mcm": "nope"}, 400)["error"])
        self.assertIn("Configurator", self.call("POST", "/api/osd/derive", {"dump": syn.DUMP, "font": "../../etc/passwd"}, 400)["error"])

    def test_2_probe_fit_on_a_frame(self):
        p = self.derive()["profile"]
        r = self.call("POST", "/api/osd/probe", {"profile": p, "session": S, "video": "001", "t": 10.3})
        self.assertEqual(r["values"]["total"], 36)
        self.assertAlmostEqual(r["values"]["arm"], 6.3, delta=0.25)
        self.assertEqual(r["counter"], 7)
        self.assertTrue(all(c["digit"] for c in r["cells"] if c["kind"] == "timer" and c["fmt"] == "d"))
        s = self.call("POST", "/api/osd/probe", {"profile": p, "session": S, "video": "001", "t": 26.0})
        self.assertEqual((s["stats"]["total_arm"], s["stats"]["blackbox"]), (50, 42))
        off = {**p, "grid": {**p["grid"], "x": p["grid"]["x"] - 4, "y": p["grid"]["y"] + 3}}
        f = self.call("POST", "/api/osd/probe", {"profile": off, "session": S, "video": "001", "t": 10.3, "op": "fit"})
        self.assertEqual((f["x"], f["y"]), (3, -4))
        self.assertIn("op", self.call("POST", "/api/osd/probe", {"profile": p, "session": S, "video": "001", "t": 1, "op": "rm"}, 400)["error"])
        self.assertIn("t:", self.call("POST", "/api/osd/probe", {"profile": p, "session": S, "video": "001", "t": "1"}, 400)["error"])
        self.call("POST", "/api/osd/probe", {"profile": p, "session": S, "video": "../../t.db", "t": 1}, 404)
        self.assertIn("no frame", self.call("POST", "/api/osd/probe", {"profile": p, "session": S, "video": "001", "t": 999}, 400)["error"])
        huge = {**p, "grid": {**p["grid"], "w": 100000, "h": 100000}}               # nobody's DVR: refused before ffmpeg runs
        self.assertIn("grid w", self.call("POST", "/api/osd/probe", {"profile": huge, "session": S, "video": "001", "t": 1}, 400)["error"])

    def test_3_store_and_read(self):
        p = self.derive()["profile"]
        doc = self.call("GET", "/api/osd/doc")
        self.assertEqual(doc["doc"], {})
        stored = self.call("POST", "/api/osd/doc?etag=" + doc["etag"], {"version": 1, "profiles": {"synth": p}})
        self.assertIn("synth", stored["doc"]["profiles"])
        self.assertIn("changed since", self.call("POST", "/api/osd/doc?etag=" + doc["etag"], {"version": 1, "profiles": {}}, 409)["error"])
        self.assertIn("built-in", self.call("POST", "/api/osd/doc", {"version": 1, "profiles": {"otto": p}}, 400)["error"])
        d = self.call("GET", "/api/osd")
        self.assertEqual(d["profiles"]["synth"]["font"], "synth")
        self.assertEqual(d["profiles"]["synth"]["layout"]["arm_row"], 1)
        # the CLI on another machine: the store call through the API, the reading through the proxy
        self.assertIn("synth", self.r.setting("osd")["value"]["profiles"])
        out = self.r.cli(["osd-profile", "read", "synth", S, "001", "10.3"])
        self.assertEqual(out["code"], 0, out["stderr"])
        self.assertIn("total 0:36.0", out["stdout"])
        self.assertIn("flight #7", out["stdout"])
        out = self.r.cli(["osd", S, "001"])                                    # the reader picks the profile among the built-ins
        self.assertEqual(out["code"], 0, out["stderr"])
        self.assertIn("layout synth", out["stdout"])
        self.assertIn("arm run: video", out["stdout"])
        self.assertIn("total arm 0:50.0, blackbox 42 %", out["stdout"])
        refused = self.call("POST", "/api/cli", {"args": ["osd-profile", "save", "x", "dump.txt"]}, 400)
        self.assertIn("only `read`", refused["error"])
        self.assertTrue(self.call("DELETE", "/api/osd/doc")["removed"])
        self.assertEqual(self.call("GET", "/api/osd")["profiles"], {})


if __name__ == "__main__":
    unittest.main()
