"""Share one trick (#57): a moment rendered as a self-contained static bundle on a secret link.

The bundle is <shares dir>/<token>/: index.html (one page: the clip looping with the sticks drawn over it and the
share text under it; it fetches nothing but its own clip) and clip.mp4 (the moment plus some padding, cut from the
clip and re-encoded: the picture only, no sound, no metadata). A static web server publishes the shares dir on a
domain of its own; this server is never reachable from a share (it has no authentication of its own, #10). What
leaves: the frames of that window and the four stick channels over it, downsampled. Never the log, the craft, the
session, the clip's name or the date: the page names nothing, and the default text is the moment's title.

Deleting the bundle's folder revokes the share: `unshare` does that (after ACROSCOPE_SHARE_REVOKE when it is set),
housekeeping does it for shares past their expiry. ACROSCOPE_SHARE_PUSH, when set, pushes a rendered bundle somewhere
else (a host, a bucket), so a laptop needs no web server of its own. The token is the secret: 24 characters of
[a-z0-9] (more than 2^124 of them), lower case so a case-blind file system keeps two apart, and never used in a path
without token_ok (`unshare` takes it from the command line).
"""
import html
import json
import math
import re
import secrets
import shlex
import shutil
import subprocess
from bisect import bisect_right
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import blackbox, sessions, video
from .config import SHARE_PUSH, SHARE_REVOKE, SHARE_URL, SHARES_DIR, VIDEOS_DIR

TOKEN_ALPHABET = "abcdefghijklmnopqrstuvwxyz0123456789"
TOKEN_LEN = 24
TOKEN_RE = re.compile(r"[a-z0-9]{16,64}")   # ours are 24; the bounds keep a stray short or endless string out
PAD_MAX = 10.0             # seconds of padding each side, at most
MAX_SECONDS = 120.0        # a share is one trick, not a clip
MIN_SECONDS = 0.5          # a moment that is a point gets this much around it
DEFAULT_EXPIRY = "30d"     # a secret link is not private: it is forwarded, previewed, pasted; it should not live forever
STEP = 0.02                # the sticks every 20 ms, as the player's overlay
UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 7 * 86400}
TEMPLATE = Path(__file__).parent / "static" / "share.html"
ROOT_PAGE = ('<!doctype html>\n<html lang="en"><head><meta charset="utf-8"><meta name="robots" content="noindex, nofollow">'
             '<title>acroscope</title></head><body>Nothing here.</body></html>\n')


class ShareError(ValueError):
    """A render, push or revoke that did not go through: one line for the CLI, no traceback."""


def new_token() -> str:
    return "".join(secrets.choice(TOKEN_ALPHABET) for _ in range(TOKEN_LEN))


def token_ok(token) -> bool:
    return isinstance(token, str) and TOKEN_RE.fullmatch(token) is not None


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
    """The moment plus `pad` s each side, inside the clip (a point gets MIN_SECONDS around it, grown inwards at an
    edge), at most MAX_SECONDS. Only finite numbers: a NaN slips past every comparison and a clamp makes it the
    whole clip (#58 review)."""
    if not all(math.isfinite(x) for x in (start, end, pad, duration)):
        raise ValueError(f"{start}-{end} s, pad {pad}, clip {duration}: not numbers")
    if not 0 <= pad <= PAD_MAX:
        raise ValueError(f"--pad {pad:g}: between 0 and {PAD_MAX:g} s")
    if end < start:
        raise ValueError(f"{start:g}-{end:g} s: the end is before the start")
    if end - start < MIN_SECONDS:
        mid = (start + end) / 2
        start = max(0.0, min(mid - MIN_SECONDS / 2, duration - MIN_SECONDS))
        end = start + MIN_SECONDS
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


def cut_clip(src: Path, dst: Path, w0: float, w1: float) -> None:
    """The window of the clip as H.264 any phone plays: re-encoded from w0 itself (not the keyframe before it), the
    picture only (no sound: a DVR's track is motors and whoever stood near), no metadata, no chapters."""
    cmd = ["ffmpeg", "-v", "error", "-y", "-ss", f"{w0:.3f}", "-i", str(src), "-t", f"{w1 - w0:.3f}",
           "-map", "0:v:0", "-an", "-sn", "-dn", "-map_metadata", "-1", "-map_chapters", "-1",
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


def page(text: str, sticks: dict | None) -> str:
    """The viewer page: the template with the text (escaped, its first line as the title) and the sticks (inline,
    JSON) filled in, in one pass, so nothing in the text is read as a placeholder."""
    text = text or ""
    first = text.strip().splitlines()[0] if text.strip() else ""
    parts = {"TITLE": html.escape(first or "acroscope", quote=True), "TEXT": html.escape(text, quote=True),
             "STICKS": json.dumps(sticks).replace("</", "<\\/")}
    return re.sub(r"__(TITLE|TEXT|STICKS)__", lambda m: parts[m.group(1)], TEMPLATE.read_text())


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


def check_hooks() -> None:
    """A push command without a revoke command would push a share that can never be taken down, with the row
    calling it dead while the copy is up (#58 review): neither a publish nor a takedown goes through in that state."""
    if SHARE_PUSH and not SHARE_REVOKE:
        raise ShareError("ACROSCOPE_SHARE_PUSH is set without ACROSCOPE_SHARE_REVOKE: a pushed share could never be taken down; set both")


def link(token: str) -> str | None:
    return f"{SHARE_URL}/{token}/" if SHARE_URL else None


def _row(r: dict) -> dict:
    return {**r, "url": link(r["token"])}


def list_shares(all: bool = False) -> list[dict]:
    return [_row(r) for r in sessions.store().shares(all=all)]


def _root_page() -> None:
    """The shares dir's own index: a visitor at the root (or a server that would list a folder) sees nothing."""
    p = SHARES_DIR / "index.html"
    if not p.exists():
        p.write_text(ROOT_PAGE)


def render(session: str, moment_id: str, pad: float = 2.0, text: str | None = None, expires: str = DEFAULT_EXPIRY,
           sticks: bool = True, log=lambda m: None) -> dict:
    """Publish a moment: the window cut from its clip, the sticks sampled from its match, the page written, the
    bundle pushed when a push is configured, the share recorded; in that order, and nothing is left when a step
    fails. Only from a match that covers the moment (a wrong offset gives sticks that do not fit the picture), or
    with sticks=False the clip alone. Returns the share with its url and dir."""
    if sessions.remote():
        raise ValueError("share runs where the clips are (on the server: this command is proxied there)")
    check_hooks()
    expires_at = parse_expiry(expires)
    s = sessions.load(session)
    m = next((x for x in s["moments"] if x["id"] == moment_id), None)
    if m is None:
        raise FileNotFoundError(f"{session}: no moment {moment_id} (acroscope moments {session} lists them)")
    vid = m["video"]
    src = VIDEOS_DIR / session / vid
    if not src.is_file():
        raise FileNotFoundError(f"{session}/{vid}: the clip is not here")
    try:
        duration = video.probe(src)["duration"]
    except subprocess.CalledProcessError as e:
        raise ShareError(f"{vid}: ffprobe cannot read it ({(e.stderr or '').strip()[-200:] or 'still syncing?'})") from None
    w0, w1 = window(m["start"], m["end"], pad, duration)
    samples = None
    if sticks:
        mt = sessions.match_for(s, vid, m["start"])
        a0 = m["start"] - mt["offset"] if mt else None
        length = sessions.arm_length(mt) if mt else None
        if mt is None or a0 < -1 or (length is not None and a0 > length + 1):
            raise ValueError(f"{vid}: no blackbox match covers {m['start']:g} s, the sticks would not fit the picture; "
                             "match the clip first, or `share --no-sticks` for the clip alone")
        samples = stick_samples(blackbox.load_arm(mt["bbl"], mt["arm"]), mt["offset"], w0, w1)
    text = m["title"] if text is None else text
    token = new_token()
    final, staging = SHARES_DIR / token, SHARES_DIR / ".staging" / token
    staging.mkdir(parents=True)
    pushed = False
    try:
        log(f"cutting {vid} {w0:g}-{w1:g} s")
        cut_clip(src, staging / "clip.mp4", w0, w1)
        (staging / "index.html").write_text(page(text, samples))
        _root_page()
        staging.rename(final)                              # whole or not there: a static server next to this dir sees no half bundle
        if SHARE_PUSH:
            log(f"pushing {token}")
            pushed = True
            run_hook(SHARE_PUSH, token, final)
        row = sessions.store().add_share(token, session, vid, moment_id, w0, w1, text, expires_at)
    except BaseException:
        if pushed:                                         # a push that failed partway, or a row that could not be written after a
            try:                                           # good one, would leave a copy out there with no row to take it down by
                run_hook(SHARE_REVOKE, token, final)
            except ShareError as e:
                log(f"{token}: the half-pushed copy could not be taken down ({e})")
        shutil.rmtree(staging, ignore_errors=True)
        shutil.rmtree(final, ignore_errors=True)
        raise
    finally:
        try:
            staging.parent.rmdir()                         # .staging/ only while something is being rendered
        except OSError:
            pass
    return {**_row(row), "dir": str(final), "sticks": samples is not None}


def _take_down(r: dict, log) -> bool:
    """The bundle out of the world: the revoke command when one is set (the copy pushed elsewhere), whether or not
    the folder is still here (a moved shares dir, a restore, a hand cleanup took it, not the copy); then the folder
    here; then the row stamped `down_at`, which is the marker that nothing is up anywhere (#58 review: the folder
    was the marker, and it is not reliable). True when there was something to do."""
    if r.get("down_at"):
        return False
    check_hooks()
    d = SHARES_DIR / r["token"]
    if SHARE_REVOKE:
        log(f"revoking {r['token']} where it was pushed")
        run_hook(SHARE_REVOKE, r["token"], d)
    if d.is_dir():
        shutil.rmtree(d)
    sessions.store().mark_down(r["token"])
    return True


def revoke(token: str, log=lambda m: None) -> dict:
    """Take a share down and mark it revoked. While the revoke command fails the share stays active: a link is not
    reported dead while the copy may be up. Revoking a share that is down already changes nothing."""
    if not token_ok(token):
        raise ValueError(f"{token!r}: not a share token")
    store = sessions.store()
    row = store.share(token)
    if row is None:
        raise FileNotFoundError(f"{token}: no such share")
    _take_down(row, log)
    store.revoke_share(token)
    return _row(store.share(token))


def cleanup_expired(log=lambda m: None) -> list[str]:
    """Housekeeping: the bundles of shares past their expiry (and of revoked ones not taken down yet) go; the rows
    stay as the record. A takedown that fails, whatever the reason (the hook, a folder that will not go), is logged
    and tried again next time; it never stops the rest (#58 review). Returns the tokens taken down."""
    gone = []
    for r in sessions.store().shares(all=True):
        if r["state"] == "active" or not token_ok(r["token"]):
            continue
        try:
            if _take_down(r, log):
                gone.append(r["token"])
                log(f"share {r['token']} {r['state']}: taken down")
        except Exception as e:  # noqa: BLE001  (an OSError from rmtree, a hook that failed: the next row still gets its turn)
            log(f"share {r['token']}: not taken down ({type(e).__name__}: {e})")
    return gone
