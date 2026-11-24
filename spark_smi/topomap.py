"""Page 3's cluster map (the TOPOLOGY panel): every node as a box, every
fabric link as a line between boxes, with live traffic on the line.

Links come from the samples, never from configuration. A node's RDMA rails
(the sample's nic_pf rows) carry their IPv4 address and prefix; two nodes
with a rail on the same network are cabled to each other, and a network
three or more nodes share is a switch, drawn as one bus they all hang off.

This module only draws. It takes plain view/metrics dicts (the same ones
pages.build_page3 already holds), returns rows of (text, slot) runs, and
never touches curses, the network or a file. An optional `cache` dict
(cluster.ClusterUI.topo_cache in live mode) remembers links so a node that
stops answering keeps its lines, drawn dim, instead of floating alone.
"""
import ipaddress
import math

from . import term

BOX_MIN = 22        # narrowest node box (inner bar still 6 cells)
BOX_MAX = 34
GAP_LABEL = 18      # a gap this wide carries rate labels on each rail
GAP_MIN = 4
GAP_MAX = 56
IDLE_BPS = 1e6      # below 1 Mb/s both ways a link reads as idle
MAX_GPU_ROWS = 3


def _glyphs():
    if term.BAR_STYLE == "ascii":
        return {"h": "-", "H": "=", "v": "|", "V": "|", "tl": "+", "tr": "+",
                "bl": "+", "br": "+", "td": "+", "tu": "+", "x": "+",
                "ml": "+", "mr": "+", "ra": ">", "la": "<", "dn": "v", "up": "^"}
    return {"h": "─", "H": "━", "v": "│", "V": "┃", "tl": "╭", "tr": "╮",
            "bl": "╰", "br": "╯", "td": "┬", "tu": "┴", "x": "┼",
            "ml": "├", "mr": "┤", "ra": "▸", "la": "◂", "dn": "▼", "up": "▲"}


# --- graph ---------------------------------------------------------------

def net_key(row):
    """'10.0.1.0/24' for a nic_pf row, or None without an address. A row
    with no prefix (a node on an older version) is read as /24."""
    ip = (row or {}).get("ip")
    if not ip:
        return None
    prefix = row.get("prefix")
    try:
        prefix = int(prefix) if prefix is not None else 24
        if not 8 <= prefix <= 31:
            return None
        return str(ipaddress.ip_network(f"{ip}/{prefix}", strict=False))
    except Exception:
        return None


def _rail_label(rows, row):
    """'p0f1': the row's port plus its place among that node's rows on the
    same port -- the labels page 4 gives the same rails."""
    port = row.get("port") or (row.get("netdev") or row.get("rdma_dev") or "?")[:4]
    k = 0
    for r in rows:
        if r is row:
            break
        if (r.get("port") or (r.get("netdev") or r.get("rdma_dev") or "?")[:4]) == port:
            k += 1
    return f"{port}f{k}"


def build_graph(views):
    """(links, buses) from the live samples.

    links: {(a, b): [rail, ...]} for each pair of nodes (names sorted) that
    share one or more two-member networks; rail = {"net", "a", "b"} with
    each side's nic_pf row. buses: {(name, ...): [{"net", "rows"}, ...]}
    for networks three or more nodes share, keyed by the member tuple."""
    nets = {}
    order = {}
    for v in views:
        s = v.get("sample")
        if v.get("stale") or not s:
            continue
        name = v.get("name") or "?"
        rows = s.get("nic_pf") or []
        for i, r in enumerate(rows):
            k = net_key(r)
            if not k:
                continue
            members = nets.setdefault(k, {})
            if name not in members:
                members[name] = r
                order[(k, name)] = i
    links, buses = {}, {}
    for k, members in nets.items():
        names = sorted(members)
        if len(names) == 2:
            a, b = names
            links.setdefault((a, b), []).append(
                {"net": k, "a": members[a], "b": members[b], "_o": order[(k, a)]})
        elif len(names) >= 3:
            buses.setdefault(tuple(names), []).append(
                {"net": k, "rows": members, "_o": min(order[(k, n)] for n in names)})
    for rails in list(links.values()) + list(buses.values()):
        rails.sort(key=lambda r: r["_o"])
    return links, buses


def _merge_cache(links, buses, views, cache):
    """Adds remembered links/buses whose members are not all answering now,
    as rails with no rows (drawn dim). A link between two nodes that both
    answer and no longer share a network is gone, and leaves the cache."""
    if cache is None:
        return links, buses
    live = {v.get("name") for v in views if v.get("sample") and not v.get("stale")}
    known = {v.get("name") for v in views}
    mem_links = cache.setdefault("links", {})
    mem_buses = cache.setdefault("buses", {})
    for key, rails in links.items():
        mem_links[key] = len(rails)
    for key, rails in buses.items():
        mem_buses[key] = len(rails)
    for key in list(mem_links):
        if key in links:
            continue
        if not set(key) <= known or set(key) <= live:
            del mem_links[key]
            continue
        links[key] = [{"net": None, "a": None, "b": None}] * mem_links[key]
    for key in list(mem_buses):
        if key in buses:
            continue
        if not set(key) <= known or set(key) <= live:
            del mem_buses[key]
            continue
        buses[key] = [{"net": None, "rows": {}}] * mem_buses[key]
    return links, buses


def _rail_rates(rail, left):
    """(left->right bps, right->left bps, capacity bps or None, state) for
    one rail of a link drawn with node `left` on its left. state: 'up',
    'down' (a port without carrier) or 'unknown' (no fresh sample)."""
    a, b = rail.get("a"), rail.get("b")
    if not a or not b:
        return None, None, None, "unknown"
    if left == "b":
        a, b = b, a
    if not a.get("up", True) or not b.get("up", True):
        return 0.0, 0.0, None, "down"
    lr = max(a.get("tx_bps") or 0, b.get("rx_bps") or 0)
    rl = max(b.get("tx_bps") or 0, a.get("rx_bps") or 0)
    speeds = [x for x in (a.get("speed_mbit"), b.get("speed_mbit")) if x]
    cap = min(speeds) * 1e6 if speeds else None
    return float(lr), float(rl), cap, "up"


def short_rate(bps):
    """'52.1G', '830M', '0' -- bits per second in at most five cells."""
    if bps is None:
        return "—"
    if bps < 1e3:
        return "0"
    for unit, div in (("T", 1e12), ("G", 1e9), ("M", 1e6), ("K", 1e3)):
        if bps >= div:
            v = bps / div
            return f"{v:.1f}{unit}" if v < 100 else f"{v:.0f}{unit}"
    return "0"


def _line_style(lr, rl, cap, state):
    """(horizontal glyph key, vertical glyph key, slot) for a link."""
    if state == "unknown":
        return "h", "v", 6
    if state == "down":
        return "h", "v", 4
    top = max(lr or 0, rl or 0)
    if top < IDLE_BPS:
        return "h", "v", 10
    frac = top / cap if cap else 0.5
    slot = term.ramp_slot(0.3 + 0.7 * min(frac, 1.0))
    return ("H", "V", slot) if frac >= 0.5 else ("h", "v", slot)


def _rate_label(lr, rl, state, g):
    if state == "unknown":
        return "no data"
    if state == "down":
        return "link down"
    if max(lr or 0, rl or 0) < IDLE_BPS:
        return "idle"
    return f"{g['ra']}{short_rate(lr)} {g['la']}{short_rate(rl)}"


# --- canvas --------------------------------------------------------------

class _Canvas:
    def __init__(self, w, h):
        self.w, self.h = w, h
        self.ch = [[" "] * w for _ in range(h)]
        self.slot = [[3] * w for _ in range(h)]

    def put(self, y, x, ch, slot):
        if 0 <= y < self.h and 0 <= x < self.w:
            self.ch[y][x] = ch
            self.slot[y][x] = slot

    def get(self, y, x):
        if 0 <= y < self.h and 0 <= x < self.w:
            return self.ch[y][x]
        return None

    def text(self, y, x, s, slot):
        for i, c in enumerate(s):
            self.put(y, x + i, c, slot)

    def runs(self, y, x, parts):
        for text, slot in parts:
            self.text(y, x, text, slot)
            x += len(text)
        return x

    def rows(self):
        out = []
        for y in range(self.h):
            runs = []
            for c, s in zip(self.ch[y], self.slot[y]):
                if runs and runs[-1][1] == s:
                    runs[-1] = (runs[-1][0] + c, s)
                else:
                    runs.append((c, s))
            out.append(runs)
        return out


# --- node boxes ----------------------------------------------------------

def _gpu_short(name):
    words = (name or "GPU").replace("(Unified)", "").split()
    words = [w for w in words if w not in ("NVIDIA", "GeForce", "RTX", "Tesla", "Quadro")]
    return (words[-1] if words else "GPU")[:4]


def _node_power(m):
    for r in (m or {}).get("power_rails") or []:
        if r.get("label") == "dc_input" and r.get("watts") is not None:
            return r["watts"]
    pw = (m or {}).get("gpu_pwr_sum")
    return pw if pw else None


def _box_lines(view, m, cw, detail):
    """Content rows for one node box, each exactly `cw` cells of runs."""
    if view.get("stale") or not view.get("sample") or m is None:
        why = "unreachable"
        return [[(f"{why:<{cw}}"[:cw], 6)]]
    extra = cw >= 24
    bar_w = cw - 12 - (6 if extra else 0)
    lb, rb = term.bar_brackets()
    lines = []

    def line(label, pct, tail, tail_slot=3):
        pct = max(0.0, min(float(pct or 0), 100.0))
        bar, slot = term.make_bar(pct, bar_w)
        parts = [(f"{label:<4} ", 6), (lb, 10), (bar, slot), (rb, 10), (f" {int(pct):>3}%", 3)]
        if extra:
            parts.append((f" {tail:>5}"[:6], tail_slot))
        lines.append(parts)

    for g in ((m.get("gpus") or [])[:MAX_GPU_ROWS]):
        pwr = str(g.get("pwr_str") or "").replace(" ", "")
        if pwr in ("N/A", ""):
            pwr = term.fmt_temp(g.get("temp")) if g.get("temp") is not None else ""
        line(_gpu_short(g.get("name")), g.get("util", 0), pwr, 8)
    if detail in ("full", "max"):
        temp = m.get("cpu_temp")
        line("CPU", m.get("cpu_pct", 0), term.fmt_temp(temp) if temp is not None else "")
        used = m.get("mem_used")
        line("MEM", m.get("mem_pct", 0), f"{used / 1024 ** 3:.0f}G" if used else "")
    if detail == "max":
        g_ = _glyphs()
        rx, tx = m.get("rx_sum") or 0, m.get("tx_sum") or 0
        gap = "  " if cw - 5 >= 15 else " "
        net = f"{g_['dn']}{short_rate(rx):<5}{gap}{g_['up']}{short_rate(tx)}"
        lines.append([("NET  ", 6), (f"{net:<{cw - 5}}"[:cw - 5], 3 if max(rx, tx) >= IDLE_BPS else 6)])
        disks = [d for d in (m.get("disks") or []) if d.get("used_pct") is not None]
        if disks:
            d = max(disks, key=lambda d: d.get("used_pct") or 0)
            t = d.get("temp")
            line("DISK", d.get("used_pct"), term.fmt_temp(t) if t is not None else "",
                 5 if (d.get("used_pct") or 0) >= 85 else 3)
    if not lines:
        lines.append([(f"{'no GPU':<{cw}}", 6)])
    return lines


def _draw_box(cv, y, x, bw, bh, view, m, lines, selected, g, title_y=None):
    """One node box. The title (name, hottest sensor, power) sits on the
    top border, or on the border at `title_y` -- the bottom one for boxes
    whose top border faces the lines below a first row."""
    up = bool(view.get("sample")) and not view.get("stale") and m is not None
    frame = 9 if selected else (10 if up else 6)
    name = view.get("name") or "?"
    right = ""
    if up:
        bits = []
        t = m.get("max_temp")
        if t is not None:
            bits.append(term.fmt_temp(t))
        pw = _node_power(m)
        if pw:
            bits.append(f"{pw:.0f}W")
        right = " ".join(bits)
    iw = bw - 2
    name = name[:max(1, iw - 4)]
    room = iw - 2 - len(name) - 2
    if len(right) + 2 > room:
        right = ""
    cv.put(y, x, g["tl"], frame)
    cv.put(y, x + bw - 1, g["tr"], frame)
    for i in range(1, bw - 1):
        cv.put(y, x + i, g["h"], frame)
    ty = y if title_y is None else title_y
    for r in range(1, bh - 1):
        cv.put(y + r, x, g["v"], frame)
        cv.put(y + r, x + bw - 1, g["v"], frame)
    cv.put(y + bh - 1, x, g["bl"], frame)
    cv.put(y + bh - 1, x + bw - 1, g["br"], frame)
    for i in range(1, bw - 1):
        cv.put(y + bh - 1, x + i, g["h"], frame)
    cv.text(ty, x + 2, f" {name} ", 9 if selected else (8 if up else 6))
    if right:
        cv.text(ty, x + bw - 3 - len(right), f" {right} ", 3)
    for r, parts in enumerate(lines[:bh - 2]):
        cv.runs(y + 1 + r, x + 2, parts)


# --- layout --------------------------------------------------------------

def _layout(nodes, links, buses, width, rows_n, detail, g):
    """A drawn canvas, or None when this arrangement does not fit `width`."""
    n = len(nodes)
    per = math.ceil(n / rows_n)
    if rows_n > 1 and per * (rows_n - 1) >= n:
        return None
    names = [v.get("name") or "?" for v, _ in nodes]
    idx = {nm: i for i, nm in enumerate(names)}
    row_of = [i // per for i in range(n)]
    col_of = [i % per for i in range(n)]

    def adjacent(a, b):
        ia, ib = idx[a], idx[b]
        return row_of[ia] == row_of[ib] and abs(col_of[ia] - col_of[ib]) == 1

    want_label_gap = any(adjacent(a, b) for a, b in links)
    bw = 0
    for gap_try in ((GAP_LABEL, GAP_MIN) if want_label_gap else (GAP_MIN,)):
        bw = min(BOX_MAX, (width - (per - 1) * gap_try) // per) if per > 1 else min(BOX_MAX, width)
        if bw >= BOX_MIN:
            break
    if bw < BOX_MIN:
        return None
    gap = min(GAP_MAX, (width - per * bw) // (per - 1)) if per > 1 else 0
    total = per * bw + (per - 1) * gap
    x_start = (width - total) // 2
    box_x = [x_start + c * (bw + gap) for c in col_of]

    gap_links = {k: r for k, r in links.items() if adjacent(*k) and gap >= 7}
    band = [("link", k, r) for k, r in links.items() if k not in gap_links]
    band += [("bus", k, r) for k, r in buses.items()]

    cw = bw - 4
    lines = [_box_lines(v, m, cw, detail) for v, m in nodes]
    inner_h = max([len(ls) for ls in lines] + [len(r) for r in gap_links.values()] + [1])
    bh = inner_h + 2

    # attach points: each band item touches each member box once, spread
    # along the edge that faces the band, ordered toward the other end
    touching = {i: [] for i in range(n)}
    for it_i, item in enumerate(band):
        for nm in item[1]:
            touching[idx[nm]].append(it_i)
    # boxes stacked in one column share its connection points, so a line
    # leaving the upper box never runs in the same column as one leaving
    # the box below it
    attach = {}
    for c in range(per):
        col_boxes = [i for i in range(n) if col_of[i] == c]
        here = box_x[col_boxes[0]] + bw // 2
        ends = []
        for i in col_boxes:
            for it_i in touching[i]:
                others = [box_x[idx[nm]] + bw // 2 for nm in band[it_i][1] if nm != names[i]]
                toward = (sum(others) / len(others) if others else here) - here
                ends.append((toward, row_of[i], it_i, i))
        if not ends:
            continue
        ends.sort()
        span = bw - 4
        for k, (_, _, it_i, i) in enumerate(ends):
            attach[(it_i, i)] = box_x[i] + 2 + round((k + 1) * span / (len(ends) + 1)) - 1
        if len({attach[(e[2], e[3])] for e in ends}) < len(ends):
            return None

    # tracks: row-1-only items nearest row 1, mixed next, row-2-only last;
    # within a group the shortest spans sit closest to the boxes
    def group(item):
        rows = {row_of[idx[nm]] for nm in item[1]}
        return 0 if rows == {0} else (2 if rows == {1} else 1)

    spans = []
    for it_i, item in enumerate(band):
        xs = [attach[(it_i, idx[nm])] for nm in item[1]]
        spans.append((min(xs), max(xs)))
    tracks = []          # list of (group, [(x1, x2), ...])
    track_of = {}
    for it_i in sorted(range(len(band)), key=lambda j: (group(band[j]), spans[j][1] - spans[j][0])):
        x1, x2 = spans[it_i]
        grp = group(band[it_i])
        for t, (tg, used) in enumerate(tracks):
            if tg == grp and all(x2 < u1 - 1 or x1 > u2 + 1 for u1, u2 in used):
                used.append((x1, x2))
                track_of[it_i] = t
                break
        else:
            tracks.append((grp, [(x1, x2)]))
            track_of[it_i] = len(tracks) - 1
    n_tracks = len(tracks)
    band_h = n_tracks if rows_n == 1 else max(1, n_tracks)
    height = bh * rows_n + band_h
    box_y = [0 if row_of[i] == 0 else bh + band_h for i in range(n)]

    cv = _Canvas(width, height)
    return {"cv": cv, "bw": bw, "bh": bh, "box_x": box_x, "box_y": box_y, "lines": lines,
            "gap_links": gap_links, "band": band, "attach": attach, "track_of": track_of,
            "spans": spans, "row_of": row_of, "idx": idx, "height": height}


def _draw(L, nodes, selected_name, g):
    cv, bw, bh = L["cv"], L["bw"], L["bh"]
    idx, row_of = L["idx"], L["row_of"]
    for i, (v, m) in enumerate(nodes):
        below = row_of[i] > 0
        _draw_box(cv, L["box_y"][i], L["box_x"][i], bw, bh, v, m, L["lines"][i],
                  selected_name is not None and v.get("name") == selected_name, g,
                  title_y=L["box_y"][i] + bh - 1 if below else None)

    total = 0.0
    # links between neighbours: one line per rail across the gap
    for (a, b), rails in L["gap_links"].items():
        ia, ib = idx[a], idx[b]
        left, right = (ia, ib) if L["box_x"][ia] < L["box_x"][ib] else (ib, ia)
        left_side = "a" if left == ia else "b"
        x1 = L["box_x"][left] + bw - 1
        x2 = L["box_x"][right]
        y0 = L["box_y"][left]
        left_rows = ((nodes[left][0].get("sample") or {}).get("nic_pf") or [])
        room = x2 - x1 - 1
        drawn_rails = []
        for rail in rails[:bh - 2]:
            lr, rl, cap, state = _rail_rates(rail, left_side)
            own = rail.get(left_side)
            rid = _rail_label(left_rows, own) if own else ""
            drawn_rails.append((lr, rl, cap, state, _rate_label(lr, rl, state, g), rid))
        # rail names on every row or on none, so the rows stay aligned
        with_ids = all(rid and len(rid) + len(label) + 6 <= room
                       for _, _, _, _, label, rid in drawn_rails)
        for r, (lr, rl, cap, state, label, rid) in enumerate(drawn_rails):
            y = y0 + 1 + r
            total += (lr or 0) + (rl or 0)
            hk, _, slot = _line_style(lr, rl, cap, state)
            cv.put(y, x1, g["ml"], cv.slot[y][x1])
            cv.put(y, x2, g["mr"], cv.slot[y][x2])
            for x in range(x1 + 1, x2):
                cv.put(y, x, g[hk], slot)
            if with_ids:
                cv.text(y, x1 + 2, f" {rid} ", 6)
                cv.text(y, x2 - 2 - len(label) - 1, f" {label} ", 3 if state == "up" else slot)
            elif len(label) + 4 <= room:
                cv.text(y, x1 + 1 + (room - len(label) - 2) // 2, f" {label} ", 3 if state == "up" else slot)

    # band items: drops from each member box, a horizontal on its track
    top_band = bh
    verticals = {}       # y -> set of x with a vertical passing
    drawn = []
    for it_i, (kind, key, rails) in enumerate(L["band"]):
        t = L["track_of"][it_i]
        y_t = top_band + t
        x1, x2 = L["spans"][it_i]
        if kind == "link":
            a, b = key
            ia, ib = idx[a], idx[b]
            left_side = "a" if L["attach"][(it_i, ia)] <= L["attach"][(it_i, ib)] else "b"
            lr = rl = 0.0
            cap = 0.0
            states = set()
            for rail in rails:
                r1, r2, c, st = _rail_rates(rail, left_side)
                states.add(st)
                lr += r1 or 0
                rl += r2 or 0
                cap += c or 0
            state = "up" if "up" in states else ("down" if "down" in states else "unknown")
            total += lr + rl
            label = _rate_label(lr, rl, state, g)
            labels = [f"{len(rails)}× {label}", label] if len(rails) > 1 and state == "up" else [label]
            hk, vk, slot = _line_style(lr, rl, cap or None, state)
            drops = {}
        else:
            moved = 0.0
            have = False
            drops = {}
            for nm in key:
                m_tx = m_rx = m_cap = 0.0
                seen = False
                for rail in rails:
                    row = (rail.get("rows") or {}).get(nm)
                    if row:
                        seen = have = True
                        m_tx += row.get("tx_bps") or 0
                        m_rx += row.get("rx_bps") or 0
                        m_cap += (row.get("speed_mbit") or 0) * 1e6
                moved += m_tx
                drops[nm] = _line_style(m_tx, m_rx, m_cap or None, "up" if seen else "unknown")
            total += moved
            state = "up" if have else "unknown"
            n_r = f"{len(rails)} rail{'s' if len(rails) != 1 else ''}"
            busy = have and moved >= IDLE_BPS
            labels = ([f"switch · {n_r} · Σ {short_rate(moved)}", f"switch · Σ {short_rate(moved)}"]
                      if busy else [f"switch · {n_r}"]) + ["switch"]
            hk, vk, slot = ("h", "v", 6) if not have else (
                ("h", "v", 10) if not busy else ("h", "v", term.ramp_slot(0.6)))
        drawn.append((it_i, kind, key, y_t, x1, x2, hk, vk, slot, labels, state, drops))

    # horizontals first, then verticals (crossings become ┼), labels last
    for it_i, kind, key, y_t, x1, x2, hk, vk, slot, labels, state, drops in drawn:
        for x in range(x1 + 1, x2):
            cur = cv.get(y_t, x)
            cv.put(y_t, x, g["x"] if cur in (g["v"], g["V"]) else g[hk], slot)
    for it_i, kind, key, y_t, x1, x2, hk, vk, slot, labels, state, drops in drawn:
        for nm in key:
            i = idx[nm]
            x = L["attach"][(it_i, i)]
            above = row_of[i] == 0
            d_vk, d_slot = (drops[nm][1], drops[nm][2]) if nm in drops else (vk, slot)
            if above:
                edge_y = L["box_y"][i] + bh - 1
                cv.put(edge_y, x, g["td"], cv.slot[edge_y][x])
                ys = range(edge_y + 1, y_t)
            else:
                edge_y = L["box_y"][i]
                cv.put(edge_y, x, g["tu"], cv.slot[edge_y][x])
                ys = range(y_t + 1, edge_y)
            for y in ys:
                cur = cv.get(y, x)
                cv.put(y, x, g["x"] if cur in (g["h"], g["H"]) else g[d_vk], d_slot)
                verticals.setdefault(y, set()).add(x)
            if x1 == x2:
                ch = g["v"]
            elif x == x1:
                ch = g["bl"] if above else g["tl"]
            elif x == x2:
                ch = g["br"] if above else g["tr"]
            else:
                ch = g["tu"] if above else g["td"]
            cv.put(y_t, x, ch, slot)
            verticals.setdefault(y_t, set()).add(x)
    for it_i, kind, key, y_t, x1, x2, hk, vk, slot, labels, state, drops in drawn:
        blocked = verticals.get(y_t, set())
        for label in labels:
            x = _label_spot(len(label) + 2, x1 + 1, x2 - 1, blocked)
            if x is None:
                continue
            cv.text(y_t, x, f" {label} ", 3 if state == "up" else slot)
            break
    return total


def _label_spot(n, lo, hi, blocked):
    """Leftmost-from-centre column where `n` cells fit inside [lo, hi]
    without covering a vertical line, or None."""
    last = hi - n + 1
    if last < lo:
        return None
    mid = lo + (last - lo) // 2
    for off in range(0, last - lo + 1):
        for x in (mid - off, mid + off):
            if lo <= x <= last and not any(x <= bx < x + n for bx in blocked):
                return x
    return None


def render(nodes, width, max_rows, selected_name=None, cache=None):
    """The map for `nodes` ([(view, metrics), ...], drawn in this order),
    as {"rows": [[(text, slot), ...], ...], "links", "buses", "rails",
    "bps"} -- or None when fewer than two nodes, or when no arrangement
    fits `width` x `max_rows` (the page then simply leaves the map out)."""
    if len(nodes) < 2 or width < BOX_MIN * 2 + GAP_MIN or max_rows < 3:
        return None
    views = [v for v, _ in nodes]
    try:
        links, buses = build_graph(views)
        links, buses = _merge_cache(links, buses, views, cache)
    except Exception:
        links, buses = {}, {}
    g = _glyphs()
    for rows_n in (1, 2):
        for detail in ("max", "full", "gpu"):
            try:
                L = _layout(nodes, links, buses, width, rows_n, detail, g)
            except Exception:
                L = None
            if L is None or L["height"] > max_rows:
                continue
            bps = _draw(L, nodes, selected_name, g)
            return {"rows": L["cv"].rows(), "links": len(links), "buses": len(buses),
                    "rails": sum(len(r) for r in links.values()) + sum(len(r) for r in buses.values()),
                    "bps": bps}
    return None
