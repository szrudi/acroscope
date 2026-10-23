"""Phase 2's commands: attach, detach, match, unmatch, automatch.
"""
import sys

from ..match import automatch, manual, pairing
from ..store import sessions
from .common import _session, fmt_time, out


def cmd_attach(a):
    out(manual.attach(_session(a.session), a.bbl))


def cmd_detach(a):
    out(manual.detach(_session(a.session), a.bbl))


def cmd_match(a):
    session = _session(a.session)
    vid = manual.set_match(session, a.video, a.bbl, a.arm, a.offset, a.note or "", boot=a.boot)[0]["video"]
    out([m for m in sessions.load(session)["matches"] if m["video"] == vid])


def cmd_unmatch(a):
    out({"removed": manual.unmatch(_session(a.session), a.video, a.arm)})


def cmd_automatch(a):
    s = _session(a.session)
    res = automatch.run(s, a.videos, write=a.write, fps=a.fps, overwrite=a.overwrite, unmatched=a.unmatched,
                        log=lambda m: print(m, file=sys.stderr), each=None if a.json else lambda r: _say(r, a.write))
    if res is None:            # --unmatched, and every clip has a match: nothing to do
        if a.json:
            out([])
        return
    results, errors = res["results"], res["errors"]
    if a.json:      # without errors the shape it always had: one clip's result, or the list
        out({"results": results, "errors": errors} if errors else results[0] if len(results) == 1 else results)
    # clips named on the command line must all go through; a whole session only fails when none did (some clips
    # there have no log at all)
    if (errors and a.videos) or not results:
        sys.exit(1)


def _say(r: dict, write: bool) -> None:
    """One clip's automatch, for a human."""
    print(f"{r['clip']}: {r['readable']}/{r['readings']} frames readable, {len(r['video_arms'])} arm runs, {len(r['matches'])} matched"
          + (" (written)" if write else "") + (" (ambiguous in the session plan, settled by its boot fit)" if r.get("ambiguous") else ""))
    f = r.get("flash_full")
    if f:
        print(f"   flash full: STATS shows BLACKBOX 100 % after the run at {fmt_time(f['at'])} in {f['clip']}; "
              + ("nothing in this clip is in the log" if f["clip"] != r["name"] else "that run can only be the log's cut-off last arm, nothing after it is in the log"))
    lo, hi = r.get("arm_window") or (None, None)
    if lo is not None or hi is not None:
        print("   other clips leave " + " and ".join(x for x in (f"arms after {lo}" if lo is not None else "", f"arms before {hi}" if hi is not None else "") if x))
    for m in r["matches"]:
        how = f"video run {m['video_length']:5.1f} s vs log {m['log_length']:5.1f} s" if not m.get("derived") else f"derived from the boot's fit, log {m['log_length']:5.1f} s"
        print(f"   arm {m['arm']:2} @ +{m['offset']:7.2f}  {how}" + ("" if not write else ("  written" if m.get("written") else "  kept existing")))
    for b, f in sorted(r.get("boot_fits", {}).items()):
        if f.get("rejected"):
            print(f"   boot {b}: video = {f['a']:.5f} x uptime + {f['b']:.2f} ({f['n']} arms) explains no clock (beyond {100 * pairing.MAX_CLOCK:g} %"
                  " or offsets apart: a cut in the clip, or a wrong pair): nothing placed or derived from it, check its matches by hand")
        elif f["a"] != 1.0:
            print(f"   boot {b}: video = {f['a']:.5f} x uptime + {f['b']:.2f} ({f['n']} arms; {100 * (f['a'] - 1):+.2f} % clock difference)")
    for v in r["unmatched_video_arms"]:
        print(f"   unmatched run at {fmt_time(v['start'])}: {v['length']} s")


def add(sp) -> None:
    """This group's subcommands."""
    p = sp.add_parser("attach", help="add a blackbox file to a session (a log dated another day)")
    p.add_argument("session"); p.add_argument("bbl"); p.set_defaults(f=cmd_attach)
    p = sp.add_parser("detach", help="remove a blackbox file from a session"); p.add_argument("session"); p.add_argument("bbl"); p.set_defaults(f=cmd_detach)
    p = sp.add_parser("match", help="record video <-> arm with offset (video time = arm time + offset)")
    p.add_argument("session"); p.add_argument("video"); p.add_argument("bbl"); p.add_argument("arm", type=int)
    p.add_argument("offset", type=float); p.add_argument("--note")
    p.add_argument("--boot", action="store_true", help="also match every other arm of the same power cycle (offsets derived from the FC uptime)")
    p.set_defaults(f=cmd_match)
    p = sp.add_parser("unmatch", help="remove a clip's matches (all, or one arm)")
    p.add_argument("session"); p.add_argument("video"); p.add_argument("arm", type=int, nargs="?"); p.set_defaults(f=cmd_unmatch)
    p = sp.add_parser("automatch", help="match clips to arms from the OSD timers (proposes; --write records)")
    p.add_argument("session"); p.add_argument("videos", nargs="*"); p.add_argument("--write", action="store_true"); p.add_argument("--overwrite", action="store_true", help="replace existing matches of the clip too")
    p.add_argument("--unmatched", action="store_true", help="only the clips without a match yet (not missing ones)")
    p.add_argument("--fps", type=float, default=2.0); p.add_argument("--json", action="store_true"); p.set_defaults(f=cmd_automatch)
