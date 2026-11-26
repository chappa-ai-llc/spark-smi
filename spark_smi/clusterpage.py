"""Page 3's big-screen layout (#14 step 3): the cluster on 200+ columns.

The normal page 3 (pages.build_page3) lists the cluster as one row per node
per section, with 60-second sparklines. On a big terminal this module draws
instead, top to bottom:

  CLUSTER     a dial strip: GPU average, slowest GPU, power, memory,
              fabric, hottest sensor, disk, agents answering
  TOPOLOGY    topomap's map, at the largest size that still leaves the
              charts room
  one column per node (up to four across, two rows for 5-8 nodes): every
              GPU's utilisation chart, CPU, node power and the fabric's
              mirrored in/out chart, on one time axis, then the busiest GPU
              process
  GPU UTIL    a heat grid, one row per GPU in the cluster: a row that lags
              the rest is a straggler

History is the viewer's own: cluster.NodeHistory fills from every poll and
never goes on the wire. Until it holds more than the 60 points an agent's
sample carries, a chart draws those. Heights are worked out by arithmetic,
so the page fills the terminal exactly: every chart starts at a floor and
grows a row at a time. Nothing fits, more than MAX_NODES nodes, or no
braille: build_page3_large() returns None and page 3 keeps its normal
layout. Panels only, no curses calls, every colour through a slot.
"""
import math
import time

from . import bigpage, cluster, pages, panels, term, topomap

MAX_NODES = 16
MAX_GRID_ROWS = 4
COL_MIN_W = 56          # a node column narrower than this cannot hold its charts
DIAL_W = bigpage.DIAL_W
# The `v` key's page-3 views (#14 step 5).
VIEWS = ("map+graphs", "map", "heat")
HEAT_THICK_MAX = 3      # the heat view draws each series up to this many rows tall
TOP_PROCS = 3

# Chart heights in rows ("fab" is each half of the mirror), the number of
# process lines ("procs") and the heat grid's level ("heat": 0 none, 1 GPU
# rows, 2 + fabric rows, 3 + CPU rows). Optional charts (pwr, mem, temp)
# open at 2 rows: a one-row chart under its own label reads as noise.
_FLOOR = {"gpu": 6, "cpu": 3, "pwr": 2, "fab": 2, "mem": 0, "temp": 0, "procs": 1, "heat": 1}
_MIN = {"gpu": 3, "cpu": 2, "pwr": 0, "fab": 1, "mem": 0, "temp": 0, "procs": 1, "heat": 0}
_MAX = {"gpu": 20, "cpu": 8, "pwr": 6, "fab": 6, "mem": 6, "temp": 6, "procs": TOP_PROCS, "heat": 3}
_GROW = ["gpu", "cpu", "fab", "pwr", "gpu", "heat", "procs", "mem", "temp", "gpu", "heat", "fab", "cpu",
         "procs", "gpu", "mem", "temp", "pwr"]
_OPEN_AT_2 = ("pwr", "mem", "temp")

_LW = bigpage._LABEL_W


# --- per-node facts -----------------------------------------------------------

def _series(view, key, fallback):
    """The viewer's long history for `key`, or the sample's own short one
    while that is longer (a viewer that has just started)."""
    long = []
    hist = view.get("hist")
    if hist is not None:
        try:
            long = hist.series(key)
        except Exception:
            long = []
    short = list(fallback or [])
    return long if len(long) >= len(short) else short


def _sum_aligned(lists):
    """Element-wise sum of series that all end now (lengths may differ)."""
    lists = [list(x) for x in lists if x]
    if not lists:
        return []
    n = max(len(x) for x in lists)
    out = [0.0] * n
    for x in lists:
        off = n - len(x)
        for i, v in enumerate(x):
            out[off + i] += float(v or 0)
    return out


def _top_procs(sample, n):
    """The node's `n` GPU processes using the most GPU memory, busiest
    first; a process on two GPUs counts once, with its larger figure."""
    best = {}
    for g in sample.get("gpus") or []:
        for p in g.get("procs") or []:
            pid = p.get("pid")
            if pid not in best or (p.get("mem") or 0) > (best[pid].get("mem") or 0):
                best[pid] = p
    return sorted(best.values(), key=lambda p: -(p.get("mem") or 0))[:n]


def node_info(view, m):
    """Everything a node's column, the dials and the heat grid read."""
    s = view.get("sample") or {}
    up = bool(s) and not view.get("stale") and m is not None
    info = {"name": view.get("name") or "?", "view": view, "m": m, "up": up, "gpus": []}
    if not up:
        return info
    for g in s.get("gpus") or []:
        gid = g.get("id")
        info["gpus"].append({"id": gid, "label": bigpage._short_gpu_name(g.get("name")),
                             "util": g.get("util"), "temp": g.get("temp"), "pwr": g.get("pwr_str"),
                             "limit": (g.get("power_limit_mw") or 0) / 1000,
                             "hist": _series(view, f"gpu:{gid}", g.get("history"))})
    clusters = (s.get("cpu") or {}).get("clusters") or []
    info["cores"] = sum(len(c.get("cores") or []) for c in clusters)
    info["cpu_hist"] = _series(view, "cpu", m.get("cpu_hist")) if clusters else []
    watts, src = cluster.node_power(s)
    info["pwr"], info["pwr_src"] = watts, src
    info["pwr_hist"] = _series(view, "pwr", []) if watts is not None else []
    fab = cluster.fabric_nics(s.get("nics"))
    info["fab"] = bool(fab)
    info["rx"] = sum(n.get("rx_bps") or 0 for n in fab)
    info["tx"] = sum(n.get("tx_bps") or 0 for n in fab)
    info["fab_cap"] = sum((n.get("speed_mbit") or 0) for n in fab) * 1e6
    info["rx_hist"] = _series(view, "rx", _sum_aligned([n.get("rx_history") for n in fab]))
    info["tx_hist"] = _series(view, "tx", _sum_aligned([n.get("tx_history") for n in fab]))
    info["procs"] = _top_procs(s, TOP_PROCS)
    info["mem_hist"] = _series(view, "mem", m.get("mem_hist")) if m.get("mem_total") else []
    temp = m.get("max_temp")
    info["temp"] = temp if isinstance(temp, (int, float)) else None
    info["temp_hist"] = _series(view, "temp", []) if info["temp"] is not None else []
    return info


def _enough(series):
    return len([v for v in series or [] if v is not None]) >= 2


def _has_pwr_chart(info):
    return _enough(info.get("pwr_hist"))


def gpu_heights(n_g, budget):
    """Chart heights for a node's `n_g` GPUs from the layout's GPU budget:
    the node's GPU rows, titles included, stay at 1 + budget whatever the
    count (each chart at least _MIN's), so a node with an eGPU does not pay
    double for every row and starve the optional charts."""
    if n_g <= 0:
        return []
    each, rest = divmod(max(0, budget - (n_g - 1)), n_g)
    return [max(_MIN["gpu"], each + (1 if i < rest else 0)) for i in range(n_g)]


def column_rows(info, v):
    """Content rows of a node's column at chart heights `v` -- the same
    count _build_column adds, so the fitting search needs no drawing."""
    if not info["up"]:
        return 2
    r = sum(1 + h for h in gpu_heights(len(info["gpus"]), v["gpu"])) or 1
    r += 1 + v["cpu"]
    if v["mem"] and _enough(info.get("mem_hist")):
        r += 1 + v["mem"]
    if v["temp"] and _enough(info.get("temp_hist")):
        r += 1 + v["temp"]
    if v["pwr"] and _has_pwr_chart(info):
        r += 1 + v["pwr"]
    if info["fab"]:
        r += 1 + 2 * v["fab"]
    return r + 1 + max(1, min(v["procs"], len(info.get("procs") or [])))   # time axis, processes


def grid_for(n, width):
    """Node columns per row: the fewest rows (up to MAX_GRID_ROWS) whose
    columns are at least COL_MIN_W wide, nodes spread evenly over them. One
    row of more than four nodes needs 64 columns each."""
    for rows in range(1, MAX_GRID_ROWS + 1):
        per = math.ceil(n / rows)
        need = 64 if (rows == 1 and n > 4) else COL_MIN_W
        if width // per >= need:
            return per
    return math.ceil(n / MAX_GRID_ROWS)


def _fabric_pct(info):
    """Fabric use per poll as % of the node's fabric capacity (the busier
    direction), for the heat grid."""
    cap = info.get("fab_cap") or 0
    if not cap:
        return []
    rx, tx = list(info.get("rx_hist") or []), list(info.get("tx_hist") or [])
    n = max(len(rx), len(tx))
    rx, tx = [None] * (n - len(rx)) + rx, [None] * (n - len(tx)) + tx
    return [None if a is None and b is None else max(a or 0, b or 0) / cap * 100 for a, b in zip(rx, tx)]


def heat_rows(infos, level):
    """[(label, series), ...] for the heat grid: GPU rows (level 1+), then
    one fabric row per node (2+), then one CPU row per node (3)."""
    rows = []
    for info in infos:
        multi = len(info["gpus"]) > 1
        for g in info["gpus"]:
            label = f"{info['name']} gpu{g['id']}" if multi else f"{info['name']} gpu"
            rows.append((label, g["hist"]))
    if level >= 2:
        rows += [(f"{i['name']} fab", _fabric_pct(i)) for i in infos if i["up"] and i.get("fab_cap")]
    if level >= 3:
        rows += [(f"{i['name']} cpu", i["cpu_hist"]) for i in infos if i["up"] and _enough(i.get("cpu_hist"))]
    return rows if level else []


def heat_levels(infos):
    """The heat levels that add rows: GPU rows need two or more GPUs."""
    out = [0]
    gpu = heat_rows(infos, 1)
    if len(gpu) >= 2:
        out.append(1)
        n = len(gpu)
        for lvl in (2, 3):
            k = len(heat_rows(infos, lvl))
            if k > n:
                out.append(lvl)
                n = k
    return out


# --- dials ----------------------------------------------------------------------

def cluster_dials(infos, total):
    """[(frac, value, sub, label, warn), ...]: a dial only for a number at
    least one node reports."""
    up = [i for i in infos if i["up"]]
    out = []
    gpus = [(g, i["name"]) for i in up for g in i["gpus"] if isinstance(g.get("util"), (int, float))]
    if gpus:
        avg = sum(g["util"] for g, _ in gpus) / len(gpus)
        out.append((avg / 100, f"{avg:.0f}%", f"{len(gpus)} GPU{'s' if len(gpus) != 1 else ''}", "GPU AVG", None))
    if len(gpus) >= 2:
        g, where = min(gpus, key=lambda t: t[0]["util"])
        out.append((g["util"] / 100, f"{g['util']:.0f}%", where[:12], "SLOWEST GPU", None))
    pw = [i for i in up if i.get("pwr") is not None]
    if pw:
        watts = sum(i["pwr"] for i in pw)
        wall = all(i["pwr_src"] == "wall" for i in pw)
        cap = 0.0
        for i in pw:
            if wall:
                rails = {r.get("label"): r for r in (i["m"] or {}).get("power_rails") or []}
                cap += (rails.get("syspl1") or {}).get("cap_w") or 0
            else:
                cap += sum(g.get("limit") or 0 for g in i["gpus"])
        if cap > 0:
            out.append((watts / cap, f"{watts:.0f} W", f"of {cap:.0f} W", "WALL POWER" if wall else "GPU POWER",
                        (0.8, 0.95)))
    mem_t = sum((i["m"] or {}).get("mem_total") or 0 for i in up)
    mem_u = sum((i["m"] or {}).get("mem_used") or 0 for i in up)
    if mem_t:
        out.append((mem_u / mem_t, f"{mem_u / mem_t * 100:.0f}%", f"{mem_u / 2**30:.0f}/{mem_t / 2**30:.0f} GiB",
                    "MEMORY", None))
    cap = sum(i.get("fab_cap") or 0 for i in up)
    if cap:
        rate = sum(max(i.get("rx") or 0, i.get("tx") or 0) for i in up)
        out.append((rate / cap, term.fmt_rate(rate, "bit"), f"of {cap / 1e9:.0f} Gb/s", "FABRIC", None))
    temps = [((i["m"] or {}).get("max_temp"), i["name"]) for i in up]
    temps = [t for t in temps if isinstance(t[0], (int, float))]
    if temps:
        t, where = max(temps)
        out.append((t / 100, term.fmt_temp(t), where[:12], "HOTTEST", (0.8, 0.9)))
    out.append((sum(1 for i in infos if i["up"]) / max(1, total), f"{len(up)}/{total}",
                "all up" if len(up) == total else f"{total - len(up)} silent", "AGENTS UP", None))
    disks = [((i["m"] or {}).get("worst_disk_pct"), i["name"]) for i in up if (i["m"] or {}).get("disks")]
    disks = [d for d in disks if isinstance(d[0], (int, float))]
    if disks:
        d, where = max(disks)
        out.append((d / 100, f"{d:.0f}%", where[:12], "DISK USED", (0.85, 0.95)))
    return out


# --- panels -----------------------------------------------------------------------

def _axis_rate(bps):
    """A y-axis label for a bits/s scale, at most four cells: '200G'."""
    for unit, div in (("T", 1e12), ("G", 1e9), ("M", 1e6), ("K", 1e3)):
        if bps >= div:
            v = bps / div
            return f"{v:.0f}{unit}" if v >= 10 else f"{v:.1f}{unit}"
    return "0"


def _vals(series):
    return [float(x) for x in series if x is not None]


def _fmt_w(w):
    return f"{w:.0f} W" if w >= 10 else f"{w:.1f} W"


def _build_column(y, x, w, info, v, span, selected, alerts, extra=0):
    """One node's column. `extra` rows (a shorter column beside a taller
    one) go to its own charts -- the GPUs', else the CPU's -- so every
    column in a row ends on its content, not on blank rows."""
    iw = w - 2
    cw = iw - 1
    title = [("▶ " if selected else "", 9), (info["name"], 9)]
    if alerts:
        name, reason, sev = alerts[0]
        title.append((f" ▲ {reason}", 4 if sev == "red" else 5))
    right = []
    m = info["m"] or {}
    if info["up"]:
        if isinstance(m.get("max_temp"), (int, float)):
            right.append((term.fmt_temp(m["max_temp"]), 5 if m["max_temp"] >= 80 else 3))
        if info.get("pwr") is not None:
            right += [(" · " if right else "", 10), (_fmt_w(info["pwr"]), 3)]
    p = panels.Panel(y, x, w, title=title, title_right=right, kind="top")
    if not info["up"]:
        view = info["view"]
        why = (view.get("error") or "").strip()
        p.add_row([(1, ("— unreachable" + (f" ({why})" if why else ""))[:iw - 1], 6)])
        last = view.get("last_seen_summary")
        ok = view.get("last_ok_ts") or 0
        text = f"silent {term.fmt_span(max(0, int(time.time() - ok)))}" if ok else "never answered"
        if last:
            text += f" · last seen: {last}"
        p.add_row([(1, text[:iw - 1], 6)])
        return p
    W = bigpage._w(span)
    n_g = len(info["gpus"])
    heights = gpu_heights(n_g, v["gpu"])
    for gi, g in enumerate(info["gpus"]):
        gh = heights[gi] + extra // n_g + (1 if gi < extra % n_g else 0)
        win = bigpage._window(g["hist"], span)
        vals = _vals(win)
        avg = sum(vals) / len(vals) if vals else 0.0
        util = g.get("util")
        head = [(f"{g['label']}", 2), ("  ", 3),
                (f"{util:3.0f}%" if isinstance(util, (int, float)) else " N/A", 8),
                ("  avg ", 6), (f"{avg:.0f}%", 3), (f"  peak {max(vals):.0f}%" if vals else "", 6)]
        gr = []
        if isinstance(g.get("temp"), (int, float)):
            gr.append((term.fmt_temp(g["temp"]), 5 if g["temp"] >= 80 else 3))
        if g.get("pwr") and g["pwr"] != "N/A":
            gr += [(" · " if gr else "", 10), (str(g["pwr"]), 3)]
        p.add_row(bigpage._row(1, head), right=gr)
        for r in bigpage._chart_rows(win, cw, gh, 100, span, bigpage._pct_labels(gh), False):
            p.add_row(r)
    if not info["gpus"]:
        p.add_row([(1, "no GPU", 6)])
    # CPU, with memory on the same line
    cpu_now = (m.get("cpu_pct") or 0)
    head = [("CPU ", 6), (f"{cpu_now:3.0f}%", 8)]
    if info.get("cores"):
        head.append((f"  {info['cores']} cores", 6))
    mr = []
    if m.get("mem_total"):
        mr = [("MEM ", 6), (f"{m.get('mem_pct', 0):.0f}%", 8),
              (f" · {(m.get('mem_used') or 0) / 2**30:.0f}/{m['mem_total'] / 2**30:.0f} GiB", 3)]
    mem_chart = bool(v["mem"]) and _enough(info.get("mem_hist"))
    p.add_row(bigpage._row(1, head), right=[] if mem_chart else mr)
    ch = v["cpu"] + (0 if n_g else extra)
    for r in bigpage._chart_rows(bigpage._window(info["cpu_hist"], span), cw, ch, 100, span,
                                 bigpage._pct_labels(ch), False):
        p.add_row(r)
    if mem_chart:
        win = bigpage._window(info["mem_hist"], span)
        p.add_row(bigpage._row(1, mr), right=[("peak ", 6), (f"{max(_vals(win) or [0]):.0f}%", 3)])
        for r in bigpage._chart_rows(win, cw, v["mem"], 100, span, bigpage._pct_labels(v["mem"]), False):
            p.add_row(r)
    if v["temp"] and _enough(info.get("temp_hist")):
        win = bigpage._window(info["temp_hist"], span)
        t = info["temp"]
        p.add_row(bigpage._row(1, [("HOTTEST ", 6), (term.fmt_temp(t), 4 if t >= 90 else 5 if t >= 80 else 8),
                                   ("  CPU, GPUs, NIC", 6)]),
                  right=[("peak ", 6), (term.fmt_temp(max(_vals(win) or [t])), 3)])
        for r in bigpage._chart_rows(win, cw, v["temp"], 100, span,
                                     {0: term.fmt_temp(100).replace(" ", ""), v["temp"] - 1: "0"}, False):
            p.add_row(r)
    if v["pwr"] and _has_pwr_chart(info):
        win = bigpage._window(info["pwr_hist"], span)
        vals = _vals(win) or [0.0]
        ymax = max(max(vals) * 1.15, 1.0)
        src = "wall" if info["pwr_src"] == "wall" else "GPUs"
        p.add_row(bigpage._row(1, [("POWER ", 6), (_fmt_w(info["pwr"]), 8), (f"  {src}", 6)]),
                  right=[("peak ", 6), (_fmt_w(max(vals)), 3)])
        for r in bigpage._chart_rows(win, cw, v["pwr"], ymax, span,
                                     {0: f"{ymax:.0f}W", v["pwr"] - 1: "0"}, False):
            p.add_row(r)
    if info["fab"]:
        rx_w = bigpage._window(info["rx_hist"], span)
        tx_w = bigpage._window(info["tx_hist"], span)
        peak = max(_vals(rx_w) + _vals(tx_w) + [0.0])
        p.add_row(bigpage._row(1, [("FABRIC ", 6), ("▼ ", 1), (f"{term.fmt_rate(info['rx'], 'bit')}  ", 8),
                                   ("▲ ", 5), (term.fmt_rate(info["tx"], "bit"), 8)]),
                  right=[("peak ", 6), (term.fmt_rate(peak, "bit"), 3)])
        half = v["fab"]
        width = cw - _LW - 1
        chart = None
        top = max(peak, (info.get("fab_cap") or 0) * 0.01, 1e6)
        if width >= 8:
            n = width * 2
            up_ = term.fit_window(info["rx_hist"], W, n)
            dn_ = term.fit_window(info["tx_hist"], W, n)
            chart = term.mirror_chart(up_, dn_, width, half_rows=half, up_floor=top, down_floor=top)
        for r in range(2 * half):
            lab = _axis_rate(top) if r in (0, 2 * half - 1) else ""
            tick = "┤" if lab else "│"
            row = [(1, f"{lab:>{_LW - 1}}", 6), (None, tick, 10)]
            if chart:
                row += bigpage._row(None, chart[r])
            p.add_row(row)
    p.add_row(bigpage._axis_row(cw, span, False))
    procs = (info.get("procs") or [])[:max(1, v["procs"])]
    for k, proc in enumerate(procs):
        pname = (proc.get("name") or "").strip() or str(proc.get("pid", "?"))
        row = [("top " if k == 0 else "    ", 6), (pname[:max(8, iw - 40)], 3), (f" · pid {proc.get('pid', '?')}", 6)]
        if proc.get("mem"):
            row.append((f"  {term.fmt_mem(proc['mem'])}", 3))
        if isinstance(proc.get("sm"), (int, float)) and proc.get("sm"):
            row.append((f"  SM {proc['sm']:.0f}%", 3))
        p.add_row(bigpage._row(1, row))
    if not procs:
        p.add_row([(1, "no GPU processes", 6)])
    return p


_HEAT_TITLES = {
    1: [("GPU UTIL", 9), (" every GPU, one row each · a row that lags is a straggler", 3)],
    2: [("HEAT", 9), (" GPU use, then fabric (% of link speed) · a row that lags is a straggler", 3)],
    3: [("HEAT", 9), (" GPU use, fabric (% of link speed), CPU · a row that lags is a straggler", 3)],
}


def _build_heat(y, x0, width, rows, span, pad, level=1, thick=1):
    """`thick` rows per series (the heat view); the label sits on the first."""
    lw = min(24, max(len(lbl) for lbl, _ in rows)) + 1
    cells = width - 2 - 1 - lw
    p = panels.Panel(y, x0, width, title=_HEAT_TITLES.get(level, _HEAT_TITLES[1]),
                     title_right=[("· idle ", 7), ("▆", 12), ("▆", 15), (" busy", 6)], kind="top")
    for label, hist in rows:
        runs = term.heat_runs(bigpage._window(hist, span), cells, window=bigpage._w(span)) or []
        for k in range(thick):
            p.add_row([(1, f"{label[:lw - 1] if not k else '':<{lw}}", 6)] + bigpage._row(None, runs))
    p.add_row(bigpage._row(1 + lw, term.time_axis(cells, span)))
    for _ in range(pad):
        p.add_row([])
    return p


def _footer(x0, width, y, up, total, rate, span, has_perf, theme_toast):
    page_nums = "1 2 3 4 5" if has_perf else "1 2 3 5"
    keys = (f" q quit · ←→ select · enter drill into node · {page_nums} page · v view · L layout"
            " · g graphs · +/- history · c colors")
    if theme_toast:
        info = [theme_toast]
    else:
        info = [(f"agents {up}/{total} · poll {rate:g} s · history {term.fmt_span(span)}", 6)]
    p = panels.Panel(y, x0, width, kind="plain")
    p.rows.append(([(keys, 9)], info))
    return p


# --- assembly ----------------------------------------------------------------------

def _topo_candidates(pairs, width, cap, selected_name, cache):
    """topomap renders at every distinct height up to `cap` rows, tallest
    first (topomap picks its detail level from the rows it is given)."""
    out = []
    rows = cap
    while rows >= 3:
        t = topomap.render(pairs, width - 4, rows, selected_name=selected_name, cache=cache)
        if not t or not t["rows"]:
            break
        out.append(t)
        rows = len(t["rows"]) - 1
    return out


def _total(infos, per, v, dials_h, topo, heat_n):
    used = 1 + 1                       # header, footer
    used += dials_h
    if topo is not None:
        used += len(topo["rows"]) + 2
    for k in range(0, len(infos), per):
        used += max(column_rows(i, v) for i in infos[k:k + per]) + 2
    n = heat_n(v["heat"])
    if n:
        used += n + 1 + 2
    return used


def build_page3_large(cluster_ctx, width, height, x0=0, layout="large", history=None,
                      theme_toast=None, has_perf=False, preset="auto", view="map+graphs"):
    """Page 3 for a big terminal, exactly `height` rows; None when it does
    not apply (no braille, no nodes, over MAX_NODES) or nothing fits.
    `preset` (the `L` key): graphs drops the CLUSTER dials, dials draws
    them bigger. `view` (the `v` key, VIEWS): map or heat fill the page
    with that alone; with no map or no heat data it falls back to
    map+graphs."""
    if term.BAR_STYLE != "eighths" or not height:
        return None
    pre = pages.page3_prepare(cluster_ctx)
    views, metrics, n = pre["views"], pre["metrics"], pre["n"]
    if n == 0 or n > MAX_NODES:
        return None
    ui = cluster_ctx.get("ui")
    rate = cluster_ctx.get("rate", 1.0)
    span = history or bigpage.DEFAULT_HISTORY.get(layout, 300)
    try:
        bigpage._RATE[0] = float(rate or 1.0)
    except Exception:
        bigpage._RATE[0] = 1.0

    infos = [node_info(vw, m) for vw, m in zip(views, metrics)]
    per = grid_for(n, width)
    if math.ceil(n / per) > MAX_GRID_ROWS:
        return None
    dial_w = (bigpage.BIG_DIAL_W if preset == "dials" else DIAL_W).get(layout, 16)
    dials = cluster_dials(infos, pre["total"]) if preset != "graphs" else []
    dials_h = term.dial_size(dial_w) + 2 if dials else 0
    levels = heat_levels(infos)
    heat_count = {lvl: len(heat_rows(infos, lvl)) for lvl in levels}

    def heat_n(level):
        return heat_count.get(level, 0)

    pairs_by_name = sorted(zip(views, metrics), key=lambda vm: vm[0].get("name") or "")
    sel_name = ui.selected_name if ui else None
    cache = getattr(ui, "topo_cache", None) if ui else None
    if view in ("map", "heat"):
        alone = _build_alone(view, pre, infos, levels, heat_count, pairs_by_name, sel_name, cache, width, height,
                             x0, dials, dial_w, dials_h, span, cluster_ctx, rate, has_perf, theme_toast)
        if alone is not None:
            return alone
    topos = _topo_candidates(pairs_by_name, width, max(6, min(16, height // 5)), sel_name, cache)

    # Keep the dials and the heat grid before the map's size, the map before
    # nothing; first at comfortable chart heights, then at the least.
    choice = None
    first_heat = 1 if 1 in levels else 0
    for floor in (_FLOOR, _MIN):
        configs = [(d, h, t) for d, h in ((True, first_heat), (True, 0), (False, 0)) for t in topos]
        configs += [(d, h, None) for d, h in ((True, first_heat), (True, 0), (False, 0))]
        for d, h, t in configs:
            vv = dict(floor, heat=h)
            if _total(infos, per, vv, dials_h if d else 0, t, heat_n) <= height:
                choice = (vv, d, t)
                break
        if choice:
            break
    if choice is None:
        return None
    v, dials_on, topo = choice

    def total(vv):
        return _total(infos, per, vv, dials_h if dials_on else 0, topo, heat_n)

    grown = True
    while grown:
        grown = False
        for k in _GROW:
            if k == "heat":
                if not v["heat"]:
                    continue       # the grid was dropped to fit: do not bring it back half-way
                nxt = [lvl for lvl in levels if lvl > v["heat"]]
                if not nxt:
                    continue
                trial = dict(v, heat=nxt[0])
            else:
                if v[k] >= _MAX[k]:
                    continue
                step = 2 if (k in _OPEN_AT_2 and v[k] == 0) else 1
                trial = dict(v, **{k: v[k] + step})
            if total(trial) <= height:
                v = trial
                grown = True
    pad = max(0, height - total(v))

    out = [pages._build_header(x0, width, "wide", page=3, cluster_tabs=True,
                               cluster_summary=pre["summary"], has_perf=has_perf)]
    y = 1
    if dials_on:
        strip = bigpage._build_dial_strip(y, x0, width, dials, dial_w,
                                          [(f"{cluster_ctx.get('name', 'cluster')} · {pre['total']} nodes", 6)],
                                          title="CLUSTER")
        if strip is not None:
            out.append(strip)
            y += strip.total_height + 1
        else:
            pad += dials_h
    if topo is not None:
        tp = pages._build_topology_panel(y, x0, width, topo, n)
        out.append(tp)
        y += tp.total_height + 1
    alerts = {}
    for vw, m in zip(views, metrics):
        alerts[vw.get("name")] = pages._cluster_alerts(vw, m)
    for k in range(0, n, per):
        chunk = infos[k:k + per]
        # a short last row (16 nodes as 6, 6, 4) widens its columns to fill
        col_w = width // len(chunk)
        row_h = max(column_rows(i, v) for i in chunk)
        if not v["heat"] and k + per >= n:
            row_h += pad            # no heat grid: the last row of columns takes the spare
        for j, info in enumerate(chunk):
            w = col_w if j < len(chunk) - 1 else width - col_w * (len(chunk) - 1)
            extra = row_h - column_rows(info, v) if info["up"] else 0
            cp = _build_column(y, x0 + j * col_w, w, info, v, span, (k + j) == pre["selected"],
                               alerts.get(info["view"].get("name")) or [], extra)
            while len(cp.rows) < row_h:
                cp.add_row([])
            out.append(cp)
        y += row_h + 2
    if v["heat"]:
        hp = _build_heat(y, x0, width, heat_rows(infos, v["heat"]), span, pad, v["heat"])
        out.append(hp)
        y += hp.total_height + 1
    out.append(_footer(x0, width, height - 1, pre["up"], pre["total"], rate, span, has_perf, theme_toast))
    return out


def _build_alone(view, pre, infos, levels, heat_count, pairs, sel_name, cache, width, height, x0,
                 dials, dial_w, dials_h, span, cluster_ctx, rate, has_perf, theme_toast):
    """The map view (TOPOLOGY as tall as it gets, centred in the rest) or
    the heat view (the deepest heat grid, each series drawn up to
    HEAT_THICK_MAX rows tall). None when there is no map or heat to show."""
    out = [pages._build_header(x0, width, "wide", page=3, cluster_tabs=True,
                               cluster_summary=pre["summary"], has_perf=has_perf)]
    y = 1
    if dials and dials_h + 2 + 6 <= height:
        strip = bigpage._build_dial_strip(y, x0, width, dials, dial_w,
                                          [(f"{cluster_ctx.get('name', 'cluster')} · {pre['total']} nodes", 6)],
                                          title="CLUSTER")
        if strip is not None:
            out.append(strip)
            y += strip.total_height + 1
    room = height - 1 - y - 2          # rows inside the one panel left, above the footer
    if view == "map":
        topo = topomap.render(pairs, width - 4, room, selected_name=sel_name, cache=cache) if room >= 3 else None
        if not topo or not topo["rows"]:
            return None
        p = pages._build_topology_panel(y, x0, width, topo, pre["n"])
        top = (room - len(topo["rows"])) // 2
        p.rows[0:0] = [([], [])] * top
        while len(p.rows) < room:
            p.add_row([])
    else:
        if not levels:
            return None
        level = max(levels)
        rows = heat_rows(infos, level)
        if not rows or len(rows) + 1 > room:
            return None
        thick = max(1, min(HEAT_THICK_MAX, (room - 1) // len(rows)))
        pad = room - (len(rows) * thick + 1)
        p = _build_heat(y, x0, width, rows, span, pad, level, thick)
    out.append(p)
    out.append(_footer(x0, width, height - 1, pre["up"], pre["total"], rate, span, has_perf, theme_toast))
    return out
