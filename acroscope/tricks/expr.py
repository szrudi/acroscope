"""The condition language of the tricks file: arithmetic and comparisons over a window's features.

    150 <= abs(rotation_deg.roll) <= 300 and throttle_pct.min < 35
    max(peak_rate_dps.roll, peak_rate_dps.pitch) <= tumble_dps

Numbers, strings in quotes, names with dots, + - * / % **, the comparisons (chained like Python's), `and` `or`
`not`, parentheses, and the functions abs min max round sqrt. Nothing else: no attribute access, no indexing, no
Python. A trick file from the internet must not run code, so this is a parser and a walker, never `eval`.

A feature the log cannot give (no eRPM, no setpoint) is `none`: arithmetic on it gives none, a comparison with it is
false, so a condition on a missing feature never fires and never crashes. `{expr}` and `{expr:fmt}` in a title or a
`why` take the same language (`render`)."""
import math
import re

__all__ = ["Expr", "ExprError", "compile", "render", "template_exprs"]

_TOKEN = re.compile(r"""\s*(?:
    (?P<num>\d+\.\d*|\.\d+|\d+)
  | (?P<name>[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z0-9_]+)*)
  | (?P<str>'[^']*'|"[^"]*")
  | (?P<op>\*\*|<=|>=|==|!=|[-+*/%<>(),])
)""", re.VERBOSE)
_KEYWORDS = {"and", "or", "not", "true", "false", "none"}
_FUNCTIONS = {"abs": abs, "min": min, "max": max, "round": round, "sqrt": math.sqrt}
_CMP = {"<", "<=", ">", ">=", "==", "!="}
_TEMPLATE = re.compile(r"\{([^{}]*)\}")


class ExprError(ValueError):
    pass


def _tokens(text: str) -> list[tuple[str, str]]:
    out, pos = [], 0
    while pos < len(text):
        m = _TOKEN.match(text, pos)
        if not m or m.end() == pos:
            rest = text[pos:].lstrip()
            if not rest:
                break
            raise ExprError(f"cannot read {rest[:12]!r} in {text!r}")
        pos = m.end()
        kind = m.lastgroup
        out.append((kind, m.group(kind)))
    return out


class _Parser:
    def __init__(self, text: str):
        self.text = text
        self.toks = _tokens(text)
        self.i = 0

    def peek(self, kind=None, value=None):
        if self.i >= len(self.toks):
            return None
        k, v = self.toks[self.i]
        if (kind is None or k == kind) and (value is None or v == value):
            return v
        return None

    def take(self, kind=None, value=None):
        v = self.peek(kind, value)
        if v is None:
            want = value or kind or "more"
            got = self.toks[self.i][1] if self.i < len(self.toks) else "the end"
            raise ExprError(f"expected {want}, got {got} in {self.text!r}")
        self.i += 1
        return v

    def parse(self):
        node = self.disjunction()
        if self.i != len(self.toks):
            raise ExprError(f"unexpected {self.toks[self.i][1]!r} in {self.text!r}")
        return node

    def disjunction(self):
        parts = [self.conjunction()]
        while self.peek("name", "or"):
            self.take()
            parts.append(self.conjunction())
        return parts[0] if len(parts) == 1 else ("or", parts)

    def conjunction(self):
        parts = [self.negation()]
        while self.peek("name", "and"):
            self.take()
            parts.append(self.negation())
        return parts[0] if len(parts) == 1 else ("and", parts)

    def negation(self):
        if self.peek("name", "not"):
            self.take()
            return ("not", self.negation())
        return self.comparison()

    def comparison(self):
        first = self.sum()
        operands, ops = [first], []
        while self.peek("op") in _CMP:
            ops.append(self.take())
            operands.append(self.sum())
        return first if not ops else ("cmp", operands, ops)

    def sum(self):
        node = self.term()
        while self.peek("op") in ("+", "-"):
            node = ("bin", self.take(), node, self.term())
        return node

    def term(self):
        node = self.unary()
        while self.peek("op") in ("*", "/", "%"):
            node = ("bin", self.take(), node, self.unary())
        return node

    def unary(self):
        if self.peek("op") in ("-", "+"):
            return ("unary", self.take(), self.unary())
        return self.power()

    def power(self):
        node = self.atom()
        if self.peek("op", "**"):
            self.take()
            node = ("bin", "**", node, self.unary())
        return node

    def atom(self):
        if self.peek("num") is not None:
            v = self.take()
            return ("const", float(v) if "." in v else int(v))
        if self.peek("str") is not None:
            return ("const", self.take()[1:-1])
        if self.peek("op", "("):
            self.take()
            node = self.disjunction()
            self.take("op", ")")
            return node
        name = self.take("name")
        if name in ("true", "false", "none"):
            return ("const", {"true": True, "false": False, "none": None}[name])
        if name in _KEYWORDS:
            raise ExprError(f"{name!r} out of place in {self.text!r}")
        if self.peek("op", "("):
            if name not in _FUNCTIONS:
                raise ExprError(f"unknown function {name}() in {self.text!r} (have {', '.join(sorted(_FUNCTIONS))})")
            self.take()
            args = []
            if not self.peek("op", ")"):
                args.append(self.disjunction())
                while self.peek("op", ","):
                    self.take()
                    args.append(self.disjunction())
            self.take("op", ")")
            if not args:
                raise ExprError(f"{name}() needs an argument in {self.text!r}")
            return ("call", name, args)
        return ("name", name)


def _names(node, out: set):
    kind = node[0]
    if kind == "name":
        out.add(node[1])
    elif kind in ("and", "or"):
        for n in node[1]:
            _names(n, out)
    elif kind == "not":
        _names(node[1], out)
    elif kind == "cmp":
        for n in node[1]:
            _names(n, out)
    elif kind == "bin":
        _names(node[2], out)
        _names(node[3], out)
    elif kind == "unary":
        _names(node[2], out)
    elif kind == "call":
        for n in node[2]:
            _names(n, out)
    return out


def _truth(v) -> bool:
    return bool(v) if v is not None else False


def _ev(node, ns: dict, text: str):
    kind = node[0]
    if kind == "const":
        return node[1]
    if kind == "name":
        try:
            return ns[node[1]]
        except KeyError:
            raise ExprError(f"unknown name {node[1]} in {text!r}") from None
    if kind == "and":
        for n in node[1]:
            if not _truth(_ev(n, ns, text)):
                return False
        return True
    if kind == "or":
        for n in node[1]:
            if _truth(_ev(n, ns, text)):
                return True
        return False
    if kind == "not":
        return not _truth(_ev(node[1], ns, text))
    if kind == "cmp":
        vals = [_ev(n, ns, text) for n in node[1]]
        for op, a, b in zip(node[2], vals, vals[1:]):
            if a is None or b is None:
                return False
            try:
                ok = {"<": a < b, "<=": a <= b, ">": a > b, ">=": a >= b, "==": a == b, "!=": a != b}[op]
            except TypeError:
                raise ExprError(f"cannot compare {a!r} {op} {b!r} in {text!r}") from None
            if not ok:
                return False
        return True
    if kind == "unary":
        v = _ev(node[2], ns, text)
        if v is None:
            return None
        if not isinstance(v, (int, float)) or isinstance(v, bool):
            raise ExprError(f"{node[1]}{v!r}: not a number, in {text!r}")
        return -v if node[1] == "-" else v
    if kind == "bin":
        a, b = _ev(node[2], ns, text), _ev(node[3], ns, text)
        if a is None or b is None:
            return None
        if not all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in (a, b)):
            raise ExprError(f"{a!r} {node[1]} {b!r}: not numbers, in {text!r}")
        op = node[1]
        try:
            if op == "+":
                return a + b
            if op == "-":
                return a - b
            if op == "*":
                return a * b
            if op == "/":
                return a / b
            if op == "%":
                return a % b
            return float(a) ** b
        except (ZeroDivisionError, OverflowError):
            return None
    if kind == "call":
        args = [_ev(n, ns, text) for n in node[2]]
        if any(a is None for a in args):
            return None
        try:
            if node[1] in ("min", "max") and len(args) > 1:
                return _FUNCTIONS[node[1]](args)
            if node[1] == "sqrt" and args[0] < 0:
                return None
            return _FUNCTIONS[node[1]](*args)
        except TypeError:
            raise ExprError(f"{node[1]}({', '.join(map(repr, args))}) in {text!r}") from None
    raise ExprError(f"bad node {kind}")


class Expr:
    """One compiled expression: `names` is what it reads, `.eval(ns)` its value over a flat {name: value} namespace."""

    def __init__(self, text: str):
        self.text = text.strip()
        if not self.text:
            raise ExprError("empty expression")
        self.ast = _Parser(self.text).parse()
        self.names = frozenset(_names(self.ast, set()))

    def eval(self, ns: dict):
        return _ev(self.ast, ns, self.text)

    def __repr__(self):
        return f"Expr({self.text!r})"


def compile(text: str) -> Expr:  # noqa: A001  (the module's verb, like re.compile)
    return Expr(text)


def template_exprs(template: str) -> list[tuple[Expr, str]]:
    """The `{expr}` / `{expr:fmt}` holes of a title or why template, compiled: [(expr, fmt)]."""
    out = []
    for hole in _TEMPLATE.findall(template):
        text, _, fmt = hole.partition(":")
        out.append((Expr(text), fmt))
    return out


def render(template: str, ns: dict) -> str:
    """A title or why with its holes filled from the namespace; a missing value shows as '?'."""
    def fill(m):
        text, _, fmt = m.group(1).partition(":")
        v = Expr(text).eval(ns)
        if v is None:
            return "?"
        if isinstance(v, bool):
            return str(v).lower()
        if fmt and isinstance(v, float) and v == int(v) and fmt[-1] in "d+":
            v = int(v)
        try:
            return format(v, fmt)
        except (ValueError, TypeError):
            raise ExprError(f"cannot format {v!r} with {fmt!r} in {template!r}") from None
    return _TEMPLATE.sub(fill, template)
