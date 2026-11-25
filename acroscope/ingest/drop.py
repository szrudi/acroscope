"""The drop (#75): a paired device hands a batch in over HTTP, file by file, and the inbox gets it whole.

A phone has no rsync. Its app sends a batch's files one at a time, each resumable from where it stopped, and then
commits the batch with its manifest. Until the commit the files are staged under drop/<device>/<batch>/files/:
outside inbox/ and out of the push user's reach, so nothing half-arrived is ever in the inbox. The commit checks
every file against the manifest, writes the .done marker and renames the folder to inbox/<batch>/: what the laptop's
push leaves there, appearing in one step. Ingest and its rules for the inbox are untouched.

A device is trusted no further than the push user. It names the batch and the files, so a batch id has the shape of
the laptop's (a time and a suffix: ids sort in the order the batches were made) and a file is one ingest would take
(a clip, a log, the sidecar) under a plain name. It declares each file's length before sending it; the declared
lengths of everything staged are held against a quota and against the room left on the disk, so a device cannot fill
the data dir. It reaches only its own staging: every path here starts at drop/<device>/. And a batch id is used once:
a device deletes its own copy when the server reports that id done, so a second batch under an id the inbox or the
database already has is refused.

One process stages (the server); `expire` also runs from housekeeping, on batches nothing has touched for days.
"""
import errno
import json
import os
import re
import shutil
import threading
import time
from pathlib import Path

from . import intake
from .. import ingest
from ..store import sessions
from ..config import CACHE_DIR, DATA_DIR, DROP_DIR, DROP_MIN_FREE, DROP_QUOTA


BATCH_RE = re.compile(r"[0-9]{8}-[0-9]{6}-[a-z0-9]{1,16}")
DEVICE_RE = re.compile(r"d[0-9a-f]{10}")
MAX_FILE = 4 << 30         # bytes in one file: the largest a FAT32 card holds, so the longest clip a DVR writes
MAX_FILES = 500            # files in one batch
MAX_BATCHES = 20           # batches one device has staged at a time
STALE_DAYS = 7             # a staged batch nothing was added to for this long is dropped
TAKEOVER = 5               # seconds a piece waits for the sender before it to let go of the file
CHUNK = 1 << 20


class Refused(ValueError):
    """A request the drop does not take, for a reason the device can act on."""


class Conflict(Refused):
    """It does not fit what is here: a piece at the wrong offset, a batch id that is taken."""


class TooLarge(Refused):
    """More bytes than a file may have, or than were declared."""


class Full(Refused):
    """No room: the quota, the disk, or the count of files and batches."""


class Busy(Refused):
    """Another request is writing that file right now."""


_meta = threading.Lock()         # held for every look at and change to what is declared
_busy: dict[Path, object] = {}   # the staged files a piece is arriving on, each with the way to stop its sender (or None)


# ---- names -------------------------------------------------------------------------------------------------------

def batch_ok(batch) -> str:
    if not isinstance(batch, str) or not BATCH_RE.fullmatch(batch):
        raise ValueError(f"{batch!r}: a batch is named YYYYmmdd-HHMMSS-<suffix>, the suffix of lower-case letters and digits")
    return batch


def name_ok(name) -> str:
    """A file a batch may hold: what ingest takes (a clip, a log, the sidecar), under a plain name. One a file system
    can hold (200 bytes, as it counts them; half a surrogate pair, which JSON can spell, is no text at all) and no
    command takes for an option."""
    if not isinstance(name, str) or any(ord(c) < 32 or ord(c) == 127 for c in name):
        raise ValueError(f"{name!r}: not a plain file name")
    try:
        long = len(name.encode()) > 200
    except UnicodeEncodeError:
        long = True
    if long or name.startswith("-"):
        raise ValueError(f"{name!r}: not a plain file name")
    intake.plain_name(name)
    if name.startswith(".") or not (name == "import.json" or intake.CLIP_RE.match(name) or name.lower().endswith(".bbl")):
        raise ValueError(f"{name!r}: a batch holds clips (VID*.mov), logs (*.bbl) and import.json")
    return name


def device_ok(device) -> str:
    if not isinstance(device, str) or not DEVICE_RE.fullmatch(device):
        raise ValueError(f"{device!r}: not a device")
    return device


def _stage(device, batch) -> Path:
    return DROP_DIR / device_ok(device) / batch_ok(batch)


def _whole(n) -> bool:
    return isinstance(n, int) and not isinstance(n, bool) and n >= 0


# ---- what is staged ----------------------------------------------------------------------------------------------

def _lengths(d: Path) -> dict:
    """{file name: declared length} of a staged batch. A list that is not there or cannot be read (cut short by a
    crash) declares nothing: its files are sent again, and no other batch is held up by it."""
    try:
        lengths = json.loads((d / "lengths.json").read_text())
    except (FileNotFoundError, NotADirectoryError, ValueError):
        return {}
    return lengths if isinstance(lengths, dict) and all(_whole(n) and n <= MAX_FILE for n in lengths.values()) else {}


def _save_lengths(d: Path, lengths: dict) -> None:
    tmp = d / "lengths.json.new"
    tmp.write_text(json.dumps(lengths))
    os.replace(tmp, d / "lengths.json")


def _all_staged() -> dict[Path, dict]:
    """{staging dir: its declared lengths}, every batch of every device."""
    if not DROP_DIR.is_dir():
        return {}
    staged = {}
    for dev in sorted(DROP_DIR.iterdir()):
        try:
            staged.update((d, _lengths(d)) for d in sorted(dev.iterdir()) if d.is_dir())
        except (FileNotFoundError, NotADirectoryError):     # a device's folder that went with its last batch while this looked
            pass
    return staged


def _arrived(d: Path, name: str) -> int:
    try:
        return os.lstat(d / "files" / name).st_size
    except FileNotFoundError:
        return 0


def _free() -> int:
    return shutil.disk_usage(DROP_DIR).free


def usage() -> dict:
    return {"quota": DROP_QUOTA, "staged": sum(sum(v.values()) for v in _all_staged().values()), "max_file": MAX_FILE}


def _taken(batch: str, device: str) -> bool:
    """A batch by that id was handed in: it is in the inbox, or the database knows it. Not so the device's own batch
    whose commit was cut short before the move: the database has it, the inbox never did, and its device may go on
    with it (the app sends the sidecar anew before every commit)."""
    if os.path.lexists(ingest.INBOX_DIR / batch):       # a link there too: followed or not, the name is not free
        return True
    row = sessions.store().batch(batch)
    return row is not None and not (row.get("device") == device and row["status"] == "pending")


# ---- a file ------------------------------------------------------------------------------------------------------

def create(device: str, batch: str, name: str, length: int) -> int:
    """Declare a file of `length` bytes in a device's batch; its bytes follow through append(). Returns how many are
    here already: declaring the same file again is the same upload, picked up where it stopped."""
    d, name = _stage(device, batch), name_ok(name)
    if not _whole(length):
        raise ValueError("a file's length is a whole number of bytes")
    if length > MAX_FILE:
        raise TooLarge(f"{name}: {length} bytes, and a file may have {MAX_FILE}")
    with _meta:
        staged = _all_staged()
        mine = staged.get(d, {})
        if _taken(batch, device):
            raise Conflict(f"{batch}: a batch by that id was already handed in")
        if name in mine:
            if mine[name] != length:
                raise Conflict(f"{name}: already declared with {mine[name]} bytes; take it back first to send another file")
            (d / "files").mkdir(parents=True, exist_ok=True)       # there again, should its bytes have gone from under the name
            os.close(os.open(d / "files" / name, os.O_WRONLY | os.O_CREAT, 0o644))
            return _arrived(d, name)
        if len(mine) >= MAX_FILES:
            raise Full(f"{batch}: a batch holds {MAX_FILES} files")
        if d not in staged and sum(1 for x in staged if x.parent == d.parent) >= MAX_BATCHES:
            raise Full(f"{MAX_BATCHES} batches are staged already: commit one or let it go first")
        declared = sum(sum(v.values()) for v in staged.values())
        if declared + length > DROP_QUOTA:
            raise Full(f"the drop is full: {declared} of {DROP_QUOTA} bytes are staged, {name} has {length}")
        DROP_DIR.mkdir(parents=True, exist_ok=True)
        to_come = declared - sum(_arrived(x, n) for x, v in staged.items() for n in v)
        if _free() - to_come - length < DROP_MIN_FREE:
            raise Full(f"no room on the server's disk for {name} ({length} bytes)")
        (d / "files").mkdir(parents=True, exist_ok=True)
        os.close(os.open(d / "files" / name, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644))
        _save_lengths(d, {**mine, name: length})
    return 0


def offset(device: str, batch: str, name: str) -> tuple[int, int]:
    """(bytes here, bytes declared) of a staged file."""
    d, name = _stage(device, batch), name_ok(name)
    length = _lengths(d).get(name)
    if length is None:
        raise FileNotFoundError(f"{batch}/{name}: not declared")
    return _arrived(d, name), length


def append(device: str, batch: str, name: str, at: int, stream, n: int, stop=None) -> int:
    """`n` bytes from `stream` (a buffered reader: its read1 gives what has arrived) onto a staged file, which must
    end at `at`. Returns where it ends now: short of at + n when the stream ended early. What came is kept either
    way, also when the stream breaks or goes silent and raises.

    `stop` ends this piece from outside: called from another thread, it makes `stream` end. A piece that fits a file
    another one is still arriving on is its device come back (only that device reaches the file, and it knows where
    the file ends): the sender before it is stopped and this piece goes on from there, instead of waiting until a
    connection that died without a word is given up."""
    d, name = _stage(device, batch), name_ok(name)
    if not _whole(at) or not _whole(n):
        raise ValueError("an offset and a size are whole numbers of bytes")
    f = d / "files" / name
    patience = time.monotonic() + TAKEOVER
    while True:
        with _meta:
            length = _lengths(d).get(name)
            if length is None:
                raise FileNotFoundError(f"{batch}/{name}: not declared")
            here = _arrived(d, name)
            if at != here:
                raise Conflict(f"{name}: {here} bytes are here, not {at}")
            if at + n > length:
                raise TooLarge(f"{name}: {at + n} bytes are more than the {length} declared")
            if f not in _busy:
                _busy[f] = stop
                break
            before = _busy[f]
            if before is None or time.monotonic() >= patience:
                raise Busy(f"{name}: another upload is writing it")
            try:
                before()                                # under the lock: it still holds the file, so this is its sender
            except OSError:
                pass
        time.sleep(0.02)
    try:
        left = n
        with open(f, "r+b", buffering=0) as out:        # unbuffered: what arrived is in the file, and offset() says so
            out.seek(at)
            try:
                while left:
                    piece = stream.read1(min(CHUNK, left))
                    if not piece:
                        break
                    rest = memoryview(piece)
                    while rest:                         # a write straight to the file may take part of it and say so
                        took = out.write(rest)
                        if not took:
                            raise OSError(errno.ENOSPC, "the disk took nothing of a piece")
                        rest = rest[took:]
                    left -= len(piece)
            finally:
                os.fsync(out.fileno())
        return at + n - left
    finally:
        with _meta:
            _busy.pop(f, None)


def terminate(device: str, batch: str, name: str) -> None:
    """Take a staged file back: its bytes go, its name and its room are free again."""
    d, name = _stage(device, batch), name_ok(name)
    with _meta:
        lengths = _lengths(d)
        if name not in lengths:
            raise FileNotFoundError(f"{batch}/{name}: not declared")
        if d / "files" / name in _busy:
            raise Busy(f"{name}: an upload is writing it")
        (d / "files" / name).unlink(missing_ok=True)
        del lengths[name]
        if lengths:
            _save_lengths(d, lengths)
        else:
            _clear(d)


def _clear(d: Path) -> None:
    """A staged batch gone, and its device's folder with its last one."""
    shutil.rmtree(d, ignore_errors=True)
    try:
        d.parent.rmdir()
    except OSError:
        pass


# ---- the batch ---------------------------------------------------------------------------------------------------

def _manifest(man) -> dict:
    files = man.get("files") if isinstance(man, dict) else None
    if not isinstance(files, dict) or not files:
        raise ValueError('a commit carries the manifest: {"files": {name: bytes}}')
    for name, size in files.items():
        name_ok(name)
        if not _whole(size):
            raise ValueError(f"{name}: its size is a whole number of bytes")
    if all(name == "import.json" for name in files):
        raise ValueError("nothing to ingest: the manifest names no clip and no log")
    return files


# an absolute path in quotes (Python quotes one with an apostrophe in it with "), and its last part
_QUOTED = re.compile(r"""'/[^'\n]*/([^/'\n]*)'|""" + r'"/[^"\n]*/([^/"\n]*)"')
# the folders of one that stands bare, from where a word starts: after a space, a quote, a bracket or a separator
_FOLDERS = re.compile(r"""(?<![^\s'"(\[=,|:])(?:~?/)?(?:[^/\s'"|:,()\[\]]+/)+""")


def _public(b: dict) -> dict:
    """A batch as its device is told of it: not who handed it in, and what went wrong without where. Ingest keeps the
    system's own words for a failure and the inbox shows them whole; they name the folders the server keeps things
    in. Those come out, the file's own name stays, and it is kept short."""
    told = {k: v for k, v in b.items() if k != "device"}
    if told.get("error"):
        said = _QUOTED.sub(lambda m: m[0][0] + (m[1] if m[1] is not None else m[2]) + m[0][0], str(told["error"]))
        for root in sorted({str(DATA_DIR), str(CACHE_DIR)}, key=len, reverse=True):     # by name: a space may be in one
            said = said.replace(root + "/", "").replace(root, "")
        said = _FOLDERS.sub("", said)
        told["error"] = said if len(said) <= 300 else said[:300] + "..."
    return told


def _status(batch: str) -> dict:
    return _public(next((b for b in ingest.list_inbox() if b["id"] == batch), {"id": batch, "status": "pending"}))


def commit(device: str, batch: str, manifest: dict) -> dict:
    """Hand a staged batch to the inbox: every file the manifest names must be here whole, anything else staged is
    left out, .done gets the manifest and the folder moves to inbox/<batch>/ in one rename. Returns the batch as the
    inbox lists it. A commit of a batch this device already handed in gives that answer again (its reply may have
    been lost); one cut short between the database and the rename is finished by the next."""
    d = _stage(device, batch)
    files = d / "files"
    db = sessions.store()
    with _meta:
        row = db.batch(batch)
        if not files.is_dir():
            if row and row.get("device") == device:
                return _status(batch)
            raise FileNotFoundError(f"{batch}: nothing staged")
        want = _manifest(manifest)
        if any(f.parent == files for f in _busy):
            raise Busy(f"{batch}: a file is still arriving")
        lengths = _lengths(d)
        for name, size in want.items():
            if name not in lengths:
                raise ValueError(f"{name}: not uploaded")
            here = _arrived(d, name)
            if here != lengths[name]:
                raise ValueError(f"{name}: {here} of {lengths[name]} bytes are here")
            if size != here:
                raise ValueError(f"{name}: {here} bytes are here, the manifest says {size}")
        target = ingest.INBOX_DIR / batch
        if _taken(batch, device):
            raise Conflict(f"{batch}: a batch by that id was already handed in")
        _save_lengths(d, {name: lengths[name] for name in want})     # first: whatever fails next, nothing gone is still declared
        for f in files.iterdir():
            if f.name not in want:
                f.unlink()
        with open(files / ".done", "w") as f:
            f.write(json.dumps({"files": want}))
            f.flush()
            os.fsync(f.fileno())
        db.set_batch(batch, device=device)
        ingest.INBOX_DIR.mkdir(parents=True, exist_ok=True)
        os.rename(files, target)         # onto a name that turned up meanwhile: a full folder or a link refuses, as it should
        _clear(d)
    return _status(batch)


def batches_of(device: str) -> list[dict]:
    """The batches a device handed in, as the inbox lists them."""
    return [_public(b) for b in ingest.list_inbox() if b.get("device") == device]


def retry(device: str, batch: str) -> dict:
    """A device's own failed batch back to pending, for ingest to try again."""
    db = sessions.store()
    row = db.batch(batch_ok(batch))
    if row is None or row.get("device") != device:
        raise FileNotFoundError(f"{batch}: no such batch")
    if row["status"] != "failed":
        raise Conflict(f"{batch} is {row['status']}: only a failed batch is tried again")
    db.set_batch(batch, status="pending", error=None)
    return _status(batch)


def events(limit: int = 20) -> list[str]:
    """What the recent sessions were called, newest first, each once: the names a device offers for the next one."""
    out = []
    for s in sorted(sessions.all_sessions(), key=lambda s: (s.get("date") or "", s["session"]), reverse=True):
        m = re.fullmatch(r"\d{4}-\d{2}-\d{2}-(.+)", s["session"])
        if m and m.group(1) != "unsorted" and m.group(1) not in out:
            out.append(m.group(1))
    return out[:limit]


# ---- upkeep ------------------------------------------------------------------------------------------------------

def expire(days: float = STALE_DAYS, now: float | None = None, log=print) -> list[str]:
    """Drop the staged batches nothing was added to for `days`: an app uninstalled, a batch given up."""
    cutoff = (time.time() if now is None else now) - days * 86400
    gone = []
    for d in _all_staged():
        try:
            fresh = max(p.stat().st_mtime for p in (d, *d.rglob("*"))) >= cutoff
        except OSError:                        # committed or taken back while this looked: not stale
            continue
        if not fresh:
            _clear(d)
            gone.append(f"{d.parent.name}/{d.name}")
            log(f"staged batch dropped: {gone[-1]}")
    return gone


def forget(device: str) -> None:
    """Everything a device has staged, gone. Under the lock a commit holds, so not from under a batch on its way into
    the inbox; and the pieces arriving for the device are stopped first, as a piece come back stops the one before
    it. One that cannot be stopped is waited for as long as a takeover waits.

    That is so in the server. From another process (the CLI on the server's own host) the lock and the senders are
    not this one's: the token stops at once, the folder goes as it stands."""
    root = DROP_DIR / device_ok(device)
    patience = time.monotonic() + TAKEOVER
    while True:
        with _meta:
            arriving = [stop for f, stop in _busy.items() if root in f.parents]
            if not arriving or time.monotonic() >= patience:
                shutil.rmtree(root, ignore_errors=True)
                return
            for stop in arriving:
                try:
                    if stop is not None:
                        stop()
                except OSError:
                    pass
        time.sleep(0.02)


def revoke(device: str) -> bool:
    """A device's token stopped, and what it had staged gone with it. False when it was not paired (any more)."""
    revoked = sessions.store().revoke_device(device_ok(device))
    forget(device)
    return revoked
