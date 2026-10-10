"""acroscope command line: the agent side. Every command prints JSON unless it is a table for humans.

Times: video seconds or m:ss.s ('1:32.7'). Sessions and clips can be given by a unique fragment ('schammer', '007').
Matches, moments and notes live in the server's database: with ACROSCOPE_URL set, this CLI is a client of that
server; without it, it opens the local database (ACROSCOPE_DB) itself.
"""
import argparse
import json
import sys

from . import blackbox, events as ev, metrics, osd, sessions, video, xspf
from .config import CACHE_DIR, DATA_DIR, TAGS


def parse_time(s: str) -> float:
    if ":" in s:
        m, _, sec = s.rpartition(":")
        return int(m) * 60 + float(sec)
    return float(s)


def fmt_time(t: float) -> str:
    m, s = divmod(t, 60)
    return f"{int(m)}:{s:04.1f}"


def out(obj):
    print(json.dumps(obj, indent=1, ensure_ascii=False))


def _session(name):
    return sessions.resolve_session(name)


def _arm_for(s: dict, vid: str, t: float):
    """(match, Arm) for a video time, or (None, None) when the clip has no match."""
    m = sessions.match_for(s, vid, t)
    if not m:
        return None, None
    return m, blackbox.load_arm(m["bbl"], m["arm"])


# ---- commands ---------------------------------------------------------------------------------------------------

def cmd_sessions(a):
    if not a.session:
        rows = sessions.all_sessions()
        if a.json:
            return out(rows)
        print(f"# {sessions.store()}")
        print(f"{'session':40} {'clips':>5} {'gone':>4} {'match':>5} {'mom.':>4}  blackbox")
        for r in rows:
            print(f"{r['session']:40} {r['videos']:5} {r['missing'] or '-':>4} {r['matches']:5} {r['moments']:4}  {', '.join(b[25:40] for b in r['blackbox'])}")
        return
    name = _session(a.session)
    for b in sessions.load(name)["blackbox"]:     # decoding happens here, with progress, not silently inside the view
        try:
            blackbox.index_bbl(b, progress=lambda m: print(m, file=sys.stderr))
        except FileNotFoundError as e:
            print(f"{b}: {e}", file=sys.stderr)
    s = sessions.overview(name)
    if a.json:
        return out(s)
    print(f"# {s['session']}  {s.get('note', '')}")
    print(f"blackbox: {', '.join(s['blackbox']) or '-'}")
    print("\n## videos")
    for v in s["videos"]:
        ms = [m for m in s["matches"] if m["video"] == v["file"]]
        cov = "; ".join(f"arm {m['arm']} @ +{m['offset']}s" for m in ms) or "no log"
        d = fmt_time(v["duration"]) if v.get("duration") else "?"
        gone = f"  MISSING since {v['missing_since'][:10]}" if v.get("missing_since") else ""
        print(f"  {v['file']:24} {d:>7}  {cov}{gone}" + (f"  | {v['note']}" if v.get("note") else ""))
    print("\n## arms")
    for b in s["blackbox"]:                       # one block per log, so three "arm 4" rows can't be confused
        rows = [r for r in s.get("arms", []) if r["bbl"] == b]
        if b in s.get("arms_pending", []):
            print(f"  {b}: not decoded yet (run `acroscope arms {b[25:40]}`)")
            continue
        if not rows:
            print(f"  {b}: no arms" + ("" if (DATA_DIR / 'blackbox' / b).exists() else " (file not found)"))
            continue
        print(f"  {b}  ({len(rows)} arms, {sum(1 for r in rows if r['videos'])} with a clip)")
        for r in rows:
            vids = ", ".join(f"{x['video'][-7:-4]} +{x['offset']}" for x in r["videos"]) or "no clip"
            flags = ("" if r["motors_spun"] else " (motors off)") + ("" if r["complete"] else " (cut off)")
            vb = f"{r['vbat_start']:.2f}→{r['vbat_end']:.2f} V" if r.get("vbat_start") is not None else "no frames"
            print(f"    arm {r['index']:2}  {r['length']:6.1f} s  {vb:12}  {vids}{flags}")
    print("\n## moments")
    for m in s["moments"]:
        print(f"  {m['id']:4} {m['video'][-7:-4]} {fmt_time(m['start'])}-{fmt_time(m['end'])}  {m['title']}  [{', '.join(m['tags'])}]" + (f"  | {m['note']}" if m.get("note") else ""))


def cmd_refresh(a):
    name = a.session if (video.VIDEOS_DIR / a.session).is_dir() else _session(a.session)   # a new folder is not known yet
    s = sessions.refresh(name, probe_videos=not a.no_probe)
    out({"session": s["session"], "videos": len(s["videos"]), "missing": [v["file"] for v in s["videos"] if v.get("missing_since")],
         "blackbox": s["blackbox"], "store": repr(sessions.store())})


def cmd_scan(a):
    rows = sessions.scan()
    out([{k: r[k] for k in ("session", "videos", "missing", "matches", "moments")} for r in rows])


def cmd_note(a):
    sessions.set_note(_session(a.session), a.text, a.clip)
    out({"ok": True})


def cmd_attach(a):
    out(sessions.attach(_session(a.session), a.bbl))


def cmd_detach(a):
    out(sessions.detach(_session(a.session), a.bbl))


def cmd_purge(a):
    s = _session(a.session)
    if a.clip is None and not a.yes:
        gone = [v for v in sessions.load(s)["videos"] if v.get("missing_since")]
        print(f"{len(gone)} clip(s) marked missing in {s}; add --yes to remove those missing for more than {a.days} days, "
              f"or name one clip", file=sys.stderr)
        return out([{"video": v["file"], "missing_since": v["missing_since"]} for v in gone])
    out(sessions.purge(s, a.clip, a.days))


def cmd_import_json(a):
    out(sessions.import_json(_session(a.session) if a.session else None))


def cmd_tags(a):
    st = sessions.store()
    t, use = st.tags(), st.tag_usage(_session(a.session) if a.session else None)
    if a.json:
        return out({**t, "usage": use})
    defined = {x["name"] for x in t["tags"]}
    for c in t["categories"]:
        print(f"{c['name']}  {c['color']}")
        for x in t["tags"]:
            if x["category"] == c["name"]:
                print(f"  {x['name']:12} {x['color'] or '':8} {use.get(x['name'], 0):3} moment(s)")
    loose = {k: v for k, v in use.items() if k not in defined}
    if loose:
        print("(no category)")
        for k, v in sorted(loose.items()):
            print(f"  {k:12} {'':8} {v:3} moment(s)")


def cmd_tagdef(a):
    st = sessions.store()
    if a.rm:
        return out({"removed": st.delete_tag(a.name)})
    if not a.category:
        sys.exit("tagdef: --category is needed (or --rm)")
    out(st.set_tag(a.name, a.category, a.color))


def cmd_category(a):
    st = sessions.store()
    if a.rm:
        return out({"removed": st.delete_category(a.name)})
    out(st.set_category(a.name, a.color))


def cmd_arms(a):
    idx = blackbox.index_bbl(a.bbl, force=a.force, progress=lambda m: print(m, file=sys.stderr))
    if a.json:
        return out(idx)
    print(f"{idx['file']}  ({len(idx['arms'])} arms)")
    for r in idx["arms"]:
        flags = ("" if r["motors_spun"] else " motors-off") + ("" if r["complete"] else " cut-off") + (" TRUNCATED" if r["truncated"] else "")
        print(f"  arm {r['index']:2}  {r['length']:6.1f} s  uptime {r.get('uptime_start', 0):7.1f}-{r.get('uptime_end', 0):7.1f}  {r['vbat_start']:.2f}→{r['vbat_end']:.2f} V (min {r['vbat_min']:.2f})  disarm {r['disarm_reason']}{flags}")
    for b in idx.get("boots", []):
        print(f"  boot: arms {b['first']}-{b['last']} ({len(b['arms'])} arms, uptime {b['uptime_start']:.1f}-{b['uptime_end']:.1f} s, {b['length']:.0f} s span): one offset covers them all (`match --boot`)")


def cmd_decode(a):
    arm = blackbox.load_arm(a.bbl, a.arm)
    if a.csv:
        w = sys.stdout
        w.write(",".join(["time (us)"] + [f for f in arm.fields if f != "time"]) + "\n")
        cols = [arm.col(f) for f in arm.fields if f != "time"]
        for i in range(arm.n):
            w.write(f"{int(arm.t[i] * 1e6)}," + ",".join(f"{c[i]:g}" for c in cols) + "\n")
        return
    out({k: v for k, v in arm.meta.items() if k not in ("fields", "events")} | {"cache": str(blackbox._bin_path(arm.bbl, arm.index))})


def cmd_match(a):
    s = sessions.set_match(_session(a.session), a.video, a.bbl, a.arm, a.offset, a.note or "", boot=a.boot)
    out([m for m in s["matches"] if m["video"] == video.resolve_video(s["session"], a.video).name])


def cmd_unmatch(a):
    out({"removed": sessions.unmatch(_session(a.session), a.video, a.arm)})


def cmd_metrics(a):
    t0, t1 = parse_time(a.start), parse_time(a.end)
    if a.arm:
        bbl, _, n = a.arm.rpartition(":")
        arm, off = blackbox.load_arm(bbl, int(n)), 0.0
        head = {"bbl": arm.bbl, "arm": arm.index}
    else:
        s = sessions.load(_session(a.session))
        vid = video.resolve_video(s["session"], a.video).name
        m, arm = _arm_for(s, vid, t0)
        if not arm:
            sys.exit(f"{vid}: no blackbox match (set one with `acroscope match`)")
        off = 0.0 if a.arm_time else m["offset"]
        head = {"video": vid, "bbl": m["bbl"], "arm": m["arm"], "offset": m["offset"]}
    a0, a1 = t0 - off, t1 - off
    res = dict(head, window={"arm_from": round(a0, 2), "arm_to": round(a1, 2)}, conventions=metrics.CONVENTIONS)
    if a.profile:
        rows = metrics.profile(arm, a0, a1, a.profile)
        if a.json:
            res["profile"] = rows
        else:
            print("conventions: roll + = right, pitch + = nose down (back flip = negative), yaw + = nose right; sticks in %, gyro deg/s, tilt 0 level / 180 inverted")
            print("  arm_t  vid_t  stick r/p/y   thr | gyro r/p/y      | rot r/p/y        | vbat  mot  accZ tilt")
            for r in rows:
                print(f"{r['t']:7.2f} {fmt_time(r['t'] + off):>6} {r['stick'][0]:4} {r['stick'][1]:4} {r['stick'][2]:4}  {r['throttle']:3} | "
                      f"{r['gyro'][0]:5} {r['gyro'][1]:5} {r['gyro'][2]:5} | {r['rotation'][0]:5} {r['rotation'][1]:5} {r['rotation'][2]:5} | "
                      f"{r['vbat']:.2f} {r['motor'] or 0:4} {r['acc_z']:4.1f} {r['tilt']:4}")
            return
    res["summary"] = metrics.summary(arm, a0, a1)
    res["segments"] = metrics.rotation_segments(arm, a0, a1)
    if a.loop:
        res["loop"] = metrics.loop_summary(arm, a0, a1)
    if a.orbit:
        res["orbit"] = metrics.orbit(arm, a0, a1)
    out(res)


def cmd_events(a):
    s = sessions.load(_session(a.session)) if a.session else None
    rows = []
    if a.arm:
        bbl, _, n = a.arm.rpartition(":")
        arms = [(None, blackbox.load_arm(bbl, int(n)))]
    elif a.video:
        vid = video.resolve_video(s["session"], a.video).name
        arms = [(m, blackbox.load_arm(m["bbl"], m["arm"])) for m in s["matches"] if m["video"] == vid]
    else:
        arms = [(m, blackbox.load_arm(m["bbl"], m["arm"])) for m in s["matches"]]
        if not a.matched_only:
            seen = {(m["bbl"], m["arm"]) for m in s["matches"]}
            for b in s["blackbox"]:
                for r in blackbox.index_bbl(b)["arms"]:
                    if (blackbox.index_bbl(b)["file"], r["index"]) not in seen and r["frames"]:
                        arms.append((None, blackbox.load_arm(b, r["index"])))
    for m, arm in arms:
        for e in ev.detect(arm):
            r = {"bbl": arm.bbl[25:40], "arm": arm.index, **e}
            if m:
                r["video"], r["start"], r["end"] = m["video"], round(e["arm_from"] + m["offset"], 2), round(e["arm_to"] + m["offset"], 2)
            rows.append(r)
    if a.json:
        return out(rows)
    for r in rows:
        where = f"{r['video'][-7:-4]} {fmt_time(r['start'])}-{fmt_time(r['end'])}" if "video" in r else f"arm {r['arm']:2} {r['arm_from']:6.2f}-{r['arm_to']:6.2f} (no clip)"
        print(f"{where:28} {r['tag']:10} {r['title']:24} | {r['why']}")


def cmd_frame(a):
    s = _session(a.session)
    p = video.resolve_video(s, a.video)
    dur = video.probe(p)["duration"]
    for t in a.times:
        tt = parse_time(t)
        if tt > dur - 0.1:
            print(f"{t}: past the end of the clip ({fmt_time(dur)}), skipped", file=sys.stderr)
            continue
        print(video.frame(p, tt, osd=a.osd, scale=a.scale))


def cmd_sheet(a):
    s = _session(a.session)
    p = video.resolve_video(s, a.video)
    dur = video.probe(p)["duration"]
    if a.times:
        times = [parse_time(t) for t in a.times]
    else:
        t0, t1 = parse_time(a.start or "0"), parse_time(a.end) if a.end else dur
        step = a.every or max(0.5, (t1 - t0) / 5)
        times = []
        t = t0
        while t <= t1 + 1e-6 and len(times) < 24:
            times.append(round(t, 2))
            t += step
    times = [t for t in times if t < dur - 0.1]
    if not times:
        sys.exit("no frames inside the clip")
    print(video.sheet(p, times, cols=a.cols, width=a.width, osd=a.osd))


def cmd_tag(a):
    s = _session(a.session)
    start, end = parse_time(a.start), parse_time(a.end)
    tags = [t.strip() for t in (a.tags or "").split(",") if t.strip()]
    snap = None
    if a.metrics:
        sd = sessions.load(s)
        m, arm = _arm_for(sd, video.resolve_video(s, a.video).name, start)
        if arm:
            snap = metrics.summary(arm, start - m["offset"], end - m["offset"])
    out(sessions.tag(s, a.video, start, end, a.title, tags, a.note or "", snap, a.id))


def cmd_untag(a):
    out({"removed": sessions.untag(_session(a.session), a.id)})


def cmd_moments(a):
    s = sessions.load(_session(a.session))
    if a.json:
        return out(s["moments"])
    for m in s["moments"]:
        print(f"{m['id']:4} {m['video'][-7:-4]} {fmt_time(m['start'])}-{fmt_time(m['end'])}  {m['title']}  [{', '.join(m['tags'])}]" + (f"  | {m['note']}" if m.get("note") else ""))


def cmd_import_xspf(a):
    out(xspf.import_session(_session(a.session)))


def cmd_transcode(a):
    s = _session(a.session)
    sd = sessions.load(s)
    files = [video.resolve_video(s, v) for v in a.videos] or [video.resolve_video(s, v["file"]) for v in sd["videos"]]
    for src in files:
        if video.probe(src)["codec"] == "h264" and not a.force:
            print(f"{src.name}: already H.264", file=sys.stderr)
            continue
        dst = src.with_suffix(".h264.mp4") if a.replace else video.proxy_path(src)
        if dst.exists() and not a.force:
            print(f"{src.name}: proxy exists", file=sys.stderr)
            continue
        video.transcode(src, dst, progress=lambda f, n=src.name: print(f"\r{n}: {f:4.0%}", end="", file=sys.stderr, flush=True))
        print(file=sys.stderr)
        if a.replace:
            dst.replace(src.with_suffix(".mp4"))
            if src.suffix != ".mp4":
                src.unlink()
        print(dst if not a.replace else src.with_suffix(".mp4"))


def cmd_osd(a):
    s = _session(a.session)
    rows = osd.read_timers(video.resolve_video(s, a.video), a.fps)
    if a.json:
        return out(rows)
    ok = [r for r in rows if r["top"] is not None]
    print(f"{len(rows)} frames, {len(ok)} with a readable arm timer")
    for v in osd.arms_from_timers(rows, a.fps):
        print(f"  arm run: video {fmt_time(v['start'])} - {fmt_time(v['end'])}  {v['length']:5.1f} s  (timer {v['timer_max']} s, total before {v['total_before']}, {v['readings']} readings)")


def cmd_automatch(a):
    s = _session(a.session)
    clips = a.videos or [v["file"] for v in sessions.load(s)["videos"] if v.get("duration")]
    for c in clips:
        try:
            r = osd.automatch(s, c, write=a.write, fps=a.fps, overwrite=a.overwrite)
        except Exception as e:  # noqa: BLE001  (a clip still syncing, no log, ...)
            print(f"{c}: {type(e).__name__}: {e}", file=sys.stderr)
            continue
        print(f"{r['clip']}: {r['readable']}/{r['readings']} frames readable, {len(r['video_arms'])} arm runs, {len(r['matches'])} matched" + (" (written)" if a.write else ""))
        for m in r["matches"]:
            print(f"   arm {m['arm']:2} @ +{m['offset']:7.2f}  video run {m['video_length']:5.1f} s vs log {m['log_length']:5.1f} s" + ("" if not a.write else ("  written" if m.get("written") else "  kept existing")))
        for v in r["unmatched_video_arms"]:
            print(f"   unmatched run at {fmt_time(v['start'])}: {v['length']} s")


def cmd_osd_learn(a):
    labels = json.load(open(a.labels))
    out(osd.learn(labels))


def cmd_serve(a):
    from .server import serve
    serve(a.host, a.port, warm=not a.no_warm)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="acroscope", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    class Version(argparse.Action):   # the store is opened only when asked: `--help` must not create a database
        def __call__(self, parser, ns, values, option_string=None):
            print(f"acroscope (data {DATA_DIR}, cache {CACHE_DIR}, store {sessions.store()})"); parser.exit()
    ap.add_argument("--version", action=Version, nargs=0)
    sp = ap.add_subparsers(dest="cmd", required=True)

    p = sp.add_parser("sessions", help="list sessions, or show one (videos, arms, matches, moments)")
    p.add_argument("session", nargs="?"); p.add_argument("--json", action="store_true"); p.set_defaults(f=cmd_sessions)
    p = sp.add_parser("refresh", help="bring a session in line with its folder: new clips in, gone clips marked missing")
    p.add_argument("session"); p.add_argument("--no-probe", action="store_true", help="skip ffprobe (durations)"); p.set_defaults(f=cmd_refresh)
    p = sp.add_parser("scan", help="register every session folder in the database (the server also does this every 5 min)"); p.set_defaults(f=cmd_scan)
    p = sp.add_parser("note", help="set the note of a session, or of one clip")
    p.add_argument("session"); p.add_argument("text"); p.add_argument("--clip"); p.set_defaults(f=cmd_note)
    p = sp.add_parser("attach", help="add a blackbox file to a session (a log dated another day)")
    p.add_argument("session"); p.add_argument("bbl"); p.set_defaults(f=cmd_attach)
    p = sp.add_parser("detach", help="remove a blackbox file from a session"); p.add_argument("session"); p.add_argument("bbl"); p.set_defaults(f=cmd_detach)
    p = sp.add_parser("purge", help="remove clips marked missing (one clip, or with --yes every one missing for more than --days), with their matches and moments")
    p.add_argument("session"); p.add_argument("clip", nargs="?"); p.add_argument("--days", type=float, default=7); p.add_argument("--yes", action="store_true")
    p.set_defaults(f=cmd_purge)
    p = sp.add_parser("import-json", help="one-off: import the pre-database videos/<session>/session.json files")
    p.add_argument("session", nargs="?"); p.set_defaults(f=cmd_import_json)
    p = sp.add_parser("tags", help="the tag vocabulary by category, with how many moments use each tag")
    p.add_argument("session", nargs="?", help="count in one session only"); p.add_argument("--json", action="store_true"); p.set_defaults(f=cmd_tags)
    p = sp.add_parser("tagdef", help="define a tag in a category (or move it there); --rm drops the definition")
    p.add_argument("name"); p.add_argument("--category"); p.add_argument("--color", help="#rrggbb; '' clears it, the category colour is used then")
    p.add_argument("--rm", action="store_true"); p.set_defaults(f=cmd_tagdef)
    p = sp.add_parser("category", help="create a tag category (needs --color) or change its colour; --rm drops an empty one")
    p.add_argument("name"); p.add_argument("--color"); p.add_argument("--rm", action="store_true"); p.set_defaults(f=cmd_category)
    p = sp.add_parser("arms", help="list the arms of a .bbl (decodes every arm into the cache once)")
    p.add_argument("bbl"); p.add_argument("--force", action="store_true"); p.add_argument("--json", action="store_true"); p.set_defaults(f=cmd_arms)
    p = sp.add_parser("decode", help="decode one arm into the cache; --csv dumps it like blackbox_decode")
    p.add_argument("bbl"); p.add_argument("arm", type=int); p.add_argument("--csv", action="store_true"); p.set_defaults(f=cmd_decode)
    p = sp.add_parser("match", help="record video <-> arm with offset (video time = arm time + offset)")
    p.add_argument("session"); p.add_argument("video"); p.add_argument("bbl"); p.add_argument("arm", type=int)
    p.add_argument("offset", type=float); p.add_argument("--note")
    p.add_argument("--boot", action="store_true", help="also match every other arm of the same power cycle (offsets derived from the FC uptime)")
    p.set_defaults(f=cmd_match)
    p = sp.add_parser("unmatch", help="remove a clip's matches (all, or one arm)")
    p.add_argument("session"); p.add_argument("video"); p.add_argument("arm", type=int, nargs="?"); p.set_defaults(f=cmd_unmatch)
    p = sp.add_parser("metrics", help="numbers for a window: summary + rotation segments; --profile for a table")
    p.add_argument("session", nargs="?"); p.add_argument("video", nargs="?"); p.add_argument("start"); p.add_argument("end")
    p.add_argument("--arm", help="bbl:N instead of session/video (times are then arm seconds)")
    p.add_argument("--arm-time", action="store_true", help="start/end are arm seconds, not video seconds")
    p.add_argument("--profile", type=float, nargs="?", const=0.2, metavar="STEP", help="per-STEP table (default 0.2 s)")
    p.add_argument("--loop", action="store_true", help="add the powerloop summary"); p.add_argument("--orbit", action="store_true", help="add the orbit table")
    p.add_argument("--json", action="store_true"); p.set_defaults(f=cmd_metrics)
    p = sp.add_parser("events", help="suggested moments from the detectors (flips, loops, split-S, dives, crashes, kicks, motor loss)")
    p.add_argument("session", nargs="?"); p.add_argument("video", nargs="?"); p.add_argument("--arm", help="bbl:N")
    p.add_argument("--matched-only", action="store_true"); p.add_argument("--json", action="store_true"); p.set_defaults(f=cmd_events)
    p = sp.add_parser("frame", help="extract frames as JPEG (prints the paths); --osd crops and enlarges the OSD strip")
    p.add_argument("session"); p.add_argument("video"); p.add_argument("times", nargs="+"); p.add_argument("--osd", action="store_true")
    p.add_argument("--scale", type=int, default=1); p.set_defaults(f=cmd_frame)
    p = sp.add_parser("sheet", help="one JPEG tiling several timestamped frames (given times, or --from/--to/--every)")
    p.add_argument("session"); p.add_argument("video"); p.add_argument("times", nargs="*")
    p.add_argument("--from", dest="start"); p.add_argument("--to", dest="end"); p.add_argument("--every", type=float, metavar="SEC")
    p.add_argument("--cols", type=int, default=3); p.add_argument("--width", type=int, default=360, help="frame width in the sheet"); p.add_argument("--osd", action="store_true")
    p.set_defaults(f=cmd_sheet)
    p = sp.add_parser("tag", help="add (or with --id replace) a moment")
    p.add_argument("session"); p.add_argument("video"); p.add_argument("start"); p.add_argument("end"); p.add_argument("title")
    p.add_argument("--tags", help=f"comma-separated, e.g. {','.join(TAGS[:4])}"); p.add_argument("--note"); p.add_argument("--id")
    p.add_argument("--metrics", action="store_true", help="store a metrics snapshot (needs a match)"); p.set_defaults(f=cmd_tag)
    p = sp.add_parser("untag", help="remove a moment by id"); p.add_argument("session"); p.add_argument("id"); p.set_defaults(f=cmd_untag)
    p = sp.add_parser("moments", help="list a session's moments"); p.add_argument("session"); p.add_argument("--json", action="store_true"); p.set_defaults(f=cmd_moments)
    p = sp.add_parser("import-xspf", help="one-off: import videos/<session>/moments.xspf into the session file"); p.add_argument("session"); p.set_defaults(f=cmd_import_xspf)
    p = sp.add_parser("transcode", help="H.264 for the player: proxies in the cache, or --replace the clips in the data dir")
    p.add_argument("session"); p.add_argument("videos", nargs="*"); p.add_argument("--replace", action="store_true"); p.add_argument("--force", action="store_true"); p.set_defaults(f=cmd_transcode)
    p = sp.add_parser("osd", help="read the OSD timers through a clip and list the arm runs")
    p.add_argument("session"); p.add_argument("video"); p.add_argument("--fps", type=float, default=2.0); p.add_argument("--json", action="store_true"); p.set_defaults(f=cmd_osd)
    p = sp.add_parser("automatch", help="match clips to arms from the OSD timers (proposes; --write records)")
    p.add_argument("session"); p.add_argument("videos", nargs="*"); p.add_argument("--write", action="store_true"); p.add_argument("--overwrite", action="store_true", help="replace existing matches of the clip too")
    p.add_argument("--fps", type=float, default=2.0); p.set_defaults(f=cmd_automatch)
    p = sp.add_parser("osd-learn", help="rebuild the OSD digit templates from a labels file")
    p.add_argument("labels", nargs="?", default="scripts/osd-labels.json"); p.set_defaults(f=cmd_osd_learn)
    p = sp.add_parser("serve", help="run the player (http)"); p.add_argument("--host", default="0.0.0.0"); p.add_argument("--port", type=int, default=8070)
    p.add_argument("--no-warm", action="store_true", help="don't decode/probe everything in the background at start"); p.set_defaults(f=cmd_serve)

    a = ap.parse_args(argv)
    a.f(a)


if __name__ == "__main__":
    main()
