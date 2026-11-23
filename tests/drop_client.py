"""A device's side of the drop (#75), as small as it gets: the requests the companion app makes, for the tests to
make them too. Standard library only.

The protocol is tus 1.0.0 (core, creation, termination) for the files, plus a commit:

    POST   /drop/batches/<batch>/files          Upload-Length, Upload-Metadata: filename <base64>   -> 201, Location
    HEAD   <location>                                                                             -> Upload-Offset
    PATCH  <location>                           Upload-Offset, the bytes                          -> 204, Upload-Offset
    POST   /drop/batches/<batch>/commit         {"files": {name: bytes}}                          -> the batch's status

every request with `Authorization: Bearer <the device's token>` and `Tus-Resumable: 1.0.0`."""
import base64
import http.client
import json
from urllib.parse import quote, urlparse


class DropClient:
    def __init__(self, url: str, token: str | None):
        self.url, self.token = urlparse(url), token

    def request(self, method: str, path: str, body=None, headers: dict | None = None, tus: bool = True) -> tuple[int, dict, bytes]:
        """One request on its own connection: (status, headers by lower-case name, body)."""
        h = {"Tus-Resumable": "1.0.0"} if tus else {}
        if self.token is not None:
            h["Authorization"] = f"Bearer {self.token}"
        h.update(headers or {})
        c = http.client.HTTPConnection(self.url.hostname, self.url.port, timeout=30)
        try:
            c.request(method, path, body=body, headers=h)
            r = c.getresponse()
            return r.status, {k.lower(): v for k, v in r.getheaders()}, r.read()
        finally:
            c.close()

    def json(self, method: str, path: str, body=None) -> tuple[int, object]:
        data = json.dumps(body).encode() if body is not None else None
        status, _, out = self.request(method, path, data, {"Content-Type": "application/json"} if data else None)
        return status, json.loads(out) if out else None

    def create(self, batch: str, name: str, length: int) -> str:
        meta = "filename " + base64.b64encode(name.encode()).decode()
        status, h, body = self.request("POST", f"/drop/batches/{batch}/files", headers={"Upload-Length": str(length), "Upload-Metadata": meta})
        if status != 201:
            raise RuntimeError(f"create {name}: {status} {body[:200]!r}")
        return h["location"]

    def offset(self, upload: str) -> int:
        status, h, _ = self.request("HEAD", upload)
        if status != 200:
            raise RuntimeError(f"HEAD {upload}: {status}")
        return int(h["upload-offset"])

    def patch(self, upload: str, at: int, data: bytes) -> int:
        status, h, body = self.request("PATCH", upload, data, {"Upload-Offset": str(at), "Content-Type": "application/offset+octet-stream"})
        if status != 204:
            raise RuntimeError(f"PATCH {upload} at {at}: {status} {body[:200]!r}")
        return int(h["upload-offset"])

    def upload(self, batch: str, name: str, data: bytes, piece: int = 1 << 20) -> str:
        """A file from wherever the server has it to its end, in pieces: the same call starts and resumes."""
        upload = self.create(batch, name, len(data))
        at = self.offset(upload)
        while at < len(data):
            at = self.patch(upload, at, data[at:at + piece])
        return upload

    def commit(self, batch: str, files: dict) -> dict:
        status, out = self.json("POST", f"/drop/batches/{batch}/commit", {"files": files})
        if status != 200:
            raise RuntimeError(f"commit {batch}: {status} {out}")
        return out

    def push(self, batch: str, files: dict[str, bytes], piece: int = 1 << 20) -> dict:
        """A whole batch: every file, then the commit with the manifest."""
        for name, data in files.items():
            self.upload(batch, name, data, piece)
        return self.commit(batch, {name: len(data) for name, data in files.items()})


def upload_url(batch: str, name: str) -> str:
    return f"/drop/batches/{batch}/files/{quote(name, safe='')}"
