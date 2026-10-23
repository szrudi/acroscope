"""A clip compressed again from its kept original (a cut that took real footage, or a mis-stamped frame rate), its
moments and matches moved with it.
"""
import os
from pathlib import Path

from . import clock, encode
from .. import ingest
from ..sources import blackbox, video
from ..store import sessions
from ..config import BLACKBOX_DIR, CACHE_DIR, DATA_DIR, VIDEOS_DIR


def recut(session: str, vid: str, log=print) -> dict:
    """Compress a clip again from its kept original, under the same id: for a cut that took real footage (the
    detector before 2026-10-14 took dusk and a grey sky for static, and 3.5 s of flight each in 2026-10-14
    schammer 018 and 022). Moments and matches move with the footage: old clip time -> original -> new clip time.
    A match whose arm spans an old cut held on one side of it only, so it is left as it is and listed in `check`
    for a look. The clip's cached frames and sheets go; the original stays until its date.
    {video, old_cuts, cuts, duration, moments, matches, shares, check}"""
    db = sessions.store()
    file = sessions.resolve_clip(session, vid)
    s = db.load(session)
    v = next((x for x in s["videos"] if x["file"] == file), None)
    if v is None:
        raise ValueError(f"{session}: no clip {vid}")
    src = DATA_DIR / v["original"] if v.get("original") else None
    if not src or not src.is_file():
        raise ValueError(f"{v.get('name') or file}: no original kept (they go {ingest.ORIGINAL_DAYS} days after the import)")
    k_old = v.get("timescale") or 1.0
    old = v.get("cuts") or [[0.0, v["duration"] / k_old]]
    work = ingest.ORIGINALS_DIR / ".ingest" / f"recut-{file}"                     # not under videos/: the scan would take it
    work.parent.mkdir(parents=True, exist_ok=True)
    log(f"{session}/{v.get('name') or file}: compressing again from {v['original']}")
    try:
        res = encode.compress(src, work)
        if not res["verified"]:
            raise ValueError(f"{file}: compressed clip is {res['duration']} s against {sum(e - s for s, e in res['kept']):.1f} s kept: not replaced")
    except Exception:
        work.unlink(missing_ok=True)
        raise
    new = res["kept"]
    k_new = clock.fix_clock(work, new, log)
    if k_new is None:                       # cannot tell from the new encode: the clock is the original's, as it was
        k_new = k_old
        if k_old != 1.0:
            clock.retime(work, k_old)
    move = lambda t: clock.move_time(t, old, k_old, new, k_new)
    reclocked = abs(k_new / k_old - 1) > 0.01
    moments, matches, check = [], [], []
    for m in (m for m in s["moments"] if m["video"] == file):
        a, b = move(m["start"]), move(m["end"])
        if (a, b) != (m["start"], m["end"]):
            db.tag(session, file, a, b, m["title"], m.get("tags") or [], m.get("note") or "", mid=m["id"])
            moments.append({"id": m["id"], "from": [m["start"], m["end"]], "to": [a, b]})
    for m in (m for m in s["matches"] if m["video"] == file):
        idx = blackbox.index_bbl(m["bbl"], cached_only=True) if (BLACKBOX_DIR / m["bbl"]).is_file() else None
        length = next((a["length"] for a in (idx or {}).get("arms", []) if a["index"] == m["arm"]), None)
        if length is None or reclocked or clock._stretch(m["offset"] / k_old, old) != clock._stretch((m["offset"] + length) / k_old, old):
            why = ("arm length unknown" if length is None else f"the clip's clock changed ({k_old} -> {k_new}): the offset was measured on the old one"
                   if reclocked else "the arm spans an old cut: its offset held on one side only")
            check.append({"bbl": m["bbl"], "arm": m["arm"], "offset": m["offset"], "why": why})
            continue
        o = move(m["offset"])
        if o != m["offset"]:
            db.set_match(session, file, m["bbl"], m["arm"], o, m.get("note") or "")
            matches.append({"bbl": m["bbl"], "arm": m["arm"], "from": m["offset"], "to": o})
    shares = []
    for sh in (x for x in db.shares() if x["session"] == session and x["video"] == file):   # the ones up: their window on the new timeline (#59 review)
        a, b = move(sh["start"]), move(sh["end"])
        if (a, b) != (sh["start"], sh["end"]):
            db.update_share(sh["token"], start=a, end=b)
            shares.append({"token": sh["token"], "from": [sh["start"], sh["end"]], "to": [a, b]})
    os.replace(work, VIDEOS_DIR / session / file)
    duration = video.probe(VIDEOS_DIR / session / file)["duration"]
    db.set_video_fields(session, file, cuts=new, duration=duration, codec=res["codec"], timescale=k_new if k_new != 1.0 else None)
    for f in (CACHE_DIR / "frames" / session).glob(f"{Path(file).stem}_*"):
        f.unlink()
    return {"video": file, "old_cuts": old, "cuts": new, "timescale": k_new, "duration": duration, "moments": moments,
            "matches": matches, "shares": shares, "check": check}
