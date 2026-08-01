"""Screen-agnostic panel engine. pages.py builds a list of Panel objects
once per frame; render() draws them through the same duck-typed screen
interface used everywhere in this app (addstr(y, x, text, attr), getmaxyx(),
erase()) -- no curses-specific calls here, so the exact same panels render
into a real curses window (live mode) or a VirtualCurses grid (snapshot).
"""
from . import term

FRAME = {"tl": "╭", "tr": "╮", "bl": "╰", "br": "╯", "h": "─", "v": "│",
         "ml": "├", "mr": "┤"}
FRAME_ASCII = {"tl": "+", "tr": "+", "bl": "+", "br": "+", "h": "-", "v": "|",
               "ml": "+", "mr": "+"}

def _frame_chars():
    return FRAME_ASCII if term.BAR_STYLE == "ascii" else FRAME


class Panel:
    """One panel section, `w` columns wide, drawn at absolute (y, x).

    kind='top'   opens a fresh bordered frame (╭─╮ ... ╰─╯).
    kind='mid'   continues the frame opened by the PREVIOUS panel in the
                 list at the same y its predecessor's content ended on,
                 replacing the gap with a ├─┤ separator line -- this is how
                 CPU+MEMORY (and later NETWORK+STORAGE) share one border in
                 the mockups instead of drawing two separate boxes.
    kind='plain' no border/side-rails at all -- used for the header and
                 footer bars, which are single flowing lines of text.

    `title`/`title_right` are [(text, slot), ...] runs shown in the top (or
    ├─┤ separator) border line. `.rows` are added with add_row(): each row
    is a list of segments (col, text, slot) -- col is a column stop inside
    the content area, None means "flow immediately after the previous
    segment" -- plus an optional `right` group of (text, slot) flushed
    against the right border/edge.
    """

    def __init__(self, y, x, w, title=None, title_right=None, kind="top"):
        self.y = y
        self.x = x
        self.w = w
        self.title = title or []
        self.title_right = title_right or []
        self.kind = kind
        self.rows = []

    def add_row(self, segments, right=None):
        self.rows.append((segments, right or []))
        return self

    @property
    def content_height(self):
        return len(self.rows)

    @property
    def total_height(self):
        """Title/border line + content rows. Does NOT include the closing
        bottom border, which render() draws (or skips) depending on whether
        the next panel continues this frame."""
        return 1 + len(self.rows)


def bar_segments(col, pct, width):
    """Row segments for a threshold-colored bar `width` cells wide, framed
    by term.bar_brackets() in slot 6 (dim/frame)."""
    lb, rb = term.bar_brackets()
    bar, slot = term.make_bar(pct, width)
    return [(col, lb, 6), (None, bar, slot), (None, rb, 6)]

def seg_bar_segments(col, chunks, suffix=None, suffix_slot=3):
    """Row segments for a pre-built multi-segment bar (term.seg_bar output),
    framed the same way as bar_segments(), with an optional trailing label
    (e.g. " 56%")."""
    lb, rb = term.bar_brackets()
    segs = [(col, lb, 6)]
    for text, slot in chunks:
        segs.append((None, text, slot))
    segs.append((None, rb, 6))
    if suffix:
        segs.append((None, suffix, suffix_slot))
    return segs


def _safe_addstr(screen, y, x, text, attr, max_x):
    """Writes `text` clipped to [0, max_x); swallows any backend error so
    one bad row never takes the whole dashboard down."""
    if not text or x >= max_x or x <= -len(text):
        return
    if x < 0:
        text = text[-x:]
        x = 0
    if x + len(text) > max_x:
        text = text[:max_x - x]
    if not text:
        return
    try:
        screen.addstr(y, x, text, attr)
    except Exception:
        pass


def _draw_segments(screen, y, x0, segments, colors_map, max_x, limit=None):
    """`limit` (if given) additionally caps drawing so column-positioned
    content can never grow into a right-flushed group on the same row."""
    cap = min(max_x, limit) if limit is not None else max_x
    cursor = x0
    for col, text, slot in segments:
        cx = x0 + col if col is not None else cursor
        _safe_addstr(screen, y, cx, text, colors_map.get(slot), cap)
        cursor = cx + len(text)


def _draw_flow(screen, y, x, runs, colors_map, max_x, limit=None):
    """Draws (text, slot) runs left-to-right starting at x, each flowing
    immediately after the previous one -- used for the borderless header/
    footer rows (no column stops, unlike bordered-panel content rows).
    `limit` (if given) additionally caps drawing so left-flowing content
    can never grow into a right-flushed group sharing the same row."""
    cap = min(max_x, limit) if limit is not None else max_x
    cursor = x
    for text, slot in runs:
        if cursor >= cap:
            break
        _safe_addstr(screen, y, cursor, text, colors_map.get(slot), cap)
        cursor += len(text)


def _draw_right(screen, y, right_edge_x, right, colors_map, max_x):
    """Draws `right` segments flush against right_edge_x (exclusive)."""
    total = sum(len(t) for t, _ in right)
    cursor = right_edge_x - total
    for text, slot in right:
        _safe_addstr(screen, y, cursor, text, colors_map.get(slot), max_x)
        cursor += len(text)


def _draw_title(screen, y, x, w, corner_l, corner_r, title, title_right, colors_map, max_x, fc):
    dim = colors_map.get(6)
    h = fc["h"]
    _safe_addstr(screen, y, x, corner_l, dim, max_x)
    _safe_addstr(screen, y, x + 1, h, dim, max_x)
    cursor = x + 2
    if title:
        _safe_addstr(screen, y, cursor, " ", dim, max_x)
        cursor += 1
        for text, slot in title:
            _safe_addstr(screen, y, cursor, text, colors_map.get(slot), max_x)
            cursor += len(text)
        _safe_addstr(screen, y, cursor, " ", dim, max_x)
        cursor += 1
    right_x = x + w - 1
    right_len = sum(len(t) for t, _ in title_right) + (2 if title_right else 0)
    fill_end = right_x - right_len
    if fill_end > cursor:
        _safe_addstr(screen, y, cursor, h * (fill_end - cursor), dim, max_x)
        cursor = fill_end
    if title_right:
        _safe_addstr(screen, y, cursor, " ", dim, max_x)
        cursor += 1
        for text, slot in title_right:
            _safe_addstr(screen, y, cursor, text, colors_map.get(slot), max_x)
            cursor += len(text)
        _safe_addstr(screen, y, cursor, " ", dim, max_x)
        cursor += 1
    if cursor < right_x:
        _safe_addstr(screen, y, cursor, h * (right_x - cursor), dim, max_x)
    _safe_addstr(screen, y, right_x, corner_r, dim, max_x)


def render(screen, panels, colors_map):
    """Draws every panel. Every content row is padded to exactly panel width
    (via the frame's side rails) so panels can sit side by side without
    gaps. Clips at the screen's *current* bounds every call so a resize
    mid-frame can't crash the draw."""
    try:
        max_y, max_x = screen.getmaxyx()
    except Exception:
        return
    fc = _frame_chars()
    dim = colors_map.get(6)

    for i, p in enumerate(panels):
        try:
            if p.y >= max_y or p.y < -1:
                continue

            if p.kind == "plain":
                y = p.y
                if p.title or p.title_right:
                    if 0 <= y < max_y:
                        right_len = sum(len(t) for t, _ in p.title_right)
                        limit = (p.x + p.w - right_len - 1) if p.title_right else None
                        _draw_flow(screen, y, p.x, p.title, colors_map, max_x, limit)
                        if p.title_right:
                            _draw_right(screen, y, p.x + p.w, p.title_right, colors_map, max_x)
                    y += 1
                for segments, right in p.rows:
                    if y >= max_y:
                        break
                    if y >= 0:
                        right_len = sum(len(t) for t, _ in right)
                        limit = (p.x + p.w - right_len - 1) if right else None
                        _draw_flow(screen, y, p.x, segments, colors_map, max_x, limit)
                        if right:
                            _draw_right(screen, y, p.x + p.w, right, colors_map, max_x)
                    y += 1
                continue

            # Bordered panel ('top' opens a frame, 'mid' continues one).
            corner_l = fc["tl"] if p.kind == "top" else fc["ml"]
            corner_r = fc["tr"] if p.kind == "top" else fc["mr"]
            if 0 <= p.y < max_y:
                _draw_title(screen, p.y, p.x, p.w, corner_l, corner_r,
                            p.title, p.title_right, colors_map, max_x, fc)
            y = p.y + 1
            for segments, right in p.rows:
                if y >= max_y:
                    break
                if y >= 0:
                    _safe_addstr(screen, y, p.x, fc["v"], dim, max_x)
                    right_len = sum(len(t) for t, _ in right)
                    limit = (p.x + p.w - 1 - right_len - 1) if right else None
                    _draw_segments(screen, y, p.x + 1, segments, colors_map, max_x, limit)
                    if right:
                        _draw_right(screen, y, p.x + p.w - 1, right, colors_map, max_x)
                    _safe_addstr(screen, y, p.x + p.w - 1, fc["v"], dim, max_x)
                y += 1

            # Bottom border, unless the next panel continues this same frame.
            next_p = panels[i + 1] if i + 1 < len(panels) else None
            continues = next_p is not None and next_p.kind == "mid" and next_p.y == y
            if not continues and 0 <= y < max_y:
                bottom = fc["bl"] + fc["h"] * max(0, p.w - 2) + fc["br"]
                _safe_addstr(screen, y, p.x, bottom, dim, max_x)
        except Exception:
            # One malformed panel must never take the whole dashboard down.
            continue
