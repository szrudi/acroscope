"""acroscope command line: the agent side. Every command prints JSON unless it is a table for humans.

Times: video seconds or m:ss.s ('1:32.7'). Sessions and clips can be given by a unique fragment ('schammer', '007').
Matches, moments and notes live in the server's database: with ACROSCOPE_URL set, this CLI is a client of that
server; without it, it opens the local database (ACROSCOPE_DB) itself.
"""
import argparse
import json
import sys
from pathlib import Path

from . import blackbox, ingest, metrics, osd, sessions, share, tricks, video, xspf
from .config import CACHE_DIR, DATA_DIR, DB_PATH, SERVER_URL, TAGS
from .remote import RemoteError


def parse_time(s: str) -> float:
    if ":" in s:
        m, _, sec = s.rpartition(":")
        return int(m) * 60 + float(sec)
    return float(s)


def fmt_time(t: float) -> str:
    t = round(t, 1)                               # round first, or 59.96 prints as 0:60.0
    m, s = divmod(abs(t), 60)
    return ("-" if t < 0 else "") + f"{int(m)}:{s:04.1f}"    # before the clip's start: signed, not 0:00.0


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
        if v.get("timescale"):
            gone += f"  RE-TIMED x{v['timescale']:g} (mis-stamped frame rate, no sound)"
        label = v["file"] if v.get("name") in (None, Path(v["file"]).stem) else f"{v['name']} ({v['file']})"
        print(f"  {label:24} {d:>7}  {cov}{gone}" + (f"  | {v['note']}" if v.get("note") else ""))
    print("\n## arms")
    for b in s["blackbox"]:                       # one block per log, so three "arm 4" rows can't be confused
        rows = [r for r in s.get("arms", []) if r["bbl"] == b]
        if b in s.get("arms_pending", []):
            print(f"  {b}: not decoded yet (run `acroscope arms {b[25:40]}`)")
            continue
        if not rows:
            print(f"  {b}: no arms" + ("" if (DATA_DIR / 'blackbox' / b).exists() else " (file not found)"))
            continue
        print(f"  {b}  ({_segment_count(rows)}, {sum(1 for r in rows if r['videos'])} with a clip)")
        names = {v["file"]: (v.get("name") or v["file"]) for v in s["videos"]}
        for r in rows:
            if r.get("frames") == 0:
                print(f"    segment {r['index']:2}  no frames: {r.get('error') or 'empty'}")
                continue
            vids = ", ".join(f"{names.get(x['video'], x['video'])[-3:]} +{x['offset']}" for x in r["videos"]) or "no clip"
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


def cmd_session(a):
    s = _session(a.session)
    if a.action == "rename":
        r = sessions.rename_session(s, a.arg)
    elif a.action == "date":
        r = sessions.set_date(s, a.arg)
    elif a.action == "move":
        r = sessions.move_clip(s, a.arg, a.to)
    elif a.action == "merge":
        r = sessions.merge_sessions(s, a.arg)
    elif a.action == "delete":
        return out(sessions.delete_clip(s, a.arg))
    out({"session": r["session"], "videos": len(r["videos"]), "matches": len(r["matches"]), "moments": len(r["moments"])})


def cmd_inbox(a):
    rows = ingest.list_inbox()
    if a.json:
        return out(rows)
    if not rows:
        return print(f"inbox empty ({ingest.INBOX_DIR})")
    for b in rows:
        extra = f"  -> {b['session']}" if b.get("session") else ""
        prog = b.get("progress") or {}
        p = f"  {prog.get('step', '')} {prog.get('clip', '')} {prog.get('pct', '')}%".rstrip(" %") if prog else ""
        err = f"  ERROR {b['error']}" if b.get("error") else ""
        print(f"  {b['id']:30} {b['status']:9} {b.get('clips', '?'):>3} clips {b.get('logs', '?'):>2} logs{extra}{p}{err}")


def cmd_ingest(a):
    if sessions.remote():
        sys.exit("ingest runs where the data is: on the server (unset ACROSCOPE_URL there)")
    batches = a.batches or ingest.pending()
    if not batches:
        return print("nothing to ingest")
    failed = 0
    for b in batches:
        try:
            out(ingest.process_batch(b, log=lambda m: print(m, file=sys.stderr)))
        except Exception as e:  # noqa: BLE001
            print(f"{b}: FAILED {type(e).__name__}: {e}", file=sys.stderr)
            failed += 1
    if failed:
        sys.exit(1)


def cmd_recut(a):
    out(ingest.recut(_session(a.session), a.clip, log=lambda m: print(m, file=sys.stderr)))


def cmd_housekeeping(a):
    if sessions.remote():
        sys.exit("housekeeping runs on the server")
    out({"originals_removed": ingest.cleanup_originals(log=lambda m: print(m, file=sys.stderr)),
         "trash_emptied": ingest.cleanup_trash(log=lambda m: print(m, file=sys.stderr)),
         "shares_taken_down": share.cleanup_expired(log=lambda m: print(m, file=sys.stderr)),
         "shares_refreshed": share.refresh_pages(log=lambda m: print(m, file=sys.stderr)),
         "backup": str(ingest.backup_db() or "")})


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


def _segment_count(segs: list[dict]) -> str:
    """'60 arms, 1 segment without frames': a segment without frames (unreadable, or empty) is no arm."""
    bad = sum(1 for r in segs if not r.get("frames", 1))
    return f"{len(segs) - bad} arms" + (f", {bad} segment{'' if bad == 1 else 's'} without frames" if bad else "")


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


def cmd_match(a):
    session = _session(a.session)
    vid = sessions.set_match(session, a.video, a.bbl, a.arm, a.offset, a.note or "", boot=a.boot)[0]["video"]
    out([m for m in sessions.load(session)["matches"] if m["video"] == vid])


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
        vid = sessions.resolve_clip(s["session"], a.video)
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
            print("conventions: roll + = right, pitch + = nose down (back flip = negative), yaw + = nose left; sticks in %, gyro deg/s, tilt 0 level / 180 inverted")
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
        vid = sessions.resolve_clip(s["session"], a.video)
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
        for e in tricks.detect(arm):
            r = {"bbl": arm.bbl[25:40], "arm": arm.index, **e}
            if m:
                r["video"], r["start"], r["end"] = m["video"], round(e["arm_from"] + m["offset"], 2), round(e["arm_to"] + m["offset"], 2)
            rows.append(r)
    if a.json:
        return out(rows)
    for r in rows:
        where = f"{r['video'][-7:-4]} {fmt_time(r['start'])}-{fmt_time(r['end'])}" if "video" in r else f"arm {r['arm']:2} {r['arm_from']:6.2f}-{r['arm_to']:6.2f} (no clip)"
        print(f"{where:28} {r['tag']:10} {r['title']:24} | {r['why']}")


def cmd_tricks(a):
    """The tricks file(s) in force (#22): list them, show one, the feature names, run the examples, or explain an
    arm window by window. Proxied like `events`: the server's library is the one that detects."""
    what, args = a.what, a.args
    if what in ("set", "unset", "on", "off", "skill"):
        return _edit_tricks(a)
    if what == "layer":
        row = sessions.store().setting(tricks.LAYER_KEY)
        if a.json:
            return out({"layer": row["value"] if row else {}, "updated_at": row["updated_at"] if row else None})
        if not row:
            return print("no pilot's layer in the database (acroscope tricks set|on|off|skill write one)")
        print(f"# the pilot's layer in the database, {row['updated_at']}")
        return out(row["value"])
    if what in ("export", "import"):
        return _share_tricks(a)
    base = tricks.library()
    lib = base.for_craft(a.craft)                      # the library in force: the default layer, or a craft's
    if what == "list":
        if a.json:
            return out(tricks.describe(base, a.craft))
        print(f"# {', '.join(base.sources)}")
        crafts = [n for n in base.crafts if n != "default"]
        print(f"# skill: {base.skill or 'none (the defaults)'}; presets: {', '.join(base.skills) or 'none'}; crafts: {', '.join(crafts) or 'none'}"
              + (f"; showing {a.craft}" if a.craft else ""))
        print(f"{'id':16} {'tag':11} {'segment':28} {'kind':13} prio  on   ex  conditions")
        for t in lib.ordered():
            seg_name = t.segment if not t.phases else " > ".join(ph.segment for ph in t.phases)
            kind = lib.segments[t.segment].kind if not t.phases else f"{len(t.phases)} phases"
            conds = len(t.when) + sum(len(ph.when) for ph in t.phases)
            print(f"{t.id:16} {t.tag:11} {seg_name[:28]:28} {kind:13} {t.priority:4}  {'yes' if t.enabled else 'no ':3} {len(t.examples):3}  {conds}")
        print(f"params: {', '.join(f'{k}={v}' for k, v in lib.params.items())}")
        if base.crafts.get("default"):
            print(f"craft: {', '.join(f'{k}={v}' for k, v in sorted(base.craft_context(a.craft).items()) if v is not None)}")
        return
    if what == "show":
        for tid in args or [None]:
            t = lib.tricks.get(tid)
            if not t:
                raise FileNotFoundError(f"{tid}: no such trick (have {', '.join(lib.tricks)})")
            seg = lib.segments.get(t.segment)
            print(f"[[trick]] {t.id}  ({t.source})")
            print(f"  tag       {t.tag}    priority {t.priority}    {'enabled' if t.enabled else 'DISABLED'}")
            if t.phases:
                for k, ph in enumerate(t.phases):
                    pseg = lib.segments[ph.segment]
                    print(f"  phase {k + 1}   {ph.segment}: {pseg.kind}" + (f", within {ph.within} s" if k else ""))
                    for c in ph.conditions:
                        print(f"            when {c}")
            else:
                print(f"  segment   {t.segment}: {seg.kind} {' '.join(f'{k}={v}' for k, v in seg.opts.items())}")
            for c in t.conditions:
                print(f"  when      {c}")
            print(f"  title     {t.title}\n  why       {t.why}")
            for r in t.suppress:
                print(f"  suppress  {', '.join(r.tags)}: " + (f"windows within {r.margin} s" if r.margin is not None else f"instants within {r.within} s" if r.within is not None
                                                           else f"windows starting from {r.start_after:+} s of the instant on"))
            if lib.requires(t):
                print(f"  requires  {', '.join(lib.requires(t))}")
            for e in t.examples:
                where = f"at {e['at']}" if "at" in e else f"{'none in' if e.get('absent') else 'in'} {e['from']}-{e['to']}"
                print(f"  example   {e['log']} arm {e['arm']} {where}" + (f"  ({e['note']})" if e.get("note") else ""))
            if t.note:
                print(f"  note      {t.note}")
        return
    if what == "features":
        from .tricks import features, segments
        kinds = args or list(segments.KINDS)
        if a.json:
            return out({k: features.vocabulary(k) for k in kinds})
        for k in kinds:
            print(f"# {k}: {', '.join(f'{o}={d}' for o, d in segments.KINDS[k].items())}")
            last = ""
            for name, desc in features.vocabulary(k).items():
                print(f"  {name:28} {desc or last}")
                last = desc or last
        return
    if what == "check":
        res = tricks.check(lib, args or None, a.craft)
        if a.json:
            out(res)
        else:
            for r in res["results"]:
                print(f"{r['status']:8} {r['trick']:16} {r['log'][18:40]} arm {r['arm']:2} {r['where']:14} {r.get('found', '')}")
            print(f"{res['passed']} passed, {res['failed']} failed, {res['unchecked']} unchecked (log not here)")
        if res["failed"]:
            sys.exit(1)
        return
    if what == "eval":
        if not args:
            raise SystemExit("acroscope tricks eval <session>")
        s = sessions.load(_session(args[0]))
        res = tricks.evaluate(s, blackbox.load_arm, lib)
        if a.json:
            return out(res)
        print(f"# {s['session']}: {len(s['moments'])} moments, {len(s['matches'])} matches")
        for tag, c in res["per_tag"].items():
            print(f"{tag:12} {c['found']:3} found, {c['missed']:3} missed")
        for m in res["missed"]:
            print(f"missed {m['tag']:11} {m['id']} {m['video'][-7:-4]} {fmt_time(m['start'])}-{fmt_time(m['end'])} {m['title'][:40]}  arms {', '.join(f'{a}@{t}' for a, t in m['arms'].items())}")
        for e in res["untagged"]:
            print(f"untagged {e['tag']:9} {e['video'][-7:-4]} {fmt_time(e['start'])}-{fmt_time(e['end'])} {e['title'][:36]} (arm {e['arm']} @{e['arm_from']})" + (f"  [{e['moment'][:40]}]" if e["moment"] else ""))
        return
    if what == "explain":
        if not args:
            raise SystemExit("acroscope tricks explain <bbl>:<arm> [from] [to]")
        bbl, _, n = args[0].rpartition(":")
        arm = blackbox.load_arm(bbl, int(n))
        t0 = parse_time(args[1]) if len(args) > 1 else 0.0
        t1 = parse_time(args[2]) if len(args) > 2 else 1e9
        ex = tricks.explain(arm, lib, t0, t1)
        if a.json:
            return out(ex)
        for tid, missing in ex["skipped"].items():
            print(f"# {tid}: skipped, the log has no {', '.join(missing)}")
        for w in ex["windows"]:
            n = w["numbers"]
            rot, pk, thr, tilt = n.get("rotation_deg", {}), n.get("peak_rate_dps", {}), n.get("throttle_pct", {}), n.get("tilt_deg", {})
            line = (f"{w['segment']} {w['arm_from']:.2f}-{w['arm_to']:.2f} ({n.get('duration', 0)} s)  roll {rot.get('roll', 0):+} pitch {rot.get('pitch', 0):+} "
                    f"yaw {rot.get('yaw', 0):+}  peak {max(pk.values(), default=0)}  thr {thr.get('min')}-{thr.get('max')}  tilt {tilt.get('min')}-{tilt.get('max')}")
            extras = {k: v for k, v in n.items() if k in ("axis", "direction", "arm_t", "impact_g", "jolt_dps", "raw_yaw_dps", "motor", "erpm_before", "erpm_after")}
            print(line + ("  " + " ".join(f"{k}={v}" for k, v in extras.items()) if extras else ""))
            for t in w["tricks"]:
                fate = "fired" if t["fired"] else f"failed: {t['failed']}"
                if t["lost_to"]:
                    fate = f"lost to {t['lost_to']}"
                elif t["suppressed_by"]:
                    fate = f"suppressed by {t['suppressed_by']}"
                print(f"    {t['id']:16} {fate}")
        return
    raise SystemExit(f"tricks: {what}: not a subcommand (list, show, features, check, explain, eval, layer, set, unset, on, off, skill, export, import)")


def _share_tricks(a):
    """export [path]: the pilot's layer as a tricks file (stdout, or the file); import <path> [--merge]: a tricks file
    as the layer, whole or over the current one. Both run on this machine against the store (remote or local):
    the file is here, the layer is there."""
    store = sessions.store()
    row = store.setting(tricks.LAYER_KEY)
    if a.what == "export":
        text = tricks.export_toml(row["value"] if row else None)
        if a.args:
            Path(a.args[0]).write_text(text)
            return print(a.args[0])
        return sys.stdout.write(text)
    if len(a.args) != 1:
        raise SystemExit("acroscope tricks import <file.toml> [--merge]")
    text = Path(a.args[0]).read_text()
    table = tricks.import_table(text, row["value"] if row else None, "merge" if a.merge else "replace")
    store.set_setting(tricks.LAYER_KEY, table)
    print(f"{'merged into' if a.merge else 'replaced'} the pilot's layer: {len(table.get('params', {}))} params, {len(table.get('trick', []))} trick entries, "
          f"{len(table.get('craft', {}))} crafts" + (f", skill {table['skill']}" if table.get("skill") else ""))


def _layer_from_store():
    row = sessions.store().setting(tricks.LAYER_KEY)
    return (row["value"], row["updated_at"]) if row else (None, None)


def _edit_tricks(a):
    """set <param> <value> | unset <param> | on <id> | off <id> | skill <name|none>, into the pilot's layer in the
    database ([--craft X]: that craft's section). Checked as a file is before it is stored; a refusal changes nothing."""
    what, args = a.what, a.args
    need = {"set": 2, "unset": 1, "on": 1, "off": 1, "skill": 1}[what]
    if len(args) != need:
        raise SystemExit(f"acroscope tricks {what} takes {need} argument{'s' if need > 1 else ''}" + (" (<param> <value>)" if what == "set" else ""))
    if what == "set":
        args = (args[0], tricks.parse_value(args[1]))
    store = sessions.store()
    row = store.setting(tricks.LAYER_KEY)
    table = tricks.edit_layer(row["value"] if row else None, what, *args, craft=a.craft)
    tricks.check_layer(table)                          # a LibraryError names the place
    store.set_setting(tricks.LAYER_KEY, table, etag=tricks.layer_etag(row["value"] if row else None))
    base = tricks.library()
    lib = base.for_craft(a.craft)
    craft = base.craft_name(a.craft) if a.craft else None
    if what in ("set", "unset"):
        print(f"{args[0]} = {lib.params.get(args[0])}" + (f" for {craft}" if craft else ""))
    elif what in ("on", "off"):
        print(f"{args[0]}: {'on' if lib.tricks[args[0]].enabled else 'off'}" + (f" for {craft}" if craft else ""))
    else:
        print(f"skill: {base.crafts.get(craft, {}).get('skill') if craft else base.skill}")


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


def cmd_tag(a):
    s = _session(a.session)
    start, end = parse_time(a.start), parse_time(a.end)
    tags = [t.strip() for t in (a.tags or "").split(",") if t.strip()]
    snap = None
    if a.metrics:
        sd = sessions.load(s)
        m, arm = _arm_for(sd, sessions.resolve_clip(s, a.video), start)
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


def cmd_share(a):
    """Publish one moment (#57): the clip around it with the sticks drawn over, on a secret link. Runs where the
    clips and the logs are (proxied to the server); prints the share with its url."""
    out(share.render(_session(a.session), a.moment, pad=a.pad, text=a.text, expires=a.expires, sticks=not a.no_sticks,
                     audio=a.audio, poster=parse_time(a.poster) if a.poster else None,
                     start=parse_time(a.start) if a.start else None, end=parse_time(a.end) if a.end else None,
                     token=a.update, log=lambda m: print(m, file=sys.stderr)))


def cmd_unshare(a):
    out(share.revoke(a.token, log=lambda m: print(m, file=sys.stderr)))


def cmd_shares(a):
    rows = share.list_shares(all=a.all)
    if a.json:
        return out(rows)
    if not rows:
        return print("no shares" + ("" if a.all else " (--all lists expired and revoked ones too)"))
    for r in rows:
        until = "never expires" if not r["expires_at"] else f"until {r['expires_at'][:16]}"
        print(f"{r['token']}  {r['state']:8} {r['session']} {r['clip'][-3:]} {fmt_time(r['start'])}-{fmt_time(r['end'])}  "
              f"{r['text'][:40]!r}  {until}  {r['url'] or '(no ACROSCOPE_SHARE_URL on the server)'}")


def cmd_import_xspf(a):
    out(xspf.import_session(_session(a.session)))


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
    rows, cum = rt["rows"], osd.LAYOUTS[rt["layout"]]["cumulative"]
    ok = [r for r in rows if (r["bottom"] if cum else r["top"]) is not None]
    print(f"layout {rt['layout']}: {len(rows)} frames, {len(ok)} with a readable {'total' if cum else 'arm'} timer")
    runs = osd.video_arms(rt, a.fps)
    for v, st in zip(runs, osd.read_stats(sessions.clip_path(s, a.video), runs, rt["layout"])):
        stats = "" if st.get("total_arm") is None else f"; STATS after: total arm {fmt_time(st['total_arm'])}, blackbox {st['blackbox']} %"
        print(f"  arm run: video {fmt_time(v['start'])} - {fmt_time(v['end'])}  {v['length']:5.1f} s  (timer {v['timer_max']} s, total before {v['total_before']}, {v['readings']} readings{stats})")


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
            plan = osd.session_plan(s, a.fps)
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
                r = osd.automatch(s, c, write=False, fps=a.fps, overwrite=a.overwrite, pending=pending, windows=p["windows"])
                fits = r.get("boot_fits") or {}
                settled = bool(r["matches"]) and bool(fits) and all(f.get("n", 0) >= 2 and not f.get("rejected") for f in fits.values())
                if not settled:
                    print(f"{c}: ambiguous in the session plan and only lengths to go by: nothing "
                          f"{'written' if a.write else 'proposed'}, left for a hand match", file=sys.stderr)
                    results.append(dict(r, ambiguous=True, matches=[]))
                    continue
            r = osd.automatch(s, c, write=a.write, fps=a.fps, overwrite=a.overwrite, pending=pending, windows=p["windows"] if p else None)
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
                print(f"   boot {b}: video = {f['a']:.5f} x uptime + {f['b']:.2f} ({f['n']} arms) explains no clock (beyond {100 * osd.MAX_CLOCK:g} %"
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


def cmd_osd_learn(a):
    labels = json.load(open(a.labels))
    out(osd.learn(labels))


def cmd_serve(a):
    from .server import serve
    serve(a.host, a.port, warm=not a.no_warm)


# with ACROSCOPE_URL set these run on the server (they need the clips, the logs or the cache); frames and sheets
# come back as files into the local cache under the same relative path
PROXIED = {"arms", "decode", "metrics", "events", "tricks", "frame", "sheet", "osd", "automatch", "inbox", "sessions", "match", "attach", "detach", "tag", "recut",
           "share", "unshare", "shares"}


def _proxy(argv: list[str]) -> None:
    r = sessions.store().cli(argv)
    if r["stderr"]:
        print(r["stderr"], end="", file=sys.stderr)
    if argv[0] in ("frame", "sheet") and r["code"] == 0:
        for line in r["stdout"].splitlines():
            if not line.strip():
                continue
            rel = line.strip().split("/.cache/acroscope/", 1)[-1] if "/.cache/acroscope/" in line else line.strip().split("/cache/", 1)[-1]
            dest = CACHE_DIR / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            sessions.store().fetch(line.strip(), dest)
            print(dest)
    else:
        print(r["stdout"], end="")
    sys.exit(r["code"])


def parser() -> argparse.ArgumentParser:
    """The CLI's arguments; the server parses a client's command with it too, to tell names from free text."""
    ap = argparse.ArgumentParser(prog="acroscope", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    class Version(argparse.Action):   # the store is opened only when asked: `--help` must not create a database
        def __call__(self, parser, ns, values, option_string=None):
            store = f"server {SERVER_URL}" if SERVER_URL else f"sqlite {DB_PATH}"      # named, not opened
            print(f"acroscope (data {DATA_DIR}, cache {CACHE_DIR}, store {store})"); parser.exit()
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
    p = sp.add_parser("session", help="edit a session: rename <new>, date <YYYY-MM-DD>, move <clip> <to-session>, merge <into-session>, delete <clip> (into trash/ for 7 days)")
    p.add_argument("action", choices=["rename", "date", "move", "merge", "delete"]); p.add_argument("session"); p.add_argument("arg")
    p.add_argument("to", nargs="?", help="for move: the session the clip goes to"); p.set_defaults(f=cmd_session)
    p = sp.add_parser("inbox", help="the batches in inbox/ and their status"); p.add_argument("--json", action="store_true"); p.set_defaults(f=cmd_inbox)
    p = sp.add_parser("ingest", help="consume inbox batches now (all pending ones, or the named ones); the server does this on its own")
    p.add_argument("batches", nargs="*"); p.set_defaults(f=cmd_ingest)
    p = sp.add_parser("recut", help="compress a clip again from its kept original (a cut that took real footage); its moments and matches move with it")
    p.add_argument("session"); p.add_argument("clip"); p.set_defaults(f=cmd_recut)
    p = sp.add_parser("housekeeping", help="delete originals past their retention and back the database up into the data dir"); p.set_defaults(f=cmd_housekeeping)
    p = sp.add_parser("tags", help="the tag vocabulary by category, with how many moments use each tag")
    p.add_argument("session", nargs="?", help="count in one session only"); p.add_argument("--json", action="store_true"); p.set_defaults(f=cmd_tags)
    p = sp.add_parser("tagdef", help="define a tag in a category (or move it there); --rm drops the definition")
    p.add_argument("name"); p.add_argument("--category"); p.add_argument("--color", help="#rrggbb; '' clears it, the category colour is used then")
    p.add_argument("--rm", action="store_true"); p.set_defaults(f=cmd_tagdef)
    p = sp.add_parser("category", help="create a tag category (needs --color) or change its colour; --rm drops an empty one")
    p.add_argument("name"); p.add_argument("--color"); p.add_argument("--rm", action="store_true"); p.set_defaults(f=cmd_category)
    p = sp.add_parser("arms", help="list the arms of a .bbl (decodes every arm into the cache once)")
    p.add_argument("bbl"); p.add_argument("--force", action="store_true"); p.add_argument("--json", action="store_true"); p.set_defaults(f=cmd_arms)
    p = sp.add_parser("fetch-log", help="the .bbl as it is, from the server onto this machine (for blackbox_decode or Blackbox Explorer)")
    p.add_argument("bbl", help="file name, or a fragment unique among the logs the sessions know")
    p.add_argument("--to", help="directory to save into (default: the cache's blackbox/)"); p.set_defaults(f=cmd_fetch_log)
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
    p = sp.add_parser("events", help="suggested moments from the tricks file (flips, loops, split-S, dives, crashes, kicks, motor loss)")
    p.add_argument("session", nargs="?"); p.add_argument("video", nargs="?"); p.add_argument("--arm", help="bbl:N")
    p.add_argument("--matched-only", action="store_true"); p.add_argument("--json", action="store_true"); p.set_defaults(f=cmd_events)
    p = sp.add_parser("tricks", help="the tricks in force: list | show <id> | features [kind] | check [id...] | explain <bbl>:<arm> [from] [to] | eval <session> | "
                                     "layer | set <param> <value> | unset <param> | on <id> | off <id> | skill <name|none> | export [file] | import <file> [--merge]")
    p.add_argument("what", nargs="?", default="list", choices=["list", "show", "features", "check", "explain", "eval", "layer", "set", "unset", "on", "off", "skill", "export", "import"])
    p.add_argument("--merge", action="store_true", help="import: over the current layer instead of replacing it")
    p.add_argument("args", nargs="*")
    p.add_argument("--craft", help="the library in force for this craft (by the log header's Craft name); with set/unset/on/off/skill: write into its section")
    p.add_argument("--json", action="store_true"); p.set_defaults(f=cmd_tricks)
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
    p = sp.add_parser("share", help="publish one moment on a secret link: the clip around it (no sound, no metadata) with the sticks drawn over it, as a static bundle")
    p.add_argument("session"); p.add_argument("moment", help="the moment's id (m12)")
    p.add_argument("--pad", type=float, default=2.0, help=f"seconds kept before and after the moment (default 2, at most {share.PAD_MAX:g})")
    p.add_argument("--text", help="the line shown under the clip (default: the moment's title; never the session or the date)")
    p.add_argument("--expires", help=f"30d, 12h, 2w or never (default {share.DEFAULT_EXPIRY}; with --update the share's own unless given): the bundle is taken down after that")
    p.add_argument("--no-sticks", action="store_true", help="the clip alone (a clip without a blackbox match)")
    p.add_argument("--audio", action="store_true", help="keep the clip's sound (off by default: a DVR's track is motors and bystanders)")
    p.add_argument("--poster", metavar="T", help="the frame at this video time as the page's poster and the preview image messengers show (none by default)")
    p.add_argument("--from", dest="start", help="the window's start in video time (instead of the moment minus --pad)")
    p.add_argument("--to", dest="end", help="the window's end in video time")
    p.add_argument("--update", metavar="TOKEN", help="edit the share with this token instead of making a new one: the bundle made again, the link kept")
    p.set_defaults(f=cmd_share)
    p = sp.add_parser("unshare", help="take a share down: its bundle removed (and the revoke command run, when set), the link dead")
    p.add_argument("token"); p.set_defaults(f=cmd_unshare)
    p = sp.add_parser("shares", help="the shares that are up; --all lists the expired and revoked ones too")
    p.add_argument("--all", action="store_true"); p.add_argument("--json", action="store_true"); p.set_defaults(f=cmd_shares)
    p = sp.add_parser("import-xspf", help="one-off: import videos/<session>/moments.xspf into the session file"); p.add_argument("session"); p.set_defaults(f=cmd_import_xspf)
    p = sp.add_parser("transcode", help="H.264 for the player: proxies in the cache, or --replace the clips in the data dir")
    p.add_argument("session"); p.add_argument("videos", nargs="*"); p.add_argument("--replace", action="store_true"); p.add_argument("--force", action="store_true"); p.set_defaults(f=cmd_transcode)
    p = sp.add_parser("osd", help="read the OSD timers through a clip and list the arm runs")
    p.add_argument("session"); p.add_argument("video"); p.add_argument("--fps", type=float, default=2.0); p.add_argument("--json", action="store_true"); p.set_defaults(f=cmd_osd)
    p = sp.add_parser("automatch", help="match clips to arms from the OSD timers (proposes; --write records)")
    p.add_argument("session"); p.add_argument("videos", nargs="*"); p.add_argument("--write", action="store_true"); p.add_argument("--overwrite", action="store_true", help="replace existing matches of the clip too")
    p.add_argument("--unmatched", action="store_true", help="only the clips without a match yet (not missing ones)")
    p.add_argument("--fps", type=float, default=2.0); p.add_argument("--json", action="store_true"); p.set_defaults(f=cmd_automatch)
    p = sp.add_parser("osd-learn", help="rebuild the OSD digit templates from a labels file")
    p.add_argument("labels", nargs="?", default="scripts/osd-labels.json"); p.set_defaults(f=cmd_osd_learn)
    p = sp.add_parser("serve", help="run the player (http)"); p.add_argument("--host", default="0.0.0.0"); p.add_argument("--port", type=int, default=8070)
    p.add_argument("--no-warm", action="store_true", help="don't decode/probe everything in the background at start"); p.set_defaults(f=cmd_serve)
    return ap


def main(argv=None):
    a = parser().parse_args(argv)
    args = list(sys.argv[1:] if argv is None else argv)
    if a.cmd in PROXIED and sessions.remote() and not (a.cmd == "tricks" and a.what in ("export", "import")):
        return _proxy(args)
    if not sessions.remote() and tricks.LAYER_SOURCE is None:          # the local database's layer for every command that detects
        tricks.LAYER_SOURCE = _layer_from_store
    try:
        a.f(a)
    except (FileNotFoundError, ValueError, RemoteError) as e:   # a name that is not there, a refused edit: one line
        print(f"acroscope: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
