"""The share's viewer page (#57) in a headless browser, served as a static host serves the bundle: at a phone's size
(where links get opened) and a desktop's. What it fetches (its own clip, nothing else), the video's attributes, the
sticks drawn from the inline samples at a stood-in time and gone off the log, the tap and the button that hide
them, slow motion, the text shown as text. Skipped without the browser (scripts/browser-setup.sh), like the
player's tests. The test clip does not decode in this browser: size, time and playback are stood in for."""
import functools
import http.server
import json
import os
import re
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sync_playwright = None
try:
    from tests.test_share import LOG, SESSION, build, cli
except ImportError:
    from test_share import LOG, SESSION, build, cli

LIBS = Path(os.environ.get("ACROSCOPE_BROWSER_LIBS", "~/.cache/acroscope-browser")).expanduser() / "usr" / "lib" / "x86_64-linux-gnu"
PHONE = {"viewport": {"width": 412, "height": 927}, "is_mobile": True, "has_touch": True}
DESKTOP = {"viewport": {"width": 1440, "height": 900}}
TEXT = '<b>hi</b> & a 1/2 roll\nsecond line'


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
    except Exception:  # noqa: BLE001
        return False


class _Static(http.server.SimpleHTTPRequestHandler):
    """A static host: the shares dir as files, nothing else, with byte ranges as any real one has (a video seeks
    by them; without, Chromium never settles a seek)."""
    def log_message(self, *a):
        pass

    def end_headers(self):
        if not getattr(self, "_ranged", False):
            self.send_header("Accept-Ranges", "bytes")
        super().end_headers()

    def do_GET(self):
        path = self.translate_path(self.path)
        if os.path.isdir(path):
            path = os.path.join(path, "index.html")
        m = re.fullmatch(r"bytes=(\d*)-(\d*)", self.headers.get("Range") or "")
        if not m or not (m.group(1) or m.group(2)) or not os.path.isfile(path):
            return super().do_GET()
        size = os.path.getsize(path)
        start = int(m.group(1)) if m.group(1) else max(0, size - int(m.group(2)))
        end = min(int(m.group(2)) if m.group(1) and m.group(2) else size - 1, size - 1)
        self._ranged = True
        self.send_response(206)
        self.send_header("Content-Type", self.guess_type(path))
        self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.send_header("Content-Length", str(end - start + 1))
        self.send_header("Accept-Ranges", "bytes")
        self.end_headers()
        with open(path, "rb") as f:
            f.seek(start)
            self.wfile.write(f.read(end - start + 1))


@unittest.skipUnless(browser_available(), "no headless browser: run scripts/browser-setup.sh")
class SharePageTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.tmp.name)
        env = build(cls.root)
        for args in (("refresh", SESSION), ("match", SESSION, "001", LOG, "1", "2"), ("tag", SESSION, "001", "3", "4", "half roll")):
            r = cli(env, *args)
            assert r.returncode == 0, r.stderr
        r = cli(env, "share", SESSION, "m01", "--pad", "1", "--text", TEXT)          # window 2-5 s; the arm covers 2-4.99
        assert r.returncode == 0, r.stderr
        cls.token = json.loads(r.stdout)["token"]
        port = free_port()
        cls.srv = http.server.ThreadingHTTPServer(("127.0.0.1", port), functools.partial(_Static, directory=str(cls.root / "shares")))
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.url = f"http://127.0.0.1:{port}/{cls.token}/"
        cls.pw = sync_playwright().start()
        cls.browser = _browser(cls.pw)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()
        cls.srv.shutdown()
        cls.srv.server_close()
        cls.tmp.cleanup()

    def open(self, size=PHONE):
        ctx = self.browser.new_context(**size)
        self.addCleanup(ctx.close)
        page = ctx.new_page()
        errors, requests = [], []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.on("request", lambda r: requests.append(r.url))
        self.addCleanup(lambda: self.assertEqual(errors, [], "script errors on the page"))
        page.goto(self.url)
        page.wait_for_function("() => typeof drawSticks === 'function'")
        return page, requests

    STAND_IN = """t => {
      Object.defineProperty(v, 'videoWidth', {get: () => 720, configurable: true});
      Object.defineProperty(v, 'videoHeight', {get: () => 480, configurable: true});
      Object.defineProperty(v, 'currentTime', {get: () => t, set: () => {}, configurable: true});
      v.style.height = '300px';
      const r = drawSticks(), px = (x, y) => [...sk.getContext('2d').getImageData(x, y, 1, 1).data];
      return {drawn: r, pixels: r.map(b => px(Math.round(b.dot[0]), Math.round(b.dot[1]))), w: sk.width, h: sk.height, on: $('#stk').classList.contains('on')};
    }"""

    def test_fetches_only_its_clip(self):
        """A share talks to nothing: the page and the clip beside it, no font, no script, no tracker, no API."""
        page, requests = self.open()
        for _ in range(30):
            if self.url + "clip.mp4" in requests:
                break
            time.sleep(0.1)
        self.assertIn(self.url + "clip.mp4", requests)
        self.assertEqual(set(requests), {self.url, self.url + "clip.mp4"}, requests)

    def test_video_text_and_robots(self):
        page, _ = self.open()
        self.assertEqual(page.evaluate("() => [v.loop, v.muted, v.autoplay, v.playsInline, v.controls]"), [True, True, True, True, False])
        self.assertEqual(page.evaluate("() => document.querySelector('meta[name=robots]').content"), "noindex, nofollow, noarchive")
        self.assertEqual(page.title(), "<b>hi</b> & a 1/2 roll")                             # the text's first line, as text
        self.assertEqual(page.inner_text("#text"), TEXT)
        self.assertIsNone(page.query_selector("#text b"))                                    # shown, not rendered
        self.assertEqual(page.inner_text("footer"), "acroscope")
        self.assertIn("space", page.inner_text(".keys"))                                      # the keys, said at the bottom

    def test_sticks_drawn_from_the_samples_and_hidden_by_a_tap(self):
        """At 1 s into the bundle the fake arm's sticks are: throttle 10 %, yaw 60 % left, roll 50 % right, pitch
        20 % back (+ = up on the gimbal, so 20 % below centre). Past the arm nothing is drawn. A tap on the picture
        hides them, the button shows them again."""
        page, _ = self.open()
        r = page.evaluate(self.STAND_IN, 1.0)
        (left, right), h = r["drawn"], r["h"]
        self.assertAlmostEqual(left["dot"][0], left["box"][0] + left["box"][2] * 0.2, delta=0.5)            # yaw: 20 % from the left
        self.assertAlmostEqual(left["dot"][1], left["box"][1] + left["box"][2] * 0.9, delta=0.5)            # throttle 10 %: near the bottom
        self.assertAlmostEqual(right["dot"][0], right["box"][0] + right["box"][2] * 0.75, delta=0.5)        # roll 50 % right
        self.assertAlmostEqual(right["dot"][1], right["box"][1] + right["box"][2] * 0.6, delta=0.5)         # pitch -20: below centre
        self.assertLess(left["box"][0] + left["box"][2], right["box"][0])                                   # side by side
        for b in (left, right):
            self.assertGreater(b["box"][1], h / 2)                                                          # the lower half of the picture
            self.assertLess(b["box"][1] + b["box"][2], h)
        self.assertTrue(all(p[3] == 255 and p[0] > 200 for p in r["pixels"]), r["pixels"])                  # the dots are drawn
        self.assertEqual(page.evaluate(self.STAND_IN, 3.5)["drawn"], [])                                   # past the arm: nothing
        self.assertEqual(len(page.evaluate(self.STAND_IN, 2.99)["drawn"]), 2)                              # the last sample still draws
        page.click("#stk")
        r = page.evaluate(self.STAND_IN, 1.0)
        self.assertEqual((r["drawn"], r["on"]), ([], False))
        page.click("#stk")
        r = page.evaluate(self.STAND_IN, 1.0)
        self.assertEqual((len(r["drawn"]), r["on"]), (2, True))
        # a tap on the picture plays and pauses (Rudi, 2026-10-22), and leaves the sticks as they are
        page.evaluate("""() => { window._p = []; v.play = () => { _p.push('play'); return Promise.resolve(); }; v.pause = () => _p.push('pause');
                               Object.defineProperty(v, 'paused', {get: () => _p.length % 2 === 0, configurable: true}); }""")
        page.click("#wrap")
        page.click("#wrap")
        self.assertEqual(page.evaluate("() => _p"), ["play", "pause"])
        self.assertEqual((len(page.evaluate(self.STAND_IN, 1.0)["drawn"]), page.evaluate("() => $('#stk').classList.contains('on')")), (2, True))

    def test_slow_motion_play_and_seek(self):
        page, _ = self.open()
        page.click("text=½×")
        self.assertEqual(page.evaluate("() => v.playbackRate"), 0.5)
        self.assertEqual(page.evaluate("() => [...document.querySelectorAll('[data-rate]')].filter(b => b.classList.contains('on')).map(b => b.dataset.rate)"), ["0.5"])
        page.click("text=¼×")
        self.assertEqual(page.evaluate("() => v.playbackRate"), 0.25)
        page.evaluate("""() => { window._p = []; v.play = () => { _p.push('play'); return Promise.resolve(); }; v.pause = () => _p.push('pause');
                               Object.defineProperty(v, 'paused', {get: () => _p.length % 2 === 0, configurable: true}); }""")
        page.click("#play")
        page.click("#play")
        self.assertEqual(page.evaluate("() => _p"), ["play", "pause"])
        page.evaluate("""() => { window._t = 0; Object.defineProperty(v, 'duration', {get: () => 3, configurable: true});
                               Object.defineProperty(v, 'currentTime', {get: () => _t, set: x => { _t = x; }, configurable: true}); }""")
        box = page.locator("#bar").bounding_box()
        page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
        self.assertAlmostEqual(page.evaluate("() => _t"), 1.5, delta=0.05)

    def test_keys(self):
        """The player's keys on the share too (Rudi, 2026-10-22): space plays and pauses, , and . step a frame
        (a sixtieth, the DVR's rate) and pause, the arrows 2 s, s the sticks. The page's keys, not a focused
        button's: space on the ½× button toggles the video and leaves the speed alone. Real playback: this clip
        decodes here."""
        page, _ = self.open(DESKTOP)
        page.wait_for_function("() => v.readyState >= 2 && v.duration > 0", timeout=20000)
        page.evaluate("() => { v.pause(); v.currentTime = 1.0; }")
        page.wait_for_function("() => v.paused && Math.abs(v.currentTime - 1.0) < 0.01")
        t = lambda: page.evaluate("() => v.currentTime")
        page.keyboard.press(".")
        self.assertAlmostEqual(t(), 1 + 1 / 60, delta=0.005)
        page.keyboard.press(",")
        page.keyboard.press(",")
        self.assertAlmostEqual(t(), 1 - 1 / 60, delta=0.005)
        self.assertTrue(page.evaluate("() => v.paused"))
        page.keyboard.press("ArrowRight")
        self.assertAlmostEqual(t(), 3 - 1 / 60, delta=0.01)
        page.keyboard.press("ArrowLeft")
        self.assertAlmostEqual(t(), 1 - 1 / 60, delta=0.01)
        page.keyboard.press(" ")
        page.wait_for_function("() => !v.paused", timeout=5000)
        page.keyboard.press(" ")
        page.wait_for_function("() => v.paused", timeout=5000)
        page.keyboard.press(".")                                                           # a step while playing pauses first
        self.assertTrue(page.evaluate("() => v.paused"))
        page.keyboard.press("s")
        self.assertFalse(page.evaluate("() => $('#stk').classList.contains('on')"))
        page.focus('[data-rate="0.5"]')
        page.keyboard.press(" ")
        page.wait_for_function("() => !v.paused", timeout=5000)
        self.assertEqual(page.evaluate("() => v.playbackRate"), 1)                           # the button did not fire

    def test_fits_the_phone_and_stays_narrow_on_the_desktop(self):
        page, _ = self.open(PHONE)
        self.assertFalse(page.evaluate("() => document.documentElement.scrollWidth > innerWidth"))
        self.assertEqual(page.evaluate("() => v.getBoundingClientRect().width"), 412)
        self.assertLessEqual(page.evaluate("() => document.querySelector('.row').getBoundingClientRect().height"), 60)   # one row of buttons
        page, _ = self.open(DESKTOP)
        self.assertLessEqual(page.evaluate("() => document.querySelector('main').clientWidth"), 720)


if __name__ == "__main__":
    unittest.main()
