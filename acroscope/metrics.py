"""Numbers for a stretch of an arm: the analysis scripts (profile, flips, loopsum, orbit, kicks) as functions.

All windows are in ARM seconds. Convert from video time with `t - offset` (see sessions.match_for).
Sign conventions (Betaflight): roll + = right, pitch + = nose down / forward, yaw + = nose right. A back flip and a
powerloop integrate to a NEGATIVE pitch angle.
"""
import math

from .blackbox import Arm

AXES = ("roll", "pitch", "yaw")

# Fixed facts the agent would otherwise re-derive every session. Stated once, shipped with every metrics result.
CONVENTIONS = {
    "roll": "+ = roll right (right stick right). In the camera view the horizon rotates anticlockwise, the right side of the picture drops",
    "pitch": "+ = nose down / forward (right stick forward). In the camera view the horizon rises; a back flip and a powerloop integrate to NEGATIVE pitch",
    "yaw": "+ = nose right (left stick right). In the camera view the scene slides left",
    "stick_pct": "rcCommand[0..2] / 5: -100..100 % of stick travel; throttle_pct = (rcCommand[3] - 1000) / 10",
    "tilt_deg": "angle between the quad's up and world up: 0 level, 90 knife edge, 180 inverted",
    "lean": "[forward, right] in degrees from the IMU; negative forward = nose up",
    "rates_dps": "gyroADC is deg/s as logged (the gyro is already scaled); peaks are absolute values",
    "video_time": "video = arm time + offset (session.json matches); the OSD arm timer shows floor(seconds)",
}


def _mean(xs):
    xs = list(xs)
    return sum(xs) / len(xs) if xs else 0.0


def integrate(arm: Arm, i0: int, i1: int) -> list[float]:
    """Integrated rotation per axis (deg) over frames [i0, i1)."""
    ang = [0.0, 0.0, 0.0]
    t = arm.t
    g = [arm.col(f"gyroADC[{a}]") for a in range(3)]
    for i in range(max(i0 + 1, 1), i1):
        dt = t[i] - t[i - 1]
        for a in range(3):
            ang[a] += g[a][i] * dt
    return ang


def summary(arm: Arm, t0: float, t1: float) -> dict:
    """What the agent wants to know about a window: rotation, peaks, sticks, throttle, lean, battery, g."""
    i0, i1 = arm.window(t0, t1)
    if i1 <= i0:
        return {"arm_from": t0, "arm_to": t1, "frames": 0}
    r = range(i0, i1)
    g = [arm.col(f"gyroADC[{a}]") for a in range(3)]
    rc = [arm.col(f"rcCommand[{a}]") for a in range(4)]
    ang = integrate(arm, i0, i1)
    thr = [(rc[3][i] - 1000) / 10 for i in r]
    acc = [math.sqrt(sum(v * v for v in arm.acc_g(i))) for i in r]
    tilt = [arm.tilt(i) for i in r]
    motors = [arm.col(f"motor[{m}]") for m in range(4) if f"motor[{m}]" in arm]
    out = {
        "arm_from": round(t0, 2), "arm_to": round(t1, 2), "duration": round(arm.t[i1 - 1] - arm.t[i0], 2), "frames": i1 - i0,
        "rotation_deg": {AXES[a]: round(ang[a]) for a in range(3)},
        "peak_rate_dps": {AXES[a]: round(max(abs(g[a][i]) for i in r)) for a in range(3)},
        "stick_pct_max": {AXES[a]: round(max(abs(rc[a][i]) for i in r) / 5) for a in range(3)},
        "stick_pct_mean": {AXES[a]: round(_mean(rc[a][i] for i in r) / 5) for a in range(3)},
        "throttle_pct": {"min": round(min(thr)), "mean": round(_mean(thr)), "max": round(max(thr))},
        "tilt_deg": {"min": round(min(tilt)), "max": round(max(tilt)), "start": round(tilt[0]), "end": round(tilt[-1])},
        "lean_start": [round(x) for x in arm.lean(i0)], "lean_end": [round(x) for x in arm.lean(i1 - 1)],
        "acc_g": {"min": round(min(acc), 2), "max": round(max(acc), 2)},
        "vbat": {"min": round(min(arm.col("vbatLatest")[i] for i in r) / 100, 2),
                 "max": round(max(arm.col("vbatLatest")[i] for i in r) / 100, 2)},
    }
    if motors:
        out["motor_mean"] = round(_mean(_mean(m[i] for m in motors) for i in r))
        out["motor_max"] = round(max(max(m[i] for m in motors) for i in r))
    inv = [thr[k] for k, i in enumerate(r) if tilt[k] > 135]
    if inv:
        out["throttle_inverted_pct"] = {"mean": round(_mean(inv)), "min": round(min(inv)), "frames": len(inv)}
    return out


def profile(arm: Arm, t0: float, t1: float, step: float = 0.2) -> list[dict]:
    """Per-`step` table (profile.py): mean sticks and gyro, cumulative rotation, vbat, mean motor, accZ."""
    i0, i1 = arm.window(t0, t1)
    rc = [arm.col(f"rcCommand[{a}]") for a in range(4)]
    g = [arm.col(f"gyroADC[{a}]") for a in range(3)]
    motors = [arm.col(f"motor[{m}]") for m in range(4) if f"motor[{m}]" in arm]
    accz = arm.col("accSmooth[2]")
    vb = arm.col("vbatLatest")
    rows, ang, prev, cur, bin_i = [], [0.0, 0.0, 0.0], None, [], None

    def flush(b):
        n = len(cur)
        rows.append({"t": round(t0 + b * step, 2),
                     "stick": [round(_mean(rc[a][i] for i in cur) / 5) for a in range(3)],
                     "throttle": round(_mean((rc[3][i] - 1000) / 10 for i in cur)),
                     "gyro": [round(_mean(g[a][i] for i in cur)) for a in range(3)],
                     "rotation": [round(x) for x in ang],
                     "vbat": round(_mean(vb[i] for i in cur) / 100, 2),
                     "motor": round(_mean(_mean(m[i] for m in motors) for i in cur)) if motors else None,
                     "acc_z": round(_mean(accz[i] for i in cur) / 2048, 1),
                     "tilt": round(_mean(arm.tilt(i) for i in cur[:: max(1, n // 8)]))})

    for i in range(i0, i1):
        if prev is not None:
            dt = arm.t[i] - arm.t[prev]
            for a in range(3):
                ang[a] += g[a][i] * dt
        prev = i
        b = int((arm.t[i] - t0) / step)
        if bin_i is not None and b != bin_i:
            flush(bin_i)
            cur = []
        bin_i = b
        cur.append(i)
    if cur:
        flush(bin_i)
    return rows


def rotation_segments(arm: Arm, t0: float, t1: float, threshold: float = 150, min_dur: float = 0.2, gap: float = 0.1) -> list[dict]:
    """flips.py: stretches where |roll| or |pitch| rate > threshold, merged across gaps < `gap`, at least `min_dur`.
    Each gets integrated rotation, peak rates, stick and throttle. A 360 flip shows as |rotation| ~ 360."""
    i0, i1 = arm.window(t0, t1)
    g = [arm.col(f"gyroADC[{a}]") for a in range(3)]
    rc = [arm.col(f"rcCommand[{a}]") for a in range(4)]
    segs, cur = [], None
    for i in range(i0, i1):
        if max(abs(g[0][i]), abs(g[1][i])) > threshold:
            if cur and arm.t[i] - arm.t[cur[-1]] < gap:
                cur.append(i)
            else:
                cur = [i]
                segs.append(cur)
    out = []
    for s in segs:
        a, b = s[0], s[-1] + 1
        if arm.t[b - 1] - arm.t[a] < min_dur:
            continue
        ang = integrate(arm, a, b)
        thr = [(rc[3][i] - 1000) / 10 for i in range(a, b)]
        main = 0 if abs(ang[0]) >= abs(ang[1]) else 1
        out.append({
            "arm_from": round(arm.t[a], 2), "arm_to": round(arm.t[b - 1], 2), "duration": round(arm.t[b - 1] - arm.t[a], 2),
            "rotation_deg": {AXES[k]: round(ang[k]) for k in range(3)},
            "axis": AXES[main],
            "direction": ("right" if ang[0] > 0 else "left") if main == 0 else ("forward" if ang[1] > 0 else "back"),
            "peak_rate_dps": {AXES[k]: round(max(abs(g[k][i]) for i in range(a, b))) for k in range(2)},
            "stick_pct_max": round(max(max(abs(rc[0][i]), abs(rc[1][i])) for i in range(a, b)) / 5),
            "throttle_pct": {"min": round(min(thr)), "mean": round(_mean(thr))},
            "vbat_min": round(min(arm.col("vbatLatest")[i] for i in range(a, b)) / 100, 2),
        })
    return out


def loop_summary(arm: Arm, t0: float, t1: float, active_rate: float = -40) -> dict | None:
    """loopsum.py: one powerloop/back-flip window. Active part = where pitch rate < active_rate (pitching back).
    Reports total rotation, peak pitch rate, throttle while inverted, lean at start and end."""
    i0, i1 = arm.window(t0, t1)
    g = [arm.col(f"gyroADC[{a}]") for a in range(3)]
    rc3 = arm.col("rcCommand[3]")
    act = [i for i in range(i0, i1) if g[1][i] < active_rate]
    if not act:
        return None
    s, e = act[0], act[-1] + 1
    P = R = Y = 0.0
    inv = []
    for i in range(s + 1, e):
        dt = arm.t[i] - arm.t[i - 1]
        P += g[1][i] * dt
        R += g[0][i] * dt
        Y += g[2][i] * dt
        if -225 < P < -135:
            inv.append((rc3[i] - 1000) / 10)
    return {
        "arm_from": round(arm.t[s], 2), "arm_to": round(arm.t[e - 1], 2), "duration": round(arm.t[e - 1] - arm.t[s], 2),
        "rotation_deg": {"roll": round(R), "pitch": round(P), "yaw": round(Y)},
        "peak_pitch_rate_dps": round(min(g[1][i] for i in range(i0, i1))),
        "throttle_inverted_pct": {"mean": round(_mean(inv)), "min": round(min(inv))} if inv else None,
        "throttle_max_pct": round(max((rc3[i] - 1000) / 10 for i in range(i0, i1))),
        "lean_start": [round(x) for x in arm.lean(s)], "lean_end": [round(x) for x in arm.lean(e - 1)],
        "vbat_min": round(min(arm.col("vbatLatest")[i] for i in range(i0, i1)) / 100, 2),
    }


def orbit(arm: Arm, t0: float, t1: float, step: float = 0.5) -> list[dict]:
    """orbit.py: per `step`, the unwrapped azimuth of the thrust direction, its rate, the tilt and the lean
    direction relative to the nose (0 = forward, 90 = right). A nose-in orbit shows a steady azimuth rate."""
    i0, i1 = arm.window(t0, t1)
    prev, az, out = None, 0.0, []
    rows = []
    for i in range(i0, i1):
        tx, ty, tz = arm.thrust_world(i)
        a = math.degrees(math.atan2(ty, tx))
        if prev is not None:
            az += (a - prev + 180) % 360 - 180
        prev = a
        ux, uy, _ = arm.up_body(i)
        rows.append((arm.t[i], az, math.degrees(math.acos(max(-1.0, min(1.0, tz)))), math.degrees(math.atan2(-uy, -ux))))
    b = t0
    while b < t1:
        d = [r for r in rows if b <= r[0] < b + step]
        if d:
            out.append({"t": round(b, 2), "azimuth": round(d[-1][1]), "azimuth_rate": round((d[-1][1] - d[0][1]) / step),
                        "tilt": round(_mean(r[2] for r in d)), "lean_dir_vs_nose": round(_mean(r[3] for r in d))})
        b += step
    return out


def kicks(arm: Arm, t0: float = 0, t1: float = 1e9, rate: float = 400, ratio: float = 2, stick: float = 30) -> list[dict]:
    """kicks.py: gyro yaw kicks = |raw yaw| > rate, more than `ratio` x roll/pitch, yaw stick within `stick`.
    Misses kicks while the yaw stick is deflected (see the 10-05 003 note)."""
    i0, i1 = arm.window(t0, t1)
    u = [arm.col(f"gyroUnfilt[{a}]") for a in range(3)]
    rc2 = arm.col("rcCommand[2]")
    out, last = [], -9.0
    for i in range(i0, i1):
        y = abs(u[2][i])
        if y > rate and y > ratio * max(abs(u[0][i]), abs(u[1][i])) and abs(rc2[i]) < stick and arm.t[i] - last > 0.5:
            out.append({"arm_t": round(arm.t[i], 2), "raw_yaw_dps": round(u[2][i])})
            last = arm.t[i]
    return out
