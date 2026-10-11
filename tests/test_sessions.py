"""Clip resolution against the database (no files on disk): by id, readable name or number, and ambiguity."""
import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from acroscope import blackbox, cli, osd, sessions, video
from acroscope.db import Db


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


if __name__ == "__main__":
    unittest.main()
