"""The aligner's pure parts on made-up runs and arms (no video, no log)."""
import unittest
from pathlib import Path

from acroscope import osd


def logs_of(boot_arms):
    """[(index, uptime_start, length)] -> log arm dicts in one boot, with cumulative totals."""
    out, cum = [], 0.0
    for i, u, l in boot_arms:
        out.append({"index": i, "length": l, "cum_before": round(cum, 1), "boot": 0, "uptime_start": u})
        cum += l
    return out


class AlignerTest(unittest.TestCase):
    def test_fit_measures_the_clock_difference(self):
        # the clip's clock runs 0.3 % slow against the FC: video = 0.997 * uptime + 10
        logs = logs_of([(1, 20, 10), (2, 50, 5), (3, 100, 20), (4, 200, 8), (5, 260, 6)])
        runs = [{"start": round(0.997 * l["uptime_start"] + 10, 2), "length": l["length"], "readings": 10} for l in logs]
        pairs = [(i, i) for i in range(5)]
        f = osd.fit_boots(runs, logs, pairs)[0]
        self.assertAlmostEqual(f["a"], 0.997, places=4)
        self.assertAlmostEqual(f["b"], 10.0, places=1)
        self.assertEqual(f["n"], 5)
        # two pairs: no line, the median offset with a = 1
        f = osd.fit_boots(runs[:2], logs[:2], [(0, 0), (1, 1)])[0]
        self.assertEqual(f["a"], 1.0)

    def test_short_arms_derived_inside_the_clip(self):
        logs = logs_of([(1, 20, 10), (2, 50, 1.5), (3, 100, 20), (4, 200, 8), (5, 260, 6), (6, 400, 5)])
        runs = [{"start": round(0.997 * logs[i]["uptime_start"] + 10, 2), "length": logs[i]["length"], "readings": 10} for i in (0, 2, 3, 4)]
        pairs = [(0, 0), (1, 2), (2, 3), (3, 4)]
        fits = osd.fit_boots(runs, logs, pairs)
        derived = osd.derive_unread(logs, pairs, fits, duration=300.0)
        self.assertEqual([logs[li]["index"] for li, _ in derived], [2])       # arm 6 lies past the clip's end
        self.assertAlmostEqual(derived[0][1], 0.997 * 50 + 10, delta=0.05)

    def test_place_by_boot_uses_the_line(self):
        logs = logs_of([(1, 20, 10), (2, 50, 5), (3, 100, 20), (4, 200, 8), (5, 260, 30)])
        runs = [{"start": round(0.997 * l["uptime_start"] + 10, 2), "length": l["length"], "readings": 10} for l in logs]
        runs[4]["length"] = 12.0                                                # the last arm's end was unreadable
        pairs = osd.place_by_boot(runs, logs, [(0, 0), (1, 1), (2, 2), (3, 3)])
        self.assertIn((4, 4), pairs)

    def test_fit_beyond_one_percent_is_rejected(self):
        # three runs of one boot that no clock explains (-9 %): the line is not used to place or derive arms (#9)
        logs = logs_of([(1, 20, 10), (2, 50, 2), (3, 100, 20), (4, 200, 8)])
        runs = [{"start": round(0.91 * logs[i]["uptime_start"] + 10, 2), "length": logs[i]["length"], "readings": 10} for i in (0, 2, 3)]
        pairs = [(0, 0), (1, 2), (2, 3)]
        fits = osd.fit_boots(runs, logs, pairs)
        self.assertTrue(fits[0]["rejected"])
        self.assertEqual(osd.derive_unread(logs, pairs, fits, duration=300.0), [])
        # two pairs whose offsets disagree by more than the timer jitter: no median offset to trust either
        two = [{"start": 30.0, "length": 10, "readings": 10}, {"start": 50.0, "length": 20, "readings": 10}]
        self.assertTrue(osd.fit_boots(two, logs, [(0, 0), (1, 2)])[0]["rejected"])
        # a real 0.3 % passes
        ok = [{"start": round(0.997 * l["uptime_start"] + 10, 2), "length": l["length"], "readings": 10} for l in logs]
        self.assertNotIn("rejected", osd.fit_boots(ok, logs, [(i, i) for i in range(4)])[0])

    def test_in_order_drops_a_pair_out_of_time_order(self):
        # arms 61-64 in order and arm 53 between 62 and 63: a clip's arms come in log order (#9, 10-02 002)
        logs = [{"index": i, "length": 10} for i in (53, 61, 62, 63, 64)]
        runs = [{"start": t, "length": 10, "readings": 8} for t in (10, 30, 50, 70, 90)]
        pairs = [(0, 1), (1, 2), (2, 0), (3, 3), (4, 4)]
        self.assertEqual(osd.in_order(runs, logs, pairs), [(0, 1), (1, 2), (3, 3), (4, 4)])
        # of two that cross, the one with more readings stays
        runs[2]["readings"] = 50
        self.assertEqual(osd.in_order(runs, logs, [(1, 2), (2, 0)]), [(2, 0)])

    def test_arm_window_from_the_other_clips(self):
        # clips are chronological: a clip's arms come after the previous clip's and before the next one's
        others = [("2026-10-02_001", 58), ("2026-10-02_002", 61), ("2026-10-02_002", 74), ("2026-10-02_003", 75),
                  ("2026-10-03_001", 80)]
        self.assertEqual(osd.arm_window("2026-10-02_009", others), (75, 80))
        self.assertEqual(osd.arm_window("2026-10-01_001", others), (None, 58))
        self.assertEqual(osd.arm_window("2026-10-04_001", others), (80, None))
        self.assertEqual(osd.arm_window("x", []), (None, None))
        # a clip without a readable name (its id) has no place in time, and neither has a match in one
        self.assertEqual(osd.arm_window("c1a2b3.mp4", others), (None, None))
        self.assertEqual(osd.arm_window("2026-10-02_009", others + [("c1a2b3.mp4", 77)]), (75, 80))

    def test_stats_values(self):
        # (digit, score) per cell; the % glyph and a frame without the STATS screen score low (survey of #9)
        self.assertEqual(osd.stats_value([("1", .8), ("0", .87), ("0", .88)], "pct"), 100)
        self.assertEqual(osd.stats_value([("5", .84), ("5", .84), ("2", .29)], "pct"), 55)
        self.assertEqual(osd.stats_value([("6", .93), ("6", .93), ("2", .47)], "pct"), 66)
        self.assertEqual(osd.stats_value([("4", .9), ("2", .4), ("2", .3)], "pct"), 4)
        self.assertIsNone(osd.stats_value([("4", .32), (None, 0), (None, 0)], "pct"))
        self.assertEqual(osd.stats_value([("0", .8), ("2", .91), ("1", .73), ("9", .79)], "mmss"), 139)
        self.assertIsNone(osd.stats_value([("3", .26), ("2", .29), ("2", .28), (None, 0)], "mmss"))
        self.assertIsNone(osd.stats_value([("0", .8), ("2", .91), ("7", .73), ("9", .79)], "mmss"))     # 02:79



class DetectLayoutRateTest(unittest.TestCase):
    """Review on #30: detect_layout scored a layout by readings that count on at 1 s/s (a step of 2 +- 0.6 s at
    0.5 fps); a clock running 2.2x the clip (2026-10-14 007) steps about 4.4 s, so no layout scored and 007's
    layout won 2 to 0 by chance. When nothing counts at 1 s/s, other rates are tried."""

    def detect(self, readings):
        from unittest import mock
        from acroscope import osd
        frames = lambda path, fps, layout=None: ((i * 2.0, layout) for i in range(len(readings["A"])))
        seq = {n: iter(r) for n, r in readings.items()}
        with mock.patch.object(osd, "layouts", lambda: {"A": {}, "B": {}}), mock.patch.object(osd, "_load_templates", lambda n: {"x": 1}), \
                mock.patch.object(osd, "_frames", frames), mock.patch.object(osd, "read_frame", lambda buf, tpl, name: next(seq[name])):
            return osd.detect_layout(Path("clip.mp4"))

    def test_a_fast_clock_finds_its_layout(self):
        fast = [(round(i * 4.4, 1), round(i * 4.4, 1), 1.0) for i in range(30)]                  # 2.2x: 4.4 s a sample
        junk = [(7.0, 3.0, 1.0)] * 30                                                            # static text read as digits
        self.assertEqual(self.detect({"A": junk, "B": fast}), "B")

    def test_counting_at_one_rate_still_wins(self):
        on_time = [(i * 2.0, i * 2.0, 1.0) for i in range(30)]
        fast = [(round(i * 4.4, 1), round(i * 4.4, 1), 1.0) for i in range(30)]
        self.assertEqual(self.detect({"A": on_time, "B": fast}), "A")                          # 1 s/s first


if __name__ == "__main__":
    unittest.main()
