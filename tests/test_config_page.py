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

from acroscope import blackbox

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
        from acroscope.remote import Remote
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

    def test_tabs_and_the_way_in(self):
        page = self.open()
        self.assertEqual(page.eval_on_selector_all("#tabs span", "els => els.map(e => e.dataset.tab)"), ["profile", "drones", "tricks", "settings", "tags", "shares", "osd"])
        self.assertEqual(page.get_attribute("section.on", "data-panel"), "profile")
        self.assertIn("15 of 15 tricks on", page.text_content("#status"))
        for t in ("drones", "tricks", "settings", "tags", "osd", "shares"):
            page.click(f"#tabs [data-tab={t}]")
            self.assertEqual(page.get_attribute("section.on", "data-panel"), t)
            if t == "osd":
                page.wait_for_selector("#oDump")                         # its panel loads the profiles first
            self.assertTrue(page.eval_on_selector(f"section[data-panel={t}]", "e => e.innerHTML.length > 100"), t)
        self.assertEqual(page.evaluate("() => location.hash"), "#shares")
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
        page = self.open(size=PHONE)
        self.assertTrue(page.evaluate("() => document.body.classList.contains('phone')"))
        r = page.evaluate("() => { const t = document.getElementById('tabs').getBoundingClientRect(); return {bottom: t.bottom, h: innerHeight, w: t.width, iw: innerWidth}; }")
        self.assertAlmostEqual(r["bottom"], r["h"], delta=1)                 # the tab bar at the bottom of the screen, where the thumb is
        self.assertEqual(r["w"], r["iw"])
        self.assertFalse(page.evaluate("() => document.documentElement.scrollWidth > innerWidth"))   # nothing pokes out sideways
        page.click("#tabs [data-tab=drones]")
        self.assertFalse(page.evaluate("() => document.documentElement.scrollWidth > innerWidth"))
        self.assertTrue(page.is_visible("#crafts [data-craft=OttO]"))
        # seven tabs side by side on the phone: none wraps or spills (the labels shrink)
        boxes = page.eval_on_selector_all("#tabs span", "els => els.map(e => { const r = e.getBoundingClientRect(); return [r.left, r.right, r.height, e.scrollWidth <= e.clientWidth + 1]; })")
        self.assertTrue(all(b[3] for b in boxes), boxes)
        self.assertTrue(all(a[1] <= b[0] + 0.5 for a, b in zip(boxes, boxes[1:])), boxes)
        page.click("#tabs [data-tab=osd]")
        page.wait_for_selector("#oDump")
        self.assertFalse(page.evaluate("() => document.documentElement.scrollWidth > innerWidth"))

    # ---- profile -------------------------------------------------------------------------------------------------

    def test_profile_sets_numbers_and_the_preset(self):
        page = self.open()
        self.assertEqual(self.events(), ["right roll attempt, 249 deg"])
        row = page.locator("#params .prm[data-p=flip_min_deg]")
        self.assertEqual(row.locator(".def").text_content(), "270")
        self.assertEqual(row.locator(".src").text_content(), "shipped")
        self.assertIn("roll", row.locator(".used").text_content())
        self.act(page, lambda: (row.locator("[data-val]").fill("240"), row.locator("[data-val]").press("Tab")), {"version": 1, "params": {"flip_min_deg": 240}})   # the Tab blurs: one change
        row = page.locator("#params .prm[data-p=flip_min_deg]")
        self.assertEqual(row.locator(".src").text_content(), "yours")
        self.assertTrue(row.locator("[data-reset]").is_visible())
        self.assertIn("your layer in the database", page.text_content("#status"))
        self.assertEqual(self.events(), ["right roll"])                     # the suggestions follow, no restart
        self.act(page, lambda: row.locator("[data-reset]").click(), {"version": 1})
        self.assertEqual(page.locator("#params .prm[data-p=flip_min_deg] .src").text_content(), "shipped")
        self.act(page, lambda: page.check("input[name=skill][value=beginner]"), {"version": 1, "skill": "beginner"})
        self.assertEqual(page.locator("#params .prm[data-p=flip_min_deg] .src").text_content(), "preset beginner")
        self.assertEqual(page.input_value("#params .prm[data-p=flip_min_deg] [data-val]"), "240")
        self.assertEqual(self.events(), ["right roll"])
        page.fill("#pfilter", "orbit")
        self.assertEqual(page.eval_on_selector_all("#params .prm", "els => els.map(e => e.dataset.p)"), ["orbit_min_deg", "orbit_min_dur", "orbit_max_tilt"])

    # ---- drones --------------------------------------------------------------------------------------------------

    def test_drones_set_a_craft_apart(self):
        page = self.open("/config#drones")
        self.assertEqual(page.eval_on_selector_all("#crafts [data-craft]", "els => els.map(e => e.dataset.craft)"), ["OttO"])   # seen in the log
        self.assertTrue(page.locator("#crafts [data-craft=OttO]").evaluate("e => e.classList.contains('on')"))
        self.act(page, lambda: (page.fill("[data-prop=size_mm]", "75"), page.press("[data-prop=size_mm]", "Tab")), {"version": 1, "craft": {"OttO": {"size_mm": 75}}})
        row = page.locator("#cparams .prm[data-p=flip_min_deg]")
        self.act(page, lambda: (row.locator("[data-val]").fill("200"), row.locator("[data-val]").press("Tab")), {"version": 1, "craft": {"OttO": {"size_mm": 75, "params": {"flip_min_deg": 200}}}})
        self.assertEqual(page.locator("#cparams .prm[data-p=flip_min_deg] .src").text_content(), "craft OttO")
        self.assertEqual(self.events(), ["right roll"])                     # the arm is OttO's
        self.act(page, lambda: page.uncheck("#ctricks input[data-id=roll]"), {"version": 1, "craft": {"OttO": {"size_mm": 75, "params": {"flip_min_deg": 200}, "trick": [{"id": "roll", "enabled": False}]}}})
        self.assertEqual(self.events(), [])                                 # 249 deg is a roll at 200, and the roll is off for OttO
        page.click("#tabs [data-tab=tricks]")
        self.assertTrue(page.is_checked("#tricks .trk[data-id=roll] [data-on]"))        # for any other quad it is on
        self.assertEqual(page.locator("#tricks .trk[data-id=roll] .src").text_content(), "shipped")
        page.click("#tabs [data-tab=drones]")
        self.act(page, lambda: page.check("input[name=cskill][value=advanced]"),
                 {"version": 1, "craft": {"OttO": {"size_mm": 75, "params": {"flip_min_deg": 200}, "trick": [{"id": "roll", "enabled": False}], "skill": "advanced"}}})

    def test_a_craft_declared_in_another_case_is_the_same_chip(self):
        """A section written as `otto` and a log that says `OttO`: one chip, and the knobs write into the section."""
        self.api.set_setting("tricks", {"version": 1, "craft": {"otto": {"size_mm": 75}}})
        page = self.open("/config#drones")
        self.assertEqual(page.eval_on_selector_all("#crafts [data-craft]", "els => els.map(e => e.dataset.craft)"), ["otto"])
        self.assertEqual(page.input_value("[data-prop=size_mm]"), "75")
        self.act(page, lambda: (page.fill("[data-prop=weight_g]", "40"), page.press("[data-prop=weight_g]", "Tab")), {"version": 1, "craft": {"otto": {"size_mm": 75, "weight_g": 40}}})
        row = page.locator("#cparams .prm[data-p=flip_min_deg]")
        self.act(page, lambda: (row.locator("[data-val]").fill("200"), row.locator("[data-val]").press("Tab")), {"version": 1, "craft": {"otto": {"size_mm": 75, "weight_g": 40, "params": {"flip_min_deg": 200}}}})
        self.assertEqual(self.events(), ["right roll"])                     # the arm's header says OttO

    # ---- tricks --------------------------------------------------------------------------------------------------

    def test_tricks_switch_show_check_and_eval(self):
        page = self.open("/config#tricks")
        self.assertEqual(page.input_value("#evalSession"), S)
        page.click("#evalBtn")
        page.wait_for_function("() => document.querySelector('#evalList table')", timeout=60000)
        self.assertEqual(page.eval_on_selector_all("#evalList table td", "els => els.map(e => e.textContent)"), ["roll", "1", "0"])   # the attempt finds the roll moment
        self.act(page, lambda: page.uncheck("#tricks .trk[data-id=roll-attempt] [data-on]"), {"version": 1, "trick": [{"id": "roll-attempt", "enabled": False}]})
        self.assertTrue(page.locator("#tricks .trk[data-id=roll-attempt]").evaluate("e => e.classList.contains('off')"))
        self.assertEqual(self.events(), [])
        self.assertIn("14 of 15 tricks on", page.text_content("#status"))
        page.click("#evalBtn")
        page.wait_for_function("() => document.querySelector('#evalList table td.bad')", timeout=60000)
        self.assertEqual(page.eval_on_selector_all("#evalList table td", "els => els.map(e => e.textContent)"), ["roll", "0", "1"])   # and without it the moment is missed
        self.assertIn("a roll", page.text_content("#evalList"))
        page.click("#tricks .trk[data-id=roll] .head .id")
        det = page.locator("#tricks .trk[data-id=roll] .det")
        self.assertTrue(det.is_visible())
        self.assertIn("abs(rotation_peak_deg.roll) >= flip_min_deg", det.text_content())
        self.assertIn("arm 1 at 6.5", det.text_content())
        page.click("#tricks .trk[data-id=split-s] .head .id")
        self.assertIn("phase 2 pitch-back within 1 s", page.locator("#tricks .trk[data-id=split-s] .det").text_content())
        page.click("#checkBtn")
        page.wait_for_function("() => document.querySelector('#checkOut').textContent.includes('unchecked')", timeout=60000)
        self.assertIn("0 failed", page.text_content("#checkOut"))          # the real logs are not on this server: unchecked, never failed
        self.assertGreater(page.locator("#checkList .m").count(), 10)

    # ---- settings ------------------------------------------------------------------------------------------------

    def test_settings_raw_layer_and_this_browser(self):
        page = self.open("/config#settings")
        page.fill("#layerText", "{not json")
        page.click("#layerSave")
        page.wait_for_function("() => document.querySelector('#err').classList.contains('on')")
        self.assertIn("not JSON", page.text_content("#err"))
        page.fill("#layerText", json.dumps({"version": 1, "params": {"flip_min_dg": 240}}))
        page.click("#layerSave")
        page.wait_for_function("() => document.querySelector('#err').textContent.includes('flip_min_dg')")
        self.assertEqual(self.layer(), {})                                   # refused: nothing stored
        self.act(page, lambda: (page.fill("#layerText", json.dumps({"version": 1, "params": {"flip_min_deg": 250}, "skill": "advanced"})), page.click("#layerSave")),
                 {"version": 1, "params": {"flip_min_deg": 250}, "skill": "advanced"})
        self.assertFalse(page.evaluate("() => document.querySelector('#err').classList.contains('on')"))
        self.assertIn("database", page.text_content("#settingsPanel .kv"))
        page.on("dialog", lambda d: d.accept())
        self.act(page, lambda: page.click("#layerClear"), {})
        # share: the export link, and a tricks file pasted in, replacing then merging
        self.assertEqual(page.get_attribute("#exportLink", "href"), "/api/tricks/export")
        self.assertEqual(page.get_attribute("#exportLink", "download"), "acroscope-tricks.toml")
        self.assertTrue(page.is_disabled("#importReplace"))
        page.fill("#importText", 'version = 1\nskill = "beginner"\n[params]\norbit_min_deg = 240\n')
        self.assertFalse(page.is_disabled("#importReplace"))
        self.act(page, lambda: page.click("#importReplace"), {"version": 1, "skill": "beginner", "params": {"orbit_min_deg": 240}})
        self.assertEqual(page.locator("#params, #settingsPanel").count(), 1)                     # still on settings
        page.fill("#importText", "[params]\nflip_min_deg = 250\n")
        self.act(page, lambda: page.click("#importMerge"), {"version": 1, "skill": "beginner", "params": {"orbit_min_deg": 240, "flip_min_deg": 250}})
        page.fill("#importText", "[params]\nflip_min_dg = 250\n")
        page.click("#importReplace")
        page.wait_for_function("() => document.querySelector('#err').textContent.includes('flip_min_dg')")
        self.assertEqual(self.layer(), {"version": 1, "skill": "beginner", "params": {"orbit_min_deg": 240, "flip_min_deg": 250}})
        page.uncheck("#stSticks")
        self.assertEqual(page.evaluate("() => localStorage.getItem('acro.sticks')"), "0")
        page.check("#stAll")
        self.assertEqual(page.evaluate("() => localStorage.getItem('acro.allClips')"), "1")

    def test_a_broken_layer_still_shows_the_page_and_its_clear_button(self):
        """A stored layer a later release made invalid: the page says so, the tabs still draw, the settings tab clears it."""
        import sqlite3
        self.api.set_setting("tricks", {"version": 1, "skill": "beginner"})
        db = sqlite3.connect(Path(self.tmp.name) / "t.db")
        db.execute("UPDATE settings SET value = ? WHERE key = 'tricks'", (json.dumps({"version": 1, "params": {"roll_hold_share": 0.85}}),))
        db.commit(); db.close()
        page = self.open()
        self.assertIn("roll_hold_share", page.text_content("#err"))
        self.assertIn("15 of 15 tricks on", page.text_content("#status"))
        page.click("#tabs [data-tab=settings]")
        self.assertIn("roll_hold_share", page.input_value("#layerText"))
        page.on("dialog", lambda d: d.accept())
        self.act(page, lambda: page.click("#layerClear"), {})
        self.assertFalse(page.evaluate("() => document.querySelector('#err').classList.contains('on')"))

    def test_a_stale_tab_does_not_overwrite(self):
        """Two tabs on the same layer: the second save is refused and that tab reloads."""
        page = self.open()
        other = self.open()
        row = page.locator("#params .prm[data-p=flip_min_deg]")
        self.act(page, lambda: (row.locator("[data-val]").fill("240"), row.locator("[data-val]").press("Tab")), {"version": 1, "params": {"flip_min_deg": 240}})
        before = other.evaluate("() => renders")
        other.check("input[name=skill][value=beginner]")
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
        from acroscope.db import Db
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


if __name__ == "__main__":
    unittest.main()
