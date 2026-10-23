"""Tag definitions and categories in the database (red first, then green)."""
import tempfile
import unittest
from pathlib import Path

from acroscope.store.db import Db


class TagsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Db(Path(self.tmp.name) / "t.db")

    def tearDown(self):
        self.tmp.cleanup()

    def test_seeded(self):
        t = self.db.tags()
        self.assertEqual([c["name"] for c in t["categories"]], ["tricks", "milestones", "poi", "technical"])
        by = {x["name"]: x for x in t["tags"]}
        self.assertEqual(by["flip"]["category"], "tricks")
        self.assertEqual(by["milestone"]["category"], "milestones")
        self.assertEqual(by["crash"]["category"], "technical")
        self.assertEqual(by["poi"]["category"], "poi")
        self.assertTrue(all(c["color"].startswith("#") for c in t["categories"]))
        self.assertEqual([x["name"] for x in t["tags"]][:3], ["flip", "roll", "powerloop"])   # kept in order

    def test_define_and_move(self):
        self.db.set_category("cruise", "#00ff00")
        self.db.set_tag("hover", "cruise")
        self.db.set_tag("flip", "cruise", color="#123456")          # moved and coloured
        t = self.db.tags()
        by = {x["name"]: x for x in t["tags"]}
        self.assertEqual((by["hover"]["category"], by["hover"]["color"]), ("cruise", None))
        self.assertEqual((by["flip"]["category"], by["flip"]["color"]), ("cruise", "#123456"))
        self.assertEqual(t["categories"][-1], {"name": "cruise", "color": "#00ff00", "pos": 4})
        self.db.set_category("cruise", "#0f0f0f")                   # colour change keeps the position
        self.assertEqual(self.db.tags()["categories"][-1]["pos"], 4)
        with self.assertRaises(ValueError):
            self.db.set_tag("x", "no-such-category")

    def test_colours_are_colours(self):
        """The player puts a colour into an attribute and a style: anything but #rrggbb is refused (it would be markup)."""
        for bad in ['"><img src=x onerror=alert(1)>', "red", "#12345", "#1234567", "#12345g", "url(x)"]:
            with self.subTest(bad):
                with self.assertRaises(ValueError):
                    self.db.set_category("c-" + str(len(bad)), bad)
                with self.assertRaises(ValueError):
                    self.db.set_tag("flip", "tricks", bad)
        self.db.set_category("cruise", "#A0b1C2")
        self.db.set_tag("hover", "cruise", "#00ff00")
        self.db.set_tag("hover", "cruise", "")                      # '' still clears a tag's own colour
        self.assertIsNone({x["name"]: x for x in self.db.tags()["tags"]}["hover"]["color"])

    def test_delete(self):
        self.db.set_category("c1", "#111111")
        self.db.set_tag("t1", "c1")
        self.assertTrue(self.db.delete_tag("t1"))
        self.assertFalse(self.db.delete_tag("t1"))
        self.db.set_tag("t2", "c1")
        with self.assertRaises(ValueError):                         # a category in use stays
            self.db.delete_category("c1")
        self.db.delete_tag("t2")
        self.assertTrue(self.db.delete_category("c1"))

    def test_usage(self):
        self.db.tag("2026-10-07-s", "a.mp4", 0, 1, "x", ["flip", "crash", "custom"])
        self.db.tag("2026-10-09-s", "b.mp4", 0, 1, "y", ["flip"])
        u = self.db.tag_usage()
        self.assertEqual(u, {"flip": 2, "crash": 1, "custom": 1})
        self.assertEqual(self.db.tag_usage("2026-10-09-s"), {"flip": 1})


if __name__ == "__main__":
    unittest.main()
