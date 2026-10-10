"""Clip resolution against the database (no files on disk): by id, readable name or number, and ambiguity."""
import tempfile
import unittest
from pathlib import Path

from acroscope import blackbox, sessions, video
from acroscope.db import Db


class ResolveClipTest(unittest.TestCase):
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


class EditGuardsTest(ResolveClipTest):
    def session_with_clip(self, name, file="2026-10-09_001.mp4"):
        (sessions.VIDEOS_DIR / name).mkdir()
        (sessions.VIDEOS_DIR / name / file).write_bytes(b"")
        sessions.refresh(name, probe_videos=False)
        sessions.tag(name, file, 1, 2, "keep me", [])
        return file

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


if __name__ == "__main__":
    unittest.main()
