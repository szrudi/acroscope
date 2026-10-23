"""Clip resolution against the database (no files on disk): by id, readable name or number, and ambiguity."""
import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from acroscope import cli
from acroscope.sources import blackbox, osd, video
from acroscope.store import sessions
from acroscope.store.db import Db


class _Scratch(unittest.TestCase):
    """A database and a videos/ folder of their own, patched into the modules; no test of its own."""
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.saved = (sessions._store, sessions.VIDEOS_DIR, video.VIDEOS_DIR, sessions.DATA_DIR, blackbox.BLACKBOX_DIR)
        sessions._store = Db(root / "t.db")
        sessions.DATA_DIR = root
        sessions.VIDEOS_DIR = video.VIDEOS_DIR = root / "videos"
        sessions.VIDEOS_DIR.mkdir()
        blackbox.BLACKBOX_DIR = root / "blackbox"                     # no real logs get attached by date

    def tearDown(self):
        sessions._store, sessions.VIDEOS_DIR, video.VIDEOS_DIR, sessions.DATA_DIR, blackbox.BLACKBOX_DIR = self.saved
        self.tmp.cleanup()

    def session_with_clip(self, name, file="2026-10-09_001.mp4"):
        (sessions.VIDEOS_DIR / name).mkdir()
        (sessions.VIDEOS_DIR / name / file).write_bytes(b"")
        sessions.refresh(name, probe_videos=False)
        sessions.tag(name, file, 1, 2, "keep me", [])
        return file


class ResolveClipTest(_Scratch):
    def test_ingested_clip_by_id_name_or_number(self):
        S = "2026-10-07-s"
        sessions.store().upsert_video(S, "c0ffee0001.mp4", name="2026-10-07_003")      # the file name is the id
        for q in ("c0ffee0001.mp4", "2026-10-07_003", "003", "3"):
            self.assertEqual(sessions.resolve_clip(S, q), "c0ffee0001.mp4", q)
            self.assertEqual(sessions.clip_path(S, q), sessions.VIDEOS_DIR / S / "c0ffee0001.mp4", q)
        with self.assertRaises(FileNotFoundError):
            sessions.resolve_clip(S, "004")

    def test_ambiguous_number_is_refused(self):
        S = "2026-10-07-s"
        (sessions.VIDEOS_DIR / S).mkdir()
        for f in ("2026-10-07_007.mp4", "2026-10-08_007.mp4"):                           # one moved in from another day
            (sessions.VIDEOS_DIR / S / f).write_bytes(b"")
            sessions.store().upsert_video(S, f)
        with self.assertRaises(FileNotFoundError):
            sessions.resolve_clip(S, "007")
        self.assertEqual(sessions.resolve_clip(S, "2026-10-08_007"), "2026-10-08_007.mp4")
        with self.assertRaises(ValueError):
            sessions.resolve_clip("../../etc", "007")                                    # a name, never a path

    def test_original_mov_beside_its_mp4_is_one_clip(self):
        S = "2026-10-07-s"
        (sessions.VIDEOS_DIR / S).mkdir()
        for f in ("2026-10-07_007.mov", "2026-10-07_007.mp4"):
            (sessions.VIDEOS_DIR / S / f).write_bytes(b"")
            sessions.store().upsert_video(S, f)
        self.assertEqual(sessions.resolve_clip(S, "007"), "2026-10-07_007.mp4")


class EditGuardsTest(_Scratch):
    def test_merge_into_itself_is_refused(self):
        S = "2026-10-09-g"
        f = self.session_with_clip(S)
        (sessions.VIDEOS_DIR / S / f).unlink()                     # the file is gone (or the mount is half there)
        with self.assertRaises(ValueError):
            sessions.merge_sessions(S, S)
        self.assertEqual(len(sessions.load(S)["moments"]), 1)

    def test_rename_checks_every_target_before_moving(self):
        S = "2026-10-09-h"
        self.session_with_clip(S)
        (sessions.DATA_DIR / "originals" / S).mkdir(parents=True)              # so the old code moved videos/ first, then failed here
        (sessions.DATA_DIR / "originals" / "2026-10-10-taken").mkdir(parents=True)
        with self.assertRaises(ValueError):
            sessions.rename_session(S, "2026-10-10-taken")
        self.assertTrue((sessions.VIDEOS_DIR / S).is_dir())         # videos/ was not moved on its own
        self.assertTrue(sessions.store().exists(S))

    def test_edits_refuse_an_unmounted_data_dir(self):
        S = "2026-10-09-k"
        f = self.session_with_clip(S)
        import shutil
        shutil.rmtree(sessions.VIDEOS_DIR)                          # what an unmounted bind mount looks like
        sessions.VIDEOS_DIR.mkdir()
        for call in (lambda: sessions.move_clip(S, f, "2026-10-09-l"), lambda: sessions.rename_session(S, "2026-10-09-l"),
                     lambda: sessions.merge_sessions(S, "2026-10-09-l")):
            with self.assertRaises(ValueError):
                call()
        self.assertEqual([v["file"] for v in sessions.load(S)["videos"]], [f])

    def test_refresh_marks_missing_and_back_but_never_on_an_empty_folder(self):
        S = "2026-10-09-m"
        d = sessions.VIDEOS_DIR / S
        d.mkdir()
        for f in ("a.mp4", "b.mp4"):
            (d / f).write_bytes(b"")
        missing = lambda: sorted(v["file"] for v in sessions.refresh(S, probe_videos=False)["videos"] if v.get("missing_since"))
        self.assertEqual(missing(), [])
        (d / "b.mp4").unlink()
        self.assertEqual(missing(), ["b.mp4"])
        (d / "b.mp4").write_bytes(b"")
        self.assertEqual(missing(), [])                                  # back: the mark is cleared
        (d / "a.mp4").unlink(); (d / "b.mp4").unlink()
        self.assertEqual(missing(), [])                                  # an empty folder marks nothing (a half-synced mount)
        import shutil
        shutil.rmtree(d)
        self.assertEqual(missing(), [])                                  # nor does a missing one

    def test_merge_target_is_a_session_name(self):
        S = "2026-10-09-n"
        (sessions.VIDEOS_DIR / S).mkdir()
        sessions.store().attach(S, "x.bbl")                          # logs only: no clip whose move would check the name
        with self.assertRaises(ValueError):
            sessions.merge_sessions(S, "../../x")
        self.assertTrue(sessions.store().exists(S))
        self.assertFalse(sessions.store().exists("../../x"))

    def test_detach_a_log_that_is_gone(self):
        S = "2026-10-09-d"
        self.session_with_clip(S)
        sessions.store().attach(S, "deleted.bbl")                    # attached once, the file removed since
        self.assertEqual(sessions.detach(S, "deleted.bbl"), [])

    def test_detach_drops_that_logs_matches_here_only(self):
        """A log out of a session takes its matches there with it (they would point at a log the session no longer
        has); the session's other log keeps its matches, and so does another session holding the same log."""
        S, T = "2026-10-09-d", "2026-10-09-e"
        f = self.session_with_clip(S)
        g = self.session_with_clip(T, "2026-10-09_002.mp4")
        sessions.store().set_match(S, f, "a.bbl", 1, 5.0)
        sessions.store().set_match(S, f, "b.bbl", 1, 90.0)          # the same arm number in the other log
        sessions.store().set_match(T, g, "a.bbl", 2, 7.0)
        self.assertEqual(sessions.detach(S, "a.bbl"), ["b.bbl"])
        self.assertEqual([(m["bbl"], m["arm"]) for m in sessions.load(S)["matches"]], [("b.bbl", 1)])
        self.assertEqual([(m["bbl"], m["arm"]) for m in sessions.load(T)["matches"]], [("a.bbl", 2)])

    def test_a_log_taken_out_stays_out_through_a_refresh(self):
        """A log of the session's date is attached by the refresh; taken out (🗑, or ⇄ to another session) it stays
        out, though the refresh still finds the session without a log. Attached again by hand, it counts again."""
        S = "2026-10-15-x"
        self.session_with_clip(S, "2026-10-15_001.mp4")
        blackbox.BLACKBOX_DIR.mkdir()
        log = "BTFL_BLACKBOX_LOG_OTTOJR_20261015_094754_BETAFPVG473_V2.bbl"
        (blackbox.BLACKBOX_DIR / log).write_bytes(b"H Product:Blackbox flight data recorder")
        self.assertEqual(sessions.refresh(S, probe_videos=False)["blackbox"], [log])
        sessions.detach(S, log)
        self.assertEqual(sessions.refresh(S, probe_videos=False)["blackbox"], [])
        sessions.attach(S, log)
        sessions.store().set_blackbox(S, [])                          # emptied some other way: the date rule is back
        self.assertEqual(sessions.refresh(S, probe_videos=False)["blackbox"], [log])

    def test_move_log(self):
        """⇄: the log into another session in one go, its matches here deleted, and it does not come back here."""
        S, T = "2026-10-15-x", "2026-10-14-schammer"
        f = self.session_with_clip(S, "2026-10-15_001.mp4")
        self.session_with_clip(T, "2026-10-14_022.mp4")
        blackbox.BLACKBOX_DIR.mkdir()
        log = "BTFL_BLACKBOX_LOG_OTTOJR_20261015_094754_BETAFPVG473_V2.bbl"
        (blackbox.BLACKBOX_DIR / log).write_bytes(b"H Product:Blackbox flight data recorder")
        sessions.refresh(S, probe_videos=False)
        sessions.store().set_match(S, f, log, 1, 3.0)
        with self.assertRaises(ValueError):                           # a target that does not exist: nothing moves
            sessions.move_log(S, log, "2026-10-14-typo")
        self.assertEqual(sessions.load(S)["blackbox"], [log])
        self.assertFalse(sessions.store().exists("2026-10-14-typo"))
        sessions.move_log(S, log, T)
        self.assertEqual((sessions.load(S)["blackbox"], sessions.load(S)["matches"], sessions.load(T)["blackbox"]), ([], [], [log]))
        self.assertEqual(sessions.refresh(S, probe_videos=False)["blackbox"], [])

    def log_of(self, date="20261015"):
        blackbox.BLACKBOX_DIR.mkdir(exist_ok=True)
        log = f"BTFL_BLACKBOX_LOG_OTTOJR_{date}_094754_BETAFPVG473_V2.bbl"
        (blackbox.BLACKBOX_DIR / log).write_bytes(b"H Product:Blackbox flight data recorder")
        return log

    def test_marks_survive_a_rename_and_a_merge(self):
        """A log taken out stays out of a session renamed, and of the session another one is merged into (its clips
        came before the dump, so it has no log of its own for the date rule to see)."""
        A, B = "2026-10-15-a", "2026-10-15-b"
        self.session_with_clip(A, "2026-10-15_001.mp4")
        log = self.log_of()
        sessions.refresh(A, probe_videos=False)
        sessions.detach(A, log)
        sessions.rename_session(A, "2026-10-15-a2")
        self.assertEqual(sessions.refresh("2026-10-15-a2", probe_videos=False)["blackbox"], [])
        (sessions.VIDEOS_DIR / B).mkdir()
        (sessions.VIDEOS_DIR / B / "2026-10-15_002.mp4").write_bytes(b"")
        sessions.store().ensure_session(B)
        sessions.store().upsert_video(B, "2026-10-15_002.mp4")
        sessions.merge_sessions("2026-10-15-a2", B)
        self.assertEqual(sessions.refresh(B, probe_videos=False)["blackbox"], [])

    def test_move_log_guards(self):
        """⇄ onto the session itself changes nothing; a log the session has not is refused; into a session that has
        the log already it is not doubled; a mark the target had for it goes; an unknown source says so."""
        S, T = "2026-10-15-x", "2026-10-15-t"
        f = self.session_with_clip(S, "2026-10-15_001.mp4")
        self.session_with_clip(T, "2026-10-15_002.mp4")
        log = self.log_of()
        sessions.refresh(S, probe_videos=False)
        sessions.store().set_match(S, f, log, 1, 3.0)
        sessions.move_log(S, log, S)
        self.assertEqual((sessions.load(S)["blackbox"], len(sessions.load(S)["matches"])), ([log], 1))
        with self.assertRaisesRegex(ValueError, "no log"):
            sessions.move_log(S, "other.bbl", T)
        with self.assertRaisesRegex(ValueError, "no such session"):
            sessions.move_log("2026-10-15-nope", log, T)
        sessions.refresh(T, probe_videos=False)                       # T has the same-date log too
        sessions.detach(T, log)
        sessions.move_log(S, log, T)
        self.assertEqual(sessions.load(T)["blackbox"], [log])
        self.assertEqual(sessions.store().detached(T), set())
        sessions.store().attach(S, log)
        sessions.move_log(S, log, T)                                  # T has it already: once
        self.assertEqual(sessions.load(T)["blackbox"], [log])

    def test_detach_from_an_unknown_session(self):
        self.assertEqual(sessions.store().detach("2026-10-15-nope", "x.bbl"), [])

    def test_merge_carries_logs_and_notes(self):
        A, B = "2026-10-09-a", "2026-10-09-b"
        self.session_with_clip(A, "2026-10-09_001.mp4")
        self.session_with_clip(B, "2026-10-09_002.mp4")
        sessions.store().attach(A, "x.bbl")
        sessions.store().set_note(A, "from a")
        s = sessions.merge_sessions(A, B)
        self.assertEqual((s["blackbox"], s["note"], len(s["videos"]), len(s["moments"])), (["x.bbl"], "from a", 2, 2))
        self.assertFalse(sessions.store().exists(A))
        self.assertFalse((sessions.VIDEOS_DIR / A).exists())

    def test_a_probe_running_through_a_merge_does_not_bring_the_session_back(self):
        # #16: the player opens a session whose clips have no duration yet, which starts a background refresh that
        # probes them; the merge lands while ffprobe runs. The probe's results must not make the merged-away session
        # (or a row of the clip that left it) again. Flaky in test_player before (2 runs in 8): here the merge
        # happens inside the probe, every time
        A, B = "2026-10-09-a", "2026-10-09-b"
        self.session_with_clip(A, "2026-10-09_001.mp4")
        self.session_with_clip(B, "2026-10-09_002.mp4")

        def probe_while_merging(path):
            sessions.merge_sessions(A, B)
            return {"duration": 12.5, "codec": "h264"}
        with mock.patch.object(video, "probe", probe_while_merging):
            sessions.refresh(A)
        self.assertFalse(sessions.store().exists(A))
        self.assertEqual(sessions.store().c.execute("SELECT count(*) FROM videos WHERE session = ?", (A,)).fetchone()[0], 0)
        self.assertEqual(sorted(v["file"] for v in sessions.load(B)["videos"]), ["2026-10-09_001.mp4", "2026-10-09_002.mp4"])

    def test_merge_checks_every_clip_before_moving_one(self):
        # a merge that would stop at clip 2 must not have moved clip 1 already: nothing moves, and the error names
        # every clash. Clashes: the clip's file in the target folder, its original in the target's originals/,
        # and its row in the target session
        A, B = "2026-10-09-a", "2026-10-09-b"
        for f in ("2026-10-09_001.mp4", "2026-10-09_002.mp4", "2026-10-09_003.mp4"):
            self.session_with_clip(A, f) if f.endswith("1.mp4") else (sessions.VIDEOS_DIR / A / f).write_bytes(b"")
        sessions.refresh(A, probe_videos=False)
        (sessions.DATA_DIR / "originals" / A).mkdir(parents=True)
        (sessions.DATA_DIR / "originals" / A / "VID1.mov").write_bytes(b"a's")
        sessions.store().set_video_fields(A, "2026-10-09_001.mp4", original=f"originals/{A}/VID1.mov")
        self.session_with_clip(B, "2026-10-09_002.mp4")                         # the same file id: a clash on disk
        (sessions.DATA_DIR / "originals" / B).mkdir(parents=True)
        (sessions.DATA_DIR / "originals" / B / "VID1.mov").write_bytes(b"b's")  # rename() would replace it silently
        sessions.store().upsert_video(B, "2026-10-09_003.mp4")                   # a row only (file gone): a clash in the db
        before = (sorted(p.name for p in (sessions.VIDEOS_DIR / A).iterdir()), sessions.load(A), sessions.load(B))
        with self.assertRaises(ValueError) as cm:
            sessions.merge_sessions(A, B)
        for clash in ("2026-10-09_002.mp4", "VID1.mov", "2026-10-09_003.mp4"):
            self.assertIn(clash, str(cm.exception))
        self.assertEqual((sorted(p.name for p in (sessions.VIDEOS_DIR / A).iterdir()), sessions.load(A), sessions.load(B)), before)
        self.assertEqual((sessions.DATA_DIR / "originals" / B / "VID1.mov").read_bytes(), b"b's")
        self.assertTrue((sessions.DATA_DIR / "originals" / A / "VID1.mov").exists())

    def test_move_clip_does_not_replace_an_original(self):
        A, B = "2026-10-09-a", "2026-10-09-b"
        self.session_with_clip(A, "2026-10-09_001.mp4")
        self.session_with_clip(B, "2026-10-09_002.mp4")
        for s, data in ((A, b"a's"), (B, b"b's")):
            (sessions.DATA_DIR / "originals" / s).mkdir(parents=True)
            (sessions.DATA_DIR / "originals" / s / "VID1.mov").write_bytes(data)
        sessions.store().set_video_fields(A, "2026-10-09_001.mp4", original=f"originals/{A}/VID1.mov")
        with self.assertRaisesRegex(ValueError, "VID1.mov"):
            sessions.move_clip(A, "001", B)
        self.assertEqual((sessions.DATA_DIR / "originals" / B / "VID1.mov").read_bytes(), b"b's")
        self.assertTrue((sessions.VIDEOS_DIR / A / "2026-10-09_001.mp4").exists())


class SessionsListingTest(_Scratch):
    def test_a_retimed_clip_says_so(self):
        """#30: `acroscope sessions <s>` marks a clip re-timed for a mis-stamped frame rate, with the factor."""
        S = "2026-10-14-x"
        f = self.session_with_clip(S, "2026-10-14_007.mp4")
        sessions.store().set_video_fields(S, f, timescale=2.2001, duration=259.8)
        out = io.StringIO()
        with mock.patch.object(sessions, "SERVER_URL", ""), redirect_stdout(out):
            cli.main(["sessions", S])
        line = next(l for l in out.getvalue().splitlines() if "2026-10-14_007" in l)
        self.assertIn("RE-TIMED x2.2001 (mis-stamped frame rate, no sound)", line)


class AutomatchExitTest(_Scratch):
    """automatch tells by its exit code whether it could run (review finding 10): clips named on the command line
    must all go through; a whole-session run fails only when no clip did (some clips there have no log, as 10-07's
    001/002 and 012-019). With --json the failures are in the output too."""
    S = "2026-10-09-am"

    def setUp(self):
        super().setUp()
        for n in (1, 2):
            sessions.store().upsert_video(self.S, f"2026-10-09_00{n}.mp4", 60.0, "h264")

    def automatch(self, *args, fail=()):
        def fake(session, clip, **kw):
            if clip in fail:
                raise FileNotFoundError(f"{clip}: no log covers it")
            return {"clip": clip, "matches": []}
        out, err = io.StringIO(), io.StringIO()
        # SERVER_URL pinned empty: an unset ACROSCOPE_URL means the URL file, and cli.main would proxy this to it
        with mock.patch.object(sessions, "SERVER_URL", ""), mock.patch.object(osd, "automatch", fake), \
                redirect_stdout(out), redirect_stderr(err):
            try:
                cli.main(["automatch", self.S, *args, "--json"])
                code = 0
            except SystemExit as e:
                code = e.code if isinstance(e.code, int) else 1
        return code, json.loads(out.getvalue() or "null"), err.getvalue()

    def test_a_named_clip_that_fails_fails_the_command(self):
        code, res, err = self.automatch("2026-10-09_002.mp4", fail={"2026-10-09_002.mp4"})
        self.assertEqual(code, 1)
        self.assertIn("no log covers it", err)
        self.assertEqual(res["errors"], [{"clip": "2026-10-09_002.mp4", "error": "FileNotFoundError: 2026-10-09_002.mp4: no log covers it"}])
        code, _, _ = self.automatch("2026-10-09_001.mp4", "2026-10-09_002.mp4", fail={"2026-10-09_002.mp4"})
        self.assertEqual(code, 1)

    def test_a_session_run_fails_only_when_no_clip_went_through(self):
        code, res, _ = self.automatch(fail={"2026-10-09_002.mp4"})
        self.assertEqual(code, 0)
        self.assertEqual(([r["clip"] for r in res["results"]], [e["clip"] for e in res["errors"]]), (["2026-10-09_001.mp4"], ["2026-10-09_002.mp4"]))
        code, res, _ = self.automatch(fail={"2026-10-09_001.mp4", "2026-10-09_002.mp4"})
        self.assertEqual(code, 1)

    def test_json_without_errors_keeps_its_shape(self):
        code, res, _ = self.automatch("2026-10-09_001.mp4")
        self.assertEqual((code, res), (0, {"clip": "2026-10-09_001.mp4", "matches": []}))   # one clip: the result itself
        code, res, _ = self.automatch()
        self.assertEqual((code, [r["clip"] for r in res]), (0, ["2026-10-09_001.mp4", "2026-10-09_002.mp4"]))

    def test_unmatched_takes_only_clips_without_a_match(self):
        """--unmatched (the player's automatch button): a clip matched by hand is never touched, a missing clip and
        one not probed yet are skipped, and nothing left to match is not a failure."""
        sessions.store().set_match(self.S, "2026-10-09_001.mp4", "a.bbl", 1, 5.0)
        sessions.store().upsert_video(self.S, "2026-10-09_003.mp4", 60.0, "h264", missing_since="2026-10-10T00:00:00")
        sessions.store().upsert_video(self.S, "2026-10-09_004.mp4")
        code, res, _ = self.automatch("--unmatched")
        self.assertEqual((code, res), (0, {"clip": "2026-10-09_002.mp4", "matches": []}))
        sessions.store().set_match(self.S, "2026-10-09_002.mp4", "a.bbl", 2, 5.0)
        code, res, err = self.automatch("--unmatched")
        self.assertEqual((code, res), (0, []))
        self.assertIn("every clip has a match", err)

    def test_unmatched_by_number_and_with_a_log_gone(self):
        """A clip named by its number is the clip; a match to a log no longer in the session is no match."""
        sessions.store().set_match(self.S, "2026-10-09_001.mp4", "a.bbl", 1, 5.0)
        code, res, _ = self.automatch("001", "--unmatched")
        self.assertEqual((code, res), (0, []))
        sessions.store().set_blackbox(self.S, [])                     # a.bbl gone from the session, its match left behind
        code, res, _ = self.automatch("001", "--unmatched")
        self.assertEqual((code, res), (0, {"clip": "2026-10-09_001.mp4", "matches": []}))

    def test_unmatched_with_clips_named(self):
        """Named clips under --unmatched: an unknown name is an error like without the flag (in `errors`, exit 1), a
        missing clip is an error too, a matched one is skipped and said so."""
        sessions.store().set_match(self.S, "2026-10-09_001.mp4", "a.bbl", 1, 5.0)
        code, res, err = self.automatch("nope", "--unmatched")
        self.assertEqual(code, 1)
        self.assertEqual([e["clip"] for e in res["errors"]], ["nope"])
        sessions.store().upsert_video(self.S, "2026-10-09_002.mp4", missing_since="2026-10-10T00:00:00")
        code, res, err = self.automatch("002", "--unmatched")
        self.assertEqual(code, 1)
        self.assertIn("missing", res["errors"][0]["error"])
        self.assertNotIn("every clip has a match", err)
        code, res, err = self.automatch("001", "--unmatched")
        self.assertEqual((code, res), (0, []))
        self.assertIn("has a match, skipped", err)

    def test_unmatched_with_nothing_probed_is_still_a_failure(self):
        sessions.store().upsert_video("2026-10-09-new", "2026-10-09_001.mp4")
        S, self.S = self.S, "2026-10-09-new"
        try:
            code, _, err = self.automatch("--unmatched")
        finally:
            self.S = S
        self.assertEqual(code, 1)
        self.assertIn("no probed clips", err)


if __name__ == "__main__":
    unittest.main()
