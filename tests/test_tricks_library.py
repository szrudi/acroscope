"""The tricks file loader (acroscope/tricks/library.py): the shipped file, what a bad file is refused for (at load,
with the entry named: a typo must never become a trick that quietly cannot fire), and a second file merged over
the first (what a pilot's overrides will be)."""
import tomllib
import unittest

from acroscope.tricks import segments
from acroscope.tricks.library import SHIPPED, Library, LibraryError

BASE = '''
version = 1
[params]
flip_min_deg = 270
[segment.fast]
kind = "rotation"
threshold = 100
[[trick]]
id = "roll"
tag = "roll"
segment = "fast"
when = ["axis == 'roll'", "abs(rotation_deg.roll) >= flip_min_deg"]
title = "{direction} roll"
why = "roll {rotation_deg.roll:+} deg in {duration} s"
priority = 10
'''


def load(text: str) -> Library:
    return Library.from_text(text, "test.toml")


class ShippedTest(unittest.TestCase):
    def test_loads_with_the_pre_22_detectors(self):
        lib = Library.load([SHIPPED])
        self.assertEqual([t.id for t in lib.ordered()],
                         ["crash", "hard-landing", "powerloop", "split-s", "flip", "flip-with-yaw", "flip-attempt", "roll", "barrel-roll", "roll-attempt",
                          "orbit", "figure-8", "dive", "gyro-kick", "motor-loss"])
        self.assertEqual({t.tag for t in lib.ordered()}, {"crash", "powerloop", "split-s", "flip", "roll", "barrel-roll", "orbit", "dive", "gyro-kick", "motor-loss"})
        self.assertEqual({lib.segments[t.segment].kind for t in lib.ordered() if not t.phases}, set(segments.KINDS))
        for t in lib.ordered():
            self.assertTrue(t.title and t.why and t.enabled, t.id)
            for e in t.examples:
                self.assertTrue(e["log"].endswith(".bbl") and isinstance(e["arm"], int) and ("at" in e or "from" in e), (t.id, e))

    def test_the_real_data_pins_are_examples(self):
        """The 10-07 truths of tests/test_realdata.py live in the file too: `acroscope tricks check` runs them."""
        lib = Library.load([SHIPPED])
        ex = {(t.id, e["arm"], e.get("at")) for t in lib.ordered() for e in t.examples}
        self.assertIn(("motor-loss", 20, 70.4), ex)
        self.assertIn(("hard-landing", 20, 71.4), ex)
        self.assertIn(("powerloop", 13, 29.0), ex)
        self.assertIn(("roll", 1, 6.5), ex)

    def test_the_precedence_is_in_the_file(self):
        lib = Library.load([SHIPPED])
        self.assertEqual([(r.tags, r.margin, r.within) for r in lib.tricks["crash"].suppress], [(("motor-loss",), None, 0.5)])         # #20: a mid-flight impact swallows no trick
        self.assertEqual([(r.tags, r.margin, r.within) for r in lib.tricks["hard-landing"].suppress],
                         [(("flip", "roll", "barrel-roll", "split-s"), 0, None), (("motor-loss",), None, 0.5)])
        self.assertEqual(lib.tricks["powerloop"].suppress[0].margin, 0)
        self.assertGreater(lib.tricks["split-s"].priority, lib.tricks["roll"].priority)
        self.assertGreater(lib.tricks["roll"].priority, lib.tricks["roll-attempt"].priority)
        self.assertEqual(lib.requires(lib.tricks["motor-loss"]), ["eRPM[0]", "motor[0]"])
        self.assertEqual(lib.requires(lib.tricks["roll"]), [])


class LoadTest(unittest.TestCase):
    def test_a_small_file(self):
        lib = load(BASE)
        self.assertEqual(lib.params, {"flip_min_deg": 270})
        seg = lib.segments["fast"]
        self.assertEqual((seg.kind, seg.opts), ("rotation", {"threshold": 100, "min_dur": 0.3, "gap": 0.3}))     # defaults filled in
        t = lib.tricks["roll"]
        self.assertEqual(t.conditions, ["axis == 'roll'", "abs(rotation_deg.roll) >= flip_min_deg"])
        self.assertEqual((t.tag, t.priority, t.enabled, t.source), ("roll", 10, True, "test.toml"))

    def test_inline_segment(self):
        lib = load(BASE + '''
[[trick]]
id = "bump"
tag = "crash"
segment = { kind = "impact", g = 6 }
title = "bump"
''')
        name = lib.tricks["bump"].segment
        self.assertEqual(lib.segments[name].kind, "impact")
        self.assertEqual(lib.segments[name].opts["g"], 6)
        self.assertEqual(lib.segments[name].opts["jolt_dps"], 500.0)
        self.assertEqual(lib.requires(lib.tricks["bump"]), ["gyroUnfilt[0]"])

    def test_examples_and_requires(self):
        lib = load(BASE + '''
[[trick]]
id = "x"
tag = "x"
segment = "fast"
title = "x"
requires = ["setpoint[0]"]
examples = [{ log = "a.bbl", arm = 3, at = 1.5 }, { log = "a.bbl", arm = 4, from = 1, to = 2, absent = true, note = "n" }]
''')
        t = lib.tricks["x"]
        self.assertEqual(len(t.examples), 2)
        self.assertEqual(lib.requires(t), ["setpoint[0]"])


class RefusalTest(unittest.TestCase):
    def refuse(self, text: str, *needles: str):
        with self.assertRaises(LibraryError) as cm:
            load(text)
        for n in needles:
            self.assertIn(n, str(cm.exception), str(cm.exception))

    def test_unknown_names_are_refused_at_load(self):
        self.refuse(BASE.replace("rotation_deg.roll) >= flip_min_deg", "rotation_deg.rol) >= flip_min_deg"), "trick roll", "unknown name 'rotation_deg.rol'")
        self.refuse(BASE.replace("flip_min_deg\"]", "flip_min_dg\"]"), "unknown name 'flip_min_dg'")
        self.refuse(BASE.replace("{duration}", "{durations}"), "why", "unknown name 'durations'")
        self.refuse(BASE.replace("{direction} roll", "{directon} roll"), "title", "'directon'")
        # a feature of another segment kind is unknown here: impact_g belongs to impact windows
        self.refuse(BASE.replace("\"axis == 'roll'\"", "\"impact_g > 4\""), "unknown name 'impact_g'")

    def test_bad_expressions(self):
        self.refuse(BASE.replace("\"axis == 'roll'\"", "\"axis == 'roll\""), "trick roll")
        self.refuse(BASE.replace("\"axis == 'roll'\"", "\"__import__('os')\""), "trick roll")
        self.refuse(BASE.replace("{direction} roll", "{direction[0]} roll"), "title")

    def test_bad_segments(self):
        self.refuse(BASE.replace("threshold = 100", "treshold = 100"), "segment.fast", "no option 'treshold'", "threshold")
        self.refuse(BASE.replace('kind = "rotation"', 'kind = "rotations"'), "unknown kind 'rotations'")
        self.refuse(BASE.replace('kind = "rotation"\n', ''), "segment.fast", "needs a kind")
        self.refuse(BASE.replace("threshold = 100", 'threshold = "high"'), "must be a number")
        self.refuse(BASE.replace('segment = "fast"', 'segment = "slow"'), "trick roll", "unknown segment 'slow'")
        self.refuse(BASE.replace('segment = "fast"', 'segment = { threshold = 100 }'), "inline segment needs a kind")
        self.refuse(BASE + '\n[segment.pb]\nkind = "sustained"\naxis = "up"\n', "axis must be roll, pitch or yaw")

    def test_bad_tricks(self):
        self.refuse(BASE.replace("when = [", "whenn = ["), "trick roll", "unknown key(s) whenn")
        self.refuse(BASE.replace('tag = "roll"\n', ''), "trick roll", "needs a tag")
        self.refuse(BASE.replace('title = "{direction} roll"\n', ''), "needs a title")
        self.refuse(BASE.replace('id = "roll"\n', ''), "needs an id")
        self.refuse(BASE.replace("when = [\"axis == 'roll'\", \"abs(rotation_deg.roll) >= flip_min_deg\"]", "when = \"axis == 'roll'\""), "list of conditions")
        self.refuse(BASE.replace("priority = 10", "priority = 1.5"), "priority must be an integer")
        self.refuse(BASE.replace("priority = 10", 'enabled = "no"'), "enabled must be true or false")
        self.refuse(BASE.replace("priority = 10", "requires = \"eRPM[0]\""), "requires must be a list")
        self.refuse(BASE.replace("priority = 10", 'suppress = [{ tags = ["flip"], margin = 1, within = 1 }]'), "either margin or within")
        self.refuse(BASE.replace("priority = 10", 'suppress = [{ margin = 1 }]'), "suppress rule")
        self.refuse(BASE.replace("priority = 10", 'suppress = [{ tags = ["flip"], margin = "1" }]'), "margin must be a number")
        self.refuse(BASE.replace("priority = 10", 'examples = [{ log = "a.bbl", arm = 1 }]'), "example has `at`, or `from` and `to`")
        self.refuse(BASE.replace("priority = 10", 'examples = [{ log = "a.bbl", arm = 1, from = 1 }]'), "`from` and `to`")
        self.refuse(BASE.replace("priority = 10", 'examples = [{ log = "a.bbl", arm = "1", at = 2 }]'), "an example is")
        self.refuse(BASE.replace("priority = 10", 'examples = [{ log = "a.bbl", arm = 1, at = 2, when = 3 }]'), "an example is")

    def test_bad_params_and_file(self):
        self.refuse(BASE.replace("flip_min_deg = 270", "duration = 3"), "param 'duration'", "not a feature")
        self.refuse(BASE.replace("flip_min_deg = 270", "\"a.b\" = 3"), "param 'a.b'")
        self.refuse(BASE.replace("flip_min_deg = 270", "flip_min_deg = [1, 2]"), "must be a number")
        self.refuse(BASE.replace("flip_min_deg = 270", 'flip_min_deg = "240"'), "must be a number")      # '240' would crash every comparison
        self.refuse(BASE.replace("flip_min_deg = 270", "flip_min_deg = nan"), "must be a number")
        self.refuse(BASE.replace("flip_min_deg = 270", "flip_min_deg = inf"), "must be a number")
        self.refuse(BASE.replace("version = 1", "version = 2"), "version 2")
        self.refuse("param = 3\n" + BASE, "unknown top-level key 'param'")
        self.refuse(BASE + "\n[[trick]\n", "test.toml")
        self.refuse(BASE.replace("[params]\nflip_min_deg = 270", "params = 3"), "params must be a table")


class MergeTest(unittest.TestCase):
    OVERRIDE = '''
[params]
flip_min_deg = 200
tumble_dps = 1800
[segment.fast]
threshold = 150
[[trick]]
id = "roll"
priority = 5
[[trick]]
id = "flip"
tag = "flip"
segment = "fast"
when = ["axis == 'pitch'", "abs(rotation_deg.pitch) >= flip_min_deg", "peak_rate_dps.pitch <= tumble_dps"]
title = "{direction} flip"
'''

    def merged(self, *texts) -> Library:
        lib = Library()
        for i, text in enumerate(texts):
            lib.add(tomllib.loads(text), f"file{i}.toml")
        lib.validate()
        return lib

    def test_params_segments_and_tricks_merge(self):
        lib = self.merged(BASE, self.OVERRIDE)
        self.assertEqual(lib.params, {"flip_min_deg": 200, "tumble_dps": 1800})
        self.assertEqual(lib.segments["fast"].opts, {"threshold": 150, "min_dur": 0.3, "gap": 0.3})
        self.assertEqual(lib.segments["fast"].kind, "rotation")
        roll = lib.tricks["roll"]
        self.assertEqual(roll.priority, 5)                                      # changed
        self.assertEqual(roll.conditions, ["axis == 'roll'", "abs(rotation_deg.roll) >= flip_min_deg"])     # kept
        self.assertEqual((roll.tag, roll.title, roll.source), ("roll", "{direction} roll", "file1.toml"))
        self.assertEqual([t.id for t in lib.ordered()], ["roll", "flip"])     # a new trick comes after the ones before it
        self.assertEqual(lib.sources, ["file0.toml", "file1.toml"])

    def test_disable(self):
        lib = self.merged(BASE, '[[trick]]\nid = "roll"\nenabled = false\n')
        self.assertFalse(lib.tricks["roll"].enabled)

    def test_a_segment_keeps_its_kind(self):
        with self.assertRaises(LibraryError) as cm:
            self.merged(BASE, '[segment.fast]\nkind = "impact"\n')
        self.assertIn("is a rotation segment, cannot become impact", str(cm.exception))

    def test_an_override_is_checked_against_the_merged_whole(self):
        """A later file that breaks an earlier trick (a param it used taken away is fine, a name it never had is
        not) fails at validate with the trick named."""
        with self.assertRaises(LibraryError) as cm:
            self.merged(BASE, '[[trick]]\nid = "roll"\nwhen = ["abs(rotation_deg.roll) >= min_deg"]\n')
        self.assertIn("trick roll", str(cm.exception))
        self.assertIn("'min_deg'", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
