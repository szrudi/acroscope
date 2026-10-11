"""The hand-verified numbers of the 10-07 schammer session (video-blackbox-index.md) against the real log and clip.

Two ways to reach the data: the files under the local data dir (a machine with the data), or the server named by
ACROSCOPE_URL / ~/.config/acroscope/url, through the CLI proxy (joan: the clips and logs live on CT 109 only).
Skipped when neither answers, so the suite still runs anywhere; with the data these guard the detectors, the
metrics, the OSD reader and the aligner against regressions."""
import json
import unittest

from acroscope import blackbox, events, metrics, osd, sessions
from acroscope.config import BLACKBOX_DIR, SERVER_URL, VIDEOS_DIR

BBL = "BTFL_BLACKBOX_LOG_AIR65_F_20261007_212530_BETAFPVG473_V2.bbl"
CLIP = VIDEOS_DIR / "2026-10-07-schammer" / "2026-10-07_008.mp4"
LOCAL = (BLACKBOX_DIR / BBL).is_file() and CLIP.is_file()


def server_has_data() -> bool:
    if LOCAL or not SERVER_URL:
        return False
    try:
        r = sessions.store().cli(["arms", BBL, "--json"])
        return r["code"] == 0
    except Exception:  # noqa: BLE001
        return False


REMOTE = server_has_data()


def cli_json(*args):
    r = sessions.store().cli([str(a) for a in args])
    assert r["code"] == 0, r["stderr"]
    return json.loads(r["stdout"])


@unittest.skipUnless(LOCAL or REMOTE, "10-07 schammer data neither mounted nor on a reachable server")
class RealDataTest(unittest.TestCase):
    def events_of(self, arm: int) -> list[dict]:
        if LOCAL:
            return events.detect(blackbox.load_arm(BBL, arm))
        return cli_json("events", "--arm", f"{BBL}:{arm}", "--json")

    def test_arm20_motor_loss_and_fall(self):
        ev = self.events_of(20)
        loss = [e for e in ev if e["tag"] == "motor-loss"]
        self.assertEqual(len(loss), 1)
        self.assertEqual(loss[0]["numbers"]["motor"], 1)
        self.assertAlmostEqual(loss[0]["numbers"]["arm_t"], 70.4, delta=0.2)       # index: 70.37 s
        crash = [e for e in ev if e["tag"] == "crash"]
        self.assertAlmostEqual(crash[-1]["numbers"]["arm_t"], 71.4, delta=0.2)     # ground at 71.4 s

    def test_arm13_powerloop(self):
        if LOCAL:
            s = metrics.summary(blackbox.load_arm(BBL, 13), 26.8, 31.8)
        else:
            s = cli_json("metrics", "--arm", f"{BBL}:13", "26.8", "31.8", "--json")["summary"]
        self.assertEqual(s["rotation_deg"]["pitch"], -385)
        self.assertEqual(s["peak_rate_dps"]["pitch"], 169)
        loops = [e for e in self.events_of(13) if e["tag"] == "powerloop"]
        self.assertEqual(len(loops), 2)                                            # 27.0-31.7 and 39.8-43.9
        self.assertAlmostEqual(loops[0]["arm_from"], 27.0, delta=0.3)

    def test_arm1_roll_flips(self):
        segs = [e for e in self.events_of(1) if e["tag"] in ("roll", "flip")]
        found = {e["title"]: e["arm_from"] + 86.5 for e in segs}                     # offset 86.5 for clip 003
        self.assertAlmostEqual(found["right roll"], 92.7, delta=1)                  # index: 1:32.7
        self.assertAlmostEqual(found["left roll"], 94.5, delta=1)                   # index: 1:34.5

    def test_osd_arm_runs_clip_008(self):
        """The three arm runs of clip 008 (arms 14, 15, 17 by hand), and nothing else that the aligner would take:
        a stray run read off the STATS screen while disarmed may survive, but it is ghost-sized and matches no arm."""
        if LOCAL:
            runs = osd.video_arms(osd.read_timers(CLIP))
            idx = blackbox.index_bbl(BBL)
        else:
            runs = osd.video_arms(cli_json("osd", "10-07", "008", "--json"))
            idx = cli_json("arms", BBL, "--json")
        real = [r for r in runs if r["length"] >= 2]
        for got, want in zip([r["start"] for r in real], [4.75, 20.25, 64.25]):     # ±0.1: the bias is 0.25, not 0.2, since #4
            self.assertAlmostEqual(got, want, delta=0.1)
        self.assertEqual(len(real), 3)
        self.assertEqual([r["timer_max"] for r in real], [12, 7, 24])
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

    @unittest.skipUnless(REMOTE, "automatch through the server")
    def test_automatch_clip_008_derives_nothing_from_a_line_no_clock_explains(self):
        """#9: the 008 runs give boot 7 a line at -9 % (no clock does that); arm 16 was derived from it. The
        three read arms stay, nothing is derived."""
        res = cli_json("automatch", "10-07", "008", "--json")
        self.assertEqual(sorted(m["arm"] for m in res["matches"]), [14, 15, 17])
        self.assertTrue(all(f.get("rejected") for f in res["boot_fits"].values() if abs(f["a"] - 1) > osd.MAX_CLOCK))


@unittest.skipUnless(REMOTE, "the 10-10 clips live on the server only")
class TenTenTest(unittest.TestCase):
    """automatch against the frame-by-frame truths of szrudi/acroscope#4 on 2026-10-10 001: arm 1 at 7.95 (yaw snap
    and nose-down hit), arm 9 at 150.35 (a right roll); and the clock difference the boot fit measures."""
    def test_clip_001_offsets_and_fit(self):
        r = sessions.store().cli(["automatch", "2026-10-10", "001", "--json"])
        self.assertEqual(r["code"], 0, r["stderr"])
        res = json.loads(r["stdout"])
        by = {m["arm"]: m["offset"] for m in res["matches"]}
        self.assertAlmostEqual(by[1], 7.95, delta=0.15)
        self.assertAlmostEqual(by[9], 150.35, delta=0.15)
        self.assertTrue({3, 7, 8, 14} <= set(by), sorted(by))                   # the short arms, derived from the fit
        fit = list(res["boot_fits"].values())[0]
        self.assertAlmostEqual(fit["a"], 0.997, delta=0.001)


if __name__ == "__main__":
    unittest.main()
