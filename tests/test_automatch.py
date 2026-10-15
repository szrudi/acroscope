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
         "arms": [{"index": i, "length": l, "frames": 100, "motors_spun": True, "vbat_start": 4.2, "uptime_start": 100.0 * i,
                   "truncated": False} for i, l in LENGTHS.items()]}
# the same log, but the flash filled during arm 6: its last arm is cut off
FULL = {**INDEX, "arms": INDEX["arms"][:-1] + [{**INDEX["arms"][-1], "truncated": True}]}


class AutomatchTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Db(Path(self.tmp.name) / "t.db")
        self.index, self.stats = INDEX, {}
        patches = [mock.patch.object(sessions, "_store", self.db),
                   # the STATS screen after each run: {clip file: [per run {total_arm, blackbox}]}
                   mock.patch.object(osd, "read_stats", lambda p, runs, layout: self.stats.get(p.name, [{} for _ in runs])),
                   mock.patch.object(sessions, "clip_path", lambda s, c: Path(self.tmp.name) / s / c),
                   mock.patch.object(blackbox, "index_bbl", lambda b, **k: self.index),
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

    def test_writes_nothing_to_a_log_taken_out_meanwhile(self):
        """A log taken out of the session while automatch read the clip (🗑 or ⇄ during a 🎯 job): no match is
        written to it, so it is not attached back."""
        def timers(p, fps=2.0):                                         # the OSD read, minutes in real life
            self.db.detach("2026-10-03", BBL)
            return {"layout": "air65f", "rows": []}
        with mock.patch.object(osd, "read_timers", timers):
            r = osd.automatch("2026-10-03", "2026-10-03_001.mp4", write=True)
        self.assertEqual(self.arms(r), [2, 6])                        # proposed all the same
        self.assertEqual((self.db.load("2026-10-03")["blackbox"], self.db.load("2026-10-03")["matches"]), ([], []))

    def test_a_hand_match_set_while_reading_is_kept(self):
        """A match set by hand on the clip while automatch read it (minutes): kept, not overwritten."""
        def timers(p, fps=2.0):
            self.db.set_match("2026-10-03", "2026-10-03_001.mp4", BBL, 2, 99.0, "by hand")
            return {"layout": "air65f", "rows": []}
        with mock.patch.object(osd, "read_timers", timers):
            osd.automatch("2026-10-03", "2026-10-03_001.mp4", write=True)
        m = {m["arm"]: m for m in self.db.load("2026-10-03")["matches"]}
        self.assertEqual((m[2]["offset"], m[2]["note"]), (99.0, "by hand"))
        self.assertIn(6, m)

    def test_a_write_never_attaches_the_log(self):
        """The match goes in only while the log is in the session, checked in the same statement: a 🗑 landing
        between the re-read and the write leaves the log out (and its mark in place)."""
        real = self.db.load
        def load(session):                                            # the 🗑 right after automatch's last read
            out = real(session)
            if session == "2026-10-03" and load.n == 1:
                self.db.detach("2026-10-03", BBL)
            load.n += 1
            return out
        load.n = 0
        with mock.patch.object(self.db, "load", load):
            osd.automatch("2026-10-03", "2026-10-03_001.mp4", write=True)
        self.assertEqual((self.db.load("2026-10-03")["blackbox"], self.db.load("2026-10-03")["matches"]), ([], []))
        self.assertEqual(self.db.detached("2026-10-03"), {BBL})

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

    def test_flash_full_in_this_clip(self):
        # the first run's disarm shows BLACKBOX 100 %: nothing after it is in the log, and that run can only be the
        # log's cut-off last arm (here: arm 6, 40 s, does not fit a 10 s run). It is this fill: the day before's
        # clip holds an arm of the log.
        self.index = FULL
        self.db.set_match("2026-10-02", "2026-10-02_001.mp4", BBL, 1, 5.0)
        self.stats["2026-10-03_001.mp4"] = [{"total_arm": 10, "blackbox": 100}, {"total_arm": 50, "blackbox": 100}]
        r = osd.automatch("2026-10-03", "2026-10-03_001.mp4")
        self.assertEqual(self.arms(r), [])
        self.assertEqual(r["flash_full"]["clip"], "2026-10-03_001")
        # a log that did not end full: the STATS screen says nothing about it
        self.index = INDEX
        self.assertEqual(self.arms(osd.automatch("2026-10-03", "2026-10-03_001.mp4")), [2, 6])

    def test_flash_full_in_an_earlier_clip(self):
        # the flash filling up (55 %, then 100 %) in the clip before
        self.index = FULL
        self.stats["2026-10-03_001.mp4"] = [{"total_arm": 10, "blackbox": 55}, {"total_arm": 50, "blackbox": 100}]
        r = osd.automatch("2026-10-03", "2026-10-03_002.mp4")
        self.assertEqual(self.arms(r), [])
        self.assertEqual(r["flash_full"]["clip"], "2026-10-03_001")
        # an earlier clip below 100 % leaves the log open
        self.stats["2026-10-03_001.mp4"] = [{"total_arm": 10, "blackbox": 55}, {"total_arm": 50, "blackbox": 66}]
        self.assertEqual(self.arms(osd.automatch("2026-10-03", "2026-10-03_002.mp4")), [2, 6])

    def test_a_full_flash_before_the_erase_is_not_this_log(self):
        # 10-07 001-003: 001 shows the previous fill at 100 %, the flash is erased in 003, and this log starts
        # after it. A 100 % with neither a lower reading nor an arm of the log before it says nothing.
        self.index = FULL
        self.stats["2026-10-03_001.mp4"] = [{"total_arm": 10, "blackbox": 100}, {"total_arm": 50, "blackbox": 100}]
        r = osd.automatch("2026-10-03", "2026-10-03_002.mp4")
        self.assertEqual(self.arms(r), [2, 6])
        self.assertIsNone(r["flash_full"])
        self.assertEqual(self.arms(osd.automatch("2026-10-03", "2026-10-03_001.mp4")), [2, 6])


if __name__ == "__main__":
    unittest.main()
