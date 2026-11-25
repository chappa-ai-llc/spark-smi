"""Page 4 (fabric tests) on big screens (#14): at bigpage.layout_for() large
or xl, the page uses the whole terminal instead of the centred 160 columns.

  FABRIC TEST   the normal control panel, across the top
  BANDWIDTH     the per-rail chart, as tall as the page allows, beside
  RAILS         the last run's per-rail results, and
  FABRIC NOW    every member's fabric traffic in and out, from the
                viewer's own history (cluster.NodeHistory), so a test's load
                -- or someone else's -- shows on every node at once
  MATRIX        the all-pairs grid, beside
  RUN HISTORY   the newest results from the results file, newest first

Heights are pure arithmetic and fill the terminal exactly; None (the normal
page 4) with fewer than two members, without braille, or when nothing
fits. Like the normal page it only displays: starting a run stays behind
the same confirm gate in app.py."""
import time

from . import advpage, bigpage, pages, panels, term

LEFT_SHARE = 0.62       # BANDWIDTH's and MATRIX's share of the width
RIGHT_MIN = 72
HISTORY_MIN = 10        # RUN HISTORY rows kept before the chart grows past its minimum
_MIN = {"chart": 6, "now": 1}
_MAX = {"chart": 32, "now": 6}
CHART_SHARE = 0.42      # on tall screens the chart may take this share of the height
_GROW = ["chart", "now", "chart", "chart", "now", "chart", "now"]


def _top(p):
    """The normal page chains its panels into one frame; here each stands
    alone."""
    p.kind = "top"
    return p


def _members(views):
    out = []
    for v in views or []:
        hist = v.get("hist")
        try:
            rx, tx = (hist.series("rx"), hist.series("tx")) if hist is not None else ([], [])
        except Exception:
            rx, tx = [], []
        out.append((v.get("name") or "?", bool(v.get("stale")), rx, tx))
    return out


def _now_cells(members, span, half):
    cells = []
    for name, stale, rx, tx in members:
        last_rx = next((x for x in reversed(rx) if x is not None), None)
        last_tx = next((x for x in reversed(tx) if x is not None), None)
        head = [(f"{name} ", 9)]
        if stale:
            head.append(("silent", 4))
        elif last_rx is not None:
            head += [("▼ ", 2), (advpage._fmt_bps(last_rx), 8), ("  ▲ ", 5), (advpage._fmt_bps(last_tx), 8)]
        cells.append(lambda w, a=rx, b=tx, hd=head: advpage.mirror_cell(hd, [], a, b, 1e8, w, 2 * half, span))
    return cells


def now_rows(n_members, inner, half):
    return advpage.grid_rows(n_members, inner, 2 * half, [True] * n_members)


def _fmt_when(ts):
    try:
        return time.strftime("%m-%d %H:%M", time.localtime(float(ts)))
    except Exception:
        return "?"


def _run_row(run):
    """(text, slot) runs for one result line."""
    when = [(_fmt_when(run.get("ts")), 6), ("  ", 3)]
    mode = run.get("mode") or "?"
    if mode == "burst":
        pairs = run.get("pairs") or []
        agg = run.get("aggregate_gbps")
        return when + [("burst ", 9), (f"{len(pairs)} pair{'s' if len(pairs) != 1 else ''}", 3),
                       ("  Σ ", 6), (f"{agg:.1f} Gb/s" if isinstance(agg, (int, float)) else "no result", 8)]
    rails = [r for r in run.get("rails") or [] if isinstance(r.get("gbps"), (int, float))]
    total = sum(r["gbps"] for r in rails)
    out = when + [(f"{mode} ", 9), (f"{run.get('src') or '?'} → {run.get('dst') or '?'}", 3)]
    if rails:
        lo, hi = min(r["gbps"] for r in rails), max(r["gbps"] for r in rails)
        out += [("  Σ ", 6), (f"{total:.1f} Gb/s", 8),
                (f"  {len(rails)} rail{'s' if len(rails) != 1 else ''} ", 6), (f"{lo:.1f}–{hi:.1f}", 3)]
    else:
        out += [("  no result", 4)]
    lat = run.get("lat_us")
    if isinstance(lat, (int, float)):
        out += [("  lat ", 6), (f"{lat:.2f} µs", 3)]
    return out


def _build_history(y, x, w, runs, rows):
    p = panels.Panel(y, x, w, title=[("RUN HISTORY", 9), (" newest first", 3)],
                     title_right=[(f"{len(runs)} shown", 6)] if runs else [], kind="top")
    if not runs:
        p.add_row([(1, "no fabric tests recorded yet — space starts one", 6)])
    for run in runs[:max(0, rows)]:
        p.add_row(bigpage._row(1, advpage._fit(_run_row(run), w - 3)))
    while len(p.rows) < rows:
        p.add_row([])
    return p


def build_page4_large(fab_ctx, width, height, x0=0, layout="large", history=None, runs=None, theme_toast=None):
    """Page 4 for a big terminal, exactly `height` rows; None when it does
    not apply (fewer than two members, no braille) or nothing fits."""
    views = fab_ctx.get("views") or []
    if term.BAR_STYLE != "eighths" or not height or len(views) < 2:
        return None
    bigpage._RATE[0] = max(0.1, float(fab_ctx.get("rate", 1.0) or 1.0))
    span = history or bigpage.DEFAULT_HISTORY.get(layout, 300)
    runs = runs or []
    left_w = max(pages.content_width("wide", width) - 40, int(width * LEFT_SHARE))
    right_w = width - left_w - 1
    if right_w < RIGHT_MIN:
        return None
    members = _members(views)

    ft_rows = len(pages._build_fabtest_panel(0, 0, width, fab_ctx).rows)
    rails_rows = len(pages._build_rails_panel(0, 0, right_w, fab_ctx).rows)
    matrix_rows = len(pages._build_matrix_panel(0, 0, left_w, fab_ctx).rows)

    def bw_rows(v):
        return len(pages._build_bandwidth_panel(0, 0, left_w, fab_ctx, v["chart"], True).rows)

    def mid_rows(v):
        right = rails_rows + 2 + now_rows(len(members), right_w - 2, v["now"]) + 2
        return max(bw_rows(v) + 2, right)

    bottom_min = max(matrix_rows, min(max(1, len(runs)), HISTORY_MIN))

    def total(v):
        return 1 + 1 + ft_rows + 2 + mid_rows(v) + bottom_min + 2

    limit = dict(_MAX, chart=max(_MAX["chart"], int(height * CHART_SHARE)))
    v = dict(_MIN)
    if 1 + 1 + ft_rows + 2 + mid_rows(v) + matrix_rows + 2 > height:
        return None
    if total(v) > height:          # short screen: the history gets what is left
        bottom_min = matrix_rows
    grown = True
    while grown:
        grown = False
        for key in _GROW:
            if v[key] >= limit[key]:
                continue
            trial = dict(v, **{key: v[key] + 1})
            if total(trial) <= height:
                v = trial
                grown = True
    # Rows the bottom row cannot use (few results recorded) go to the chart.
    bottom_min = max(bottom_min, matrix_rows, len(runs))
    while v["chart"] < height and total(dict(v, chart=v["chart"] + 1)) <= height:
        v["chart"] += 1
    # A chart taller than RAILS + FABRIC NOW: the node charts grow into the gap.
    while v["now"] < height and mid_rows(dict(v, now=v["now"] + 1)) == mid_rows(v):
        v["now"] += 1

    out = [pages._build_header(x0, width, "wide", page=4, cluster_tabs=True,
                               cluster_summary=f"{fab_ctx.get('name', 'cluster')} — fabric validation",
                               has_perf=True)]
    y = 1
    ft = _top(pages._build_fabtest_panel(y, x0, width, fab_ctx))
    out.append(ft)
    y += ft.total_height + 1

    mid_h = mid_rows(v)
    bw = _top(pages._build_bandwidth_panel(y, x0, left_w, fab_ctx, v["chart"], True))
    while len(bw.rows) < mid_h - 2:
        bw.add_row([])
    out.append(bw)
    rx = x0 + left_w + 1
    rp = _top(pages._build_rails_panel(y, rx, right_w, fab_ctx))
    out.append(rp)
    ny = y + rp.total_height + 1
    np_ = panels.Panel(ny, rx, right_w, title=[("FABRIC NOW", 9), (" every node · in ▼ above, out ▲ below", 3)],
                       title_right=[("history ", 6), (term.fmt_span(span), 3)], kind="top")
    advpage._grid(np_, _now_cells(members, span, v["now"]), right_w - 2, span)
    while ny + np_.total_height < y + mid_h:
        np_.add_row([])
    out.append(np_)
    y += mid_h

    bottom_h = height - 1 - y - 2          # content rows of the bottom row's panels
    mp = _top(pages._build_matrix_panel(y, x0, left_w, fab_ctx))
    while len(mp.rows) < bottom_h:
        mp.add_row([])
    out.append(mp)
    out.append(_build_history(y, rx, right_w, runs, bottom_h))
    out.append(pages.page4_footer(fab_ctx, x0, width, height - 1, theme_toast))
    return out
