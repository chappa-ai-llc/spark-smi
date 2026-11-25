"""Page 2 (ADVANCED) on big screens (#14): at bigpage.layout_for() large or
xl, the page uses the whole terminal instead of the centred 160 columns.

  GPUs        one column per GPU: the normal page's detail panel (knob
              widgets included), then charts in a grid beneath it -- SM
              clock, power against its limit, temperature, PCIe in/out
  NIC         the per-PF table beside PORTS: a mirrored in/out chart per
              physical port (its PFs summed) and a CONGESTION chart (CNP
              sent + handled + ECN marks, per second)
  THERMALS    every sensor as a heat row over time, in as many columns as
              the height needs, beside POWER RAILS (the normal panel, knobs
              included, with a wall-power chart when the `spbm` driver
              reports one), PCIE LINKS and NVME SMART

Heights are pure arithmetic and fill the terminal exactly; None (the
normal page 2) when there is no long history (a remote drilldown), no
braille, or nothing fits. Reads sample["long"] -- State's local history,
never on the wire."""
import math

from . import bigpage, pages, panels, term

GPU_COL_MIN = 96      # a GPU column narrower than this cannot hold its detail rows
CELL_W = 72           # a chart cell's target width; a column holds inner // CELL_W across
CELL_MIN = 44         # narrower than this, a chart cell is not worth drawing
NIC_TABLE_W = 112     # the per-PF table's natural width
HEAT_COL_MIN = 48     # one thermal heat column: label, reading and a trend
HEAT_LABEL_W = 14
_LW = 5               # chart y-axis label column, as bigpage's

_MIN = {"gpu": 2, "port": 1, "wall": 0}
_MAX = {"gpu": 12, "port": 5, "wall": 8}
_GROW = ["gpu", "port", "wall", "gpu", "port", "gpu", "wall", "gpu", "port"]
_OPEN_AT = {"wall": 3}


# --- chart cells ----------------------------------------------------------
# A cell is a list of rows, each a list of (text, slot) runs exactly `w`
# cells wide, so cells side by side are plain concatenation.

def _fit(runs, w):
    """Pad or clip runs to exactly `w` cells."""
    out, used = [], 0
    for text, slot in runs:
        if used >= w:
            break
        text = text[:w - used]
        out.append((text, slot))
        used += len(text)
    if used < w:
        out.append((" " * (w - used), 3))
    return out


def _head(left, right, w):
    lw = sum(len(t) for t, _ in left)
    rw = sum(len(t) for t, _ in right)
    if lw + 1 + rw > w:
        return _fit(left, w)
    return _fit(left + [(" " * (w - lw - rw), 3)] + right, w)


def _labelled(rows, labels, w):
    out = []
    for r, runs in enumerate(rows):
        lab = labels.get(r)
        out.append(_fit([(f"{(lab or ''):>{_LW - 1}}", 6), ("┤" if lab else "│", 10)] + runs, w))
    return out


def _blank_chart(h, w, note="collecting history…"):
    rows = [_fit([(" " * (_LW - 1), 6), ("│", 10)], w) for _ in range(h)]
    if h:
        rows[h - 1] = _fit([(" " * (_LW - 1), 6), ("│", 10), (f" {note}", 6)], w)
    return rows


def _axis(w, span):
    return _fit([(" " * (_LW - 1), 3), ("└", 10)] + term.time_axis(w - _LW, span), w)


def area_cell(head, right, values, y_max, labels, w, h, span):
    """Head row, an `h`-row area chart with a y-axis, its time axis."""
    cw = w - _LW
    vals = [0.0 if v is None else float(v) for v in bigpage._window(values, span)]
    rows = term.area_chart(vals, cw, h, y_max=max(y_max, 1e-9), window=bigpage._w(span)) if len(vals) >= 2 else None
    body = _labelled(rows, labels, w) if rows else _blank_chart(h, w)
    return [_head(head, right, w)] + body + [_axis(w, span)]


def mirror_cell(head, right, up, down, floor, w, h, span, fmt=None):
    """Head row, a mirrored chart (`up` above the baseline, `down` below,
    h // 2 rows each way, at least 1), its time axis. h is even."""
    half = max(1, h // 2)
    cw = w - _LW
    n = cw * 2
    u = [0.0 if v is None else float(v) for v in up or []]
    d = [0.0 if v is None else float(v) for v in down or []]
    chart = None
    top = floor
    if len(u) >= 2 and len(d) >= 2:
        u, d = term.fit_window(u, bigpage._w(span), n), term.fit_window(d, bigpage._w(span), n)
        top = max(max(u), max(d), floor)
        chart = term.mirror_chart(u, d, cw, half_rows=half, up_floor=top, down_floor=top)
    lab = (fmt or _axis_rate)(top)
    body = _labelled(chart, {0: lab, 2 * half - 1: lab}, w) if chart else _blank_chart(2 * half, w)
    return [_head(head, right, w)] + body + [_axis(w, span)]


def cell_rows(h, mirror=False):
    """Rows a cell takes at chart height `h`."""
    return 1 + (2 * max(1, h // 2) if mirror else h) + 1


def _axis_rate(bps):
    for unit, div in (("T", 1e12), ("G", 1e9), ("M", 1e6), ("K", 1e3)):
        if bps >= div:
            v = bps / div
            return f"{v:.0f}{unit}" if v >= 10 else f"{v:.1f}{unit}"
    return "0"


def _axis_bytes(v):
    """A y-axis label for a bytes/s scale, at most four cells: '24GB'."""
    for unit, div in (("T", 1e12), ("G", 1e9), ("M", 1e6), ("K", 1e3)):
        if v >= div:
            x = v / div
            t = f"{x:.0f}" if x >= 10 else f"{x:.1f}"
            return t + unit + ("B" if len(t) + 2 <= 4 else "")
    return "0"


def _fmt_bps(v):
    return bigpage._net_scale(v or 0, "bit")


def _fmt_Bps(v):
    return bigpage._net_scale(v or 0, "byte")


GAP = 3               # columns between chart cells side by side


def grid_across(inner):
    """Chart cells per grid row in `inner` columns."""
    return max(1, min(4, (inner + GAP) // (CELL_W + GAP))) if inner >= CELL_MIN else 0


def grid_rows(cells, inner, h, mirror_flags=None):
    """Rows a grid of `cells` chart cells takes."""
    k = grid_across(inner)
    if not cells or not k:
        return 0
    flags = mirror_flags or [False] * cells
    total = 0
    for i in range(0, cells, k):
        total += max(cell_rows(h, f) for f in flags[i:i + k])
    return total


def _grid(p, cells, inner, span):
    """Add cells (lists of rows) to panel `p`, k across with GAP blank
    columns between; a short grid row widens its cells. Rows start at
    column 1, so they use inner - 1 cells."""
    k = grid_across(inner)
    if not k:
        return
    usable = inner - 1
    for i in range(0, len(cells), k):
        chunk = cells[i:i + k]
        n_c = len(chunk)
        cw = (usable - GAP * (n_c - 1)) // n_c
        widths = [cw] * (n_c - 1) + [usable - (cw + GAP) * (n_c - 1)]
        built = [fn(widths[j]) for j, fn in enumerate(chunk)]
        n = max(len(c) for c in built)
        for r in range(n):
            runs = []
            for j, c in enumerate(built):
                runs += c[r] if r < len(c) else [(" " * widths[j], 3)]
                if j < n_c - 1:
                    runs.append((" " * GAP, 3))
            p.add_row(bigpage._row(1, runs))


# --- GPUs -------------------------------------------------------------------

def _gpu_specs(gpu, caps, long, span, h):
    """The chart cells for one GPU: [(is_mirror, fn(width) -> rows)]."""
    adv = ((long.get("adv") or {}).get("gpu") or {}).get(gpu.get("id")) or {}
    specs = []
    clk = adv.get("clk") or []
    if len(clk) >= 2 or isinstance(gpu.get("clk_sm"), (int, float)):
        top = gpu.get("clk_sm_max") if isinstance(gpu.get("clk_sm_max"), (int, float)) else max([v or 0 for v in clk] or [1])
        now = gpu.get("clk_sm")
        head = [("SM CLOCK ", 6), (f"{now} MHz" if isinstance(now, (int, float)) else "N/A", 8)]
        right = [("max ", 6), (f"{top:.0f} MHz", 3)]
        lab = {0: f"{top / 1000:.1f}G", h - 1: "0"}
        specs.append((False, lambda w, c=clk, t=top, hd=head, r=right, lb=lab: area_cell(hd, r, c, t, lb, w, h, span)))
    pwr = (long.get("gpu_pwr") or {}).get(gpu.get("id")) or []
    if len(pwr) >= 2:
        lim = gpu.get("power_limit_mw")
        top = lim / 1000 if isinstance(lim, (int, float)) and lim > 0 else max(v or 0 for v in pwr) * 1.15
        now = pwr[-1] or 0
        head = [("POWER ", 6), (bigpage._fmt_watts(now), 8)]
        right = [("limit ", 6), (f"{top:.0f} W", 3)] if isinstance(lim, (int, float)) and lim > 0 else \
            [("peak ", 6), (bigpage._fmt_watts(max(v or 0 for v in bigpage._window(pwr, span))), 3)]
        lab = {0: f"{top:.0f}W", h - 1: "0"}
        specs.append((False, lambda w, s=pwr, t=top, hd=head, r=right, lb=lab: area_cell(hd, r, s, t, lb, w, h, span)))
    temp = adv.get("temp") or []
    if len(temp) >= 2:
        now = gpu.get("temp")
        slow = gpu.get("temp_slowdown")
        head = [("TEMP ", 6), (term.fmt_temp(now), 5 if isinstance(now, (int, float)) and now >= 80 else 8)]
        right = [("slowdown ", 6), (term.fmt_temp(slow), 3)] if isinstance(slow, (int, float)) else \
            [("peak ", 6), (term.fmt_temp(max(v or 0 for v in bigpage._window(temp, span))), 3)]
        lab = {0: term.fmt_temp(100).replace(" ", "").rstrip("CF"), h - 1: "0"}
        specs.append((False, lambda w, s=temp, hd=head, r=right, lb=lab: area_cell(hd, r, s, 100, lb, w, h, span)))
    rx, tx = adv.get("pcie_rx") or [], adv.get("pcie_tx") or []
    if caps.get("pcie") and len(rx) >= 2 and len(tx) >= 2:
        head = [("PCIE ", 6), ("▼ ", 2), (_fmt_Bps(rx[-1]), 8), ("  ▲ ", 5), (_fmt_Bps(tx[-1]), 8)]
        gen, wid = gpu.get("pcie_gen_cur"), gpu.get("pcie_width_cur")
        right = [(f"gen {gen} ×{wid}", 3)] if gen and wid else []
        specs.append((True, lambda w, a=rx, b=tx, hd=head, r=right: mirror_cell(hd, r, a, b, 1e7, w, h, span,
                                                                              fmt=_axis_bytes)))
    return specs


def _gpu_column(y, x, w, gpu, caps, state, long, v, span, idx, n_gpus, knob_ui):
    p = pages._build_gpu_detail_panel(y, x, w, gpu, caps, state.get("mem") or {}, idx == n_gpus - 1,
                                      state.get("driver", "Unknown"), state.get("cuda", "Unknown"),
                                      gpu_idx=idx, knob_ui=knob_ui)
    specs = _gpu_specs(gpu, caps, long, span, v["gpu"])
    if specs and grid_across(w - 2):
        p.add_row([])
        _grid(p, [fn for _, fn in specs], w - 2, span)
    return p


def gpu_column_rows(detail_rows, specs_flags, inner, h):
    if not specs_flags or not grid_across(inner):
        return detail_rows
    return detail_rows + 1 + grid_rows(len(specs_flags), inner, h, specs_flags)



# --- NIC --------------------------------------------------------------------

def _ports(nic_pf, long):
    """PFs grouped by physical port: [(port, [pf rows], rx hist, tx hist)]."""
    pf_hist = (long.get("adv") or {}).get("pf") or {}
    by = {}
    for r in nic_pf or []:
        by.setdefault(r.get("port") or r.get("netdev"), []).append(r)
    out = []
    for port, rows in by.items():
        rx = _sum_aligned([pf_hist.get(r.get("netdev"), {}).get("rx") or [] for r in rows])
        tx = _sum_aligned([pf_hist.get(r.get("netdev"), {}).get("tx") or [] for r in rows])
        out.append((port, rows, rx, tx))
    return out


def _sum_aligned(lists):
    lists = [list(l) for l in lists if l]
    if not lists:
        return []
    n = max(len(l) for l in lists)
    out = [0.0] * n
    for l in lists:
        off = n - len(l)
        for i, val in enumerate(l):
            out[off + i] += val or 0.0
    return out


def _nic_specs(nic_pf, long, span, h):
    pf_hist = (long.get("adv") or {}).get("pf") or {}
    specs = []
    for port, rows, rx, tx in _ports(nic_pf, long):
        if len(rx) < 2:
            continue
        up = [r for r in rows if r.get("up")]
        speeds = {r.get("speed_mbit") for r in up if r.get("speed_mbit")}
        cap = max(speeds) * 1e6 if speeds else 0
        head = [(f"{port} ", 9), (f"{len(rows)} PF{'s' if len(rows) != 1 else ''}  ", 6), ("▼ ", 2),
                (_fmt_bps(rx[-1]), 8), ("  ▲ ", 5), (_fmt_bps(tx[-1]), 8)]
        right = [("of ", 6), (_axis_rate(cap), 3)] if cap else [("down", 6)]
        specs.append((True, lambda w, a=rx, b=tx, hd=head, r=right: mirror_cell(hd, r, a, b, 1e8, w, h, span)))
    cong = _sum_aligned([(pf_hist.get(r.get("netdev")) or {}).get("cong") or [] for r in nic_pf or []])
    if len(cong) >= 2:
        win = bigpage._window(cong, span)
        top = max(max(v or 0 for v in win) * 1.15, 10.0)
        head = [("CONGESTION ", 6), (f"{cong[-1] or 0:.0f}/s", 8), ("  CNP + ECN marks", 6)]
        right = [("peak ", 6), (f"{max(v or 0 for v in win):.0f}/s", 3)]
        lab = {0: f"{top:.0f}" if top < 1e4 else f"{top / 1e3:.0f}k", h - 1: "0"}
        specs.append((False, lambda w, s=cong, t=top, hd=head, r=right, lb=lab: area_cell(hd, r, s, t, lb, w, h, span)))
    return specs


# --- thermals -----------------------------------------------------------------

def _thermal_series(entries, long):
    temps = long.get("temp") or {}
    out = []
    for t in entries:
        key = f"{t.get('hwname') or t.get('kind')}:{t.get('label')}"
        out.append((t, temps.get(key) or []))
    return out


def heat_cols(n, width, rows_avail):
    """Columns for `n` sensors in `width`: the fewest that fit in
    `rows_avail` rows, each at least HEAT_COL_MIN wide; 0 when none do."""
    inner = width - 3
    most = max(1, (inner + GAP) // (HEAT_COL_MIN + GAP))
    for k in range(1, most + 1):
        if math.ceil(n / k) <= rows_avail:
            return k
    return 0


def _build_thermals(y, x, w, series, k, span, rows):
    inner = w - 2
    p = panels.Panel(y, x, w, title=[("THERMALS", 9), (" every sensor over time", 3)],
                     title_right=[("cool ", 6), ("▆", 11), ("▆", 13), ("▆", 15), (" hot", 6)], kind="top")
    cw = (inner - 1 - GAP * (k - 1)) // k
    per = math.ceil(len(series) / k) if k else 0
    cells = cw - HEAT_LABEL_W - 6 - 1
    for r in range(per):
        segs = []
        for j in range(k):
            i = j * per + r
            col = 1 + j * (cw + GAP)
            if i >= len(series):
                continue
            t, hist = series[i]
            now = t.get("temp_c")
            hot = isinstance(now, (int, float)) and now >= 80
            segs.append((col, f"{str(t.get('label') or '?')[:HEAT_LABEL_W - 1]:<{HEAT_LABEL_W}}", 6))
            segs.append((None, f"{term.fmt_temp(now):>5} ", 5 if hot else 8))
            runs = term.heat_runs(bigpage._window(hist, span), cells, y_max=100, window=bigpage._w(span)) or []
            segs += bigpage._row(None, runs)
        p.add_row(segs)
    axis = []
    for j in range(k):
        axis += [(1 + j * (cw + GAP) + HEAT_LABEL_W + 6, "", 3)] + bigpage._row(None, term.time_axis(cells, span))
    p.add_row(axis)
    while len(p.rows) < rows:
        p.add_row([])
    return p


# --- assembly -------------------------------------------------------------------

def _right_panels(y, x, w, state, long, v, span, knob_ui):
    """POWER RAILS (+ wall chart), PCIE LINKS, NVME SMART stacked; the
    panels and the rows they take."""
    out = []
    rails = state.get("power_rails") or []
    has_spbm = bool(((state.get("caps") or {}).get("thermal") or {}).get("spbm"))
    if has_spbm and rails:
        rp = pages._build_power_rails_panel(y, x, w, rails, knob_ui=knob_ui)
        wall = _wall_series(rails, long)
        if v["wall"] and wall is not None:
            label, hist = wall
            win = bigpage._window(hist, span)
            top = max(max(v2 or 0 for v2 in win) * 1.15, 10.0)
            rp.add_row([])
            cell = area_cell([(f"{label.upper()} ", 6), (bigpage._fmt_watts(hist[-1] or 0), 8)],
                             [("peak ", 6), (bigpage._fmt_watts(max(v2 or 0 for v2 in win)), 3)],
                             hist, top, {0: f"{top:.0f}W", v["wall"] - 1: "0"}, w - 3, v["wall"], span)
            for row in cell:
                rp.add_row(bigpage._row(1, row))
        out.append(rp)
        y += rp.total_height + 1
    links = state.get("pcie_links") or []
    if links:
        lp = pages._build_pcie_links_panel(y, x, w, links, state.get("gpus") or [])
        out.append(lp)
        y += lp.total_height + 1
    sp = pages._build_nvme_smart_panel(y, x, w, state.get("smart"))
    out.append(sp)
    return out


def _wall_series(rails, long):
    hist = long.get("rail") or {}
    for label in ("dc_input", "sys_total"):
        if len(hist.get(label) or []) >= 2:
            return label, hist[label]
    return None


def _right_rows(state, long, w, v, knob_ui):
    rails = state.get("power_rails") or []
    has_spbm = bool(((state.get("caps") or {}).get("thermal") or {}).get("spbm"))
    total = 0
    if has_spbm and rails:
        total += len(pages._build_power_rails_panel(0, 0, w, rails, knob_ui=knob_ui).rows) + 2
        if v["wall"] and _wall_series(rails, long) is not None:
            total += 1 + v["wall"] + 2
    links = state.get("pcie_links") or []
    if links:
        total += len(pages._build_pcie_links_panel(0, 0, w, links, state.get("gpus") or []).rows) + 2
    total += len(pages._build_nvme_smart_panel(0, 0, w, state.get("smart")).rows) + 2
    return total


def build_page2_large(state, width, height, x0=0, layout="large", history=None, knob_ui=None,
                      theme_toast=None, remote=None, cluster_tabs=False, has_perf=False):
    """Page 2 for a big terminal, exactly `height` rows; None when it does
    not apply (no long history, no braille) or nothing fits."""
    long = state.get("long")
    if term.BAR_STYLE != "eighths" or not height or not long:
        return None
    bigpage._RATE[0] = max(0.1, float(state.get("rate", 1.0) or 1.0))
    span = history or bigpage.DEFAULT_HISTORY.get(layout, 300)
    gpus = state.get("gpus") or []
    gpu_caps = (state.get("caps") or {}).get("gpu") or []
    nic_pf = state.get("nic_pf") or []

    # GPU columns, `per` across
    per = max(1, min(len(gpus), width // GPU_COL_MIN)) if gpus else 1
    gpu_info = []
    for idx, g in enumerate(gpus):
        caps = gpu_caps[idx] if idx < len(gpu_caps) else {}
        detail = len(pages._build_gpu_detail_panel(0, 0, GPU_COL_MIN, g, caps, state.get("mem") or {},
                                                   idx == len(gpus) - 1, "", "", gpu_idx=idx,
                                                   knob_ui=knob_ui).rows)
        flags = [m for m, _ in _gpu_specs(g, caps, long, span, 4)]
        gpu_info.append((detail, flags))

    def col_w(n_in_row, j):
        cw = width // n_in_row
        return cw if j < n_in_row - 1 else width - cw * (n_in_row - 1)

    def gpu_rows(v):
        total = 0
        for k in range(0, len(gpus), per):
            chunk = gpu_info[k:k + per]
            total += max(gpu_column_rows(d, f, col_w(len(chunk), j) - 2, v["gpu"])
                         for j, (d, f) in enumerate(chunk)) + 2
        return total

    nic_w = min(NIC_TABLE_W, width // 2) if nic_pf else width
    ports_w = width - nic_w - 1
    nic_flags = [m for m, _ in _nic_specs(nic_pf, long, span, 4)]
    nic_table_rows = len(pages._build_nic_panel(0, 0, nic_w, nic_pf, "", None).rows)

    def nic_rows(v):
        ports = grid_rows(len(nic_flags), ports_w - 2, v["port"] * 2, nic_flags) if nic_flags else 0
        return max(nic_table_rows, ports) + 2

    therm = _thermal_series(state.get("thermal") or [], long)
    if not (gpus or nic_pf or therm):
        return None         # nothing to chart: the normal page says it better
    right_w = max(GPU_COL_MIN, width * 2 // 5)
    therm_w = width - right_w - 1

    def bottom(v, avail):
        """(rows the bottom section takes, thermal columns) or None."""
        r = _right_rows(state, long, right_w, v, knob_ui)
        if not therm:
            return r, 0
        k = heat_cols(len(therm), therm_w, avail - 3)      # borders and the time axis
        if not k:
            return None
        return max(r, math.ceil(len(therm) / k) + 1 + 2), k

    def total(v):
        used = 1 + 1 + gpu_rows(v) + nic_rows(v)
        b = bottom(v, height - used)
        if b is None:
            return None, 0
        return used + b[0], b[1]

    v = dict(_MIN)
    t, _ = total(v)
    if t is None or t > height:
        return None
    grown = True
    while grown:
        grown = False
        for key in _GROW:
            if v[key] >= _MAX[key]:
                continue
            step = _OPEN_AT.get(key, 1) if v[key] == 0 else 1
            trial = dict(v, **{key: v[key] + step})
            tt, _ = total(trial)
            if tt is not None and tt <= height:
                v = trial
                grown = True
    _, k_th = total(v)

    out = [pages._build_header(x0, width, "wide", page=2, cluster_tabs=cluster_tabs, remote=remote,
                               has_perf=has_perf)]
    y = 1
    for k in range(0, len(gpus), per):
        chunk = list(range(k, min(len(gpus), k + per)))
        row_h = max(gpu_column_rows(gpu_info[i][0], gpu_info[i][1], col_w(len(chunk), j) - 2, v["gpu"])
                    for j, i in enumerate(chunk))
        cw0 = width // len(chunk)
        for j, i in enumerate(chunk):
            caps = gpu_caps[i] if i < len(gpu_caps) else {}
            p = _gpu_column(y, x0 + j * cw0, col_w(len(chunk), j), gpus[i], caps, state, long, v, span, i,
                            len(gpus), knob_ui)
            while len(p.rows) < row_h:
                p.add_row([])
            out.append(p)
        y += row_h + 2

    nic_h = nic_rows(v) - 2
    title_extra = pages._nic_panel_title(state.get("nics") or [], nic_pf, state.get("nic_fw") or {})
    np_ = pages._build_nic_panel(y, x0, nic_w, nic_pf, title_extra, state.get("nic_asic_temp"))
    while len(np_.rows) < nic_h:
        np_.add_row([])
    out.append(np_)
    if nic_pf:
        pp = panels.Panel(y, x0 + nic_w + 1, ports_w, title=[("PORTS", 9), (" in ▼ above, out ▲ below", 3)],
                          title_right=[("history ", 6), (term.fmt_span(span), 3)], kind="top")
        specs = _nic_specs(nic_pf, long, span, v["port"] * 2)
        if specs:
            _grid(pp, [fn for _, fn in specs], ports_w - 2, span)
        else:
            pp.add_row([(1, "collecting history…", 6)])
        while len(pp.rows) < nic_h:
            pp.add_row([])
        out.append(pp)
    y += nic_h + 2

    bottom_h = height - 1 - y - 2
    right = _right_panels(y, x0 + therm_w + 1 if therm else x0, right_w if therm else width, state, long, v,
                          span, knob_ui)
    if right:
        last = right[-1]
        end = last.y + last.total_height
        while end < height - 2:
            last.add_row([])
            end += 1
    if therm:
        out.append(_build_thermals(y, x0, therm_w, therm, k_th, span, bottom_h))
    out += right
    out.append(pages.page2_footer(state, x0, width, height - 1, knob_ui, theme_toast, remote))
    return out
