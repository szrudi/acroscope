"""What Betaflight itself says about its OSD: a CLI dump (`diff all` or `dump`) and a MAX7456 font (`.mcm`).

The OSD reader (osd.py) needs to know where the timers are, what they show, and what the digits look like. All of
that is Betaflight's: the element positions and the timer settings are in the dump, and the glyphs are in the
font file the Configurator uploaded to the flight controller (the font is not in the dump: it lives on the OSD
chip). This module reads both, with the encodings of Betaflight's `osd.h` (firmware 4.x to 2026.6):

- an element's `osd_<name>_pos`: column = bits 0-4 (plus bit 10 for HD displays), row = bits 5-9, the element shown
  in OSD profile n when bit 10 + n is set (0x800 for profile 1). An element left at its default is not in a diff:
  defaults are not shown (`OSD_POS(midCol - 5, midRow)` without a profile bit).
- `osd_tim1` / `osd_tim2`: source = bits 0-3 (0 on time, 1 total armed, 2 last armed, 3 on/armed, 4 launch),
  precision = bits 4-7 (0 seconds, 1 hundredths, 2 tenths), alarm = bits 8-15 (minutes). Defaults: timer 1 on
  time in seconds (2560), timer 2 total armed in seconds (2561). A timer element is its symbol, then MM:SS, then
  .h or .hh for the precision.
- the analog grid: 30 columns, 13 rows on NTSC, 16 on PAL; a character is 12 x 18 font pixels.
- the STATS screen after a disarm: a label at midCol - 13, the value at midCol + 7, rows centred vertically under
  a "--- STATS ---" line; which rows there are depends on the stats switched on and on the hardware (a current
  sensor, a flash), so the reader finds a row by its label instead of counting.

A font is 256 characters of 12 x 18 pixels, two bits each: 00 black (the outline), 10 white (the stroke), 01/11
transparent (the camera picture shows through). Betaflight's fonts map ASCII to itself for digits and capitals.
"""
import base64
import os
import re
import time
import urllib.request
from pathlib import Path

COLS = 30
ROWS = {"NTSC": 13, "PAL": 16}
CHAR_W, CHAR_H = 12, 18
TIMER_SOURCES = ["on", "total", "last", "on_or_armed", "launch"]     # OSD_TIMER_SRC_*
TIMER_FORMATS = ["dd:dd", "dd:dd.dd", "dd:dd.d"]                    # OSD_TIMER_PREC_SECOND, _HUNDREDTHS, _TENTHS
TIMER_DEFAULTS = {"osd_tim1": 2560, "osd_tim2": 2561}
# osdTimerSourceNames: the STATS screen's label of a timer's row
TIMER_LABELS = ["ON TIME", "TOTAL ARM", "LAST ARM", "ON/ARM", "LAUNCH TIME"]
STATS_LABEL_COL, STATS_VALUE_COL = -13, 7                           # from midCol (cols // 2)

# The Configurator's fonts (resources/osd/2/, the set for firmware 4.1 on), fetched by name at this commit; a pilot
# with a font of their own uploads the .mcm instead. They are GPL-3.0 (betaflight-configurator), so they are
# fetched, not shipped.
FONT_COMMIT = "519a34e745177db87c4053d9679224470e0bdf58"
FONT_URL = "https://raw.githubusercontent.com/betaflight/betaflight-configurator/{commit}/resources/osd/2/{name}.mcm"
FONTS = ["default", "bold", "large", "extra_large", "betaflight", "digital", "clarity", "vision", "impact", "impact_mini"]


# ---- the dump --------------------------------------------------------------------------------------------------

def parse_dump(text: str) -> dict:
    """{version, settings} from a CLI `diff all` or `dump`: the global `set name = value` lines (the ones before
    the first `profile`/`rateprofile`/`battery_profile` section; the OSD's are all global) and the firmware line.
    Values stay text; numbers are read where they are used."""
    settings: dict[str, str] = {}
    version = None
    section = None
    for line in text.splitlines():
        line = line.strip()
        m = re.match(r"#\s*(Betaflight\s*/.*)$", line)
        if m and version is None:
            version = m.group(1).strip()
            continue
        if re.match(r"(profile|rateprofile|battery_profile)\s+\d+\s*$", line):
            section = line
            continue
        m = re.match(r"set\s+(\w+)\s*=\s*(.*?)\s*$", line)
        if m and section is None:
            settings[m.group(1).lower()] = m.group(2)
    if not settings:
        raise ValueError("no `set name = value` lines: paste the output of `diff all` (or `dump`) from the Betaflight CLI")
    return {"version": version, "settings": settings}


def _int(settings: dict, key: str, default: int | None = None) -> int | None:
    v = settings.get(key)
    if v is None:
        return default
    try:
        return int(v)
    except ValueError:
        raise ValueError(f"{key} = {v}: not a number") from None


def element(settings: dict, name: str, profile: int = 1) -> dict | None:
    """{col, row} of an OSD element (`tim_1`, `tim_2`, `total_flights`, ...) when it is shown in OSD profile
    `profile`; None when it is hidden or left at its default (which is hidden)."""
    pos = _int(settings, f"osd_{name}_pos")
    if pos is None or not pos & (1 << (10 + profile)):
        return None
    return {"col": (pos & 0x1F) | ((pos & 0x400) >> 5), "row": (pos >> 5) & 0x1F}


def timer(settings: dict, n: int) -> dict:
    """Timer n (1 or 2): {source, format, alarm}; the default when the dump does not set it."""
    v = _int(settings, f"osd_tim{n}", TIMER_DEFAULTS[f"osd_tim{n}"])
    src, prec = v & 0x0F, (v >> 4) & 0x0F
    return {"source": TIMER_SOURCES[src] if src < len(TIMER_SOURCES) else f"source {src}",
            "format": TIMER_FORMATS[prec] if prec < len(TIMER_FORMATS) else "dd:dd", "alarm": (v >> 8) & 0xFF,
            "label": TIMER_LABELS[src] if src < len(TIMER_LABELS) else None}


def video_system(settings: dict) -> str:
    """NTSC or PAL; AUTO (and nothing set, Betaflight's default) is taken as NTSC, the analog DVRs' 720 x 480."""
    v = settings.get("vcd_video_system", "AUTO").upper()
    return v if v in ROWS else "NTSC"


def osd_elements(settings: dict) -> dict:
    """What the OSD reader uses from a dump: the two timers (where and what), the flight counter, the grid."""
    vs = video_system(settings)
    out = {"video_system": vs, "rows": ROWS[vs], "cols": COLS, "timers": [], "counter": element(settings, "total_flights"),
           "displayport": settings.get("osd_displayport_device"), "craft": settings.get("craft_name")}
    for n in (1, 2):
        e = element(settings, f"tim_{n}")
        if e:
            out["timers"].append({"n": n, **e, **timer(settings, n)})
    out["timers"].sort(key=lambda t: (t["row"], t["col"]))
    return out


# ---- the font --------------------------------------------------------------------------------------------------

def parse_mcm(text: str) -> list[list[str]]:
    """256 characters, each 18 rows of 12 pixel codes ('00' black, '10' white, else transparent)."""
    lines = text.split()
    if not lines or lines[0] != "MAX7456":
        raise ValueError("not a MAX7456 font (.mcm): the first line is not MAX7456")
    bits = lines[1:]
    if len(bits) < 256 * 64 - 10 or any(len(b) != 8 or set(b) - {"0", "1"} for b in bits[:256 * 64 - 10]):
        raise ValueError(f"not a MAX7456 font (.mcm): {len(bits)} lines of 8 bits, 256 x 64 expected")
    chars = []
    for c in range(256):
        px = [b[2 * k:2 * k + 2] for b in bits[c * 64:c * 64 + 54] for k in range(4)]
        chars.append([px[r * CHAR_W:(r + 1) * CHAR_W] for r in range(CHAR_H)])
    return chars


def pack_font(chars: list[list[str]]) -> str:
    """A font as 256 x 54 bytes, base64: how a profile carries it (18 kB, not the .mcm's 147)."""
    out = bytearray()
    for ch in chars:
        px = [p for row in ch for p in row]
        for i in range(0, len(px), 4):
            out.append(int("".join(px[i:i + 4]), 2))
    return base64.b64encode(bytes(out)).decode()


def unpack_font(data: str) -> list[list[str]]:
    raw = base64.b64decode(data)
    if len(raw) != 256 * 54:
        raise ValueError(f"a packed font is {256 * 54} bytes, not {len(raw)}")
    chars = []
    for c in range(256):
        px = [f"{b:08b}"[2 * k:2 * k + 2] for b in raw[c * 54:(c + 1) * 54] for k in range(4)]
        chars.append([px[r * CHAR_W:(r + 1) * CHAR_W] for r in range(CHAR_H)])
    return chars


# Where fetched fonts are kept: a folder of its own, not the cache (a test's or a scratch run's throwaway cache would
# fetch them again). ACROSCOPE_FONTS names a folder of .mcm files to use as they are, never fetched into (a machine
# without the internet: drop default.mcm, impact.mcm there).
FONT_DIR = os.environ.get("ACROSCOPE_FONTS")
FONT_CACHE = Path("~/.cache/acroscope-fonts").expanduser() / FONT_COMMIT[:12]
FETCH_RETRY = 600                 # seconds before a font that could not be fetched is tried again
_failed: dict[str, tuple[float, str]] = {}


def fetch_font(name: str) -> str:
    """The .mcm text of a Configurator font by name (FONTS): from ACROSCOPE_FONTS, else from the font cache, else
    fetched once into it. A fetch that failed is not tried again for FETCH_RETRY s (OSError, said why)."""
    if name not in FONTS:
        raise ValueError(f"{name}: not a Configurator font ({', '.join(FONTS)}); upload the .mcm of your own font instead")
    if FONT_DIR is not None:
        p = Path(FONT_DIR).expanduser() / f"{name}.mcm"
        if not p.is_file():
            raise OSError(f"{name}.mcm is not in ACROSCOPE_FONTS ({FONT_DIR})")
        return p.read_text()
    p = FONT_CACHE / f"{name}.mcm"
    if p.is_file():
        return p.read_text()
    if name in _failed and time.time() - _failed[name][0] < FETCH_RETRY:
        raise OSError(_failed[name][1])
    try:
        with urllib.request.urlopen(FONT_URL.format(commit=FONT_COMMIT, name=name), timeout=20) as r:
            text = r.read().decode("ascii")
        parse_mcm(text)                                       # a broken download is not kept
    except (OSError, ValueError) as e:
        _failed[name] = (time.time(), f"the font {name} could not be fetched from the Betaflight Configurator's repository: {e}")
        raise OSError(_failed[name][1]) from None
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    return text
