"""The player end to end in a headless browser: a real `acroscope serve` child on a scratch data dir and database,
driven by Chromium through Playwright, at a desktop size and at a phone's (Rudi's: 412 x 927 CSS px).

Needs `scripts/browser-setup.sh` once (Playwright in the venv, the browser and its libraries under ~/.cache, no
root); skipped where the browser is not there, so the suite still runs anywhere. What a browser cannot stand in for:
the installed app's own viewport on the phone, and how a gesture feels."""
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

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sync_playwright = None
try:
    from tests.test_share import LOG as SHARE_LOG, fake_arm, make_clip
    from tests.test_config_page import save_arm
    from tests.synthetic import axis, combine, make_arm, pulse
except ImportError:
    from test_share import LOG as SHARE_LOG, fake_arm, make_clip
    from test_config_page import save_arm
    from synthetic import axis, combine, make_arm, pulse

LIBS = Path(os.environ.get("ACROSCOPE_BROWSER_LIBS", "~/.cache/acroscope-browser")).expanduser() / "usr" / "lib" / "x86_64-linux-gnu"
PHONE = {"viewport": {"width": 412, "height": 927}, "is_mobile": True, "has_touch": True}
DESKTOP = {"viewport": {"width": 1440, "height": 900}}


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _browser(p):
    env = dict(os.environ, LD_LIBRARY_PATH=f"{LIBS}:{os.environ.get('LD_LIBRARY_PATH', '')}")
    return p.chromium.launch(env=env)


def browser_available() -> bool:
    if sync_playwright is None:
        return False
    try:
        with sync_playwright() as p:
            _browser(p).close()
        return True
    except Exception:  # noqa: BLE001  (no browser downloaded, a library missing)
        return False


@unittest.skipUnless(browser_available(), "no headless browser: run scripts/browser-setup.sh")
class PlayerTest(unittest.TestCase):
    LOG_A, LOG_B = "BTFL_BLACKBOX_LOG_OTTOJR_20261014_185010_BETAFPVG473_V2.bbl", "BTFL_BLACKBOX_LOG_OTTOJR_20261015_094754_BETAFPVG473_V2.bbl"
    LOG_ODD = "dump #2.bbl"                                           # a name the link must encode
    ODD_BYTES = b"H Product:Blackbox flight data recorder\n" + bytes(range(256))
    SESSIONS = {"2026-10-11-unsorted": ["2026-10-11_001.mp4", "2026-10-11_002.mp4"], "2026-10-11-bea": ["2026-10-11_005.mp4"],
                "2026-10-12-del": ["2026-10-12_001.mp4", "2026-10-12_002.mp4"], "2026-10-13-phone": ["2026-10-13_001.mp4"],
                "2026-10-09-attick-is-the-limit": ["2026-10-09_001.mp4"],
                "2026-10-14-logs": ["2026-10-14_001.mp4", "2026-10-14_002.mp4"], "2026-10-14-other": ["2026-10-14_005.mp4"],
                "2026-10-14-typo-src": ["2026-10-14_007.mp4"], "2026-10-14-dl": ["2026-10-14_008.mp4"],
                "2026-10-14-gone": ["2026-10-14_009.mp4"], "2026-10-14-job": ["2026-10-14_010.mp4"],
                "2026-10-14-am": ["2026-10-14_001.mp4", "2026-10-14_002.mp4"],
                "2026-10-14-run-a": ["2026-10-14_011.mp4"], "2026-10-14-run-b": ["2026-10-14_012.mp4"],
                "2026-10-14-nologs": ["2026-10-14_013.mp4"],
                "2026-10-16-swipe": ["2026-10-16_001.mp4", "2026-10-16_002.mp4", "2026-10-16_003.mp4"],
                "2026-10-16-all": ["2026-10-16_001.mp4", "2026-10-16_002.mp4"],
                "2026-10-16-step": ["2026-10-16_001.mp4", "2026-10-16_002.mp4", "2026-10-16_003.mp4"],
                "2026-10-16-zero": ["2026-10-16_001.mp4", "2026-10-16_002.mp4"],
                "2026-10-16-quick": ["2026-10-16_001.mp4", "2026-10-16_002.mp4", "2026-10-16_003.mp4"],
                "2026-10-16-two": ["2026-10-16_001.mp4", "2026-10-16_002.mp4"],
                "2026-10-04-share": [],                                          # a real clip, made below: sharing renders it
                "2026-10-04-sugg": ["2026-10-04_002.mp4"]}                      # a scripted arm with two attempts: suggestions
    SHARE = "2026-10-04-share"
    SUGG = "2026-10-04-sugg"

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.tmp.name)
        cls.data = cls.root / "data"
        for s, clips in cls.SESSIONS.items():
            (cls.data / "videos" / s).mkdir(parents=True)
            for c in clips:
                (cls.data / "videos" / s / c).write_bytes(b"")      # not playable, which the player says; enough here
        (cls.data / "blackbox").mkdir()
        for b in (cls.LOG_A, cls.LOG_B):
            (cls.data / "blackbox" / b).write_bytes(b"")              # decodes to nothing; the logs tab lists it all the same
        (cls.data / "blackbox" / cls.LOG_ODD).write_bytes(cls.ODD_BYTES)
        make_clip(cls.data / "videos" / cls.SHARE / "2026-10-04_001.mp4")       # 8 s that ffmpeg can cut and frame
        (cls.data / "blackbox" / SHARE_LOG).write_bytes(b"")
        fake_arm(cls.root / "cache", SHARE_LOG[:-4], 3.0, 100, {"rcCommand[0]": lambda t: 250.0, "rcCommand[1]": lambda t: -100.0,
                                                               "rcCommand[2]": lambda t: 300.0, "rcCommand[3]": lambda t: 1000.0 + 100.0 * t})
        (cls.data / "blackbox" / "sugg.bbl").write_bytes(b"")
        save_arm(make_arm(10, gyro=combine(axis(0, pulse(2.0, 2.5, 500)), axis(0, pulse(6.0, 6.5, 500))), craft="OttO"), cls.root / "cache", "sugg")   # two 250 deg rolls: two attempts
        cls.port = free_port()
        env = dict(os.environ, ACROSCOPE_DB=str(cls.root / "t.db"), ACROSCOPE_DATA=str(cls.data),
                   ACROSCOPE_CACHE=str(cls.root / "cache"), ACROSCOPE_URL="",
                   ACROSCOPE_SHARES=str(cls.root / "shares"), ACROSCOPE_SHARE_URL="https://share.example")
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
        for s in cls.SESSIONS:
            cls.api.refresh(s, probe_videos=s == cls.SHARE)
        cls.api.set_match(cls.SHARE, "2026-10-04_001.mp4", SHARE_LOG, 1, 2.0)      # arm 1 covers video 2.0-4.99
        cls.api.tag(cls.SHARE, "2026-10-04_001.mp4", 3.0, 4.0, "half roll", ["roll"])    # m01
        cls.api.tag(cls.SHARE, "2026-10-04_001.mp4", 6.0, 7.0, "late", ["roll"])         # m02: no arm there
        cls.api.set_match(cls.SUGG, "2026-10-04_002.mp4", "sugg.bbl", 1, 10.0)
        cls.pw = sync_playwright().start()
        cls.browser = _browser(cls.pw)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()
        cls.proc.terminate()
        cls.proc.wait(5)
        cls.tmp.cleanup()

    def open(self, session, size=DESKTOP):
        ctx = self.browser.new_context(**size)
        self.addCleanup(ctx.close)
        page = ctx.new_page()
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        self.addCleanup(lambda: self.assertEqual(errors, [], "script errors on the page"))
        page.goto(f"{self.url}/?session={session}")
        page.wait_for_function("() => typeof S !== 'undefined' && S && S.session === " + repr(session))
        return page

    # ---- the desktop header -------------------------------------------------------------------------------------

    def keys_in_header(self, page):
        return page.evaluate("""() => {
          const h = document.querySelector('header').getBoundingClientRect(), k = document.querySelector('header .keys');
          const r = k.getBoundingClientRect(), st = document.getElementById('status').getBoundingClientRect();
          const prev = document.getElementById('renameSession').getBoundingClientRect();
          return {shown: r.width > 0, inside: r.right <= Math.min(h.right, innerWidth) + 0.5, header: h.width, inner: innerWidth, clear: r.left >= prev.right, status: st.width};
        }""")

    def test_key_hints_show_where_they_fit(self):
        """The key hints sit in the header whenever they fit: Rudi's screen (2069 px at 150 %, about 1380 CSS px)
        has plenty of space, and a fixed 1400 px breakpoint hid them there; he wants them down to 1100. A status
        message comes first: while there is one it keeps 160 px and the hints step aside. Where they show, nothing
        spills off the window or overlaps, at any width."""
        page = self.open("2026-10-09-attick-is-the-limit", {"viewport": {"width": 1100, "height": 900}})
        status = lambda text: page.evaluate("t => { $('#status').textContent = t }", text)
        page.wait_for_timeout(100)
        self.assertEqual(self.keys_in_header(page)["shown"], not page.evaluate("() => $('#status').textContent.trim()"))
        status("")                                                    # these clips never probe: "probing clips…" stays
        page.wait_for_timeout(100)
        self.assertTrue(self.keys_in_header(page)["shown"])          # the longest session label at 1100: they fit
        for msg in ("", "decoding blackbox in the background, arms appear when done…"):
            status(msg)
            for width in range(900, 1501, 50):
                with self.subTest(width=width, msg=msg):
                    page.set_viewport_size({"width": width, "height": 900})
                    page.wait_for_timeout(50)
                    k = self.keys_in_header(page)
                    self.assertLessEqual(k["header"], k["inner"], k)                  # the header never outgrows the window
                    if k["shown"]:
                        self.assertTrue(k["inside"] and k["clear"], k)             # nothing overlaps, nothing spills
                        if msg:
                            self.assertGreaterEqual(k["status"], 160, k)           # the message keeps its room
                    if width >= (1100 if not msg else 1400):
                        self.assertTrue(k["shown"], k)
                    if width <= 1050:
                        self.assertFalse(k["shown"], k)
        page.set_viewport_size({"width": 1100, "height": 900})
        status("decoding blackbox in the background, arms appear when done…")         # a message at 1100: they step aside
        page.wait_for_timeout(100)
        self.assertFalse(self.keys_in_header(page)["shown"])
        status("")                                                                    # and come back when it clears
        page.wait_for_timeout(100)
        self.assertTrue(self.keys_in_header(page)["shown"])
        # the session list's label growing after the first look (it is empty when the page starts)
        page.evaluate("() => { $('#session').options[0].textContent += ' · and a much longer label than any session has'.repeat(3) }")
        page.wait_for_timeout(100)
        k = self.keys_in_header(page)
        self.assertTrue(not k["shown"] or k["inside"], k)

    def test_no_key_hints_on_a_phone(self):
        page = self.open("2026-10-13-phone", PHONE)
        self.assertFalse(self.keys_in_header(page)["shown"])

    # ---- the phone layout --------------------------------------------------------------------------------------

    def test_phone_tab_bar_at_the_bottom(self):
        """The tab bar sits on the bottom edge whatever the pane holds, with the header shown or hidden (it was
        left mid-screen by a short pane with the header hidden)."""
        page = self.open("2026-10-13-phone", PHONE)
        for tab in ("clips", "inbox", "moments"):
            for rows in (1, 40):
                for hide in (False, True):
                    with self.subTest(tab=tab, rows=rows, hide=hide):
                        r = page.evaluate("""([tab, rows, hide]) => {
                          showMTab(tab);
                          const el = tab === 'moments' ? document.querySelector('#paneMoments .list') : document.getElementById(tab);
                          el.innerHTML = Array.from({length: rows}, (_, i) => `<div class="clip">row ${i}</div>`).join('');
                          document.body.classList.toggle('hide-head', hide);
                          const t = document.getElementById('mtabs').getBoundingClientRect();
                          return {inner: innerHeight, top: t.top, bottom: t.bottom, height: t.height};
                        }""", [tab, rows, hide])
                        self.assertGreater(r["height"], 20)
                        self.assertAlmostEqual(r["bottom"], r["inner"], delta=1)

    def test_no_empty_row_between_the_controls_and_the_pane(self):
        """The readout under the controls is empty until the video reports a time: it takes no room then (it left
        ~32 px of nothing between the buttons and the list); once it reads, it is there."""
        page = self.open("2026-10-13-phone", PHONE)
        gap = page.evaluate("""() => { showMTab('clips'); document.getElementById('readout').innerHTML = '';
          return document.querySelector('aside').getBoundingClientRect().top - document.querySelector('.mctl').getBoundingClientRect().bottom; }""")
        self.assertLessEqual(gap, 8)
        h = page.evaluate("() => { readout(); return document.getElementById('readout').getBoundingClientRect().height; }")
        self.assertGreater(h, 10)

    def test_pull_to_refresh_needs_a_long_pull(self):
        """With the header showing, a pull down on it of 22 % of the screen height (204 px on Rudi's phone) reloads
        the page; a shorter one does not."""
        page = self.open("2026-10-13-phone", PHONE)
        pull = """dy => {
          const el = document.querySelector('header'), x = 200, y = 60;
          const t = yy => new Touch({identifier: 1, target: el, clientX: x, clientY: yy});
          const fire = (type, yy) => el.dispatchEvent(new TouchEvent(type, {bubbles: true, touches: type === 'touchend' ? [] : [t(yy)], changedTouches: [t(yy)]}));
          fire('touchstart', y);
          for (let d = 10; d <= dy; d += 10) fire('touchmove', y + d);
          window.__release = () => fire('touchend', y + dy);     // a separate call: the release may reload the page
          return document.getElementById('ptr').textContent;
        }"""
        release = "() => { setTimeout(window.__release, 100); }"
        page.evaluate("window.__before = 1")
        self.assertEqual(page.evaluate(pull, 180), "↓ pull to refresh")
        page.evaluate(release)
        page.wait_for_timeout(400)
        self.assertEqual(page.evaluate("window.__before"), 1)                  # no reload
        self.assertEqual(page.evaluate(pull, 220), "↻ release to refresh")
        with page.expect_navigation():
            page.evaluate(release)
        page.wait_for_load_state("load")
        page.wait_for_function("() => typeof S !== 'undefined' && S && S.session")      # the reloaded player is up
        self.assertIsNone(page.evaluate("window.__before"))
        self.assertIn("session=2026-10-13-phone", page.url)                    # comes back to the same session

    # ---- the phone: swipe between clips, the hints, all clips' moments, tag pauses ---------------------------------

    # [dx, dy, start this many px above the video's bottom (None: at 40 % of its height), fingers]
    SWIPE = """([dx, dy, fromBottom, fingers]) => {
      const v = document.getElementById('v'), r = v.getBoundingClientRect();
      const x = r.left + r.width / 2 - dx / 2, y = fromBottom != null ? r.bottom - fromBottom : r.top + r.height * 0.4;
      const t = (xx, yy, id = 1) => new Touch({identifier: id, target: v, clientX: xx, clientY: yy});
      const fire = (type, xx, yy) => v.dispatchEvent(new TouchEvent(type, {bubbles: true, touches: type === 'touchend' ? [] : [t(xx, yy)], changedTouches: [t(xx, yy)]}));
      if ((fingers || 1) > 1) v.dispatchEvent(new TouchEvent('touchstart', {bubbles: true, touches: [t(x, y), t(x + 40, y, 2)], changedTouches: [t(x, y), t(x + 40, y, 2)]}));
      else fire('touchstart', x, y);
      for (let k = 1; k <= 5; k++) fire('touchmove', x + dx * k / 5, y + dy * k / 5);
      fire('touchend', x + dx, y + dy);
    }"""

    def test_swipe_on_the_video_changes_clip(self):
        """On the phone a sideways swipe on the video goes to the next clip (to the left) or the previous one (to the
        right), like turning a page; not past the ends, not on a mostly vertical drag, not from the video's own
        controls at its bottom (the seek bar)."""
        page = self.open("2026-10-16-swipe", PHONE)
        page.evaluate("() => loadClip('2026-10-16_001.mp4')")
        on = lambda: page.evaluate("() => clip")
        page.evaluate(self.SWIPE, [-150, 10, None])
        page.wait_for_function("() => clip === '2026-10-16_002.mp4'")
        page.evaluate(self.SWIPE, [-150, 10, None])
        page.wait_for_function("() => clip === '2026-10-16_003.mp4'")
        page.evaluate(self.SWIPE, [-150, 10, None])                     # the last one: stays
        page.wait_for_timeout(200)
        self.assertEqual(on(), "2026-10-16_003.mp4")
        page.evaluate(self.SWIPE, [150, -10, None])
        page.wait_for_function("() => clip === '2026-10-16_002.mp4'")
        page.evaluate(self.SWIPE, [-60, 120, None])                     # mostly down: the header's gesture
        page.evaluate(self.SWIPE, [-30, 0, None])                       # too short: a tap that slid
        page.evaluate(self.SWIPE, [-150, 0, 20])                       # on the seek bar
        page.wait_for_timeout(200)
        self.assertEqual(on(), "2026-10-16_002.mp4")
        dpage = self.open("2026-10-16-swipe")                            # the desktop has keys and the list: no swipe
        dpage.evaluate("() => loadClip('2026-10-16_001.mp4')")
        dpage.evaluate(self.SWIPE, [-150, 10, None])
        dpage.wait_for_timeout(200)
        self.assertEqual(dpage.evaluate("() => clip"), "2026-10-16_001.mp4")

    def test_swipe_hints_in_the_padding(self):
        """A faint arrow each side of the video where a swipe leads somewhere, inside the padding that is already
        there: the video keeps its size and place."""
        page = self.open("2026-10-16-swipe", PHONE)
        box = "() => { const r = document.getElementById('v').getBoundingClientRect(); return [r.left, r.top, r.width, r.height]; }"
        page.evaluate("() => loadClip('2026-10-16_001.mp4')")
        before = page.evaluate(box)
        hints = """() => [...document.querySelectorAll('.swipe-hint')].filter(h => getComputedStyle(h).display !== 'none')
          .map(h => { const r = h.getBoundingClientRect(); return {side: h.dataset.side, left: r.left, right: r.right, top: r.top, bottom: r.bottom}; })"""
        h = page.evaluate(hints)
        self.assertEqual([x["side"] for x in h], ["next"])               # first clip: only onwards
        page.evaluate("() => loadClip('2026-10-16_002.mp4')")
        h = page.evaluate(hints)
        self.assertEqual(sorted(x["side"] for x in h), ["next", "prev"])
        v = page.evaluate(box)
        self.assertEqual(v, before)
        for x in h:                                                     # in the gutter beside the video, on screen
            if x["side"] == "prev":
                self.assertGreaterEqual(x["left"], 0); self.assertLessEqual(x["right"], v[0] + 0.5)
            else:
                self.assertGreaterEqual(x["left"], v[0] + v[2] - 0.5); self.assertLessEqual(x["right"], 412)
            self.assertTrue(v[1] <= x["top"] and x["bottom"] <= v[1] + v[3])
        page.evaluate("() => loadClip('2026-10-16_003.mp4')")
        self.assertEqual([x["side"] for x in page.evaluate(hints)], ["prev"])
        self.assertEqual(page.evaluate(box), before)
        dpage = self.open("2026-10-16-swipe")                            # not on the desktop
        self.assertEqual(dpage.evaluate(hints), [])

    def test_all_clips_moments_on_the_phone(self):
        """The 'all clips' switch is in the phone's moments tab too; on, the list and ◀◀ ▶▶ go across clips; it
        stays on through a reload."""
        S = "2026-10-16-all"
        self.api.tag(S, "2026-10-16_001.mp4", 1, 2, "one", [])
        self.api.tag(S, "2026-10-16_002.mp4", 3, 4, "two", [])
        page = self.open(S, PHONE)
        page.click("#mtabs [data-mtab=moments]")
        page.evaluate("() => loadClip('2026-10-16_001.mp4')")
        self.assertTrue(page.is_visible("#allClips"))
        self.assertEqual(page.locator("#moments .m").count(), 1)
        page.click("#allClips")
        self.assertEqual(page.locator("#moments .m").count(), 2)
        page.click(".mctl [data-act=next]")
        page.click(".mctl [data-act=next]")
        page.wait_for_function("() => clip === '2026-10-16_002.mp4'")
        page.reload()
        page.wait_for_function("() => typeof S !== 'undefined' && S && S.session")
        self.assertTrue(page.eval_on_selector("#allClips", "c => c.checked"))

    def test_tag_pauses_the_video(self):
        """tag opens the editor at the spot and pauses there, to fill it in at that frame; the t key too."""
        page = self.open("2026-10-16-swipe", PHONE)
        page.evaluate("() => { window.__paused = 0; v.pause = () => { window.__paused++; }; }")
        page.click(".mctl [data-act=tag]")
        self.assertEqual(page.evaluate("window.__paused"), 1)
        self.assertTrue(page.is_visible("#editor"))
        dpage = self.open("2026-10-16-swipe")
        dpage.evaluate("() => { window.__paused = 0; v.pause = () => { window.__paused++; }; }")
        dpage.keyboard.press("t")
        self.assertEqual(dpage.evaluate("window.__paused"), 1)

    # review on #34 ------------------------------------------------------------------------------------------------

    def swipe_page(self):
        page = self.open("2026-10-16-swipe", PHONE)
        page.evaluate("() => loadClip('2026-10-16_002.mp4')")
        return page

    def swiped(self, page, *args, to=None):
        """Swipe from 002 and say where it went (and back to 002 for the next one)."""
        page.evaluate("() => loadClip('2026-10-16_002.mp4')")
        page.evaluate(self.SWIPE, list(args) + [None] * (4 - len(args)))
        page.wait_for_timeout(150)
        return page.evaluate("() => clip")

    def test_swipe_boundaries(self):
        """60 px across swipes and 59 does not; up or down, under half the travel swipes and more does not; from
        57 px above the video's bottom it swipes, from 55 (the seek bar) it does not; two fingers never."""
        page = self.swipe_page()
        self.assertEqual(self.swiped(page, -60, 0), "2026-10-16_003.mp4")
        self.assertEqual(self.swiped(page, -59, 0), "2026-10-16_002.mp4")
        self.assertEqual(self.swiped(page, -150, 70), "2026-10-16_003.mp4")
        self.assertEqual(self.swiped(page, -150, -70), "2026-10-16_003.mp4")
        self.assertEqual(self.swiped(page, -150, -80), "2026-10-16_002.mp4")
        self.assertEqual(self.swiped(page, -150, 80), "2026-10-16_002.mp4")
        self.assertEqual(self.swiped(page, -150, 0, 57), "2026-10-16_003.mp4")
        self.assertEqual(self.swiped(page, -150, 0, 55), "2026-10-16_002.mp4")
        self.assertEqual(self.swiped(page, -150, 0, None, 2), "2026-10-16_002.mp4")

    def test_swipe_says_where_it_went(self):
        """The status names the clip a swipe went to, or that there is none further; two quick swipes leave the
        status as it was before them, not on the first one's text."""
        page = self.swipe_page()
        page.evaluate("() => { document.getElementById('status').textContent = 'before'; }")
        page.evaluate(self.SWIPE, [-150, 0, None, 1])
        self.assertEqual(page.text_content("#status"), "clip 003")
        page.evaluate(self.SWIPE, [-150, 0, None, 1])
        self.assertEqual(page.text_content("#status"), "last clip")
        page.wait_for_timeout(1700)
        self.assertEqual(page.text_content("#status"), "before")
        page.evaluate("() => loadClip('2026-10-16_001.mp4')")
        page.evaluate(self.SWIPE, [150, 0, None, 1])
        self.assertEqual(page.text_content("#status"), "first clip")

    def test_an_arcing_swipe_leaves_the_header(self):
        """A sideways swipe that drifts up is a swipe, not the header's drag: the header (and its status) stays."""
        page = self.swipe_page()
        page.evaluate(self.SWIPE, [-150, -45, None, 1])
        page.wait_for_function("() => clip === '2026-10-16_003.mp4'")
        self.assertFalse(page.evaluate("() => document.body.classList.contains('hide-head')"))
        page.evaluate(self.SWIPE, [0, -120, None, 1])                      # a drag up still hides it
        self.assertTrue(page.evaluate("() => document.body.classList.contains('hide-head')"))

    def test_no_swipe_while_editing(self):
        """With the editor (or a question) open a swipe does nothing: the moment being typed is not lost."""
        page = self.swipe_page()
        page.click(".mctl [data-act=tag]")
        page.fill("#edTitle", "half typed")
        page.evaluate(self.SWIPE, [-150, 0, None, 1])
        page.wait_for_timeout(150)
        self.assertEqual(page.evaluate("() => clip"), "2026-10-16_002.mp4")
        self.assertEqual(page.input_value("#edTitle"), "half typed")
        page.click("#editor button:has-text('cancel')")
        page.evaluate("() => { ask('a question', ''); }")
        page.evaluate(self.SWIPE, [-150, 0, None, 1])
        page.wait_for_timeout(150)
        self.assertEqual(page.evaluate("() => clip"), "2026-10-16_002.mp4")

    def test_sideways_drags_on_the_video_are_the_pages(self):
        """touch-action keeps vertical scrolling and zoom for the browser and takes sideways drags (no back gesture)."""
        page = self.swipe_page()
        self.assertEqual(page.eval_on_selector(".vwrap", "w => getComputedStyle(w).touchAction"), "pan-y pinch-zoom")

    def test_swipe_measures_the_video_not_its_wrapper(self):
        """In fullscreen the wrapper collapses (the video is in the top layer): the seek-bar zone is the video's."""
        page = self.swipe_page()
        page.evaluate("() => { const w = document.querySelector('.vwrap'); w.style.height = '0px'; w.style.overflow = 'visible'; }")
        page.evaluate(self.SWIPE, [-150, 0, None, 1])
        page.wait_for_function("() => clip === '2026-10-16_003.mp4'")

    def test_hints_follow_the_clip_list(self):
        """The hints follow the list, not only a clip load: a list that shrinks to one clip shows none."""
        page = self.open("2026-10-16-swipe", PHONE)
        page.evaluate("() => loadClip('2026-10-16_001.mp4')")
        self.assertTrue(page.evaluate("() => document.querySelector('.vwrap').classList.contains('has-next')"))
        page.evaluate("() => { S.videos = S.videos.slice(0, 1); renderClips(); }")
        self.assertFalse(page.evaluate("() => document.querySelector('.vwrap').classList.contains('has-next')"))
        dpage = self.open("2026-10-16-swipe")                            # the desktop: none, at either end
        dpage.evaluate("() => loadClip('2026-10-16_002.mp4')")
        self.assertEqual(dpage.evaluate("() => [...document.querySelectorAll('.swipe-hint')].filter(h => getComputedStyle(h).display !== 'none').length"), 0)

    def step_session(self):
        """Three clips with a moment each (m1-m3, at 10, 11 and 12 s), made once."""
        S = "2026-10-16-step"
        if not self.api.load(S)["moments"]:
            for i, c in enumerate(self.SESSIONS[S]):
                self.api.tag(S, c, 10 + i, 12 + i, f"m{i + 1}", [])
        return S

    def test_step_after_a_swipe_goes_on_from_here(self):
        """All clips on, a swipe to a clip, then ▶▶: that clip's next moment, not the session's first; ◀◀: the one
        before here. Across clips the order is the clips list's, not the files' names (ids since 10-10)."""
        S = self.step_session()
        page = self.open(S, PHONE)
        page.click("#mtabs [data-mtab=moments]")
        if not page.eval_on_selector("#allClips", "c => c.checked"):
            page.click("#allClips")
        page.evaluate("() => loadClip('2026-10-16_001.mp4')")
        page.evaluate(self.SWIPE, [-150, 0, None, 1])
        page.wait_for_function("() => clip === '2026-10-16_002.mp4'")
        page.click(".mctl [data-act=next]")
        page.wait_for_function("() => cur && S.moments.find(m => m.id === cur).title === 'm2'")
        page.evaluate("() => loadClip('2026-10-16_003.mp4')")
        page.click(".mctl [data-act=prev]")
        page.wait_for_function("() => cur && S.moments.find(m => m.id === cur).title === 'm2'")
        page.evaluate("() => { S.videos.reverse(); renderMoments(); }")   # the list's order rules, not the names
        self.assertEqual(page.evaluate("() => momentsShown().map(m => m.title)"), ["m3", "m2", "m1"])

    def test_a_late_clip_load_does_not_pull_back(self):
        """▶▶ into the next clip, a swipe on before that clip has loaded: the late load does not bring it back."""
        S = self.step_session()
        page = self.open(S, PHONE)
        page.evaluate("() => { document.getElementById('allClips').checked = true; renderMoments(); loadClip('2026-10-16_001.mp4'); }")
        page.evaluate("""() => { const f = window.fetch;   // 002's series answers 1.5 s late
          window.fetch = (u, o) => String(u).includes('series?video=2026-10-16_002') ? new Promise(r => setTimeout(() => r(f(u, o)), 1500)) : f(u, o); }""")
        page.evaluate("() => { playMoment(S.moments.find(m => m.video === '2026-10-16_002.mp4').id); }")
        page.wait_for_function("() => clip === '2026-10-16_002.mp4'")
        page.evaluate(self.SWIPE, [-150, 0, None, 1])
        page.wait_for_function("() => clip === '2026-10-16_003.mp4'")
        page.wait_for_timeout(300)
        page.evaluate("() => { window.__series = series; }")             # 003's, loaded
        page.wait_for_timeout(2200)
        self.assertEqual(page.evaluate("() => clip"), "2026-10-16_003.mp4")
        self.assertTrue(page.evaluate("() => series === window.__series"))   # 002's late answer did not replace it

    # review round 2 on #34 ----------------------------------------------------------------------------------------

    # a touch on the video at 40 % of its height, through these offsets from the start, then released at the last
    GESTURE = """(pts) => {
      const v = document.getElementById('v'), r = v.getBoundingClientRect(), x = r.left + r.width * 0.7, y = r.top + r.height * 0.4;
      const t = (xx, yy) => new Touch({identifier: 1, target: v, clientX: xx, clientY: yy});
      const fire = (type, [dx, dy]) => v.dispatchEvent(new TouchEvent(type, {bubbles: true, touches: type === 'touchend' ? [] : [t(x + dx, y + dy)], changedTouches: [t(x + dx, y + dy)]}));
      fire('touchstart', [0, 0]);
      for (const p of pts) fire('touchmove', p);
      fire('touchend', pts[pts.length - 1]);
    }"""

    def all_clips_on(self, page):
        page.evaluate("() => { const c = document.getElementById('allClips'); if (!c.checked) c.click(); }")

    def title(self, page):
        return page.evaluate("() => cur && S.moments.find(m => m.id === cur).title")

    def test_step_from_here_counts_a_moment_at_the_spot(self):
        """From here (no moment playing): a moment starting right here is the next one, not one behind; from mid-clip
        ▶▶ goes to the next moment after the time, also in the next clip, and ◀◀ to the one before it."""
        S = "2026-10-16-zero"
        if not self.api.load(S)["moments"]:
            self.api.tag(S, "2026-10-16_001.mp4", 0, 2, "z0", [])
            self.api.tag(S, "2026-10-16_001.mp4", 5, 7, "z5", [])
            self.api.tag(S, "2026-10-16_002.mp4", 2, 4, "z2", [])
        page = self.open(S, PHONE)
        self.all_clips_on(page)
        at = lambda t: page.evaluate(f"() => loadClip('2026-10-16_001.mp4').then(() => {{ v.currentTime = {t}; }})")
        at(0); page.click(".mctl [data-act=next]"); self.assertEqual(self.title(page), "z0")
        at(3); page.click(".mctl [data-act=next]"); self.assertEqual(self.title(page), "z5")
        at(3); page.click(".mctl [data-act=prev]"); self.assertEqual(self.title(page), "z0")
        at(7); page.click(".mctl [data-act=prev]"); self.assertEqual(self.title(page), "z5")
        at(7); page.click(".mctl [data-act=next]")
        page.wait_for_function("() => clip === '2026-10-16_002.mp4'")
        self.assertEqual(self.title(page), "z2")

    def test_quick_steps_across_clips(self):
        """▶▶ ▶▶ fast across a clip boundary (the next clip still loading): two moments on, not one."""
        S = "2026-10-16-quick"
        if not self.api.load(S)["moments"]:
            for i, c in enumerate(self.SESSIONS[S]):
                self.api.tag(S, c, 10 + i, 12 + i, f"q{i + 1}", [])
        page = self.open(S, PHONE)
        self.all_clips_on(page)
        page.evaluate("() => playMoment(S.moments.find(m => m.title === 'q1').id)")
        page.wait_for_function("() => clip === '2026-10-16_001.mp4' && cur")
        page.evaluate("""() => { const f = window.fetch;   // 002's series answers 1 s late
          window.fetch = (u, o) => String(u).includes('series?video=2026-10-16_002') ? new Promise(r => setTimeout(() => r(f(u, o)), 1000)) : f(u, o); }""")
        page.click(".mctl [data-act=next]")
        page.click(".mctl [data-act=next]")
        page.wait_for_timeout(1500)
        self.assertEqual((page.evaluate("() => clip"), self.title(page)), ("2026-10-16_003.mp4", "q3"))

    def test_quick_steps_into_a_clip_with_two_moments(self):
        """▶▶ ▶▶ fast into a clip holding both next moments: the second press plays the later one at once, and the
        first press's load, finishing after, does not play its moment over it."""
        S = "2026-10-16-two"
        if not self.api.load(S)["moments"]:
            self.api.tag(S, "2026-10-16_001.mp4", 10, 12, "w1", [])
            self.api.tag(S, "2026-10-16_002.mp4", 5, 7, "w2", [])
            self.api.tag(S, "2026-10-16_002.mp4", 20, 22, "w3", [])
        page = self.open(S, PHONE)
        self.all_clips_on(page)
        page.evaluate("() => playMoment(S.moments.find(m => m.title === 'w1').id)")
        page.wait_for_function("() => clip === '2026-10-16_001.mp4' && cur")
        page.evaluate("""() => { const f = window.fetch;   // 002's series answers 1 s late
          window.fetch = (u, o) => String(u).includes('series?video=2026-10-16_002') ? new Promise(r => setTimeout(() => r(f(u, o)), 1000)) : f(u, o); }""")
        page.click(".mctl [data-act=next]")
        page.click(".mctl [data-act=next]")
        page.wait_for_timeout(1500)
        self.assertEqual(self.title(page), "w3")
        self.assertGreaterEqual(page.evaluate("() => v.currentTime"), 20)

    def test_a_steep_start_is_still_a_swipe(self):
        """A thumb arc that starts more up than sideways and ends as a swipe changes clip and leaves the header;
        on the video the header's drag goes by the whole drag, decided at the end."""
        page = self.swipe_page()
        page.evaluate(self.GESTURE, [[-10, -20], [-15, -35], [-60, -45], [-150, -50]])
        page.wait_for_function("() => clip === '2026-10-16_003.mp4'")
        self.assertFalse(page.evaluate("() => document.body.classList.contains('hide-head')"))
        page.evaluate(self.GESTURE, [[-5, -40], [-10, -90]])              # up, a little sideways: the header goes
        self.assertTrue(page.evaluate("() => document.body.classList.contains('hide-head')"))
        page.evaluate(self.GESTURE, [[5, 40], [10, 90]])                  # and comes back
        self.assertFalse(page.evaluate("() => document.body.classList.contains('hide-head')"))

    def test_a_diagonal_drag_on_the_video_does_neither(self):
        """Between the two: too steep for a swipe, too flat for the header (|dy| over half |dx|, not over |dx|)."""
        page = self.swipe_page()
        page.evaluate(self.GESTURE, [[-40, -25], [-80, -50]])
        page.wait_for_timeout(150)
        self.assertEqual(page.evaluate("() => clip"), "2026-10-16_002.mp4")
        self.assertFalse(page.evaluate("() => document.body.classList.contains('hide-head')"))

    def test_late_suggestions_stay_with_their_clip(self):
        """A clip's suggestions answering after a swipe on do not land on the next clip."""
        page = self.swipe_page()
        page.wait_for_timeout(300)
        page.evaluate("""() => { const f = window.fetch;   // 002's suggestions answer 1.5 s late
          window.fetch = (u, o) => String(u).includes('events?video=2026-10-16_002') ? new Promise(r => setTimeout(() => r(f(u, o)), 1500)) : f(u, o);
          loadClip('2026-10-16_002.mp4'); }""")
        page.wait_for_timeout(200)
        page.evaluate(self.SWIPE, [-150, 0, None, 1])
        page.wait_for_function("() => clip === '2026-10-16_003.mp4'")
        page.wait_for_timeout(300)
        page.evaluate("() => { window.__sugg = sugg; }")
        page.wait_for_timeout(1800)
        self.assertTrue(page.evaluate("() => sugg === window.__sugg"))

    def test_an_arm_click_does_not_seek_a_clip_chosen_since(self):
        """The logs tab's arm row loads its clip and seeks there; a swipe on before that clip loaded is not
        seeked to the arm's time."""
        page = self.open("2026-10-16-swipe", PHONE)
        page.click("#mtabs [data-mtab=logs]")
        page.evaluate("""() => { loadClip('2026-10-16_001.mp4');
          S.blackbox = ['L.bbl']; S.boots = {}; S.arms_pending = [];
          S.arms = [{bbl: 'L.bbl', index: 1, length: 10, motors_spun: true, complete: true, videos: [{video: '2026-10-16_002.mp4', offset: 30}]}];
          renderLogs();
          const f = window.fetch;   // 002's series answers 1 s late: the click's loadClip waits
          window.fetch = (u, o) => String(u).includes('series?video=2026-10-16_002') ? new Promise(r => setTimeout(() => r(f(u, o)), 1000)) : f(u, o); }""")
        page.click("#logs .arm.has")
        page.wait_for_function("() => clip === '2026-10-16_002.mp4'")
        page.evaluate(self.SWIPE, [-150, 0, None, 1])
        page.wait_for_function("() => clip === '2026-10-16_003.mp4'")
        page.wait_for_timeout(1500)
        self.assertLess(page.evaluate("() => v.currentTime"), 1)

    def test_an_unreadable_segment_is_not_an_arm(self):
        """A log segment without frames (2026-10-16's stub, segment 27 of 61) is not counted as an arm (it was: "61
        arms · 60 with a clip" over 60 rows with a clip); the log's line names it, with the decoder's error on hover."""
        page = self.open("2026-10-16-swipe", PHONE)
        page.click("#mtabs [data-mtab=logs]")
        page.evaluate("""() => { S.blackbox = ['L.bbl']; S.boots = {}; S.arms_pending = [];
          const arm = (i, v) => ({bbl: 'L.bbl', index: i, length: 10, frames: 500, motors_spun: true, complete: true, videos: v ? [{video: v, offset: 3}] : []});
          S.arms = [arm(1, '2026-10-16_001.mp4'), {bbl: 'L.bbl', index: 2, length: 0, frames: 0, error: 'ValueError: read length must be non-negative or -1', videos: []}, arm(3, '2026-10-16_002.mp4')];
          renderLogs(); }""")
        cov = page.text_content("#logs .log .cov")
        self.assertIn("2 arms · 2 with a clip", cov)
        self.assertIn("1 unreadable segment (2)", cov)
        self.assertNotIn("without", cov)
        self.assertEqual(page.locator("#logs .arm").count(), 2)              # rows are arms; the segment is in the line
        self.assertIn("read length", page.get_attribute("#logs .log .cov .seg", "title"))

    def test_a_retimed_clip_says_so(self):
        """#30: a clip re-timed for a mis-stamped frame rate says so in the clips list, with the factor and that its
        sound went; the others say nothing."""
        page = self.open("2026-10-16-swipe", PHONE)
        page.click("#mtabs [data-mtab=clips]")
        page.evaluate("() => { S.videos[1].timescale = 2.2001; renderClips(); }")
        texts = page.locator("#clips .clip").all_inner_texts()
        self.assertIn("re-timed ×2.20", texts[1])
        self.assertIn("no sound", texts[1])
        self.assertNotIn("re-timed", texts[0] + texts[2])

    def test_suggestions_keep_their_clicks_after_the_moments_render(self):
        """Rudi: accept on a suggestion worked at first, then after playing and clicking around did nothing, and a
        suggestion no longer jumped. renderMoments put its click handler on every `.m` in the page, the suggestion rows
        included (same class), so each later render of the moments list took them over: a click "played" a moment
        that is not there. Its handler stays on its own list now."""
        S = "2026-10-16-all"
        if not any(m["title"] == "one" for m in self.api.load(S)["moments"]):
            self.api.tag(S, "2026-10-16_001.mp4", 1, 2, "one", [])
        page = self.open(S, PHONE)
        page.wait_for_function("() => window._open !== undefined")              # the page's own clip load has rendered
        page.evaluate("""() => { sugg = [{tag: 'roll', title: 'right roll', start: 20, end: 22, why: 'test', numbers: {}}, {tag: 'flip', title: 'flip', start: 40, end: 41, why: 'test', numbers: {}}];
          suggErr = ''; renderSugg(); renderMoments(); }""")                     # the moments list renders after the suggestions
        page.click("#mtabs [data-mtab=sugg]")
        page.locator("#sugg [data-acc]").nth(1).click()
        self.assertTrue(page.evaluate("() => !!ed && ed.start === 40 && document.getElementById('editor').classList.contains('open')"))
        page.click("#editor button:has-text('cancel')")
        page.evaluate("() => { renderMoments(); v.pause = () => {}; window.__seek = null; Object.defineProperty(v, 'currentTime', {set(t) { window.__seek = t; }, get() { return 0; }, configurable: true}); v.play = () => Promise.resolve(); }")
        page.locator("#sugg .m .ttl").first.click()
        self.assertEqual(page.evaluate("() => window.__seek"), 19.5)                # a suggestion still jumps (half a second before it)

    def test_flash_restores_what_is_there_now(self):
        """A status written between two flashes (the reload clearing "decoding…") is what comes back, not the text
        before the first flash; and a flash after the last one ran out starts afresh."""
        page = self.open("2026-10-16-swipe", PHONE)
        st = "document.getElementById('status').textContent"
        page.evaluate(f"() => {{ {st} = 'decoding'; flash('a'); {st} = ''; flash('b'); }}")
        page.wait_for_timeout(1700)
        self.assertEqual(page.evaluate(f"() => {st}"), "")
        page.evaluate(f"() => {{ {st} = 'x'; flash('c'); }}")
        page.wait_for_timeout(1700)
        page.evaluate(f"() => {{ {st} = 'y'; flash('d'); }}")
        page.wait_for_timeout(1700)
        self.assertEqual(page.evaluate(f"() => {st}"), "y")

    def test_a_late_refresh_does_not_land_on_the_next_clip(self):
        """refreshSession's series and suggestions are for the clip it started on: a swipe meanwhile keeps the new
        clip's (the last refresh after decoding, with 001's series late)."""
        page = self.swipe_page()
        page.evaluate("() => loadClip('2026-10-16_001.mp4')")
        page.wait_for_timeout(300)
        page.evaluate("""() => { const f = window.fetch;
          window.fetch = async (u, o) => {
            const s = String(u);
            if (s.endsWith('/api/session/2026-10-16-swipe')) {      // nothing pending: the refresh goes on to the series
              const j = await (await f(u, o)).json(); j.probe_pending = false; j.arms_pending = [];
              return new Response(JSON.stringify(j));
            }
            if (s.includes('series?video=2026-10-16_001') || s.includes('events?video=2026-10-16_001'))
              return new Promise(r => setTimeout(() => r(f(u, o)), 1500));
            return f(u, o);
          }; refreshSession(); }""")
        page.wait_for_timeout(200)
        page.evaluate(self.SWIPE, [-150, 0, None, 1])
        page.wait_for_function("() => clip === '2026-10-16_002.mp4'")
        page.wait_for_timeout(300)
        page.evaluate("() => { window.__series = series; window.__sugg = sugg; }")
        page.wait_for_timeout(2500)
        self.assertTrue(page.evaluate("() => series === window.__series && sugg === window.__sugg"))

    def test_pull_to_refresh_only_from_the_header(self):
        """Rudi: pull to refresh from the header only; a long pull inside a pane does nothing."""
        page = self.open("2026-10-16-all", PHONE)
        page.click("#mtabs [data-mtab=moments]")
        shown = page.evaluate("""() => {
          const el = document.getElementById('moments'), r = el.getBoundingClientRect(), x = 200, y = r.top + 10;
          const t = yy => new Touch({identifier: 1, target: el, clientX: x, clientY: yy});
          const fire = (type, yy) => el.dispatchEvent(new TouchEvent(type, {bubbles: true, touches: type === 'touchend' ? [] : [t(yy)], changedTouches: [t(yy)]}));
          fire('touchstart', y);
          for (let d = 10; d <= 260; d += 10) fire('touchmove', y + d);
          const on = document.getElementById('ptr').classList.contains('on');
          fire('touchcancel', y + 260);
          return on;
        }""")
        self.assertFalse(shown)

    def test_all_clips_off_is_remembered_too(self):
        S = "2026-10-16-all"
        page = self.open(S, PHONE)
        page.click("#mtabs [data-mtab=moments]")
        if not page.eval_on_selector("#allClips", "c => c.checked"):
            page.click("#allClips")
        page.click("#allClips")                                           # off
        page.reload()
        page.wait_for_function("() => typeof S !== 'undefined' && S && S.session")
        self.assertFalse(page.eval_on_selector("#allClips", "c => c.checked"))

    def test_the_moments_heading_does_not_fold_on_the_phone(self):
        page = self.open("2026-10-16-all", PHONE)
        page.click("#mtabs [data-mtab=moments]")
        page.click("#paneMoments h2", position={"x": 20, "y": 8})
        self.assertFalse(page.eval_on_selector("#paneMoments", "p => p.classList.contains('closed')"))

    # ---- the readout under the timeline ------------------------------------------------------------------------------

    READOUT = """() => {
      series = [{offset: 0, arm: 14, step: 0.1, length: 300, rows: Array(3000).fill([100, 179, 3.62, -1234])}];
      loopM = 'm1'; readout();
      const r = document.getElementById('readout'), spans = [...r.children];
      const box = r.getBoundingClientRect();
      return {tops: new Set(spans.map(s => Math.round(s.getBoundingClientRect().top))).size, height: box.height,
              right: box.right, text: r.innerText, page: document.documentElement.scrollWidth};
    }"""

    def test_the_readout_holds_still(self):
        """Rudi: each value in a box as wide as its largest, so the labels don't shift as the numbers grow and shrink
        (9 to -1234 deg/s, 1 to 3 digits of tilt). On the phone the widest of them all still fits the line."""
        for size in (PHONE, DESKTOP):
            page = self.open("2026-10-16-swipe", size)
            at = """(row) => { series = [{offset: 0, arm: 14, step: 0.1, length: 300, rows: Array(3000).fill(row)}]; readout();
              const r = document.getElementById('readout');
              return {lefts: [...r.querySelectorAll('span')].map(s => Math.round(s.getBoundingClientRect().left)), over: r.scrollWidth - r.clientWidth}; }"""
            small, big = page.evaluate(at, [5, 3, 4.2, 9]), page.evaluate(at, [100, 179, 3.62, -1234])
            self.assertEqual(small["lefts"], big["lefts"], size)
            self.assertLessEqual(big["over"], 0, size)                          # the widest values: nothing cut off

    def test_a_label_sits_against_its_own_value(self):
        """Rudi (phone screenshot): with the values right-aligned in their boxes, a short value sat at the far end of
        its box and its label read as the previous value's ("30.1s thr", "0% tilt"). Each label is followed directly
        by its value: the gap between a label and its value is a space's width, whatever the value."""
        page = self.open("2026-10-16-swipe", PHONE)
        gaps = page.evaluate("""() => { series = [{offset: 0, arm: 14, step: 0.1, length: 300, rows: Array(3000).fill([0, 5, 4.2, 9])}]; readout();
          const r = document.getElementById('readout'), out = [];
          for (const b of r.querySelectorAll('b.n')) {
            const range = document.createRange(); range.selectNodeContents(b); const text = range.getBoundingClientRect();
            const prev = b.previousSibling;                                 // the label text, or another value
            if (prev && prev.nodeType === 3 && prev.textContent.trim()) {
              const lr = document.createRange(); lr.selectNodeContents(prev); out.push([prev.textContent.trim(), Math.round(text.left - lr.getBoundingClientRect().right)]);
            }
          }
          return [out, r.textContent]; }""")                               # in one go: the test clip's error may overwrite it
        gaps, text = gaps
        self.assertTrue(gaps, gaps)
        for label, gap in gaps:
            self.assertLessEqual(gap, 6, (label, gaps))
        self.assertIn("gyro", text)

    def test_the_readout_stays_one_line_on_the_phone(self):
        """Rudi: under the timeline on the phone the readout broke into two lines with some values and back to one,
        jumping. On the phone it is one line whatever the values (the widest: a 4-digit gyro, a 3-digit tilt, looping),
        clipped at the end if it ever has to be, and the page never gets wider than the screen."""
        page = self.open("2026-10-16-swipe", PHONE)
        r = page.evaluate(self.READOUT)
        self.assertEqual(r["tops"], 1, r)
        self.assertLessEqual(r["height"], 20, r)
        self.assertLessEqual(r["page"], 412, r)
        for want in ("arm", "14", "thr", "tilt", "3.62V", "gyro", "-1234°/s"):  # the short labels, every value in view
            self.assertIn(want, r["text"])
        d = self.open("2026-10-16-swipe").evaluate(self.READOUT)             # the desktop keeps its words
        self.assertIn("throttle", d["text"])
        # a message in that row (the clip cannot play, its file is missing) is read, not glanced at: it still wraps
        page.evaluate("() => v.dispatchEvent(new Event('error'))")           # the test clip's own error may have come and gone
        self.assertIn("cannot play this clip", page.text_content("#readout"))
        m = page.evaluate("() => { const r = document.getElementById('readout'); return [r.scrollWidth, r.clientWidth, r.getBoundingClientRect().height]; }")
        self.assertLessEqual(m[0], m[1] + 1, m)                               # nothing cut off
        self.assertGreater(m[2], 20, m)                                       # on more than one line

    # ---- virtual sticks ------------------------------------------------------------------------------------------

    def test_virtual_sticks(self):
        """The sticks over the picture, Mode 2, from what /sticks gives (every axis + = right or up): full throttle
        and full left yaw put the left stick in its top left corner, full right roll and centred pitch the right one
        on its right edge, both inside the picture's lower part. Nothing between arms; `s` hides them and the
        choice survives a reload. (These clips do not play: the video's size and time are stood in for.)"""
        page = self.open("2026-10-13-phone")
        draw = """t => {
          Object.defineProperty(v, 'videoWidth', {get: () => 1280, configurable: true});
          Object.defineProperty(v, 'videoHeight', {get: () => 720, configurable: true});
          Object.defineProperty(v, 'currentTime', {get: () => t, set: () => {}, configurable: true});
          v.style.height = '450px';                                  // an element without a clip is 150 px high
          const n = 200, arr = x => Array(n).fill(x);
          sticks = [{offset: 3, arm: 1, length: 3.98, step: 0.02, throttle: arr(100), yaw: arr(-100), pitch: arr(0), roll: arr(100)}];
          const r = drawSticks(), px = (x, y) => [...document.getElementById('sticks').getContext('2d').getImageData(x, y, 1, 1).data];
          return {drawn: r, pixels: r.map(b => px(Math.round(b.dot[0]), Math.round(b.dot[1]))), w: sk.width, h: sk.height};
        }"""
        r = page.evaluate(draw, 5.0)
        (left, right), (w, h) = r["drawn"], (r["w"], r["h"])
        self.assertAlmostEqual(left["dot"][0], left["box"][0])                          # yaw full left
        self.assertAlmostEqual(left["dot"][1], left["box"][1])                          # throttle full: the top
        self.assertAlmostEqual(right["dot"][0], right["box"][0] + right["box"][2])      # roll full right
        self.assertAlmostEqual(right["dot"][1], right["box"][1] + right["box"][2] / 2)  # pitch centred
        self.assertLess(left["box"][0] + left["box"][2], right["box"][0])               # side by side, apart
        for b in (left, right):
            self.assertGreater(b["box"][1], h / 2)                                     # the lower half
            self.assertLess(b["box"][1] + b["box"][2], h)
        self.assertTrue(all(p[3] == 255 and p[0] > 200 for p in r["pixels"]), r["pixels"])   # the dots are drawn
        self.assertEqual(page.evaluate(draw, 1.0)["drawn"], [])                         # before the arm: nothing
        page.keyboard.press("s")
        r = page.evaluate(draw, 5.0)
        self.assertEqual(r["drawn"], [])
        page.reload()
        page.wait_for_function("() => typeof S !== 'undefined' && S && S.session")
        self.assertEqual(page.evaluate(draw, 5.0)["drawn"], [])                         # still off after a reload
        page.keyboard.press("s")
        self.assertEqual(len(page.evaluate(draw, 5.0)["drawn"]), 2)

    # ---- editing sessions ----------------------------------------------------------------------------------------

    # ---- sharing a moment (#57) ----------------------------------------------------------------------------------

    def shares(self, session=None):
        q = f"?session={session}" if session else "?all=1"
        return json.loads(urllib.request.urlopen(f"{self.url}/api/shares{q}").read())["shares"]

    def test_share_a_moment(self):
        """The ⤴ on a moment opens the panel filled in (the moment plus 2 s each side, its title, the sticks on
        since a match covers it), frames across the window to pick the preview image, a preview of the bundle in a
        frame, then publish: the moment carries the mark, the panel the link, the server the bundle with the
        chosen poster and the preview taken as it was. Take down: the link row goes, the mark too."""
        page = self.open(self.SHARE)
        page.click("#moments .m[data-id=m01] [data-share]")
        page.wait_for_selector("#sharebox.open")
        self.assertEqual([page.input_value("#shFrom"), page.input_value("#shTo"), page.input_value("#shText")], ["0:01.0", "0:06.0", "half roll"])
        self.assertTrue(page.is_checked("#shSticks") and page.is_enabled("#shSticks"))
        self.assertFalse(page.is_checked("#shAudio"))
        self.assertEqual(page.input_value("#shExpires"), "30d")
        self.assertEqual(page.eval_on_selector_all("#shThumbs img", "els => els.map(e => e.dataset.t)"), ["1", "2.25", "3.5", "4.75", "6"])
        self.assertEqual(page.eval_on_selector_all("#shThumbs .chip.on", "els => els.map(e => e.textContent)"), ["none"])
        page.click('#shThumbs img[data-t="3.5"]')
        self.assertEqual(page.eval_on_selector_all("#shThumbs img.on", "els => els.map(e => e.dataset.t)"), ["3.5"])
        page.evaluate("() => { Object.defineProperty(v, 'currentTime', {get: () => 4.1, set: () => {}, configurable: true}); }")
        page.click("#shThisFrame")                                                         # the playhead's frame joins the strip, picked
        self.assertEqual(page.eval_on_selector_all("#shThumbs img", "els => els.map(e => e.dataset.t)"), ["1", "2.25", "3.5", "4.1", "4.75", "6"])
        self.assertEqual(page.eval_on_selector_all("#shThumbs img.on", "els => els.map(e => e.dataset.t)"), ["4.1"])
        page.click("#shPreview")
        page.wait_for_selector("#shOut iframe", timeout=60000)
        src = page.get_attribute("#shOut iframe", "src")
        self.assertRegex(src, r"^/share-preview/[a-z0-9]+/$")
        pid = src.split("/")[2]
        self.assertTrue((self.root / "cache" / "share-preview" / pid / "poster.jpg").exists())
        frame = page.frame_locator("#shOut iframe")
        frame.locator("#v").wait_for(timeout=20000)                                        # the viewer itself, from the server's cache
        self.assertEqual(self.shares(self.SHARE), [])                                      # nothing published yet
        page.click("#shPublish")
        page.wait_for_selector("#shState [data-down]", timeout=60000)
        rows = self.shares(self.SHARE)
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["moment"], rows[0]["start"], rows[0]["end"], rows[0]["text"], rows[0]["state"]), ("m01", 1.0, 6.0, "half roll", "active"))
        tok = rows[0]["token"]
        self.assertEqual(sorted(p.name for p in (self.root / "shares" / tok).iterdir()), ["clip.mp4", "index.html", "poster.jpg"])
        self.assertFalse((self.root / "cache" / "share-preview" / pid).exists())            # taken as it was, not cut again
        self.assertEqual(page.get_attribute("#shState a.btn", "href"), f"https://share.example/{tok}/")
        self.assertIn("up until " + page.evaluate("x => new Date(x).toLocaleDateString()", rows[0]["expires_at"]), page.inner_text("#shState"))   # the reader's day (#59 review)
        self.assertIsNotNone(page.query_selector("#moments .m[data-id=m01] .shared"))
        self.assertIsNone(page.query_selector("#moments .m[data-id=m02] .shared"))
        self.assertEqual(page.evaluate("() => $('#shOut').innerHTML"), "")                 # the preview frame is gone with the publish
        # with a share up the panel edits it (Rudi, 2026-10-22): `update` keeps the link, `new link` makes another
        self.assertEqual((page.text_content("#shPublish"), page.input_value("#shExpires")), ("update", ""))
        self.assertTrue(page.is_visible("#shNew"))
        page.fill("#shText", "half roll, edited")
        page.click("#shPublish")
        page.wait_for_function("() => $('#status').textContent === 'updated'", timeout=60000)
        rows = self.shares(self.SHARE)
        self.assertEqual([(r["token"], r["text"], r["poster"]) for r in rows], [(tok, "half roll, edited", 4.1)])
        self.assertIn(">half roll, edited<", (self.root / "shares" / tok / "index.html").read_text())
        page.click("#moments .m[data-id=m01] [data-share]")                                 # opened again: prefilled from the share
        page.wait_for_function("() => $('#shText').value === 'half roll, edited'")
        self.assertEqual(page.eval_on_selector_all("#shThumbs img.on", "els => els.map(e => e.dataset.t)"), ["4.1"])
        self.assertTrue(page.is_checked("#shSticks"))
        self.assertFalse(page.is_checked("#shAudio"))
        page.click("#shNew")
        page.wait_for_function("() => document.querySelectorAll('#shState [data-down]').length === 2", timeout=60000)
        self.assertEqual(len(self.shares(self.SHARE)), 2)
        for _ in range(2):
            page.once("dialog", lambda d: d.accept())
            n = page.evaluate("() => document.querySelectorAll('#shState [data-down]').length")
            page.click("#shState [data-down]")
            page.wait_for_function("(n) => document.querySelectorAll('#shState [data-down]').length < n", arg=n, timeout=20000)
        self.assertEqual(self.shares(self.SHARE), [])
        self.assertFalse((self.root / "shares" / tok).exists())
        self.assertIsNone(page.query_selector("#moments .m[data-id=m01] .shared"))
        self.assertEqual((page.text_content("#shPublish"), page.input_value("#shExpires")), ("publish", "30d"))   # nothing up: a new one again

    def test_moment_buttons_under_the_thumbnail(self):
        """Four buttons beside the title squeezed it into a four-line column (Rudi's screenshot, 2026-10-22), and
        two by two under the thumbnail read badly on the desktop: on both layouts the thumbnail takes about a
        third of the card with the four buttons in one row under it, and the title has the rest; the phone's row
        is no taller than the two-by-two made it."""
        rect = """() => { const m = document.querySelector('#moments .m'), r = s => m.querySelector(s).getBoundingClientRect();
          const bs = [...m.querySelectorAll('.act button')].map(b => b.getBoundingClientRect());
          return {row: m.getBoundingClientRect().width, height: m.getBoundingClientRect().height, img: r('img'), act: r('.act'), ttl: r('.ttl'),
                  buttons: bs.length, rows: new Set(bs.map(b => Math.round(b.top))).size, bw: Math.min(...bs.map(b => b.width))}; }"""
        for size, min_button in ((PHONE, 28), (DESKTOP, 24)):
            page = self.open(self.SHARE, size)
            if size is PHONE:
                page.click("#mtabs [data-mtab=moments]")
            page.wait_for_selector("#moments .m .act button")
            r = page.evaluate(rect)
            with self.subTest(size=size["viewport"]["width"]):
                self.assertEqual((r["buttons"], r["rows"]), (4, 1))                          # one row of four
                self.assertGreaterEqual(r["act"]["top"], r["img"]["bottom"] - 1)             # under the thumbnail
                self.assertLessEqual(r["act"]["right"], r["ttl"]["left"])                    # in the thumbnail's column
                self.assertGreater(r["ttl"]["left"], r["img"]["right"])                       # the words beside, not under
                self.assertGreaterEqual(r["img"]["width"], 100)                              # a thumbnail worth looking at
                self.assertLessEqual(r["img"]["width"], 0.45 * r["row"])                     # about a third: the words keep most of the card
                self.assertGreaterEqual(r["ttl"]["width"], 0.55 * r["row"])
                self.assertGreaterEqual(r["bw"], min_button)
                self.assertLessEqual(r["height"], 130)

    def test_suggestion_rows_line_up(self):
        """A suggestion has no thumbnail: its diamond had the thumbnail's column (a gap on the left) and the accept
        button sat in the stretching column, so its distance to the title changed with the title's length (Rudi's
        screenshot, 2026-10-22). Now: a narrow diamond column, the words left, the button at the right edge, the
        same on every row and at both sizes."""
        rect = """() => [...document.querySelectorAll('#sugg .m')].map(m => { const R = m.getBoundingClientRect(), r = s => m.querySelector(s).getBoundingClientRect();
          return {left: R.left, right: R.right, dia: r('.dia'), ttl: r('.ttl'), t: r('.t'), act: r('.act'), why: r('.why')}; })"""
        for size in (DESKTOP, PHONE):
            page = self.open(self.SUGG, size)
            if size is PHONE:
                page.click("#mtabs [data-mtab=sugg]")
            page.wait_for_function("() => document.querySelectorAll('#sugg .m').length >= 2", timeout=20000)
            rows = page.evaluate(rect)
            with self.subTest(size=size["viewport"]["width"]):
                for r in rows:
                    self.assertLessEqual(r["ttl"]["left"] - r["left"], 40)                      # no thumbnail-sized gap before the words
                    self.assertLess(r["dia"]["right"], r["ttl"]["left"])                        # the diamond before the title
                    self.assertAlmostEqual(r["t"]["left"], r["ttl"]["left"], delta=1)           # the time under the title, same edge
                    self.assertGreater(r["act"]["left"], r["ttl"]["right"] - 1)                 # the button after the words
                    self.assertLessEqual(r["right"] - r["act"]["right"], 20)                    # pinned at the right edge
                    self.assertGreaterEqual(r["why"]["top"], r["act"]["bottom"] - 1)            # the why under it all
                    self.assertAlmostEqual(r["why"]["left"], r["ttl"]["left"], delta=1)
                self.assertEqual(len({round(r["act"]["left"]) for r in rows}), 1)               # the button in the same place on every row

    def test_share_panel_from_a_link_and_back(self):
        """The shares tab links a moment as the player's own links do, plus &share=1: the moment plays and its
        share panel opens. The panel links back to the tab."""
        ctx = self.browser.new_context(**DESKTOP)
        self.addCleanup(ctx.close)
        page = ctx.new_page()
        page.goto(f"{self.url}/?session={self.SHARE}&clip=2026-10-04_001&m=m01&share=1")
        page.wait_for_selector("#sharebox.open")
        self.assertEqual((page.text_content("#shId"), page.text_content("#shTitle"), page.evaluate("() => cur")), ("m01", "half roll", "m01"))
        self.assertEqual(page.get_attribute("#shAllLink", "href"), "/config#shares")
        page.goto(f"{self.url}/?session={self.SHARE}&clip=2026-10-04_001&m=m01")          # without it: the moment alone
        page.wait_for_function("() => cur === 'm01'")
        self.assertIsNone(page.query_selector("#sharebox.open"))

    def test_share_requests_one_at_a_time_and_what_comes_back_late(self):
        """#59 review: a second publish while the first ran put up a second share; an answer that was not JSON (a
        proxy's 502) left "publishing…" for good; an answer for a panel closed meanwhile threw and orphaned its
        preview. Now: the buttons wait, an error is said and the buttons come back, and a late preview for a
        closed panel is dropped."""
        page = self.open(self.SHARE)
        page.click("#moments .m[data-id=m01] [data-share]")
        page.wait_for_selector("#sharebox.open")
        page.evaluate("() => { shPublish(); shPublish(); }")                                 # two clicks in a row
        self.assertTrue(page.is_disabled("#shPublish") and page.is_disabled("#shPreview") and page.is_disabled("#shNew"))
        page.wait_for_selector("#shState [data-down]", timeout=60000)
        page.wait_for_function("() => !$('#shPublish').disabled")
        self.assertEqual(len(self.shares(self.SHARE)), 1)                                     # one share, not two
        # a proxy's 502 on the way to the server
        page.route("**/api/share/preview", lambda r: r.fulfill(status=502, body="bad gateway", content_type="text/html"))
        page.click("#shPreview")
        page.wait_for_function("() => $('#shMsg').textContent.startsWith('no answer from the server')", timeout=10000)
        self.assertFalse(page.is_disabled("#shPreview"))
        page.unroute("**/api/share/preview")
        # a preview that comes back after the panel closed: dropped, not orphaned
        held = []
        page.route("**/api/share/preview", lambda r: held.append(r))
        page.click("#shPreview")
        page.wait_for_function("() => $('#shPreview').disabled")
        page.keyboard.press("Escape")
        self.assertIsNone(page.query_selector("#sharebox.open"))
        self.assertEqual(len(held), 1)
        held[0].continue_()
        previews = lambda: [d for d in (self.root / "cache" / "share-preview").glob("*") if d.is_dir()] if (self.root / "cache" / "share-preview").exists() else []
        for _ in range(100):                                                                 # rendered, then dropped by the page
            if page.evaluate("() => !$('#shPreview').disabled") and not previews():
                break
            time.sleep(0.1)
        self.assertEqual(previews(), [])
        page.unroute("**/api/share/preview")
        page.click("#moments .m[data-id=m01] [data-share]")
        page.wait_for_selector("#sharebox.open")
        page.once("dialog", lambda d: d.accept())
        page.click("#shState [data-down]")
        page.wait_for_function("() => !document.querySelector('#shState [data-down]')", timeout=20000)

    def test_share_panel_follows_the_moments_clip(self):
        """#59 review: `now` and `this frame` read the playhead of the clip that is loaded; with "all clips" on, ⤴ on
        another clip's moment opened the panel over the wrong clip. The panel loads the moment's clip first, and a
        clip change (n/p, a swipe) closes it."""
        S = "2026-10-16-two"
        self.api.tag(S, "2026-10-16_001.mp4", 1, 2, "first", [])
        m2 = self.api.tag(S, "2026-10-16_002.mp4", 3, 4, "second", [])
        page = self.open(S)
        page.evaluate("() => loadClip('2026-10-16_001.mp4')")
        page.check("#allClips")
        page.click(f"#moments .m[data-id={m2['id']}] [data-share]")
        page.wait_for_selector("#sharebox.open")
        self.assertEqual(page.evaluate("() => [clip, SH.video, SH.moment]"), ["2026-10-16_002.mp4", "2026-10-16_002.mp4", m2["id"]])
        page.evaluate("() => loadClip('2026-10-16_001.mp4')")
        page.wait_for_function("() => !document.querySelector('#sharebox.open')")
        self.assertIsNone(page.evaluate("() => SH"))
        page.uncheck("#allClips")

    def test_share_without_a_match_and_a_refused_window(self):
        page = self.open(self.SHARE)
        page.click("#moments .m[data-id=m02] [data-share]")
        page.wait_for_selector("#sharebox.open")
        self.assertFalse(page.is_checked("#shSticks"))
        self.assertTrue(page.is_disabled("#shSticks"))
        self.assertIn("no blackbox match", page.text_content("#shSticksNote"))
        page.fill("#shTo", "0:03.0")                                                       # before the start: refused by the server, said in the panel
        page.click("#shPublish")
        page.wait_for_function("() => $('#shMsg').textContent.length > 0 && !$('#shMsg').classList.contains('busy')", timeout=20000)
        self.assertIn("before the start", page.text_content("#shMsg"))
        self.assertEqual(self.shares(self.SHARE), [])
        page.fill("#shTo", "0:08.0")
        page.click("#shPublish")
        page.wait_for_selector("#shState [data-down]", timeout=60000)
        rows = self.shares(self.SHARE)
        self.assertEqual((rows[0]["moment"], rows[0]["start"], rows[0]["end"]), ("m02", 4.0, 8.0))
        self.assertIn("const STICKS = null;", (self.root / "shares" / rows[0]["token"] / "index.html").read_text())
        page.once("dialog", lambda d: d.accept())
        page.click("#shState [data-down]")
        page.wait_for_function("() => !document.querySelector('#shState [data-down]')", timeout=20000)

    def test_share_panel_on_the_phone(self):
        """A sheet over the bottom of the screen, its buttons reachable; a preview nobody published goes with the close."""
        page = self.open(self.SHARE, PHONE)
        page.click("#moments .m[data-id=m01] [data-share]")
        page.wait_for_selector("#sharebox.open")
        box = page.locator("#sharebox").bounding_box()
        self.assertLessEqual(box["y"] + box["height"], 927 + 0.5)
        self.assertGreaterEqual(box["y"], 0)
        self.assertTrue(page.locator("#shPublish").is_visible())
        page.click("#shPreview")
        page.wait_for_selector("#shOut iframe", timeout=60000)
        pid = page.get_attribute("#shOut iframe", "src").split("/")[2]
        self.assertTrue((self.root / "cache" / "share-preview" / pid).exists())
        page.keyboard.press("Escape")
        page.wait_for_function("() => !document.querySelector('#sharebox.open')")
        for _ in range(50):
            if not (self.root / "cache" / "share-preview" / pid).exists():
                break
            time.sleep(0.1)
        self.assertFalse((self.root / "cache" / "share-preview" / pid).exists())

    def test_rename_onto_an_existing_session_merges(self):
        """#16: ✎ with the name of another session asks, then merges into it and opens it."""
        page = self.open("2026-10-11-unsorted")
        asked = []
        page.once("dialog", lambda d: (asked.append(d.message), d.accept()))
        page.click("#renameSession")
        page.fill("#askInput", "2026-10-11-bea")
        page.click("#askOk")
        page.wait_for_function("() => S.session === '2026-10-11-bea'")
        self.assertIn("Merge 2026-10-11-unsorted into it?", asked[0])
        self.assertIn("2 clips", asked[0])
        self.assertEqual(sorted(v["file"] for v in self.api.load("2026-10-11-bea")["videos"]),
                         ["2026-10-11_001.mp4", "2026-10-11_002.mp4", "2026-10-11_005.mp4"])
        self.assertNotIn("2026-10-11-unsorted", [s["session"] for s in self.api.sessions()])
        self.assertEqual(page.eval_on_selector("#session", "s => s.value"), "2026-10-11-bea")

    def test_delete_a_clip(self):
        """🗑 asks; cancelled nothing happens, confirmed the clip leaves the list and its file goes to trash/."""
        page = self.open("2026-10-12-del")
        page.once("dialog", lambda d: d.dismiss())
        page.click("[data-rm='2026-10-12_002.mp4']")
        page.wait_for_timeout(300)
        self.assertEqual(len(self.api.load("2026-10-12-del")["videos"]), 2)
        page.once("dialog", lambda d: d.accept())
        page.click("[data-rm='2026-10-12_002.mp4']")
        page.wait_for_function("() => S.videos.length === 1")
        self.assertEqual(page.locator(".clip").count(), 1)
        self.assertEqual([p.name for p in (self.data / "trash").rglob("*.mp4")], ["2026-10-12_002.mp4"])


    # ---- the logs tab: automatch, move, remove -------------------------------------------------------------------

    def logs_session(self, name):
        """A session holding both logs, 001 matched by hand to log A's arm 1."""
        self.api.attach(name, self.LOG_A)
        self.api.attach(name, self.LOG_B)
        self.api.set_match(name, "2026-10-14_001.mp4", self.LOG_A, 1, 5.0)

    def open_logs(self, session, size=DESKTOP):
        page = self.open(session, size)
        if size is PHONE:
            page.click("#mtabs [data-mtab=logs]")
        else:
            page.click("aside .tab[data-tab=logs]")
        return page

    def test_log_automatch_runs_on_the_clips_without_a_match(self):
        """The automatch button on top of the logs runs `automatch <session> --unmatched --write` on the server as a
        job, once however often it is pressed, waits for it, says what it found and reloads the session (the new
        matches, their suggestions)."""
        self.logs_session("2026-10-14-am")
        page = self.open_logs("2026-10-14-am")
        posted = []

        def done(r):                                                  # the job wrote a match meanwhile
            self.api.set_match("2026-10-14-am", "2026-10-14_002.mp4", self.LOG_B, 1, 3.0)
            r.fulfill(json={"done": True, "code": 0, "stderr": "",
                            "stdout": "2026-10-14_002.mp4: 40/50 frames readable, 1 arm runs, 1 matched (written)\n"})
        page.route("**/api/cli", lambda r: (posted.append(r.request.post_data_json), r.fulfill(json={"job": "j1"})))
        page.route("**/api/cli/j1", done)
        page.evaluate("() => { document.getElementById('logsAutomatch').click(); automatchSession(); }")
        page.wait_for_function("() => /1 matched over 1 clip/.test(document.getElementById('status').textContent)")
        self.assertEqual(posted, [{"args": ["automatch", "2026-10-14-am", "--unmatched", "--write"], "async": True}])
        self.assertTrue(page.evaluate("() => S.matches.some(m => m.video === '2026-10-14_002.mp4')"))

    def test_while_automatch_runs(self):
        """While the job runs: 🎯 is disabled and ⇄/🗑 say to wait (nothing is posted), in that session only; the
        other session's buttons work, and the job's end there neither shows A's result nor leaves 🎯 disabled."""
        A, B = "2026-10-14-run-a", "2026-10-14-run-b"
        self.api.attach(A, self.LOG_A)
        self.api.attach(B, self.LOG_A)
        page = self.open_logs(A)
        state, moves, said = {"done": False}, [], []
        page.route("**/api/cli", lambda r: r.fulfill(json={"job": "j3"}))
        page.route("**/api/cli/j3", lambda r: r.fulfill(json={"done": True, "code": 0, "stderr": "", "stdout": "x: 1 matched (written)\n"}
                                                          if state["done"] else {"done": False, "seconds": 3}))
        page.route("**/move-log", lambda r: (moves.append(1), r.continue_()))
        page.on("dialog", lambda d: (said.append(d.message), d.dismiss()))
        page.click("#logsAutomatch")
        self.assertTrue(page.eval_on_selector("#logsAutomatch", "b => b.disabled"))   # at once, not at the next poll
        self.assertEqual(page.eval_on_selector("#logsAutomatch", "b => getComputedStyle(b).opacity"), "0.45")
        page.click(f"[data-lmv='{self.LOG_A}']")
        page.click(f"[data-lrm='{self.LOG_A}']")
        self.assertEqual([m[:20] for m in said], ["automatch is running"] * 2)
        self.assertFalse(page.is_visible("#askbox"))
        self.assertEqual(moves, [])
        page.evaluate(f"() => loadSession({B!r})")
        page.wait_for_function(f"() => S.session === {B!r} && !document.getElementById('logsAutomatch').disabled")
        page.click(f"[data-lrm='{self.LOG_A}']")                       # B's: asks (dismissed), no wait
        self.assertTrue(said[-1].startswith("Take "))
        state["done"] = True
        page.wait_for_timeout(3500)                                   # A's job ends while B is open
        self.assertNotIn("matched", page.text_content("#status"))
        self.assertFalse(page.eval_on_selector("#logsAutomatch", "b => b.disabled"))

    def test_no_automatch_without_a_log(self):
        page = self.open_logs("2026-10-14-nologs")
        self.assertEqual(page.locator("#logsAutomatch").count(), 0)
        self.assertIn("no blackbox file", page.text_content("#logs"))

    def test_log_automatch_says_what_it_left_ambiguous(self):
        """#41: clips the session plan finds ambiguous, with only lengths to go by, get nothing written; 🎯 says how
        many it left for a hand match, also when that is every clip."""
        self.api.attach("2026-10-14-job", self.LOG_A)
        page = self.open_logs("2026-10-14-job")
        amb = "x.mp4: ambiguous in the session plan and only lengths to go by: nothing written, left for a hand match\n"
        answers = iter([{"stdout": "a.mp4: 9/9 frames readable, 1 arm runs, 1 matched (written)\n", "stderr": amb * 2},
                        {"stdout": "", "stderr": amb * 2}])
        page.route("**/api/cli", lambda r: r.fulfill(json={"job": "j9"}))
        page.route("**/api/cli/j9", lambda r: r.fulfill(json={"done": True, "code": 0, **next(answers)}))
        page.click("#logsAutomatch")
        page.wait_for_function("() => /left for a hand match/.test(document.getElementById('status').textContent)")
        self.assertEqual(page.text_content("#status"), "automatch: 1 matched over 1 clip · 2 ambiguous, left for a hand match")
        page.wait_for_function("() => !document.getElementById('logsAutomatch').disabled")
        page.click("#logsAutomatch")
        page.wait_for_function("() => /^automatch: 2 ambiguous/.test(document.getElementById('status').textContent)")
        self.assertEqual(page.text_content("#status"), "automatch: 2 ambiguous, left for a hand match")

    def test_log_automatch_job_gone(self):
        """A job the server does not know (restarted meanwhile): said, and no more polling."""
        self.api.attach("2026-10-14-job", self.LOG_A)
        page = self.open_logs("2026-10-14-job")
        polls = []
        page.route("**/api/cli", lambda r: r.fulfill(json={"job": "j2"}))
        page.route("**/api/cli/j2", lambda r: (polls.append(1), r.fulfill(status=404, json={"error": "no job j2 (finished and fetched, or the server restarted)"})))
        page.click("#logsAutomatch")
        page.wait_for_function("() => /no job j2/.test(document.getElementById('status').textContent)")
        page.wait_for_timeout(3500)
        self.assertEqual(len(polls), 1)
        self.assertFalse(page.eval_on_selector("#logsAutomatch", "b => b.disabled"))

    def test_suggestions_say_why_they_are_missing(self):
        """A match whose log will not decode: the pane says the server's error, not "no blackbox match"."""
        self.logs_session("2026-10-14-logs")
        page = self.open("2026-10-14-logs")
        page.wait_for_function("() => /could not load the suggestions/.test(document.getElementById('sugg').textContent)")

    def test_log_remove_and_move(self):
        """🗑 on a log asks and takes it out of the session with its matches here (the file stays in blackbox/);
        ⇄ moves it to another session, its matches here dropped the same way."""
        self.logs_session("2026-10-14-logs")
        page = self.open_logs("2026-10-14-logs")
        asked = []
        page.once("dialog", lambda d: (asked.append(d.message), d.dismiss()))
        page.click(f"[data-lrm='{self.LOG_A}']")
        page.wait_for_timeout(300)
        self.assertIn("1 match to clips here is deleted", asked[0])
        self.assertEqual(len(self.api.load("2026-10-14-logs")["blackbox"]), 2)
        page.once("dialog", lambda d: d.accept())
        page.click(f"[data-lrm='{self.LOG_A}']")
        page.wait_for_function("() => S.blackbox.length === 1 && /taken out of/.test(document.getElementById('status').textContent)")
        s = self.api.load("2026-10-14-logs")
        self.assertEqual((s["blackbox"], s["matches"]), ([self.LOG_B], []))
        page.click(f"[data-lmv='{self.LOG_B}']")
        page.fill("#askInput", "2026-10-14-other")
        page.click("#askOk")
        page.wait_for_function("() => S.blackbox.length === 0 && /moved to 2026-10-14-other/.test(document.getElementById('status').textContent)")
        self.assertEqual(self.api.load("2026-10-14-other")["blackbox"], [self.LOG_B])

    def test_log_download(self):
        """⤓ saves the .bbl from the server under its own name (one with a '#' too, which the link must encode)."""
        self.api.attach("2026-10-14-dl", self.LOG_ODD)
        page = self.open_logs("2026-10-14-dl")
        with page.expect_download() as d:
            page.click(f"[data-ldl='{self.LOG_ODD}']")
        self.assertEqual(d.value.suggested_filename, self.LOG_ODD)   # from the link's download attribute: also a 404's
        self.assertIsNone(d.value.failure())
        self.assertEqual(Path(d.value.path()).read_bytes(), self.ODD_BYTES)

    def test_a_log_whose_file_is_gone(self):
        """No ⤓ for a log the server no longer has; the head says so."""
        (self.data / "blackbox" / "gone.bbl").write_bytes(b"")
        self.api.attach("2026-10-14-gone", "gone.bbl")
        (self.data / "blackbox" / "gone.bbl").unlink()
        page = self.open_logs("2026-10-14-gone")
        self.assertEqual(page.locator("[data-ldl='gone.bbl']").count(), 0)
        self.assertIn("file gone", page.locator("#logs .log").first.inner_text())

    def test_log_move_refuses_a_session_that_does_not_exist(self):
        self.api.attach("2026-10-14-typo-src", self.LOG_A)
        page = self.open_logs("2026-10-14-typo-src")
        said = []
        page.on("dialog", lambda d: (said.append(d.message), d.accept()))
        page.click(f"[data-lmv='{self.LOG_A}']")
        page.fill("#askInput", "2026-10-14-typo")
        page.click("#askOk")
        page.wait_for_timeout(300)
        self.assertIn("no such session", said[0])
        self.assertIn(self.LOG_A, self.api.load("2026-10-14-typo-src")["blackbox"])
        self.assertNotIn("2026-10-14-typo", [s["session"] for s in self.api.sessions()])

    def test_log_buttons_on_the_phone(self):
        """On the phone the buttons are there without a hover and big enough to hit, inside the screen."""
        self.api.attach("2026-10-13-phone", self.LOG_A)
        page = self.open_logs("2026-10-13-phone", PHONE)
        for k in ("logsAutomatch", "lmv", "lrm", "ldl"):
            sel = "#" + k if k == "logsAutomatch" else f"[data-{k}='{self.LOG_A}']"
            b = page.locator(sel).bounding_box()
            self.assertIsNotNone(b, k)
            self.assertGreaterEqual(b["height"], 28, k)
            self.assertLessEqual(b["x"] + b["width"], 412, k)
            self.assertEqual(page.eval_on_selector(sel, "e => getComputedStyle(e).opacity"), "1", k)


if __name__ == "__main__":
    unittest.main()
