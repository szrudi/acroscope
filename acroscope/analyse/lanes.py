"""What the player draws from an arm: the lanes under the video (throttle, tilt, vbat and the gyro's peak per step) and
the virtual sticks (the four channels in the picture's terms).
"""
from functools import lru_cache

from ..sources import blackbox


@lru_cache(maxsize=16)
def series(bbl: str, arm: int, step: float) -> dict:
    """Downsampled lanes for the strip under the video: per `step` s, throttle %, tilt deg, vbat V, |gyro| max."""
    a = blackbox.load_arm(bbl, arm)
    rc3, vb = a.col("rcCommand[3]"), a.col("vbatLatest")
    g = [a.col(f"gyroADC[{k}]") for k in range(3)]
    n = int(a.length / step) + 1
    rows = []
    for b in range(n):
        i0, i1 = a.window(b * step, (b + 1) * step)
        if i1 <= i0:
            rows.append(None)
            continue
        mid = (i0 + i1) // 2
        rows.append([round((rc3[mid] - 1000) / 10), round(a.tilt(mid)), round(vb[mid] / 100, 2),
                     round(max(max(abs(g[k][i]) for k in range(3)) for i in range(i0, i1, max(1, (i1 - i0) // 20))))])
    return {"step": step, "length": a.length, "fields": ["throttle", "tilt", "vbat", "gyro_max"], "rows": rows}


@lru_cache(maxsize=16)
def sticks(bbl: str, arm: int, step: float) -> dict:
    """The sticks every `step` s for the player's virtual sticks, in the picture's terms: Mode 2, every axis + = stick
    right or up, in % (throttle 0..100, the others -100..100). rcCommand[2] is + for the left stick LEFT
    (metrics.CONVENTIONS), so yaw is negated here and nowhere else."""
    a = blackbox.load_arm(bbl, arm)
    rc = [a.col(f"rcCommand[{k}]") for k in range(4)]
    out = {"throttle": [], "yaw": [], "pitch": [], "roll": []}
    i = 0
    for b in range(int(a.length / step) + 1):
        while i + 1 < a.n and a.t[i + 1] <= b * step:
            i += 1
        out["throttle"].append(max(0, min(100, round((rc[3][i] - 1000) / 10))))
        out["yaw"].append(round(-rc[2][i] / 5))
        out["pitch"].append(round(rc[1][i] / 5))
        out["roll"].append(round(rc[0][i] / 5))
    return {"step": step, "length": a.length, **out}
