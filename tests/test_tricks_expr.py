"""The tricks file's condition language (acroscope/tricks/expr.py): what it computes, what it refuses, and that a
downloaded trick file cannot run code through it."""
import unittest

from acroscope.tricks import expr
from acroscope.tricks.expr import ExprError

NS = {"rotation_deg.roll": -304, "rotation_deg.pitch": -40, "throttle_pct.min": 49, "duration": 1.88, "axis": "roll",
      "tumble_dps": 1500, "peak_rate_dps.roll": 429, "peak_rate_dps.pitch": 144, "erpm_before": None, "landing": True}


def ev(text, ns=NS):
    return expr.compile(text).eval(ns)


class ExprTest(unittest.TestCase):
    def test_arithmetic_and_precedence(self):
        self.assertEqual(ev("1 + 2 * 3"), 7)
        self.assertEqual(ev("(1 + 2) * 3"), 9)
        self.assertEqual(ev("-2 ** 2"), -4.0)                  # like Python: unary minus binds looser than **
        self.assertEqual(ev("7 % 4 / 2"), 1.5)
        self.assertEqual(ev("abs(rotation_deg.roll) - 4"), 300)
        self.assertEqual(ev("max(peak_rate_dps.roll, peak_rate_dps.pitch)"), 429)
        self.assertEqual(ev("min(3, 1, 2)"), 1)
        self.assertEqual(ev("round(duration)"), 2)
        self.assertEqual(ev("round(duration, 1)"), 1.9)
        self.assertEqual(ev("sqrt(16)"), 4.0)
        self.assertIsNone(ev("sqrt(-1)"))
        self.assertIsNone(ev("1 / 0"))

    def test_comparisons_chain_like_python(self):
        self.assertTrue(ev("150 <= abs(rotation_deg.roll) <= 310"))
        self.assertFalse(ev("150 <= abs(rotation_deg.roll) <= 300"))
        self.assertTrue(ev("rotation_deg.pitch <= -40"))
        self.assertTrue(ev("axis == 'roll'"))
        self.assertTrue(ev('axis != "pitch"'))
        self.assertTrue(ev("1 < 2 == 2 < 3"))
        self.assertTrue(ev("landing"))
        self.assertTrue(ev("landing == true"))

    def test_boolean_logic(self):
        self.assertTrue(ev("axis == 'roll' and throttle_pct.min < 50"))
        self.assertFalse(ev("axis == 'roll' and throttle_pct.min < 40"))
        self.assertTrue(ev("axis == 'pitch' or throttle_pct.min < 50"))
        self.assertTrue(ev("not axis == 'pitch'"))
        self.assertTrue(ev("not (axis == 'pitch' or duration > 2)"))
        self.assertFalse(ev("not duration"))
        # and binds tighter than or
        self.assertTrue(ev("false and true or true"))
        self.assertFalse(ev("true and (false or false)"))

    def test_none_never_fires_and_never_crashes(self):
        """A feature the log cannot give (no eRPM on this quad) is none: a condition on it is false, arithmetic
        with it stays none, so a trick that needs it simply does not fire."""
        self.assertFalse(ev("erpm_before > 1500"))
        self.assertFalse(ev("erpm_before < 1500"))
        self.assertFalse(ev("erpm_before == none"))              # a comparison with none is false either way
        self.assertIsNone(ev("erpm_before * 2"))
        self.assertIsNone(ev("abs(erpm_before)"))
        self.assertIsNone(ev("max(erpm_before, 3)"))
        self.assertFalse(ev("erpm_before * 2 > 1"))
        self.assertTrue(ev("not erpm_before"))
        self.assertTrue(ev("erpm_before > 1 or duration > 1"))

    def test_names_are_what_it_reads(self):
        e = expr.compile("150 <= abs(rotation_deg.roll) <= tumble_dps and axis == 'roll'")
        self.assertEqual(set(e.names), {"rotation_deg.roll", "tumble_dps", "axis"})
        self.assertEqual(expr.compile("max(1, 2)").names, frozenset())

    def test_refuses_what_is_not_the_language(self):
        """No attribute access, no indexing, no calls but the five functions, no Python at all: a trick file
        from the internet must not run code."""
        for bad in ("__import__('os')", "open('x')", "a[0]", "lambda: 1", "1 if 2 else 3",
                    "a = 1", "a; b", "f'{x}'", "[1, 2]", "{1: 2}", "x and", "and x", "(1", "1 +", "foo(", "'unterminated",
                    "abs()", "1 2", "", "   ", "not", "true()", "1 = 1", "a.b.", "@"):
            with self.assertRaises(ExprError, msg=bad):
                expr.compile(bad)
        with self.assertRaises(ExprError):
            ev("nothing_here")
        with self.assertRaises(ExprError):
            ev("axis.__class__")                 # a dotted name is a key in a flat namespace, never an attribute
        with self.assertRaises(ExprError):
            ev("axis + 1")
        with self.assertRaises(ExprError):
            ev("axis < 1")
        with self.assertRaises(ExprError):
            ev("-axis")

    def test_no_eval_anywhere(self):
        """The module calls none of Python's doors: eval, exec, compile, getattr, __import__ (Expr.eval is its own walker)."""
        import ast
        import inspect
        calls = {n.func.id for n in ast.walk(ast.parse(inspect.getsource(expr))) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
        self.assertFalse(calls & {"eval", "exec", "compile", "getattr", "setattr", "__import__", "globals", "locals", "vars"}, calls)


class TemplateTest(unittest.TestCase):
    def test_render(self):
        self.assertEqual(expr.render("roll {rotation_deg.roll:+} / pitch {rotation_deg.pitch:+} deg in {duration} s", NS),
                         "roll -304 / pitch -40 deg in 1.88 s")
        self.assertEqual(expr.render("peak {max(peak_rate_dps.roll, peak_rate_dps.pitch)} dps", NS), "peak 429 dps")
        self.assertEqual(expr.render("{duration:.1f} s, {duration * 1000:.0f} ms", NS), "1.9 s, 1880 ms")
        self.assertEqual(expr.render("{axis} roll", NS), "roll roll")
        self.assertEqual(expr.render("no holes", NS), "no holes")
        self.assertEqual(expr.render("eRPM {erpm_before:.0f} before", NS), "eRPM ? before")      # a missing value: '?'
        self.assertEqual(expr.render("{landing}", NS), "true")
        self.assertEqual(expr.render("{2.0 * 3:+}", NS), "+6")                                  # a whole float prints whole with +/d

    def test_template_exprs(self):
        holes = expr.template_exprs("{a:.1f} g at {arm_t:.2f} s of {arm.length}")
        self.assertEqual([(e.text, f) for e, f in holes], [("a", ".1f"), ("arm_t", ".2f"), ("arm.length", "")])
        with self.assertRaises(ExprError):
            expr.template_exprs("{a[0]}")
        with self.assertRaises(ExprError):
            expr.render("{axis:.2f}", NS)


if __name__ == "__main__":
    unittest.main()
