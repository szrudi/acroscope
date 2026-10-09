"""One-off import of the VLC moment playlists (videos/<session>/moments.xspf) into the session file."""
import xml.etree.ElementTree as ET

from . import sessions
from .config import VIDEOS_DIR

NS = {"x": "http://xspf.org/ns/0/", "vlc": "http://www.videolan.org/vlc/playlist/ns/0/"}


def import_session(session: str) -> list[dict]:
    p = VIDEOS_DIR / session / "moments.xspf"
    if not p.exists():
        return []
    s = sessions.load(session)
    have = {(m["video"], m["start"], m["end"]) for m in s["moments"]}
    added = []
    for tr in ET.parse(p).getroot().iterfind(".//x:track", NS):
        loc = tr.findtext("x:location", default="", namespaces=NS)
        title = tr.findtext("x:title", default="", namespaces=NS)
        opts = {o.text.partition("=")[0]: float(o.text.partition("=")[2]) for o in tr.iterfind(".//vlc:option", NS) if "=" in (o.text or "")}
        start, end = opts.get("start-time", 0.0), opts.get("stop-time", 0.0)
        if (loc, start, end) in have:
            continue
        # titles are "10-07 powerloop 1": strip the date prefix
        if len(title) > 6 and title[2] == "-" and title[:2].isdigit() and title[5] == " ":
            title = title[6:]
        m = {"id": sessions.next_id(s), "video": loc, "start": start, "end": end, "title": title, "tags": [], "note": ""}
        s["moments"].append(m)
        added.append(m)
    sessions.save(s)
    return added
