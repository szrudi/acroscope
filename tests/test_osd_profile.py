"""OSD profiles derived from Betaflight's dump and font (#10): the dump's encodings, the font file, the shipped
profiles against the layouts once measured by hand (HAND), and the reader on synthetic frames drawn with a made-up
font (tests/osd_synthetic.py; the Configurator's fonts are GPL-3.0 and fetched at run time, never in here: in this
module every Configurator font is the synthetic one). The real footage is in the proofs on PRs #63 and #64, and in
the real-data pins (tests/test_realdata.py)."""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from acroscope import betaflight, osd, osdprofile

try:
    from tests import osd_synthetic as syn
except ImportError:
    import osd_synthetic as syn

DUMPS = Path(__file__).parent / "data" / "osd-dumps"
REAL_FETCH = betaflight.fetch_font
_fonts = None


def setUpModule():
    """No network here: a Configurator font by any name is the synthetic font."""
    global _fonts
    _fonts = mock.patch.object(betaflight, "fetch_font", lambda name: syn.mcm_text(syn.font_chars()))
    _fonts.start()


def tearDownModule():
    _fonts.stop()


# The layouts as they were measured by hand on Rudi's footage (osd.LAYOUTS until 2026-10-23, glyph surveys of
# 2026-10-09 and 10-10): the cell origin x0 and row y each survey chose, the glyph box (x, w, y, h) of the font it
# was measured on, the formats, which row was the arm timer. The record the shipped profiles are proved against.
SMALL, BOLD = (4, 10, 3, 18), (1, 18, 1, 22)
HAND = {
    "otto": {"x0": [559, 559], "rows": [362, 398], "formats": ["dd:dd", "dd:dd"], "glyph": SMALL, "cumulative": False, "arm_row": 0},
    "air65f": {"x0": [559], "rows": [434], "formats": ["dd:dd"], "glyph": SMALL, "cumulative": True, "arm_row": 0},
    "air65f-b": {"x0": [583], "rows": [434], "formats": ["dd:dd"], "glyph": SMALL, "cumulative": True, "arm_row": 0},
    "otto-bold": {"x0": [558, 558], "rows": [401, 437], "formats": ["dd:dd.d", "dd:dd"], "glyph": BOLD, "cumulative": False, "arm_row": 0},
    "otto-bold-swap": {"x0": [558, 486], "rows": [401, 437], "formats": ["dd:dd", "dd:dd.dd"], "glyph": BOLD, "cumulative": False, "arm_row": 1},
    "otto-bold-v3": {"x0": [558, 510], "rows": [401, 437], "formats": ["dd:dd", "dd:dd.d"], "glyph": BOLD, "cumulative": False, "arm_row": 1},
}


def synth_profile(**grid) -> dict:
    return osdprofile.make(syn.DUMP, mcm=syn.mcm_text(syn.font_chars()), font="synth", grid=grid or None)


class DumpTest(unittest.TestCase):
    def test_settings_are_the_global_ones(self):
        d = betaflight.parse_dump(syn.DUMP)
        self.assertTrue(d["version"].startswith("Betaflight / STM32G47X"))
        self.assertEqual(d["settings"]["osd_tim_1_pos"], "2452")            # not the profile 0 section's 1
        self.assertEqual(d["settings"]["craft_name"], "SYNTH")
        with self.assertRaises(ValueError):
            betaflight.parse_dump("# version\nfeature OSD\n")

    def test_positions(self):
        # values from Rudi's dumps (2026-09-23 to 10-11), decoded by hand from Betaflight's osd.h
        s = {"osd_tim_1_pos": "2452", "osd_tim_2_pos": "2422", "osd_total_flights_pos": "2072", "osd_mah_drawn_pos": "386",
             "osd_hd_pos": str(0x800 | 0x400 | (3 << 5) | 5)}
        self.assertEqual(betaflight.element(s, "tim_1"), {"col": 20, "row": 12})
        self.assertEqual(betaflight.element(s, "tim_2"), {"col": 22, "row": 11})
        self.assertEqual(betaflight.element(s, "total_flights"), {"col": 24, "row": 0})
        self.assertIsNone(betaflight.element(s, "mah_drawn"))                # 386 = column 2 row 12, no profile bit: hidden
        self.assertIsNone(betaflight.element(s, "rssi"))                     # not in the diff: the default, hidden
        self.assertEqual(betaflight.element(s, "hd"), {"col": 37, "row": 3})  # bit 10 is column + 32 (HD displays)
        self.assertIsNone(betaflight.element(s, "tim_1", profile=2))         # shown in OSD profile 1 only

    def test_timers(self):
        t = lambda v, n=1: betaflight.timer({f"osd_tim{n}": str(v)} if v is not None else {}, n)
        self.assertEqual((t(2594)["source"], t(2594)["format"], t(2594)["alarm"]), ("last", "dd:dd.d", 10))   # 10-11: tenths
        self.assertEqual((t(2578)["source"], t(2578)["format"]), ("last", "dd:dd.dd"))                        # hundredths
        self.assertEqual((t(2562)["source"], t(2562)["format"]), ("last", "dd:dd"))                           # 10-05: seconds
        self.assertEqual((t(None)["source"], t(None)["format"]), ("on", "dd:dd"))                             # the defaults
        self.assertEqual((t(None, 2)["source"], t(None, 2)["label"]), ("total", "TOTAL ARM"))

    def test_video_system(self):
        self.assertEqual(betaflight.video_system({"vcd_video_system": "PAL"}), "PAL")
        self.assertEqual(betaflight.video_system({}), "NTSC")
        self.assertEqual(osdprofile.default_grid("PAL")["h"], 576)


class FontTest(unittest.TestCase):
    def test_mcm_round_trip(self):
        chars = syn.font_chars()
        parsed = betaflight.parse_mcm(syn.mcm_text(chars))
        self.assertEqual(parsed, chars)
        self.assertEqual(betaflight.unpack_font(betaflight.pack_font(parsed)), chars)
        self.assertEqual(len(betaflight.pack_font(parsed)), 18432)           # 256 x 54 bytes, base64

    def test_a_broken_font_is_refused(self):
        with self.assertRaises(ValueError):
            betaflight.parse_mcm("hello\n")
        with self.assertRaises(ValueError):
            betaflight.parse_mcm("MAX7456\n" + "0101\n" * 100)
        with self.assertRaises(ValueError):
            REAL_FETCH("comic-sans")                                         # not a Configurator name: never a URL

    def test_fonts_from_a_folder_or_fetched_once(self):
        mcm = syn.mcm_text(syn.font_chars())
        refuse = mock.patch("urllib.request.urlopen", side_effect=AssertionError("no network here"))
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "impact.mcm").write_text(mcm)
            with mock.patch.object(betaflight, "FONT_DIR", tmp), refuse:          # ACROSCOPE_FONTS: that folder, nothing fetched
                self.assertEqual(REAL_FETCH("impact"), mcm)
                with self.assertRaisesRegex(OSError, "not in ACROSCOPE_FONTS"):
                    REAL_FETCH("default")

        class Answer:                                                            # what urlopen hands back
            def __init__(self, body): self.body = body
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self): return self.body.encode()
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(betaflight, "FONT_DIR", None), \
                mock.patch.object(betaflight, "FONT_CACHE", Path(tmp)), mock.patch.object(betaflight, "_failed", {}):
            with mock.patch("urllib.request.urlopen", return_value=Answer("<html>rate limited</html>")):
                with self.assertRaisesRegex(OSError, "could not be fetched"):
                    REAL_FETCH("bold")
            self.assertFalse((Path(tmp) / "bold.mcm").exists())                 # a broken download is not kept
            with mock.patch("urllib.request.urlopen", side_effect=AssertionError("tried again at once")):
                with self.assertRaises(OSError):
                    REAL_FETCH("bold")                                           # nor fetched again straight away
            with mock.patch("urllib.request.urlopen", return_value=Answer(mcm)):
                self.assertEqual(REAL_FETCH("clarity"), mcm)
            self.assertEqual((Path(tmp) / "clarity.mcm").read_text(), mcm)       # kept: the next one reads it
            with refuse:
                self.assertEqual(REAL_FETCH("clarity"), mcm)

    def test_render_follows_the_font(self):
        # a cell rendered from the font correlates with itself drawn on a frame, and not with another digit
        chars = syn.font_chars()
        p = synth_profile()
        lay = osdprofile.derive(p)
        buf = bytes(syn.osd_frame(chars, lay["grid"], 10.35))
        d, sc, margin = osdprofile._best(buf, 720, lay["x0"][0] + 3 * 24, lay["rows"][0], lay["glyph"], lay["tpl"])
        self.assertEqual(d, "3")                                             # total 00:36
        self.assertGreater(sc, 0.9)
        self.assertGreater(margin, 0.2)


class DeriveAgainstHandTest(unittest.TestCase):
    """The proof in the unit suite: the dump of a day derives the layout measured by hand for that day's clips, and
    the shipped profiles are those dumps. The hand layouts placed their cells by their own glyph box, so a cell is
    compared by its centre: the hand glyph box's centre against the derived cell's (within 4 px, the bold font
    sitting a little low in its cell)."""

    def check(self, dump: str, hand: str, extra: str = ""):
        p = osdprofile.make((DUMPS / dump).read_text() + extra, mcm=syn.mcm_text(syn.font_chars()), font="synth")
        lay = osdprofile.derive(p, "derived")
        self.compare(lay, hand)
        return lay

    def compare(self, lay: dict, hand: str):
        H = HAND[hand]
        self.assertEqual(lay["formats"], H["formats"])
        self.assertEqual(lay["cumulative"], H["cumulative"])
        self.assertEqual(lay.get("arm_row", 0), H["arm_row"])
        gx, gw, gy, gh = H["glyph"]
        for row in range(len(H["formats"])):
            hx, hy = H["x0"][row] + gx + gw / 2, H["rows"][row] + gy + gh / 2
            self.assertLessEqual(abs(lay["x0"][row] + 12 - hx), 4, f"{hand} row {row}: x {lay['x0'][row] + 12} vs {hx}")
            self.assertLessEqual(abs(lay["rows"][row] + 18 - hy), 4, f"{hand} row {row}: y {lay['rows'][row] + 18} vs {hy}")

    def test_air65f_single_total(self):
        lay = self.check("2026-09-23-air65f.txt", "air65f")
        self.assertEqual(lay["names"], ["total"])
        self.assertEqual(lay["derived"]["timers"][0]["source"], "total")

    def test_air65f_one_column_right(self):
        self.check("2026-10-04-air65f.txt", "air65f-b")                     # 10-04 005/006: tim_2 at column 23

    def test_otto_two_timers(self):
        self.check("2026-10-05-air65f.txt", "otto")                         # the arm timer on top, MM:SS

    def test_otto_bold_v3(self):
        lay = self.check("2026-10-11-otto.txt", "otto-bold-v3")             # the arm timer with tenths below, from column 21
        self.assertEqual(lay["counter"], {"x0": 3 + 24 * 25, "y": -4, "cells": 5})   # "#" at column 24, the digits after it

    def test_the_bold_layouts_without_a_dump(self):
        # 10-10's two layouts have no dump of their own: the settings that make them, derived the same way
        self.check("2026-10-11-otto.txt", "otto-bold", "set osd_tim_1_pos = 2422\nset osd_tim_2_pos = 2454\n")
        self.check("2026-10-11-otto.txt", "otto-bold-swap", "set osd_tim1 = 2578\nset osd_tim_1_pos = 2451\nset osd_tim_2_pos = 2422\n")

    def test_the_shipped_profiles(self):
        """Every layout once measured by hand ships as a profile of the same name: derived as the hand one was laid
        out, its settings those of the day's dump, its font the one flown then."""
        shipped = osd.shipped()
        self.assertEqual(sorted(shipped), sorted(HAND))
        dumps = {"air65f": "2026-09-23-air65f.txt", "air65f-b": "2026-10-04-air65f.txt", "otto": "2026-10-05-air65f.txt", "otto-bold-v3": "2026-10-11-otto.txt"}
        for name, p in shipped.items():
            self.compare(osdprofile.derive(p, name), name)
            self.assertEqual(p["font"]["name"], "default" if name in ("otto", "air65f", "air65f-b") else "impact", name)
            if name in dumps:
                day = osdprofile.osd_settings(betaflight.parse_dump((DUMPS / dumps[name]).read_text()))
                self.assertEqual({k: day.get(k) for k in p["settings"]}, p["settings"], name)


class DeriveTest(unittest.TestCase):
    def test_what_is_not_read_is_said(self):
        s = (DUMPS / "2026-10-11-otto.txt").read_text() + "set osd_tim2 = 2560\n"     # timer 2 counts power-on time
        lay = osdprofile.derive(osdprofile.make(s, mcm=syn.mcm_text(syn.font_chars()), font="synth"))
        self.assertEqual(lay["names"], ["arm"])                              # the arm timer alone
        self.assertTrue(any("'on'" in w for w in lay["warnings"]))
        with self.assertRaises(ValueError):                                  # nothing to read at all
            osdprofile.derive(osdprofile.make("set osd_vbat_pos = 2433\n", mcm=syn.mcm_text(syn.font_chars())))

    def test_a_cell_past_the_right_edge(self):
        # the arm timer with tenths from column 23 (10-10 001): the tenths digit's cell at column 29 is cut by the
        # 720 px frame; read on what is left of it, every box narrowed alike
        s = (DUMPS / "2026-10-11-otto.txt").read_text() + "set osd_tim_1_pos = 2422\nset osd_tim_2_pos = 2454\n"
        lay = osdprofile.derive(osdprofile.make(s, mcm=syn.mcm_text(syn.font_chars()), font="synth"))
        right = lay["x0"][0] + 6 * 24 + lay["glyph"][0] + lay["glyph"][1]
        self.assertLessEqual(right, 720)
        self.assertEqual(lay["formats"][0], "dd:dd.d")
        far = osdprofile.derive(osdprofile.make(s, mcm=syn.mcm_text(syn.font_chars()), font="synth", grid={"x": 14}))
        self.assertEqual(far["formats"][0], "dd:dd.x")                       # most of it outside: skipped, and said
        self.assertTrue(far["warnings"])

    def test_profile_keeps_only_the_osd(self):
        p = synth_profile()
        self.assertNotIn("pilot_name", p["settings"])
        self.assertIn("osd_tim_1_pos", p["settings"])
        self.assertEqual(p["font"]["name"], "synth")
        self.assertEqual(p["grid"]["x"], 3)

    def test_check_profiles(self):
        ok = {"version": 1, "profiles": {"mine": synth_profile()}}
        self.assertIs(osdprofile.check_profiles(ok), ok)
        for bad in ({"profiles": {"Mine Too": synth_profile()}}, {"profiles": {"otto": synth_profile()}},           # a built-in's name
                    {"profiles": {"mine": {**synth_profile(), "font": {"name": "impact"}}}},                        # no font data
                    {"profiles": {"mine": {**synth_profile(), "grid": {"cw": 3}}}},
                    {"profiles": {"mine": {**synth_profile(), "grid": {"x": 2.5}}}}, {"profiles": {"mine": {**synth_profile(), "grid": {"h": True}}}},
                    {"profiles": {"mine": {**synth_profile(), "settings": {}}}}, [1]):
            with self.assertRaises(ValueError, msg=str(bad)[:80]):
                osdprofile.check_profiles(bad)


class ProbeTest(unittest.TestCase):
    """One frame, as the configuration page shows it."""

    @classmethod
    def setUpClass(cls):
        cls.chars = syn.font_chars()
        cls.p = synth_profile()
        cls.lay = osdprofile.derive(cls.p, "synth")

    def frame(self, t, grid=None):
        return bytes(syn.osd_frame(self.chars, grid or self.lay["grid"], t))

    def test_timers_and_counter(self):
        r = osdprofile.probe(self.frame(10.35), self.lay)
        self.assertEqual(r["values"], {"arm": 6.3, "total": 36})
        self.assertEqual(r["counter"], 7)
        self.assertIsNone(r["stats"])
        r = osdprofile.probe(self.frame(2.0), self.lay)
        self.assertEqual(r["values"], {"arm": 0.0, "total": 30})
        # the counter is left-aligned "#N": the digits end at the first cell that is not one (a letter scores low)
        r = osdprofile.probe(bytes(syn.osd_frame(self.chars, self.lay["grid"], 10.35, flights=42, after_counter="K")), self.lay)
        self.assertEqual(r["counter"], 42)

    def test_stats_rows_by_their_labels(self):
        self.assertEqual(osdprofile.stats_rows(self.frame(26.0), self.lay), {"total_arm": -4 + 36 * 5, "blackbox": -4 + 36 * 8})
        self.assertEqual(osdprofile.stats_rows(self.frame(10.0), self.lay), {})
        r = osdprofile.probe(self.frame(26.0), self.lay)
        self.assertEqual((r["stats"]["total_arm"], r["stats"]["blackbox"]), (50, 42))
        self.assertEqual(r["values"], {"arm": None, "total": None})
        # the reader's own STATS function takes the derived layout's rows from the labels
        self.assertEqual(osd.read_stats_frame(self.frame(26.0), self.lay, self.lay["tpl"]), {"total_arm": 50, "blackbox": 42})

    def test_fit_finds_the_grid(self):
        drawn = {**self.lay["grid"], "x": self.lay["grid"]["x"] + 5, "y": self.lay["grid"]["y"] + 3}
        f = osdprofile.fit([self.frame(10.35, drawn)], self.p)
        self.assertEqual((f["x"], f["y"]), (drawn["x"], drawn["y"]))
        self.assertGreater(f["score"], f["was"]["score"])

    def test_fonts_ranked(self):
        fonts = {"narrow": syn.mcm_text(syn.font_chars()), "wide": syn.mcm_text(syn.font_chars(wide=True))}
        with mock.patch.object(betaflight, "fetch_font", lambda n: fonts[n]):
            r = osdprofile.rank_fonts([self.frame(10.35)], self.p, ["wide", "narrow"])
        self.assertEqual([x["font"] for x in r], ["narrow", "wide"])
        self.assertEqual(r[0]["values"], {"arm": 6.3, "total": 36})


class RegistryTest(unittest.TestCase):
    """The shipped profiles and the pilot's: what detect_layout chooses from, and the cache key."""

    def tearDown(self):
        osd.PROFILE_SOURCE = None

    def test_layouts_and_stamp(self):
        osd.PROFILE_SOURCE = None
        self.assertEqual(sorted(osd.layouts()), sorted(HAND))
        self.assertTrue(all(L["origin"] == "shipped" for L in osd.layouts().values()))
        before = osd._templates_stamp()
        doc = {"version": 1, "profiles": {"synth": synth_profile()}}
        osd.PROFILE_SOURCE = lambda: (doc, "t")
        self.assertIn("synth", osd.layouts())
        self.assertEqual(osd.layouts()["synth"]["origin"], "yours")
        self.assertNotEqual(osd._templates_stamp(), before)                  # a profile changed: every reading again
        self.assertIs(osd._load_templates("synth"), osd.layouts()["synth"]["tpl"])
        # a stored profile under a shipped name (the store refuses one; an old document might hold it) is not the reader's
        clash = {"version": 1, "profiles": {"otto": synth_profile()}}
        osd.PROFILE_SOURCE = lambda: (clash, "t")
        self.assertEqual(osd.layouts()["otto"]["origin"], "shipped")
        self.assertEqual(osd.layouts()["otto"]["formats"], HAND["otto"]["formats"])
        bad = {"version": 1, "profiles": {"broken": {"settings": {}, "font": {"data": synth_profile()["font"]["data"]}}}}
        osd.PROFILE_SOURCE = lambda: (bad, "t")
        self.assertNotIn("broken", osd.layouts())
        self.assertIn("broken", osd.derived_errors())

    def test_a_font_not_to_be_had(self):
        # offline: the shipped layouts on the Impact font are left out and said why; once it can be had, they are back
        osd.PROFILE_SOURCE = None
        def fetch(name):
            if name == "impact":
                raise OSError("the font impact could not be fetched")
            return syn.mcm_text(syn.font_chars())
        with mock.patch.object(betaflight, "fetch_font", fetch):
            osd._derived["key"] = None
            self.assertEqual(sorted(osd.layouts()), ["air65f", "air65f-b", "otto"])
            self.assertIn("could not be fetched", osd.derived_errors()["otto-bold-v3"])
            stamp = osd._templates_stamp()
        with mock.patch.object(osd, "RETRY_DERIVE", 0):
            self.assertEqual(sorted(osd.layouts()), sorted(HAND))
            self.assertNotEqual(osd._templates_stamp(), stamp)                # the readings without it are not reused
        with mock.patch.object(betaflight, "fetch_font", lambda n: (_ for _ in ()).throw(OSError("offline"))):
            osd._derived["key"] = None
            with self.assertRaisesRegex(ValueError, "no OSD layout to read with: .*offline"):
                osd.detect_layout(Path("clip.mp4"))                          # nothing to read with: said, not guessed
        osd._derived["key"] = None

    def test_read_frame_with_the_arm_timer_alone(self):
        s = (DUMPS / "2026-10-11-otto.txt").read_text() + "set osd_tim2 = 2560\n"
        doc = {"version": 1, "profiles": {"armonly": osdprofile.make(s, mcm=syn.mcm_text(syn.font_chars()), font="synth")}}
        osd.PROFILE_SOURCE = lambda: (doc, "t")
        lay = osd.layouts()["armonly"]
        chars = syn.font_chars()
        full = syn.osd_frame(chars, lay["grid"], 10.35)
        x, y, w, h = osd._crop("armonly")
        crop = bytes(full[(y + j) * 720 + x + i] for j in range(h) for i in range(w))
        self.assertEqual(osd.read_frame(crop, lay["tpl"], "armonly")[:2], (6.3, None))

    def test_a_tie_keeps_the_shipped_layout(self):
        same = [(i * 2.0, i * 2.0, 1.0) for i in range(30)]
        frames = lambda path, fps, layout=None: ((i * 2.0, layout) for i in range(30))
        seq = iter(range(10 ** 6))                 # both layouts read the very same counting timers
        two = {"otto": {"origin": "shipped"}, "zz-mine": {"origin": "yours"}}
        with mock.patch.object(osd, "layouts", lambda: two), mock.patch.object(osd, "_frames", frames), \
                mock.patch.object(osd, "_load_templates", lambda n: {"x": 1}), \
                mock.patch.object(osd, "read_frame", lambda buf, tpl, name: same[next(seq) % 30]):
            self.assertEqual(osd.detect_layout(Path("clip.mp4")), "otto")


class ClipTest(unittest.TestCase):
    """A whole synthetic clip through the reader the way automatch reads it: the profile is detected, and the arm
    run comes out where it was drawn."""

    def test_clip(self):
        chars = syn.font_chars()
        p = synth_profile()
        doc = {"version": 1, "profiles": {"synth": p}}
        with tempfile.TemporaryDirectory() as tmp:
            clip = syn.make_clip(Path(tmp) / "videos" / "s" / "c.mp4", chars, osdprofile.derive(p)["grid"])
            # without the shipped profiles: here they would be the synthetic font at the same cells, and win the tie
            with mock.patch.object(osd, "PROFILE_SOURCE", lambda: (doc, "t")), mock.patch("acroscope.config.CACHE_DIR", Path(tmp) / "cache"), \
                    mock.patch.object(osd, "SHIPPED", Path(tmp) / "none.json"):
                self.assertEqual(osd.detect_layout(clip), "synth")
                rt = osd.read_timers(clip, 2.0, layout="synth")
                runs = osd.video_arms(rt)
                self.assertEqual(len(runs), 1)
                # drawn armed from 4.0 s; the reader's frame sampling (ffmpeg's fps filter hands over a frame up to 0.2 s
                # after its nominal time, on real clips as here: OSD_LAG was calibrated with it) puts the start up to that early
                self.assertAlmostEqual(runs[0]["start"], 4.0 - osd.OSD_LAG + 0.05 - 0.1, delta=0.15)
                self.assertAlmostEqual(runs[0]["length"], 20.0, delta=0.6)
                self.assertAlmostEqual(runs[0]["total_before"], 30, delta=0.5)      # whole seconds less the tenths
                st = osd.read_stats(clip, runs, "synth")
                self.assertEqual(st, [{"total_arm": 50, "blackbox": 42}])
                # a DVR that records 640 x 360: the frame is scaled to the grid's 720 x 480 before the cells are cut
                small = syn.make_clip(Path(tmp) / "videos" / "s" / "small.mp4", chars, osdprofile.derive(p)["grid"], seconds=12, size="640:360")
                runs = osd.video_arms(osd.read_timers(small, 2.0, layout="synth"))
                self.assertEqual([round(r["length"]) for r in runs], [8])


if __name__ == "__main__":
    unittest.main()
