"""The aligner's pure parts on made-up runs and arms (no video, no log)."""
import unittest

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


if __name__ == "__main__":
    unittest.main()
