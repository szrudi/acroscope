"""One-off: move the hand-made matches and moments of Oct 2026 into session files.
Sources: video-blackbox-index.md (matches, notes) and videos/<session>/moments.xspf (moments).
Run once from the acroscope venv: python scripts/migrate-index.py
"""
from acroscope import sessions, xspf
from acroscope.config import TAGS

S07 = "2026-10-07-schammer"
S04 = "2026-10-04-motor-jitter-testing"
S05 = "2026-10-05-motor-jitter-testing"
B07 = "20261007_212530"

MATCHES = [  # session, video, bbl fragment, arm, offset, note
    (S07, "003", B07, 1, 86.5, "flash erase + reboot at video 0:20-0:52, then this flight; OSD vbat vs log: 20 s 3.82/3.84, 58 s 3.69/3.67"),
    (S07, "004", B07, 5, 1.0, ""),
    (S07, "005", B07, 7, 12.0, ""),
    (S07, "005", B07, 8, 36.5, ""),
    (S07, "006", B07, 9, 14.8, ""),
    (S07, "007", B07, 13, 53.2, "arms 10-12 (2.5/8.2/5.2 s) are the short arms before it, offsets not read"),
    (S07, "008", B07, 14, 4.5, "OSD at the start; ~3.4-3.5 at the crash (detector: impact at arm 12.54 s, video ~0:16)"),
    (S07, "008", B07, 15, 20.2, "armed, motors not spinning (search for the quad)"),
    (S07, "008", B07, 17, 64.2, "armed, motors not spinning"),
    (S07, "009", B07, 18, 2.8, "OSD gives ~1.7 around 1:33 (the powerloop)"),
    (S07, "010", B07, 19, 21.0, "OSD gives ~20.5 around 1:41 (split-S)"),
    (S07, "011", B07, 20, 13.5, "log ends at 72.0 s: flash full"),
    (S04, "009", "20261004_160419", 4, 26.7, "jolt at video 1:07 = arm ~40.3 s"),
    (S05, "001", "20261005_171233", 4, 79.0, "OSD"),
    (S05, "003", "20261005_174301", 1, 24.0, "OSD"),
]

VIDEO_NOTES = {
    (S07, "001"): "before the flash erase, no log",
    (S07, "002"): "before the flash erase, no log",
    (S07, "012"): "flash full, no log. Flown in HOR (horizon) by accident, right after the fall",
    (S07, "013"): "flash full, no log. HOR by accident",
    (S07, "014"): "flash full, no log. Back to AIR (acro) at 0:40",
    (S07, "015"): "flash full, no log",
    (S07, "016"): "flash full, no log",
    (S07, "017"): "flash full, no log",
    (S07, "018"): "flash full, no log",
    (S07, "019"): "flash full, no log",
    (S04, "002"): "log 20261004_132241 arms 1-3: crash into a metal beam at 1:11 (end of arm 2, OSD total 00:47); first gyro yaw kicks in arm 3 (1:21); hair stall at 2:33. Offsets not read",
}

SESSION_NOTES = {
    S07: "Blackbox ~400 Hz. Flash filled during arm 20, so 012-019 have no log. The flash erase is in 003 (OSD Blackbox menu -> ERASING FLASH -> save/reboot), so 001-002 were flown before it. Arms 2, 3 and 4 (two batteries) have no video: the lost DVR clips (stopping REC by hand disables Auto&Follow).",
    S04: "Motor-jitter testing after the 10-04 hair stall and metal-beam crash.",
    S05: "Indoor gyro yaw-kick tests; motor 1 swapped for a new one before 003 (log 174301).",
}

# moment title fragment -> tags, plus a note from the index where there is one
MOMENT_TAGS = [("powerloop", ["powerloop"]), ("roll flip", ["roll"]), ("rolls", ["roll"]), ("roll", ["roll"]), ("flips", ["flip"]),
               ("flip", ["flip"]), ("split-s", ["split-s"]), ("dive", ["dive"]), ("orbit", ["orbit"]), ("circle", ["orbit"]),
               ("crash", ["crash"]), ("kick", ["gyro-kick"]), ("jolt", ["gyro-kick"]), ("motor 1 power loss", ["motor-loss", "crash"]),
               ("stall", ["crash"]), ("trees", ["poi"]), ("searching", ["poi"]), ("arrives", ["poi"]), ("wave", ["poi"]),
               ("high fly", ["poi"]), ("flying high", ["dive", "poi"])]
MOMENT_NOTES = {
    "roll flips right + left": "right at 1:32.7 (364 deg, 0.97 s, peak 717 dps, full stick), left at 1:34.5 (371 deg, 0.94 s, 526 dps). Both clean, throttle ~40-50 %",
    "pitch flips forward + back x2": "forward at 1:40.6 (~400 deg, peak 562 dps, ~40 deg over-rotation corrected at 1:42), back at 1:44.6 (peak 410 dps), left turn 1:46-1:49, back again at 1:51.6 (exactly 360 deg, 1.2 s, ~320 dps). Throttle 35-50 %",
    "orbit right around tree": "nose-in, moving right (roll right + yaw left), arm 57-68 s. Tree in view 0:59-1:06.5, ~2 laps by IMU. Steadiest 1:03.5-1:06.5 (yaw ~18 %, roll ~25-35 %, pitch ~25-30 %, throttle ~74 %). Lean ~90 deg at 1:05. Tree lost at 1:07, inputs flip",
    "flying high, horizontal dive, overshoot at the stop": "high 0:47-1:13, then roughly level at 0 % throttle 1:11-1:13 dropping low over the grass; left turn at 0 % 1:13-1:14. Stop 1:14.3-1:15.1: throttle 80-100 % + pitch back -44 %, good brake. Overshoot 1:15.1-1:15.9: forward pitch +32 % with throttle ~20 % tips the nose down at the path",
    "orbit left around tree": "nose-in, moving left (roll left + yaw right), arm 12.2-22.2 s. Orbit proper 0:30-0:36, ~1.75 laps. Steadiest 0:32-0:34 (yaw ~21 %, roll ~30-37 %, pitch ~20 %). Yaw drops to ~4 % at 0:35 while roll stays 46 %: tree slides to the frame edge. Lost at 0:36, big yaw back -53 % at 0:37",
    "powerloop 1": "arm 27.0-31.7 s: -383 deg pitch in 4.5 s, peak 169 dps, throttle inverted 44 %",
    "powerloop 2": "arm 39.8-43.9 s: -384 deg pitch in 3.7 s, peak 218 dps, throttle inverted 51 %",
    "flips and rolls": "back flip at 1:39.4 (~396 deg, 0.87 s, peak 632-667 dps, full stick, throttle 25-30 %), roll right at 1:43.0 (~330 deg, 0.89 s, 550 dps, throttle 47 %), at 1:45.5 a half roll right (~185 deg) with ~220 deg of yaw (throttle 45 %): meant as a roll around the direction of travel, but yaw stick ~110-120 % of roll where ~40-45 % fits the ~23 deg nose-to-travel angle, so the nose swept ~100 deg off instead of circling it",
    "failed back flip, crash": "low along the path, throttle cut to ~21-25 % at ~0:14.8, then a hard pitch-back flip (stick -88 %, peak 536 dps, throttle 44-55 %), ~300 deg in ~1 s, ground at ~0:16. A flip, not a powerloop: fast rotation without an arc, from too low with no pop-up first",
    "crashing into a branch": "impacts at arm 64.7 and 66.7 s (4.4 g / 4.9 g)",
    "powerloop, fast, throttle up inverted": "setup turn 1:30-1:32, loop ~1:33-1:35.5, inverted ~1:34.5 = arm 91.1-94.3 s. 397 deg in 3.2 s, peak 352 dps (~2x the 007 loops). Throttle ~90 % while inverted (007 loops eased to 44-51 %). ~24 deg roll drift in the first half. vbat 3.10 V (LOW BATTERY). Video breaks up inverted",
    "dive from high up, late throttle catch": "tip-over 1:03-1:05 (pitch ~15 %, ~60 deg forward). Throttle 0 % from 1:04.9 to ~1:09.5. Free fall 1:05-1:06.7. Pull-out 1:06.8-1:08.5 (pitch back ~30 %, ~115 deg) still at 0 % throttle, then ~145 deg left yaw. Throttle back at 1:09.7, 100 % at 1:10.5, low over the grass. vbat 3.78 -> 3.29 V on the punch",
    "roll flip": "left, ~400 deg in 0.9 s, peak 640-700 dps (roll stick ~97 %), throttle ~35 % during, 80 % after; ~40 deg over-rotation corrected with roll right",
    "split-S 1 (turns into diving turn)": "climbing left turn setup 1:38-1:41, split-S 1:41.2-1:42.5 (arm ~81-82). Roll left ~250 deg (70 past inverted) at 0 % throttle, then a pull of only ~80 deg at 47-60 % throttle: a diving turn. LOW BATTERY, vbat 3.2-3.4 V",
    "split-S 2 (exits low)": "roll left ~210 deg at 27-36 % throttle, pull ~120 deg with throttle rising to 91 %, 2.7 g at the bottom, exits very low over the grass",
    "rolls right + left (with yaw)": "axis-roll attempts: right at 0:42.3 (roll ~226 + yaw ~192 deg, ~85 % yaw, 0.93 s, peak 315 dps, throttle ~50 %), finished with a quick ~77 deg roll at 0:43.5 at 73-87 % throttle. Left at 0:52.2 (roll ~222 + yaw ~240, ~108 % yaw, 1.06 s, throttle ~60 %). Still far more yaw than the ~40 % the nose-to-travel angle needs",
    "motor 1 power loss, fall": "motor 1 (position 1, the repaired original motor) loses RPM 2440 -> ~800 within 30 ms at arm 70.37 s while its command goes to 2047; stays down ~1 s; ground at 71.4 s. LOW BATTERY 3.30 V. No failsafe. Log ends at 72.0 s (flash full)",
    "longer orbits around a tree": "tree in frame almost continuously 0:53-1:09 with close passes at ~0:58, 1:04 and 1:09; lost 1:11-1:12, wider orbit 1:13-1:22 with the tree small and left of centre (no log)",
    "clean dive, hard stop": "hard stop right after pitching back to level (no log)",
    "crash into metal beam (gyro fault starts)": "end of arm 2 of log 132241 (OSD total 00:47). Every arm after this has 10-30 yaw kicks/min; arms 1-2 had 0",
    "pitch jolt": "log 160419 arm 4, t~40.3 s: D term -1259, motors pinned 48/2047, vbat sag 3.48 V; raw gyro ramps to +-1200 dps in ~3 ms then rings ~190 Hz for ~20 ms, acc jumps ~5 g",
    "gyro yaw kick (indoor)": "arm 3.2 s of log 171233 arm 4",
    "gyro yaw kicks x2 (indoor)": "arm 27.5 / 28.5 s: raw yaw rings +-1900 dps at ~210 Hz for ~10 ms, acc calm, no eRPM dip: the FC/gyro, not a prop strike",
    "gyro yaw kicks after motor swap (7 in 16 s)": "arm 4.1, 6.9, 12.6, 14.5, 17.1, 19.3, 19.8 s of log 174301 arm 1; sharp leftward spikes, several against a right-yaw stick",
}


def main():
    for s in (S04, S05, S07):
        sessions.refresh(s, probe_videos=False)
        d = sessions.load(s)
        d["note"] = SESSION_NOTES.get(s, d.get("note", ""))
        for v in d["videos"]:
            n = VIDEO_NOTES.get((s, v["file"][-7:-4]))
            if n:
                v["note"] = n
        sessions.save(d)
    for s, vid, bbl, arm, off, note in MATCHES:
        sessions.set_match(s, vid, bbl, arm, off, note)
    for s in (S04, S05, S07):
        added = xspf.import_session(s)
        d = sessions.load(s)
        for m in d["moments"]:
            low = m["title"].lower()
            if not m["tags"]:
                for frag, tags in MOMENT_TAGS:
                    if frag in low:
                        m["tags"] = tags
                        break
                else:
                    m["tags"] = ["poi"]
            if not m.get("note") and m["title"] in MOMENT_NOTES:
                m["note"] = MOMENT_NOTES[m["title"]]
        sessions.save(d)
        print(f"{s}: {len(added)} moments imported, {len(d['moments'])} total, {len(d['matches'])} matches")


if __name__ == "__main__":
    main()
