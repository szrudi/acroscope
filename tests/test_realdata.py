"""The hand-verified numbers of the 10-07 schammer session (video-blackbox-index.md) against the real log and clip.
Skipped when the data dir is not mounted, so the suite still runs anywhere; on joan these guard the detectors,
the metrics and the OSD reader against regressions."""
import unittest

from acroscope import blackbox, events, metrics, osd
from acroscope.config import BLACKBOX_DIR, VIDEOS_DIR

BBL = "BTFL_BLACKBOX_LOG_AIR65_F_20261007_212530_BETAFPVG473_V2.bbl"
CLIP = VIDEOS_DIR / "2026-10-07-schammer" / "2026-10-07_008.mp4"


@unittest.skipUnless((BLACKBOX_DIR / BBL).is_file() and CLIP.is_file(), "10-07 schammer data not mounted")
class RealDataTest(unittest.TestCase):
    def test_arm20_motor_loss_and_fall(self):
        ev = events.detect(blackbox.load_arm(BBL, 20))
        loss = [e for e in ev if e["tag"] == "motor-loss"]
        self.assertEqual(len(loss), 1)
        self.assertEqual(loss[0]["numbers"]["motor"], 1)
        self.assertAlmostEqual(loss[0]["numbers"]["arm_t"], 70.4, delta=0.2)       # index: 70.37 s
        crash = [e for e in ev if e["tag"] == "crash"]
        self.assertAlmostEqual(crash[-1]["numbers"]["arm_t"], 71.4, delta=0.2)     # ground at 71.4 s

    def test_arm13_powerloop(self):
        arm = blackbox.load_arm(BBL, 13)
        s = metrics.summary(arm, 26.8, 31.8)
        self.assertEqual(s["rotation_deg"]["pitch"], -385)
        self.assertEqual(s["peak_rate_dps"]["pitch"], 169)
        loops = [e for e in events.detect(arm) if e["tag"] == "powerloop"]
        self.assertEqual(len(loops), 2)                                            # 27.0-31.7 and 39.8-43.9
        self.assertAlmostEqual(loops[0]["arm_from"], 27.0, delta=0.3)

    def test_arm1_roll_flips(self):
        segs = [e for e in events.detect(blackbox.load_arm(BBL, 1)) if e["tag"] in ("roll", "flip")]
        found = {e["title"]: e["arm_from"] + 86.5 for e in segs}                     # offset 86.5 for clip 003
        self.assertAlmostEqual(found["right roll"], 92.7, delta=1)                  # index: 1:32.7
        self.assertAlmostEqual(found["left roll"], 94.5, delta=1)                   # index: 1:34.5

    def test_osd_arm_runs_clip_008(self):
        """The three arm runs of clip 008 (arms 14, 15, 17 by hand), and nothing else that the aligner would take:
        a stray run read off the STATS screen while disarmed may survive, but it is ghost-sized and matches no arm."""
        runs = osd.video_arms(osd.read_timers(CLIP))
        real = [r for r in runs if r["length"] >= 2]
        self.assertEqual([round(r["start"], 1) for r in real], [4.7, 20.2, 64.2])
        self.assertEqual([r["timer_max"] for r in real], [12, 7, 24])
        idx = blackbox.index_bbl(BBL)
        boot_of = {i: k for k, bt in enumerate(idx["boots"]) for i in bt["arms"]}
        cum, logs = {}, []
        for a in idx["arms"]:
            if a["frames"] and a["motors_spun"]:
                k = boot_of[a["index"]]
                logs.append({"index": a["index"], "length": a["length"], "cum_before": round(cum.get(k, 0.0), 1),
                             "boot": k, "uptime_start": a["uptime_start"]})
                cum[k] = cum.get(k, 0.0) + a["length"]
        pairs = osd.consistent_pairs(runs, logs, osd.align(runs, logs))
        # arm 16 (2.1 s, 30 s further on in the boot than the stray run) must not be taken
        self.assertEqual(sorted(logs[li]["index"] for _, li in pairs), [14, 15, 17])


if __name__ == "__main__":
    unittest.main()
