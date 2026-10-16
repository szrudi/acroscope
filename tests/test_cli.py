"""The CLI's small helpers: time parsing and formatting, and --version not touching the database."""
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from acroscope.cli import fmt_time, parse_time


class CliTest(unittest.TestCase):
    def test_times(self):
        self.assertEqual(parse_time("1:32.7"), 92.7)
        self.assertEqual(parse_time("5"), 5.0)
        self.assertEqual(fmt_time(92.7), "1:32.7")
        self.assertEqual(fmt_time(59.96), "1:00.0")          # never "0:60.0"
        self.assertEqual(fmt_time(119.99), "2:00.0")
        self.assertEqual(fmt_time(-0.2), "-0:00.2")          # an event that starts before the clip does: not at 0
        self.assertEqual(fmt_time(-75.0), "-1:15.0")
        self.assertEqual(fmt_time(-0.04), "0:00.0")          # rounds to zero: no "-0:00.0"

    def test_version_opens_no_database(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = dict(os.environ, ACROSCOPE_DB=f"{tmp}/x.db", ACROSCOPE_URL="")
            r = subprocess.run([sys.executable, "-m", "acroscope.cli", "--version"], env=env, capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("x.db", r.stdout)
            self.assertFalse(Path(tmp, "x.db").exists())

    def test_a_name_that_is_not_there_is_one_line(self):
        """An unknown session (or clip) is a message and exit 1, not a traceback: the flight-analysis session and
        the player's jobs read stderr."""
        with tempfile.TemporaryDirectory() as tmp:
            env = dict(os.environ, ACROSCOPE_DB=f"{tmp}/x.db", ACROSCOPE_DATA=tmp, ACROSCOPE_URL="")
            r = subprocess.run([sys.executable, "-m", "acroscope.cli", "sessions", "2026-01-01-nope"], env=env, capture_output=True, text=True)
            self.assertEqual(r.returncode, 1)
            self.assertNotIn("Traceback", r.stderr)
            self.assertEqual(r.stderr.splitlines()[-1], "acroscope: 2026-01-01-nope: 0 sessions match (need exactly 1)")


if __name__ == "__main__":
    unittest.main()
