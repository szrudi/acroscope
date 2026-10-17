"""Writing a tricks table as TOML (acroscope/tricks/toml_out.py): what tomllib reads back is what went in, for every
shape a tricks file has, including the shipped file's own tables."""
import tomllib
import unittest

from acroscope.tricks import toml_out
from acroscope.tricks.library import SHIPPED

LAYER = {"version": 1, "skill": "beginner", "params": {"flip_min_deg": 240, "held_share": 0.85, "orbit_min_deg": -1, "name": "x"},
         "trick": [{"id": "gyro-kick", "enabled": False},
                   {"id": "hard-roll", "tag": "roll", "segment": "fast-rotation", "when": ["axis == 'roll'", "abs(rotation_deg.roll) >= 330"],
                    "title": "{direction} roll at full rate", "why": 'a "clean" one\\n', "priority": 15,
                    "suppress": [{"tags": ["flip", "roll"], "margin": 1.0}, {"tags": ["motor-loss"], "within": 0.5}],
                    "examples": [{"log": "a.bbl", "arm": 3, "at": 1.5, "note": "it's fine"}, {"log": "a.bbl", "arm": 4, "from": 1, "to": 2, "absent": True}],
                    "requires": ["eRPM[0]"]},
                   {"id": "s", "tag": "s", "title": "s", "phases": [{"segment": "fast-rotation", "when": ["axis == 'roll'"]}, {"segment": "pitch-back", "within": 1.0, "when": []}]}],
         "craft": {"AIR65 F": {"size_mm": 65, "skill": "advanced", "params": {"loop_min_dur": 1.2}, "segment": {"impact": {"g": 5}},
                               "trick": [{"id": "motor-loss", "enabled": False}]},
                   "default": {"hover_pct": 35}}}


class DumpsTest(unittest.TestCase):
    def test_round_trip(self):
        text = toml_out.dumps(LAYER, "acroscope tricks\nexported")
        self.assertTrue(text.startswith("# acroscope tricks\n# exported\n"))
        self.assertEqual(tomllib.loads(text), LAYER)

    def test_shapes_read_as_a_tricks_file(self):
        text = toml_out.dumps(LAYER)
        self.assertIn('\n[[trick]]\nid = "gyro-kick"\nenabled = false\n', text)
        self.assertIn('[craft."AIR65 F"]', text)
        self.assertIn('[craft."AIR65 F".segment.impact]\ng = 5', text)
        self.assertIn('[[craft."AIR65 F".trick]]', text)
        self.assertIn('suppress = [\n  { tags = ["flip", "roll"], margin = 1.0 },', text)
        self.assertIn('when = ["axis == \'roll\'", "abs(rotation_deg.roll) >= 330"]', text)

    def test_the_shipped_file_round_trips(self):
        table = tomllib.loads(SHIPPED.read_text())
        self.assertEqual(tomllib.loads(toml_out.dumps(table)), table)

    def test_edge_values(self):
        for t in ({"version": 1}, {}, {"a": {"b": {"c": 1}}}, {"params": {}}, {"x": [1, 2.5, "s", True]}, {"q": 'he said "hi"\t\\ ok'}, {"k e y": 1, "dotted.key": 2},
                  {"n": "two\nlines\r\n"}, {"empty": []}, {"del": "a\x7fb"}, {"trick": [{"id": "x", "when": []}]}):
            self.assertEqual(tomllib.loads(toml_out.dumps(t)), t, t)
        with self.assertRaises(ValueError):
            toml_out.dumps({"x": float("nan")})
        with self.assertRaises(ValueError):
            toml_out.dumps({"x": None})
        self.assertEqual(tomllib.loads(toml_out.dumps({"trick": [{"id": "a", "sub": [{"deep": 1}]}]})), {"trick": [{"id": "a", "sub": [{"deep": 1}]}]})


if __name__ == "__main__":
    unittest.main()
