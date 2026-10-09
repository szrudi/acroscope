"""acroscope command line: the agent side. Every command prints JSON unless it is a table for humans.

Times: video seconds or m:ss.s ('1:32.7'). Sessions and clips can be given by a unique fragment ('schammer', '007').
"""
import argparse
import json
import sys

from . import blackbox, events as ev, metrics, sessions, video, xspf
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
        print(f"{'session':40} {'clips':>5} {'file':>4} {'match':>5} {'mom.':>4}  blackbox")
        for r in rows:
            print(f"{r['session']:40} {r['videos']:5} {'yes' if r['has_file'] else '-':>4} {r['matches']:5} {r['moments']:4}  {', '.join(b[25:40] for b in r['blackbox'])}")
        return
    s = sessions.overview(_session(a.session))
    if a.json:
        return out(s)
    print(f"# {s['session']}  {s.get('note', '')}")
    print(f"blackbox: {', '.join(s['blackbox']) or '-'}")
    print("\n## videos")
    for v in s["videos"]:
        ms = [m for m in s["matches"] if m["video"] == v["file"]]
        cov = "; ".join(f"arm {m['arm']} @ +{m['offset']}s" for m in ms) or "no log"
        d = fmt_time(v["duration"]) if v.get("duration") else "?"
        print(f"  {v['file']:24} {d:>7}  {cov}" + (f"  | {v['note']}" if v.get("note") else ""))
    print("\n## arms")
    for r in s.get("arms", []):
        vids = ", ".join(f"{x['video'][-7:-4]} +{x['offset']}" for x in r["videos"]) or "no clip"
        flags = ("" if r["motors_spun"] else " (motors off)") + ("" if r["complete"] else " (cut off)")
        print(f"  arm {r['index']:2}  {r['length']:6.1f} s  {r['vbat_start']:.2f}→{r['vbat_end']:.2f} V  {vids}{flags}")
    print("\n## moments")
    for m in s["moments"]:
        print(f"  {m['id']:4} {m['video'][-7:-4]} {fmt_time(m['start'])}-{fmt_time(m['end'])}  {m['title']}  [{', '.join(m['tags'])}]" + (f"  | {m['note']}" if m.get("note") else ""))


def cmd_refresh(a):
    s = sessions.refresh(_session(a.session), probe_videos=not a.no_probe)
    out({"session": s["session"], "videos": len(s["videos"]), "blackbox": s["blackbox"], "path": str(sessions.session_path(s["session"]))})


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
    res = dict(head, window={"arm_from": round(a0, 2), "arm_to": round(a1, 2)})
    if a.profile:
        rows = metrics.profile(arm, a0, a1, a.profile)
        if a.json:
            res["profile"] = rows
        else:
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


def cmd_serve(a):
    from .server import serve
    serve(a.host, a.port, warm=not a.no_warm)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="acroscope", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--version", action="version", version=f"acroscope (data {DATA_DIR}, cache {CACHE_DIR})")
    sp = ap.add_subparsers(dest="cmd", required=True)

    p = sp.add_parser("sessions", help="list sessions, or show one (videos, arms, matches, moments)")
    p.add_argument("session", nargs="?"); p.add_argument("--json", action="store_true"); p.set_defaults(f=cmd_sessions)
    p = sp.add_parser("refresh", help="create/update videos/<session>/session.json from the folder")
    p.add_argument("session"); p.add_argument("--no-probe", action="store_true", help="skip ffprobe (durations)"); p.set_defaults(f=cmd_refresh)
    p = sp.add_parser("arms", help="list the arms of a .bbl (decodes every arm into the cache once)")
    p.add_argument("bbl"); p.add_argument("--force", action="store_true"); p.add_argument("--json", action="store_true"); p.set_defaults(f=cmd_arms)
    p = sp.add_parser("decode", help="decode one arm into the cache; --csv dumps it like blackbox_decode")
    p.add_argument("bbl"); p.add_argument("arm", type=int); p.add_argument("--csv", action="store_true"); p.set_defaults(f=cmd_decode)
    p = sp.add_parser("match", help="record video <-> arm with offset (video time = arm time + offset)")
    p.add_argument("session"); p.add_argument("video"); p.add_argument("bbl"); p.add_argument("arm", type=int)
    p.add_argument("offset", type=float); p.add_argument("--note")
    p.add_argument("--boot", action="store_true", help="also match every other arm of the same power cycle (offsets derived from the FC uptime)")
    p.set_defaults(f=cmd_match)
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
    p = sp.add_parser("tag", help="add (or with --id replace) a moment")
    p.add_argument("session"); p.add_argument("video"); p.add_argument("start"); p.add_argument("end"); p.add_argument("title")
    p.add_argument("--tags", help=f"comma-separated, e.g. {','.join(TAGS[:4])}"); p.add_argument("--note"); p.add_argument("--id")
    p.add_argument("--metrics", action="store_true", help="store a metrics snapshot (needs a match)"); p.set_defaults(f=cmd_tag)
    p = sp.add_parser("untag", help="remove a moment by id"); p.add_argument("session"); p.add_argument("id"); p.set_defaults(f=cmd_untag)
    p = sp.add_parser("moments", help="list a session's moments"); p.add_argument("session"); p.add_argument("--json", action="store_true"); p.set_defaults(f=cmd_moments)
    p = sp.add_parser("import-xspf", help="one-off: import videos/<session>/moments.xspf into the session file"); p.add_argument("session"); p.set_defaults(f=cmd_import_xspf)
    p = sp.add_parser("transcode", help="H.264 for the player: proxies in the cache, or --replace the clips in the data dir")
    p.add_argument("session"); p.add_argument("videos", nargs="*"); p.add_argument("--replace", action="store_true"); p.add_argument("--force", action="store_true"); p.set_defaults(f=cmd_transcode)
    p = sp.add_parser("serve", help="run the player (http)"); p.add_argument("--host", default="0.0.0.0"); p.add_argument("--port", type=int, default=8070)
    p.add_argument("--no-warm", action="store_true", help="don't decode/probe everything in the background at start"); p.set_defaults(f=cmd_serve)

    a = ap.parse_args(argv)
    a.f(a)


if __name__ == "__main__":
    main()
