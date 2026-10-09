"""Blackbox (.bbl) access: list the arms in a file, decode an arm with orangebox, cache it as columns.

One .bbl is a full flash dump holding many arms ("logs" in decoder terms, 1-based). Decoding is pure Python
(~2.5 s per 100 s arm), so every arm is decoded once into CACHE_DIR/arms/<bbl stem>/arm-NN.bin and
indexed in arms.json next to it.

Units as they come out of the log (same as blackbox_decode's CSV):
  time (us) -> here converted to seconds from the arm's first frame
  gyroADC/gyroUnfilt: deg/s        rcCommand[0..2]: -500..500 (= -100..100 % stick)
  rcCommand[3]: 1000..2000         motor[]: 48..2047            accSmooth: /2048 = g
  vbatLatest: 0.01 V               amperageLatest: 0.01 A       imuQuaternion[0..2]: x,y,z * 32767
"""
import array
import json
import logging
import math
import struct
from bisect import bisect_left
from pathlib import Path

from .config import BLACKBOX_DIR, cache

logging.getLogger("orangebox").setLevel(logging.ERROR)

HEADER_KEYS = ["Firmware revision", "Craft name", "looptime", "pid_process_denom", "P interval", "P ratio",
               "rates", "rc_rates", "rc_expo", "rollPID", "pitchPID", "yawPID", "gyro_notch1_hz", "motorOutput",
               "acc_1G", "vbat_scale", "gyro_scale"]
MAGIC = b"ACRO1\n"
INDEX_VERSION = 2


def resolve_bbl(name: str) -> Path:
    """Accept a full path, a file name, or a unique fragment such as '20261007_212530'."""
    p = Path(name).expanduser()
    if p.is_file():
        return p
    if (BLACKBOX_DIR / name).is_file():
        return BLACKBOX_DIR / name
    hits = [f for f in sorted(BLACKBOX_DIR.glob("*.bbl")) if name in f.name]
    if len(hits) != 1:
        raise FileNotFoundError(f"{name}: {len(hits)} blackbox files match (need exactly 1)")
    return hits[0]


def bbl_date(path: Path) -> str | None:
    """'2026-10-07' from BTFL_BLACKBOX_LOG_..._20261007_212530_....bbl, or None."""
    for part in path.stem.split("_"):
        if len(part) == 8 and part.isdigit():
            return f"{part[:4]}-{part[4:6]}-{part[6:]}"
    return None


class Arm:
    """One decoded arm as columns. `t` is seconds from the first frame; `col(name)` gives a float32 array."""

    def __init__(self, meta: dict, t: array.array, cols: dict):
        self.meta = meta
        self.bbl = meta["bbl"]
        self.index = meta["index"]
        self.fields = meta["fields"]
        self.rate = meta["rate"]
        self.length = meta["length"]
        self.n = meta["frames"]
        self.t = t
        self._cols = cols

    def col(self, name: str) -> array.array:
        return self._cols[name]

    def __contains__(self, name):
        return name in self._cols

    def window(self, t0: float, t1: float) -> tuple[int, int]:
        """Index range [i0, i1) covering arm time t0 <= t < t1."""
        return bisect_left(self.t, t0), bisect_left(self.t, t1)

    # unit helpers
    def throttle_pct(self, i: int) -> float:
        return (self._cols["rcCommand[3]"][i] - 1000) / 10

    def stick_pct(self, axis: int, i: int) -> float:
        return self._cols[f"rcCommand[{axis}]"][i] / 5

    def vbat(self, i: int) -> float:
        return self._cols["vbatLatest"][i] / 100

    def acc_g(self, i: int) -> tuple[float, float, float]:
        return tuple(self._cols[f"accSmooth[{a}]"][i] / 2048 for a in range(3))

    def quat(self, i: int) -> tuple[float, float, float, float]:
        x, y, z = (self._cols[f"imuQuaternion[{a}]"][i] / 32767 for a in range(3))
        return x, y, z, math.sqrt(max(0.0, 1 - x * x - y * y - z * z))

    def up_body(self, i: int) -> tuple[float, float, float]:
        """World-up expressed in body axes. Negative x = leaning forward."""
        x, y, z, w = self.quat(i)
        return 2 * (x * z - w * y), 2 * (y * z + w * x), 1 - 2 * (x * x + y * y)

    def thrust_world(self, i: int) -> tuple[float, float, float]:
        """Body-up (thrust axis) expressed in world axes."""
        x, y, z, w = self.quat(i)
        return 2 * (x * z + w * y), 2 * (y * z - w * x), 1 - 2 * (x * x + y * y)

    def lean(self, i: int) -> tuple[float, float]:
        """(forward, right) lean in degrees from the IMU."""
        ux, uy, uz = self.up_body(i)
        return -math.degrees(math.atan2(ux, uz)), -math.degrees(math.atan2(uy, uz))

    def tilt(self, i: int) -> float:
        """Angle between body-up and world-up, degrees (0 level, 180 inverted)."""
        return math.degrees(math.acos(max(-1.0, min(1.0, self.up_body(i)[2]))))


# ---- decoding -------------------------------------------------------------------------------------------------

def _decode(path: Path, index: int):
    """Decode one log with orangebox. Returns (meta, t, cols). The last log of a full flash is truncated and ends in
    an exception mid-frame; what was parsed until then is kept and flagged."""
    from orangebox import Parser
    p = Parser.load(str(path), index)
    fields = p.field_names
    ix = {n: i for i, n in enumerate(fields)}
    rows = []
    truncated = False
    try:
        for fr in p.frames():
            rows.append(fr.data)
    except Exception as e:  # noqa: BLE001  (orangebox raises TypeError/RuntimeError on a cut-off log)
        truncated = True
        trunc_err = f"{type(e).__name__}: {e}"[:120]
    headers = {k: p.headers.get(k) for k in HEADER_KEYS if k in p.headers}
    events = [{"type": e.type.name, **(e.data or {})} for e in p.events]
    if not rows:
        meta = {"bbl": path.stem, "file": path.name, "index": index, "frames": 0, "length": 0.0, "rate": 0.0,
                "fields": fields, "truncated": truncated, "headers": headers, "events": events}
        return meta, array.array("d"), {}
    ti = ix["time"]
    t0 = rows[0][ti]
    t = array.array("d", ((r[ti] - t0) / 1e6 for r in rows))
    # `time` is the FC's uptime in us: it runs on across arms within one power cycle ("boot"), so the gaps
    # between arms are known, and one video offset covers every arm of a boot
    uptime_start, uptime_end = round(t0 / 1e6, 3), round(rows[-1][ti] / 1e6, 3)
    cols = {}
    for name, i in ix.items():
        if name == "time":
            continue
        cols[name] = array.array("f", (float(r[i]) if r[i] != "" else 0.0 for r in rows))
    length = t[-1]
    rpms = [cols[f"eRPM[{m}]"] for m in range(4) if f"eRPM[{m}]" in cols]
    motors = [cols[f"motor[{m}]"] for m in range(4) if f"motor[{m}]" in cols]
    # "armed, motors not spinning" arms show eRPM ~0 throughout (the motor command still moves)
    spun = any(max(r) > 100 for r in rpms) if rpms else (any(max(m) > 600 for m in motors) if motors else None)
    disarm = next((e.get("reason") for e in events if e["type"] == "DISARM"), None)
    meta = {
        "bbl": path.stem, "file": path.name, "index": index, "frames": len(rows), "length": round(length, 3),
        "rate": round(len(rows) / length, 1) if length else 0.0, "fields": fields, "truncated": truncated,
        "uptime_start": uptime_start, "uptime_end": uptime_end,
        "vbat_start": round(cols["vbatLatest"][0] / 100, 2) if "vbatLatest" in cols else None,
        "vbat_end": round(cols["vbatLatest"][-1] / 100, 2) if "vbatLatest" in cols else None,
        "vbat_min": round(min(cols["vbatLatest"]) / 100, 2) if "vbatLatest" in cols else None,
        "motors_spun": spun, "disarm_reason": disarm, "headers": headers, "events": events,
    }
    if truncated:
        meta["error"] = trunc_err
    # orangebox ends a log at its LOG_END event; without one the log stopped mid-stream (flash full, power cut)
    meta["complete"] = any(e["type"] == "LOG_END" for e in events)
    return meta, t, cols


def _bin_path(stem: str, index: int) -> Path:
    return cache("arms", stem, f"arm-{index:02d}.bin")


def _save(meta: dict, t: array.array, cols: dict, path: Path):
    head = json.dumps(meta).encode()
    with open(path, "wb") as f:
        f.write(MAGIC)
        f.write(struct.pack("<Q", len(head)))
        f.write(head)
        f.write(t.tobytes())
        for name in meta["fields"]:
            if name != "time":
                f.write(cols[name].tobytes())


def _load(path: Path) -> Arm:
    with open(path, "rb") as f:
        assert f.read(len(MAGIC)) == MAGIC, f"{path}: not an acroscope arm cache"
        (n,) = struct.unpack("<Q", f.read(8))
        meta = json.loads(f.read(n))
        frames = meta["frames"]
        t = array.array("d")
        t.frombytes(f.read(8 * frames))
        cols = {}
        for name in meta["fields"]:
            if name == "time":
                continue
            a = array.array("f")
            a.frombytes(f.read(4 * frames))
            cols[name] = a
    return Arm(meta, t, cols)


def index_bbl(name: str, force: bool = False, progress=None, cached_only: bool = False) -> dict | None:
    """Decode every arm of a .bbl into the cache (once) and return the index {file, stem, date, arms: [meta…]}.
    The per-arm meta leaves out `fields`/`events`/`headers` details that only matter when loading.
    With cached_only, return None instead of decoding (a 16 MB file takes ~30 s plus the download)."""
    path = resolve_bbl(name)
    idx_path = cache("arms", path.stem, "arms.json")
    if idx_path.exists() and not force:
        idx = json.loads(idx_path.read_text())
        if idx.get("v") == INDEX_VERSION:
            return idx
    if cached_only:
        return None
    if path.stat().st_size == 0:
        idx = {"v": INDEX_VERSION, "file": path.name, "stem": path.stem, "date": bbl_date(path), "arms": [], "empty": True}
        idx_path.write_text(json.dumps(idx, indent=1))
        return idx
    from orangebox.reader import Reader
    count = Reader(str(path)).log_count
    arms = []
    for i in range(1, count + 1):
        if progress:
            progress(f"{path.name}: decoding arm {i}/{count}")
        meta, t, cols = _decode(path, i)
        if meta["frames"]:
            _save(meta, t, cols, _bin_path(path.stem, i))
        arms.append({k: v for k, v in meta.items() if k not in ("fields", "events", "bbl", "file")}
                    | {"headers": {k: meta["headers"].get(k) for k in ("pitchPID", "rollPID", "yawPID", "gyro_notch1_hz")}})
    idx = {"v": INDEX_VERSION, "file": path.name, "stem": path.stem, "date": bbl_date(path), "arms": arms, "boots": boots(arms)}
    idx_path.write_text(json.dumps(idx, indent=1))
    return idx


def boots(arms: list[dict]) -> list[dict]:
    """Group arms into power cycles: uptime runs on within a boot and restarts (goes backwards) on the next.
    Each boot: {first, last, arms: [index…], length} with length = uptime span from the first arm's start."""
    out = []
    for a in arms:
        if not a.get("frames"):
            continue
        if out and a["uptime_start"] > out[-1]["_end"]:
            out[-1]["arms"].append(a["index"]); out[-1]["last"] = a["index"]; out[-1]["_end"] = a["uptime_end"]
        else:
            out.append({"first": a["index"], "last": a["index"], "arms": [a["index"]], "_start": a["uptime_start"], "_end": a["uptime_end"]})
    for b in out:
        b["uptime_start"], b["uptime_end"] = b.pop("_start"), b.pop("_end")
        b["length"] = round(b["uptime_end"] - b["uptime_start"], 1)
    return out


def load_arm(name: str, index: int) -> Arm:
    path = resolve_bbl(name)
    bp = _bin_path(path.stem, index)
    if not bp.exists():
        index_bbl(name)
    if not bp.exists():
        raise FileNotFoundError(f"{path.name} arm {index}: no frames decoded")
    return _load(bp)


def all_bbls() -> list[Path]:
    return sorted(BLACKBOX_DIR.glob("*.bbl"))
