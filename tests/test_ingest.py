"""Ingest end to end on a synthetic batch: a clip with a 7 s no-signal stretch in the middle, twice, with a
sidecar and a manifest, run through the CLI in a child process against a scratch data dir and database."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from acroscope.db import Db
from acroscope import ingest


def make_clip(path: Path) -> None:
    """3 s of colour bars, 7 s of flat grey (no signal), 3 s of bars; MJPEG in a .mov like the Cobra's, with audio."""
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                    "testsrc=d=3:s=160x120:r=30[a];color=c=gray:d=7:s=160x120:r=30[b];testsrc=d=3:s=160x120:r=30[c];[a][b][c]concat=n=3:v=1:a=0",
                    "-f", "lavfi", "-i", "anullsrc=r=48000:cl=mono", "-shortest", "-c:v", "mjpeg", "-q:v", "5", "-pix_fmt", "yuvj420p",
                    "-c:a", "pcm_s16le", str(path)], check=True)


class IngestTest(unittest.TestCase):
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

    def test_keep_segments(self):
        sig, noise = [60] * 20, [15] * 20
        self.assertEqual(ingest.keep_segments(noise + sig + noise, 30), [(9.0, 21.0)])      # static at both ends trimmed, padded
        self.assertEqual(ingest.keep_segments(sig + [15, 15] + sig, 21), [(0, 21)])          # a 1 s breakup mid-flight is kept
        self.assertEqual(ingest.keep_segments(sig + noise + sig, 30), [(0, 11.0), (19.0, 30)])  # a 10 s gap is cut
        self.assertEqual(ingest.keep_segments(sig + [15] * 10 + sig, 30), [(0, 11.0), (14.0, 26.0)])  # exactly 5 s: cut
        self.assertEqual(ingest.keep_segments(noise, 10), [])

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
        self.assertEqual(v["cuts"], [[0, 4.0], [9.0, 13.0]])                                   # the 7 s of grey cut, 1 s padding kept
        self.assertAlmostEqual(v["duration"], 8.0, delta=1.5)
        self.assertTrue(v["original"].startswith("originals/2026-10-11-Test-Flight/") and v["original_until"])
        self.assertTrue((self.data / v["original"]).exists())
        self.assertTrue((self.data / "videos" / "2026-10-11-Test-Flight" / v["file"]).exists())
        self.assertFalse(b.exists())                                                              # consumed
        self.assertEqual(db.batches()[0]["status"], "done")
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


if __name__ == "__main__":
    unittest.main()
