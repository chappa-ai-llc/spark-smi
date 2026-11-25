"""Big-screen page 1 (#14): the layouts for terminals of 200+ columns.

pages.build_page1 tops out at 160 columns, centred, which leaves a full-screen
1080p terminal (about 240x67 cells) a third empty across and half empty down.
build_page1_large() uses the whole terminal instead:

  large (200-299 cols)  a dial strip, then CPU+MEMORY | GPUs side by side,
                        then NETWORK+STORAGE across the full width
  xl    (300+ cols)     the same with a third column, POWER & THERMAL, and a
                        per-core heatmap under the CPU charts
  tall  (`g` = large on a 120-199 column terminal) one column, no dials

Charts carry value and time axes and draw the last `history` seconds of the
local long buffers (State's sample["long"]; a remote drilldown sample has
none, so its 60-second histories are used). Every chart starts at a minimum
height and grows one row at a time, round-robin, until the page fills the
terminal exactly -- spare rows always become chart rows. When even the
minimum doesn't fit, layout_for() says so and page 1 keeps its normal layout.

Same rules as the rest of the UI: no sensor, no row (a dial, a column or a
chart appears only when its data exists), panels only -- no curses calls --
and every colour through a slot.
"""
from . import panels, pages, term

LARGE_MIN_W = 200
XL_MIN_W = 300
LARGE_MIN_H = 45
TALL_MIN_W, TALL_MIN_H = 120, 36
GRAPH_MODES = ("auto", "large", "compact")
HISTORY_STEPS = (60, 300, 600, 1800, 3600)
DEFAULT_HISTORY = {"tall": 300, "large": 300, "xl": 600}

_LABEL_W = 5        # y-axis label column: "100%", "420W", "24G"


def layout_for(width, height, graphs="auto"):
    """Which big layout fits a width x height terminal, or None for the
    normal page 1. `graphs` is the `g` key's mode: compact never uses one,
    large also opens the one-column layout on 120-199 column terminals."""
    if graphs == "compact" or not height or height > 400:
        return None
    if width >= LARGE_MIN_W and height >= LARGE_MIN_H:
        return "xl" if width >= XL_MIN_W else "large"
    if graphs == "large" and width >= TALL_MIN_W and height >= TALL_MIN_H:
        return "tall"
    return None


def history_step(current, direction, layout=None):
    """The next history window for the + / - keys, in HISTORY_STEPS."""
    cur = current or DEFAULT_HISTORY.get(layout, 300)
    steps = list(HISTORY_STEPS)
    if cur not in steps:
        steps = sorted(set(steps + [cur]))
    i = steps.index(cur) + (1 if direction > 0 else -1)
    return steps[max(0, min(len(steps) - 1, i))]


# --- small helpers ------------------------------------------------------

def _row(col, runs):
    """(text, slot) runs -> panel segments starting at column `col`."""
    segs = []
    for i, (text, slot) in enumerate(runs):
        segs.append((col if i == 0 else None, text, slot))
    return segs


def _chart_rows(values, width, height, y_max, window, labels, measure):
    """Rows for an area chart with a y-axis: `labels` {row: text}."""
    if measure:
        return [[] for _ in range(height)]
    cw = width - _LABEL_W - 1
    rows = term.area_chart(values, cw, height, y_max=y_max, window=_w(window))
    if rows is None:
        return [[] for _ in range(height)]
    out = []
    for r, runs in enumerate(rows):
        lab = labels.get(r)
        tick = "┤" if lab else "│"
        out.append([(1, f"{(lab or ''):>{_LABEL_W - 1}}", 6), (None, tick, 10)] + _row(None, runs))
    return out


def _axis_row(width, span, measure):
    if measure:
        return []
    return [(1, " " * (_LABEL_W - 1), 3), (None, "└", 10)] + _row(None, term.time_axis(width - _LABEL_W - 1, span))


def _pct_labels(h):
    labels = {0: "100%", h - 1: "0"}
    if h >= 5:
        labels[(h - 1) // 2] = "50%"
    return labels


def _short_gpu_name(name):
    s = str(name or "GPU")
    for junk in ("NVIDIA ", "GeForce ", " (Unified)", "Tesla "):
        s = s.replace(junk, "")
    return s.strip()[:14]


# Charts span `history` seconds; at -n 2 that is half as many samples.
# Set once per build by build_page1_large (rendering is single-threaded).
_RATE = [1.0]


def _w(span):
    """Samples in `span` seconds at the current refresh rate."""
    return max(2, int(round(span / max(0.1, _RATE[0]))))


def _window(series, span):
    return list(series or [])[-_w(span):]


def _peak_avg_pct(values):
    vals = [float(v or 0) for v in values] or [0.0]
    return max(vals), sum(vals) / len(vals)


def _bar_runs(pct, width, slot):
    """A bar as runs: the filled part in `slot`, the track in slot 7, so a
    column of bars reads as bars rather than one block."""
    bar, _ = term.make_bar(pct, width)
    filled = bar.rstrip("░")
    return [(filled, slot), (bar[len(filled):], 7)] if filled else [(bar, 7)]


def _fmt_watts(w):
    return f"{w:.0f} W" if w >= 10 else f"{w:.1f} W"


# --- dials --------------------------------------------------------------

def _dials(state, long):
    """Capability-driven: a dial only for a number the sample really has.
    Returns [(frac, value_text, sub_text, label, warn), ...] in priority
    order; the strip shows as many as fit."""
    out = []
    cpu = state.get("cpu") or {}
    clusters = cpu.get("clusters") or []
    n_cores = sum(len(c.get("cores") or []) for c in clusters)
    if clusters and n_cores:
        avg = sum((c.get("avg") or 0) * len(c.get("cores") or []) for c in clusters) / n_cores
        out.append((avg / 100, f"{avg:.0f}%", f"{n_cores} cores", "CPU", None))
    vm = (state.get("mem") or {}).get("vm")
    if vm is not None and getattr(vm, "total", 0):
        used = vm.total - getattr(vm, "available", vm.total - getattr(vm, "used", 0))
        out.append((vm.percent / 100, f"{vm.percent:.0f}%",
                    f"{used / 2**30:.0f}/{vm.total / 2**30:.0f} GiB", "MEMORY", None))
    gpus = state.get("gpus") or []
    caps = (state.get("caps") or {}).get("gpu") or []
    for g in gpus:
        u = g.get("util")
        if isinstance(u, (int, float)):
            out.append((u / 100, f"{u:.0f}%", _short_gpu_name(g.get("name")), f"GPU {g.get('id', '?')} UTIL", None))
    for i, g in enumerate(gpus):
        c = caps[i] if i < len(caps) else {}
        watts = _watts(g.get("pwr_str"))
        limit = (g.get("power_limit_mw") or 0) / 1000
        if c.get("power_limit") and watts is not None and limit > 0:
            out.append((watts / limit, f"{watts:.0f} W", f"of {limit:.0f} W", f"GPU {g.get('id', '?')} POWER", (0.8, 0.95)))
    rails = {r.get("label"): r for r in state.get("power_rails") or []}
    sysw = (rails.get("dc_input") or rails.get("sys_total") or {}).get("watts")
    sys_cap = (rails.get("syspl1") or {}).get("cap_w")
    if sysw is not None and sys_cap:
        out.append((sysw / sys_cap, f"{sysw:.0f} W", f"of {sys_cap:.0f} W", "SYSTEM POWER", (0.8, 0.95)))
    fab = [n for n in state.get("nics") or [] if n.get("up") and (n.get("speed_mbit") or 0) >= 25000]
    if fab:
        cap = sum(n.get("speed_mbit") or 0 for n in fab) * 1e6
        rate = sum(max(n.get("rx_bps") or 0, n.get("tx_bps") or 0) for n in fab)
        out.append((rate / cap if cap else 0, term.fmt_rate(rate, "bit"), f"of {cap / 1e9:.0f} Gb/s", "FABRIC", None))
    temps = []
    for t in state.get("thermal") or []:
        if isinstance(t.get("temp_c"), (int, float)):
            temps.append((t["temp_c"], str(t.get("label") or "sensor")))
    for g in gpus:
        if isinstance(g.get("temp"), (int, float)):
            temps.append((float(g["temp"]), f"GPU {g.get('id', '?')}"))
    if temps:
        t, where = max(temps)
        out.append((t / 100, term.fmt_temp(t), where[:12], "HOTTEST", (0.8, 0.9)))
    disks = [d for d in state.get("disks") or [] if isinstance(d.get("temp"), (int, float))]
    if disks:
        d = max(disks, key=lambda d: d["temp"])
        out.append((d["temp"] / 80, term.fmt_temp(d["temp"]), str(d.get("name"))[:12], "DISK TEMP", (0.75, 0.875)))
    used = [d for d in state.get("disks") or [] if isinstance(d.get("used_pct"), (int, float))]
    if used:
        d = max(used, key=lambda d: d.get("size") or 0)
        out.append((d["used_pct"] / 100, f"{d['used_pct']:.0f}%", str(d.get("name"))[:12], "DISK USED", (0.85, 0.95)))
    return out


def _watts(text):
    try:
        return float(str(text).split()[0])
    except Exception:
        return None


def _build_dial_strip(y, x0, width, dials, dial_w, title_right, measure=False, title="AT A GLANCE"):
    per = dial_w + 6
    fit = dials[:max(1, (width - 4) // per)]
    if not fit:
        return None
    if measure:
        p = panels.Panel(y, x0, width, kind="top")
        for _ in range(term.dial_size(dial_w)):
            p.add_row([])
        return p
    rendered = [term.dial(f, dial_w, v, s, lab, warn) for f, v, s, lab, warn in fit]
    if any(r is None for r in rendered):
        return None
    p = panels.Panel(y, x0, width, title=[(title, 9)], title_right=title_right, kind="top")
    slot_w = (width - 2) // len(fit)
    for r in range(term.dial_size(dial_w)):
        segs = []
        for i, rows in enumerate(rendered):
            col = 1 + i * slot_w + (slot_w - dial_w) // 2
            segs += _row(col, rows[r])
        p.add_row(segs)
    return p


# --- CPU + MEMORY ---------------------------------------------------------

def _build_cpu(y, x0, w, state, long, cpu_h, mem_h, heat_rows, span, measure):
    cpu = state.get("cpu") or {}
    clusters = cpu.get("clusters") or []
    out = []
    summary = " + ".join(f"{c.get('label', 'CPU')} ×{len(c.get('cores') or [])}" for c in clusters)
    right = []
    temp = cpu.get("temp")
    if isinstance(temp, (int, float)):
        right.append((term.fmt_temp(temp), 2))
    la = cpu.get("loadavg")
    if la:
        right.append(((" · " if right else "") + "load " + " ".join(f"{v:.2f}" for v in la[:3]), 2))
    p = panels.Panel(y, x0, w, title=[("CPU", 9), (f" {summary}", 3)], title_right=right, kind="top")
    inner = w - 2
    long_cpu = (long or {}).get("cpu") or []
    for i, cl in enumerate(clusters):
        hist = long_cpu[i] if i < len(long_cpu) and long_cpu[i] else cl.get("history") or []
        win = _window(hist, span)
        avg = cl.get("avg") or 0
        bar, slot = term.make_bar(avg, max(10, min(30, inner // 6)))
        lb, rb = term.bar_brackets()
        runs = [(f"{cl.get('label', 'CPU'):<13}", 3), (f"{cl.get('ghz', 0) or 0:>5.2f} GHz  ", 8),
                (lb, 10), (bar, slot), (rb, 10), (f" {avg:4.0f}%", 3)]
        if cl.get("range"):
            runs.append((f"  cores {cl['range']}", 2))
        strip = term.core_strip(cl.get("loads") or [])
        if strip:
            runs.append(("  " + strip, 1))
        peak, mean = _peak_avg_pct(win)
        p.add_row(_row(1, runs), right=[("peak ", 6), (f"{peak:.0f}%", 3), ("  avg ", 6), (f"{mean:.0f}%", 3)])
        for r in _chart_rows(win, inner - 1, cpu_h, 100, span, _pct_labels(cpu_h), measure):
            p.add_row(r)
    if clusters:
        p.add_row(_axis_row(inner - 1, span, measure))
    cores = (long or {}).get("core") or {}
    if heat_rows and cores:
        # Every core or none: a partial list reads as missing sensors. Short
        # on rows, the cores wrap into side-by-side columns, read downwards.
        idxs = sorted(cores)
        ncol = -(-len(idxs) // heat_rows)
        cw = (inner - 1) // ncol
        digits = len(str(idxs[-1]))
        p.add_row([(1, "per-core load", 6)], right=[("· idle ", 7), ("▆", 12), ("▆", 15), (" busy", 6)])
        for r in range(heat_rows):
            if measure:
                p.add_row([])
                continue
            segs = []
            for c in range(ncol):
                k = c * heat_rows + r
                if k >= len(idxs):
                    break
                x = 1 + c * cw
                cells = (cw - 2 if c < ncol - 1 else inner - x) - (6 + digits)
                runs = term.heat_runs(_window(cores[idxs[k]], span), cells, window=_w(span)) or []
                segs += [(x, f"core {idxs[k]:>{digits}} ", 6)] + _row(None, runs)
            p.add_row(segs)
    out.append(p)
    y += p.total_height

    mem_state = state.get("mem") or {}
    gpu_caps = (state.get("caps") or {}).get("gpu") or []
    mp = pages._build_memory_panel(y, x0, w, mem_state, gpu_caps, "wide", True)
    hist = (long or {}).get("mem") or mem_state.get("history") or []
    if mem_h and hist:
        for r in _chart_rows(_window(hist, span), inner - 1, mem_h, 100, span, {0: "100%", mem_h - 1: "0"}, measure):
            mp.add_row(r)
    out.append(mp)
    return out, sum(q.total_height for q in out) + 1


# --- GPUs -------------------------------------------------------------------

def _build_gpus(y, x0, w, state, long, gpu_h, pwr_h, span, measure):
    gpus = state.get("gpus") or []
    caps = (state.get("caps") or {}).get("gpu") or []
    inner = w - 2
    out = []
    lg = (long or {}).get("gpu") or {}
    lp = (long or {}).get("gpu_pwr") or {}
    for i, g in enumerate(gpus):
        c = caps[i] if i < len(caps) else {}
        p = pages._build_gpu_card(y, x0, w, g, c, False)
        if i:
            p.kind = "mid"
        gid = g.get("id")
        hist = lg.get(gid) or g.get("history") or []
        for r in _chart_rows(_window(hist, span), inner - 1, gpu_h, 100, span, _pct_labels(gpu_h), measure):
            p.add_row(r)
        phist = lp.get(gid) or []
        if pwr_h and len(phist) >= 2:
            limit = (g.get("power_limit_mw") or 0) / 1000 or max(phist) * 1.1 or 1
            for r in _chart_rows(_window(phist, span), inner - 1, pwr_h, limit, span,
                                 {0: f"{limit:.0f}W", pwr_h - 1: "0"}, measure):
                p.add_row(r)
        if i == len(gpus) - 1:
            p.add_row(_axis_row(inner - 1, span, measure))
        out.append(p)
        y += p.total_height
    return out, (sum(q.total_height for q in out) + 1) if out else 0


# --- POWER & THERMAL (xl) -------------------------------------------------------

def _build_power_thermal(y, x0, w, state, long, rail_chart_h, span, measure, fill_to):
    rails = [r for r in state.get("power_rails") or []
             if r.get("watts") is not None and r.get("label") not in pages._CONTROLLER_ORDER]
    thermal = [t for t in state.get("thermal") or [] if isinstance(t.get("temp_c"), (int, float))]
    if not rails and not thermal:
        return [], 0
    inner = w - 2
    lr = (long or {}).get("rail") or {}
    lt = (long or {}).get("temp") or {}
    p = panels.Panel(y, x0, w, title=[("POWER", 9), (" & ", 3), ("THERMAL", 9)], kind="top")
    if rails:
        order = {k: i for i, k in enumerate(pages._RAIL_ORDER)}
        rails.sort(key=lambda r: (order.get(r["label"], 99), r["label"]))
        top = max(r["watts"] for r in rails) or 1
        p.add_row([(1, "rails", 6)], right=[("W, last " + term.fmt_span(span), 6)])
        for r in rails:
            cap = r.get("cap_w") or top
            runs = [(f"{r['label']:<10}", 3)] + _bar_runs(min(100, r["watts"] / cap * 100), 16, 2) + \
                [(f" {_fmt_watts(r['watts']):>7}", 8)]
            hist = lr.get(r["label"]) or []
            cw = inner - 1 - 1 - 36
            if not measure and cw >= 8 and len(hist) >= 2:
                chart = term.area_chart(_window(hist, span), cw, 1, y_max=max(hist) or 1, window=_w(span))
                if chart:
                    runs += [("  ", 3)] + chart[0]
            p.add_row(_row(1, runs))
        main = next((r for r in rails if r["label"] in ("dc_input", "sys_total")), None)
        if main and rail_chart_h and len(lr.get(main["label"]) or []) >= 2:
            hist = _window(lr[main["label"]], span)
            ymax = max(hist) * 1.15 or 1
            p.add_row([(1, f"{main['label']} · last {term.fmt_span(span)}", 6)])
            for row in _chart_rows(hist, inner - 1, rail_chart_h, ymax, span,
                                   {0: f"{ymax:.0f}W", rail_chart_h - 1: "0"}, measure):
                p.add_row(row)
            p.add_row(_axis_row(inner - 1, span, measure))
    out = [p]
    used = p.total_height
    if thermal:
        tp = panels.Panel(y + used, x0, w, title=[("THERMAL", 9), (" every sensor, hottest first", 3)],
                          kind="mid" if rails else "top")
        thermal.sort(key=lambda t: -t["temp_c"])
        room = max(0, fill_to - used - 2) if fill_to else len(thermal)
        for t in thermal[:room]:
            key = f"{t.get('hwname') or t.get('kind')}:{t.get('label')}"
            hw, lab = str(t.get("hwname") or ""), str(t.get("label") or "")
            name = (lab if not hw or lab.startswith(hw) else f"{hw} {lab}")[:20]
            tc = t["temp_c"]
            slot = 4 if tc >= 90 else 5 if tc >= 80 else 8
            runs = [(f"{name:<21}", 3), (f"{term.fmt_temp(tc):>6}", slot)]
            hist = lt.get(key) or []
            cw = inner - 1 - 30
            if not measure and cw >= 8 and len(hist) >= 2:
                chart = term.area_chart([max(0.0, v - 20) for v in _window(hist, span)], cw, 1, y_max=80, window=_w(span))
                if chart:
                    runs += [("  ", 3)] + chart[0]
            tp.add_row(_row(1, runs))
        out.append(tp)
        used += tp.total_height
    return out, used + 1


# --- NETWORK + STORAGE -----------------------------------------------------------

_INFO_W = 44
_SCALE_W = 11


def _net_scale(v, mode):
    if mode == "byte":
        return f"{v / 1e9:.2f} GB/s" if v >= 1e8 else f"{v / 1e6:.0f} MB/s"
    return f"{v / 1e9:.1f} Gb/s" if v >= 1e8 else f"{v / 1e6:.0f} Mb/s"


def _mirror_block(p, inner, info_rows, up, down, half, span, floor, mode, measure):
    """Info rows on the left, a mirrored chart (`half` rows each way) on the
    right with its scale at both ends. Adds max(len(info_rows), 2*half)."""
    n_rows = max(len(info_rows), 2 * half)
    cw = inner - 1 - _INFO_W - 2 - _SCALE_W - 1
    chart = None
    top = floor
    if not measure and cw >= 8 and len(up) >= 2 and len(down) >= 2:
        n = cw * 2
        u, d = term.fit_window(up, _w(span), n), term.fit_window(down, _w(span), n)
        top = max(max(u), max(d), floor)
        chart = term.mirror_chart(u, d, cw, half_rows=half, up_floor=top, down_floor=top)
    for r in range(n_rows):
        segs = list(info_rows[r]) if r < len(info_rows) else []
        if chart and r < len(chart):
            segs += _row(1 + _INFO_W + 2, chart[r])
            if r == 0 or r == len(chart) - 1:
                segs.append((1 + _INFO_W + 3 + cw, _net_scale(top, mode), 6))
        p.add_row(segs)


def _build_net(y, x0, w, state, long, net_half, disk_half, span, measure):
    nics = state.get("nics") or []
    disks = state.get("disks") or []
    inner = w - 2
    ln = (long or {}).get("nic") or {}
    ld = (long or {}).get("disk") or {}
    net_caps = (state.get("caps") or {}).get("net") or {}
    if net_caps.get("port_counting"):
        caption = "physical ports · port MAC counters, RDMA + kernel"
    elif net_caps.get("rdma"):
        caption = "physical ports · port counters, RDMA + kernel"
    else:
        caption = "physical ports · kernel traffic"
    p = panels.Panel(y, x0, w, title=[("NETWORK", 9), (f" {caption}", 3)],
                     title_right=[("▼ in rises · ▲ out hangs below", 6)], kind="top")
    any_chart = False
    for n in nics:
        name = str(n.get("name", "?"))
        label = f"{name[:13]:<14}{str(n.get('hw_label') or '')[:22]}"
        if n.get("pf_count", 1) and (n.get("pf_count") or 1) > 1:
            label += f" ×{n['pf_count']} PF"
        head = _row(1, [(f"{label:<{_INFO_W - 6}}", 8), (f"{n.get('speed_str', ''):>6}", 2)])
        if not n.get("up"):
            p.add_row(_row(1, [(f"{label:<{_INFO_W - 6}}", 6), (f"{n.get('label') or 'Link Down':>10}", 6)]))
            continue
        h = ln.get(name) or {"rx": n.get("rx_history") or [], "tx": n.get("tx_history") or []}
        rx, tx = h.get("rx") or [], h.get("tx") or []
        info = [head,
                _row(1, [("▼ ", 1), (f"{term.fmt_rate(n.get('rx_bps', 0), 'bit'):>12}", 8),
                         (f"  {int(n.get('rx_pct', 0) or 0)}%", 3)]),
                _row(1, [("▲ ", 5), (f"{term.fmt_rate(n.get('tx_bps', 0), 'bit'):>12}", 8),
                         (f"  {int(n.get('tx_pct', 0) or 0)}%", 3)])]
        if net_half >= 2 and rx and tx:
            win_r, win_t = _window(rx, span), _window(tx, span)
            info.append(_row(1, [("peak ", 6), (f"▼ {term.fmt_rate(max(win_r), 'bit')}  ▲ {term.fmt_rate(max(win_t), 'bit')}", 3)]))
        floor = (n.get("speed_mbit") or 0) * 1e6 * 0.01 or 1e6
        _mirror_block(p, inner, info, rx, tx, net_half, span, floor, "bit", measure)
        any_chart = True
    if any_chart:
        p.add_row(_row(1 + _INFO_W + 2, [] if measure else term.time_axis(inner - 1 - _INFO_W - 2 - _SCALE_W - 1, span)))
    out = [p]
    y += p.total_height
    if disks:
        sp = panels.Panel(y, x0, w, title=[("STORAGE", 9), (" read rises · write hangs below", 3)], kind="mid")
        for d in disks:
            name = str(d.get("name", "?"))
            size = term.fmt_disk_size(d.get("size")) if d.get("size") else ""
            temp = d.get("temp")
            head = _row(1, [(f"{name[:10]:<11}", 8), (f"{str(d.get('model') or '')[:20]} ", 3), (size, 6)])
            if isinstance(temp, (int, float)):
                head.append((1 + _INFO_W - 6, f"{term.fmt_temp(temp):>6}", 3))
            info = [head,
                    _row(1, [("R ", 1), (f"{term.fmt_rate(d.get('read_bps', 0), 'byte'):>11}", 8),
                             ("   W ", 5), (f"{term.fmt_rate(d.get('write_bps', 0), 'byte'):>11}", 8)])]
            if isinstance(d.get("used_pct"), (int, float)):
                info.append(_row(1, _bar_runs(d["used_pct"], 20, 2) + [(f" {d['used_pct']:.0f}% used", 3)]))
            h = ld.get(name) or {"r": d.get("read_history") or [], "w": d.get("write_history") or []}
            _mirror_block(sp, inner, info, h.get("r") or [], h.get("w") or [], disk_half, span, 1e6, "byte", measure)
        out.append(sp)
    return out, sum(q.total_height for q in out) + 1


# --- assembly -------------------------------------------------------------------

def _columns(state, width, layout):
    """(has_gpus, has_pt, column widths) of the body for this layout."""
    has_gpus = bool(state.get("gpus"))
    has_pt = layout == "xl" and bool(state.get("power_rails") or state.get("thermal"))
    n_cols = 1 if layout == "tall" else (2 + (1 if has_pt else 0))
    if not has_gpus and n_cols > 1:
        n_cols -= 1
    col_w = width // n_cols
    return has_gpus, has_pt, [col_w] * (n_cols - 1) + [width - col_w * (n_cols - 1)]


_HEAT_MIN_CELLS = 16   # history cells per core, the least that reads as a trend


def _heat_steps(n_cores, col_w):
    """Row counts the heatmap may take: each one shows every core, in as
    few side-by-side columns as that many rows needs, each column at
    least _HEAT_MIN_CELLS wide. Ascending; empty when none fits."""
    inner = col_w - 2
    digits = len(str(max(0, n_cores - 1)))
    steps = set()
    for ncol in range(1, n_cores + 1):
        if (inner - 1) // ncol - 2 - (6 + digits) < _HEAT_MIN_CELLS:
            break
        steps.add(-(-n_cores // ncol))
    return sorted(steps)


def _assemble(state, width, height, x0, layout, span, v, measure, dials_on, header_kw, footer_kw, graphs, pad=0):
    """One pass: every panel at the chart heights in `v`. Returns (panels,
    rows used including header and footer)."""
    long = state.get("long")
    out = [pages._build_header(x0, width, "wide", **header_kw)]
    y = 1
    if dials_on:
        dial_w = 18 if layout == "xl" else 16
        strip = _build_dial_strip(y, x0, width, _dials(state, long), dial_w,
                                  [("history ", 6), (term.fmt_span(span), 8), ("  graphs ", 6), (graphs, 8)], measure)
        if strip is not None:
            out.append(strip)
            y += strip.total_height + 1
    has_gpus, has_pt, widths = _columns(state, width, layout)
    n_cols = len(widths)
    xs = [x0 + i * widths[0] for i in range(n_cols)]

    cpu_p, cpu_h = _build_cpu(y, xs[0], widths[0], state, long, v["cpu"], v["mem"],
                              v["heat"] if layout == "xl" else 0, span, measure)
    blocks = [(cpu_p, cpu_h)]
    if layout == "tall":
        gy = y + cpu_h
        g_p, g_h = _build_gpus(gy, x0, width, state, long, v["gpu"], v["pwr"], span, measure)
        body_h = cpu_h + g_h
        blocks.append((g_p, g_h))
    else:
        col = 1
        if has_gpus:
            g_p, g_h = _build_gpus(y, xs[col], widths[col], state, long, v["gpu"], v["pwr"], span, measure)
            blocks.append((g_p, g_h))
            col += 1
        body_h = max(h for _, h in blocks)
        if has_pt and col < n_cols:
            pt_p, pt_h = _build_power_thermal(y, xs[col], widths[col], state, long, v["rail"], span, measure, body_h)
            blocks.append((pt_p, pt_h))
            body_h = max(body_h, pt_h)
        # equal bottoms: pad each column's last panel to the tallest
        for ps, h in blocks:
            if ps and h < body_h:
                for _ in range(body_h - h):
                    ps[-1].add_row([])
    for ps, _ in blocks:
        out.extend(ps)
    y += body_h
    net_p, net_h = _build_net(y, x0, width, state, long, v["net"], v["disk"], span, measure)
    # rows no chart could take (every chart grows by a row or more at a time)
    for _ in range(pad):
        net_p[-1].add_row([])
    net_h += pad
    out.extend(net_p)
    y += net_h
    used = y + 1
    if not measure:
        out.append(pages._build_footer(x0, width, height - 1, **footer_kw))
    return out, used


_MIN = {"cpu": 3, "mem": 0, "gpu": 3, "pwr": 0, "net": 1, "disk": 1, "heat": 0, "rail": 0}
_TALL_MIN = {"cpu": 2, "gpu": 2}
_MAX = {"cpu": 12, "mem": 4, "gpu": 16, "pwr": 6, "net": 4, "disk": 3, "rail": 10}
_GROW = ["gpu", "cpu", "net", "pwr", "heat", "heat", "heat", "heat", "mem", "rail", "gpu", "cpu", "disk"]


def build_page1_large(state, width, height, x0=0, layout="large", history=None, graphs="auto",
                      theme_toast=None, remote=None, cluster_tabs=False, has_perf=False):
    """Page 1 for a big terminal: exactly `height` rows. None when even the
    minimum layout can't fit (the caller then draws the normal page 1)."""
    if term.BAR_STYLE != "eighths":
        return None
    span = history or DEFAULT_HISTORY.get(layout, 300)
    try:
        _RATE[0] = float(state.get("rate") or 1.0)
    except Exception:
        _RATE[0] = 1.0
    header_kw = {"cluster_tabs": cluster_tabs, "remote": remote, "has_perf": has_perf}
    rate = state.get("rate", 1.0)
    footer_kw = {"driver": state.get("driver", "Unknown"), "cuda": state.get("cuda", "Unknown"), "rate": rate,
                 "has_nvml": (state.get("caps") or {}).get("has_nvml", False),
                 "knob_ui": {"toast": theme_toast} if theme_toast else None}
    key = (width, height, layout, span, _shape(state))
    cached = _FIT_CACHE.get(key, False)
    if cached is None:
        return None
    if cached:
        v, dials_on, pad = cached
        built, _ = _assemble(state, width, height, x0, layout, span, v, False, dials_on, header_kw, footer_kw, graphs, pad)
        return built
    v = dict(_MIN, **(_TALL_MIN if layout == "tall" else {}))
    dials_on = layout in ("large", "xl")

    def fits(vv, d):
        return _assemble(state, width, height, x0, layout, span, vv, True, d, header_kw, footer_kw, graphs)[1] <= height

    if not fits(v, dials_on):
        dials_on = False
        if not fits(v, dials_on):
            _FIT_CACHE[key] = None
            return None
    heat_steps = []
    if layout == "xl":
        cores = (state.get("long") or {}).get("core") or {}
        heat_steps = _heat_steps(len(cores), _columns(state, width, layout)[2][0]) if cores else []
    grown = True
    while grown:
        grown = False
        for k in _GROW:
            if k == "heat":
                nxt = [r for r in heat_steps if r > v[k]]
                if not nxt:
                    continue
                trial = dict(v, heat=nxt[0])
            else:
                if v[k] >= _MAX[k]:
                    continue
                step = 1
                if k in ("mem", "pwr", "rail") and v[k] == 0:
                    step = 2   # a 1-row chart under its own label reads as noise
                trial = dict(v, **{k: v[k] + step})
            if fits(trial, dials_on):
                v = trial
                grown = True
    if len(_FIT_CACHE) > 32:
        _FIT_CACHE.clear()
    used = _assemble(state, width, height, x0, layout, span, v, True, dials_on, header_kw, footer_kw, graphs)[1]
    pad = max(0, height - used)
    _FIT_CACHE[key] = (v, dials_on, pad)
    built, _ = _assemble(state, width, height, x0, layout, span, v, False, dials_on, header_kw, footer_kw, graphs, pad)
    return built


# The fitting search builds the page dozens of times without charts; its
# answer depends only on the terminal size and on how many rows each panel
# has, so it is cached on that shape and redone only when the shape changes.
_FIT_CACHE = {}


def _shape(state):
    long = state.get("long") or {}
    gpus = state.get("gpus") or []
    return (
        len((state.get("cpu") or {}).get("clusters") or []),
        tuple((bool(g.get("pcie_stuck")), len((long.get("gpu_pwr") or {}).get(g.get("id")) or []) >= 2) for g in gpus),
        tuple(bool(n.get("up")) for n in state.get("nics") or []),
        tuple(isinstance(d.get("used_pct"), (int, float)) for d in state.get("disks") or []),
        len(state.get("power_rails") or []), len(state.get("thermal") or []),
        len(long.get("core") or {}), bool(long.get("mem") or (state.get("mem") or {}).get("history")),
        len(pages._build_memory_panel(0, 0, 200, state.get("mem") or {},
                                      (state.get("caps") or {}).get("gpu") or [], "wide", True).rows),
    )
