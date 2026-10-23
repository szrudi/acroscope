"""Moments and notes: a stretch of a clip with a title, tags and a note (what the player's tagging and `acroscope tag`
write), and the note of a session or a clip.
"""
import math

from ..store import sessions


def tag(session: str, vid: str, start: float, end: float, title: str, tags: list[str], note: str = "",
        metrics: dict | None = None, mid: str | None = None) -> dict:
    """Add a moment, or replace the one with id `mid`."""
    if not (math.isfinite(start) and math.isfinite(end)) or end < start:
        raise ValueError(f"a moment runs from start to end, in seconds: {start}..{end}")
    return sessions.store().tag(session, sessions.resolve_clip(session, vid), start, end, title, tags, note, metrics, mid)


def untag(session: str, mid: str) -> bool:
    return sessions.store().untag(session, mid)


def set_note(session: str, note: str, vid: str | None = None) -> None:
    sessions.store().set_note(session, note, sessions.resolve_clip(session, vid) if vid else None)
