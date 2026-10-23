"""OSD profiles derived from Betaflight's dump and font (#10): the dump's encodings, the font file, the shipped
profiles against the layouts once measured by hand (HAND), and the reader on synthetic frames drawn with a made-up
font (tests/osd_synthetic.py; the Configurator's fonts are GPL-3.0 and fetched at run time, never in here: in this
module every Configurator font is the synthetic one). The real footage is in the proofs on PRs #63 and #64, and in
the real-data pins (tests/test_realdata.py)."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from acroscope.sources import betaflight, osd, osdprofile

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
                with self.assertRaisesRegex(betaflight.FontUnavailable, "not in ACROSCOPE_FONTS"):
                    REAL_FETCH("default")

        class Answer:                                                            # what urlopen hands back
            def __init__(self, body): self.body = body
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self): return self.body.encode()
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(betaflight, "FONT_DIR", None), \
                mock.patch.object(betaflight, "FONT_CACHE", Path(tmp)), mock.patch.object(betaflight, "_failed", {}):
            with mock.patch("urllib.request.urlopen", return_value=Answer("<html>rate limited</html>")):
                with self.assertRaisesRegex(betaflight.FontUnavailable, "could not be fetched"):
                    REAL_FETCH("bold")
            self.assertFalse((Path(tmp) / "bold.mcm").exists())                 # a broken download is not kept
            with mock.patch("urllib.request.urlopen", side_effect=AssertionError("tried again at once")):
                with self.assertRaises(betaflight.FontUnavailable):
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
        with mock.patch.object(osd, "RETRY_DERIVE", 0), mock.patch.object(osd, "SOURCE_EVERY", 0):
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
        with mock.patch.object(osd, "layouts", lambda fresh=False: two), mock.patch.object(osd, "_frames", frames), \
                mock.patch.object(osd, "_load_templates", lambda n: {"x": 1}), \
                mock.patch.object(osd, "read_frame", lambda buf, tpl, name: same[next(seq) % 30]):
            self.assertEqual(osd.detect_layout(Path("clip.mp4")), "otto")
        # and when nothing counts at 1 s/s (a clock off the clip's), the tie at the other rate goes the same way
        fast = [(round(i * 4.4, 1), round(i * 4.4, 1), 1.0) for i in range(30)]
        seq = iter(range(10 ** 6))
        with mock.patch.object(osd, "layouts", lambda fresh=False: two), mock.patch.object(osd, "_frames", frames), \
                mock.patch.object(osd, "_load_templates", lambda n: {"x": 1}), \
                mock.patch.object(osd, "read_frame", lambda buf, tpl, name: fast[next(seq) % 30]):
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



def profile_with(extra: str, **kw) -> dict:
    """A profile from the 10-11 dump with some settings over it, in the synthetic font (or `chars`)."""
    chars = kw.pop("chars", None) or syn.font_chars()
    return osdprofile.make((DUMPS / "2026-10-11-otto.txt").read_text() + extra, mcm=syn.mcm_text(chars), font="synth", **kw)


class ReviewTest(unittest.TestCase):
    """The review of #63, finding by finding: each test is what the finding showed, and what holds now."""

    def tearDown(self):
        osd.PROFILE_SOURCE = None

    def test_1_seconds_past_the_edge(self):
        # timer 2 from column 24 (MM:SS in columns 25-29, a normal bottom-right place) on a grid 5 px right: its last
        # seconds digit is outside the 720 px frame. That was 'dd:dx', and reading it an IndexError on every clip.
        edge = "set osd_tim_2_pos = " + str(0x800 | (12 << 5) | 24) + "\n"
        with self.assertRaisesRegex(ValueError, "timer 2: its digit at column 29 is outside the picture"):
            osdprofile.derive(profile_with(edge, grid={"x": 8}))
        self.assertEqual(osdprofile.derive(profile_with(edge))["formats"][1], "dd:dd")     # at x 3 it fits
        self.assertIsNone(osd._value(["0", "1", "2"]))                                    # and short rows read as nothing
        # fit searches grids around the profile's; the ones that cut the seconds score nothing instead of failing
        lay = osdprofile.derive(profile_with(edge))
        buf = bytes(syn.osd_frame(syn.font_chars(), lay["grid"], 10.35))
        self.assertIn("x", osdprofile.fit([buf], profile_with(edge), reach=8))

    def test_2_the_store_is_not_asked_per_cell(self):
        calls = []
        doc = {"version": 1, "profiles": {"synth": synth_profile()}}

        def source():
            calls.append(1)
            return doc, "2026-10-23T10:00:00"
        osd.PROFILE_SOURCE = source
        with mock.patch("time.monotonic", return_value=1000.0):                # a frozen clock: the count is the code's, not the machine's speed
            lay = osd.layouts()["synth"]
            full = syn.osd_frame(syn.font_chars(), lay["grid"], 10.35)
            x, y, w, h = osd._crop("synth")
            crop = bytes(full[(y + j) * 720 + x + i] for j in range(h) for i in range(w))
            calls.clear()
            for _ in range(20):                                                # 20 frames, every cell asking for its layout
                self.assertEqual(osd.read_frame(crop, lay["tpl"], "synth")[:2], (6.3, 36))
        self.assertEqual(len(calls), 0)                                         # within the second: not once, let alone 652 times a frame
        # a change is seen at the next look, by its updated_at
        doc2 = {"version": 1, "profiles": {"synth": {**synth_profile(), "grid": {**synth_profile()["grid"], "x": 5}}}}
        osd.PROFILE_SOURCE = lambda: (doc2, "2026-10-23T10:05:00")
        self.assertEqual(osd.layouts()["synth"]["grid"]["x"], 5)
        same = osd.layouts()
        with mock.patch.object(osd, "SOURCE_EVERY", 0):
            self.assertIs(osd.layouts()["synth"], same["synth"])                # the same updated_at: nothing derived again

    def test_3_stats_labels_that_only_begin_alike(self):
        lay = osdprofile.derive(synth_profile())
        chars, g = syn.font_chars(), lay["grid"]
        frame = lambda st: bytes(syn.osd_frame(chars, g, 26.0, stats=st))
        # the persistent stats on, TOTAL ARM off: TOTAL FLIGHTS / FLIGHT TIME / DISTANCE are not it
        self.assertEqual(osdprofile.stats_rows(frame([(4, "TOTAL FLIGHTS", "25"), (5, "TOTAL FLIGHT TIME", "07:32"),
                                                      (6, "TOTAL DISTANCE", "0"), (8, "BLACKBOX", "42%")]), lay), {"blackbox": -4 + 36 * 8})
        # one longer label alone (no runner-up to lose to): its letters in the two cells after TOTAL ARM's end keep it out
        self.assertEqual(osdprofile.stats_rows(frame([(5, "TOTAL FLIGHT TIME", "07:32"), (8, "BLACKBOX", "42%")]), lay), {"blackbox": -4 + 36 * 8})
        # the arm timer's row (LAST ARM) is not the total's
        self.assertEqual(osdprofile.stats_rows(frame([(5, "LAST ARM", "00:20.4"), (8, "BLACKBOX", "42%")]), lay), {"blackbox": -4 + 36 * 8})
        # two rows that read alike (both timers' stats on the total armed time): which one is not guessed
        self.assertEqual(osdprofile.stats_rows(frame([(4, "TOTAL ARM", "00:50"), (5, "TOTAL ARM", "00:50"), (8, "BLACKBOX", "42%")]), lay), {"blackbox": -4 + 36 * 8})
        # with TOTAL ARM on as well, its own row
        self.assertEqual(osdprofile.stats_rows(frame([(4, "TOTAL ARM", "00:50"), (5, "TOTAL FLIGHTS", "25"), (6, "TOTAL FLIGHT TIME", "07:32"),
                                                      (8, "BLACKBOX", "42%")]), lay), {"total_arm": -4 + 36 * 4, "blackbox": -4 + 36 * 8})
        # one label found and not the other: no STATS reading (not a KeyError)
        self.assertEqual(osd.read_stats_frame(frame([(5, "TOTAL ARM", "00:50")]), lay, lay["tpl"]), {"total_arm": None, "blackbox": None})

    def test_4_a_timer_on_row_0(self):
        # timer 2 on row 0, timer 1 on row 1: the cells' tops are above the picture (y -4); the reader's strip and
        # the page's look read the same digits
        s = "set osd_tim_2_pos = " + str(0x800 | (0 << 5) | 22) + "\nset osd_tim_1_pos = " + str(0x800 | (1 << 5) | 20) + "\nset osd_total_flights_pos = 0\n"
        doc = {"version": 1, "profiles": {"top": profile_with(s)}}
        osd.PROFILE_SOURCE = lambda: (doc, "t0")
        lay = osd.layouts()["top"]
        self.assertGreaterEqual(lay["rows"][0] + lay["glyph"][2], 0)          # the boxes start inside the picture
        chars = syn.font_chars()
        full = syn.background()
        syn.draw(full, chars, lay["grid"], 22, 0, "\x9c00:36")
        syn.draw(full, chars, lay["grid"], 20, 1, "\x9c00:06.3")
        x, y, w, h = osd._crop("top")
        self.assertEqual(y, 0)
        crop = bytes(full[(y + j) * 720 + x + i] for j in range(h) for i in range(w))
        self.assertEqual(osd.read_frame(crop, lay["tpl"], "top")[:2], (6.3, 36))
        self.assertEqual(osdprofile.probe(bytes(full), lay)["values"], {"arm": 6.3, "total": 36})
        checker = bytes(255 * ((i + i // 720) % 2) for i in range(720 * 480))          # contrast everywhere, the bottom rows too
        self.assertTrue(osd._glyph(checker, 720, 0, 0, (0, 4, 0, 4)))
        self.assertEqual(osd._glyph(checker, 720, 0, -2, (0, 4, 0, 4)), [])          # above the picture: no cell, not the bottom rows

    def test_5_the_osd_profile_in_use(self):
        p2 = lambda v: str(0x1000 | (v & 0x3FF))                              # shown in OSD profile 2 only
        s = (f"set osd_tim_1_pos = {p2((12 << 5) | 20)}\nset osd_tim_2_pos = {p2((11 << 5) | 22)}\nset osd_total_flights_pos = 0\n")
        with self.assertRaisesRegex(ValueError, "no timer to read"):
            osdprofile.derive(profile_with(s))                                  # profile 1 in use: none of them shows
        lay = osdprofile.derive(profile_with(s + "set osd_profile = 2\n"))
        self.assertEqual(lay["x0"], [3 + 24 * 23, 3 + 24 * 21])
        with self.assertRaises(ValueError):
            osdprofile.derive(profile_with("set osd_profile = 4\n"))

    def test_6_stats_cells_at_the_layout_pitch(self):
        p = profile_with("", grid={"x": -40, "cw": 26, "ch": 36})          # wider cells, the grid moved left to fit them
        lay = osdprofile.derive(p)
        buf = bytes(syn.osd_frame(syn.font_chars(), lay["grid"], 26.0))
        self.assertEqual(osd.read_stats_frame(buf, lay, lay["tpl"]), {"total_arm": 50, "blackbox": 42})

    def test_7_shapes_and_numbers(self):
        good = synth_profile()
        for bad in ([1], {**good, "settings": ["a"]}, {**good, "font": {"data": 5}}, {**good, "font": "impact"},
                    {**good, "grid": [3, -4]}, {**good, "version": 2}, {**good, "settings": {"osd_tim1": True}}):
            with self.assertRaises(ValueError, msg=str(bad)[:60]):
                osdprofile.derive(bad)
        for v in (float("inf"), float("nan"), True):
            with self.assertRaises(ValueError):
                osdprofile.check_grid({**osdprofile.GRID_NTSC, "x": v})
        # a stored profile that breaks in any way is named, and the others still read
        doc = {"version": 1, "profiles": {"ok": good, "odd": good}}
        osd.PROFILE_SOURCE = lambda: (doc, "t7")
        real = osdprofile.derive
        with mock.patch.object(osdprofile, "derive", lambda p, name="profile": (_ for _ in ()).throw(TypeError("odd")) if name == "odd" else real(p, name)):
            self.assertIn("ok", osd.layouts())
            self.assertEqual(osd.derived_errors(), {"odd": "odd"})
            self.assertEqual(osdprofile.describe(doc)["errors"], {"odd": "odd"})

    def test_8_fonts_fetched_safely(self):
        mcm = syn.mcm_text(syn.font_chars())

        class Answer:
            def __init__(self, body): self.body = body
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self): return self.body
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(betaflight, "FONT_CACHE", Path(tmp)), \
                mock.patch.object(betaflight, "FONT_DIR", None), mock.patch.object(betaflight, "_failed", {}):
            import urllib.error
            with mock.patch("urllib.request.urlopen", side_effect=urllib.error.URLError("no route to host")):
                with self.assertRaises(betaflight.FontUnavailable) as e:
                    REAL_FETCH("impact")
            self.assertIsInstance(e.exception, ValueError)                    # a 400 with what to do, not a 500
            self.assertIn("upload its .mcm instead", str(e.exception))
            betaflight._failed.clear()                                          # each step its own fetch (no retry wait)
            with mock.patch("urllib.request.urlopen", return_value=Answer("<html>Wi-Fi login ✓</html>".encode())):
                with self.assertRaises(betaflight.FontUnavailable):
                    REAL_FETCH("impact")                                     # a captive page: not a font, not kept
            self.assertFalse(betaflight.have_font("impact"))
            betaflight._failed.clear()
            with mock.patch("urllib.request.urlopen", return_value=Answer(mcm.encode())):
                self.assertEqual(REAL_FETCH("impact"), mcm)
            self.assertTrue(betaflight.have_font("impact"))
            self.assertEqual(sorted(x.name for x in betaflight._font_path("impact").parent.iterdir()), ["impact.mcm"])   # no temp left
            betaflight._font_path("impact").write_text(mcm[:5000])            # a write cut short in the cache
            with mock.patch("urllib.request.urlopen", return_value=Answer(mcm.encode())) as u:
                self.assertEqual(REAL_FETCH("impact"), mcm)                   # fetched again, not returned broken
                self.assertEqual(u.call_count, 1)
            with mock.patch("urllib.request.urlopen", side_effect=AssertionError("cached: not fetched")):
                self.assertEqual(REAL_FETCH("impact"), mcm)
            # ranking with no way out: one wait for the network, then the rest are said to be unavailable
            lay = osdprofile.derive(synth_profile())
            buf = bytes(syn.osd_frame(syn.font_chars(), lay["grid"], 10.35))
            with mock.patch.object(betaflight, "fetch_font", REAL_FETCH), \
                    mock.patch("urllib.request.urlopen", side_effect=urllib.error.URLError("no route to host")) as u:
                r = osdprofile.rank_fonts([buf], synth_profile(), ["bold", "vision", "impact", "default"])
            self.assertEqual(u.call_count, 1)
            self.assertEqual([x["font"] for x in r if x["score"] is not None], ["impact"])   # the one in the cache
            self.assertTrue(all("could not be fetched" in x["error"] for x in r if x["font"] != "impact"))

    def test_14_a_font_that_does_not_fill_its_cell(self):
        # the real fonts' digits sit in columns 2-9 of 12; the glyph box and the templates follow
        chars = syn.font_chars(narrow=True)
        p = osdprofile.make(syn.DUMP, mcm=syn.mcm_text(chars), font="narrow")
        lay = osdprofile.derive(p)
        self.assertEqual(lay["glyph"][:2], (4, 14))
        self.assertEqual(osdprofile.probe(bytes(syn.osd_frame(chars, lay["grid"], 10.35)), lay)["values"], {"arm": 6.3, "total": 36})
        # read with the wide box instead, the same digits score lower: the box is not just any box
        wide = {**lay, "glyph": (0, 24, lay["glyph"][2], lay["glyph"][3]), "tpl": {d: osdprofile.render(chars, d, lay["grid"], (0, 24, lay["glyph"][2], lay["glyph"][3])) for d in "0123456789"}}
        buf = bytes(syn.osd_frame(chars, lay["grid"], 10.35))
        narrow_score = min(c["score"] for c in osdprofile.probe(buf, lay)["cells"] if c.get("digit"))
        wide_score = min(c["score"] for c in osdprofile.probe(buf, wide)["cells"] if c.get("digit"))
        self.assertGreater(narrow_score, wide_score)

    def test_14_guards(self):
        with self.assertRaisesRegex(ValueError, "two timers count the same thing"):
            osdprofile.derive(profile_with("set osd_tim2 = 2594\n"))          # both timers the arm timer
        # a stored profile named like a built-in layout (an old document) is not the reader's
        doc = {"version": 1, "profiles": {"otto": synth_profile()}}
        osd.PROFILE_SOURCE = lambda: (doc, "t14")
        self.assertEqual(osd.layouts()["otto"]["origin"], "shipped")
        with self.assertRaises(ValueError):                                     # True is not a pixel (it is 1)
            osdprofile.check_profiles({"profiles": {"mine": {**synth_profile(), "grid": {**synth_profile()["grid"], "x": True}}}})
        # a profile kept as it was is not checked again: one broken since does not block saving the others
        broken = {**synth_profile(), "grid": {**synth_profile()["grid"], "cw": 7}}
        stored = {"version": 1, "profiles": {"old": broken}}
        self.assertIn("new", osdprofile.check_profiles({"version": 1, "profiles": {"old": broken, "new": synth_profile()}}, stored)["profiles"])
        with self.assertRaises(ValueError):
            osdprofile.check_profiles({"version": 1, "profiles": {"old": broken}})              # nor taken in new



class SecondReviewTest(unittest.TestCase):
    """The independent pass over the review's fixes (2026-10-23), finding by finding."""

    def tearDown(self):
        osd.PROFILE_SOURCE = None

    def test_bottom_edge(self):
        # PAL, the arm timer on row 15 (the last), the total on row 14, the grid 8 px lower than the default: the arm
        # cell's box ended 2 px below the 576 px frame, no warning, and ffmpeg moved the reader's crop up 4 px
        pal = lambda y: profile_with(f"set vcd_video_system = PAL\nset osd_tim_1_pos = {0x800 | (15 << 5) | 20}\nset osd_tim_2_pos = {0x800 | (14 << 5) | 22}\n"
                                     "set osd_total_flights_pos = 0\n", grid={"y": y})
        doc = {"version": 1, "profiles": {"pal": pal(4)}}
        osd.PROFILE_SOURCE = lambda: (doc, "pal")
        lay = osd.layouts()["pal"]
        self.assertLessEqual(lay["rows"][1] + lay["glyph"][2] + lay["glyph"][3], 576)   # every box inside the picture
        x, y, w, h = osd._crop("pal")
        self.assertLessEqual(y + h, 576)                                        # and the reader's crop
        chars, buf = syn.font_chars(), syn.background(720, 576)
        syn.draw(buf, chars, lay["grid"], 22, 14, "\x9c00:36")
        syn.draw(buf, chars, lay["grid"], 20, 15, "\x9c00:06.3")
        crop = bytes(buf[(y + j) * 720 + x + i] for j in range(h) for i in range(w))
        self.assertEqual(osd.read_frame(crop, lay["tpl"], "pal")[:2], (6.3, 36))
        with self.assertRaisesRegex(ValueError, "px below the picture"):
            osdprofile.derive(pal(30))                                          # most of the cell below: refused, said

    def test_top_edge_judged_per_cell(self):
        # timers on rows 1 and 2 cut 2 px at the top, the counter on row 0 cut 38: the counter goes, the timers stay
        s = (f"set osd_tim_2_pos = {0x800 | (1 << 5) | 22}\nset osd_tim_1_pos = {0x800 | (2 << 5) | 20}\nset osd_total_flights_pos = {0x800 | 24}\n")
        lay = osdprofile.derive(profile_with(s, grid={"y": -40}))
        self.assertNotIn("counter", lay)
        self.assertTrue(any("flight counter" in w for w in lay["warnings"]))
        self.assertEqual(lay["glyph"][2], 2 + 2)                               # the box narrowed by the timers' own 2 px

    def test_settings_are_text(self):
        for bad in ({"vcd_video_system": 1}, {"osd_displayport_device": 7}, {"osd_tim_1_pos": float("inf")}):
            with self.assertRaises(ValueError, msg=str(bad)):
                osdprofile.derive({**synth_profile(), "settings": {**synth_profile()["settings"], **bad}})
        with self.assertRaisesRegex(ValueError, "not a number"):
            osdprofile.derive(profile_with("set osd_tim_1_pos = 9e999\n"))      # text that is no number either

    def test_whatever_derive_trips_on_is_a_400(self):
        # what the type checks do not foresee still names the profile as a ValueError (a 400), never a 500
        with mock.patch.object(osdprofile, "derive", side_effect=TypeError("something unforeseen")):
            with self.assertRaisesRegex(ValueError, "mine: something unforeseen"):
                osdprofile.check_profiles({"version": 1, "profiles": {"mine": synth_profile()}})

    def test_a_stored_font_has_a_name(self):
        p = synth_profile()
        with self.assertRaisesRegex(ValueError, "font.name"):
            osdprofile.check_profiles({"version": 1, "profiles": {"noname": {**p, "font": {"data": p["font"]["data"]}}}})

    def test_two_writers_of_one_font(self):
        import threading
        mcm = syn.mcm_text(syn.font_chars())

        class Slow:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self):
                gate.wait(5)
                return mcm.encode()
        gate, both_written, out, errors = threading.Barrier(2), threading.Barrier(2), [], []
        real_replace = os.replace

        def replace(src, dst):                                                  # both have written before either moves its file
            both_written.wait(5)
            real_replace(src, dst)
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(betaflight, "FONT_CACHE", Path(tmp)), \
                mock.patch.object(betaflight, "FONT_DIR", None), mock.patch.object(betaflight, "_failed", {}), \
                mock.patch("urllib.request.urlopen", return_value=Slow()), mock.patch("os.replace", replace):
            def fetch():
                try:
                    out.append(REAL_FETCH("bold"))
                except Exception as e:  # noqa: BLE001
                    errors.append(e)
            ts = [threading.Thread(target=fetch) for _ in range(2)]
            [t.start() for t in ts]
            [t.join(10) for t in ts]
            self.assertEqual((len(out), errors), (2, []))
            self.assertEqual(sorted(x.name for x in betaflight._font_path("bold").parent.iterdir()), ["bold.mcm"])

    def test_fonts_ranked_from_a_folder_each_named(self):
        # ACROSCOPE_FONTS with impact alone: every other font says it is missing itself (not the first one's message)
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(betaflight, "FONT_DIR", tmp), mock.patch.object(betaflight, "fetch_font", REAL_FETCH):
            (Path(tmp) / "impact.mcm").write_text(syn.mcm_text(syn.font_chars()))
            lay = osdprofile.derive(synth_profile())
            r = {x["font"]: x for x in osdprofile.rank_fonts([bytes(syn.osd_frame(syn.font_chars(), lay["grid"], 10.35))], synth_profile(), ["default", "bold", "impact"])}
        self.assertIsNotNone(r["impact"]["score"])
        self.assertIn("default.mcm is not in", r["default"]["error"])
        self.assertIn("bold.mcm is not in", r["bold"]["error"])

    def test_the_stamp_is_the_layouts_own(self):
        # a long reader looked at the profiles a moment ago; a newer document in the store must not stamp a reading
        # made with the older layouts, and a reader starting a clip looks again
        box = {"doc": {"version": 1, "profiles": {"mine": synth_profile(x=39)}}}
        osd.PROFILE_SOURCE = lambda: (box["doc"], "2026-10-23T12:00:00")
        with mock.patch.object(osd, "SOURCE_EVERY", 3600):
            old = osd.layouts()["mine"]
            stamp = osd._templates_stamp()
            box["doc"] = {"version": 1, "profiles": {"mine": synth_profile(x=3)}}   # saved within the same second
            self.assertIs(osd.layouts()["mine"], old)
            self.assertEqual(osd._templates_stamp(), stamp)                     # still the old layouts: the old stamp
            self.assertEqual(osd.layouts(fresh=True)["mine"]["grid"]["x"], 3)   # a fresh look sees it, same updated_at or not
            self.assertNotEqual(osd._templates_stamp(), stamp)
        with tempfile.TemporaryDirectory() as tmp:
            clip = syn.make_clip(Path(tmp) / "videos" / "s" / "c.mp4", syn.font_chars(), {"x": 3, "y": -4, "cw": 24, "ch": 36}, seconds=8)
            with mock.patch("acroscope.config.CACHE_DIR", Path(tmp) / "cache"), mock.patch.object(osd, "SOURCE_EVERY", 3600):
                box["doc"] = {"version": 1, "profiles": {"mine": synth_profile(x=39)}}
                osd.layouts(fresh=True)
                box["doc"] = {"version": 1, "profiles": {"mine": synth_profile(x=3)}}
                rt = osd.read_timers(clip, 2.0, layout="mine")                  # starts a clip: looks again
                self.assertTrue(any(r["bottom"] is not None for r in rt["rows"]))


if __name__ == "__main__":
    unittest.main()
