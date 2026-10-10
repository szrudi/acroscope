"""Clip resolution against the database (no files on disk): by id, readable name or number, and ambiguity."""
import tempfile
import unittest
from pathlib import Path

from acroscope import sessions, video
from acroscope.db import Db


class ResolveClipTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.saved = (sessions._store, sessions.VIDEOS_DIR, video.VIDEOS_DIR)
        sessions._store = Db(root / "t.db")
        sessions.VIDEOS_DIR = video.VIDEOS_DIR = root / "videos"
        sessions.VIDEOS_DIR.mkdir()

    def tearDown(self):
        sessions._store, sessions.VIDEOS_DIR, video.VIDEOS_DIR = self.saved
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


if __name__ == "__main__":
    unittest.main()
