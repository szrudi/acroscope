"""The condition language of the tricks file: arithmetic and comparisons over a window's features.

    150 <= abs(rotation_deg.roll) <= 300 and throttle_pct.min < 35
    max(peak_rate_dps.roll, peak_rate_dps.pitch) <= tumble_dps

Numbers, strings in quotes, names with dots, + - * / % **, the comparisons (chained like Python's), `and` `or`
`not`, parentheses, and the functions abs min max round sqrt, plus if(c, a, b) for a title that depends on the
numbers. Nothing else: no attribute access, no indexing, no Python. A trick file from the internet must not run code, so this is a parser and a walker, never `eval`.

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
_FUNCTIONS = {"abs": abs, "min": min, "max": max, "round": round, "sqrt": math.sqrt, "if": None}
_ARITY = {"abs": (1, 1), "sqrt": (1, 1), "round": (1, 2), "min": (1, None), "max": (1, None), "if": (3, 3)}
_CMP = {"<", "<=", ">", ">=", "==", "!="}
_TEMPLATE = re.compile(r"\{([^{}]*)\}")
# an imported file is a trust boundary: nothing in it may take long, recurse deep or print pages
MAX_DEPTH = 32              # nested parentheses and calls
MAX_LITERAL = 24            # characters in a number
MAX_ROUND = 12              # |ndigits| for round()
MAX_SPEC = 12               # characters in a {value:spec} format spec
MAX_WIDTH = 60              # a width or precision in a format spec
_WIDTHS = re.compile(r"\d+")


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
        self.depth = 0

    def deeper(self):
        self.depth += 1
        if self.depth > MAX_DEPTH:
            raise ExprError(f"nested deeper than {MAX_DEPTH} in {self.text!r}")

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
            if len(v) > MAX_LITERAL:
                raise ExprError(f"a number longer than {MAX_LITERAL} characters in {self.text!r}")
            return ("const", float(v) if "." in v else int(v))
        if self.peek("str") is not None:
            return ("const", self.take()[1:-1])
        if self.peek("op", "("):
            self.take()
            self.deeper()
            node = self.disjunction()
            self.depth -= 1
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
            self.deeper()
            args = []
            if not self.peek("op", ")"):
                args.append(self.disjunction())
                while self.peek("op", ","):
                    self.take()
                    args.append(self.disjunction())
            self.depth -= 1
            self.take("op", ")")
            lo, hi = _ARITY[name]
            if len(args) < lo or (hi is not None and len(args) > hi):
                want = f"{lo}" if hi == lo else f"{lo} or more" if hi is None else f"{lo} to {hi}"
                raise ExprError(f"{name}() takes {want} argument{'s' if want != '1' else ''}, not {len(args)}, in {self.text!r}")
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
            if op == "==":                                      # equality is defined between any two values
                ok = a == b
            elif op == "!=":
                ok = a != b
            else:
                try:
                    ok = {"<": lambda: a < b, "<=": lambda: a <= b, ">": lambda: a > b, ">=": lambda: a >= b}[op]()
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
            r = float(a) ** b
            return None if isinstance(r, complex) else r          # a negative base to a fractional power
        except (ZeroDivisionError, OverflowError):
            return None
    if kind == "call":
        if node[1] == "if":
            return _ev(node[2][1] if _truth(_ev(node[2][0], ns, text)) else node[2][2], ns, text)
        args = [_ev(n, ns, text) for n in node[2]]
        if any(a is None for a in args):
            return None
        try:
            if node[1] in ("min", "max"):
                return _FUNCTIONS[node[1]](args) if len(args) > 1 else args[0]
            if node[1] == "sqrt" and args[0] < 0:
                return None
            if node[1] == "round" and len(args) == 2:
                if not isinstance(args[1], int) or isinstance(args[1], bool):
                    raise ExprError(f"round() wants a whole number of digits in {text!r}")
                return round(args[0], max(-MAX_ROUND, min(MAX_ROUND, args[1])))
            return _FUNCTIONS[node[1]](*args)
        except (TypeError, OverflowError):
            raise ExprError(f"{node[1]}({', '.join(map(repr, args))}) in {text!r}") from None
    raise ExprError(f"bad node {kind}")


class Expr:
    """One compiled expression: `names` is what it reads, `.eval(ns)` its value over a flat {name: value} namespace."""

    def __init__(self, text: str):
        self.text = text.strip()
        if not self.text:
            raise ExprError("empty expression")
        try:
            self.ast = _Parser(self.text).parse()
        except ExprError:
            raise
        except (RecursionError, ValueError, OverflowError) as e:
            raise ExprError(f"cannot read {self.text!r}: {type(e).__name__}") from None
        self.names = frozenset(_names(self.ast, set()))

    def eval(self, ns: dict):
        try:
            return _ev(self.ast, ns, self.text)
        except ExprError:
            raise
        except (RecursionError, ValueError, OverflowError) as e:
            raise ExprError(f"{self.text!r}: {type(e).__name__}: {e}") from None

    def __repr__(self):
        return f"Expr({self.text!r})"


def compile(text: str) -> Expr:  # noqa: A001  (the module's verb, like re.compile)
    return Expr(text)


def check_spec(fmt: str, template: str):
    """A format spec a title may use: short, no wide fields, and one Python's format() accepts for a number or a
    string (so a spec a condition's value cannot take fails at load, not on every arm)."""
    if len(fmt) > MAX_SPEC:
        raise ExprError(f"format spec {fmt!r} longer than {MAX_SPEC} in {template!r}")
    if any(int(w) > MAX_WIDTH for w in _WIDTHS.findall(fmt)):
        raise ExprError(f"format spec {fmt!r} wider than {MAX_WIDTH} in {template!r}")
    for sample in (1.0, 1, "x"):
        try:
            format(sample, fmt)
            return
        except (ValueError, TypeError):
            continue
    raise ExprError(f"format spec {fmt!r} fits no value in {template!r}")


def template_exprs(template: str) -> list[tuple[Expr, str]]:
    """The `{expr}` / `{expr:fmt}` holes of a title or why template, compiled and their specs checked: [(expr, fmt)]."""
    out = []
    for hole in _TEMPLATE.findall(template):
        text, _, fmt = hole.partition(":")
        check_spec(fmt, template)
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
