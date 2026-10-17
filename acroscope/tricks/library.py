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

Every name a condition or template uses must be a feature of the trick's segment kind (features.vocabulary) or a
param; every segment option must exist for its kind; an unknown key is an error. So a typo fails at load, with
the file and the entry named, never silently as a trick that cannot fire."""
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from . import expr, features, segments

SHIPPED = Path(__file__).with_name("tricks.toml")
VERSION = 1
TRICK_KEYS = {"id", "tag", "segment", "when", "title", "why", "priority", "suppress", "requires", "enabled", "examples", "note"}
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
class Trick:
    id: str
    tag: str = ""
    segment: str = ""
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
        self._order = 0

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
            if key not in ("version", "params", "segment", "trick"):
                raise LibraryError(_where(source, f"unknown top-level key {key!r} (have version, params, segment, trick)"))
        params = table.get("params", {})
        if not isinstance(params, dict):
            raise LibraryError(_where(source, "params must be a table"))
        for k, v in params.items():
            self.params[k] = v
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
                if not isinstance(v, list) or not all(isinstance(c, str) for c in v):
                    raise LibraryError(_where(source, f"trick {tid}: when must be a list of conditions"))
                try:
                    t.when = [(c, expr.compile(c)) for c in v]
                except expr.ExprError as e:
                    raise LibraryError(_where(source, f"trick {tid}: {e}")) from None
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
        for k, v in self.params.items():
            if not isinstance(k, str) or "." in k or k in features.WINDOW or k in features.ARM:
                raise LibraryError(f"param {k!r}: a param is a plain name that is not a feature")
            if isinstance(v, bool) or not isinstance(v, (int, float, str)):
                raise LibraryError(f"param {k!r}: must be a number or a string, not {v!r}")
        for t in self.tricks.values():
            w = f"trick {t.id}"
            if not t.tag:
                raise LibraryError(_where(t.source, f"{w}: needs a tag"))
            if t.segment not in self.segments:
                raise LibraryError(_where(t.source, f"{w}: unknown segment {t.segment!r} (have {', '.join(self.segments) or 'none'})"))
            if not t.title:
                raise LibraryError(_where(t.source, f"{w}: needs a title"))
            known = set(features.vocabulary(self.segments[t.segment].kind)) | set(self.params)
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

    # ---- views --------------------------------------------------------------------------------------------------

    def ordered(self) -> list[Trick]:
        return sorted(self.tricks.values(), key=lambda t: t.order)

    def requires(self, t: Trick) -> list[str]:
        """The log fields a trick needs: its segment kind's and its own."""
        return list(dict.fromkeys(segments.REQUIRES.get(self.segments[t.segment].kind, []) + t.requires))
