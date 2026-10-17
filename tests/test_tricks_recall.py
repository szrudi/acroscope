"""The tricks that came with the recall work (#31, #29, #20, #22 step 2) on scripted arms: orbits from the heading
in the world, sequences (a split-S as a half roll then a pull-through, a figure 8 as a turn each way), attempts,
barrel rolls, tricks that ended with contact, the tumble and ground rules, and `tricks eval` against moments."""
import math
import tomllib
import unittest
from unittest import mock

from acroscope import tricks
from acroscope.tricks import expr
from acroscope.tricks.library import SHIPPED, Library, LibraryError

try:
    from tests.synthetic import axis, combine, make_arm, pulse, quat_from_lean
except ImportError:
    from synthetic import axis, combine, make_arm, pulse, quat_from_lean

LIB = tricks.library()


def titles(arm, lib=LIB):
    return [e["title"] for e in tricks.detect(arm, lib)]


def events(arm, lib=LIB):
    return [(e["tag"], e["title"]) for e in tricks.detect(arm, lib)]


def banked_turn(t0: float, t1: float, rate_dps: float, bank_deg: float = 40.0):
    """A turn about the world's vertical at `rate_dps` (+ = anticlockwise seen from above) with the quad banked
    right by `bank_deg`: in body axes that is yaw rate * cos(bank) and pitch rate * sin(bank)."""
    b = math.radians(bank_deg)
    return lambda t: (0.0, rate_dps * math.sin(b), rate_dps * math.cos(b)) if t0 <= t < t1 else (0.0, 0.0, 0.0)


def impact(at: float, g: float = 6.0, jolt: float = 900.0):
    return (lambda t: (0.0, 0.0, g) if at <= t < at + 0.05 else (0.0, 0.0, 1.0),
            lambda t: (jolt, 0.0, 0.0) if at <= t < at + 0.05 else (0.0, 0.0, 0.0))


class OrbitTest(unittest.TestCase):
    def test_a_banked_turn_is_an_orbit_named_by_the_side_it_slides_to(self):
        """Rudi names an orbit by the way the quad slides: anticlockwise seen from above = sliding right (#29). A
        60 dps turn for 8 s is 480 deg, 1.3 laps; the bank stays 40 deg because the turn is about the vertical."""
        arm = make_arm(14, gyro=banked_turn(2.0, 10.0, 60), quat0=quat_from_lean(0, 40))
        ev = tricks.detect(arm, LIB)
        self.assertEqual([e["title"] for e in ev], ["orbit sliding right, 1.3 laps"])
        n = ev[0]["numbers"]
        self.assertAlmostEqual(n["turn_deg"], 480, delta=15)
        self.assertEqual((n["side"], n["laps"]), ("right", 1.3))
        self.assertAlmostEqual(n["tilt_deg"]["mean"], 40, delta=2)
        self.assertRegex(ev[0]["why"], r"heading \+4\d\d deg in 8\.\d+ s \(\+\d\d dps, \+ = anticlockwise from above\), bank 40 deg, throttle 50 % against about 35 % level")
        left = make_arm(14, gyro=banked_turn(2.0, 10.0, -60, bank_deg=-40), quat0=quat_from_lean(0, -40))
        self.assertEqual(titles(left), ["orbit sliding left, 1.3 laps"])

    def test_a_turn_short_of_a_lap_or_too_brief_is_no_orbit(self):
        self.assertEqual(titles(make_arm(10, gyro=banked_turn(2.0, 6.0, 60), quat0=quat_from_lean(0, 40))), [])      # 240 deg
        self.assertEqual(titles(make_arm(10, gyro=banked_turn(2.0, 4.5, 150), quat0=quat_from_lean(0, 40))), [])     # 375 deg in 2.5 s
        beginner = Library()
        beginner.add(tomllib.loads(SHIPPED.read_text()), "shipped")
        beginner.add({"skill": "beginner"}, "mine")
        beginner.validate()
        self.assertEqual(titles(make_arm(10, gyro=banked_turn(2.0, 6.0, 60), quat0=quat_from_lean(0, 40)), beginner), ["orbit sliding right, 0.7 laps"])

    def test_a_flat_turn_is_an_orbit_too(self):
        """Indoors Rudi orbits the lamp at 7-12 deg of bank (10-15, 10-16): the heading, not the bank, is the signal."""
        self.assertEqual(titles(make_arm(14, gyro=axis(2, pulse(2.0, 10.0, 50)))), ["orbit sliding right, 1.1 laps"])

    def test_a_figure_8_is_a_turn_each_way(self):
        gyro = combine(banked_turn(2.0, 6.0, 60), banked_turn(6.5, 10.5, -60))                         # 240 deg right, 240 deg left
        ev = tricks.detect(make_arm(14, gyro=gyro, quat0=quat_from_lean(0, 40)), LIB)
        self.assertEqual([(e["tag"], e["title"]) for e in ev], [("orbit", "figure 8: right then left")])
        self.assertAlmostEqual(ev[0]["arm_from"], 2.0, delta=0.35)
        self.assertAlmostEqual(ev[0]["arm_to"], 10.5, delta=0.35)
        self.assertRegex(ev[0]["why"], r"turned \+2\d\d deg then -2\d\d in 8\.\d+ s")
        self.assertEqual(len(ev[0]["numbers"]["phases"]), 2)
        # four alternations are two figure 8s, not three: a turn that ended one does not start the next
        gyro = combine(banked_turn(2.0, 6.0, 60), banked_turn(6.5, 10.5, -60), banked_turn(11.0, 15.0, 60), banked_turn(15.5, 19.5, -60))
        self.assertEqual(titles(make_arm(22, gyro=gyro, quat0=quat_from_lean(0, 40))), ["figure 8: right then left", "figure 8: right then left"])
        # a lap and a half one way then a nudge back is an orbit, not a figure 8
        gyro = combine(banked_turn(2.0, 11.0, 60), banked_turn(11.5, 14.5, -60))
        self.assertEqual(titles(make_arm(18, gyro=gyro, quat0=quat_from_lean(0, 40))), ["orbit sliding right, 1.5 laps"])


HALF_ROLL_THEN_PULL = combine(axis(0, pulse(2.0, 2.4, 450)), axis(1, pulse(2.75, 3.75, -120)))   # 180 deg over, then 120 deg of pull


class SequenceTest(unittest.TestCase):

    def test_a_split_s_is_a_half_roll_then_a_pull_through(self):
        ev = tricks.detect(make_arm(8, gyro=HALF_ROLL_THEN_PULL, throttle=60), LIB)
        self.assertEqual([(e["tag"], e["title"], e["trick"]) for e in ev], [("split-s", "split-S", "split-s")])
        e = ev[0]
        self.assertEqual((e["arm_from"], e["arm_to"]), (2.0, 3.75))
        self.assertRegex(e["why"], r"half roll \+1[78]\d deg over to 1[78]\d deg, pulled through -1[12]\d deg of pitch out to 6\d deg, in 1\.75 s; throttle 60-60 %")
        self.assertEqual([n["arm_from"] for n in e["numbers"]["phases"]], [2.0, 2.75])
        # the half roll alone is a roll attempt; the pull alone is nothing
        self.assertEqual(titles(make_arm(8, gyro=axis(0, pulse(2.0, 2.4, 450)))), ["right roll attempt, 179 deg"])
        self.assertEqual(titles(make_arm(8, gyro=axis(1, pulse(2.75, 3.75, -120)))), [])

    def test_the_phases_must_follow_in_time(self):
        late = combine(axis(0, pulse(2.0, 2.4, 450)), axis(1, pulse(4.0, 5.0, -120)))                 # the pull 1.6 s after the roll ends
        self.assertEqual(titles(make_arm(8, gyro=late)), ["right roll attempt, 179 deg"])
        short = combine(axis(0, pulse(2.0, 2.4, 450)), axis(1, pulse(2.75, 3.05, -120)))              # only 36 deg of pull
        self.assertEqual(titles(make_arm(8, gyro=short)), ["right roll attempt, 179 deg"])

    def test_explain_lists_the_chain(self):
        ex = tricks.explain(make_arm(8, gyro=HALF_ROLL_THEN_PULL), LIB)
        seq = [w for w in ex["windows"] if w["kind"] == "phases"]
        self.assertEqual([(w["segment"], w["arm_from"], w["arm_to"]) for w in seq], [("split-s:phases", 2.0, 3.75)])
        attempt = next(t for w in ex["windows"] if w["kind"] == "rotation" for t in w["tricks"] if t["id"] == "roll-attempt")
        self.assertEqual((attempt["fired"], attempt["suppressed_by"]), (True, "split-s"))


class RotationRulesTest(unittest.TestCase):
    def test_attempts(self):
        self.assertEqual(events(make_arm(5, gyro=axis(0, pulse(2.0, 2.4, 500)))), [("roll", "right roll attempt, 199 deg")])
        self.assertEqual(events(make_arm(5, gyro=axis(1, pulse(2.0, 2.4, -500)))), [("flip", "back flip attempt, 199 deg")])
        self.assertEqual(titles(make_arm(5, gyro=axis(0, pulse(2.0, 3.33, 150)))), [])                 # 200 deg at 150 dps: a steep bank
        self.assertEqual(titles(make_arm(5, gyro=axis(0, pulse(2.0, 2.2, 500)))), [])                  # 100 deg: under attempt_min_deg

    def test_the_rotation_must_hold_its_direction(self):
        """A roll of 300 deg that comes 100 back within the window is a wobble, not a roll nor an attempt."""
        gyro = combine(axis(0, pulse(2.0, 2.6, 500)), axis(0, pulse(2.6, 2.8, -500)))
        self.assertEqual(titles(make_arm(5, gyro=gyro)), [])
        ex = tricks.explain(make_arm(5, gyro=gyro), LIB)
        roll = next(t for w in ex["windows"] for t in w["tricks"] if t["id"] == "roll")
        self.assertEqual(roll["failed"], "rotation_deg.roll * rotation_peak_deg.roll > 0 and abs(rotation_deg.roll) >= held_share * abs(rotation_peak_deg.roll)")

    def test_a_barrel_roll(self):
        barrel = make_arm(5, gyro=combine(axis(0, pulse(2.0, 2.72, 500)), axis(2, pulse(2.0, 2.72, 200))))       # roll 360 with yaw 144
        self.assertEqual(events(barrel), [("barrel-roll", "right barrel roll")])
        little_yaw = make_arm(5, gyro=combine(axis(0, pulse(2.0, 2.72, 500)), axis(2, pulse(2.0, 2.72, 100))))   # yaw 72: a roll
        self.assertEqual(events(little_yaw), [("roll", "right roll")])
        self.assertEqual(events(make_arm(5, gyro=combine(axis(1, pulse(2.0, 2.72, -500)), axis(2, pulse(2.0, 2.72, 300))))), [("flip", "back flip with yaw")])

    def test_a_trick_that_touched_something_mid_flight_is_still_the_trick(self):
        """#20: a roll that ends on the floor and flies on is a roll, titled so, next to the impact."""
        acc, raw = impact(2.55)
        arm = make_arm(20, gyro=axis(0, pulse(2.0, 2.72, 500)), acc=acc, raw=raw)
        self.assertEqual(events(arm), [("roll", "right roll, ended with contact"), ("crash", "impact")])
        # at a hard landing the tumble across the final impact is the landing; a trick that ended before it stays
        acc, raw = impact(19.0)
        landing = make_arm(20, gyro=axis(0, pulse(18.6, 19.32, 500)), acc=acc, raw=raw)
        self.assertEqual(events(landing), [("crash", "hard landing (arm end)")])
        before = make_arm(20, gyro=axis(0, pulse(17.0, 17.72, 500)), acc=acc, raw=raw)
        self.assertEqual(events(before), [("roll", "right roll"), ("crash", "hard landing (arm end)")])

    def test_a_tumble_is_not_a_barrel_roll(self):
        """A rotation on several axes with an impact in it is a crash tumble, never a barrel roll with contact."""
        acc, raw = impact(2.55)
        arm = make_arm(20, gyro=combine(axis(0, pulse(2.0, 2.72, 500)), axis(2, pulse(2.0, 2.72, 200))), acc=acc, raw=raw)
        self.assertEqual(events(arm), [("crash", "impact")])

    def test_a_stopped_motor_means_not_flying(self):
        roll = axis(0, pulse(2.0, 2.72, 500))
        self.assertEqual(titles(make_arm(5, gyro=roll, motors=lambda t: (1200, 1200, 1200, 1200))), ["right roll"])
        self.assertEqual(titles(make_arm(5, gyro=roll, motors=lambda t: (0, 1200, 1200, 1200))), [])    # crash flip mode, a disarm
        self.assertEqual(titles(make_arm(8, gyro=axis(1, pulse(2.0, 4.4, -150)), motors=lambda t: (0, 0, 0, 0))), [])   # nor a loop
        self.assertEqual(titles(make_arm(5, gyro=roll)), ["right roll"])                                   # no motor fields: no opinion

    def test_without_the_stick_a_rotation_is_not_asked_for(self):
        self.assertEqual(titles(make_arm(5, gyro=axis(0, pulse(2.0, 2.72, 500)), sticks=lambda t: (0.0, 0.0, 0.0))), [])


class ThrottleRulesTest(unittest.TestCase):
    def test_a_cut_from_the_first_frame_is_the_quad_on_the_ground(self):
        self.assertEqual(titles(make_arm(10, throttle=lambda t: 0 if t < 6 else 50, quat0=quat_from_lean(60, 0))), [])
        self.assertEqual(titles(make_arm(10, throttle=lambda t: 0 if 3 <= t < 6 else 50, quat0=quat_from_lean(60, 0))), ["dive"])

    def test_a_tumbling_cut_is_a_crash_not_a_dive(self):
        tumble = make_arm(10, gyro=combine(axis(0, pulse(3.5, 4.0, 2000)), axis(1, pulse(4.0, 4.5, 2000))), throttle=lambda t: 0 if 3 <= t < 6 else 50, quat0=quat_from_lean(60, 0))
        self.assertNotIn("dive", titles(tumble))

    def test_a_tumbling_pitch_back_is_no_powerloop(self):
        self.assertNotIn("powerloop", titles(make_arm(8, gyro=axis(1, pulse(2.0, 3.6, -2000)))))


class IfTest(unittest.TestCase):
    def test_if(self):
        self.assertEqual(expr.compile("if(1 < 2, 'a', 'b')").eval({}), "a")
        self.assertEqual(expr.compile("if(x > 4, ', ended with contact', '')").eval({"x": 3.4}), "")
        self.assertEqual(expr.compile("if(x > 4, 1, 2)").eval({"x": None}), 2)
        self.assertEqual(expr.render("{direction} roll{if(acc > g, ', ended with contact', '')}", {"direction": "right", "acc": 6.9, "g": 4.0}), "right roll, ended with contact")
        for bad in ("if(1, 2)", "if(1, 2, 3, 4)", "if()"):
            with self.assertRaises(expr.ExprError):
                expr.compile(bad)


class PhasesLoadTest(unittest.TestCase):
    BASE = '''
[segment.fast]
kind = "rotation"
[segment.pb]
kind = "sustained"
[[trick]]
id = "s"
tag = "s"
title = "s {p1.rotation_deg.roll} {p2.rotation_deg.pitch} {duration}"
phases = [
  { segment = "fast", when = ["abs(rotation_deg.roll) >= 150"] },
  { segment = "pb", within = 1.0, when = ["rotation_deg.pitch <= -50", "p1.tilt_deg.max >= 90"] },
]
'''

    def test_loads_and_checks(self):
        lib = Library.from_text(self.BASE)
        t = lib.tricks["s"]
        self.assertEqual([(ph.segment, ph.within, ph.conditions) for ph in t.phases],
                         [("fast", 1.0, ["abs(rotation_deg.roll) >= 150"]), ("pb", 1.0, ["rotation_deg.pitch <= -50", "p1.tilt_deg.max >= 90"])])
        self.assertEqual(lib.requires(t), [])
        for bad, needle in (('segment = "pb"\n', "has phases, so no segment"),
                            (None, None)):
            if bad is None:
                break
            with self.assertRaises(LibraryError) as cm:
                Library.from_text(self.BASE.replace('tag = "s"\n', 'tag = "s"\n' + bad))
            self.assertIn(needle, str(cm.exception))

    def refuse(self, text, needle):
        with self.assertRaises(LibraryError) as cm:
            Library.from_text(text)
        self.assertIn(needle, str(cm.exception))

    def test_refusals(self):
        self.refuse(self.BASE.replace('{ segment = "fast", when', '{ segment = "fast", within = 2, when'), "the first phase has no `within`")
        self.refuse(self.BASE.replace('{ segment = "pb", within = 1.0,', '{ segment = "pbb", within = 1.0,'), "phase 2: unknown segment 'pbb'")
        self.refuse(self.BASE.replace('"p1.tilt_deg.max >= 90"', '"p2.tilt_deg.max >= 90"'), "unknown name 'p2.tilt_deg.max'")      # not before it is cut
        self.refuse(self.BASE.replace('"abs(rotation_deg.roll) >= 150"', '"impact_g > 1"'), "phase 1 condition")
        self.refuse(self.BASE.replace("{p2.rotation_deg.pitch}", "{p3.rotation_deg.pitch}"), "'p3.rotation_deg.pitch'")
        self.refuse(self.BASE.replace("{p2.rotation_deg.pitch}", "{p2.impact_g}"), "'p2.impact_g'")
        self.refuse(self.BASE.replace("phases = [\n", "phases = [\n  { segment = \"fast\", whenn = [] },\n"), "phase 1 is")
        self.refuse(self.BASE.replace("phases = [", "phases = [ 3,"), "two or more")
        self.refuse(self.BASE.replace('{ segment = "pb", within = 1.0, when = ["rotation_deg.pitch <= -50", "p1.tilt_deg.max >= 90"] },\n', ""), "two or more")
        self.refuse(self.BASE.replace('within = 1.0', 'within = "soon"'), "within must be a number")

    def test_the_shipped_sequences(self):
        lib = Library.load([SHIPPED])
        self.assertEqual([ph.segment for ph in lib.tricks["split-s"].phases], ["fast-rotation", "pitch-back"])
        self.assertEqual([ph.segment for ph in lib.tricks["figure-8"].phases], ["turn", "turn"])
        self.assertEqual(lib.requires(lib.tricks["figure-8"]), ["imuQuaternion[0]"])


class EvalTest(unittest.TestCase):
    def test_evaluate_against_moments(self):
        roll = make_arm(20, gyro=axis(0, pulse(5.0, 5.72, 500)))
        orbit = make_arm(20, gyro=banked_turn(2.0, 10.0, 60), quat0=quat_from_lean(0, 40))
        arms = {("L.bbl", 1): roll, ("L.bbl", 2): orbit}

        def load(b, n):
            try:
                return arms[(b, n)]
            except KeyError:
                raise FileNotFoundError(b) from None
        session = {"session": "2026-10-04-motor-jitter-testing",
                   "matches": [{"video": "a.mp4", "bbl": "L.bbl", "arm": 1, "offset": 100.0}, {"video": "b.mp4", "bbl": "L.bbl", "arm": 2, "offset": 10.0},
                               {"video": "c.mp4", "bbl": "L.bbl", "arm": 3, "offset": 0.0}],
                   "moments": [{"id": "m01", "video": "a.mp4", "start": 104.5, "end": 106.5, "title": "roll", "tags": ["roll"]},
                               {"id": "m02", "video": "a.mp4", "start": 110.0, "end": 112.0, "title": "another roll", "tags": ["roll", "poi"]},
                               {"id": "m03", "video": "b.mp4", "start": 15.0, "end": 25.0, "title": "far end", "tags": ["poi"]},
                               {"id": "m04", "video": "c.mp4", "start": 1.0, "end": 2.0, "title": "no arm here", "tags": ["flip"]},
                               {"id": "m05", "video": "a.mp4", "start": 105.0, "end": 106.0, "title": "a backflip, says the tag", "tags": ["backflip"]}]}
        res = tricks.evaluate(session, load, LIB)
        self.assertEqual(res["per_tag"], {"flip": {"found": 0, "missed": 1}, "roll": {"found": 1, "missed": 1}})
        self.assertEqual([m["id"] for m in res["missed"]], ["m05", "m02"])                      # in time order
        self.assertEqual(next(m for m in res["missed"] if m["id"] == "m02")["arms"], {1: 10.0})
        self.assertEqual(res["found"]["roll"][0]["found"], ["right roll"])
        self.assertEqual([(e["tag"], e["video"], round(e["start"]), e["moment"]) for e in res["untagged"]],
                         [("orbit", "b.mp4", 12, "far end ['poi']")])                                    # the orbit overlaps a poi moment


if __name__ == "__main__":
    unittest.main()
