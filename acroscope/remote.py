"""A client of a running acroscope server: the same methods as db.Db, over its JSON API.

Used by the CLI when ACROSCOPE_URL is set (the CLI on joan, the database on the LXC). Only the store operations go
over the wire; metrics, events, frames and the OSD reader still run locally against the mounted data dir and the
local cache.
"""
import json
import urllib.error
import urllib.parse
import urllib.request


class RemoteError(RuntimeError):
    pass


class Remote:
    def __init__(self, url: str):
        self.url = url.rstrip("/")

    def __repr__(self):
        return f"server {self.url}"

    def _call(self, method: str, path: str, body: dict | None = None, **query):
        q = {k: v for k, v in query.items() if v is not None}
        url = self.url + path + ("?" + urllib.parse.urlencode(q) if q else "")
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=600) as r:
                out = json.loads(r.read() or b"null")
        except urllib.error.HTTPError as e:
            try:
                msg = json.loads(e.read()).get("error", str(e))
            except Exception:  # noqa: BLE001
                msg = str(e)
            raise RemoteError(f"{method} {path}: {msg}") from None
        except urllib.error.URLError as e:
            raise RemoteError(f"{self.url}: {e.reason}") from None
        if isinstance(out, dict) and "error" in out and len(out) == 1:
            raise RemoteError(f"{method} {path}: {out['error']}")
        return out

    def _s(self, session: str) -> str:
        return "/api/session/" + urllib.parse.quote(session, safe="")

    def sessions(self) -> list[dict]:
        return self._call("GET", "/api/sessions")

    def exists(self, session: str) -> bool:
        return any(s["session"] == session for s in self.sessions())

    def load(self, session: str) -> dict:
        return self._call("GET", self._s(session) + "/data")

    def set_note(self, session, note, video=None):
        self._call("POST", self._s(session) + "/note", {"note": note, **({"video": video} if video else {})})

    def attach(self, session, bbl):
        return self._call("POST", self._s(session) + "/blackbox", {"add": [bbl]})

    def detach(self, session, bbl):
        return self._call("POST", self._s(session) + "/blackbox", {"remove": [bbl]})

    def set_match(self, session, video, bbl, arm, offset, note=""):
        return self._call("POST", self._s(session) + "/match", {"video": video, "bbl": bbl, "arm": arm, "offset": offset, "note": note})

    def unmatch(self, session, video, arm=None) -> int:
        return self._call("DELETE", self._s(session) + "/match", video=video, arm=arm)["removed"]

    def tag(self, session, video, start, end, title, tags, note="", metrics=None, mid=None) -> dict:
        return self._call("POST", self._s(session) + "/moment", {"id": mid, "video": video, "start": start, "end": end,
                                                                  "title": title, "tags": tags, "note": note, "metrics": metrics})

    def untag(self, session, mid) -> bool:
        return self._call("DELETE", self._s(session) + "/moment/" + urllib.parse.quote(mid))["removed"]

    def purge(self, session, video=None, days=0) -> list[dict]:
        return self._call("POST", self._s(session) + "/purge", {"video": video, "days": days})

    def tags(self) -> dict:
        return self._call("GET", "/api/tags")

    def batches(self) -> list[dict]:
        return self._call("GET", "/api/inbox")

    def names_on(self, date: str) -> list[str]:
        return [v["name"] for s in self.sessions() for v in self.load(s["session"])["videos"] if (v.get("name") or "").startswith(date + "_")]

    def set_category(self, name, color=None) -> dict:
        return self._call("POST", "/api/tag-categories", {"name": name, "color": color})

    def set_tag(self, name, category, color=None) -> dict:
        return self._call("POST", "/api/tags", {"name": name, "category": category, "color": color})

    def delete_tag(self, name) -> bool:
        return self._call("DELETE", "/api/tags/" + urllib.parse.quote(name, safe=""))["removed"]

    def delete_category(self, name) -> bool:
        return self._call("DELETE", "/api/tag-categories/" + urllib.parse.quote(name, safe=""))["removed"]

    def tag_usage(self, session=None) -> dict:
        return self._call("GET", "/api/tag-usage", session=session)

    # the server does these against its own mount of the data dir
    def refresh(self, session: str, probe_videos: bool = True) -> dict:
        return self._call("POST", self._s(session) + "/refresh", {"probe": probe_videos})

    def scan(self) -> list[dict]:
        return self._call("POST", "/api/scan")

    def import_json(self, session: str | None = None) -> list[dict]:
        return self._call("POST", "/api/import", {"session": session})
