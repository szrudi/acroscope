"""A clip's clock against the OSD's: a DVR that stamped the wrong frame rate shows as an arm clock running off the
clip's, evenly through it; such a clip is re-timed, and a time moves between a clip's old and new cuts and clock.
"""
import os
import subprocess
from pathlib import Path

from ..sources import osd, video


CLOCK_TOL = 0.05           # an OSD clock off the clip's by more than this = a mis-stamped frame rate (2026-10-14 007:
                           # the DVR wrote ~27 fps stamped 60, the OSD ran 2.20x the clip): the clip is re-timed


def _to_source(t: float, cuts: list) -> float:
    """A time in a compressed clip -> the same moment in its original, through the kept stretches."""
    acc = 0.0
    for s, e in cuts:
        if t <= acc + (e - s):
            return s + (t - acc)
        acc += e - s
    return cuts[-1][1]


def _to_clip(o: float, cuts: list) -> float:
    """A time in an original -> the compressed clip (a moment inside a cut lands where the next stretch starts)."""
    acc = 0.0
    for s, e in cuts:
        if o <= e:
            return acc + max(0.0, o - s)
        acc += e - s
    return acc


def move_time(t: float, old: list, k_old: float, new: list, k_new: float) -> float:
    """A time in a clip as it was (kept stretches `old`, re-timed by k_old) -> the same moment in the clip as it is
    now (`new`, k_new). Cuts are in the original's own (stamped) time; a re-timed clip runs k times that."""
    return round(_to_clip(_to_source(t / k_old, old), new) * k_new, 2)


def _stretch(t: float, cuts: list) -> int:
    """Which kept stretch a compressed clip's time t falls in."""
    acc = 0.0
    for i, (s, e) in enumerate(cuts):
        acc += e - s
        if t < acc:
            return i
    return len(cuts) - 1


CLOCK_EVEN = 0.03          # clock_fit: each third's slope within this of the whole fit's (frames dropped evenly; 007's
                           # thirds are within 1.3 % of its 2.2001)


def clock_fit(rows: list[dict], kept: list | None = None) -> float | None:
    """How fast the OSD arm clock runs against the clip's own time, from read_timers rows: only readings where both
    timers agree (a second arm's total and arm timers differ) and that advanced since the last one (a disarmed timer
    stands still), misreads trimmed. Fitted in the original's time (each reading back through the kept stretches): a
    cut inside an arm is not a fast clock. None (cannot tell) on less than 20 readings over 15 s, a fit worse than
    1 s rms, a third with too few readings, or a third whose rate is not the whole fit's: an off clock is only fixed when the
    frames were dropped evenly (2026-10-14 007: 2.200 / 2.183 / 2.207), not for one DVR stall."""
    pts, prev = [], None
    for x in rows:
        top, bot = x.get("top"), x.get("bottom")
        if top is None or bot is None or abs(top - bot) > 1.2 or top <= 1:
            continue
        if prev is not None and top > prev:
            pts.append((_to_source(x["t"], kept) if kept else x["t"], top))
        prev = top

    def fit(p):
        n = len(p)
        mx, my = sum(t for t, _ in p) / n, sum(y for _, y in p) / n
        sxx = sum((t - mx) ** 2 for t, _ in p)
        a = sum((t - mx) * (y - my) for t, y in p) / sxx if sxx else 0.0
        return a, my - a * mx

    for lim in (10.0, 4.0, 2.0, 1.5):
        if len(pts) < 20:
            return None
        a, b = fit(pts)
        pts = [(t, y) for t, y in pts if abs(y - a * t - b) < lim]
    if len(pts) < 20 or pts[-1][0] - pts[0][0] < 15:
        return None
    a, b = fit(pts)
    rms = (sum((y - a * t - b) ** 2 for t, y in pts) / len(pts)) ** 0.5
    if not (rms < 1.0 and a > 0):
        return None
    t0, t1 = pts[0][0], pts[-1][0]
    thirds = [[p for p in pts if t0 + k * (t1 - t0) / 3 <= p[0] <= t0 + (k + 1) * (t1 - t0) / 3] for k in range(3)]
    if any(len(th) < 5 for th in thirds):
        return None
    # each third runs at the whole fit's rate: a stall between two thirds leaves them all at 1.0 against a fit of 1.06
    return a if all(abs(fit(th)[0] - a) <= CLOCK_EVEN * a for th in thirds) else None


def retime(path: Path, k: float) -> None:
    """Stretch a clip's clock by k: the video's timestamps scaled, no re-encode. The audio goes (the DVR stamped it as
    wrongly). Built beside the clip as .part (not a video suffix: the scan leaves it alone), then put in place."""
    tmp = path.with_name(path.name + ".retime.part")
    try:
        subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-y", "-itsscale", f"{k:.5f}", "-i", str(path), "-map", "0:v", "-c", "copy",
                        "-movflags", "+faststart", "-f", "mp4", str(tmp)], check=True)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    os.replace(tmp, path)


def fix_clock(path: Path, kept: list, log=print) -> float | None:
    """1.0 (on time), the factor a clip was re-timed by, or None when it cannot tell (an OSD the reader cannot read, no
    arm long enough, an uneven clock): the clip then stays as it is. The OSD's arm clock against the clip's, in the
    original's time (`kept`: the stretches compress kept), read at automatch's 2 fps."""
    try:
        k = clock_fit(osd.read_timers(path, 2.0)["rows"], kept)
    except Exception:  # noqa: BLE001  (an OSD the reader has no layout for: nothing to go by)
        return None
    if k is None:
        return None
    if abs(k - 1) <= CLOCK_TOL:
        return 1.0
    k = round(k, 4)
    log(f"{path.name}: the OSD clock runs {k}x the clip's (a mis-stamped frame rate): re-timed to {video.probe(path)['duration'] * k:.1f} s")
    retime(path, k)
    return k
