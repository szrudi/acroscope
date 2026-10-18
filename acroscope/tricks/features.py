"""What a window measures, as the names a trick's conditions and texts use: one vocabulary, the same as
`metrics.summary` (what the agent reads for any stretch of an arm), plus what the segmenter's scan found, plus the
arm's own facts (its rate profile, its craft). Flat, dotted: `rotation_deg.roll`, `throttle_pct.min`.

`numbers` on an event is the same measurement nested, so an accepted suggestion stores the same snapshot the
`metrics` command gives for that window."""
from .. import metrics
from ..blackbox import Arm
from . import segments

AXES = ("roll", "pitch", "yaw")

# the common features of any window (metrics.summary's keys); the yaw peak and the setpoint peaks need their columns
WINDOW = {
    "arm_from": "the window's start, arm seconds", "arm_to": "its end, arm seconds",
    "duration": "its length, s", "frames": "log frames in it",
    "rotation_deg.roll": "integrated rotation, deg: + = right", "rotation_deg.pitch": "+ = nose down; a back flip is negative",
    "rotation_deg.yaw": "+ = nose left",
    "peak_rate_dps.roll": "peak |gyro| rate, deg/s", "peak_rate_dps.pitch": "", "peak_rate_dps.yaw": "",
    "stick_pct_max.roll": "peak |stick|, % of travel", "stick_pct_max.pitch": "", "stick_pct_max.yaw": "",
    "stick_pct_mean.roll": "mean stick (signed), %", "stick_pct_mean.pitch": "", "stick_pct_mean.yaw": "",
    "throttle_pct.min": "throttle, %", "throttle_pct.mean": "", "throttle_pct.max": "",
    "tilt_deg.min": "angle between the quad's up and world up: 0 level, 90 knife edge, 180 inverted", "tilt_deg.max": "",
    "tilt_deg.mean": "", "tilt_deg.start": "", "tilt_deg.end": "",
    "acc_g.min": "total acceleration, g", "acc_g.max": "",
    "vbat.min": "battery, V", "vbat.max": "",
    "throttle_inverted_pct.mean": "throttle while inverted (tilt > 135); none when never inverted",
    "throttle_inverted_pct.min": "", "throttle_inverted_pct.frames": "",
    "motor_mean": "mean motor command, 48-2047; none without motor fields", "motor_max": "",
    "motor_min": "the lowest motor command: 0 = a motor stopped (crash flip mode, a disarm), the quad was not flying",
    "setpoint_peak_dps.roll": "peak |setpoint|: the rate the sticks asked for, deg/s; none when not logged",
    "setpoint_peak_dps.pitch": "", "setpoint_peak_dps.yaw": "",
    "crash_flip": "the share of the window with the crash-flip (turtle) switch on; none without flightModeFlags. No scan cuts a window there",
    "to_arm_end_s": "seconds from the window's end (a point event: from its instant) to the arm's end",
}
ARM = {
    "arm.length": "the arm's length, s", "arm.rate": "log rate, frames/s", "arm.craft": "the craft name from the header",
    "arm.rates_type": "'betaflight', 'raceflight', 'kiss', 'actual' or 'quick'; none when the log did not say",
    "arm.rates_max_dps.roll": "the rate profile's full-stick rate, deg/s", "arm.rates_max_dps.pitch": "", "arm.rates_max_dps.yaw": "",
    "arm.rates_centre_dps.roll": "its rate at stick centre per full stick, deg/s", "arm.rates_centre_dps.pitch": "",
    "arm.rates_centre_dps.yaw": "",
}


def vocabulary(kind: str) -> dict[str, str]:
    """Every name a trick on this segment kind may use, with what it means (opt.* are the segment's options)."""
    return {**WINDOW, **segments.EXTRAS[kind], **ARM, **{f"opt.{k}": f"the segment's {k} option" for k in segments.KINDS[kind]}}


def flatten_numbers(numbers: dict) -> dict:
    return flatten({k: v for k, v in numbers.items() if k not in ("rates", "phases")})


def phase_names(kind: str) -> set[str]:
    """The features of a phase's window the next phases see as p<k>.<name>: the window's and the scan's own."""
    return set(WINDOW) | set(segments.EXTRAS[kind])


def flatten(d: dict, prefix: str = "") -> dict:
    out = {}
    for k, v in d.items():
        name = f"{prefix}{k}"
        if isinstance(v, dict):
            out.update(flatten(v, name + "."))
        elif not isinstance(v, list):
            out[name] = v
    return out


def arm_context(arm: Arm) -> dict:
    h = arm.meta.get("headers") or {}
    rates = arm.meta.get("rates")
    out = {"arm.length": arm.length, "arm.rate": arm.rate, "arm.craft": h.get("Craft name"), "arm.rates_type": rates["type"] if rates else None}
    for a, ax in enumerate(AXES):
        out[f"arm.rates_max_dps.{ax}"] = rates["max_dps"][a] if rates else None
        out[f"arm.rates_centre_dps.{ax}"] = rates["centre_dps"][a] if rates else None
    return out


def measure(arm: Arm, win: segments.Window, kind: str, opts: dict) -> tuple[dict, dict]:
    """(the flat namespace a condition sees, the nested `numbers` an event carries) for one window."""
    numbers = metrics.summary(arm, arm.t[win.i0], arm.t[win.i1 - 1] + 1e-6) if win.i1 > win.i0 else {}
    numbers["arm_from"], numbers["arm_to"] = win.arm_from, win.arm_to
    numbers.update(win.extras)
    if kind == "rotation":
        rot = numbers["rotation_peak_deg"]              # the furthest it got, not the net: a counter-correction cancels the net
        main = "roll" if abs(rot["roll"]) >= abs(rot["pitch"]) else "pitch"
        numbers["axis"] = main
        numbers["direction"] = ("right" if rot["roll"] > 0 else "left") if main == "roll" else ("forward" if rot["pitch"] > 0 else "back")
    rates = arm.meta.get("rates")
    if rates:
        numbers["rates"] = rates
    flat = {name: None for name in WINDOW}
    flat.update(flatten_numbers(numbers))
    end = win.extras["arm_t"] if win.point is not None else win.arm_to
    flat["to_arm_end_s"] = round(arm.length - end, 2)
    flat.update({f"opt.{k}": v for k, v in opts.items()})
    return flat, numbers
