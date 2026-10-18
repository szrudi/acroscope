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
        t = tricks.edit_layer(t, "on", "gyro-kick")                                 # on is said, not just off taken away
        t = tricks.edit_layer(t, "unset", "flip_min_deg")
        t = tricks.edit_layer(t, "skill", "none")
        self.assertEqual(t, {"version": 1, "trick": [{"id": "gyro-kick", "enabled": True}]})
        t = tricks.edit_layer({"version": 1}, "set", "loop_min_dur", 1.2, craft="OttoJr")
        t = tricks.edit_layer(t, "off", "motor-loss", craft="OttoJr")
        self.assertEqual(t, {"version": 1, "craft": {"OttoJr": {"params": {"loop_min_dur": 1.2}, "trick": [{"id": "motor-loss", "enabled": False}]}}})
        t = tricks.edit_layer(t, "unset", "loop_min_dur", craft="OttoJr")
        self.assertEqual(t, {"version": 1, "craft": {"OttoJr": {"trick": [{"id": "motor-loss", "enabled": False}]}}})
        # the entry's other fields stay; a name not set is refused
        t = tricks.edit_layer({"version": 1, "trick": [{"id": "roll", "priority": 42}, {"id": "my-roll", "tag": "roll", "segment": "fast-rotation", "title": "m"}]}, "off", "roll")
        self.assertEqual(t["trick"][0], {"id": "roll", "priority": 42, "enabled": False})
        t = tricks.edit_layer(t, "off", "my-roll")
        self.assertEqual(t["trick"][1], {"id": "my-roll", "tag": "roll", "segment": "fast-rotation", "title": "m", "enabled": False})
        with self.assertRaises(ValueError):
            tricks.edit_layer({"version": 1}, "unset", "flip_min_dg")
        original = {"version": 1, "params": {"a": 1}}
        tricks.edit_layer(original, "set", "b", 2)
        self.assertEqual(original, {"version": 1, "params": {"a": 1}})           # never in place

    def test_crafts_match_in_any_case(self):
        t = tricks.edit_layer({"version": 1, "craft": {"OttO": {"size_mm": 75}}}, "set", "loop_min_dur", 1.2, craft="otto")
        self.assertEqual(t, {"version": 1, "craft": {"OttO": {"size_mm": 75, "params": {"loop_min_dur": 1.2}}}})      # into the spelling the layer has
        self.assertEqual(tricks.craft_key(t, "OTTO"), "OttO")
        self.assertEqual(tricks.craft_key(t, "AIR65 F"), "AIR65 F")
        m = tricks.merge_tables({"version": 1, "craft": {"OttO": {"size_mm": 75}}}, {"craft": {"otto": {"weight_g": 40}}})
        self.assertEqual(m["craft"], {"OttO": {"size_mm": 75, "weight_g": 40}})

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
        self.assertEqual(roll["params"], ["barrel_off_axis_deg", "contact_g", "flip_min_deg", "held_share", "landing_failures", "landing_window_s", "roll_max_dur", "stick_min", "tumble_dps"])
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
        """One entry per quad whatever the header's case; the declared spelling when a section exists."""
        idx = {"arms": [{"index": 1, "frames": 10, "headers": {"Craft name": "OttO"}}, {"index": 2, "frames": 10, "headers": {"Craft name": "AIR65 F"}},
                        {"index": 3, "frames": 0}, {"index": 4, "frames": 10, "headers": {"Craft name": "Otto"}}]}
        with mock.patch.object(blackbox, "all_bbls", lambda: [Path("a.bbl"), Path("b.bbl")]), mock.patch.object(blackbox, "index_bbl", lambda b, **k: idx if b == "a.bbl" else None):
            d = tricks.describe(tricks.library())
            self.assertEqual(d["crafts_seen"], ["AIR65 F", "OttO"])
            self.assertIsNone(d["craft_section"])
            self.db.set_setting("tricks", {"version": 1, "craft": {"otto": {"size_mm": 75}}})
            d = tricks.describe(tricks.library(), "OTTO")
            self.assertEqual((d["crafts_seen"], d["craft_section"], d["craft_context"]["craft.size_mm"]), (["AIR65 F", "otto"], "otto", 75))


class ShareTest(Layered):
    """A tricks file is the exchange format: the layer out as TOML, a TOML in as the layer, whole or merged."""

    def test_export(self):
        import tomllib
        text = tricks.export_toml({"version": 1, "skill": "beginner", "params": {"flip_min_deg": 240}, "craft": {"AIR65 F": {"size_mm": 65}}}, "from here")
        self.assertTrue(text.startswith("# acroscope tricks, exported 20"))
        self.assertIn(": from here\n", text)
        self.assertIn("import it with `acroscope tricks import`", text)
        self.assertEqual(tomllib.loads(text), {"version": 1, "skill": "beginner", "params": {"flip_min_deg": 240}, "craft": {"AIR65 F": {"size_mm": 65}}})
        self.assertEqual(tomllib.loads(tricks.export_toml(None)), {"version": 1})
        self.assertEqual(tomllib.loads(tricks.export_toml({"params": {"a": 1}})), {"version": 1, "params": {"a": 1}})     # version first, always there

    def test_merge_tables(self):
        base = {"version": 1, "skill": "beginner", "params": {"a": 1, "b": 2}, "trick": [{"id": "roll", "enabled": False}, {"id": "x", "priority": 3}],
                "craft": {"X": {"size_mm": 65, "params": {"c": 3}, "trick": [{"id": "dive", "enabled": False}], "segment": {"impact": {"g": 4}}}}}
        top = {"skill": "advanced", "params": {"b": 20, "d": 4}, "trick": [{"id": "roll", "enabled": True}, {"id": "y"}],
               "craft": {"X": {"weight_g": 30, "params": {"e": 5}, "trick": [{"id": "dive", "priority": 1}], "segment": {"impact": {"jolt_dps": 600}}}, "Y": {"size_mm": 75}}}
        m = tricks.merge_tables(base, top)
        self.assertEqual(m["skill"], "advanced")
        self.assertEqual(m["params"], {"a": 1, "b": 20, "d": 4})
        self.assertEqual(m["trick"], [{"id": "roll", "enabled": True}, {"id": "x", "priority": 3}, {"id": "y"}])
        self.assertEqual(m["craft"]["X"], {"size_mm": 65, "weight_g": 30, "params": {"c": 3, "e": 5}, "trick": [{"id": "dive", "enabled": False, "priority": 1}], "segment": {"impact": {"g": 4, "jolt_dps": 600}}})
        self.assertEqual(m["craft"]["Y"], {"size_mm": 75})
        self.assertEqual(base["params"], {"a": 1, "b": 2})                     # never in place
        self.assertEqual(tricks.merge_tables(None, {"params": {"a": 1}}), {"version": 1, "params": {"a": 1}})
        self.assertEqual(tricks.merge_tables({"version": 1}, None), {"version": 1})

    def test_import_table(self):
        current = {"version": 1, "params": {"flip_min_deg": 240}}
        self.assertEqual(tricks.import_table('version = 1\nskill = "beginner"\n', current, "replace"), {"version": 1, "skill": "beginner"})
        self.assertEqual(tricks.import_table('skill = "beginner"\n', current, "merge"), {"version": 1, "params": {"flip_min_deg": 240}, "skill": "beginner"})
        with self.assertRaises(LibraryError) as cm:
            tricks.import_table("not = [toml", current)
        self.assertIn("not a tricks file", str(cm.exception))
        with self.assertRaises(LibraryError) as cm:
            tricks.import_table("[params]\nflip_min_dg = 1\n", current)
        self.assertIn("flip_min_dg", str(cm.exception))
        with self.assertRaises(ValueError):
            tricks.import_table("version = 1\n", current, "append")

    def test_cli_export_and_import(self):
        import tomllib
        out = io.StringIO()
        with mock.patch.object(blackbox, "load_arm", lambda b, n: ROLL_250), redirect_stdout(out):
            from acroscope import cli
            cli.main(["tricks", "set", "flip_min_deg", "240"])
            cli.main(["tricks", "export"])
        text = out.getvalue().split("\n", 1)[1]
        self.assertEqual(tomllib.loads(text), {"version": 1, "params": {"flip_min_deg": 240}})
        f = Path(self.tmp.name) / "shared.toml"
        f.write_text('version = 1\nskill = "beginner"\n[params]\norbit_min_deg = 240\n')
        out = io.StringIO()
        with mock.patch.object(blackbox, "load_arm", lambda b, n: ROLL_250), redirect_stdout(out):
            cli.main(["tricks", "import", str(f), "--merge"])
            cli.main(["tricks", "export", str(f.with_name("out.toml"))])
        self.assertIn("merged into the pilot's layer: 2 params, 0 trick entries, 0 crafts, skill beginner", out.getvalue())
        self.assertEqual(self.db.setting("tricks")["value"], {"version": 1, "params": {"flip_min_deg": 240, "orbit_min_deg": 240}, "skill": "beginner"})
        self.assertEqual(tomllib.loads(f.with_name("out.toml").read_text()), {"version": 1, "params": {"flip_min_deg": 240, "orbit_min_deg": 240}, "skill": "beginner"})
        out = io.StringIO()
        with mock.patch.object(blackbox, "load_arm", lambda b, n: ROLL_250), redirect_stdout(out):
            cli.main(["tricks", "import", str(f)])
        self.assertIn("replaced the pilot's layer: 1 params", out.getvalue())
        self.assertEqual(self.db.setting("tricks")["value"], {"version": 1, "skill": "beginner", "params": {"orbit_min_deg": 240}})
        f.write_text("[params]\nflip_min_dg = 1\n")
        err = io.StringIO()
        with mock.patch("sys.stderr", err), self.assertRaises(SystemExit):
            cli.main(["tricks", "import", str(f)])
        self.assertIn("flip_min_dg", err.getvalue())
        self.assertEqual(self.db.setting("tricks")["value"], {"version": 1, "skill": "beginner", "params": {"orbit_min_deg": 240}})   # a refused import changes nothing


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
        self.assertEqual(self.db.setting("tricks")["value"], {"version": 1, "trick": [{"id": "roll", "enabled": True}], "craft": {"X": {"params": {"loop_min_dur": 1.2, "flip_min_deg": 200}}}})

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
