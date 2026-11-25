"""The drop's routes (#75): the door a paired device uploads through, everything under /drop/.

    GET     /drop/device                         who the token is, and the room there is
    GET     /drop/events                         what the recent sessions were called
    GET     /drop/batches                        the batches this device handed in, with their status
    POST    /drop/batches/<batch>/files          declare a file (tus creation): Upload-Length, Upload-Metadata: filename
    HEAD    /drop/batches/<batch>/files/<name>   how much of it is here (Upload-Offset)
    PATCH   /drop/batches/<batch>/files/<name>   the next piece, at Upload-Offset
    DELETE  /drop/batches/<batch>/files/<name>   take it back (tus termination)
    POST    /drop/batches/<batch>/commit         {"files": {name: bytes}}: the batch into the inbox
    POST    /drop/batches/<batch>/retry          a failed batch of this device's, tried again

Every request carries the device's token (Authorization: Bearer); without one, with a wrong or a revoked one, the
answer is 401 whatever the path. The files go by tus 1.0.0 (core, creation, termination), so an upload cut off
anywhere goes on from the last byte that arrived. Nothing here reads a file back or lists what is staged: a device
is told about its own batches, their status and their names, and nothing else. What is checked about names, sizes
and room is ingest.drop's; this module is the HTTP around it.
"""
import base64
import errno
import json
import re
import socket
import time
from urllib.parse import quote

from .. import ingest
from ..store import sessions


TUS = "1.0.0"
STALL = 60                 # seconds a sender may stay silent in the middle of a request before it is let go
DRAIN = 2 << 20            # bytes of a refused request's body read and thrown away, so the answer arrives
DRAIN_WAIT = 2             # seconds that may take: a sender in no hurry, or with nothing more to send, is answered anyway
MAX_JSON = 1 << 20         # bytes a commit's manifest may have


class _Answer(Exception):
    """An answer decided while reading the request: (status, message, extra headers)."""
    def __init__(self, status: int, msg: str, headers: dict | None = None):
        super().__init__(msg)
        self.status, self.headers = status, headers or {}


class _Body:
    """The request's body as the staging takes it. What is read is counted off, so that however a piece ends (whole,
    cut off, or on a disk that filled up under it) the handler knows what is still on the connection."""
    def __init__(self, h):
        self.h = h

    def read1(self, n: int) -> bytes:
        got = self.h.rfile.read1(min(n, self.h._drop_unread))
        self.h._drop_unread -= len(got)
        return got


def _drain(h) -> None:
    """What is left of a request's body, read and thrown away before an answer that does not want it: closing on
    unread bytes resets the connection under the client, answer and all. Within bounds, DRAIN bytes and DRAIN_WAIT
    seconds: whoever feeds a long body a byte at a time, token or none, holds no thread with it."""
    left, until = h._drop_unread, time.monotonic() + DRAIN_WAIT
    try:
        while left and h._drop_drain > 0:
            wait = until - time.monotonic()
            if wait <= 0:
                break
            h.connection.settimeout(wait)
            got = h.rfile.read1(min(1 << 16, left, h._drop_drain))
            if not got:
                break
            left -= len(got)
            h._drop_drain -= len(got)
    except OSError:
        pass
    finally:
        try:
            h.connection.settimeout(STALL)     # the answer itself is written with the usual patience
        except OSError:
            pass


def _send(h, status: int, obj=None, headers: dict | None = None) -> None:
    """One answer: JSON when there is something to say, else no body. A request whose body was not read to its end,
    or whose end cannot be told, ends the connection with the answer: those bytes must never be taken for the next
    request."""
    if h._drop_unread:
        _drain(h)
    body = json.dumps(obj, ensure_ascii=False).encode() if obj is not None else b""
    try:
        h.send_response(status)
        h.send_header("Tus-Resumable", TUS)
        h.send_header("Cache-Control", "no-store")
        for k, v in (headers or {}).items():
            h.send_header(k, v)
        if h._drop_unread or h._drop_unframed:
            h.send_header("Connection", "close")
        if body:
            h.send_header("Content-Type", "application/json; charset=utf-8")
        h.send_header("Content-Length", str(len(body)))
        h.end_headers()
        if body and h.command != "HEAD":
            h.wfile.write(body)
    except OSError:                            # the device is gone: nobody to tell
        h.close_connection = True


def _bytes(value) -> int | None:
    """A header's count of bytes, or None when it is not one."""
    return int(value) if value is not None and re.fullmatch(r"[0-9]{1,19}", value.strip()) else None


def _number(h, name: str) -> int:
    n = _bytes(h.headers.get(name))
    if n is None:
        raise _Answer(400, f"{name}: a whole number of bytes")
    return n


def _filename(h) -> str:
    """The file's name from Upload-Metadata (`key base64value` pairs, comma-separated)."""
    for pair in (h.headers.get("Upload-Metadata") or "").split(","):
        key, _, value = pair.strip().partition(" ")
        if key == "filename":
            try:
                return base64.b64decode(value.strip(), validate=True).decode()
            except ValueError:
                raise _Answer(400, "Upload-Metadata: filename is not base64 of UTF-8 text") from None
    raise _Answer(400, "Upload-Metadata: no filename")


def _tus(h) -> None:
    if h.headers.get("Tus-Resumable") != TUS:
        raise _Answer(412, f"this server speaks tus {TUS}", {"Tus-Version": TUS})


def _json_body(h):
    n = h._drop_unread
    if n > MAX_JSON:
        raise _Answer(413, f"a manifest has at most {MAX_JSON} bytes")
    raw = h.rfile.read(n)
    h._drop_unread = 0
    try:
        return json.loads(raw)
    except (ValueError, RecursionError):           # not JSON, or brackets nested without end
        raise _Answer(400, 'a commit carries the manifest: {"files": {name: bytes}}') from None


def _route(h, device: dict, parts: list[str]) -> None:
    staging, method = ingest.drop, h.command
    if h._drop_unframed:
        if h.headers.get("Transfer-Encoding"):
            raise _Answer(411, "Content-Length: say how long the body is (a chunked one is not taken)")
        raise _Answer(400, "Content-Length: a whole number of bytes")
    if method == "OPTIONS":
        return _send(h, 204, headers={"Tus-Version": TUS, "Tus-Extension": "creation,termination", "Tus-Max-Size": str(staging.MAX_FILE)})
    if parts in ([], ["device"]) and method == "GET":
        return _send(h, 200, {**device, **staging.usage()})
    if parts == ["events"] and method == "GET":
        return _send(h, 200, {"events": staging.events()})
    if parts == ["batches"] and method == "GET":
        return _send(h, 200, staging.batches_of(device["id"]))
    if len(parts) == 3 and parts[0] == "batches" and method == "POST":
        batch = parts[1]
        if parts[2] == "files":                                         # tus creation
            _tus(h)
            if h.headers.get("Upload-Length") is None:
                raise _Answer(400, "Upload-Length: say how long the file is (a length given later is not taken)")
            name = _filename(h)
            here = staging.create(device["id"], batch, name, _number(h, "Upload-Length"))
            return _send(h, 201, headers={"Location": f"/drop/batches/{quote(batch, safe='')}/files/{quote(name, safe='')}", "Upload-Offset": str(here)})
        if parts[2] == "commit":
            return _send(h, 200, staging.commit(device["id"], batch, _json_body(h)))
        if parts[2] == "retry":
            return _send(h, 200, staging.retry(device["id"], batch))
    if len(parts) == 4 and parts[0] == "batches" and parts[2] == "files":
        batch, name = parts[1], parts[3]
        if method == "HEAD":
            _tus(h)
            here, length = staging.offset(device["id"], batch, name)
            return _send(h, 200, headers={"Upload-Offset": str(here), "Upload-Length": str(length)})
        if method == "PATCH":
            _tus(h)
            if h.headers.get("Content-Type") != "application/offset+octet-stream":
                raise _Answer(415, "a piece is sent as application/offset+octet-stream")
            at = _number(h, "Upload-Offset")
            if h.headers.get("Content-Length") is None:
                raise _Answer(411, "Content-Length: say how long the piece is")
            n = _number(h, "Content-Length")
            here = staging.append(device["id"], batch, name, at, _Body(h), n, stop=lambda: h.connection.shutdown(socket.SHUT_RDWR))
            return _send(h, 204, headers={"Upload-Offset": str(here)})     # short of the piece: the sender closed, the rest is not coming
        if method == "DELETE":
            _tus(h)
            staging.terminate(device["id"], batch, name)
            return _send(h, 204)
        if method == "GET":
            raise _Answer(405, "a staged file is written and asked about, not read back", {"Allow": "HEAD, PATCH, DELETE"})
    raise _Answer(404, "not found")


def handle(h, parts: list[str]) -> None:
    """One request under /drop/ (`parts`: the path after it), answered on the server's handler `h`."""
    staging = ingest.drop
    # what of the request's body is still unread, and how much of that may be read and thrown away before an answer
    length = h.headers.get("Content-Length")
    h._drop_unread, h._drop_drain = _bytes(length) or 0, DRAIN
    # a body whose end cannot be told (chunked, or a length that is no number): refused, and the connection ends with the answer
    h._drop_unframed = bool(h.headers.get("Transfer-Encoding")) or (length is not None and _bytes(length) is None)
    before = h.connection.gettimeout()
    h.connection.settimeout(STALL)
    try:
        auth = (h.headers.get("Authorization") or "").split(" ", 1)
        device = sessions.store().device_for(auth[1].strip()) if len(auth) == 2 and auth[0].lower() == "bearer" else None
        if device is None:
            return _send(h, 401, {"error": "this door takes a paired device's token (Authorization: Bearer ...)"}, {"WWW-Authenticate": "Bearer"})
        _route(h, device, parts)
    except _Answer as e:
        _send(h, e.status, {"error": str(e)}, e.headers)
    except FileNotFoundError as e:                  # the drop's own say what is not here; the system's would say where the server keeps it
        _send(h, 404, {"error": str(e) if e.errno is None else "not found"})
    except staging.Conflict as e:
        _send(h, 409, {"error": str(e)})
    except staging.TooLarge as e:
        _send(h, 413, {"error": str(e)})
    except staging.Busy as e:
        _send(h, 423, {"error": str(e)})
    except staging.Full as e:
        _send(h, 507, {"error": str(e)})
    except ValueError as e:
        _send(h, 400, {"error": str(e)})
    except TimeoutError:                            # a silent sender: what came is kept, the rest is not waited for
        _send(h, 408, {"error": f"nothing came for {STALL} s"})
    except ConnectionError:                         # the sender broke off: what came is kept, and there is nobody to answer
        h.close_connection = True
    except Exception as e:  # noqa: BLE001
        full = isinstance(e, OSError) and e.errno == errno.ENOSPC
        if not full:                                # what went wrong inside is for the log: the device is told that it did
            h.log_error("drop: %s: %s", type(e).__name__, e)
        _send(h, 507 if full else 500, {"error": "no room on the server's disk" if full else "the server failed; its log says why"})
    finally:
        try:
            h.connection.settimeout(before)
        except OSError:
            pass
