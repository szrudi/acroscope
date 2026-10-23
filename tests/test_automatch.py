"""osd.automatch over a scratch database with the readings and the log stubbed: what the other clips' matches
leave for a clip (#9). No video, no log."""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from acroscope.sources import blackbox, osd
from acroscope.store import sessions
from acroscope.store.db import Db

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

    def test_the_plan_window_and_this_runs_picks_combine(self):
        """The plan's window and the bounds from the other clips' matches: the narrower of each end wins."""
        pending = [{"session": "2026-10-03", "video": "x.mp4", "name": "2026-10-03_000", "bbl": BBL, "arm": 1}]
        r = osd.automatch("2026-10-03", "2026-10-03_001.mp4", pending=pending, windows={BBL: (2, None)})
        self.assertEqual(self.arms(r), [5, 6])                                    # the plan's lo 2 over the pick's 1
        self.db.set_match("2026-10-03", "2026-10-03_002.mp4", BBL, 6, 5.0)
        r = osd.automatch("2026-10-03", "2026-10-03_001.mp4", windows={BBL: (None, 4)})
        self.assertEqual(r["arm_window"], (None, 4))                              # the plan's hi 4 under the match's 6

    def test_a_plan_window_narrowed_by_this_runs_picks(self):
        """With the plan's window (one arm of slack into the neighbours), what the clips before this one took in the
        same run still bounds it: a clip before took arm 3, so this clip's 10 s run is arm 5, not arm 2."""
        pending = [{"session": "2026-10-03", "video": "x.mp4", "name": "2026-10-03_000", "bbl": BBL, "arm": 3}]
        r = osd.automatch("2026-10-03", "2026-10-03_001.mp4", pending=pending, windows={BBL: (None, None)})
        self.assertEqual(self.arms(r), [5, 6])

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



# ---- #26: the whole session aligned at once ---------------------------------------------------------------------

# 2026-10-14 (one log, 17 arms, every pack its own boot): the log's arm lengths and the clips' OSD runs, from #26
DAY_ARMS = [188.5, 4.9, 191.6, 131.5, 110.1, 11.8, 92.5, 74.2, 111.1, 20.7, 121.8, 49.8, 5.6, 118.4, 80.0, 92.6, 91.6]
DAY_RUNS = [("005", 187.8), ("005", 3.9), ("006", 190.8), ("007", 71.8), ("008", 98.75), ("008", 11.15), ("009", 91.8),
            ("010", 73.15), ("011", 110.35), ("013", 18.35), ("014", 121.15), ("015", 52.85), ("016", 117.85),
            ("017", 78.85), ("018", 92.15), ("019", 90.85)]


class SessionPlanTest(unittest.TestCase):
    """Clip by clip, a pick bounds the clips after it: on 2026-10-14, 009 took arm 17 (91.6 is 0.7 s nearer its 91.8 s
    run than arm 7's 92.5) and left 010-019 "arms after 17", none. The clips are in card order and the arms in log
    order, so the whole session is one monotone alignment: 009 -> 7 costs 0.7 s more and lets ten clips fit."""

    def plan(self, runs, arms):
        return osd.plan_session_pairs([{"clip": c, "length": l} for c, l in runs], [{"length": l} for l in arms])

    def test_the_day_aligns_in_order(self):
        pairs, ambiguous = self.plan(DAY_RUNS, DAY_ARMS)
        got = {}
        for ri, ai in pairs:
            got.setdefault(DAY_RUNS[ri][0], []).append(ai + 1)
        # the hand matches (STATS screens): 005 = 1, 2 · 006 = 3 · 009 = 7 · 010 = 8 ... 019 = 17; 007 (wrong clock)
        # and 008's first run (cut short) fit nothing by length, so the plan leaves them to the clip's own pass
        self.assertEqual(got, {"005": [1, 2], "006": [3], "008": [6], "009": [7], "010": [8], "011": [9], "013": [10],
                               "014": [11], "015": [12], "016": [14], "017": [15], "018": [16], "019": [17]})
        self.assertEqual(ambiguous, set())

    def test_a_thin_choice_between_clips_is_ambiguous(self):
        """Ambiguous is a runner-up that hands an arm to another clip: then the clips' windows are not known. Two clips,
        each one run, three arms of the same length: which clip holds the middle one is a coin toss."""
        pairs, ambiguous = self.plan([("001", 92.0), ("002", 92.0)], [92.0, 92.0, 92.0])
        self.assertEqual((pairs, ambiguous), ([], {"001", "002"}))
        pairs, ambiguous = self.plan([("001", 92.0), ("002", 74.0)], [92.5, 74.2, 91.6])   # the next clip decides
        self.assertEqual((pairs, ambiguous), ([(0, 0), (1, 1)], set()))

    def test_the_cumulative_total_decides_between_equal_lengths(self):
        """On a cumulative-timer OSD a run shows the boot's flying time before it; with two arms of one length, the one
        at that place in its boot is the run's (as in a clip's own alignment)."""
        pairs, _ = osd.plan_session_pairs([{"clip": "001", "length": 30.0, "total_before": 0.0}],
                                          [{"length": 30.0, "cum_before": 0.0}, {"length": 30.0, "cum_before": 60.0}])
        self.assertEqual(pairs, [(0, 0)])                                     # by length alone a tie, which takes the later

    def test_clips_that_move_together_are_ambiguous(self):
        """Review on #41: a runner-up that moves two clips onto other arms together changes no single arm's owner, but
        both clips' arms: ambiguous all the same."""
        pairs, ambiguous = self.plan([("001", 92.0), ("002", 30.1)], [92.5, 30.0, 50.0, 91.6, 30.2])
        self.assertEqual((pairs, ambiguous), ([], {"001", "002"}))

    def test_a_thin_choice_inside_one_clip_is_not(self):
        """Within one clip (2026-10-16 001 and 004: a dozen short indoor hops of near equal length) which run is which
        arm is the clip's own pass to settle (its boot fit does); the clip's arms, and so every window, are the same
        either way. One clip and one run between two arms is the same case."""
        pairs, ambiguous = self.plan([("001", 5.0), ("001", 5.1), ("002", 30.0)], [5.05, 5.0, 5.1, 30.2])
        self.assertEqual(ambiguous, set())
        pairs, ambiguous = self.plan([("001", 92.0)], [92.5, 91.6])
        self.assertEqual(ambiguous, set())


class SessionAutomatchTest(unittest.TestCase):
    """The whole session through `automatch <session> --write`: three clips whose greedy picks cascade (the first
    takes the last arm), matched in order by the plan; a match set by hand is a fixed point; an ambiguous clip gets
    nothing written and says so."""
    S = "2026-10-05"
    ARMS = [92.5, 74.2, 91.6]
    RUNS = {"2026-10-05_001.mp4": 91.8, "2026-10-05_002.mp4": 73.15, "2026-10-05_003.mp4": 90.85}

    def setUp(self):
        self.ARMS, self.RUNS = list(self.ARMS), dict(self.RUNS)                  # a test may change its own
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Db(Path(self.tmp.name) / "t.db")
        index = lambda: {"file": BBL, "boots": [], "arms": [{"index": i + 1, "length": l, "frames": 100, "motors_spun": True, "vbat_start": 4.2,
                                                             "truncated": False} for i, l in enumerate(self.ARMS)]}
        runs = lambda p: [{"start": 10.0, "end": 10.0 + self.RUNS[p.name], "length": self.RUNS[p.name], "timer_max": int(self.RUNS[p.name]),
                           "total_before": None, "readings": 100}]
        for p in [mock.patch.object(sessions, "_store", self.db), mock.patch.object(sessions, "SERVER_URL", ""),
                  mock.patch.object(osd, "read_stats", lambda p, r, layout: [{} for _ in r]),
                  mock.patch.object(sessions, "clip_path", lambda s, c: Path(self.tmp.name) / s / c),
                  mock.patch.object(blackbox, "index_bbl", lambda b, **k: index()),
                  mock.patch.object(osd, "read_timers", lambda p, fps=2.0: {"layout": "air65f", "rows": [], "runs": runs(p)}),
                  mock.patch.object(osd, "video_arms", lambda rt, fps=2.0: rt["runs"])]:
            p.start()
            self.addCleanup(p.stop)
        self.db.ensure_session(self.S)
        self.db.attach(self.S, BBL)
        for f in self.RUNS:
            self.db.upsert_video(self.S, f, 300.0, name=f[:-4])

    def run_cli(self, *args):
        import io
        from contextlib import redirect_stderr, redirect_stdout
        from acroscope import cli
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            try:
                cli.main(["automatch", self.S, *args])
            except SystemExit:
                pass
        return out.getvalue(), err.getvalue()

    def arms(self):
        return {m["video"][-7:-4]: m["arm"] for m in self.db.load(self.S)["matches"]}

    def test_the_session_in_order(self):
        self.run_cli("--write")
        self.assertEqual(self.arms(), {"001": 1, "002": 2, "003": 3})

    def test_a_window_reaches_one_arm_into_its_neighbours(self):
        """The lengths can put a boundary between two clips one arm off (2026-10-11 004 and 005); the clip's own pass,
        with its boot fit, settles it, so a clip's window reaches one arm (PLAN_SLACK) into each neighbour's."""
        self.ARMS[:] = [10.0, 20.0, 30.0, 40.0, 50.0]
        self.RUNS.update({"2026-10-05_001.mp4": 10.0, "2026-10-05_002.mp4": 30.0, "2026-10-05_003.mp4": 50.0})
        w = {f[-7:-4]: p["windows"][BBL] for f, p in osd.session_plan(self.S).items()}
        self.assertEqual(w, {"001": (None, 4), "002": (None, None), "003": (2, None)})

    def test_a_hand_match_is_a_fixed_point(self):
        """A match in the session is a fixed point the plan aligns around: 004 by hand on arm 4, the three before it
        align against arms 1-3 in order (clip by clip, 001 would take arm 3 and leave the others nothing)."""
        self.ARMS[:] = [92.5, 74.2, 91.6, 50.0]
        self.RUNS["2026-10-05_004.mp4"] = 50.0
        self.db.upsert_video(self.S, "2026-10-05_004.mp4", 300.0, name="2026-10-05_004")
        self.db.set_match(self.S, "2026-10-05_004.mp4", BBL, 4, 12.0, "by hand")
        self.run_cli("--write", "--unmatched")
        self.assertEqual(self.arms(), {"001": 1, "002": 2, "003": 3, "004": 4})

    def test_clips_named_by_number_are_planned(self):
        """Review on #41: the plan is keyed by file, the clips were taken as typed: `automatch S 001 002 003` fell back
        to the cascade. Names are resolved first."""
        self.run_cli("001", "2026-10-05_002", "003", "--write")
        self.assertEqual(self.arms(), {"001": 1, "002": 2, "003": 3})

    def test_one_clip_named_alone_is_not_planned(self):
        with mock.patch.object(osd, "session_plan", side_effect=AssertionError("planned")):
            out, err = self.run_cli("001")
        self.assertNotIn("session plan", err)
        self.assertIn("1 matched", out)

    def test_no_plan_falls_back_clip_by_clip(self):
        with mock.patch.object(osd, "session_plan", side_effect=RuntimeError("no")):
            out, err = self.run_cli("--write")
        self.assertIn("no session plan", err)
        self.assertEqual(self.arms(), {"001": 3})                                    # the old cascade, said so

    def ambiguous_two(self):
        self.RUNS.update({"2026-10-05_001.mp4": 92.0, "2026-10-05_002.mp4": 92.0})      # two clips, three arms alike
        del self.RUNS["2026-10-05_003.mp4"]
        self.db.c.execute("DELETE FROM videos WHERE file = '2026-10-05_003.mp4'")
        self.ARMS[:] = [92.0, 92.0, 92.0]

    def test_an_ambiguous_clip_gets_nothing(self):
        """Ambiguous in the plan and nothing but lengths to go by in its own pass (one arm per boot): nothing written,
        said on stderr in words the player does not count as a match."""
        self.ambiguous_two()
        out, err = self.run_cli("--write")
        self.assertEqual(self.arms(), {})
        self.assertIn("ambiguous", err)
        self.assertNotIn(" matched", err)

    def test_ambiguous_clips_keep_json_whole(self):
        """Review on #41: the ambiguity line went to stdout before the JSON. Each ambiguous clip is a result like the
        others (its run, its window), with ambiguous set and no matches."""
        import json
        self.ambiguous_two()
        out, err = self.run_cli("--json")
        res = json.loads(out)
        self.assertEqual([(r["clip"], r["ambiguous"], r["matches"]) for r in res],
                         [("2026-10-05_001.mp4", True, []), ("2026-10-05_002.mp4", True, [])])
        self.assertTrue(all("video_arms" in r and "arm_window" in r and "name" in r for r in res))


class AmbiguousButSettledTest(unittest.TestCase):
    """Option A on #41: a clip ambiguous in the plan still gets its own pass, in the plan's window (which covers both
    alignments); its matches are written only when that pass had more than lengths to go by: every boot fit it used
    spans two arms or more and explains a clock. The clip's pass is stubbed: this pins the decision, not the pass."""
    S = "2026-10-08"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Db(Path(self.tmp.name) / "t.db")
        self.calls, self.fits = [], {}
        files = [f"{self.S}_00{n}.mp4" for n in (1, 2, 3, 4)]

        def automatch(session, clip, write=False, **k):
            self.calls.append((clip[-7:-4], write))
            return {"clip": clip, "name": clip[:-4], "readable": 1, "readings": 1, "video_arms": [{}], "unmatched_video_arms": [],
                    "arm_window": k.get("windows", {}).get(BBL, (None, None)), "flash_full": None,
                    "boot_fits": self.fits[clip[-7:-4]], "matches": [{"bbl": BBL, "arm": int(clip[-5]), "offset": 1.0, "video_length": 10,
                                                                       "log_length": 10}]}
        plan = {f: {"windows": {BBL: (None, None)}, "ambiguous": True, "planned": []} for f in files}
        for p in [mock.patch.object(sessions, "_store", self.db), mock.patch.object(sessions, "SERVER_URL", ""),
                  mock.patch.object(osd, "automatch", automatch), mock.patch.object(osd, "session_plan", lambda s, fps=2.0: plan)]:
            p.start()
            self.addCleanup(p.stop)
        self.db.ensure_session(self.S)
        for f in files:
            self.db.upsert_video(self.S, f, 300.0, name=f[:-4])

    def test_only_a_boot_fit_settles_it(self):
        import io
        import json
        from contextlib import redirect_stderr, redirect_stdout
        from acroscope import cli
        self.fits = {"001": {0: {"a": 1.0, "b": 2.0, "n": 2}},                 # two arms in one boot: settled
                     "002": {0: {"a": 1.0, "b": 2.0, "n": 1}},                 # one arm: only its length
                     "003": {0: {"a": 1.0, "b": 2.0, "n": 3, "rejected": True}},   # no clock explains it
                     "004": {}}                                                 # no boot at all
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            cli.main(["automatch", self.S, "--write", "--json"])
        self.assertEqual(self.calls, [("001", False), ("001", True), ("002", False), ("003", False), ("004", False)])
        res = {r["clip"][-7:-4]: r for r in json.loads(out.getvalue())}
        self.assertEqual({k: (r["ambiguous"], len(r["matches"])) for k, r in res.items()},
                         {"001": (True, 1), "002": (True, 0), "003": (True, 0), "004": (True, 0)})
        self.assertEqual(err.getvalue().count("left for a hand match"), 3)


class SessionPlanOrderTest(unittest.TestCase):
    """The plan's two orders: logs by their dump time (not by when they were attached), and a match already in the
    session as a fixed point that the clips after it align past."""
    S = "2026-10-06"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Db(Path(self.tmp.name) / "t.db")
        self.logs, self.runs, self.motors_off, self.boots, self.unreadable = {}, {}, set(), [], set()

        def index(b, **k):
            return {"file": b, "boots": self.boots, "arms": [{"index": i + 1, "length": l, "frames": 100, "motors_spun": i + 1 not in self.motors_off}
                                                         for i, l in enumerate(self.logs[b])]}

        def timers(p, fps=2.0):
            if p.name in self.unreadable:
                raise FileNotFoundError(p.name)
            return {"layout": "air65f", "rows": [], "name": p.name}
        for p in [mock.patch.object(sessions, "_store", self.db),
                  mock.patch.object(sessions, "clip_path", lambda s, c: Path(self.tmp.name) / s / c),
                  mock.patch.object(blackbox, "index_bbl", index),
                  mock.patch.object(osd, "read_timers", timers),
                  mock.patch.object(osd, "video_arms", lambda rt, fps=2.0: [r if isinstance(r, dict) else {"start": 5.0, "length": r} for r in self.runs[rt["name"]]])]:
            p.start()
            self.addCleanup(p.stop)
        self.db.ensure_session(self.S)

    def clips(self, *runs):
        for n, r in enumerate(runs, 1):
            f = f"{self.S}_{n:03d}.mp4"
            self.db.upsert_video(self.S, f, 300.0, name=f[:-4])
            self.runs[f] = r

    def planned(self):
        return {f[-7:-4]: p["planned"] for f, p in osd.session_plan(self.S).items()}

    def test_logs_in_dump_order(self):
        early, late = "BTFL_BLACKBOX_LOG_X_20261006_090000_V2.bbl", "BTFL_BLACKBOX_LOG_X_20261006_180000_V2.bbl"
        self.logs = {early: [60.0], late: [60.0]}
        self.db.attach(self.S, late)                                    # the evening's dump attached first
        self.db.attach(self.S, early)
        self.clips([60.0], [60.0])
        self.assertEqual(self.planned(), {"001": [(early, 1)], "002": [(late, 1)]})

    def test_another_sessions_matches_bound_this_one(self):
        """A day's clips are numbered across its sessions, so a log arm matched to an earlier-named clip in another
        session comes before this session's arms (2026-10-02: one log held both of the day's sessions)."""
        self.logs = {BBL: [60.0, 61.0, 60.4]}
        self.db.attach(self.S, BBL)
        self.db.ensure_session("2026-10-06-earlier")
        self.db.upsert_video("2026-10-06-earlier", f"{self.S}_001.mp4", 300.0, name=f"{self.S}_001")
        self.db.set_match("2026-10-06-earlier", f"{self.S}_001.mp4", BBL, 2, 5.0)      # 001, in the other session: arm 2
        f = f"{self.S}_002.mp4"
        self.db.upsert_video(self.S, f, 300.0, name=f[:-4])
        self.runs[f] = [60.0]
        self.assertEqual(self.planned(), {"002": [(BBL, 3)]})                     # not arm 1, nearer in length but before

    def test_clips_before_a_fixed_point_align_before_it(self):
        self.logs = {BBL: [74.6, 50.0, 74.1]}
        self.db.attach(self.S, BBL)
        self.clips([74.1], [50.0], [74.1])
        self.db.set_match(self.S, f"{self.S}_002.mp4", BBL, 2, 5.0, "by hand")
        self.assertEqual(self.planned()["001"], [(BBL, 1)])                       # arm 3 fits better, but it is after 002's

    def test_an_arm_held_elsewhere_is_left_out(self):
        self.logs = {BBL: [60.0, 60.4]}
        self.db.attach(self.S, BBL)
        self.db.ensure_session("2026-10-06-other")
        self.db.upsert_video("2026-10-06-other", "c0ffee.mp4", 300.0)               # no readable name: no place in time
        self.db.set_match("2026-10-06-other", "c0ffee.mp4", BBL, 1, 5.0)
        self.clips([60.0])
        self.assertEqual(self.planned(), {"001": [(BBL, 2)]})

    def test_a_later_named_clip_elsewhere_bounds_from_above(self):
        self.logs = {BBL: [60.0, 60.2, 60.1]}
        self.db.attach(self.S, BBL)
        self.db.ensure_session("2026-10-06-later")
        self.db.upsert_video("2026-10-06-later", f"{self.S}_009.mp4", 300.0, name=f"{self.S}_009")
        self.db.set_match("2026-10-06-later", f"{self.S}_009.mp4", BBL, 2, 5.0)
        f = f"{self.S}_002.mp4"
        self.db.upsert_video(self.S, f, 300.0, name=f[:-4])
        self.runs[f] = [60.1]
        self.assertEqual(self.planned(), {"002": [(BBL, 1)]})                     # arm 3 fits exactly, but 009 holds arm 2

    def test_a_missing_clips_arm_is_taken(self):
        """Review on #41: a missing clip keeps its matches; its arm is neither a fixed point nor free for a neighbour."""
        self.logs = {BBL: [60.0, 74.0, 74.5]}
        self.db.attach(self.S, BBL)
        self.clips([60.0], [74.0], [74.0])
        self.db.set_match(self.S, f"{self.S}_002.mp4", BBL, 2, 5.0)
        self.db.upsert_video(self.S, f"{self.S}_002.mp4", missing_since="2026-10-07T00:00:00")
        p = self.planned()
        self.assertNotIn("002", p)
        self.assertEqual(p["003"], [(BBL, 3)])

    def test_arms_that_did_not_fly_are_left_out(self):
        self.logs = {BBL: [60.0, 60.0]}
        self.db.attach(self.S, BBL)
        self.motors_off = {2}
        self.clips([60.0])
        self.assertEqual(self.planned(), {"001": [(BBL, 1)]})

    def test_the_boots_total_counts_in_the_session_plan(self):
        self.logs = {BBL: [30.0, 30.0]}
        self.boots = [{"first": 1, "last": 2, "arms": [1, 2]}]
        self.db.attach(self.S, BBL)
        f = f"{self.S}_001.mp4"
        self.db.upsert_video(self.S, f, 300.0, name=f[:-4])
        self.runs[f] = [{"start": 5.0, "length": 30.0, "total_before": 0.0}]
        self.assertEqual(self.planned(), {"001": [(BBL, 1)]})                     # lengths tie; the total says arm 1

    def test_two_quads_flown_in_turn(self):
        """Review on #41: two quads' logs dumped one after the other are not one chain: OTTO's arms and OTTOJR's
        interleave in the clips. Each craft's logs are planned on their own."""
        otto, jr = "BTFL_BLACKBOX_LOG_OTTO_20261006_180000_V2.bbl", "BTFL_BLACKBOX_LOG_OTTOJR_20261006_180500_V2.bbl"
        self.logs = {otto: [60.0, 90.0], jr: [30.0, 45.0]}
        self.db.attach(self.S, otto)
        self.db.attach(self.S, jr)
        self.clips([60.0], [30.0], [90.0], [45.0])
        self.assertEqual(self.planned(), {"001": [(otto, 1)], "002": [(jr, 1)], "003": [(otto, 2)], "004": [(jr, 2)]})

    def test_no_named_clip_no_plan(self):
        self.logs = {BBL: [60.0]}
        self.db.attach(self.S, BBL)
        self.db.upsert_video(self.S, "c0ffee.mp4", 300.0)
        self.db.ensure_session("2026-10-06-other")
        self.db.upsert_video("2026-10-06-other", f"{self.S}_001.mp4", 300.0, name=f"{self.S}_001")
        self.db.set_match("2026-10-06-other", f"{self.S}_001.mp4", BBL, 1, 5.0)
        self.assertEqual(osd.session_plan(self.S), {})

    def test_an_unreadable_or_unprobed_clip_drops_out_alone(self):
        self.logs = {BBL: [60.0, 30.0]}
        self.db.attach(self.S, BBL)
        self.clips([60.0], [30.0])
        self.unreadable = {f"{self.S}_001.mp4"}
        self.db.upsert_video(self.S, f"{self.S}_003.mp4", name=f"{self.S}_003")       # no duration yet: not read at all
        p = self.planned()
        self.assertEqual((p["001"], p["002"], "003" in p), ([], [(BBL, 2)], False))

    def test_clips_after_a_fixed_point_align_past_it(self):
        self.logs = {BBL: [74.1, 50.0, 74.6]}
        self.db.attach(self.S, BBL)
        self.clips([74.1], [50.0], [74.1])
        self.db.set_match(self.S, f"{self.S}_002.mp4", BBL, 2, 5.0, "by hand")
        self.assertEqual(self.planned(), {"001": [(BBL, 1)], "002": [(BBL, 2)], "003": [(BBL, 3)]})


if __name__ == "__main__":
    unittest.main()
