"""One-off for #38: moment snapshots made before the fix hold lean[right] negated. Lists them (default) or, with
--apply, rewrites each through the API with the right component negated and `lean_convention` added. A snapshot that
has `lean_convention` was made by the fixed code or corrected already, so it is left alone: running this twice, or
after the deploy, never flips one back.

    .venv/bin/python scripts/fix-lean-38.py            # list
    .venv/bin/python scripts/fix-lean-38.py --apply    # correct
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from acroscope.metrics import LEAN_CONVENTION  # noqa: E402
from acroscope.remote import Remote  # noqa: E402
from acroscope import sessions  # noqa: E402


def todo(r: Remote):
    for s in r.sessions():
        for m in r.load(s["session"])["moments"]:
            k = m.get("metrics") or {}
            if ("lean_start" in k or "lean_end" in k) and "lean_convention" not in k:
                yield s["session"], m


def fixed(k: dict) -> dict:
    k = dict(k)
    for key in ("lean_start", "lean_end"):
        if isinstance(k.get(key), list) and len(k[key]) == 2:
            k[key] = [k[key][0], -k[key][1] or 0]
    k["lean_convention"] = LEAN_CONVENTION
    return k


def main(apply: bool) -> None:
    if not sessions.remote():
        sys.exit("point it at the server: ACROSCOPE_URL or ~/.config/acroscope/url")
    r = sessions.store()
    n = 0
    for session, m in todo(r):
        k = m["metrics"]
        new = fixed(k)
        print(f"{session:40} {m['id']:4} {m['title'][:30]:30} start {k.get('lean_start')} -> {new.get('lean_start')}"
              f"  end {k.get('lean_end')} -> {new.get('lean_end')}")
        if apply:
            r.tag(session, m["video"], m["start"], m["end"], m["title"], m["tags"], m.get("note", ""), new, m["id"])
        n += 1
    print(f"{n} snapshot(s) {'corrected' if apply else 'to correct (run with --apply)'}")


if __name__ == "__main__":
    main("--apply" in sys.argv[1:])
