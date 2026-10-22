"""Share one trick (#57): a moment rendered as a self-contained static bundle on a secret link.

The bundle is <shares dir>/<token>/: index.html (one page: the clip looping with the sticks drawn over it and the
share text under it; it fetches nothing but its own files), clip.mp4 (the window cut from the clip and re-encoded:
the picture, and the sound only when asked for; no metadata) and, when a frame was chosen, poster.jpg (the page's
poster and the preview image a messenger shows). A static web server publishes the shares dir on a domain of its
own; this server is never reachable from a share (it has no authentication of its own, #10). What leaves: the
frames of that window and the four stick channels over it, downsampled. Never the log, the craft, the session,
the clip's name or the date: the page names nothing, and the default text is the moment's title.

The player's flow is preview, then publish: `preview` renders the same bundle into the cache (share-preview/<id>/,
which the server serves back to the player only), `publish` takes a preview whose facts still match as it is (the
clip is not cut twice) and renders afresh otherwise. The CLI publishes in one go (`render`).

Deleting the bundle's folder revokes the share: `unshare` does that (after ACROSCOPE_SHARE_REVOKE when it is set),
housekeeping does it for shares past their expiry. ACROSCOPE_SHARE_PUSH, when set, pushes a rendered bundle somewhere
else (a host, a bucket), so a laptop needs no web server of its own. The token is the secret: 24 characters of
[a-z0-9] (more than 2^124 of them), lower case so a case-blind file system keeps two apart, and never used in a path
without token_ok (`unshare` takes it from the command line, the API from the URL).
"""
import html
import json
import re
import secrets
import shlex
import shutil
import subprocess
import time
from bisect import bisect_right
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import blackbox, sessions, video
from .config import CACHE_DIR, SHARE_PUSH, SHARE_REVOKE, SHARE_URL, SHARES_DIR, VIDEOS_DIR

TOKEN_ALPHABET = "abcdefghijklmnopqrstuvwxyz0123456789"
TOKEN_LEN = 24
TOKEN_RE = re.compile(r"[a-z0-9]{16,64}")   # ours are 24; the bounds keep a stray short or endless string out
PREVIEW_RE = re.compile(r"[a-z0-9]{12,32}")  # a preview's id: 16 of the same alphabet
PAD_MAX = 10.0             # seconds of padding each side, at most
MAX_SECONDS = 120.0        # a share is one trick, not a clip
MIN_SECONDS = 0.5          # a moment that is a point gets this much around it
DEFAULT_EXPIRY = "30d"     # a secret link is not private: it is forwarded, previewed, pasted; it should not live forever
STEP = 0.02                # the sticks every 20 ms, as the player's overlay
PREVIEW_KEEP = 86400       # seconds a preview nobody published stays in the cache
UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 7 * 86400}
TEMPLATE = Path(__file__).parent / "static" / "share.html"
PREVIEW_DIR = CACHE_DIR / "share-preview"
ROOT_PAGE = ('<!doctype html>\n<html lang="en"><head><meta charset="utf-8"><meta name="robots" content="noindex, nofollow">'
             '<title>acroscope</title></head><body>Nothing here.</body></html>\n')
FACTS = ("video", "start", "end", "sticks", "audio", "poster")   # what makes two renders the same bundle (the text is in the page only)


class ShareError(ValueError):
    """A render, push or revoke that did not go through: one line for the CLI, a 400 for the API, no traceback."""


def new_token(n: int = TOKEN_LEN) -> str:
    return "".join(secrets.choice(TOKEN_ALPHABET) for _ in range(n))


def token_ok(token) -> bool:
    return isinstance(token, str) and TOKEN_RE.fullmatch(token) is not None


def preview_ok(pid) -> bool:
    return isinstance(pid, str) and PREVIEW_RE.fullmatch(pid) is not None


def parse_expiry(spec: str, start: datetime | None = None) -> str | None:
    """'30d', '12h', '2w', '90m' from now (or `start`) as an ISO stamp; 'never' is None."""
    s = (spec or "").strip().lower()
    if s == "never":
        return None
    m = re.fullmatch(r"(\d+)([smhdw])", s)
    if not m:
        raise ValueError(f"--expires {spec!r}: a number and a unit (30d, 12h, 2w, 90m), or never")
    start = start or datetime.now(timezone.utc)
    return (start + timedelta(seconds=int(m.group(1)) * UNITS[m.group(2)])).replace(microsecond=0).isoformat()


def window(start: float, end: float, pad: float, duration: float) -> tuple[float, float]:
    """The moment plus `pad` s each side, inside the clip (a point gets MIN_SECONDS around it), at most MAX_SECONDS."""
    if not 0 <= pad <= PAD_MAX:
        raise ValueError(f"--pad {pad:g}: between 0 and {PAD_MAX:g} s")
    if end < start:
        raise ValueError(f"{start:g}-{end:g} s: the end is before the start")
    if end - start < MIN_SECONDS:
        mid = (start + end) / 2
        start, end = mid - MIN_SECONDS / 2, mid + MIN_SECONDS / 2
    w0, w1 = max(0.0, start - pad), min(duration, end + pad)
    if w1 - w0 < MIN_SECONDS:
        raise ValueError(f"{start:g}-{end:g} s: outside the clip ({duration:g} s)")
    if w1 - w0 > MAX_SECONDS:
        raise ValueError(f"{w1 - w0:.0f} s: a share is one trick, at most {MAX_SECONDS:g} s")
    return round(w0, 3), round(w1, 3)


def stick_samples(arm: blackbox.Arm, offset: float, w0: float, w1: float, step: float = STEP) -> dict:
    """The four stick channels every `step` s over video time w0..w1, in the picture's terms (Mode 2, every axis
    + = stick right or up, in %; yaw negated since rcCommand[2] is + for the left stick LEFT, see metrics.CONVENTIONS
    and the player's /sticks); None where the arm does not cover the picture (video = arm time + offset)."""
    rc = [arm.col(f"rcCommand[{k}]") for k in range(4)]
    out = {"step": step, "throttle": [], "yaw": [], "pitch": [], "roll": []}
    for b in range(int(round((w1 - w0) / step)) + 1):
        a = w0 + b * step - offset
        if a < 0 or a > arm.length:
            for k in ("throttle", "yaw", "pitch", "roll"):
                out[k].append(None)
            continue
        i = max(0, bisect_right(arm.t, a) - 1)          # the last frame at or before a
        out["throttle"].append(max(0, min(100, round((rc[3][i] - 1000) / 10))))
        out["yaw"].append(round(-rc[2][i] / 5))
        out["pitch"].append(round(rc[1][i] / 5))
        out["roll"].append(round(rc[0][i] / 5))
    return out


def cut_clip(src: Path, dst: Path, w0: float, w1: float, audio: bool = False) -> None:
    """The window of the clip as H.264 any phone plays: re-encoded from w0 itself (not the keyframe before it), the
    picture, the sound only when asked (a DVR's track is motors and whoever stood near), no metadata, no chapters."""
    cmd = ["ffmpeg", "-v", "error", "-y", "-ss", f"{w0:.3f}", "-i", str(src), "-t", f"{w1 - w0:.3f}", "-map", "0:v:0"]
    cmd += ["-map", "0:a?", "-c:a", "aac", "-b:a", "96k"] if audio else ["-an"]
    cmd += ["-sn", "-dn", "-map_metadata", "-1", "-map_chapters", "-1",
            "-c:v", "libx264", "-preset", "medium", "-crf", "23", "-pix_fmt", "yuv420p", "-movflags", "+faststart",
            "-fflags", "+bitexact", "-flags", "+bitexact", str(dst)]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode or not dst.is_file():
        raise ShareError(f"ffmpeg could not cut {src.name}: {r.stderr.strip()[-300:] or f'exit {r.returncode}'}")
    if _duration(dst) < MIN_SECONDS / 2:                   # a header ffmpeg read but frames it could not: an empty file is no share
        raise ShareError(f"ffmpeg cut nothing from {src.name} ({r.stderr.strip()[-200:] or 'no frames'})")


def _duration(path: Path) -> float:
    """The clip's duration by ffprobe, 0 when it cannot be read (not video.probe: that caches under the clip's folder
    name, which here would be the token)."""
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", str(path)], capture_output=True, text=True)
    try:
        return float(json.loads(r.stdout)["format"]["duration"]) if r.returncode == 0 else 0.0
    except (ValueError, KeyError, TypeError):
        return 0.0


def page(text: str, sticks: dict | None, audio: bool = False, poster: str | None = None) -> str:
    """The viewer page: the template with the text (escaped, its first line as the title), the sticks (inline,
    JSON), the sound flag and the poster (its URL: absolute when the share's is known, so a messenger's preview
    finds it) filled in, in one pass, so nothing in the text is read as a placeholder."""
    text = text or ""
    first = text.strip().splitlines()[0] if text.strip() else ""
    title = html.escape(first or "acroscope", quote=True)
    og = ""
    if poster:
        og = (f'<meta property="og:title" content="{title}">\n<meta property="og:image" content="{html.escape(poster, quote=True)}">\n'
              '<meta name="twitter:card" content="summary_large_image">\n')
    parts = {"TITLE": title, "TEXT": html.escape(text, quote=True), "STICKS": json.dumps(sticks).replace("</", "<\\/"),
             "AUDIO": "true" if audio else "false", "POSTER": ' poster="poster.jpg"' if poster else "", "OG": og}
    return re.sub(r"__(TITLE|TEXT|STICKS|AUDIO|POSTER|OG)__", lambda m: parts[m.group(1)], TEMPLATE.read_text())


def run_hook(template: str, token: str, d: Path) -> None:
    """ACROSCOPE_SHARE_PUSH / ACROSCOPE_SHARE_REVOKE: split as a shell line, {dir} and {token} filled in per
    argument, run without a shell. A non-zero exit is an error with the command's last words."""
    args = [a.replace("{dir}", str(d)).replace("{token}", token) for a in shlex.split(template)]
    if not args:
        return
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=900)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise ShareError(f"{args[0]}: {e}") from None
    if r.returncode:
        raise ShareError(f"{args[0]} exited {r.returncode}: {(r.stderr or r.stdout).strip()[-300:]}")


def link(token: str) -> str | None:
    return f"{SHARE_URL}/{token}/" if SHARE_URL else None


def _row(r: dict) -> dict:
    return {**r, "url": link(r["token"])}


def list_shares(all: bool = False, session: str | None = None) -> list[dict]:
    """The shares with their url, the clip's readable name and the moment's title (as they are now; '' when the
    moment is gone): what the CLI's table and the configuration page show."""
    rows = [_row(r) for r in sessions.store().shares(all=all) if session is None or r["session"] == session]
    loaded: dict[str, dict] = {}
    for r in rows:
        s = loaded.get(r["session"])
        if s is None:
            s = loaded[r["session"]] = sessions.load(r["session"])
        r["clip"] = next((v.get("name") or v["file"] for v in s["videos"] if v["file"] == r["video"]), r["video"])
        r["title"] = next((m["title"] for m in s["moments"] if m["id"] == r["moment"]), "") if r["moment"] else ""
    return rows


def _root_page() -> None:
    """The shares dir's own index: a visitor at the root (or a server that would list a folder) sees nothing."""
    p = SHARES_DIR / "index.html"
    if not p.exists():
        p.write_text(ROOT_PAGE)


# ---- what a share is made of ---------------------------------------------------------------------------------------

def resolve(session: str, moment: str | None = None, video: str | None = None, start: float | None = None,
            end: float | None = None, pad: float = 2.0, text: str | None = None, sticks: bool = True,
            audio: bool = False, poster: float | None = None) -> dict:
    """The facts of a share before anything is rendered: the clip (the moment's, or `video`), the window (the
    moment plus `pad`, or `start`/`end` as given, inside the clip), the text (the moment's title unless given), the
    stick samples when wanted (from the match that covers the window's moment; a ValueError names --no-sticks when
    none does), the poster time (inside the window). Raises FileNotFoundError for a moment or clip that is not."""
    if sessions.remote():
        raise ValueError("share runs where the clips are (on the server: this command is proxied there)")
    s = sessions.load(session)
    if not s["videos"] and not sessions.store().exists(session):
        raise FileNotFoundError(f"{session}: no such session")
    m = None
    if moment:
        m = next((x for x in s["moments"] if x["id"] == moment), None)
        if m is None:
            raise FileNotFoundError(f"{session}: no moment {moment} (acroscope moments {session} lists them)")
        video = m["video"]
    elif not video:
        raise ValueError("a share is a moment (its id), or a clip with a start and an end")
    else:
        video = sessions.resolve_clip(session, video)
    src = VIDEOS_DIR / session / video
    if not src.is_file():
        raise FileNotFoundError(f"{session}/{video}: the clip is not here")
    try:
        duration = video_duration = video_probe(src)
    except subprocess.CalledProcessError as e:
        raise ShareError(f"{video}: ffprobe cannot read it ({(e.stderr or '').strip()[-200:] or 'still syncing?'})") from None
    if start is not None or end is not None:
        if start is None or end is None:
            raise ValueError("a window needs both a start and an end")
        w0, w1 = window(float(start), float(end), 0.0, duration)
    elif m:
        w0, w1 = window(m["start"], m["end"], pad, duration)
    else:
        raise ValueError("a clip without a moment needs a start and an end")
    if poster is not None:
        poster = round(float(poster), 2)
        if not w0 <= poster <= w1:
            raise ValueError(f"poster {poster:g} s: outside the window {w0:g}-{w1:g} s")
    samples = None
    if sticks:
        at = m["start"] if m and w0 <= m["start"] <= w1 else w0
        mt = sessions.match_for(s, video, at)
        a0 = at - mt["offset"] if mt else None
        length = sessions.arm_length(mt) if mt else None
        if mt is None or a0 < -1 or (length is not None and a0 > length + 1):
            raise ValueError(f"{video}: no blackbox match covers {at:g} s, the sticks would not fit the picture; "
                             "match the clip first, or `share --no-sticks` for the clip alone")
        samples = stick_samples(blackbox.load_arm(mt["bbl"], mt["arm"]), mt["offset"], w0, w1)
    return {"session": session, "video": video, "moment": m["id"] if m else None, "src": src, "duration": video_duration,
            "start": w0, "end": w1, "text": (m["title"] if m else "") if text is None else text,
            "sticks": samples is not None, "samples": samples, "audio": bool(audio), "poster": poster}


def video_probe(src: Path) -> float:
    return video.probe(src)["duration"]


def _facts(r: dict) -> dict:
    return {k: r[k] for k in FACTS}


def build(r: dict, into: Path, log=lambda m: None) -> None:
    """The files of a bundle into `into` (made here): clip.mp4 and, with a poster time, poster.jpg. The page is
    written by `_write_page`, since its poster URL depends on where the bundle ends up."""
    into.mkdir(parents=True)
    log(f"cutting {r['video']} {r['start']:g}-{r['end']:g} s")
    cut_clip(r["src"], into / "clip.mp4", r["start"], r["end"], audio=r["audio"])
    if r["poster"] is not None:
        shutil.copyfile(video.frame(r["src"], r["poster"]), into / "poster.jpg")


def _write_page(r: dict, into: Path, token: str | None) -> None:
    """The page beside the clip; the poster is there or not (the file is the truth, so a refresh keeps it). Written
    whole: a visitor mid-write never sees half a page."""
    poster = None
    if (into / "poster.jpg").is_file():
        poster = f"{SHARE_URL}/{token}/poster.jpg" if token and SHARE_URL else "poster.jpg"
    tmp = into / "index.html.new"
    tmp.write_text(page(r["text"], r["samples"], audio=r["audio"], poster=poster))
    tmp.replace(into / "index.html")


def _public(r: dict) -> dict:
    """A share's facts for the API and the CLI: no paths, no samples."""
    return {k: v for k, v in r.items() if k not in ("src", "samples", "duration")}


# ---- preview, publish, revoke ----------------------------------------------------------------------------------------

def preview(log=lambda m: None, **what) -> dict:
    """Render a bundle into the cache's share-preview/<id>/ for the player to look at (the server serves it back to
    the player only), with its facts beside it so `publish` can take it as it is. {preview, url, ...the facts}."""
    r = resolve(**what)
    pid = new_token(16)
    d = PREVIEW_DIR / pid
    try:
        build(r, d, log)
        _write_page(r, d, None)
        (d / "preview.json").write_text(json.dumps(_facts(r)))
    except BaseException:
        shutil.rmtree(d, ignore_errors=True)
        raise
    return {"preview": pid, "url": f"/share-preview/{pid}/", **_public(r)}


def drop_preview(pid: str) -> bool:
    if not preview_ok(pid):
        raise ValueError(f"{pid!r}: not a preview")
    d = PREVIEW_DIR / pid
    if not d.is_dir():
        return False
    shutil.rmtree(d)
    return True


def publish(expires: str = DEFAULT_EXPIRY, preview_id: str | None = None, log=lambda m: None, **what) -> dict:
    """Publish: the window cut from its clip (or taken from a preview whose facts still match), the page written,
    the bundle pushed when a push is configured, the share recorded; in that order, and nothing is left when a step
    fails. Returns the share's row with its url, dir and facts."""
    expires_at = parse_expiry(expires)
    r = resolve(**what)
    token = new_token()
    final, staging = SHARES_DIR / token, SHARES_DIR / ".staging" / token
    staging.parent.mkdir(parents=True, exist_ok=True)
    try:
        taken = False
        if preview_id and preview_ok(preview_id) and (PREVIEW_DIR / preview_id / "preview.json").is_file():
            pd = PREVIEW_DIR / preview_id
            try:
                taken = json.loads((pd / "preview.json").read_text()) == _facts(r)
            except ValueError:
                taken = False
            if taken:
                log(f"taking the preview {preview_id} as it is")
                shutil.move(str(pd), str(staging))
                (staging / "preview.json").unlink()
        if not taken:
            build(r, staging, log)
        _write_page(r, staging, token)
        _root_page()
        staging.rename(final)                              # whole or not there: a static server next to this dir sees no half bundle
        if SHARE_PUSH:
            log(f"pushing {token}")
            run_hook(SHARE_PUSH, token, final)
        row = sessions.store().add_share(token, r["session"], r["video"], r["moment"], r["start"], r["end"], r["text"], expires_at)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        shutil.rmtree(final, ignore_errors=True)
        raise
    finally:
        try:
            staging.parent.rmdir()                         # .staging/ only while something is being rendered
        except OSError:
            pass
    return {**_row(row), "dir": str(final), "sticks": r["sticks"], "audio": r["audio"], "poster": r["poster"]}


def render(session: str, moment_id: str, pad: float = 2.0, text: str | None = None, expires: str = DEFAULT_EXPIRY,
           sticks: bool = True, audio: bool = False, poster: float | None = None, start: float | None = None,
           end: float | None = None, log=lambda m: None) -> dict:
    """The CLI's one-step publish of a moment."""
    return publish(expires=expires, log=log, session=session, moment=moment_id, pad=pad, text=text, sticks=sticks,
                   audio=audio, poster=poster, start=start, end=end)


def _take_down(token: str, d: Path, log) -> bool:
    """The bundle out of the world: the revoke command when one is set (the copy pushed elsewhere), then the folder
    here. The folder is the marker: while it is here, the share is still up somewhere. True when there was one."""
    if not d.is_dir():
        return False
    if SHARE_REVOKE:
        log(f"revoking {token} where it was pushed")
        run_hook(SHARE_REVOKE, token, d)
    shutil.rmtree(d)
    return True


def revoke(token: str, log=lambda m: None) -> dict:
    """Take a share down and mark it revoked. While the revoke command fails the share stays active: a link is not
    reported dead while the copy may be up. Revoking a share that is down already changes nothing."""
    if not token_ok(token):
        raise ValueError(f"{token!r}: not a share token")
    store = sessions.store()
    if store.share(token) is None:
        raise FileNotFoundError(f"{token}: no such share")
    _take_down(token, SHARES_DIR / token, log)
    store.revoke_share(token)
    return _row(store.share(token))


def refresh_pages(log=lambda m: None) -> list[str]:
    """Housekeeping (so at every server start too): a bundle is static, and a deploy with a better viewer would
    leave the shares that are up with the old page. Every active share whose page is older than the template gets
    its page written again from the row (the window, the text), the match (the sticks, as before), the clip (the
    sound, as before) and the poster beside it; the clip is not touched, the link does not change, and the push
    hook runs again for the copy elsewhere. One that cannot be rendered (its clip or match gone) is logged and
    kept as it is. Returns the tokens refreshed."""
    done = []
    stamp = TEMPLATE.stat().st_mtime
    for r in sessions.store().shares():
        d = SHARES_DIR / r["token"]
        pg = d / "index.html"
        if not token_ok(r["token"]) or not pg.is_file() or pg.stat().st_mtime >= stamp:
            continue
        old = pg.read_text()
        try:
            facts = resolve(session=r["session"], video=r["video"], start=r["start"], end=r["end"], text=r["text"],
                            sticks="const STICKS = null;" not in old, audio="const AUDIO = true;" in old)
            _write_page(facts, d, r["token"])
            if SHARE_PUSH:
                run_hook(SHARE_PUSH, r["token"], d)
        except (ValueError, FileNotFoundError, OSError) as e:
            log(f"share {r['token']}: page not refreshed ({e})")
            continue
        done.append(r["token"])
        log(f"share {r['token']}: page refreshed")
    return done


def cleanup_expired(log=lambda m: None) -> list[str]:
    """Housekeeping: the bundles of shares past their expiry (and of revoked ones still here) go; the rows stay as
    the record. A takedown that fails is logged and tried again next time. Previews nobody published go after a
    day. Returns the tokens taken down."""
    gone = []
    for r in sessions.store().shares(all=True):
        if r["state"] == "active" or not token_ok(r["token"]):
            continue
        try:
            if _take_down(r["token"], SHARES_DIR / r["token"], log):
                gone.append(r["token"])
                log(f"share {r['token']} {r['state']}: bundle removed")
        except ShareError as e:
            log(f"share {r['token']}: {e}")
    if PREVIEW_DIR.is_dir():
        for d in PREVIEW_DIR.iterdir():
            if d.is_dir() and preview_ok(d.name) and time.time() - d.stat().st_mtime > PREVIEW_KEEP:
                shutil.rmtree(d, ignore_errors=True)
                log(f"preview {d.name} dropped")
    return gone
