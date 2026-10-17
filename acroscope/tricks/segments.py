"""Segmenters: the handful of ways to cut an arm into candidate windows. A trick names one (with its options) and
judges each window it yields by the window's features. All six are the scans of the pre-#22 detectors, unchanged:

  rotation      stretches where |roll| or |pitch| rate is over a threshold (flips, rolls, split-S)
  sustained     one axis past a signed rate with short gaps allowed (powerloops: pitch < -40 dps)
  throttle_cut  throttle under a level for a while, blips allowed (dives)
  impact        a g spike with the gyro jolting right after it (crashes, landings)
  kick          a raw yaw spike with the yaw stick centred (gyro kicks)
  motor_drop    one motor's eRPM falling while its command is at full (motor loss)

Each window is a frame range [i0, i1) plus the nominal arm seconds it reports (a point event's window runs around
its instant, even past the arm's ends), the instant for point events, and what the scan itself measured."""
import math
from dataclasses import dataclass, field

from ..blackbox import Arm

# kind: {option: default}. A trick's segment may set any of these and no other name (a typo is an error at load).
KINDS = {
    "rotation": {"threshold": 100.0, "min_dur": 0.3, "gap": 0.3},
    "sustained": {"axis": "pitch", "rate": -40.0, "gap": 0.6},
    "throttle_cut": {"below": 5.0, "min_dur": 1.2, "blip": 0.3},
    "impact": {"g": 4.0, "jolt_dps": 500.0, "jolt_window": 0.05, "cooldown": 2.0, "before": 0.5, "after": 1.0},
    "kick": {"rate": 400.0, "ratio": 2.0, "stick": 6.0, "cooldown": 0.5, "before": 0.3, "after": 0.7},
    "motor_drop": {"drop": 0.5, "window": 0.05, "hold": 0.4, "cooldown": 2.0, "rpm_min": 1500.0, "command_full": 1800.0,
                   "command_hold": 1500.0, "hold_ratio": 0.6, "before": 0.5, "after": 1.5},
}
# what a kind's scan measures on its own, beyond the window's common features: name -> description
EXTRAS = {
    "rotation": {"axis": "the axis with the most rotation, 'roll' or 'pitch'",
                 "direction": "the sign of that rotation: 'right'/'left' for roll, 'forward'/'back' for pitch"},
    "sustained": {},
    "throttle_cut": {},
    "impact": {"arm_t": "the instant of the impact, arm seconds", "impact_g": "the acceleration at that instant, g",
               "jolt_dps": "the largest raw gyro rate within the jolt window after it, deg/s"},
    "kick": {"arm_t": "the instant of the kick, arm seconds", "raw_yaw_dps": "the raw yaw rate at it, deg/s (+ = nose left)"},
    "motor_drop": {"arm_t": "the instant of the drop, arm seconds", "motor": "the motor, 1-4 (Betaflight order)",
                   "erpm_before": "its eRPM a window before the drop", "erpm_after": "its eRPM at the drop"},
}
# the log fields a kind needs beyond gyroADC, rcCommand, accSmooth, imuQuaternion and vbatLatest
REQUIRES = {"impact": ["gyroUnfilt[0]"], "kick": ["gyroUnfilt[0]"], "motor_drop": ["eRPM[0]", "motor[0]"]}
AXES = ("roll", "pitch", "yaw")


@dataclass
class Window:
    i0: int                    # frames [i0, i1), clipped to the arm
    i1: int
    arm_from: float            # the window as reported, arm seconds (a point event's runs around its instant)
    arm_to: float
    point: int | None = None   # the frame of a point event
    extras: dict = field(default_factory=dict)


def _runs(arm: Arm, active, gap: float) -> list[tuple[int, int]]:
    """[(first, last)] frame runs where `active(i)`, merged when the gap between two is under `gap` s."""
    runs, cur = [], None
    t = arm.t
    for i in range(arm.n):
        if active(i):
            if cur and t[i] - t[cur[1]] < gap:
                cur[1] = i
            else:
                cur = [i, i]
                runs.append(cur)
    return [(a, b) for a, b in runs]


def _span(arm: Arm, a: int, b: int, **extras) -> Window:
    """A window over frames [a, b]."""
    return Window(a, b + 1, round(arm.t[a], 2), round(arm.t[b], 2), extras=extras)


def _around(arm: Arm, i: int, before: float, after: float, **extras) -> Window:
    """A point event's window: `before` s to `after` s around frame i, as reported; the frames are what the arm has."""
    t = arm.t[i]
    i0, i1 = arm.window(t - before, t + after)
    return Window(i0, max(i1, i + 1), round(t - before, 2), round(t + after, 2), point=i, extras={"arm_t": round(t, 2), **extras})


def rotation(arm: Arm, o: dict) -> list[Window]:
    g = [arm.col(f"gyroADC[{a}]") for a in range(2)]
    thr = o["threshold"]
    out = []
    for a, b in _runs(arm, lambda i: max(abs(g[0][i]), abs(g[1][i])) > thr, o["gap"]):
        if arm.t[b] - arm.t[a] >= o["min_dur"]:
            out.append(_span(arm, a, b))
    return out


def sustained(arm: Arm, o: dict) -> list[Window]:
    g = arm.col(f"gyroADC[{AXES.index(o['axis'])}]")
    rate = o["rate"]
    active = (lambda i: g[i] < rate) if rate < 0 else (lambda i: g[i] > rate)
    return [_span(arm, a, b) for a, b in _runs(arm, active, o["gap"])]


def throttle_cut(arm: Arm, o: dict) -> list[Window]:
    """Throttle under `below` % for at least `min_dur` s; a blip above it shorter than `blip` s does not end the cut,
    a longer one ends it where the blip began."""
    rc3 = arm.col("rcCommand[3]")
    level = 1000 + 10 * o["below"]
    out, start, up_since = [], None, None
    for i in range(arm.n + 1):
        cut = i < arm.n and rc3[i] < level
        if cut and start is None:
            start = i
        elif cut:
            up_since = None
        elif start is not None and i < arm.n and (up_since is None or arm.t[i] - arm.t[up_since] < o["blip"]):
            up_since = up_since if up_since is not None else i
        elif start is not None:
            end = up_since if up_since is not None else i
            up_since = None
            if arm.t[end - 1] - arm.t[start] >= o["min_dur"]:
                out.append(_span(arm, start, end - 1))
            start = None
    return out


def impact(arm: Arm, o: dict) -> list[Window]:
    """Acceleration over `g` with the raw gyro jolting over `jolt_dps` within `jolt_window` s: contact. One per
    `cooldown` s."""
    acc = [arm.col(f"accSmooth[{a}]") for a in range(3)]
    raw = [arm.col(f"gyroUnfilt[{a}]") for a in range(3)]
    w = max(1, int(arm.rate * o["jolt_window"]))
    out, last = [], -1e9
    for i in range(arm.n):
        if arm.t[i] - last < o["cooldown"]:
            continue
        a = math.sqrt(sum((c[i] / 2048) ** 2 for c in acc))
        if a > o["g"]:
            j = max(abs(raw[k][x]) for k in range(3) for x in range(i, min(arm.n, i + w)))
            if j > o["jolt_dps"]:
                out.append(_around(arm, i, o["before"], o["after"], impact_g=round(a, 1), jolt_dps=round(j)))
                last = arm.t[i]
    return out


def kick(arm: Arm, o: dict) -> list[Window]:
    """|raw yaw| over `rate`, more than `ratio` x the raw roll and pitch, with the yaw stick within `stick` % of
    its travel (6 % = 30 of rcCommand's 500: centred)."""
    raw = [arm.col(f"gyroUnfilt[{a}]") for a in range(3)]
    rc2 = arm.col("rcCommand[2]")
    stick = 5 * o["stick"]
    out, last = [], -1e9
    for i in range(arm.n):
        y = abs(raw[2][i])
        if y > o["rate"] and y > o["ratio"] * max(abs(raw[0][i]), abs(raw[1][i])) and abs(rc2[i]) < stick and arm.t[i] - last > o["cooldown"]:
            out.append(_around(arm, i, o["before"], o["after"], raw_yaw_dps=round(raw[2][i])))
            last = arm.t[i]
    return out


def motor_drop(arm: Arm, o: dict) -> list[Window]:
    """One motor's eRPM falls by `drop` (a fraction) within `window` s while its command is over `command_full`,
    and stays under `hold_ratio` of what it was for `hold` s with the command over `command_hold`: the motor,
    not the pilot, let go. The PID cuts the other motors to compensate, so they drop too; the tell is the one at
    full command with low RPM."""
    rpm = [arm.col(f"eRPM[{m}]") for m in range(4)]
    cmd = [arm.col(f"motor[{m}]") for m in range(4)]
    w, h = max(1, int(arm.rate * o["window"])), max(1, int(arm.rate * o["hold"]))
    out, last = [], -1e9
    for i in range(w, arm.n - h):
        if arm.t[i] - last < o["cooldown"]:
            continue
        for m in range(4):
            before, now = rpm[m][i - w], rpm[m][i]
            if before > o["rpm_min"] and now < before * (1 - o["drop"]) and cmd[m][i] > o["command_full"]:
                seg = range(i, i + h)
                if max(rpm[m][k] for k in seg) < before * o["hold_ratio"] and min(cmd[m][k] for k in seg) > o["command_hold"]:
                    out.append(_around(arm, i, o["before"], o["after"], motor=m + 1, erpm_before=round(before), erpm_after=round(now)))
                    last = arm.t[i]
    return out


SCANS = {"rotation": rotation, "sustained": sustained, "throttle_cut": throttle_cut, "impact": impact, "kick": kick,
         "motor_drop": motor_drop}


def windows(arm: Arm, kind: str, opts: dict) -> list[Window]:
    return SCANS[kind](arm, opts)
