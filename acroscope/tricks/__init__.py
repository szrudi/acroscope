"""Trick detection: an engine plus a tricks file (#22).

The engine cuts an arm into candidate windows (segments.py), measures each (features.py: one vocabulary, the same as
`metrics.summary`), and judges the windows by the tricks file (library.py: one TOML entry per trick, conditions in a
small language of its own, expr.py). `detect(arm)` gives the suggestions as `events` always did: [{tag, arm_from,
arm_to, title, why, numbers}], plus `trick`, the entry that fired. `explain(arm)` shows every window and what each
trick made of it, for tuning.

Precedence is in the file, not in code: on one window the trick with the highest `priority` wins; a detection
`suppress`es others by tag (overlapping windows, or point events within so many seconds), judged in priority order.
"""
import os
from dataclasses import dataclass
from pathlib import Path

from ..blackbox import Arm
from . import expr, features, segments
from .library import SHIPPED, Library, LibraryError, Trick

__all__ = ["check", "detect", "explain", "library", "user_path", "Library", "LibraryError", "SHIPPED", "TEMPLATE"]

# what `acroscope tricks init` writes: a pilot's file with the knobs shown and off
TEMPLATE = """# Your tricks, over acroscope's shipped ones (acroscope/tricks/tricks.toml). `acroscope tricks` shows what is in
# force, `acroscope tricks show <id>` one trick, `acroscope tricks explain <bbl>:<arm>` every window's verdict.
# Uncomment what you need; a name that does not exist is refused when the file loads.
version = 1

# skill = "beginner"            # a preset over the defaults: "beginner" (tricks count when nearly there) or
                                # "advanced" (only when clean); your own [params] below win over it

[params]                        # any number from `acroscope tricks` (its params line)
# split_s_throttle_max = 50     # my split-S keeps some throttle on
# flip_min_deg = 250

# [[trick]]                     # turn a trick off, or change one of its fields
# id = "gyro-kick"
# enabled = false

# [[trick]]                     # or add one of your own (acroscope tricks features: the names a condition may use)
# id = "hard-roll"
# tag = "roll"
# segment = "fast-rotation"
# when = ["axis == 'roll'", "abs(rotation_deg.roll) >= 330", "peak_rate_dps.roll >= 0.8 * arm.rates_max_dps.roll"]
# title = "{direction} roll at full rate"
# why = "roll {rotation_deg.roll:+} deg at {peak_rate_dps.roll} dps"
# priority = 15

# [craft."OttoJr"]              # by the Craft name in the log header: these arms only
# size_mm = 75                  # the craft.* features (craft.size_mm, …) for conditions that scale with the quad
# weight_g = 40
# skill = "advanced"            # a preset for this quad
# [craft."OttoJr".params]
# loop_min_dur = 1.2
# [craft."OttoJr".segment.impact]
# g = 5                         # a heavier quad hits harder before it is a crash
# [[craft."OttoJr".trick]]
# id = "motor-loss"
# enabled = false
"""

_cache: tuple[tuple, Library] | None = None


def user_path() -> Path:
    """Where a pilot's own tricks file lives: ACROSCOPE_TRICKS (a file, or a directory of .toml files read in name
    order), else ~/.config/acroscope/tricks.toml. The server reads its own (the container's /state/tricks.toml)."""
    return Path(os.environ.get("ACROSCOPE_TRICKS") or "~/.config/acroscope/tricks.toml").expanduser()


def library_paths() -> list[Path]:
    """The shipped file, then the pilot's (a file, or every .toml of a directory, sorted)."""
    p = user_path()
    if p.is_dir():
        return [SHIPPED, *sorted(p.glob("*.toml"))]
    return [SHIPPED, p]


def library() -> Library:
    """The tricks in force, loaded once and again whenever a file changed (the server keeps running; a pilot
    edits the file and refreshes)."""
    global _cache
    paths = [p for p in library_paths() if p.is_file()]
    stamp = tuple((str(p), p.stat().st_mtime_ns) for p in paths)
    if _cache is None or _cache[0] != stamp:
        _cache = (stamp, Library.load(paths))
    return _cache[1]


def fingerprint() -> tuple:
    return library() and _cache[0]


@dataclass
class Hit:
    trick: Trick
    segment: str
    index: int                     # the window's index within its segment
    window: segments.Window
    ns: dict
    numbers: dict
    lost_to: str | None = None     # a higher-priority trick on the same window
    suppressed_by: str | None = None

    @property
    def alive(self) -> bool:
        return self.lost_to is None and self.suppressed_by is None

    @property
    def anchor(self) -> float:
        return self.window.extras["arm_t"] if self.window.point is not None else self.window.arm_from

    def event(self) -> dict:
        return {"trick": self.trick.id, "tag": self.trick.tag, "arm_from": self.window.arm_from, "arm_to": self.window.arm_to,
                "title": expr.render(self.trick.title, self.ns), "why": expr.render(self.trick.why, self.ns), "numbers": self.numbers}


class Run:
    """One arm through the library: the windows per segment (cut once, shared by the tricks on it), every trick's
    verdict on every window, and the hits after precedence."""

    def __init__(self, arm: Arm, lib: Library):
        craft = (arm.meta.get("headers") or {}).get("Craft name")
        self.base = lib
        self.arm, self.lib = arm, lib.for_craft(craft)          # the library in force for this quad
        self.context = {**self.lib.params, **lib.craft_context(craft), **features.arm_context(arm)}
        self.windows: dict[str, list] = {}           # segment name -> [(window, ns, numbers)]
        self.verdicts: dict[tuple, list] = {}        # (segment, index) -> [(trick, failed condition or None)]
        self.skipped: dict[str, list[str]] = {}      # trick id -> the fields it needs and the log lacks
        self.hits: list[Hit] = []
        self._judge()
        self._precedence()

    def _cut(self, name: str) -> list:
        if name not in self.windows:
            seg = self.lib.segments[name]
            out = []
            for win in segments.windows(self.arm, seg.kind, seg.opts):
                flat, numbers = features.measure(self.arm, win, seg.kind, seg.opts)
                out.append((win, {**self.context, **flat}, numbers))
            self.windows[name] = out
        return self.windows[name]

    def _judge(self):
        for t in self.lib.ordered():
            if not t.enabled:
                continue
            missing = [f for f in self.lib.requires(t) if f not in self.arm]
            if missing:
                self.skipped[t.id] = missing
                continue
            for i, (win, ns, numbers) in enumerate(self._cut(t.segment)):
                failed = next((text for text, e in t.when if not e.eval(ns)), None)
                self.verdicts.setdefault((t.segment, i), []).append((t, failed))
                if failed is None:
                    self.hits.append(Hit(t, t.segment, i, win, ns, numbers))

    def _precedence(self):
        best: dict[tuple, Hit] = {}
        for h in self.hits:                                  # file order: a tie goes to the earlier entry
            key = (h.segment, h.index)
            if key not in best or h.trick.priority > best[key].trick.priority:
                best[key] = h
        for h in self.hits:
            if best[(h.segment, h.index)] is not h:
                h.lost_to = best[(h.segment, h.index)].trick.id
        for h in sorted(self.hits, key=lambda h: (-h.trick.priority, h.trick.order, h.window.arm_from)):
            if not h.alive:
                continue
            for rule in h.trick.suppress:
                for o in self.hits:
                    if o is h or not o.alive or o.trick.tag not in rule.tags:
                        continue
                    if rule.margin is not None:
                        if o.window.arm_from <= h.window.arm_to + rule.margin and o.window.arm_to >= h.window.arm_from - rule.margin:
                            o.suppressed_by = h.trick.id
                    elif abs(o.anchor - h.anchor) < rule.within:
                        o.suppressed_by = h.trick.id

    def events(self) -> list[dict]:
        return sorted((h.event() for h in self.hits if h.alive), key=lambda e: (e["arm_from"], e["arm_to"]))


def detect(arm: Arm, lib: Library | None = None) -> list[dict]:
    """The suggestions for an arm: the detections that survived precedence, in time order."""
    return Run(arm, lib or library()).events()


def explain(arm: Arm, lib: Library | None = None, t0: float = 0.0, t1: float = 1e9) -> dict:
    """Every window the segments cut (between t0 and t1) with each trick's verdict on it, and the hits' fate:
    {windows: [{segment, kind, arm_from, arm_to, numbers, tricks: [{id, fired, failed, lost_to, suppressed_by}]}],
    skipped: {trick: [missing fields]}}. What `acroscope tricks explain` prints."""
    run = Run(arm, lib or library())
    fate = {(h.segment, h.index, h.trick.id): h for h in run.hits}
    out = []
    for name, wins in run.windows.items():
        seg = run.lib.segments[name]
        for i, (win, ns, numbers) in enumerate(wins):
            if win.arm_to < t0 or win.arm_from > t1:
                continue
            tricks = []
            for t, failed in run.verdicts.get((name, i), []):
                h = fate.get((name, i, t.id))
                tricks.append({"id": t.id, "fired": failed is None, "failed": failed,
                               "lost_to": h.lost_to if h else None, "suppressed_by": h.suppressed_by if h else None})
            out.append({"segment": name, "kind": seg.kind, "arm_from": win.arm_from, "arm_to": win.arm_to, "numbers": numbers, "tricks": tricks})
    out.sort(key=lambda w: (w["arm_from"], w["arm_to"]))
    return {"windows": out, "skipped": run.skipped}


def check(lib: Library | None = None, only: list[str] | None = None) -> dict:
    """Run the tricks' examples against the logs on this machine: a detection of the trick over `at` (or none over
    from-to when `absent`). {results: [{trick, log, arm, where, status, found}], passed, failed, unchecked}; a log
    that is not here leaves its examples unchecked, never failed."""
    from .. import blackbox
    lib = lib or library()
    results = []
    arms: dict[tuple, list | None] = {}
    for t in lib.ordered():
        if only and t.id not in only:
            continue
        for e in t.examples:
            key = (e["log"], e["arm"])
            if key not in arms:
                try:
                    arms[key] = detect(blackbox.load_arm(e["log"], e["arm"]), lib)
                except FileNotFoundError:
                    arms[key] = None
            events = arms[key]
            where = f"at {e['at']}" if "at" in e else f"{'none in' if e.get('absent') else 'in'} {e['from']}-{e['to']}"
            r = {"trick": t.id, "log": e["log"], "arm": e["arm"], "where": where, "note": e.get("note", "")}
            if events is None:
                r["status"] = "unchecked"
            else:
                if "at" in e:
                    hits = [x for x in events if x["trick"] == t.id and x["arm_from"] - 0.05 <= e["at"] <= x["arm_to"] + 0.05]
                else:
                    hits = [x for x in events if x["trick"] == t.id and x["arm_from"] <= e["to"] and x["arm_to"] >= e["from"]]
                ok = (not hits) if e.get("absent") else bool(hits)
                r["status"] = "ok" if ok else "FAILED"
                r["found"] = "; ".join(f"{x['title']} {x['arm_from']}-{x['arm_to']}" for x in hits)
            results.append(r)
    return {"results": results, "passed": sum(r["status"] == "ok" for r in results),
            "failed": sum(r["status"] == "FAILED" for r in results), "unchecked": sum(r["status"] == "unchecked" for r in results)}
