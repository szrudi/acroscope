"""Decoding a .bbl into the arm index, with orangebox stubbed: a log segment orangebox cannot open."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import orangebox
import orangebox.reader

from acroscope import blackbox


class UnreadableSegmentTest(unittest.TestCase):
    """2026-10-16's 12 MB dump: 61 segments, the 27th unreadable (orangebox's Reader computes a negative size for it:
    `read length must be non-negative or -1`). That one exception killed the whole decode, so no index was written,
    and the server started the decode again whenever the session was looked at, for an hour."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        (root / "blackbox").mkdir()
        self.bbl = root / "blackbox" / "BTFL_BLACKBOX_LOG_OTTOJR_20261016_185732_BETAFPVG473_V2.bbl"
        self.bbl.write_bytes(b"H Product:Blackbox flight data recorder\n")
        cache = root / "cache"

        def load(path, index):
            raise ValueError("read length must be non-negative or -1")
        reader = mock.Mock(log_count=3)
        for p in (mock.patch.object(blackbox, "BLACKBOX_DIR", root / "blackbox"),
                  mock.patch.object(blackbox, "cache", lambda *parts: (cache.joinpath(*parts[:-1]).mkdir(parents=True, exist_ok=True), cache.joinpath(*parts))[1]),
                  mock.patch.object(orangebox.Parser, "load", staticmethod(load)),
                  mock.patch.object(orangebox.reader, "Reader", lambda path: reader)):
            p.start()
            self.addCleanup(p.stop)

    def tearDown(self):
        self.tmp.cleanup()

    def test_an_unreadable_segment_is_noted_and_the_rest_indexed(self):
        idx = blackbox.index_bbl(self.bbl.name)
        self.assertEqual([a["index"] for a in idx["arms"]], [1, 2, 3])
        self.assertEqual({a["frames"] for a in idx["arms"]}, {0})
        self.assertIn("read length must be non-negative", idx["arms"][1]["error"])
        # the index is written, so the next look finds it instead of starting the decode again
        self.assertIsNotNone(blackbox.index_bbl(self.bbl.name, cached_only=True))
        self.assertEqual(json.loads(blackbox.cache("arms", self.bbl.stem, "arms.json").read_text())["arms"][1]["index"], 2)


class OverviewTest(unittest.TestCase):
    def test_the_overview_says_which_segments_hold_frames(self):
        """The player tells an arm from an unreadable segment by `frames` and `error`, so the overview passes them on."""
        from acroscope import sessions
        from acroscope.db import Db
        with tempfile.TemporaryDirectory() as tmp:
            db = Db(Path(tmp) / "t.db")
            db.ensure_session("2026-10-16-x")
            db.attach("2026-10-16-x", "L.bbl")
            idx = {"file": "L.bbl", "boots": [], "arms": [{"index": 1, "frames": 500, "length": 10.0},
                                                         {"index": 2, "frames": 0, "length": 0.0, "error": "ValueError: bad"}]}
            with mock.patch.object(sessions, "_store", db), mock.patch.object(sessions, "SERVER_URL", ""), \
                    mock.patch.object(blackbox, "index_bbl", lambda b, **k: idx):
                arms = sessions.overview("2026-10-16-x")["arms"]
        self.assertEqual([(a["index"], a["frames"], a.get("error")) for a in arms], [(1, 500, None), (2, 0, "ValueError: bad")])


if __name__ == "__main__":
    unittest.main()
