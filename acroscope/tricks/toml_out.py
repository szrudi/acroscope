"""Writing a tricks table as TOML: the stdlib reads TOML (tomllib) and writes none, and a pilot's layer leaves the
database as a tricks file others can read (`acroscope tricks export`, the page's export). Only the shapes a tricks
file has: scalars, lists of scalars, lists of inline tables (examples, suppress, phases), tables and arrays of
tables, nested. `tomllib.loads(dumps(t)) == t` for any such table."""
import re

_BARE = re.compile(r"^[A-Za-z0-9_-]+$")


def _key(k: str) -> str:
    return k if _BARE.match(k) else _str(k)


def _str(s: str) -> str:
    out = s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\t", "\\t").replace("\r", "\\r")
    out = "".join(c if (c >= " " and c != "\x7f") or c in "\n\t\r" else f"\\u{ord(c):04X}" for c in out)
    return f'"{out}"'


def _scalar(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        if v != v or v in (float("inf"), float("-inf")):
            raise ValueError(f"cannot write {v!r} to a tricks file")
        return repr(v)
    if isinstance(v, str):
        return _str(v)
    raise ValueError(f"cannot write {type(v).__name__} to a tricks file: {v!r}")


def _inline(v) -> str:
    if isinstance(v, dict):
        return "{ " + ", ".join(f"{_key(k)} = {_inline(x)}" for k, x in v.items()) + " }" if v else "{}"
    if isinstance(v, list):
        return "[" + ", ".join(_inline(x) for x in v) + "]"
    return _scalar(v)


def _is_table_array(v) -> bool:
    return isinstance(v, list) and bool(v) and all(isinstance(x, dict) for x in v)


def dumps(table: dict, header: str = "") -> str:
    """The table as TOML text. Scalars and lists first, then the arrays of tables ([[trick]]), then the sub-tables,
    each as its own section, nested with dotted headers. `header` is a comment on top."""
    lines = [f"# {h}" for h in header.splitlines()] if header else []
    _emit(table, [], lines)
    return "\n".join(lines).rstrip("\n") + "\n"


def _emit(table: dict, path: list, lines: list):
    scalars = [(k, v) for k, v in table.items() if not isinstance(v, dict) and not _is_table_array(v)]
    arrays = [(k, v) for k, v in table.items() if _is_table_array(v)]
    subs = [(k, v) for k, v in table.items() if isinstance(v, dict)]
    if path and (scalars or not (arrays or subs)):
        lines.append("")
        lines.append("[" + ".".join(_key(p) for p in path) + "]")
    for k, v in scalars:
        if isinstance(v, list) and any(isinstance(x, dict) for x in v):          # a list of inline tables: one per line
            lines.append(f"{_key(k)} = [")
            lines.extend(f"  {_inline(x)}," for x in v)
            lines.append("]")
        else:
            lines.append(f"{_key(k)} = {_inline(v)}")
    for k, v in arrays:
        for item in v:
            lines.append("")
            lines.append("[[" + ".".join(_key(p) for p in [*path, k]) + "]]")
            # inside an entry a list of tables is inline (examples, suppress, phases), one per line; a table is a sub-table
            for kk, vv in item.items():
                if isinstance(vv, dict):
                    continue
                if isinstance(vv, list) and any(isinstance(x, dict) for x in vv):
                    lines.append(f"{_key(kk)} = [")
                    lines.extend(f"  {_inline(x)}," for x in vv)
                    lines.append("]")
                else:
                    lines.append(f"{_key(kk)} = {_inline(vv)}")
            for kk, vv in item.items():
                if isinstance(vv, dict):
                    _emit(vv, [*path, k, kk], lines)
    for k, v in subs:
        _emit(v, [*path, k], lines)
