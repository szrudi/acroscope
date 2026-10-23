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
        # a proxy nobody listens on: no font is fetched from here, and a fetch fails the way it does with no way out
        cls.env = dict(os.environ, ACROSCOPE_DB=str(root / "t.db"), ACROSCOPE_DATA=str(root / "data"), ACROSCOPE_CACHE=str(root / "cache"), ACROSCOPE_URL="",
                       https_proxy="http://127.0.0.1:9", HTTPS_PROXY="http://127.0.0.1:9")
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

    def call(self, method, path, body=None, code=200, raw=None):
        data = raw.encode() if raw is not None else json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.url + path, data=data, method=method,
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

    def cli(self, *args):
        """The CLI on another machine: a child pointed at this server."""
        r = subprocess.run([sys.executable, "-m", "acroscope.cli", *args], capture_output=True, text=True, timeout=300,
                           env=dict(self.env, ACROSCOPE_URL=self.url, ACROSCOPE_DB=str(Path(self.tmp.name) / "client.db")))
        return r.returncode, r.stdout, r.stderr

    def test_4_review_inputs(self):
        """#63 review: what a client sends wrong is a 400 that says what, never a 500."""
        p = self.derive()["profile"]
        for body, says in (({"dump": syn.DUMP, "mcm": 5}, "mcm: text"), ({"profile": {**p, "settings": ["a"]}}, "settings"),
                           ({"profile": {**p, "font": {"data": 5}}}, "font"), ({"dump": syn.DUMP, "mcm": self.mcm, "grid": [1]}, "grid")):
            self.assertIn(says, self.call("POST", "/api/osd/derive", body, 400)["error"])
        self.assertIn("font", self.call("POST", "/api/osd/doc", {"version": 1, "profiles": {"x": {**p, "font": {"data": 5}}}}, 400)["error"])
        raw = json.dumps({"profile": p, "session": S, "video": "001", "t": 1}).replace('"x": 3', '"x": Infinity')
        self.assertIn("grid x", self.call("POST", "/api/osd/probe", raw=raw, code=400)["error"])
        for t in (-1, "NaN"):
            raw = json.dumps({"profile": p, "session": S, "video": "001"})[:-1] + f', "t": {t}}}'
            self.assertIn("t:", self.call("POST", "/api/osd/probe", raw=raw, code=400)["error"])
        # rebuilt with another font, a profile keeps its note and its firmware line
        again = self.call("POST", "/api/osd/derive", {"profile": {**p, "note": "the garden quad"}, "mcm": syn.mcm_text(syn.font_chars(narrow=True)), "font": "narrow"})
        self.assertEqual((again["profile"]["note"], again["profile"]["font"]["name"], again["profile"]["version"]), ("the garden quad", "narrow", p["version"]))
        # a Configurator font with no way out: said, quickly, with what to do instead
        started = time.time()
        r = self.call("POST", "/api/osd/derive", {"dump": syn.DUMP, "font": "impact"}, 400)
        self.assertIn("upload its .mcm instead", r["error"])
        fonts = self.call("POST", "/api/osd/probe", {"profile": p, "session": S, "video": "001", "t": 10.3, "op": "fonts"})["fonts"]
        self.assertEqual([f["score"] for f in fonts], [None] * 10)
        self.assertLess(time.time() - started, 60)                             # one wait for the network, not ten

    def test_5_review_cli(self):
        """#63 review: the osd-profile commands, from a client: made from local files, stored through the server,
        read where the clips are; --json on either side of the subcommand; a frame past the clip's end refused."""
        tmp = Path(self.tmp.name)
        (tmp / "dump.txt").write_text(syn.DUMP)
        (tmp / "synth.mcm").write_text(self.mcm)
        code, out, err = self.cli("osd-profile", "derive", str(tmp / "dump.txt"), "--mcm", str(tmp / "synth.mcm"))
        self.assertEqual(code, 0, err)
        self.assertIn("timer 2 total dd:dd at column 22 row 11", out)
        self.assertEqual(self.call("GET", "/api/osd/doc")["doc"], {})           # derive stores nothing
        code, out, err = self.cli("osd-profile", "save", "cli-synth", str(tmp / "dump.txt"), "--mcm", str(tmp / "synth.mcm"), "--grid", "3,-4", "--note", "from the CLI")
        self.assertEqual(code, 0, err)
        stored = self.call("GET", "/api/osd/doc")["doc"]["profiles"]["cli-synth"]
        self.assertEqual((stored["font"]["name"], stored["note"]), ("synth", "from the CLI"))
        code, out, err = self.cli("osd-profile", "save", "otto", str(tmp / "dump.txt"), "--mcm", str(tmp / "synth.mcm"))
        self.assertNotEqual(code, 0)                                            # checked before it is stored
        self.assertIn("built-in", err)
        code, out, _ = self.cli("osd-profile", "--json", "list")
        self.assertIn("cli-synth", json.loads(out)["profiles"])
        code, out, _ = self.cli("osd-profile", "show", "cli-synth", "--json")
        self.assertEqual(json.loads(out)["profile"]["font"], "synth")
        for args in (["osd-profile", "--json", "read", "cli-synth", S, "001", "10.3"], ["osd-profile", "read", "--json", "cli-synth", S, "001", "10.3"]):
            code, out, err = self.cli(*args)
            self.assertEqual(code, 0, err)
            self.assertEqual(json.loads(out)["values"]["total"], 36)
        code, out, err = self.cli("osd-profile", "read", "cli-synth", S, "001", "10.3", "--fit")
        self.assertEqual((json.loads(out)["x"], json.loads(out)["y"]), (3, -4), err)
        code, out, err = self.cli("osd-profile", "read", "cli-synth", S, "001", "999")
        self.assertNotEqual(code, 0)
        self.assertIn("no frame at 999 s", err)
        self.assertIn("only `read`", self.call("POST", "/api/cli", {"args": ["osd-profile", "--json", "list"]}, 400)["error"])
        code, out, err = self.cli("osd-profile", "rm", "cli-synth")
        self.assertEqual(code, 0, err)
        self.assertNotIn("cli-synth", self.call("GET", "/api/osd/doc")["doc"].get("profiles", {}))
        self.r.set_setting("osd", {"version": 1, "profiles": {}})
        self.assertTrue(self.r.delete_setting("osd"))                           # the remote store's route for it
        self.assertEqual(self.call("GET", "/api/osd/doc")["doc"], {})

    def test_6_review_a_broken_stored_profile(self):
        """#63 review: a stored profile that no longer derives is listed, and does not block saving the others."""
        from acroscope.db import Db
        p = self.derive()["profile"]
        broken = {**p, "grid": {**p["grid"], "cw": 7}}                          # a check stricter than when it was stored
        db = Db(Path(self.tmp.name) / "t.db")
        db.set_setting("osd", {"version": 1, "profiles": {"old": broken}})
        d = self.call("GET", "/api/osd")
        self.assertIn("grid cw", d["errors"]["old"])
        doc = self.call("GET", "/api/osd/doc")
        saved = self.call("POST", "/api/osd/doc?etag=" + doc["etag"], {"version": 1, "profiles": {"old": broken, "new": p}})
        self.assertEqual(sorted(saved["doc"]["profiles"]), ["new", "old"])
        self.call("DELETE", "/api/osd/doc")


if __name__ == "__main__":
    unittest.main()
