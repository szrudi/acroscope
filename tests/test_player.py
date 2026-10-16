"""The player end to end in a headless browser: a real `acroscope serve` child on a scratch data dir and database,
driven by Chromium through Playwright, at a desktop size and at a phone's (Rudi's: 412 x 927 CSS px).

Needs `scripts/browser-setup.sh` once (Playwright in the venv, the browser and its libraries under ~/.cache, no
root); skipped where the browser is not there, so the suite still runs anywhere. What a browser cannot stand in for:
the installed app's own viewport on the phone, and how a gesture feels."""
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
                "2026-10-16-all": ["2026-10-16_001.mp4", "2026-10-16_002.mp4"]}

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
        cls.port = free_port()
        env = dict(os.environ, ACROSCOPE_DB=str(cls.root / "t.db"), ACROSCOPE_DATA=str(cls.data),
                   ACROSCOPE_CACHE=str(cls.root / "cache"), ACROSCOPE_URL="")
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
            cls.api.refresh(s, probe_videos=False)
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

    SWIPE = """([dx, dy, fromBottom]) => {
      const v = document.getElementById('v'), r = v.getBoundingClientRect();
      const x = r.left + r.width / 2 - dx / 2, y = fromBottom ? r.bottom - 20 : r.top + r.height * 0.4;
      const t = (xx, yy) => new Touch({identifier: 1, target: v, clientX: xx, clientY: yy});
      const fire = (type, xx, yy) => v.dispatchEvent(new TouchEvent(type, {bubbles: true, touches: type === 'touchend' ? [] : [t(xx, yy)], changedTouches: [t(xx, yy)]}));
      fire('touchstart', x, y);
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
        page.evaluate(self.SWIPE, [-150, 10, False])
        page.wait_for_function("() => clip === '2026-10-16_002.mp4'")
        page.evaluate(self.SWIPE, [-150, 10, False])
        page.wait_for_function("() => clip === '2026-10-16_003.mp4'")
        page.evaluate(self.SWIPE, [-150, 10, False])                     # the last one: stays
        page.wait_for_timeout(200)
        self.assertEqual(on(), "2026-10-16_003.mp4")
        page.evaluate(self.SWIPE, [150, -10, False])
        page.wait_for_function("() => clip === '2026-10-16_002.mp4'")
        page.evaluate(self.SWIPE, [-60, 120, False])                     # mostly down: the header's gesture
        page.evaluate(self.SWIPE, [-30, 0, False])                       # too short: a tap that slid
        page.evaluate(self.SWIPE, [-150, 0, True])                       # on the seek bar
        page.wait_for_timeout(200)
        self.assertEqual(on(), "2026-10-16_002.mp4")
        dpage = self.open("2026-10-16-swipe")                            # the desktop has keys and the list: no swipe
        dpage.evaluate("() => loadClip('2026-10-16_001.mp4')")
        dpage.evaluate(self.SWIPE, [-150, 10, False])
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
        """tag opens the editor at the spot and pauses there, to fill it in at that frame."""
        page = self.open("2026-10-16-swipe", PHONE)
        page.evaluate("() => { window.__paused = 0; v.pause = () => { window.__paused++; }; }")
        page.click(".mctl [data-act=tag]")
        self.assertEqual(page.evaluate("window.__paused"), 1)
        self.assertTrue(page.is_visible("#editor"))

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
