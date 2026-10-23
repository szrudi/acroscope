"""What every command group shares: times as the CLI reads and prints them, JSON out, a session by fragment.
"""
import json

from ..sources import blackbox
from ..store import sessions


def parse_time(s: str) -> float:
    if ":" in s:
        m, _, sec = s.rpartition(":")
        return int(m) * 60 + float(sec)
    return float(s)


def fmt_time(t: float) -> str:
    t = round(t, 1)                               # round first, or 59.96 prints as 0:60.0
    m, s = divmod(abs(t), 60)
    return ("-" if t < 0 else "") + f"{int(m)}:{s:04.1f}"    # before the clip's start: signed, not 0:00.0


def out(obj):
    print(json.dumps(obj, indent=1, ensure_ascii=False))


def _session(name):
    return sessions.resolve_session(name)


def _arm_for(s: dict, vid: str, t: float):
    """(match, Arm) for a video time, or (None, None) when the clip has no match."""
    m = sessions.match_for(s, vid, t)
    if not m:
        return None, None
    return m, blackbox.load_arm(m["bbl"], m["arm"])


def _segment_count(segs: list[dict]) -> str:
    """'60 arms, 1 segment without frames': a segment without frames (unreadable, or empty) is no arm."""
    bad = sum(1 for r in segs if not r.get("frames", 1))
    return f"{len(segs) - bad} arms" + (f", {bad} segment{'' if bad == 1 else 's'} without frames" if bad else "")
