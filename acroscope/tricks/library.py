"""The tricks file: what the engine looks for, read with the stdlib's tomllib and checked before anything runs.

    version = 1
    [params]                      # the numbers the conditions use; one place to tune
    flip_min_deg = 270
    [segment.fast-rotation]       # a named way to cut an arm into windows (segments.KINDS has the kinds and options)
    kind = "rotation"
    threshold = 100
    [[trick]]                     # one entry per trick
    id = "roll"                   # unique; a later file's entry with the same id changes this one (`enabled = false` turns it off)
    tag = "roll"                  # the tag a suggestion carries (several tricks may share one)
    segment = "fast-rotation"     # or an inline table: { kind = "rotation", threshold = 100 }
    when = ["axis == 'roll'", "abs(rotation_deg.roll) >= flip_min_deg"]     # all must hold (expr.py's language)
    title = "{direction} roll"    # templates over the same names
    why = "roll {rotation_deg.roll:+} deg in {duration} s"
    priority = 10                 # on one window the highest wins; ties go to the earlier entry
    suppress = [{ tags = ["flip"], margin = 1.0 }, { tags = ["motor-loss"], within = 0.5 }]
                                  # a detection of this trick drops those tags' detections: windows overlapping this one
                                  # grown by `margin` s, or point events within `within` s of this one's instant
    requires = ["eRPM[0]"]        # log fields the trick needs beyond its segment's; without them it is skipped
    examples = [{ log = "x.bbl", arm = 20, at = 70.4 }, { log = "x.bbl", arm = 6, from = 10, to = 20, absent = true }]
                                  # `acroscope tricks check` runs them: a detection of this trick over `at`, or none over from-to

A trick that is a series of phases names them instead of one segment:

    [[trick]]
    id = "split-s"
    phases = [
      { segment = "fast-rotation", when = ["150 <= abs(rotation_peak_deg.roll) <= 240"] },
      { segment = "pitch-back", within = 1.0, when = ["rotation_deg.pitch <= -50", "tilt_deg.end <= 75"] },
    ]                             # each phase's `when` sees its own window (and the earlier phases' features as
                                  # p1.*); the next phase starts after the one before it and no later than `within` s
                                  # after it ends. The trick's own `when`, title and why see the whole span plus
                                  # p1.*, p2.* for each phase's features.

Every name a condition or template uses must be a feature of the trick's segment kind (features.vocabulary) or a
param; every segment option must exist for its kind; an unknown key is an error. So a typo fails at load, with
the file and the entry named, never silently as a trick that cannot fire.

Profiles, the layers over the shipped file (a pilot's file: ACROSCOPE_TRICKS, else ~/.config/acroscope/tricks.toml):

    skill = "beginner"            # a preset of params the shipped file defines in [skill.beginner]; your own
                                  # [params] win over it
    [craft.default]               # what any quad is unless its own section says otherwise; the names here are
    size_mm = 65                  # features too (craft.size_mm, craft.name) for conditions that scale with the quad
    [craft."OttoJr"]              # by the log header's Craft name: these arms only
    size_mm = 75
    skill = "advanced"
    [craft."OttoJr".params]       # over everything above, for this quad
    loop_min_dur = 1.2
    [craft."OttoJr".segment.impact]
    g = 5
    [[craft."OttoJr".trick]]
    id = "gyro-kick"
    enabled = false

The effective library for an arm is `for_craft(name)`: the files in order, the chosen preset under the files after
the one that defines it, then the craft's own layer."""
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from . import expr, features, segments

SHIPPED = Path(__file__).with_name("tricks.toml")
VERSION = 1
TRICK_KEYS = {"id", "tag", "segment", "phases", "when", "title", "why", "priority", "suppress", "requires", "enabled", "examples", "note"}
PHASE_KEYS = {"segment", "when", "within"}
TOP_KEYS = {"version", "params", "segment", "trick", "skill", "craft"}
CRAFT_KEYS = {"params", "segment", "trick", "skill"}         # the rest of a craft section are its properties
DEFAULT_CRAFT = "default"
EXAMPLE_KEYS = {"log", "arm", "at", "from", "to", "absent", "note"}


class LibraryError(ValueError):
    pass


@dataclass
class Segment:
    name: str
    kind: str
    opts: dict
    source: str = ""


@dataclass
class Rule:
    tags: tuple
    margin: float | None = None
    within: float | None = None


@dataclass
class Phase:
    segment: str
    when: list = field(default_factory=list)          # [(text, Expr)]
    within: float = 1.0                               # s after the previous phase's end by which this one starts

    @property
    def conditions(self) -> list[str]:
        return [text for text, _ in self.when]


@dataclass
class Trick:
    id: str
    tag: str = ""
    segment: str = ""
    phases: list = field(default_factory=list)        # [Phase]; a sequence trick has these instead of a segment
    when: list = field(default_factory=list)          # [(text, Expr)]
    title: str = ""
    why: str = ""
    priority: int = 0
    suppress: list = field(default_factory=list)      # [Rule]
    requires: list = field(default_factory=list)
    enabled: bool = True
    examples: list = field(default_factory=list)
    note: str = ""
    source: str = ""
    order: int = 0                                    # position in the merged file order

    @property
    def conditions(self) -> list[str]:
        return [text for text, _ in self.when]


def _where(source: str, what: str) -> str:
    return f"{source}: {what}" if source else what


def _number(v, what: str, source: str) -> float:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise LibraryError(_where(source, f"{what} must be a number, not {v!r}"))
    return v


class Library:
    """The merged tricks: params, named segments and tricks in file order. `Library.load(paths)` reads several
    files in turn, each on top of the one before (the shipped file first, then a pilot's)."""

    def __init__(self):
        self.params: dict = {}
        self.segments: dict[str, Segment] = {}
        self.tricks: dict[str, Trick] = {}
        self.sources: list[str] = []
        self.skills: dict[str, dict] = {}                 # preset name -> params
        self.skill: str | None = None                     # the preset in force, and the source that chose it
        self.crafts: dict[str, dict] = {}                 # craft name -> {props, params, segments, tricks, skill}
        self.layers: list = []                            # [(source, params, skill chosen there)] in file order
        self._order = 0
        self._effective: dict = {}

    @classmethod
    def load(cls, paths=(SHIPPED,)) -> "Library":
        lib = cls()
        for p in paths:
            p = Path(p)
            try:
                table = tomllib.loads(p.read_text())
            except tomllib.TOMLDecodeError as e:
                raise LibraryError(f"{p}: {e}") from None
            lib.add(table, str(p))
        lib.validate()
        return lib

    @classmethod
    def from_text(cls, text: str, source: str = "<text>") -> "Library":
        lib = cls()
        try:
            lib.add(tomllib.loads(text), source)
        except tomllib.TOMLDecodeError as e:
            raise LibraryError(f"{source}: {e}") from None
        lib.validate()
        return lib

    # ---- merging ------------------------------------------------------------------------------------------------

    def add(self, table: dict, source: str):
        """One file's tables on top of what is loaded: params update, segments merge by name, tricks merge by id."""
        self.sources.append(source)
        if not isinstance(table, dict):
            raise LibraryError(_where(source, "not a table"))
        version = table.get("version", VERSION)
        if version != VERSION:
            raise LibraryError(_where(source, f"version {version!r}: this acroscope reads version {VERSION}"))
        for key in table:
            if key not in TOP_KEYS:
                raise LibraryError(_where(source, f"unknown top-level key {key!r} (have {', '.join(sorted(TOP_KEYS))})"))
        params = table.get("params", {})
        if not isinstance(params, dict):
            raise LibraryError(_where(source, "params must be a table"))
        for k, v in params.items():
            self.params[k] = v
        skill = table.get("skill")
        chosen = None
        if isinstance(skill, str):                         # a choice: skill = "beginner"
            chosen = skill
            self.skill = skill
        elif isinstance(skill, dict):                      # presets: [skill.beginner] flip_min_deg = 240
            for name, p in skill.items():
                if not isinstance(p, dict):
                    raise LibraryError(_where(source, f"skill.{name} must be a table of params"))
                self.skills.setdefault(name, {}).update(p)
        elif skill is not None:
            raise LibraryError(_where(source, "skill is the name of a preset (skill = \"beginner\") or presets ([skill.beginner])"))
        self.layers.append((source, dict(params), chosen))
        crafts = table.get("craft", {})
        if not isinstance(crafts, dict):
            raise LibraryError(_where(source, "craft must be a table of craft sections"))
        for name, spec in crafts.items():
            self._merge_craft(name, spec, source)
        segs = table.get("segment", {})
        if not isinstance(segs, dict):
            raise LibraryError(_where(source, "segment must be a table of named segments"))
        for name, spec in segs.items():
            if not isinstance(spec, dict):
                raise LibraryError(_where(source, f"segment.{name} must be a table"))
            self._merge_segment(name, spec, source)
        tricks = table.get("trick", [])
        if not isinstance(tricks, list):
            raise LibraryError(_where(source, "trick must be an array of tables ([[trick]])"))
        for i, spec in enumerate(tricks):
            if not isinstance(spec, dict):
                raise LibraryError(_where(source, f"trick #{i + 1} must be a table"))
            self._merge_trick(spec, source)

    def _merge_segment(self, name: str, spec: dict, source: str):
        spec = dict(spec)
        kind = spec.pop("kind", None)
        seg = self.segments.get(name)
        if seg is None:
            if kind is None:
                raise LibraryError(_where(source, f"segment.{name}: needs a kind ({', '.join(segments.KINDS)})"))
            seg = Segment(name, kind, dict(segments.KINDS.get(kind, {})), source)
            self.segments[name] = seg
        elif kind is not None and kind != seg.kind:
            raise LibraryError(_where(source, f"segment.{name}: is a {seg.kind} segment, cannot become {kind}"))
        if seg.kind not in segments.KINDS:
            raise LibraryError(_where(source, f"segment.{name}: unknown kind {seg.kind!r} (have {', '.join(segments.KINDS)})"))
        for k, v in spec.items():
            if k not in segments.KINDS[seg.kind]:
                raise LibraryError(_where(source, f"segment.{name}: a {seg.kind} segment has no option {k!r} (have {', '.join(segments.KINDS[seg.kind])})"))
            if k == "axis":
                if v not in segments.AXES:
                    raise LibraryError(_where(source, f"segment.{name}: axis must be roll, pitch or yaw, not {v!r}"))
            else:
                _number(v, f"segment.{name}.{k}", source)
            seg.opts[k] = v

    def _inline_segment(self, spec: dict, source: str, trick_id: str) -> str:
        if "kind" not in spec:
            raise LibraryError(_where(source, f"trick {trick_id}: an inline segment needs a kind"))
        name = f"{trick_id}:" + ",".join(f"{k}={spec[k]}" for k in sorted(spec))
        self._merge_segment(name, spec, source)
        return name

    def _merge_trick(self, spec: dict, source: str):
        tid = spec.get("id")
        if not isinstance(tid, str) or not tid:
            raise LibraryError(_where(source, f"a trick needs an id: {spec}"))
        bad = set(spec) - TRICK_KEYS
        if bad:
            raise LibraryError(_where(source, f"trick {tid}: unknown key(s) {', '.join(sorted(bad))} (have {', '.join(sorted(TRICK_KEYS))})"))
        t = self.tricks.get(tid)
        if t is None:
            t = Trick(tid, source=source, order=self._order)
            self._order += 1
            self.tricks[tid] = t
        t.source = source
        for key, v in spec.items():
            if key == "id":
                continue
            if key == "segment":
                if isinstance(v, dict):
                    v = self._inline_segment(v, source, tid)
                elif not isinstance(v, str):
                    raise LibraryError(_where(source, f"trick {tid}: segment must be a name or an inline table"))
                t.segment = v
            elif key == "when":
                t.when = self._conditions(v, tid, source)
            elif key == "phases":
                if not isinstance(v, list) or len(v) < 2 or not all(isinstance(ph, dict) for ph in v):
                    raise LibraryError(_where(source, f"trick {tid}: phases must be a list of two or more {{ segment, when, within }} tables"))
                t.phases = [self._phase(ph, k, tid, source) for k, ph in enumerate(v)]
            elif key in ("title", "why", "tag", "note"):
                if not isinstance(v, str):
                    raise LibraryError(_where(source, f"trick {tid}: {key} must be a string"))
                setattr(t, key, v)
            elif key == "priority":
                if isinstance(v, bool) or not isinstance(v, int):
                    raise LibraryError(_where(source, f"trick {tid}: priority must be an integer"))
                t.priority = v
            elif key == "enabled":
                if not isinstance(v, bool):
                    raise LibraryError(_where(source, f"trick {tid}: enabled must be true or false"))
                t.enabled = v
            elif key == "requires":
                if not isinstance(v, list) or not all(isinstance(f, str) for f in v):
                    raise LibraryError(_where(source, f"trick {tid}: requires must be a list of log field names"))
                t.requires = list(v)
            elif key == "suppress":
                t.suppress = [self._rule(r, tid, source) for r in (v if isinstance(v, list) else [v])]
            elif key == "examples":
                if not isinstance(v, list):
                    raise LibraryError(_where(source, f"trick {tid}: examples must be a list of tables"))
                t.examples = [self._example(e, tid, source) for e in v]

    def _merge_craft(self, name: str, spec, source: str):
        if not isinstance(spec, dict):
            raise LibraryError(_where(source, f"craft.{name} must be a table"))
        c = self.crafts.setdefault(name, {"props": {}, "params": {}, "segments": {}, "tricks": [], "skill": None, "source": source})
        c["source"] = source
        for k, v in spec.items():
            if k == "params":
                if not isinstance(v, dict):
                    raise LibraryError(_where(source, f"craft.{name}.params must be a table"))
                c["params"].update(v)
            elif k == "segment":
                if not isinstance(v, dict) or not all(isinstance(o, dict) for o in v.values()):
                    raise LibraryError(_where(source, f"craft.{name}.segment must be a table of segment tables"))
                for sname, opts in v.items():
                    c["segments"].setdefault(sname, {}).update(opts)
            elif k == "trick":
                if not isinstance(v, list) or not all(isinstance(t, dict) for t in v):
                    raise LibraryError(_where(source, f"craft.{name}.trick must be an array of tables ([[craft.\"{name}\".trick]])"))
                c["tricks"].extend(v)
            elif k == "skill":
                if not isinstance(v, str):
                    raise LibraryError(_where(source, f"craft.{name}.skill must be the name of a preset"))
                c["skill"] = v
            else:
                if isinstance(v, bool) or not isinstance(v, (int, float, str)):
                    raise LibraryError(_where(source, f"craft.{name}.{k}: a craft property is a number or a string, not {v!r}"))
                c["props"][k] = v

    def craft_properties(self) -> set[str]:
        """Every property name any craft section declares (plus `name`): the craft.* features a condition may use."""
        return {"name"} | {k for c in self.crafts.values() for k in c["props"]}

    def _conditions(self, v, tid: str, source: str) -> list:
        if not isinstance(v, list) or not all(isinstance(c, str) for c in v):
            raise LibraryError(_where(source, f"trick {tid}: when must be a list of conditions"))
        try:
            return [(c, expr.compile(c)) for c in v]
        except expr.ExprError as e:
            raise LibraryError(_where(source, f"trick {tid}: {e}")) from None

    def _phase(self, ph: dict, k: int, tid: str, source: str) -> Phase:
        bad = set(ph) - PHASE_KEYS
        if bad or "segment" not in ph:
            raise LibraryError(_where(source, f"trick {tid}: phase {k + 1} is {{ segment = ..., when = [...], within = s }}, not {ph!r}"))
        seg = ph["segment"]
        if isinstance(seg, dict):
            seg = self._inline_segment(seg, source, f"{tid}.{k + 1}")
        elif not isinstance(seg, str):
            raise LibraryError(_where(source, f"trick {tid}: phase {k + 1}: segment must be a name or an inline table"))
        within = ph.get("within", 1.0)
        if k == 0 and "within" in ph:
            raise LibraryError(_where(source, f"trick {tid}: the first phase has no `within`"))
        return Phase(seg, self._conditions(ph.get("when", []), tid, source), _number(within, f"trick {tid}: phase {k + 1} within", source))

    def _rule(self, r, tid: str, source: str) -> Rule:
        if not isinstance(r, dict) or not isinstance(r.get("tags"), list) or not r["tags"] or set(r) - {"tags", "margin", "within"}:
            raise LibraryError(_where(source, f"trick {tid}: a suppress rule is {{ tags = [...], margin = s }} or {{ tags = [...], within = s }}, not {r!r}"))
        if ("margin" in r) == ("within" in r):
            raise LibraryError(_where(source, f"trick {tid}: a suppress rule has either margin or within"))
        margin = _number(r["margin"], f"trick {tid}: margin", source) if "margin" in r else None
        within = _number(r["within"], f"trick {tid}: within", source) if "within" in r else None
        return Rule(tuple(r["tags"]), margin, within)

    def _example(self, e, tid: str, source: str) -> dict:
        if not isinstance(e, dict) or set(e) - EXAMPLE_KEYS or not isinstance(e.get("log"), str) or not isinstance(e.get("arm"), int):
            raise LibraryError(_where(source, f"trick {tid}: an example is {{ log = \"file.bbl\", arm = N, at = s }} (or from/to, absent), not {e!r}"))
        if ("at" in e) == ("from" in e or "to" in e) or ("from" in e) != ("to" in e):
            raise LibraryError(_where(source, f"trick {tid}: an example has `at`, or `from` and `to`: {e!r}"))
        for k in ("at", "from", "to"):
            if k in e:
                _number(e[k], f"trick {tid}: example {k}", source)
        return dict(e)

    # ---- checking -------------------------------------------------------------------------------------------------

    def validate(self):
        self._effective = {}
        for group in [self.params, *self.skills.values(), *(c["params"] for c in self.crafts.values())]:
            for k, v in group.items():
                if not isinstance(k, str) or "." in k or k in features.WINDOW or k in features.ARM:
                    raise LibraryError(f"param {k!r}: a param is a plain name that is not a feature")
                if isinstance(v, bool) or not isinstance(v, (int, float, str)):
                    raise LibraryError(f"param {k!r}: must be a number or a string, not {v!r}")
        for chosen, where in [(self.skill, "skill"), *((c["skill"], f"craft.{n}.skill") for n, c in self.crafts.items())]:
            if chosen is not None and chosen not in self.skills:
                raise LibraryError(f"{where} = {chosen!r}: no such preset (have {', '.join(self.skills) or 'none'})")
        self._check_tricks(self)
        for name in self.crafts:
            self._check_tricks(self._derive(name))

    def _check_tricks(self, lib: "Library"):
        crafts = {f"craft.{p}" for p in self.craft_properties()}
        for t in lib.tricks.values():
            w = f"trick {t.id}"
            if not t.tag:
                raise LibraryError(_where(t.source, f"{w}: needs a tag"))
            if t.phases and t.segment:
                raise LibraryError(_where(t.source, f"{w}: has phases, so no segment of its own"))
            if not t.title:
                raise LibraryError(_where(t.source, f"{w}: needs a title"))
            base = set(lib.params) | crafts
            if t.phases:
                known = set(features.WINDOW) | set(features.ARM) | base
                earlier = set()
                for k, ph in enumerate(t.phases):
                    if ph.segment not in lib.segments:
                        raise LibraryError(_where(t.source, f"{w}: phase {k + 1}: unknown segment {ph.segment!r} (have {', '.join(lib.segments) or 'none'})"))
                    own = set(features.vocabulary(lib.segments[ph.segment].kind))
                    for text, e in ph.when:
                        self._check_names(e, own | earlier | base, f"{w}: phase {k + 1} condition {text!r}", t.source)
                    earlier |= {f"p{k + 1}.{n}" for n in own}
                known |= earlier
            else:
                if t.segment not in lib.segments:
                    raise LibraryError(_where(t.source, f"{w}: unknown segment {t.segment!r} (have {', '.join(lib.segments) or 'none'})"))
                known = set(features.vocabulary(lib.segments[t.segment].kind)) | base
            for text, e in t.when:
                self._check_names(e, known, f"{w}: condition {text!r}", t.source)
            for tpl, what in ((t.title, "title"), (t.why, "why")):
                try:
                    holes = expr.template_exprs(tpl)
                except expr.ExprError as err:
                    raise LibraryError(_where(t.source, f"{w}: {what}: {err}")) from None
                for e, _ in holes:
                    self._check_names(e, known, f"{w}: {what} {tpl!r}", t.source)

    @staticmethod
    def _check_names(e: expr.Expr, known: set, what: str, source: str):
        unknown = sorted(e.names - known)
        if unknown:
            raise LibraryError(_where(source, f"{what}: unknown name {unknown[0]!r} (not a feature of this segment kind, not a param)"))

    # ---- the effective library for an arm ---------------------------------------------------------------------------

    def effective_params(self, craft: str | None = None) -> dict:
        """The params in force for a craft: the files in order with the chosen preset over the first (the shipped
        defaults) and under every later one, then the default craft's params, then the craft's own."""
        default, own = self.crafts.get(DEFAULT_CRAFT, {}), self.crafts.get(craft, {}) if craft is not None else {}
        chosen = own.get("skill") or default.get("skill") or self.skill
        out = {}
        for i, (source, params, _) in enumerate(self.layers):
            out.update(params)
            if i == 0 and chosen:
                out.update(self.skills.get(chosen, {}))
        out.update(default.get("params", {}))
        out.update(own.get("params", {}))
        return out

    def _derive(self, craft: str | None) -> "Library":
        """A copy of this library with the default craft's layer and then the craft's own applied (params,
        segment options, trick overrides)."""
        import copy
        lib = Library()
        lib.sources, lib.skills, lib.skill, lib.layers, lib.crafts = self.sources, self.skills, self.skill, self.layers, self.crafts
        lib.segments = {n: Segment(s.name, s.kind, dict(s.opts), s.source) for n, s in self.segments.items()}
        lib.tricks = {tid: copy.copy(t) for tid, t in self.tricks.items()}
        for t in lib.tricks.values():
            t.when, t.suppress, t.requires, t.examples = list(t.when), list(t.suppress), list(t.requires), list(t.examples)
        lib._order = self._order
        lib.params = self.effective_params(craft)
        for name in (DEFAULT_CRAFT, craft):
            c = self.crafts.get(name) if name is not None else None
            if not c:
                continue
            where = f"{c['source']} craft.{name}"
            for sname, opts in c["segments"].items():
                lib._merge_segment(sname, opts, where)
            for spec in c["tricks"]:
                lib._merge_trick(spec, where)
        return lib

    def craft_context(self, craft: str | None) -> dict:
        """The craft.* features for an arm: the default section's properties under its own section's."""
        props = dict(self.crafts.get(DEFAULT_CRAFT, {}).get("props", {}))
        if craft in self.crafts:
            props.update(self.crafts[craft]["props"])
        out = {f"craft.{p}": None for p in self.craft_properties()}
        out.update({f"craft.{k}": v for k, v in props.items()})
        out["craft.name"] = craft
        return out

    def for_craft(self, craft: str | None) -> "Library":
        """The library in force for an arm of this craft (by the log header's Craft name), derived once per craft."""
        key = craft if craft in self.crafts else None
        if key not in self._effective:
            self._effective[key] = self._derive(key)
        return self._effective[key]

    # ---- views --------------------------------------------------------------------------------------------------

    def ordered(self) -> list[Trick]:
        return sorted(self.tricks.values(), key=lambda t: t.order)

    def requires(self, t: Trick) -> list[str]:
        """The log fields a trick needs: its segments' kinds' and its own."""
        kinds = [self.segments[ph.segment].kind for ph in t.phases] if t.phases else [self.segments[t.segment].kind]
        return list(dict.fromkeys([f for k in kinds for f in segments.REQUIRES.get(k, [])] + t.requires))
