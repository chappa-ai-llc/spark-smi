"""Write a rendered screen as SVG: `--shot FILE.svg` in snapshot mode and the
`S` key in live mode.

Input is a VirtualCurses grid of (char, slot) cells, so the picture is the
page exactly as the terminal would show it. Box drawing, block elements,
shades, braille and the arrow/marker shapes are drawn as SVG shapes, cell by
cell, so borders join and graphs line up whatever fonts the viewer has.
Everything else is text in a monospace font, stretched to the cell grid, so
it stays selectable. Standard library only.
"""
import html
import unicodedata

from . import term

FONT_FAMILY = "'DejaVu Sans Mono','Liberation Mono',Menlo,Consolas,'Courier New',monospace"
CELL_W = 9
LINE_H = 18
PAD = 12
DARK_BG, DARK_FG = "#1e1e1e", "#cccccc"
BOLD_SLOTS = (8, 9)  # the slots _ansi_for_slot() also bolds

_BASE16 = ["#000000", "#cd3131", "#0dbc79", "#e5e510", "#2472c8", "#bc3fbc", "#11a8cd", "#e5e5e5",
           "#666666", "#f14c4c", "#23d18b", "#f5f543", "#3b8eea", "#d670d6", "#29b8db", "#ffffff"]
_CUBE = [0, 95, 135, 175, 215, 255]


def xterm256(n):
    """'#rrggbb' for xterm colour index n (0-255)."""
    n = max(0, min(int(n), 255))
    if n < 16:
        return _BASE16[n]
    if n >= 232:
        v = 8 + 10 * (n - 232)
        return "#%02x%02x%02x" % (v, v, v)
    n -= 16
    return "#%02x%02x%02x" % (_CUBE[n // 36], _CUBE[(n // 6) % 6], _CUBE[n % 6])


# --- glyph geometry ----------------------------------------------------------
# Box drawing: (left, right, up, down) arm weights, 1 light, 2 heavy.
_BOX = {
    "─": (1, 1, 0, 0), "│": (0, 0, 1, 1), "┌": (0, 1, 0, 1), "┐": (1, 0, 0, 1),
    "└": (0, 1, 1, 0), "┘": (1, 0, 1, 0), "├": (0, 1, 1, 1), "┤": (1, 0, 1, 1),
    "┬": (1, 1, 0, 1), "┴": (1, 1, 1, 0), "┼": (1, 1, 1, 1),
    "━": (2, 2, 0, 0), "┃": (0, 0, 2, 2), "╴": (1, 0, 0, 0), "╶": (0, 1, 0, 0),
    "╵": (0, 0, 1, 0), "╷": (0, 0, 0, 1),
}
_ROUND = {"╭": ("r", "d"), "╮": ("l", "d"), "╰": ("r", "u"), "╯": ("l", "u")}
# Block elements as rectangles in eighths of the cell: (x0, y0, x1, y1).
_BLOCKS = {"█": [(0, 0, 8, 8)], "▀": [(0, 0, 8, 4)], "▐": [(4, 0, 8, 8)],
           "▕": [(7, 0, 8, 8)], "▔": [(0, 0, 8, 1)]}
for _k in range(1, 8):
    _BLOCKS[chr(0x2580 + _k)] = [(0, 8 - _k, 8, 8)]   # ▁ .. ▇: lower k/8
    _BLOCKS[chr(0x2590 - _k)] = [(0, 0, _k, 8)]       # ▉ .. ▏: left k/8
_SHADES = {"░": 0.25, "▒": 0.5, "▓": 0.75}
# Markers as polygons in a unit cell.
_SHAPES = {
    "▲": [(0.5, 0.22), (0.92, 0.78), (0.08, 0.78)], "▼": [(0.08, 0.22), (0.92, 0.22), (0.5, 0.78)],
    "▶": [(0.12, 0.2), (0.9, 0.5), (0.12, 0.8)], "◀": [(0.88, 0.2), (0.1, 0.5), (0.88, 0.8)],
    "■": [(0.15, 0.28), (0.85, 0.28), (0.85, 0.72), (0.15, 0.72)],
    "▪": [(0.3, 0.38), (0.7, 0.38), (0.7, 0.62), (0.3, 0.62)],
}
_BRAILLE_DOTS = [(0, 0, 0x01), (0, 1, 0x02), (0, 2, 0x04), (1, 0, 0x08), (1, 1, 0x10),
                 (1, 2, 0x20), (0, 3, 0x40), (1, 3, 0x80)]


def _f(v):
    return ("%.2f" % v).rstrip("0").rstrip(".")


def _cell_prims(ch, x, y):
    """Drawing primitives for one cell, [] for a blank braille cell, or None
    when the character is ordinary text. Primitives: ("rect", x0, y0, x1,
    y1, opacity), ("dot", cx, cy), ("line", x0, y0, x1, y1, width) and
    ("raw", svg-with-{fill}-placeholder)."""
    cw, lh = CELL_W, LINE_H
    if ch in _BLOCKS:
        return [("rect", x + cw * a / 8, y + lh * b / 8, x + cw * c / 8, y + lh * d / 8, 1)
                for a, b, c, d in _BLOCKS[ch]]
    if ch in _SHADES:
        return [("rect", x, y, x + cw, y + lh, _SHADES[ch])]
    code = ord(ch)
    if code == 0x2800:
        return []
    if 0x2800 < code <= 0x28FF:
        bits = code - 0x2800
        return [("dot", x + cw * (0.28 + 0.44 * c), y + lh * (0.125 + 0.25 * row))
                for c, row, bit in _BRAILLE_DOTS if bits & bit]
    if ch in _BOX:
        cx, cy = x + cw / 2, y + lh / 2
        out = []
        for weight, x0, y0, x1, y1 in zip(_BOX[ch], (x, cx, cx, cx), (cy, cy, y, cy),
                                          (cx, x + cw, cx, cx), (cy, cy, cy, y + lh)):
            if weight:
                sw = 1.0 * weight
                # run each arm half a stroke into the centre so joints close
                if x0 == x1:
                    y0, y1 = (y0 - sw / 2, y1) if y1 == y + lh else (y0, y1 + sw / 2)
                else:
                    x0, x1 = (x0 - sw / 2, x1) if x1 == x + cw else (x0, x1 + sw / 2)
                out.append(("line", x0, y0, x1, y1, sw))
        return out
    if ch in _ROUND:
        horiz, vert = _ROUND[ch]
        cx, cy, r = x + cw / 2, y + lh / 2, cw / 2
        hx = x + cw if horiz == "r" else x
        sx = cx + r if horiz == "r" else cx - r
        vy = y + lh if vert == "d" else y
        ey = cy + r if vert == "d" else cy - r
        sweep = {"╭": 0, "╮": 1, "╯": 0, "╰": 1}[ch]
        return [("raw", f'<path d="M{_f(hx)} {_f(cy)}H{_f(sx)}A{_f(r)} {_f(r)} 0 0 {sweep} '
                        f'{_f(cx)} {_f(ey)}V{_f(vy)}" fill="none" stroke="{{fill}}" stroke-width="1"/>')]
    if ch in _SHAPES:
        pts = " ".join(f"{_f(x + cw * px)},{_f(y + lh * py)}" for px, py in _SHAPES[ch])
        return [("raw", f'<polygon points="{pts}" fill="{{fill}}"/>')]
    return None


def _emit(prims_by_fill):
    """SVG for {fill: [primitive, ...]}: touching rectangles and line
    pieces merge, and each colour's dots and lines become one path."""
    out = []
    dot_r = min(CELL_W, LINE_H / 2) * 0.14
    for fill, prims in prims_by_fill.items():
        rects = sorted((p for p in prims if p[0] == "rect"), key=lambda p: (p[2], p[4], p[5], p[1]))
        merged = []
        for _, x0, y0, x1, y1, op in rects:
            m = merged[-1] if merged else None
            if m and m[1] == y0 and m[3] == y1 and m[4] == op and abs(m[2] - x0) < 0.01:
                m[2] = x1
            else:
                merged.append([x0, y0, x1, y1, op])
        for x0, y0, x1, y1, op in merged:
            opa = f' fill-opacity="{_f(op)}"' if op < 1 else ""
            out.append(f'<rect x="{_f(x0)}" y="{_f(y0)}" width="{_f(x1 - x0)}" height="{_f(y1 - y0)}" '
                       f'fill="{fill}"{opa}/>')
        dots = [p for p in prims if p[0] == "dot"]
        if dots:
            d = "".join(f"M{_f(cx)} {_f(cy)}h0" for _, cx, cy in dots)
            out.append(f'<path d="{d}" stroke="{fill}" stroke-width="{_f(dot_r * 2)}" '
                       f'stroke-linecap="round"/>')
        for width in sorted({p[5] for p in prims if p[0] == "line"}):
            lines = [p for p in prims if p[0] == "line" and p[5] == width]
            horiz = sorted((p for p in lines if p[2] == p[4]), key=lambda p: (p[2], p[1]))
            vert = sorted((p for p in lines if p[2] != p[4]), key=lambda p: (p[1], p[2]))
            segs = []
            for _, x0, y0, x1, y1, _w in horiz:
                if segs and segs[-1][0] == "H" and segs[-1][2] == y0 and x0 <= segs[-1][3] + 0.01:
                    segs[-1][3] = max(segs[-1][3], x1)
                else:
                    segs.append(["H", x0, y0, x1])
            for _, x0, y0, x1, y1, _w in vert:
                if segs and segs[-1][0] == "V" and segs[-1][1] == x0 and y0 <= segs[-1][3] + 0.01:
                    segs[-1][3] = max(segs[-1][3], y1)
                else:
                    segs.append(["V", x0, y0, y1])
            d = "".join(f"M{_f(a)} {_f(b)}H{_f(c)}" if k == "H" else f"M{_f(a)} {_f(b)}V{_f(c)}"
                        for k, a, b, c in segs)
            out.append(f'<path d="{d}" stroke="{fill}" stroke-width="{_f(width)}" fill="none"/>')
        out.extend(p[1].replace("{fill}", fill) for p in prims if p[0] == "raw")
    return "".join(out)


def slot_colors(palette=None):
    """{slot: '#rrggbb'} for a 9-slot xterm-256 palette (default: the live
    theme). Pictures always use the 256-colour palette, whatever the
    terminal running spark-smi supports."""
    palette = term.PALETTE_256 if palette is None else palette
    return {slot: xterm256(code) for slot, code in palette.items()}


def grid_to_svg(grid, colors=None, bg=DARK_BG, fg=DARK_FG):
    """SVG text for a VirtualCurses grid (rows of (char, slot) cells),
    trimmed to the last row and column with content."""
    colors = slot_colors() if colors is None else colors
    rows = [row for row in grid]
    last = max((i for i, row in enumerate(rows) if any(c != " " for c, _ in row)), default=-1)
    rows = rows[:last + 1]
    ncols = max((max((i for i, (c, _) in enumerate(row) if c != " "), default=-1) + 1 for row in rows),
                default=0)
    width, height = PAD * 2 + ncols * CELL_W, PAD * 2 + len(rows) * LINE_H
    font_size = round(CELL_W / 0.6, 2)
    baseline = (LINE_H - 1.133 * font_size) / 2 + 0.833 * font_size
    prims, texts = {}, []
    for ri, row in enumerate(rows):
        y = PAD + ri * LINE_H
        row = row[:ncols]
        ci = 0
        while ci < len(row):
            ch, slot = row[ci]
            fill = colors.get(slot, fg)
            x = PAD + ci * CELL_W
            cell = _cell_prims(ch, x, y)
            if cell is not None:
                prims.setdefault(fill, []).extend(cell)
                ci += 1
                continue
            cj = ci + 1
            while cj < len(row) and row[cj][1] == slot and _cell_prims(row[cj][0], 0, 0) is None:
                cj += 1
            run = "".join(c for c, _ in row[ci:cj]).rstrip(" ")
            body = run.lstrip(" ")
            if body:
                lead = len(run) - len(body)
                n = sum(2 if unicodedata.east_asian_width(c) in ("W", "F") else 1 for c in body)
                bold = ' font-weight="bold"' if slot in BOLD_SLOTS else ""
                texts.append(f'<text x="{_f(x + lead * CELL_W)}" y="{_f(y + baseline)}" fill="{fill}" '
                             f'textLength="{_f(n * CELL_W)}" lengthAdjust="spacingAndGlyphs" '
                             f'xml:space="preserve"{bold}>{html.escape(body, quote=False)}</text>')
            ci = cj
    return "\n".join([
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}">',
        f'<rect width="{width}" height="{height}" rx="8" fill="{bg}"/>',
        f'<g font-family="{html.escape(FONT_FAMILY)}" font-size="{_f(font_size)}">',
        _emit(prims),
        "".join(texts),
        "</g></svg>",
    ]) + "\n"


def write_svg(path, grid, **kw):
    """Write grid_to_svg() to path (UTF-8). Raises OSError."""
    with open(path, "w", encoding="utf-8") as f:
        f.write(grid_to_svg(grid, **kw))
