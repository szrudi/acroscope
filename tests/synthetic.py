"""A made-up arm for the trick tests: the gyro rates, throttle and sticks you script, the attitude integrated from
the rates (so a 360 roll really passes through inverted), gravity in the body frame as the accelerometer, and
the log's units (gyro deg/s, rcCommand 1000-2000 / +-500, accSmooth /2048 = g, imuQuaternion * 32767, vbat 0.01 V).

Betaflight's body frame is X forward, Y left, Z up, and its gyro signs are right-handed rotations in it: roll + =
right side down, pitch + = nose down, yaw + = nose left."""
import array
import math

from acroscope.blackbox import Arm

RATE = 400


def _qmul(a, b):
    ax, ay, az, aw = a
    bx, by, bz, bw = b
    return (aw * bx + ax * bw + ay * bz - az * by,
            aw * by - ax * bz + ay * bw + az * bx,
            aw * bz + ax * by - ay * bx + az * bw,
            aw * bw - ax * bx - ay * by - az * bz)


def quat_from_lean(forward_deg: float = 0.0, right_deg: float = 0.0) -> tuple:
    """An attitude from a nose-down angle and a right-bank angle (the IMU's lean, acroscope's signs)."""
    q = (0.0, 0.0, 0.0, 1.0)
    for axis, deg in ((1, forward_deg), (0, right_deg)):
        h = math.radians(deg) / 2
        d = [0.0, 0.0, 0.0, math.cos(h)]
        d[axis] = math.sin(h)
        q = _qmul(q, tuple(d))
    return q


def make_arm(seconds: float, gyro=None, throttle=50.0, sticks=None, raw=None, acc=None, quat0=None, motors=None,
             erpm=None, setpoint=False, vbat=3.8, rate: int = RATE, craft: str = "TEST", rates=None) -> Arm:
    """`gyro(t)` -> (roll, pitch, yaw) deg/s; `throttle` a number (%) or `throttle(t)`; `sticks(t)` -> (roll, pitch,
    yaw) % (default: what asks for the gyro's rate on a 670 deg/s profile, so a scripted trick is a trick the
    pilot flew); `raw(t)` the unfiltered gyro (default: the gyro); `acc(t)` -> (x, y, z) g in the body frame (default:
    gravity, from the attitude); `motors(t)` -> four commands 48-2047; `erpm(t)` -> four eRPMs; `quat0` the
    attitude at the start (`quat_from_lean`)."""
    n = int(seconds * rate) + 1
    t = array.array("d", (i / rate for i in range(n)))
    gyro = gyro or (lambda tt: (0.0, 0.0, 0.0))
    thr = throttle if callable(throttle) else (lambda tt: throttle)
    sticks = sticks or (lambda tt: tuple(max(-100.0, min(100.0, v / 670 * 100)) for v in gyro(tt)))
    raw = raw or gyro
    q = quat0 or (0.0, 0.0, 0.0, 1.0)
    cols = {}
    names = [f"gyroADC[{a}]" for a in range(3)] + [f"gyroUnfilt[{a}]" for a in range(3)] + [f"rcCommand[{a}]" for a in range(4)] \
        + [f"accSmooth[{a}]" for a in range(3)] + [f"imuQuaternion[{a}]" for a in range(3)] + ["vbatLatest"]
    if motors:
        names += [f"motor[{m}]" for m in range(4)]
    if erpm:
        names += [f"eRPM[{m}]" for m in range(4)]
    if setpoint:
        names += [f"setpoint[{a}]" for a in range(4)]
    for name in names:
        cols[name] = array.array("f", bytes(4 * n))
    for i in range(n):
        tt = t[i]
        g = gyro(tt)
        if i:
            dt = t[i] - t[i - 1]
            w = [math.radians(v) * dt for v in g]
            ang = math.sqrt(sum(v * v for v in w))
            if ang > 0:
                s = math.sin(ang / 2) / ang
                q = _qmul(q, (w[0] * s, w[1] * s, w[2] * s, math.cos(ang / 2)))
                norm = math.sqrt(sum(v * v for v in q))
                q = tuple(v / norm for v in q)
        x, y, z, w_ = q
        if w_ < 0:                                   # the log stores x, y, z and acroscope takes w >= 0
            x, y, z, w_ = -x, -y, -z, -w_
        up = (2 * (x * z - w_ * y), 2 * (y * z + w_ * x), 1 - 2 * (x * x + y * y))
        a = acc(tt) if acc else up
        r = raw(tt)
        st = sticks(tt)
        for k in range(3):
            cols[f"gyroADC[{k}]"][i] = g[k]
            cols[f"gyroUnfilt[{k}]"][i] = r[k]
            cols[f"rcCommand[{k}]"][i] = st[k] * 5
            cols[f"accSmooth[{k}]"][i] = a[k] * 2048
            cols[f"imuQuaternion[{k}]"][i] = (x, y, z)[k] * 32767
        cols["rcCommand[3]"][i] = 1000 + 10 * thr(tt)
        cols["vbatLatest"][i] = vbat * 100
        if motors:
            for m, v in enumerate(motors(tt)):
                cols[f"motor[{m}]"][i] = v
        if erpm:
            for m, v in enumerate(erpm(tt)):
                cols[f"eRPM[{m}]"][i] = v
        if setpoint:
            for k in range(3):
                cols[f"setpoint[{k}]"][i] = g[k]
            cols["setpoint[3]"][i] = thr(tt) * 10
    meta = {"bbl": "SYNTH", "file": "SYNTH.bbl", "index": 1, "frames": n, "length": round(t[-1], 3), "rate": float(rate),
            "fields": ["time", *cols], "headers": {"Craft name": craft},
            "rates": rates if rates is not None else {"type": "actual", "max_dps": [670.0] * 3, "centre_dps": [70.0] * 3}}
    return Arm(meta, t, cols)


def pulse(t0: float, t1: float, value: float):
    """A rate (or anything) of `value` between t0 and t1, 0 outside: the building block of a scripted trick."""
    return lambda t: value if t0 <= t < t1 else 0.0


def combine(*parts):
    """Several (roll, pitch, yaw) functions added up."""
    return lambda t: tuple(sum(p(t)[k] for p in parts) for k in range(3))


def axis(k: int, f):
    """A scalar function of t put on one axis of (roll, pitch, yaw)."""
    return lambda t: tuple(f(t) if a == k else 0.0 for a in range(3))
