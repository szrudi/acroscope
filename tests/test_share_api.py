"""The share API (#57, the player's side): a preview rendered into the cache and served back to the player, a
share published from it (or afresh), the list, the takedown; audio and a poster frame in the bundle; what is
refused. Against a server child with the fixture of test_share (a real clip, a fake arm)."""
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

try:
    from tests.test_share import CLIP, LOG, SESSION, build, cli, ffprobe, free_port
except ImportError:
    from test_share import CLIP, LOG, SESSION, build, cli, ffprobe, free_port

PY = sys.executable


class ShareApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.tmp.name)
        cls.env = build(cls.root)
        cls.port = free_port()
        cls.proc = subprocess.Popen([PY, "-m", "acroscope.cli", "serve", "--host", "127.0.0.1", "--port", str(cls.port), "--no-warm"],
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
        from acroscope.remote import Remote
        cls.api = Remote(cls.url)
        cls.api.refresh(SESSION)
        cls.api.set_match(SESSION, CLIP, LOG, 1, 2.0)                                   # arm 1 covers video 2.0-4.99
        cls.api.tag(SESSION, CLIP, 3.0, 4.0, "half roll", ["roll"])                     # m01
        cls.api.tag(SESSION, CLIP, 6.0, 7.0, "late", ["roll"])                          # m02: no arm there

    @classmethod
    def tearDownClass(cls):
        cls.proc.terminate()
        cls.proc.wait(5)
        cls.tmp.cleanup()

    def call(self, method, path, body=None):
        """(status, json) without raising."""
        req = urllib.request.Request(self.url + path, data=json.dumps(body).encode() if body is not None else None, method=method,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                return r.status, json.loads(r.read() or b"null")
        except urllib.error.HTTPError as e:
            raw = e.read()
            try:
                return e.code, json.loads(raw)
            except ValueError:
                return e.code, raw

    def get(self, path):
        req = urllib.request.Request(self.url + path)
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.status, r.read(), dict(r.headers)
        except urllib.error.HTTPError as e:
            return e.code, e.read(), dict(e.headers)

    def test_preview_then_publish(self):
        """The player's flow: a preview rendered into the cache and shown from /share-preview/, then published
        from it (the clip is not cut again), then listed, then taken down."""
        st, p = self.call("POST", "/api/share/preview", {"session": SESSION, "moment": "m01", "pad": 1, "text": "a 1/2 roll", "poster": 3.5})
        self.assertEqual(st, 200, p)
        self.assertRegex(p["preview"], r"^[a-z0-9]{12,}$")
        self.assertEqual((p["start"], p["end"], p["sticks"], p["audio"], p["poster"]), (2.0, 5.0, True, False, 3.5))
        self.assertEqual(p["url"], f"/share-preview/{p['preview']}/")
        st, html, _ = self.get(p["url"])
        self.assertEqual(st, 200)
        self.assertIn(b"a 1/2 roll", html)
        self.assertIn(b'poster="poster.jpg"', html)
        self.assertIn(b'property="og:image" content="poster.jpg"', html)                  # no token yet: relative
        st, clip, h = self.get(p["url"] + "clip.mp4")
        self.assertEqual((st, h.get("Content-Type"), h.get("Accept-Ranges")), (200, "video/mp4", "bytes"))
        st, poster, h = self.get(p["url"] + "poster.jpg")
        self.assertEqual((st, h.get("Content-Type"), poster[:2]), (200, "image/jpeg", b"\xff\xd8"))
        pdir = self.root / "cache" / "share-preview" / p["preview"]
        self.assertTrue((pdir / "clip.mp4").exists())
        clip_size = (pdir / "clip.mp4").stat().st_size
        # the preview is in the cache, not in the shares dir, and not a share
        self.assertFalse((self.root / "shares" / p["preview"]).exists())
        st, lst = self.call("GET", "/api/shares")
        self.assertEqual((st, lst["shares"], lst["public"], lst["base"]), (200, [], True, "https://share.example"))
        # publish from it: the same window, text and poster, so the clip is taken as it is
        st, s = self.call("POST", "/api/share", {"session": SESSION, "moment": "m01", "pad": 1, "text": "a 1/2 roll", "poster": 3.5,
                                                 "expires": "7d", "preview": p["preview"]})
        self.assertEqual(st, 200, s)
        self.assertRegex(s["token"], r"^[a-z0-9]{24}$")
        self.assertEqual((s["start"], s["end"], s["text"], s["sticks"], s["audio"], s["poster"]), (2.0, 5.0, "a 1/2 roll", True, False, 3.5))
        self.assertEqual(s["url"], f"https://share.example/{s['token']}/")
        d = self.root / "shares" / s["token"]
        self.assertEqual(sorted(x.name for x in d.iterdir()), ["clip.mp4", "index.html", "poster.jpg"])
        self.assertEqual((d / "clip.mp4").stat().st_size, clip_size)
        self.assertFalse(pdir.exists())                                                  # moved, not copied
        html = (d / "index.html").read_text()
        self.assertIn(f'property="og:image" content="https://share.example/{s["token"]}/poster.jpg"', html)   # absolute now
        self.assertIn('property="og:title" content="a 1/2 roll"', html)
        self.assertIn(">a 1/2 roll<", html)
        self.assertNotIn("preview.json", {x.name for x in d.iterdir()})
        # listed, for this session and for all; the moment's title and the clip's readable name come along
        st, lst = self.call("GET", f"/api/shares?session={SESSION}")
        row = lst["shares"][0]
        self.assertEqual((row["token"], row["clip"], row["title"], row["state"], row["moment"]), (s["token"], "2026-10-04_001", "half roll", "active", "m01"))
        st, lst = self.call("GET", "/api/shares?session=2026-01-01-none")
        self.assertEqual(lst["shares"], [])
        # taken down
        st, r = self.call("DELETE", f"/api/share/{s['token']}")
        self.assertEqual((st, r["state"]), (200, "revoked"))
        self.assertFalse(d.exists())
        st, lst = self.call("GET", "/api/shares")
        self.assertEqual(lst["shares"], [])
        st, lst = self.call("GET", "/api/shares?all=1")
        self.assertEqual([x["state"] for x in lst["shares"] if x["token"] == s["token"]], ["revoked"])

    def test_publish_afresh_and_with_a_stale_preview(self):
        """No preview, or a preview of another window: the clip is cut for the share itself."""
        st, s = self.call("POST", "/api/share", {"session": SESSION, "moment": "m01", "start": 2.5, "end": 4.5, "audio": True})
        self.assertEqual(st, 200, s)
        self.assertEqual((s["start"], s["end"], s["text"], s["audio"], s["poster"]), (2.5, 4.5, "half roll", True, None))
        d = self.root / "shares" / s["token"]
        pr = ffprobe(d / "clip.mp4")
        self.assertEqual(sorted(st["codec_type"] for st in pr["streams"]), ["audio", "video"])
        self.assertAlmostEqual(float(pr["format"]["duration"]), 2.0, delta=0.15)
        self.assertNotIn("poster.jpg", {x.name for x in d.iterdir()})
        html = (d / "index.html").read_text()
        self.assertIn("const AUDIO = true;", html)
        self.assertNotIn("og:image", html)
        st, p = self.call("POST", "/api/share/preview", {"session": SESSION, "moment": "m01", "pad": 1})
        st, s2 = self.call("POST", "/api/share", {"session": SESSION, "moment": "m01", "pad": 2, "preview": p["preview"]})   # another window
        self.assertEqual(st, 200, s2)
        self.assertEqual((s2["start"], s2["end"]), (1.0, 6.0))
        self.assertTrue((self.root / "cache" / "share-preview" / p["preview"]).exists())       # the stale preview stays until it is dropped
        st, r = self.call("DELETE", f"/api/share/preview/{p['preview']}")
        self.assertEqual((st, r["removed"]), (200, True))
        self.assertFalse((self.root / "cache" / "share-preview" / p["preview"]).exists())
        for tok in (s["token"], s2["token"]):
            self.call("DELETE", f"/api/share/{tok}")

    def test_update_keeps_the_link(self):
        """Editing a share that is up (Rudi, 2026-10-22: "I can only re-publish?"): the bundle is made again under
        the same token and swapped in whole, the row follows, the link stays; the expiry is kept unless given;
        a preview is taken as it is; a revoked share is not updated, an unknown token is a 404. The row remembers
        the settings (sticks, sound, poster), so a panel can open prefilled."""
        st, s = self.call("POST", "/api/share", {"session": SESSION, "moment": "m01", "pad": 1, "text": "a", "poster": 3.5, "expires": "7d"})
        self.assertEqual(st, 200, s)
        tok, until = s["token"], s["expires_at"]
        d = self.root / "shares" / tok
        self.assertEqual((s["sticks"], s["audio"], s["poster"]), (True, False, 3.5))
        st, lst = self.call("GET", f"/api/shares?session={SESSION}")
        self.assertEqual({k: lst["shares"][0][k] for k in ("sticks", "audio", "poster")}, {"sticks": True, "audio": False, "poster": 3.5})
        st, u = self.call("POST", f"/api/share/{tok}", {"session": SESSION, "moment": "m01", "start": 2.5, "end": 4.5, "text": "b", "audio": True})
        self.assertEqual(st, 200, u)
        self.assertEqual((u["token"], u["start"], u["end"], u["text"], u["expires_at"], u["sticks"], u["audio"], u["poster"]), (tok, 2.5, 4.5, "b", until, True, True, None))
        self.assertEqual(sorted(x.name for x in d.iterdir()), ["clip.mp4", "index.html"])                 # the poster went with the edit
        pr = ffprobe(d / "clip.mp4")
        self.assertAlmostEqual(float(pr["format"]["duration"]), 2.0, delta=0.15)
        self.assertEqual(sorted(x["codec_type"] for x in pr["streams"]), ["audio", "video"])
        html = (d / "index.html").read_text()
        self.assertIn(">b<", html)
        self.assertIn("const AUDIO = true;", html)
        self.assertEqual(list((self.root / "shares").glob(".staging/*")), [])
        st, lst = self.call("GET", f"/api/shares?session={SESSION}")
        self.assertEqual([(x["token"], x["text"], x["start"], x["audio"], x["poster"]) for x in lst["shares"]], [(tok, "b", 2.5, True, None)])
        # a preview of the edit, taken as it is; a new expiry when given
        st, p = self.call("POST", "/api/share/preview", {"session": SESSION, "moment": "m01", "pad": 0.5, "text": "c", "poster": 3.2})
        size = (self.root / "cache" / "share-preview" / p["preview"] / "clip.mp4").stat().st_size
        st, u = self.call("POST", f"/api/share/{tok}", {"session": SESSION, "moment": "m01", "pad": 0.5, "text": "c", "poster": 3.2, "preview": p["preview"], "expires": "never"})
        self.assertEqual((st, u["token"], u["expires_at"], u["poster"]), (200, tok, None, 3.2))
        self.assertEqual((d / "clip.mp4").stat().st_size, size)
        self.assertTrue((d / "poster.jpg").exists())
        self.assertFalse((self.root / "cache" / "share-preview" / p["preview"]).exists())
        st, r = self.call("POST", "/api/share/" + "z" * 24, {"session": SESSION, "moment": "m01"})
        self.assertEqual(st, 404)
        self.call("DELETE", f"/api/share/{tok}")
        st, r = self.call("POST", f"/api/share/{tok}", {"session": SESSION, "moment": "m01"})
        self.assertEqual(st, 400)
        self.assertIn("revoked", r["error"])

    def test_refusals(self):
        bad = [({"session": SESSION, "moment": "m09"}, 404, "m09"),
               ({"session": "2026-01-01-nope", "moment": "m01"}, 404, "nope"),
               ({"session": SESSION, "moment": "m02"}, 400, "--no-sticks"),                 # no arm covers it
               ({"session": SESSION, "moment": "m01", "pad": 20}, 400, "pad"),
               ({"session": SESSION, "moment": "m01", "start": 5, "end": 4}, 400, "end"),
               ({"session": SESSION, "moment": "m01", "expires": "soon"}, 400, "soon"),
               ({"session": SESSION, "moment": "m01", "poster": 7.5}, 400, "poster"),       # outside the window
               ({"session": SESSION}, 400, "moment"),
               ({"session": SESSION, "video": CLIP, "start": 1, "end": 2, "sticks": False, "text": "no moment"}, 200, None)]   # a window of a clip, no moment
        for body, code, word in bad:
            st, r = self.call("POST", "/api/share", body)
            self.assertEqual(st, code, (body, r))
            if word:
                self.assertIn(word, r["error"], body)
            elif st == 200:
                self.assertEqual((r["moment"], r["start"], r["end"], r["sticks"]), (None, 1.0, 2.0, False))
                self.call("DELETE", f"/api/share/{r['token']}")
        st, r = self.call("POST", "/api/share/preview", {"session": SESSION, "moment": "m02", "sticks": False, "pad": 0.5})
        self.assertEqual((st, r["sticks"]), (200, False))
        pid = r["preview"]                                                                 # a folder that exists: '..' walks out of it
        st, r = self.call("DELETE", "/api/share/../x")
        self.assertIn(st, (400, 404))
        st, r = self.call("DELETE", "/api/share/" + "Z" * 24)
        self.assertEqual(st, 400)
        st, r = self.call("DELETE", "/api/share/" + "z" * 24)
        self.assertEqual(st, 404)
        for path in ("/share-preview/../../t.db", "/share-preview/nope00000000/index.html", "/share-preview/%2e%2e/x", "/share-preview/",
                     f"/share-preview/{pid}/..%2F..%2F..%2Ft.db", f"/share-preview/{pid}/preview.json", f"/share-preview/{pid}%2F..%2F..%2Ft.db/x"):
            st, body, _ = self.get(path)                                                    # an encoded '/' lands inside one segment
            self.assertIn(st, (400, 404), path)
            self.assertFalse(body.startswith(b"SQLite format 3"), path)
        st, r = self.call("DELETE", "/api/share/preview/not-a-preview")
        self.assertEqual(st, 400)
        # a match on a log not decoded yet: no decoding in the request (30 s with the GIL held), a 503 like metrics
        (self.root / "data" / "blackbox" / "cold.bbl").write_bytes(b"")
        self.api.set_match(SESSION, CLIP, "cold.bbl", 1, 0.0)
        try:
            st, r = self.call("POST", "/api/share/preview", {"session": SESSION, "moment": "m01"})
            self.assertEqual((st, "decoded" in r["error"]), (503, True))
        finally:
            self.api.unmatch(SESSION, CLIP, 1)
            self.api.set_match(SESSION, CLIP, LOG, 1, 2.0)                               # as the class set it up

    def test_old_previews_go_with_housekeeping(self):
        st, p = self.call("POST", "/api/share/preview", {"session": SESSION, "moment": "m01", "pad": 0.5})
        d = self.root / "cache" / "share-preview" / p["preview"]
        old = time.time() - 2 * 86400
        os.utime(d, (old, old))
        st, p2 = self.call("POST", "/api/share/preview", {"session": SESSION, "moment": "m01", "pad": 0.6})   # a fresh one stays
        r = cli(self.env, "housekeeping")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse(d.exists())
        self.assertTrue((self.root / "cache" / "share-preview" / p2["preview"]).exists())


if __name__ == "__main__":
    unittest.main()
