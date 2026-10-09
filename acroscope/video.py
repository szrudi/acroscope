"""Video access: ffprobe metadata (cached), frame extraction, H.264 proxies.

Clips come from the Skyzone Cobra SD DVR, cut and compressed by cobra-compress.py in the data dir. Reading them over
the Drive mount is slow the first time (rclone fetches the file), so probe results are cached by (size, mtime).
"""
import json
import subprocess
from pathlib import Path

from .config import H264_ARGS, VIDEOS_DIR, cache


def resolve_video(session: str, file: str) -> Path:
    """Accept a clip name ('2026-10-07_007.mp4'), its bare number ('007'/'7') or a path."""
    p = Path(file).expanduser()
    if p.is_file():
        return p
    d = VIDEOS_DIR / session
    if (d / file).is_file():
        return d / file
    if file.isdigit():
        hits = sorted(d.glob(f"*_{int(file):03d}.*"))
        hits = [h for h in hits if h.suffix.lower() in (".mp4", ".mov", ".mkv")]
        if len(hits) == 1:
            return hits[0]
    raise FileNotFoundError(f"{session}/{file}: no such clip")


def probe(path: Path) -> dict:
    """{duration, codec, width, height, fps, size} for a clip, cached."""
    st = path.stat()
    cp = cache("probe", path.parent.name, f"{path.name}.{st.st_size}.{int(st.st_mtime)}.json")
    if cp.exists():
        return json.loads(cp.read_text())
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
         "format=duration:stream=codec_name,width,height,r_frame_rate", "-of", "json", str(path)],
        capture_output=True, text=True, check=True).stdout
    j = json.loads(out)
    s = (j.get("streams") or [{}])[0]
    num, _, den = (s.get("r_frame_rate") or "0/1").partition("/")
    info = {"duration": round(float(j["format"]["duration"]), 3), "codec": s.get("codec_name"),
            "width": s.get("width"), "height": s.get("height"),
            "fps": round(float(num) / float(den or 1), 3), "size": st.st_size}
    cp.write_text(json.dumps(info))
    return info


def frame(path: Path, t: float, osd: bool = False, scale: int = 1) -> Path:
    """Extract one frame at video time t as JPEG (cached). osd=True crops the OSD strip at the bottom (arm timer,
    total armed time, battery, link) and enlarges it so the digits are easy to read."""
    tag = f"{t:08.2f}" + ("-osd" if osd else "") + (f"-x{scale}" if scale != 1 else "")
    out = cache("frames", path.parent.name, f"{path.stem}_{tag}.jpg")
    if out.exists():
        return out
    vf = []
    if osd:
        vf.append("crop=iw:ih*0.22:0:ih*0.73")   # bottom strip: LQ/RSSI/vbat left, the two timers right
    if scale != 1 or osd:
        vf.append(f"scale=iw*{scale if scale != 1 else 2}:-1:flags=lanczos")
    cmd = ["ffmpeg", "-v", "error", "-y", "-ss", f"{t:.3f}", "-i", str(path), "-frames:v", "1", "-q:v", "3"]
    if vf:
        cmd += ["-vf", ",".join(vf)]
    subprocess.run(cmd + [str(out)], check=True)
    return out


def proxy_path(path: Path) -> Path:
    return cache("h264", path.parent.name, path.with_suffix(".mp4").name)


def playable(path: Path) -> Path:
    """The file to serve to the browser: the clip itself when it is H.264 already, else its proxy (if made)."""
    if probe(path).get("codec") == "h264":
        return path
    px = proxy_path(path)
    return px if px.exists() else path


def transcode(src: Path, dst: Path, progress=None) -> Path:
    """Re-encode a clip to the player's H.264 settings (see config.H264_ARGS)."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    part = dst.with_name(dst.name + ".part.mp4")
    dur = probe(src)["duration"] or 1
    p = subprocess.Popen(["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostats", "-progress", "pipe:1", "-y",
                          "-i", str(src), *H264_ARGS, str(part)], stdout=subprocess.PIPE, text=True)
    for line in p.stdout:
        if progress and line.startswith("out_time_us=") and line[12:].strip().isdigit():
            progress(min(1.0, int(line[12:]) / 1e6 / dur))
    if p.wait():
        raise subprocess.CalledProcessError(p.returncode, "ffmpeg")
    part.replace(dst)
    return dst
