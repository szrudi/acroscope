"""The trick engine's suggestions for an arm (tricks.detect), kept per arm under the tricks in force: a change to the
shipped file or the pilot's layer shows on the next look.
"""
from functools import lru_cache

from .. import tricks
from ..sources import blackbox


@lru_cache(maxsize=64)
def arm_events(bbl: str, arm: int, stamp: tuple) -> list[dict]:
    """The suggestions for an arm under the tricks in force (`stamp`: tricks.fingerprint(), the shipped file's mtime
    and the pilot's layer, so a change shows on the next look without a restart)."""
    return tricks.detect(blackbox.load_arm(bbl, arm))


def on_clip(e: dict, m: dict) -> dict:
    """A suggestion placed on the clip its arm is matched to: video = arm time + offset (a match holds within one
    arm), to the hundredth."""
    return {**e, "video": m["video"], "start": round(e["arm_from"] + m["offset"], 2), "end": round(e["arm_to"] + m["offset"], 2)}
