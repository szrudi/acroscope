"""Session files: videos/<session>/session.json is the source of truth for a flying session.

{
  "session": "2026-10-07-schammer",
  "date": "2026-10-07",
  "note": "free text about the session",
  "videos":  [{"file": "2026-10-07_007.mp4", "duration": 175.02, "note": "..."}],
  "blackbox": ["BTFL_BLACKBOX_LOG_..._20261007_212530_....bbl"],
  "matches": [{"video": "2026-10-07_007.mp4", "bbl": "...", "arm": 13, "offset": 53.2, "note": "..."}],
  "moments": [{"id": "m07", "video": "2026-10-07_007.mp4", "start": 78.0, "end": 87.0, "title": "powerloop 1",
               "tags": ["powerloop"], "note": "...", "metrics": {...}}]
}
Times are video seconds. video time = arm time + offset (offset holds within one arm; the clips have static cut
out, so a clip's timeline is not continuous across arms). Arm details come from the blackbox cache, not from here.
"""
import json
import re
import subprocess
import sys
from pathlib import Path

from . import blackbox, video
from .config import BLACKBOX_DIR, VIDEOS_DIR

VIDEO_EXT = (".mp4", ".mov", ".mkv")


def session_dirs() -> list[Path]:
    if not VIDEOS_DIR.is_dir():   # data dir not mounted (yet): no sessions rather than a crash
        return []
    return sorted(d for d in VIDEOS_DIR.iterdir() if d.is_dir() and re.match(r"\d{4}-\d{2}-\d{2}", d.name))


def session_path(session: str) -> Path:
    return VIDEOS_DIR / session / "session.json"


def resolve_session(name: str) -> str:
    """Accept the full dir name or a unique fragment ('10-07', 'schammer')."""
    if (VIDEOS_DIR / name).is_dir():
        return name
    hits = [d.name for d in session_dirs() if name in d.name]
    if len(hits) != 1:
        raise FileNotFoundError(f"{name}: {len(hits)} sessions match (need exactly 1)")
    return hits[0]


def load(session: str) -> dict:
    p = session_path(session)
    if p.exists():
        return json.loads(p.read_text())
    return {"session": session, "date": session[:10], "note": "", "videos": [], "blackbox": [], "matches": [],
            "moments": []}


def save(s: dict) -> Path:
    p = session_path(s["session"])
    s["moments"].sort(key=lambda m: (m["video"], m["start"]))
    p.write_text(json.dumps(s, indent=1, ensure_ascii=False) + "\n")
    return p


def refresh(session: str, probe_videos: bool = True) -> dict:
    """Create or update the session file from the folder: add new clips (with duration), keep notes, and pick the
    blackbox files of that date when none are set. Nothing is removed."""
    s = load(session)
    d = VIDEOS_DIR / session
    known = {v["file"]: v for v in s["videos"]}
    for f in sorted(d.iterdir()):
        if f.suffix.lower() in VIDEO_EXT and f.name not in known:
            known[f.name] = {"file": f.name, "duration": None, "note": ""}
    if probe_videos:
        for v in known.values():
            if (v.get("duration") is None or v.get("codec") is None) and (d / v["file"]).exists():
                try:
                    info = video.probe(d / v["file"])
                except subprocess.CalledProcessError:   # still syncing / half-written: try again next refresh
                    print(f"{v['file']}: ffprobe failed (still syncing?), skipped", file=sys.stderr)
                    continue
                v["duration"], v["codec"] = info["duration"], info["codec"]
    s["videos"] = sorted(known.values(), key=lambda v: v["file"])
    if not s["blackbox"]:
        s["blackbox"] = [b.name for b in blackbox.all_bbls() if blackbox.bbl_date(b) == s["date"] and b.stat().st_size]
    save(s)
    return s


def match_for(s: dict, vid: str, t: float | None = None) -> dict | None:
    """The match covering video `vid` (at video time t if given, else the first)."""
    ms = [m for m in s["matches"] if m["video"] == vid]
    if t is None or not ms:
        return ms[0] if ms else None
    best = None
    for m in ms:
        a = t - m["offset"]
        length = arm_length(m)
        if a >= -1 and (length is None or a <= length + 1):
            best = m
    return best or ms[0]


def arm_length(m: dict) -> float | None:
    try:
        idx = blackbox.index_bbl(m["bbl"])
    except FileNotFoundError:
        return None
    for a in idx["arms"]:
        if a["index"] == m["arm"]:
            return a["length"]
    return None


def set_match(session: str, vid: str, bbl: str, arm: int, offset: float, note: str = "", boot: bool = False) -> dict:
    """Record video <-> arm with `offset` (video = arm time + offset). With boot=True, also record every other arm
    of the same power cycle, their offsets derived from the FC uptime (video = uptime + boot offset)."""
    s = load(session)
    vid = video.resolve_video(session, vid).name
    bbl_name = blackbox.resolve_bbl(bbl).name
    new = [(arm, round(offset, 2), note)]
    if boot:
        idx = blackbox.index_bbl(bbl_name)
        meta = {a["index"]: a for a in idx["arms"]}
        b = next((b for b in idx.get("boots", []) if arm in b["arms"]), None)
        if b:
            boot_offset = offset - meta[arm]["uptime_start"]
            new = [(i, round(boot_offset + meta[i]["uptime_start"], 2), note if i == arm else f"derived from arm {arm} via uptime (boot {b['first']}-{b['last']})")
                   for i in b["arms"]]
    for i, off, n in new:
        s["matches"] = [m for m in s["matches"] if not (m["video"] == vid and m["bbl"] == bbl_name and m["arm"] == i)]
        s["matches"].append({"video": vid, "bbl": bbl_name, "arm": i, "offset": off, "note": n})
    s["matches"].sort(key=lambda m: (m["video"], m["offset"]))
    if bbl_name not in s["blackbox"]:
        s["blackbox"].append(bbl_name)
    save(s)
    return s


def unmatch(session: str, vid: str, arm: int | None = None) -> int:
    """Remove the matches of a clip (all, or one arm). Returns how many were removed."""
    s = load(session)
    vid = video.resolve_video(session, vid).name
    before = len(s["matches"])
    s["matches"] = [m for m in s["matches"] if not (m["video"] == vid and (arm is None or m["arm"] == arm))]
    save(s)
    return before - len(s["matches"])


def next_id(s: dict) -> str:
    n = max((int(m["id"][1:]) for m in s["moments"] if re.fullmatch(r"m\d+", m.get("id", ""))), default=0)
    return f"m{n + 1:02d}"


def tag(session: str, vid: str, start: float, end: float, title: str, tags: list[str], note: str = "",
        metrics: dict | None = None, mid: str | None = None) -> dict:
    """Add a moment, or replace the one with id `mid`."""
    s = load(session)
    vid = video.resolve_video(session, vid).name
    m = {"id": mid or next_id(s), "video": vid, "start": round(start, 2), "end": round(end, 2), "title": title,
         "tags": tags, "note": note}
    if metrics:
        m["metrics"] = metrics
    s["moments"] = [x for x in s["moments"] if x["id"] != m["id"]] + [m]
    save(s)
    return m


def untag(session: str, mid: str) -> bool:
    s = load(session)
    before = len(s["moments"])
    s["moments"] = [x for x in s["moments"] if x["id"] != mid]
    save(s)
    return len(s["moments"]) < before


def overview(session: str, with_arms: bool = True, cached_only: bool = False) -> dict:
    """Session file plus the arm index of its blackbox files and coverage (which arms have a clip).
    With cached_only, blackbox files not decoded yet are listed in `arms_pending` instead of decoded here."""
    s = load(session)
    out = dict(s)
    if with_arms:
        arms = []
        out["arms_pending"] = []
        for b in s["blackbox"]:
            try:
                idx = blackbox.index_bbl(b, cached_only=cached_only)
            except FileNotFoundError:
                continue
            if idx is None:
                out["arms_pending"].append(b)
                continue
            for a in idx["arms"]:
                matched = [m for m in s["matches"] if m["bbl"] == idx["file"] and m["arm"] == a["index"]]
                arms.append({"bbl": idx["file"], **{k: a.get(k) for k in ("index", "length", "rate", "vbat_start",
                             "vbat_end", "vbat_min", "motors_spun", "disarm_reason", "complete")},
                             "videos": [{"video": m["video"], "offset": m["offset"]} for m in matched]})
        out["arms"] = arms
    return out


def all_sessions() -> list[dict]:
    rows = []
    for d in session_dirs():
        s = load(d.name)
        rows.append({"session": d.name, "videos": len(s["videos"]) or len([f for f in d.iterdir() if f.suffix.lower() in VIDEO_EXT]),
                     "has_file": session_path(d.name).exists(), "matches": len(s["matches"]), "moments": len(s["moments"]),
                     "blackbox": s["blackbox"]})
    return rows
