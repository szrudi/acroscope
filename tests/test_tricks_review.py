"""What the review of #49 found (2026-10-17), pinned: a bad entry fails its own verdict and nothing else, a stored
layer a later release made invalid leaves the shipped tricks in force, the layer edits keep an entry's fields,
suppression runs to a fixed point, sequences never reuse a window, the eval files a moment under the tag that
finds it and credits one detection once, the cost caps of the language, and the identity of the motor scan."""
import io
import tempfile
import tomllib
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from acroscope import tricks
from acroscope.sources import blackbox
from acroscope.store import sessions
from acroscope.tricks import expr
from acroscope.store.db import Db
from acroscope.tricks.expr import ExprError
from acroscope.tricks.library import SHIPPED, Library, LibraryError

try:
    from tests.synthetic import axis, combine, make_arm, pulse, quat_from_lean
except ImportError:
    from synthetic import axis, combine, make_arm, pulse, quat_from_lean

LIB = tricks.library()
ROLL = make_arm(5, gyro=axis(0, pulse(2.0, 2.72, 500)))


def impact(at, g=6.0, jolt=900.0):
    return (lambda t: (0.0, 0.0, g) if at <= t < at + 0.05 else (0.0, 0.0, 1.0),
            lambda t: (jolt, 0.0, 0.0) if at <= t < at + 0.05 else (0.0, 0.0, 0.0))


def titles(arm, lib=LIB):
    return [e["title"] for e in tricks.detect(arm, lib)]


def shipped_plus(text: str) -> Library:
    lib = Library()
    lib.add(tomllib.loads(SHIPPED.read_text()), "shipped")
    lib.add(tomllib.loads(text), "database")
    lib.validate()
    return lib


class ErrorsStayLocalTest(unittest.TestCase):
    def test_a_condition_that_fails_to_evaluate_fails_its_trick_only(self):
        """`craft.frame == 'air65'` loads, and on a quad where frame is a number `<` would raise: the trick's
        verdict says so, the other tricks go on, nothing is a 500."""
        lib = shipped_plus('''
[craft.default]
frame = 65
[[trick]]
id = "big-frame-roll"
tag = "roll"
segment = "fast-rotation"
when = ["craft.frame < 'x'"]
title = "big"
priority = 50
''')
        self.assertEqual(titles(ROLL, lib), ["right roll"])
        ex = tricks.explain(ROLL, lib)
        verdict = next(t for w in ex["windows"] for t in w["tricks"] if t["id"] == "big-frame-roll")
        self.assertTrue(verdict["failed"].startswith("error: cannot compare 65 < 'x'"))
        self.assertIn("big-frame-roll", tricks.Run(ROLL, lib).errors)

    def test_a_title_that_fails_to_render_fails_its_trick_only(self):
        lib = shipped_plus('''
[[trick]]
id = "bad-title"
tag = "roll"
segment = "fast-rotation"
title = "{direction:.2f} roll"
priority = 50
''')
        self.assertEqual(titles(ROLL, lib), ["right roll"])

    def test_a_layer_a_later_release_made_invalid_leaves_the_shipped_tricks_in_force(self):
        """A stored layer naming a param that no longer exists: the shipped library applies, the error is kept for
        the page (describe) to show, and a repaired layer clears it."""
        layer = {"value": {"version": 1, "params": {"roll_hold_share": 0.85}}, "stamp": "a"}
        with mock.patch.object(tricks, "LAYER_SOURCE", lambda: (layer["value"], layer["stamp"])), mock.patch.object(tricks, "_cache", None), \
                mock.patch.object(tricks, "LAYER_ERROR", None), mock.patch.object(blackbox, "all_bbls", lambda: []):
            lib = tricks.library()
            self.assertEqual(lib.sources, [str(SHIPPED)])
            self.assertIn("roll_hold_share", tricks.LAYER_ERROR)
            self.assertEqual(titles(ROLL, lib), ["right roll"])
            d = tricks.describe(lib)
            self.assertIn("roll_hold_share", d["layer_error"])
            self.assertFalse(d["database"])
            layer["value"], layer["stamp"] = {"version": 1, "skill": "beginner"}, "b"
            self.assertEqual(tricks.library().skill, "beginner")
            self.assertIsNone(tricks.LAYER_ERROR)


class LayerEditsTest(unittest.TestCase):
    def test_on_for_one_quad_while_off_below(self):
        """`off gyro-kick` for everyone, then `on gyro-kick --craft X`: on for X, off for the rest."""
        t = tricks.edit_layer({"version": 1}, "off", "gyro-kick")
        t = tricks.edit_layer(t, "on", "gyro-kick", craft="X")
        lib = tricks.check_layer(t)
        self.assertFalse(lib.for_craft(None).tricks["gyro-kick"].enabled)
        self.assertTrue(lib.for_craft("X").tricks["gyro-kick"].enabled)
        # a trick shipped off (here: made off by a layer) can be switched on
        t = tricks.edit_layer(t, "on", "gyro-kick")
        self.assertTrue(tricks.check_layer(t).for_craft(None).tricks["gyro-kick"].enabled)

    def test_a_craft_preset_beats_the_pilots_general_numbers(self):
        """The quad's own word: `[craft.X] skill = "beginner"` over a global `flip_min_deg = 250` gives X 240; the
        file-level preset still sits under the pilot's numbers."""
        lib = shipped_plus('skill = "advanced"\n[params]\nflip_min_deg = 250\n[craft."X"]\nskill = "beginner"\n')
        self.assertEqual(lib.for_craft(None).params["flip_min_deg"], 250)
        self.assertEqual(lib.for_craft("X").params["flip_min_deg"], 240)
        self.assertEqual(lib.param_sources("X")["flip_min_deg"], "preset beginner")
        self.assertEqual(lib.param_sources(None)["flip_min_deg"], "database")
        # the craft's own numbers still beat its preset
        lib = shipped_plus('[craft."X"]\nskill = "beginner"\n[craft."X".params]\nflip_min_deg = 260\n')
        self.assertEqual(lib.for_craft("X").params["flip_min_deg"], 260)

    def test_a_derived_library_answers_for_craft_from_the_base(self):
        """`--craft X` derives a library; an arm of craft Y seen through it must not carry X's overrides."""
        lib = shipped_plus('[[craft."X".trick]]\nid = "orbit"\nenabled = false\n')
        self.assertFalse(lib.for_craft("X").tricks["orbit"].enabled)
        self.assertTrue(lib.for_craft("X").for_craft("Y").tricks["orbit"].enabled)
        self.assertIs(lib.for_craft("X").for_craft("Y"), lib.for_craft("Y"))

    def test_a_crafts_own_trick_may_read_a_new_param(self):
        lib = shipped_plus('''
[craft."X".params]
my_thr = 40
[[craft."X".trick]]
id = "my-dive"
tag = "dive"
segment = "throttle-cut"
when = ["throttle_before_pct >= my_thr"]
title = "my dive"
''')
        self.assertIn("my-dive", lib.for_craft("X").tricks)

    def test_import_folds_case_duplicate_crafts_and_refuses_dates(self):
        t = tricks.import_table('version = 1\n[craft.otto]\nsize_mm = 75\n[craft.OTTO]\nskill = "advanced"\n', None, "replace")
        self.assertEqual(t["craft"], {"otto": {"size_mm": 75, "skill": "advanced"}})
        with self.assertRaises(LibraryError) as cm:
            tricks.import_table('version = 1\n[[trick]]\nid = "roll"\nexamples = [{ log = "a.bbl", arm = 1, at = 1, note = 2026-10-05 }]\n', None)
        self.assertIn("date", str(cm.exception))

    def test_a_parse_that_is_not_a_number_is_refused(self):
        self.assertEqual(tricks.parse_value("24O"), "24O")
        with self.assertRaises(LibraryError):
            tricks.check_layer(tricks.edit_layer(None, "set", "flip_min_deg", "24O"))
        with self.assertRaises(LibraryError):
            tricks.check_layer(tricks.edit_layer(None, "set", "flip_min_deg", float("nan")))

    def test_an_example_log_is_a_bare_bbl_name(self):
        for bad in ("/etc/hostname", "../x.bbl", "x.csv", ".bbl", "a/b.bbl"):
            with self.assertRaises(LibraryError, msg=bad):
                shipped_plus(f'[[trick]]\nid = "roll"\nexamples = [{{ log = "{bad}", arm = 1, at = 1 }}]\n')
        for bad in ('note = 3', 'absent = "yes"'):
            with self.assertRaises(LibraryError, msg=bad):
                shipped_plus(f'[[trick]]\nid = "roll"\nexamples = [{{ log = "a.bbl", arm = 1, at = 1, {bad} }}]\n')
        with self.assertRaises(LibraryError):
            shipped_plus('[[trick]]\nid = "roll"\nsuppress = [{ tags = [1], margin = 1 }]\n')

    def test_a_phase_may_read_only_what_the_engine_hands_over(self):
        with self.assertRaises(LibraryError) as cm:
            shipped_plus('''
[[trick]]
id = "s2"
tag = "split-s"
title = "s"
phases = [{ segment = "fast-rotation" }, { segment = "pitch-back", when = ["p1.arm.length > 0"] }]
''')
        self.assertIn("p1.arm.length", str(cm.exception))

    def test_craft_params_are_checked_like_the_rest(self):
        for bad in ('[craft."X".params]\nduration = 3\n', '[craft."X".params]\nflip_min_deg = true\n', '[skill.beginner]\nflip_min_deg = "x"\n'):
            with self.assertRaises(LibraryError, msg=bad):
                shipped_plus(bad)


class EngineRulesTest(unittest.TestCase):
    def test_suppression_runs_to_a_fixed_point(self):
        """A over B over C (priorities 0, 10, 5) on one window leaves A and C: a suppressed B suppresses nothing."""
        lib = Library.from_text('''
[segment.fast]
kind = "rotation"
[segment.k]
kind = "impact"
[[trick]]
id = "a"
tag = "a"
segment = "k"
title = "a"
priority = 0
suppress = [{ tags = ["b"], margin = 5 }]
[[trick]]
id = "b"
tag = "b"
segment = "fast"
title = "b"
priority = 10
suppress = [{ tags = ["c"], margin = 5 }]
[[trick]]
id = "c"
tag = "c"
segment = { kind = "kick" }
title = "c"
priority = 5
''')
        acc, raw = impact(3.0)
        raw2 = lambda t: raw(t) if t < 3.1 else ((0.0, 0.0, 700.0) if 3.5 <= t < 3.53 else (0.0, 0.0, 0.0))
        arm = make_arm(8, gyro=axis(0, pulse(2.0, 2.72, 500)), acc=acc, raw=raw2)
        self.assertEqual([e["trick"] for e in tricks.detect(arm, lib)], ["a", "c"])
        ex = tricks.explain(arm, lib)
        fate = {t["id"]: t["suppressed_by"] for w in ex["windows"] for t in w["tricks"]}
        self.assertEqual(fate, {"a": None, "b": "a", "c": None})

    def test_a_figure_8_is_one_event(self):
        import math
        def turn(t0, t1, rate, bank=40.0):
            b = math.radians(bank)
            return lambda t: (0.0, rate * math.sin(b), rate * math.cos(b)) if t0 <= t < t1 else (0.0, 0.0, 0.0)
        arm = make_arm(16, gyro=combine(turn(2.0, 8.0, 60), turn(8.5, 14.5, -60)), quat0=quat_from_lean(0, 40))    # 360 then 360: each half an orbit on its own
        self.assertEqual(titles(arm), ["figure 8: right then left"])

    def test_phases_never_reuse_a_window_and_chain_once(self):
        lib = shipped_plus('''
[[trick]]
id = "double"
tag = "roll"
title = "double"
priority = 60
phases = [{ segment = "fast-rotation", when = ["abs(rotation_peak_deg.roll) >= 150"] }, { segment = "fast-rotation", within = 1.0, when = ["abs(rotation_peak_deg.roll) >= 150"] }]
suppress = [{ tags = ["roll"], margin = 0 }]
''')
        self.assertEqual(titles(make_arm(6, gyro=axis(0, pulse(2.0, 2.4, 500))), lib), ["right roll attempt, 199 deg"])     # one window is not two phases
        two = make_arm(6, gyro=combine(axis(0, pulse(2.0, 2.4, 500)), axis(0, pulse(2.9, 3.3, 500))))
        self.assertEqual(titles(two, lib), ["double"])
        three = make_arm(8, gyro=combine(axis(0, pulse(2.0, 2.4, 500)), axis(0, pulse(2.9, 3.3, 500)), axis(0, pulse(3.8, 4.2, 500))))
        self.assertEqual(titles(three, lib), ["double", "right roll attempt, 199 deg"])                           # one chain per first window, greedy

    def test_strict_edges(self):
        self.assertEqual(tricks.explain(make_arm(5, gyro=axis(0, pulse(2.0, 3.0, 100.0))), LIB)["windows"], [])    # at the threshold: no window
        self.assertEqual(len([w for w in tricks.explain(make_arm(5, gyro=axis(0, pulse(2.0, 3.0, 100.5))), LIB)["windows"] if w["kind"] == "rotation"]), 1)
        # the same peak on roll and pitch: roll is the axis
        both = make_arm(5, gyro=combine(axis(0, pulse(2.0, 2.72, 500)), axis(1, pulse(2.0, 2.72, 500))))
        self.assertEqual(next(w for w in tricks.explain(both, LIB)["windows"] if w["kind"] == "rotation")["numbers"]["axis"], "roll")
        # `within` is strict: instants exactly 0.5 s apart are not within 0.5
        lib = Library.from_text('''
[[trick]]
id = "k"
tag = "k"
segment = { kind = "kick" }
title = "k"
[[trick]]
id = "c"
tag = "c"
segment = { kind = "impact" }
title = "c"
priority = 5
suppress = [{ tags = ["k"], within = 0.5 }]
''')
        acc, raw = impact(4.0)
        raw2 = lambda t: raw(t) if t < 4.1 else ((0.0, 0.0, 700.0) if 4.5 <= t < 4.53 else (0.0, 0.0, 0.0))
        arm = make_arm(10, acc=acc, raw=raw2)
        self.assertEqual([e["trick"] for e in tricks.detect(arm, lib)], ["c", "k"])

    def test_a_tumble_without_contact_is_nothing_and_the_net_goes_the_peaks_way(self):
        self.assertEqual(titles(make_arm(5, gyro=axis(0, pulse(2.0, 2.5, 2000)))), [])                 # 1000 deg at 2000 dps, no impact
        acc, raw = impact(2.4)
        self.assertEqual(titles(make_arm(10, gyro=axis(0, pulse(2.0, 2.5, 2000)), acc=acc, raw=raw)), ["impact", "right roll, ended with contact"])   # the impact's window starts first
        back = make_arm(5, gyro=combine(axis(0, pulse(2.0, 2.6, 500)), axis(0, pulse(2.6, 3.76, -500))))      # 300 right then 580 left
        self.assertEqual(titles(back), [])

    def test_a_drop_at_an_impact_uses_no_cooldown(self):
        """An eRPM dip at the impact is the impact; the real loss 0.9 s later is still reported (the pre-#22 scan)."""
        acc, raw = impact(4.0)
        erpm = lambda t: (5000, 1000 if 4.1 <= t < 4.6 or t >= 4.9 else 5000, 5000, 5000)
        arm = make_arm(12, motors=lambda t: (1900,) * 4, erpm=erpm, acc=acc, raw=raw)
        self.assertEqual(titles(arm), ["impact", "motor 2 power loss"])
        self.assertAlmostEqual(next(e for e in tricks.detect(arm, LIB) if e["tag"] == "motor-loss")["numbers"]["arm_t"], 4.9, delta=0.06)


class EvalTest(unittest.TestCase):
    def test_found_under_the_tag_that_finds_it_and_one_detection_once(self):
        roll = make_arm(20, gyro=axis(0, pulse(5.0, 5.72, 500)))
        flip = make_arm(20, gyro=axis(1, pulse(5.0, 5.72, -500)))
        arms = {("L.bbl", 1): roll, ("L.bbl", 2): flip}
        session = {"matches": [{"video": "a.mp4", "bbl": "L.bbl", "arm": 1, "offset": 0.0}, {"video": "b.mp4", "bbl": "L.bbl", "arm": 2, "offset": 0.0}],
                   "moments": [{"id": "m1", "video": "a.mp4", "start": 5.0, "end": 5.3, "title": "first half", "tags": ["roll"]},
                               {"id": "m2", "video": "a.mp4", "start": 5.4, "end": 5.8, "title": "second half", "tags": ["roll"]},
                               {"id": "m3", "video": "b.mp4", "start": 5.0, "end": 6.0, "title": "a backflip", "tags": ["backflip", "flip"]},
                               {"id": "m4", "video": "b.mp4", "start": 6.1, "end": 7.0, "title": "right after", "tags": ["flip"]}]}
        res = tricks.evaluate(session, lambda b, n: arms[(b, n)], LIB)
        self.assertEqual(res["per_tag"], {"flip": {"found": 1, "missed": 1}, "roll": {"found": 1, "missed": 1}})
        self.assertEqual([m["id"] for m in res["missed"]], ["m2", "m4"])                 # one detection credits one moment; m4 starts 0.38 s after the flip ends: within tolerance? no, 6.1 - 5.72 = 0.38 < 0.5: found
        self.assertEqual(sorted(res["found"]), ["flip", "roll"])
        self.assertEqual(res["found"]["flip"][0]["id"], "m3")                           # backflip filed under flip, counted once


class LanguageCostTest(unittest.TestCase):
    def test_caps_and_refusals(self):
        import time
        t = time.time()
        self.assertEqual(expr.compile("round(7, -1000000)").eval({}), 0)
        self.assertLess(time.time() - t, 0.05)
        for bad in ("(" * 40 + "1" + ")" * 40, "1" * 30, "round(1.5, 1.5)", "abs(1, 2)", "sqrt()", "round(1, 2, 3)", "if(1, 2)", "-true", "and", "x and and y"):
            with self.assertRaises(ExprError, msg=bad):
                expr.compile(bad).eval({"x": 1, "y": 2})
        self.assertEqual(expr.compile("min(3)").eval({}), 3)
        self.assertEqual(expr.compile("2 ** -1").eval({}), 0.5)
        self.assertIsNone(expr.compile("(0 - 8) ** 0.5").eval({}))
        self.assertFalse(expr.compile("x ** 0.5 > 1").eval({"x": -8}))
        self.assertFalse(expr.compile("axis == 1").eval({"axis": "roll"}))
        self.assertTrue(expr.compile("axis != 1").eval({"axis": "roll"}))
        for bad in ("{duration:>10000000}", "{duration:zz}", "{duration:" + "x" * 20 + "}"):
            with self.assertRaises(ExprError, msg=bad):
                expr.template_exprs(bad)
        self.assertEqual(len(expr.template_exprs("{duration:d} and {axis:>8}")), 2)      # specs that fit some value load; the value decides at render


class CliLayerTest(unittest.TestCase):
    """The layer reaches every local command (`events` detected with the shipped numbers only), and export/import
    run on the client when a server is set."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Db(Path(self.tmp.name) / "t.db")
        for p in (mock.patch.object(sessions, "_store", self.db), mock.patch.object(sessions, "SERVER_URL", ""),
                  mock.patch.object(tricks, "_cache", None), mock.patch.object(tricks, "LAYER_SOURCE", None),
                  mock.patch.object(blackbox, "all_bbls", lambda: []), mock.patch.object(blackbox, "load_arm", lambda b, n: make_arm(5, gyro=axis(0, pulse(2.0, 2.5, 500))))):
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(self.tmp.cleanup)

    def cli(self, *args) -> str:
        out = io.StringIO()
        with redirect_stdout(out):
            from acroscope import cli
            cli.main(list(args))
        return out.getvalue()

    def test_events_sees_the_layer(self):
        self.db.set_setting("tricks", {"version": 1, "skill": "beginner"})
        self.assertIn("right roll", self.cli("events", "--arm", "x.bbl:1"))
        self.assertNotIn("attempt", self.cli("events", "--arm", "x.bbl:1"))

    def test_skill_for_a_craft_in_another_case(self):
        self.db.set_setting("tricks", {"version": 1, "craft": {"OttO": {"size_mm": 75}}})
        self.assertEqual(self.cli("tricks", "skill", "beginner", "--craft", "OTTO").strip(), "skill: beginner")
        self.assertEqual(self.db.setting("tricks")["value"]["craft"], {"OttO": {"size_mm": 75, "skill": "beginner"}})
        self.assertEqual(self.cli("tricks", "on", "gyro-kick", "--craft", "otto").strip(), "gyro-kick: on for OttO")

    def test_a_stale_write_is_refused(self):
        self.db.set_setting("tricks", {"version": 1})
        with self.assertRaises(ValueError):
            self.db.set_setting("tricks", {"version": 1, "skill": "beginner"}, etag=tricks.layer_etag({"version": 1, "params": {"a": 1}}))
        self.db.set_setting("tricks", {"version": 1, "skill": "beginner"}, etag=tricks.layer_etag({"version": 1}))
        self.assertEqual(self.db.setting("tricks")["value"]["skill"], "beginner")
        with self.assertRaises(ValueError):
            self.db.set_setting("tricks", {"version": 1, "params": {"x": float("nan")}})

    def test_export_and_import_run_here_with_a_server(self):
        calls = []

        class FakeRemote:
            def setting(self, key):
                return {"value": {"version": 1, "skill": "beginner"}, "updated_at": "t"}

            def set_setting(self, key, value, etag=None):
                calls.append(value)
                return {"value": value, "updated_at": "t"}
        with mock.patch.object(sessions, "_store", FakeRemote()), mock.patch.object(sessions, "SERVER_URL", "http://example.invalid"):
            text = self.cli("tricks", "export")
            self.assertEqual(tomllib.loads(text), {"version": 1, "skill": "beginner"})
            self.assertNotIn("example.invalid", text)                                   # nothing about the store in a shared file
            f = Path(self.tmp.name) / "in.toml"
            f.write_text("[params]\nflip_min_deg = 250\n")
            self.cli("tricks", "import", str(f), "--merge")
        self.assertEqual(calls, [{"version": 1, "skill": "beginner", "params": {"flip_min_deg": 250}}])


if __name__ == "__main__":
    unittest.main()
