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

from .config import BLACKBOX_DIR, NAMES_ONLY, cache

logging.getLogger("orangebox").setLevel(logging.ERROR)

HEADER_KEYS = ["Firmware revision", "Craft name", "looptime", "pid_process_denom", "P interval", "P ratio",
               "rates_type", "rates", "rc_rates", "rc_expo", "rate_limits", "quick_rates_rc_expo", "thr_mid", "thr_expo", "thr_hover", "rollPID", "pitchPID", "yawPID",
               "gyro_notch1_hz", "motorOutput", "acc_1G", "vbat_scale", "gyro_scale"]
MAGIC = b"ACRO1\n"
INDEX_VERSION = 3        # 3: rates_type read and the rate profile (max and centre rate per axis) in every arm's meta (#22)

# Betaflight's rate types (`rates_type`, since 4.2): what `rates`, `rc_rates` and `rc_expo` mean.
RATES_TYPES = ["betaflight", "raceflight", "kiss", "actual", "quick"]
SETPOINT_RATE_LIMIT = 1998.0
RC_RATE_INCREMENTAL = 14.54


def resolve_bbl(name: str) -> Path:
    """Accept a full path (not for a client's command), a file name, or a unique fragment such as '20261007_212530'."""
    p = Path(name).expanduser()
    if p.is_file() and not NAMES_ONLY:
        return p
    if (BLACKBOX_DIR / name).is_file():
        return BLACKBOX_DIR / name
    hits = [f for f in sorted(BLACKBOX_DIR.glob("*.bbl")) if name in f.name]
    if len(hits) != 1:
        raise FileNotFoundError(f"{name}: {len(hits)} blackbox files match (need exactly 1)")
    return hits[0]


def rates(headers: dict) -> dict | None:
    """The rate profile the arm was flown on, from its header: {type, max_dps: [r, p, y], centre_dps: [r, p, y]}.
    `max_dps` is the rate at full stick, `centre_dps` the slope at stick centre per full stick (what a small
    correction does). Ported from Betaflight's rc.c (applyBetaflightRates and friends, 4.4); expo never moves the
    full-stick rate. None when the header has no rates_type (a log decoded before it was kept: re-decode) or
    the values are not three per axis."""
    try:
        if not 0 <= int(headers["rates_type"]) < len(RATES_TYPES):
            return None
        kind = RATES_TYPES[int(headers["rates_type"])]
        rc, sup, expo = ([float(v) for v in headers[k]] for k in ("rc_rates", "rates", "rc_expo"))
        assert len(rc) == len(sup) == len(expo) == 3
        limits = [float(v) for v in headers.get("rate_limits") or [SETPOINT_RATE_LIMIT] * 3]
        assert len(limits) == 3
    except (KeyError, IndexError, TypeError, ValueError, AssertionError):
        return None
    mx, ce = [], []
    for r, s, e, lim in zip(rc, sup, expo, limits):
        e /= 100
        if kind == "betaflight":
            rr = r / 100
            if rr > 2:
                rr += RC_RATE_INCREMENTAL * (rr - 2)
            top = 200 * rr / max(1 - s / 100, 0.01)
            mid = 200 * rr * (1 - e)
        elif kind == "raceflight":
            top = 10 * r * (1 + s / 100)
            mid = 10 * r * (1 - e)
        elif kind == "kiss":
            top = 2000 / max(1 - s / 100, 0.01) * r / 1000
            mid = 2000 * (1 - e) * r / 1000
        elif kind == "actual":
            mid = r * 10
            top = max(s * 10, mid)
        else:                                       # quick: expo shapes the centre only with quick_rates_rc_expo on
            mid = r * 2
            top = max(s * 10, mid)
            if int(headers.get("quick_rates_rc_expo") or 0):
                mid *= 1 - e
        mx.append(round(min(top, lim, SETPOINT_RATE_LIMIT), 1))
        ce.append(round(mid, 1))
    return {"type": kind, "max_dps": mx, "centre_dps": ce}


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
        """World-up expressed in body axes, Betaflight's: X forward, Y LEFT, Z up (its rMat[2] row). Negative x = leaning
        forward; positive y = up leans to the left side, i.e. the right side is down (a right bank)."""
        x, y, z, w = self.quat(i)
        return 2 * (x * z - w * y), 2 * (y * z + w * x), 1 - 2 * (x * x + y * y)

    def thrust_world(self, i: int) -> tuple[float, float, float]:
        """Body-up (thrust axis) expressed in world axes."""
        x, y, z, w = self.quat(i)
        return 2 * (x * z + w * y), 2 * (y * z - w * x), 1 - 2 * (x * x + y * y)

    def lean(self, i: int) -> tuple[float, float]:
        """(forward, right) lean in degrees from the IMU: + = nose down, + = right side down. Right is Betaflight's
        own roll angle, atan2(uy, uz) (#38: it was negated, a left bank read as right)."""
        ux, uy, uz = self.up_body(i)
        return -math.degrees(math.atan2(ux, uz)), math.degrees(math.atan2(uy, uz))

    def tilt(self, i: int) -> float:
        """Angle between body-up and world-up, degrees (0 level, 180 inverted)."""
        return math.degrees(math.acos(max(-1.0, min(1.0, self.up_body(i)[2]))))


# ---- decoding -------------------------------------------------------------------------------------------------

def _decode(path: Path, index: int):
    """Decode one log with orangebox. Returns (meta, t, cols). The last log of a full flash is truncated and ends in
    an exception mid-frame; what was parsed until then is kept and flagged."""
    from orangebox import Parser
    try:
        p = Parser.load(str(path), index)
    except Exception as e:  # noqa: BLE001  (a segment orangebox cannot open: noted, the other arms decode on)
        # 2026-10-16's dump: segment 27 of 61 gave "read length must be non-negative" and, uncaught, cost the whole
        # index, so the server started the decode again on every look
        return ({"bbl": path.stem, "file": path.name, "index": index, "frames": 0, "length": 0.0, "rate": 0.0,
                 "fields": [], "truncated": False, "headers": {}, "events": [], "error": f"{type(e).__name__}: {e}"[:160]},
                array.array("d"), {})
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
        "motors_spun": spun, "disarm_reason": disarm, "headers": headers, "events": events, "rates": rates(headers),
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
                    | {"headers": {k: meta["headers"].get(k) for k in ("Craft name", "pitchPID", "rollPID", "yawPID", "gyro_notch1_hz")}})
    idx = {"v": INDEX_VERSION, "file": path.name, "stem": path.stem, "date": bbl_date(path), "arms": arms, "boots": boots(arms)}
    idx_path.write_text(json.dumps(idx, indent=1))
    return idx


def segments(name: str) -> list[dict]:
    """Every log segment in a .bbl as orangebox finds them (by the header's first line): [{index, start, size}] in
    bytes, so a pack missing from the flash can be told from one there but unreadable (#21). [] for an empty file."""
    from orangebox.reader import Reader
    path = resolve_bbl(name)
    if path.stat().st_size == 0:
        return []
    ptrs = Reader(str(path)).log_pointers
    ends = ptrs[1:] + [path.stat().st_size]
    return [{"index": i + 1, "start": s, "size": e - s} for i, (s, e) in enumerate(zip(ptrs, ends))]


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
    """One arm from the cache; a log whose index is missing or from an older version is decoded first, so the arm
    carries what the current version writes into its meta (the rates since v3)."""
    path = resolve_bbl(name)
    bp = _bin_path(path.stem, index)
    if not bp.exists() or index_bbl(name, cached_only=True) is None:
        index_bbl(name)
    if not bp.exists():
        raise FileNotFoundError(f"{path.name} arm {index}: no frames decoded")
    return _load(bp)


def all_bbls() -> list[Path]:
    return sorted(BLACKBOX_DIR.glob("*.bbl"))
