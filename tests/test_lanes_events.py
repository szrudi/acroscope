"""What analyse/ computes for the player and the CLI alike: a suggestion placed on its clip, the sticks in the
picture's terms (#60 put each in one place; the CLI and the server, and the server and the share, had a copy each)."""
import unittest

from acroscope.analyse import events, lanes


class OnClipTest(unittest.TestCase):
    def test_a_suggestion_on_its_clip(self):
        """video = arm time + offset, to the hundredth; the suggestion's own fields first, as the API and the CLI
        always gave them."""
        e = {"tag": "flip", "arm_from": 1.016, "arm_to": 2.5, "title": "flip", "why": "-", "numbers": {}}
        r = events.on_clip(e, {"video": "c.mp4", "offset": 10.0, "bbl": "L.bbl", "arm": 3})
        self.assertEqual(list(r), ["tag", "arm_from", "arm_to", "title", "why", "numbers", "video", "start", "end"])
        self.assertEqual((r["video"], r["start"], r["end"]), ("c.mp4", 11.02, 12.5))                 # not 11.0: hundredths
        self.assertEqual(e, {"tag": "flip", "arm_from": 1.016, "arm_to": 2.5, "title": "flip", "why": "-", "numbers": {}})


class StickFrameTest(unittest.TestCase):
    def test_the_picture_s_terms(self):
        """Mode 2, every axis + = stick right or up, in %: throttle 0..100 from 1000..2000 (clamped), the others
        rcCommand / 5, and yaw negated because rcCommand[2] is + for the left stick LEFT (metrics.CONVENTIONS)."""
        rc = [[500, -250, 0], [-100, 0, 0], [300, -500, 0], [1500, 2200, 950]]          # roll, pitch, yaw, throttle
        self.assertEqual(lanes.stick_frame(rc, 0), (50, -60, -20, 100))
        self.assertEqual(lanes.stick_frame(rc, 1), (100, 100, 0, -50))
        self.assertEqual(lanes.stick_frame(rc, 2), (0, 0, 0, 0))                          # below 1000: 0, not -5
