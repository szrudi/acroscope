"""Phase 2's commands: attach, detach, match, unmatch, automatch.
"""
import sys

from ..match import automatch, manual, pairing, planner
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
    loaded = sessions.load(s)
    clips = a.videos or [v["file"] for v in loaded["videos"] if v.get("duration")]     # in name order: chronological
    results, pending, errors = [], [], []
    if a.videos:               # named clips by their file first: the plan and every check below go by file (#41)
        named = []
        for c in clips:
            try:
                named.append(sessions.resolve_clip(s, c))
            except Exception as e:  # noqa: BLE001  (an unknown name: an error for that clip, the others go on)
                errors.append({"clip": c, "error": f"{type(e).__name__}: {e}"})
                print(f"{c}: {errors[-1]['error']}", file=sys.stderr)
        clips = named
    if a.unmatched and clips:  # the player's automatch button: never a clip with a match (by hand or not), never a missing one
        matched = {m["video"] for m in loaded["matches"] if m["bbl"] in loaded["blackbox"]}   # a match to a log taken out is none
        gone = {v["file"] for v in loaded["videos"] if v.get("missing_since")}
        picked = []
        for f in clips:
            c = f
            if f in gone:
                if a.videos:             # named: the file is what was asked for
                    errors.append({"clip": c, "error": "file missing, nothing to read"})
                    print(f"{c}: {errors[-1]['error']}", file=sys.stderr)
                continue
            if f in matched:
                if a.videos:
                    print(f"{c}: has a match, skipped (--unmatched)", file=sys.stderr)
                continue
            picked.append(f)
        clips = picked
        if not clips and not errors:
            print(f"{s}: every clip has a match, nothing to do", file=sys.stderr)
            if a.json:
                out([])
            return
    elif not clips:
        print(f"{s}: no probed clips, nothing to match", file=sys.stderr)
    # a session's worth of clips is planned whole first (#26): each clip's arms come from one alignment of the session,
    # not from the picks of the clips before it; one clip named alone stays bounded by the others' matches
    plan = None
    if clips and (len(clips) > 1 or not a.videos):
        try:
            plan = planner.session_plan(s, a.fps)
        except Exception as e:  # noqa: BLE001  (no log, a log still syncing: clip by clip, as before)
            print(f"{s}: no session plan ({type(e).__name__}: {e}), clip by clip", file=sys.stderr)
    for c in clips:
        p = (plan or {}).get(c)
        settled = False
        try:
            if p and p["ambiguous"]:
                # ambiguous in the plan (a runner-up within a second hands its arms to another clip): its own pass in a
                # window over both alignments decides it, but only with more than lengths to go by, a boot fit over two
                # arms or more (the gaps between its runs against the arms' uptimes); else nothing is written (#41)
                r = automatch.automatch(s, c, write=False, fps=a.fps, overwrite=a.overwrite, pending=pending, windows=p["windows"])
                fits = r.get("boot_fits") or {}
                settled = bool(r["matches"]) and bool(fits) and all(f.get("n", 0) >= 2 and not f.get("rejected") for f in fits.values())
                if not settled:
                    print(f"{c}: ambiguous in the session plan and only lengths to go by: nothing "
                          f"{'written' if a.write else 'proposed'}, left for a hand match", file=sys.stderr)
                    results.append(dict(r, ambiguous=True, matches=[]))
                    continue
            r = automatch.automatch(s, c, write=a.write, fps=a.fps, overwrite=a.overwrite, pending=pending, windows=p["windows"] if p else None)
            if settled:
                r = dict(r, ambiguous=True)
        except Exception as e:  # noqa: BLE001  (a clip still syncing, no log, ...)
            errors.append({"clip": c, "error": f"{type(e).__name__}: {e}"})
            print(f"{c}: {errors[-1]['error']}", file=sys.stderr)
            continue
        results.append(r)
        if not a.write:            # the next clips see these as taken, as they would once written
            pending += [{"session": s, "video": r["clip"], "name": r["name"], "bbl": m["bbl"], "arm": m["arm"]} for m in r["matches"]]
        if a.json:
            continue
        print(f"{r['clip']}: {r['readable']}/{r['readings']} frames readable, {len(r['video_arms'])} arm runs, {len(r['matches'])} matched"
              + (" (written)" if a.write else "") + (" (ambiguous in the session plan, settled by its boot fit)" if r.get("ambiguous") else ""))
        f = r.get("flash_full")
        if f:
            print(f"   flash full: STATS shows BLACKBOX 100 % after the run at {fmt_time(f['at'])} in {f['clip']}; "
                  + ("nothing in this clip is in the log" if f["clip"] != r["name"] else "that run can only be the log's cut-off last arm, nothing after it is in the log"))
        lo, hi = r.get("arm_window") or (None, None)
        if lo is not None or hi is not None:
            print("   other clips leave " + " and ".join(x for x in (f"arms after {lo}" if lo is not None else "", f"arms before {hi}" if hi is not None else "") if x))
        for m in r["matches"]:
            how = f"video run {m['video_length']:5.1f} s vs log {m['log_length']:5.1f} s" if not m.get("derived") else f"derived from the boot's fit, log {m['log_length']:5.1f} s"
            print(f"   arm {m['arm']:2} @ +{m['offset']:7.2f}  {how}" + ("" if not a.write else ("  written" if m.get("written") else "  kept existing")))
        for b, f in sorted(r.get("boot_fits", {}).items()):
            if f.get("rejected"):
                print(f"   boot {b}: video = {f['a']:.5f} x uptime + {f['b']:.2f} ({f['n']} arms) explains no clock (beyond {100 * pairing.MAX_CLOCK:g} %"
                      " or offsets apart: a cut in the clip, or a wrong pair): nothing placed or derived from it, check its matches by hand")
            elif f["a"] != 1.0:
                print(f"   boot {b}: video = {f['a']:.5f} x uptime + {f['b']:.2f} ({f['n']} arms; {100 * (f['a'] - 1):+.2f} % clock difference)")
        for v in r["unmatched_video_arms"]:
            print(f"   unmatched run at {fmt_time(v['start'])}: {v['length']} s")
    if a.json:      # without errors the shape it always had: one clip's result, or the list
        out({"results": results, "errors": errors} if errors else results[0] if len(results) == 1 else results)
    # clips named on the command line must all go through; a whole session only fails when none did (some clips
    # there have no log at all)
    if (errors and a.videos) or not results:
        sys.exit(1)


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
