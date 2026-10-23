"""The no-signal cut and the encode: where a clip is static (a flat frame that changes like noise), which stretches are
kept around it, and the H.264 encode of those stretches.
"""
import re
import subprocess
import tempfile
from pathlib import Path

from ..sources import video
from ..config import H264_ARGS


RATE = 2                   # no-signal analysis samples per second
THRESHOLD = 35             # luma range (16x12 frame, 10th-90th percentile) below this = flat: static, or a dark/flat picture
CHANGE = 8.0               # and frame-to-frame change (mean |difference|, 64x48) above this = static (snow 15-21; a flat
                           # live picture 0.3-3: dusk, a grey sky with the OSD on, 2026-10-14 schammer 018 and 022)
MIN_GAP = 5.0              # only cut no-signal runs at least this long (brief breakups mid-flight are kept)
PAD = 1.0                  # seconds of context kept around each cut


# ---- the compressor (cobra-compress.py as functions) ---------------------------------------------------------------

def signal_stats(path: Path) -> tuple[list[float], list[float]]:
    """Per RATE-th of a second: the luma spread of the frame shrunk to 16x12 (analog static averages to flat grey,
    ~15; footage mostly stays above 50, but dusk or a sky-filled frame drops to 22-34), and the mean change from one
    frame to the next at 64x48 over that stretch (static is new noise every frame, 15-21; a flat live picture with
    its OSD is nearly still). One decode, two branches, each printing its own stats to a file."""
    with tempfile.TemporaryDirectory() as tmp:
        a, b = Path(tmp) / "range", Path(tmp) / "change"
        cmd = ["ffmpeg", "-hide_banner", "-nostats", "-nostdin", "-i", str(path), "-filter_complex",
               f"[0:v]split[x][y];[x]fps={RATE},scale=16:12:flags=area,signalstats,metadata=print:file={a}[ra];"
               f"[y]scale=64:48:flags=area,tblend=all_mode=difference,signalstats,metadata=print:file={b}[rb]",
               "-map", "[ra]", "-f", "null", "-", "-map", "[rb]", "-f", "null", "-"]
        try:
            subprocess.run(cmd, capture_output=True, text=True, check=True, timeout=900)
        except subprocess.TimeoutExpired:   # ffmpeg hung here once (2026-10-09, main thread spinning); a rerun passed
            subprocess.run(cmd, capture_output=True, text=True, check=True, timeout=900)
        ra, rb = a.read_text(), b.read_text()
    lo = [float(x) for x in re.findall(r"YLOW=([\d.]+)", ra)]
    hi = [float(x) for x in re.findall(r"YHIGH=([\d.]+)", ra)]
    ranges = [h - l for l, h in zip(lo, hi)]
    sums, counts = [0.0] * len(ranges), [0] * len(ranges)
    for t, d in zip(re.findall(r"pts_time:([\d.]+)", rb), re.findall(r"YAVG=([\d.]+)", rb)):
        i = int(float(t) * RATE)
        if i < len(ranges):
            sums[i] += float(d); counts[i] += 1
    return ranges, [s / c if c else CHANGE for s, c in zip(sums, counts)]


def keep_segments(ranges: list[float], duration: float, changes: list[float] | None = None) -> list[tuple[float, float]]:
    """[(start, end)] seconds worth keeping: signal runs, gaps shorter than MIN_GAP bridged, PAD around each. A
    sample is no signal when it is flat (range below THRESHOLD) and, where `changes` are given, changes like noise
    (above CHANGE): flat and still is a picture (dusk, a grey sky) and is kept."""
    live = [r >= THRESHOLD or (changes is not None and i < len(changes) and changes[i] <= CHANGE) for i, r in enumerate(ranges)]
    segs, start = [], None
    for i, ok in enumerate(live + [False]):
        t = i / RATE
        if ok and start is None:
            start = t
        elif not ok and start is not None:
            segs.append([start, t])
            start = None
    merged: list[list[float]] = []
    for s, e in segs:
        if merged and s - merged[-1][1] < MIN_GAP:
            merged[-1][1] = e
        else:
            merged.append([s, e])
    return [(max(0.0, s - PAD), min(duration, e + PAD)) for s, e in merged]


def encode(src: Path, dst: Path, segs: list[tuple[float, float]], progress=None) -> None:
    """The kept stretches of src as one H.264 clip (config.H264_ARGS: what the player wants), niced."""
    sel = "+".join(f"between(t,{s:.2f},{e:.2f})" for s, e in segs)
    kept = sum(e - s for s, e in segs) or 1
    part = dst.with_name(dst.name + ".part.mp4")
    cmd = ["nice", "-n", "10", "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostats", "-nostdin", "-progress", "pipe:1", "-y",
           "-i", str(src), "-vf", f"select='{sel}',setpts=N/FRAME_RATE/TB", "-af", f"aselect='{sel}',asetpts=N/SR/TB",
           *H264_ARGS, str(part)]
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, text=True)
    for line in p.stdout:
        if progress and line.startswith("out_time_us=") and line[12:].strip().isdigit():
            progress(min(1.0, int(line[12:]) / 1e6 / kept))
    if p.wait():
        part.unlink(missing_ok=True)
        raise subprocess.CalledProcessError(p.returncode, "ffmpeg")
    part.replace(dst)


def compress(src: Path, dst: Path, progress=None) -> dict:
    """Analyse, cut and encode src into dst. Returns {kept: [[s, e]...], source_duration, duration, verified}.
    A clip without any signal is encoded whole (nothing cut), so nothing is silently dropped."""
    dur = video.probe(src)["duration"]
    ranges, changes = signal_stats(src)
    segs = keep_segments(ranges, dur, changes) or [(0.0, dur)]
    encode(src, dst, segs, progress)
    info = video.probe(dst)
    kept = sum(e - s for s, e in segs)
    return {"kept": [[round(s, 2), round(e, 2)] for s, e in segs], "source_duration": round(dur, 2),
            "duration": info["duration"], "codec": info["codec"], "verified": abs(info["duration"] - kept) <= 1.5}
