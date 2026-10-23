"""Phase 3's commands: metrics, events, tricks (the trick engine, its file and the pilot's layer).
"""
import sys
from pathlib import Path

from .. import tricks
from ..analyse import metrics
from ..sources import blackbox
from ..store import sessions
from .common import _arm_for, _session, fmt_time, out, parse_time


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
        from ..tricks import features, segments
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


def add(sp) -> None:
    """This group's subcommands."""
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
