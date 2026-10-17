"""Profiles over the shipped tricks (#22 step 3): the pilot's layer as a second table over the shipped one (skill
presets, own params, craft sections keyed by the log header's Craft name), each tried on a scripted arm."""
import io
import os
import tempfile
import tomllib
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from acroscope import blackbox, sessions, tricks
from acroscope.tricks.library import SHIPPED, Library, LibraryError

try:
    from tests.synthetic import axis, make_arm, pulse
except ImportError:
    from synthetic import axis, make_arm, pulse

ROLL_250 = make_arm(5, gyro=axis(0, pulse(2.0, 2.5, 500)))                    # 250 deg: a roll only to a beginner, else an attempt
ATTEMPT = ["right roll attempt, 249 deg"]
ROLL_250_X = make_arm(5, gyro=axis(0, pulse(2.0, 2.5, 500)), craft="X")
ROLL_360 = make_arm(5, gyro=axis(0, pulse(2.0, 2.72, 500)))


def lib_with(user: str) -> Library:
    """The shipped file with a pilot's text on top."""
    lib = Library()
    lib.add(tomllib.loads(SHIPPED.read_text()), "shipped")
    lib.add(tomllib.loads(user), "user")
    lib.validate()
    return lib


def titles(arm, lib):
    return [e["title"] for e in tricks.detect(arm, lib)]


class PresetTest(unittest.TestCase):
    def test_a_preset_loosens_the_numbers(self):
        self.assertEqual(titles(ROLL_250, lib_with("")), ATTEMPT)
        self.assertEqual(titles(ROLL_250, lib_with('skill = "beginner"')), ["right roll"])
        self.assertEqual(titles(ROLL_360, lib_with('skill = "advanced"')), ["right roll"])
        self.assertEqual(lib_with('skill = "beginner"').for_craft(None).params["flip_min_deg"], 240)

    def test_the_pilots_own_params_win_over_the_preset(self):
        self.assertEqual(titles(ROLL_250, lib_with('skill = "beginner"\n[params]\nflip_min_deg = 300\n')), ATTEMPT)
        self.assertEqual(titles(ROLL_360, lib_with('skill = "advanced"\n[params]\nflip_min_deg = 400\n')), ["right roll attempt, 359 deg"])

    def test_a_preset_per_craft(self):
        lib = lib_with('[craft."X"]\nskill = "beginner"\n')
        self.assertEqual(titles(ROLL_250_X, lib), ["right roll"])
        self.assertEqual(titles(ROLL_250, lib), ATTEMPT)
        self.assertEqual(lib.for_craft("X").params["flip_min_deg"], 240)
        self.assertEqual(lib.for_craft("Y").params["flip_min_deg"], 270)      # a craft without a section: the default layer

    def test_an_unknown_preset_is_refused(self):
        for text in ('skill = "pro"', '[craft."X"]\nskill = "pro"\n'):
            with self.assertRaises(LibraryError) as cm:
                lib_with(text)
            self.assertIn("'pro': no such preset", str(cm.exception))
        with self.assertRaises(LibraryError):
            lib_with("skill = 3")
        with self.assertRaises(LibraryError):
            Library.from_text('[skill]\nbeginner = 3\n')


class CraftTest(unittest.TestCase):
    def test_a_crafts_params_segments_and_tricks(self):
        params = lib_with('[craft."X".params]\nflip_min_deg = 200\n')
        self.assertEqual((titles(ROLL_250_X, params), titles(ROLL_250, params)), (["right roll"], ATTEMPT))
        segment = lib_with('[craft."X".segment.fast-rotation]\nthreshold = 600\n')              # the 500 dps roll cuts no window
        self.assertEqual((titles(make_arm(5, gyro=axis(0, pulse(2.0, 2.72, 500)), craft="X"), segment), titles(ROLL_360, segment)), ([], ["right roll"]))
        off = lib_with('[[craft."X".trick]]\nid = "roll"\nenabled = false\n')
        self.assertEqual((titles(make_arm(5, gyro=axis(0, pulse(2.0, 2.72, 500)), craft="X"), off), titles(ROLL_360, off)), ([], ["right roll"]))

    def test_craft_properties_are_features(self):
        lib = lib_with('''
[craft."X"]
size_mm = 75
[[trick]]
id = "big-roll"
tag = "roll"
segment = "fast-rotation"
when = ["axis == 'roll'", "abs(rotation_deg.roll) >= 200", "craft.size_mm >= 70", "craft.name == 'X'"]
title = "a {craft.size_mm} mm roll"
priority = 20
''')
        self.assertEqual(titles(ROLL_250_X, lib), ["a 75 mm roll"])
        self.assertEqual(titles(ROLL_250, lib), ATTEMPT)                                      # the default craft is 65 mm
        self.assertEqual(lib.craft_context("X"), {"craft.size_mm": 75, "craft.weight_g": 25, "craft.hover_pct": 35, "craft.name": "X"})
        self.assertEqual(lib.craft_context(None)["craft.size_mm"], 65)

    def test_the_default_craft_under_a_named_one(self):
        lib = lib_with('[craft.default]\nweight_g = 30\n[craft.default.params]\nflip_min_deg = 200\n[craft."X"]\nsize_mm = 75\n')
        self.assertEqual(lib.craft_context("X"), {"craft.size_mm": 75, "craft.weight_g": 30, "craft.hover_pct": 35, "craft.name": "X"})
        self.assertEqual(lib.for_craft("X").params["flip_min_deg"], 200)
        self.assertEqual(titles(ROLL_250_X, lib), ["right roll"])
        self.assertEqual(titles(ROLL_250, lib), ["right roll"])
        # the named craft's own params, segments and tricks go over the default's
        lib = lib_with('[craft.default.params]\nflip_min_deg = 200\n[craft."X".params]\nflip_min_deg = 300\n')
        self.assertEqual((titles(ROLL_250_X, lib), titles(ROLL_250, lib)), (ATTEMPT, ["right roll"]))
        lib = lib_with('[[craft.default.trick]]\nid = "roll"\nenabled = false\n[craft."X"]\nsize_mm = 75\n')
        self.assertEqual((titles(ROLL_250_X, lib_with('skill = "beginner"\n[[craft.default.trick]]\nid = "roll"\nenabled = false\n[craft."X"]\nsize_mm = 75\n')),
                          titles(ROLL_360, lib)), ([], []))
        lib = lib_with('[craft.default.segment.fast-rotation]\nthreshold = 600\n[craft."X"]\nsize_mm = 75\n')
        self.assertEqual(titles(make_arm(5, gyro=axis(0, pulse(2.0, 2.72, 500)), craft="X"), lib), [])

    def test_craft_names_are_checked_and_sections_shaped(self):
        with self.assertRaises(LibraryError) as cm:
            lib_with('[[trick]]\nid = "x"\ntag = "x"\nsegment = "fast-rotation"\nwhen = ["craft.sizee > 1"]\ntitle = "x"\n')
        self.assertIn("'craft.sizee'", str(cm.exception))
        for bad, needle in (('[craft."X"]\nparams = 3\n', "craft.X.params must be a table"),
                            ('[craft."X"]\nsegment = 3\n', "craft.X.segment"),
                            ('[craft."X"]\ntrick = 3\n', "craft.X.trick"),
                            ('[craft."X"]\nskill = 3\n', "craft.X.skill"),
                            ('[craft."X"]\nsize = [1]\n', "a craft property is a number or a string"),
                            ('craft = 3\n', "craft must be a table"),
                            ('[[craft."X".trick]]\nid = "roll"\nwhenn = []\n', "unknown key(s) whenn"),
                            ('[craft."X".segment.fast-rotation]\ntreshold = 1\n', "no option 'treshold'")):
            with self.assertRaises(LibraryError, msg=bad) as cm:
                lib_with(bad)
            self.assertIn(needle, str(cm.exception))

    def test_a_craft_name_matches_in_any_case(self):
        """Betaflight's craft name was `OttO` one day and `Otto` the next: one quad, one section."""
        lib = lib_with('[craft."OttO"]\nsize_mm = 75\n[craft."OttO".params]\nflip_min_deg = 200\n[[craft."OttO".trick]]\nid = "gyro-kick"\nenabled = false\n')
        for name in ("OttO", "Otto", "otto", "OTTO"):
            self.assertEqual(lib.craft_name(name), "OttO")
            self.assertEqual(lib.for_craft(name).params["flip_min_deg"], 200, name)
            self.assertFalse(lib.for_craft(name).tricks["gyro-kick"].enabled, name)
            self.assertEqual(lib.craft_context(name)["craft.size_mm"], 75, name)
            self.assertEqual(lib.param_sources(name)["flip_min_deg"], "craft OttO", name)
            self.assertIs(lib.for_craft(name), lib.for_craft("OttO"))
        self.assertEqual(titles(make_arm(5, gyro=axis(0, pulse(2.0, 2.5, 500)), craft="otto"), lib), ["right roll"])
        self.assertEqual(lib.craft_name("Y"), "Y")
        # two spellings in the layers are one section, the first spelling kept
        lib = lib_with('[craft."OttO"]\nsize_mm = 75\n[craft.otto]\nweight_g = 40\n[craft.otto.params]\nloop_min_dur = 1.2\n')
        self.assertEqual(list(c for c in lib.crafts if c != "default"), ["OttO"])
        self.assertEqual((lib.craft_context("OTTO")["craft.size_mm"], lib.craft_context("OTTO")["craft.weight_g"], lib.for_craft("otto").params["loop_min_dur"]), (75, 40, 1.2))

    def test_for_craft_is_derived_once(self):
        lib = lib_with('[craft."X"]\nsize_mm = 75\n')
        self.assertIs(lib.for_craft("X"), lib.for_craft("X"))
        self.assertIs(lib.for_craft("Y"), lib.for_craft(None))
        self.assertIsNot(lib.for_craft("X"), lib.for_craft(None))


class CliTest(unittest.TestCase):
    def cli(self, user: str, *args) -> str:
        """The CLI with `user` (TOML) as the pilot's layer, handed in as the database would."""
        import tomllib
        out = io.StringIO()
        table = tomllib.loads(user)
        with mock.patch.object(sessions, "SERVER_URL", ""), mock.patch.object(tricks, "_cache", None), \
                mock.patch.object(tricks, "LAYER_SOURCE", lambda: (table, "t") if table else (None, None)), \
                mock.patch.object(blackbox, "load_arm", lambda b, n: ROLL_250_X), redirect_stdout(out):
            from acroscope import cli
            cli.main(list(args))
        return out.getvalue()

    def test_list_shows_the_layers_in_force(self):
        text = self.cli('skill = "beginner"\n[craft."X"]\nsize_mm = 75\n[craft."X".params]\nflip_min_deg = 200\n', "tricks")
        self.assertIn("# skill: beginner; presets: beginner, advanced; crafts: X", text)
        self.assertIn("flip_min_deg=240", text)                                         # the preset, in force
        self.assertIn("craft: craft.hover_pct=35, craft.size_mm=65, craft.weight_g=25", text)
        for_x = self.cli('skill = "beginner"\n[craft."X"]\nsize_mm = 75\n[craft."X".params]\nflip_min_deg = 200\n', "tricks", "--craft", "X")
        self.assertIn("showing X", for_x)
        self.assertIn("flip_min_deg=200", for_x)
        self.assertIn("craft.size_mm=75", for_x)
        self.assertIn("craft.name=X", for_x)
        text = self.cli("", "tricks")
        self.assertIn("# skill: none (the defaults)", text)

    def test_explain_uses_the_arms_craft(self):
        text = self.cli('[craft."X".params]\nflip_min_deg = 200\n', "tricks", "explain", "SYNTH.bbl:1")
        self.assertRegex(text, r"\broll\s+fired")
        text = self.cli("", "tricks", "explain", "SYNTH.bbl:1")
        self.assertRegex(text, r"\broll\s+failed: abs\(rotation_peak_deg.roll\) >= flip_min_deg")


if __name__ == "__main__":
    unittest.main()
