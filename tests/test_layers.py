"""The package's layers (#60), read from the source: config, then the sources (the raw inputs), then the store, then
the phases in the workflow's order, then the surfaces. A module imports from its own layer or the ones before it,
never a later one: a phase never reaches into the CLI or the server, the store never into a phase, a source never
into the store. Lazy imports inside functions count too. Running a command in a child process (`python -m
acroscope.cli`) is not an import and is how a lower layer asks for a later one's work (ingest runs automatch)."""
import ast
import tempfile
import unittest
from pathlib import Path

PKG = Path(__file__).resolve().parent.parent / "acroscope"
# one entry per layer, in order; the parts of one layer may import each other
LAYERS = [
    {"config"},
    {"sources"},
    {"store"},
    {"ingest"},                 # phase 1
    {"match"},                  # phase 2
    {"analyse", "tricks"},      # phase 3: the numbers, and the trick engine on them
    {"review"},                 # phase 4
    {"share"},                  # phase 5
    {"cli", "server"},          # the surfaces: the CLI starts the server, the server parses a client's command with the CLI's parser
]
RANK = {part: i for i, layer in enumerate(LAYERS) for part in layer}


def part_of(module: str) -> str | None:
    """acroscope.<part>[...] -> part; the package itself (acroscope/__init__.py) is no layer."""
    bits = module.split(".")
    return bits[1] if len(bits) > 1 and bits[0] == "acroscope" else None


def module_of(path: Path, pkg: Path = PKG) -> str:
    rel = path.relative_to(pkg.parent).with_suffix("")
    bits = list(rel.parts)
    return ".".join(bits[:-1] if bits[-1] == "__init__" else bits)


def imported(path: Path, pkg: Path = PKG) -> list[tuple[int, str]]:
    """(line, absolute module) for every import of the package in a file, nested ones included."""
    tree = ast.parse(path.read_text())
    here = module_of(path, pkg)
    package = here if path.name == "__init__.py" else here.rpartition(".")[0]
    out = []
    for n in ast.walk(tree):
        if isinstance(n, ast.ImportFrom):
            if n.level:
                base = package.split(".")
                base = ".".join(base[:len(base) - n.level + 1] + ([n.module] if n.module else []))
            else:
                base = n.module or ""
            if base == "acroscope":
                out += [(n.lineno, f"acroscope.{a.name}") for a in n.names]
            elif base.startswith("acroscope."):
                out.append((n.lineno, base))
        elif isinstance(n, ast.Import):
            out += [(n.lineno, a.name) for a in n.names if a.name.startswith("acroscope.")]
    return out


def violations(pkg: Path = PKG) -> list[str]:
    wrong = []
    for path in sorted(pkg.rglob("*.py")):
        mine = part_of(module_of(path, pkg))
        if mine is None:
            continue
        for line, mod in imported(path, pkg):
            theirs = part_of(mod)
            if theirs is not None and RANK.get(theirs, -1) > RANK[mine]:
                wrong.append(f"{path.relative_to(pkg.parent)}:{line}: {mine} imports {mod} ({theirs} is a later layer)")
    return wrong


class LayersTest(unittest.TestCase):
    def test_every_part_has_a_layer(self):
        parts = {p.stem if p.is_file() else p.name for p in PKG.iterdir()
                 if (p.suffix == ".py" and p.name != "__init__.py") or (p / "__init__.py").is_file()}
        self.assertEqual(parts - set(RANK), set(), "a new part of the package needs its place in LAYERS")

    def test_no_import_reaches_a_later_layer(self):
        self.assertEqual(violations(), [])

    def test_the_check_sees_a_wrong_import(self):
        """The check on a made-up package: a store module importing the CLI at the top and the server lazily is
        caught twice; a phase importing the store, and the store importing itself, are not."""
        with tempfile.TemporaryDirectory() as tmp:
            pkg = Path(tmp) / "acroscope"
            (pkg / "store").mkdir(parents=True)
            (pkg / "match").mkdir()
            (pkg / "store" / "x.py").write_text("from .. import cli\nfrom . import db\n\ndef f():\n    from ..server import serve\n")
            (pkg / "match" / "y.py").write_text("from ..store import sessions\nfrom ..config import DATA_DIR\n")
            self.assertEqual(violations(pkg), ["acroscope/store/x.py:1: store imports acroscope.cli (cli is a later layer)",
                                               "acroscope/store/x.py:5: store imports acroscope.server (server is a later layer)"])


if __name__ == "__main__":
    unittest.main()
