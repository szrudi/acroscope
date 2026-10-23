"""Reading the raw inputs: arms, fetch-log, decode (the blackbox), frame, sheet, transcode (the clips), osd and
osd-profile (the OSD in the picture, and the profiles it is read with).
"""
import argparse
import sys
from pathlib import Path

from .. import tricks
from ..sources import betaflight, blackbox, osd, osdprofile, video
from ..store import sessions
from ..config import CACHE_DIR
from .common import _segment_count, _session, fmt_time, out, parse_time


def cmd_arms(a):
    idx = blackbox.index_bbl(a.bbl, force=a.force, progress=lambda m: print(m, file=sys.stderr))
    try:
        nbytes = {s["index"]: s["size"] for s in blackbox.segments(a.bbl)}
    except Exception:  # noqa: BLE001  (the sizes are extra: a listing from the cache without the file still prints)
        nbytes = {}
    if a.json:       # the agent reads this, not the listing: each segment says its size in bytes (#21)
        return out(idx | {"arms": [r | ({"size": nbytes[r["index"]]} if r["index"] in nbytes else {}) for r in idx["arms"]]})
    size = {i: _bytes(n) for i, n in nbytes.items()}
    print(f"{idx['file']}  ({_segment_count(idx['arms'])})")
    for r in idx["arms"]:
        if not r.get("frames"):      # a segment without frames is no arm: say why, so missing and unreadable differ (#21)
            print(f"  segment {r['index']:2}  no frames: {r.get('error') or 'empty'}  {size.get(r['index'], '')}".rstrip())
            continue
        flags = ("" if r.get("motors_spun", True) else " motors-off") + ("" if r.get("complete", True) else " cut-off") + (" TRUNCATED" if r.get("truncated") else "")
        v = {k: "-" if r.get(k) is None else f"{r[k]:.2f}" for k in ("vbat_start", "vbat_end", "vbat_min")}   # an arm can log no vbat
        print(f"  arm {r['index']:2}  {r['length']:6.1f} s  uptime {r.get('uptime_start') or 0:7.1f}-{r.get('uptime_end') or 0:7.1f}  {v['vbat_start']}→{v['vbat_end']} V (min {v['vbat_min']})  disarm {r.get('disarm_reason')}  {size.get(r['index'], '')}{flags}")
    for b in idx.get("boots", []):
        print(f"  boot: arms {b['first']}-{b['last']} ({len(b['arms'])} arms, uptime {b['uptime_start']:.1f}-{b['uptime_end']:.1f} s, {b['length']:.0f} s span): one offset covers them all (`match --boot`)")


def _bytes(n: int) -> str:
    return f"{n} B" if n < 1000 else f"{n / 1000:.0f} KB" if n < 1_000_000 else f"{n / 1e6:.1f} MB"


def cmd_fetch_log(a):
    """The .bbl as it is, on this machine (#21): from the server through GET /blackbox/<file> (the player's ⤓), by its
    file name or a fragment unique among the logs the sessions know, into --to or the cache's blackbox/. Not proxied:
    it runs here. With a local store the log is already here: its path is printed, or it is copied into --to."""
    import shutil
    if sessions.remote():
        r = sessions.store()
        name = a.bbl
        if not name.lower().endswith(".bbl"):
            hits = sorted({b for s in r.sessions() for b in s.get("blackbox", []) if name in b})
            if len(hits) != 1:
                raise FileNotFoundError(f"{name}: {len(hits)} logs match (need exactly 1)" + (": " + ", ".join(hits) if hits else ""))
            name = hits[0]
        dest = Path(a.to or CACHE_DIR / "blackbox") / Path(name).name
        dest.parent.mkdir(parents=True, exist_ok=True)
        r.fetch_log(name, dest)
    else:
        src = blackbox.resolve_bbl(a.bbl)
        if not a.to:
            return print(src)
        dest = Path(a.to) / src.name
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dest)
    print(dest)


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


def cmd_frame(a):
    s = _session(a.session)
    p = sessions.clip_path(s, a.video)
    dur = video.probe(p)["duration"]
    for t in a.times:
        tt = parse_time(t)
        if tt > dur - 0.1:
            print(f"{t}: past the end of the clip ({fmt_time(dur)}), skipped", file=sys.stderr)
            continue
        print(video.frame(p, tt, osd=a.osd, scale=a.scale))


def cmd_sheet(a):
    s = _session(a.session)
    p = sessions.clip_path(s, a.video)
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


def cmd_transcode(a):
    s = _session(a.session)
    sd = sessions.load(s)
    files = [sessions.clip_path(s, v) for v in a.videos] or [sessions.clip_path(s, v["file"]) for v in sd["videos"]]
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
    rt = osd.read_timers(sessions.clip_path(s, a.video), a.fps)
    if a.json:
        return out(rt)
    rows, cum = rt["rows"], osd.layouts()[rt["layout"]]["cumulative"]
    ok = [r for r in rows if (r["bottom"] if cum else r["top"]) is not None]
    print(f"layout {rt['layout']}: {len(rows)} frames, {len(ok)} with a readable {'total' if cum else 'arm'} timer")
    runs = osd.video_arms(rt, a.fps)
    for v, st in zip(runs, osd.read_stats(sessions.clip_path(s, a.video), runs, rt["layout"])):
        stats = "" if st.get("total_arm") is None else f"; STATS after: total arm {fmt_time(st['total_arm'])}, blackbox {st['blackbox']} %"
        print(f"  arm run: video {fmt_time(v['start'])} - {fmt_time(v['end'])}  {v['length']:5.1f} s  (timer {v['timer_max']} s, total before {v['total_before']}, {v['readings']} readings{stats})")


def _profile_summary(name: str, lay: dict) -> str:
    d = lay["derived"]
    rows = []
    for t in d["timers"]:
        rows.append(f"timer {t['n']} {t['source']} {t['format']} at column {t['col']} row {t['row']}")
    out = [f"{name}: font {d['font']}, {d.get('version') or 'firmware not named'}{', craft ' + d['craft'] if d.get('craft') else ''}",
           "  " + "; ".join(rows),
           f"  cells: digits from x {lay['x0']} on rows y {lay['rows']}, glyph box {list(lay['glyph'])}, grid x {lay['grid']['x']} y {lay['grid']['y']} "
           f"cell {lay['grid']['cw']} x {lay['grid']['ch']} on {lay['frame'][0]} x {lay['frame'][1]}"]
    if lay.get("counter"):
        out.append(f"  flight counter: digits from x {lay['counter']['x0']}, y {lay['counter']['y']}")
    out += [f"  warning: {w}" for w in lay.get("warnings", [])]
    return "\n".join(out)


def cmd_osd_profile(a):
    """OSD profiles derived from Betaflight's own dump and font (#10): list | show <name> | derive <dump> |
    save <name> <dump> | rm <name> | read <name> <session> <clip> <t> [--fit | --fonts]."""
    store = sessions.store()
    row = store.setting(osdprofile.PROFILES_KEY)
    doc = row["value"] if row else {"version": 1, "profiles": {}}

    def made():
        grid = None
        if a.grid:
            try:
                x, y = (int(v) for v in a.grid.split(","))
            except ValueError:
                raise ValueError(f"--grid {a.grid}: x,y of column 0, row 0 in pixels (3,-4)") from None
            grid = {"x": x, "y": y}
        if not a.font and not a.mcm:
            raise ValueError(f"osd-profile {a.what}: --font <{'|'.join(betaflight.FONTS)}> or --mcm <the .mcm of your own>")
        mcm = Path(a.mcm).read_text() if a.mcm else None
        font = a.font or (Path(a.mcm).stem.lower() if a.mcm else None)     # your own font: named after its file, as on the page
        return osdprofile.make(Path(a.dump).read_text(errors="replace"), font=font, mcm=mcm, grid=grid, note=a.note or "")
    what = a.what or "list"
    if what == "list":
        d = osdprofile.describe(doc)
        if a.json:
            return out(d)
        for name in d["profiles"]:
            print(_profile_summary(name, osdprofile.derive(doc["profiles"][name], name)))
        for name, e in d["errors"].items():
            print(f"{name}: does not derive: {e}")
        if not d["profiles"] and not d["errors"]:
            print(f"no OSD profiles; the built-in layouts: {', '.join(d['builtin'])}")
        return
    if what == "derive":
        lay = osdprofile.derive(made(), "derived")
        return out(osdprofile._public(lay)) if a.json else print(_profile_summary("derived", lay))
    if what == "save":
        new = {**doc, "version": 1, "profiles": {**doc.get("profiles", {}), a.name: made()}}
        osdprofile.check_profiles(new, row["value"] if row else None)
        store.set_setting(osdprofile.PROFILES_KEY, new, etag=tricks.layer_etag(row["value"] if row else None))
        return print(_profile_summary(a.name, osdprofile.derive(new["profiles"][a.name], a.name)))
    if a.name not in doc.get("profiles", {}):
        raise ValueError(f"{a.name}: no such OSD profile ({', '.join(doc.get('profiles', {})) or 'none stored'})")
    p = doc["profiles"][a.name]
    if what == "show":
        lay = osdprofile.derive(p, a.name)
        return out({"profile": {k: v for k, v in p.items() if k != "font"} | {"font": p["font"]["name"]}, "layout": osdprofile._public(lay)}) if a.json \
            else print(_profile_summary(a.name, lay))
    if what == "rm":
        new = {**doc, "profiles": {k: v for k, v in doc["profiles"].items() if k != a.name}}
        store.set_setting(osdprofile.PROFILES_KEY, new, etag=tricks.layer_etag(doc))
        return print(f"{a.name}: removed")
    # read: on the frame at that time (here or, through the proxy, on the server where the clips are)
    path = sessions.clip_path(_session(a.session), a.clip)
    lay = osdprofile.derive(p, a.name)
    buf = osdprofile.frame(path, parse_time(a.time), lay)
    if a.fit:
        return out(osdprofile.fit([buf], p))
    if a.fonts:
        return out(osdprofile.rank_fonts([buf], p))
    r = osdprofile.probe(buf, lay)
    if a.json:
        return out(r)
    v = r["values"]
    print(f"{a.name} on {path.name} at {a.time}: arm {fmt_time(v['arm']) if v['arm'] is not None else '-'}, total {fmt_time(v['total']) if v['total'] is not None else '-'}"
          f"{', flight #' + str(r['counter']) if r['counter'] is not None else ''}"
          f"{', STATS total arm ' + fmt_time(r['stats']['total_arm']) + ', blackbox ' + str(r['stats']['blackbox']) + ' %' if r['stats'] and r['stats'].get('total_arm') is not None else ''}")
    for c in r["cells"]:
        if c["fmt"] == "d":
            print(f"  {c['kind']:7} {'row ' + str(c['row']) if c['row'] is not None else '':6} x {c['x']:3} y {c['y']:3}: {c['digit'] or '?'}  score {c['score']:.2f}  margin {c['margin']:.2f}")


def add(sp) -> None:
    """This group's subcommands."""
    p = sp.add_parser("arms", help="list the arms of a .bbl (decodes every arm into the cache once)")
    p.add_argument("bbl"); p.add_argument("--force", action="store_true"); p.add_argument("--json", action="store_true"); p.set_defaults(f=cmd_arms)
    p = sp.add_parser("fetch-log", help="the .bbl as it is, from the server onto this machine (for blackbox_decode or Blackbox Explorer)")
    p.add_argument("bbl", help="file name, or a fragment unique among the logs the sessions know")
    p.add_argument("--to", help="directory to save into (default: the cache's blackbox/)"); p.set_defaults(f=cmd_fetch_log)
    p = sp.add_parser("decode", help="decode one arm into the cache; --csv dumps it like blackbox_decode")
    p.add_argument("bbl"); p.add_argument("arm", type=int); p.add_argument("--csv", action="store_true"); p.set_defaults(f=cmd_decode)
    p = sp.add_parser("frame", help="extract frames as JPEG (prints the paths); --osd crops and enlarges the OSD strip")
    p.add_argument("session"); p.add_argument("video"); p.add_argument("times", nargs="+"); p.add_argument("--osd", action="store_true")
    p.add_argument("--scale", type=int, default=1); p.set_defaults(f=cmd_frame)
    p = sp.add_parser("sheet", help="one JPEG tiling several timestamped frames (given times, or --from/--to/--every)")
    p.add_argument("session"); p.add_argument("video"); p.add_argument("times", nargs="*")
    p.add_argument("--from", dest="start"); p.add_argument("--to", dest="end"); p.add_argument("--every", type=float, metavar="SEC")
    p.add_argument("--cols", type=int, default=3); p.add_argument("--width", type=int, default=360, help="frame width in the sheet"); p.add_argument("--osd", action="store_true")
    p.set_defaults(f=cmd_sheet)
    p = sp.add_parser("transcode", help="H.264 for the player: proxies in the cache, or --replace the clips in the data dir")
    p.add_argument("session"); p.add_argument("videos", nargs="*"); p.add_argument("--replace", action="store_true"); p.add_argument("--force", action="store_true"); p.set_defaults(f=cmd_transcode)
    p = sp.add_parser("osd", help="read the OSD timers through a clip and list the arm runs")
    p.add_argument("session"); p.add_argument("video"); p.add_argument("--fps", type=float, default=2.0); p.add_argument("--json", action="store_true"); p.set_defaults(f=cmd_osd)
    p = sp.add_parser("osd-profile", help="OSD profiles derived from a Betaflight dump and font: list | show <name> | derive <dump> | save <name> <dump> | rm <name> | "
                                          "read <name> <session> <clip> <t> (what the profile reads on that frame; --fit: the grid that reads best; --fonts: the fonts ranked)")
    p.add_argument("--json", action="store_true"); p.set_defaults(f=cmd_osd_profile, what=None)
    op = p.add_subparsers(dest="what")              # --json before or after the subcommand (SUPPRESS: the sub's does not reset it)

    def making(q):                                      # what makes a profile: a dump and a font
        q.add_argument("--font", help=f"a Configurator font: {', '.join(betaflight.FONTS)}"); q.add_argument("--mcm", help="the .mcm file of your own font")
        q.add_argument("--grid", help="x,y of column 0, row 0 on the 720 x 480 frame (default 3,-4: the DVR flown here)")
        q.add_argument("--note")
    q = op.add_parser("list", help="the stored profiles"); q.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    q = op.add_parser("show", help="one stored profile, derived"); q.add_argument("name"); q.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    q = op.add_parser("derive", help="a profile from a dump file, not stored"); q.add_argument("dump"); making(q); q.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    q = op.add_parser("save", help="a profile from a dump file, stored under a name"); q.add_argument("name"); q.add_argument("dump"); making(q)
    q = op.add_parser("rm", help="remove a stored profile"); q.add_argument("name")
    q = op.add_parser("read", help="what a stored profile reads on one frame (on the server, where the clips are)")
    q.add_argument("name"); q.add_argument("session"); q.add_argument("clip"); q.add_argument("time")
    q.add_argument("--fit", action="store_true", help="the grid that reads that frame best"); q.add_argument("--fonts", action="store_true", help="the Configurator fonts ranked on it")
    q.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
