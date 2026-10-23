"""A made-up MAX7456 font and a clip with a Betaflight OSD drawn in it, for the OSD profile tests (#10): no
Configurator font (they are GPL-3.0, fetched at run time, never in the repo) and no real footage needed.

The font draws a 5 x 7 bitmap per character, each bitmap pixel 2 x 2 font pixels, white with a black outline, the
rest transparent: the shape of a Betaflight font. `wide=True` makes a second, different-looking font (each bitmap
pixel 2 font pixels wide and the strokes doubled), for the font ranking. A clip is frames of a grey gradient with
the OSD text drawn on Betaflight's character grid (`grid` as osdprofile has it), encoded with ffmpeg."""
import random
import subprocess
from pathlib import Path

GLYPHS = {
    "0": ["01110", "10001", "10011", "10101", "11001", "10001", "01110"],
    "1": ["00100", "01100", "00100", "00100", "00100", "00100", "01110"],
    "2": ["01110", "10001", "00001", "00010", "00100", "01000", "11111"],
    "3": ["11110", "00001", "00001", "01110", "00001", "00001", "11110"],
    "4": ["00010", "00110", "01010", "10010", "11111", "00010", "00010"],
    "5": ["11111", "10000", "11110", "00001", "00001", "10001", "01110"],
    "6": ["00110", "01000", "10000", "11110", "10001", "10001", "01110"],
    "7": ["11111", "00001", "00010", "00100", "01000", "01000", "01000"],
    "8": ["01110", "10001", "10001", "01110", "10001", "10001", "01110"],
    "9": ["01110", "10001", "10001", "01111", "00001", "00010", "01100"],
    ":": ["00000", "00100", "00100", "00000", "00100", "00100", "00000"],
    ".": ["00000", "00000", "00000", "00000", "00000", "01100", "01100"],
    "#": ["01010", "01010", "11111", "01010", "11111", "01010", "01010"],
    "%": ["11001", "11010", "00010", "00100", "01000", "01011", "10011"],
    "-": ["00000", "00000", "00000", "11111", "00000", "00000", "00000"],
    "A": ["01110", "10001", "10001", "11111", "10001", "10001", "10001"],
    "B": ["11110", "10001", "10001", "11110", "10001", "10001", "11110"],
    "C": ["01110", "10001", "10000", "10000", "10000", "10001", "01110"],
    "D": ["11110", "10001", "10001", "10001", "10001", "10001", "11110"],
    "E": ["11111", "10000", "10000", "11110", "10000", "10000", "11111"],
    "F": ["11111", "10000", "10000", "11110", "10000", "10000", "10000"],
    "G": ["01110", "10001", "10000", "10111", "10001", "10001", "01111"],
    "H": ["10001", "10001", "10001", "11111", "10001", "10001", "10001"],
    "I": ["01110", "00100", "00100", "00100", "00100", "00100", "01110"],
    "K": ["10001", "10010", "10100", "11000", "10100", "10010", "10001"],
    "L": ["10000", "10000", "10000", "10000", "10000", "10000", "11111"],
    "M": ["10001", "11011", "10101", "10101", "10001", "10001", "10001"],
    "N": ["10001", "11001", "10101", "10011", "10001", "10001", "10001"],
    "O": ["01110", "10001", "10001", "10001", "10001", "10001", "01110"],
    "R": ["11110", "10001", "10001", "11110", "10100", "10010", "10001"],
    "S": ["01111", "10000", "10000", "01110", "00001", "00001", "11110"],
    "T": ["11111", "00100", "00100", "00100", "00100", "00100", "00100"],
    "X": ["10001", "10001", "01010", "00100", "01010", "10001", "10001"],
    "Y": ["10001", "10001", "01010", "00100", "00100", "00100", "00100"],
    "\x9c": ["11111", "10001", "10101", "10101", "10101", "10001", "11111"],    # SYM_FLY_M, the timers' symbol
}


def font_chars(wide: bool = False, narrow: bool = False) -> list[list[str]]:
    """256 characters of 18 rows x 12 pixel codes, like betaflight.parse_mcm returns. `narrow`: one font pixel per
    bitmap column from column 3, ink in columns 2-8 with the outline, as the real fonts sit in their cells (the
    default fills the cell, so the glyph box starts at 0)."""
    chars = []
    for code in range(256):
        g = GLYPHS.get(chr(code))
        px = [["01"] * 12 for _ in range(18)]
        if g:
            ink = set()
            for r, row in enumerate(g):
                for c, b in enumerate(row):
                    if b == "1":
                        for dy in (0, 1):
                            if narrow:
                                ink.add((2 + 2 * r + dy, 3 + c))
                                continue
                            for dx in ((0, 1, 2) if wide else (0, 1)):
                                ink.add((2 + 2 * r + dy, 1 + 2 * c + dx - (1 if wide else 0)))
            ink = {(y, x) for y, x in ink if 0 <= x < 12 and 0 <= y < 18}
            for y, x in ink:
                for oy in (-1, 0, 1):
                    for ox in (-1, 0, 1):
                        if 0 <= y + oy < 18 and 0 <= x + ox < 12 and (y + oy, x + ox) not in ink:
                            px[y + oy][x + ox] = "00"
            for y, x in ink:
                px[y][x] = "10"
        chars.append(px)
    return chars


def mcm_text(chars: list[list[str]]) -> str:
    """The .mcm file of a font: MAX7456, then 64 lines of 8 bits per character (54 of pixels, 10 of padding)."""
    lines = ["MAX7456"]
    for ch in chars:
        px = [p for row in ch for p in row]
        lines += ["".join(px[i:i + 4]) for i in range(0, len(px), 4)]
        lines += ["01010101"] * 10
    return "\n".join(lines) + "\n"


_BG: dict = {}


def background(w: int = 720, h: int = 480, seed: int = 1) -> bytearray:
    """A grey picture: a gradient with a little noise, like a camera's (a fresh copy each time)."""
    if (w, h, seed) not in _BG:
        rnd = random.Random(seed)
        _BG[w, h, seed] = bytes(max(0, min(255, 70 + (x * 80) // w + (y * 60) // h + rnd.randint(-12, 12))) for y in range(h) for x in range(w))
    return bytearray(_BG[w, h, seed])


def draw(buf: bytearray, chars, grid: dict, col: int, row: int, text: str, w: int = 720) -> None:
    """`text` from (col, row) on the grid, scaled from font pixels to the cell (nearest), transparent left alone."""
    cw, ch = grid["cw"], grid["ch"]
    h = len(buf) // w
    for k, c in enumerate(text):
        x0, y0 = grid["x"] + cw * (col + k), grid["y"] + ch * row
        for j in range(ch):
            fy = min(17, j * 18 // ch)
            y = y0 + j
            if not 0 <= y < h:
                continue
            for i in range(cw):
                x = x0 + i
                p = chars[ord(c)][fy][min(11, i * 12 // cw)]
                if 0 <= x < w and p in ("00", "10"):
                    buf[y * w + x] = 255 if p == "10" else 16


def mmss(t: float, fmt: str = "dd:dd") -> str:
    """A timer's text as Betaflight prints it: MM:SS, MM:SS.h, MM:SS.hh."""
    m, s = int(t // 60), int(t % 60)
    out = f"{m:02d}:{s:02d}"
    if fmt == "dd:dd.d":
        out += f".{int(t * 10) % 10}"
    elif fmt == "dd:dd.dd":
        out += f".{int(t * 100) % 100:02d}"
    return out


# The OSD of the synthetic quad (the v3 layout Rudi flies): timer 2 the total armed time on row 11 from column 22,
# timer 1 the arm timer with tenths on row 12 from column 20, the flight counter on row 0 from column 24.
DUMP = """# version
# Betaflight / STM32G47X (G473) 2026.6.0-alpha May 15 2026 / 06:14:55 (e92c10887) MSP API: 1.48
# master
set osd_tim1 = 2594
set osd_tim_1_pos = 2452
set osd_tim_2_pos = 2422
set osd_total_flights_pos = 2072
set osd_vbat_pos = 2433
set osd_displayport_device = MAX7456
set vcd_video_system = NTSC
set craft_name = SYNTH
set pilot_name = nobody
profile 0
set osd_tim_1_pos = 1
"""


def osd_frame(chars, grid: dict, t: float, arm_start: float = 4.0, arm_end: float = 24.0, total0: float = 30.0,
              flights: int = 7, stats_from: float | None = 25.0, seed: int = 1, after_counter: str = "", stats=None) -> bytearray:
    """The frame at video time t: disarmed before arm_start (the arm timer at 0, the total at total0), armed until
    arm_end (both count), then the STATS screen from stats_from: `stats` [(row, label, value)] as Betaflight writes
    them (label at column 2, ':' at 20, value at 22), by default TOTAL ARM on row 5, MIN BATTERY on 6, BLACKBOX on 8."""
    buf = background(seed=seed)
    armed = min(max(t - arm_start, 0.0), arm_end - arm_start)
    if stats_from is not None and t >= stats_from:
        draw(buf, chars, grid, 15 - 6, 3, "--- STATS ---")
        for row, label, value in stats or [(5, "TOTAL ARM", mmss(total0 + armed)), (6, "MIN BATTERY", "3.41"), (8, "BLACKBOX", "42%")]:
            draw(buf, chars, grid, 2, row, label)
            draw(buf, chars, grid, 20, row, ":")
            draw(buf, chars, grid, 22, row, value)
        return buf
    draw(buf, chars, grid, 22, 11, "\x9c" + mmss(total0 + armed))
    draw(buf, chars, grid, 20, 12, "\x9c" + mmss(armed, "dd:dd.d"))
    draw(buf, chars, grid, 24, 0, f"#{flights}{after_counter}")
    return buf


def make_clip(path: Path, chars, grid: dict, seconds: float = 28.0, fps: int = 10, size: str | None = None, **kw) -> Path:
    """An mp4 of osd_frame over `seconds`, 720 x 480 (or scaled to `size`, a DVR that records smaller), H.264 at a
    high quality (the OSD survives as on a DVR)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    p = subprocess.Popen(["ffmpeg", "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "gray", "-s", "720x480", "-r", str(fps), "-i", "-",
                          *(["-vf", f"scale={size}"] if size else []), "-c:v", "libx264", "-crf", "12", "-pix_fmt", "yuv420p", str(path)], stdin=subprocess.PIPE)
    for i in range(int(seconds * fps)):
        p.stdin.write(bytes(osd_frame(chars, grid, i / fps, **kw)))
    p.stdin.close()
    p.wait()
    return path
