"""Matches made by hand, and which logs a session has: a clip <-> arm match with its offset (one arm, or every arm of
its power cycle, derived from the FC's uptime), a log attached or detached, a log moved to another session.
"""
from ..sources import blackbox
from ..store import sessions


def set_match(session: str, vid: str, bbl: str, arm: int, offset: float, note: str = "", boot: bool = False) -> list[dict]:
    """Record video <-> arm with `offset` (video = arm time + offset). With boot=True, also record every other arm
    of the same power cycle, their offsets derived from the FC uptime (video = uptime + boot offset). Returns the
    matches written, as the store gives them back (the named arm first)."""
    vid = sessions.resolve_clip(session, vid)
    if boot and sessions.remote():
        raise ValueError("match --boot reads the log: run it where the data is (unset ACROSCOPE_URL)")
    bbl_name = bbl if sessions.remote() else blackbox.resolve_bbl(bbl).name      # the server resolves it against its own logs
    new = [(arm, round(offset, 2), note)]
    if boot:
        idx = blackbox.index_bbl(bbl_name)
        meta = {a["index"]: a for a in idx["arms"]}
        b = next((b for b in idx.get("boots", []) if arm in b["arms"]), None)
        if b:
            # the clip's clock runs a few tenths of a percent off the FC's (#4): when this clip already has three
            # or more arms of the boot matched over a minute, derive along their line, anchored on the named arm
            cur = sessions.load(session)
            known = [(meta[m["arm"]]["uptime_start"], m["offset"]) for m in cur["matches"]
                     if m["video"] == vid and m["bbl"] == bbl_name and m["arm"] in b["arms"] and m["arm"] != arm and m["arm"] in meta]
            known.append((meta[arm]["uptime_start"], offset))
            a = 1.0
            xs = [x for x, _ in known]
            if len(known) >= 3 and max(xs) - min(xs) >= 60:
                mx, my = sum(xs) / len(xs), sum(y for _, y in known) / len(known)
                a = sum((x - mx) * (y - my) for x, y in known) / sum((x - mx) ** 2 for x in xs)
            boot_offset = offset - a * meta[arm]["uptime_start"]
            dur = next((v.get("duration") for v in cur["videos"] if v["file"] == vid), None)
            new = []
            for i in b["arms"]:
                off = round(boot_offset + a * meta[i]["uptime_start"], 2)
                # an arm of the boot that lies outside this clip belongs to another clip (or to none): leave it
                if i != arm and dur and (off > dur or off + meta[i]["length"] < 0):
                    continue
                new.append((i, off, note if i == arm else f"derived from arm {arm} via uptime (boot {b['first']}-{b['last']}" + (f", clock {100 * (a - 1):+.2f} %)" if a != 1.0 else ")")))
    new.sort(key=lambda r: r[0] != arm)
    return [sessions.store().set_match(session, vid, bbl_name, i, off, n) for i, off, n in new]


def unmatch(session: str, vid: str, arm: int | None = None) -> int:
    """Remove the matches of a clip (all, or one arm). Returns how many were removed."""
    return sessions.store().unmatch(session, sessions.resolve_clip(session, vid), arm)


def attach(session: str, bbl: str) -> list[str]:
    return sessions.store().attach(session, bbl if sessions.remote() else blackbox.resolve_bbl(bbl).name)


def detach(session: str, bbl: str) -> list[str]:
    known = bbl in sessions.load(session)["blackbox"]          # a log that was deleted from blackbox/ can still be detached by name
    return sessions.store().detach(session, bbl if sessions.remote() or known else blackbox.resolve_bbl(bbl).name)


def move_log(session: str, bbl: str, to: str) -> list[str]:
    """⇄ on a log: into session `to` (which must exist), out of this one with its matches here. `to`'s logs."""
    if sessions.remote():
        return sessions.store().move_log(session, bbl, to)
    sessions.store().move_log(session, bbl, to)
    return sessions.load(to)["blackbox"]
