"""The pilot's layer in the database (#45): one more layer of the library, handed in through tricks.LAYER_SOURCE;
describe() as the configuration page reads it; the CLI's set/unset/on/off/skill writes, checked before they land."""
import io
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from acroscope import blackbox, sessions, tricks
from acroscope.db import Db
from acroscope.tricks.library import SHIPPED, LibraryError

try:
    from tests.synthetic import axis, make_arm, pulse
except ImportError:
    from synthetic import axis, make_arm, pulse

ROLL_250 = make_arm(5, gyro=axis(0, pulse(2.0, 2.5, 500)))
ROLL_250_X = make_arm(5, gyro=axis(0, pulse(2.0, 2.5, 500)), craft="X")


class Layered(unittest.TestCase):
    """A scratch database as the store and the layer source, no pilot's file."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Db(Path(self.tmp.name) / "t.db")
        self.table, self.stamp = None, None

        def source():
            row = self.db.setting(tricks.LAYER_KEY)
            return (row["value"], row["updated_at"]) if row else (None, None)
        for p in (mock.patch.object(sessions, "_store", self.db), mock.patch.object(sessions, "SERVER_URL", ""),
                  mock.patch.object(tricks, "_cache", None), mock.patch.object(tricks, "LAYER_SOURCE", source),
                  mock.patch.dict(os.environ, {"ACROSCOPE_TRICKS": str(Path(self.tmp.name) / "none.toml")}),
                  mock.patch.object(blackbox, "all_bbls", lambda: [])):
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(self.tmp.cleanup)

    def titles(self, arm):
        return [e["title"] for e in tricks.detect(arm, tricks.library())]


class LayerTest(Layered):
    def test_the_database_is_a_layer(self):
        self.assertEqual(tricks.library().sources, [str(SHIPPED)])
        self.assertEqual(self.titles(ROLL_250), ["right roll attempt, 249 deg"])
        self.db.set_setting("tricks", {"version": 1, "skill": "beginner", "params": {"orbit_min_deg": 200}})
        lib = tricks.library()                                                   # the stamp changed: loaded again
        self.assertEqual(lib.sources, [str(SHIPPED), "database"])
        self.assertEqual((lib.skill, lib.params["orbit_min_deg"]), ("beginner", 200))
        self.assertEqual(self.titles(ROLL_250), ["right roll"])
        self.assertIs(tricks.library(), lib)                                     # and once only
        self.db.delete_setting("tricks")
        self.assertEqual(tricks.library().sources, [str(SHIPPED)])
        self.assertEqual(self.titles(ROLL_250), ["right roll attempt, 249 deg"])

    def test_without_a_source_the_package_opens_nothing(self):
        with mock.patch.object(tricks, "LAYER_SOURCE", None), mock.patch.object(tricks, "_cache", None):
            self.assertEqual(tricks.library().sources, [str(SHIPPED)])
            self.assertEqual(tricks.database_layer(), (None, None))

    def test_check_layer_refuses_what_a_file_would(self):
        for bad, needle in (({"version": 1, "params": {"flip_min_dg": 1}}, "flip_min_dg"),
                            ({"version": 1, "skill": "pro"}, "no such preset"),
                            ({"version": 1, "trick": [{"id": "roll", "whenn": []}]}, "unknown key(s) whenn"),
                            ({"version": 2}, "version 2"),
                            ({"version": 1, "craft": {"X": {"segment": {"impact": {"g": "high"}}}}}, "must be a number")):
            with self.assertRaises(LibraryError, msg=bad) as cm:
                tricks.check_layer(bad)
            self.assertIn(needle, str(cm.exception))
            self.assertIn("database", str(cm.exception))
        self.assertEqual(tricks.check_layer({"version": 1}).sources, [str(SHIPPED), "database"])

    def test_edit_layer(self):
        t = tricks.edit_layer(None, "set", "flip_min_deg", 240)
        self.assertEqual(t, {"version": 1, "params": {"flip_min_deg": 240}})
        t = tricks.edit_layer(t, "off", "gyro-kick")
        t = tricks.edit_layer(t, "skill", "beginner")
        self.assertEqual(t, {"version": 1, "params": {"flip_min_deg": 240}, "trick": [{"id": "gyro-kick", "enabled": False}], "skill": "beginner"})
        t = tricks.edit_layer(t, "on", "gyro-kick")
        t = tricks.edit_layer(t, "unset", "flip_min_deg")
        t = tricks.edit_layer(t, "skill", "none")
        self.assertEqual(t, {"version": 1})
        t = tricks.edit_layer(t, "set", "loop_min_dur", 1.2, craft="OttoJr")
        t = tricks.edit_layer(t, "off", "motor-loss", craft="OttoJr")
        self.assertEqual(t, {"version": 1, "craft": {"OttoJr": {"params": {"loop_min_dur": 1.2}, "trick": [{"id": "motor-loss", "enabled": False}]}}})
        t = tricks.edit_layer(t, "on", "motor-loss", craft="OttoJr")
        t = tricks.edit_layer(t, "unset", "loop_min_dur", craft="OttoJr")
        self.assertEqual(t, {"version": 1})                                      # an emptied craft section goes
        original = {"version": 1, "params": {"a": 1}}
        tricks.edit_layer(original, "set", "b", 2)
        self.assertEqual(original, {"version": 1, "params": {"a": 1}})           # never in place

    def test_parse_value(self):
        self.assertEqual([tricks.parse_value(x) for x in ("240", "-50", "0.85", "true", "false", "beginner")], [240, -50, 0.85, True, False, "beginner"])


class DescribeTest(Layered):
    def test_describe(self):
        self.db.set_setting("tricks", {"version": 1, "skill": "beginner", "params": {"orbit_min_deg": 200},
                                       "craft": {"OttoJr": {"size_mm": 75, "params": {"loop_min_dur": 1.2}, "trick": [{"id": "motor-loss", "enabled": False}]}}})
        d = tricks.describe(tricks.library())
        self.assertEqual((d["skill"], d["database"], d["craft"]), ("beginner", True, None))
        self.assertEqual(d["params"]["flip_min_deg"], {"value": 240, "default": 270, "source": "preset beginner"})
        self.assertEqual(d["params"]["orbit_min_deg"], {"value": 200, "default": 300, "source": "database"})
        self.assertEqual(d["params"]["tumble_dps"], {"value": 1500, "default": 1500, "source": str(SHIPPED)})
        self.assertEqual(d["params"]["loop_min_dur"]["value"], 1.5)
        self.assertEqual(sorted(d["skills"]), ["advanced", "beginner"])
        self.assertEqual(d["crafts"]["OttoJr"]["size_mm"], 75)
        self.assertEqual(d["crafts"]["OttoJr"]["source"], "database")
        self.assertEqual(d["crafts"]["default"]["hover_pct"], 35)
        roll = next(t for t in d["tricks"] if t["id"] == "roll")
        self.assertEqual(roll["params"], ["barrel_off_axis_deg", "flip_min_deg", "held_share", "roll_max_dur", "stick_min"])
        self.assertEqual(roll["source"], str(SHIPPED))
        self.assertTrue(roll["enabled"])
        split = next(t for t in d["tricks"] if t["id"] == "split-s")
        self.assertEqual([ph["segment"] for ph in split["phases"]], ["fast-rotation", "pitch-back"])
        self.assertIsNone(split["segment"])
        self.assertIn("rotation_deg.roll", d["features"]["rotation"])
        self.assertIn("turn_deg", d["features"]["turn"])
        # for one craft: its layer in force
        d = tricks.describe(tricks.library(), "OttoJr")
        self.assertEqual(d["params"]["loop_min_dur"], {"value": 1.2, "default": 1.5, "source": "craft OttoJr"})
        self.assertFalse(next(t for t in d["tricks"] if t["id"] == "motor-loss")["enabled"])
        self.assertEqual(d["craft_context"]["craft.size_mm"], 75)

    def test_crafts_seen_in_the_logs(self):
        idx = {"arms": [{"index": 1, "frames": 10, "headers": {"Craft name": "OttO"}}, {"index": 2, "frames": 10, "headers": {"Craft name": "AIR65 F"}}, {"index": 3, "frames": 0}]}
        with mock.patch.object(blackbox, "all_bbls", lambda: [Path("a.bbl"), Path("b.bbl")]), mock.patch.object(blackbox, "index_bbl", lambda b, **k: idx if b == "a.bbl" else None):
            self.assertEqual(tricks.describe(tricks.library())["crafts_seen"], ["AIR65 F", "OttO"])


class CliWriteTest(Layered):
    def cli(self, *args) -> str:
        out = io.StringIO()
        with mock.patch.object(blackbox, "load_arm", lambda b, n: ROLL_250_X if n == 2 else ROLL_250), redirect_stdout(out):
            from acroscope import cli
            cli.main(list(args))
        return out.getvalue()

    def test_set_unset_on_off_skill(self):
        self.assertIn("no pilot's layer", self.cli("tricks", "layer"))
        self.assertEqual(self.cli("tricks", "set", "flip_min_deg", "240").strip(), "flip_min_deg = 240")
        self.assertEqual(self.db.setting("tricks")["value"], {"version": 1, "params": {"flip_min_deg": 240}})
        self.assertIn("right roll", self.cli("events", "--arm", "x.bbl:1"))
        self.assertIn("flip_min_deg=240", self.cli("tricks"))
        self.assertEqual(self.cli("tricks", "off", "roll").strip(), "roll: off")
        self.assertNotIn("roll", self.cli("events", "--arm", "x.bbl:1"))              # a 249 deg roll is a roll at 240: off, nothing
        self.assertEqual(self.cli("tricks", "unset", "flip_min_deg").strip(), "flip_min_deg = 270")
        self.assertIn("roll attempt", self.cli("events", "--arm", "x.bbl:1"))         # at 270 it is an attempt, which stays on
        self.assertEqual(self.cli("tricks", "on", "roll").strip(), "roll: on")
        self.assertEqual(self.cli("tricks", "skill", "beginner").strip(), "skill: beginner")
        self.assertIn("# skill: beginner", self.cli("tricks"))
        self.assertEqual(self.cli("tricks", "skill", "none").strip(), "skill: None")
        self.assertIn("the pilot's layer in the database", self.cli("tricks", "layer"))
        self.assertEqual(self.cli("tricks", "set", "loop_min_dur", "1.2", "--craft", "X").strip(), "loop_min_dur = 1.2 for X")
        self.assertEqual(self.cli("tricks", "set", "flip_min_deg", "200", "--craft", "X").strip(), "flip_min_deg = 200 for X")
        self.assertIn("right roll", self.cli("events", "--arm", "x.bbl:2"))           # arm 2 is craft X
        self.assertIn("roll attempt", self.cli("events", "--arm", "x.bbl:1"))
        self.assertEqual(self.db.setting("tricks")["value"], {"version": 1, "craft": {"X": {"params": {"loop_min_dur": 1.2, "flip_min_deg": 200}}}})

    def test_a_bad_write_changes_nothing(self):
        self.cli("tricks", "set", "flip_min_deg", "240")
        before = self.db.setting("tricks")
        for args, needle in ((("tricks", "set", "flip_min_dg", "240"), "flip_min_dg"), (("tricks", "skill", "pro"), "no such preset"),
                             (("tricks", "off", "nope"), "nope"), (("tricks", "set", "crash_g", "x", "--craft", "Y"), "crash_g")):
            err = io.StringIO()
            with mock.patch("sys.stderr", err), self.assertRaises(SystemExit):
                self.cli(*args)
            self.assertIn(needle, err.getvalue(), args)
        self.assertEqual(self.db.setting("tricks"), before)
        with self.assertRaises(SystemExit):
            self.cli("tricks", "set", "flip_min_deg")                                 # the usage line, nothing written
        self.assertEqual(self.db.setting("tricks"), before)


if __name__ == "__main__":
    unittest.main()
