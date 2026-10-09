"""Event detectors: suggested moments from an arm (flips/rolls, powerloops, split-S, dives, crashes, yaw kicks,
single-motor loss). Heuristics tuned on the Air65 II logs of Oct 2026; each result carries the numbers it was judged
on (`why`, `numbers`) so the agent or the person can accept or dismiss it.
"""
import math

from .blackbox import Arm
from . import metrics


def _acc(arm, i):
    return math.sqrt(sum(v * v for v in arm.acc_g(i)))


def crashes(arm: Arm, g_min: float = 4.0, cooldown: float = 2.0) -> list[dict]:
    """A hard acceleration spike (> g_min) with the gyro jolting within 50 ms = contact."""
    out, last = [], -9.0
    g = [arm.col(f"gyroUnfilt[{a}]") for a in range(3)]
    w = max(1, int(arm.rate * 0.05))
    for i in range(arm.n):
        if arm.t[i] - last < cooldown:
            continue
        a = _acc(arm, i)
        if a > g_min:
            j = max(abs(g[k][x]) for k in range(3) for x in range(i, min(arm.n, i + w)))
            if j > 500:
                # an impact in the last seconds of an arm is how most arms end: a (hard) landing, not a crash mid-flight
                landing = arm.length - arm.t[i] < 2.5
                out.append({"tag": "crash", "arm_from": round(arm.t[i] - 0.5, 2), "arm_to": round(arm.t[i] + 1.0, 2),
                            "title": "hard landing (arm end)" if landing else "impact",
                            "why": f"{a:.1f} g with {j:.0f} dps gyro jolt at arm {arm.t[i]:.2f} s" + (f" of {arm.length:.1f}" if landing else ""),
                            "numbers": {"acc_g": round(a, 1), "gyro_jolt_dps": round(j), "arm_t": round(arm.t[i], 2)}})
                last = arm.t[i]
    return out


def loops(arm: Arm, active_rate: float = -40, gap: float = 0.6, min_deg: float = 300, min_dur: float = 1.5) -> list[dict]:
    """Powerloops: sustained pitching back (rate < active_rate, gaps < `gap` s) adding up to >= min_deg over at
    least min_dur s. Fast back flips (< min_dur) are left to flips_and_rolls."""
    g1 = arm.col("gyroADC[1]")
    runs, cur = [], None
    for i in range(arm.n):
        if g1[i] < active_rate:
            if cur and arm.t[i] - arm.t[cur[1]] < gap:
                cur[1] = i
            else:
                cur = [i, i]
                runs.append(cur)
    out = []
    for a, b in runs:
        dur = arm.t[b] - arm.t[a]
        if dur < min_dur:
            continue
        ls = metrics.loop_summary(arm, arm.t[a], arm.t[b] + 0.01)
        if not ls or ls["rotation_deg"]["pitch"] > -min_deg:
            continue
        ti = ls["throttle_inverted_pct"]
        out.append({"tag": "powerloop", "arm_from": ls["arm_from"], "arm_to": ls["arm_to"], "title": "powerloop",
                    "why": f"{ls['rotation_deg']['pitch']} deg pitch in {ls['duration']} s, peak {-ls['peak_pitch_rate_dps']} dps, "
                           f"throttle inverted {ti['mean'] if ti else '?'} %", "numbers": ls})
    return out


def flips_and_rolls(arm: Arm, exclude: list[tuple[float, float]] = ()) -> list[dict]:
    """Fast rotation segments with >= 270 deg on one axis (flip = pitch, roll = roll). A segment with a big roll
    plus a pull back at low throttle is a split-S. Segments inside `exclude` windows (loops, crashes) are skipped,
    as are tumbles (peak > 1500 dps)."""
    out = []
    for s in metrics.rotation_segments(arm, 0, arm.length, threshold=100, min_dur=0.3, gap=0.3):
        if any(lo <= s["arm_from"] <= hi or lo <= s["arm_to"] <= hi for lo, hi in exclude):
            continue
        rot, main = s["rotation_deg"], s["axis"]
        if max(s["peak_rate_dps"].values()) > 1500:
            continue
        if 150 <= abs(rot["roll"]) <= 300 and rot["pitch"] <= -60 and s["throttle_pct"]["min"] < 35:
            tag, title = "split-s", "split-S"
        elif abs(rot[main]) < 270:
            continue
        elif main == "pitch":
            tag, title = "flip", f"{s['direction']} flip"
        else:
            tag, title = "roll", f"{s['direction']} roll"
        if abs(rot["yaw"]) > 0.5 * abs(rot[main]) and tag in ("flip", "roll"):
            title += " with yaw"
        out.append({"tag": tag, "arm_from": s["arm_from"], "arm_to": s["arm_to"], "title": title,
                    "why": f"roll {rot['roll']:+} / pitch {rot['pitch']:+} / yaw {rot['yaw']:+} deg in {s['duration']} s, "
                           f"peak {max(s['peak_rate_dps'].values())} dps, throttle {s['throttle_pct']['min']}-{s['throttle_pct']['mean']} %",
                    "numbers": s})
    return out


def dives(arm: Arm, min_dur: float = 1.2, min_tilt: float = 40, blip: float = 0.3) -> list[dict]:
    """Throttle cut (< 5 %) for at least min_dur s while pointing well down (mean tilt >= min_tilt): a dive.
    Throttle blips shorter than `blip` s don't end it. Note the total acceleration stays near 1 g from drag; only
    body-z is ~0, so g is not the criterion."""
    rc3 = arm.col("rcCommand[3]")
    out, start, up_since = [], None, None
    for i in range(arm.n + 1):
        cut = i < arm.n and rc3[i] < 1050
        if cut and start is None:
            start = i
        elif cut:
            up_since = None
        elif start is not None and i < arm.n and (up_since is None or arm.t[i] - arm.t[up_since] < blip):
            up_since = up_since if up_since is not None else i
        elif start is not None:
            i = up_since if up_since is not None else i
            up_since = None
            if arm.t[i - 1] - arm.t[start] >= min_dur:
                step = max(1, (i - start) // 40)
                tilt = [arm.tilt(k) for k in range(start, i, step)]
                if sum(tilt) / len(tilt) >= min_tilt:
                    d = round(arm.t[i - 1] - arm.t[start], 2)
                    out.append({"tag": "dive", "arm_from": round(arm.t[start], 2), "arm_to": round(arm.t[i - 1], 2), "title": "dive",
                                "why": f"throttle cut for {d} s, tilt {min(tilt):.0f}-{max(tilt):.0f} deg",
                                "numbers": {"throttle_cut_s": d, "tilt_max": round(max(tilt)), "vbat_min": round(min(arm.col('vbatLatest')[start:i]) / 100, 2)}})
            start = None
    return out


def yaw_kicks(arm: Arm) -> list[dict]:
    return [{"tag": "gyro-kick", "arm_from": round(k["arm_t"] - 0.3, 2), "arm_to": round(k["arm_t"] + 0.7, 2),
             "title": "gyro yaw kick", "why": f"raw yaw {k['raw_yaw_dps']} dps with the yaw stick centred", "numbers": k}
            for k in metrics.kicks(arm)]


def motor_loss(arm: Arm, crash_times: list[float] = (), drop: float = 0.5, window: float = 0.05, hold: float = 0.4) -> list[dict]:
    """One motor's eRPM falls by `drop` within `window` s while its command saturates, stays down for `hold` s, and
    nothing hit the quad (no crash within 0.5 s before). The PID cuts the other motors to compensate, so they drop too;
    the tell is the one motor at full command with low RPM (10-07 arm 20, the repaired motor)."""
    if "eRPM[0]" not in arm:
        return []
    rpm = [arm.col(f"eRPM[{m}]") for m in range(4)]
    cmd = [arm.col(f"motor[{m}]") for m in range(4)]
    w, h = max(1, int(arm.rate * window)), max(1, int(arm.rate * hold))
    out, last = [], -9.0
    for i in range(w, arm.n - h):
        if arm.t[i] - last < 2 or any(abs(arm.t[i] - c) < 0.5 for c in crash_times):
            continue
        for m in range(4):
            before, now = rpm[m][i - w], rpm[m][i]
            if before > 1500 and now < before * (1 - drop) and cmd[m][i] > 1800:
                seg = range(i, i + h)
                if max(rpm[m][k] for k in seg) < before * 0.6 and min(cmd[m][k] for k in seg) > 1500:
                    out.append({"tag": "motor-loss", "arm_from": round(arm.t[i] - 0.5, 2), "arm_to": round(arm.t[i] + 1.5, 2),
                                "title": f"motor {m + 1} power loss",
                                "why": f"motor {m + 1} eRPM {before:.0f} -> {now:.0f} in {window * 1000:.0f} ms at full command, "
                                       f"held {hold} s, no impact",
                                "numbers": {"motor": m + 1, "erpm_before": round(before), "erpm_after": round(now), "arm_t": round(arm.t[i], 2)}})
                    last = arm.t[i]
    return out


def detect(arm: Arm) -> list[dict]:
    cr = crashes(arm)
    lp = loops(arm)
    excl = [(e["arm_from"] - 1, e["arm_to"] + 1) for e in cr] + [(e["arm_from"], e["arm_to"]) for e in lp]
    ev = cr + lp + flips_and_rolls(arm, excl) + dives(arm) + yaw_kicks(arm) + motor_loss(arm, [c["numbers"]["arm_t"] for c in cr])
    ev.sort(key=lambda e: e["arm_from"])
    return ev
