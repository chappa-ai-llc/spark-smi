"""Page builders. build_page1(state, tier, width, x0) turns a plain-dict
sample (see app.py's State.sample()) into a list of positioned panels.Panel
objects -- no drawing happens here, panels.render() does that. Every panel
build is wrapped defensively: missing/degraded collector data must never
stop the rest of the page from rendering.
"""
import platform
import re
import time

import psutil

from . import panels
from . import term

# --- tier helpers -----------------------------------------------------
def tier_for_width(w):
    if w < 84:
        return "compact"
    if w <= 110:
        return "standard"
    return "wide"

def content_width(tier, term_width):
    cap = {"compact": 80, "standard": 110, "wide": 160}.get(tier, 110)
    return max(40, min(term_width, cap))


class _Flow:
    """Left-to-right layout helper for one row: tracks how much of an
    available width has been consumed so optional trailing items can be
    dropped WHOLE -- never truncated mid-word/mid-token -- once space runs
    out. Segments are emitted with col=None (each flows immediately after
    the previous one); the caller re-tags the first segment's col once the
    row is built (see the `segs[0] = (col, ...)` idiom below)."""

    def __init__(self, avail):
        self.avail = max(0, avail)
        self.used = 0
        self.segs = []

    def room(self):
        return self.avail - self.used

    def add(self, text, slot):
        self.segs.append((None, text, slot))
        self.used += len(text)

    def try_add(self, text, slot):
        """Adds one (text, slot) run if it fits; returns whether it did."""
        if len(text) <= self.room():
            self.add(text, slot)
            return True
        return False

    def try_add_multi(self, parts):
        """Adds several (text, slot) runs atomically -- all or nothing, so
        a multi-part item (e.g. a legend square + its label) never gets
        split with only the square landing."""
        total = sum(len(t) for t, _ in parts)
        if total <= self.room():
            for t, s in parts:
                self.add(t, s)
            return True
        return False


def _fmt_uptime(seconds):
    try:
        seconds = int(seconds)
    except Exception:
        return "N/A"
    d, rem = divmod(seconds, 86400)
    h, rem = divmod(rem, 3600)
    m, _ = divmod(rem, 60)
    if d:
        return f"{d}d {h:02}:{m:02}"
    return f"{h:02}:{m:02}"

def _safe(fn, default):
    try:
        return fn()
    except Exception:
        return default

def _read_text_stripped(path):
    try:
        with open(path, "rb") as f:
            data = f.read()
        text = data.decode("utf-8", "ignore").replace("\x00", "").strip()
        return text or None
    except Exception:
        return None

def _machine_model():
    """DGX Spark (and other ARM boards) identify themselves via the
    device-tree "model" property; x86 desktops/servers expose the same idea
    via DMI. Neither is fabricated -- the header segment is simply omitted
    when both are absent (e.g. this dev box, or a VM)."""
    # DMI names arrive underscore-separated on some firmware ("NVIDIA_DGX_Spark")
    m = (_read_text_stripped("/proc/device-tree/model")
         or _read_text_stripped("/sys/devices/virtual/dmi/id/product_name"))
    return m.replace("_", " ") if m else None

def _short_mem(bytes_val):
    """Ultra-compact single-letter-unit form used only in the compact-tier
    MEMORY row suffix ("68.5/121.7G", matching mock_compact) -- fmt_mem's
    normal "68.5 GiB" doesn't fit that row at 80 columns."""
    try:
        return f"{bytes_val / (1024 ** 3):.1f}G"
    except Exception:
        return "N/A"


# =========================================================================
# Header / footer (plain, borderless panels)
# =========================================================================

def _build_header(x0, width, tier):
    host = _safe(platform.node, "host") or "host"
    model = _machine_model()
    arch = _safe(platform.machine, "") or ""
    uptime = _fmt_uptime(time.time() - _safe(psutil.boot_time, time.time()))
    clock = time.strftime("%H:%M:%S")

    if tier == "compact":
        chip = model or arch
        core = f" │ {host}" + (f" · {chip}" if chip else "")
        right = [("▌1▐ 2  ", 9), (clock, 3)]
        p = panels.Panel(0, x0, width, kind="plain")
        p.rows.append(([(" SPARK-SMI 2.0", 9), (core, 3)], right))
        return p

    right = [("▌1▐ OVERVIEW  2 ADVANCED  ", 9), (clock, 3)]
    right_len = sum(len(t) for t, _ in right)

    # Never truncate mid-token: drop whole optional segments right-to-left
    # in this priority order (arch first, then uptime, then model) until
    # the line fits.
    show = {"model": bool(model), "arch": bool(arch), "uptime": True}

    prefix = " SPARK-SMI 2.0"

    def build():
        tail = [t for t, key in ((model, "model"), (arch, "arch")) if show[key] and t]
        s = f" │ {host}"
        if tail:
            s += " — " + " · ".join(tail)
        if show["uptime"]:
            s += f" │ up {uptime}"
        return s

    core = build()
    for key in ("arch", "uptime", "model"):
        if len(prefix) + len(core) + right_len + 1 <= width:
            break
        show[key] = False
        core = build()

    p = panels.Panel(0, x0, width, kind="plain")
    p.rows.append(([(prefix, 9), (core, 3)], right))
    return p


def _build_footer(x0, width, y, driver, cuda, rate, has_nvml):
    # Ladder of key-bar variants, most to least detailed. Tried together
    # with the driver/CUDA info first; if none fit alongside it, the info
    # is dropped (not the keys) and the shortest keys variant that fits on
    # its own is used -- so the info block degrades consistently by width
    # rather than disappearing at one width and reappearing at another.
    variants = [
        " q quit · 1 2 page · t °C/°F · u GiB/GB · n active NICs · s sort · ? help",
        " q quit · 1 2 page · t °C/°F · u GiB/GB · n NICs",
        " q quit · 1 2 page · t temp · u units",
    ]
    src = "NVML" if has_nvml else "CLI"
    info = f"{src} · driver {driver} · CUDA {cuda} · {rate:g}s"

    keys, show_info = variants[-1], False
    for v in variants:
        if len(v) + len(info) + 2 <= width:
            keys, show_info = v, True
            break
    else:
        for v in variants:
            if len(v) <= width:
                keys, show_info = v, False
                break

    p = panels.Panel(y, x0, width, kind="plain")
    right = [(info, 2)] if show_info else []
    p.rows.append(([(keys, 9)], right))
    return p


# =========================================================================
# CPU + MEMORY (one compound frame: CPU is 'top', MEMORY continues as 'mid')
# =========================================================================

def _build_cpu_panel(y, x0, width, cpu_state, tier, show_sparkline):
    clusters = cpu_state.get("clusters") or []
    summary = " + ".join(f"{c['label']} ×{len(c['cores'])}" for c in clusters) or "CPU"
    temp = term.fmt_temp(cpu_state.get("temp"))
    loadavg = cpu_state.get("loadavg")
    right = [(f"{temp}", 2)]
    if loadavg:
        right = [(f"{temp} · load {loadavg[0]:.2f} {loadavg[1]:.2f} {loadavg[2]:.2f}", 2)]

    p = panels.Panel(y, x0, width, title=[("CPU", 9), (f" {summary}", 3)],
                      title_right=right, kind="top")
    inner = width - 2

    if not clusters:
        p.add_row([(1, "no CPU data", 6)])
        return p

    if tier == "compact":
        # Packs "label bar pct% strip" for every cluster into as few rows as
        # fit -- there's no room for GHz or the "cores ..." range text at
        # this width (mock_compact keeps the strip, drops the range text).
        cell_w = max(18, inner // max(1, len(clusters)))
        rows, cur = [[]], 0
        for cl in clusters:
            if cur + cell_w > inner and rows[-1]:
                rows.append([])
                cur = 0
            # "Cortex-A725" -> "A725": the family prefix is dead weight at
            # this width and truncating it instead yields "Corte"
            short = re.sub(r"^(Cortex|Neoverse)-", "", cl.get("label", ""))
            f = _Flow(cell_w - 1)
            f.try_add_multi([(f"{short[:5]:<5}", 3)])
            lb, rb = term.bar_brackets()
            bar, slot = term.make_bar(cl.get("avg", 0.0), 12)
            f.try_add_multi([(lb, 6), (bar, slot), (rb, 6), (f"{int(cl.get('avg', 0)):>3}%", 3)])
            strip = term.core_strip(cl.get("loads") or [])
            if strip:
                f.try_add(" " + strip, 1)
            segs = list(f.segs)
            if segs:
                segs[0] = (cur + 1, segs[0][1], segs[0][2])
            rows[-1].extend(segs)
            cur += cell_w
        for r in rows:
            p.add_row(r)
        return p

    for cl in clusters:
        avg = cl.get("avg", 0.0)
        f = _Flow(inner - 1)
        f.add(f"{cl.get('label', ''):<12}", 3)
        f.add(f"{cl.get('ghz', 0):>5.2f} GHz  ", 8)
        bar_w = 20 if tier == "wide" else 16
        lb, rb = term.bar_brackets()
        bar, slot = term.make_bar(avg, bar_w)
        f.add(lb, 6)
        f.add(bar, slot)
        f.add(rb, 6)
        f.add(f" {avg:>3.0f}%  ", 3)
        f.try_add(f"cores {cl.get('range', '')}", 2)
        # The strip gets its own >=3-space gap baked into ONE atomic add, so
        # a tight row drops the whole "gap + strip" together rather than
        # ever emitting a bare gap with nothing after it.
        strip = term.core_strip(cl.get("loads") or [])
        if strip:
            f.try_add("   " + strip, 1)
        if show_sparkline:
            room = min(f.room() - 2, 20)  # capped so it doesn't balloon into all leftover width
            if room >= 4:
                spark = term.sparkline(cl.get("history") or [], room)
                if spark:
                    f.add("  " + spark, 2)
        segs = list(f.segs)
        segs[0] = (1, segs[0][1], segs[0][2])
        p.add_row(segs)
    return p


def _build_memory_panel(y, x0, width, mem_state, gpu_caps, tier, show_legend):
    vm = mem_state.get("vm")
    swap = mem_state.get("swap")
    gpu_alloc = mem_state.get("gpu_alloc") or 0
    unified = any(not c.get("mem_local", True) for c in (gpu_caps or []))

    if vm is not None:
        total_str = term.fmt_mem(vm.total)
        pct = vm.percent
    else:
        total_str, pct = "N/A", 0
    subtitle = f"{total_str}" + (" · unified CPU+GPU address space" if unified else "")

    p = panels.Panel(y, x0, width, title=[("MEMORY", 9), (f" {subtitle}", 3)], kind="mid")

    if vm is None:
        p.add_row([(2, "memory data unavailable", 6)])
        return p

    used_other = max(0, vm.used - gpu_alloc)
    cache = getattr(vm, "cached", 0) or getattr(vm, "buffers", 0) or 0
    free = max(0, vm.total - vm.used)
    # gpu_alloc is the NVML per-process sum (MemoryCollector._nvml_gpu_alloc_sum)
    # -- NEVER derived from psutil arithmetic. It's typically 0 on GB10 (no
    # compute contexts tracked that way for the integrated GPU), in which
    # case the segment and legend item are simply absent below.
    segs_spec = [(used_other / vm.total, 1, "█")]
    if gpu_alloc > 0:
        segs_spec.append((gpu_alloc / vm.total, 2, "▓"))
    if cache > 0:
        segs_spec.append((cache / vm.total, 5, "▒"))
    segs_spec.append((free / vm.total, 7, "░"))

    if tier == "compact":
        # No legend row at all in compact -- the single bar line carries
        # both the percent and a short used/total suffix (mock_compact).
        # The suffix is longer here than the plain " NN%" case below, so its
        # length is reserved from the bar budget BEFORE sizing the bar --
        # never truncate the suffix itself to make room.
        suffix = f" {int(pct)}% {_short_mem(vm.used)}/{_short_mem(vm.total)}"
        bar_w = max(8, width - 7 - len(suffix))
        chunks = term.seg_bar(segs_spec, bar_w)
        p.add_row(panels.seg_bar_segments(2, chunks, suffix=suffix, suffix_slot=3))
        return p

    # Row starts at col 2; brackets + " NNN%" suffix need ~7 cols of the
    # remaining (width-2) budget on top of the bar itself.
    bar_w = max(10, width - 13)
    chunks = term.seg_bar(segs_spec, bar_w)
    p.add_row(panels.seg_bar_segments(2, chunks, suffix=f" {int(pct)}%", suffix_slot=3))

    if show_legend:
        swap_text = ""
        if swap is not None and getattr(swap, "total", 0) > 0:
            swap_text = f"swap {term.fmt_mem(swap.used)} / {term.fmt_mem(swap.total)}"
        reserve = len(swap_text) + 2 if swap_text else 0
        f = _Flow(max(0, width - 4 - reserve))
        f.try_add_multi([("■", 1), (f" processes {term.fmt_mem(used_other)}", 3)])
        if gpu_alloc > 0:
            f.try_add_multi([("   ■", 2), (f" gpu-alloc {term.fmt_mem(gpu_alloc)}", 3)])
        # Degrade order per review: drop whole items right-to-left (free,
        # then cache) rather than truncate mid-word. A plain left-to-right
        # try_add already yields exactly that ordering, since "free" is the
        # last item attempted and so the first one dropped when room is short.
        if cache > 0:
            f.try_add_multi([("   ■", 5), (f" cache {term.fmt_mem(cache)}", 3)])
        f.try_add_multi([("   ■", 7), (f" free {term.fmt_mem(free)}", 3)])
        segs = list(f.segs)
        if segs:
            segs[0] = (2, segs[0][1], segs[0][2])
        right = [(swap_text, 3)] if swap_text else []
        p.add_row(segs, right=right)
    return p


# =========================================================================
# GPU cards
# =========================================================================

def _build_gpu_card(y, x0, w, gpu, caps, show_sparkline):
    name = gpu.get("name", "Unknown")
    temp = term.fmt_temp(gpu.get("temp"))
    pwr = gpu.get("pwr_str", "N/A")
    title = f"GPU {gpu.get('id', '?')} {name}"
    inner = w - 2
    if len(title) > inner - 2:
        title = title[:inner - 3] + "…"
    p = panels.Panel(y, x0, w, title=[(title, 9)], title_right=[(f"{temp} · {pwr}", 2)], kind="top")

    unified = not caps.get("mem_local", True)
    util = gpu.get("util", 0) or 0
    bar_w = max(6, inner - 24)

    f1 = _Flow(inner - 1)
    f1.add("UTIL", 3)
    f1.add("   ", 3)
    lb, rb = term.bar_brackets()
    bar, slot = term.make_bar(util, bar_w)
    f1.add(lb, 6)
    f1.add(bar, slot)
    f1.add(rb, 6)
    f1.add(f" {int(util):>3}%", 3)
    if show_sparkline:
        room = min(f1.room() - 2, 16)  # capped so it doesn't balloon into all leftover width
        if room >= 4:
            spark = term.sparkline(gpu.get("history") or [], room)
            if spark:
                f1.add("  " + spark, 2)
    segs1 = list(f1.segs)
    segs1[0] = (2, segs1[0][1], segs1[0][2])
    p.add_row(segs1)

    if unified:
        # No invented number here: GB10 doesn't reliably expose a true
        # GPU-attributable memory figure (that's what MemoryCollector's
        # NVML per-process sum is for, shown in the MEMORY legend when
        # nonzero) -- the card just states the fact.
        p.add_row([(2, "MEM", 3), (9, "shares system RAM (unified)", 3)])
    else:
        used, total = gpu.get("mem_used"), gpu.get("mem_total")
        try:
            pct = (used / total) * 100 if total else 0
        except Exception:
            pct = 0
        mem_bar_w = max(6, bar_w - 18)  # leave room for "NNN.N GiB/NNN.N GiB"
        row2 = [(2, "MEM", 3)] + panels.bar_segments(9, pct, mem_bar_w)
        row2.append((9 + mem_bar_w + 2, f"{term.fmt_mem(used)}/{term.fmt_mem(total)}", 3))
        p.add_row(row2)

    # Clock text needs BOTH the capability flag and an actual sampled value
    # -- GpuCollector's "clocks" cap now falls back to a CLI probe when NVML
    # can't read it (verified: GB10's own clock query fails there while
    # `nvidia-smi --query-gpu=clocks.sm` works), matching the sample-side
    # fallback in collectors._collect_gpus.
    clk_bits = []
    clk = gpu.get("clk_sm")
    if caps.get("clocks") and clk not in (None, "N/A"):
        clk_bits.append(f"{clk} MHz sm")
    clk_bits.append(f"PWR {pwr}")
    if caps.get("fan"):
        fan = gpu.get("fan", "N/A")
        if fan not in ("N/A", "None"):
            clk_bits.append(f"FAN {fan}")
    p.add_row([(2, "CLK", 3), (9, " · ".join(clk_bits), 2)])
    return p


def _build_gpu_row(y, x0, width, tier, gpus, gpu_caps, show_sparkline):
    """Returns (panels, height) for one row of GPU cards -- side by side in
    wide tier (space permitting), stacked full-width otherwise."""
    if not gpus:
        return [], 0
    per_row = 2 if (tier == "wide" and width >= 90 and len(gpus) > 1) else 1
    out = []
    row_h = 0
    for chunk_start in range(0, len(gpus), per_row):
        chunk = gpus[chunk_start:chunk_start + per_row]
        gap = 2
        card_w = (width - gap * (len(chunk) - 1)) // len(chunk) if len(chunk) > 1 else width
        cx = x0
        chunk_h = 0
        for j, gpu in enumerate(chunk):
            idx = chunk_start + j
            caps = gpu_caps[idx] if idx < len(gpu_caps) else {}
            w = card_w if j < len(chunk) - 1 else (width - cx + x0)
            card = _build_gpu_card(y, cx, w, gpu, caps, show_sparkline)
            out.append(card)
            chunk_h = max(chunk_h, card.total_height + 1)
            cx += card_w + gap
        y += chunk_h
        row_h += chunk_h
    return out, row_h


# =========================================================================
# NETWORK + STORAGE (one compound frame, same as CPU+MEMORY)
# =========================================================================

def _net_storage_columns(inner):
    """Shared column widths for the joined NETWORK/STORAGE frame's first
    three fields (netdev/device name, hardware/model, link-speed/temp) --
    kept identical between the two panels so their columns line up
    vertically, matching mock_page1.rendered.txt. hw_w shrinks below the
    mock's 29 as the terminal narrows; the trailing 40 is a rough reserve
    for whatever each panel puts in the rest of the row."""
    name_w = 15
    link_w = 8
    hw_w = max(14, min(29, inner - name_w - link_w - 40))
    return name_w, hw_w, link_w


def _build_network_compact_rows(p, inner, nics):
    """mock_compact NET form: two groups per cell-row. Per spec, the
    hardware-derived short names in the mock ("cx7 p0") are NOT required --
    the netdev name, truncated, is used instead."""
    cell_w = max(20, inner // 2)
    rows, cur = [[]], 0
    for n in nics:
        if cur + cell_w > inner and rows[-1]:
            rows.append([])
            cur = 0
        f = _Flow(cell_w - 1)
        f.add(f"{n.get('name', '?')[:8]:<9}", 8)
        if n.get("up"):
            f.add(f"{n.get('speed_str', '?'):<5}", 3)
            lb, rb = term.bar_brackets()
            pct = max(n.get("rx_pct", 0) or 0, n.get("tx_pct", 0) or 0)
            bar, slot = term.make_bar(pct, 10)
            f.try_add_multi([(lb, 6), (bar, slot), (rb, 6), (f" {int(pct):>2}%", 3)])
        else:
            f.add("down ", 4)
            f.try_add(f"— {n.get('down_reason', 'no carrier')}", 6)
        segs = list(f.segs)
        if segs:
            segs[0] = (cur + 1, segs[0][1], segs[0][2])
        rows[-1].extend(segs)
        cur += cell_w
    if len(nics) % 2 == 1 and rows[-1]:
        # Odd group count leaves one cell empty -- fill it with a summary
        # rather than a blank gap (mock_compact: "4 PFs · all ports up").
        # Only count PFs belonging to a MULTI-pf group -- a lone Realtek
        # isn't a "PF" worth mentioning here, it's just "the NIC".
        multi_pf_total = sum(n.get("pf_count", 1) for n in nics if n.get("pf_count", 1) > 1)
        up = sum(1 for n in nics if n.get("up"))
        down = len(nics) - up
        if multi_pf_total:
            status = "all ports up" if up == len(nics) else f"{up}/{len(nics)} up"
            summary = f"{multi_pf_total} PFs · {status}"
        else:
            summary = "all up" if down == 0 else f"{down} down"
        rows[-1].append((cur + 1, summary, 6))
    for r in rows:
        p.add_row(r)
    return p


def _build_network_panel(y, x0, width, nics, collapse, tier):
    inner = width - 2
    p = panels.Panel(y, x0, width,
                      title=[("NETWORK", 9), (" physical ports · RDMA read from HCA counters", 3)],
                      kind="top")
    if not nics:
        p.add_row([(1, "no network interfaces detected", 6)])
        return p
    if collapse:
        up = sum(1 for n in nics if n.get("up"))
        p.add_row([(1, f"{len(nics)} interfaces · {up} up", 3)])
        return p
    if tier == "compact":
        return _build_network_compact_rows(p, inner, nics)

    name_w, hw_w, link_w = _net_storage_columns(inner)
    rate_w = 11
    for n in nics:
        f = _Flow(inner - 1)
        f.add(f"{n.get('name', '?')[:name_w - 1]:<{name_w}}", 8)

        hw_text = n.get("hw_label", "?")
        pf = n.get("pf_count", 1)
        pf_marker = f" ×{pf} PF" if pf > 1 else ""
        name_part = hw_text[:max(0, hw_w - len(pf_marker))]
        pad = max(0, hw_w - len(name_part) - len(pf_marker))
        f.add(name_part, 3)
        if pf_marker:
            f.add(pf_marker, 6)
        f.add(" " * pad, 3)

        up = n.get("up")
        if not up:
            f.add(f"{'down':<{link_w}}", 4)
            f.try_add(f"— {n.get('down_reason', 'no carrier')}", 6)
            segs = list(f.segs)
            segs[0] = (1, segs[0][1], segs[0][2])
            p.add_row(segs)
            continue
        f.add(f"{n.get('speed_str', '?'):<{link_w}}", 3)

        # Suffix is ONE separator space + the number right-justified to 2
        # digits (not 3) -- a stray extra `:>3` here previously reserved a
        # column no mock row actually uses (a "reserve the right-group
        # twice" flavor of the STORAGE bar bug fixed below).
        lb, rb = term.bar_brackets()
        f.add(f"{term.fmt_rate(n.get('rx_bps', 0), 'bit'):<{rate_w}}", 2)
        rx_bar, rx_slot = term.make_bar(n.get("rx_pct", 0) or 0, 10)
        f.try_add_multi([(lb, 6), (rx_bar, rx_slot), (rb, 6), (f" {int(n.get('rx_pct', 0) or 0):>2}% ", 3)])
        f.add(f"{term.fmt_rate(n.get('tx_bps', 0), 'bit'):<{rate_w}}", 2)
        tx_bar, tx_slot = term.make_bar(n.get("tx_pct", 0) or 0, 10)
        f.try_add_multi([(lb, 6), (tx_bar, tx_slot), (rb, 6), (f" {int(n.get('tx_pct', 0) or 0):>2}%", 3)])

        segs = list(f.segs)
        segs[0] = (1, segs[0][1], segs[0][2])
        p.add_row(segs)
    return p


# =========================================================================
# STORAGE (continues the NETWORK frame, same 'mid' pattern as MEMORY/CPU)
# =========================================================================

def _build_storage_compact_rows(p, inner, disks):
    for d in disks:
        f = _Flow(inner - 1)
        f.add(f"{d.get('name', '?')[:10]:<11}", 8)
        temp = d.get("temp")
        f.add(f"{term.fmt_temp(temp):<5}" if temp is not None else "—    ", 3 if temp is not None else 6)
        f.add(f"R {term.fmt_rate(d.get('read_bps', 0), 'byte')} W {term.fmt_rate(d.get('write_bps', 0), 'byte')}", 2)
        segs = list(f.segs)
        if segs:
            segs[0] = (1, segs[0][1], segs[0][2])
        right = []
        used_pct = d.get("used_pct")
        if used_pct is not None:
            lb, rb = term.bar_brackets()
            bar, slot = term.make_bar(used_pct, 10)
            right = [(lb, 6), (bar, slot), (rb, 6), (f" {int(used_pct):>2}%", 3)]
        p.add_row(segs, right=right)
    return p


def _model_size_segments(model, size_str, model_w):
    """(prefix, size, pad) segments for the STORAGE model column: model is
    truncated to reserve >=1 separating space before size_str AND >=1
    trailing space before whatever column follows (temp), and the three
    pieces always sum to exactly model_w so temp has a fixed stop regardless
    of how long/short the model string is. A bare `model[:avail]` slice got
    this wrong twice over: a model text longer than its budget ate the
    separating space too (running straight into size), and even once that
    was reserved, a size_str that exactly filled the remainder left no gap
    before temp (size ran straight into temp)."""
    max_model_len = max(1, model_w - len(size_str) - 2)
    model_trunc = (model or "")[:max_model_len].rstrip() or (model or "?")[:1]
    prefix = f"{model_trunc} "
    remaining = max(0, model_w - len(prefix) - 1)  # -1 reserves the trailing gap
    size_shown = size_str[:remaining]
    pad = max(0, model_w - len(prefix) - len(size_shown))
    return prefix, size_shown, " " * pad


def _build_storage_panel(y, x0, width, disks, tier):
    inner = width - 2
    p = panels.Panel(y, x0, width,
                      title=[("STORAGE", 9), (" block devices · rates from /proc/diskstats · temps from hwmon", 3)],
                      kind="mid")
    if not disks:
        p.add_row([(1, "no block devices detected", 6)])
        return p
    if tier == "compact":
        return _build_storage_compact_rows(p, inner, disks)

    name_w, hw_w_cap, temp_w = _net_storage_columns(inner)
    rw_texts = [f"R {term.fmt_rate(d.get('read_bps', 0), 'byte')} · W {term.fmt_rate(d.get('write_bps', 0), 'byte')}"
                for d in disks]
    rate_w = max([len(t) for t in rw_texts] + [25]) + 1  # +1 gap before the bar
    has_bar = any(d.get("used_pct") is not None for d in disks)
    # Worst-case (3-digit pct) suffix width, so every row's bar column lands
    # in the same place regardless of which disk happens to be near-full.
    bar_total = (1 + 10 + 1 + len(" 100% used")) if has_bar else 0
    # The usage bar is functional data; the model-name column is decoration.
    # Size model_w to whatever's left AFTER reserving room for the rate text
    # and the bar -- capped at the NETWORK-aligned width -- rather than
    # holding model_w fixed and letting the bar silently lose the fight for
    # space (that was the "dropped despite available room" bug: model_w was
    # pinned to 29 even when inner - 29 - the rest genuinely had no room for
    # the bar, while shrinking model_w by a few columns would free exactly
    # enough).
    model_w = max(10, min(hw_w_cap, inner - 1 - name_w - temp_w - rate_w - bar_total))

    for d, rw in zip(disks, rw_texts):
        f = _Flow(inner - 1)
        f.add(f"{d.get('name', '?')[:name_w - 1]:<{name_w}}", 8)

        model = d.get("model") or "Unknown"
        size_str = term.fmt_disk_size(d.get("size"))
        prefix, size_shown, pad = _model_size_segments(model, size_str, model_w)
        f.add(prefix, 3)
        f.add(size_shown, 6)
        f.add(pad, 3)

        temp = d.get("temp")
        f.add(f"{term.fmt_temp(temp):<{temp_w}}" if temp is not None else f"{'—':<{temp_w}}",
              3 if temp is not None else 6)

        f.add(f"{rw:<{rate_w}}", 2)

        used_pct = d.get("used_pct")
        if used_pct is not None:
            lb, rb = term.bar_brackets()
            bar, slot = term.make_bar(used_pct, 10)
            f.try_add_multi([(lb, 6), (bar, slot), (rb, 6), (f" {int(used_pct)}% used", 3)])

        segs = list(f.segs)
        segs[0] = (1, segs[0][1], segs[0][2])
        p.add_row(segs)
    return p


# =========================================================================
# Page 1 assembly
# =========================================================================

def build_page1(state, tier, width, x0=0, height=None):
    """Builds page 1: header, CPU+MEMORY compound frame, GPU card(s),
    NETWORK+STORAGE compound frame, footer. `height` (available screen rows),
    when given, drives a simple degrade order: sparklines -> memory legend
    row -> NIC rows collapse to one summary line."""
    out = []
    show_sparkline = True
    show_legend = True
    collapse_nics = False

    cpu = state.get("cpu") or {}
    mem = state.get("mem") or {}
    gpus = state.get("gpus") or []
    nics = state.get("nics") or []
    disks = state.get("disks") or []
    gpu_caps = (state.get("caps") or {}).get("gpu") or []

    if height:
        n_clusters = len(cpu.get("clusters") or [])
        gpu_rows = max(1, (len(gpus) + 1) // 2) if (tier == "wide" and len(gpus) > 1) else len(gpus)
        est = 1 + 2 + n_clusters + 1 + 2 + gpu_rows * 4 + 1 + len(nics) + 1 + len(disks) + 1
        if est > height:
            show_sparkline = False
            est -= n_clusters
        if est > height:
            show_legend = False
            est -= 1
        if est > height and len(nics) > 1:
            collapse_nics = True

    out.append(_build_header(x0, width, tier))
    y = 1

    cpu_panel = _build_cpu_panel(y, x0, width, cpu, tier, show_sparkline)
    out.append(cpu_panel)
    y += cpu_panel.total_height

    mem_panel = _build_memory_panel(y, x0, width, mem, gpu_caps, tier, show_legend)
    out.append(mem_panel)
    y += mem_panel.total_height + 1  # +1 for the compound frame's bottom border

    gpu_panels, gpu_h = _build_gpu_row(y, x0, width, tier, gpus, gpu_caps, show_sparkline)
    out.extend(gpu_panels)
    y += gpu_h

    net_panel = _build_network_panel(y, x0, width, nics, collapse_nics, tier)
    out.append(net_panel)
    y += net_panel.total_height  # no +1: STORAGE continues this same frame

    storage_panel = _build_storage_panel(y, x0, width, disks, tier)
    out.append(storage_panel)
    y += storage_panel.total_height + 1  # +1 for the compound frame's bottom border

    driver, cuda = state.get("driver", "Unknown"), state.get("cuda", "Unknown")
    has_nvml = (state.get("caps") or {}).get("has_nvml", False)
    rate = state.get("rate", 1.0)
    out.append(_build_footer(x0, width, y, driver, cuda, rate, has_nvml))

    return out
