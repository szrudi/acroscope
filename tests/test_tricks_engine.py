"""The trick engine (acroscope/tricks) on scripted arms (tests/synthetic.py): each segment kind finds its window,
the shipped tricks judge it as the pre-#22 detectors did, precedence follows the file (priority on a window,
suppress across tricks), and the CLI shows it all. The real logs are the pins in test_realdata.py and the
examples `acroscope tricks check` runs."""
import io
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from acroscope import blackbox, sessions, tricks
from acroscope.tricks.library import Library

try:
    from tests.synthetic import axis, combine, make_arm, pulse, quat_from_lean
except ImportError:                                                           # run from inside tests/
    from synthetic import axis, combine, make_arm, pulse, quat_from_lean

LIB = tricks.library()


def titles(evs):
    return [(e["tag"], e["title"]) for e in evs]


def impact(at: float, g: float = 6.0, jolt: float = 900.0):
    """An accelerometer spike with a gyro jolt at `at`: the body of a crash."""
    return (lambda t: (0.0, 0.0, g) if at <= t < at + 0.05 else (0.0, 0.0, 1.0),
            lambda t: (jolt, 0.0, 0.0) if at <= t < at + 0.05 else (0.0, 0.0, 0.0))


class RotationTest(unittest.TestCase):
    def test_a_right_roll(self):
        arm = make_arm(6, gyro=axis(0, pulse(2.0, 2.72, 500)))                 # 500 dps for 0.72 s = 360 deg
        evs = tricks.detect(arm, LIB)
        self.assertEqual(titles(evs), [("roll", "right roll")])
        e = evs[0]
        self.assertEqual((e["trick"], e["arm_from"], e["arm_to"]), ("roll", 2.0, 2.72))
        self.assertEqual(e["why"], "roll +359 / pitch +0 / yaw +0 deg in 0.72 s, peak 500 dps, throttle 50-50 %")
        n = e["numbers"]
        self.assertEqual((n["rotation_deg"]["roll"], n["axis"], n["direction"]), (359, "roll", "right"))
        self.assertEqual(n["tilt_deg"]["max"], 180)                            # it went through inverted
        self.assertEqual(n["throttle_inverted_pct"]["mean"], 50)
        self.assertEqual(n["rates"]["max_dps"], [670.0] * 3)
        self.assertEqual(set(e), {"trick", "tag", "arm_from", "arm_to", "title", "why", "numbers"})

    def test_the_four_directions_and_yaw(self):
        self.assertEqual(titles(tricks.detect(make_arm(5, gyro=axis(0, pulse(2.0, 2.72, -500))), LIB)), [("roll", "left roll")])
        self.assertEqual(titles(tricks.detect(make_arm(5, gyro=axis(1, pulse(2.0, 2.72, -500))), LIB)), [("flip", "back flip")])
        self.assertEqual(titles(tricks.detect(make_arm(5, gyro=axis(1, pulse(2.0, 2.72, 500))), LIB)), [("flip", "forward flip")])
        with_yaw = make_arm(5, gyro=combine(axis(0, pulse(2.0, 2.72, 500)), axis(2, pulse(2.0, 2.72, 300))))       # yaw 216 > half of 360
        self.assertEqual(titles(tricks.detect(with_yaw, LIB)), [("roll", "right roll with yaw")])
        little_yaw = make_arm(5, gyro=combine(axis(0, pulse(2.0, 2.72, 500)), axis(2, pulse(2.0, 2.72, 150))))
        self.assertEqual(titles(tricks.detect(little_yaw, LIB)), [("roll", "right roll")])

    def test_less_than_a_flip_is_nothing(self):
        self.assertEqual(tricks.detect(make_arm(5, gyro=axis(0, pulse(2.0, 2.4, 500))), LIB), [])            # 200 deg
        self.assertEqual(tricks.detect(make_arm(5, gyro=axis(0, pulse(2.0, 2.2, 400))), LIB), [])            # 80 deg

    def test_a_tumble_is_nothing(self):
        self.assertEqual(tricks.detect(make_arm(5, gyro=axis(0, pulse(2.0, 2.3, 2000))), LIB), [])          # 600 deg at 2000 dps

    def test_a_pause_under_the_gap_joins_two_halves(self):
        """Two half rolls 0.2 s apart are one 400 deg window (gap 0.3); 0.5 s apart they are two 200 deg stretches."""
        joined = make_arm(6, gyro=combine(axis(0, pulse(2.0, 2.4, 500)), axis(0, pulse(2.6, 3.0, 500))))
        self.assertEqual(titles(tricks.detect(joined, LIB)), [("roll", "right roll")])
        self.assertEqual((tricks.detect(joined, LIB)[0]["arm_from"], tricks.detect(joined, LIB)[0]["arm_to"]), (2.0, 3.0))
        apart = make_arm(6, gyro=combine(axis(0, pulse(2.0, 2.4, 500)), axis(0, pulse(2.9, 3.3, 500))))
        self.assertEqual(tricks.detect(apart, LIB), [])

    def test_too_short_a_stretch_is_no_window(self):
        self.assertEqual(tricks.detect(make_arm(5, gyro=axis(0, pulse(2.0, 2.25, 1200))), LIB), [])         # 300 deg in 0.25 s: under min_dur

    def test_split_s_wins_the_window(self):
        """A half roll and a pull back at low throttle in one window: split-S, not a roll (priority), not a flip."""
        gyro = combine(axis(0, pulse(2.0, 2.4, 500)), axis(1, pulse(2.5, 2.8, -500)))      # roll 200, then pitch -150, gap 0.1 s
        arm = make_arm(6, gyro=gyro, throttle=lambda t: 20 if 2 <= t < 3 else 50)
        evs = tricks.detect(arm, LIB)
        self.assertEqual(titles(evs), [("split-s", "split-S")])
        self.assertEqual(evs[0]["why"], "roll +199 / pitch -150 / yaw +0 deg in 0.8 s, peak 500 dps, throttle 20-20 %")
        # with the throttle on it is no split-S, and a 200 deg roll is no roll: nothing
        self.assertEqual(tricks.detect(make_arm(6, gyro=gyro, throttle=50), LIB), [])
        ex = tricks.explain(make_arm(6, gyro=gyro, throttle=50), LIB)
        verdict = {t["id"]: t for t in ex["windows"][0]["tricks"]}
        self.assertEqual(verdict["split-s"]["failed"], "throttle_pct.min < split_s_throttle_max")
        self.assertEqual(verdict["roll"]["failed"], "abs(rotation_deg.roll) >= flip_min_deg")
        self.assertEqual(verdict["flip"]["failed"], "axis == 'pitch'")


class LoopTest(unittest.TestCase):
    def test_a_powerloop_suppresses_the_flip_inside_it(self):
        arm = make_arm(8, gyro=axis(1, pulse(2.0, 4.4, -150)))                 # -150 dps for 2.4 s = -360 deg
        evs = tricks.detect(arm, LIB)
        self.assertEqual(titles(evs), [("powerloop", "powerloop")])
        self.assertEqual(evs[0]["why"], "-360 deg pitch in 2.4 s, peak 150 dps, throttle inverted 50 %")
        self.assertEqual((evs[0]["arm_from"], evs[0]["arm_to"]), (2.0, 4.4))
        ex = tricks.explain(arm, LIB)
        flip = next(t for w in ex["windows"] if w["kind"] == "rotation" for t in w["tricks"] if t["id"] == "flip")
        self.assertEqual((flip["fired"], flip["suppressed_by"]), (True, "powerloop"))

    def test_a_fast_back_flip_is_a_flip(self):
        arm = make_arm(6, gyro=axis(1, pulse(2.0, 2.9, -400)))                 # -360 deg in 0.9 s: under the loop's 1.5 s
        self.assertEqual(titles(tricks.detect(arm, LIB)), [("flip", "back flip")])

    def test_slow_pitching_short_of_a_loop_is_nothing(self):
        self.assertEqual(tricks.detect(make_arm(6, gyro=axis(1, pulse(2.0, 4.0, -100))), LIB), [])         # -200 deg


class ImpactTest(unittest.TestCase):
    def test_impact_and_hard_landing(self):
        acc, raw = impact(10.0)
        evs = tricks.detect(make_arm(20, acc=acc, raw=raw), LIB)
        self.assertEqual(titles(evs), [("crash", "impact")])
        e = evs[0]
        self.assertEqual((e["trick"], e["arm_from"], e["arm_to"], e["numbers"]["arm_t"]), ("crash", 9.5, 11.0, 10.0))
        self.assertEqual(e["why"], "6.0 g with 900 dps gyro jolt at arm 10.00 s")
        acc, raw = impact(19.0)
        evs = tricks.detect(make_arm(20, acc=acc, raw=raw), LIB)
        self.assertEqual(titles(evs), [("crash", "hard landing (arm end)")])
        self.assertEqual(evs[0]["why"], "6.0 g with 900 dps gyro jolt at arm 19.00 s of 20.0")

    def test_a_spike_without_a_jolt_is_no_crash(self):
        acc, _ = impact(10.0)
        self.assertEqual(tricks.detect(make_arm(20, acc=acc), LIB), [])

    def test_a_crash_swallows_the_roll_around_it(self):
        acc, raw = impact(10.0)
        near = make_arm(20, gyro=axis(0, pulse(8.9, 9.62, 500)), acc=acc, raw=raw)     # the roll ends 0.38 s before the crash window
        self.assertEqual(titles(tricks.detect(near, LIB)), [("crash", "impact")])
        far = make_arm(20, gyro=axis(0, pulse(5.0, 5.72, 500)), acc=acc, raw=raw)
        self.assertEqual(titles(tricks.detect(far, LIB)), [("roll", "right roll"), ("crash", "impact")])


class ThrottleTest(unittest.TestCase):
    def test_a_dive(self):
        arm = make_arm(10, throttle=lambda t: 0 if 3 <= t < 6 else 50, quat0=quat_from_lean(60, 0))
        evs = tricks.detect(arm, LIB)
        self.assertEqual(titles(evs), [("dive", "dive")])
        self.assertEqual((evs[0]["arm_from"], evs[0]["arm_to"]), (3.0, 6.0))
        self.assertEqual(evs[0]["why"], "throttle cut for 3.0 s, tilt 60-60 deg")
        self.assertEqual(evs[0]["numbers"]["tilt_deg"]["mean"], 60)

    def test_a_blip_does_not_end_it_but_a_longer_rise_does(self):
        blip = make_arm(10, throttle=lambda t: 60 if 4.5 <= t < 4.6 else (0 if 3 <= t < 6 else 50), quat0=quat_from_lean(60, 0))
        self.assertEqual([(e["arm_from"], e["arm_to"]) for e in tricks.detect(blip, LIB)], [(3.0, 6.0)])
        rise = make_arm(10, throttle=lambda t: 60 if 4.5 <= t < 5.0 else (0 if 3 <= t < 6 else 50), quat0=quat_from_lean(60, 0))
        self.assertEqual([(e["arm_from"], e["arm_to"]) for e in tricks.detect(rise, LIB)], [(3.0, 4.5)])      # the 1 s after it is too short

    def test_a_level_cut_is_not_a_dive(self):
        self.assertEqual(tricks.detect(make_arm(10, throttle=lambda t: 0 if 3 <= t < 6 else 50), LIB), [])


class KickTest(unittest.TestCase):
    def test_a_yaw_kick_with_the_stick_centred(self):
        raw = lambda t: (0.0, 0.0, 700.0) if 5.0 <= t < 5.03 else (0.0, 0.0, 0.0)
        evs = tricks.detect(make_arm(10, raw=raw), LIB)
        self.assertEqual(titles(evs), [("gyro-kick", "gyro yaw kick")])
        self.assertEqual((evs[0]["arm_from"], evs[0]["arm_to"], evs[0]["why"]), (4.7, 5.7, "raw yaw 700 dps with the yaw stick centred"))
        self.assertEqual(tricks.detect(make_arm(10, raw=raw, sticks=lambda t: (0, 0, 50)), LIB), [])       # the pilot asked for it
        self.assertEqual(tricks.detect(make_arm(10, raw=lambda t: (500.0, 0.0, 700.0) if 5.0 <= t < 5.03 else (0.0, 0.0, 0.0)), LIB), [])   # not yaw alone


class MotorTest(unittest.TestCase):
    def arm(self, crash_at=None):
        acc, raw = impact(crash_at) if crash_at else (None, None)
        return make_arm(12, motors=lambda t: (1900,) * 4, erpm=lambda t: (5000, 5000 if t < 8 else 1000, 5000, 5000), acc=acc, raw=raw)

    def test_motor_loss(self):
        evs = tricks.detect(self.arm(), LIB)
        self.assertEqual(titles(evs), [("motor-loss", "motor 2 power loss")])
        e = evs[0]
        self.assertEqual((e["numbers"]["motor"], e["numbers"]["arm_t"], e["arm_from"], e["arm_to"]), (2, 8.0, 7.5, 9.5))
        self.assertEqual(e["why"], "motor 2 eRPM 5000 -> 1000 in 50 ms at full command, held 0.4 s, no impact")

    def test_a_crash_at_the_drop_is_the_crash(self):
        self.assertEqual(titles(tricks.detect(self.arm(crash_at=8.2), LIB)), [("crash", "impact")])
        self.assertEqual(titles(tricks.detect(self.arm(crash_at=9.0), LIB)), [("motor-loss", "motor 2 power loss"), ("crash", "impact")])

    def test_without_erpm_the_trick_is_skipped(self):
        arm = make_arm(12, motors=lambda t: (1900,) * 4)
        self.assertEqual(tricks.detect(arm, LIB), [])
        self.assertEqual(tricks.explain(arm, LIB)["skipped"], {"motor-loss": ["eRPM[0]"]})


class PrecedenceTest(unittest.TestCase):
    TEXT = '''
[params]
min_deg = 270
[segment.fast]
kind = "rotation"
[[trick]]
id = "a"
tag = "a"
segment = "fast"
when = ["abs(rotation_deg.roll) >= min_deg"]
title = "a"
priority = 10
[[trick]]
id = "b"
tag = "b"
segment = "fast"
when = ["abs(rotation_deg.roll) >= min_deg"]
title = "b"
priority = {b}
'''
    ROLL = make_arm(5, gyro=axis(0, pulse(2.0, 2.72, 500)))

    def lib(self, b: int, extra: str = "") -> Library:
        """The two tricks, with `extra` as a second file on top (how a pilot's overrides land)."""
        import tomllib
        lib = Library()
        lib.add(tomllib.loads(self.TEXT.replace("{b}", str(b))), "base")
        if extra:
            lib.add(tomllib.loads(extra), "extra")
        lib.validate()
        return lib

    def test_the_highest_priority_wins_a_window_and_ties_go_to_the_earlier_entry(self):
        self.assertEqual([e["trick"] for e in tricks.detect(self.ROLL, self.lib(20))], ["b"])
        self.assertEqual([e["trick"] for e in tricks.detect(self.ROLL, self.lib(10))], ["a"])
        ex = tricks.explain(self.ROLL, self.lib(20))
        self.assertEqual({t["id"]: t["lost_to"] for t in ex["windows"][0]["tricks"]}, {"a": "b", "b": None})

    def test_a_param_changes_the_outcome(self):
        self.assertEqual(tricks.detect(self.ROLL, self.lib(10, "[params]\nmin_deg = 400\n")), [])

    def test_a_disabled_trick_does_not_fire(self):
        self.assertEqual([e["trick"] for e in tricks.detect(self.ROLL, self.lib(20, '[[trick]]\nid = "b"\nenabled = false\n'))], ["a"])

    def test_suppress_by_margin_and_within(self):
        acc, raw = impact(4.0)                                                 # the roll 2.0-2.72, the crash window 3.5-5.0: 0.78 s apart
        arm = make_arm(10, gyro=axis(0, pulse(2.0, 2.72, 500)), acc=acc, raw=raw)
        crash = '''
[segment.hit]
kind = "impact"
[[trick]]
id = "c"
tag = "c"
segment = "hit"
title = "c"
suppress = [{ tags = ["a"], margin = %s }]
'''
        self.assertEqual([e["trick"] for e in tricks.detect(arm, self.lib(0, crash % "1.0"))], ["c"])
        self.assertEqual([e["trick"] for e in tricks.detect(arm, self.lib(0, crash % "0.5"))], ["a", "c"])
        kick = '''
[segment.k]
kind = "kick"
[[trick]]
id = "k"
tag = "k"
segment = "k"
title = "k"
[[trick]]
id = "c"
tag = "c"
segment = { kind = "impact" }
title = "c"
priority = 5
suppress = [{ tags = ["k"], within = %s }]
'''
        raw2 = lambda t: (900.0, 0.0, 0.0) if 4.0 <= t < 4.05 else ((0.0, 0.0, 700.0) if 4.3 <= t < 4.33 else (0.0, 0.0, 0.0))
        arm = make_arm(10, acc=acc, raw=raw2)                                  # the kick 0.3 s after the impact
        self.assertEqual([e["trick"] for e in tricks.detect(arm, self.lib(0, kick % "0.5"))], ["c"])
        self.assertEqual([e["trick"] for e in tricks.detect(arm, self.lib(0, kick % "0.2"))], ["c", "k"])


class LibraryCacheTest(unittest.TestCase):
    def test_an_edited_file_is_read_again(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "tricks.toml"
            f.write_text(PrecedenceTest.TEXT.replace("{b}", "20"))
            with mock.patch.object(tricks, "library_paths", lambda: [f]), mock.patch.object(tricks, "_cache", None):
                self.assertEqual(sorted(tricks.library().tricks), ["a", "b"])
                stamp = tricks.fingerprint()
                self.assertIs(tricks.library(), tricks.library())
                f.write_text(PrecedenceTest.TEXT.replace("{b}", "20") + '[[trick]]\nid = "b"\nenabled = false\n')
                os.utime(f, ns=(0, f.stat().st_mtime_ns + 10**9))
                self.assertFalse(tricks.library().tricks["b"].enabled)
                self.assertNotEqual(tricks.fingerprint(), stamp)


class CliTest(unittest.TestCase):
    """`acroscope tricks` and `events` on a scripted arm, the store local and the arm loader stubbed."""

    def cli(self, *args) -> str:
        acc, raw = impact(10.0)
        arm = make_arm(20, gyro=axis(0, pulse(5.0, 5.72, 500)), acc=acc, raw=raw)
        out = io.StringIO()
        with mock.patch.object(sessions, "SERVER_URL", ""), mock.patch.object(blackbox, "load_arm", lambda b, n: arm), redirect_stdout(out):
            from acroscope import cli
            cli.main(list(args))
        return out.getvalue()

    def test_events_name_the_trick(self):
        import json
        rows = json.loads(self.cli("events", "--arm", "SYNTH.bbl:1", "--json"))
        self.assertEqual([(r["trick"], r["tag"], r["title"]) for r in rows], [("roll", "roll", "right roll"), ("crash", "crash", "impact")])
        text = self.cli("events", "--arm", "SYNTH.bbl:1")
        self.assertIn("right roll", text)
        self.assertIn("6.0 g with 900 dps", text)

    def test_list_show_features(self):
        text = self.cli("tricks")
        self.assertIn("tricks.toml", text.splitlines()[0])
        self.assertRegex(text, r"motor-loss\s+motor-loss\s+motor-drop\s+motor_drop")
        self.assertIn("flip_min_deg=270", text)
        show = self.cli("tricks", "show", "crash")
        self.assertIn("suppress  flip, roll, split-s: windows within 1.0 s", show)
        self.assertIn("suppress  motor-loss: instants within 0.5 s", show)
        self.assertIn("example   BTFL_BLACKBOX_LOG_AIR65_F_20261007_212530_BETAFPVG473_V2.bbl arm 18 at 64.7", show)
        feats = self.cli("tricks", "features", "impact")
        self.assertIn("impact_g", feats)
        self.assertIn("opt.jolt_window", feats)
        self.assertNotIn("erpm_before", feats)

    def test_explain(self):
        text = self.cli("tricks", "explain", "SYNTH.bbl:1", "4", "7")
        self.assertIn("fast-rotation 5.00-5.72", text)
        self.assertRegex(text, r"roll\s+fired")
        self.assertRegex(text, r"flip\s+failed: axis == 'pitch'")
        self.assertNotIn("impact 9.50", text)                                 # outside 4-7

    def test_check_runs_the_examples(self):
        lib = Library.from_text(PrecedenceTest.TEXT.replace("{b}", "20") + '''
[[trick]]
id = "a"
examples = [{ log = "SYNTH.bbl", arm = 1, at = 5.3 }, { log = "SYNTH.bbl", arm = 1, from = 12, to = 14, absent = true }]
[[trick]]
id = "b"
examples = [{ log = "SYNTH.bbl", arm = 1, at = 5.3 }, { log = "SYNTH.bbl", arm = 1, at = 15 }, { log = "gone.bbl", arm = 1, at = 1 }]
''')
        acc, raw = impact(10.0)
        arm = make_arm(20, gyro=axis(0, pulse(5.0, 5.72, 500)), acc=acc, raw=raw)

        def load(b, n):
            if b == "gone.bbl":
                raise FileNotFoundError(b)
            return arm
        with mock.patch.object(blackbox, "load_arm", load):
            res = tricks.check(lib)
        self.assertEqual([(r["trick"], r["status"]) for r in res["results"]],
                         [("a", "FAILED"), ("a", "ok"), ("b", "ok"), ("b", "FAILED"), ("b", "unchecked")])     # b wins the window from a
        self.assertEqual((res["passed"], res["failed"], res["unchecked"]), (2, 2, 1))
        out = io.StringIO()
        with mock.patch.object(sessions, "SERVER_URL", ""), mock.patch.object(blackbox, "load_arm", load), \
                mock.patch.object(tricks, "library", lambda: lib), redirect_stdout(out), self.assertRaises(SystemExit) as cm:
            from acroscope import cli
            cli.main(["tricks", "check"])
        self.assertEqual(cm.exception.code, 1)
        self.assertIn("2 passed, 2 failed, 1 unchecked", out.getvalue())


if __name__ == "__main__":
    unittest.main()
