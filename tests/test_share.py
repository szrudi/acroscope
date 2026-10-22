"""Sharing one trick (#57): a moment rendered as a static bundle on a secret link.

The store's rows, the window and the stick samples, the bundle (a cut of the clip without sound or metadata and
one HTML file that names nothing and fetches nothing), the push and revoke hooks, expiry through housekeeping, and
the command through a server as the CLI on another machine runs it. The clip is made with ffmpeg, the arm is a
fake in the cache (sticks only)."""
import array
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

from acroscope import blackbox, share
from acroscope.db import Db

SESSION = "2026-10-04-motor-jitter-testing"
CLIP = "2026-10-04_001.mp4"
LOG = "BTFL_BLACKBOX_LOG_TEST_20261004_120000.bbl"
PY = sys.executable


def make_clip(path: Path, seconds: float = 8.0) -> None:
    """A test pattern with a sound track and metadata that must not reach a share."""
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"testsrc=d={seconds}:s=160x120:r=30",
                    "-f", "lavfi", "-i", f"sine=f=440:d={seconds}", "-c:v", "libx264", "-preset", "ultrafast", "-crf", "30",
                    "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "32k", "-shortest",
                    "-metadata", "title=SECRET-TITLE", "-metadata", "comment=SECRET-COMMENT", "-movflags", "+faststart", str(path)], check=True)


def fake_arm(cache: Path, stem: str, length: float, rate: float, cols: dict) -> None:
    """A decoded arm in the cache as index_bbl would leave it: `cols` maps a field to a function of the arm time."""
    n = int(length * rate)
    t = array.array("d", (i / rate for i in range(n)))
    meta = {"bbl": stem, "file": stem + ".bbl", "index": 1, "frames": n, "length": t[-1], "rate": rate, "fields": ["time", *cols]}
    (cache / "arms" / stem).mkdir(parents=True, exist_ok=True)
    blackbox._save(meta, t, {k: array.array("f", (f(x) for x in t)) for k, f in cols.items()}, cache / "arms" / stem / "arm-01.bin")
    (cache / "arms" / stem / "arms.json").write_text(json.dumps({"v": blackbox.INDEX_VERSION, "file": stem + ".bbl", "stem": stem, "date": "2026-10-04",
                                                                 "arms": [{k: v for k, v in meta.items() if k != "fields"}], "boots": []}))


def build(root: Path) -> dict:
    """A data dir with one 8 s clip, one (empty) log whose arm 1 is a fake of 3 s in the cache: roll 50 %, pitch
    -20 %, yaw stick 60 % LEFT (rcCommand[2] +), throttle rising 10 %/s. The environment for a CLI against it."""
    data, cache = root / "data", root / "cache"
    (data / "videos" / SESSION).mkdir(parents=True)
    (data / "blackbox").mkdir()
    (data / "blackbox" / LOG).write_bytes(b"")
    make_clip(data / "videos" / SESSION / CLIP)
    fake_arm(cache, LOG[:-4], 3.0, 100, {"rcCommand[0]": lambda t: 250.0, "rcCommand[1]": lambda t: -100.0,
                                          "rcCommand[2]": lambda t: 300.0, "rcCommand[3]": lambda t: 1000.0 + 100.0 * t})
    return dict(os.environ, ACROSCOPE_DATA=str(data), ACROSCOPE_DB=str(root / "t.db"), ACROSCOPE_CACHE=str(cache),
                ACROSCOPE_URL="", ACROSCOPE_SHARES=str(root / "shares"), ACROSCOPE_SHARE_URL="https://share.example",
                ACROSCOPE_SHARE_PUSH="", ACROSCOPE_SHARE_REVOKE="")


def cli(env: dict, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run([PY, "-m", "acroscope.cli", *args], env=env, capture_output=True, text=True)


def ffprobe(path: Path) -> dict:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration:format_tags:stream=codec_type,codec_name",
                          "-of", "json", str(path)], capture_output=True, text=True, check=True).stdout
    return json.loads(out)


class PureTest(unittest.TestCase):
    def test_token(self):
        toks = {share.new_token() for _ in range(50)}
        self.assertEqual(len(toks), 50)
        for t in toks:
            self.assertRegex(t, r"^[a-z0-9]{24}$")                       # lower case: a case-blind file system keeps them apart
            self.assertTrue(share.token_ok(t))
        for bad in ("", "../x", "ABCDEFGHIJKLMNOPQRSTUVWX", "a" * 15, "a" * 65, "abc/def", "abcdefghijklmnopqrstuvw.", "-abcdefghijklmnopqrstuvwx"):
            self.assertFalse(share.token_ok(bad), bad)

    def test_expiry(self):
        now = datetime(2026, 10, 22, 12, 0, tzinfo=timezone.utc)
        self.assertEqual(share.parse_expiry("30d", now), "2026-11-21T12:00:00+00:00")
        self.assertEqual(share.parse_expiry("12h", now), "2026-10-23T00:00:00+00:00")
        self.assertEqual(share.parse_expiry("2w", now), "2026-11-05T12:00:00+00:00")
        self.assertEqual(share.parse_expiry("90m", now), "2026-10-22T13:30:00+00:00")
        self.assertIsNone(share.parse_expiry("never", now))
        for bad in ("", "soon", "30", "-3d", "3 d", "1y", "0.5d"):
            with self.assertRaises(ValueError, msg=bad):
                share.parse_expiry(bad, now)

    def test_window(self):
        """The moment plus the padding, clamped to the clip; a share is one trick, not a clip."""
        self.assertEqual(share.window(4.0, 5.0, 2.0, 60.0), (2.0, 7.0))
        self.assertEqual(share.window(1.0, 5.0, 2.0, 6.0), (0.0, 6.0))          # clamped at both ends
        self.assertEqual(share.window(4.0, 4.0, 0.0, 60.0), (3.75, 4.25))        # a point: half a second around it
        for start, end, pad, dur in ((4.0, 5.0, share.PAD_MAX + 0.1, 60.0), (4.0, 5.0, -1.0, 60.0),
                                     (4.0, 4.0 + share.MAX_SECONDS, 2.0, 600.0), (70.0, 80.0, 2.0, 60.0)):
            with self.assertRaises(ValueError, msg=(start, end, pad, dur)):
                share.window(start, end, pad, dur)

    def test_stick_samples(self):
        """The four channels over the window, every step, in the picture's terms (Mode 2, + = right or up, yaw
        negated as the player's API does); null where the arm does not cover the video."""
        n = 300
        t = array.array("d", (i / 100 for i in range(n)))
        cols = {"rcCommand[0]": array.array("f", [250.0] * n), "rcCommand[1]": array.array("f", [-100.0] * n),
                "rcCommand[2]": array.array("f", [300.0] * n), "rcCommand[3]": array.array("f", (1000.0 + 100.0 * x for x in t))}
        arm = blackbox.Arm({"bbl": "x", "file": "x.bbl", "index": 1, "frames": n, "length": t[-1], "rate": 100.0, "fields": ["time", *cols]}, t, cols)
        s = share.stick_samples(arm, offset=2.0, w0=2.0, w1=5.0, step=0.02)
        self.assertEqual(s["step"], 0.02)
        self.assertEqual(len(s["throttle"]), 151)
        self.assertEqual((s["roll"][0], s["pitch"][0], s["yaw"][0], s["throttle"][0]), (50, -20, -60, 0))
        self.assertEqual(s["throttle"][50], 10)                                   # one second in: 10 %
        self.assertEqual(s["throttle"][149], 30)                                  # 2.98 s: the last frame at or before
        self.assertEqual([s[k][150] for k in ("roll", "pitch", "yaw", "throttle")], [None] * 4)   # 3.0 s: past the arm
        s = share.stick_samples(arm, offset=4.0, w0=2.0, w1=5.0, step=0.5)       # the arm starts 2 s into the window
        self.assertEqual(s["throttle"], [None, None, None, None, 0, 5, 10])

    def test_page_escapes_and_names_nothing(self):
        html = share.page('<b>hi</b> & "you" </script>', {"step": 0.02, "throttle": [1], "yaw": [2], "pitch": [3], "roll": [None]})
        self.assertIn("&lt;b&gt;hi&lt;/b&gt; &amp; &quot;you&quot; &lt;/script&gt;", html)
        self.assertNotIn("<b>hi", html)
        self.assertIn('<meta name="robots" content="noindex', html)
        self.assertIn('"roll": [null]', html)
        self.assertEqual(set(re.findall(r'(?:src|href)=["\']([^"\']+)', html)), {"clip.mp4"})   # nothing else is fetched
        self.assertNotIn("http", html.lower())
        self.assertIn("<title>&lt;b&gt;hi&lt;/b&gt; &amp; &quot;you&quot; &lt;/script&gt;</title>", html)
        html = share.page("", None)
        self.assertIn("<title>acroscope</title>", html)
        self.assertIn("const STICKS = null;", html)

    def test_hook_runs_without_a_shell(self):
        """The push/revoke command: split like a shell would, {dir} and {token} filled in per argument, no shell
        (a token is [a-z0-9] and a dir is ours, but a shell would be one more thing to get right)."""
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "out.txt"
            share.run_hook(f'{PY} -c "import sys; open(sys.argv[1], \'w\').write(sys.argv[2] + chr(10) + sys.argv[3])" {out} {{dir}} "x {{token}} y"',
                           "abc123", Path("/some where/abc123"))
            self.assertEqual(out.read_text(), "/some where/abc123\nx abc123 y")
            with self.assertRaises(share.ShareError) as cm:
                share.run_hook(f'{PY} -c "import sys; sys.stderr.write(\'boom\'); sys.exit(3)"', "abc123", Path(tmp))
            self.assertIn("boom", str(cm.exception))
            self.assertIn("3", str(cm.exception))


class DbTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Db(Path(self.tmp.name) / "t.db")

    def tearDown(self):
        self.tmp.cleanup()

    def test_rows_and_states(self):
        past = (datetime.now(timezone.utc) - timedelta(days=1)).replace(microsecond=0).isoformat()
        future = (datetime.now(timezone.utc) + timedelta(days=1)).replace(microsecond=0).isoformat()
        a = self.db.add_share("a" * 24, SESSION, CLIP, "m01", 3.0, 6.0, "half roll", future)
        self.assertEqual(a["state"], "active")
        self.db.add_share("b" * 24, SESSION, CLIP, "m01", 3.0, 6.0, "old", past)
        self.db.add_share("c" * 24, SESSION, CLIP, None, 1.0, 2.0, "forever", None)
        self.assertEqual([r["token"] for r in self.db.shares()], ["a" * 24, "c" * 24])            # active only
        self.assertEqual({r["token"]: r["state"] for r in self.db.shares(all=True)}, {"a" * 24: "active", "b" * 24: "expired", "c" * 24: "active"})
        self.assertTrue(self.db.revoke_share("c" * 24))
        self.assertFalse(self.db.revoke_share("c" * 24))                                          # once
        self.assertFalse(self.db.revoke_share("nope"))
        self.assertEqual(self.db.share("c" * 24)["state"], "revoked")
        self.assertIsNotNone(self.db.share("c" * 24)["revoked_at"])
        self.assertEqual([r["token"] for r in self.db.shares()], ["a" * 24])
        self.assertIsNone(self.db.share("zzz"))
        with self.assertRaises(Exception):
            self.db.add_share("a" * 24, SESSION, CLIP, "m01", 3.0, 6.0, "again", future)          # a token is used once

    def test_rows_follow_a_rename_and_a_move(self):
        self.db.upsert_video(SESSION, CLIP)
        self.db.add_share("a" * 24, SESSION, CLIP, "m01", 3.0, 6.0, "x", None)
        self.db.rename_session(SESSION, "2026-10-04-renamed")
        self.assertEqual(self.db.share("a" * 24)["session"], "2026-10-04-renamed")
        self.db.move_clip("2026-10-04-renamed", CLIP, "2026-10-04-other")
        self.assertEqual((self.db.share("a" * 24)["session"], self.db.share("a" * 24)["video"]), ("2026-10-04-other", CLIP))


class ShareTest(unittest.TestCase):
    """The command against a local store: the bundle, what it leaves out, the hooks, expiry."""
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.tmp.name)
        cls.env = build(cls.root)
        r = cli(cls.env, "refresh", SESSION)
        assert r.returncode == 0, r.stderr
        assert cli(cls.env, "match", SESSION, "001", LOG, "1", "2").returncode == 0      # arm 1 covers video 2.0-4.99
        assert cli(cls.env, "tag", SESSION, "001", "3", "4", "half roll", "--tags", "roll").returncode == 0     # m01
        assert cli(cls.env, "tag", SESSION, "001", "6", "7", "late", "--tags", "roll").returncode == 0          # m02: no arm there
        assert cli(cls.env, "tag", SESSION, "001", "0", "8", "whole", "--tags", "poi").returncode == 0          # m03

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def names(self) -> set:
        """What is in the shares dir (nothing, before the first share)."""
        d = self.root / "shares"
        return {p.name for p in d.iterdir()} if d.is_dir() else set()

    def share(self, *args, env=None, ok=True):
        r = cli(env or self.env, "share", SESSION, *args)
        if ok:
            self.assertEqual(r.returncode, 0, r.stderr)
            return json.loads(r.stdout)
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertNotIn("Traceback", r.stderr)
        return r.stderr

    def test_bundle(self):
        s = self.share("m01", "--pad", "1")
        self.assertRegex(s["token"], r"^[a-z0-9]{24}$")
        self.assertEqual(s["url"], f"https://share.example/{s['token']}/")
        self.assertEqual((s["start"], s["end"], s["text"], s["sticks"]), (2.0, 5.0, "half roll", True))
        self.assertEqual(s["expires_at"][:10], (datetime.now(timezone.utc) + timedelta(days=30)).strftime("%Y-%m-%d"))   # 30 days by default
        d = self.root / "shares" / s["token"]
        self.assertEqual(sorted(p.name for p in d.iterdir()), ["clip.mp4", "index.html"])
        self.assertEqual(list((self.root / "shares").glob(".staging/*")), [])             # nothing half-rendered left
        # the clip: the window, one video stream, no sound, nothing from the source's metadata
        p = ffprobe(d / "clip.mp4")
        self.assertAlmostEqual(float(p["format"]["duration"]), 3.0, delta=0.15)
        self.assertEqual([(st["codec_type"], st["codec_name"]) for st in p["streams"]], [("video", "h264")])
        self.assertNotIn("title", p["format"].get("tags", {}))
        self.assertNotIn("comment", p["format"].get("tags", {}))
        self.assertNotIn(b"SECRET", (d / "clip.mp4").read_bytes())
        # the page: the sticks of the window inline, null past the arm, the text, and nothing that names the source
        html = (d / "index.html").read_text()
        m = re.search(r"const STICKS = (.*?);\n", html)
        sticks = json.loads(m.group(1))
        self.assertEqual(sticks["step"], 0.02)
        self.assertEqual(len(sticks["throttle"]), 151)
        self.assertEqual((sticks["roll"][0], sticks["pitch"][0], sticks["yaw"][0], sticks["throttle"][0]), (50, -20, -60, 0))
        self.assertEqual(sticks["throttle"][100], 20)
        self.assertIsNone(sticks["throttle"][150])
        self.assertIn(">half roll<", html)
        for secret in (SESSION, CLIP, LOG, LOG[:-4], "m01", "2026-10-04", "2026", str(self.root)):
            self.assertNotIn(secret, html, secret)
        self.assertEqual(set(re.findall(r'(?:src|href)=["\']([^"\']+)', html)), {"clip.mp4"})
        self.assertNotIn("http", html.lower())
        self.assertIn('name="robots" content="noindex', html)
        # the root of the shares dir answers with a page of its own that lists nothing
        root_html = (self.root / "shares" / "index.html").read_text()
        self.assertNotIn(s["token"], root_html)
        # listed, as the CLI on another machine would see it
        r = cli(self.env, "shares", "--json")
        self.assertEqual(r.returncode, 0, r.stderr)
        rows = [x for x in json.loads(r.stdout) if x["token"] == s["token"]]
        self.assertEqual((rows[0]["state"], rows[0]["url"], rows[0]["moment"], rows[0]["session"]), ("active", s["url"], "m01", SESSION))
        r = cli(self.env, "shares")
        self.assertIn(s["token"], r.stdout)
        self.assertIn("half roll", r.stdout)
        self.assertIn(" 001 ", r.stdout)                                                    # the clip by its readable name's number

    def test_text_and_expiry_options(self):
        s = self.share("m01", "--text", '<b>hi</b> & a 1/2 roll', "--expires", "never")
        self.assertIsNone(s["expires_at"])
        html = (self.root / "shares" / s["token"] / "index.html").read_text()
        self.assertIn("&lt;b&gt;hi&lt;/b&gt; &amp; a 1/2 roll", html)
        self.assertNotIn("<b>hi", html)
        err = self.share("m01", "--expires", "soon", ok=False)
        self.assertIn("soon", err)
        err = self.share("m01", "--pad", "11", ok=False)
        self.assertIn("pad", err)

    def test_needs_a_match_unless_told_otherwise(self):
        err = self.share("m02", ok=False)
        self.assertIn("--no-sticks", err)
        self.assertEqual([p.name for p in (self.root / "shares").glob("*") if p.is_dir() and p.name.startswith(".")], [])
        s = self.share("m02", "--no-sticks", "--pad", "0.5")
        self.assertEqual((s["sticks"], s["start"], s["end"]), (False, 5.5, 7.5))
        self.assertIn("const STICKS = null;", (self.root / "shares" / s["token"] / "index.html").read_text())
        err = self.share("m09", ok=False)                                                 # no such moment
        self.assertIn("m09", err)

    def test_window_is_clamped_to_the_clip(self):
        s = self.share("m03", "--pad", "3", "--no-sticks")                                # the whole clip: no arm covers its start
        self.assertEqual((s["start"], s["end"]), (0.0, 8.0))
        self.assertAlmostEqual(float(ffprobe(self.root / "shares" / s["token"] / "clip.mp4")["format"]["duration"]), 8.0, delta=0.15)

    def test_audio_poster_and_an_explicit_window(self):
        """--audio keeps the clip's sound (an AAC track), --poster <t> adds the frame at that video time as the
        page's poster and preview image, --from/--to set the window themselves (no padding)."""
        s = self.share("m01", "--audio", "--poster", "3.5", "--from", "2.5", "--to", "4.5")
        self.assertEqual((s["start"], s["end"], s["audio"], s["poster"]), (2.5, 4.5, True, 3.5))
        d = self.root / "shares" / s["token"]
        self.assertEqual(sorted(p.name for p in d.iterdir()), ["clip.mp4", "index.html", "poster.jpg"])
        p = ffprobe(d / "clip.mp4")
        self.assertEqual(sorted((st["codec_type"], st["codec_name"]) for st in p["streams"]), [("audio", "aac"), ("video", "h264")])
        self.assertAlmostEqual(float(p["format"]["duration"]), 2.0, delta=0.15)
        self.assertEqual((d / "poster.jpg").read_bytes()[:2], b"\xff\xd8")
        html = (d / "index.html").read_text()
        self.assertIn('<video id="v" src="clip.mp4" poster="poster.jpg"', html)
        self.assertIn(f'<meta property="og:image" content="https://share.example/{s["token"]}/poster.jpg">', html)
        self.assertIn("const AUDIO = true;", html)
        self.assertEqual(set(re.findall(r'(?:src|href|content|poster)=["\']([^"\']+)', html)) - {"noindex, nofollow, noarchive", "no-referrer", "#14161a", "width=device-width, initial-scale=1, viewport-fit=cover", "half roll", "summary_large_image"},
                         {"clip.mp4", "poster.jpg", f"https://share.example/{s['token']}/poster.jpg"})
        err = self.share("m01", "--poster", "7", ok=False)                                # outside the window
        self.assertIn("poster", err)
        s = self.share("m01")                                                             # the defaults: no sound, no poster
        html = (self.root / "shares" / s["token"] / "index.html").read_text()
        self.assertIn("const AUDIO = false;", html)
        self.assertNotIn("poster", html)
        self.assertNotIn("og:", html)

    def test_revoke(self):
        s = self.share("m01")
        d = self.root / "shares" / s["token"]
        r = cli(self.env, "unshare", s["token"])
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout)["state"], "revoked")
        self.assertFalse(d.exists())
        self.assertEqual(json.loads(cli(self.env, "unshare", s["token"]).stdout)["state"], "revoked")   # again: still revoked, no error
        r = cli(self.env, "shares", "--json")
        self.assertNotIn(s["token"], [x["token"] for x in json.loads(r.stdout)])
        r = cli(self.env, "shares", "--all", "--json")
        self.assertEqual([x["state"] for x in json.loads(r.stdout) if x["token"] == s["token"]], ["revoked"])
        for bad in ("../x", "x" * 24 + "/", "A" * 24):                                   # never a path, never touched
            r = cli(self.env, "unshare", bad)
            self.assertEqual(r.returncode, 1)
            self.assertNotIn("Traceback", r.stderr)
            self.assertIn("token", r.stderr)
        r = cli(self.env, "unshare", "z" * 24)
        self.assertEqual(r.returncode, 1)
        self.assertIn("no such share", r.stderr)

    def test_push_and_revoke_hooks(self):
        """ACROSCOPE_SHARE_PUSH copies the bundle elsewhere (a host, a bucket) after it is rendered; a push that
        fails leaves no share. ACROSCOPE_SHARE_REVOKE takes the copy down; while it fails the share stays, so a
        link is never thought dead while the copy is up."""
        target = self.root / "target"
        target.mkdir()
        copy = f'{PY} -c "import shutil, sys; shutil.copytree(sys.argv[1], sys.argv[2])" {{dir}} {target}/{{token}}'
        env = dict(self.env, ACROSCOPE_SHARE_PUSH=copy, ACROSCOPE_SHARE_REVOKE=f'{PY} -c "import shutil, sys; shutil.rmtree(sys.argv[1])" {target}/{{token}}')
        s = self.share("m01", env=env)
        self.assertTrue((target / s["token"] / "clip.mp4").exists())
        self.assertTrue((target / s["token"] / "index.html").exists())
        r = cli(env, "unshare", s["token"])
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse((target / s["token"]).exists())
        self.assertFalse((self.root / "shares" / s["token"]).exists())
        # a push that fails: nothing published, nothing kept, the error on one line
        before = self.names()
        env_bad = dict(self.env, ACROSCOPE_SHARE_PUSH=f'{PY} -c "import sys; sys.stderr.write(\'no route\'); sys.exit(2)"')
        err = self.share("m01", env=env_bad, ok=False)
        self.assertIn("no route", err)
        self.assertEqual(self.names(), before)
        rows = json.loads(cli(self.env, "shares", "--all", "--json").stdout)
        self.assertFalse(any(r["text"] == "half roll" and r["state"] == "active" and not (self.root / "shares" / r["token"]).exists() for r in rows))
        # a revoke that fails: the share stays active, the bundle stays
        s = self.share("m01", env=env)
        env_bad = dict(env, ACROSCOPE_SHARE_REVOKE=f'{PY} -c "import sys; sys.stderr.write(\'still there\'); sys.exit(2)"')
        r = cli(env_bad, "unshare", s["token"])
        self.assertEqual(r.returncode, 1)
        self.assertIn("still there", r.stderr)
        self.assertTrue((self.root / "shares" / s["token"]).exists())
        self.assertTrue((target / s["token"]).exists())
        self.assertIn(s["token"], [x["token"] for x in json.loads(cli(self.env, "shares", "--json").stdout)])   # still active
        self.assertEqual(cli(env, "unshare", s["token"]).returncode, 0)

    def test_expired_bundles_go_with_housekeeping(self):
        db = Db(self.root / "t.db")
        past = (datetime.now(timezone.utc) - timedelta(hours=1)).replace(microsecond=0).isoformat()
        tok = "e" * 24
        db.add_share(tok, SESSION, CLIP, "m01", 2.0, 5.0, "old", past)
        (self.root / "shares" / tok).mkdir(parents=True)
        (self.root / "shares" / tok / "index.html").write_text("x")
        keep = self.share("m01")                                                          # an active one stays
        r = cli(self.env, "housekeeping")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn(tok, json.loads(r.stdout)["shares_taken_down"])
        self.assertFalse((self.root / "shares" / tok).exists())
        self.assertTrue((self.root / "shares" / keep["token"] / "clip.mp4").exists())
        self.assertEqual(db.share(tok)["state"], "expired")                                # the row stays as the record
        r = cli(self.env, "housekeeping")                                                 # nothing to do the next day
        self.assertNotIn(tok, json.loads(r.stdout)["shares_taken_down"])

    def test_a_newer_viewer_reaches_the_shares_that_are_up(self):
        """A bundle is static, so a deploy with a better viewer left the shares that were up with the old page
        (Rudi, 2026-10-22: the keys did nothing on a share made before them). Housekeeping, which also runs at
        every server start, re-renders the page of every active share whose page is older than the template:
        the same clip, poster, sticks and sound, the link unchanged; a revoked one is left alone, and the push
        hook runs again for the copy elsewhere."""
        pushes = self.root / "pushes.txt"
        env = dict(self.env, ACROSCOPE_SHARE_PUSH=f'{PY} -c "import sys; open(sys.argv[1], \'a\').write(sys.argv[2] + chr(10))" {pushes} {{token}}')
        a = self.share("m01", "--poster", "3.5", "--audio", env=env)                         # sticks, a poster, sound
        b = self.share("m02", "--no-sticks", "--pad", "0.5", env=env)                         # none of those
        c = self.share("m01", env=env)
        assert cli(env, "unshare", c["token"]).returncode == 0
        self.assertEqual(pushes.read_text().split(), [a["token"], b["token"], c["token"]])
        pages = {t: self.root / "shares" / t / "index.html" for t in (a["token"], b["token"])}
        old = time.time() - 7 * 86400
        for pg in pages.values():
            pg.write_text(pg.read_text().replace("<footer>acroscope</footer>", "<footer>old viewer</footer>"))
            os.utime(pg, (old, old))                                                          # made before the template changed
        clips = {t: (self.root / "shares" / t / "clip.mp4").stat() for t in pages}
        # an expired share whose takedown fails (its revoke hook) keeps its bundle: not a page to refresh, nor to push
        db = Db(self.root / "t.db")
        past = (datetime.now(timezone.utc) - timedelta(hours=1)).replace(microsecond=0).isoformat()
        db.add_share("x" * 24, SESSION, CLIP, "m01", 2.0, 5.0, "stuck", past)
        (self.root / "shares" / ("x" * 24)).mkdir()
        (self.root / "shares" / ("x" * 24) / "index.html").write_text("<footer>old viewer</footer>")
        os.utime(self.root / "shares" / ("x" * 24) / "index.html", (old, old))
        env = dict(env, ACROSCOPE_SHARE_REVOKE=f'{PY} -c "import sys; sys.exit(2)"')
        r = cli(env, "housekeeping")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(sorted(json.loads(r.stdout)["shares_refreshed"]), sorted(pages))
        self.assertEqual((self.root / "shares" / ("x" * 24) / "index.html").read_text(), "<footer>old viewer</footer>")
        for t, pg in pages.items():
            html = pg.read_text()
            self.assertNotIn("old viewer", html)
            self.assertIn("<footer>acroscope</footer>", html)
            self.assertEqual((self.root / "shares" / t / "clip.mp4").stat().st_mtime, clips[t].st_mtime)   # the clip untouched
        html_a, html_b = pages[a["token"]].read_text(), pages[b["token"]].read_text()
        self.assertIn('"throttle": [', html_a)
        self.assertIn("const AUDIO = true;", html_a)
        self.assertIn(f'content="https://share.example/{a["token"]}/poster.jpg"', html_a)
        self.assertIn(">half roll<", html_a)
        self.assertIn("const STICKS = null;", html_b)
        self.assertIn("const AUDIO = false;", html_b)
        self.assertNotIn("og:image", html_b)
        self.assertEqual(pushes.read_text().split()[3:], sorted([a["token"], b["token"]], key=lambda t: pushes.read_text().split()[3:].index(t)))   # pushed again, the revoked one not
        self.assertEqual(len(pushes.read_text().split()), 5)
        r = cli(env, "housekeeping")                                                          # nothing to do the next time
        self.assertEqual(json.loads(r.stdout)["shares_refreshed"], [])
        env = dict(env, ACROSCOPE_SHARE_REVOKE="")
        for t in list(pages) + ["x" * 24]:
            cli(env, "unshare", t)
        cli(env, "housekeeping")

    def test_a_clip_that_cannot_be_cut_leaves_nothing(self):
        """An empty file (ffprobe fails), and a clip cut short just after its header (ffprobe reads 8 s from the
        header, ffmpeg gets no frame out of the stub of data): one line each, no bundle, nothing half-rendered, no row."""
        clips = self.root / "data" / "videos" / SESSION
        (clips / "2026-10-04_002.mp4").write_bytes(b"")
        whole = (clips / CLIP).read_bytes()
        (clips / "2026-10-04_003.mp4").write_bytes(whole[:whole.index(b"mdat") + 4 + 600])     # faststart: the header is at the front, the frames after
        assert cli(self.env, "refresh", SESSION, "--no-probe").returncode == 0
        for n, title in (("002", "empty"), ("003", "cut short")):
            assert cli(self.env, "tag", SESSION, n, "1", "2", title).returncode == 0
        moments = json.loads(cli(self.env, "moments", SESSION, "--json").stdout)
        for n, title in (("002", "empty"), ("003", "cut short")):
            mid = [m for m in moments if m["title"] == title][0]["id"]
            before = self.names()
            err = self.share(mid, "--no-sticks", ok=False)
            self.assertIn(n, err)
            self.assertEqual(self.names(), before)
            self.assertEqual(list((self.root / "shares").glob(".staging/*")), [])
            self.assertFalse(any(r["moment"] == mid for r in json.loads(cli(self.env, "shares", "--all", "--json").stdout)))


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class ThroughServerTest(unittest.TestCase):
    """The command as the CLI on another machine runs it: proxied to the server, which has the clip, the arm and
    the shares dir; the share text may hold a '/'."""
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.tmp.name)
        env = build(cls.root)
        cls.port = free_port()
        cls.proc = subprocess.Popen([PY, "-m", "acroscope.cli", "serve", "--host", "127.0.0.1", "--port", str(cls.port), "--no-warm"],
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
        cls.client = dict(os.environ, ACROSCOPE_URL=cls.url, ACROSCOPE_DATA=str(cls.root / "nowhere"),
                          ACROSCOPE_CACHE=str(cls.root / "client-cache"), ACROSCOPE_DB=str(cls.root / "unused.db"))
        for k in ("ACROSCOPE_SHARES", "ACROSCOPE_SHARE_URL", "ACROSCOPE_SHARE_PUSH", "ACROSCOPE_SHARE_REVOKE"):
            cls.client.pop(k, None)

    @classmethod
    def tearDownClass(cls):
        cls.proc.terminate()
        cls.proc.wait(5)
        cls.tmp.cleanup()

    def test_share_through_the_server(self):
        from acroscope.remote import Remote
        c = self.client
        Remote(self.url).refresh(SESSION)                                                 # the server walks its folder
        self.assertEqual(cli(c, "match", SESSION, "001", LOG, "1", "2").returncode, 0)
        r = cli(c, "tag", SESSION, "001", "3", "4", "half roll", "--tags", "roll")
        self.assertEqual(r.returncode, 0, r.stderr)
        r = cli(c, "share", SESSION, "m01", "--text", "a 1/2 roll, nearly", "--pad", "1")
        self.assertEqual(r.returncode, 0, r.stderr)
        s = json.loads(r.stdout)
        self.assertEqual(s["url"], f"https://share.example/{s['token']}/")               # the server's base URL, not the client's
        d = self.root / "shares" / s["token"]
        self.assertTrue((d / "clip.mp4").exists())
        self.assertIn("a 1/2 roll, nearly", (d / "index.html").read_text())
        self.assertFalse((self.root / "client-cache").exists() and any((self.root / "client-cache").rglob("clip.mp4")))   # nothing comes back
        r = cli(c, "shares", "--json")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual([x["token"] for x in json.loads(r.stdout)], [s["token"]])
        r = cli(c, "unshare", s["token"])
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse(d.exists())
        r = cli(c, "share", SESSION, "m01", "--text", "../../etc/passwd")                # free text, not a path: accepted as text
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("../../etc/passwd", (self.root / "shares" / json.loads(r.stdout)["token"] / "index.html").read_text())


if __name__ == "__main__":
    unittest.main()
