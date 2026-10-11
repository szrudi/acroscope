"""osd.automatch over a scratch database with the readings and the log stubbed: what the other clips' matches
leave for a clip (#9). No video, no log."""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from acroscope import blackbox, osd, sessions
from acroscope.db import Db

BBL = "L.bbl"
# one boot, arms 1-6; arms 2 and 5 are about the same length, which is all the cumulative timer has to go on
LENGTHS = {1: 5.0, 2: 10.0, 3: 20.0, 4: 30.0, 5: 10.5, 6: 40.0}
INDEX = {"file": BBL, "boots": [{"arms": list(LENGTHS)}],
         "arms": [{"index": i, "length": l, "frames": 100, "motors_spun": True, "vbat_start": 4.2, "uptime_start": 100.0 * i}
                  for i, l in LENGTHS.items()]}


class AutomatchTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Db(Path(self.tmp.name) / "t.db")
        patches = [mock.patch.object(sessions, "_store", self.db),
                   mock.patch.object(sessions, "clip_path", lambda s, c: Path(self.tmp.name) / s / c),
                   mock.patch.object(blackbox, "index_bbl", lambda b, **k: INDEX),
                   mock.patch.object(osd, "read_timers", lambda p, fps=2.0: {"layout": "air65f", "rows": []}),
                   # the clip shows a 10 s run and a 40 s run
                   mock.patch.object(osd, "video_arms", lambda rt, fps=2.0: [
                       {"start": 10.0, "end": 20.0, "length": 10.0, "timer_max": 10, "total_before": None, "readings": 20},
                       {"start": 100.0, "end": 140.0, "length": 40.0, "timer_max": 40, "total_before": None, "readings": 80}])]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        # the log was dumped on 10-03 and is attached to both days' sessions
        for s in ("2026-10-02", "2026-10-03"):
            self.db.ensure_session(s)
            self.db.attach(s, BBL)
        self.db.upsert_video("2026-10-02", "2026-10-02_001.mp4", 300.0)
        self.db.upsert_video("2026-10-02", "2026-10-02_002.mp4", 300.0)
        self.db.upsert_video("2026-10-03", "2026-10-03_001.mp4", 300.0)
        self.db.upsert_video("2026-10-03", "2026-10-03_002.mp4", 300.0)

    def tearDown(self):
        self.tmp.cleanup()

    def arms(self, r):
        return sorted(m["arm"] for m in r["matches"])

    def test_alone_the_lengths_decide(self):
        self.assertEqual(self.arms(osd.automatch("2026-10-03", "2026-10-03_001.mp4")), [2, 6])

    def test_taken_in_another_session_and_bounded_by_later_clips(self):
        self.db.set_match("2026-10-02", "2026-10-02_001.mp4", BBL, 2, 50.0)     # arm 2 is the day before's
        self.db.set_match("2026-10-03", "2026-10-03_002.mp4", BBL, 6, 5.0)      # arm 6 is in the next clip
        r = osd.automatch("2026-10-03", "2026-10-03_001.mp4")
        self.assertEqual(self.arms(r), [5])
        self.assertEqual(tuple(r["arm_window"]), (2, 6))

    def test_proposals_earlier_in_the_run_count(self):
        pending = [{"session": "2026-10-02", "video": "2026-10-02_002.mp4", "name": "2026-10-02_002", "bbl": BBL, "arm": 5}]
        self.assertEqual(self.arms(osd.automatch("2026-10-03", "2026-10-03_001.mp4", pending=pending)), [6])


if __name__ == "__main__":
    unittest.main()
