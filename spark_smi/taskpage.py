"""Page 5 -- the GPU task manager's screen (#4; fuller in 5.1.0, #14 step 2).

Pure drawing of what app.py hands over: the tasks.TaskCollector tree, the
tasks.flatten() rows, TaskUI.render_ctx(), the tasks.ProcTracker snapshot
(per-process history and the recently-ended list) and tasks.proc_detail()
for the selected process. Two layouts:

  normal  the GPU TASKS tree, then a PROCESS detail pane and the recently
          ended list when the height allows.
  large   (bigpage.layout_for: 200+ columns and 45+ rows) one card per GPU
          on top, with SM by process and stacked GPU memory charts; the
          tree on the left with an SM sparkline column; the detail pane
          with its own charts on the right.

Columns follow the capability rule: SM% only where NVML reports
per-process utilization, ENC/DEC only where a GPU also reports an encoder.
"""
import time

from . import bigpage
from . import pages
from . import panels
from . import tasks
from . import term

try:
    from . import VERSION
except Exception:  # pragma: no cover
    VERSION = "5.4.0"


_KIND_SLOT = {"orphan": 5, "tmux": 2, "screen": 2, "zellij": 2, "docker": 2,
              "systemd": 2, "ssh": 3, "tty": 3, "none": 6}
SERIES_SLOTS = (2, 5, 1, 9)     # per-process lines, biggest first
SPARK_SPAN = 120                # the table's SM column: last 2 minutes
DETAIL_MIN = 7                  # detail pane rows (frame included) worth drawing
_LABEL_W = 5


# --- small helpers ------------------------------------------------------

def _tree_glyphs():
    if term.BAR_STYLE == "ascii":
        return {"mid": "|- ", "last": "`- ", "bar": "|  ", "gap": "   ", "sel": "> ", "gpu": "* "}
    return {"mid": "├─ ", "last": "└─ ", "bar": "│  ", "gap": "   ", "sel": "▶ ", "gpu": "▾ "}


def _fmt_age(seconds):
    if seconds is None:
        return "-"
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m{s % 60:02d}"
    if s < 86400:
        return f"{s // 3600}h{(s % 3600) // 60:02d}"
    return f"{s // 86400}d{(s % 86400) // 3600:02d}h"


def _task_mem(bytes_val):
    """Single-letter-unit memory for the fixed-width stats columns: "512M"
    below a GiB, "4.1G" above -- an always-G form would print a 12 MiB
    worker as "0.0G"."""
    try:
        b = float(bytes_val)
    except Exception:
        return "-"
    if b < 1024 ** 3:
        return f"{b / (1024 ** 2):.0f}M"
    return f"{b / (1024 ** 3):.1f}G"


def _clip_runs(runs, avail):
    """Cuts a list of (text, slot) runs to at most `avail` chars total,
    keeping slot boundaries intact."""
    out, used = [], 0
    for text, slot in runs:
        if used >= avail:
            break
        room = avail - used
        piece = text if len(text) <= room else text[:room]
        if piece:
            out.append((piece, slot))
            used += len(piece)
    return out


def _row(col, runs):
    return [(col if i == 0 else None, t, s) for i, (t, s) in enumerate(runs)]


def _level_slot(pct):
    """A busy GPU is the point, not an alarm: amber from 75%, else teal."""
    return 5 if pct >= 75 else 2


def _axis_mem(b):
    """A memory axis label that fits the 4-column label space."""
    g = float(b) / 1024 ** 3
    if g >= 1:
        return f"{g:.0f}G"
    return f"{float(b) / 1024 ** 2:.0f}M"


def _samples(span, rate):
    return max(2, int(round(span / max(0.1, rate or 1.0))))


def column_caps(sample):
    """{"sm", "encdec"}: which per-process columns have data on this node."""
    caps = (sample.get("caps") or {}).get("gpu") or []
    sm = any(c.get("proc_util") for c in caps if isinstance(c, dict))
    enc = sm and any(g.get("enc_util") is not None for g in sample.get("gpus") or [])
    return {"sm": sm, "encdec": enc}


def task_cols(tier, cc, spark_w=0):
    """(label, width) column set for the right-flushed stats block."""
    sm = [("SM%", 4)] if cc.get("sm") else []
    if tier == "compact":
        return sm + [("GPU MEM", 8), ("AGE", 7)]
    if tier == "standard":
        return sm + [("CPU", 5), ("RSS", 7), ("GPU MEM", 8), ("AGE", 7)]
    ed = [("ENC", 4), ("DEC", 4)] if cc.get("encdec") else []
    cols = [("USER", 9)] + sm + ed + [("CPU", 5), ("RSS", 7), ("GPU MEM", 8), ("AGE", 7)]
    if spark_w and cc.get("sm"):
        cols.append(("SPARK", spark_w))
    return cols


def _proc_hist(track, row):
    if not track or row.get("type") != "proc":
        return None
    root = row.get("gpu_id")
    if root is None:
        return None
    return (track.get("proc") or {}).get((str(root), row["proc"]["pid"]))


# --- the tree -----------------------------------------------------------

def _task_row(row, cols, glyphs, selected, track=None, rate=1.0):
    """(left_runs, right_runs) for one flattened tasks row."""
    sel = glyphs["sel"] if selected else "  "
    sel_slot = 9
    if row["type"] == "gpu":
        g = row["gpu"]
        n = sum(len(gr.get("procs") or []) for gr in g.get("groups") or [])
        mem = sum(gr.get("gpu_mem") or 0 for gr in g.get("groups") or [])
        util = g.get("util")
        left = [(sel, sel_slot), (glyphs["gpu"], 9), (f"GPU {g['idx']}  ", 9), (str(g.get("name") or ""), 8)]
        bits = []
        if util not in (None, "N/A"):
            try:
                bits.append(f"util {int(float(util))}%")
            except (TypeError, ValueError):
                bits.append(f"util {util}%")
        bits.append(f"{n} proc" + ("" if n == 1 else "s"))
        if mem:
            bits.append(term.fmt_mem(mem))
        return left, [("  ·  ".join(bits), 3)]
    if row["type"] == "group":
        g = row["group"]
        n = len(g.get("procs") or [])
        branch = glyphs["last"] if row.get("last") else glyphs["mid"]
        slot = _KIND_SLOT.get(g.get("kind"), 3)
        left = [(sel, sel_slot), ("  " + branch, 10)]
        if g.get("kind") == "orphan":
            left.append(("! ", 5))
        left.append((g.get("label") or "?", slot))
        if g.get("detail"):
            left.append((f"  · {g['detail']}", 6))
        mem = g.get("gpu_mem") or 0
        rtxt = f"{n} proc" + ("" if n == 1 else "s")
        if mem:
            rtxt += f" · {term.fmt_mem(mem)}"
        return left, [(rtxt, 6)]
    # process row
    p = row["proc"]
    if row.get("flat"):
        left = [(sel, sel_slot), ("  ", 10)]
    else:
        stem = glyphs["gap"] if row.get("group_last") else glyphs["bar"]
        branch = glyphs["last"] if row.get("last") else glyphs["mid"]
        left = [(sel, sel_slot), ("  " + stem + branch, 10)]
    if p.get("orphan"):
        left.append(("! ", 5))
    left.append((f"{p['pid']:>6} ", 8))
    left.append((p.get("name") or "?", 8 if selected else 3))
    cmd = p.get("cmd") or ""
    name = p.get("name") or ""
    # argv[0]'s basename usually repeats the process name (case-insensitively
    # on Windows: "explorer.exe Explorer.EXE") -- show only the arguments.
    if cmd.lower().startswith(name.lower() + " "):
        cmd = cmd[len(name) + 1:]
    elif cmd.lower() == name.lower():
        cmd = ""
    if row.get("flat"):
        label = (row.get("group") or {}).get("label")
        if label:
            left.append((f"  [{label}]", 6))
    if cmd:
        left.append((f" {cmd}", 6))
    tags = []
    if p.get("holder"):
        tags.append("device open")
    if p.get("state") == "Z" or (p.get("status") == "zombie"):
        tags.append("zombie")
    if len(p.get("on_gpus") or []) > 1:
        tags.append(f"on {len(p['on_gpus'])} GPUs")
    if p.get("tty_gone"):
        tags.append("tty gone")
    if tags:
        left.append((f"  ({', '.join(tags)})", 5))
    right = []
    for label, w in cols:
        if label == "SPARK":
            h = _proc_hist(track, row)
            runs = []
            if h and len(h.get("sm") or []) >= 2:
                vals = term.fit_window(h["sm"], _samples(SPARK_SPAN, rate), w * 2)
                runs = term.sparkline_runs(vals, w, 100)
            right.append(runs or [(" " * w, 3)])
            continue
        if label == "SM%":
            if "sm" in p:
                v = p.get("sm") or 0
                right.append([(f"{v:>{w}}" if v else f"{'·':>{w}}", _level_slot(v) if v else 6)])
            else:
                right.append([(f"{'-':>{w}}", 6)])
            continue
        if label in ("ENC", "DEC"):
            v = p.get(label.lower()) or 0
            right.append([(f"{v:>{w}}" if v else f"{'·':>{w}}", 3 if v else 6)])
            continue
        val = {
            "USER": (p.get("user") or "?")[:8],
            "CPU": f"{p.get('cpu') or 0:.0f}%",
            "RSS": _task_mem(p.get("rss") or 0) if p.get("rss") else "-",
            "GPU MEM": _task_mem(p["gpu_mem"]) if p.get("gpu_mem") else "-",
            "AGE": _fmt_age(p.get("age")),
        }[label]
        slot = 2 if label in ("CPU", "GPU MEM") else 3
        right.append([(f"{val:>{w}}", slot)])
    return left, right


def _sort_title(ui):
    sort = (ui or {}).get("sort") or "mem"
    down = "▼" if term.BAR_STYLE != "ascii" else "v"
    arrow = "▲" if sort == "pid" and term.BAR_STYLE != "ascii" else ("^" if sort == "pid" else down)
    out = [("sort ", 6), (f"{tasks.SORT_LABELS.get(sort, sort)} {arrow}", 8)]
    if (ui or {}).get("flat"):
        out += [("  flat", 8)]
    filt = (ui or {}).get("filter") or ""
    if (ui or {}).get("editing"):
        out += [("  filter ", 6), (filt + "_", 9)]
    elif filt:
        out += [("  filter ", 6), (filt, 9)]
    return out


def collapse_ended(ended):
    """Repeats of one command (a health check every 30 s) as one entry,
    newest first, with a count, the longest run and the highest peaks."""
    out, seen = [], {}
    for e in ended:
        key = (e.get("user"), e.get("cmd") or e.get("name"), tuple(e.get("gpus") or []))
        if key in seen:
            m = seen[key]
            m["count"] += 1
            m["ran"] = max(m.get("ran") or 0, e.get("ran") or 0)
            if e.get("peak_sm") is not None:
                m["peak_sm"] = max(m.get("peak_sm") or 0, e["peak_sm"])
            m["peak_mem"] = max(m.get("peak_mem") or 0, e.get("peak_mem") or 0)
            continue
        m = dict(e, count=1)
        seen[key] = m
        out.append(m)
    return out


def _ended_rows(ended, inner, now):
    rows = [[(1, "▾ " if term.BAR_STYLE != "ascii" else "* ", 9), (None, "RECENTLY ENDED", 9),
             (None, "  this node, last hour · kept in memory only", 6)]]
    for e in collapse_ended(ended):
        ago = _fmt_age(max(0, now - e.get("ended", now)))
        if e.get("count", 1) > 1:
            bits = f"×{e['count']}, longest {_fmt_age(e.get('ran'))} · last {e.get('how', 'ended')} {ago} ago"
        else:
            bits = f"ran {_fmt_age(e.get('ran'))} · {e.get('how', 'ended')} {ago} ago"
        if e.get("peak_sm") is not None:
            bits += f" · peak SM {e['peak_sm']}%"
        if e.get("peak_mem"):
            bits += f" · peak MEM {_task_mem(e['peak_mem'])}"
        gpus = ",".join(str(g) for g in e.get("gpus") or [])
        runs = [(f"    {e['pid']:>6} ", 6), (f"{(e.get('user') or '?')[:8]:<9}", 6), (f"GPU {gpus}  ", 6),
                (bits + "  ", 6), (e.get("cmd") or e.get("name") or "", 6)]
        rows.append(_row(1, _clip_runs(runs, inner - 2)))
    return rows


def _build_tasks_panel(y, x0, width, tree, rows, cols, ui, height_avail, track=None, rate=1.0,
                       fill=False, now=None):
    """The GPU TASKS frame: the tree scrolled around the selection, then
    the recently-ended list when rows are left over. `height_avail` is
    content rows (frame excluded); `fill` pads to exactly that."""
    inner = width - 2
    glyphs = _tree_glyphs()
    n_gpus = len(tree.get("gpus") or [])
    n_procs = tree.get("n_procs") or 0
    n_orph = tree.get("n_orphans") or 0
    n_sess = sum(len(g.get("groups") or []) for g in tree.get("gpus") or [])
    title = [("GPU TASKS", 9), (f"  {n_gpus} GPU" + ("" if n_gpus == 1 else "s"), 3),
             (f" · {n_procs} process" + ("" if n_procs == 1 else "es"), 3)]
    if n_sess:
        title.append((f" · {n_sess} session" + ("" if n_sess == 1 else "s"), 3))
    if n_orph:
        title.append((f" · {n_orph} orphan" + ("" if n_orph == 1 else "s"), 5))
    tr = _sort_title(ui) if ui else []
    src = tree.get("source") or "none"
    tr = tr + ([("  ·  ", 6)] if tr else []) + [(f"source {src}", 6)]
    if tree.get("caps", {}).get("fd_scan") and inner > 100:
        tr.append(("  ·  a holders " + ("on" if tree.get("holders") else "off"), 6))
    p = panels.Panel(y, x0, width, title=title, title_right=tr, kind="top")

    hdr_right = []
    for label, w in cols:
        if label == "SPARK":
            hdr_right.append((f"{'SM, last ' + term.fmt_span(SPARK_SPAN):<{w}}"[:w], 6))
        else:
            hdr_right.append((f"{label:>{w}}", 6))
        hdr_right.append((" ", 6))
    hdr_right = hdr_right[:-1]
    head = "PID / COMMAND" if not (ui or {}).get("flat") else "PID / COMMAND  [session]"
    p.add_row([(1, f"{'':2}{head}", 6)], right=hdr_right)

    avail = max(3, height_avail - 1)
    ended = list((track or {}).get("ended") or [])
    if (ui or {}).get("filter") and rows and not any(r["type"] == "proc" for r in rows):
        rows = []
    if not rows:
        if not n_gpus:
            p.add_row([(2, "no NVIDIA GPU detected -- nothing to list", 6)])
        elif (ui or {}).get("filter"):
            p.add_row([(2, f"no process matches \"{ui['filter']}\"", 6)])
        else:
            p.add_row([(2, "no GPU processes", 6)])
    else:
        selected = ui.get("selected", 0) if ui else -1
        start = 0
        if len(rows) > avail:
            win = avail - 2
            if selected >= 0:
                start = max(0, min(selected - win // 2, len(rows) - win))
            end = min(len(rows), start + win)
            if start == 0:
                end = min(len(rows), win + 1)
            if end == len(rows):
                start = max(0, end - (win + (1 if start == 0 else 0)))
        else:
            end = len(rows)
        if start:
            p.add_row([(1, f"▲ {start} more" if term.BAR_STYLE != "ascii" else f"^ {start} more", 6)])
        right_w = sum(w for _, w in cols) + len(cols) - 1
        for i in range(start, end):
            row = rows[i]
            left, right = _task_row(row, cols, glyphs, i == selected, track, rate)
            if row["type"] == "proc":
                runs = []
                for j, cell in enumerate(right):
                    runs.extend(cell)
                    if j < len(right) - 1:
                        runs.append((" ", 3))
                right, rw = runs, right_w
            else:
                rw = sum(len(t) for t, _ in right)
            avail_left = inner - 1 - rw - 2
            p.add_row(_row(1, _clip_runs(left, avail_left)), right=right)
        if len(rows) - end:
            more = len(rows) - end
            p.add_row([(1, f"▼ {more} more" if term.BAR_STYLE != "ascii" else f"v {more} more", 6)])
    # Recently ended: only into rows the tree left free, never scrolling it.
    spare = height_avail - len(p.rows)
    if ended and spare >= 3:
        p.add_row([])
        for r in _ended_rows(ended, inner, now or time.time())[:spare - 1]:
            p.add_row(r)
    if fill:
        while len(p.rows) < height_avail:
            p.add_row([])
    return p


# --- the detail pane ----------------------------------------------------

def selected_proc_row(rows, ui):
    """The process row the detail pane describes: the selected row, or for
    a GPU/session row the first process below it. Snapshot mode (no ui)
    describes the first process."""
    if not rows:
        return None
    i = ui.get("selected", 0) if ui else 0
    i = max(0, min(i, len(rows) - 1))
    for r in rows[i:]:
        if r["type"] == "proc":
            if rows[i]["type"] == "gpu" or r.get("gpu_idx") == rows[i].get("gpu_idx"):
                return r
            break
    return None


def _when(ts, now):
    if not ts:
        return "?"
    lt, ln = time.localtime(ts), time.localtime(now)
    if lt.tm_yday == ln.tm_yday and lt.tm_year == ln.tm_year:
        return "today " + time.strftime("%H:%M", lt)
    return time.strftime("%Y-%m-%d %H:%M", lt)


def detail_fields(row, detail, now):
    """[(label, text)] for the detail pane, only the fields that read."""
    p = row["proc"]
    g = row.get("group") or {}
    d = detail or {}
    out = [("command", p.get("cmd") or p.get("name") or "?")]
    sess = g.get("label") or ""
    if g.get("detail"):
        sess += f" · {g['detail']}"
    if sess:
        out.append(("session", sess))
    user = p.get("user") or "?"
    if d.get("uid") is not None:
        user += f" ({d['uid']})"
    if d.get("ppid"):
        user += f"  ppid {d['ppid']}" + (f" {d['parent']}" if d.get("parent") else "")
    out.append(("user", user))
    started = _when(d.get("started"), now)
    if p.get("age") is not None:
        started += f" · up {_fmt_age(p['age'])}"
    if d.get("threads"):
        started += f" · {d['threads']} threads"
    out.append(("started", started))
    if d.get("cwd"):
        out.append(("cwd", d["cwd"]))
    if d.get("env"):
        out.append(("gpu env", "  ".join(f"{k}={v}" for k, v in d["env"])))
    if d.get("devices"):
        out.append(("devices", " · ".join(d["devices"])))
    ctx = f"GPU {row.get('gpu_idx')}"
    if p.get("gpu_mem"):
        ctx += f" · {term.fmt_mem(p['gpu_mem'])}"
    if len(p.get("on_gpus") or []) > 1:
        ctx += f" · on GPUs {','.join(str(i) for i in p['on_gpus'])}"
    out.append(("gpu", ctx))
    if d.get("denied"):
        out.append(("", "another user's: cwd, environment, open files need root"))
    return out


def _labels_pct(h):
    labels = {0: "100%", h - 1: "0"}
    if h >= 5:
        labels[(h - 1) // 2] = "50%"
    return labels


def _area_rows(col, values, width, height, y_max, window, labels):
    """An area chart with a y-axis starting at column `col`: rows of
    segments, or [] outside the braille tier / without history."""
    cw = width - _LABEL_W - 1
    rows = term.area_chart(values, cw, height, y_max=y_max, window=window) if cw > 4 else None
    if rows is None:
        return []
    out = []
    for r, runs in enumerate(rows):
        lab = labels.get(r)
        out.append([(col, f"{(lab or ''):>{_LABEL_W - 1}}", 6), (None, "┤" if lab else "│", 10)] + _row(None, runs))
    return out


def _axis(col, width, span):
    return [(col, " " * (_LABEL_W - 1), 3), (None, "└", 10)] + _row(None, term.time_axis(width - _LABEL_W - 1, span))


def _mem_top(row):
    """The GPU's own memory size (a fixed scale shows a leak as a climb);
    0 for unified memory, where the chart scales to the process."""
    try:
        return float(row.get("gpu_mem_total") or 0)
    except (TypeError, ValueError):
        return 0.0


def _detail_charts(row, track, width, height, span, rate, cc, mem_total):
    """Rows of SM / GPU MEM / CPU charts for one process, stacked, `height`
    rows in all (axis included). [] when there is no history yet."""
    if term.BAR_STYLE != "eighths":
        return []
    h = _proc_hist(track, row)
    pid = row["proc"]["pid"]
    cpu = ((track or {}).get("cpu") or {}).get(pid) or []
    if not h or len(h.get("mem") or []) < 2:
        return []
    window = _samples(span, rate)
    charts = []
    if cc.get("sm") and len(h.get("sm") or []) >= 2:
        vals = h["sm"][-window:]
        peak = max(vals) if vals else 0
        avg = sum(vals) / len(vals) if vals else 0
        charts.append(([("SM ", 6), (f"{row['proc'].get('sm', 0)}%", 8), (f"   peak {peak:.0f}%  avg {avg:.0f}%", 6)],
                       h["sm"], 100.0, "pct", 3))
    mem = h["mem"]
    # The process's own peak with headroom (at least 1 GiB), never above
    # the GPU's memory: a 174 MiB job on 122 GiB of unified memory would
    # otherwise draw as an empty chart. Fixed per window, so a leak climbs.
    top = max(max(mem[-window:] or [0]) * 1.5, float(1024 ** 3))
    if mem_total:
        top = min(top, mem_total)
    old = mem[-window] if len(mem) >= window else mem[0]
    grew = mem[-1] - old
    trend = f"   {'grew' if grew >= 0 else 'fell'} {term.fmt_mem(abs(grew))} in {term.fmt_span(min(span, len(mem) * rate))}" \
        if abs(grew) >= 64 * 1024 ** 2 else ""
    charts.append(([("GPU MEM ", 6), (term.fmt_mem(mem[-1]), 8), (trend, 6)], mem, top, "mem", 2))
    if len(cpu) >= 2:
        # 100% is one core; a multi-threaded job scales the chart up
        top_cpu = max(100.0, max(cpu[-window:] or [0]))
        charts.append(([("CPU ", 6), (f"{row['proc'].get('cpu') or 0:.0f}%", 8)], cpu, top_cpu, "cpu", 2))
    # each chart: label row + chart rows; one axis row at the end
    budget = height - 1 - len(charts)
    if budget < 2 * len(charts):
        charts = charts[:max(1, budget // 2)]
        budget = height - 1 - len(charts)
    if budget < 2:
        return []
    weights = {"pct": 3, "mem": 2, "cpu": 2}
    total_w = sum(weights[c[3]] for c in charts)
    heights = [max(2, budget * weights[c[3]] // total_w) for c in charts]
    heights[0] += budget - sum(heights)
    out = []
    for (label, vals, top_v, kind, _), ch in zip(charts, heights):
        out.append(_row(1, label))
        if kind == "mem":
            labels = {0: _axis_mem(top_v), ch - 1: "0"}
        elif kind == "cpu":
            labels = {0: f"{top_v:.0f}%", ch - 1: "0"}
        else:
            labels = _labels_pct(ch)
        rows = _area_rows(1, vals, width, ch, top_v, window, labels)
        out.extend(rows or [[] for _ in range(ch)])
    out.append(_axis(1, width, span))
    return out


def _build_detail_panel(y, x0, width, height, row, detail, track, rate, span, cc, ui, side=False, now=None):
    """The PROCESS frame, `height` lines including its frame. Side-by-side
    (normal layout, wide enough): fields on the left, charts on the
    right. Stacked (large layout's right column): fields, then charts."""
    now = now or time.time()
    inner = width - 2
    p = row["proc"]
    title = [("PROCESS", 9), (f"  {p['pid']} {p.get('name') or ''}", 8)]
    tr = [("selected", 6)] if ui else [("first process · -l to select", 6)]
    panel = panels.Panel(y, x0, width, title=title, title_right=tr, kind="top")
    body = height - 2
    fields = detail_fields(row, detail, now)
    mem_top = _mem_top(row)
    if side:
        fw = min(70, max(40, inner // 2 - 2))
        cw = inner - fw - 3
        lines = []
        for k, v in fields[:body]:
            lines.append(_clip_runs([(f"{k:<9}", 6), (v, 3)], fw))
        if ui and body >= len(lines) + 3:
            lines += [[] for _ in range(body - len(lines) - 2)]
            lines.append(_clip_runs([("e", 9), (" end (SIGTERM)  ", 6), ("k", 9), (" kill (SIGKILL)", 6)], fw))
            lines.append(_clip_runs([("every action asks y to confirm", 6)], fw))
        charts = _detail_charts(row, track, cw, body, span, rate, cc, mem_top) if cw >= 30 else []
        for i in range(body):
            segs = _row(1, lines[i]) if i < len(lines) else []
            if i < len(charts) and charts[i]:
                c = charts[i]
                segs = segs + [(fw + 3, c[0][1], c[0][2])] + list(c[1:])
            panel.add_row(segs)
        return panel
    key_rows = 2 if ui and body >= 8 else 0
    n_f = min(len(fields), body - key_rows)
    for k, v in fields[:n_f]:
        if k == "command" and len(v) > inner - 12 and body - n_f > 8:
            # a long command line wraps once in the stacked layout
            panel.add_row(_row(1, [(f"{k:<9}", 6), (v[:inner - 12], 3)]))
            panel.add_row(_row(1, [(" " * 9, 6), (v[inner - 12:2 * (inner - 12)], 3)]))
            n_f += 1
            continue
        panel.add_row(_row(1, _clip_runs([(f"{k:<9}", 6), (v, 3)], inner - 2)))
    keys = []
    if key_rows:
        open_note = "open the session" if (row.get("group") or {}).get("attachable") or \
            (row.get("group") or {}).get("kind") in ("systemd", "docker") else ""
        keys = ([("Enter", 9), (f" {open_note}  ", 6)] if open_note else []) + \
               [("e", 9), (" end (SIGTERM)  ", 6), ("k", 9), (" kill (SIGKILL)", 6)]
    left = body - len(panel.rows) - (3 if keys else 0)
    if left >= 6:
        panel.add_row([])
        for r in _detail_charts(row, track, inner - 2, left - 1, span, rate, cc, mem_top):
            panel.add_row(r)
    while len(panel.rows) < body - (2 if keys else 0):
        panel.add_row([])
    if keys:
        panel.add_row(_row(1, keys))
        panel.add_row(_row(1, [("every action asks y to confirm; another user's process needs root", 6)]))
    return panel


# --- the large layout's GPU cards -----------------------------------------

def _bar(pct, width, slot=None):
    bar, s = term.make_bar(pct, width)
    filled = bar.rstrip("░")
    s = slot or s
    return [(filled, s), (bar[len(filled):], 7)] if filled else [(bar, 7)]


def _pad_window(values, window):
    """The newest `window` samples, None-padded on the left: a process's
    line starts where it was first seen instead of rising from a made-up
    zero (braille_chart skips None)."""
    vals = list(values)[-window:]
    return [None] * (window - len(vals)) + vals


def _gpu_procs(root):
    return [p for gr in root.get("groups") or [] for p in gr.get("procs") or []]


def _line_rows(col, series, width, height, y_max, labels):
    cw = width - _LABEL_W - 1
    rows = term.braille_chart(series, cw, height, y_max) if cw > 4 else None
    if rows is None:
        return []
    out = []
    for r, runs in enumerate(rows):
        lab = labels.get(r)
        out.append([(col, f"{(lab or ''):>{_LABEL_W - 1}}", 6), (None, "┤" if lab else "│", 10)] + _row(None, runs))
    return out


def _build_gpu_card(y, x0, width, root, sample_gpu, cap, track, rate, span, chart_h, mem_h, cc):
    inner = width - 2
    procs = _gpu_procs(root)
    n = len(procs)
    title = [(f"GPU {root['idx']}", 9), (f"  {root.get('name') or ''}", 8)]
    p = panels.Panel(y, x0, width, title=title,
                     title_right=[(f"{n} process" + ("" if n == 1 else "es"), 6)], kind="top")
    g = sample_gpu or {}
    try:
        util = float(g.get("util") or 0)
    except (TypeError, ValueError):
        util = 0.0
    bw = max(10, (inner - 60) // 2)
    runs = [("SM ", 6)] + _bar(util, bw) + [(f" {util:3.0f}%   ", 8)]
    local = cap.get("mem_local", True)
    used, total = g.get("mem_used"), g.get("mem_total")
    try:
        mem_pct = float(used) / float(total) * 100 if local and total else None
    except (TypeError, ValueError, ZeroDivisionError):
        mem_pct = None
    gpu_mem = sum(pp.get("gpu_mem") or 0 for pp in procs)
    if mem_pct is not None:
        runs += [("MEM ", 6)] + _bar(mem_pct, bw, 5) + [(f" {term.fmt_mem(used)} / {term.fmt_mem(total)}", 3)]
    else:
        runs += [("MEM ", 6), (f"{term.fmt_mem(gpu_mem)} of unified RAM", 3)]
    right = []
    if g.get("enc_util") is not None:
        right = [("ENC ", 6), (f"{g.get('enc_util')}%", 8), ("  DEC ", 6), (f"{g.get('dec_util') or 0}%", 8)]
    p.add_row(_row(1, runs), right=right)
    window = _samples(span, rate)
    hist = (track or {}).get("proc") or {}
    gid = str(root.get("id"))
    top3 = sorted(procs, key=lambda pp: -(pp.get("sm") or 0) - (pp.get("gpu_mem") or 0) / 1e15)[:3]
    if chart_h:
        legend = []
        series = []
        for k, pp in enumerate(top3):
            h = hist.get((gid, pp["pid"])) or {}
            s = SERIES_SLOTS[k]
            if len(h.get("sm") or []) >= 2:
                series.append((_pad_window(h["sm"], window), s))
            legend += [("━ " if term.BAR_STYLE != "ascii" else "- ", s), (f"{pp['pid']} {pp.get('name') or ''}"[:22], 3),
                       ("   ", 3)]
        p.add_row(_row(1, [("SM by process", 6), (f" · last {term.fmt_span(span)}", 6)]), right=legend[:-1])
        rows = _line_rows(1, list(reversed(series)), inner - 1, chart_h, 100, _labels_pct(chart_h)) if series else []
        for r in rows or [[(1, "    history fills in as processes run", 6)]] + [[] for _ in range(chart_h - 1)]:
            p.add_row(r)
    if mem_h:
        by_mem = sorted(procs, key=lambda pp: -(pp.get("gpu_mem") or 0))[:3]
        acc = None
        stacked = []
        for k, pp in enumerate(by_mem):
            h = hist.get((gid, pp["pid"])) or {}
            vals = _pad_window(h.get("mem") or [], window)
            acc = vals if acc is None else [(a or 0) + v if v is not None else a for a, v in zip(acc, vals)]
            stacked.append((list(acc), SERIES_SLOTS[k]))
        top = float(total) if (local and total) else max(max([v for v in acc or [] if v is not None] or [0]) * 1.25, 1.0)
        p.add_row(_row(1, [("GPU memory by process", 6), (" · stacked", 6)]),
                  right=[("a steady climb here is a leak", 6)])
        rows = _line_rows(1, list(reversed(stacked)), inner - 1, mem_h, top,
                          {0: _axis_mem(top), mem_h - 1: "0"}) if stacked else []
        for r in rows or [[] for _ in range(mem_h)]:
            p.add_row(r)
    p.add_row(_axis(1, inner - 1, span))
    return p


# --- footer ---------------------------------------------------------------

def _build_tasks_footer(x0, width, y, sample, ui, theme_toast=None):
    """Confirm prompt / toast take the whole line (same precedence
    pages._build_footer gives knobs); then the filter prompt while typing;
    otherwise the page-5 key ladder + info."""
    if theme_toast and not (ui and (ui.get("confirming") or ui.get("toast"))):
        p = panels.Panel(y, x0, width, kind="plain")
        p.rows.append(([(f" {theme_toast[0]}", theme_toast[1])], []))
        return p
    if ui and (ui.get("confirming") or ui.get("toast")):
        return pages._build_footer(x0, width, y, sample.get("driver"), sample.get("cuda"), sample.get("rate", 1.0),
                                   (sample.get("caps") or {}).get("has_nvml", False), knob_ui=ui)
    if ui and ui.get("editing"):
        p = panels.Panel(y, x0, width, kind="plain")
        p.rows.append(([(" filter: ", 9), ((ui.get("filter") or "") + "_", 8),
                        ("   pid, name, command, user or session · Enter keep · Esc clear", 6)], []))
        return p
    ud = "↑↓" if term.BAR_STYLE != "ascii" else "up/dn"
    if ui is None:
        variants = [" q quit · 1 2 5 page · snapshot is read-only — run with -l to manage tasks",
                    " q quit · 1 2 5 page · read-only snapshot", " 1 2 5 page"]
    else:
        variants = [f" {ud} select · Enter open · e end · k kill · s sort · f flat · / filter · a holders · ? help",
                    f" {ud} select · Enter open · e end · k kill · s sort · f flat · / filter · ? help",
                    f" {ud} · Enter · e · k · s · f · / · a · ?", " 1 2 5 page"]
    src = "NVML" if (sample.get("caps") or {}).get("has_nvml") else "CLI"
    info_parts = [f"v{VERSION}", src]
    driver, cuda = sample.get("driver"), sample.get("cuda")
    if driver and str(driver) != "Unknown":
        info_parts.append(f"driver {driver}")
    if cuda and str(cuda) != "Unknown":
        info_parts.append(f"CUDA {cuda}")
    info_parts.append(f"{sample.get('rate', 1.0):g}s")
    # Keys matter more than the info line: each key variant first tries
    # the full info, then the version alone, then none.
    infos = [" · ".join(info_parts), f"v{VERSION}", ""]
    keys, info = variants[-1], ""
    done = False
    for v in variants:
        for inf in infos:
            if len(v) + (len(inf) + 2 if inf else 0) <= width:
                keys, info, done = v, inf, True
                break
        if done:
            break
    p = panels.Panel(y, x0, width, kind="plain")
    p.rows.append(([(keys, 9)], [(info, 2)] if info else []))
    return p


# --- the page -----------------------------------------------------------------

def _gpu_ids(rows, tree):
    """Stamps each row with its GPU's sample id (history keys use it)."""
    roots = {r["idx"]: r for r in (tree or {}).get("gpus") or []}
    for r in rows:
        root = roots.get(r.get("gpu_idx")) or {}
        r["gpu_id"] = root.get("id")
        r["gpu_mem_total"] = root.get("mem_total")
    return rows


def build_page5(sample, tree, rows, tier, width, x0=0, height=None, ui=None, theme_toast=None,
                cluster_tabs=False, has_perf=False, track=None, detail=None, span=None,
                layout=None, now=None):
    """Page 5. `layout` is bigpage.layout_for()'s answer for this terminal
    ("large"/"xl" use the big layout, anything else the normal one);
    `width`/`x0` are the content box for the normal layout and the whole
    terminal for the large one. `track` is ProcTracker.snapshot() (None in
    a one-shot snapshot), `detail` is proc_detail() of the selected
    process. Height is honoured by scrolling the tree, never by dropping
    columns."""
    sample = sample or {}
    tree = tree or {}
    rows = _gpu_ids(list(rows or []), tree)
    rate = sample.get("rate", 1.0) or 1.0
    cc = column_caps(sample)
    sel_row = selected_proc_row(rows, ui)
    height = height or 40
    now = now or time.time()
    if layout in ("large", "xl"):
        return _build_large(sample, tree, rows, width, x0, height, ui, theme_toast, cluster_tabs, has_perf,
                            track, detail, span or bigpage.DEFAULT_HISTORY.get(layout, 300), cc, sel_row, rate, now)
    out = [pages._build_header(x0, width, tier, page=5, cluster_tabs=cluster_tabs, has_perf=has_perf)]
    y = 1
    cols = task_cols(tier, cc, spark_w=12 if track and width >= 140 else 0)
    # lines between the header and the footer; panels stack with no gap
    room = max(5, height - 2)
    n_ended = len(collapse_ended((track or {}).get("ended") or []))
    want_detail = sel_row is not None and width >= 70
    det_h = 0
    if want_detail:
        # tree rows + column header + the frame's two lines
        spare = room - (len(rows) + 1 + 2)
        if spare >= DETAIL_MIN:
            # the detail takes the spare height, less what a few recently
            # ended rows need (they live in the tasks frame)
            ended_rows = min(n_ended, 5) + 2 if n_ended else 0
            det_h = spare - ended_rows if spare - ended_rows >= 12 else spare
        elif room >= 26:
            det_h = DETAIL_MIN + 2
        if det_h and (not track or term.BAR_STYLE != "eighths"):
            # no charts to draw: the fields alone (plus the key lines)
            det_h = min(det_h, len(detail_fields(sel_row, detail, now)) + 2 + (2 if ui else 0))
    tab_rows = room - 2 - det_h
    panel = _build_tasks_panel(y, x0, width, tree, rows, cols, ui, tab_rows, track, rate, fill=bool(det_h), now=now)
    out.append(panel)
    y += panel.total_height + 1          # + its closing border
    if det_h:
        out.append(_build_detail_panel(y, x0, width, det_h, sel_row, detail, track, rate,
                                       span or 300, cc, ui, side=width >= 110, now=now))
        y += det_h
    y = min(y, height - 1)
    out.append(_build_tasks_footer(x0, width, y, sample, ui, theme_toast=theme_toast))
    return out


def _build_large(sample, tree, rows, width, x0, height, ui, theme_toast, cluster_tabs, has_perf,
                 track, detail, span, cc, sel_row, rate, now):
    out = [pages._build_header(x0, width, "wide", page=5, cluster_tabs=cluster_tabs, has_perf=has_perf)]
    roots = tree.get("gpus") or []
    gpus = sample.get("gpus") or []
    caps = (sample.get("caps") or {}).get("gpu") or []
    y = 1
    if roots:
        per_row = min(len(roots), 3)
        n_rows = (len(roots) + per_row - 1) // per_row
        top_h = max(14, int(height * 0.45)) // n_rows
        # title, bars, axis, closing border, plus a label row per chart
        charts = top_h - 4 - (2 if cc["sm"] else 1)
        chart_h = max(3, charts * 3 // 5) if cc["sm"] else 0
        mem_h = max(2, charts - chart_h)
        cw = width // per_row
        for ri in range(n_rows):
            heights = []
            for ci in range(per_row):
                k = ri * per_row + ci
                if k >= len(roots):
                    break
                root = roots[k]
                idx = root["idx"]
                w = cw if ci < per_row - 1 else width - cw * (per_row - 1)
                card = _build_gpu_card(y, x0 + ci * cw, w, root, gpus[idx] if idx < len(gpus) else {},
                                       caps[idx] if idx < len(caps) else {}, track, rate, span, chart_h, mem_h, cc)
                out.append(card)
                heights.append(card.total_height + 1)
            y += max(heights or [0])
    bottom = height - 1 - y
    tw = width if width < 200 else max(120, int(width * 0.62))
    dw = width - tw
    if dw < 60 or sel_row is None:
        tw, dw = width, 0
    cols = task_cols("wide", cc, spark_w=20 if track else 0)
    out.append(_build_tasks_panel(y, x0, tw, tree, rows, cols, ui, bottom - 2, track, rate, fill=True, now=now))
    if dw:
        out.append(_build_detail_panel(y, x0 + tw, dw, bottom, sel_row, detail, track, rate, SPARK_SPAN,
                                       cc, ui, side=False, now=now))
    out.append(_build_tasks_footer(x0, width, height - 1, sample, ui, theme_toast=theme_toast))
    return out
