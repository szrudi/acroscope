"""One Db used from several threads at once, as the server does (request threads plus the scan thread): no errors,
and every write is visible to a fresh connection afterwards."""
import tempfile
import threading
import time
import unittest
from pathlib import Path

from acroscope.db import Db


class ThreadsTest(unittest.TestCase):
    def test_concurrent_readers_and_writers(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "t.db"
            db = Db(path)
            S = "2026-10-07-s"
            db.upsert_video(S, "a.mp4", 1.0, "h264")
            errors, stop = [], time.time() + 1.5

            def reader():
                while time.time() < stop:
                    try:
                        db.sessions(); db.load(S)
                    except Exception as e:  # noqa: BLE001
                        errors.append(repr(e)); return

            def writer(k):
                i = 0
                while time.time() < stop:
                    try:
                        db.set_note(S, f"w{k}-{i}"); db.tag(S, "a.mp4", i, i + 1, "t", [])
                        db.move_clip(S, "a.mp4", S)                     # a BEGIN/COMMIT block, like rename and move
                    except Exception as e:  # noqa: BLE001
                        errors.append(repr(e)); return
                    i += 1

            ts = [threading.Thread(target=reader) for _ in range(6)] + [threading.Thread(target=writer, args=(k,)) for k in range(2)]
            for t in ts:
                t.start()
            for t in ts:
                t.join()
            self.assertEqual(errors, [])
            n = len(db.load(S)["moments"])
            self.assertGreater(n, 0)
            self.assertEqual(len(Db(path).load(S)["moments"]), n)       # nothing left in an open transaction


if __name__ == "__main__":
    unittest.main()
