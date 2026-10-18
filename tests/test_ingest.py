"""Ingest end to end on a synthetic batch: a clip with a 7 s no-signal stretch (static) in the middle, twice, with a
sidecar and a manifest, run through the CLI in a child process against a scratch data dir and database."""
import json
from datetime import datetime, timezone
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from acroscope.db import Db
from acroscope import blackbox, ingest


MIDDLE = {
    "static": "nullsrc=s=160x120:r=30:d=7,geq=lum='random(1)*255':cb=128:cr=128,format=yuv420p",   # snow, as the Cobra records no signal
    "dusk": "color=c=0x141c40:d=7:s=160x120:r=30",       # a dark, flat, still picture: footage at dusk, a grey sky (2026-10-14)
}


def make_clip(path: Path, middle: str = "static") -> None:
    """3 s of colour bars, 7 s of `middle`, 3 s of bars; MJPEG in a .mov like the Cobra's, with audio."""
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                    f"testsrc=d=3:s=160x120:r=30[a];{MIDDLE[middle]}[b];testsrc=d=3:s=160x120:r=30[c];[a][b][c]concat=n=3:v=1:a=0",
                    "-f", "lavfi", "-i", "anullsrc=r=48000:cl=mono", "-shortest", "-c:v", "mjpeg", "-q:v", "5", "-pix_fmt", "yuvj420p",
                    "-c:a", "pcm_s16le", str(path)], check=True)


class _Scratch(unittest.TestCase):
    """A scratch data dir and database; the CLI runs in a child against them."""
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.data = self.root / "data"
        (self.data / "videos").mkdir(parents=True)
        self.env = dict(os.environ, ACROSCOPE_DATA=str(self.data), ACROSCOPE_DB=str(self.root / "t.db"),
                        ACROSCOPE_CACHE=str(self.root / "cache"), ACROSCOPE_URL="")

    def tearDown(self):
        self.tmp.cleanup()

    def cli(self, *args):
        return subprocess.run([sys.executable, "-m", "acroscope.cli", *args], env=self.env, capture_output=True, text=True)


class InboxOrderTest(_Scratch):
    def test_batches_in_the_order_they_came(self):
        """By batch id (the push's timestamp), whatever their state: a finished batch whose folder is gone does not
        go after one still arriving (the player shows the list newest first, so new batches went to the bottom and
        jumped to the top when done)."""
        inbox = self.data / "inbox"
        for b, done in (("20261015-094754-1", False), ("20261016-120000-2", True)):
            (inbox / b).mkdir(parents=True)
            if done:
                (inbox / b / ".done").write_text("{}")
        db = Db(self.root / "t.db")
        db.set_batch("20261014-201551-3", status="done", result={"clips": [], "logs": []})   # its folder long gone
        db.set_batch("20261016-130000-4", status="done", result={"clips": [], "logs": []})
        r = self.cli("inbox", "--json")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual([b["id"] for b in json.loads(r.stdout)],
                         ["20261014-201551-3", "20261015-094754-1", "20261016-120000-2", "20261016-130000-4"])


class IngestTest(_Scratch):
    def test_trash_emptied_after_its_days(self):
        # a deleted clip waits in trash/<date>/<session>/ for TRASH_DAYS, then housekeeping removes the day's folder
        t = self.data / "trash"
        for day in ("2026-10-01", "2026-10-05", "2026-10-11"):
            (t / day / "2026-10-11-x").mkdir(parents=True)
            (t / day / "2026-10-11-x" / "c1.mp4").write_bytes(b"x")
        (t / "not-a-date").mkdir()
        gone = ingest.cleanup_trash(root=t, today="2026-10-11", days=7, log=lambda m: None)
        self.assertEqual(gone, ["2026-10-01"])
        self.assertEqual(sorted(p.name for p in t.iterdir()), ["2026-10-05", "2026-10-11", "not-a-date"])

    def test_keep_segments(self):
        sig, noise = [60] * 20, [15] * 20
        self.assertEqual(ingest.keep_segments(noise + sig + noise, 30), [(9.0, 21.0)])      # static at both ends trimmed, padded
        self.assertEqual(ingest.keep_segments(sig + [15, 15] + sig, 21), [(0, 21)])          # a 1 s breakup mid-flight is kept
        self.assertEqual(ingest.keep_segments(sig + noise + sig, 30), [(0, 11.0), (19.0, 30)])  # a 10 s gap is cut
        self.assertEqual(ingest.keep_segments(sig + [15] * 10 + sig, 30), [(0, 11.0), (14.0, 26.0)])  # exactly 5 s: cut
        self.assertEqual(ingest.keep_segments(noise, 10), [])
        # flat but still is a picture (dusk, a grey sky: the 2026-10-14 schammer clips 018 and 022 lost 3.5 s of
        # flight each): only flat AND changing like noise is static
        still, snow = [1.0] * 20, [18.0] * 20
        self.assertEqual(ingest.keep_segments(sig + noise + sig, 30, changes=still + still + still), [(0, 30)])
        self.assertEqual(ingest.keep_segments(sig + noise + sig, 30, changes=still + snow + still), [(0, 11.0), (19.0, 30)])

    def test_clock_fit(self):
        """The OSD arm clock against clip time, from read_timers rows. 2026-10-14 007: the DVR wrote ~27 fps stamped
        60, and the OSD ran 2.20x the clip. Misreads, a disarmed (frozen) timer and a second arm whose rows disagree
        must not bend the fit; too little to go on is None."""
        def rows(rate, n=60, start=10.0):
            out = [{"t": t / 2, "top": 0.0, "bottom": 0} for t in range(0, int(start * 2))]          # disarmed: 0/0
            for i in range(n):
                t = start + i / 2
                v = round((t - start) * rate + 0.4, 2)
                out.append({"t": t, "top": v, "bottom": int(v)})
            if n > 25:
                out[int(start * 2) + 20]["top"] = 3.0                                              # misreads
                out[int(start * 2) + 21]["bottom"] = 99
            last = out[-1]["top"]
            out += [{"t": start + n / 2 + i / 2, "top": last, "bottom": int(last)} for i in range(30)]   # frozen after disarm
            out += [{"t": start + n / 2 + 15 + i / 2, "top": last + i, "bottom": i} for i in range(20)]  # next arm: total != arm
            return out
        self.assertAlmostEqual(ingest.clock_fit(rows(2.2)), 2.2, delta=0.02)
        self.assertAlmostEqual(ingest.clock_fit(rows(1.0)), 1.0, delta=0.02)
        self.assertIsNone(ingest.clock_fit(rows(2.2, n=12)))                                  # 6 s of readings: too few
        self.assertIsNone(ingest.clock_fit([]))

    @staticmethod
    def arm_rows(rate=1.0, start=10.0, length=120.0, skip=None, cut=None):
        """read_timers rows for one arm at `rate` x the clip's clock, every 0.5 s of the original; `skip` (at, s): a DVR
        stall where the OSD jumps s ahead; `cut` [a, b]: original time cut from the compressed clip (1 s padding
        kept, as compress leaves it), rows in compressed time. Returns (rows, kept)."""
        kept = [[0.0, cut[0]], [cut[1], start + length + 10]] if cut else [[0.0, start + length + 10]]
        rows = []
        for i in range(int((start + length + 10) * 2)):
            o = i / 2                                                               # original's time
            if cut and cut[0] < o < cut[1]:
                continue
            v = round(max(0.0, (o - start) * rate + (skip[1] if skip and o >= skip[0] else 0)), 2) if start <= o <= start + length else 0.0
            rows.append({"t": round(ingest._to_clip(o, kept), 2), "top": v, "bottom": int(v)})
        return rows, kept

    def test_a_cut_inside_an_arm_is_not_a_fast_clock(self):
        """Review on #30 (blocker): fitted against the compressed clip's time, a no-signal run cut from inside an arm
        (5 s or more) reads as a fast clock: 7 s gave 1.062, 10 s 1.098, so a clip on time was re-timed. Fitted in the
        original's time (each reading back through the kept stretches), it is 1.0."""
        for gap in (7, 10):
            rows, kept = self.arm_rows(cut=[60.0, 60.0 + gap])
            self.assertAlmostEqual(ingest.clock_fit(rows, kept), 1.0, delta=0.01, msg=gap)
        rows, kept = self.arm_rows(rate=2.2, cut=[60.0, 67.0])
        self.assertAlmostEqual(ingest.clock_fit(rows, kept), 2.2, delta=0.02)

    def test_an_uneven_clock_is_no_rate(self):
        """Review on #30 (major): a clip only gets re-timed when the frames were dropped evenly (007: 2.200 / 2.183 /
        2.207 over thirds). One DVR stall skipping 5 s fitted 1.060; that third runs faster than the rest, so it is
        None. A stall right at a third's edge leaves every third at 1.0 against the whole fit's 1.058: None too. A
        stall near the end is trimmed away and the clip is on time. Stray misreads (real clips: about a fifth) are
        trimmed and the fit stands."""
        rows, _ = self.arm_rows(skip=(70.0, 5.0))
        self.assertIsNone(ingest.clock_fit(rows))
        for at in (50.0, 90.0):                                                       # at a third's edge of the arm (10-130 s)
            rows, _ = self.arm_rows(skip=(at, 5.0))
            self.assertIsNone(ingest.clock_fit(rows), at)
        rows, _ = self.arm_rows(skip=(125.0, 5.0))
        self.assertAlmostEqual(ingest.clock_fit(rows), 1.0, delta=0.01)
        rows, _ = self.arm_rows(rate=2.2)
        self.assertAlmostEqual(ingest.clock_fit(rows), 2.2, delta=0.02)
        rows, _ = self.arm_rows()                                                    # the middle unreadable: a third without readings
        self.assertIsNone(ingest.clock_fit([r for r in rows if not 45 <= r["t"] <= 95]))
        rows, _ = self.arm_rows()
        for i, r in enumerate(rows):                                                 # one reading in six read wrong, agreeing
            if r["top"] > 2 and i % 6 == 0:
                r["top"] = r["bottom"] = r["top"] + 40
        self.assertAlmostEqual(ingest.clock_fit(rows), 1.0, delta=0.01)

    def test_fix_clock_retimes_a_misstamped_clip(self):
        """A clip whose OSD clock runs k times its own is stretched by k (timestamps scaled, no re-encode; the audio,
        stamped as wrongly, goes); a clip on time is left alone."""
        from unittest import mock
        for k, want in ((2.2, 2.2), (1.02, 1.0)):
            with self.subTest(k=k):
                src = self.root / f"c{k}.mp4"
                subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=d=4:s=160x120:r=60", "-f", "lavfi",
                                "-i", "anullsrc=r=32000:cl=mono", "-shortest", "-c:v", "libx264", "-c:a", "aac", str(src)], check=True)
                rt = {"layout": "x", "rows": [{"t": t / 2, "top": round(t / 2 * k + 1.3, 2), "bottom": int(t / 2 * k + 1.3)} for t in range(8)]}
                with mock.patch.object(ingest, "clock_fit", return_value=k), mock.patch.object(ingest.osd, "read_timers", return_value=rt):
                    got = ingest.fix_clock(src, [[0.0, 4.0]], log=lambda m: None)
                self.assertEqual(got, want)
                info = ingest.video.probe(src)
                self.assertAlmostEqual(info["duration"], 4.0 * want, delta=0.1)
                types = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_type", "-of", "csv=p=0", str(src)],
                                       capture_output=True, text=True).stdout.split()
                self.assertEqual(types, ["video"] if want != 1.0 else ["video", "audio"])
        with mock.patch.object(ingest.osd, "read_timers", side_effect=RuntimeError("no layout")):      # cannot tell: None
            self.assertIsNone(ingest.fix_clock(src, [[0.0, 4.0]], log=lambda m: None))
        with mock.patch.object(ingest, "clock_fit", return_value=None), mock.patch.object(ingest.osd, "read_timers", return_value=rt):
            self.assertIsNone(ingest.fix_clock(src, [[0.0, 4.0]], log=lambda m: None))

    def test_a_failed_retime_leaves_nothing(self):
        """A retime that fails leaves no .retime.part (one a crash left before is gone too)."""
        bad = self.root / "not-a-video.mp4"
        bad.write_bytes(b"no video here")
        (self.root / "not-a-video.mp4.retime.part").write_bytes(b"from a crash")
        with self.assertRaises(subprocess.CalledProcessError):
            ingest.retime(bad, 2.2)
        self.assertEqual(sorted(p.name for p in self.root.iterdir() if p.name.startswith("not-a-video")), ["not-a-video.mp4"])

    def test_times_move_through_cuts_and_clocks(self):
        # old clip time -> its original (through the old cuts, at the old clock) -> the new clip (new cuts, new clock)
        old, new = [[0.0, 4.0], [9.0, 13.0]], [[0.0, 13.0]]
        self.assertEqual(ingest.move_time(5.0, old, 1.0, new, 1.0), 10.0)
        self.assertEqual(ingest.move_time(5.0, [[0.0, 13.0]], 1.0, new, 2.2), 11.0)             # re-timed: 2.2x later
        self.assertEqual(ingest.move_time(11.0, new, 2.2, new, 1.0), 5.0)

    def test_flat_footage_is_kept_and_static_cut(self):
        for middle, kept in (("dusk", [[0.0, 13.0]]), ("static", [[0.0, 4.0], [9.0, 13.0]])):
            with self.subTest(middle=middle):
                src, dst = self.root / f"{middle}.mov", self.root / f"{middle}.mp4"
                make_clip(src, middle)
                res = ingest.compress(src, dst)
                self.assertEqual([s for s, e in res["kept"]], [s for s, e in kept], res)
                self.assertTrue(all(abs(e - k[1]) <= 0.5 for (s, e), k in zip(res["kept"], kept)), res)   # -shortest: ends a little past 13 s

    def test_batch(self):
        b = self.data / "inbox" / "b1"
        b.mkdir(parents=True)
        make_clip(b / "VID0002.mov")
        shutil.copy(b / "VID0002.mov", b / "VID0001.mov")
        (b / "import.json").write_text(json.dumps({"date": "2026-10-11", "session": "Test Flight", "imported_at": "2026-10-11T10:00:00+02:00"}))
        (b / ".done").write_text(json.dumps({"files": {f.name: f.stat().st_size for f in b.iterdir() if not f.name.startswith(".")}}))
        r = self.cli("inbox")
        self.assertIn("pending", r.stdout)
        r = self.cli("ingest", "b1")
        self.assertEqual(r.returncode, 0, r.stderr)
        res = json.loads(r.stdout)
        self.assertEqual(res["session"], "2026-10-11-Test-Flight")
        self.assertEqual(len(res["clips"]), 2)
        db = Db(self.root / "t.db")
        s = db.load("2026-10-11-Test-Flight")
        self.assertEqual([v["name"] for v in s["videos"]], ["2026-10-11_001", "2026-10-11_002"])   # card order, numbered
        v = s["videos"][0]
        self.assertTrue(v["file"].startswith("c") and v["file"].endswith(".mp4"))
        self.assertEqual(v["codec"], "h264")
        # the 7 s of grey cut, 1 s of padding kept; the clip's end lands a little past 13 s (ffmpeg's -shortest with audio)
        self.assertEqual(v["cuts"][0], [0, 4.0])
        self.assertEqual(v["cuts"][1][0], 9.0)
        self.assertTrue(13.0 <= v["cuts"][1][1] <= 13.5, v["cuts"])
        self.assertAlmostEqual(v["duration"], 8.0, delta=1.5)
        self.assertTrue(v["original"].startswith("originals/2026-10-11-Test-Flight/") and v["original_until"])
        self.assertTrue((self.data / v["original"]).exists())
        self.assertTrue((self.data / "videos" / "2026-10-11-Test-Flight" / v["file"]).exists())
        self.assertFalse(b.exists())                                                              # consumed
        self.assertEqual(db.batches()[0]["status"], "done")
        self.assertEqual(db.batches()[0]["result"], {"clips": ["2026-10-11_001", "2026-10-11_002"], "logs": []})
        r = self.cli("inbox", "--json")
        self.assertEqual(json.loads(r.stdout)[0]["clips"], 2)                                     # the dir is gone; the result tells
        r = self.cli("frame", res["session"], "001", "0.5")                                       # by number, like the README says
        self.assertEqual(r.returncode, 0, r.stderr)
        # numbering continues across the day: a second batch, unnamed, lands in <date>-unsorted as 003
        b2 = self.data / "inbox" / "b2"
        b2.mkdir()
        shutil.copy(self.data / v["original"], b2 / "VID0003.mov")
        (b2 / "import.json").write_text(json.dumps({"date": "2026-10-11"}))
        (b2 / ".done").write_text("")
        r = self.cli("ingest", "b2")
        self.assertEqual(r.returncode, 0, r.stderr)
        res = json.loads(r.stdout)
        self.assertEqual(res["session"], "2026-10-11-unsorted")
        self.assertEqual([x["name"] for x in db.load(res["session"])["videos"]], ["2026-10-11_003"])
        self.assertEqual(db.batch_clips("b1"), {"VID0001.mov": s["videos"][0]["file"], "VID0002.mov": s["videos"][1]["file"]})
        # an original is kept 90 days (Rudi, 2026-10-18: clips still get recut as the cutting changes)
        until = datetime.fromisoformat(v["original_until"])
        self.assertAlmostEqual((until - datetime.now(timezone.utc)).total_seconds() / 86400, 90, delta=0.1)
        # retention: past the date, the original goes
        db.set_video_fields(s["session"], v["file"], original_until="2000-01-01T00:00:00+00:00")
        r = self.cli("housekeeping")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse((self.data / v["original"]).exists())
        self.assertTrue(list((self.data / "state").glob("acroscope-*.db")))

    def test_failed_clip_waits_in_the_inbox(self):
        """A clip that cannot be compressed stays in the batch for the retry; nothing is moved or half-recorded."""
        b = self.data / "inbox" / "b3"
        b.mkdir(parents=True)
        (b / "VID0001.mov").write_text("not a video")
        make_clip(b / "VID0002.mov")
        (b / ".done").write_text("")
        r = self.cli("ingest", "b3")
        self.assertNotEqual(r.returncode, 0)
        db = Db(self.root / "t.db")
        self.assertEqual(db.batches()[0]["status"], "failed")
        self.assertTrue((b / "VID0001.mov").exists() and (b / "VID0002.mov").exists())
        self.assertEqual(list((self.data / "originals").glob("*/*")), [])
        self.assertEqual(list((self.data / "videos").glob("*/*")), [])
        self.assertEqual(db.batch_clips("b3"), {})
        make_clip(b / "VID0001.mov")                                 # the laptop pushes a good copy
        db.set_batch("b3", status="pending", error=None)             # what POST /api/inbox/b3/retry does
        self.assertEqual([b["id"] for b in json.loads(self.cli("inbox", "--json").stdout) if b["status"] == "pending"], ["b3"])
        r = self.cli("ingest")                                       # no batch named: the pending ones
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(len(json.loads(r.stdout)["clips"]), 2)
        self.assertEqual(sorted(db.batch_clips("b3")), ["VID0001.mov", "VID0002.mov"])
        self.assertFalse(b.exists())

    def test_recut_from_the_original(self):
        """A clip cut by the old detector (dusk taken for static, 2026-10-14 schammer 018 and 022) is compressed again
        from its kept original. Moments and a match inside one old stretch move with the footage (old clip time ->
        original -> new clip time); a match whose arm spans an old cut held on one side of it only: it is left as it
        is and reported. Cached frames of the clip go."""
        b = self.data / "inbox" / "dusk"
        b.mkdir(parents=True)
        make_clip(b / "VID0001.mov", "dusk")
        (b / "import.json").write_text(json.dumps({"date": "2026-10-14", "session": "dusk"}))
        (b / ".done").write_text("")
        r = self.cli("ingest", "dusk")
        self.assertEqual(r.returncode, 0, r.stderr)
        S = "2026-10-14-dusk"
        db = Db(self.root / "t.db")
        v = db.load(S)["videos"][0]
        db.set_video_fields(S, v["file"], cuts=[[0.0, 4.0], [9.0, 13.0]], duration=8.0)    # as the old detector left it
        db.tag(S, v["file"], 2.0, 3.0, "before the cut", [])
        db.tag(S, v["file"], 5.0, 6.0, "after the cut", [])
        (self.data / "blackbox").mkdir(exist_ok=True)
        (self.data / "blackbox" / "BTFL_d.bbl").write_bytes(b"x")
        idx = self.root / "cache" / "arms" / "BTFL_d" / "arms.json"
        idx.parent.mkdir(parents=True)
        idx.write_text(json.dumps({"v": blackbox.INDEX_VERSION, "file": "BTFL_d.bbl", "stem": "BTFL_d", "date": None, "boots": [],
                                   "arms": [{"index": 1, "length": 2.0, "frames": 10}, {"index": 2, "length": 3.0, "frames": 10}]}))
        db.set_match(S, v["file"], "BTFL_d.bbl", 1, 4.5)                  # 4.5-6.5: inside the second stretch
        db.set_match(S, v["file"], "BTFL_d.bbl", 2, 3.0)                  # 3-6: across the old cut at 4.0
        stale = self.root / "cache" / "frames" / S / (Path(v["file"]).stem + "_5.00.jpg")
        stale.parent.mkdir(parents=True)
        stale.write_bytes(b"old")
        r = self.cli("recut", S, "001")
        self.assertEqual(r.returncode, 0, r.stderr)
        res = json.loads(r.stdout)
        self.assertEqual(res["cuts"][0][0], 0.0)
        self.assertEqual(len(res["cuts"]), 1)                                 # nothing cut now
        s = db.load(S)
        self.assertAlmostEqual(s["videos"][0]["duration"], 13.0, delta=0.6)
        self.assertEqual(json.loads(json.dumps(s["videos"][0]["cuts"])), res["cuts"])
        ms = {m["title"]: (m["start"], m["end"]) for m in s["moments"]}
        self.assertEqual(ms, {"before the cut": (2.0, 3.0), "after the cut": (10.0, 11.0)})
        off = {m["arm"]: m["offset"] for m in s["matches"]}
        self.assertEqual(off, {1: 9.5, 2: 3.0})
        self.assertEqual([(c["bbl"], c["arm"]) for c in res["check"]], [("BTFL_d.bbl", 2)])
        self.assertFalse(stale.exists())
        self.assertTrue((self.data / v["original"]).exists())               # the original stays until its date

    def test_recut_keeps_the_clock_it_cannot_read(self):
        """Review on #30: a re-timed clip (timescale 2.2) recut where the OSD cannot be read keeps its clock: the new
        encode is re-timed by the stored factor, and moments and matches stay put, instead of the clip playing 2.2x
        fast again with every match listed under check."""
        b = self.data / "inbox" / "slow"
        b.mkdir(parents=True)
        make_clip(b / "VID0001.mov", "dusk")
        (b / "import.json").write_text(json.dumps({"date": "2026-10-14", "session": "slow"}))
        (b / ".done").write_text("")
        self.assertEqual(self.cli("ingest", "slow").returncode, 0)
        S = "2026-10-14-slow"
        db = Db(self.root / "t.db")
        v = db.load(S)["videos"][0]
        p = self.data / "videos" / S / v["file"]
        ingest.retime(p, 2.2)                                                           # as ingest left a mis-stamped clip
        db.set_video_fields(S, v["file"], timescale=2.2, duration=ingest.video.probe(p)["duration"])
        db.tag(S, v["file"], 11.0, 13.2, "at 5-6 s of the original", [])
        code = ("from acroscope import ingest\nimport json\ningest.fix_clock = lambda path, kept, log=print: None\n"
                f"print(json.dumps(ingest.recut({S!r}, '001', log=lambda m: None)))")
        r = subprocess.run([sys.executable, "-c", code], env=self.env, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        res = json.loads(r.stdout.strip().splitlines()[-1])
        self.assertEqual(res["timescale"], 2.2)
        s = db.load(S)
        self.assertEqual(s["videos"][0]["timescale"], 2.2)
        self.assertAlmostEqual(s["videos"][0]["duration"], 13.0 * 2.2, delta=1.5)
        self.assertEqual([(m["start"], m["end"]) for m in s["moments"]], [(11.0, 13.2)])

    def test_the_clock_reading_goes_with_the_clip(self):
        """The OSD reading ingest takes on the encode for the clock goes with the clip into videos/, so automatch
        (here not run) does not read the OSD again."""
        batch = "carry"
        self.two_clip_batch(batch)
        (self.data / "inbox" / batch / "import.json").write_text(json.dumps({"date": "2026-10-11", "session": "carry"}))
        self.env["ACROSCOPE_DB"] = str(self.root / "tcarry.db")
        r = self.run_patched(batch, "ingest._cli = lambda *a: None")              # no decode, no automatch child
        self.assertEqual(r.returncode, 0, r.stderr)
        vids = Db(self.root / "tcarry.db").load("2026-10-11-carry")["videos"]
        self.assertEqual(len(vids), 2)
        for x in vids:
            self.assertTrue(list((self.root / "cache" / "osd" / "2026-10-11-carry").glob(x["file"] + ".*.json")), x["file"])

    def test_a_crash_in_the_clock_step_is_redone_by_the_retry(self):
        """Review on #30: the clock step ran after the clip was recorded, so a crash in it (the OSD read is the slow
        part) left the clip done for the retry and never re-timed. It runs on the encode before the clip is recorded,
        and what it found is recorded with it."""
        batch = "clockcrash"
        self.two_clip_batch(batch)
        (self.data / "inbox" / batch / "import.json").write_text(json.dumps({"date": "2026-10-11", "session": "clock crash"}))
        self.env["ACROSCOPE_DB"] = str(self.root / "tc.db")
        r = self.run_patched(batch, """n = [0]
def fix(path, kept, log=print):
    n[0] += 1
    if n[0] == 2:
        os._exit(9)
    return 1.0
ingest.fix_clock = fix""")
        self.assertEqual(r.returncode, 9, r.stderr)
        r = self.run_patched(batch, """def fix(path, kept, log=print):
    ingest.retime(path, 2.0)
    return 2.0
ingest.fix_clock = fix""")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assert_consistent(self.root / "tc.db", batch, 2)
        db = Db(self.root / "tc.db")
        vids = sorted(db.load(db.batches()[0]["session"])["videos"], key=lambda v: v["name"])
        self.assertEqual([v.get("timescale") for v in vids], [None, 2.0])          # 001 done before the crash, 002 by the retry
        self.assertGreater(vids[1]["duration"], vids[0]["duration"] * 1.8)

    def two_clip_batch(self, name: str) -> Path:
        b = self.data / "inbox" / name
        b.mkdir(parents=True)
        make_clip(b / "VID0001.mov")
        shutil.copy(b / "VID0001.mov", b / "VID0002.mov")
        (b / ".done").write_text(json.dumps({"files": {f.name: f.stat().st_size for f in b.iterdir() if not f.name.startswith(".")}}))
        return b

    def run_patched(self, batch: str, patch: str):
        """process_batch in a child with `patch` applied first (a crash is os._exit: no except or finally runs)."""
        code = f"from acroscope import ingest, db\nimport os, sqlite3\n{patch}\ningest.process_batch({batch!r})"
        return subprocess.run([sys.executable, "-c", code], env=self.env, capture_output=True, text=True)

    def test_a_crash_anywhere_is_finished_by_the_retry(self):
        """The process dies (a redeploy, the OOM killer) at each step of the second clip; the retry completes the
        batch with nothing lost, nothing twice and nothing stray."""
        crashes = {
            "mid-encode": """real = ingest.compress
n = [0]
def compress(src, dst, pct=None):
    n[0] += 1
    if n[0] == 2:
        dst.parent.mkdir(parents=True, exist_ok=True); dst.write_bytes(b"half an mp4"); os._exit(9)
    return real(src, dst, pct)
ingest.compress = compress""",
            "after the batch note": """real = db.Db.add_batch_clip
n = [0]
def add(self, *a):
    real(self, *a); n[0] += 1
    if n[0] == 2:
        os._exit(9)
db.Db.add_batch_clip = add""",
            "while putting files in place": """real = ingest.shutil.move
n = [0]
def move(src, dst, *a):
    if str(dst).endswith(".mp4"):
        n[0] += 1
        if n[0] == 2:
            os._exit(9)
    return real(src, dst, *a)
ingest.shutil.move = move""",
        }
        for i, (where, patch) in enumerate(crashes.items()):
            with self.subTest(where):
                batch = f"crash{i}"
                self.two_clip_batch(batch)
                (self.data / "inbox" / batch / "import.json").write_text(json.dumps({"date": "2026-10-11", "session": f"crash {i}"}))
                self.env["ACROSCOPE_DB"] = str(self.root / f"t{i}.db")
                shutil.rmtree(self.data / "originals", ignore_errors=True)
                r = self.run_patched(batch, patch)
                self.assertEqual(r.returncode, 9, r.stderr)
                r = self.cli("ingest", batch)
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assert_consistent(self.root / f"t{i}.db", batch, 2)

    def assert_consistent(self, path: Path, batch: str, clips: int):
        """Every clip recorded once, numbered from 001, with its file and its original in place; nothing stray under
        videos/ or originals/ (a partial encode the scan would take for a clip, an original nothing points at)."""
        db = Db(path)
        session = db.batches()[0]["session"]
        vids = db.load(session)["videos"]
        self.assertEqual(sorted(v["name"][-3:] for v in vids), [f"{i:03d}" for i in range(1, clips + 1)], vids)
        self.assertEqual(sorted(p.name for p in (self.data / "videos" / session).iterdir()), sorted(v["file"] for v in vids))
        originals = sorted(str(p.relative_to(self.data)) for p in (self.data / "originals").rglob("*") if p.is_file())
        self.assertEqual(originals, sorted(v["original"] for v in vids))
        self.assertEqual(len(db.batch_clips(batch)), clips)
        self.assertFalse((self.data / "inbox" / batch).exists())

    def test_a_late_database_error_leaves_no_row(self):
        """The database refuses the batch note after the clip row was written ("database is locked"): the clip is
        not half-registered (the scan would show it as missing, the retry would number it 002) and waits in the batch."""
        b = self.two_clip_batch("late")
        patch = """def add(self, *a):
    raise sqlite3.OperationalError("database is locked")
db.Db.add_batch_clip = add"""
        r = self.run_patched("late", patch)
        self.assertIn("database is locked", r.stderr)
        db = Db(self.root / "t.db")
        session = db.batches()[0]["session"]
        self.assertEqual(db.load(session)["videos"], [])
        self.assertEqual(list((self.data / "videos" / session).iterdir()), [])
        self.assertTrue((b / "VID0001.mov").exists())
        r = self.cli("ingest", "late")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assert_consistent(self.root / "t.db", "late", 2)

    def test_retry_with_a_manifest(self):
        """The first clip went in, the second was bad; the laptop pushes a good second clip and a new manifest. The
        clip already taken out of the batch must not fail the size check of the retry."""
        b = self.data / "inbox" / "man"
        b.mkdir(parents=True)
        make_clip(b / "VID0001.mov")
        (b / "VID0002.mov").write_text("not a video")
        sizes = {f.name: f.stat().st_size for f in b.iterdir()}
        (b / ".done").write_text(json.dumps({"files": sizes}))
        self.assertNotEqual(self.cli("ingest", "man").returncode, 0)
        self.assertFalse((b / "VID0001.mov").exists())                   # in
        make_clip(b / "VID0002.mov")                                     # the laptop pushes a good copy, and a new manifest
        (b / ".done").write_text(json.dumps({"files": dict(sizes, **{"VID0002.mov": (b / "VID0002.mov").stat().st_size})}))
        r = self.cli("ingest", "man")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assert_consistent(self.root / "t.db", "man", 2)


    def test_a_log_after_its_clips_automatches_them(self):
        """2026-10-14 schammer: the clips came in one batch, the log in the next. Automatch ran on the first batch's
        clips with no log yet, and the second batch had no clips of its own, so nothing was matched. A batch that
        brings a log automatches every clip of its session that has no match yet (a clip already matched keeps
        its matches and is left alone)."""
        calls = self.root / "calls"
        self.env["CALLS"] = str(calls)
        patch = """import json
def _cli(*a):
    with open(os.environ["CALLS"], "a") as f:
        f.write(json.dumps(a) + "\\n")
    return 0
ingest._cli = _cli"""
        side = json.dumps({"date": "2026-10-14", "session": "late"})
        b = self.two_clip_batch("clips")
        (b / "import.json").write_text(side)
        r = self.run_patched("clips", patch)
        self.assertEqual(r.returncode, 0, r.stderr)
        db = Db(self.root / "t.db")
        S = "2026-10-14-late"
        c1, c2 = [v["file"] for v in db.load(S)["videos"]]
        ran = lambda: [json.loads(x) for x in calls.read_text().splitlines()]
        self.assertEqual(ran(), [["automatch", S, c1, c2, "--write"]])    # one run: the clips are planned together (#26)
        calls.unlink()
        db.set_match(S, c1, "BTFL_other.bbl", 1, 2.0)                  # matched by hand meanwhile: not touched again
        b = self.data / "inbox" / "log"
        b.mkdir()
        (b / "BTFL_late.bbl").write_bytes(b"x")
        (b / "import.json").write_text(side)
        (b / ".done").write_text(json.dumps({"files": {"BTFL_late.bbl": 1, "import.json": len(side)}}))
        r = self.run_patched("log", patch)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(ran(), [["arms", "BTFL_late.bbl"], ["automatch", S, c2, "--write"]])
        self.assertEqual(db.batches()[-1]["result"], {"clips": [], "logs": ["BTFL_late.bbl"]})


class HostileInboxTest(_Scratch):
    def test_symlink_and_manifest_paths_fail_the_batch(self):
        b = self.data / "inbox" / "evil"
        b.mkdir(parents=True)
        make_clip(b / "VID0001.mov")
        os.symlink("/etc/hostname", b / "VID0002.mov")                 # a link posing as a clip
        (b / ".done").write_text("")
        r = self.cli("ingest", "evil")
        self.assertIn("not a regular file", r.stderr)
        self.assertTrue((b / "VID0001.mov").exists())                   # nothing was taken
        os.unlink(b / "VID0002.mov")
        (b / ".done").write_text(json.dumps({"files": {"../../videos/x": 1}}))
        r = self.cli("ingest", "evil")
        self.assertIn("not a plain file name", r.stderr)
        (b / "sub").mkdir()                                             # a directory in a batch
        (b / ".done").write_text("")
        r = self.cli("ingest", "evil")
        self.assertIn("not a regular file", r.stderr)
        db = Db(self.root / "t.db")
        self.assertEqual(db.batches()[0]["status"], "failed")
        with self.assertRaises(ValueError):
            ingest.plain_name("a/b")


if __name__ == "__main__":
    unittest.main()
