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
    SESSIONS = {"2026-10-11-unsorted": ["2026-10-11_001.mp4", "2026-10-11_002.mp4"], "2026-10-11-bea": ["2026-10-11_005.mp4"],
                "2026-10-12-del": ["2026-10-12_001.mp4", "2026-10-12_002.mp4"], "2026-10-13-phone": ["2026-10-13_001.mp4"]}

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

    def test_pull_to_refresh_needs_a_long_pull(self):
        """With the header showing, a pull down on it of 150 px reloads the page; a shorter one does not."""
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
        self.assertEqual(page.evaluate(pull, 100), "↓ pull to refresh")
        page.evaluate(release)
        page.wait_for_timeout(400)
        self.assertEqual(page.evaluate("window.__before"), 1)                  # no reload
        self.assertEqual(page.evaluate(pull, 170), "↻ release to refresh")
        with page.expect_navigation():
            page.evaluate(release)
        page.wait_for_load_state("load")
        page.wait_for_function("() => typeof S !== 'undefined' && S && S.session")      # the reloaded player is up
        self.assertIsNone(page.evaluate("window.__before"))
        self.assertIn("session=2026-10-13-phone", page.url)                    # comes back to the same session

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


if __name__ == "__main__":
    unittest.main()
