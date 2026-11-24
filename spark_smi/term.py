"""Terminal capability detection, the shared color-slot table, VirtualCurses
(the in-memory screen used for snapshot mode), and the low-level glyph
renderers (bars, core strips, sparklines) that both the panel engine
(panels.py) and pages.py build on.

Everything here is screen-agnostic: it produces strings/slot-ints, never
curses objects. `app.py` is the only module allowed to touch real curses.
"""
import os
import shutil
import sys
import time

from . import color

# --- Unit/temperature toggles ---------------------------------------------
# Module globals flipped by keypress in app.py's main_loop, same convention
# as the pre-2.0 single-file version. fmt_temp/fmt_mem read them directly so
# callers never have to thread the current unit choice through every call.
USE_FAHRENHEIT = False
USE_DECIMAL_UNITS = False

# --- Bar-rendering tier detection ------------------------------------------
# Three tiers:
#   "eighths" - sub-cell resolution via U+2588..U+258F (8 steps per cell)
#   "blocks"  - whole-cell blocks only; the Linux framebuffer console font
#               (CP437 heritage) has "█" and "░" but NOT the eighth-blocks
#   "ascii"   - legacy [|||   ] style for non-UTF-8 locales or --ascii
def _detect_bar_style():
    if "--ascii" in sys.argv or os.environ.get("SPARK_SMI_ASCII"):
        return "ascii"
    enc = (getattr(sys.stdout, "encoding", "") or "").lower()
    if "utf" not in enc:
        return "ascii"
    if os.environ.get("TERM", "") == "linux":
        return "blocks"
    return "eighths"

BAR_STYLE = _detect_bar_style()

def _detect_256color():
    term_env = os.environ.get("TERM", "")
    return "256color" in term_env or os.environ.get("COLORTERM") in ("truecolor", "24bit")

HAS_256COLOR = _detect_256color()

# --- Color slots / themes -----------------------------------------------
# Logical slots used throughout both rendering backends. Bar-threshold
# coloring (<50 slot1, <80 slot5, else slot4) is applied by make_bar().
#   slot : meaning                 (spark theme) xterm-256  basic-8 fallback
#     1  : good / bar fill            70       green
#     2  : info / graphs / io         74       cyan
#     3  : label text                250       white
#     4  : critical / down           167       red
#     5  : warn                      172       yellow
#     6  : dim text                  derived   white + dim
#     7  : bar track (empty)         238       white + dim
#     8  : bright value              255       white + bold
#     9  : accent: titles/keys       112       green + bold
#    10  : frames, bar brackets      243       white + dim
#  11-15 : graph ramp, low to high   derived   slot 2's colour
#
# Measured, not hand-picked (D-02 item 3): build_palette() holds every ink
# to a contrast floor against the terminal's background (WCAG 2.2 contrast
# ratio): 4.5:1 for text (slots 1-6, 8, 9), 3:1 for graph marks (11-15),
# and a quiet band (1.3-3:1) for frames and bar tracks (7, 10), because a
# loud frame is as much a defect as faint text. A colour that misses its
# floor moves only as far as it must, then snaps to the nearest xterm-256
# colour that passes. On a light terminal the neutral inks (3, 6, 8) are
# mirrored in lightness and the coloured ones darkened only as far as the
# floors need. tests/test_themes.py holds every theme to these floors.
#
# THEMES holds each theme's design intent, in cycling order (live mode's
# 'c' key, --theme list): its nine xterm-256 colours, where slot 6 is the
# frame colour as before the measured themes (dim text is now derived). A
# new theme may give only its four anchors -- 9 accent, 1 good, 5 warn and
# 4 critical -- and the rest is derived. "terminal" draws with the
# terminal's own 16 colours and default foreground instead.
THEMES = {
    "spark":     {1: 70,  2: 74,  3: 250, 4: 167, 5: 172, 6: 243, 7: 238, 8: 255, 9: 112},
    "nord":      {1: 109, 2: 116, 3: 252, 4: 131, 5: 222, 6: 240, 7: 237, 8: 255, 9: 110},
    "dracula":   {1: 141, 2: 117, 3: 253, 4: 203, 5: 215, 6: 243, 7: 238, 8: 231, 9: 212},
    "solarized": {1: 37,  2: 33,  3: 247, 4: 160, 5: 136, 6: 241, 7: 236, 8: 254, 9: 33},
    "gruvbox":   {1: 214, 2: 108, 3: 223, 4: 167, 5: 208, 6: 243, 7: 237, 8: 230, 9: 208},
    "mono":      {1: 252, 2: 245, 3: 247, 4: 231, 5: 250, 6: 240, 7: 236, 8: 255, 9: 255},
    "amber":     {1: 214, 2: 178, 3: 180, 4: 208, 5: 220, 6: 94,  7: 58,  8: 230, 9: 214},
    "ice":       {1: 75,  2: 123, 3: 253, 4: 167, 5: 186, 6: 242, 7: 238, 8: 255, 9: 81},
    "sunset":    {1: 205, 2: 215, 3: 252, 4: 196, 5: 220, 6: 243, 7: 238, 8: 255, 9: 209},
    "cyber":     {1: 81,  2: 201, 3: 252, 4: 197, 5: 214, 6: 60,  7: 237, 8: 255, 9: 201},
    "terminal":  {},
}
TERMINAL_THEME = "terminal"
# "terminal": ANSI indices 0-15 and -1 (the terminal's default foreground).
_TERMINAL_SLOTS = {1: 2, 2: 6, 3: -1, 4: 1, 5: 3, 6: 8, 7: 8, 8: -1, 9: 4, 10: 8,
                   11: 6, 12: 6, 13: 6, 14: 14, 15: 14}

TEXT_SLOTS = (1, 2, 3, 4, 5, 6, 8, 9)
RAMP_SLOTS = (11, 12, 13, 14, 15)
QUIET_SLOTS = (7, 10)
TEXT_FLOOR, MARK_FLOOR = 4.5, 3.0
QUIET_BAND = (1.3, 3.0)
DARK_BG, LIGHT_BG = (30, 30, 30), (255, 255, 255)  # assumed when the terminal won't say

# Each theme's basic-8 "good" color (slot 1 and, bolded, slot 9) -- the only
# thing that actually varies theme to theme in the <256-color fallback (see
# _basic_slots_for below for the fixed rules governing the rest).
_THEME_BASIC1 = {
    "spark": "GREEN", "nord": "BLUE", "dracula": "MAGENTA", "solarized": "CYAN",
    "gruvbox": "YELLOW", "mono": "WHITE", "amber": "YELLOW", "ice": "CYAN",
    "sunset": "MAGENTA", "cyber": "CYAN", "terminal": "GREEN",
}

_ANSI_CODE = {"BLACK": 30, "RED": 31, "GREEN": 32, "YELLOW": 33,
              "BLUE": 34, "MAGENTA": 35, "CYAN": 36, "WHITE": 37}


def _basic_slots_for(name):
    """(curses color constant name, bold, dim) per slot for theme `name`'s
    <256-color fallback. Fixed rules per the architect's spec: slot 1/9
    follow the theme's basic "good" color; slot 2 is cyan except nord
    (blue) and amber/gruvbox (yellow); slots 4/5 are red/yellow except mono
    (white, since mono has no red/yellow to spend); 3/8 are always white
    (8 bold); 6/7/10 are always white+dim; the ramp (11-15) is slot 2's
    colour, its top step bold."""
    basic1 = _THEME_BASIC1.get(name, "GREEN")
    slot2 = "BLUE" if name == "nord" else ("YELLOW" if name in ("amber", "gruvbox") else "CYAN")
    slot4 = "WHITE" if name == "mono" else "RED"
    slot5 = "WHITE" if name == "mono" else "YELLOW"
    out = {
        1: (basic1, False, False), 2: (slot2, False, False), 3: ("WHITE", False, False),
        4: (slot4, False, False), 5: (slot5, False, False),
        6: ("WHITE", False, True), 7: ("WHITE", False, True),
        8: ("WHITE", True, False), 9: (basic1, True, False), 10: ("WHITE", False, True),
    }
    for s in RAMP_SLOTS:
        out[s] = (slot2, s == RAMP_SLOTS[-1], False)
    return out


def _ansi_basic_for(basic_slots):
    """Plain ANSI (no 256-color, no curses) SGR codes for VirtualCurses's
    fallback, derived from a theme's basic_slots. Dim white keeps the
    original convention of rendering as bright-black ("90") rather than the
    poorly-supported literal SGR dim code."""
    out = {}
    for slot, (name, bold, dim) in basic_slots.items():
        code = _ANSI_CODE.get(name, 37)
        if bold:
            out[slot] = f"1;{code}"
        elif dim:
            out[slot] = "90" if name == "WHITE" else str(code)
        else:
            out[slot] = str(code)
    return out


def _anchors_to_nine(spec):
    """Fill a theme given only some slots (at least 9, 1, 5, 4) up to the
    nine design slots: graphs follow the accent, label text is a light
    neutral tinted toward the accent, the frame and track are the label
    faded toward a dark ground."""
    rgb = color.xterm_rgb
    spec = dict(spec)
    accent = rgb(spec[9])
    spec.setdefault(1, spec[9])
    spec.setdefault(2, spec[9])
    if 3 not in spec:
        spec[3] = color.nearest_256(color.mix((235, 235, 235), accent, 0.12))
    label = rgb(spec[3])
    spec.setdefault(8, 255)
    spec.setdefault(6, color.nearest_256(color.mix(DARK_BG, label, 0.42)))
    spec.setdefault(7, color.nearest_256(color.mix(DARK_BG, label, 0.22)))
    return spec


def _mirror_lightness(rgb):
    L, a, b = color.to_oklab(rgb)
    return color.from_oklab((1.0 - L, a, b))


def _quiet(rgb, bg, ink):
    """rgb held inside QUIET_BAND against bg: raised toward `ink` when it
    vanishes, lowered toward bg when it shouts."""
    lo, hi = QUIET_BAND
    c = color.contrast(rgb, bg)
    if c < lo:
        rgb = color.reach(rgb, bg, lo + 0.05, ink)
    elif c > hi:
        steps = [color.mix(rgb, bg, t / 40) for t in range(41)]
        rgb = next(x for x in steps if color.contrast(x, bg) <= hi - 0.1)
    return rgb


def _snap_quiet(rgb, bg):
    lo, hi = QUIET_BAND
    best = None
    for n in range(16, 256):
        c = color.contrast(color.xterm_rgb(n), bg)
        if lo <= c <= hi:
            d = sum((x - y) ** 2 for x, y in zip(color.to_oklab(rgb), color.to_oklab(color.xterm_rgb(n))))
            if best is None or d < best[0]:
                best = (d, n)
    return best[1] if best else color.nearest_256(rgb)


def _floor_lightness(bg, floor, light):
    """OKLab lightness at which a neutral grey just reaches floor:1 on bg."""
    grey = color.to_floor((0, 0, 0) if not light else (255, 255, 255), bg, floor, light)
    return color.to_oklab(grey)[0]


def _dimmest(rgb, bg, floor, light):
    """rgb's hue at the lightness nearest the ground that still reaches
    floor:1 on bg (rgb itself if even it does not)."""
    L_hi = color.to_oklab(rgb)[0]
    if color.contrast(rgb, bg) < floor:
        return rgb
    lo, hi = (L_hi, 1.0) if light else (0.0, L_hi)
    for _ in range(24):
        mid = (lo + hi) / 2
        ok = color.contrast(color.with_lightness(rgb, mid), bg) >= floor
        if light:
            lo, hi = (mid, hi) if ok else (lo, mid)
        else:
            lo, hi = (lo, mid) if ok else (mid, hi)
    return color.with_lightness(rgb, lo if light else hi)


def build_palette(name, bg=DARK_BG, light=False):
    """{slot: xterm index} for theme `name`, measured against background
    rgb `bg` (see the slot table above). "terminal" returns ANSI indices
    0-15 and -1 (default foreground) unmeasured: the terminal's own theme
    owns those colours."""
    if name == TERMINAL_THEME:
        return dict(_TERMINAL_SLOTS)
    spec = _anchors_to_nine(THEMES[name])
    rgb = {s: color.xterm_rgb(n) for s, n in spec.items()}
    ink = (0, 0, 0) if light else (255, 255, 255)
    out = {}
    frame = rgb[6]                       # the design slot 6 was the frame ink
    seeds = {1: rgb[1], 2: rgb[2], 3: rgb[3], 4: rgb[4], 5: rgb[5],
             6: color.mix(rgb[3], frame, 0.5), 8: rgb[8], 9: rgb[9]}
    if light:
        # Neutral inks mirror in lightness (bright text becomes dark text).
        # Coloured inks keep their order of lightness, compressed to just
        # under the light-ground floor, so no two collapse into one colour.
        lf_dark = _floor_lightness(DARK_BG, TEXT_FLOOR, False)
        lf_light = _floor_lightness(bg, TEXT_FLOOR, True)
        for s in seeds:
            L = color.to_oklab(seeds[s])[0]
            if s in (3, 6, 8):
                seeds[s] = color.with_lightness(seeds[s], 1.0 - L)
            else:
                seeds[s] = color.with_lightness(seeds[s], lf_light - 0.02 - 0.3 * max(0.0, L - lf_dark))
        frame = color.with_lightness(frame, 1.0 - color.to_oklab(frame)[0])
    for s, seed in seeds.items():
        out[s] = color.nearest_256(color.to_floor(seed, bg, TEXT_FLOOR + 0.15, light), bg, TEXT_FLOOR)
    track = rgb[7] if not light else color.mix(bg, color.xterm_rgb(out[3]), 0.16)
    out[7] = _snap_quiet(_quiet(track, bg, ink), bg)
    out[10] = _snap_quiet(_quiet(frame, bg, ink), bg)
    # Graph ramp: the graph colour (slot 2) itself, from the lightness that
    # just passes the mark floor up to full strength, evenly spaced in OKLab.
    g_hi = color.xterm_rgb(out[2])
    g_lo = _dimmest(g_hi, bg, MARK_FLOOR + 0.15, light)
    used = set()
    for i, s in enumerate(RAMP_SLOTS):
        if s == RAMP_SLOTS[-1]:
            out[s] = out[2]
            break
        step = color.mix(g_lo, g_hi, i / (len(RAMP_SLOTS) - 1))
        out[s] = color.nearest_256(color.to_floor(step, bg, MARK_FLOOR + 0.15, light), bg, MARK_FLOOR,
                                   exclude=used | {out[2]})
        used.add(out[s])
    # steps rise in contrast, so a brighter cell always means a higher value
    steps = sorted((out[s] for s in RAMP_SLOTS), key=lambda n: color.contrast(color.xterm_rgb(n), bg))
    out.update(zip(RAMP_SLOTS, steps))
    return out


# --- Background detection (light or dark terminal) -------------------------
def parse_osc11(reply):
    """(r, g, b) 0-255 from an OSC 11 reply such as
    "\033]11;rgb:1e1e/1e1e/1e1e\033\\" (1-4 hex digits per channel), or None."""
    import re
    m = re.search(r"\]11;rgba?:([0-9a-fA-F]{1,4})/([0-9a-fA-F]{1,4})/([0-9a-fA-F]{1,4})", reply or "")
    if not m:
        return None
    return tuple(int(round(int(h, 16) / (16 ** len(h) - 1) * 255)) for h in m.groups())


def parse_colorfgbg(value):
    """True (light), False (dark) or None from $COLORFGBG ("fg;bg" or
    "fg;default;bg"): background indices 7 and 9-15 are light."""
    try:
        bg = int((value or "").split(";")[-1])
    except ValueError:
        return None
    if not 0 <= bg <= 15:
        return None
    return bg == 7 or bg >= 9


def is_light(rgb):
    """A ground is light when black text contrasts with it more than white."""
    return color.contrast(rgb, (0, 0, 0)) > color.contrast(rgb, (255, 255, 255))


def query_osc11(timeout=0.15):
    """Ask the terminal for its background colour (OSC 11). Returns (r, g,
    b) or None when stdin/stdout is not a terminal, the platform has no
    termios, or no answer comes within `timeout` seconds. Skipped on the
    Linux console, which reads ESC ] as its own palette command."""
    if os.environ.get("TERM", "") in ("linux", "dumb", ""):
        return None
    try:
        import select
        import termios
        import tty
        if not (sys.stdin.isatty() and sys.stdout.isatty()):
            return None
        fd = sys.stdin.fileno()
        saved = termios.tcgetattr(fd)
    except Exception:
        return None
    reply = ""
    try:
        tty.setcbreak(fd)
        sys.stdout.write("\033]11;?\033\\")
        sys.stdout.flush()
        end = time.time() + timeout
        while time.time() < end:
            ready, _, _ = select.select([fd], [], [], max(0.0, end - time.time()))
            if not ready:
                break
            reply += os.read(fd, 64).decode("latin-1")
            if reply.endswith("\a") or reply.endswith("\033\\"):
                break
    except Exception:
        reply = ""
    finally:
        try:
            termios.tcsetattr(fd, termios.TCSAFLUSH, saved)
        except Exception:
            pass
    return parse_osc11(reply)


def detect_background(setting="auto", ask=True):
    """(light, rgb or None, source) for the palettes. `setting` is "dark",
    "light" or "auto" (--background / $SPARK_SMI_BACKGROUND). auto asks the
    terminal (OSC 11) when `ask`, then reads $COLORFGBG, else assumes dark."""
    if setting in ("dark", "light"):
        return setting == "light", None, "setting"
    rgb = query_osc11() if ask else None
    if rgb is not None:
        return is_light(rgb), rgb, "terminal"
    guess = parse_colorfgbg(os.environ.get("COLORFGBG"))
    if guess is not None:
        return guess, None, "COLORFGBG"
    return False, None, "default"


# Live palette -- module globals every renderer reads at draw/construct
# time. Populated by set_theme() below (called once at import for "spark",
# so nothing regresses for callers that never touch themes at all).
ACTIVE_THEME = None
PALETTE_256 = {}
BASIC_SLOTS = {}
_ANSI_BASIC = {}
BACKGROUND = {"light": False, "rgb": DARK_BG, "source": "default"}
_PALETTE_CACHE = {}


def set_background(light, rgb=None, source="setting"):
    """Which ground the palettes are measured against: light or dark, and
    its exact colour when the terminal reported one. Re-applies the active
    theme."""
    BACKGROUND.update(light=bool(light), rgb=tuple(rgb) if rgb else (LIGHT_BG if light else DARK_BG),
                      source=source)
    if ACTIVE_THEME:
        set_theme(ACTIVE_THEME)


def set_theme(name):
    """Swaps the live slot color mapping to the named theme, measured
    against the current background (set_background). Both rendering
    backends read PALETTE_256/BASIC_SLOTS/_ANSI_BASIC at draw/construct time
    (module-global lookups, not copies taken at import), so this takes
    effect on the next frame -- app.py's live-mode 'c' key additionally
    re-runs curses.init_pair() since curses caches color-pair definitions
    across frames. Raises ValueError (never crashes) for an unknown name,
    message includes the valid list."""
    global ACTIVE_THEME, PALETTE_256, BASIC_SLOTS, _ANSI_BASIC
    if name not in THEMES:
        raise ValueError(f"unknown theme '{name}' -- valid: {', '.join(THEMES)}")
    key = (name, BACKGROUND["light"], BACKGROUND["rgb"])
    if key not in _PALETTE_CACHE:
        _PALETTE_CACHE[key] = build_palette(name, BACKGROUND["rgb"], BACKGROUND["light"])
    ACTIVE_THEME = name
    PALETTE_256 = dict(_PALETTE_CACHE[key])
    BASIC_SLOTS = _basic_slots_for(name)
    _ANSI_BASIC = _ansi_basic_for(BASIC_SLOTS)


set_theme("spark")

def _ansi_for_slot(slot):
    if HAS_256COLOR or ACTIVE_THEME == TERMINAL_THEME:
        n = PALETTE_256.get(slot, 250)
        code = "39" if n < 0 else f"38;5;{n}"
        return f"\033[1;{code}m" if slot in (8, 9) else f"\033[{code}m"
    return f"\033[{_ANSI_BASIC.get(slot, '37')}m"


class VirtualCurses:
    """In-memory character grid standing in for a curses window in snapshot
    mode. Implements the same duck-typed interface real curses windows use:
    addstr(y, x, text, attr), getmaxyx(), erase(). `size=(cols, rows)`
    fixes the grid size (--size, --shot, the live `S` key); without it the
    grid follows the terminal."""
    def __init__(self, size=None):
        self.size = size
        self.update_dims()
        self.grid = [[(" ", None) for _ in range(self.cols)] for _ in range(self.rows)]
        self.colors = {s: _ansi_for_slot(s) for s in PALETTE_256}
        self.colors[0] = "\033[0m"

    def update_dims(self):
        if self.size:
            self.cols, self.rows = self.size
            return
        try:
            self.cols, self.rows = shutil.get_terminal_size()
        except Exception:
            self.cols, self.rows = 120, 50

    def getmaxyx(self):
        self.update_dims()
        return self.rows, self.cols

    def erase(self):
        self.update_dims()
        self.grid = [[(" ", None) for _ in range(self.cols)] for _ in range(self.rows)]

    def addstr(self, y, x, text, attr=None):
        if 0 <= y < self.rows and 0 <= x < self.cols:
            for i, char in enumerate(str(text)):
                if x + i < self.cols:
                    self.grid[y][x + i] = (char, attr)

    def render(self):
        output = []
        last_row = max((r for r in range(self.rows) if any(c[0] != " " for c in self.grid[r])), default=0)
        for r in range(last_row + 1):
            row_str, current_fmt = "", None
            for c, fmt in self.grid[r]:
                if fmt != current_fmt:
                    row_str += self.colors.get(fmt, self.colors[0])
                    current_fmt = fmt
                row_str += c
            output.append(row_str + self.colors[0])
        return "\n".join(output)


# --- Formatting helpers ------------------------------------------------
def fmt_temp(celsius_val):
    try:
        c = float(celsius_val)
        return f"{int((c * 9 / 5) + 32)}°F" if USE_FAHRENHEIT else f"{int(c)}°C"
    except Exception:
        return "N/A"

def fmt_mem(bytes_val):
    """Always a space between the number and the unit ("121.7 GiB", not
    "121.7GiB") -- matches every mock (header subtitle, legend, GPU cards)."""
    if bytes_val in [None, "N/A"]:
        return "N/A"
    div = 1000.0 if USE_DECIMAL_UNITS else 1024.0
    s_m, s_g = ("MB", "GB") if USE_DECIMAL_UNITS else ("MiB", "GiB")
    try:
        if bytes_val > (div ** 3):
            return f"{bytes_val / (div ** 3):.1f} {s_g}"
        return f"{int(bytes_val / (div ** 2))} {s_m}"
    except Exception:
        return "N/A"

def fmt_disk_size(bytes_val):
    """Compact single-letter capacity for the STORAGE panel's model column
    ("3.73T", "465.7G") -- unlike fmt_mem's "121.7 GiB" this drops the space
    and the "i" since STORAGE rows are tighter on width. 2 decimal places at
    the T scale, 1 at the G scale, matching mock_page1.rendered.txt."""
    try:
        b = float(bytes_val)
    except Exception:
        return "N/A"
    div = 1000.0 if USE_DECIMAL_UNITS else 1024.0
    if b >= div ** 4:
        return f"{b / (div ** 4):.2f}T"
    return f"{b / (div ** 3):.1f}G"

def fmt_rate(value, mode="bit"):
    """Formats a throughput value for the NETWORK/STORAGE panels.
    mode="byte": value is bytes/s, rendered at storage-throughput scale, e.g.
    "1.18 GB/s" -- fixed-unit, no auto-scaling through k/M/G, matching how
    the mockups show even sub-1 values in-unit ("0.02 GB/s") rather than
    dropping to MB/s.
    mode="bit": value is bits/s (NIC rates). Idle/low-rate links spend most
    of their life well under 1 Gb/s, where a fixed Gb/s unit prints "0.0
    Gb/s" forever -- so this mode has one unit step: >=100 Mbit/s renders in
    Gb/s (2dp below 1 Gb/s so "0.94 Gb/s"/"0.11 Gb/s" keep precision, 1dp at
    or above 1 Gb/s -- "3.2 Gb/s", "142.6 Gb/s"); below 100 Mbit/s renders as
    integer Mb/s ("12 Mb/s", "0 Mb/s")."""
    try:
        v = float(value)
    except Exception:
        return "N/A"
    if mode == "byte":
        return f"{v / 1e9:.2f} GB/s"
    if v >= 1e8:  # >= 100 Mbit/s
        gbps = v / 1e9
        return f"{gbps:.1f} Gb/s" if gbps >= 1 else f"{gbps:.2f} Gb/s"
    return f"{int(round(v / 1e6))} Mb/s"


# --- Bars ----------------------------------------------------------------
# Partial-cell fill characters, 1/8th to 7/8ths (U+258F down to U+2589)
_EIGHTHS = ["▏", "▎", "▍", "▌", "▋", "▊", "▉"]

def bar_brackets():
    """Frame glyphs a bar is wrapped in: `[ ]` in ascii tier, `▕ ▏` otherwise
    (drawn in slot 10, frame, by the caller)."""
    return ("[", "]") if BAR_STYLE == "ascii" else ("▕", "▏")

def knob_arrows():
    """Left/right step-arrow glyphs for the Phase 4 interactive power/clock
    knob widget -- "[< 450 W >]" in ascii tier, "[◂ 450 W ▸]" otherwise
    (matches mock_page2.rendered.txt; the surrounding [ ] are literal, not
    bar_brackets(), since this widget is a stepper control, not a bar)."""
    return ("<", ">") if BAR_STYLE == "ascii" else ("◂", "▸")

def make_bar(percent, width):
    """3-tier bar fill, `width` chars long in every tier so layout math is
    style-independent. Returns (bar_string, slot) -- slot is threshold-colored
    (<50 good, <80 warn, else critical) so callers don't choose colors."""
    if width < 1:
        return "", 3
    pct = max(0.0, min(float(percent), 100.0))
    slot = 1 if pct < 50 else (5 if pct < 80 else 4)
    if BAR_STYLE == "ascii":
        filled = int((pct / 100.0) * width)
        return "|" * filled + " " * (width - filled), slot
    if BAR_STYLE == "blocks":
        filled = int(round((pct / 100.0) * width))
        return "█" * filled + "░" * (width - filled), slot
    # "eighths": 8 resolution steps per character cell
    total_eighths = int(round((pct / 100.0) * width * 8))
    full, rem = divmod(total_eighths, 8)
    bar_str = "█" * full
    if rem and full < width:
        bar_str += _EIGHTHS[rem - 1]
    return bar_str + "░" * (width - len(bar_str)), slot

# Segment-bar glyphs (fullest to emptiest); ascii tier substitutes plain chars.
_SEG_GLYPHS_ASCII = {"█": "#", "▓": "=", "▒": "-", "░": "."}

def seg_bar(segments, width):
    """Multi-segment bar for MEMORY. `segments` is [(fraction, slot, glyph), ...]
    with fraction in [0, 1] (fractions need not sum to 1 -- any remainder is
    absorbed by the last segment, typically "free"). Returns a list of
    (text, slot) chunks -- not a single string -- because each segment needs
    its own color; concatenating the chunk texts reconstructs the full-width
    bar."""
    if width < 1 or not segments:
        return []
    chunks = []
    remaining = width
    for i, (frac, slot, glyph) in enumerate(segments):
        g = _SEG_GLYPHS_ASCII.get(glyph, glyph) if BAR_STYLE == "ascii" else glyph
        if i == len(segments) - 1:
            n = remaining
        else:
            n = max(0, min(int(round(frac * width)), remaining))
        if n > 0:
            chunks.append((g * n, slot))
        remaining -= n
    return chunks


# --- Core strip & sparkline (eighths tier only) ---------------------------
_LEVELS = "▁▂▃▄▅▆▇█"

def core_strip(loads):
    """One glyph per core, height-mapped 0-100% to ▁..█ (slot 1). The
    framebuffer console font used in "blocks"/"ascii" tiers lacks these
    levels, so the strip is omitted (empty string) outside "eighths"."""
    if BAR_STYLE != "eighths":
        return ""
    out = []
    for p in loads:
        try:
            pct = max(0.0, min(float(p), 100.0))
        except Exception:
            pct = 0.0
        idx = min(int(pct / 100.0 * (len(_LEVELS) - 1)), len(_LEVELS) - 1)
        out.append(_LEVELS[idx])
    return "".join(out)

# Braille dot bits, bottom row to top, per Unicode U+2800 block layout:
# left column = dots 7,3,2,1 (bits 0x40,0x04,0x02,0x01); right column = dots
# 8,6,5,4 (bits 0x80,0x20,0x10,0x08). Packing two resampled history points
# per character (one per column, 4 vertical levels each) gives the graph
# double the horizontal density of a single-column-per-sample approach.
_BRAILLE_L = [0x40, 0x04, 0x02, 0x01]
_BRAILLE_R = [0x80, 0x20, 0x10, 0x08]

def sparkline(history, width):
    """Braille ring-buffer history graph. Eighths tier only; omitted (returns
    "") when the terminal can't render braille reliably in that tier, or
    history has fewer than 2 samples (snapshot mode takes exactly one)."""
    if BAR_STYLE != "eighths" or width < 1 or len(history) < 2:
        return ""
    n = width * 2
    hist = list(history)[-n:]
    if len(hist) < n:
        hist = [hist[0]] * (n - len(hist)) + hist
    hi = max(hist) or 1.0
    chars = []
    for i in range(0, len(hist), 2):
        a = hist[i]
        b = hist[i + 1] if i + 1 < len(hist) else hist[i]
        la = min(4, int((a / hi) * 4 + 0.5))
        lb = min(4, int((b / hi) * 4 + 0.5))
        bits = 0
        for k in range(la):
            bits |= _BRAILLE_L[k]
        for k in range(lb):
            bits |= _BRAILLE_R[k]
        chars.append(chr(0x2800 + bits))
    return "".join(chars)


# --- Multi-series braille chart (Phase 7: page 4's BANDWIDTH panel) -------
# Full 2x4-dot-per-cell braille plotting -- unlike sparkline() above (one
# series, 2 vertical levels per column, no line segments between samples),
# this connects consecutive points with a vertical dot run (so a fast ramp
# reads as a solid line, not a scatter of dots) and draws several series into
# the SAME cell grid with a deterministic "last drawn wins" color rule per
# cell. Ported verbatim from scratchpad/gen_chart.py's plotting approach
# (the script that generated mock_page4.txt's chart block) -- same BITS
# table, same divmod-into-cell-and-sub-cell-position math, same
# per-row same-color-run grouping on the way out.
_CHART_BITS = [[0x01, 0x08], [0x02, 0x10], [0x04, 0x20], [0x40, 0x80]]  # [dot_row][dot_col]

def braille_chart(series, width_cells, height_cells, y_max):
    """series = [(values, slot), ...] -- `values` is a list of y-values (any
    length; resampled to the dot grid, so callers don't need to pre-align to
    width_cells*2 points), drawn in list order so a later series' dots win
    any cell its line shares with an earlier one (mirrors gen_chart.py's
    "sagging amber rail drawn last so it wins contested cells" trick --
    callers control draw-order priority simply by list order).

    Returns a list of `height_cells` rows, each a list of (text, slot) runs
    (grouped by consecutive same-color cells, same shape term.sparkline's
    callers already expect from a rendered row) -- concatenating one row's
    run texts reconstructs exactly `width_cells` characters. Empty (no
    series drew into it) cells render as a blank braille cell (U+2800) in
    slot 7 (bar-track/empty), matching the mock's "e" (dim track) class.

    Only meaningful in the "eighths" glyph tier -- the "blocks"/"ascii"
    tiers have no braille glyphs in their console font, so this returns None
    there and the caller (pages.py) falls back to a plain per-series table."""
    if BAR_STYLE != "eighths" or width_cells < 1 or height_cells < 1:
        return None
    w_dots, h_dots = width_cells * 2, height_cells * 4
    y_max = float(y_max) if y_max else 1.0
    dots = [[0] * width_cells for _ in range(height_cells)]
    cell_slot = [[None] * width_cells for _ in range(height_cells)]

    for values, slot in series:
        if not values:
            continue
        n = len(values)
        prev_dy = None
        for dx in range(w_dots):
            idx = int(round(dx / (w_dots - 1) * (n - 1))) if (n > 1 and w_dots > 1) else 0
            v = values[min(idx, n - 1)]
            if v is None:
                prev_dy = None
                continue
            try:
                v = min(max(float(v), 0.0), y_max)
            except Exception:
                prev_dy = None
                continue
            dy = int(round((1 - v / y_max) * (h_dots - 1)))
            dy = max(0, min(h_dots - 1, dy))
            ys = [dy] if prev_dy is None else range(min(prev_dy, dy), max(prev_dy, dy) + 1)
            for y in ys:
                cy, ry = divmod(y, 4)
                cx, rx = divmod(dx, 2)
                dots[cy][cx] |= _CHART_BITS[ry][rx]
                cell_slot[cy][cx] = slot
            prev_dy = dy

    rows = []
    for cy in range(height_cells):
        runs, cur_slot, buf = [], None, []
        for cx in range(width_cells):
            ch = chr(0x2800 + dots[cy][cx]) if dots[cy][cx] else "⠀"
            slot = cell_slot[cy][cx] if cell_slot[cy][cx] is not None else 7
            if slot != cur_slot and buf:
                runs.append((cur_slot, "".join(buf)))
                buf = []
            cur_slot = slot
            buf.append(ch)
        if buf:
            runs.append((cur_slot, "".join(buf)))
        rows.append([(text, slot) for slot, text in runs])
    return rows
