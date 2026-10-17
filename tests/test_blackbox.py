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


class LeanTest(unittest.TestCase):
    """#38: lean[right] had the wrong sign. Betaflight's body axes are X forward, Y LEFT, Z up: world-up in body axes is
    its rMat[2] row, and its roll angle, + for a right bank, is atan2(uy, uz). Checked on 10-14 016 too: near level and
    rolling right (gyro roll +), lean[right] fell in 434 of 486 samples."""

    def arm(self, q):
        """An arm holding one attitude, as the log stores it: imuQuaternion x, y, z * 32767 (w follows)."""
        import array
        import math
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        n, rate = 40, 200
        t = array.array("d", (i / rate for i in range(n)))
        cols = {f"imuQuaternion[{k}]": array.array("f", [q[k] * 32767] * n) for k in range(3)}
        meta = {"bbl": "x", "file": "x.bbl", "index": 1, "frames": n, "length": t[-1], "rate": rate, "fields": ["time", *cols]}
        p = Path(tmp.name) / "a.bin"
        blackbox._save(meta, t, cols, p)
        return blackbox._load(p)

    def test_a_known_bank_and_pitch(self):
        import math
        h = math.radians(30) / 2
        right = self.arm((math.sin(h), 0.0, 0.0))                  # about body X: Betaflight's roll +30 = right side down
        nose_down = self.arm((0.0, math.sin(h), 0.0))              # about body Y (left): + = nose down
        for a, want in ((right, (0, 30)), (nose_down, (30, 0))):
            self.assertEqual(tuple(round(x) for x in a.lean(5)), want)
            self.assertEqual(round(a.tilt(5)), 30)
        left = self.arm((-math.sin(h), 0.0, 0.0))
        self.assertEqual(tuple(round(x) for x in left.lean(5)), (0, -30))

    def test_orbit_lean_direction(self):
        """metrics.orbit's lean direction against the nose: 0 forward, 90 right, -90 left (it had the side flipped)."""
        import math
        from acroscope import metrics
        h = math.radians(30) / 2
        for q, want in (((math.sin(h), 0.0, 0.0), 90), ((-math.sin(h), 0.0, 0.0), -90), ((0.0, math.sin(h), 0.0), 0)):
            rows = metrics.orbit(self.arm(q), 0.0, 0.19, step=0.1)
            self.assertEqual(rows[0]["lean_dir_vs_nose"], want, q)

    def test_a_snapshot_says_its_lean_convention(self):
        """A moment's metrics snapshot (metrics.summary) carries the lean convention it was made with: the #38
        correction of stored snapshots flips only those without it, so it can run before or after the deploy, twice."""
        import array
        import math
        from acroscope import metrics
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        n, rate = 200, 200
        t = array.array("d", (i / rate for i in range(n)))
        names = [f"{f}[{k}]" for f in ("gyroADC", "accSmooth") for k in range(3)] + [f"rcCommand[{k}]" for k in range(4)] + ["vbatLatest"]
        cols = {c: array.array("f", [400.0 if c == "vbatLatest" else 0.0] * n) for c in names}
        cols |= {f"imuQuaternion[{k}]": array.array("f", [math.sin(math.radians(15)) * 32767 if k == 0 else 0.0] * n) for k in range(3)}
        p = Path(tmp.name) / "a.bin"
        blackbox._save({"bbl": "x", "file": "x.bbl", "index": 1, "frames": n, "length": t[-1], "rate": rate, "fields": ["time", *cols]}, t, cols, p)
        snap = metrics.summary(blackbox._load(p), 0.1, 0.9)
        self.assertEqual(snap["lean_start"], [0, 30])
        self.assertEqual(snap["lean_convention"], metrics.LEAN_CONVENTION)

    def test_the_conventions_say_which_side(self):
        from acroscope import metrics
        self.assertIn("right side down", metrics.CONVENTIONS["lean"])
        self.assertIn("right end rises", metrics.CONVENTIONS["roll"])   # the horizon, not "the picture": the video check (#38)


if __name__ == "__main__":
    unittest.main()
