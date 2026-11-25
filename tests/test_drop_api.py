"""The drop's routes (#75) as a device uses them: a real `acroscope serve` child on a scratch data dir, a device
paired through the API, and the small client in tests/drop_client.py speaking the protocol; then ingest on what
arrived. The staging rules themselves are in tests/test_drop.py."""
import base64
import errno
import http.client
import io
import json
import os
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from acroscope import ingest, server
from acroscope.ingest import drop
from acroscope.server import drop as routes
from acroscope.store import sessions
from acroscope.store.db import Db

try:
    from tests.drop_client import DropClient, upload_url
    from tests.test_ingest import make_clip
except ImportError:
    from drop_client import DropClient, upload_url
    from test_ingest import make_clip


LOG = b"H Product:Blackbox flight data recorder by Nicholas Sherlock\n" + bytes(range(256)) * 4
CLIP = b"\x00\x00\x00\x14ftypqt  " + bytes(range(256)) * 40
QUOTA = 20_000_000
_n = [0]


def batch_id() -> str:
    """A fresh batch id per call, in the order they were made."""
    _n[0] += 1
    return f"20261011-1000{_n[0] // 36:02d}-t{_n[0]:03d}"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class DropApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.tmp.name)
        cls.data = cls.root / "data"
        (cls.data / "videos").mkdir(parents=True)
        cls.port = free_port()
        cls.env = dict(os.environ, ACROSCOPE_DB=str(cls.root / "t.db"), ACROSCOPE_DATA=str(cls.data), ACROSCOPE_CACHE=str(cls.root / "cache"),
                       ACROSCOPE_URL="", ACROSCOPE_DROP_QUOTA=str(QUOTA), ACROSCOPE_DROP_MIN_FREE="0", ACROSCOPE_FONTS=str(cls.root / "fonts"),
                       ACROSCOPE_DROP_URL="https://drop.example.org/")
        (cls.root / "fonts").mkdir()
        cls.proc = subprocess.Popen([sys.executable, "-m", "acroscope.cli", "serve", "--host", "127.0.0.1", "--port", str(cls.port), "--no-warm"],
                                    env=cls.env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        cls.url = f"http://127.0.0.1:{cls.port}"
        for _ in range(100):
            try:
                urllib.request.urlopen(cls.url + "/api/store", timeout=1).read()
                break
            except Exception:  # noqa: BLE001
                time.sleep(0.1)
        else:
            raise RuntimeError("server did not come up")
        cls.db = Db(cls.root / "t.db")
        cls.dev = cls.pair("field phone")
        cls.c = DropClient(cls.url, cls.dev["token"])

    @classmethod
    def tearDownClass(cls):
        cls.proc.terminate()
        cls.proc.wait(5)
        cls.tmp.cleanup()

    @classmethod
    def pair(cls, name: str) -> dict:
        req = urllib.request.Request(cls.url + "/api/devices", data=json.dumps({"name": name}).encode(), method="POST")
        return json.loads(urllib.request.urlopen(req).read())

    def api(self, method: str, path: str, body=None) -> tuple[int, object]:
        return DropClient(self.url, None).json(method, path, body)

    def cli(self, *args):
        return subprocess.run([sys.executable, "-m", "acroscope.cli", *args], env=self.env, capture_output=True, text=True)

    def staged(self, dev: dict, batch: str) -> Path:
        return self.data / "drop" / dev["id"] / batch / "files"

    # ---- the token ---------------------------------------------------------------------------------------------------

    def test_every_route_wants_the_devices_token(self):
        """Without a token, with a wrong one, with a revoked one, with the token anywhere but in the header: 401,
        whatever the path, and nothing happens."""
        b = batch_id()
        up = self.c.create(b, "VID00001.mov", 10)
        gone = self.pair("lost phone")
        self.assertEqual(self.api("DELETE", f"/api/devices/{gone['id']}"), (200, {"revoked": True}))
        routes_ = [("GET", "/drop/device"), ("GET", "/drop/events"), ("GET", "/drop/batches"), ("OPTIONS", "/drop/batches"),
                   ("POST", f"/drop/batches/{b}/files"), ("HEAD", up), ("PATCH", up), ("DELETE", up), ("GET", up),
                   ("POST", f"/drop/batches/{b}/commit"), ("POST", f"/drop/batches/{b}/retry"), ("GET", "/drop/nothing/here"), ("GET", "/drop")]
        for who in (None, "", "wrong", self.dev["token"] + "x", self.dev["id"], gone["token"]):
            for method, path in routes_:
                status, h, body = DropClient(self.url, who).request(method, path, b"0123456789" if method in ("PATCH", "POST") else None,
                                                                    {"Upload-Offset": "0", "Content-Type": "application/offset+octet-stream", "Upload-Length": "10"})
                self.assertEqual(status, 401, (who, method, path))
                self.assertEqual(h.get("www-authenticate"), "Bearer", (who, method, path))
                if method != "HEAD":
                    self.assertEqual(json.loads(body), {"error": "this door takes a paired device's token (Authorization: Bearer ...)"})
        for path in (f"/drop/device?token={self.dev['token']}", f"/drop/device?access_token={self.dev['token']}"):
            self.assertEqual(DropClient(self.url, None).request("GET", path)[0], 401)
        status, _, _ = DropClient(self.url, None).request("GET", "/drop/device", headers={"Authorization": f"Basic {self.dev['token']}"})
        self.assertEqual(status, 401)
        self.assertEqual(self.c.offset(up), 0)                              # nothing was written by any of them
        self.assertFalse((self.data / "inbox" / b).exists())
        self.assertEqual(self.c.request("GET", "/drop/device")[0], 200)      # and the device itself is still in
        self.assertEqual(self.api("GET", "/api/inbox")[0], 200)              # the rest of the API is as it was: no token

    def test_dots_in_a_path_lead_nowhere(self):
        """A reverse proxy that lets /drop/ past its login matches on the path with its dots resolved, and sends the
        path on as it was written: /api/sessions/../../drop/x would pass as the drop's and be served as the session
        list. So a path with a . or .. segment, written plainly or percent-encoded, is nowhere, whatever it names.
        The dots are looked for in the path as it was sent: behind a ; or a # a URL parser would cut off as a
        parameter or a fragment, the proxy still sees them."""
        sessions_ = self.api("GET", "/api/sessions")
        self.assertEqual(sessions_[0], 200)
        paired = self.paired()
        for who in (None, self.dev["token"]):
            c = DropClient(self.url, who)
            for method, path in (("GET", "/api/sessions/../../drop/device"), ("GET", "/api/sessions/%2e%2e/%2E%2E/drop/device"),
                                 ("GET", "/api/inbox/./../../drop/x"), ("GET", "/drop/../api/sessions"), ("GET", "/drop/%2e%2e/api/sessions"),
                                 ("GET", "/./drop/device"), ("GET", "/video/../drop/x.mp4"), ("HEAD", "/api/sessions/../../drop/device"),
                                 ("GET", "/api/sessions/x%2F..%2F..%2F..%2Fdrop%2Fy"), ("GET", "/api/sessions/x%2f%2e%2e%2f..%2F..%2Fdrop"),
                                 ("GET", "/api/sessions%2F..%2F..%2Fdrop%2Fy"), ("GET", "/api/inbox/x%2F."),
                                 ("POST", "/api/scan/../../drop/x"), ("DELETE", "/api/tags/../../drop/x"), ("PATCH", "/api/x/../../drop/x"),
                                 ("OPTIONS", "/api/x/../../drop/x"),
                                 ("GET", "/api/sessions;%2F..%2F..%2Fdrop%2Fy"), ("GET", "/api/sessions;/../../drop/y"), ("GET", "/api/sessions;x=1/../../drop/y"),
                                 ("POST", "/api/devices;%2F..%2F..%2Fdrop%2Fy"), ("DELETE", "/api/tags/x;%2F..%2F..%2F..%2Fdrop"),
                                 ("GET", "/api/sessions#/../../drop/y"), ("GET", "/api/sessions%3F/../../drop/y"), ("GET", "/drop/device;/../../api/sessions")):
                status, h, body = c.request(method, path, b"{}" if method in ("POST", "PATCH") else None)
                self.assertEqual((status, h.get("connection")), (404, "close"), (who, method, path))
                self.assertEqual(body, b"" if method == "HEAD" else b'{"error": "not found"}', (who, method, path))
        self.assertEqual(self.api("GET", "/api/sessions"), sessions_)          # a plain path is served as ever
        self.assertEqual(self.paired(), paired)                                # and nobody was paired by a path that led nowhere
        self.assertEqual(self.c.request("GET", "/drop/device")[0], 200)
        status, h, body = DropClient(self.url, None).request("GET", "/api/session/..hidden")
        self.assertEqual((status, h.get("connection")), (404, None))           # dots inside a name are a name: one that is not there,
        self.assertIn("..hidden", json.loads(body)["error"])                   # as the route itself says
        self.assertEqual(self.api("GET", "/api/sessions?from=/../x"), sessions_)   # and the query is no path
        self.assertEqual(self.api("DELETE", "/api/tags/deg%2Fs"), (200, {"removed": False}))   # and a slash inside one is a name too
        self.assertEqual(self.api("GET", "/api/sessions/%252e%252e/x")[0], 200)   # encoded twice: not dots to anyone

    def paired(self) -> list:
        return [(d["id"], d["revoked_at"]) for d in self.api("GET", "/api/devices")[1]["devices"]]

    def test_the_drops_own_host_serves_the_drop_and_nothing_else(self):
        """Where the drop has a host of its own (`ACROSCOPE_DROP_URL`), a request that arrives under that host's name
        gets the drop and nothing else, however its path is spelled: the proxy in front and this server then need not
        read a path alike for that host to be no way to the API."""
        spare = self.pair("a phone nobody uses")                               # one a slip here may revoke
        paired = self.paired()
        nobody = DropClient(self.url, None)
        # the name as a proxy reads a Host to pick its site: whatever the case, the port, a final dot, and brackets
        # round it, which a proxy takes off unseen
        names = [{"Host": host} for host in ("drop.example.org", "DROP.example.org", "drop.example.org:443", "drop.example.org.", "drop.example.org.:8070",
                                             "drop.example.org:x", "[drop.example.org]", "[drop.example.org]:443", "[drop.example.org", "drop.example.org]",
                                             " drop.example.org")]
        # and where the proxy sends its own Host on, the name it was asked under comes beside it
        names += [{"Host": "upstream:8070", "X-Forwarded-Host": "drop.example.org"}, {"Host": "upstream:8070", "X-Forwarded-Host": "edge.example.org, Drop.Example.Org:443"}]
        for hdrs in names:
            for method, path in (("GET", "/api/sessions"), ("HEAD", "/api/sessions"), ("GET", "/"), ("GET", "/config"), ("GET", "/api/devices"),
                                 ("POST", "/api/devices"), ("DELETE", f"/api/devices/{spare['id']}"), ("GET", "/video/x/y.mp4"),
                                 ("PATCH", "/api/x"), ("OPTIONS", "/api/x"), ("GET", "/dropped"), ("GET", "/Drop/device")):
                status, h, body = nobody.request(method, path, b'{"name": "x"}' if method == "POST" else None, hdrs)
                self.assertEqual((status, h.get("connection")), (404, "close"), (hdrs, method, path))
                self.assertEqual(body, b"" if method == "HEAD" else b'{"error": "not found"}', (hdrs, method, path))
            self.assertEqual(self.c.request("GET", "/drop/device", headers=hdrs)[0], 200, hdrs)     # the drop itself,
            self.assertEqual(nobody.request("GET", "/drop/device", headers=hdrs)[0], 401, hdrs)     # which asks for the token
        self.assertEqual(self.paired(), paired)                                # nobody was paired or revoked through it
        # a request target in absolute form names the host it is for, whatever the Host says
        answer = self.raw(b"GET http://drop.example.org/api/sessions HTTP/1.1\r\nHost: player.example.org\r\n\r\n")
        self.assertEqual(answer[:12], b"HTTP/1.1 404", answer[:200])
        for hdrs in ({"Host": "player.example.org"}, {"Host": "drop.example.org.evil.example"}, {"Host": "xdrop.example.org"}, {"Host": ""}, {"Host": "[::1"},
                     {"Host": "[::1]:8070"}, {"Host": "player.example.org", "X-Forwarded-Host": "player.example.org"}):
            self.assertEqual(nobody.request("GET", "/api/sessions", headers=hdrs)[0], 200, hdrs)   # under any other name, or none, the server is as it was

    def test_the_device_asks_who_it_is(self):
        status, me = self.c.json("GET", "/drop/device")
        self.assertEqual(status, 200)
        self.assertEqual((me["id"], me["name"], me["quota"], me["max_file"]), (self.dev["id"], "field phone", QUOTA, 4 << 30))
        self.assertIsInstance(me["staged"], int)
        self.assertEqual(self.c.json("GET", "/drop")[1]["id"], self.dev["id"])

    # ---- a batch, as the app sends it ----------------------------------------------------------------------------------

    def test_a_batch_goes_up_and_lands_in_the_inbox(self):
        b = batch_id()
        sidecar = json.dumps({"date": "2026-10-11", "session": "park", "imported_at": "2026-10-11T18:30:00+02:00", "host": "field phone"}).encode()
        name = "BTFL_BLACKBOX_LOG_OTTO_20261011_183000_BETAFPVG473_V2.bbl"
        # the first request of each kind, header by header
        status, h, body = self.c.request("POST", f"/drop/batches/{b}/files",
                                         headers={"Upload-Length": str(len(CLIP)), "Upload-Metadata": "filename " + base64.b64encode(b"VID00001.mov").decode()})
        self.assertEqual((status, body), (201, b""))
        self.assertEqual((h["location"], h["tus-resumable"], h["upload-offset"]), (f"/drop/batches/{b}/files/VID00001.mov", "1.0.0", "0"))
        up = h["location"]
        status, h, body = self.c.request("HEAD", up)
        self.assertEqual((status, body), (200, b""))
        self.assertEqual((h["upload-offset"], h["upload-length"], h["cache-control"], h["tus-resumable"]), ("0", str(len(CLIP)), "no-store", "1.0.0"))
        status, h, body = self.c.request("PATCH", up, CLIP[:4000], {"Upload-Offset": "0", "Content-Type": "application/offset+octet-stream"})
        self.assertEqual((status, body), (204, b""))
        self.assertEqual((h["upload-offset"], h["tus-resumable"]), ("4000", "1.0.0"))
        self.assertEqual(self.c.offset(up), 4000)
        self.assertEqual(self.c.upload(b, "VID00001.mov", CLIP, piece=3000), up)   # the same call again goes on from there
        self.assertEqual(self.c.offset(up), len(CLIP))
        self.c.upload(b, name, LOG)
        self.c.upload(b, "import.json", sidecar)
        self.assertEqual(self.staged(self.dev, b).joinpath("VID00001.mov").read_bytes(), CLIP)
        self.assertFalse((self.data / "inbox" / b).exists())                 # staged, not in the inbox
        self.assertNotIn(b, [x["id"] for x in self.api("GET", "/api/inbox")[1]])
        man = {"VID00001.mov": len(CLIP), name: len(LOG), "import.json": len(sidecar)}
        r = self.c.commit(b, man)
        self.assertEqual((r["id"], r["status"], r["clips"], r["logs"]), (b, "pending", 1, 1))
        d = self.data / "inbox" / b
        self.assertEqual(sorted(p.name for p in d.iterdir()), sorted([".done", *man]))
        self.assertEqual(json.loads((d / ".done").read_text()), {"files": man})
        self.assertEqual((d / "VID00001.mov").read_bytes(), CLIP)
        self.assertEqual((d / name).read_bytes(), LOG)
        self.assertEqual(json.loads((d / "import.json").read_text())["session"], "park")
        self.assertFalse((self.data / "drop" / self.dev["id"] / b).exists())
        row = next(x for x in self.api("GET", "/api/inbox")[1] if x["id"] == b)
        self.assertEqual((row["status"], row["device"]), ("pending", self.dev["id"]))   # the inbox says who handed it in
        self.assertEqual(self.c.commit(b, man)["id"], b)                     # a lost reply: asked again, answered again
        self.assertEqual(self.c.request("HEAD", up)[0], 404)                 # the upload is over
        status, out = self.c.json("POST", f"/drop/batches/{b}/files")       # and the id is spent
        self.assertEqual(status, 400)
        status, h, body = self.c.request("POST", f"/drop/batches/{b}/files", headers={"Upload-Length": "5", "Upload-Metadata": "filename " + base64.b64encode(b"VID00002.mov").decode()})
        self.assertEqual(status, 409)
        self.assertIn("already handed in", json.loads(body)["error"])

    def test_a_piece_cut_off_is_kept_and_resumed(self):
        """The phone left the network in the middle of a piece: the bytes that arrived are there, the next PATCH
        starts after them."""
        b = batch_id()
        up = self.c.create(b, "VID00001.mov", len(CLIP))
        s = socket.create_connection(("127.0.0.1", self.port))
        s.sendall((f"PATCH {up} HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer {self.dev['token']}\r\nTus-Resumable: 1.0.0\r\n"
                   f"Upload-Offset: 0\r\nContent-Type: application/offset+octet-stream\r\nContent-Length: {len(CLIP)}\r\n\r\n").encode() + CLIP[:3000])
        s.close()
        for _ in range(100):
            if self.c.request("HEAD", up)[1].get("upload-offset") == "3000" and self.c.request("PATCH", up, b"", {"Upload-Offset": "3000", "Content-Type": "application/offset+octet-stream"})[0] == 204:
                break
            time.sleep(0.05)
        self.assertEqual(self.c.offset(up), 3000)
        self.c.upload(b, "VID00001.mov", CLIP)
        self.assertEqual(self.staged(self.dev, b).joinpath("VID00001.mov").read_bytes(), CLIP)

    def test_the_protocol_is_held_to(self):
        b = batch_id()
        meta = "filename " + base64.b64encode(b"VID00001.mov").decode()
        up = upload_url(b, "VID00001.mov")
        octets = {"Content-Type": "application/offset+octet-stream"}
        # a client of another protocol version, or none: 412 and the version spoken here
        for hdr in ({"Tus-Resumable": "0.2.2"}, {"Tus-Resumable": ""}):
            status, h, _ = self.c.request("POST", f"/drop/batches/{b}/files", headers={"Upload-Length": "10", "Upload-Metadata": meta, **hdr})
            self.assertEqual((status, h.get("tus-version")), (412, "1.0.0"), hdr)
        status, h, _ = self.c.request("POST", f"/drop/batches/{b}/files", headers={"Upload-Length": "10", "Upload-Metadata": meta}, tus=False)
        self.assertEqual((status, h.get("tus-version")), (412, "1.0.0"))
        self.assertFalse(self.staged(self.dev, b).exists())
        # a creation says how long the file is and what it is called
        for hdrs, why in (({"Upload-Metadata": meta}, "Upload-Length"), ({"Upload-Length": "ten", "Upload-Metadata": meta}, "Upload-Length"),
                          ({"Upload-Length": "-1", "Upload-Metadata": meta}, "Upload-Length"), ({"Upload-Length": "1.5", "Upload-Metadata": meta}, "Upload-Length"),
                          ({"Upload-Defer-Length": "1", "Upload-Metadata": meta}, "Upload-Length"),
                          ({"Upload-Length": "10"}, "filename"), ({"Upload-Length": "10", "Upload-Metadata": "name " + meta.split()[1]}, "filename"),
                          ({"Upload-Length": "10", "Upload-Metadata": "filename not-base64!"}, "Upload-Metadata"),
                          ({"Upload-Length": "10", "Upload-Metadata": "filename " + base64.b64encode(b"\xff\xfe").decode()}, "Upload-Metadata"),
                          ({"Upload-Length": "10", "Upload-Metadata": "filename " + base64.b64encode(b"../VID00001.mov").decode()}, "plain"),
                          ({"Upload-Length": "10", "Upload-Metadata": "filename " + base64.b64encode(b"notes.txt").decode()}, "clips")):
            status, _, body = self.c.request("POST", f"/drop/batches/{b}/files", headers=hdrs)
            self.assertEqual(status, 400, hdrs)
            self.assertIn(why, json.loads(body)["error"], hdrs)
        self.assertFalse(self.staged(self.dev, b).exists())
        self.assertEqual(self.c.request("POST", "/drop/batches/b1/files", headers={"Upload-Length": "10", "Upload-Metadata": meta})[0], 400)   # not a batch id
        self.assertEqual(self.c.request("POST", f"/drop/batches/{b}/files", headers={"Upload-Length": str((4 << 30) + 1), "Upload-Metadata": meta})[0], 413)
        status, _, body = self.c.request("POST", f"/drop/batches/{b}/files", headers={"Upload-Length": str(QUOTA + 1), "Upload-Metadata": meta})
        self.assertEqual(status, 507)
        self.assertIn("full", json.loads(body)["error"])
        self.assertEqual(self.c.create(b, "VID00001.mov", 10), up)
        # a piece says where it goes, how long it is, and that it is bytes
        cases = [({"Upload-Offset": "0"}, b"0123456789", 415), ({"Upload-Offset": "0", "Content-Type": "application/json"}, b"0123456789", 415),
                 (octets, b"0123456789", 400), ({**octets, "Upload-Offset": "x"}, b"0123456789", 400), ({**octets, "Upload-Offset": "-0"}, b"0123456789", 400),
                 ({**octets, "Upload-Offset": "5"}, b"56789", 409), ({**octets, "Upload-Offset": "0"}, b"0123456789a", 413)]
        for hdrs, data, want in cases:
            status, h, body = self.c.request("PATCH", up, data, hdrs)
            self.assertEqual(status, want, hdrs)
            self.assertEqual(h.get("connection"), "close", hdrs)             # its bytes were not read: the connection ends with the answer
            self.assertTrue(json.loads(body)["error"], hdrs)
            self.assertEqual(self.c.offset(up), 0, hdrs)
        status, h, _ = self.c.request("PATCH", up, iter([b"01234", b"56789"]), {**octets, "Upload-Offset": "0"})   # chunked: no length given
        self.assertEqual((status, h.get("connection")), (411, "close"))
        self.assertEqual(self.c.offset(up), 0)
        self.assertEqual(self.c.patch(up, 0, b"0123456789"), 10)
        # a file is written and asked about, never read back
        status, h, body = self.c.request("GET", up)
        self.assertEqual((status, h.get("allow")), (405, "HEAD, PATCH, DELETE"))
        self.assertNotIn(b"0123456789", body)
        for method, path in (("GET", f"/drop/batches/{b}/files"), ("GET", f"/drop/batches/{b}"), ("PUT", up), ("GET", "/drop/nowhere"), ("POST", "/drop/device"),
                             ("DELETE", f"/drop/batches/{b}"), ("GET", f"/drop/batches/{b}/commit")):
            status, _, body = self.c.request(method, path)
            self.assertIn(status, (404, 405, 501), (method, path))
            self.assertNotIn(b"0123456789", body)
        # PATCH and OPTIONS exist under /drop/ only
        for method in ("PATCH", "OPTIONS"):
            status, h, _ = self.c.request(method, "/api/sessions", b"x" if method == "PATCH" else None)
            self.assertEqual((status, h.get("connection")), (501, "close"), method)
        self.assertEqual(self.api("GET", "/api/sessions")[0], 200)
        # what the server speaks, for a client that asks first
        status, h, body = self.c.request("OPTIONS", f"/drop/batches/{b}/files", tus=False)
        self.assertEqual((status, body), (204, b""))
        self.assertEqual((h["tus-version"], h["tus-extension"], h["tus-max-size"], h["tus-resumable"]), ("1.0.0", "creation,termination", str(4 << 30), "1.0.0"))

    def raw(self, request: bytes) -> bytes:
        """Bytes sent as they are on a connection of their own: everything that comes back until the server ends it."""
        with socket.create_connection(("127.0.0.1", self.port), timeout=10) as s:
            s.sendall(request)
            out = b""
            while got := s.recv(65536):
                out += got
            return out

    def test_a_body_whose_end_cannot_be_told_ends_the_connection(self):
        """A chunked body, or a length that is no number: nothing of it is read, the answer says so and the connection
        ends with it. Otherwise what was left of the body would be taken for the next request."""
        b = batch_id()
        self.c.push(b, {"VID00001.mov": CLIP, "import.json": b"{}"})         # handed in: a commit again would be answered 200
        up = self.c.create(batch_id(), "VID00001.mov", 100)
        auth = f"Host: x\r\nAuthorization: Bearer {self.dev['token']}\r\nTus-Resumable: 1.0.0\r\n".encode()
        manifest = json.dumps({"files": {"VID00001.mov": len(CLIP), "import.json": 2}}).encode()
        chunked = b"%x\r\n%s\r\n0\r\n\r\n" % (len(manifest), manifest)
        smuggled = b"1 GET /drop/device HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer " + self.dev["token"].encode() + b"\r\n\r\n"
        piece = b"64\r\n" + b"a" * 100 + b"\r\n0\r\n\r\n"
        for request, want in ((b"POST /drop/batches/%s/commit HTTP/1.1\r\n" % b.encode() + auth + b"Transfer-Encoding: chunked\r\n\r\n" + chunked, b"411"),
                              (b"POST /drop/batches/%s/commit HTTP/1.1\r\n" % b.encode() + auth + b"Transfer-Encoding: chunked\r\n\r\n" + smuggled, b"411"),
                              (b"POST /drop/batches/%s/commit HTTP/1.1\r\n" % b.encode() + auth + b"Content-Length: abc\r\n\r\n" + manifest + b"\r\n", b"400"),
                              (b"POST /drop/batches/%s/commit HTTP/1.1\r\n" % b.encode() + auth + b"Content-Length: +5\r\n\r\n" + manifest + b"\r\n", b"400"),
                              (b"PATCH " + up.encode() + b" HTTP/1.1\r\n" + auth + b"Upload-Offset: 0\r\nContent-Type: application/offset+octet-stream\r\n"
                               b"Content-Length: 100\r\nTransfer-Encoding: chunked\r\n\r\n" + piece, b"411"),
                              (b"GET /drop/device HTTP/1.1\r\n" + auth + b"Transfer-Encoding: chunked\r\n\r\n0\r\n\r\n", b"411"),
                              # a header given twice is read by its first here and by its last elsewhere
                              (b"POST /drop/batches/%s/commit HTTP/1.1\r\n" % b.encode() + auth + b"Content-Length: 2\r\nContent-Length: %d\r\n\r\n" % len(manifest) + manifest, b"400"),
                              (b"POST /drop/batches/%s/commit HTTP/1.1\r\n" % b.encode() + auth + b"Transfer-Encoding:\r\nTransfer-Encoding: chunked\r\n\r\n" + chunked, b"411"),
                              (b"GET /drop/device HTTP/1.1\r\nHost: x\r\nTransfer-Encoding: chunked\r\n\r\n0\r\n\r\n", b"401"),
                              # a header line the parser cannot read (a space before its colon): it and every one after
                              # it are gone from the headers, which the proxy would not have let through
                              (b"POST /drop/batches/%s/commit HTTP/1.1\r\n" % b.encode() + auth + b"Transfer-Encoding : chunked\r\n\r\n" + chunked, b"400"),
                              (b"POST /drop/batches/%s/commit HTTP/1.1\r\n" % b.encode() + auth + b"Content-Length: %d\xa0\r\n\r\n" % len(manifest) + manifest, b"400")):
            answer = self.raw(request)
            self.assertEqual(answer[:12], b"HTTP/1.1 " + want, request[:60])
            self.assertEqual(answer.count(b"HTTP/1."), 1, answer)            # one request, one answer: nothing of the body was read as another
            self.assertIn(b"Connection: close", answer.split(b"\r\n\r\n")[0], request[:60])
        self.assertEqual(self.c.offset(up), 0)                               # and no framing was taken for a file's bytes

    def test_what_a_tus_client_says_besides_the_name(self):
        """Upload-Metadata is a list of pairs: a tus library sends the file's type along, before or after its name."""
        b = batch_id()
        name, kind = base64.b64encode(b"VID00001.mov").decode(), base64.b64encode(b"video/quicktime").decode()
        for meta in (f"filetype {kind},filename {name}", f"filename {name},filetype {kind}", f"is_draft, filename {name} ,filetype {kind}"):
            status, h, _ = self.c.request("POST", f"/drop/batches/{b}/files", headers={"Upload-Length": "10", "Upload-Metadata": meta})
            self.assertEqual((status, h.get("location")), (201, upload_url(b, "VID00001.mov")), meta)

    def test_a_name_that_needs_escaping_goes_up_under_itself(self):
        """A log saved by hand is called what its pilot called it. The way to it is given percent-encoded and leads to
        that file, not to a path cut at a # or a name read out of a %41."""
        b = batch_id()
        for name in ("dump #2 100%.bbl", "VID%41 a?b.mov", "flight é.bbl"):
            up = self.c.create(b, name, 4)
            self.assertEqual(up, upload_url(b, name))
            self.assertNotIn(" ", up)
            self.assertEqual(self.c.offset(up), 0)
            self.assertEqual(self.c.patch(up, 0, b"abcd"), 4)
            self.assertEqual((self.staged(self.dev, b) / name).read_bytes(), b"abcd")
        self.assertEqual(sorted(p.name for p in self.staged(self.dev, b).iterdir()), ["VID%41 a?b.mov", "dump #2 100%.bbl", "flight é.bbl"])

    def test_the_patience_and_the_bounds(self):
        self.assertEqual((routes.STALL, drop.TAKEOVER, routes.DRAIN, routes.DRAIN_WAIT, routes.MAX_JSON), (60, 5, 2 << 20, 2, 1 << 20))

    def test_a_refused_piece_does_not_break_the_next_request(self):
        """One connection, kept alive: a piece that is refused ends it, so its unread bytes are never taken for the
        next request; a piece that is taken leaves it open."""
        b = batch_id()
        up = self.c.create(b, "VID00001.mov", 20)
        hdrs = {"Authorization": f"Bearer {self.dev['token']}", "Tus-Resumable": "1.0.0", "Content-Type": "application/offset+octet-stream"}
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        c.request("PATCH", up, b"0123456789", {**hdrs, "Upload-Offset": "0"})
        r = c.getresponse()
        r.read()
        self.assertEqual((r.status, r.getheader("Connection")), (204, None))
        c.request("HEAD", up, headers=hdrs)                                  # on the same connection
        r = c.getresponse()
        r.read()
        self.assertEqual((r.status, r.getheader("Upload-Offset")), (200, "10"))
        c.request("HEAD", upload_url(b, "VID00002.mov"), headers=hdrs)       # a HEAD that is refused has no body either
        r = c.getresponse()
        r.read()
        self.assertEqual(r.status, 404)
        c.request("HEAD", up, headers=hdrs)
        r = c.getresponse()
        r.read()
        self.assertEqual((r.status, r.getheader("Upload-Offset")), (200, "10"))
        c.request("PATCH", up, b"GET /api/sessions HTTP/1.1\r\n\r\n", {**hdrs, "Upload-Offset": "3"})   # refused, its body looking like a request
        r = c.getresponse()
        r.read()
        self.assertEqual((r.status, r.getheader("Connection")), (409, "close"))
        c.close()
        self.assertEqual(self.c.offset(up), 10)

    def test_a_file_taken_back(self):
        b = batch_id()
        up = self.c.upload(b, "VID00001.mov", CLIP)
        keep = self.c.upload(b, "VID00002.mov", CLIP)
        status, h, body = self.c.request("DELETE", up)
        self.assertEqual((status, body, h["tus-resumable"]), (204, b"", "1.0.0"))
        self.assertEqual(self.c.request("HEAD", up)[0], 404)
        self.assertEqual(self.c.request("DELETE", up)[0], 404)
        self.assertEqual(self.c.request("PATCH", up, b"x", {"Upload-Offset": "0", "Content-Type": "application/offset+octet-stream"})[0], 404)
        self.assertEqual(self.c.offset(keep), len(CLIP))
        self.assertEqual(sorted(p.name for p in self.staged(self.dev, b).iterdir()), ["VID00002.mov"])

    def test_a_commit_that_is_not_one(self):
        b = batch_id()
        self.c.upload(b, "VID00001.mov", CLIP)
        for body, want in ((b"{not json", 400), (b"[]", 400), (b"", 400), (json.dumps({"files": {"VID00001.mov": 5}}).encode(), 400),
                           (json.dumps({"files": {"VID00009.mov": 5}}).encode(), 400), (b" " * ((1 << 20) + 1), 413)):
            status, _, out = self.c.request("POST", f"/drop/batches/{b}/commit", body, {"Content-Type": "application/json"})
            self.assertEqual(status, want, body[:40])
            self.assertTrue(json.loads(out)["error"])
        self.assertFalse((self.data / "inbox" / b).exists())
        self.assertEqual(self.c.json("POST", f"/drop/batches/{batch_id()}/commit", {"files": {"VID00001.mov": 5}})[0], 404)   # nothing staged
        self.assertEqual(self.c.commit(b, {"VID00001.mov": len(CLIP)})["status"], "pending")

    # ---- what a device sees ----------------------------------------------------------------------------------------------

    def test_one_device_is_not_another(self):
        other = self.pair("second phone")
        o = DropClient(self.url, other["token"])
        b, b2 = batch_id(), batch_id()
        up = self.c.upload(b, "VID00001.mov", CLIP)
        octets = {"Upload-Offset": str(len(CLIP)), "Content-Type": "application/offset+octet-stream"}
        self.assertEqual([o.request("HEAD", up)[0], o.request("PATCH", up, b"", octets)[0], o.request("DELETE", up)[0]], [404, 404, 404])
        self.assertEqual(o.json("POST", f"/drop/batches/{b}/commit", {"files": {"VID00001.mov": len(CLIP)}})[0], 404)
        self.assertEqual(self.c.offset(up), len(CLIP))
        self.c.commit(b, {"VID00001.mov": len(CLIP)})
        o.push(b2, {"x.bbl": LOG})
        self.assertIn(b, [x["id"] for x in self.c.json("GET", "/drop/batches")[1]])
        self.assertNotIn(b2, [x["id"] for x in self.c.json("GET", "/drop/batches")[1]])
        self.assertEqual([x["id"] for x in o.json("GET", "/drop/batches")[1]], [b2])
        self.db.set_batch(b, status="failed", error="ValueError: boom")
        self.assertEqual(o.json("POST", f"/drop/batches/{b}/retry")[0], 404)   # not its batch to retry
        self.assertEqual(self.db.batch(b)["status"], "failed")
        self.assertEqual(o.json("POST", f"/drop/batches/{b}/commit", {"files": {"VID00001.mov": len(CLIP)}})[0], 404)   # nor to hear about

    def test_status_and_retry(self):
        b = batch_id()
        self.c.push(b, {"VID00001.mov": CLIP})
        mine = {x["id"]: x for x in self.c.json("GET", "/drop/batches")[1]}
        self.assertEqual((mine[b]["status"], mine[b]["clips"], mine[b]["logs"]), ("pending", 1, 0))
        self.assertNotIn("device", mine[b])
        status, out = self.c.json("POST", f"/drop/batches/{b}/retry")
        self.assertEqual(status, 409)                                        # pending: nothing failed
        self.db.set_batch(b, status="failed", error="ValueError: VID00001.mov: no video stream")
        self.assertEqual({x["id"]: x for x in self.c.json("GET", "/drop/batches")[1]}[b]["error"], "ValueError: VID00001.mov: no video stream")
        status, out = self.c.json("POST", f"/drop/batches/{b}/retry")
        self.assertEqual((status, out["id"], out["status"], out["error"]), (200, b, "pending", None))
        self.assertEqual(self.db.batch(b)["status"], "pending")

    def test_recent_events(self):
        for name in ("2026-10-11-park", "2026-10-12-home", "2026-10-13-park", "2026-10-14-unsorted"):
            self.db.ensure_session(name, name[:10])
        self.assertEqual(self.c.json("GET", "/drop/events"), (200, {"events": ["park", "home"]}))

    # ---- devices -------------------------------------------------------------------------------------------------------

    def test_devices_are_paired_and_revoked_through_the_api(self):
        status, d = self.api("POST", "/api/devices", {"name": "  goggles phone "})
        self.assertEqual((status, d["name"], set(d)), (200, "goggles phone", {"id", "name", "token", "created_at"}))
        status, out = self.api("GET", "/api/devices")
        self.assertEqual(out["url"], "https://drop.example.org")                # where the drop is reached, for the pairing code
        row = next(x for x in out["devices"] if x["id"] == d["id"])
        self.assertEqual(row, {"id": d["id"], "name": "goggles phone", "created_at": d["created_at"], "last_seen_at": None, "revoked_at": None})
        self.assertNotIn(d["token"], json.dumps(out))                         # the token was said once
        for bad in ({}, {"name": ""}, {"name": 5}, {"name": "x" * 61}, []):
            self.assertEqual(self.api("POST", "/api/devices", bad)[0], 400, bad)
        g = DropClient(self.url, d["token"])
        b = batch_id()
        up = g.upload(b, "VID00001.mov", CLIP)
        self.assertTrue(next(x for x in self.api("GET", "/api/devices")[1]["devices"] if x["id"] == d["id"])["last_seen_at"])
        self.assertEqual(self.api("DELETE", f"/api/devices/{d['id']}"), (200, {"revoked": True}))
        self.assertEqual(g.request("HEAD", up)[0], 401)
        self.assertFalse((self.data / "drop" / d["id"]).exists())             # what it had staged went with it
        self.assertEqual(self.api("DELETE", f"/api/devices/{d['id']}"), (200, {"revoked": False}))
        self.assertEqual(self.api("DELETE", "/api/devices/d0000000000"), (200, {"revoked": False}))
        for bad in ("videos", "D0123456789", d["id"] + "0", "d012345678%2Fx"):
            self.assertEqual(self.api("DELETE", f"/api/devices/{bad}")[0], 400, bad)   # not a device's id: nothing is looked for
        self.assertEqual(self.api("DELETE", "/api/devices/..%2F..%2Fvideos")[0], 404)
        self.assertTrue((self.data / "videos").is_dir())
        self.assertTrue(next(x for x in self.api("GET", "/api/devices")[1]["devices"] if x["id"] == d["id"])["revoked_at"])

    def test_the_cli_pairs_and_revokes(self):
        """`acroscope devices` on the server's own database, and from another machine as a client of the server."""
        for env in (self.env, dict(self.env, ACROSCOPE_URL=self.url)):
            run = lambda *args: subprocess.run([sys.executable, "-m", "acroscope.cli", "devices", *args], env=env, capture_output=True, text=True)
            r = run("add", "cli phone")
            self.assertEqual(r.returncode, 0, r.stderr)
            d = json.loads(r.stdout)
            self.assertEqual((d["name"], set(d)), ("cli phone", {"id", "name", "token", "created_at"}))
            self.assertIn("once", r.stderr)
            c = DropClient(self.url, d["token"])
            self.assertEqual(c.json("GET", "/drop/device")[1]["id"], d["id"])
            b = batch_id()
            c.upload(b, "VID00001.mov", CLIP)
            listed = run()
            line = next(x for x in listed.stdout.splitlines() if d["id"] in x)
            self.assertRegex(line, rf"{d['id']}\s+paired\s+cli phone  \(since \d{{4}}-\d\d-\d\d, last seen \d{{4}}-\d\d-\d\d\)")
            self.assertNotIn(d["token"], listed.stdout)
            rows = json.loads(run("--json").stdout)
            self.assertEqual([set(x) for x in rows if x["id"] == d["id"]], [{"id", "name", "created_at", "last_seen_at", "revoked_at"}])
            r = run("revoke", d["id"])
            self.assertEqual((r.returncode, json.loads(r.stdout)), (0, {"revoked": True}))
            self.assertEqual(c.request("GET", "/drop/device")[0], 401)
            self.assertFalse((self.data / "drop" / d["id"]).exists())          # what it had staged is gone
            self.assertEqual(json.loads(run("revoke", d["id"]).stdout), {"revoked": False})
            self.assertIn("revoked", next(x for x in run().stdout.splitlines() if d["id"] in x))
            for args, why in ((("revoke", "../videos"), "not a device"), (("revoke",), "devices revoke <id>"), (("add",), "devices add <name>"), (("add", ""), "name")):
                r = run(*args)
                self.assertNotEqual(r.returncode, 0, args)
                self.assertIn(why, r.stderr, args)
            self.assertTrue((self.data / "videos").is_dir())

    def test_housekeeping_drops_what_nobody_finished(self):
        b, fresh = batch_id(), batch_id()
        up, keep = self.c.create(b, "VID00001.mov", 10), self.c.create(fresh, "VID00001.mov", 10)
        old = time.time() - 8 * 86400
        stale = self.data / "drop" / self.dev["id"] / b
        for p in (stale, *stale.rglob("*")):
            os.utime(p, (old, old))
        r = self.cli("housekeeping")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout)["staged_dropped"], [f"{self.dev['id']}/{b}"])
        self.assertIn(b, r.stderr)
        self.assertEqual((self.c.request("HEAD", up)[0], self.c.request("HEAD", keep)[0]), (404, 200))

    @unittest.skipIf(os.geteuid() == 0, "root reads a folder whatever its mode")
    def test_housekeeping_backs_the_database_up_before_it_looks_at_the_drop(self):
        """The drop's upkeep is the newest step of the nightly run and reads what devices left behind. Should it ever
        fail, the database was backed up first."""
        state = self.data / "state"
        for old in state.glob("acroscope-*.db") if state.is_dir() else ():
            old.unlink()
        locked = self.data / "drop" / "d0000000000"
        locked.mkdir(parents=True)
        locked.chmod(0)
        self.addCleanup(lambda: (locked.chmod(0o755), locked.rmdir()))
        r = self.cli("housekeeping")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("PermissionError", r.stderr)
        self.assertEqual(len(list(state.glob("acroscope-*.db"))), 1)

    # ---- and then ingest ---------------------------------------------------------------------------------------------------

    def test_what_a_device_sends_becomes_a_session(self):
        """End to end: a clip as the DVR writes one and the sidecar through the drop, then ingest, as on a pushed
        batch; the device then hears that its batch is done and what it became."""
        b = batch_id()
        clip = self.root / "VID00007.mov"
        make_clip(clip)
        sidecar = json.dumps({"date": "2026-10-21", "session": "whoop race", "imported_at": "2026-10-21T19:02:11+02:00", "host": "field phone"}).encode()
        self.c.push(b, {"import.json": sidecar, "VID00007.mov": clip.read_bytes()}, piece=100_000)
        r = self.cli("ingest", b)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout)["session"], "2026-10-21-whoop-race")
        s = self.db.load("2026-10-21-whoop-race")
        self.assertEqual([v["name"] for v in s["videos"]], ["2026-10-21_001"])
        self.assertTrue((self.data / "videos" / "2026-10-21-whoop-race" / s["videos"][0]["file"]).is_file())
        self.assertFalse((self.data / "inbox" / b).exists())                 # consumed
        mine = {x["id"]: x for x in self.c.json("GET", "/drop/batches")[1]}[b]
        self.assertEqual((mine["status"], mine["session"], mine["result"]), ("done", "2026-10-21-whoop-race", {"clips": ["2026-10-21_001"], "logs": []}))
        self.assertIn("whoop-race", self.c.json("GET", "/drop/events")[1]["events"])
        r = self.cli("inbox", "--json")
        self.assertEqual(next(x for x in json.loads(r.stdout) if x["id"] == b)["device"], self.dev["id"])


class InProcessTest(unittest.TestCase):
    """The server in this process, so that its patience and its disk can be set."""
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        db = Db(self.root / "t.db")
        self.dev = db.add_device("phone")
        for p in (mock.patch.object(ingest, "INBOX_DIR", self.root / "data" / "inbox"), mock.patch.object(drop, "DROP_DIR", self.root / "data" / "drop"),
                  mock.patch.object(drop, "DROP_MIN_FREE", 0), mock.patch.object(sessions, "_store", db), mock.patch.object(sessions, "SERVER_URL", ""),
                  mock.patch.object(routes, "STALL", 1.5), mock.patch.object(server.Handler, "log_message", lambda *a: None)):
            p.start()
            self.addCleanup(p.stop)
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        self.srv.daemon_threads = True
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.addCleanup(self.srv.server_close)
        self.addCleanup(self.srv.shutdown)
        self.c = DropClient(f"http://127.0.0.1:{self.srv.server_address[1]}", self.dev["token"])
        self.octets = {"Content-Type": "application/offset+octet-stream"}

    def staged(self, batch: str, name: str) -> Path:
        return self.root / "data" / "drop" / self.dev["id"] / batch / "files" / name

    def test_a_silent_sender_is_let_go(self):
        """A phone that left the Wi-Fi sends no FIN: its connection would hold the file until the kernel gave up.
        After STALL seconds without a byte the piece ends, what came is kept, and the file is free for the resume."""
        b = "20261011-100000-stall"
        up, s = self.half_sent(b)
        self.assertEqual(self.c.request("DELETE", up)[0], 423)               # the silent one holds the file
        s.settimeout(10)
        answer = s.recv(4096)                                                # until its patience is over
        self.assertEqual(answer[:12], b"HTTP/1.1 408")                       # and it is told, had it still listened
        self.assertIn(b"Connection: close", answer)
        self.assertEqual(self.c.patch(up, 300, b"b" * 700), 1000)            # what came was kept and the file is free
        self.assertEqual(self.staged(b, "VID00001.mov").read_bytes(), b"a" * 300 + b"b" * 700)

    def half_sent(self, b: str) -> tuple[str, socket.socket]:
        """A file of 1000 bytes declared and a connection that is inside its first piece: 300 bytes sent, 700 owed."""
        up = self.c.create(b, "VID00001.mov", 1000)
        s = socket.create_connection(self.srv.server_address)
        self.addCleanup(s.close)
        s.sendall((f"PATCH {up} HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer {self.dev['token']}\r\nTus-Resumable: 1.0.0\r\n"
                   "Upload-Offset: 0\r\nContent-Type: application/offset+octet-stream\r\nContent-Length: 1000\r\n\r\n").encode() + b"a" * 300)
        for _ in range(100):                                                  # its 300 bytes are in the file: it is inside the piece
            if self.c.offset(up) == 300:
                break
            time.sleep(0.02)
        return up, s

    def test_the_device_come_back_takes_its_file_over(self):
        """The phone is back on the Wi-Fi within the minute and sends the next piece. Its old connection is still
        counted as silent, not gone (a real phone waited 42 s here, asking 21 times): the new piece ends the old
        request at once and goes on from the last byte that arrived."""
        with mock.patch.object(routes, "STALL", 30):
            up, s = self.half_sent("20261011-100000-back")
            started = time.monotonic()
            self.assertEqual(self.c.patch(up, 300, b"b" * 700), 1000)
            self.assertLess(time.monotonic() - started, 3)
        self.assertEqual(self.staged("20261011-100000-back", "VID00001.mov").read_bytes(), b"a" * 300 + b"b" * 700)
        s.settimeout(5)
        self.assertEqual(s.recv(4096), b"")                                  # the old connection was closed under its sender

    def test_a_sender_that_breaks_off_in_a_piece_is_no_error(self):
        """A connection reset in the middle of a piece is how an upload is interrupted, not a fault of the server's:
        there is nobody to answer, nothing is logged (a real phone's left a 500 in the log), what came is kept and
        the next piece goes on from there."""
        seen, errors = [], []
        self.srv.handle_error = lambda request, client: errors.append(client)
        with mock.patch.object(server.Handler, "log_request", lambda h, code="-", size="-": seen.append((h.requestline, str(code)))), \
                mock.patch.object(server.Handler, "log_message", lambda h, fmt, *args: seen.append((fmt % args, "logged"))):
            up, s = self.half_sent("20261011-100000-reset")
            s.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
            s.close()                                                        # a reset, not a goodbye
            for _ in range(100):                                             # until the broken request has let go of the file
                if not drop._busy:
                    break
                time.sleep(0.02)
            self.assertEqual(self.c.patch(up, 300, b"b" * 700), 1000)
        self.assertEqual(errors, [])
        self.assertEqual([code for line, code in seen if line.startswith("PATCH") or code == "logged"], ["204"])     # the piece that went on, and no error

    def test_a_full_disk_is_said_as_no_room(self):
        """The disk filled up under a piece (the room was there when the file was declared): 507, as when the quota
        is reached, so the app waits instead of taking it for a broken server; and the file is not left busy."""
        b = "20261011-100000-full"
        up = self.c.create(b, "VID00001.mov", 10)
        with mock.patch.object(drop, "append", side_effect=OSError(errno.ENOSPC, "No space left on device")):
            status, h, body = self.c.request("PATCH", up, b"0123456789", {**self.octets, "Upload-Offset": "0"})
        self.assertEqual((status, h.get("connection"), json.loads(body)), (507, "close", {"error": "no room on the server's disk"}))
        # not every disk error is a full disk, and what went wrong inside is the log's to say, not the answer's: the
        # system's words name where the server keeps things
        for boom, said in ((RuntimeError("something else"), "RuntimeError: something else"),
                           (OSError(errno.EACCES, "Permission denied", "/srv/data/drop/d0/x"), "PermissionError: [Errno 13] Permission denied: '/srv/data/drop/d0/x'")):
            seen, kinds = [], []
            with mock.patch.object(drop, "append", side_effect=boom), mock.patch.object(server.Handler, "log_message", lambda h, fmt, *args: (seen.append(fmt % args), kinds.extend(map(type, args)))):
                status, h, body = self.c.request("PATCH", up, b"0123456789", {**self.octets, "Upload-Offset": "0"})
            self.assertEqual((status, h.get("connection"), json.loads(body)), (500, "close", {"error": "the server failed; its log says why"}))
            self.assertIn("drop: " + said, seen)
            self.assertEqual(set(kinds), {str})                               # text, which the log cuts when it is long: not the error itself
        self.assertEqual(self.c.patch(up, 0, b"0123456789"), 10)

    def test_a_disk_that_fills_under_a_piece_is_said_at_once(self):
        """The disk fills while a piece is being written, after some or all of its bytes were read. The answer does
        not wait for bytes the device has no more of: what was read is counted, so only what is really still on the
        connection is waited for."""
        b = "20261011-100000-under"
        patience = mock.patch.object(routes, "STALL", 6)                     # what an answer must not wait for
        patience.start()
        self.addCleanup(patience.stop)
        up = self.c.create(b, "VID00001.mov", 1000)
        with mock.patch.object(drop.os, "fsync", side_effect=OSError(errno.ENOSPC, "No space left on device")):   # every byte read, then no room
            started = time.monotonic()
            status, h, body = self.c.request("PATCH", up, b"a" * 1000, {**self.octets, "Upload-Offset": "0"})
        self.assertLess(time.monotonic() - started, 3)                       # not after STALL
        self.assertEqual((status, h.get("connection"), json.loads(body)), (507, None, {"error": "no room on the server's disk"}))
        self.assertEqual(self.c.request("DELETE", up)[0], 204)               # and the file is not left busy
        up = self.c.create(b, "VID00001.mov", 1000)
        real_open, calls = open, []

        class Filling:
            """The staged file, on a disk that takes the first write and no more."""
            def __init__(self, f): self.f = f
            def __enter__(self): return self
            def __exit__(self, *a): self.f.close()
            def seek(self, n): return self.f.seek(n)
            def fileno(self): return self.f.fileno()
            def write(self, piece):
                calls.append(len(piece))
                if len(calls) > 1:
                    raise OSError(errno.ENOSPC, "No space left on device")
                return self.f.write(piece)

        def fake(path, mode="r", **kw):
            return Filling(real_open(path, mode, **kw)) if mode == "r+b" else real_open(path, mode, **kw)

        with mock.patch.object(drop, "CHUNK", 100), mock.patch.object(drop, "open", fake, create=True):
            started = time.monotonic()
            status, h, body = self.c.request("PATCH", up, b"a" * 1000, {**self.octets, "Upload-Offset": "0"})
        self.assertLess(time.monotonic() - started, 3)
        self.assertEqual((status, h.get("connection")), (507, "close"))      # 800 bytes of the piece were never read: the connection ends
        self.assertEqual(self.c.offset(up), 100)                             # what the disk took is what is here
        self.assertEqual(self.c.patch(up, 100, b"b" * 900), 1000)

    def test_a_refusal_does_not_wait_for_a_body_in_no_hurry(self):
        """Before a refusal is answered, what was sent of the request's body is read and thrown away so that the answer
        arrives. That takes a moment at most: somebody without a token, feeding a long body a byte at a time, holds
        nothing."""
        with mock.patch.object(routes, "DRAIN_WAIT", 0.4), mock.patch.object(routes, "STALL", 6):
            s = socket.create_connection(self.srv.server_address)
            self.addCleanup(s.close)
            s.sendall(b"POST /drop/x HTTP/1.1\r\nHost: x\r\nContent-Length: 2000000\r\n\r\n")
            stop = threading.Event()

            def trickle():
                while not stop.wait(0.1):
                    try:
                        s.sendall(b"x")
                    except OSError:
                        return

            t = threading.Thread(target=trickle, daemon=True)
            t.start()
            started = time.monotonic()
            s.settimeout(5)
            answer = s.recv(4096)
            took = time.monotonic() - started
            stop.set()
            t.join(2)
        self.assertEqual(answer[:12], b"HTTP/1.1 401")
        self.assertIn(b"Connection: close", answer)
        self.assertLess(took, 3)                                             # the body never stops coming, and STALL is 6 here

    def test_a_refusal_does_not_wait_for_a_body_that_never_comes(self):
        """Declared and not sent: the answer waits DRAIN_WAIT for it, not the patience a piece in the middle gets."""
        with mock.patch.object(routes, "DRAIN_WAIT", 0.4), mock.patch.object(routes, "STALL", 6):
            s = socket.create_connection(self.srv.server_address)
            self.addCleanup(s.close)
            started = time.monotonic()
            s.sendall(b"POST /drop/x HTTP/1.1\r\nHost: x\r\nContent-Length: 10\r\n\r\n")
            s.settimeout(10)
            answer = s.recv(4096)
        self.assertEqual(answer[:12], b"HTTP/1.1 401")
        self.assertLess(time.monotonic() - started, 3)

    def test_what_is_read_before_a_refusal_is_bounded(self):
        """A refused piece of any size: no more than DRAIN bytes of it are read before the answer, and the connection
        ends with it."""
        sent, timeouts = [], []
        h = mock.Mock(rfile=io.BufferedReader(io.BytesIO(b"x" * (3 << 20))), wfile=io.BytesIO(), command="PATCH",
                      _drop_unread=3 << 20, _drop_drain=routes.DRAIN, _drop_unframed=False)
        h.send_header = lambda k, v: sent.append((k, v))
        h.connection.settimeout = timeouts.append
        with mock.patch.object(routes, "STALL", 60):
            routes._send(h, 409, {"error": "x"})
        self.assertEqual(h.rfile.tell(), 2 << 20)
        self.assertIn(("Connection", "close"), sent)
        self.assertEqual(timeouts[-1], 60)                                   # the answer itself is written with the usual patience,
        self.assertTrue(len(timeouts) > 1 and all(0 < t <= routes.DRAIN_WAIT for t in timeouts[:-1]), timeouts)    # each read before it with what is left of DRAIN_WAIT

    def test_a_connection_kept_open_is_given_its_patience_back(self):
        """STALL is for the middle of a request. Between two requests a device may keep its connection as long as it
        likes: the next one on it is answered."""
        c = http.client.HTTPConnection(*self.srv.server_address, timeout=10)
        self.addCleanup(c.close)
        hdrs = {"Authorization": f"Bearer {self.dev['token']}"}
        c.request("GET", "/drop/device", headers=hdrs)
        self.assertEqual(c.getresponse().read() and 200, 200)
        time.sleep(routes.STALL + 0.5)
        c.request("GET", "/drop/device", headers=hdrs)                       # on the same connection
        r = c.getresponse()
        r.read()
        self.assertEqual(r.status, 200)

    def test_what_the_system_says_of_a_missing_file_is_not_passed_on(self):
        up = self.c.create("20261011-100000-gone", "VID00001.mov", 10)
        self.staged("20261011-100000-gone", "VID00001.mov").unlink()         # under a declared file: it is not here, and no path says where
        status, _, body = self.c.request("PATCH", up, b"0123456789", {**self.octets, "Upload-Offset": "0"})
        self.assertEqual((status, json.loads(body)), (404, {"error": "not found"}))
        status, _, body = self.c.request("HEAD", upload_url("20261011-100000-gone", "VID00002.mov"))
        self.assertEqual(status, 404)
        status, body = self.c.json("POST", "/drop/batches/20261011-100000-none/commit", {"files": {"VID00001.mov": 1}})
        self.assertEqual((status, body), (404, {"error": "20261011-100000-none: nothing staged"}))     # what the drop itself says is said

    def test_a_manifest_as_long_as_a_full_batch(self):
        """A batch may hold 500 files; its manifest is taken whole (it is a few kilobytes), one nested without end is
        no manifest."""
        b = "20261011-100000-many"
        names = {f"VID{n:05d}.mov": 0 for n in range(drop.MAX_FILES - 1)} | {"import.json": 2}
        self.assertGreater(len(json.dumps({"files": names})), 9_000)
        self.assertEqual(self.c.json("POST", f"/drop/batches/{b}/commit", {"files": names}), (404, {"error": f"{b}: nothing staged"}))
        self.c.create(b, "VID00000.mov", 0)
        status, _, body = self.c.request("POST", f"/drop/batches/{b}/commit", b"[" * 100_000, {"Content-Type": "application/json"})
        self.assertEqual(status, 400)
        self.assertIn("manifest", json.loads(body)["error"])

    def test_a_client_that_drops_an_idle_connection_is_no_error(self):
        """A phone resets a connection it kept open and no longer wants (seen on the first upload from a real one:
        six tracebacks in the server's log for six such resets). Between two requests that is nothing: no error to
        handle, nothing to log, and the server goes on answering."""
        errors = []
        self.srv.handle_error = lambda request, client: errors.append(client)
        for path in ("/api/store", "/drop/device"):
            s = socket.create_connection(self.srv.server_address)
            s.sendall(f"GET {path} HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer {self.dev['token']}\r\n\r\n".encode())
            self.assertEqual(s.recv(4096)[:15], b"HTTP/1.1 200 OK")              # answered, and the connection is kept
            s.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
            s.close()                                                        # a reset, not a goodbye
        time.sleep(0.5)
        self.assertEqual(errors, [])
        self.assertEqual(self.c.request("GET", "/drop/device")[0], 200)

    def test_a_drop_without_an_address_of_its_own_says_none(self):
        """The drop is reached where the server is, unless `ACROSCOPE_DROP_URL` says it has a host of its own."""
        api = DropClient(self.c.url.geturl(), None)
        self.assertEqual(api.json("GET", "/api/devices"), (200, {"devices": [{"id": self.dev["id"], "name": "phone", "created_at": self.dev["created_at"],
                                                                              "last_seen_at": None, "revoked_at": None}], "url": None}))
        for host in ("drop.example.org", "", ".", "[", ":", "[]:"):          # and no host is the drop's alone, not the one without a name either
            self.assertEqual(api.request("GET", "/api/devices", headers={"Host": host})[0], 200, host)
        for url in ("drop.example.org", "https://drop.example.org:8443/", "//drop.example.org", "https://user@Drop.Example.Org.", "drop.example.org:8443/x",
                    "https://drop.example.org?x", "https://drop.example.org#top", "https://u:p@ss@drop.example.org"):   # however the address was written
            with mock.patch.object(server, "DROP_URL", url):
                self.assertEqual(api.request("GET", "/api/devices", headers={"Host": "drop.example.org"})[0], 404, url)
                self.assertEqual(api.request("GET", "/api/devices", headers={"Host": "player.example.org"})[0], 200, url)
        # an address is the drop's on its own port only: the server is reached by address too, on its own port
        for url, theirs, others in (("http://10.0.0.5:8072", ("10.0.0.5:8072",), ("10.0.0.5:8070", "10.0.0.5", "10.0.0.50:8072")),
                                    ("https://10.0.0.5", ("10.0.0.5", "10.0.0.5:443"), ("10.0.0.5:8070",)),
                                    ("http://[2001:db8::5]", ("[2001:db8::5]", "[2001:db8::5]:80", "[2001:DB8::5]"), ("[2001:db8::7]", "[2001:db8::5]:8070", "[2001:db8::]"))):
            with mock.patch.object(server, "DROP_URL", url):
                for host in theirs:
                    self.assertEqual(api.request("GET", "/api/devices", headers={"Host": host})[0], 404, (url, host))
                for host in others:
                    self.assertEqual(api.request("GET", "/api/devices", headers={"Host": host})[0], 200, (url, host))
        # one with no host to be read is no drop's address: the server will not start with it, and a request is
        # never taken for one not the drop's
        for url in ("https://[x", "https://u:p/w@drop.example.org", "https:/drop.example.org", "https://", ":8070", "drop example.org", "ftp://drop.example.org"):
            with mock.patch.object(server, "DROP_URL", url):
                self.assertIsNone(server.drop_host(url), url)
                self.assertEqual(api.request("GET", "/api/devices", headers={"Host": "player.example.org"})[0], 404, url)
                with mock.patch.object(server, "ThreadingHTTPServer") as srv, self.assertRaises(SystemExit) as no:
                    server.serve(port=0)
                self.assertIn("ACROSCOPE_DROP_URL", str(no.exception.code))
                srv.assert_not_called()
        with mock.patch.object(server, "DROP_URL", "https://drop.example.org"):
            self.assertEqual(api.json("GET", "/api/devices")[1]["url"], "https://drop.example.org")
            self.assertEqual(api.request("GET", "/api/devices", headers={"Host": "drop.example.org"})[0], 404)
            self.assertEqual(self.c.request("GET", "/drop/device", headers={"Host": "drop.example.org"})[0], 200)

    def test_the_scheme_in_any_case(self):
        for scheme in ("bearer", "BEARER"):
            status, _, _ = DropClient(self.c.url.geturl(), None).request("GET", "/drop/device", headers={"Authorization": f"{scheme} {self.dev['token']}"})
            self.assertEqual(status, 200, scheme)


class LogTest(unittest.TestCase):
    def line(self, fmt: str, *args) -> str:
        h = server.Handler.__new__(server.Handler)
        h.client_address = ("192.0.2.7", 40000)
        with mock.patch("sys.stderr", io.StringIO()) as err:
            h.log_message(fmt, *args)
        return err.getvalue()

    def test_a_long_request_line_is_logged_by_its_start(self):
        """A request line is up to 64 kB of anybody's making, and every refusal is logged: the log keeps its start."""
        said = self.line('"%s" %s %s', "GET /drop/" + "a" * 60_000 + " HTTP/1.1", "401", "-")
        self.assertLess(len(said), 500)
        self.assertIn('"GET /drop/aaaa', said)
        self.assertTrue(said.rstrip().endswith('..." 401 -'), said[-40:])
        self.assertIn("192.0.2.7", said)
        self.assertLess(len(self.line("drop: %s: %s", "OSError", "x" * 60_000)), 500)       # what an error says of itself likewise
        short = "GET /drop/batches/20261011-100000-ab12/files/VID00001.mov HTTP/1.1"
        self.assertIn(f'"{short}" 404 -', self.line('"%s" %s %s', short, "404", "-"))          # a line of ordinary length is whole
        self.assertIn("x" * 300 + " ", self.line("%s %s", "x" * 300, "404"))                    # to the last character it may have
        self.assertIn("drop: KeyError: 200", self.line("drop: %s: %s", "KeyError", "200"))     # an error whose text is a status is an error
        for code, logged in ((204, False), (201, False), (200, False), (404, True), (500, True)):   # and what went well is not logged, as ever
            h = server.Handler.__new__(server.Handler)
            h.client_address, h.requestline = ("192.0.2.7", 40000), short
            with mock.patch("sys.stderr", io.StringIO()) as err:
                h.log_request(code)
            self.assertEqual(bool(err.getvalue()), logged, code)
        self.assertIn("Request timed out: TimeoutError('timed out')", self.line("Request timed out: %r", TimeoutError("timed out")))   # nor is a line of one value a line too short


if __name__ == "__main__":
    unittest.main()
