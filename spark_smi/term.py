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

# --- Color slots -------------------------------------------------------
# Logical slots used throughout both rendering backends. Bar-threshold
# coloring (<50 slot1, <80 slot5, else slot4) is applied by make_bar().
#   slot : meaning                 xterm-256  basic-8 fallback
#     1  : good / bar fill            70       green
#     2  : info / graphs / io         74       cyan
#     3  : label text                250       white
#     4  : critical / down           167       red
#     5  : warn                      172       yellow
#     6  : dim / frame               243       white + dim
#     7  : bar track (empty)         238       white + dim
#     8  : bright value              255       white + bold
#     9  : accent: titles/keys       112       green + bold
PALETTE_256 = {1: 70, 2: 74, 3: 250, 4: 167, 5: 172, 6: 243, 7: 238, 8: 255, 9: 112}

# Basic-8 fallback: (curses color constant name, bold, dim) -- app.py maps
# the name to the real curses.COLOR_* constant since that's curses-specific.
BASIC_SLOTS = {
    1: ("GREEN", False, False), 2: ("CYAN", False, False), 3: ("WHITE", False, False),
    4: ("RED", False, False), 5: ("YELLOW", False, False),
    6: ("WHITE", False, True), 7: ("WHITE", False, True),
    8: ("WHITE", True, False), 9: ("GREEN", True, False),
}

# Plain ANSI (no 256-color, no curses) SGR codes for VirtualCurses fallback.
_ANSI_BASIC = {1: "32", 2: "36", 3: "37", 4: "31", 5: "33", 6: "90", 7: "90", 8: "1;37", 9: "1;32"}

def _ansi_for_slot(slot):
    if HAS_256COLOR:
        code = f"38;5;{PALETTE_256.get(slot, 250)}"
        return f"\033[1;{code}m" if slot in (8, 9) else f"\033[{code}m"
    return f"\033[{_ANSI_BASIC.get(slot, '37')}m"


class VirtualCurses:
    """In-memory character grid standing in for a curses window in snapshot
    mode. Implements the same duck-typed interface real curses windows use:
    addstr(y, x, text, attr), getmaxyx(), erase()."""
    def __init__(self):
        self.update_dims()
        self.grid = [[(" ", None) for _ in range(self.cols)] for _ in range(self.rows)]
        self.colors = {s: _ansi_for_slot(s) for s in PALETTE_256}
        self.colors[0] = "\033[0m"

    def update_dims(self):
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
    (drawn in slot 6, dim/frame, by the caller)."""
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
