"""The database on its own: a temp file, no data dir. Run: .venv/bin/python -m unittest discover tests"""
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from acroscope.db import Db


class DbTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Db(Path(self.tmp.name) / "t.db")

    def tearDown(self):
        self.tmp.cleanup()

    def test_empty_shape(self):
        s = self.db.load("2026-10-07-x")
        self.assertEqual(s, {"session": "2026-10-07-x", "date": "2026-10-07", "note": "", "videos": [], "blackbox": [], "matches": [], "moments": []})
        self.assertFalse(self.db.exists("2026-10-07-x"))

    def test_clips_matches_moments(self):
        S = "2026-10-07-s"
        self.db.upsert_video(S, "2026-10-07_001.mp4", 10.5, "h264")
        self.db.upsert_video(S, "2026-10-07_001.mp4")            # a re-scan without a probe keeps the probe
        self.db.set_match(S, "2026-10-07_001.mp4", "a.bbl", 3, 1.234, "osd")
        self.db.set_match(S, "2026-10-07_001.mp4", "a.bbl", 3, 2.254)  # same key: replaced, not duplicated; hundredths kept
        m1 = self.db.tag(S, "2026-10-07_001.mp4", 1, 4, "flip", ["flip"], metrics={"x": 1})
        m2 = self.db.tag(S, "2026-10-07_001.mp4", 0.5, 2, "earlier", [], note="first try")
        self.db.tag(S, "2026-10-07_001.mp4", 1, 5, "flip edited", ["flip", "roll"], mid=m1["id"])
        s = self.db.load(S)
        self.assertEqual(s["videos"], [{"file": "2026-10-07_001.mp4", "name": "2026-10-07_001", "duration": 10.5, "codec": "h264", "note": ""}])
        self.assertEqual(s["blackbox"], ["a.bbl"])                   # attached by the match
        self.assertEqual(s["matches"], [{"video": "2026-10-07_001.mp4", "bbl": "a.bbl", "arm": 3, "offset": 2.25, "note": ""}])
        self.assertEqual([m["id"] for m in s["moments"]], [m2["id"], m1["id"]])   # sorted by start
        self.assertEqual(s["moments"][0]["note"], "first try")
        self.assertEqual(s["moments"][1]["title"], "flip edited")
        self.assertEqual(s["moments"][1]["tags"], ["flip", "roll"])
        self.assertEqual(s["moments"][1]["metrics"], {"x": 1})
        self.assertEqual(self.db.next_id(S), "m03")
        self.assertTrue(self.db.untag(S, m2["id"]))
        self.assertFalse(self.db.untag(S, m2["id"]))
        self.assertEqual(self.db.unmatch(S, "2026-10-07_001.mp4"), 1)
        self.assertEqual(self.db.sessions()[0]["moments"], 1)

    def test_names_and_fields(self):
        S = "2026-10-07-s"
        self.db.upsert_video(S, "c0ffee0001.mp4", name="2026-10-07_003")      # an ingested clip: id on disk, readable name
        self.db.upsert_video(S, "c0ffee0001.mp4", 12.0, "h264")              # a later probe keeps the name
        self.db.set_video_fields(S, "c0ffee0001.mp4", cuts=[[0, 5.0], [9.5, 12.0]], original="originals/x.mov", original_until="2026-10-17")
        v = self.db.load(S)["videos"][0]
        self.assertEqual((v["name"], v["duration"], v["cuts"], v["original"]), ("2026-10-07_003", 12.0, [[0, 5.0], [9.5, 12.0]], "originals/x.mov"))
        self.db.upsert_video("2026-10-07-other", "2026-10-07_004.mp4")
        self.assertEqual(sorted(self.db.names_on("2026-10-07")), ["2026-10-07_003", "2026-10-07_004"])

    def test_missing_and_purge(self):
        S = "2026-10-09-s"
        old = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
        self.db.upsert_video(S, "a.mp4", 1, "h264", missing_since=old)
        self.db.upsert_video(S, "b.mp4", 1, "h264", missing_since=datetime.now(timezone.utc).isoformat())
        self.db.upsert_video(S, "c.mp4", 1, "h264")
        self.db.upsert_video(S, "d.mp4", 1, "h264", missing_since=(datetime.now(timezone.utc) - timedelta(days=7, seconds=60)).isoformat())
        self.db.upsert_video(S, "e.mp4", 1, "h264", missing_since=(datetime.now(timezone.utc) - timedelta(days=7) + timedelta(hours=1)).isoformat())
        self.db.tag(S, "a.mp4", 0, 1, "kept until purged", [])
        self.assertEqual(self.db.sessions()[0]["missing"], 4)
        gone = self.db.purge(S, days=7)                                 # a and d are past 7 days, e is an hour short of it
        self.assertEqual([(r["video"], r["missing_since"], r["moments"]) for r in gone], [("a.mp4", old, 1), ("d.mp4", gone[1]["missing_since"], 0)])
        self.assertEqual([v["file"] for v in self.db.load(S)["videos"]], ["b.mp4", "c.mp4", "e.mp4"])
        self.assertEqual(self.db.purge(S, days=7), [])                # b is too recent
        self.assertEqual(len(self.db.purge(S, video="b.mp4")), 1)    # named explicitly: removed regardless
        self.db.upsert_video(S, "c.mp4", missing_since=None)          # back: mark cleared
        self.assertNotIn("missing_since", self.db.load(S)["videos"][0])

    def test_import_json_idempotent(self):
        p = Path(self.tmp.name) / "session.json"
        p.write_text(json.dumps({"session": "2026-10-07-s", "date": "2026-10-07", "note": "n",
                                 "videos": [{"file": "x.mp4", "duration": 3.0, "codec": "hevc", "note": "vn"}],
                                 "blackbox": ["z.bbl"], "matches": [{"video": "x.mp4", "bbl": "z.bbl", "arm": 1, "offset": 5.0, "note": ""}],
                                 "moments": [{"id": "m07", "video": "x.mp4", "start": 1, "end": 2, "title": "t", "tags": ["poi"], "note": ""}]}))
        self.db.import_json(p)
        self.db.import_json(p)
        s = self.db.load("2026-10-07-s")
        self.assertEqual((s["note"], s["videos"][0]["note"], s["blackbox"], len(s["matches"]), [m["id"] for m in s["moments"]]),
                         ("n", "vn", ["z.bbl"], 1, ["m07"]))
        self.assertEqual(self.db.next_id("2026-10-07-s"), "m08")


class EditTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Db(Path(self.tmp.name) / "t.db")

    def tearDown(self):
        self.tmp.cleanup()

    def test_rename_move_delete(self):
        A, B = "2026-10-11-a", "2026-10-11-b"
        self.db.upsert_video(A, "c1.mp4", name="2026-10-11_001")
        self.db.upsert_video(A, "c2.mp4", name="2026-10-11_002")
        self.db.attach(A, "x.bbl")
        self.db.set_match(A, "c1.mp4", "x.bbl", 1, 2.0)
        self.db.tag(A, "c1.mp4", 0, 1, "m", ["flip"])
        self.db.set_note(A, "note a")
        self.db.set_video_fields(A, "c1.mp4", original=f"originals/{A}/c1.mov")
        self.db.rename_session(A, "2026-10-12-renamed")
        s = self.db.load("2026-10-12-renamed")
        self.assertEqual((s["date"], s["note"], len(s["videos"]), s["blackbox"], len(s["matches"]), len(s["moments"])),
                         ("2026-10-12", "note a", 2, ["x.bbl"], 1, 1))
        self.assertEqual(s["videos"][0]["original"], "originals/2026-10-12-renamed/c1.mov")   # the folder moved with it
        self.assertFalse(self.db.exists(A))
        with self.assertRaises(ValueError):
            self.db.rename_session("2026-10-12-renamed", "2026-10-12-renamed")
        self.db.tag(B, "other.mp4", 0, 1, "already here", [])               # B has an m01 of its own
        self.db.move_clip("2026-10-12-renamed", "c1.mp4", B)
        b = self.db.load(B)
        self.assertEqual(([v["file"] for v in b["videos"]], len(b["matches"]), len(b["moments"])), (["c1.mp4"], 1, 2))
        self.assertEqual(sorted(m["id"] for m in b["moments"]), ["m01", "m02"])    # the moved moment was renumbered
        self.assertEqual([v["file"] for v in self.db.load("2026-10-12-renamed")["videos"]], ["c2.mp4"])
        self.db.delete_session(B)
        self.assertFalse(self.db.exists(B))
        for t in ("videos", "matches", "moments"):               # cascaded (load() would hide orphans: it returns the empty shape)
            self.assertEqual(self.db.c.execute(f"SELECT count(*) FROM {t} WHERE session = ?", (B,)).fetchone()[0], 0, t)


if __name__ == "__main__":
    unittest.main()
