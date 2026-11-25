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
from . import node
from . import topomap

try:
    from . import VERSION
except ImportError:
    VERSION = "5.0.0"

# The header shows the major.minor series ("4.0"). Derived from VERSION rather
# than written out, because a hardcoded literal here silently drifted a whole
# major version behind the footer once already.
SERIES = ".".join(VERSION.split(".")[:2])

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


def _add_graded_spark(f, history, max_cells, lead="  ", cap=20):
    """Graded history graph (term.sparkline_runs, fixed 0-100% scale) plus
    its window peak, added to Flow `f` as one atomic group: the peak label
    drops first when the row is tight, then the whole graph. `cap` limits
    the graph's own cells (None: as wide as the row allows)."""
    hist = [v for v in (history or []) if v is not None]
    room = min(f.room() - len(lead), max_cells)
    if room < 4 or len(hist) < 2:
        return
    label_w = 10  # " peak 100%", the widest label
    if room - label_w >= 4:
        cells = room - label_w if cap is None else min(room - label_w, cap)
        runs = term.sparkline_runs(hist, cells)
        if runs:
            # the peak of the samples drawn, not of the whole ring buffer;
            # none for an all-idle window, whose graph is blank anyway
            top = max(hist[-2 * cells:])
            label = [(f" peak {top:.0f}%", 6)] if round(top) > 0 else []
            f.try_add_multi([(lead, 3)] + runs + label)
        return
    runs = term.sparkline_runs(hist, room)
    if runs:
        f.try_add_multi([(lead, 3)] + runs)


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

def _fmt_kbs(v):
    """NVML PCIe throughput is reported in KB/s -- a short human string for
    page 2's PCIE row, or None (segment hidden) when the value is missing."""
    if v is None:
        return None
    try:
        v = float(v)
    except Exception:
        return None
    if v >= 1e6:
        return f"{v / 1e6:.1f} GB/s"
    if v >= 1e3:
        return f"{v / 1e3:.1f} MB/s"
    return f"{v:.0f} KB/s"

def _fmt_count_rate(v):
    """Per-second delta formatter for the NIC advanced panel's hw_counters
    columns (CNP/ECN/discard): bare integer below 1000/s, "X.X k/s" above.
    None (rendered "n/a") when the counter file wasn't readable."""
    if v is None:
        return None
    try:
        v = float(v)
    except Exception:
        return None
    if v >= 1000:
        return f"{v / 1000:.1f} k/s"
    return f"{int(round(v))}"

def _flow_join(inner, items, col=2):
    """Row segments from a list of (text, slot) | None items, dropping Nones
    and joining the survivors with "  ·  " -- unlike a bare sequence of
    try_add_multi() calls gated only on "is this field present", this never
    emits a leading/orphaned separator when an earlier field in the list was
    absent (the NVME SMART panel's rows are all fully-optional fields, so
    that per-field gating alone isn't enough there)."""
    f = _Flow(inner - 1)
    started = False
    for item in items:
        if item is None:
            continue
        text, slot = item
        parts = [("  ·  ", 6), (text, slot)] if started else [(text, slot)]
        if f.try_add_multi(parts):
            started = True
    return _finish_row(f, col) if started else None


def _finish_row(f, col=2):
    """Re-tags a _Flow's first segment with its column stop -- the shared
    idiom every page-2 row builder below ends with (mirrors the
    `segs[0] = (col, ...)` pattern used throughout page 1)."""
    segs = list(f.segs)
    if segs:
        segs[0] = (col, segs[0][1], segs[0][2])
    return segs

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

def _hdr_glyphs():
    """(tab open, tab close, separator) for the header: block elements and a
    box-drawing bar, or ASCII stand-ins under --ascii (no UTF-8 box drawing)."""
    if term.BAR_STYLE == "ascii":
        return "[", "]", "|"
    return "\u258c", "\u2590", "\u2502"


def _tab(n, text=None):
    lo, hi, _ = _hdr_glyphs()
    return f"{lo}{n}{(' ' + text) if text else ''}{hi}"


def _cluster_tabs_text(page, has_perf=False):
    order = [(1, "NODE"), (2, "ADV"), (3, "CLUSTER")]
    if has_perf:
        order.append((4, "PERF"))
    order.append((5, "TASKS"))
    return " ".join(_tab(n, t) if n == page else f"{n} {t}" for n, t in order) + " "


def _cluster_tabs_compact(page, has_perf=False):
    nums = (1, 2, 3, 4, 5) if has_perf else (1, 2, 3, 5)
    return "".join(_tab(n) if n == page else str(n) for n in nums) + "  "


# Page 5 (GPU tasks) is local-only but always available, so it sits on the
# plain 2-tab bar too; the 3/4 gap is deliberate -- those numbers are the
# --cluster pages, and keeping 5 stable across both bars means the key never
# moves depending on how spark-smi was launched.
def _local_tabs_text(page):
    order = [(1, "OVERVIEW"), (2, "ADVANCED"), (5, "TASKS")]
    return "  ".join(f"{_tab(n)} {t}" if n == page else f"{n} {t}" for n, t in order) + "  "


def _local_tabs_compact(page):
    return "".join(_tab(n) if n == page else str(n) for n in (1, 2, 5)) + "  "


def _build_header(x0, width, tier, page=1, cluster_tabs=False, cluster_summary=None, remote=None, has_perf=False):
    """`cluster_tabs` (Phase 6, --cluster only) swaps the 2-tab
    "OVERVIEW/ADVANCED" bar for the 3-tab "NODE/ADV/CLUSTER" one shown in
    mock_cluster_sections/fleet -- pages 1/2 use it too whenever --cluster
    was given (not just page 3 itself), since key '3' is live on all of
    them in that mode. `cluster_summary` (page 3 only) replaces the normal
    host/model/uptime block with the cluster-level line ("spark-grid — 4
    nodes · all up │ Σ wall 412 W"). `remote` (pages 1/2 drilldown only) is
    the selected node's name -- shown in place of the local hostname, with
    a dim " · remote" tag appended so it's never confused with the local
    node's own page 1/2."""
    host = remote or node.display_name(fallback=_safe(platform.node, "host") or "host")
    # model/arch/uptime are LOCAL machine probes (device-tree/DMI, uname,
    # psutil.boot_time()) -- meaningless (actively misleading) for a remote
    # drilldown node, since nothing about that data travels over the wire
    # format. Skip probing them entirely when `remote` is set; the header
    # just shows the remote node's name + the dim "· remote" tag.
    model = _machine_model() if not remote else None
    arch = (_safe(platform.machine, "") or "") if not remote else ""
    uptime = _fmt_uptime(time.time() - _safe(psutil.boot_time, time.time())) if not remote else ""
    clock = time.strftime("%H:%M:%S")
    bar = _hdr_glyphs()[2]
    remote_tag = [(" · remote", 6)] if remote else []

    if tier == "compact":
        if cluster_summary is not None:
            core = f" {bar} {cluster_summary}"
        elif remote:
            core = f" {bar} {host}"
        else:
            chip = model or arch
            core = f" {bar} {host}" + (f" · {chip}" if chip else "")
        tabs = _cluster_tabs_compact(page, has_perf) if cluster_tabs else _local_tabs_compact(page)
        right = [(tabs, 9), (clock, 3)]
        p = panels.Panel(0, x0, width, kind="plain")
        p.rows.append(([(f" SPARK-SMI {SERIES}", 9), (core, 3)] + remote_tag, right))
        return p

    tabs = _cluster_tabs_text(page, has_perf) if cluster_tabs else _local_tabs_text(page)
    right = [(tabs, 9), (clock, 3)]
    right_len = sum(len(t) for t, _ in right)
    remote_len = sum(len(t) for t, _ in remote_tag)

    prefix = f" SPARK-SMI {SERIES}"

    if cluster_summary is not None:
        core = f" {bar} {cluster_summary}"
        p = panels.Panel(0, x0, width, kind="plain")
        p.rows.append(([(prefix, 9), (core, 3)], right))
        return p

    # Never truncate mid-token: drop whole optional segments right-to-left
    # in this priority order (arch first, then uptime, then model) until
    # the line fits.
    show = {"model": bool(model), "arch": bool(arch), "uptime": not remote}

    def build():
        tail = [t for t, key in ((model, "model"), (arch, "arch")) if show[key] and t]
        s = f" {bar} {host}"
        if tail:
            s += " — " + " · ".join(tail)
        if show["uptime"]:
            s += f" {bar} up {uptime}"
        return s

    core = build()
    for key in ("arch", "uptime", "model"):
        if len(prefix) + len(core) + remote_len + right_len + 1 <= width:
            break
        show[key] = False
        core = build()

    p = panels.Panel(0, x0, width, kind="plain")
    p.rows.append(([(prefix, 9), (core, 3)] + remote_tag, right))
    return p


def _build_footer(x0, width, y, driver, cuda, rate, has_nvml, extra_keys=None, knob_ui=None, remote_note=None):
    """`extra_keys`/`knob_ui` (Phase 4, page 2 only -- build_page1 never
    passes them) add the tab/P/C/M/R/X knob hints to the key-bar ladder and,
    while a knob is armed or a toast is live, replace the whole footer line
    with the confirm prompt / apply result instead. `remote_note` (Phase 6,
    page 2 drilldown only) replaces the whole footer with a short key
    reminder plus a dim note that knobs aren't available against a remote
    sample -- the registry is never built for one (see app.py), so nothing
    downstream of this even has to know why; this is purely the visible
    explanation."""
    if remote_note:
        p = panels.Panel(y, x0, width, kind="plain")
        p.rows.append(([(" q quit · 1 2 3 5 page · esc back to cluster", 9), (f"  {remote_note}", 6)], []))
        return p

    if knob_ui and (knob_ui.get("confirming") or knob_ui.get("toast")):
        if knob_ui.get("confirming"):
            text, slot = (knob_ui.get("confirm_text") or "apply? y/N"), 5
            # The warning rides in the critical slot ahead of the prompt, so
            # the last thing between a keystroke and a hardware write always
            # says so. Dropped only when the terminal is too narrow to hold
            # both -- the prompt itself must never be the thing that's cut.
            warn = knob_ui.get("confirm_warn")
            p = panels.Panel(y, x0, width, kind="plain")
            if warn and len(warn) + len(text) + 4 <= width:
                p.rows.append(([(f" {warn}", 4), (f"  {text}", slot)], []))
            else:
                p.rows.append(([(f" {text}", slot)], []))
            return p
        text, slot = knob_ui["toast"]
        p = panels.Panel(y, x0, width, kind="plain")
        p.rows.append(([(f" {text}", slot)], []))
        return p

    # Ladder of key-bar variants, most to least detailed. Tried together
    # with the driver/CUDA info first; if none fit alongside it, the info
    # is dropped (not the keys) and the shortest keys variant that fits on
    # its own is used -- so the info block degrades consistently by width
    # rather than disappearing at one width and reappearing at another.
    if extra_keys:
        # Page 2: 'n active NICs'/'s sort' are page-1-only bindings, not
        # worth the space here -- the knob hints (tab/P/C/M/R/X) take their
        # place as the thing this footer prioritizes, matching
        # mock_page2.rendered.txt. t/u still work on page 2 (temp/unit
        # toggles are global) so they're kept, but dropped before the knob
        # hints themselves are abbreviated/dropped -- "knobs absent from
        # the registry grey out or drop from the bar" is about registry
        # gating (extra_keys already reflects that), not width pressure.
        base = " q quit · 1 2 5 page"
        short_keys = extra_keys.replace("pwr limit", "pwr").replace("clock locks", "clk")
        variants = [
            base + " · t °C/°F · u GiB/GB · c colors" + extra_keys,
            base + extra_keys,
            base + short_keys,
            base,
        ]
    else:
        variants = [
            " q quit · 1 2 5 page · t °C/°F · u GiB/GB · c colors · n active NICs · s sort · g graphs · +/- history · ? help",
            " q quit · 1 2 5 page · t °C/°F · u GiB/GB · c colors · n active NICs · s sort · ? help",
            " q quit · 1 2 5 page · t °C/°F · u GiB/GB · c colors · n NICs",
            " q quit · 1 2 5 page · t temp · u units",
        ]
    src = "NVML" if has_nvml else "CLI"
    # Same no-sensor-no-row rule the panels follow: on a box with no NVIDIA
    # driver these read "driver Unknown · CUDA Unknown", which is exactly the
    # misleading placeholder this project sets out not to print.
    info_parts = [f"v{VERSION}", src]
    if driver and str(driver) != "Unknown":
        info_parts.append(f"driver {driver}")
    if cuda and str(cuda) != "Unknown":
        info_parts.append(f"CUDA {cuda}")
    info_parts.append(f"{rate:g}s")
    info = " · ".join(info_parts)

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


def build_help_overlay(term_w, term_h):
    """'?' key (live mode, both pages): a single centered bordered panel
    listing every key binding + the running version, appended AFTER the
    already-built page so panels.render() paints it on top -- dismissed by
    any keypress (app.py's main_loop), not drawn by this function itself.
    Rows are padded to the panel's inner width so they blank out whatever
    page content sits underneath (bordered rows aren't auto-padded by
    panels.render -- only the frame/border chars are)."""
    la, ra = term.knob_arrows()
    lines = [
        ("Global", 9),
        ("  q            quit", 3),
        ("  1 - 5        switch page: overview / advanced /", 3),
        ("               cluster / fabric (3-4 need --cluster) / tasks", 3),
        ("  t            toggle °C / °F", 3),
        ("  u            toggle GiB / GB (decimal units)", 3),
        (f"  c            cycle color theme  (now: {term.ACTIVE_THEME})", 3),
        ("  S            save this screen as SVG (current folder)", 3),
        ("  ?            toggle this help", 3),
        ("", 3),
        ("Page 1 -- overview", 9),
        ("  n            show active NICs only", 3),
        ("  s            sort NIC rows by rate (toggle)", 3),
        ("  g            graphs: auto / large / compact (big-screen", 3),
        ("               layouts need 200+ columns, or large: 120+)", 3),
        ("  + / -        history window: 1m 5m 10m 30m 1h", 3),
        ("", 3),
        ("Page 2 -- advanced (GPU knobs, root-gated)", 9),
        ("  Tab          select GPU", 3),
        ("  P            power limit knob", 3),
        ("  C / M        SM / memory clock lock knob", 3),
        ("  R            reset clocks", 3),
        ("  X            toggle persistence mode", 3),
        (f"  {la} / {ra}          adjust focused knob", 3),
        ("  Enter        apply focused knob (confirm)", 3),
        ("  Esc          cancel / dismiss confirm", 3),
        ("  ! knobs write real hardware -- no warranty,", 4),
        ("    entirely at your own risk", 4),
        ("", 3),
        ("Page 3 -- cluster (--cluster)", 9),
        ("  up / down    select node", 3),
        ("  Enter        drill into node (knobs stay local-only)", 3),
        ("  Esc          back out of a drilldown", 3),
        ("  a            fleet matrix: alerting nodes only", 3),
        ("  o            fleet matrix: cycle sort column", 3),
        ("", 3),
        ("Page 4 -- fabric validation (--cluster, 2+ members)", 9),
        ("  space        start test (confirm) / stop immediately (no confirm)", 3),
        ("  m            cycle mode: pair / sweep / burst", 3),
        ("  d            cycle duration: 10s / 30s / 60s", 3),
        ("  up / down    select pair (pair mode)", 3),
        ("", 3),
        ("Page 5 -- GPU tasks (local only)", 9),
        ("  up / down    select GPU / session / process row", 3),
        ("  Enter / o    open session: tmux/screen attach,", 3),
        ("               journalctl / docker logs tail", 3),
        ("  e            end process (SIGTERM) or session", 3),
        ("               via tmux/screen/systemctl/docker", 3),
        ("  k            kill process (SIGKILL) -- confirm", 3),
        ("  a            also list bare /dev/nvidia* holders", 3),
        ("  Esc          cancel / dismiss confirm", 3),
        ("", 3),
        ("press any key to close", 6),
    ]
    # One column when it fits. When it doesn't -- a 30-row terminal can't
    # show all of this -- spill into two columns rather than silently
    # truncating the list, which used to drop whole pages' bindings off the
    # bottom with nothing to indicate they existed.
    col_w = 56
    max_rows = max(4, min(len(lines), max(6, term_h - 2) - 2))
    two_col = len(lines) > max_rows and term_w >= (col_w * 2 + 6)

    if two_col:
        rows_per_col = min(max_rows, (len(lines) + 1) // 2)
        left, right = lines[:rows_per_col], lines[rows_per_col:rows_per_col * 2]
        width = min(term_w - 4, col_w * 2 + 3)
        n_rows = rows_per_col
    else:
        left, right = lines[:max_rows], []
        width = min(58, max(40, term_w - 4))
        n_rows = len(left)

    inner = width - 2
    height = n_rows + 2
    x0 = max(0, (term_w - width) // 2)
    y0 = max(0, (term_h - height) // 2)

    p = panels.Panel(y0, x0, width, title=[("HELP", 9), (f" spark-smi {VERSION}", 3)], kind="top")
    # Rows start at inner column 0 and are padded to exactly `inner` chars, so
    # they blank out the page underneath edge to edge. Starting at column 1
    # (as this did) left the first inner column showing through the overlay
    # and pushed the last char past the right border.
    if two_col:
        half = inner // 2
        for i in range(n_rows):
            ltext, lslot = left[i]
            segs = [(0, (" " + ltext)[:half].ljust(half), lslot)]
            if i < len(right):
                rtext, rslot = right[i]
                segs.append((half, (" " + rtext)[:inner - half].ljust(inner - half), rslot))
            else:
                segs.append((half, " " * (inner - half), 3))
            p.add_row(segs)
    else:
        for text, slot in left:
            p.add_row([(0, (" " + text)[:inner].ljust(inner), slot)])
    return [p]


# =========================================================================
# CPU + MEMORY (one compound frame: CPU is 'top', MEMORY continues as 'mid')
# =========================================================================

def _build_cpu_panel(y, x0, width, cpu_state, tier, show_sparkline):
    clusters = cpu_state.get("clusters") or []
    summary = " + ".join(f"{c['label']} ×{len(c['cores'])}" for c in clusters) or "CPU"
    temp = term.fmt_temp(cpu_state.get("temp"))
    loadavg = cpu_state.get("loadavg")
    # No temp sensor and no loadavg (Windows) -> no title_right at all,
    # rather than a bare "N/A" sitting where a live number should be.
    load_txt = (f"load {loadavg[0]:.2f} {loadavg[1]:.2f} {loadavg[2]:.2f}"
                if loadavg else "")
    temp_txt = temp if temp and temp != "N/A" else ""
    right = [(" · ".join(t for t in (temp_txt, load_txt) if t), 2)] if (temp_txt or load_txt) else []

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
            f.try_add_multi([(lb, 10), (bar, slot), (rb, 10), (f"{int(cl.get('avg', 0)):>3}%", 3)])
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
        # No frequency reported by the platform -> omit the field entirely
        # (no-sensor-no-row), rather than printing a bogus "0.00 GHz".
        ghz = cl.get("ghz", 0) or 0
        f.add(f"{ghz:>5.2f} GHz  " if ghz > 0 else " " * 11, 8)
        bar_w = 20 if tier == "wide" else 16
        lb, rb = term.bar_brackets()
        bar, slot = term.make_bar(avg, bar_w)
        f.add(lb, 10)
        f.add(bar, slot)
        f.add(rb, 10)
        f.add(f" {avg:>3.0f}%  ", 3)
        f.try_add(f"cores {cl.get('range', '')}", 2)
        # The strip gets its own >=3-space gap baked into ONE atomic add, so
        # a tight row drops the whole "gap + strip" together rather than
        # ever emitting a bare gap with nothing after it.
        strip = term.core_strip(cl.get("loads") or [])
        if strip:
            f.try_add("   " + strip, 1)
        if show_sparkline:
            _add_graded_spark(f, cl.get("history"), 30)
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
    f1.add(lb, 10)
    f1.add(bar, slot)
    f1.add(rb, 10)
    f1.add(f" {int(util):>3}%", 3)
    if show_sparkline:
        _add_graded_spark(f1, gpu.get("history"), 26)
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

    # Phase 8: only appears once app.py's stuck-under-load detection has
    # actually fired (3+ consecutive loaded samples below max link gen) --
    # a normal idle downtrain never adds this row at all, matching the
    # spec's "amber/red row -- only then" rule.
    if gpu.get("pcie_stuck"):
        w_c = gpu.get("pcie_width_cur")
        txt = f"⚠ PCIe gen1×{w_c} under load" if w_c else "⚠ PCIe gen1 under load"
        p.add_row([(2, txt, 4)])
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
            f.try_add_multi([(lb, 10), (bar, slot), (rb, 10), (f" {int(pct):>2}%", 3)])
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


def _build_network_panel(y, x0, width, nics, collapse, tier, has_rdma=True,
                         rdma_counting=True, port_counting=False, mirror=False):
    inner = width - 2
    # Port MAC counters see RDMA and kernel traffic alike (issue #11). Else
    # only advertise the HCA-counter path when there's actually an RDMA
    # device behind one of these NICs AND its counters have moved at least
    # once; on a plain box, or where the driver leaves the HCA counters at 0
    # (issue #10), the rates come from netdev counters, which miss RDMA.
    if has_rdma and port_counting:
        caption = " physical ports · port counters, RDMA + kernel"
    elif has_rdma and rdma_counting:
        caption = " physical ports · RDMA read from HCA counters"
    elif has_rdma:
        caption = " physical ports · HCA counters read 0, kernel traffic only"
    else:
        caption = " physical ports"
    p = panels.Panel(y, x0, width,
                      title=[("NETWORK", 9), (caption, 3)],
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

        if mirror and can_mirror(n, "rx_history", "tx_history"):
            segs = list(f.segs)
            segs[0] = (1, segs[0][1], segs[0][2])
            chart_col = 1 + name_w + hw_w + link_w
            labels = ((f"▼ {term.fmt_rate(n.get('rx_bps', 0), 'bit')}  {int(n.get('rx_pct', 0) or 0)}%", 2),
                      (f"▲ {term.fmt_rate(n.get('tx_bps', 0), 'bit')}  {int(n.get('tx_pct', 0) or 0)}%", 2))
            floor = (n.get("speed_mbit") or 0) * 1e6 * 0.01  # 1% of the link
            if _add_mirror_rows(p, inner, chart_col, [segs, [], [], []], n["rx_history"], n["tx_history"],
                                labels, "bit", (floor, floor)):
                continue

        # Suffix is ONE separator space + the number right-justified to 2
        # digits (not 3) -- a stray extra `:>3` here previously reserved a
        # column no mock row actually uses (a "reserve the right-group
        # twice" flavor of the STORAGE bar bug fixed below).
        lb, rb = term.bar_brackets()
        f.add(f"{term.fmt_rate(n.get('rx_bps', 0), 'bit'):<{rate_w}}", 2)
        rx_bar, rx_slot = term.make_bar(n.get("rx_pct", 0) or 0, 10)
        f.try_add_multi([(lb, 10), (rx_bar, rx_slot), (rb, 10), (f" {int(n.get('rx_pct', 0) or 0):>2}% ", 3)])
        f.add(f"{term.fmt_rate(n.get('tx_bps', 0), 'bit'):<{rate_w}}", 2)
        tx_bar, tx_slot = term.make_bar(n.get("tx_pct", 0) or 0, 10)
        f.try_add_multi([(lb, 10), (tx_bar, tx_slot), (rb, 10), (f" {int(n.get('tx_pct', 0) or 0):>2}%", 3)])

        segs = list(f.segs)
        segs[0] = (1, segs[0][1], segs[0][2])
        p.add_row(segs)
    return p


# =========================================================================
# Mirrored history rows (D-02 item 1): page 1 NETWORK and STORAGE, when the
# terminal has the height to spare (build_page1 decides)
# =========================================================================
MIRROR_ROWS = 4          # rows per mirrored NIC or disk (2 per half)
MIRROR_MAX_CELLS = 30    # 60 samples, the whole history ring buffer
MIRROR_LABEL_W = 27      # "peak 142.6 · avg 100.1 Gb/s"


def _peak_avg(values, mode):
    """"peak 96.1 · avg 41.8 Gb/s" over `values`, both in the peak's unit."""
    vals = [float(v or 0) for v in values] or [0.0]
    peak, avg = max(vals), sum(vals) / len(vals)
    if mode == "byte":
        return f"peak {peak / 1e9:.2f} · avg {avg / 1e9:.2f} GB/s"
    if peak >= 1e8:
        return f"peak {peak / 1e9:.1f} · avg {avg / 1e9:.1f} Gb/s"
    return f"peak {peak / 1e6:.0f} · avg {avg / 1e6:.0f} Mb/s"


def can_mirror(item, up_key, down_key):
    return (term.BAR_STYLE == "eighths" and len(item.get(up_key) or []) >= 2
            and len(item.get(down_key) or []) >= 2)


def _add_mirror_rows(p, inner, chart_col, left_rows, up, down, labels, mode, floors):
    """Adds MIRROR_ROWS rows to panel p: left_rows[i] are the segments left
    of the chart on row i, the chart starts at chart_col, and the right-hand
    labels are, top to bottom: up rate, up peak/avg, down peak/avg, down
    rate. Falls back to nothing (returns False) when the chart can't fit."""
    cells = min(MIRROR_MAX_CELLS, inner - 1 - chart_col - 2 - MIRROR_LABEL_W)
    if cells < 8:
        return False
    rows = term.mirror_chart(up, down, cells, half_rows=MIRROR_ROWS // 2,
                             up_floor=floors[0], down_floor=floors[1])
    if rows is None:
        return False
    n = cells * 2
    texts = [labels[0], (_peak_avg(up[-n:], mode), 6), (_peak_avg(down[-n:], mode), 6), labels[1]]
    for i, runs in enumerate(rows):
        segs = list(left_rows[i])
        segs.append((chart_col, runs[0][0], runs[0][1]))
        for t, slot in runs[1:]:
            segs.append((None, t, slot))
        text, slot = texts[i]
        segs.append((chart_col + cells + 2, text, slot))
        p.add_row(segs)
    return True


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
            right = [(lb, 10), (bar, slot), (rb, 10), (f" {int(used_pct):>2}%", 3)]
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


def _build_storage_panel(y, x0, width, disks, tier, mirror=False):
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

        if mirror and can_mirror(d, "read_history", "write_history"):
            segs = list(f.segs)
            segs[0] = (1, segs[0][1], segs[0][2])
            # the usage bar moves under the model name, into the rows the
            # chart opens up
            below = []
            used_pct = d.get("used_pct")
            if used_pct is not None:
                lb, rb = term.bar_brackets()
                bar, slot = term.make_bar(used_pct, 10)
                below = [(1 + name_w, lb, 10), (None, bar, slot), (None, rb, 10),
                         (None, f" {int(used_pct)}% used", 3)]
            chart_col = 1 + name_w + model_w + temp_w
            labels = ((f"R {term.fmt_rate(d.get('read_bps', 0), 'byte')}", 2),
                      (f"W {term.fmt_rate(d.get('write_bps', 0), 'byte')}", 2))
            if _add_mirror_rows(p, inner, chart_col, [segs, below, [], []], d["read_history"],
                                d["write_history"], labels, "byte", (10e6, 10e6)):
                continue

        f.add(f"{rw:<{rate_w}}", 2)

        used_pct = d.get("used_pct")
        if used_pct is not None:
            lb, rb = term.bar_brackets()
            bar, slot = term.make_bar(used_pct, 10)
            f.try_add_multi([(lb, 10), (bar, slot), (rb, 10), (f" {int(used_pct)}% used", 3)])

        segs = list(f.segs)
        segs[0] = (1, segs[0][1], segs[0][2])
        p.add_row(segs)
    return p


# =========================================================================
# Page 1 assembly
# =========================================================================

def build_page1(state, tier, width, x0=0, height=None, sort_nics=False, theme_toast=None,
                 remote=None, cluster_tabs=False, has_perf=False):
    """Builds page 1: header, CPU+MEMORY compound frame, GPU card(s),
    NETWORK+STORAGE compound frame, footer. `height` (available screen rows),
    when given, drives a simple degrade order: sparklines -> memory legend
    row -> NIC rows collapse to one summary line. `sort_nics` ('s' key)
    reorders the NETWORK rows by max(rx, tx) rate descending, recomputed
    fresh every frame; False (default) keeps the collector's detection
    order (grouped by physical port, stable frame-to-frame). `theme_toast`
    ('c' key, live mode; capital 'T' is an undocumented alias) is a
    (text, slot) pair shown briefly in the footer
    via the same toast rendering _build_footer already has for Phase 4's
    knob-apply toast -- reused here by handing it a knob_ui-shaped dict with
    just a "toast" key."""
    out = []
    show_sparkline = True
    show_legend = True
    collapse_nics = False
    mirror_nics = mirror_disks = False

    cpu = state.get("cpu") or {}
    mem = state.get("mem") or {}
    gpus = state.get("gpus") or []
    nics = state.get("nics") or []
    disks = state.get("disks") or []
    gpu_caps = (state.get("caps") or {}).get("gpu") or []

    if sort_nics and nics:
        nics = sorted(nics, key=lambda n: max(n.get("rx_bps", 0) or 0, n.get("tx_bps", 0) or 0),
                      reverse=True)

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
        # Mirrored history (D-02 item 1) only with height to spare: NICs
        # first, then disks; each mirrored row costs MIRROR_ROWS - 1 more.
        if tier != "compact" and est <= height:
            spare = height - est
            up_nics = [n for n in nics if n.get("up") and can_mirror(n, "rx_history", "tx_history")]
            cost = (MIRROR_ROWS - 1) * len(up_nics)
            if up_nics and cost <= spare:
                mirror_nics, spare = True, spare - cost
            cost = (MIRROR_ROWS - 1) * sum(1 for d in disks if can_mirror(d, "read_history", "write_history"))
            if mirror_nics and cost and cost <= spare:
                mirror_disks = True

    out.append(_build_header(x0, width, tier, cluster_tabs=cluster_tabs, remote=remote, has_perf=has_perf))
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

    net_caps = (state.get("caps") or {}).get("net") or {}
    has_rdma = bool(net_caps.get("rdma"))
    # Older --serve agents send no "rdma_counting": keep their old caption.
    rdma_counting = bool(net_caps.get("rdma_counting", True))
    port_counting = bool(net_caps.get("port_counting", False))
    net_panel = _build_network_panel(y, x0, width, nics, collapse_nics, tier, has_rdma,
                                     rdma_counting, port_counting, mirror=mirror_nics)
    out.append(net_panel)
    if disks:
        y += net_panel.total_height  # no +1: STORAGE continues this same frame
        storage_panel = _build_storage_panel(y, x0, width, disks, tier, mirror=mirror_disks)
        out.append(storage_panel)
        y += storage_panel.total_height + 1  # +1 for the frame's bottom border
    else:
        # No block devices enumerable at all (Windows, or a container with no
        # /sys/block): drop the panel entirely rather than draw an empty box
        # captioned with Linux sysfs paths that don't exist here.
        y += net_panel.total_height + 1

    driver, cuda = state.get("driver", "Unknown"), state.get("cuda", "Unknown")
    has_nvml = (state.get("caps") or {}).get("has_nvml", False)
    rate = state.get("rate", 1.0)
    footer_knob_ui = {"toast": theme_toast} if theme_toast else None
    out.append(_build_footer(x0, width, y, driver, cuda, rate, has_nvml, knob_ui=footer_knob_ui))

    return out


# =========================================================================
# Page 2: per-GPU detail, NIC advanced, THERMALS / POWER RAILS / NVME SMART
# =========================================================================
# Read-only (Phase 3) -- every row below is capability-driven: a field the
# collector couldn't read is either an atomic try_add_multi() that drops the
# whole segment, or (for whole rows -- PCIE/POWER/THERMAL/THROTTLE) the row
# builder returning None/falsy and the caller skipping p.add_row() entirely.
# Nothing here ever prints a bare "N/A" for a missing API -- either the
# field/row is absent, or (STATE's ecc, matching mock_page2) an explicit
# "n/a" note for a field that IS queryable but unsupported on this GPU.

def _right_reserve(right):
    """Column budget to withhold from a row's _Flow so its atomic segments
    drop WHOLE when a `right` group is also present -- mirrors the
    `reserve`/`room()` idiom in _build_memory_panel's legend row. Without
    this, a too-long left segment would rely on panels.render's per-row
    `limit` clip for correctness, which truncates mid-token rather than
    dropping the segment, exactly what _Flow exists to avoid."""
    return sum(len(t) for t, _ in right) + 2 if right else 0


def _row_clocks(inner, gpu, caps, reserve=0, focus_id=None, pending=None, sm_knob=None, mem_knob=None):
    """`sm_knob`/`mem_knob` (Phase 4) are that GPU's clk_sm/clk_mem registry
    entries, or None when the clock-lock knobs aren't available (unified
    SoC, no nvidia-smi) -- when one IS focused (focus_id matches its id),
    its normal "sm 2745 MHz (max 3105)" text is replaced by the
    "[◂ 2745 MHz ▸]" stepper widget showing the pending value."""
    f = _Flow(inner - 1 - reserve)
    f.add(f"{'CLOCKS':<11}", 3)
    clk = gpu.get("clk_sm")
    sm_focused = sm_knob is not None and focus_id == sm_knob.get("id")
    if sm_focused:
        shown = pending if pending is not None else clk
        la, ra = term.knob_arrows()
        f.add(f"sm [{la} {shown:.0f} MHz {ra}]", 9)
    elif caps.get("clocks") and clk not in (None, "N/A"):
        txt = f"sm {clk} MHz"
        mx = gpu.get("clk_sm_max")
        if mx not in (None, "N/A"):
            txt += f" (max {mx})"
        f.add(txt, 8)
    else:
        f.add("sm N/A", 6)
    mem = gpu.get("clk_mem")
    mem_focused = mem_knob is not None and focus_id == mem_knob.get("id")
    if mem_focused:
        shown = pending if pending is not None else mem
        la, ra = term.knob_arrows()
        f.try_add_multi([("  ·  ", 6), (f"mem [{la} {shown:.0f} MHz {ra}]", 9)])
    elif mem is not None:
        f.try_add_multi([("  ·  ", 6), (f"mem {mem} MHz", 2)])
    vid = gpu.get("clk_video")
    if vid is not None:
        f.try_add_multi([("  ·  ", 6), (f"video {vid} MHz", 2)])
    enc, dec = gpu.get("enc_util"), gpu.get("dec_util")
    if enc is not None or dec is not None:
        parts = []
        if enc is not None:
            parts.append(f"enc {enc}%")
        if dec is not None:
            parts.append(f"dec {dec}%")
        f.try_add_multi([("  ·  ", 6), (" · ".join(parts), 2)])
    return f


def _row_pcie(inner, gpu):
    """Phase 8: `pcie_stuck` (set by app.py's State.sample() from a rolling
    per-GPU (util, gen_cur) history -- see _pcie_stuck_from_history) means
    this GPU has been observed downtrained BELOW its max link generation for
    3+ consecutive samples while under load (util>=20%) -- the DGX Spark's
    known power-draw-safety-mode quirk (gen1 x4 and stuck there). That's
    loud: the gen/width value itself turns red and the throughput/replay
    fields are replaced by the warning text outright, since a GPU stuck at
    gen1 under load isn't moving meaningful PCIe traffic anyway. A merely
    IDLE downtrain (degraded vs. max, but util<20, not stuck) is normal and
    gets only a small dim annotation."""
    gen_c, w_c = gpu.get("pcie_gen_cur"), gpu.get("pcie_width_cur")
    if gen_c is None or w_c is None:
        return None
    stuck = bool(gpu.get("pcie_stuck"))
    # Slot-effective ceiling (min of device and parent bridge, set in app.py)
    # with the device's own max as fallback -- judging against the raw device
    # max would call an M.2-fed card degraded forever, contradicting the
    # PCIE LINKS panel's verdict on the same link.
    gen_dev, w_dev = gpu.get("pcie_gen_max"), gpu.get("pcie_width_max")
    gen_m = gpu.get("pcie_gen_max_eff")
    w_m = gpu.get("pcie_width_max_eff")
    if gen_m is None:
        gen_m = gen_dev
    if w_m is None:
        w_m = w_dev
    degraded = (gen_m is not None and gen_c < gen_m) or (w_m is not None and w_c < w_m)
    idle_downtrain = degraded and not stuck and (gpu.get("util", 0) or 0) < 20

    f = _Flow(inner - 1)
    f.add(f"{'PCIE':<11}", 3)
    f.add(f"gen {gen_c} ×{w_c}", 4 if stuck else 8)
    if gen_m is not None and w_m is not None:
        f.try_add_multi([(" ", 3), (f"(max gen {gen_m} ×{w_m})", 6)])
        # The card can be wider than the slot feeding it -- say so, same as
        # the PCIE LINKS panel's "(dev ×16)" suffix.
        if w_dev is not None and w_m is not None and w_dev > w_m:
            f.try_add_multi([(" ", 3), (f"(dev ×{w_dev})", 6)])
    if stuck:
        f.try_add_multi([("  ", 3), ("⚠ gen1 under load — power safety mode?", 4)])
    else:
        rx, tx = _fmt_kbs(gpu.get("pcie_rx_kbs")), _fmt_kbs(gpu.get("pcie_tx_kbs"))
        if rx is not None:
            f.try_add_multi([("  ·  ", 6), (f"rx {rx}", 2)])
        if tx is not None:
            f.try_add_multi([("  ·  ", 6), (f"tx {tx}", 2)])
        replay = gpu.get("pcie_replay")
        if replay is not None:
            f.try_add_multi([("  ·  ", 6), (f"replays {replay}", 3)])
        if idle_downtrain:
            f.try_add_multi([("  ", 3), ("(idle downtrain)", 6)])
    return f


def _row_power(inner, gpu, knob=None, focus_id=None, pending=None):
    """(flow, right_segments) when caps.power_limit is True, else (None,
    None) -- caller only calls this once that cap's already been checked,
    but the limit value itself can still independently be missing.
    `knob` (Phase 4) is this GPU's gpu_power registry entry, or None when
    unavailable; when its id matches focus_id the static "limit 450 W" text
    is replaced by the "[◂ 450 W ▸]" stepper widget showing `pending`."""
    limit_mw = gpu.get("power_limit_mw")
    if limit_mw is None:
        return None, None
    limit_w = limit_mw / 1000.0
    draw = gpu.get("pwr_str", "N/A")
    try:
        draw_w = float(str(draw).replace("W", "").strip())
    except Exception:
        draw_w = None
    pct = max(0.0, min((draw_w / limit_w) * 100, 100.0)) if draw_w is not None and limit_w else 0.0
    is_focused = knob is not None and focus_id == knob.get("id")
    if is_focused:
        shown = pending if pending is not None else limit_w
        la, ra = term.knob_arrows()
        right = [(f"limit [{la} {shown:.0f} W {ra}]", 9)]
    else:
        right = [(f"limit {limit_w:.0f} W", 6)]
    lo, hi = gpu.get("power_min_mw"), gpu.get("power_max_mw")
    if lo is not None and hi is not None:
        right.append((f"  range {lo / 1000:.0f}–{hi / 1000:.0f} W", 6))
    f = _Flow(inner - 1 - _right_reserve(right))
    f.add(f"{'POWER':<11}", 3)
    f.add(f"draw {draw}  ", 8)
    lb, rb = term.bar_brackets()
    bar_w = 20 if inner > 90 else 14
    bar, slot = term.make_bar(pct, bar_w)
    f.add(lb, 10)
    f.add(bar, slot)
    f.add(rb, 10)
    f.add(f" {int(pct)}% of limit", 3)
    return f, right


def _row_thermal(inner, gpu):
    hotspot, vram = gpu.get("temp_hotspot"), gpu.get("temp_vram")
    slowdown, shutdown = gpu.get("temp_slowdown"), gpu.get("temp_shutdown")
    if hotspot is None and vram is None and slowdown is None and shutdown is None:
        return None, None
    right = []
    if slowdown is not None:
        right.append((f"slowdown {term.fmt_temp(slowdown)}", 6))
    if shutdown is not None:
        right.append((f"  shutdown {term.fmt_temp(shutdown)}", 6))
    f = _Flow(inner - 1 - _right_reserve(right))
    f.add(f"{'THERMAL':<11}", 3)
    f.add(f"core {term.fmt_temp(gpu.get('temp'))}", 8)
    if hotspot is not None:
        f.try_add_multi([("  ·  ", 6), (f"hotspot {term.fmt_temp(hotspot)}", 2)])
    if vram is not None:
        f.try_add_multi([("  ·  ", 6), (f"vram {term.fmt_temp(vram)}", 2)])
    return f, right


_THROTTLE_ORDER = ["power", "thermal", "hw-slowdown", "sync-boost"]

def _row_throttle(inner, gpu):
    th = gpu.get("throttle")
    if th is None:
        return None
    f = _Flow(inner - 1)
    f.add(f"{'THROTTLE':<11}", 3)
    if not th.get("any"):
        f.add("none  ", 1)
    else:
        active = [k for k in _THROTTLE_ORDER if th.get("flags", {}).get(k)]
        f.add((", ".join(active) or "active") + "  ", 4)
    for i, k in enumerate(_THROTTLE_ORDER):
        if i > 0:
            f.add(" · ", 6)
        f.add(k, 3)
        on = th.get("flags", {}).get(k)
        f.add(" ●" if on else " ─", 4 if on else 6)
    return f


def _row_state(inner, gpu, show_driver_cuda, driver, cuda):
    right = [(f"driver {driver}", 2), (f"    CUDA {cuda}", 2)] if show_driver_cuda else []
    f = _Flow(inner - 1 - _right_reserve(right))
    f.add(f"{'STATE':<11}", 3)
    f.add(f"persistence {gpu.get('persistence') or 'n/a'}", 8)
    compute_mode = gpu.get("compute_mode")
    if compute_mode:
        f.try_add_multi([("  ·  ", 6), (f"compute {compute_mode}", 2)])
        f.try_add_multi([("  ·  ", 6), (f"ecc {gpu.get('ecc') or 'n/a'}", 2)])
    vbios = gpu.get("vbios")
    if vbios:
        f.try_add_multi([("  ·  ", 6), (f"vbios {vbios}", 2)])
    return f, right


def _row_memory_unified(inner, gpu, caps, alloc_gib, swap_text):
    right = [] if caps.get("fan") else [("fan: no sensor — hidden", 6)]
    f = _Flow(inner - 1 - _right_reserve(right))
    f.add(f"{'MEMORY':<11}", 3)
    f.add("unified with system", 8)
    if alloc_gib:
        f.try_add_multi([(" — ", 6), (f"{alloc_gib:.1f} GiB allocated", 2)])
    if swap_text:
        f.try_add_multi([("  ·  ", 6), (swap_text, 3)])
    return f, right


def _row_procs(inner, gpu):
    f = _Flow(inner - 1)
    f.add(f"{'PROCS':<11}", 3)
    procs = gpu.get("procs") or []
    if not procs:
        f.add("none", 6)
        return f
    started = False
    for p in procs:
        pid, pname = p.get("pid", "?"), (p.get("name") or "").strip()
        # Unresolvable name -> show the pid alone, never "2224 2224".
        txt = f"{pid} {pname}" if pname and pname != str(pid) else str(pid)
        mem = p.get("mem")
        if mem is not None:
            txt += f" {term.fmt_mem(mem)}"
        sep = [("   ·   ", 6)] if started else []
        if f.try_add_multi(sep + [(txt, 3)]):
            started = True
        else:
            break
    return f


def _build_gpu_detail_panel(y, x0, width, gpu, caps, mem_state, show_driver_cuda, driver, cuda,
                             gpu_idx=None, knob_ui=None):
    """`gpu_idx`/`knob_ui` (Phase 4, both None outside live mode / read-only
    page 2) select this GPU's slice of the interactive knob registry and
    whether it's the tab-selected GPU -- see knobs.py's KnobUI and
    build_page2's per-GPU loop."""
    name = gpu.get("name", "Unknown")
    inner = width - 2
    is_selected = knob_ui is not None and gpu_idx == knob_ui.get("selected_gpu")
    title_prefix = [("> ", 8)] if is_selected else []
    prefix_len = sum(len(t) for t, _ in title_prefix)
    title = f"GPU {gpu.get('id', '?')} {name}"
    if len(title) > inner - 2 - prefix_len:
        title = title[:inner - 3 - prefix_len] + "…"
    temp, pwr = term.fmt_temp(gpu.get("temp")), gpu.get("pwr_str", "N/A")
    right_bits = [temp, pwr]
    if caps.get("fan"):
        fan = gpu.get("fan", "N/A")
        if fan not in ("N/A", "None"):
            right_bits.append(f"fan {fan}")
    p = panels.Panel(y, x0, width, title=title_prefix + [(title, 9)],
                      title_right=[(" · ".join(right_bits), 2)], kind="top")

    unified = not caps.get("mem_local", True)

    registry = (knob_ui or {}).get("registry") or []
    focus_id = (knob_ui or {}).get("focus_id")
    pending = (knob_ui or {}).get("pending")
    gid = gpu.get("id")
    def _knob(kind):
        return next((k for k in registry if k.get("gpu_id") == gid and k.get("kind") == kind), None)
    power_knob = _knob("gpu_power")
    sm_knob = _knob("clk_sm")
    mem_clk_knob = _knob("clk_mem")

    clk_right = [] if caps.get("power_limit") else [("power-limit API: N/A — knob hidden", 6)]
    f_clk = _row_clocks(inner, gpu, caps, reserve=_right_reserve(clk_right),
                         focus_id=focus_id, pending=pending, sm_knob=sm_knob, mem_knob=mem_clk_knob)
    p.add_row(_finish_row(f_clk), right=clk_right)

    if unified:
        alloc = mem_state.get("gpu_alloc") or 0
        swap = mem_state.get("swap")
        swap_text = ""
        if swap is not None and getattr(swap, "total", 0) > 0:
            swap_text = f"swap {term.fmt_mem(swap.used)} / {term.fmt_mem(swap.total)}"
        f_mem, mem_right = _row_memory_unified(inner, gpu, caps, alloc / (1024 ** 3) if alloc else 0, swap_text)
        p.add_row(_finish_row(f_mem), right=mem_right)
    else:
        if caps.get("pcie"):
            f_pcie = _row_pcie(inner, gpu)
            if f_pcie is not None:
                p.add_row(_finish_row(f_pcie))
        if caps.get("power_limit"):
            f_pwr, pwr_right = _row_power(inner, gpu, knob=power_knob, focus_id=focus_id, pending=pending)
            if f_pwr is not None:
                p.add_row(_finish_row(f_pwr), right=pwr_right)
        f_th, th_right = _row_thermal(inner, gpu)
        if f_th is not None:
            p.add_row(_finish_row(f_th), right=th_right)
        f_thr = _row_throttle(inner, gpu)
        if f_thr is not None:
            p.add_row(_finish_row(f_thr))

    f_state, state_right = _row_state(inner, gpu, show_driver_cuda, driver, cuda)
    p.add_row(_finish_row(f_state), right=state_right)

    if not unified:
        p.add_row(_finish_row(_row_procs(inner, gpu)))

    return p


# --- NIC advanced panel -------------------------------------------------

def _nic_panel_title(nics, nics_pf, nic_fw):
    hw_label = nics[0].get("hw_label") if nics else "NIC"
    n_ports = len(nics)
    total_pf = len(nics_pf) or sum(n.get("pf_count", 1) for n in nics)
    if n_ports and total_pf and total_pf % n_ports == 0:
        pf_each = total_pf // n_ports
        extra = (f"{hw_label} · {n_ports} port{'s' if n_ports != 1 else ''} "
                 f"× {pf_each} PF{'s' if pf_each != 1 else ''} each")
    elif total_pf:
        extra = f"{hw_label} · {total_pf} PFs"
    else:
        extra = hw_label
    fw = next(iter(nic_fw.values()), None) if nic_fw else None
    if fw and fw != "?":
        extra += f" · fw {fw}"
    return extra


_NIC_COLS = [("PORT", 5), ("RDMA DEV", 16), ("NETDEV", 16), ("STATE", 7), ("SPEED", 7),
             ("RX", 12), ("TX", 12), ("CNP S/H", 11), ("ECN MARK", 11), ("DISCARD", 9)]
# Column ladder: at narrow widths, drop whole trailing columns in this order
# (least essential first) rather than let panels.render's per-row clip cut
# them off mid-token ("TX          C..."). PORT..TX are never dropped.
_NIC_DROPPABLE = ["DISCARD", "ECN MARK", "CNP S/H"]

def _nic_columns_for_width(inner):
    cols = list(_NIC_COLS)
    for name in _NIC_DROPPABLE:
        if sum(w for _, w in cols) + 2 <= inner:
            break
        cols = [c for c in cols if c[0] != name]
    return cols


def _build_nic_panel(y, x0, width, nics_pf, title_extra, asic_temp):
    inner = width - 2
    title_right = [(f"asic {term.fmt_temp(asic_temp)}", 2)] if asic_temp is not None else []
    p = panels.Panel(y, x0, width, title=[("NIC", 9), (f" {title_extra}", 3)],
                      title_right=title_right, kind="top")
    if not nics_pf:
        p.add_row([(1, "no RDMA-capable NICs detected", 6)])
        return p

    cols = _nic_columns_for_width(inner)
    positions, x = [], 2
    for _label, w in cols:
        positions.append(x)
        x += w
    p.add_row([(pos, label, 6) for pos, (label, _w) in zip(positions, cols)])

    for row in nics_pf:
        state_txt, state_slot = ("up", 1) if row.get("up") else ("down", 4)
        cnp_s = _fmt_count_rate(row.get("cnp_sent"))
        cnp_h = _fmt_count_rate(row.get("cnp_handled"))
        cnp = f"{cnp_s} / {cnp_h}" if cnp_s is not None and cnp_h is not None else "n/a"
        ecn = _fmt_count_rate(row.get("ecn_marked"))
        disc = _fmt_count_rate(row.get("discard"))
        by_col = {
            "PORT": (row.get("port", "-"), 3), "RDMA DEV": (row.get("rdma_dev", "-"), 8),
            "NETDEV": (row.get("netdev", "-"), 3), "STATE": (state_txt, state_slot),
            "SPEED": (row.get("speed_str", "-"), 3),
            "RX": (term.fmt_rate(row.get("rx_bps", 0), "bit"), 2),
            "TX": (term.fmt_rate(row.get("tx_bps", 0), "bit"), 2),
            "CNP S/H": (cnp, 3), "ECN MARK": (ecn if ecn is not None else "n/a", 3),
            "DISCARD": (disc if disc is not None else "n/a", 3),
        }
        segs = [(pos, str(by_col[name][0]), by_col[name][1])
                for pos, (name, _w) in zip(positions, cols)]
        p.add_row(segs)
    return p


# --- PCIE LINKS panel (Phase 8) ------------------------------------------
# Every PCIe endpoint regardless of occupant -- the DGX Spark's shared M.2
# slot can carry an NVMe drive or an eGPU depending on what's plugged in,
# and the known GPU power-safety-mode quirk (downtrain to gen1 x4 and STAY
# there under load) is a link-state symptom this panel exists to surface
# loudly, while a routine idle downtrain (any device, any slot) stays quiet.

def _pcie_status_cell(row, gpu_by_addr):
    """(text, slot) for one PCIE LINKS row's status column. `gpu_by_addr` is
    state["gpus"] keyed by pci_addr (see app.py's GPU<->slot join) -- only
    consulted for rows whose class is "gpu", since only a GPU sample carries
    the util/pcie_stuck context needed to tell "idle downtrain" (normal)
    apart from a real problem."""
    if not row.get("degraded"):
        return "ok", 1
    gpu = gpu_by_addr.get(row.get("addr")) if row.get("class_label") == "gpu" else None
    if gpu is not None:
        if gpu.get("pcie_stuck"):
            return "⚠ gen1 under load — power safety mode?", 4
        if (gpu.get("util", 0) or 0) < 20:
            return "idle downtrain", 6
    return "⚠ degraded", 5


def _build_pcie_links_panel(y, x0, width, pcie_links, gpus):
    """One row per PCIe endpoint (storage/NVMe, network, display/3D),
    collectors.PcieCollector's sort order already applied (degraded first,
    then gpu > nvme/storage > network, then address). Caller (build_page2)
    only invokes this when `pcie_links` is non-empty -- the panel is absent
    entirely on a platform with no /sys/bus/pci/devices tree (Windows/macOS
    dev boxes), matching the rest of this module's "no sensor, no row" rule
    at the whole-panel level."""
    inner = width - 2
    p = panels.Panel(y, x0, width,
                      title=[("PCIE LINKS", 9),
                             (" negotiated link state · every endpoint, regardless of occupant", 3)],
                      kind="top")
    if not pcie_links:
        p.add_row([(1, "no PCIe topology detected", 6)])
        return p

    gpu_by_addr = {g.get("pci_addr"): g for g in (gpus or []) if g.get("pci_addr")}
    for row in pcie_links:
        status_text, status_slot = _pcie_status_cell(row, gpu_by_addr)
        right = [(status_text, status_slot)]
        f = _Flow(inner - 1 - _right_reserve(right))
        name = row.get("name") or "?"
        f.add(f"{name[:21]:<22}", 8)
        f.try_add(f"{row.get('addr', '?'):<13}", 6)
        gen_c, w_c = row.get("gen_cur"), row.get("width_cur")
        cur_txt = f"gen {gen_c} ×{w_c}" if gen_c is not None and w_c is not None else "gen ? ×?"
        f.try_add(f"{cur_txt:<11}", 8)
        # "of gen X xY" compares against the SLOT-EFFECTIVE max
        # (gen_max_eff/width_max_eff -- min(device, parent bridge), see
        # collectors.PcieCollector.sample()), not the device's own raw
        # capability -- an M.2/OCuLink GPU already at its slot's ceiling
        # must read "of gen 4 x4", not "of gen 4 x16". When the device COULD
        # do more than the slot allows, a dim "(dev ...)" suffix says so --
        # informative (the slot is the limiter), not an alarm.
        gen_eff, w_eff = row.get("gen_max_eff"), row.get("width_max_eff")
        gen_dev, w_dev = row.get("gen_max"), row.get("width_max")
        if gen_eff is not None and w_eff is not None:
            dev_bits = []
            if gen_dev is not None and gen_dev > gen_eff:
                dev_bits.append(f"gen{gen_dev}")
            if w_dev is not None and w_dev > w_eff:
                dev_bits.append(f"×{w_dev}")
            max_txt = f"of gen {gen_eff} ×{w_eff}"
            if dev_bits:
                max_txt += f" (dev {' '.join(dev_bits)})"
            f.try_add(max_txt, 6)
        p.add_row(_finish_row(f, 1), right=right)
    return p


# --- THERMALS / POWER RAILS / NVME SMART ---------------------------------

def _build_thermals_panel(y, x0, width, entries, kind="top", paired=False, show_bars=True, has_spbm=False):
    """`paired` is True in the wide-tier THERMALS/NVME-SMART side-by-side
    layout: that card has plenty of per-row width (~50+ cols) for a single
    "label temp bar" entry, so it always packs ONE entry per row WITH its
    bar -- two-per-row packing is for the full-width standalone variant
    only (cramming two cells into half the width is what silently starved
    the bar of room before). `show_bars` only applies in that standalone
    mode; the paired layout never drops bars (see build_page2's degrade
    order: it drops the Zone-A..G group first instead)."""
    inner = width - 2
    title = [("THERMALS", 9), (" zones + hwmon · spbm" if has_spbm else " zones + hwmon", 3)]
    p = panels.Panel(y, x0, width, title=title, kind=kind)
    if not entries:
        p.add_row([(1, "no thermal sensors detected", 6)])
        return p

    label_w = 14
    if paired:
        for e in entries:
            f = _Flow(inner - 1)
            f.try_add_multi([(f"{e['label'][:label_w]:<{label_w}}", 3), (f"{term.fmt_temp(e['temp_c']):>5} ", 8)])
            lb, rb = term.bar_brackets()
            bar, slot = term.make_bar(max(0.0, min(e["temp_c"], 100.0)), 11)
            f.try_add_multi([(lb, 10), (bar, slot), (rb, 10)])
            p.add_row(_finish_row(f))
        return p

    per_row = 2 if inner >= (label_w + 20) * 2 else 1
    show_bars = show_bars and inner // per_row >= label_w + 20
    cell_w = inner // per_row
    for i in range(0, len(entries), per_row):
        chunk = entries[i:i + per_row]
        row = []
        for j, e in enumerate(chunk):
            f = _Flow(cell_w - 1)
            f.try_add_multi([(f"{e['label'][:label_w]:<{label_w}}", 3), (f"{term.fmt_temp(e['temp_c']):>5} ", 8)])
            if show_bars:
                lb, rb = term.bar_brackets()
                bar, slot = term.make_bar(max(0.0, min(e["temp_c"], 100.0)), 11)
                f.try_add_multi([(lb, 10), (bar, slot), (rb, 10)])
            segs = list(f.segs)
            if segs:
                segs[0] = (j * cell_w + 1, segs[0][1], segs[0][2])
            row.extend(segs)
        p.add_row(row)
    return p


_RAIL_ORDER = ["dc_input", "sys_total", "soc_pkg", "cpu_gpu", "gpu", "cpu_p", "cpu_e",
               "vcore", "prereg", "dla"]
_CONTROLLER_ORDER = ["pl1", "pl2", "syspl1", "syspl2"]
_RAIL_ESSENTIALS = ["dc_input", "sys_total"]
_CONTROLLER_ESSENTIALS = ["pl1", "syspl1"]

def _build_power_rails_panel(y, x0, width, rails, kind="top", essentials_only=False, knob_ui=None):
    inner = width - 2
    p = panels.Panel(y, x0, width, title=[("POWER RAILS", 9), (" spbm power monitor", 3)], kind=kind)
    if not rails:
        p.add_row([(1, "no power-rail data", 6)])
        return p
    by_label = {r["label"]: r for r in rails}
    plain_order = _RAIL_ESSENTIALS if essentials_only else _RAIL_ORDER
    ctrl_order = _CONTROLLER_ESSENTIALS if essentials_only else _CONTROLLER_ORDER
    plain = [by_label[l] for l in plain_order if l in by_label]
    controllers = [by_label[l] for l in ctrl_order if l in by_label]
    if not essentials_only:
        known = set(_RAIL_ORDER) | set(_CONTROLLER_ORDER)
        for r in rails:
            if r["label"] in known:
                continue
            (controllers if r.get("cap_w") is not None else plain).append(r)

    per_row = 2 if inner >= 34 else 1
    cell_w = inner // per_row
    for i in range(0, len(plain), per_row):
        chunk = plain[i:i + per_row]
        row = []
        for j, r in enumerate(chunk):
            f = _Flow(cell_w - 1)
            f.try_add_multi([(f"{r['label'][:10]:<10}", 3), (f"{r['watts']:>6.1f} W", 8)])
            segs = list(f.segs)
            if segs:
                segs[0] = (j * cell_w + 1, segs[0][1], segs[0][2])
            row.extend(segs)
        p.add_row(row)

    focus_id = (knob_ui or {}).get("focus_id")
    pending = (knob_ui or {}).get("pending")
    for r in controllers:
        cap = r.get("cap_w")
        if cap is None:
            continue
        # Phase 4: draw stays the measured powerN_input reading (r["watts"])
        # -- what's interactive is the CAP, so a focused rail steps/shows
        # the pending cap value in place of the static "of 250 W" tail.
        is_focused = focus_id == f"rail:{r['label']}"
        pct = max(0.0, min((r["watts"] / cap) * 100 if cap else 0.0, 100.0))
        f = _Flow(inner - 1)
        f.add(f"{r['label']:<10}", 3)
        f.add(f"{r['watts']:>5.1f} W ", 8)
        lb, rb = term.bar_brackets()
        bar, slot = term.make_bar(pct, 16 if inner > 60 else 10)
        f.add(lb, 10)
        f.add(bar, slot)
        f.add(rb, 10)
        if is_focused:
            shown = pending if pending is not None else cap
            la, ra = term.knob_arrows()
            f.add(f" of [{la} {shown:.0f} W {ra}]", 9)
        else:
            f.add(f" of {cap:.0f} W", 3)
        p.add_row(_finish_row(f))
    return p


def _build_nvme_smart_panel(y, x0, width, smart, kind="top"):
    inner = width - 2
    if smart is None:
        p = panels.Panel(y, x0, width, title=[("NVME SMART", 9)], kind=kind)
        p.add_row([(1, "no NVMe device detected", 6)])
        return p

    device, model = smart.get("device", "nvme?"), smart.get("model") or "Unknown"
    p = panels.Panel(y, x0, width, title=[("NVME SMART", 9), (f" {device} · {model}", 3)], kind=kind)

    if smart.get("needs_root"):
        temp = smart.get("temp")
        if temp is not None:
            p.add_row([(2, f"composite {term.fmt_temp(temp)}", 3)])
        p.add_row([(2, "SMART needs root — run with sudo (composite temp via hwmon)", 6)])
        return p

    wear, spare = smart.get("wear_pct"), smart.get("spare_pct")
    temp = smart.get("smart_temp") if smart.get("smart_temp") is not None else smart.get("temp")
    row1 = _flow_join(inner, [
        (f"wear {wear}%", 3) if wear is not None else None,
        (f"spare {spare}%", 3) if spare is not None else None,
        (f"composite {term.fmt_temp(temp)}", 3) if temp is not None else None,
    ])
    if row1:
        p.add_row(row1)

    written, read = smart.get("written_tb"), smart.get("read_tb")
    row2 = _flow_join(inner, [
        (f"written {written:.1f} TB", 3) if written is not None else None,
        (f"read {read:.1f} TB", 3) if read is not None else None,
    ])
    if row2:
        p.add_row(row2)

    poh, cycles = smart.get("power_on_hours"), smart.get("cycles")
    row3 = _flow_join(inner, [
        (f"power-on {poh:,} h", 3) if poh is not None else None,
        (f"cycles {cycles}", 3) if cycles is not None else None,
    ])
    if row3:
        p.add_row(row3)

    unsafe, media = smart.get("unsafe_shutdowns"), smart.get("media_errors")
    row4 = _flow_join(inner, [
        (f"unsafe shutdowns {unsafe}", 3) if unsafe is not None else None,
        (f"media errors {media}", 3) if media is not None else None,
    ])
    if row4:
        p.add_row(row4)

    p.add_row([(2, "via nvme hwmon · smartctl/nvme -j fallback", 6)])
    return p


def build_page2(state, tier, width, x0=0, height=None, knob_ui=None, theme_toast=None,
                 remote=None, cluster_tabs=False, has_perf=False):
    """Builds page 2: header (ADVANCED tab active), one full-width detail
    panel per GPU, the NIC advanced panel (ungrouped per-PF rows), and
    THERMALS / POWER RAILS / NVME SMART. Every panel build is wrapped
    defensively same as build_page1 -- missing/degraded collector data must
    never stop the rest of the page from rendering.

    `knob_ui` (Phase 4) is None in snapshot mode and read-only page 2 --
    when present (live mode only, from KnobUI.render_ctx()) it's a plain
    dict {registry, selected_gpu, focus_id, pending, confirming,
    confirm_text, toast} threaded into the GPU/POWER-RAILS panels for the
    focused-knob widget and into the footer for the tab/P/C/M/R/X hints and
    the confirm/toast overlay. `theme_toast` ('c' key, live mode) is a
    (text, slot) pair that overlays the footer's toast slot for a few ticks
    after a theme switch -- deferred to a KnobUI apply-toast that's
    currently confirming (that prompt needs the key press it's waiting on),
    otherwise shown in preference to (and without disturbing) any other
    knob_ui state the per-GPU/rail widgets still read this frame."""
    out = []

    gpus = state.get("gpus") or []
    gpu_caps = (state.get("caps") or {}).get("gpu") or []
    mem = state.get("mem") or {}
    nics = state.get("nics") or []
    nics_pf = state.get("nic_pf") or []
    thermal_entries = state.get("thermal") or []
    power_rails = state.get("power_rails") or []
    smart = state.get("smart")
    thermal_caps = (state.get("caps") or {}).get("thermal") or {}
    driver, cuda = state.get("driver", "Unknown"), state.get("cuda", "Unknown")
    nic_asic_temp = state.get("nic_asic_temp")
    nic_fw = state.get("nic_fw") or {}
    pcie_links = state.get("pcie_links") or []

    has_spbm = bool(thermal_caps.get("spbm"))
    paired = tier == "wide" and width >= 90

    # Height degrade order differs by layout: the paired (side-by-side)
    # THERMALS card always has room for one-entry-per-row WITH its bar (see
    # _build_thermals_panel), so it degrades by dropping the redundant
    # Zone-A..G group (only once spbm's own named zones are live) before it
    # ever touches bars; the standalone full-width card has no zone
    # redundancy to shed, so it degrades by dropping bars first instead.
    drop_zones = False
    show_thermal_bars = True
    essentials_only = False
    if height:
        if paired:
            if height < 42:
                drop_zones = has_spbm
            if height < 36:
                essentials_only = True
        else:
            if height < 45:
                show_thermal_bars = False
            if height < 38:
                essentials_only = True

    if drop_zones:
        thermal_entries = [e for e in thermal_entries if e.get("kind") != "thermal"]

    out.append(_build_header(x0, width, tier, page=2, cluster_tabs=cluster_tabs, remote=remote, has_perf=has_perf))
    y = 1

    for idx, gpu in enumerate(gpus):
        try:
            caps = gpu_caps[idx] if idx < len(gpu_caps) else {}
            panel = _build_gpu_detail_panel(y, x0, width, gpu, caps, mem,
                                             idx == len(gpus) - 1, driver, cuda,
                                             gpu_idx=idx, knob_ui=knob_ui)
            out.append(panel)
            y += panel.total_height + 1
        except Exception:
            continue

    try:
        title_extra = _nic_panel_title(nics, nics_pf, nic_fw)
        nic_panel = _build_nic_panel(y, x0, width, nics_pf, title_extra, nic_asic_temp)
        out.append(nic_panel)
        y += nic_panel.total_height + 1
    except Exception:
        pass

    # Phase 8: absent entirely (not even an empty-state row) when the
    # collector found nothing -- e.g. Windows/macOS dev boxes with no
    # /sys/bus/pci/devices tree at all, matching the POWER RAILS panel's
    # "only build it when there's something to show" gating just below.
    if pcie_links:
        try:
            pcie_panel = _build_pcie_links_panel(y, x0, width, pcie_links, gpus)
            out.append(pcie_panel)
            y += pcie_panel.total_height + 1
        except Exception:
            pass

    show_rails = has_spbm and bool(power_rails)

    try:
        if paired:
            half = (width - 2) // 2
            w1, w2 = half, width - half - 2
            th_panel = _build_thermals_panel(y, x0, w1, thermal_entries, paired=True, has_spbm=has_spbm)
            sm_panel = _build_nvme_smart_panel(y, x0 + w1 + 2, w2, smart)
            out.append(th_panel)
            out.append(sm_panel)
            y += max(th_panel.total_height, sm_panel.total_height) + 1
            if show_rails:
                rails_panel = _build_power_rails_panel(y, x0, width, power_rails,
                                                         essentials_only=essentials_only, knob_ui=knob_ui)
                out.append(rails_panel)
                y += rails_panel.total_height + 1
        else:
            th_panel = _build_thermals_panel(y, x0, width, thermal_entries,
                                              show_bars=show_thermal_bars, has_spbm=has_spbm)
            out.append(th_panel)
            y += th_panel.total_height + 1
            if show_rails:
                rails_panel = _build_power_rails_panel(y, x0, width, power_rails,
                                                         essentials_only=essentials_only, knob_ui=knob_ui)
                out.append(rails_panel)
                y += rails_panel.total_height + 1
            sm_panel = _build_nvme_smart_panel(y, x0, width, smart)
            out.append(sm_panel)
            y += sm_panel.total_height + 1
    except Exception:
        pass

    has_nvml = (state.get("caps") or {}).get("has_nvml", False)
    rate = state.get("rate", 1.0)

    extra_keys = None
    if knob_ui is not None:
        registry = knob_ui.get("registry") or []
        has_power_knob = any(k["kind"] in ("gpu_power", "rail_power") for k in registry)
        has_clock_knob = any(k["kind"] in ("clk_sm", "clk_mem") for k in registry)
        has_reset = any(k["kind"] == "reset_clocks" for k in registry)
        has_persist = any(k["kind"] == "persist" for k in registry)
        bits = []
        if len(gpus) > 1 or has_power_knob or has_clock_knob:
            bits.append("tab gpu")
        if has_power_knob:
            bits.append("P pwr limit")
        if has_clock_knob:
            bits.append("C/M clock locks")
        if has_reset:
            bits.append("R reset")
        if has_persist:
            bits.append("X persist")
        extra_keys = (" · " + " · ".join(bits)) if bits else None

    footer_knob_ui = knob_ui
    if theme_toast and not (knob_ui and knob_ui.get("confirming")):
        footer_knob_ui = dict(knob_ui) if knob_ui else {}
        footer_knob_ui["toast"] = theme_toast

    remote_note = "knobs are local-only" if remote else None
    out.append(_build_footer(x0, width, y, driver, cuda, rate, has_nvml,
                              extra_keys=extra_keys, knob_ui=footer_knob_ui, remote_note=remote_note))
    return out


# =========================================================================
# Page 3 (Phase 6, --cluster only): SECTIONS (<=8 nodes) / FLEET (>8 nodes)
# =========================================================================
# `cluster_ctx` (build_page3's only argument besides the usual tier/width/x0/
# height) is a plain dict: {"name": str, "views": [...] (from
# cluster.ClusterAggregator.get_views()), "rate": float, "ui": a
# cluster.ClusterUI or None}. Each view's "sample" -- when present -- is the
# SAME shape build_page1/build_page2 already consume (local State.sample()
# or cluster.from_wire() of a remote /sample payload), so every helper below
# reads it exactly like page 1's builders read the local sample. Every
# section/row builder takes `selected_idx` as a plain int (-1 = none) rather
# than threading a ClusterUI object around, matching the "screen-agnostic,
# pure data in" style of the rest of this module.

def _cluster_fmt_capacity(bytes_val):
    """Sum-of-nodes capacity, TiB once it's big enough to want it -- unlike
    term.fmt_mem (GiB only) or term.fmt_disk_size (no space/i, single disk
    scale), the SECTIONS header sums whole-cluster storage/memory into the
    TiB range routinely (mock: "Σ 14.9 TiB")."""
    try:
        v = float(bytes_val)
    except Exception:
        return "N/A"
    div = 1024.0 ** 4
    if v >= div:
        return f"{v / div:.1f} TiB"
    return f"{v / (1024.0 ** 3):.1f} GiB"


def _cluster_metrics(sample):
    """Node-level rollups from one node's sample dict -- shared by the
    SECTIONS row builders, the FLEET matrix, and the alert rules. Returns
    None for a node with no sample yet (unreachable / never polled)."""
    if not sample:
        return None
    try:
        cpu = sample.get("cpu") or {}
        clusters = cpu.get("clusters") or []
        total_cores = sum(len(c.get("cores") or []) for c in clusters)
        cpu_pct = (sum(c.get("avg", 0.0) * len(c.get("cores") or []) for c in clusters) / total_cores
                   if total_cores else 0.0)
        cpu_ghz = max((c.get("ghz") or 0.0 for c in clusters), default=0.0)
        pairs = []
        for c in clusters:
            pairs.extend(zip(c.get("cores") or [], c.get("loads") or []))
        pairs.sort(key=lambda t: t[0])
        percpu = [l for _, l in pairs] or (cpu.get("percpu") or [])
        hist_len = max((len(c.get("history") or []) for c in clusters), default=0)
        cpu_hist = []
        for i in range(hist_len):
            num = den = 0.0
            for c in clusters:
                h = c.get("history") or []
                if i < len(h):
                    w = len(c.get("cores") or []) or 1
                    num += h[i] * w
                    den += w
            cpu_hist.append(num / den if den else 0.0)

        mem = sample.get("mem") or {}
        vm = mem.get("vm")
        mem_total = getattr(vm, "total", None) if vm is not None else None
        mem_used = getattr(vm, "used", None) if vm is not None else None
        gpu_alloc = mem.get("gpu_alloc") or 0

        gpus = sample.get("gpus") or []
        gpu_util_max = max((g.get("util", 0) or 0 for g in gpus), default=0)
        gpu_pwr_sum = 0.0
        for g in gpus:
            try:
                gpu_pwr_sum += float(str(g.get("pwr_str", "")).replace("W", "").strip())
            except Exception:
                pass

        nics = sample.get("nics") or []
        rx_sum = sum(n.get("rx_bps", 0) or 0 for n in nics)
        tx_sum = sum(n.get("tx_bps", 0) or 0 for n in nics)

        nic_pf = sample.get("nic_pf") or []
        cnp_sent_sum = sum((p.get("cnp_sent") or 0) for p in nic_pf)
        cnp_handled_sum = sum((p.get("cnp_handled") or 0) for p in nic_pf)

        disks = sample.get("disks") or []
        worst_disk_pct = max((d.get("used_pct") or 0 for d in disks), default=0)
        hottest_disk = max((d for d in disks if d.get("temp") is not None),
                            key=lambda d: d["temp"], default=(disks[0] if disks else None))

        all_temps = [cpu.get("temp")] + [g.get("temp") for g in gpus] + [sample.get("nic_asic_temp")]
        max_temp = max((t for t in all_temps if isinstance(t, (int, float))), default=None)

        power_rails = sample.get("power_rails") or []

        return {
            "cpu_pct": cpu_pct, "cpu_ghz": cpu_ghz, "cpu_temp": cpu.get("temp"),
            "percpu": percpu, "cpu_hist": cpu_hist,
            "mem_total": mem_total, "mem_used": mem_used,
            "mem_pct": (mem_used / mem_total * 100) if mem_total else 0.0,
            "mem_hist": list(mem.get("history") or []),
            "gpu_alloc": gpu_alloc, "gpus": gpus,
            "gpu_util_max": gpu_util_max, "gpu_pwr_sum": gpu_pwr_sum,
            "nics": nics, "rx_sum": rx_sum, "tx_sum": tx_sum,
            "nic_pf": nic_pf, "cnp_sent_sum": cnp_sent_sum, "cnp_handled_sum": cnp_handled_sum,
            "nic_asic_temp": sample.get("nic_asic_temp"),
            "disks": disks, "worst_disk_pct": worst_disk_pct, "hottest_disk": hottest_disk,
            "max_temp": max_temp, "power_rails": power_rails,
        }
    except Exception:
        return None


def _cluster_alerts(view, m):
    """[(node_name, reason_text, "red"|"amber"), ...] for one node, per the
    spec's rules: unreachable is the only "red" case, everything else
    (temp>=80, cnp rate>50/s, a port link down, a disk>=85% used, a GPU
    stuck at gen1 under load) is "amber" -- matches the design mockup's
    ALERTS row (one node cnp+temp, one link down, both amber; one
    unreachable, red). The pcie_stuck flag itself is computed
    node-side by app.py's State.sample() and travels here unchanged over
    the wire format (cluster.to_wire/from_wire pass gpu dict fields through
    verbatim) -- this function only has to read it."""
    name = view.get("name") or "?"
    if view.get("stale") or not view.get("sample"):
        return [(name, "unreachable", "red")]
    if m is None:
        return []
    out = []
    if m.get("max_temp") is not None and m["max_temp"] >= 80:
        out.append((name, f"{m['max_temp']:.0f}°C", "amber"))
    for g in m.get("gpus") or []:
        if g.get("pcie_stuck"):
            out.append((name, "pcie stuck gen1 under load", "amber"))
    for p in m.get("nic_pf") or []:
        if (p.get("cnp_sent") or 0) > 50:
            out.append((name, f"cnp storm {p.get('port', '?')}", "amber"))
    for n in m.get("nics") or []:
        if not n.get("up"):
            out.append((name, f"{n.get('name', '?')} link down", "amber"))
    for d in m.get("disks") or []:
        if (d.get("used_pct") or 0) >= 85:
            out.append((name, f"{d.get('name', '?')} {d.get('used_pct'):.0f}%", "amber"))
    return out


def _short_health_code(reason):
    if reason == "unreachable":
        return "down"
    if reason.startswith("pcie stuck"):
        return "pcie⚠"
    if reason.startswith("cnp storm"):
        return "cnp"
    if reason.endswith("link down"):
        port = reason.split()[0]
        return f"{port}▼"
    if reason.endswith("%"):
        return reason.rsplit(" ", 1)[-1]
    if "°C" in reason:
        return "hot"
    return reason[:6]


def _cluster_sort_key(mode):
    if mode == "gpu":
        return lambda vm: -((vm[1] or {}).get("gpu_util_max", 0))
    if mode == "power":
        return lambda vm: -((vm[1] or {}).get("gpu_pwr_sum", 0))
    if mode == "alerts":
        return lambda vm: 0 if _cluster_alerts(vm[0], vm[1]) else 1
    return lambda vm: (vm[0].get("name") or "")


def _cluster_node_field(name, w):
    return f"{(name or '?')[:w]:<{w}}"


def _cluster_row_prefix(f, view, is_selected, name_w):
    f.add("▶ " if is_selected else "  ", 9 if is_selected else 3)
    f.add(_cluster_node_field(view.get("name"), name_w) + " ", 8)


def _cluster_pct_bar(f, pct, width, suffix_slot=3):
    lb, rb = term.bar_brackets()
    bar, slot = term.make_bar(pct, width)
    f.add(lb, 10)
    f.add(bar, slot)
    f.add(rb, 10)
    f.add(f" {int(pct):>3}%", suffix_slot)


# --- SECTIONS mode (<=8 nodes) ------------------------------------------

def _cluster_bar_w(width):
    """Bar cells for the SECTIONS rows: 11 up to a 100-column interior, then
    growing with the terminal (to 24) so a wide window is not left blank."""
    inner = width - 2
    return 11 if inner < 100 else min(24, 11 + (inner - 100) // 3)


def _cluster_layout(width, metrics, name_w):
    """{"bar": cells, "gcol": column} shared by the CPU, MEMORY and GPU rows
    so their history graphs start in one column and run to the right edge."""
    bw = _cluster_bar_w(width)
    strip_w = max((len(term.core_strip((m or {}).get("percpu") or [])) for m in metrics), default=0)
    lead = 2 + name_w + 1 + 2 + bw + 5
    detail = max(2 + (strip_w + 2 if strip_w else 0) + 10 + 6,   # CPU
                 2 + 17 + 14,                                   # MEMORY
                 2 + 13 + 6 + 7)                                # GPU
    return {"bar": bw, "gcol": lead + detail + 1}


def _cluster_graph(f, history, lay):
    """Pads Flow `f` to the shared graph column (when the row has room for
    a useful graph there), then adds the history graph to the row's end."""
    if lay and f.room() >= (lay["gcol"] - f.used) + 14:
        f.add(" " * max(0, lay["gcol"] - f.used), 3)
        _add_graded_spark(f, history, f.room(), lead="", cap=None)
    else:
        _add_graded_spark(f, history, 26, lead="")


def _cluster_cpu_summary(views):
    total_cores = total_clusters = 0
    family = None
    for v in views:
        s = v.get("sample")
        if not s:
            continue
        clusters = (s.get("cpu") or {}).get("clusters") or []
        total_clusters += len(clusters)
        total_cores += sum(len(c.get("cores") or []) for c in clusters)
        if clusters and family is None:
            family = (clusters[0].get("label") or "CPU").split("-")[0]
    if not total_clusters:
        return "no data"
    return f"{total_cores} cores · {total_clusters}× {family or 'CPU'} clusters"


def _build_cluster_cpu_section(y, x0, width, views, metrics, selected_idx, name_w, kind, lay=None):
    inner = width - 2
    loadavg_sum = 0.0
    have_loadavg = False
    for v, m in zip(views, metrics):
        s = v.get("sample")
        if s and not v.get("stale"):
            la = (s.get("cpu") or {}).get("loadavg")
            if la:
                loadavg_sum += la[0]
                have_loadavg = True
    title_right = [(f"Σ load {loadavg_sum:.1f}", 3)] if have_loadavg else []
    p = panels.Panel(y, x0, width, title=[("CPU", 9), (f" {_cluster_cpu_summary(views)}", 3)],
                      title_right=title_right, kind=kind)
    for i, (view, m) in enumerate(zip(views, metrics)):
        f = _Flow(inner - 2)
        _cluster_row_prefix(f, view, i == selected_idx, name_w)
        if view.get("stale") or m is None:
            why = (view.get("error") or "").strip()
            f.add("— unreachable" + (f" ({why})" if why else ""), 6)
            p.add_row(_finish_row(f, 1))
            continue
        _cluster_pct_bar(f, m["cpu_pct"], lay["bar"] if lay else 11)
        f.add("  ", 3)
        strip = term.core_strip(m["percpu"])
        if strip:
            f.try_add(strip + "  ", 1)
        f.try_add(f"{m['cpu_ghz']:.2f} GHz  ", 3)
        f.try_add(f"{term.fmt_temp(m['cpu_temp']):<5} ", 3)
        _cluster_graph(f, m["cpu_hist"], lay)
        p.add_row(_finish_row(f, 1))
    return p


def _build_cluster_mem_section(y, x0, width, views, metrics, selected_idx, name_w, kind, lay=None):
    inner = width - 2
    total = sum((m or {}).get("mem_total") or 0 for m in metrics)
    used = sum((m or {}).get("mem_used") or 0 for m in metrics)
    subtitle = (f"Σ {_cluster_fmt_capacity(total)} unified · Σ used {_cluster_fmt_capacity(used)}"
                if total else "no data")
    p = panels.Panel(y, x0, width, title=[("MEMORY", 9), (f" {subtitle}", 3)], kind=kind)
    for i, (view, m) in enumerate(zip(views, metrics)):
        f = _Flow(inner - 2)
        _cluster_row_prefix(f, view, i == selected_idx, name_w)
        if view.get("stale") or m is None or not m.get("mem_total"):
            f.add("— unreachable" if (view.get("stale") or m is None) else "no data", 6)
            p.add_row(_finish_row(f, 1))
            continue
        gpu_alloc = m.get("gpu_alloc") or 0
        used_other = max(0.0, (m["mem_used"] or 0) - gpu_alloc)
        segs_spec = [(used_other / m["mem_total"], 1, "█")]
        if gpu_alloc > 0:
            segs_spec.append((gpu_alloc / m["mem_total"], 2, "▓"))
        segs_spec.append((max(0.0, 1.0 - sum(s[0] for s in segs_spec)), 7, "░"))
        chunks = term.seg_bar(segs_spec, lay["bar"] if lay else 16)
        lb, rb = term.bar_brackets()
        f.add(lb, 10)
        for text, slot in chunks:
            f.add(text, slot)
        f.add(rb, 10)
        f.add(f" {int(m['mem_pct']):>3}%  ", 3)
        f.try_add(f"{m['mem_used'] / (1024 ** 3):.1f} / {m['mem_total'] / (1024 ** 3):.1f} G  ", 3)
        if gpu_alloc > 0:
            f.try_add(f"gpu-alloc {gpu_alloc / (1024 ** 3):.1f}G", 3)
        else:
            f.try_add("gpu-alloc —", 6)
        if m.get("mem_hist"):
            _cluster_graph(f, m["mem_hist"], lay)
        p.add_row(_finish_row(f, 1))
    return p


def _build_cluster_gpu_section(y, x0, width, views, metrics, selected_idx, name_w, kind, lay=None):
    inner = width - 2
    # Mixed fleets (eGPU on one node) need per-model counts, not
    # "3× <first GPU's name>". Strip marketing prefixes for brevity.
    counts = {}
    for m in metrics:
        for g in (m or {}).get("gpus") or []:
            short = re.sub(r"^NVIDIA\s+(GeForce\s+)?", "", g.get("name", "GPU")) or "GPU"
            counts[short] = counts.get(short, 0) + 1
    total_gpus = sum(counts.values())
    subtitle = (" · ".join(f"{n}× {name}" for name, n in counts.items())
                if counts else "no data")
    p = panels.Panel(y, x0, width, title=[("GPU", 9), (f" {subtitle}", 3)], kind=kind)
    for i, (view, m) in enumerate(zip(views, metrics)):
        gpus = (m or {}).get("gpus") or []
        if not gpus:
            f = _Flow(inner - 2)
            _cluster_row_prefix(f, view, i == selected_idx, name_w)
            f.add("— unreachable" if (view.get("stale") or m is None) else "no GPU", 6)
            p.add_row(_finish_row(f, 1))
            continue
        for gi, g in enumerate(gpus):
            f = _Flow(inner - 2)
            if gi == 0:
                _cluster_row_prefix(f, view, i == selected_idx, name_w)
            else:
                f.add("  ", 3)
                f.add(_cluster_node_field(view.get("name"), name_w) + " ", 6)
            util = g.get("util", 0) or 0
            _cluster_pct_bar(f, util, lay["bar"] if lay else 11)
            f.add("  ", 3)
            # A discrete card allocates from its own VRAM; only a unified SoC
            # draws on the node's system RAM. Printing the node-level figure
            # on every row put an RTX 3090 at "alloc 101.7G" -- a GB10's
            # unified allocation, on a 24 GB card.
            if "(Unified)" in (g.get("name") or ""):
                shown = m.get("gpu_alloc") or 0
            else:
                own = g.get("mem_used")
                shown = own if isinstance(own, (int, float)) and own > 0 else 0
            if shown:
                f.try_add(f"alloc {shown / (1024 ** 3):.1f}G  ", 3)
            else:
                f.try_add("alloc —  ", 6)
            f.try_add(f"{term.fmt_temp(g.get('temp')):<5} ", 3)
            f.try_add(f"{g.get('pwr_str', 'N/A'):<6} ", 8)
            _cluster_graph(f, g.get("history"), lay)
            p.add_row(_finish_row(f, 1))
    return p


def _build_cluster_fabric_section(y, x0, width, views, metrics, selected_idx, name_w, kind):
    inner = width - 2
    rx_sum = sum((m or {}).get("rx_sum", 0) for m in metrics)
    tx_sum = sum((m or {}).get("tx_sum", 0) for m in metrics)
    # Count models across the fleet rather than naming the first node's card:
    # a mixed cluster (ConnectX-4 here, ConnectX-7 there) was labelled with
    # whichever NIC happened to sort first, same trap the GPU title had.
    hw_counts = {}
    for m in metrics:
        # Only the NICs the rows below actually show (nics[:2] per node) --
        # counting every netdev dragged USB and management ports into what is
        # meant to name the fabric.
        for n in ((m or {}).get("nics") or [])[:2]:
            lbl = (n.get("hw_label") or "").strip()
            if lbl:
                hw_counts[lbl] = hw_counts.get(lbl, 0) + 1
    if len(hw_counts) == 1:
        hw = next(iter(hw_counts))
    elif not hw_counts:
        hw = "NIC"
    else:
        short = sorted({re.sub(r"^Mellanox\s+", "", k) for k in hw_counts})
        hw = " + ".join(short) if len(short) <= 3 else f"{len(short)} NIC models"
    subtitle = (f"{hw} · RoCEv2 · Σ ▼ {term.fmt_rate(rx_sum, 'bit')} "
                f"· Σ ▲ {term.fmt_rate(tx_sum, 'bit')}")
    p = panels.Panel(y, x0, width, title=[("FABRIC", 9), (f" {subtitle}", 3)], kind=kind)
    for i, (view, m) in enumerate(zip(views, metrics)):
        f = _Flow(inner - 2)
        _cluster_row_prefix(f, view, i == selected_idx, name_w)
        if view.get("stale") or m is None:
            why = (view.get("error") or "").strip()
            f.add("— unreachable" + (f" ({why})" if why else ""), 6)
            p.add_row(_finish_row(f, 1))
            continue
        nics = m.get("nics") or []
        if not nics:
            f.try_add("no NICs  ", 6)
        for j, n in enumerate(nics[:2]):
            # "p0"/"p1" like the mock -- the netdev name truncates uselessly
            label = f"p{j}"
            if not n.get("up"):
                f.try_add_multi([(f"{label} ", 3), ("— link down  ", 6)])
                continue
            f.try_add_multi([
                (f"{label} ", 3),
                (f"▼{term.fmt_rate(n.get('rx_bps', 0), 'bit')} ", 2),
                (f"▲{term.fmt_rate(n.get('tx_bps', 0), 'bit')}  ", 1),
            ])
        cnp_s, cnp_h = m.get("cnp_sent_sum", 0), m.get("cnp_handled_sum", 0)
        f.try_add(f"cnp {int(cnp_s)}/{int(cnp_h)}  ", 5 if cnp_s > 50 else 3)
        asic = m.get("nic_asic_temp")
        if asic is not None:
            f.try_add(f"asic {term.fmt_temp(asic)}", 5 if asic >= 80 else 3)
        p.add_row(_finish_row(f, 1))
    return p


def _build_cluster_storage_section(y, x0, width, views, metrics, selected_idx, name_w, kind, lay=None):
    inner = width - 2
    total_size = 0
    hottest = None
    for m in metrics:
        if not m:
            continue
        for d in m.get("disks") or []:
            total_size += d.get("size") or 0
            if d.get("temp") is not None and (hottest is None or d["temp"] > hottest):
                hottest = d["temp"]
    subtitle = f"Σ {_cluster_fmt_capacity(total_size)}" if total_size else "no data"
    if hottest is not None:
        subtitle += f" · hottest {term.fmt_temp(hottest)}"
    p = panels.Panel(y, x0, width, title=[("STORAGE", 9), (f" {subtitle}", 3)], kind=kind)
    for i, (view, m) in enumerate(zip(views, metrics)):
        f = _Flow(inner - 2)
        _cluster_row_prefix(f, view, i == selected_idx, name_w)
        if view.get("stale") or m is None:
            why = (view.get("error") or "").strip()
            f.add("— unreachable" + (f" ({why})" if why else ""), 6)
            p.add_row(_finish_row(f, 1))
            continue
        hd = m.get("hottest_disk")
        if hd is None:
            f.add("no disks", 6)
        else:
            f.try_add(f"{(hd.get('name') or '?')[:5]:<5} ", 3)
            used_pct = hd.get("used_pct")
            if used_pct is not None:
                lb, rb = term.bar_brackets()
                bar, slot = term.make_bar(used_pct, lay["bar"] if lay else 10)
                f.try_add_multi([(lb, 10), (bar, slot), (rb, 10), (f" {int(used_pct):>2}%  ", 3)])
            f.try_add(f"R {term.fmt_rate(hd.get('read_bps', 0), 'byte')}  ", 2)
            f.try_add(f"W {term.fmt_rate(hd.get('write_bps', 0), 'byte')}  ", 2)
            if hd.get("temp") is not None:
                f.try_add(term.fmt_temp(hd["temp"]), 3)
        p.add_row(_finish_row(f, 1))
    return p


# --- FLEET mode (>8 nodes) -----------------------------------------------

_FLEET_HEADER_COLS = ["NODE", "CPU", "MEM", "GPU", "ALLOC", "TEMP", "PWR", "FAB ▼", "FAB ▲", "DISK", "HEALTH"]


def _build_fleet_row(f, view, m, is_selected, name_w):
    f.add("▶ " if is_selected else "  ", 9 if is_selected else 3)
    f.add(_cluster_node_field(view.get("name"), name_w) + " ", 6 if (view.get("stale") or m is None) else 8)
    if view.get("stale") or m is None:
        last_ok = view.get("last_ok_ts") or 0
        last = view.get("last_seen_summary")
        if last_ok:
            text = f"— unreachable {time.time() - last_ok:.0f} s"
        else:
            text = "— never reachable"
        text += f" — last seen: {last}" if last else ""
        f.try_add(text, 6)
        return
    bar, slot = term.make_bar(m["cpu_pct"], 5)
    f.add(bar, slot)
    f.add(f" {int(m['cpu_pct']):>3} ", 8)
    bar, slot = term.make_bar(m["mem_pct"], 5)
    f.add(bar, slot)
    f.add(f" {int(m['mem_pct']):>3} ", 8)
    bar, slot = term.make_bar(m["gpu_util_max"], 5)
    f.add(bar, slot)
    f.add(f" {int(m['gpu_util_max']):>3} ", 8)
    alloc = m.get("gpu_alloc") or 0
    f.try_add(f"{alloc / (1024 ** 3):>6.1f}G " if alloc else f"{'—':>7} ", 3 if alloc else 6)
    maxtemp = m.get("max_temp")
    f.try_add(f"{term.fmt_temp(maxtemp) if maxtemp is not None else 'N/A':>5} ", 5 if (maxtemp or 0) >= 80 else 3)
    f.try_add(f"{m.get('gpu_pwr_sum', 0):>4.0f}W ", 8)
    f.try_add(f"{term.fmt_rate(m.get('rx_sum', 0), 'bit'):>9} ", 2)
    f.try_add(f"{term.fmt_rate(m.get('tx_sum', 0), 'bit'):>9} ", 1)
    worst = m.get("worst_disk_pct", 0)
    f.try_add(f"{int(worst):>3}% ", 5 if worst >= 85 else 3)
    alerts = _cluster_alerts(view, m)
    if any(sev == "red" for _, _, sev in alerts):
        health, hslot = "down", 4
    elif alerts:
        health, hslot = _short_health_code(alerts[0][1]), 5
    else:
        health, hslot = "ok", 1
    f.try_add(health, hslot)


def _build_fleet_panel(y, x0, width, views, metrics, selected_idx, name_w, more_above=0, more_below=0):
    inner = width - 2
    rx_tot = sum((m or {}).get("rx_sum", 0) for m in metrics)
    tx_tot = sum((m or {}).get("tx_sum", 0) for m in metrics)
    title_right = [(f"Σ▼ {term.fmt_rate(rx_tot, 'bit')} · Σ▲ {term.fmt_rate(tx_tot, 'bit')}", 3)]
    p = panels.Panel(y, x0, width, title=[("FLEET", 9), (" one row per node — auto-selected above 8 nodes", 3)],
                      title_right=title_right, kind="top")
    f = _Flow(inner - 2)
    f.add("  ", 3)
    for label in _FLEET_HEADER_COLS:
        f.try_add(f"{label:<8}", 6)
    p.add_row(_finish_row(f, 1))
    if more_above:
        p.add_row([(1, f"▲ {more_above} more", 6)])
    for i, (view, m) in enumerate(zip(views, metrics)):
        f = _Flow(inner - 2)
        _build_fleet_row(f, view, m, i == selected_idx, name_w)
        p.add_row(_finish_row(f, 1))
    if more_below:
        p.add_row([(1, f"▼ {more_below} more", 6)])
    return p


def _build_alerts_panel(y, x0, width, views, metrics, kind):
    inner = width - 2
    all_alerts = []
    for view, m in zip(views, metrics):
        all_alerts.extend(_cluster_alerts(view, m))
    # Reds (unreachable) first -- the highest-severity alerts must survive
    # width/height truncation, not whichever node happened to sort first by
    # iteration order.
    all_alerts.sort(key=lambda a: 0 if a[2] == "red" else 1)
    p = panels.Panel(y, x0, width, title=[("ALERTS", 9)], kind=kind)
    if not all_alerts:
        p.add_row([(1, "no active alerts", 1)])
        return p
    # Wrap onto additional rows rather than silently dropping alerts once
    # one row's width runs out -- an alert list, unlike a decorative status
    # line, must never just disappear because of a narrow terminal.
    f = _Flow(inner - 2)
    started = False
    for name, reason, sev in all_alerts:
        slot = 4 if sev == "red" else 5
        item = [("▪ ", slot), (f"{name} {reason}", 3)]
        parts = ([("  ·  ", 6)] + item) if started else item
        if f.try_add_multi(parts):
            started = True
            continue
        if started:
            p.add_row(_finish_row(f, 1))
        f = _Flow(inner - 2)
        started = f.try_add_multi(item)
    if started:
        p.add_row(_finish_row(f, 1))
    return p


def _build_topology_panel(y, x0, width, topo, n_nodes):
    """The TOPOLOGY panel around topomap.render()'s rows (drawn 1 cell in
    from each border, so the boxes never touch the frame)."""
    bits = [f"{n_nodes} nodes"]
    if topo["links"]:
        bits.append(f"{topo['links']} link{'s' if topo['links'] != 1 else ''}")
    if topo["buses"]:
        bits.append(f"{topo['buses']} switch{'es' if topo['buses'] != 1 else ''}")
    if topo["rails"]:
        bits.append(f"{topo['rails']} rail{'s' if topo['rails'] != 1 else ''}")
    if not topo["links"] and not topo["buses"]:
        bits.append("no shared fabric network found")
    p = panels.Panel(y, x0, width, title=[("TOPOLOGY", 9), (" " + " · ".join(bits), 3)],
                     title_right=[(f"Σ fabric {term.fmt_rate(topo['bps'], 'bit')}", 3)], kind="top")
    for runs in topo["rows"]:
        p.add_row([(1, runs[0][0], runs[0][1])] + [(None, t, sl) for t, sl in runs[1:]])
    return p


# --- Assembly --------------------------------------------------------

def build_page3(cluster_ctx, tier, width, x0=0, height=None, has_perf=False):
    """Builds page 3 (--cluster only): header (3-tab, cluster summary),
    then either the SECTIONS compound frame (<=8 nodes, one row per node
    per CPU/MEMORY/GPU/FABRIC/STORAGE panel) or the FLEET matrix + ALERTS
    (>8 nodes), then the footer. `cluster_ctx` = {"name", "views", "rate",
    "ui"} -- see the module-level comment above this section. `has_perf`
    (Phase 7) is True once the cluster has >=2 members, adding the 4th
    "PERF" tab to the header (page 4 itself is only reachable that way)."""
    out = []
    name = cluster_ctx.get("name", "cluster")
    views = list(cluster_ctx.get("views") or [])
    ui = cluster_ctx.get("ui")
    rate = cluster_ctx.get("rate", 1.0)

    metrics = [_cluster_metrics(v.get("sample")) for v in views]
    pairs = list(zip(views, metrics))

    if ui and ui.filter_alerts:
        pairs = [(v, m) for v, m in pairs if _cluster_alerts(v, m)]
    if ui and ui.sort_mode != "name":
        pairs = sorted(pairs, key=_cluster_sort_key(ui.sort_mode))
    elif ui:
        pairs = sorted(pairs, key=_cluster_sort_key("name"))
    views = [p[0] for p in pairs]
    metrics = [p[1] for p in pairs]
    n = len(views)

    total_nodes = len(cluster_ctx.get("views") or [])
    up = sum(1 for v in (cluster_ctx.get("views") or []) if not v.get("stale") and v.get("sample"))
    wall_w, total_w, has_rail = None, 0.0, False
    for m in metrics:
        if not m:
            continue
        for r in m.get("power_rails") or []:
            if r.get("label") == "dc_input":
                has_rail = True
                total_w += r.get("watts") or 0
    if has_rail:
        wall_w = total_w

    summary = f"{total_nodes} node{'s' if total_nodes != 1 else ''} · {'all up' if up == total_nodes else f'{up} up'}"
    if wall_w is not None:
        summary += f"  {_hdr_glyphs()[2]}  Σ wall {wall_w / 1000:.2f} kW" if wall_w >= 1000 else f"  {_hdr_glyphs()[2]}  Σ wall {wall_w:.0f} W"
    summary = f"{name} — {summary}"

    out.append(_build_header(x0, width, tier, page=3, cluster_tabs=True, cluster_summary=summary, has_perf=has_perf))
    y = 1

    if ui:
        ui.selected = 0 if n == 0 else max(0, min(n - 1, ui.selected))
        ui.last_n = n
        ui.selected_name = views[ui.selected].get("name") if n else None
    selected_idx = ui.selected if ui else -1

    # Size the node column to the longest name actually present rather than a
    # flat 10, which sliced a 14-char hostname down to "resa-ai-no".
    _longest = max((len(v.get("name") or "?") for v in views), default=0)
    name_w = max(8, min(_longest, 10 if tier == "compact" else 18))
    fleet_mode = n > 8
    avail_rows = max(4, (height or 30) - y - 3)

    if not fleet_mode:
        lay = _cluster_layout(width, metrics, name_w)
        builders = (_build_cluster_cpu_section, _build_cluster_mem_section, _build_cluster_gpu_section,
                    _build_cluster_fabric_section, _build_cluster_storage_section)

        def sections(y0, first_kind):
            built, yy = [], y0
            for k, build in enumerate(builders):
                kw = {"lay": lay} if build is not _build_cluster_fabric_section else {}
                sp = build(yy, x0, width, views, metrics, selected_idx, name_w,
                           kind=first_kind if k == 0 else "mid", **kw)
                built.append(sp)
                yy += sp.total_height
            return built, yy

        # TOPOLOGY map from spare height: whatever the sections, their
        # closing border and the footer leave (they always come first)
        _, sec_end = sections(y, "top")
        spare = (height or 0) - (sec_end + 2)
        map_p = None
        if spare >= 6:
            pairs_by_name = sorted(zip(views, metrics), key=lambda vm: vm[0].get("name") or "")
            cache = getattr(ui, "topo_cache", None) if ui else None
            topo = topomap.render(pairs_by_name, width - 4, spare - 1,
                                  selected_name=(ui.selected_name if ui else None), cache=cache)
            if topo:
                map_p = _build_topology_panel(y, x0, width, topo, len(views))
        if map_p is not None:
            out.append(map_p); y += map_p.total_height
        built, y = sections(y, "top" if map_p is None else "mid")
        out.extend(built)
        y += 1
    else:
        visible = max(2, avail_rows - 4)  # reserve a few rows for ALERTS
        start = 0
        if ui:
            ui.clamp_scroll(n, visible)
            start = ui.scroll
        end = min(n, start + visible)
        sel_in_window = (selected_idx - start) if start <= selected_idx < end else -1
        fleet_p = _build_fleet_panel(y, x0, width, views[start:end], metrics[start:end], sel_in_window,
                                      name_w, more_above=start, more_below=n - end)
        out.append(fleet_p); y += fleet_p.total_height
        alerts_p = _build_alerts_panel(y, x0, width, views, metrics, kind="mid")
        out.append(alerts_p); y += alerts_p.total_height + 1

    page_nums = "1 2 3 4 5" if has_perf else "1 2 3 5"
    if fleet_mode:
        keys = " q quit · ↑↓ select · enter node detail · a alerts only · o sort col · c colors"
    else:
        keys = f" q quit · ↑↓ select · enter drill into node · {page_nums} page · c colors"
    info = f"agents {up}/{total_nodes} · poll {rate:g} s" + ("" if fleet_mode else " · spark-smi --serve")
    p = panels.Panel(y, x0, width, kind="plain")
    p.rows.append(([(keys, 9)], [(info, 6)]))
    out.append(p)
    return out


# =========================================================================
# Page 4 (Phase 7, --cluster with >=2 members only): FABRIC TEST control +
# BANDWIDTH chart + RAILS results + all-pairs MATRIX.
# =========================================================================
# `fab_ctx` = {"name", "views" (cluster.ClusterAggregator.get_views(), same
# shape build_page3 reads), "rate", "fab" (fabtest.FabTestUI.render_ctx(),
# or None in snapshot mode -- no live session at all), "engine_status"
# (fabtest.FabricEngine.status(), or None), "last_run"
# (fabtest.load_last_run(), or None), "matrix" (fabtest.load_matrix(),
# {(src, dst): {"gbps", "lat_us"}})}. Exactly like build_page3's
# cluster_ctx, pages.py never imports fabtest.py itself -- app.py gathers
# everything into plain dicts first (the same boundary knobs.py/cluster.py
# already keep with this module).

_RAIL_SLOTS = (1, 2, 8, 5)   # per-rail chart/legend color, in rail order
_RAIL_LINK_GBPS_FALLBACK = 200.0   # only when no rail reports a link speed


def _rail_link_gbps(fab_ctx):
    """Per-rail link speed (Gb/s) for the bandwidth chart's y-scale and the
    'of {cap}' figures, taken from the fastest rail any member actually
    reports (nic_pf rows' speed_mbit, straight from sysfs). This used to be
    a hardcoded 200.0 -- correct for ConnectX-7 and wrong for every other
    NIC, which silently mis-scaled the chart on 100G or 400G fabric.

    Falls back to 200.0 only when nothing reports a speed at all (every rail
    down, or a remote node old enough not to send the field)."""
    best = 0.0
    try:
        for view in (fab_ctx.get("views") or []):
            sample = (view or {}).get("sample") or {}
            for row in (sample.get("nic_pf") or []):
                try:
                    mbit = float(row.get("speed_mbit") or 0)
                except (TypeError, ValueError):
                    continue
                best = max(best, mbit / 1000.0)
    except Exception:
        pass
    return best if best > 0 else _RAIL_LINK_GBPS_FALLBACK


def _chart_y_labels(height_cells, y_max):
    """{row_index: 'NNN'} for a handful of evenly-spaced rows (top = y_max,
    bottom = 0) -- the sparse "200 ┤ / │ / 100 ┤ / │ / 0 ┤" tick look from
    mock_page4.txt's chart block, generalized to any height/y_max rather
    than the mock's hardcoded 10-row/200 case."""
    if height_cells <= 1:
        return {0: f"{int(y_max):>3}"}
    steps = min(5, height_cells)
    labels = {}
    for k in range(steps):
        row = round(k * (height_cells - 1) / (steps - 1)) if steps > 1 else 0
        # label value from the STEP fraction, not the row position: rows
        # quantize (10 rows -> 156/111/44-style junk); quarters of y_max
        # give the mock's clean 200/150/100/50/0 ladder
        val = y_max * (1 - (k / (steps - 1) if steps > 1 else 0))
        labels[row] = f"{int(round(val)):>3}"
    return labels


def _active_pair_series(fab_ctx):
    """(src, dst, {rail_idx: [gbps, ...]}) for whatever's live right now (a
    running pair/sweep), else the last completed mode=="pair" run, else
    (None, None, {}) when there's neither -- this is what feeds the
    BANDWIDTH chart. Burst mode has no single "current pair" (all ring
    pairs run concurrently -- see _build_bandwidth_panel), so a running
    burst also falls through to the last completed pair here."""
    status = fab_ctx.get("engine_status") or {}
    if status.get("running") and status.get("series"):
        prog = status.get("progress") or {}
        if prog.get("mode") != "burst":
            return prog.get("src"), prog.get("dst"), status.get("series")
    last = fab_ctx.get("last_run")
    if last and last.get("mode") == "pair" and last.get("rails"):
        series = {i: (r.get("series") or []) for i, r in enumerate(last.get("rails") or [])}
        return last.get("src"), last.get("dst"), series
    return None, None, {}


def _build_fabtest_panel(y, x0, width, fab_ctx):
    fab = fab_ctx.get("fab")
    status = fab_ctx.get("engine_status") or {}
    la, ra = term.knob_arrows()
    mode = fab.get("mode") if fab else (fab_ctx.get("last_run") or {}).get("mode", "sweep")
    duration = fab.get("duration") if fab else (fab_ctx.get("last_run") or {}).get("duration", 30)
    mode_label = {"pair": "one pair", "sweep": "all-pairs sweep", "burst": "all-nodes burst"}.get(mode, mode)
    title = [("FABRIC TEST", 9), ("   ", 3), ("mode", 3), (" ", 3),
             (f"[{la} {mode_label} {ra}]", 9), (f"  · {duration} s/pair", 3)]

    if fab and fab.get("confirming"):
        right = [((fab.get("confirm_text") or "run? y/N")[:width - 30], 5)]
    elif status.get("running"):
        prog = status.get("progress") or {}
        n, i = prog.get("n") or 0, prog.get("i") or 0
        if mode == "sweep" and n:
            right = [(f"RUNNING · pair {i + 1} of {n}", 5)]
        else:
            right = [(f"RUNNING · {status.get('elapsed', 0):.0f} s elapsed", 5)]
    elif fab is None:
        right = [("snapshot: last results only", 6)]
    else:
        right = [("idle · press space to arm a test", 6)]

    p = panels.Panel(y, x0, width, title=title, title_right=right, kind="top")
    inner = width - 2

    f = _Flow(inner - 1)
    if status.get("running"):
        prog = status.get("progress") or {}
        src, dst = prog.get("src"), prog.get("dst")
        if src and dst:
            f.try_add_multi([("now testing", 3), (f" {src} → {dst}", 8)])
        elif prog.get("mode") == "burst":
            f.try_add_multi([("now testing", 3), ("  every node → its ring successor", 8)])
        n_rails = len(status.get("series") or {}) or 4
        f.try_add_multi([(f"  · {n_rails} rails in parallel · {status.get('elapsed', 0):.0f} s elapsed", 6)])
    else:
        last = fab_ctx.get("last_run")
        if last:
            lm = last.get("mode", "?")
            bits = f"last run: {lm}"
            if last.get("src") and last.get("dst"):
                bits += f" {last['src']} → {last['dst']}"
            if last.get("notes"):
                bits += f" — {last['notes']}"
            f.try_add_multi([(bits, 3)])
        else:
            f.try_add_multi([("no fabric test results recorded yet — press space to arm one", 6)])

    right2 = []
    last = fab_ctx.get("last_run")
    if last and last.get("mode") == "burst" and last.get("aggregate_gbps") is not None:
        gbps = last["aggregate_gbps"]
        txt = f"{gbps / 1000.0:.2f} Tb/s" if gbps >= 1000 else f"{gbps:.0f} Gb/s"
        right2 = [(f"last full burst: Σ {txt}", 6)]
    segs = list(f.segs)
    if segs:
        segs[0] = (2, segs[0][1], segs[0][2])
    p.add_row(segs, right=right2)
    return p


def _build_bandwidth_panel(y, x0, width, fab_ctx, chart_h, show_legend):
    inner = width - 2
    src, dst, series = _active_pair_series(fab_ctx)
    last = fab_ctx.get("last_run")
    rails = (last.get("rails") if last and last.get("mode") == "pair" else None) or []
    # r["dev"] is already the final short label fabtest.run_pair resolved
    # (assign_rail_labels) when it recorded this result -- nothing left to
    # derive here.
    labels = [r.get("dev", "?") for r in rails] if rails else [f"rail{i}" for i in sorted(series)]

    mid = f"{src} → {dst} · per rail" if src else "no active/recent pair · per rail"
    cur_vals = [vals[-1] for vals in series.values() if vals]
    cur_vals = [v for v in cur_vals if isinstance(v, (int, float))]
    total = sum(cur_vals) if cur_vals else None
    link_gbps = _rail_link_gbps(fab_ctx)
    cap = link_gbps * max(1, len(series) or len(rails) or 1)
    right = []
    if total is not None:
        right = [(f"Σ {total:.0f} Gb/s", 8), (f" of {cap:.0f}", 6)]

    p = panels.Panel(y, x0, width, title=[("BANDWIDTH", 9)],
                      title_right=[(mid, 3)] + ([(" — ", 6)] + right if right else []), kind="mid")

    if not series or chart_h <= 0:
        p.add_row([(2, "no bandwidth samples for this pair yet", 6)])
        return p

    chart_w = max(10, inner - 8)
    chart_series = [(series[i], _RAIL_SLOTS[idx % len(_RAIL_SLOTS)]) for idx, i in enumerate(sorted(series))]
    rows = term.braille_chart(chart_series, chart_w, chart_h, link_gbps)

    if rows is None:
        # blocks/ascii tier: no braille glyphs in that console font -- fall
        # back to one bar row per rail instead of a chart.
        for idx, i in enumerate(sorted(series)):
            cur = series[i][-1] if series[i] else 0.0
            lb, rb = term.bar_brackets()
            bar, _slot = term.make_bar(min(100.0, cur / link_gbps * 100.0), min(30, chart_w))
            slot = _RAIL_SLOTS[idx % len(_RAIL_SLOTS)]
            label = labels[idx] if idx < len(labels) else f"rail{i}"
            p.add_row([(2, f"{label:<6}", slot), (9, lb, 10), (None, bar, slot), (None, rb, 10),
                       (None, f" {cur:.0f} Gb/s", 3)])
        return p

    y_labels = _chart_y_labels(chart_h, link_gbps)
    for row_idx, run in enumerate(rows):
        lbl = y_labels.get(row_idx)
        tick, axis = ("+", "|") if term.BAR_STYLE == "ascii" else ("\u2524", "\u2502")
        prefix = f"{lbl} {tick}" if lbl else f"    {axis}"
        segs = [(2, prefix, 6)]
        col = 2 + len(prefix)
        for text, slot in run:
            segs.append((col, text, slot))
            col += len(text)
        p.add_row(segs)

    p.add_row([(2, "    └" + "─" * chart_w, 10)])

    dur = (last or {}).get("duration") or (fab_ctx.get("fab") or {}).get("duration") or 30
    tick_row = [(2, "     0s", 6)]
    for frac in (1 / 3, 2 / 3, 1.0):
        text = f"{int(round(frac * dur))}s"
        tcol = 2 + 5 + int(frac * chart_w)
        # keep the last tick fully inside the frame instead of clipping it
        tcol = min(tcol, inner - len(text) - 1)
        tick_row.append((tcol, text, 6))
    p.add_row(tick_row)

    if show_legend:
        best = max(cur_vals) if cur_vals else None
        legend = _Flow(inner - 1)
        for idx, i in enumerate(sorted(series)):
            cur = series[i][-1] if series[i] else 0.0
            slot = _RAIL_SLOTS[idx % len(_RAIL_SLOTS)]
            label = labels[idx] if idx < len(labels) else f"rail{i}"
            sagging = best is not None and best > 0 and cur < 0.85 * best
            val_slot = 5 if sagging else 8
            suffix = " ▼" if sagging else ""
            lead = "█ " if idx == 0 else "   █ "
            legend.try_add_multi([(lead, slot), (f"{label} ", 3), (f"{cur:.0f}{suffix}", val_slot)])
        lsegs = list(legend.segs)
        if lsegs:
            lsegs[0] = (2, lsegs[0][1], lsegs[0][2])
        p.add_row(lsegs)
    return p


def _build_rails_panel(y, x0, width, fab_ctx):
    inner = width - 2
    link_gbps = _rail_link_gbps(fab_ctx)
    last = fab_ctx.get("last_run")
    rails = (last.get("rails") if last and last.get("mode") == "pair" else None) or []
    p = panels.Panel(y, x0, width, title=[("RAILS", 9)],
                      title_right=[("result · HCA asic temp during run (start → now)", 3)], kind="mid")
    if not rails:
        p.add_row([(2, "no completed rail results yet", 6)])
        return p

    valid = [r.get("gbps") for r in rails if r.get("gbps") is not None]
    best = max(valid) if valid else None
    for idx, r in enumerate(rails):
        label = r.get("dev") or f"rail{idx}"
        gbps, notes = r.get("gbps"), r.get("notes")
        if notes or gbps is None:
            p.add_row([(2, f"{label:<6}", 3), (10, notes or "no data", 6)])
            continue
        asic_s, asic_m = r.get("asic_start"), r.get("asic_max")
        delta = (asic_m - asic_s) if (asic_s is not None and asic_m is not None) else None
        throttling = bool(delta is not None and delta >= 20 and best and gbps < 0.85 * best)
        pct = max(0.0, min(100.0, (gbps / link_gbps) * 100.0))
        lb, rb = term.bar_brackets()
        bar, bar_slot = term.make_bar(pct, 20)
        if throttling:
            bar_slot = 5
        f = _Flow(inner - 1)
        f.add(f"{label:<6}", 3)
        f.add(lb, 10); f.add(bar, bar_slot); f.add(rb, 10)
        f.add(f" {gbps:.0f} Gb/s", 8)
        if asic_s is not None and asic_m is not None:
            f.try_add_multi([(f"   asic {term.fmt_temp(asic_s)}→{term.fmt_temp(asic_m)}", 5 if throttling else 3)])
            if delta is not None:
                f.try_add_multi([(f"  Δ+{delta:.0f}", 6)])
        status_txt, status_slot = ("⚠ throttling — check airflow", 5) if throttling else ("ok", 1)
        f.try_add_multi([("   ", 3), (status_txt, status_slot)])
        segs = list(f.segs)
        segs[0] = (2, segs[0][1], segs[0][2])
        p.add_row(segs)
    return p


def _build_matrix_panel(y, x0, width, fab_ctx, worst_only=False):
    inner = width - 2
    views = fab_ctx.get("views") or []
    names = [v.get("name") for v in views if v.get("name")]
    matrix = fab_ctx.get("matrix") or {}
    status = fab_ctx.get("engine_status") or {}

    running_pairs = set()
    if status.get("running"):
        prog = status.get("progress") or {}
        if prog.get("mode") == "burst" and names:
            n = len(names)
            running_pairs = {(names[i], names[(i + 1) % n]) for i in range(n)}
        elif prog.get("src") and prog.get("dst"):
            running_pairs = {(prog["src"], prog["dst"])}

    gbps_vals = sorted(m["gbps"] for m in matrix.values() if m.get("gbps") is not None)
    lat_vals = sorted(m["lat_us"] for m in matrix.values() if m.get("lat_us") is not None)
    med_gbps = gbps_vals[len(gbps_vals) // 2] if gbps_vals else None
    med_lat = lat_vals[len(lat_vals) // 2] if lat_vals else None

    last = fab_ctx.get("last_run")
    right = []
    if last and last.get("mode") == "burst" and last.get("aggregate_gbps") is not None:
        gbps = last["aggregate_gbps"]
        txt = f"{gbps / 1000.0:.2f} Tb/s" if gbps >= 1000 else f"{gbps:.0f} Gb/s"
        right = [(f"burst aggregate Σ {txt}", 3)]
    p = panels.Panel(y, x0, width, title=[("MATRIX", 9)],
                      title_right=[("all-pairs · best rail Gb/s / RTT µs", 3)] + ([(" — ", 6)] + right if right else []),
                      kind="mid")

    if len(names) < 2:
        p.add_row([(2, "needs 2+ cluster members", 6)])
        return p

    anomalies = []

    def _cell(s, d):
        if s == d:
            return "—", 6
        if (s, d) in running_pairs:
            return "▶ running…", 9
        m = matrix.get((s, d))
        if not m or m.get("gbps") is None:
            return "no data", 6
        gbps, lat = m["gbps"], m.get("lat_us")
        txt = f"{gbps:.0f} / {lat:.1f}" if lat is not None else f"{gbps:.0f}"
        anomaly = bool((med_gbps and gbps < 0.8 * med_gbps) or (med_lat and lat is not None and lat > 1.5 * med_lat))
        if anomaly:
            anomalies.append((s, d, gbps, lat))
            return txt + " ⚠", 5
        return txt, 3

    if worst_only:
        pairs = [(s, d) for s in names for d in names if s != d and matrix.get((s, d))]
        pairs.sort(key=lambda sd: matrix[sd].get("gbps") if matrix[sd].get("gbps") is not None else 1e18)
        if not pairs:
            p.add_row([(2, "no pairwise results recorded yet", 6)])
            return p
        for s, d in pairs[:max(3, width // 30)]:
            txt, slot = _cell(s, d)
            f = _Flow(inner - 1)
            f.add(f"{s} → {d}", 8)
            f.try_add_multi([("   ", 3), (txt, slot)])
            segs = list(f.segs)
            segs[0] = (2, segs[0][1], segs[0][2])
            p.add_row(segs)
        return p

    name_w = 10
    col_w = max(10, min(20, (inner - name_w) // max(1, len(names))))
    header = _Flow(inner - 1 - name_w)
    for n in names:
        header.try_add(f"→ {n[:col_w - 3]:<{col_w - 3}} ", 6)
    hsegs = list(header.segs)
    if hsegs:
        hsegs[0] = (name_w + 2, hsegs[0][1], hsegs[0][2])
    p.add_row(hsegs)

    for s in names:
        segs = [(2, f"{s[:name_w - 1]:<{name_w}}", 8)]
        col = name_w + 2
        for d in names:
            txt, slot = _cell(s, d)
            segs.append((col, f"{txt:<{col_w - 3}}", slot))
            col += col_w
        p.add_row(segs)

    if anomalies:
        s, d, gbps, lat = anomalies[0]
        pct_below = int(round((1 - gbps / med_gbps) * 100)) if med_gbps else 0
        excess = (lat - med_lat) if (lat is not None and med_lat is not None) else None
        note = f"⚠ {s} ↔ {d}: {pct_below}% below fleet median"
        if excess and excess > 0:
            note += f" + {excess:.1f} µs excess RTT"
        note += " — inspect cable/transceiver"
        p.add_row([(2, note, 5)])
    return p


def build_page4(fab_ctx, tier, width, x0=0, height=None):
    """Builds page 4: header (4-tab), FABRIC TEST control panel, BANDWIDTH
    chart, RAILS results, all-pairs MATRIX, footer. Requires >=2 cluster
    members to do anything useful (a single node has no peer to test
    against) -- with fewer than that, renders just the header + a short
    explanatory panel instead, which is what makes BOTH `--page 4` with no
    --cluster at all AND `--cluster` with only one host degrade to a
    message rather than a crash or a blank page.

    Height degrade order (spec): chart shrinks 10 rows -> 6, then the
    BANDWIDTH legend row drops, then MATRIX collapses to a worst-pairs-only
    list instead of the full grid."""
    out = []
    name = fab_ctx.get("name", "cluster")
    views = fab_ctx.get("views") or []
    n_members = len(views)

    out.append(_build_header(x0, width, tier, page=4, cluster_tabs=True,
                              cluster_summary=f"{name} — fabric validation", has_perf=True))
    y = 1

    if n_members < 2:
        p = panels.Panel(y, x0, width, title=[("FABRIC TEST", 9)], kind="top")
        p.add_row([(2, "page 4 needs --cluster with 2 or more members to run RDMA stress tests.", 6)])
        p.add_row([(2, "reachable members right now: " + (", ".join(v.get("name", "?") for v in views) or "none"), 6)])
        out.append(p)
        y += p.total_height + 1
        fp = panels.Panel(y, x0, width, kind="plain")
        fp.rows.append(([(" q quit · 1 2 3 4 5 page", 9)], []))
        out.append(fp)
        return out

    chart_h = 10
    show_legend = True
    matrix_worst_only = False
    if height:
        est = 1 + 3 + 2 + chart_h + 3 + (2 + len(views)) + 4
        if est > height:
            chart_h = 6
            est -= 4
        if est > height:
            show_legend = False
            est -= 1
        if est > height:
            matrix_worst_only = True

    ft_panel = _build_fabtest_panel(y, x0, width, fab_ctx)
    out.append(ft_panel); y += ft_panel.total_height

    bw_panel = _build_bandwidth_panel(y, x0, width, fab_ctx, chart_h, show_legend)
    out.append(bw_panel); y += bw_panel.total_height

    rails_panel = _build_rails_panel(y, x0, width, fab_ctx)
    out.append(rails_panel); y += rails_panel.total_height

    matrix_panel = _build_matrix_panel(y, x0, width, fab_ctx, worst_only=matrix_worst_only)
    out.append(matrix_panel); y += matrix_panel.total_height + 1

    keys = " space start/stop · m mode · d duration · ↑↓ pair · 1-5 page"
    fab = fab_ctx.get("fab")
    if fab and fab.get("toast"):
        text, slot = fab["toast"]
        fp = panels.Panel(y, x0, width, kind="plain")
        fp.rows.append(([(f" {text}", slot)], []))
    else:
        fp = panels.Panel(y, x0, width, kind="plain")
        fp.rows.append(([(keys, 9)], [("saturates the fabric — confirm-gated", 6)]))
    out.append(fp)
    return out


# =========================================================================
# Page 5: GPU TASKS -- one tree root per GPU, session groups below it,
# processes below those (tasks.flatten() decides the order; this only draws
# what it's handed). Local-only: a --cluster drilldown never reaches here.
# =========================================================================

_KIND_SLOT = {"orphan": 5, "tmux": 2, "screen": 2, "zellij": 2, "docker": 2,
              "systemd": 2, "ssh": 3, "tty": 3, "none": 6}


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
    below a GiB, "4.1G" above -- _short_mem's always-G form would print a
    12 MiB worker as "0.0G"."""
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


def _task_right_cols(tier):
    """(label, width) column set for the right-flushed stats block."""
    if tier == "compact":
        return [("GPU MEM", 8), ("AGE", 7)]
    if tier == "standard":
        return [("CPU", 5), ("RSS", 7), ("GPU MEM", 8), ("AGE", 7)]
    return [("USER", 9), ("CPU", 5), ("RSS", 7), ("GPU MEM", 8), ("AGE", 7)]


def _task_row(row, tier, glyphs, selected):
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
        right = [("  ·  ".join(bits), 3)]
        return left, right
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
    cols = _task_right_cols(tier)
    vals = {
        "USER": (p.get("user") or "?")[:8],
        "CPU": f"{p.get('cpu') or 0:.0f}%",
        "RSS": _task_mem(p.get("rss") or 0) if p.get("rss") else "-",
        "GPU MEM": _task_mem(p["gpu_mem"]) if p.get("gpu_mem") else "-",
        "AGE": _fmt_age(p.get("age")),
    }
    right = []
    for label, w in cols:
        slot = 2 if label in ("CPU", "GPU MEM") else 3
        right.append((f"{vals[label]:>{w}}", slot))
    return left, right


def _build_tasks_panel(y, x0, width, tree, rows, tier, ui, height_avail):
    inner = width - 2
    glyphs = _tree_glyphs()
    n_gpus = len(tree.get("gpus") or [])
    n_procs = tree.get("n_procs") or 0
    n_orph = tree.get("n_orphans") or 0
    title = [("GPU TASKS", 9), (f"  {n_gpus} GPU" + ("" if n_gpus == 1 else "s"), 3),
             (f" · {n_procs} process" + ("" if n_procs == 1 else "es"), 3)]
    if n_orph:
        title.append((f" · {n_orph} orphan" + ("" if n_orph == 1 else "s"), 5))
    src = tree.get("source") or "none"
    tr = [(f"source {src}", 6)]
    if tree.get("caps", {}).get("fd_scan"):
        tr.append(("  ·  a device holders " + ("on" if tree.get("holders") else "off"), 6))
    p = panels.Panel(y, x0, width, title=title, title_right=tr, kind="top")

    cols = _task_right_cols(tier)
    hdr_right = [(f"{label:>{w}}", 6) for label, w in cols]
    p.add_row([(1, f"{'':2}{'PID / COMMAND':<}", 6)], right=hdr_right)

    if not rows:
        if not n_gpus:
            p.add_row([(2, "no NVIDIA GPU detected -- nothing to list", 6)])
        else:
            p.add_row([(2, "no GPU processes", 6)])
        return p

    # Scroll window around the selection: header row + optional more-above/
    # below markers all fit inside height_avail.
    selected = ui.get("selected", 0) if ui else -1
    avail = max(3, height_avail)
    start = 0
    if len(rows) > avail:
        # reserve one line per marker as needed
        win = avail - 2
        if selected >= 0:
            start = max(0, min(selected - win // 2, len(rows) - win))
        else:
            start = 0
        end = min(len(rows), start + win)
        if start == 0:
            end = min(len(rows), win + 1)
        if end == len(rows):
            start = max(0, end - (win + (1 if start == 0 else 0)))
    else:
        end = len(rows)
    more_above = start
    more_below = len(rows) - end
    if more_above:
        p.add_row([(1, f"▲ {more_above} more" if term.BAR_STYLE != "ascii" else f"^ {more_above} more", 6)])
    right_w = sum(w for _, w in cols) + len(cols) - 1
    for i in range(start, end):
        row = rows[i]
        left, right = _task_row(row, tier, glyphs, i == selected)
        if row["type"] == "proc":
            # fixed-width stats block, columns separated by one space
            right_runs = []
            for j, (text, slot) in enumerate(right):
                right_runs.append((text, slot))
                if j < len(right) - 1:
                    right_runs.append((" ", 3))
            right = right_runs
            rw = right_w
        else:
            rw = sum(len(t) for t, _ in right)
        avail_left = inner - 1 - rw - 2
        segs = [(1, t, s) for t, s in _clip_runs(left, avail_left)]
        if segs:
            segs = [(1, segs[0][1], segs[0][2])] + [(None, t, s) for _, t, s in segs[1:]]
        p.add_row(segs, right=right)
    if more_below:
        p.add_row([(1, f"▼ {more_below} more" if term.BAR_STYLE != "ascii" else f"v {more_below} more", 6)])
    return p


def _build_tasks_footer(x0, width, y, sample, ui, theme_toast=None):
    """Confirm prompt / toast take the whole line (same precedence
    _build_footer gives knobs); otherwise the page-5 key ladder + info."""
    if theme_toast and not (ui and (ui.get("confirming") or ui.get("toast"))):
        p = panels.Panel(y, x0, width, kind="plain")
        p.rows.append(([(f" {theme_toast[0]}", theme_toast[1])], []))
        return p
    if ui and (ui.get("confirming") or ui.get("toast")):
        return _build_footer(x0, width, y, sample.get("driver"), sample.get("cuda"), sample.get("rate", 1.0),
                             (sample.get("caps") or {}).get("has_nvml", False), knob_ui=ui)
    la, ra = term.knob_arrows()
    ud = "↑↓" if term.BAR_STYLE != "ascii" else "up/dn"
    if ui is None:
        variants = [" q quit · 1 2 5 page · snapshot is read-only — run with -l to manage tasks",
                    " q quit · 1 2 5 page · read-only snapshot", " 1 2 5 page"]
    else:
        variants = [f" q quit · 1 2 5 page · {ud} select · Enter open · e end · k kill · a device holders · ? help",
                    f" q quit · 1 2 5 page · {ud} select · Enter open · e end · k kill · a holders",
                    f" q quit · 1 2 5 page · {ud} · Enter · e · k · a", " 1 2 5 page"]
    src = "NVML" if (sample.get("caps") or {}).get("has_nvml") else "CLI"
    info_parts = [f"v{VERSION}", src]
    driver, cuda = sample.get("driver"), sample.get("cuda")
    if driver and str(driver) != "Unknown":
        info_parts.append(f"driver {driver}")
    if cuda and str(cuda) != "Unknown":
        info_parts.append(f"CUDA {cuda}")
    info_parts.append(f"{sample.get('rate', 1.0):g}s")
    info = " · ".join(info_parts)
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
    p.rows.append(([(keys, 9)], [(info, 2)] if show_info else []))
    return p


def build_page5(sample, tree, rows, tier, width, x0=0, height=None, ui=None, theme_toast=None,
                cluster_tabs=False, has_perf=False):
    """Builds page 5: header (5-tab), the GPU TASKS tree panel, footer.
    `tree`/`rows` come from tasks.TaskCollector.sample() / tasks.flatten();
    `ui` is tasks.TaskUI.render_ctx() in live mode, None in snapshot mode
    (no selection marker, read-only footer). Height is honoured by
    scrolling the tree around the selection, never by dropping columns."""
    out = [_build_header(x0, width, tier, page=5, cluster_tabs=cluster_tabs, has_perf=has_perf)]
    y = 1
    # header(1) + title(1) + column header(1) + bottom border(1) + gap(1) + footer(1)
    avail = max(3, (height or 40) - 6)
    panel = _build_tasks_panel(y, x0, width, tree or {}, rows or [], tier, ui, avail)
    out.append(panel)
    y += panel.total_height + 1
    if height:
        y = min(y, height - 1)
    out.append(_build_tasks_footer(x0, width, y, sample or {}, ui, theme_toast=theme_toast))
    return out
