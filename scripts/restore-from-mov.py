"""One-off (2026-10-18): the first days' clips again from the DVR's originals in videos/mov/.

The clips of the first days (before 2026-10-11) were cut and encoded (HEVC; 10-09/10 H.264) by the pre-database import: keyframes
up to 29 s apart (seeking lags), and cuts made by the old static detector, which took real footage too (one clip
kept 26 s of 148). Where the DVR's original (Motion JPEG) is still in videos/mov/<name>.mov:

- in place, through `ingest.recut.recut`, when nothing can shift: the clip was never cut (same length as its original, so
  its matches map exactly) or it has no matches and no moments;
- else into a new session `<session>-recut` beside the old one: the clip compressed from its original under the
  same readable name, the session's logs attached, automatch run; moments are not copied (their times would be
  off); the old session stays as it is.

Every clip made from an original points at it (`original`) without an expiry date, so housekeeping keeps the
originals and a recut can be repeated. With --proxies, the HEVC clips that have no original get an H.264 proxy in the
cache (keyframe every 0.5 s), which the player serves instead.

Runs where the data is (on the server, ACROSCOPE_URL empty). Without --apply it only lists.

    python scripts/restore-from-mov.py [--apply] [--proxies]
"""
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from acroscope import cli, ingest
from acroscope.ingest import clock, encode, recut
from acroscope.sources import video
from acroscope.store import sessions
from acroscope.config import DATA_DIR, VIDEOS_DIR

MOV = VIDEOS_DIR / "mov"


def duration(p: Path) -> float:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(p)],
                         capture_output=True, text=True).stdout.strip()
    return float(out or 0)


def plan() -> tuple[list, list, list]:
    """(in place, into copies, proxies): [(session, clip row, original path)] / [(session, clip row)]."""
    movs = {p.stem: p for p in MOV.glob("*.mov")} if MOV.is_dir() else {}
    here, copy, proxy = [], [], []
    for row in sessions.all_sessions():
        s = sessions.load(row["session"])
        if s["session"].endswith("-recut"):
            continue
        for v in s["videos"]:
            if v.get("missing_since") or v.get("original"):           # one with an original is done (or ingested)
                continue
            name = v.get("name") or Path(v["file"]).stem
            src = movs.get(name)
            if src is None:
                if v.get("codec") == "hevc":
                    proxy.append((s["session"], v))
                continue
            # any codec: 2026-10-09/10's clips are H.264 (or raw Motion JPEG) from the old pipeline, without an original
            uncut = abs((v.get("duration") or 0) - duration(src)) < 0.3
            data = any(m["video"] == v["file"] for m in s["moments"] + s["matches"])
            (here if uncut or not data else copy).append((s["session"], v, src))
    return here, copy, proxy


def in_place(session: str, v: dict, src: Path) -> None:
    sessions.store().set_video_fields(session, v["file"], original=str(src.relative_to(DATA_DIR)))
    r = recut.recut(session, v["file"], log=print)
    print(f"   {v.get('name')}: {r['duration']} s, cuts {r['cuts']}, timescale {r['timescale']}, "
          f"{len(r['matches'])} matches moved, {len(r['check'])} to check {r['check'] or ''}")


def into_copy(session: str, v: dict, src: Path) -> str:
    new = f"{session}-recut"
    db = sessions.store()
    db.ensure_session(new, session[:10])
    for b in db.load(session)["blackbox"]:
        db.attach(new, b)
    name = v.get("name") or Path(v["file"]).stem
    if any(x.get("name") == name for x in db.load(new)["videos"]):
        print(f"   {name}: in {new} already")
        return new
    (VIDEOS_DIR / new).mkdir(exist_ok=True)
    cid = ingest.new_id()
    work = ingest.ORIGINALS_DIR / ".ingest" / f"restore-{cid}.mp4"          # not under videos/: the scan would take it
    work.parent.mkdir(parents=True, exist_ok=True)
    try:
        res = encode.compress(src, work)
        k = clock.fix_clock(work, res["kept"], print)
    except Exception:
        work.unlink(missing_ok=True)
        raise
    dst = VIDEOS_DIR / new / f"{cid}.mp4"
    os.replace(work, dst)
    info = video.probe(dst)
    db.upsert_video(new, dst.name, info["duration"], info["codec"], name=name)
    db.set_video_fields(new, dst.name, cuts=res["kept"], original=str(src.relative_to(DATA_DIR)),
                        timescale=k if k not in (None, 1.0) else None)
    print(f"   {name} -> {new}/{dst.name}: {info['duration']} s (was {v.get('duration')} s), cuts {res['kept']}")
    return new


def main(apply: bool, proxies: bool) -> None:
    if sessions.remote():
        sys.exit("runs where the data is: on the server, with ACROSCOPE_URL empty")
    here, copy, proxy = plan()
    print(f"in place: {len(here)}")
    for s, v, src in here:
        print(f"  {s} {v.get('name')}")
    print(f"into -recut sessions: {len(copy)}")
    for s, v, src in copy:
        print(f"  {s} {v.get('name')} -> {s}-recut")
    print(f"proxies: {len(proxy)}" + ("" if proxies else " (not asked)"))
    if not apply:
        print("(a list: run with --apply)")
        return
    for s, v, src in here:
        print(f"{s} {v.get('name')}: in place")
        in_place(s, v, src)
    made = set()
    for s, v, src in copy:
        print(f"{s} {v.get('name')}: into a copy")
        made.add(into_copy(s, v, src))
    real = sessions.all_sessions
    for new in sorted(made):
        # the copy's clips are the old session's clips: its matches would hold their arms as taken, so automatch on
        # the copy looks past the session it was copied from (both then hold the same arms, as the same flights)
        source = new[:-len("-recut")]
        sessions.all_sessions = lambda: [r for r in real() if r["session"] != source]
        print(f"automatch {new}")
        try:
            cli.main(["automatch", new, "--write"])
        except SystemExit:
            pass
        finally:
            sessions.all_sessions = real
    if proxies:
        for s, v in proxy:
            src = VIDEOS_DIR / s / v["file"]
            dst = video.proxy_path(src)
            if dst.exists():
                continue
            print(f"proxy {s} {v.get('name')}")
            video.transcode(src, dst)
    print("done")


if __name__ == "__main__":
    main("--apply" in sys.argv[1:], "--proxies" in sys.argv[1:])
