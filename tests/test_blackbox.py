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


class SegmentListingTest(unittest.TestCase):
    """#21: every log segment in a .bbl is listed, with its size, and one that holds no frames says why (the decoder's
    error, or empty), so a pack missing from the flash can be told from one that is there but unreadable.
    `acroscope arms` crashed on 2026-10-16's unreadable segment 27 (no motors_spun), and `sessions` called it
    "motors off, cut off"."""

    IDX = {"file": "L.bbl", "stem": "L", "boots": [{"first": 1, "last": 3, "arms": [1, 3], "uptime_start": 1.0, "uptime_end": 30.0, "length": 29.0}],
           "arms": [{"index": 1, "frames": 500, "length": 10.0, "uptime_start": 1.0, "uptime_end": 11.0, "vbat_start": 4.2, "vbat_end": 4.0,
                     "vbat_min": 3.9, "disarm_reason": 4, "motors_spun": True, "complete": True, "truncated": False},
                    {"index": 2, "frames": 0, "length": 0.0, "rate": 0.0, "truncated": False, "error": "ValueError: read length must be non-negative or -1"},
                    {"index": 3, "frames": 500, "length": 9.0, "uptime_start": 20.0, "uptime_end": 29.0, "vbat_start": 4.0, "vbat_end": 3.8,
                     "vbat_min": 3.7, "disarm_reason": 4, "motors_spun": True, "complete": True, "truncated": False},
                    {"index": 4, "frames": 0, "length": 0.0, "rate": 0.0, "truncated": False}]}

    def test_segment_sizes(self):
        with tempfile.TemporaryDirectory() as tmp:
            bbl = Path(tmp) / "L.bbl"
            head = b"H Product:Blackbox flight data recorder by Nicholas Sherlock\n"
            bbl.write_bytes(head + b"a" * 100 + head + b"b" * 50)
            with mock.patch.object(blackbox, "BLACKBOX_DIR", Path(tmp)):
                segs = blackbox.segments("L.bbl")
        n = len(head)
        self.assertEqual(segs, [{"index": 1, "start": 0, "size": n + 100}, {"index": 2, "start": n + 100, "size": n + 50}])

    def cli(self, *args):
        import io
        from contextlib import redirect_stdout
        from acroscope import cli, sessions
        out = io.StringIO()
        segs = [{"index": i, "start": 0, "size": sz} for i, sz in ((1, 300_000), (2, 6144), (3, 280_000), (4, 512))]
        with mock.patch.object(sessions, "SERVER_URL", ""), mock.patch.object(blackbox, "index_bbl", lambda b, **k: self.IDX), \
                mock.patch.object(blackbox, "segments", lambda b: segs), redirect_stdout(out):
            cli.main(list(args))
        return out.getvalue()

    def test_arms_lists_every_segment(self):
        text = self.cli("arms", "L.bbl")
        self.assertIn("2 arms, 2 segments without frames", text.splitlines()[0])
        rows = {int(l.split()[1]): l for l in text.splitlines() if l.startswith("  arm ") or l.startswith("  segment ")}
        self.assertEqual(sorted(rows), [1, 2, 3, 4])
        self.assertTrue(rows[2].startswith("  segment  2"), rows[2])
        self.assertIn("no frames: ValueError: read length must be non-negative", rows[2])
        self.assertIn("6 KB", rows[2])
        self.assertIn("no frames: empty", rows[4])
        self.assertIn("300 KB", rows[1])

    def test_arms_json_carries_the_sizes(self):
        """`arms --json` says each segment's size in bytes too: the agent reads the JSON, not the listing (#21)."""
        arms = json.loads(self.cli("arms", "L.bbl", "--json"))["arms"]
        self.assertEqual([a.get("size") for a in arms], [300_000, 6144, 280_000, 512])

    def test_sessions_says_what_a_segment_without_frames_is(self):
        from acroscope import sessions
        from acroscope.db import Db
        with tempfile.TemporaryDirectory() as tmp:
            db = Db(Path(tmp) / "t.db")
            db.ensure_session("2026-10-16-x")
            db.attach("2026-10-16-x", "L.bbl")
            with mock.patch.object(sessions, "_store", db):
                text = self.cli("sessions", "2026-10-16-x")
        row = next(l for l in text.splitlines() if l.strip().startswith("segment  2"))
        self.assertIn("no frames: ValueError", row)
        self.assertNotIn("motors off", row)
        self.assertNotIn("cut off", row)


class RatesTest(unittest.TestCase):
    """#22: the rate profile is in every arm's header; `rates_type` says how to read it. Anchors: the 10-09 OTTO
    log's Actual rates (centre 50, max 300 deg/s, the 'lazyfly' profile), the Air65's Actual 70/670, and
    Betaflight's classic default (rc_rate 1.0, super 0.7, expo 0 = 667 deg/s)."""

    def test_actual(self):
        r = blackbox.rates({"rates_type": 3, "rc_rates": [5, 5, 5], "rates": [30, 30, 30], "rc_expo": [80, 80, 80]})
        self.assertEqual(r, {"type": "actual", "max_dps": [300.0] * 3, "centre_dps": [50.0] * 3})
        r = blackbox.rates({"rates_type": 3, "rc_rates": [7, 7, 7], "rates": [67, 67, 67], "rc_expo": [0, 0, 0]})
        self.assertEqual((r["max_dps"][0], r["centre_dps"][0]), (670.0, 70.0))
        # a centre sensitivity above the max rate: the max is the centre (stickMovement clamps at 0)
        self.assertEqual(blackbox.rates({"rates_type": 3, "rc_rates": [80, 80, 80], "rates": [30, 30, 30], "rc_expo": [0, 0, 0]})["max_dps"], [800.0] * 3)

    def test_betaflight_raceflight_kiss_quick(self):
        bf = blackbox.rates({"rates_type": 0, "rc_rates": [100, 100, 100], "rates": [70, 70, 70], "rc_expo": [0, 0, 0]})
        self.assertEqual(bf["type"], "betaflight")
        self.assertAlmostEqual(bf["max_dps"][0], 666.7)
        self.assertEqual(bf["centre_dps"][0], 200.0)
        # expo lowers the centre, never the full-stick rate
        self.assertEqual(blackbox.rates({"rates_type": 0, "rc_rates": [100] * 3, "rates": [70] * 3, "rc_expo": [50] * 3})["centre_dps"][0], 100.0)
        rf = blackbox.rates({"rates_type": 1, "rc_rates": [80, 80, 80], "rates": [80, 80, 80], "rc_expo": [0, 0, 0]})
        self.assertEqual((rf["max_dps"][0], rf["centre_dps"][0]), (1440.0, 800.0))
        kiss = blackbox.rates({"rates_type": 2, "rc_rates": [100] * 3, "rates": [70] * 3, "rc_expo": [0] * 3})
        self.assertAlmostEqual(kiss["max_dps"][0], 666.7)
        quick = blackbox.rates({"rates_type": 4, "rc_rates": [100] * 3, "rates": [67] * 3, "rc_expo": [30] * 3})
        self.assertEqual((quick["max_dps"][0], quick["centre_dps"][0]), (670.0, 140.0))
        # the firmware clamps the setpoint at 1998 deg/s
        self.assertEqual(blackbox.rates({"rates_type": 0, "rc_rates": [250] * 3, "rates": [99] * 3, "rc_expo": [0] * 3})["max_dps"][0], 1998.0)

    def test_unknown_or_missing(self):
        self.assertIsNone(blackbox.rates({"rc_rates": [7, 7, 7], "rates": [67, 67, 67], "rc_expo": [0, 0, 0]}))   # decoded before #22
        self.assertIsNone(blackbox.rates({"rates_type": 9, "rc_rates": [7, 7, 7], "rates": [67, 67, 67], "rc_expo": [0, 0, 0]}))
        self.assertIsNone(blackbox.rates({"rates_type": 3, "rc_rates": 7, "rates": [67, 67, 67], "rc_expo": [0, 0, 0]}))
        self.assertIsNone(blackbox.rates({}))


if __name__ == "__main__":
    unittest.main()
