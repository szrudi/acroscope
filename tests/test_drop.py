"""The drop (#75) on its own: a device's batch staged file by file on a scratch data dir and handed to the inbox
whole. No server here; tests/test_drop_api.py drives the routes."""
import errno
import io
import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from acroscope import ingest
from acroscope.ingest import drop, intake
from acroscope.store import sessions
from acroscope.store.db import Db


B = "20261011-100000-ab12"
DEV, OTHER = "d0123456789", "dfedcba9876"
CLIP, LOG = b"\x00\x00\x00\x14ftypqt  " + bytes(range(256)) * 3, b"H Product:Blackbox flight data recorder by Nicholas Sherlock\n" + b"\x01" * 300
SIDECAR = json.dumps({"date": "2026-10-11", "session": "park"}).encode()


class _Drop(unittest.TestCase):
    """A scratch data dir and database, the drop and the inbox pointed at them."""
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.inbox, self.staging = root / "data" / "inbox", root / "data" / "drop"
        self.db = Db(root / "t.db")
        for p in (mock.patch.object(ingest, "INBOX_DIR", self.inbox), mock.patch.object(drop, "DROP_DIR", self.staging),
                  mock.patch.object(drop, "DROP_MIN_FREE", 0), mock.patch.object(sessions, "_store", self.db),
                  mock.patch.object(sessions, "SERVER_URL", "")):
            p.start()
            self.addCleanup(p.stop)

    def put(self, name: str, data: bytes, batch: str = B, device: str = DEV) -> None:
        """A whole file through the drop, in two pieces."""
        self.assertEqual(drop.create(device, batch, name, len(data)), 0)
        half = len(data) // 2
        self.assertEqual(drop.append(device, batch, name, 0, io.BytesIO(data[:half]), half), half)
        self.assertEqual(drop.append(device, batch, name, half, io.BytesIO(data[half:]), len(data) - half), len(data))

    def staged(self, name: str, batch: str = B, device: str = DEV) -> Path:
        return self.staging / device / batch / "files" / name

    def tree(self, root: Path) -> list[str]:
        return sorted(str(p.relative_to(root)) for p in root.rglob("*")) if root.exists() else []


class UploadTest(_Drop):
    def test_a_file_arrives_in_pieces(self):
        self.assertEqual(drop.create(DEV, B, "VID00001.mov", len(CLIP)), 0)
        self.assertEqual(drop.offset(DEV, B, "VID00001.mov"), (0, len(CLIP)))
        self.assertEqual(drop.append(DEV, B, "VID00001.mov", 0, io.BytesIO(CLIP[:100]), 100), 100)
        self.assertEqual(drop.offset(DEV, B, "VID00001.mov"), (100, len(CLIP)))
        self.assertEqual(drop.append(DEV, B, "VID00001.mov", 100, io.BytesIO(CLIP[100:]), len(CLIP) - 100), len(CLIP))
        self.assertEqual(drop.offset(DEV, B, "VID00001.mov"), (len(CLIP), len(CLIP)))
        self.assertEqual(self.staged("VID00001.mov").read_bytes(), CLIP)
        self.assertEqual(self.tree(self.inbox), [])                         # nothing is in the inbox before the commit

    def test_a_piece_is_copied_a_chunk_at_a_time(self):
        """A piece may be tens of megabytes: it goes to the disk in chunks, never read into memory whole."""
        asked = []

        class Counting(io.BytesIO):
            def read1(self, n=-1):
                asked.append(n)
                return super().read1(n)
        data = bytes(range(256)) * 4
        drop.create(DEV, B, "VID00001.mov", len(data))
        with mock.patch.object(drop, "CHUNK", 100):
            self.assertEqual(drop.append(DEV, B, "VID00001.mov", 0, Counting(data), len(data)), len(data))
        self.assertEqual(asked, [100] * 10 + [24])
        self.assertEqual(self.staged("VID00001.mov").read_bytes(), data)
        self.assertEqual(drop.CHUNK, 1 << 20)

    def test_a_write_the_disk_took_part_of_is_finished(self):
        """A write straight to the file may take part of a piece and say so (the disk filling up). The rest is written
        again: counted as written, those bytes would be missing from the middle of a file of the right length."""
        data = bytes(i * 7 % 251 for i in range(10_000))                    # no period a shift could hide in
        drop.create(DEV, B, "VID00001.mov", len(data))
        real_open, took = open, []

        class Disk:
            """The staged file on a disk that takes `share` of the first write."""
            def __init__(self, f, share): self.f, self.share = f, share
            def __enter__(self): return self
            def __exit__(self, *a): self.f.close()
            def seek(self, n): return self.f.seek(n)
            def fileno(self): return self.f.fileno()
            def write(self, piece):
                took.append(len(piece))
                return self.f.write(piece[:len(piece) * self.share // 2 if len(took) == 1 else len(piece)])

        def disk(share):
            return lambda path, mode="r", **kw: Disk(real_open(path, mode, **kw), share) if mode == "r+b" else real_open(path, mode, **kw)

        with mock.patch.object(drop, "CHUNK", 4096), mock.patch.object(drop, "open", disk(1), create=True):
            self.assertEqual(drop.append(DEV, B, "VID00001.mov", 0, io.BytesIO(data), len(data)), len(data))
        self.assertEqual(self.staged("VID00001.mov").read_bytes(), data)
        self.assertEqual(took[:2], [4096, 2048])                             # the half it did not take, again
        drop.terminate(DEV, B, "VID00001.mov")
        drop.create(DEV, B, "VID00001.mov", len(data))
        took.clear()
        with mock.patch.object(drop, "CHUNK", 4096), mock.patch.object(drop, "open", disk(0), create=True):
            with self.assertRaises(OSError) as e:                            # a disk that takes nothing is full: said, not waited out
                drop.append(DEV, B, "VID00001.mov", 0, io.BytesIO(data), len(data))
        self.assertEqual((e.exception.errno, len(took)), (errno.ENOSPC, 1))
        self.assertEqual(drop.offset(DEV, B, "VID00001.mov"), (0, len(data)))

    def test_an_append_must_start_where_the_file_ends(self):
        drop.create(DEV, B, "VID00001.mov", 300)
        drop.append(DEV, B, "VID00001.mov", 0, io.BytesIO(b"a" * 100), 100)
        for at in (0, 99, 101, 200):
            with self.assertRaises(drop.Conflict, msg=at) as e:
                drop.append(DEV, B, "VID00001.mov", at, io.BytesIO(b"b" * 50), 50)
            self.assertIn("100 bytes are here", str(e.exception))
        self.assertEqual(self.staged("VID00001.mov").read_bytes(), b"a" * 100)

    def test_no_more_than_was_declared(self):
        drop.create(DEV, B, "VID00001.mov", 300)
        drop.append(DEV, B, "VID00001.mov", 0, io.BytesIO(b"a" * 100), 100)
        with self.assertRaises(drop.TooLarge):
            drop.append(DEV, B, "VID00001.mov", 100, io.BytesIO(b"b" * 201), 201)
        self.assertEqual(self.staged("VID00001.mov").stat().st_size, 100)   # refused before a byte was written
        self.assertEqual(drop.append(DEV, B, "VID00001.mov", 100, io.BytesIO(b"b" * 200), 200), 300)   # to the byte: fine

    def test_a_stream_cut_short_keeps_what_came(self):
        """The connection went away in the middle of a piece: what arrived stays, and the next piece goes on from there."""
        drop.create(DEV, B, "VID00001.mov", 300)
        self.assertEqual(drop.append(DEV, B, "VID00001.mov", 0, io.BytesIO(b"a" * 40), 100), 40)
        self.assertEqual(drop.offset(DEV, B, "VID00001.mov"), (40, 300))
        self.assertEqual(drop.append(DEV, B, "VID00001.mov", 40, io.BytesIO(b"b" * 260), 260), 300)
        self.assertEqual(self.staged("VID00001.mov").read_bytes(), b"a" * 40 + b"b" * 260)

    def test_a_stream_that_breaks_keeps_what_came_and_frees_the_file(self):
        class Breaks(io.BytesIO):
            def read1(self, n=-1):
                got = super().read1(min(n, 30))
                if not got:
                    raise ConnectionResetError("gone")
                return got
        drop.create(DEV, B, "VID00001.mov", 300)
        with self.assertRaises(ConnectionResetError):
            drop.append(DEV, B, "VID00001.mov", 0, Breaks(b"a" * 60), 100)
        self.assertEqual(drop.offset(DEV, B, "VID00001.mov"), (60, 300))
        self.assertEqual(drop.append(DEV, B, "VID00001.mov", 60, io.BytesIO(b"b" * 240), 240), 300)   # not left busy

    def test_declaring_a_file_again_is_the_same_upload(self):
        drop.create(DEV, B, "VID00001.mov", 300)
        drop.append(DEV, B, "VID00001.mov", 0, io.BytesIO(b"a" * 120), 120)
        self.assertEqual(drop.create(DEV, B, "VID00001.mov", 300), 120)     # an app that lost its place asks again
        self.assertEqual(self.staged("VID00001.mov").read_bytes(), b"a" * 120)
        with self.assertRaises(drop.Conflict):                              # another file under the name is another upload
            drop.create(DEV, B, "VID00001.mov", 301)
        self.assertEqual(drop.offset(DEV, B, "VID00001.mov"), (120, 300))

    def test_a_declared_file_whose_bytes_went_is_there_again(self):
        """Declared, and its bytes are gone from under it (somebody tidied the folder): declaring it again is the same
        upload from byte 0. Otherwise it would be asked for from 0 and have nowhere to go."""
        drop.create(DEV, B, "VID00001.mov", 300)
        drop.append(DEV, B, "VID00001.mov", 0, io.BytesIO(b"a" * 120), 120)
        self.staged("VID00001.mov").unlink()
        self.assertEqual(drop.offset(DEV, B, "VID00001.mov"), (0, 300))
        self.assertEqual(drop.create(DEV, B, "VID00001.mov", 300), 0)
        self.assertEqual(drop.append(DEV, B, "VID00001.mov", 0, io.BytesIO(b"b" * 300), 300), 300)
        self.assertEqual(self.staged("VID00001.mov").read_bytes(), b"b" * 300)
        self.assertEqual(drop.create(DEV, B, "VID00001.mov", 300), 300)      # and one that is here is not touched

    def test_an_empty_file_is_whole_at_once(self):
        self.assertEqual(drop.create(DEV, B, "VID00001.mov", 0), 0)
        self.assertEqual(drop.offset(DEV, B, "VID00001.mov"), (0, 0))
        self.assertEqual(drop.append(DEV, B, "VID00001.mov", 0, io.BytesIO(b""), 0), 0)

    def test_what_was_never_declared_is_not_there(self):
        drop.create(DEV, B, "VID00001.mov", 10)
        for call in (lambda: drop.offset(DEV, B, "VID00002.mov"), lambda: drop.append(DEV, B, "VID00002.mov", 0, io.BytesIO(b"x"), 1),
                     lambda: drop.terminate(DEV, B, "VID00002.mov"), lambda: drop.offset(DEV, "20261011-100000-zz", "VID00001.mov")):
            with self.assertRaises(FileNotFoundError):
                call()
        self.assertEqual(self.tree(self.staging / DEV), [B, f"{B}/files", f"{B}/files/VID00001.mov", f"{B}/lengths.json"])

    def test_lengths_and_offsets_are_whole_numbers(self):
        for bad in (-1, 1.5, "10", None, True):
            with self.assertRaises(ValueError, msg=repr(bad)):
                drop.create(DEV, B, "VID00001.mov", bad)
        drop.create(DEV, B, "VID00001.mov", 10)
        for at, n in ((-1, 1), (0, -1), ("0", 1), (0, 1.0), (True, 1)):
            with self.assertRaises(ValueError, msg=repr((at, n))) as e:
                drop.append(DEV, B, "VID00001.mov", at, io.BytesIO(b"x"), n)
            self.assertIs(type(e.exception), ValueError, (at, n))             # not a number: said so, not taken for a piece at the wrong place
        self.assertEqual(self.staged("VID00001.mov").stat().st_size, 0)

    def test_terminate_takes_a_file_back(self):
        self.put("VID00001.mov", CLIP)
        self.put("VID00002.mov", CLIP)
        drop.terminate(DEV, B, "VID00001.mov")
        self.assertFalse(self.staged("VID00001.mov").exists())
        with self.assertRaises(FileNotFoundError):
            drop.offset(DEV, B, "VID00001.mov")
        self.assertEqual(drop.offset(DEV, B, "VID00002.mov"), (len(CLIP), len(CLIP)))
        self.assertEqual(drop.create(DEV, B, "VID00001.mov", 5), 0)          # and the name is free for another file
        drop.terminate(DEV, B, "VID00001.mov")
        drop.terminate(DEV, B, "VID00002.mov")
        self.assertEqual(self.tree(self.staging / DEV), [])                  # the last file gone: nothing of the batch is left

    def test_one_writer_per_file(self):
        """A piece still arriving holds its file: a second writer, a takedown and the commit are told it is busy."""
        entered, release = threading.Event(), threading.Event()

        class Slow(io.BytesIO):
            def read1(self, n=-1):
                entered.set()
                release.wait(10)
                return super().read1(n)
        drop.create(DEV, B, "VID00001.mov", 10)
        self.put("VID00002.mov", CLIP)
        t = threading.Thread(target=lambda: drop.append(DEV, B, "VID00001.mov", 0, Slow(b"a" * 10), 10))
        t.start()
        try:
            self.assertTrue(entered.wait(10))
            for call in (lambda: drop.append(DEV, B, "VID00001.mov", 0, io.BytesIO(b"b" * 10), 10), lambda: drop.terminate(DEV, B, "VID00001.mov"),
                         lambda: drop.commit(DEV, B, {"files": {"VID00002.mov": len(CLIP)}})):
                with self.assertRaises(drop.Busy):
                    call()
            self.assertEqual(drop.append(DEV, B, "VID00002.mov", len(CLIP), io.BytesIO(b""), 0), len(CLIP))   # another file is free
        finally:
            release.set()
            t.join(10)
        self.assertEqual(self.staged("VID00001.mov").read_bytes(), b"a" * 10)
        self.assertEqual(drop.commit(DEV, B, {"files": {"VID00001.mov": 10, "VID00002.mov": len(CLIP)}})["status"], "pending")

    def gone(self, stopped: threading.Event):
        """A sender that brings three bytes and then says nothing, until it is stopped: (its stream, an event set once
        it has gone silent)."""
        silent = threading.Event()

        class Gone(io.BytesIO):
            def read1(self, n=-1):
                if self.tell() < 3:
                    return super().read1(3)
                silent.set()
                stopped.wait(10)
                return b""
        return Gone(b"a" * 10), silent

    def test_the_device_come_back_takes_its_file_over(self):
        """A sender went away without a word (a phone off the Wi-Fi sends no FIN) and its request still holds the file.
        The next piece for that file, starting where the file ends, is the same device come back: the old sender is
        stopped the way its request said, what it brought stays, and the new piece goes on from there at once."""
        stopped, first = threading.Event(), []
        stream, silent = self.gone(stopped)
        drop.create(DEV, B, "VID00001.mov", 10)
        t = threading.Thread(target=lambda: first.append(drop.append(DEV, B, "VID00001.mov", 0, stream, 10, stop=stopped.set)))
        t.start()
        self.assertTrue(silent.wait(10))
        started = time.monotonic()
        self.assertEqual(drop.append(DEV, B, "VID00001.mov", 3, io.BytesIO(b"b" * 7), 7), 10)
        self.assertLess(time.monotonic() - started, 2)
        t.join(10)
        self.assertEqual(first, [3])                                         # the old one ended short, with what it brought
        self.assertEqual(self.staged("VID00001.mov").read_bytes(), b"aaa" + b"b" * 7)

    def test_a_piece_at_the_wrong_place_stops_nobody(self):
        """Only a piece that starts where the file ends takes it over: one sent from an older offset is refused as it
        would be on a free file, and the sender that holds the file is left to go on."""
        stopped = threading.Event()
        stream, silent = self.gone(stopped)
        drop.create(DEV, B, "VID00001.mov", 10)
        t = threading.Thread(target=lambda: drop.append(DEV, B, "VID00001.mov", 0, stream, 10, stop=stopped.set))
        t.start()
        try:
            self.assertTrue(silent.wait(10))
            for at, n, error in ((0, 10, drop.Conflict), (3, 8, drop.TooLarge)):
                with self.assertRaises(error):
                    drop.append(DEV, B, "VID00001.mov", at, io.BytesIO(b"b" * n), n)
                self.assertFalse(stopped.is_set())
        finally:
            stopped.set()
            t.join(10)

    def test_a_sender_that_does_not_let_go_keeps_the_file(self):
        """Stopping is asked, not forced: when the old sender is still there after `TAKEOVER` seconds the new piece is
        told the file is busy, as it is when the old one gave no way to stop it."""
        asked, release = [], threading.Event()
        stream, silent = self.gone(release)

        def stop():                                       # as a socket that is gone already answers being shut down
            asked.append(1)
            raise OSError(errno.ENOTCONN, "Transport endpoint is not connected")
        drop.create(DEV, B, "VID00001.mov", 10)
        t = threading.Thread(target=lambda: drop.append(DEV, B, "VID00001.mov", 0, stream, 10, stop=stop))
        t.start()
        try:
            self.assertTrue(silent.wait(10))
            with mock.patch.object(drop, "TAKEOVER", 0.3):
                started = time.monotonic()
                with self.assertRaises(drop.Busy):
                    drop.append(DEV, B, "VID00001.mov", 3, io.BytesIO(b"b" * 7), 7)
            self.assertGreaterEqual(time.monotonic() - started, 0.3)
            self.assertTrue(asked)
        finally:
            release.set()
            t.join(10)
        self.assertEqual(self.staged("VID00001.mov").read_bytes(), b"aaa")


class NamesTest(_Drop):
    def test_only_what_ingest_takes_by_a_plain_name(self):
        for good in ("VID00001.mov", "vid_2.MP4", "VID0003.avi", "BTFL_BLACKBOX_LOG_OTTO_20261010_203503_BETAFPVG473_V2.bbl", "dump #2.bbl", "LOUD.BBL", "import.json"):
            self.assertEqual(drop.create(DEV, B, good, 1), 0, good)
        before = self.tree(self.staging)
        for bad in ("../VID00001.mov", "sub/VID00001.mov", "sub\\VID00001.mov", "/etc/VID00001.mov", "..", ".", "", ".done", ".hidden.bbl", ".bbl",
                    "notes.txt", "VID00001.mov.part", "clip.mov", "import.json.bak", "IMPORT.JSON", "VID00001.mov\n", "VID\x000001.mov", "VID\t1.mov",
                    "VID\x7f1.mov", "sub/x.bbl", "sub\\x.bbl", "x/../y.bbl", "/x.bbl",
                    "x" * 197 + ".bbl", "VID" + "é" * 97 + ".mov", "VID\ud800.mov", "-x.bbl", "--help.bbl", None, 7, b"VID00001.mov"):
            with self.assertRaises(ValueError, msg=repr(bad)):
                drop.create(DEV, B, bad, 1)
            for call in (lambda: drop.offset(DEV, B, bad), lambda: drop.append(DEV, B, bad, 0, io.BytesIO(b"x"), 1), lambda: drop.terminate(DEV, B, bad)):
                with self.assertRaises(ValueError, msg=repr(bad)):
                    call()
        self.assertEqual(drop.create(DEV, B, "x" * 196 + ".bbl", 1), 0)      # 200 bytes: the longest name,
        self.assertEqual(drop.create(DEV, B, "VID" + "é" * 96 + "x.mov", 1), 0)    # counted as the file system counts them
        self.assertEqual(self.tree(self.staging), sorted(before + [f"{DEV}/{B}/files/{'x' * 196}.bbl", f"{DEV}/{B}/files/VID{'é' * 96}x.mov"]))
        for bad in ("VID\ud800.mov", "-x.bbl"):                              # and why not is said in words any answer can carry
            with self.assertRaises(ValueError) as e:
                drop.name_ok(bad)
            self.assertTrue(str(e.exception).encode())
        with self.assertRaises(ValueError) as e:                             # in a manifest too, where a name comes as JSON gives it
            drop.commit(DEV, B, {"files": {"VID\ud800.mov": 1}})
        self.assertTrue(str(e.exception).encode())
        self.assertEqual(self.tree(self.staging.parent), ["drop"] + ["drop/" + x for x in self.tree(self.staging)])   # and nothing beside the drop

    def test_a_batch_is_named_like_the_laptops(self):
        for good in ("20261011-100000-ab12", "20261010-203503-564331", "20261011-100000-a", "20261011-100000-" + "z" * 16):
            self.assertEqual(drop.create(DEV, good, "VID00001.mov", 1), 0, good)
        before = self.tree(self.staging)
        for bad in ("b1", "20261011-100000", "20261011-100000-", "20261011-100000-AB12", "20261011-100000-" + "z" * 17, "2026101-100000-ab12",
                    "../20261011-100000-ab12", "20261011-100000-ab12/..", "20261011-100000-ab_2", "20261011-100000-ab12\n", "", ".", None, 5,
                    "٢٠٢٦١٠١١-100000-ab12", "20261011-١٠٠٠٠٠-ab12"):
            for call in (lambda: drop.create(DEV, bad, "VID00001.mov", 1), lambda: drop.offset(DEV, bad, "VID00001.mov"),
                         lambda: drop.commit(DEV, bad, {"files": {"VID00001.mov": 1}}), lambda: drop.terminate(DEV, bad, "VID00001.mov")):
                with self.assertRaises(ValueError, msg=repr(bad)):
                    call()
        self.assertEqual(self.tree(self.staging), before)

    def test_a_device_is_named_by_its_id(self):
        for bad in ("", "..", "../d0123456789", "d012345678", "d0123456789a", "x0123456789", "D0123456789", "d012345678g", None):
            for call in (lambda: drop.create(bad, B, "VID00001.mov", 1), lambda: drop.offset(bad, B, "VID00001.mov"), lambda: drop.forget(bad),
                         lambda: drop.commit(bad, B, {"files": {"VID00001.mov": 1}})):
                with self.assertRaises(ValueError, msg=repr(bad)):
                    call()
        self.assertEqual(self.tree(self.staging.parent), [])


class LimitsTest(_Drop):
    def test_one_file_has_a_largest_size(self):
        with mock.patch.object(drop, "MAX_FILE", 1000):
            with self.assertRaises(drop.TooLarge):
                drop.create(DEV, B, "VID00001.mov", 1001)
            self.assertEqual(drop.create(DEV, B, "VID00001.mov", 1000), 0)
        self.assertEqual(drop.MAX_FILE, 4 << 30)                             # a FAT32 card's largest file

    def test_what_is_declared_counts_against_the_quota(self):
        """Declared, not arrived: a device cannot reserve more than the drop may hold, whatever it then sends. The
        quota is everyone's together."""
        with mock.patch.object(drop, "DROP_QUOTA", 1000):
            drop.create(DEV, B, "VID00001.mov", 400)
            drop.create(DEV, "20261011-110000-cd34", "VID00001.mov", 300)
            drop.create(OTHER, B, "VID00001.mov", 200)
            with self.assertRaises(drop.Full) as e:
                drop.create(OTHER, B, "VID00002.mov", 101)
            self.assertIn("900 of 1000 bytes", str(e.exception))
            self.assertEqual(drop.usage(), {"quota": 1000, "staged": 900, "max_file": 4 << 30})
            self.assertEqual(drop.create(OTHER, B, "VID00002.mov", 100), 0)   # to the byte
            with self.assertRaises(drop.Full):
                drop.create(DEV, B, "import.json", 1)
            drop.terminate(DEV, B, "VID00001.mov")                           # room again once a file is taken back
            self.assertEqual(drop.create(DEV, B, "VID00003.mov", 400), 0)
            self.assertEqual(drop.create(DEV, B, "VID00003.mov", 400), 0)    # asking again for what is declared needs no room

    def test_the_disk_keeps_room(self):
        """What is declared and has not arrived yet is spoken for: the disk must hold that, the new file and the
        room kept free."""
        with mock.patch.object(drop, "DROP_MIN_FREE", 1000), mock.patch.object(drop, "_free", lambda: 2000):
            drop.create(DEV, B, "VID00001.mov", 600)
            drop.append(DEV, B, "VID00001.mov", 0, io.BytesIO(b"a" * 100), 100)   # 500 still to come
            with self.assertRaises(drop.Full) as e:
                drop.create(DEV, B, "VID00002.mov", 501)
            self.assertIn("disk", str(e.exception))
            self.assertEqual(drop.create(DEV, B, "VID00002.mov", 500), 0)

    def test_the_room_is_what_the_disk_has_free(self):
        """Not what the disk holds: a full disk of any size has no room."""
        disk = lambda free: mock.patch.object(drop.shutil, "disk_usage", lambda path: mock.Mock(total=10**12, used=10**12 - free, free=free))
        with mock.patch.object(drop, "DROP_MIN_FREE", 1000):
            with disk(1500):
                self.assertEqual(drop.create(DEV, B, "VID00001.mov", 400), 0)
                with self.assertRaises(drop.Full):
                    drop.create(DEV, B, "VID00002.mov", 200)                # 1500 - the 400 to come - 200 < 1000
            with disk(1600):
                self.assertEqual(drop.create(DEV, B, "VID00002.mov", 200), 0)

    def test_so_many_files_and_batches(self):
        with mock.patch.object(drop, "MAX_FILES", 3), mock.patch.object(drop, "MAX_BATCHES", 2):
            for i in range(3):
                drop.create(DEV, B, f"VID0000{i}.mov", 1)
            with self.assertRaises(drop.Full):
                drop.create(DEV, B, "VID00009.mov", 1)
            drop.create(DEV, "20261011-110000-cd34", "VID00001.mov", 1)
            with self.assertRaises(drop.Full):
                drop.create(DEV, "20261011-120000-ef56", "VID00001.mov", 1)
            self.assertEqual(drop.create(OTHER, "20261011-120000-ef56", "VID00001.mov", 1), 0)   # counted per device
            self.assertEqual(drop.create(DEV, "20261011-110000-cd34", "VID00002.mov", 1), 0)     # a batch that is there takes more
        self.assertEqual((drop.MAX_FILES, drop.MAX_BATCHES), (500, 20))


class CommitTest(_Drop):
    def whole(self, batch: str = B, device: str = DEV) -> dict:
        self.put("VID00002.mov", CLIP, batch, device)
        self.put("VID00001.mov", CLIP[:500], batch, device)
        self.put("x.bbl", LOG, batch, device)
        self.put("import.json", SIDECAR, batch, device)
        return {"files": {"VID00001.mov": 500, "VID00002.mov": len(CLIP), "x.bbl": len(LOG), "import.json": len(SIDECAR)}}

    def test_the_batch_lands_in_the_inbox_whole(self):
        man = self.whole()
        r = drop.commit(DEV, B, man)
        self.assertEqual({k: r[k] for k in ("id", "status", "clips", "logs")}, {"id": B, "status": "pending", "clips": 2, "logs": 1})
        self.assertNotIn("device", r)
        d = self.inbox / B
        self.assertEqual(sorted(p.name for p in d.iterdir()), [".done", "VID00001.mov", "VID00002.mov", "import.json", "x.bbl"])
        self.assertEqual(json.loads((d / ".done").read_text()), man)         # the manifest as the laptop's push writes it
        self.assertEqual((d / "VID00002.mov").read_bytes(), CLIP)
        self.assertEqual((d / "x.bbl").read_bytes(), LOG)
        self.assertEqual(self.tree(self.staging / DEV), [])                  # nothing left staged
        # ingest's own checks take it as they take a pushed batch
        self.assertEqual(intake.manifest(d), man)
        intake.check_sizes(d, man)
        self.assertEqual([p.name for p in intake.batch_entries(d)], [".done", "VID00001.mov", "VID00002.mov", "import.json", "x.bbl"])
        self.assertEqual(intake.sidecar(d), {"date": "2026-10-11", "session": "park"})
        self.assertEqual(ingest.pending(), [B])
        self.assertEqual(ingest.batch_date_session(d, intake.sidecar(d)), ("2026-10-11", "2026-10-11-park"))
        row = self.db.batch(B)
        self.assertEqual((row["status"], row["device"]), ("pending", DEV))
        self.assertEqual(oct((d / "VID00001.mov").stat().st_mode & 0o777), "0o644")

    def test_only_a_whole_batch_is_committed(self):
        self.put("VID00001.mov", CLIP)
        drop.create(DEV, B, "VID00002.mov", 300)
        drop.append(DEV, B, "VID00002.mov", 0, io.BytesIO(b"a" * 100), 100)
        n = len(CLIP)
        for man, why in (({"files": {"VID00001.mov": n, "VID00002.mov": 300}}, "VID00002.mov: 100 of 300 bytes"),      # still arriving
                         ({"files": {"VID00001.mov": n, "VID00002.mov": 100}}, "VID00002.mov"),      # as far as it got is not the file
                         ({"files": {"VID00001.mov": n - 1}}, "VID00001.mov"),                       # the device's own count is off
                         ({"files": {"VID00001.mov": n + 1}}, "VID00001.mov"),
                         ({"files": {"VID00001.mov": n, "VID00003.mov": 5}}, "VID00003.mov: not uploaded"),
                         ({"files": {"VID00001.mov": n, "../x.mov": 5}}, "plain"),
                         ({"files": {"VID00001.mov": n, ".done": 2}}, ".done"),
                         ({"files": {"VID00001.mov": str(n)}}, "whole number"),
                         ({"files": {"VID00001.mov": True}}, "whole number"),
                         ({"files": {"VID00001.mov": -1}}, "whole number"),
                         ({"files": {"VID00001.mov": None}}, "whole number"),
                         ({"files": {}}, "manifest"), ({"files": ["VID00001.mov"]}, "manifest"), ({}, "manifest"), (None, "manifest"), ([], "manifest")):
            with self.assertRaises(ValueError, msg=repr(man)) as e:
                drop.commit(DEV, B, man)
            self.assertNotIsInstance(e.exception, drop.Conflict, man)
            self.assertIn(why, str(e.exception), man)
        self.assertEqual(self.tree(self.inbox), [])
        self.assertIsNone(self.db.batch(B))
        self.assertEqual(drop.offset(DEV, B, "VID00001.mov"), (n, n))        # and nothing staged was lost: it can still be finished
        drop.append(DEV, B, "VID00002.mov", 100, io.BytesIO(b"b" * 200), 200)
        self.assertEqual(drop.commit(DEV, B, {"files": {"VID00001.mov": n, "VID00002.mov": 300}})["status"], "pending")

    def test_a_batch_needs_a_clip_or_a_log(self):
        self.put("import.json", SIDECAR)
        with self.assertRaises(ValueError) as e:
            drop.commit(DEV, B, {"files": {"import.json": len(SIDECAR)}})
        self.assertIn("nothing to ingest", str(e.exception))
        self.put("x.bbl", LOG)
        self.assertEqual(drop.commit(DEV, B, {"files": {"import.json": len(SIDECAR), "x.bbl": len(LOG)}})["logs"], 1)   # a log alone is a batch

    def test_what_the_manifest_leaves_out_is_not_in_the_batch(self):
        """The device took a clip out after it had gone up: the manifest is what the batch is."""
        self.put("VID00001.mov", CLIP)
        self.put("VID00002.mov", CLIP)
        drop.create(DEV, B, "VID00003.mov", 50)                              # and one that never finished
        drop.commit(DEV, B, {"files": {"VID00002.mov": len(CLIP)}})
        self.assertEqual(sorted(p.name for p in (self.inbox / B).iterdir()), [".done", "VID00002.mov"])
        self.assertEqual(self.tree(self.staging / DEV), [])
        self.assertEqual(drop.usage()["staged"], 0)

    def test_committing_again_gives_the_answer_again(self):
        """The reply to a commit was lost on the way: the device asks again and hears how its batch is doing, also
        once ingest has taken it. Nobody else's commit gets that answer."""
        man = self.whole()
        first = drop.commit(DEV, B, man)
        self.assertEqual(drop.commit(DEV, B, man), first)
        self.db.set_batch(B, status="done", session="2026-10-11-park", result={"clips": ["2026-10-11_001"], "logs": []})
        for p in (self.inbox / B).iterdir():
            p.unlink()
        (self.inbox / B).rmdir()                                            # ingest consumed it
        again = drop.commit(DEV, B, man)
        self.assertEqual((again["status"], again["session"], again["result"]["clips"]), ("done", "2026-10-11-park", ["2026-10-11_001"]))
        with self.assertRaises(FileNotFoundError):
            drop.commit(OTHER, B, man)
        with self.assertRaises(FileNotFoundError):
            drop.commit(DEV, "20261011-100000-zz", man)                      # nothing staged, never committed
        self.assertEqual(self.tree(self.inbox), [])

    def test_a_batch_id_is_used_once(self):
        """A device deletes its copy when the server reports its batch done, so a second batch may never go by an id
        that is taken: in the inbox (pushed there, or committed by another device), or in the database (ingested)."""
        man = self.whole()
        (self.inbox / B).mkdir(parents=True)                                 # the laptop pushed a batch by that name meanwhile
        (self.inbox / B / "VID00009.mov").write_bytes(b"theirs")
        with self.assertRaises(drop.Conflict):
            drop.commit(DEV, B, man)
        self.assertEqual(self.tree(self.inbox), [B, f"{B}/VID00009.mov"])
        self.assertEqual(drop.offset(DEV, B, "x.bbl"), (len(LOG), len(LOG)))   # the device's files wait, staged
        self.assertIsNone(self.db.batch(B))
        with self.assertRaises(drop.Conflict):                               # and it gets no more files under that id
            drop.create(DEV, B, "VID00003.mov", 1)
        # an id the database knows, its folder long gone
        self.db.set_batch("20261010-090000-1", status="done")
        with self.assertRaises(drop.Conflict) as e:
            drop.create(DEV, "20261010-090000-1", "VID00001.mov", 1)
        self.assertIn("already", str(e.exception))
        self.assertEqual(self.tree(self.staging / DEV / "20261010-090000-1"), [])
        # its own batch, handed in and ingested: not a second time
        b2 = "20261011-110000-cd34"
        m2 = self.whole(b2)
        drop.commit(DEV, b2, m2)
        self.db.set_batch(b2, status="done")
        with self.assertRaises(drop.Conflict):
            drop.create(DEV, b2, "VID00001.mov", 1)

    def test_a_link_in_the_inbox_is_not_followed(self):
        """The inbox is the push user's: a link planted under the batch's name must not take the batch elsewhere."""
        man = self.whole()
        elsewhere = Path(self.tmp.name) / "elsewhere"
        elsewhere.mkdir()
        self.inbox.mkdir(parents=True)
        (self.inbox / B).symlink_to(elsewhere)
        with self.assertRaises(drop.Conflict):
            drop.commit(DEV, B, man)
        self.assertEqual(list(elsewhere.iterdir()), [])
        (self.inbox / B).unlink()
        (self.inbox / B).symlink_to(Path(self.tmp.name) / "nowhere")         # a dangling one too
        with self.assertRaises(drop.Conflict):
            drop.commit(DEV, B, man)
        self.assertFalse((Path(self.tmp.name) / "nowhere").exists())
        (self.inbox / "20261011-110000-cd34").symlink_to(Path(self.tmp.name) / "nowhere")
        with self.assertRaises(drop.Conflict):                               # and an id under a link is taken before a byte goes up
            drop.create(DEV, "20261011-110000-cd34", "VID00001.mov", 5)

    def test_two_devices_with_one_id(self):
        """Each stages on its own; the first to commit has the id."""
        man = self.whole()
        self.put("VID00007.mov", b"other", B, OTHER)
        self.assertEqual(self.staged("VID00007.mov", device=OTHER).read_bytes(), b"other")
        with self.assertRaises(FileNotFoundError):                           # one device's files are not the other's
            drop.offset(OTHER, B, "VID00001.mov")
        with self.assertRaises(FileNotFoundError):
            drop.terminate(OTHER, B, "VID00001.mov")
        drop.commit(DEV, B, man)
        with self.assertRaises(drop.Conflict):
            drop.commit(OTHER, B, {"files": {"VID00007.mov": 5}})
        self.assertEqual(sorted(p.name for p in (self.inbox / B).iterdir()), [".done", "VID00001.mov", "VID00002.mov", "import.json", "x.bbl"])
        self.assertEqual(self.db.batch(B)["device"], DEV)

    def test_a_commit_cut_short_is_finished_by_the_next(self):
        """The server died between noting the batch and moving it: the device's next commit moves it."""
        man = self.whole()
        self.put("VID00007.mov", b"other", B, OTHER)                         # another device staged under the same id meanwhile
        with mock.patch.object(drop.os, "rename", side_effect=OSError("died here")):
            with self.assertRaises(OSError):
                drop.commit(DEV, B, man)
        self.assertEqual(self.tree(self.inbox), [])
        self.assertEqual(self.db.batch(B)["device"], DEV)
        self.assertEqual(ingest.pending(), [])                               # noted, but not in the inbox: ingest leaves it
        with self.assertRaises(drop.Conflict):                               # the id is this device's now: not the other's to finish
            drop.commit(OTHER, B, {"files": {"VID00007.mov": 5}})
        with self.assertRaises(FileNotFoundError):                           # nor to hear about
            drop.commit("d9999999999", B, man)
        self.assertEqual(self.tree(self.inbox), [])
        # the device comes back as the app does: it asks where each file ends, and sends the sidecar anew and whole
        self.assertEqual(drop.create(DEV, B, "VID00001.mov", 500), 500)
        drop.terminate(DEV, B, "import.json")
        newer = json.dumps({"date": "2026-10-11", "session": "race"}).encode()
        self.put("import.json", newer)                                       # its own batch, not moved yet: not "handed in already"
        with self.assertRaises(drop.Conflict):                               # which it still is to anybody else
            drop.create(OTHER, B, "VID00009.mov", 5)
        man["files"]["import.json"] = len(newer)
        self.assertEqual(drop.commit(DEV, B, man)["status"], "pending")
        self.assertEqual(json.loads((self.inbox / B / ".done").read_text()), man)
        self.assertEqual((self.inbox / B / "import.json").read_bytes(), newer)
        self.assertEqual(ingest.pending(), [B])
        with self.assertRaises(drop.Conflict):                               # moved, the id is spent for the device too
            drop.create(DEV, B, "VID00009.mov", 5)

    def test_a_commit_that_fails_late_declares_nothing_that_is_gone(self):
        """What the manifest leaves out is removed before the batch moves. Should the move then fail, those files
        are no longer declared either: a file declared and not there would be answered as "go on from byte 0" and
        then not be found."""
        self.put("VID00001.mov", CLIP)
        self.put("VID00002.mov", CLIP)
        with mock.patch.object(drop.os, "rename", side_effect=OSError("died here")):
            with self.assertRaises(OSError):
                drop.commit(DEV, B, {"files": {"VID00001.mov": len(CLIP)}})
        self.assertEqual(json.loads((self.staging / DEV / B / "lengths.json").read_text()), {"VID00001.mov": len(CLIP)})
        self.assertEqual(drop.usage()["staged"], len(CLIP))
        with self.assertRaises(FileNotFoundError):
            drop.offset(DEV, B, "VID00002.mov")
        self.put("VID00002.mov", CLIP)                                       # declared anew, it goes up as any file
        self.assertEqual(drop.commit(DEV, B, {"files": {"VID00001.mov": len(CLIP), "VID00002.mov": len(CLIP)}})["status"], "pending")
        self.assertEqual(sorted(p.name for p in (self.inbox / B).iterdir()), [".done", "VID00001.mov", "VID00002.mov"])

    def test_a_batch_being_ingested_takes_no_second_commit(self):
        """Files staged again under an id whose batch ingest already has (a crash left them, or a bug): never moved in."""
        man = self.whole()
        with mock.patch.object(drop.os, "rename", side_effect=OSError("died here")):
            with self.assertRaises(OSError):
                drop.commit(DEV, B, man)
        self.db.set_batch(B, status="running")
        with self.assertRaises(drop.Conflict):
            drop.commit(DEV, B, man)
        for name, length in (("VID00009.mov", 5), ("VID00001.mov", 500)):    # nor is a file declared in it, new or as it was
            with self.assertRaises(drop.Conflict, msg=name):
                drop.create(DEV, B, name, length)
        self.assertEqual(self.tree(self.inbox), [])


class UpkeepTest(_Drop):
    def test_a_batch_nobody_finishes_goes(self):
        self.put("VID00001.mov", CLIP)
        self.put("VID00001.mov", CLIP, "20261011-110000-cd34")
        self.put("VID00001.mov", CLIP, B, OTHER)
        now = time.time()
        old = now - 7 * 86400 - 60
        for p in [self.staging / DEV / B, *(self.staging / DEV / B).rglob("*")]:
            os.utime(p, (old, old))
        fresh = self.staging / OTHER / B
        for p in [fresh, *fresh.rglob("*")]:
            os.utime(p, (old, old))
        os.utime(fresh / "files" / "VID00001.mov", (old + 120, old + 120))   # one piece arrived within the week
        log = []
        self.assertEqual(drop.expire(now=now, log=log.append), [f"{DEV}/{B}"])
        self.assertEqual(self.tree(self.staging / DEV), ["20261011-110000-cd34", "20261011-110000-cd34/files",
                                                         "20261011-110000-cd34/files/VID00001.mov", "20261011-110000-cd34/lengths.json"])
        self.assertTrue((fresh / "files" / "VID00001.mov").exists())
        self.assertEqual(len(log), 1)
        self.assertIn(B, log[0])
        self.assertEqual(drop.expire(now=now, log=log.append), [])
        self.assertEqual(drop.expire(days=0, now=now + 1, log=log.append), [f"{DEV}/20261011-110000-cd34", f"{OTHER}/{B}"])
        self.assertEqual(self.tree(self.staging), [])                        # the devices' own folders go with their last batch
        self.assertEqual(drop.STALE_DAYS, 7)

    def test_expire_without_a_drop(self):
        self.assertEqual(drop.expire(log=lambda m: None), [])

    def test_a_revoked_device_leaves_nothing_staged(self):
        self.put("VID00001.mov", CLIP)
        self.put("VID00001.mov", CLIP, B, OTHER)
        drop.forget(DEV)
        self.assertEqual(self.tree(self.staging), [OTHER, f"{OTHER}/{B}", f"{OTHER}/{B}/files", f"{OTHER}/{B}/files/VID00001.mov", f"{OTHER}/{B}/lengths.json"])
        drop.forget(DEV)                                                     # nothing there: fine

    def test_a_revoke_waits_its_turn(self):
        """What a device staged goes under the same lock a commit holds: a revoke in the middle of a commit would
        empty the folder the commit is moving into the inbox."""
        self.put("VID00001.mov", CLIP)
        with drop._meta:                                                     # as a commit holds it
            t = threading.Thread(target=drop.forget, args=(DEV,), daemon=True)
            t.start()
            t.join(0.3)
            self.assertTrue(t.is_alive())
            self.assertTrue(self.staged("VID00001.mov").exists())
        t.join(5)
        self.assertFalse(t.is_alive())
        self.assertEqual(self.tree(self.staging), [])

    def test_a_revoke_stops_the_piece_that_is_arriving(self):
        """A revoked device's upload does not go on into a file that is gone, a piece's length outside every count."""
        drop.create(DEV, B, "VID00001.mov", 1000)
        self.put("VID00001.mov", CLIP, B, OTHER)
        r, w, hang_up = self.pipe()
        got, stopped = {}, []

        def stop():
            stopped.append(time.monotonic())
            hang_up()

        t = threading.Thread(target=lambda: got.update(end=drop.append(DEV, B, "VID00001.mov", 0, io.BufferedReader(io.FileIO(r, "rb")), 1000, stop=stop)), daemon=True)
        t.start()
        os.write(w, b"a" * 300)
        for _ in range(250):
            if drop.offset(DEV, B, "VID00001.mov")[0] == 300:
                break
            time.sleep(0.02)
        self.assertEqual(drop.offset(DEV, B, "VID00001.mov")[0], 300)
        started = time.monotonic()
        drop.forget(DEV)
        t.join(5)
        self.assertEqual((bool(stopped), got), (True, {"end": 300}))         # stopped, and it ended where it was
        self.assertLess(time.monotonic() - started, 2)
        self.assertEqual(drop._busy, {})
        self.assertEqual(self.tree(self.staging / DEV), [])
        self.assertTrue(self.staged("VID00001.mov", B, OTHER).exists())      # another device's is not touched

    def test_a_revoke_leaves_another_devices_upload_alone(self):
        drop.create(OTHER, B, "VID00001.mov", 1000)
        self.put("VID00001.mov", CLIP)
        r, w, hang_up = self.pipe()
        got, stopped = {}, []
        t = threading.Thread(target=lambda: got.update(end=drop.append(OTHER, B, "VID00001.mov", 0, io.BufferedReader(io.FileIO(r, "rb")), 1000, stop=lambda: stopped.append(1))),
                             daemon=True)
        t.start()
        os.write(w, b"a" * 300)
        self.arriving(OTHER)
        started = time.monotonic()
        drop.forget(DEV)
        self.assertLess(time.monotonic() - started, 2)                       # it waits for nobody else's piece (that would be TAKEOVER, 5 s),
        self.assertEqual((stopped, self.tree(self.staging / DEV)), ([], []))   # and stops none
        os.write(w, b"b" * 700)
        t.join(5)
        self.assertEqual(got, {"end": 1000})

    def arriving(self, device: str) -> None:
        """Wait until a piece for `device` is in flight."""
        mine = self.staging / device
        for _ in range(250):
            if any(mine in f.parents for f in drop._busy):
                return
            time.sleep(0.02)
        self.fail("no piece is arriving")

    def test_a_piece_that_cannot_be_stopped_does_not_keep_a_revoke_waiting(self):
        drop.create(DEV, B, "VID00001.mov", 1000)
        for stop in (None, mock.Mock(side_effect=OSError(errno.ENOTCONN, "Transport endpoint is not connected"))):   # no way to stop it, or one that fails
            drop.create(DEV, B, "VID00001.mov", 1000)
            r, w, hang_up = self.pipe()
            t = threading.Thread(target=lambda: drop.append(DEV, B, "VID00001.mov", 0, io.BufferedReader(io.FileIO(r, "rb")), 1000, stop=stop), daemon=True)
            t.start()
            os.write(w, b"a" * 300)
            self.arriving(DEV)
            with mock.patch.object(drop, "TAKEOVER", 0.3):
                started = time.monotonic()
                drop.forget(DEV)
                took = time.monotonic() - started
            self.assertTrue(0.25 < took < 3, took)                           # waited for as long as a takeover waits, and no longer
            self.assertEqual(self.tree(self.staging), [])
            hang_up()
            t.join(5)
            self.assertEqual((t.is_alive(), drop._busy), (False, {}))
            if stop:
                self.assertTrue(stop.called)

    def pipe(self):
        """A sender's connection as a pipe: (the end read from, the end written to, hang up). It is hung up once, by
        the test or when the test ends, however that is."""
        r, w = os.pipe()
        hung_up = []

        def hang_up():
            if not hung_up:
                hung_up.append(True)
                os.close(w)

        self.addCleanup(hang_up)
        return r, w, hang_up

    def test_what_is_declared_and_cannot_be_read_is_not_declared(self):
        """lengths.json cut short (a crash while it was written): that batch has nothing declared, its files are sent
        again. It stops no other device, and not the housekeeping that would clear it away."""
        self.put("VID00001.mov", CLIP)
        self.put("VID00001.mov", CLIP, B, OTHER)
        for junk in ("", '{"VID00001.mov": 7', "[1, 2]", "\xff\xfe", '{"VID00001.mov": "780"}', '{"VID00001.mov": -1}', '{"VID00001.mov": null}',
                     '{"VID00001.mov": 99999999999999999999}', '{"VID00001.mov": %d}' % (drop.MAX_FILE + 1)):
            (self.staging / DEV / B / "lengths.json").write_bytes(junk.encode("latin-1"))
            self.assertEqual(drop.usage()["staged"], len(CLIP), junk)        # the other device's
            self.assertEqual(drop.create(OTHER, "20261011-110000-cd34", "VID00002.mov", 5), 0, junk)
            drop.terminate(OTHER, "20261011-110000-cd34", "VID00002.mov")
            with self.assertRaises(FileNotFoundError, msg=junk):
                drop.offset(DEV, B, "VID00001.mov")
            self.assertEqual(drop.expire(log=lambda m: None), [], junk)
        self.assertEqual(drop.create(DEV, B, "VID00001.mov", len(CLIP)), 0)   # declared again, it starts over
        self.assertEqual(drop.offset(DEV, B, "VID00001.mov"), (0, len(CLIP)))
        (self.staging / DEV / B / "lengths.json").write_text("")
        self.assertEqual(drop.expire(days=0, now=time.time() + 1, log=lambda m: None), [f"{DEV}/{B}", f"{OTHER}/{B}"])
        self.assertEqual(self.tree(self.staging), [])

    def test_a_device_folder_that_goes_while_it_is_looked_at(self):
        """The server clears a device's folder with its last batch while housekeeping, another process, lists them."""
        self.put("VID00001.mov", CLIP)
        self.put("VID00001.mov", CLIP, B, OTHER)
        real = Path.iterdir

        def going(path):
            if path.name == DEV:
                raise FileNotFoundError(2, "No such file or directory", str(path))
            return real(path)

        (self.staging / "notes.txt").write_text("somebody left this here")   # and what is no device's folder is nobody's batch
        with mock.patch.object(Path, "iterdir", going):
            self.assertEqual(drop.usage()["staged"], len(CLIP))
            self.assertEqual(drop.expire(days=0, now=time.time() + 1, log=lambda m: None), [f"{OTHER}/{B}"])
        self.assertEqual(drop.create(DEV, B, "VID00002.mov", 5), 0)


class StatusTest(_Drop):
    def test_a_device_sees_its_own_batches(self):
        for dev, batch in ((DEV, B), (OTHER, "20261011-110000-cd34")):
            self.put("VID00001.mov", CLIP, batch, dev)
            drop.commit(dev, batch, {"files": {"VID00001.mov": len(CLIP)}})
        (self.inbox / "20261011-120000-77").mkdir()                          # and one the laptop pushed
        (self.inbox / "20261011-120000-77" / ".done").write_text("")
        self.db.set_batch(B, status="failed", error="ValueError: VID00001.mov: no video stream")
        mine = drop.batches_of(DEV)
        self.assertEqual([(b["id"], b["status"], b["error"], b["clips"]) for b in mine], [(B, "failed", "ValueError: VID00001.mov: no video stream", 1)])
        self.assertNotIn("device", mine[0])
        self.assertEqual([b["id"] for b in drop.batches_of(OTHER)], ["20261011-110000-cd34"])
        self.assertEqual(drop.batches_of("d9999999999"), [])

    def test_what_went_wrong_is_told_without_the_servers_paths(self):
        """Ingest keeps the system's own words for a failure, and those name where the server keeps things. The inbox
        shows them whole; the device is told what failed, by the file's own name."""
        self.put("VID00001.mov", CLIP)
        drop.commit(DEV, B, {"files": {"VID00001.mov": len(CLIP)}})
        said = ("CalledProcessError: Command '['ffprobe', '-v', 'error', '-of', 'json', '/srv/fpv data/originals/.ingest/20261011-100000-ab12/taken/VID00001.mov']' "
                "returned non-zero exit status 1.")
        self.db.set_batch(B, status="failed", error=said)
        told = "CalledProcessError: Command '['ffprobe', '-v', 'error', '-of', 'json', 'VID00001.mov']' returned non-zero exit status 1."
        self.assertEqual(drop.batches_of(DEV)[0]["error"], told)
        self.assertEqual(drop.commit(DEV, B, {"files": {"VID00001.mov": len(CLIP)}})["error"], told)      # the commit asked again says the same
        self.assertEqual(next(b for b in ingest.list_inbox() if b["id"] == B)["error"], said)             # the inbox has it whole
        self.db.set_batch(B, status="failed", error="OSError: [Errno 28] No space left on device: '/data/videos/2026-10-11-park/2026-10-11_001.mp4' " + "x" * 500)
        told = drop.batches_of(DEV)[0]["error"]
        self.assertTrue(told.startswith("OSError: [Errno 28] No space left on device: '2026-10-11_001.mp4' xxx"), told[:80])
        self.assertTrue(told.endswith("x..."))
        self.assertEqual(len(told), 303)
        self.db.set_batch(B, status="failed", error="FileNotFoundError: /data/originals/.ingest/b/taken/VID00001.mov is not there, " + "y" * 235)
        self.assertEqual(drop.batches_of(DEV)[0]["error"], "FileNotFoundError: VID00001.mov is not there, " + "y" * 235)   # bare too, and what is short enough is whole
        # the data dir is taken out by name first, whatever is in it; then any folders left, quoted or not
        with mock.patch.object(drop, "DATA_DIR", Path("/srv/fpv data")), mock.patch.object(drop, "CACHE_DIR", Path("/var/cache/it's")):
            for said, told in (("FileNotFoundError: No such file: /srv/fpv data/originals/.ingest/b/taken/VID00001.mov", "FileNotFoundError: No such file: VID00001.mov"),
                               ("PermissionError: [Errno 13] Permission denied: \"/srv/fpv data/videos/it's here/x.mp4\"", 'PermissionError: [Errno 13] Permission denied: "x.mp4"'),
                               ("CalledProcessError: Command '['ffmpeg', '-i', 'concat:/srv/fpv data/x/1.mov|/srv/fpv data/x/2.mov']' returned 1.",
                                "CalledProcessError: Command '['ffmpeg', '-i', 'concat:1.mov|2.mov']' returned 1."),
                               ("ValueError: originals/2026-10-11-park/c1234.mov: no video", "ValueError: c1234.mov: no video"),
                               ("OSError: /var/cache/it's/arms/c1/0.json, /srv/fpv data/blackbox/x.bbl", "OSError: 0.json, x.bbl")):
                self.db.set_batch(B, status="failed", error=said)
                self.assertEqual(drop.batches_of(DEV)[0]["error"], told, said)
        self.assertEqual(drop.retry(DEV, B)["error"], None)                  # and no error is none

    def test_a_device_retries_its_own_failed_batch(self):
        self.put("VID00001.mov", CLIP)
        drop.commit(DEV, B, {"files": {"VID00001.mov": len(CLIP)}})
        with self.assertRaises(drop.Conflict):                               # pending: nothing to try again
            drop.retry(DEV, B)
        self.db.set_batch(B, status="failed", error="boom")
        with self.assertRaises(FileNotFoundError):
            drop.retry(OTHER, B)
        self.assertEqual(self.db.batch(B)["status"], "failed")
        with self.assertRaises(FileNotFoundError):
            drop.retry(DEV, "20261011-100000-zz")
        self.db.set_batch("20261011-120000-77", status="failed", error="boom")   # a pushed batch is no device's
        with self.assertRaises(FileNotFoundError):
            drop.retry(DEV, "20261011-120000-77")
        r = drop.retry(DEV, B)
        self.assertEqual((r["id"], r["status"], r["error"]), (B, "pending", None))
        self.assertEqual(ingest.pending(), [B])

    def test_recent_events(self):
        """What the last sessions were called, newest first, each once: the names a device offers for the next."""
        for name, date in (("2026-10-11-park", "2026-10-11"), ("2026-10-12-home", "2026-10-12"), ("2026-10-13-park", "2026-10-13"),
                           ("2026-10-14-unsorted", "2026-10-14"), ("2026-10-09", "2026-10-09"), ("2026-10-08-whoop-race", "2026-10-08"),
                           ("odd", None)):
            self.db.ensure_session(name, date)
        self.assertEqual(drop.events(), ["park", "home", "whoop-race"])
        self.assertEqual(drop.events(limit=2), ["park", "home"])


if __name__ == "__main__":
    unittest.main()
