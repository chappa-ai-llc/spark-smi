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

MAX_NODES = 8
COL_MIN_W = 56          # a node column narrower than this cannot hold its charts
DIAL_W = {"large": 16, "xl": 18}

_FLOOR = {"gpu": 6, "cpu": 3, "pwr": 2, "fab": 2}    # tried first
_MIN = {"gpu": 3, "cpu": 2, "pwr": 0, "fab": 1}      # the least that still reads
_MAX = {"gpu": 24, "cpu": 10, "pwr": 8, "fab": 6}
_GROW = ["gpu", "cpu", "fab", "pwr", "gpu", "gpu", "cpu"]

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


def _top_proc(sample):
    best = None
    for g in sample.get("gpus") or []:
        for p in g.get("procs") or []:
            if best is None or (p.get("mem") or 0) > (best.get("mem") or 0):
                best = p
    return best


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
    info["proc"] = _top_proc(s)
    return info


def _has_pwr_chart(info):
    return len([v for v in info.get("pwr_hist") or [] if v is not None]) >= 2


def column_rows(info, v):
    """Content rows of a node's column at chart heights `v` -- the same
    count _build_column adds, so the fitting search needs no drawing."""
    if not info["up"]:
        return 2
    r = sum(1 + v["gpu"] for _ in info["gpus"]) or 1
    r += 1 + v["cpu"]
    if v["pwr"] and _has_pwr_chart(info):
        r += 1 + v["pwr"]
    if info["fab"]:
        r += 1 + 2 * v["fab"]
    return r + 2     # time axis, top process


def grid_for(n, width):
    """Node columns per row: everyone on one row up to four nodes (or when
    each still gets 64 columns), else two rows; never under COL_MIN_W."""
    per = n if (n <= 4 or width // max(1, n) >= 64) else math.ceil(n / 2)
    while per > 1 and width // per < COL_MIN_W:
        per -= 1
    return max(1, per)


def _heat_rows(infos):
    rows = []
    for info in infos:
        multi = len(info["gpus"]) > 1
        for g in info["gpus"]:
            label = f"{info['name']} {g['id']}" if multi else info["name"]
            rows.append((label, g["hist"]))
    return rows


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
    for gi, g in enumerate(info["gpus"]):
        gh = v["gpu"] + extra // n_g + (1 if gi < extra % n_g else 0)
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
    p.add_row(bigpage._row(1, head), right=mr)
    ch = v["cpu"] + (0 if n_g else extra)
    for r in bigpage._chart_rows(bigpage._window(info["cpu_hist"], span), cw, ch, 100, span,
                                 bigpage._pct_labels(ch), False):
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
    proc = info.get("proc")
    if proc:
        pname = (proc.get("name") or "").strip() or str(proc.get("pid", "?"))
        row = [("top ", 6), (pname[:max(8, iw - 40)], 3), (f" · pid {proc.get('pid', '?')}", 6)]
        if proc.get("mem"):
            row.append((f"  {term.fmt_mem(proc['mem'])}", 3))
        if isinstance(proc.get("sm"), (int, float)) and proc.get("sm"):
            row.append((f"  SM {proc['sm']:.0f}%", 3))
        p.add_row(bigpage._row(1, row))
    else:
        p.add_row([(1, "no GPU processes", 6)])
    return p


def _build_heat(y, x0, width, rows, span, pad):
    lw = min(22, max(len(lbl) for lbl, _ in rows)) + 1
    cells = width - 2 - 1 - lw
    p = panels.Panel(y, x0, width, title=[("GPU UTIL", 9), (" every GPU, one row each · a row that lags is a straggler", 3)],
                     title_right=[("· idle ", 7), ("▆", 12), ("▆", 15), (" busy", 6)], kind="top")
    for label, hist in rows:
        runs = term.heat_runs(bigpage._window(hist, span), cells, window=bigpage._w(span)) or []
        p.add_row([(1, f"{label[:lw - 1]:<{lw}}", 6)] + bigpage._row(None, runs))
    p.add_row(bigpage._row(1 + lw, term.time_axis(cells, span)))
    for _ in range(pad):
        p.add_row([])
    return p


def _footer(x0, width, y, up, total, rate, span, has_perf, theme_toast):
    page_nums = "1 2 3 4 5" if has_perf else "1 2 3 5"
    keys = f" q quit · ←→ select · enter drill into node · {page_nums} page · g graphs · +/- history · c colors"
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
    if heat_n:
        used += heat_n + 1 + 2
    return used


def build_page3_large(cluster_ctx, width, height, x0=0, layout="large", history=None,
                      theme_toast=None, has_perf=False):
    """Page 3 for a big terminal, exactly `height` rows; None when it does
    not apply (no braille, no nodes, over MAX_NODES) or nothing fits."""
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
    if math.ceil(n / per) > 2:
        return None
    dial_w = DIAL_W.get(layout, 16)
    dials = cluster_dials(infos, pre["total"])
    dials_h = term.dial_size(dial_w) + 2 if dials else 0
    heat = _heat_rows(infos)
    pairs_by_name = sorted(zip(views, metrics), key=lambda vm: vm[0].get("name") or "")
    topos = _topo_candidates(pairs_by_name, width, max(6, min(16, height // 5)),
                             ui.selected_name if ui else None, getattr(ui, "topo_cache", None) if ui else None)

    choice = None
    for floor in (_FLOOR, _MIN):
        configs = [(d, h, t) for d, h in ((True, True), (True, False), (False, False)) for t in topos]
        configs += [(d, h, None) for d, h in ((True, True), (True, False), (False, False))]
        for d, h, t in configs:
            heat_n = len(heat) if (h and len(heat) >= 2) else 0
            if _total(infos, per, floor, dials_h if d else 0, t, heat_n) <= height:
                choice = (dict(floor), d, heat_n, t)
                break
        if choice:
            break
    if choice is None:
        return None
    v, dials_on, heat_n, topo = choice

    def total(vv):
        return _total(infos, per, vv, dials_h if dials_on else 0, topo, heat_n)

    grown = True
    while grown:
        grown = False
        for k in _GROW:
            if v[k] >= _MAX[k]:
                continue
            step = 2 if (k == "pwr" and v[k] == 0) else 1
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
    col_w = width // per
    for k in range(0, n, per):
        chunk = infos[k:k + per]
        row_h = max(column_rows(i, v) for i in chunk)
        if not heat_n and k + per >= n:
            row_h += pad            # no heat grid: the last row of columns takes the spare
        for j, info in enumerate(chunk):
            w = col_w if j < per - 1 else width - col_w * (per - 1)
            extra = row_h - column_rows(info, v) if info["up"] else 0
            cp = _build_column(y, x0 + j * col_w, w, info, v, span, (k + j) == pre["selected"],
                               alerts.get(info["view"].get("name")) or [], extra)
            while len(cp.rows) < row_h:
                cp.add_row([])
            out.append(cp)
        y += row_h + 2
    if heat_n:
        hp = _build_heat(y, x0, width, heat, span, pad)
        out.append(hp)
        y += hp.total_height + 1
    out.append(_footer(x0, width, height - 1, pre["up"], pre["total"], rate, span, has_perf, theme_toast))
    return out
