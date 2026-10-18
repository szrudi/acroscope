"""Trick detection: an engine plus a tricks file (#22).

The engine cuts an arm into candidate windows (segments.py), measures each (features.py: one vocabulary, the same as
`metrics.summary`), and judges the windows by the tricks file (library.py: one TOML entry per trick, conditions in a
small language of its own, expr.py). `detect(arm)` gives the suggestions as `events` always did: [{tag, arm_from,
arm_to, title, why, numbers}], plus `trick`, the entry that fired. `explain(arm)` shows every window and what each
trick made of it, for tuning.

Precedence is in the file, not in code: on one window the trick with the highest `priority` wins; a detection
`suppress`es others by tag (overlapping windows, or point events within so many seconds), judged in priority order.

A sequence trick (`phases`) chains windows of several segments: each phase's conditions on its own window, the next
phase starting after the one before and within so many seconds of its end; the detection spans them all and its
texts see every phase's features as p1.*, p2.*.
"""
import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path

from ..blackbox import Arm
from . import expr, features, segments
from .library import SHIPPED, Library, LibraryError, Trick

__all__ = ["check", "describe", "detect", "evaluate", "explain", "export_toml", "import_table", "library", "merge_tables", "Library", "LibraryError", "SHIPPED"]

# The pilot's layer kept in the database (#45): a callable giving ({table}, stamp) or (None, None). The server and the
# local CLI set it from their store at start; nothing in this package reaches for a database on its own.
LAYER_SOURCE = None
LAYER_KEY = "tricks"
LAYER_ERROR = None          # why the database layer is not in force, when a later release made a stored one invalid


_cache: tuple[tuple, Library] | None = None


def library_paths() -> list[Path]:
    """The shipped file; the pilot's layer comes from the database (LAYER_SOURCE), a tricks file is for sharing."""
    return [SHIPPED]


def database_layer() -> tuple:
    """(the pilot's layer from the database, its stamp), or (None, None) without one."""
    return LAYER_SOURCE() if LAYER_SOURCE else (None, None)


def library() -> Library:
    """The tricks in force: the shipped file and the pilot's layer from the database. Loaded once and again whenever
    either changed (the server keeps running; a pilot edits and refreshes)."""
    global _cache
    paths = [p for p in library_paths() if p.is_file()]
    global LAYER_ERROR
    table, db_stamp = database_layer()
    # the layer's content is in the stamp too: two writes within one second share a time
    stamp = tuple((str(p), p.stat().st_mtime_ns) for p in paths) + (("database", db_stamp, json.dumps(table, sort_keys=True)),)
    if _cache is None or _cache[0] != stamp:
        try:
            lib, LAYER_ERROR = Library.load(paths, [(table, "database")] if table else []), None
        except LibraryError as e:
            # a stored layer a later release made invalid (a param renamed): the shipped tricks stay in force, the
            # page shows the reason and the raw layer to repair or clear
            lib, LAYER_ERROR = Library.load(paths), str(e)
        _cache = (stamp, lib)
    return _cache[1]


def layer_etag(table) -> str:
    """A short hash of a layer document: what a writer says it started from, so two writers do not lose each
    other's changes (`updated_at` is whole seconds)."""
    return hashlib.sha1(json.dumps(table or {}, sort_keys=True).encode()).hexdigest()[:12]


def check_layer(table: dict) -> Library:
    """The library the files plus this layer would make, or a LibraryError saying what is wrong with it."""
    paths = [p for p in library_paths() if p.is_file()]
    return Library.load(paths, [(table, "database")])


def describe(lib: Library, craft: str | None = None) -> dict:
    """The library in force as the player and `tricks --json` see it: every param with its default and where its
    value came from, the presets, the crafts (declared, and seen in the logs), the segments, the tricks with their
    definitions and sources, the feature names per segment kind."""
    from .. import blackbox
    eff = lib.for_craft(craft)
    sources = lib.param_sources(craft)
    shipped = lib.layers[0][1] if lib.layers else {}
    seen: dict[str, str] = {}                        # by case-folded name: the declared spelling, else the log's first
    for p in blackbox.all_bbls():
        idx = blackbox.index_bbl(p.name, cached_only=True)
        for a in (idx or {}).get("arms", []):
            name = (a.get("headers") or {}).get("Craft name")
            if name:
                seen.setdefault(name.casefold(), lib.craft_name(name))
    return {
        "sources": lib.sources, "database": "database" in lib.sources, "layer_error": LAYER_ERROR,
        "skill": lib.skill, "skills": lib.skills, "craft": craft,
        "params": {k: {"value": v, "default": shipped.get(k), "source": sources.get(k, "shipped")} for k, v in eff.params.items()},
        "crafts": {n: {**c["props"], "skill": c["skill"], "params": c["params"], "segment": c["segments"], "trick": c["tricks"], "source": c["source"]}
                   for n, c in lib.crafts.items()},
        "crafts_seen": sorted(seen.values()),
        "craft_section": lib.craft_name(craft) if craft is not None and lib.craft_name(craft) in lib.crafts else None,
        "craft_context": lib.craft_context(craft),
        "segments": {n: {"kind": sg.kind, **sg.opts} for n, sg in eff.segments.items()},
        "tricks": [{"id": t.id, "tag": t.tag, "segment": t.segment or None, "priority": t.priority, "enabled": t.enabled,
                    "phases": [{"segment": ph.segment, "within": ph.within, "when": ph.conditions} for ph in t.phases],
                    "when": t.conditions, "title": t.title, "why": t.why, "requires": eff.requires(t), "note": t.note,
                    "suppress": [{"tags": list(r.tags), "margin": r.margin, "within": r.within} for r in t.suppress],
                    "examples": t.examples, "source": t.source,
                    "params": sorted({n for _, e in [*t.when, *(c for ph in t.phases for c in ph.when)] for n in e.names if n in eff.params})}
                   for t in eff.ordered()],
        "features": {k: features.vocabulary(k) for k in segments.KINDS},
    }


def export_toml(table: dict | None, note: str = "") -> str:
    """The pilot's layer as a tricks file others can read: what `acroscope tricks export` and the page's export give."""
    from datetime import date
    from . import toml_out
    header = f"acroscope tricks, exported {date.today().isoformat()}" + (f": {note}" if note else "") + \
        "\nA layer over the shipped acroscope/tricks/tricks.toml: import it with `acroscope tricks import` or the configuration page."
    t = dict(table or {})
    t = {"version": t.pop("version", 1), **t}
    return toml_out.dumps(t, header)


def craft_key(table: dict | None, name: str) -> str:
    """The spelling a layer document already uses for this craft (any case), else the name as given."""
    crafts = (table or {}).get("craft") or {}
    if name in crafts:
        return name
    return next((k for k in crafts if k.casefold() == name.casefold()), name)


def merge_tables(base: dict | None, top: dict | None) -> dict:
    """One layer document over another, as the library would stack them: params and properties update, the skill
    is the top's when it has one, tricks merge by id, craft sections and their segments by name."""
    import copy
    out = copy.deepcopy(base) if base else {"version": 1}
    top = top or {}
    for k, v in top.items():
        if k == "params" and isinstance(v, dict):
            out.setdefault("params", {}).update(v)
        elif k == "trick" and isinstance(v, list):
            have = out.setdefault("trick", [])
            for item in v:
                tid = item.get("id") if isinstance(item, dict) else None
                mine = next((x for x in have if isinstance(x, dict) and x.get("id") == tid), None) if tid is not None else None
                if mine is None:
                    have.append(copy.deepcopy(item))
                else:
                    mine.update(copy.deepcopy(item))
        elif k == "craft" and isinstance(v, dict):
            crafts = out.setdefault("craft", {})
            for name, sec in v.items():
                name = craft_key(out, name)
                crafts[name] = merge_tables(crafts.get(name), sec) if isinstance(sec, dict) else copy.deepcopy(sec)
                crafts[name].pop("version", None)
        elif k == "segment" and isinstance(v, dict):
            segs = out.setdefault("segment", {})
            for name, opts in v.items():
                segs.setdefault(name, {}).update(opts) if isinstance(opts, dict) else segs.__setitem__(name, copy.deepcopy(opts))
        elif k == "version":
            out["version"] = v
        else:
            out[k] = copy.deepcopy(v)
    return out


def import_table(text: str, current: dict | None, mode: str = "replace") -> dict:
    """A tricks file's text as the pilot's new layer: `replace` takes it whole, `merge` puts it over the current
    layer. Checked as a file is (check_layer); returns the table to store."""
    import tomllib
    try:
        table = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise LibraryError(f"not a tricks file: {e}") from None
    if mode not in ("replace", "merge"):
        raise ValueError(f"import: mode is replace or merge, not {mode!r}")
    _json_only(table)
    table = merge_tables(current, table) if mode == "merge" else merge_tables(None, table)    # one spelling per craft either way
    check_layer(table)
    return table


def _json_only(v, where="the file"):
    """TOML has dates and times; a layer is stored as JSON and the tricks need neither."""
    if isinstance(v, dict):
        for k, x in v.items():
            _json_only(x, f"{where}.{k}")
    elif isinstance(v, list):
        for x in v:
            _json_only(x, where)
    elif v is not None and not isinstance(v, (str, int, float, bool)):
        raise LibraryError(f"{where}: a {type(v).__name__} is not a value a tricks file uses here")


def edit_layer(table: dict | None, what: str, *args, craft: str | None = None) -> dict:
    """One change to the pilot's layer, as the CLI makes it: set/unset a param, a trick on/off, the skill. Returns
    the new table (not yet checked or stored)."""
    import copy
    t = copy.deepcopy(table) if table else {"version": 1}
    where = t
    if craft:
        craft = craft_key(t, craft)
        where = t.setdefault("craft", {}).setdefault(craft, {})
    if what == "set":
        name, value = args
        where.setdefault("params", {})[name] = value
    elif what == "unset":
        (name,) = args
        if name not in where.get("params", {}):
            raise ValueError(f"{name}: not set in the layer" + (f" for {craft}" if craft else "") + " (acroscope tricks layer shows what is)")
        del where["params"][name]
        if not where["params"]:
            del where["params"]
    elif what in ("on", "off"):
        (tid,) = args
        entries = where.setdefault("trick", [])
        entry = next((x for x in entries if isinstance(x, dict) and x.get("id") == tid), None)
        if entry is None:
            entry = {"id": tid}
            entries.append(entry)
        entry["enabled"] = what == "on"                        # explicit either way: on for one quad while off below works too
    elif what == "skill":
        (name,) = args
        if name in (None, "none", ""):
            where.pop("skill", None)
        else:
            where["skill"] = name
    else:
        raise ValueError(f"edit_layer: {what}")
    if craft and not where:
        del t["craft"][craft]
        if not t["craft"]:
            del t["craft"]
    return t


def parse_value(text: str):
    """A param value as typed on the command line: a number, true/false, else the text."""
    if text in ("true", "false"):
        return text == "true"
    try:
        return int(text)
    except ValueError:
        pass
    try:
        return float(text)
    except ValueError:
        return text


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

    title: str = ""
    why: str = ""

    def event(self) -> dict:
        return {"trick": self.trick.id, "tag": self.trick.tag, "arm_from": self.window.arm_from, "arm_to": self.window.arm_to,
                "title": self.title, "why": self.why, "numbers": self.numbers}


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
        self.errors: dict[str, str] = {}             # trick id -> an expression that failed to evaluate (its verdict fails, nothing else)
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
            if t.phases:
                self._judge_sequence(t)
                continue
            for i, (win, ns, numbers) in enumerate(self._cut(t.segment)):
                failed, hit = self._verdict(t, t.when, ns)
                self.verdicts.setdefault((t.segment, i), []).append((t, failed))
                if hit:
                    self.hits.append(Hit(t, t.segment, i, win, ns, numbers, title=hit[0], why=hit[1]))

    def _verdict(self, t: Trick, when: list, ns: dict):
        """(the first failing condition's text or None, (title, why) when every condition held). An expression that
        cannot be evaluated on this window (a condition the loader could not foresee, a spec a value cannot take)
        fails the trick with its message, and nothing else."""
        try:
            failed = next((text for text, e in when if not e.eval(ns)), None)
            if failed is not None:
                return failed, None
            return None, (expr.render(t.title, ns), expr.render(t.why, ns))
        except expr.ExprError as e:
            self.errors[t.id] = str(e)
            return f"error: {e}", None

    def _judge_sequence(self, t: Trick):
        """Every chain of windows, one per phase in order, each passing its phase's conditions (with the earlier
        phases' features as p1.* …), the next starting after the previous and within `within` s of its end."""
        def extend(k: int, prev, prev_i: int, earlier: dict):
            """The first chain from phase k on, after window `prev` of phase k-1; None when none completes."""
            ph = t.phases[k]
            kind = self.lib.segments[ph.segment].kind
            for i, (win, ns, numbers) in enumerate(self._cut(ph.segment)):
                if prev is not None:
                    # the next phase starts after the one before it (never the same window again) and within reach
                    if win.i0 <= prev.i0 or win.arm_from > prev.arm_to + ph.within:
                        continue
                ns2 = {**ns, **earlier}
                try:
                    if any(not e.eval(ns2) for _, e in ph.when):
                        continue
                except expr.ExprError as e:
                    self.errors[t.id] = str(e)
                    return None
                here = {**earlier, **{f"p{k + 1}.{n}": v for n, v in ns.items() if n in features.phase_names(kind)}}
                if k + 1 == len(t.phases):
                    return [(i, win, numbers)], here
                rest = extend(k + 1, win, i, here)
                if rest:
                    return [(i, win, numbers), *rest[0]], rest[1]
            return None
        ended = set()                                           # the last window of a chain that fired: not a new first
        for i0, (first, ns0, numbers0) in enumerate(self._cut(t.phases[0].segment)):
            if (t.phases[0].segment, i0) in ended:
                continue
            # the first phase is window i0 itself: judge it, then chain on from it
            ph = t.phases[0]
            try:
                if any(not e.eval(ns0) for _, e in ph.when):
                    continue
            except expr.ExprError as e:
                self.errors[t.id] = str(e)
                break
            here = {f"p1.{n}": v for n, v in ns0.items() if n in features.phase_names(self.lib.segments[ph.segment].kind)}
            rest = extend(1, first, i0, here) if len(t.phases) > 1 else ([], here)
            if not rest:
                continue
            chain, feats = [(i0, first, numbers0), *rest[0]], rest[1]
            last = chain[-1][1]
            span = segments.Window(first.i0, max(w.i1 for _, w, _ in chain), first.arm_from, max(w.arm_to for _, w, _ in chain))
            flat, numbers = features.measure(self.arm, span, "span", {})
            numbers["phases"] = [n for _, _, n in chain]
            ns = {**self.context, **flat, **feats}
            key = f"{t.id}:phases"
            index = tuple(i for i, _, _ in chain)
            failed, hit = self._verdict(t, t.when, ns)
            self.verdicts.setdefault((key, index), []).append((t, failed))
            if hit:
                self.hits.append(Hit(t, key, index, span, ns, numbers, title=hit[0], why=hit[1]))
                ended.add((t.phases[-1].segment, chain[-1][0]))

    def _precedence(self):
        best: dict[tuple, Hit] = {}
        for h in self.hits:                                  # file order: a tie goes to the earlier entry
            key = (h.segment, h.index)
            if key not in best or h.trick.priority > best[key].trick.priority:
                best[key] = h
        for h in self.hits:
            if best[(h.segment, h.index)] is not h:
                h.lost_to = best[(h.segment, h.index)].trick.id
        order = sorted((h for h in self.hits if h.lost_to is None), key=lambda h: (-h.trick.priority, h.trick.order, h.window.arm_from))
        # suppression to a fixed point, the higher priority judged first: a hit that is itself suppressed
        # suppresses nothing (A over B over C leaves A and C, not A alone)
        suppressed: dict[int, str] = {}
        for _ in range(len(order) + 1):
            new: dict[int, str] = {}
            for h in order:
                if id(h) in suppressed or id(h) in new:
                    continue
                for rule in h.trick.suppress:
                    for o in order:
                        if o is h or id(o) in new or o.trick.tag not in rule.tags:
                            continue
                        if rule.margin is not None:
                            caught = o.window.arm_from <= h.window.arm_to + rule.margin and o.window.arm_to >= h.window.arm_from - rule.margin
                        elif rule.within is not None:
                            caught = abs(o.anchor - h.anchor) < rule.within
                        else:                                    # the other starts from so long after this one's instant on
                            caught = o.window.arm_from >= h.anchor + rule.start_after
                        if caught:
                            new[id(o)] = h.trick.id
            if new == suppressed:
                break
            suppressed = new
        for h in order:
            h.suppressed_by = suppressed.get(id(h))

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
    for h in run.hits:                                              # a sequence's chains, as windows of their own
        if h.trick.phases and h.window.arm_to >= t0 and h.window.arm_from <= t1:
            out.append({"segment": h.segment, "kind": "phases", "arm_from": h.window.arm_from, "arm_to": h.window.arm_to, "numbers": h.numbers,
                        "tricks": [{"id": h.trick.id, "fired": True, "failed": None, "lost_to": h.lost_to, "suppressed_by": h.suppressed_by}]})
    out.sort(key=lambda w: (w["arm_from"], w["arm_to"]))
    return {"windows": out, "skipped": run.skipped}


TOLERANCE = 0.5             # s: an automatch offset is good to about half a second

# a moment's tag -> the event tags that count as finding it (a barrel roll finds a moment tagged roll)
FOUND_BY = {"flip": {"flip"}, "backflip": {"flip"}, "frontflip": {"flip"}, "roll": {"roll", "barrel-roll"}, "barrel-roll": {"barrel-roll"},
            "barrel": {"barrel-roll"}, "powerloop": {"powerloop"}, "split-s": {"split-s"}, "dive": {"dive"}, "orbit": {"orbit"},
            "crash": {"crash"}, "gyro-kick": {"gyro-kick"}, "motor-loss": {"motor-loss"}}


def evaluate(session: dict, load_arm, lib: Library | None = None) -> dict:
    """The tricks against a session's tagged moments (`acroscope tricks eval`): for every tag a trick emits, the
    moments found (a detection of that tag overlapping the moment, in video time) and missed, and the detections of
    trick tags on no tagged moment (on a session where every trick is tagged, the false positives). A moment on a
    clip without a decoded match is not counted."""
    lib = lib or library()
    emits = {t.tag for t in lib.ordered() if t.enabled}
    events: dict[str, list] = {}                                    # video -> [{tag, start, end, title, arm, arm_from, why}]
    for m in session.get("matches", []):
        try:
            arm = load_arm(m["bbl"], m["arm"])
        except FileNotFoundError:
            continue
        events.setdefault(m["video"], [])
        for e in detect(arm, lib):
            events.setdefault(m["video"], []).append({"tag": e["tag"], "start": round(e["arm_from"] + m["offset"], 2), "end": round(e["arm_to"] + m["offset"], 2),
                                                      "title": e["title"], "arm": m["arm"], "arm_from": e["arm_from"], "why": e["why"]})
    found, missed, used = {}, [], set()
    for mo in sorted(session.get("moments", []), key=lambda m: (m["video"], m["start"])):
        if mo["video"] not in events:
            continue
        canon = {}                                                  # one count per moment per emitted tag (flip and backflip are one)
        for tag in mo.get("tags", []):
            by = FOUND_BY.get(tag, {tag}) & emits
            if by:
                canon.setdefault(tag if tag in emits else sorted(by)[0], by)
        for tag, by in canon.items():
            # one detection credits one moment (the first it overlaps, in time order), within the automatch tolerance
            hit = next((e for e in events[mo["video"]] if id(e) not in used and e["tag"] in by
                        and e["start"] <= mo["end"] + TOLERANCE and e["end"] >= mo["start"] - TOLERANCE), None)
            row = {"id": mo["id"], "video": mo["video"], "start": mo["start"], "end": mo["end"], "title": mo["title"], "tag": tag}
            if hit:
                found.setdefault(tag, []).append({**row, "found": [hit["title"]]})
                used.add(id(hit))
            else:
                arms = {m["arm"]: round(mo["start"] - m["offset"], 1) for m in session["matches"] if m["video"] == mo["video"]}
                missed.append({**row, "arms": arms})
    untagged = []
    for video, evs in events.items():
        for e in evs:
            if id(e) in used or e["tag"] in ("crash", "gyro-kick", "motor-loss"):
                continue
            near = [mo for mo in session.get("moments", []) if mo["video"] == video and mo["start"] <= e["end"] and mo["end"] >= e["start"]]
            untagged.append({**e, "video": video, "moment": f"{near[0]['title']} {near[0].get('tags')}" if near else None})
    per_tag = {}
    for tag in sorted(emits):
        n_found, n_missed = len(found.get(tag, [])), sum(1 for m in missed if m["tag"] == tag)
        if n_found + n_missed:
            per_tag[tag] = {"found": n_found, "missed": n_missed}
    return {"per_tag": per_tag, "found": found, "missed": missed, "untagged": sorted(untagged, key=lambda e: (e["video"], e["start"]))}


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
