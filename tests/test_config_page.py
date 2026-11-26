"""The configuration page (#45) end to end in a headless browser: a real `acroscope serve` child on a scratch data
dir with one decoded arm (a 250 deg roll on a craft named OttO), a match and a tagged moment; the page driven at a
desktop size and at Rudi's phone's. Each knob is checked against what the server then holds, not against the page
alone. Skipped without the browser (scripts/browser-setup.sh), like the player's tests."""
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request
from pathlib import Path

from acroscope.sources import blackbox

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sync_playwright = None

try:
    from tests import osd_synthetic as osyn
    from tests.synthetic import axis, make_arm, pulse
except ImportError:
    import osd_synthetic as osyn
    from synthetic import axis, make_arm, pulse

LIBS = Path(os.environ.get("ACROSCOPE_BROWSER_LIBS", "~/.cache/acroscope-browser")).expanduser() / "usr" / "lib" / "x86_64-linux-gnu"
PHONE = {"viewport": {"width": 412, "height": 927}, "is_mobile": True, "has_touch": True}
DESKTOP = {"viewport": {"width": 1440, "height": 900}}
# the page's fetch made slow for one kind of request, in the page itself (a Playwright route handler would hold up every
# other request behind it)
# the answers to the first request matching each part held back 1.5 s after the server gave them (the server's view
# taken now, seen late)
SLOW_ANSWER_ONCE = """(parts) => { const f = window.fetch, left = [...parts]; window.fetch = (u, o) => { const i = left.findIndex(x => String(u).includes(x)); if (i < 0) return f(u, o); left.splice(i, 1); return f(u, o).then(r => new Promise(res => setTimeout(() => res(r), 1500))); }; }"""
SLOW_FETCH = """(part) => { const f = window.fetch; window.fetch = (u, o) => String(u).includes(part) ? new Promise(r => setTimeout(r, 1500)).then(() => f(u, o)) : f(u, o); }"""
S = "2026-10-04-motor-jitter-testing"
OS = "2026-10-05-osd"                     # a session with a clip whose OSD is drawn in the synthetic font (the OSD tab)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _browser(p):
    return p.chromium.launch(env=dict(os.environ, LD_LIBRARY_PATH=f"{LIBS}:{os.environ.get('LD_LIBRARY_PATH', '')}"))


def browser_available() -> bool:
    if sync_playwright is None:
        return False
    try:
        with sync_playwright() as p:
            _browser(p).close()
        return True
    except Exception:  # noqa: BLE001
        return False


def save_arm(arm, cache: Path, stem: str):
    """A scripted arm into a server's cache as if index_bbl had decoded <stem>.bbl."""
    (cache / "arms" / stem).mkdir(parents=True, exist_ok=True)
    blackbox._save(arm.meta, arm.t, arm._cols, cache / "arms" / stem / "arm-01.bin")
    (cache / "arms" / stem / "arms.json").write_text(json.dumps({"v": blackbox.INDEX_VERSION, "file": stem + ".bbl", "stem": stem, "date": None, "boots": [],
                                                                 "arms": [{k: v for k, v in arm.meta.items() if k != "fields"}]}))


@unittest.skipUnless(browser_available(), "no headless browser: run scripts/browser-setup.sh")
class ConfigPageTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        root = Path(cls.tmp.name)
        (root / "data" / "videos" / S).mkdir(parents=True)
        (root / "data" / "videos" / S / "2026-10-04_001.mp4").write_bytes(b"")
        (root / "data" / "blackbox").mkdir()
        (root / "data" / "blackbox" / "roll.bbl").write_bytes(b"")
        save_arm(make_arm(8, gyro=axis(0, pulse(2.0, 2.5, 500)), craft="OttO"), root / "cache", "roll")     # 250 deg: an attempt, a roll to a beginner
        osyn.make_clip(root / "data" / "videos" / OS / "2026-10-05_001.mp4", osyn.font_chars(), {"x": 3, "y": -4, "cw": 24, "ch": 36})
        (root / "synth.mcm").write_text(osyn.mcm_text(osyn.font_chars()))
        cls.port = free_port()
        (root / "fonts").mkdir()                    # no Configurator font: the shipped profiles stay out of the OSD tab's clip
        env = dict(os.environ, ACROSCOPE_DB=str(root / "t.db"), ACROSCOPE_DATA=str(root / "data"), ACROSCOPE_CACHE=str(root / "cache"), ACROSCOPE_URL="",
                   ACROSCOPE_FONTS=str(root / "fonts"))
        cls.proc = subprocess.Popen([sys.executable, "-m", "acroscope.cli", "serve", "--host", "127.0.0.1", "--port", str(cls.port), "--no-warm"],
                                    env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        cls.url = f"http://127.0.0.1:{cls.port}"
        for _ in range(100):
            try:
                urllib.request.urlopen(cls.url + "/api/store", timeout=1).read()
                break
            except Exception:  # noqa: BLE001
                time.sleep(0.1)
        else:
            raise RuntimeError("server did not come up")
        from acroscope.store.remote import Remote
        cls.api = Remote(cls.url)
        cls.api.refresh(S, probe_videos=False)
        cls.api.refresh(OS)
        cls.api.set_match(S, "2026-10-04_001.mp4", "roll.bbl", 1, 10.0)
        cls.api.tag(S, "2026-10-04_001.mp4", 11.5, 13.0, "a roll", ["roll"])
        cls.pw = sync_playwright().start()
        cls.browser = _browser(cls.pw)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()
        cls.proc.terminate()
        cls.proc.wait(5)
        cls.tmp.cleanup()

    def setUp(self):
        self.api.delete_setting("tricks")                           # every test starts from the shipped tricks alone

    def open(self, path="/config", size=DESKTOP):
        ctx = self.browser.new_context(**size)
        self.addCleanup(ctx.close)
        page = ctx.new_page()
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        self.addCleanup(lambda: self.assertEqual(errors, [], "script errors on the page"))
        page.goto(self.url + path)
        page.wait_for_function("() => typeof D !== 'undefined' && D !== null && document.querySelector('section.on')")
        return page

    def layer(self) -> dict:
        return json.loads(urllib.request.urlopen(self.url + "/api/tricks/layer").read())["layer"]

    def events(self) -> list:
        return [e["title"] for e in json.loads(urllib.request.urlopen(f"{self.url}/api/session/{S}/events?video=001").read())]

    def act(self, page, do, expect):
        """`do()` makes one write: wait for the server to hold `expect` and for the page to have redrawn after it."""
        before = page.evaluate("() => renders")
        do()
        page.wait_for_function("(n) => renders > n", arg=before, timeout=10000)
        self.assertEqual(self.layer(), expect)

    # ---- the page itself -----------------------------------------------------------------------------------------

    def until(self, expect, seconds=5.0):
        """Wait for the server to hold `expect` (for writes that are not one redraw each)."""
        end = time.time() + seconds
        while time.time() < end and self.layer() != expect:
            time.sleep(0.1)
        self.assertEqual(self.layer(), expect)

    def fold(self, page, name):
        """Open one of the detection tab's folded parts (every parameter, check, advanced)."""
        if not page.eval_on_selector(f"details[data-fold={name}]", "e => e.open"):
            page.click(f"details[data-fold={name}] > summary")

    def skill(self, page, value):
        page.click(f"#skill label.seg:has(input[value={value}])")

    def test_tabs_and_the_way_in(self):
        page = self.open()
        self.assertEqual(page.eval_on_selector_all("#tabs span", "els => els.map(e => e.dataset.tab)"), ["tags", "shares", "detection", "osd", "devices"])
        self.assertEqual(page.get_attribute("section.on", "data-panel"), "tags")
        self.assertIn("15/15 tricks on", page.text_content("#status"))
        for t in ("shares", "detection", "osd", "devices", "tags"):
            page.click(f"#tabs [data-tab={t}]")
            self.assertEqual(page.get_attribute("section.on", "data-panel"), t)
            if t == "osd":
                page.wait_for_selector("#oDump")                         # its panel loads the profiles first
            self.assertTrue(page.eval_on_selector(f"section[data-panel={t}]", "e => e.innerHTML.length > 100"), t)
        self.assertEqual(page.evaluate("() => location.hash"), "#tags")
        # the four tabs that went into detection keep their links, and a remembered one
        for old in ("profile", "drones", "tricks", "settings"):
            p = self.open(f"/config#{old}")
            self.assertEqual(p.get_attribute("section.on", "data-panel"), "detection", old)
        p.evaluate("() => localStorage.setItem('acro.cfgtab', 'drones')")
        p.goto(self.url + "/config")
        p.wait_for_function("() => typeof D !== 'undefined' && D !== null && document.querySelector('section.on')")
        self.assertEqual(p.get_attribute("section.on", "data-panel"), "detection")
        tags = self.open("/config#tags")
        self.assertEqual(tags.get_attribute("section.on", "data-panel"), "tags")
        # from the player: the header's gear, and the tag bar's button, both to this page
        player = self.browser.new_context(**DESKTOP).new_page()
        self.addCleanup(player.context.close)
        player.goto(f"{self.url}/?session={S}")
        player.wait_for_function("() => typeof S !== 'undefined' && S && S.session === " + repr(S))
        self.assertEqual(player.get_attribute("#toConfig", "href"), "/config")
        self.assertEqual(player.get_attribute("#tagbar a.mgr", "href"), "/config#tags")
        self.assertIsNone(player.query_selector("#tagmgr"))                 # the manager left the player

    def test_phone_layout(self):
        page = self.open("/config#detection", size=PHONE)
        self.assertTrue(page.evaluate("() => document.body.classList.contains('phone')"))
        r = page.evaluate("() => { const t = document.getElementById('tabs').getBoundingClientRect(); return {bottom: t.bottom, h: innerHeight, w: t.width, iw: innerWidth}; }")
        self.assertAlmostEqual(r["bottom"], r["h"], delta=1)                 # the tab bar at the bottom of the screen, where the thumb is
        self.assertEqual(r["w"], r["iw"])
        self.assertFalse(page.evaluate("() => document.documentElement.scrollWidth > innerWidth"))   # nothing pokes out sideways
        # the status says it all on its own line, not cut off beside the title
        self.assertTrue(page.eval_on_selector("#status", "e => e.scrollWidth <= e.clientWidth + 1"))
        self.assertTrue(page.is_visible("#scope [data-craft=OttO]"))
        page.click("#scope [data-craft=OttO]")
        page.wait_for_selector("#quad")
        self.assertFalse(page.evaluate("() => document.documentElement.scrollWidth > innerWidth"))
        # the three skill choices of a quad on one line
        tops = page.eval_on_selector_all("#skill label.seg", "els => els.map(e => Math.round(e.getBoundingClientRect().top))")
        self.assertEqual((len(tops), len(set(tops))), (3, 1), tops)
        # five tabs side by side on the phone: none wraps or spills
        boxes = page.eval_on_selector_all("#tabs span", "els => els.map(e => { const r = e.getBoundingClientRect(); return [r.left, r.right, r.height, e.scrollWidth <= e.clientWidth + 1]; })")
        self.assertTrue(all(b[3] for b in boxes), boxes)
        self.assertTrue(all(a[1] <= b[0] + 0.5 for a, b in zip(boxes, boxes[1:])), boxes)
        self.assertEqual(len(boxes), 5)
        page.click("#tabs [data-tab=osd]")
        page.wait_for_selector("#oDump")
        self.assertFalse(page.evaluate("() => document.documentElement.scrollWidth > innerWidth"))

    # ---- detection: all quads ------------------------------------------------------------------------------------

    def test_parameters_and_the_skill_for_all_quads(self):
        page = self.open("/config#detection")
        self.assertEqual(self.events(), ["right roll attempt, 249 deg"])
        self.assertIn("none: every trick and parameter as shipped", page.text_content("#mine"))
        self.assertFalse(page.is_visible("#params"))                         # every parameter is folded away
        self.fold(page, "params")
        row = page.locator("#params .prm[data-p=flip_min_deg]")
        self.assertEqual(row.locator(".def").text_content(), "")              # as shipped: nothing to say
        self.assertEqual(row.locator(".src").count(), 0)
        self.assertIn("roll", row.locator(".used").text_content())
        self.act(page, lambda: (row.locator("[data-val]").fill("240"), row.locator("[data-val]").press("Tab")), {"version": 1, "params": {"flip_min_deg": 240}})   # the Tab blurs: one change
        self.assertTrue(page.is_visible("#params"))                          # the redraw keeps the fold open
        row = page.locator("#params .prm[data-p=flip_min_deg]")
        self.assertEqual(row.locator(".src").text_content(), "override")
        self.assertEqual(row.locator(".def").text_content(), "shipped 270")
        self.assertTrue(row.locator("[data-reset]").is_visible())
        self.assertIn("flip_min_deg 240", page.text_content("#mine"))        # what differs, up top
        self.assertIn("1 override", page.text_content(".trk[data-id=roll] .head"))
        self.assertEqual(self.events(), ["right roll"])                     # the suggestions follow, no restart
        self.act(page, lambda: page.click("#mine [data-unparam=flip_min_deg]"), {"version": 1})
        self.assertEqual(page.locator("#params .prm[data-p=flip_min_deg] .src").count(), 0)
        self.assertIn("The shipped parameters", page.text_content("#skillHint"))
        self.assertTrue(page.is_checked("#skill input[value=none]"))
        self.act(page, lambda: self.skill(page, "beginner"), {"version": 1, "skill": "beginner"})
        self.assertTrue(page.is_checked("#skill input[value=beginner]"))
        self.assertEqual(page.text_content("#skillHint"), "A trick counts when it is nearly there (a roll from 240° instead of 270°): it sets 10 parameters at once. Your overrides still win over it.")
        self.assertEqual(page.locator("#params .prm[data-p=flip_min_deg] .src").text_content(), "preset beginner")
        self.assertEqual(page.input_value("#params .prm[data-p=flip_min_deg] [data-val]"), "240")
        self.assertIn("skill beginner", page.text_content("#mine"))
        self.assertEqual(self.events(), ["right roll"])
        self.act(page, lambda: page.click("#mine [data-unskill]"), {"version": 1})
        self.act(page, lambda: self.skill(page, "advanced"), {"version": 1, "skill": "advanced"})
        self.assertTrue(page.text_content("#skillHint").startswith("A trick counts only when it is clean (a roll from 300° instead of 270°): it sets 11 parameters at once."))
        self.act(page, lambda: self.skill(page, "none"), {"version": 1})       # "the defaults" takes the skill away
        self.assertEqual(page.text_content("#skillHint"), "The shipped parameters, in between: a trick counts when it is nearly there with beginner, counts only when it is clean with advanced.")
        page.fill("#pfilter", "orbit")
        self.assertEqual(page.eval_on_selector_all("#params .prm", "els => els.map(e => e.dataset.p)"), ["orbit_min_deg", "orbit_min_dur", "orbit_max_tilt"])
        page.fill("#pfilter", "split-s")                                     # by a trick: its parameters, whatever their names
        split = ["stick_min", "split_s_roll_min", "split_s_roll_max", "split_s_over_deg", "split_s_pull_deg", "split_s_out_deg"]
        self.assertEqual(sorted(page.eval_on_selector_all("#params .prm", "els => els.map(e => e.dataset.p)")), sorted(split))
        row = page.locator("#params .prm[data-p=stick_min]")
        self.act(page, lambda: (row.locator("[data-val]").fill("35"), row.locator("[data-val]").press("Tab")), {"version": 1, "params": {"stick_min": 35}})   # a filtered row saves
        self.assertEqual(page.input_value("#pfilter"), "split-s")              # and the filter outlives the redraw
        self.assertEqual(sorted(page.eval_on_selector_all("#params .prm", "els => els.map(e => e.dataset.p)")), sorted(split))

    def test_a_tricks_parameters_under_it(self):
        """A trick opens to the parameters it reads and nothing else; one set there is the same parameter everywhere."""
        page = self.open("/config#detection")
        self.assertFalse(page.is_visible(".trk[data-id=split-s] .det"))
        page.click(".trk[data-id=split-s] .head .id")
        self.assertEqual(page.eval_on_selector_all(".trk[data-id=split-s] .det .prm", "els => els.map(e => e.dataset.p)"),
                         ["stick_min", "split_s_roll_min", "split_s_roll_max", "split_s_over_deg", "split_s_pull_deg", "split_s_out_deg"])
        self.assertIn("6 parameters", page.text_content(".trk[data-id=split-s] .chev"))
        self.assertIn("also flip", page.text_content(".trk[data-id=split-s] .prm[data-p=stick_min] .used"))   # what else it moves
        self.assertEqual(page.text_content(".trk[data-id=split-s] .prm[data-p=split_s_roll_min] .used"), "")
        self.assertFalse(page.is_visible(".trk[data-id=split-s] .def-lines div"))                            # how it is found: folded too
        row = page.locator(".trk[data-id=split-s] .prm[data-p=stick_min]")
        self.act(page, lambda: (row.locator("[data-val]").fill("35"), row.locator("[data-val]").press("Tab")), {"version": 1, "params": {"stick_min": 35}})
        self.assertTrue(page.is_visible(".trk[data-id=split-s] .det"))       # still open after the save
        self.assertIn("1 override", page.text_content(".trk[data-id=flip] .head"))
        self.act(page, lambda: page.click(".trk[data-id=split-s] .prm[data-p=stick_min] [data-reset]"), {"version": 1})

    # ---- detection: one quad -------------------------------------------------------------------------------------

    def test_a_quad_set_apart(self):
        page = self.open("/config#detection")
        self.assertEqual(page.eval_on_selector_all("#scope [data-craft]", "els => els.map(e => e.dataset.craft)"), ["OttO"])   # seen in the log
        self.assertTrue(page.locator("#allQuads").evaluate("e => e.classList.contains('on')"))
        self.assertIsNone(page.query_selector("#quad"))
        self.assertIn("apply to every quad", page.text_content("#scopeHint"))
        self.assertEqual(page.locator("#scope [data-craft=OttO] .n").count(), 0)   # no override yet
        page.click("#scope [data-craft=OttO]")
        page.wait_for_selector("#quad")
        self.assertEqual(page.eval_on_selector_all("#scope .chip.on", "els => els.map(e => e.id || e.dataset.craft)"), ["OttO"])
        self.assertTrue(page.is_checked("#skill input[value=inherit]"))
        self.assertEqual(page.eval_on_selector_all("[data-prop]", "els => els.map(e => e.dataset.prop)"), ["size_mm", "weight_g", "hover_pct"])
        self.assertIn("apply only to OttO's flights", page.text_content("#scopeHint"))
        self.assertIn("overrides for OttO", page.text_content("#mine .lbl"))
        self.assertIn("none: it flies on the all-quads settings", page.text_content("#mine"))
        self.act(page, lambda: (page.fill("[data-prop=size_mm]", "75"), page.press("[data-prop=size_mm]", "Tab")), {"version": 1, "craft": {"OttO": {"size_mm": 75}}})
        page.click(".trk[data-id=roll] .head .id")
        row = page.locator(".trk[data-id=roll] .prm[data-p=flip_min_deg]")
        self.act(page, lambda: (row.locator("[data-val]").fill("200"), row.locator("[data-val]").press("Tab")), {"version": 1, "craft": {"OttO": {"size_mm": 75, "params": {"flip_min_deg": 200}}}})
        self.assertEqual(page.locator(".trk[data-id=roll] .prm[data-p=flip_min_deg] .src").text_content(), "override")
        self.assertEqual(self.events(), ["right roll"])                     # the arm is OttO's
        self.act(page, lambda: page.uncheck(".trk[data-id=roll] [data-on]"), {"version": 1, "craft": {"OttO": {"size_mm": 75, "params": {"flip_min_deg": 200}, "trick": [{"id": "roll", "enabled": False}]}}})
        self.assertEqual(self.events(), [])                                 # 249 deg is a roll at 200, and the roll is off for OttO
        self.assertEqual(page.text_content("#detectionPanel h2 .dim"), "14 of 15 on for OttO")
        self.assertEqual(page.eval_on_selector_all(".trk[data-id=roll] .head .src", "els => els.map(e => e.textContent)"), ["2 overrides"])   # counted, no badge beside it
        self.assertEqual([x.strip() for x in page.eval_on_selector_all("#mine .it", "els => els.map(e => e.textContent)")], ["size_mm 75↺", "flip_min_deg 200shipped 270↺", "roll off↺"])
        page.click("#allQuads")
        page.wait_for_function("() => !document.querySelector('#quad')")
        self.assertTrue(page.is_checked(".trk[data-id=roll] [data-on]"))     # for any other quad it is on
        self.assertEqual(page.text_content("#scope [data-craft=OttO] .n"), "3")   # the chip says OttO has its own
        self.assertIn("none: every trick", page.text_content("#mine"))
        page.click("#scope [data-craft=OttO]")
        page.wait_for_selector("#quad")
        self.assertEqual(page.text_content("#skillHint"), "OttO uses the skill chosen for all quads: none, the shipped defaults.")
        self.assertEqual(page.eval_on_selector_all("#skill input", "els => els.map(e => e.value)"), ["inherit", "beginner", "advanced"])   # no way under the all-quads preset
        self.act(page, lambda: self.skill(page, "advanced"),
                 {"version": 1, "craft": {"OttO": {"size_mm": 75, "params": {"flip_min_deg": 200}, "trick": [{"id": "roll", "enabled": False}], "skill": "advanced"}}})
        self.assertIn("For OttO it goes over the all-quads skill and overrides", page.text_content("#skillHint"))
        self.act(page, lambda: page.click("#mine [data-untrick=roll]"),
                 {"version": 1, "craft": {"OttO": {"size_mm": 75, "params": {"flip_min_deg": 200}, "skill": "advanced"}}})
        self.act(page, lambda: page.click("#mine [data-unprop=size_mm]"), {"version": 1, "craft": {"OttO": {"params": {"flip_min_deg": 200}, "skill": "advanced"}}})
        self.act(page, lambda: self.skill(page, "inherit"), {"version": 1, "craft": {"OttO": {"params": {"flip_min_deg": 200}}}})   # "as all quads" takes the quad's away
        self.act(page, lambda: self.skill(page, "beginner"), {"version": 1, "craft": {"OttO": {"params": {"flip_min_deg": 200}, "skill": "beginner"}}})
        self.assertIn("Parameters it does not set keep their all-quads values.", page.text_content("#skillHint"))
        self.act(page, lambda: page.click("#mine [data-unskill]"), {"version": 1, "craft": {"OttO": {"params": {"flip_min_deg": 200}}}})
        self.act(page, lambda: page.click("#mine [data-unparam=flip_min_deg]"), {"version": 1})                # the quad's own, never all quads'
        self.assertEqual(page.eval_on_selector_all("#scope .chip.on", "els => els.map(e => e.id || e.dataset.craft)"), ["OttO"])   # chosen still

    def test_a_trick_off_for_all_quads_says_so_on_a_quad(self):
        self.api.set_setting("tricks", {"version": 1, "trick": [{"id": "crash", "enabled": False}], "params": {"flip_min_deg": 240}})
        page = self.open("/config#detection")
        self.assertEqual(page.eval_on_selector_all(".trk[data-id=crash] .head .src", "els => els.map(e => e.textContent)"), ["1 override"])   # counted, not badged twice
        self.assertEqual(page.locator(".trk[data-id=split-s] .head .src").count(), 0)   # as shipped: no badge
        page.click("#scope [data-craft=OttO]")
        page.wait_for_selector("#quad")
        self.assertEqual(page.text_content(".trk[data-id=crash] .head .src"), "all quads")
        self.assertFalse(page.is_checked(".trk[data-id=crash] [data-on]"))
        page.click(".trk[data-id=roll] .head .id")
        self.assertEqual(page.text_content(".trk[data-id=roll] .prm[data-p=flip_min_deg] .src"), "all quads")   # not this quad's override
        self.assertEqual(page.locator(".trk[data-id=roll] .prm[data-p=flip_min_deg] [data-reset]").count(), 0)

    def test_a_craft_declared_in_another_case_is_the_same_chip(self):
        """A section written as `otto` and a log that says `OttO`: one chip, and the knobs write into the section."""
        self.api.set_setting("tricks", {"version": 1, "craft": {"otto": {"size_mm": 75}}})
        page = self.open("/config#detection")
        self.assertEqual(page.eval_on_selector_all("#scope [data-craft]", "els => els.map(e => e.dataset.craft)"), ["otto"])
        page.click("#scope [data-craft=otto]")
        page.wait_for_selector("#quad")
        self.assertEqual(page.input_value("[data-prop=size_mm]"), "75")
        self.act(page, lambda: (page.fill("[data-prop=weight_g]", "40"), page.press("[data-prop=weight_g]", "Tab")), {"version": 1, "craft": {"otto": {"size_mm": 75, "weight_g": 40}}})
        self.fold(page, "params")
        row = page.locator("#params .prm[data-p=flip_min_deg]")
        self.act(page, lambda: (row.locator("[data-val]").fill("200"), row.locator("[data-val]").press("Tab")), {"version": 1, "craft": {"otto": {"size_mm": 75, "weight_g": 40, "params": {"flip_min_deg": 200}}}})
        self.assertEqual(self.events(), ["right roll"])                     # the arm's header says OttO

    # ---- detection: what the page must not get wrong ------------------------------------------------------------

    def test_a_quad_chip_while_its_quad_loads(self):
        """From a chip's click until its quad is in, nothing on the screen writes; a late answer for another quad is dropped."""
        start = {"version": 1, "params": {"flip_min_deg": 250}, "craft": {"OttO": {"params": {"flip_min_deg": 280}}, "Zed": {"size_mm": 90}}}
        self.api.set_setting("tricks", start)
        page = self.open("/config#detection")
        page.evaluate(SLOW_FETCH, "craft=OttO")
        page.click("#scope [data-craft=OttO]")
        self.assertEqual(page.locator("#mine").count(), 0)                    # the all-quads view went at once: no ↺ of it to hit
        self.assertIn("loading", page.text_content("#detectionPanel"))
        self.assertTrue(page.is_visible("#scope [data-craft=Zed]"))           # now, not once OttO is in
        page.click("#scope [data-craft=Zed]")                                  # the chips stay: another quad while OttO loads
        page.wait_for_selector("#quad")
        page.wait_for_timeout(1800)                                            # OttO's answer comes in after Zed's
        self.assertEqual(page.evaluate("() => [craft, DC.craft]"), ["Zed", "Zed"])
        self.assertEqual(page.text_content("#detectionPanel h2 .dim"), "15 of 15 on for Zed")
        self.assertEqual(self.layer(), start)

    def test_two_quick_edits_both_land(self):
        """A value typed and a switch clicked straight after: two saves, one after the other, neither lost."""
        page = self.open("/config#detection")
        page.click(".trk[data-id=roll] .head .id")
        page.fill(".trk[data-id=roll] .prm[data-p=flip_min_deg] [data-val]", "250")
        page.click(".trk[data-id=split-s] [data-on]")                         # the blur saves 250, the click split-s off
        self.until({"version": 1, "params": {"flip_min_deg": 250}, "trick": [{"id": "split-s", "enabled": False}]})
        page.wait_for_function("() => !document.querySelector('.trk[data-id=split-s] [data-on]').checked")
        self.assertEqual(page.text_content("#err"), "")

    def test_what_a_value_box_does_not_save(self):
        """A number that is no number, an emptied parameter, a typo in a quad's size: nothing saved, the value in force
        shown again. An emptied property is the default again; a text property is kept trimmed."""
        start = {"version": 1, "craft": {"OttO": {"size_mm": 75, "frame": "cine"}}}
        self.api.set_setting("tricks", start)
        page = self.open("/config#detection")
        page.click(".trk[data-id=roll] .head .id")
        sel = ".trk[data-id=roll] .prm[data-p=flip_min_deg] [data-val]"
        for typed in ("1e999", ""):
            before = page.evaluate("() => renders")
            page.fill(sel, typed)
            page.press(sel, "Tab")
            page.wait_for_function("(n) => renders > n", arg=before)
            self.assertEqual(page.input_value(sel), "270", typed)
        self.assertEqual(self.layer(), start)
        page.click("#scope [data-craft=OttO]")
        page.wait_for_selector("#quad")
        page.click("[data-prop=size_mm]", click_count=3)
        page.keyboard.type("1e")                                              # not a number yet: the input has no value
        before = page.evaluate("() => renders")
        page.keyboard.press("Tab")
        page.wait_for_function("(n) => renders > n", arg=before)
        self.assertEqual(page.input_value("[data-prop=size_mm]"), "75")
        self.assertEqual(self.layer(), start)
        self.act(page, lambda: (page.fill("[data-prop=frame]", "  whoop "), page.press("[data-prop=frame]", "Tab")), {"version": 1, "craft": {"OttO": {"size_mm": 75, "frame": "whoop"}}})
        self.act(page, lambda: (page.fill("[data-prop=size_mm]", ""), page.press("[data-prop=size_mm]", "Tab")), {"version": 1, "craft": {"OttO": {"frame": "whoop"}}})

    def test_the_overrides_box_says_all_the_layer_sets(self):
        """A trick's own fields, a segment's options and the default quad's section are overrides too: listed, counted,
        each with its way back."""
        self.api.set_setting("tricks", {"version": 1, "trick": [{"id": "hard-landing", "title": "landed", "enabled": False}],
                                        "segment": {"fast-rotation": {"threshold": 150}},
                                        "craft": {"default": {"params": {"flip_min_deg": 300}}, "OttO": {"segment": {"fast-rotation": {"threshold": 120}}}}})
        page = self.open("/config#detection")
        items = [x.strip() for x in page.eval_on_selector_all("#mine .it", "els => els.map(e => e.textContent)")]
        self.assertEqual(len(items), 3, items)
        self.assertIn("hard-landing off↺ title", items)
        self.assertIn("segment fast-rotation threshold 150↺", items)
        self.assertIn("every quad flip_min_deg 300shipped 270↺", items)
        self.assertEqual(page.text_content(".trk[data-id=hard-landing] .head .src"), "1 override")
        self.assertEqual(page.text_content("#scope [data-craft=OttO] .n"), "1")
        own = [{"id": "hard-landing", "title": "landed"}]                     # its own title stays: only the on/off is taken back
        self.act(page, lambda: page.click("#mine [data-untrick=hard-landing]"),
                 {"version": 1, "trick": own, "segment": {"fast-rotation": {"threshold": 150}}, "craft": {"default": {"params": {"flip_min_deg": 300}}, "OttO": {"segment": {"fast-rotation": {"threshold": 120}}}}})
        self.assertIn("hard-landing title", [x.strip() for x in page.eval_on_selector_all("#mine .it", "els => els.map(e => e.textContent)")])
        self.assertEqual(page.locator("#mine [data-untrick]").count(), 0)
        self.act(page, lambda: page.click("#mine [data-unseg=fast-rotation]"),
                 {"version": 1, "trick": own, "craft": {"default": {"params": {"flip_min_deg": 300}}, "OttO": {"segment": {"fast-rotation": {"threshold": 120}}}}})
        # the default quad's value beats the top level: at all quads its row writes there
        page.click(".trk[data-id=roll] .head .id")
        row = page.locator(".trk[data-id=roll] .prm[data-p=flip_min_deg]")
        self.act(page, lambda: (row.locator("[data-val]").fill("310"), row.locator("[data-val]").press("Tab")),
                 {"version": 1, "trick": own, "craft": {"default": {"params": {"flip_min_deg": 310}}, "OttO": {"segment": {"fast-rotation": {"threshold": 120}}}}})
        self.assertEqual(page.input_value(".trk[data-id=roll] .prm[data-p=flip_min_deg] [data-val]"), "310")
        self.act(page, lambda: page.click(".trk[data-id=roll] .prm[data-p=flip_min_deg] [data-reset]"), {"version": 1, "trick": own, "craft": {"OttO": {"segment": {"fast-rotation": {"threshold": 120}}}}})
        page.click("#scope [data-craft=OttO]")
        page.wait_for_selector("#quad")
        self.act(page, lambda: page.click("#mine [data-unseg=fast-rotation]"), {"version": 1, "trick": own})

    def test_a_trick_switched_in_the_default_quads_section(self):
        """At all quads, a trick the default quad's section switches is switched there: the top level would not win."""
        self.api.set_setting("tricks", {"version": 1, "craft": {"default": {"trick": [{"id": "roll", "enabled": False}]}}})
        page = self.open("/config#detection")
        self.assertFalse(page.is_checked(".trk[data-id=roll] [data-on]"))
        self.act(page, lambda: page.check(".trk[data-id=roll] [data-on]"), {"version": 1, "craft": {"default": {"trick": [{"id": "roll", "enabled": True}]}}})
        self.assertTrue(page.is_checked(".trk[data-id=roll] [data-on]"))

    def test_the_pilots_own_presets_and_the_default_quads_skill(self):
        self.api.set_setting("tricks", {"version": 1, "skill": None})
        page = self.open("/config#detection")
        self.assertFalse(page.is_disabled("#skill input[value=beginner]"))    # no skill at all: nothing to lock
        self.api.set_setting("tricks", {"version": 1, "skill": {"expert": {"flip_min_deg": 330}}})
        page = self.open("/config#detection")
        self.assertIn("own presets expert", page.text_content("#mine"))
        self.assertTrue(page.is_disabled("#skill input[value=beginner]"))     # the layer's skill is its presets: no choice to write over them
        self.assertEqual(page.text_content("#skillHint"), "Your layer's skill is its own presets (expert), so there is no choice for all quads here: pick one per quad.")
        page.click("#scope [data-craft=OttO]")
        page.wait_for_selector("#quad")
        self.assertEqual(page.eval_on_selector_all("#skill input", "els => els.map(e => e.value)"), ["inherit", "beginner", "advanced", "expert"])
        self.act(page, lambda: self.skill(page, "expert"), {"version": 1, "skill": {"expert": {"flip_min_deg": 330}}, "craft": {"OttO": {"skill": "expert"}}})
        self.assertTrue(page.text_content("#skillHint").startswith("expert (a roll from 330° instead of 270°): it sets 1 parameter at once."))
        self.api.set_setting("tricks", {"version": 1, "skill": "beginner", "craft": {"default": {"skill": "advanced"}}})
        page = self.open("/config#detection")
        self.assertIn("The default quad's skill, advanced, goes over this for every quad.", page.text_content("#skillHint"))
        page.click("#scope [data-craft=OttO]")
        page.wait_for_selector("#quad")
        self.assertEqual(page.text_content("#skillHint"), "OttO uses the skill chosen for all quads: advanced (the default quad's).")

    def test_names_the_page_does_not_own(self):
        """A craft or a link named like a property every JavaScript object has is a name like any other."""
        page = self.open("/config#constructor")
        self.assertEqual(page.evaluate("() => tab"), "tags")
        def seen(route):
            resp = route.fetch()
            d = resp.json()
            d["crafts_seen"] = [*d["crafts_seen"], "__proto__"]
            route.fulfill(response=resp, json=d)
        page = self.open("/config#detection")
        page.route(lambda url: url.endswith("/api/tricks"), seen)
        page.click("#scope [data-craft=OttO]")
        page.wait_for_selector("#quad")
        page.click("#allQuads")
        page.wait_for_function("() => D.crafts_seen.includes('__proto__')")
        page.click("#scope [data-craft=__proto__]")
        page.wait_for_selector("#quad")
        self.act(page, lambda: (page.fill("[data-prop=size_mm]", "75"), page.press("[data-prop=size_mm]", "Tab")), {"version": 1, "craft": {"__proto__": {"size_mm": 75}}})
        self.assertEqual(page.evaluate("() => [({}).size_mm, ({}).params]"), [None, None])

    def test_how_a_trick_is_found_reads_whole(self):
        page = self.open("/config#detection")
        page.click(".trk[data-id=hard-landing] .head .id")
        page.click(".trk[data-id=hard-landing] details[data-def] > summary")
        self.assertIn("windows starting from -0.5 s of the instant on", page.text_content(".trk[data-id=hard-landing] .def-lines"))

    def test_a_quad_with_a_long_name_on_the_phone(self):
        """Its spelling as written, the screen no wider, and its chip still there when its last override goes."""
        name = "AQuadWithAVeryLongNameThatNeverBreaksAnywhereAtAllNotEvenOnAPhoneScreen"
        self.api.set_setting("tricks", {"version": 1, "craft": {name: {"size_mm": 90}}})
        page = self.open("/config#detection", size=PHONE)
        page.click(f"#scope [data-craft={name}]")
        page.wait_for_selector("#quad")
        self.assertIn(name, page.inner_text("#mine .lbl"))                     # not in capitals like the label's words
        self.assertFalse(page.evaluate("() => document.documentElement.scrollWidth > innerWidth"))
        self.act(page, lambda: page.click("#mine [data-unprop=size_mm]"), {"version": 1})
        self.assertEqual(page.eval_on_selector_all("#scope .chip.on", "els => els.map(e => e.dataset.craft)"), [name])

    def test_after_a_save_a_quad_and_back(self):
        """A save's reload overtaken by a quad's, and that quad left before its answer: the last answer still lands."""
        page = self.open("/config#detection")
        page.evaluate(SLOW_ANSWER_ONCE, ["/api/tags", "craft=OttO"])
        with page.expect_response(lambda resp: "/api/tricks/layer?etag" in resp.url):
            page.uncheck(".trk[data-id=roll] [data-on]")
        page.click("#scope [data-craft=OttO]")
        page.click("#allQuads")
        page.wait_for_timeout(2500)
        self.assertEqual(self.layer(), {"version": 1, "trick": [{"id": "roll", "enabled": False}]})
        self.assertIn("14/15 tricks on", page.text_content("#status"))
        self.assertFalse(page.is_checked(".trk[data-id=roll] [data-on]"))

    def test_a_late_answer_for_the_same_quad(self):
        """Two loads for one quad answering out of order: the older is dropped."""
        self.api.set_setting("tricks", {"version": 1, "craft": {"OttO": {"size_mm": 75}}})
        page = self.open("/config#detection")
        page.evaluate(SLOW_ANSWER_ONCE, ["craft=OttO"])
        page.click("#scope [data-craft=OttO]")
        page.click("#allQuads")
        self.api.set_setting("tricks", {"version": 1, "craft": {"OttO": {"size_mm": 80}}})   # another writer, meanwhile
        page.click("#scope [data-craft=OttO]")
        page.wait_for_selector("#quad")
        page.wait_for_timeout(1800)                                            # the first answer, from before the change, comes in now
        self.assertEqual(page.input_value("[data-prop=size_mm]"), "80")

    def test_after_a_conflict_the_waiting_writes_go(self):
        """A stale tab: its first write is refused (someone else saved), and the one queued behind it is not sent on the
        fresh layer the pilot has not seen. The message stays."""
        page = self.open("/config#detection")
        self.api.set_setting("tricks", {"version": 1, "params": {"flip_min_deg": 240}})   # another writer
        page.click(".trk[data-id=roll] .head .id")
        page.fill(".trk[data-id=roll] .prm[data-p=flip_min_deg] [data-val]", "250")
        page.click(".trk[data-id=split-s] [data-on]")
        page.wait_for_function("() => /changed since/.test(document.querySelector('#err').textContent)")
        page.wait_for_timeout(1000)
        self.assertEqual(self.layer(), {"version": 1, "params": {"flip_min_deg": 240}})
        self.assertIn("changed since", page.text_content("#err"))
        self.assertEqual(page.input_value(".trk[data-id=roll] .prm[data-p=flip_min_deg] [data-val]"), "240")

    def test_the_raw_layer_is_sent_as_it_was_drawn(self):
        """A switch, then the raw layer's save straight after: the text was drawn before the switch, so it is refused
        rather than putting the switch back."""
        page = self.open("/config#detection")
        self.fold(page, "advanced")
        page.uncheck(".trk[data-id=roll] [data-on]")
        page.click("#layerSave")
        page.wait_for_function("() => /changed since/.test(document.querySelector('#err').textContent)")
        self.assertEqual(self.layer(), {"version": 1, "trick": [{"id": "roll", "enabled": False}]})

    def test_typing_outlives_a_redraw(self):
        """A value typed in the next box while the last one saves is still there, and still being typed, after the redraw."""
        page = self.open("/config#detection")
        page.click(".trk[data-id=roll] .head .id")
        rows = page.eval_on_selector_all(".trk[data-id=roll] .prm", "els => els.map(e => e.dataset.p)")
        page.evaluate(SLOW_ANSWER_ONCE, ["/api/tricks/layer?etag"])
        first = page.locator(f".trk[data-id=roll] .prm[data-p={rows[0]}] [data-val]")
        first.fill("0.25")
        before = page.evaluate("() => renders")
        page.keyboard.press("Tab")
        page.keyboard.type("7")
        page.wait_for_function("(n) => renders > n", arg=before)
        self.assertEqual(page.evaluate("() => [document.activeElement.closest('.prm')?.dataset.p, document.activeElement.value]"), [rows[1], "7"])
        self.act(page, lambda: page.keyboard.press("Tab"), {"version": 1, "params": {rows[0]: 0.25, rows[1]: 7}})

    def test_the_filter_and_a_quad_that_did_not_load(self):
        page = self.open("/config#detection")
        self.fold(page, "params")
        page.fill("#pfilter", "orbit")
        calls = []
        def once_broken(route):
            calls.append(1)
            if len(calls) == 1:
                route.fulfill(status=502, body="<html>Bad Gateway</html>", content_type="text/html")
            else:
                route.continue_()
        page.route(lambda url: "/api/tricks?craft=" in url, once_broken)
        page.click("#scope [data-craft=OttO]")
        page.wait_for_function("() => /nothing loaded/.test(document.querySelector('#err').textContent)")
        page.click("#scope [data-craft=OttO]")                                 # the chip tries again
        page.wait_for_selector("#quad")
        self.assertEqual(page.input_value("#pfilter"), "orbit")                # the filter came along

    def test_a_parameter_named_like_an_objects_own(self):
        self.api.set_setting("tricks", {"version": 1, "params": {"toString": 1},
                                        "trick": [{"id": "odd", "tag": "orbit", "segment": "fast-rotation", "when": ["toString > 0"], "title": "odd", "why": "odd"}]})
        page = self.open("/config#detection")
        self.assertEqual(page.locator("#tricks .trk").count(), 16)
        self.assertEqual(page.locator(".trk[data-id=roll] .head .src").count(), 0)

    # ---- detection: the tricks, the checks ----------------------------------------------------------------------

    def test_tricks_switch_show_check_and_eval(self):
        page = self.open("/config#detection")
        self.fold(page, "check")
        self.assertEqual(page.input_value("#evalSession"), S)
        page.click("#evalBtn")
        page.wait_for_function("() => document.querySelector('#evalList table')", timeout=60000)
        self.assertEqual(page.eval_on_selector_all("#evalList table td", "els => els.map(e => e.textContent)"), ["roll", "1", "0"])   # the attempt finds the roll moment
        self.act(page, lambda: page.uncheck("#tricks .trk[data-id=roll-attempt] [data-on]"), {"version": 1, "trick": [{"id": "roll-attempt", "enabled": False}]})
        self.assertTrue(page.locator("#tricks .trk[data-id=roll-attempt]").evaluate("e => e.classList.contains('off')"))
        self.assertEqual(self.events(), [])
        self.assertIn("14/15 tricks on", page.text_content("#status"))
        self.assertIn("roll-attempt off", page.text_content("#mine"))
        page.click("#evalBtn")
        page.wait_for_function("() => document.querySelector('#evalList table td.bad')", timeout=60000)
        self.assertEqual(page.eval_on_selector_all("#evalList table td", "els => els.map(e => e.textContent)"), ["roll", "0", "1"])   # and without it the moment is missed
        self.assertIn("a roll", page.text_content("#evalList"))
        page.click("#tricks .trk[data-id=roll] .head .id")
        page.click("#tricks .trk[data-id=roll] .def-lines > summary")
        det = page.locator("#tricks .trk[data-id=roll] .def-lines")
        self.assertTrue(page.is_visible("#tricks .trk[data-id=roll] .def-lines code"))
        self.assertIn("abs(rotation_peak_deg.roll) >= flip_min_deg", det.text_content())
        self.assertIn("arm 1 at 6.5", det.text_content())
        page.click("#tricks .trk[data-id=split-s] .head .id")
        self.assertIn("phase 2 pitch-back within 1 s", page.locator("#tricks .trk[data-id=split-s] .def-lines").text_content())
        page.click("#checkBtn")
        page.wait_for_function("() => document.querySelector('#checkOut').textContent.includes('unchecked')", timeout=60000)
        self.assertIn("0 failed", page.text_content("#checkOut"))          # the real logs are not on this server: unchecked, never failed
        self.assertGreater(page.locator("#checkList .m").count(), 10)
        self.act(page, lambda: page.click("#mine [data-untrick=roll-attempt]"), {"version": 1})
        self.assertTrue(page.is_checked("#tricks .trk[data-id=roll-attempt] [data-on]"))

    # ---- detection: the tricks file and the raw layer -----------------------------------------------------------

    def test_raw_layer_and_tricks_file(self):
        page = self.open("/config#detection")
        self.fold(page, "advanced")
        page.fill("#layerText", "{not json")
        page.click("#layerSave")
        page.wait_for_function("() => document.querySelector('#err').classList.contains('on')")
        self.assertIn("not JSON", page.text_content("#err"))
        page.fill("#layerText", json.dumps({"version": 1, "params": {"flip_min_dg": 240}}))
        page.click("#layerSave")
        page.wait_for_function("() => /flip_min_dg/.test(document.querySelector('#err').textContent)")
        self.assertIn("flip_min_dg", page.input_value("#layerText"))             # there to be mended
        page.wait_for_function("() => document.querySelector('#err').textContent.includes('flip_min_dg')")
        self.assertEqual(self.layer(), {})                                   # refused: nothing stored
        self.act(page, lambda: (page.fill("#layerText", json.dumps({"version": 1, "params": {"flip_min_deg": 250}, "skill": "advanced"})), page.click("#layerSave")),
                 {"version": 1, "params": {"flip_min_deg": 250}, "skill": "advanced"})
        self.assertFalse(page.evaluate("() => document.querySelector('#err').classList.contains('on')"))
        self.assertIn("database", page.text_content("#sources"))
        page.on("dialog", lambda d: d.accept())
        self.act(page, lambda: page.click("#layerClear"), {})
        # the tricks file: the export link, and one pasted in, replacing then merging
        self.assertEqual(page.get_attribute("#exportLink", "href"), "/api/tricks/export")
        self.assertEqual(page.get_attribute("#exportLink", "download"), "acroscope-tricks.toml")
        self.assertTrue(page.is_disabled("#importReplace"))
        page.fill("#importText", 'version = 1\nskill = "beginner"\n[params]\norbit_min_deg = 240\n')
        self.assertFalse(page.is_disabled("#importReplace"))
        self.act(page, lambda: page.click("#importReplace"), {"version": 1, "skill": "beginner", "params": {"orbit_min_deg": 240}})
        self.assertEqual(page.get_attribute("section.on", "data-panel"), "detection")
        self.assertTrue(page.is_visible("#importText"))                      # still there, the fold still open
        page.fill("#importText", "[params]\nflip_min_deg = 250\n")
        self.act(page, lambda: page.click("#importMerge"), {"version": 1, "skill": "beginner", "params": {"orbit_min_deg": 240, "flip_min_deg": 250}})
        page.fill("#importText", "[params]\nflip_min_dg = 250\n")
        page.click("#importReplace")
        page.wait_for_function("() => document.querySelector('#err').textContent.includes('flip_min_dg')")
        self.assertEqual(self.layer(), {"version": 1, "skill": "beginner", "params": {"orbit_min_deg": 240, "flip_min_deg": 250}})

    def test_a_broken_layer_still_shows_the_page_and_its_clear_button(self):
        """A stored layer a later release made invalid: the page says so, the tabs still draw, the raw layer clears it."""
        import sqlite3
        self.api.set_setting("tricks", {"version": 1, "skill": "beginner"})
        db = sqlite3.connect(Path(self.tmp.name) / "t.db")
        db.execute("UPDATE settings SET value = ? WHERE key = 'tricks'", (json.dumps({"version": 1, "params": {"roll_hold_share": 0.85}}),))
        db.commit(); db.close()
        page = self.open()
        self.assertIn("roll_hold_share", page.text_content("#err"))
        self.assertIn("raw layer under detection", page.text_content("#err"))
        self.assertIn("15/15 tricks on", page.text_content("#status"))
        page.click("#tabs [data-tab=detection]")
        self.fold(page, "advanced")
        self.assertIn("roll_hold_share", page.input_value("#layerText"))
        before = page.evaluate("() => renders")
        page.uncheck(".trk[data-id=roll] [data-on]")                         # refused: the whole layer is checked
        page.wait_for_function("(n) => renders > n", arg=before)
        self.assertTrue(page.is_checked(".trk[data-id=roll] [data-on]"))
        page.on("dialog", lambda d: d.accept())
        self.act(page, lambda: page.click("#layerClear"), {})
        self.assertFalse(page.evaluate("() => document.querySelector('#err').classList.contains('on')"))

    def test_a_stale_tab_does_not_overwrite(self):
        """Two tabs on the same layer: the second save is refused and that tab reloads."""
        page = self.open("/config#detection")
        other = self.open("/config#detection")
        self.fold(page, "params")
        self.fold(other, "params")
        row = page.locator("#params .prm[data-p=flip_min_deg]")
        self.act(page, lambda: (row.locator("[data-val]").fill("240"), row.locator("[data-val]").press("Tab")), {"version": 1, "params": {"flip_min_deg": 240}})
        before = other.evaluate("() => renders")
        self.skill(other, "beginner")
        other.wait_for_function("(n) => renders > n", arg=before)
        self.assertIn("changed since", other.text_content("#err"))
        self.assertEqual(self.layer(), {"version": 1, "params": {"flip_min_deg": 240}})        # the first tab's write stands
        self.assertEqual(other.input_value("#params .prm[data-p=flip_min_deg] [data-val]"), "240")   # and the second tab shows it

    # ---- tags ----------------------------------------------------------------------------------------------------

    def test_tags_moved_here(self):
        page = self.open("/config#tags")
        page.fill("#newCat", "cruise")
        page.click("#addCat")
        page.wait_for_selector("[data-cn=cruise]")
        page.fill("#newTag", "hover")
        page.select_option("#newTag + select", "cruise")
        page.click("#addTag")
        page.wait_for_selector("[data-tn=hover]")
        t = self.api.tags()
        self.assertIn(("hover", "cruise"), [(x["name"], x["category"]) for x in t["tags"]])
        page.locator("[data-tn=hover] [data-tcolor]").evaluate("e => { e.value = '#112233'; e.dispatchEvent(new Event('change')); }")
        page.wait_for_function("() => document.querySelector('[data-tn=hover] [data-tcolor]').value === '#112233'")
        self.assertEqual(next(x["color"] for x in self.api.tags()["tags"] if x["name"] == "hover"), "#112233")
        page.click("[data-tn=hover] [data-tdel]")
        page.wait_for_function("() => !document.querySelector('[data-tn=hover]')")
        page.click("[data-cn=cruise] [data-cdel]")
        page.wait_for_function("() => !document.querySelector('[data-cn=cruise]')")
        self.assertNotIn("cruise", [c["name"] for c in self.api.tags()["categories"]])


    # ---- the shares tab (#57) -----------------------------------------------------------------------------------

    def test_shares_tab(self):
        """What is up, with the moment's title, the clip and the link; take down; the expired and revoked ones on
        request. The rows come from the database (the bundles themselves are the player's business)."""
        from acroscope.store.db import Db
        from datetime import datetime, timedelta, timezone
        db = Db(Path(self.tmp.name) / "t.db")
        future = (datetime.now(timezone.utc) + timedelta(days=9)).replace(microsecond=0).isoformat()
        past = (datetime.now(timezone.utc) - timedelta(days=1)).replace(microsecond=0).isoformat()
        db.add_share("u" * 24, S, "2026-10-04_001.mp4", "m01", 9.5, 15.0, "a roll, nearly", future)
        db.add_share("e" * 24, S, "2026-10-04_001.mp4", "m01", 9.5, 15.0, "old", past)
        page = self.open("/config#shares")
        page.wait_for_selector(".shrow")
        rows = page.eval_on_selector_all(".shrow", "els => els.map(e => [e.dataset.token, e.querySelector('.st').textContent, e.querySelector('.what b').textContent, [...e.querySelectorAll('.what .dim')].map(d => d.textContent).join(' | ')])")
        self.assertEqual(len(rows), 1)                                                     # the expired one is not up
        self.assertEqual(rows[0][:3], ["u" * 24, "up", "a roll"])                          # the moment's title first
        self.assertIn("2026-10-04_001 0:09.5–0:15.0 · a roll, nearly", rows[0][3])         # the clip's readable name, the window, the text
        self.assertIn("until " + page.evaluate("x => new Date(x).toLocaleDateString()", future), rows[0][3])   # the reader's day, not the UTC date (#59 review)
        self.assertIn("no public URL", page.text_content("#shBase"))                       # this server has none
        self.assertIsNone(page.query_selector(".shrow a.btn:not([data-moment])"))           # so no link to open
        self.assertEqual(page.get_attribute(".shrow a[data-moment]", "href"), f"/?session={S}&clip=2026-10-04_001&m=m01&share=1")   # but one to the moment, panel open
        page.check("#shAll")
        page.wait_for_function("() => document.querySelectorAll('.shrow').length === 2")
        self.assertEqual(sorted(page.eval_on_selector_all(".shrow .st", "els => els.map(e => e.textContent)")), ["expired", "up"])
        page.once("dialog", lambda d: d.accept())
        page.click(f'.shrow[data-token="{"u" * 24}"] [data-down]')
        page.wait_for_function("() => [...document.querySelectorAll('.shrow .st')].map(e => e.textContent).sort().join() === 'expired,revoked'")
        self.assertEqual(db.share("u" * 24)["state"], "revoked")
        self.assertIsNone(page.query_selector(".shrow [data-down]"))

    # ---- devices --------------------------------------------------------------------------------------------------

    def drop_status(self, token: str) -> int:
        """What the drop's door answers to a token."""
        try:
            return urllib.request.urlopen(urllib.request.Request(self.url + "/drop/device", headers={"Authorization": "Bearer " + token})).status
        except urllib.error.HTTPError as e:
            return e.code

    def test_devices_tab(self):
        """A device is paired here (#75): its code is shown once, it holds the server's address and a token the
        drop takes; the list says what is paired and when it was last seen; a revoke stops the token."""
        from urllib.parse import parse_qs, urlparse
        devices = lambda: json.loads(urllib.request.urlopen(self.url + "/api/devices").read())["devices"]
        before = devices()                                                  # what other tests paired (and revoked) stays listed
        page = self.open("/config#devices")
        page.wait_for_selector("#dvAdd")
        page.wait_for_function("(n) => document.querySelector('#dvAt').textContent && document.querySelectorAll('.shrow[data-device]').length === n", arg=len(before))
        if not before:
            self.assertIn("no device paired", page.text_content("#dvList"))
        page.click("#dvAdd")                                                # no name: refused, and nothing is made
        page.wait_for_selector("#err.on")
        self.assertIn("name", page.text_content("#err"))
        self.assertEqual(devices(), before)
        self.assertIsNone(page.query_selector("#dvLink"))
        page.fill("#dvName", "field <b>phone</b>")
        page.click("#dvAdd")
        page.wait_for_selector("#dvLink")
        self.assertFalse(page.is_visible("#err"))                           # the earlier refusal is not left standing
        link = page.input_value("#dvLink")
        self.assertTrue(link.startswith("acroscope://pair?"), link)
        q = parse_qs(urlparse(link).query)
        self.assertEqual((q["url"], set(q)), ([self.url], {"url", "token"}))   # where the app finds the server, and its token
        token = q["token"][0]
        self.assertEqual(page.get_attribute("#dvOpen", "href"), link)
        self.assertEqual(page.input_value("#dvName"), "")
        [d] = [x for x in devices() if x not in before]
        self.assertEqual((d["name"], d["last_seen_at"], d["revoked_at"]), ("field <b>phone</b>", None, None))
        def row(p, has="") -> list:
            """The device's row as the page shows it, once the list is loaded (and says `has`)."""
            p.wait_for_function("([id, has]) => { const e = document.querySelector(`.shrow[data-device=\"${id}\"]`); return e && e.textContent.includes(has); }",
                                arg=[d["id"], has])
            return p.eval_on_selector(f'.shrow[data-device="{d["id"]}"]', "e => [e.querySelector('.st').textContent, e.querySelector('.what b').textContent, e.querySelector('.what .dim').textContent]")
        self.assertEqual(row(page)[:2], ["paired", "field <b>phone</b>"])    # the name as text, not as markup
        self.assertIn("not seen yet", row(page)[2])
        self.assertIn("field <b>phone</b> is paired", page.text_content("#dvCode"))
        self.assertEqual(self.drop_status(token), 200)                      # the server holds it: the token opens the drop
        page.click("#tabs [data-tab=tags]")                                 # another tab and back: the code is still to be read
        page.click("#tabs [data-tab=devices]")
        page.wait_for_selector("#dvLink")
        self.assertEqual(page.input_value("#dvLink"), link)
        today = page.evaluate("() => new Date().toLocaleDateString()")
        self.assertIn(f"last seen {today}", row(page, "last seen")[2])      # and the device was seen meanwhile
        again = self.open("/config#devices")                                # the page loaded again: the code was said once
        again.wait_for_selector(f'.shrow[data-device="{d["id"]}"]')
        self.assertIsNone(again.query_selector("#dvLink"))
        self.assertNotIn(token, again.content())
        self.assertEqual(row(again)[0], "paired")
        again.once("dialog", lambda dlg: dlg.dismiss())                     # asked first: a no leaves it paired
        again.click(f'[data-revoke="{d["id"]}"]')
        self.assertEqual(self.drop_status(token), 200)
        again.once("dialog", lambda dlg: dlg.accept())
        again.click(f'[data-revoke="{d["id"]}"]')
        self.assertEqual(row(again, "revoked")[0], "revoked")
        self.assertEqual(self.drop_status(token), 401)
        self.assertTrue(next(x for x in devices() if x["id"] == d["id"])["revoked_at"])
        self.assertIsNone(again.query_selector(f'.shrow[data-device="{d["id"]}"] [data-revoke]'))
        self.assertIn(f"revoked {today}", row(again)[2])
        # the code box goes when its device is revoked on the page that still shows it
        page.fill("#dvName", "second phone")
        page.click("#dvAdd")
        page.wait_for_function("() => document.querySelector('#dvCode').textContent.includes('second phone')")
        second = [x for x in devices() if x["name"] == "second phone"][-1]
        page.wait_for_function("(n) => document.querySelectorAll('.shrow[data-device]').length === n", arg=len(before) + 2)
        self.assertEqual(page.eval_on_selector_all(".shrow[data-device]", "els => els.map(e => e.dataset.device)")[:2], [second["id"], d["id"]])   # the newest first
        page.once("dialog", lambda dlg: dlg.accept())
        page.click(f'[data-revoke="{second["id"]}"]')
        page.wait_for_function("() => !document.querySelector('#dvLink')")
        self.assertEqual([x["revoked_at"] is not None for x in devices() if x["id"] in (d["id"], second["id"])], [True, True])

    def test_the_pairing_code_names_the_drops_own_address(self):
        """A drop on a host of its own (the server says so): the code sends the app there, not to this page's host."""
        page = self.open("/config#tags")

        def with_address(route):
            answer = route.fetch()
            route.fulfill(response=answer, json={**answer.json(), "url": "https://drop.example.org"})
        page.route("**/api/devices", lambda route: with_address(route) if route.request.method == "GET" else route.continue_())
        page.click("#tabs [data-tab=devices]")
        page.wait_for_function("() => document.querySelector('#dvAt') && document.querySelector('#dvAt').textContent.includes('drop.example.org')")
        page.fill("#dvName", "phone for the drop's host")
        page.click("#dvAdd")
        page.wait_for_selector("#dvLink")
        from urllib.parse import parse_qs, urlparse
        self.assertEqual(parse_qs(urlparse(page.input_value("#dvLink")).query)["url"], ["https://drop.example.org"])
        plain = self.open("/config#devices")                                 # and where the server names none, this page's own
        plain.wait_for_selector(".shrow[data-device]")
        self.assertIn(self.url.split("//")[1], plain.text_content("#dvAt"))
        for d in json.loads(urllib.request.urlopen(self.url + "/api/devices").read())["devices"]:
            urllib.request.urlopen(urllib.request.Request(f"{self.url}/api/devices/{d['id']}", method="DELETE"))

    def test_devices_tab_on_the_phone(self):
        page = self.open("/config#devices", size=PHONE)
        page.wait_for_selector("#dvAdd")
        page.fill("#dvName", "a phone with quite a long name, as long as a name may be here")
        page.click("#dvAdd")
        page.wait_for_selector("#dvLink")
        page.wait_for_selector(".shrow[data-device]")
        self.assertFalse(page.evaluate("() => document.documentElement.scrollWidth > innerWidth"))   # the code and the row stay on the screen
        box = page.eval_on_selector("#dvLink", "e => { const r = e.getBoundingClientRect(); return [r.left, r.right, r.width]; }")
        self.assertGreaterEqual(box[0], 0)
        self.assertLessEqual(box[1], 412)
        self.assertGreater(box[2], 100)
        self.assertEqual(page.get_attribute("#dvName", "maxlength"), "60")
        for d in json.loads(urllib.request.urlopen(self.url + "/api/devices").read())["devices"]:
            urllib.request.urlopen(urllib.request.Request(f"{self.url}/api/devices/{d['id']}", method="DELETE"))

    # ---- osd -----------------------------------------------------------------------------------------------------

    def osd_doc(self) -> dict:
        return json.loads(urllib.request.urlopen(self.url + "/api/osd/doc").read())["doc"]

    def read(self, page) -> dict:
        """What the readout says once the look at the frame is back."""
        page.wait_for_function("() => document.querySelector('#oRead b[data-v=arm]') && !/reading/.test(document.querySelector('#oRead').textContent)", timeout=60000)
        return {k: page.text_content(f"#oRead b[data-v={k}]") for k in ("arm", "total", "counter")}

    def test_osd_profile_from_a_dump_and_a_font(self):
        """#10: paste a dump, load a font of your own, look at a frame of a clip, fit the grid, save; the server then
        holds the profile and the reader uses it. The clip's OSD is drawn in the synthetic font on the grid the DVR
        flown here uses (x 3, y -4)."""
        urllib.request.urlopen(urllib.request.Request(self.url + "/api/osd/doc", method="DELETE")).read()
        page = self.open("/config#osd")
        page.wait_for_selector("#oDump")
        self.assertIn("otto-bold-v3", page.text_content("#osdPanel .hint"))        # the shipped profiles are named
        self.assertTrue(page.locator("#oNew").evaluate("e => e.classList.contains('on')"))
        page.select_option("#oSession", OS)
        page.wait_for_function(f"() => OE.session === {OS!r} && OE.video === '2026-10-05_001.mp4'")
        page.fill("#oT", "10.3")
        page.dispatch_event("#oT", "change")
        with page.expect_file_chooser() as fc:                                     # "your own .mcm…" opens the file picker
            page.select_option("#oFont", "")
        fc.value.set_files(str(Path(self.tmp.name) / "synth.mcm"))
        page.wait_for_function("() => OE.mcm && OE.mcmName === 'synth'")
        page.fill("#oDump", osyn.DUMP)
        self.assertEqual(self.read(page), {"arm": "0:06.3", "total": "0:36", "counter": "#7"})
        self.assertIn("timer 2: total dd:dd at column 22, row 11", page.text_content("#oDumpSum"))
        self.assertIn("synth (yours)", page.text_content("#oFont option:checked"))
        self.assertEqual(page.evaluate("() => OE.probe.cells.filter(c => c.kind === 'timer' && c.fmt === 'd' && c.score > .9).length"), 9)
        # a grid off by a few pixels reads nothing; fitting it on the frame finds the DVR's again
        page.fill("#oX", "-1")
        page.dispatch_event("#oX", "change")
        page.wait_for_function("() => OE.profile.grid.x === -1 && OE.probe && OE.probe.values.total !== 36", timeout=60000)
        page.click("#oFit")
        page.wait_for_function("() => OE.profile.grid.x === 3 && OE.profile.grid.y === -4", timeout=120000)
        self.assertEqual(self.read(page)["total"], "0:36")
        self.assertIn("grid fitted: x 3, y -4", page.text_content("#oSaved"))
        # the STATS screen at 26 s: its rows found by their labels
        page.click("[data-dt='10']")
        page.click("[data-dt='10']")
        page.click("[data-dt='-1']")
        page.click("[data-dt='-1']")
        page.click("[data-dt='-1']")
        page.click("[data-dt='-1']")
        page.wait_for_function("() => +OE.t === 26.3 && OE.probe && OE.probe.stats", timeout=60000)
        self.assertEqual((page.evaluate("() => OE.probe.stats.total_arm"), page.evaluate("() => OE.probe.stats.blackbox")), (50, 42))
        # save under a name: the server holds it, the reader takes it
        page.fill("#oName", "synth-v3")
        page.click("#oSave")
        page.wait_for_function("() => /saved/.test(document.querySelector('#oSaved').textContent)")
        doc = self.osd_doc()
        self.assertEqual(list(doc["profiles"]), ["synth-v3"])
        self.assertEqual(doc["profiles"]["synth-v3"]["font"]["name"], "synth")
        self.assertEqual(doc["profiles"]["synth-v3"]["grid"]["x"], 3)
        self.assertNotIn("pilot_name", doc["profiles"]["synth-v3"]["settings"])
        out = self.api.cli(["osd", OS, "001"])
        self.assertIn("layout synth-v3", out["stdout"], out["stderr"])
        # a fresh page lists it and opens it as it was saved
        page = self.open("/config#osd")
        page.wait_for_selector("[data-oname=synth-v3]")
        page.select_option("#oSession", OS)
        page.wait_for_function(f"() => OE.video === '2026-10-05_001.mp4'")
        page.fill("#oT", "10.3")
        page.dispatch_event("#oT", "change")
        page.click("[data-oname=synth-v3]")
        self.assertEqual(self.read(page)["total"], "0:36")
        self.assertEqual(page.input_value("#oName"), "synth-v3")
        page.once("dialog", lambda d: d.accept())
        page.click("#oDelete")
        page.wait_for_function("() => !document.querySelector('[data-oname=synth-v3]')")
        self.assertEqual(self.osd_doc()["profiles"], {})

    def test_osd_refuses_what_does_not_derive(self):
        urllib.request.urlopen(urllib.request.Request(self.url + "/api/osd/doc", method="DELETE")).read()
        page = self.open("/config#osd")
        page.wait_for_selector("#oDump")
        with page.expect_file_chooser() as fc:
            page.select_option("#oFont", "")
        fc.value.set_files(str(Path(self.tmp.name) / "synth.mcm"))
        page.wait_for_function("() => OE.mcm")
        page.fill("#oDump", "set osd_vbat_pos = 2433\n")                           # no timer shown
        page.wait_for_function("() => document.querySelector('#err').classList.contains('on')")
        self.assertIn("no timer to read", page.text_content("#err"))
        self.assertTrue(page.is_disabled("#oSave"))
        self.assertEqual(self.osd_doc(), {})

    def osd_ready(self, page):
        """The OSD tab on the synthetic clip at 10.3 s with the dump and the synthetic font: read once."""
        page.wait_for_selector("#oDump")
        page.select_option("#oSession", OS)
        page.wait_for_function(f"() => OE.session === {OS!r} && OE.video === '2026-10-05_001.mp4'")
        page.fill("#oT", "10.3")
        page.dispatch_event("#oT", "change")
        with page.expect_file_chooser() as fc:
            page.select_option("#oFont", "")
        fc.value.set_files(str(Path(self.tmp.name) / "synth.mcm"))
        page.wait_for_function("() => OE.mcm")
        page.fill("#oDump", osyn.DUMP)
        self.assertEqual(self.read(page)["total"], "0:36")

    def test_osd_review_on_the_page(self):
        """#63 review on the page: another session clears the last clip's reading; a double click saves once; an
        answer that is not acroscope's (a proxy's 502) is said and the buttons come back; a stored profile that no
        longer derives can be deleted from its chip, the others kept."""
        urllib.request.urlopen(urllib.request.Request(self.url + "/api/osd/doc", method="DELETE")).read()
        page = self.open("/config#osd")
        self.osd_ready(page)
        # another session: the old clip's cells are not painted over the new frame
        page.select_option("#oSession", S)
        page.wait_for_function(f"() => OE.session === {S!r} && OE.probe === null")
        page.wait_for_function("() => !/0:36/.test(document.querySelector('#oRead').textContent)")
        page.select_option("#oSession", OS)
        page.wait_for_function(f"() => OE.video === '2026-10-05_001.mp4'")
        page.fill("#oT", "10.3")
        page.dispatch_event("#oT", "change")
        self.assertEqual(self.read(page)["total"], "0:36")
        # a double click on save: one save, no false "changed since"
        posts = []
        page.on("request", lambda r: posts.append(r.url) if r.method == "POST" and "/api/osd/doc" in r.url else None)
        page.fill("#oName", "twice")
        page.dblclick("#oSave")
        page.wait_for_function("() => /saved/.test(document.querySelector('#oSaved').textContent)")
        self.assertEqual(len(posts), 1)
        self.assertFalse(page.locator("#err").evaluate("e => e.classList.contains('on')"))
        # a gateway's error page instead of an answer: said, and fit can be pressed again
        page.route("**/api/osd/probe", lambda route: route.fulfill(status=502, body="<html>Bad Gateway</html>", content_type="text/html"))
        page.click("#oFit")
        page.wait_for_function("() => document.querySelector('#err').classList.contains('on')")
        self.assertIn("502", page.text_content("#err"))
        self.assertFalse(page.is_disabled("#oFit"))
        page.unroute("**/api/osd/probe")
        page.route("**/api/osd/probe", lambda route: route.abort())            # no answer at all (the server gone)
        page.click("#oRankBtn")
        page.wait_for_function("() => /no answer from the server/.test(document.querySelector('#err').textContent)")
        self.assertFalse(page.is_disabled("#oRankBtn"))
        page.unroute("**/api/osd/probe")
        # a stored profile that no longer derives (a stricter check since): its chip deletes it, the others stay
        from acroscope.store.db import Db
        db = Db(Path(self.tmp.name) / "t.db")
        doc = db.setting("osd")["value"]
        doc["profiles"]["stale"] = {**doc["profiles"]["twice"], "grid": {**doc["profiles"]["twice"]["grid"], "cw": 7}}
        db.set_setting("osd", doc)
        page = self.open("/config#osd")
        page.wait_for_selector("[data-obroken=stale]")
        page.once("dialog", lambda d: d.accept())
        page.click("[data-obroken=stale]")
        page.wait_for_function("() => !document.querySelector('[data-obroken=stale]')")
        self.assertEqual(sorted(self.osd_doc()["profiles"]), ["twice"])
        urllib.request.urlopen(urllib.request.Request(self.url + "/api/osd/doc", method="DELETE")).read()

    def test_a_failed_load_overwrites_nothing(self):
        """Second review: a 502 for the layer on the page's first load used to leave an empty layer in the page, and the
        next knob saved it over the pilot's. Nothing is loaded, it is said, and nothing can be saved over it."""
        self.api.set_setting("tricks", {"version": 1, "skill": "beginner", "params": {"tumble_dps": 1234}})
        ctx = self.browser.new_context(**DESKTOP)
        self.addCleanup(ctx.close)
        page = ctx.new_page()
        page.route("**/api/tricks/layer", lambda route: route.fulfill(status=502, body="<html>Bad Gateway</html>", content_type="text/html")
                   if route.request.method == "GET" else route.continue_())
        page.goto(self.url + "/config")
        page.wait_for_function("() => document.querySelector('#err').classList.contains('on')")
        self.assertIn("nothing loaded", page.text_content("#err"))
        page.evaluate("() => setParam('flip_min_deg', 200)")                  # what a knob does: posts the page's layer
        page.wait_for_function("() => /changed since|reload/.test(document.querySelector('#err').textContent)")
        self.assertEqual(self.layer(), {"version": 1, "skill": "beginner", "params": {"tumble_dps": 1234}})

    def test_osd_tab_after_a_failed_load_and_a_stale_delete(self):
        """Second review: one failed load of the OSD profiles broke the tab until a reload; the broken-profile chip
        retried a stale etag after a 409."""
        urllib.request.urlopen(urllib.request.Request(self.url + "/api/osd/doc", method="DELETE")).read()
        page = self.open("/config#detection")
        page.route("**/api/osd", lambda route: route.fulfill(status=502, body="<html>Bad Gateway</html>", content_type="text/html"))
        page.click("#tabs [data-tab=osd]")
        page.wait_for_function("() => /could not be loaded/.test(document.querySelector('#osdPanel').textContent)")
        page.unroute("**/api/osd")
        page.click("#tabs [data-tab=tags]")
        page.click("#tabs [data-tab=osd]")
        page.wait_for_selector("#oDump")                                        # asked again, and loaded
        # a broken stored profile, and the document changed by someone else after the page loaded it
        from acroscope.store.db import Db
        from acroscope.sources import osdprofile
        db = Db(Path(self.tmp.name) / "t.db")
        good = osdprofile.make(osyn.DUMP, mcm=osyn.mcm_text(osyn.font_chars()), font="synth")
        db.set_setting("osd", {"version": 1, "profiles": {"good": good, "stale": {**good, "grid": {**good["grid"], "cw": 7}}}})
        page = self.open("/config#osd")
        page.wait_for_selector("[data-obroken=stale]")
        db.set_setting("osd", {"version": 1, "profiles": {"good": {**good, "note": "elsewhere"}, "stale": {**good, "grid": {**good["grid"], "cw": 7}}}})
        page.on("dialog", lambda d: d.accept())
        etag = page.evaluate("() => ODOC.etag")
        page.click("[data-obroken=stale]")
        page.wait_for_function("() => /changed since/.test(document.querySelector('#err').textContent)")
        page.wait_for_function("(e) => ODOC.etag !== e && document.querySelector('[data-obroken=stale]')", arg=etag)   # loaded again
        page.click("[data-obroken=stale]")                                      # so the retry goes through
        page.wait_for_function("() => !document.querySelector('[data-obroken=stale]')")
        self.assertEqual(self.osd_doc()["profiles"]["good"]["note"], "elsewhere")
        self.assertNotIn("stale", self.osd_doc()["profiles"])
        urllib.request.urlopen(urllib.request.Request(self.url + "/api/osd/doc", method="DELETE")).read()


if __name__ == "__main__":
    unittest.main()
