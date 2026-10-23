"""The store's commands: sessions, refresh, scan, purge, import-json, session (rename, date, move, merge, delete) and
housekeeping.
"""
import sys
from pathlib import Path

from .. import ingest, share
from ..sources import blackbox, video
from ..store import edits, sessions, upkeep
from ..config import DATA_DIR
from .common import _segment_count, _session, fmt_time, out


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


def cmd_purge(a):
    s = _session(a.session)
    if a.clip is None and not a.yes:
        gone = [v for v in sessions.load(s)["videos"] if v.get("missing_since")]
        print(f"{len(gone)} clip(s) marked missing in {s}; add --yes to remove those missing for more than {a.days} days, "
              f"or name one clip", file=sys.stderr)
        return out([{"video": v["file"], "missing_since": v["missing_since"]} for v in gone])
    out(edits.purge(s, a.clip, a.days))


def cmd_import_json(a):
    out(sessions.import_json(_session(a.session) if a.session else None))


def cmd_session(a):
    s = _session(a.session)
    if a.action == "rename":
        r = edits.rename_session(s, a.arg)
    elif a.action == "date":
        r = edits.set_date(s, a.arg)
    elif a.action == "move":
        r = edits.move_clip(s, a.arg, a.to)
    elif a.action == "merge":
        r = edits.merge_sessions(s, a.arg)
    elif a.action == "delete":
        return out(edits.delete_clip(s, a.arg))
    out({"session": r["session"], "videos": len(r["videos"]), "matches": len(r["matches"]), "moments": len(r["moments"])})


def cmd_housekeeping(a):
    if sessions.remote():
        sys.exit("housekeeping runs on the server")
    out({"originals_removed": ingest.cleanup_originals(log=lambda m: print(m, file=sys.stderr)),
         "trash_emptied": upkeep.cleanup_trash(log=lambda m: print(m, file=sys.stderr)),
         "shares_taken_down": share.cleanup_expired(log=lambda m: print(m, file=sys.stderr)),
         "shares_refreshed": share.refresh_pages(log=lambda m: print(m, file=sys.stderr)),
         "backup": str(upkeep.backup_db() or "")})


def add(sp) -> None:
    """This group's subcommands."""
    p = sp.add_parser("sessions", help="list sessions, or show one (videos, arms, matches, moments)")
    p.add_argument("session", nargs="?"); p.add_argument("--json", action="store_true"); p.set_defaults(f=cmd_sessions)
    p = sp.add_parser("refresh", help="bring a session in line with its folder: new clips in, gone clips marked missing")
    p.add_argument("session"); p.add_argument("--no-probe", action="store_true", help="skip ffprobe (durations)"); p.set_defaults(f=cmd_refresh)
    p = sp.add_parser("scan", help="register every session folder in the database (the server also does this every 5 min)"); p.set_defaults(f=cmd_scan)
    p = sp.add_parser("purge", help="remove clips marked missing (one clip, or with --yes every one missing for more than --days), with their matches and moments")
    p.add_argument("session"); p.add_argument("clip", nargs="?"); p.add_argument("--days", type=float, default=7); p.add_argument("--yes", action="store_true")
    p.set_defaults(f=cmd_purge)
    p = sp.add_parser("import-json", help="one-off: import the pre-database videos/<session>/session.json files")
    p.add_argument("session", nargs="?"); p.set_defaults(f=cmd_import_json)
    p = sp.add_parser("session", help="edit a session: rename <new>, date <YYYY-MM-DD>, move <clip> <to-session>, merge <into-session>, delete <clip> (into trash/ for 7 days)")
    p.add_argument("action", choices=["rename", "date", "move", "merge", "delete"]); p.add_argument("session"); p.add_argument("arg")
    p.add_argument("to", nargs="?", help="for move: the session the clip goes to"); p.set_defaults(f=cmd_session)
    p = sp.add_parser("housekeeping", help="delete originals past their retention and back the database up into the data dir"); p.set_defaults(f=cmd_housekeeping)
