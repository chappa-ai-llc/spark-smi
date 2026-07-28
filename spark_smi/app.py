"""Entry point: hand-rolled CLI parsing (no argparse, matching the pre-2.0
convention), the snapshot render path, and the curses live main loop.
Curses-specific calls are confined to this module -- everything else draws
through the duck-typed screen interface shared by VirtualCurses and real
curses windows.
"""
import os
import sys
import time
from collections import deque

from . import cluster
from . import collectors
from . import fabtest
from . import knobs
from . import pages
from . import panels
from . import term

try:
    from . import VERSION
except ImportError:
    VERSION = "4.0.0a1"
DEFAULT_REFRESH_RATE = 1.0
HIST_LEN = 60

HELP_TEXT = f"""spark-smi {VERSION} -- terminal system monitor for NVIDIA DGX Spark

Usage: spark-smi [options]

  -l, --loop       live mode: curses TUI, refreshed continuously
  -n <secs>        refresh rate in seconds (default: {DEFAULT_REFRESH_RATE:g})
  -p, --page <n>   which page to render in snapshot mode: 1 overview (default),
                   2 advanced (GPU detail, PCIE LINKS -- every PCIe endpoint's
                   negotiated link state, regardless of occupant --
                   NIC/thermal/SMART panels), or
                   4 fabric validation (--cluster with 2+ members; renders
                   the last recorded run only -- snapshot mode never starts
                   a test)
  --theme <name>   color theme (default: spark; or $SPARK_SMI_THEME)
  --theme list     print the available theme names, one per line, and exit
  --ascii          force plain-ASCII bars/frames (no UTF-8 box drawing)
  --json           print one sample as JSON (+ node/model/version/ts) and exit
  --serve [PORT]   read-only HTTP sample server (default port {cluster.DEFAULT_PORT});
                   GET /sample, GET /healthz. $SPARK_SMI_TOKEN, if set, is
                   required as the X-Spark-Token header on /sample
  --serve-verbose  log one line per request to stderr (--serve only)
  --cluster HOSTS  cluster mode: page 3 shows every node in HOSTS (comma-
                   separated "host"/"host:port"/"ssh:host" entries, or
                   "@file" -- one entry per line, # comments). Bare
                   --cluster (no value) tries ~/.config/spark-smi/cluster.
                   With 2+ members, page 4 (fabric validation) becomes
                   available too -- see README's "Fabric validation" section
  -h, --help       show this help and exit

Themes: {", ".join(term.THEMES)}, cycled live with the 'c' key.
$SPARK_SMI_THEME sets the default when --theme isn't passed; an unknown name
prints the valid list and exits 1.

Snapshot mode (default, no -l) renders once and prints ANSI to stdout.
`--cluster ... --page 3` (or `4`) does one blocking poll round (2s cap) then
renders the page once. `--page 4` with no --cluster (or fewer than 2
members) prints a short "needs --cluster" note instead of a fabric panel.

Live-mode keys: q quit  ·  t toggle C/F  ·  u toggle GiB/GB  ·  1/2 page
                n active-NICs-only  ·  s sort NICs by rate  ·  c cycle theme
                ? help overlay
                page 2: tab select GPU  ·  P/C/M/R/X knobs
                page 3 (--cluster only): up/down select · enter drill into
                node · esc back · a alerts only (fleet) · o sort col (fleet)
                page 4 (--cluster with 2+ members only, fabric validation):
                space start/stop test · m mode (pair/sweep/burst) · d
                duration · up/down select pair (pair mode) · y confirms a
                run -- generates real RDMA/network traffic between nodes,
                so every run is confirm-gated (see README)
"""


_UNSET = object()  # distinguishes "--cluster not given" from "--cluster given with no value"


def _pcie_stuck_from_history(history, gen_max):
    """True when the TRAILING run of consecutive (util, gen_cur) samples in
    `history` (oldest-first) is >=3 long and every sample in that run has
    util>=20 AND gen_cur < gen_max -- i.e. the GPU has been under load while
    stuck below its negotiated max link generation for at least 3 straight
    ticks. A single idle-time downtrain (util<20) breaks the run rather than
    counting toward it, so idle downtraining alone -- normal PCIe power
    saving -- never flags; only a SUSTAINED low-gen state while genuinely
    busy does (the DGX Spark's known GPU power-draw-safety-mode quirk: the
    link downtrains to gen1 x4 and STAYS there under load). `gen_max` is the
    CURRENT sample's max link generation (a static hardware ceiling, not
    tracked historically); one sample is never enough on its own, which is
    also why snapshot mode's single-sample stream can never flag."""
    if gen_max is None:
        return False
    run = 0
    for util, gen_cur in reversed(list(history)):
        if util is not None and util >= 20 and gen_cur is not None and gen_cur < gen_max:
            run += 1
        else:
            break
    return run >= 3


def _parse_args(argv):
    opts = {"loop": False, "rate": DEFAULT_REFRESH_RATE, "help": False, "page": 1,
            "theme": None, "theme_list": False, "json": False, "serve": None,
            "serve_verbose": False, "cluster": _UNSET}
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in ("-l", "--loop"):
            opts["loop"] = True
        elif a == "-n":
            i += 1
            if i < len(argv):
                try:
                    opts["rate"] = max(0.1, float(argv[i]))
                except ValueError:
                    pass
        elif a in ("-p", "--page"):
            i += 1
            if i < len(argv):
                try:
                    n = int(argv[i])
                    opts["page"] = n if n in (1, 2, 3, 4) else 1
                except ValueError:
                    pass
        elif a == "--theme":
            i += 1
            if i < len(argv):
                if argv[i] == "list":
                    opts["theme_list"] = True
                else:
                    opts["theme"] = argv[i]
        elif a == "--themes":
            opts["theme_list"] = True
        elif a == "--json":
            opts["json"] = True
        elif a == "--serve":
            opts["serve"] = cluster.DEFAULT_PORT
            if i + 1 < len(argv) and argv[i + 1].isdigit():
                i += 1
                opts["serve"] = int(argv[i])
        elif a == "--serve-verbose":
            opts["serve_verbose"] = True
        elif a == "--cluster":
            opts["cluster"] = ""  # sentinel: try ~/.config/spark-smi/cluster
            if i + 1 < len(argv) and not argv[i + 1].startswith("-"):
                i += 1
                opts["cluster"] = argv[i]
        elif a in ("-h", "--help"):
            opts["help"] = True
        # --ascii is read directly from sys.argv by term._detect_bar_style()
        # at import time; nothing to do with it here.
        i += 1
    return opts


class State:
    """Owns the long-lived collectors (some hold delta state, e.g. NIC byte
    counters) and the history ring buffers pages.py's sparklines read from."""

    def __init__(self, rate):
        self.rate = rate
        self.page = 1
        self.cpu = collectors.CpuCollector()
        self.mem = collectors.MemoryCollector()
        self.gpu = collectors.GpuCollector()
        self.net = collectors.NetCollector()
        self.disk = collectors.DiskCollector()
        self.thermal = collectors.ThermalCollector()
        self.smart = collectors.SmartCollector()
        self.pcie = collectors.PcieCollector()
        self.cluster_hist = [deque(maxlen=HIST_LEN) for _ in self.cpu.clusters]
        self.gpu_hist = {}
        self.nic_hist = {}
        # Phase 8: per-GPU (util, gen_cur) history for stuck-under-load
        # detection -- separate from gpu_hist (util-only, for sparklines)
        # since this needs the paired gen_cur reading at the same tick.
        self.gpu_pcie_hist = {}

    def sample(self):
        try:
            cpu = self.cpu.sample()
        except Exception:
            cpu = {"clusters": [], "percpu": [], "temp": "N/A", "loadavg": None}
        try:
            mem = self.mem.sample()
        except Exception:
            mem = {"vm": None, "swap": None, "gpu_alloc": 0}
        try:
            gpus = self.gpu.sample()
        except Exception:
            gpus = []
        try:
            nics = self.net.sample()
        except Exception:
            nics = []
        try:
            disks = self.disk.sample()
        except Exception:
            disks = []
        try:
            thermal = self.thermal.sample()
        except Exception:
            thermal = []
        try:
            power_rails = self.thermal.sample_power()
        except Exception:
            power_rails = []
        try:
            smart = self.smart.sample(disks)
        except Exception:
            smart = None
        try:
            nic_pf = self.net.sample_pf_detail()
        except Exception:
            nic_pf = []
        try:
            nic_asic_temp = self.net.mlx5_asic_temp()
        except Exception:
            nic_asic_temp = None
        try:
            nic_fw = {r["rdma_dev"]: self.net.rdma_fw_ver(r["rdma_dev"]) for r in nic_pf}
        except Exception:
            nic_fw = {}
        try:
            pcie_links = self.pcie.sample()
        except Exception:
            pcie_links = []

        for i, cl in enumerate(cpu.get("clusters") or []):
            if i >= len(self.cluster_hist):
                self.cluster_hist.append(deque(maxlen=HIST_LEN))
            hist = self.cluster_hist[i]
            hist.append(cl.get("avg", 0.0))
            cl["history"] = list(hist)

        # Phase 8 join: PcieCollector's sysfs rows keyed by PCI address, for
        # GPUs whose own NVML pcie_gen_cur/max fields didn't come back (the
        # GB10 unified SoC doesn't reliably expose those via NVML the way a
        # discrete/eGPU does -- see collectors.GpuCollector).
        pcie_by_addr = {p["addr"]: p for p in pcie_links if p.get("addr")}
        unified_pci_addrs = set()

        for idx, g in enumerate(gpus):
            hist = self.gpu_hist.setdefault(g.get("id"), deque(maxlen=HIST_LEN))
            try:
                hist.append(float(g.get("util", 0)))
            except Exception:
                hist.append(0.0)
            g["history"] = list(hist)

            gpu_caps = self.gpu.caps[idx] if idx < len(self.gpu.caps) else {}
            if not gpu_caps.get("mem_local", True):
                # Unified SoCs excluded entirely from stuck-under-load
                # detection: GB10's gen1 link is architectural (SoC-internal
                # fabric, not a real PCIe wire) noise, not a safety-mode
                # symptom, and its PCIE row is already hidden on page 2.
                addr = g.get("pci_addr")
                if addr:
                    unified_pci_addrs.add(addr)
                g["pcie_stuck"] = False
                continue

            gen_cur = g.get("pcie_gen_cur")
            addr = g.get("pci_addr")
            link = pcie_by_addr.get(addr)
            if gen_cur is None and link is not None:
                gen_cur = link.get("gen_cur")
            # gen_max: ALWAYS prefer the slot-aware EFFECTIVE ceiling
            # (collectors.PcieCollector's parent-bridge walk) over the GPU's
            # own raw NVML max, which only reports what the DEVICE is
            # capable of -- not what the physical slot allows. Verified
            # live: an M.2/OCuLink GPU pinned at its slot's narrower max
            # generation under load is healthy, already at the best the
            # slot offers -- comparing against the raw device max instead
            # would flag it "stuck" forever. Falls back to the GPU's own
            # NVML max only when no effective value is available at all
            # (e.g. Windows/macOS with no sysfs parent-bridge walk).
            gen_max = link.get("gen_max_eff") if link is not None else None
            if gen_max is None:
                gen_max = g.get("pcie_gen_max")
            util = g.get("util", 0) or 0
            pcie_hist = self.gpu_pcie_hist.setdefault(g.get("id"), deque(maxlen=6))
            pcie_hist.append((util, gen_cur))
            g["pcie_stuck"] = _pcie_stuck_from_history(pcie_hist, gen_max)

        # The unified SoC's own PCI endpoint (GB10's on-die GPU) reads as a
        # permanently narrow link vs. its declared max width -- architectural
        # noise per the join above, not a real degraded PCIe link, so it's
        # dropped from the PCIE LINKS panel entirely rather than showing a
        # bogus perpetual "idle downtrain" row.
        if unified_pci_addrs:
            pcie_links = [p for p in pcie_links if p.get("addr") not in unified_pci_addrs]

        # Keyed by primary netdev name -- groups are stable frame-to-frame
        # (grouping is topology, detected once at construction), so this
        # doesn't need the same re-detection care as CPU cluster indices.
        for n in nics:
            hist = self.nic_hist.setdefault(n.get("name"), {"rx": deque(maxlen=HIST_LEN), "tx": deque(maxlen=HIST_LEN)})
            hist["rx"].append(n.get("rx_bps", 0) or 0)
            hist["tx"].append(n.get("tx_bps", 0) or 0)
            n["rx_history"] = list(hist["rx"])
            n["tx_history"] = list(hist["tx"])

        try:
            driver, cuda = self.gpu.driver_info()
        except Exception:
            driver, cuda = "Unknown", "Unknown"

        return {
            "cpu": cpu, "mem": mem, "gpus": gpus, "nics": nics, "disks": disks,
            "thermal": thermal, "power_rails": power_rails, "smart": smart, "nic_pf": nic_pf,
            "nic_asic_temp": nic_asic_temp, "nic_fw": nic_fw, "pcie_links": pcie_links,
            "driver": driver, "cuda": cuda, "rate": self.rate,
            "caps": {"gpu": self.gpu.caps, "net": self.net.caps, "has_nvml": collectors.HAS_NVML,
                     "thermal": self.thermal.caps, "smart": self.smart.caps, "pcie": self.pcie.caps},
        }


_EMPTY_SAMPLE = {"cpu": {}, "mem": {}, "gpus": [], "nics": [], "disks": [], "thermal": [],
                  "power_rails": [], "smart": None, "nic_pf": [], "pcie_links": [], "driver": "Unknown",
                  "cuda": "Unknown", "rate": DEFAULT_REFRESH_RATE, "caps": {}}


def render_dashboard(stdscr, colors_map, state, active_nics_only=False, height_hint=None, page_num=1,
                      knob_ui=None, sort_nics=False, show_help=False, theme_toast=None, cluster_ctx=None,
                      fab_ui=None, fab_engine=None):
    """Single UI entry point for both backends. Builds the requested page
    (1 overview, 2 advanced, 3 cluster, 4 fabric validation -- Phases 6/7,
    both --cluster only) and draws it.

    `knob_ui` (Phase 4) is a knobs.KnobUI instance in live mode's page 2 only
    -- main() never constructs one for the snapshot path, so passing None
    there (the default) is what guarantees "snapshot mode never writes and
    shows no knob UI": the registry below is simply never built, and
    pages.build_page2 falls back to its Phase-3 read-only rendering.

    `sort_nics` ('s' key, page 1 only) reorders NETWORK rows by max(rx, tx)
    rate descending each frame; `show_help` ('?' key, live mode, both pages)
    draws a help overlay panel appended last so it paints over everything
    else already built this frame. `theme_toast` ('c' key, live mode, both
    pages) is a (text, slot) pair shown via the footer's existing toast
    rendering (see pages._build_footer) for a few ticks after a theme
    switch, or None the rest of the time.

    `cluster_ctx` (Phase 6, --cluster only) is {"name", "aggregator", "ui"}
    -- a cluster.ClusterAggregator and cluster.ClusterUI. When page_num == 3
    it drives pages.build_page3 straight from the aggregator's freshest
    poll results (never blocking: get_views() only reads what the
    background thread already collected). When cluster_ui.drilldown is set
    (Enter on page 3), pages 1/2 render from that SELECTED NODE'S remote
    sample instead of state.sample() -- knob_ui is forced off for that case
    (registry writes stay strictly local, per spec), and pages.py's `remote`
    param adds the "· remote" header tag / page-2 "knobs are local-only"
    footer note.

    `fab_ui`/`fab_engine` (Phase 7, page 4 only) are a fabtest.FabTestUI and
    fabtest.FabricEngine -- both None in snapshot mode (main() never
    constructs either there, mirroring knob_ui's "write-capable object only
    exists in live mode" guarantee), in which case page 4 shows the last
    PERSISTED run only. This function never imports/calls into fabtest.py's
    process layer directly; it only reads the plain dicts fab_ui.render_ctx()
    /fab_engine.status()/fabtest.load_last_run()/fabtest.load_matrix() hand
    back, same "pages.py only ever consumes plain data" boundary knob_ctx
    keeps above."""
    try:
        h, w = stdscr.getmaxyx()
    except Exception:
        return
    if h < 8 or w < 40:
        return
    tier = pages.tier_for_width(w)
    draw_w = pages.content_width(tier, w)
    x0 = max(0, (w - draw_w) // 2)

    has_cluster = cluster_ctx is not None
    cluster_ui = cluster_ctx.get("ui") if has_cluster else None
    has_perf = has_cluster and len(cluster_ctx["aggregator"].members) >= 2

    if page_num == 3 and has_cluster:
        try:
            views = cluster_ctx["aggregator"].get_views()
        except Exception:
            views = []
        ctx = {"name": cluster_ctx.get("name", "cluster"), "views": views,
               "rate": state.rate, "ui": cluster_ui}
        try:
            built = pages.build_page3(ctx, tier, draw_w, x0, height=height_hint or h, has_perf=has_perf)
        except Exception:
            built = []
        try:
            panels.render(stdscr, built, colors_map)
        except Exception:
            pass
        return

    if page_num == 4:
        try:
            views = cluster_ctx["aggregator"].get_views() if has_cluster else []
        except Exception:
            views = []
        try:
            last_run = fabtest.load_last_run()
        except Exception:
            last_run = None
        try:
            matrix = fabtest.load_matrix()
        except Exception:
            matrix = {}
        engine_status = None
        if fab_engine is not None:
            try:
                engine_status = fab_engine.status()
            except Exception:
                engine_status = None
        fab_render_ctx = None
        if fab_ui is not None:
            fab_ui.tick()
            fab_render_ctx = fab_ui.render_ctx()
        ctx = {"name": cluster_ctx.get("name", "cluster") if has_cluster else "cluster",
               "views": views, "rate": state.rate, "fab": fab_render_ctx,
               "engine_status": engine_status, "last_run": last_run, "matrix": matrix}
        try:
            built = pages.build_page4(ctx, tier, draw_w, x0, height=height_hint or h)
        except Exception:
            built = []
        if show_help:
            try:
                built = built + pages.build_help_overlay(w, h)
            except Exception:
                pass
        try:
            panels.render(stdscr, built, colors_map)
        except Exception:
            pass
        return

    remote_name = None
    if has_cluster and cluster_ui is not None and cluster_ui.drilldown:
        remote_name = cluster_ui.drilldown_node
        try:
            remote_sample = cluster_ctx["aggregator"].get_member_sample(remote_name)
        except Exception:
            remote_sample = None
        sample = remote_sample if remote_sample is not None else dict(_EMPTY_SAMPLE, rate=state.rate)
    else:
        try:
            sample = state.sample()
        except Exception:
            sample = dict(_EMPTY_SAMPLE, rate=state.rate)

    if active_nics_only:
        sample["nics"] = [n for n in sample.get("nics", []) if n.get("up")]

    knob_ctx = None
    if knob_ui is not None and page_num == 2 and remote_name is None:
        try:
            registry = knobs.build_registry(sample)
        except Exception:
            registry = []
        knob_ui.last_registry = registry
        knob_ui.n_gpus = len(sample.get("gpus") or [])
        knob_ui.tick()
        knob_ctx = knob_ui.render_ctx()

    try:
        if page_num == 2:
            built = pages.build_page2(sample, tier, draw_w, x0, height=height_hint or h, knob_ui=knob_ctx,
                                       theme_toast=theme_toast, remote=remote_name, cluster_tabs=has_cluster,
                                       has_perf=has_perf)
        else:
            built = pages.build_page1(sample, tier, draw_w, x0, height=height_hint or h, sort_nics=sort_nics,
                                       theme_toast=theme_toast, remote=remote_name, cluster_tabs=has_cluster,
                                       has_perf=has_perf)
    except Exception:
        built = []

    if show_help:
        try:
            built = built + pages.build_help_overlay(w, h)
        except Exception:
            pass

    try:
        panels.render(stdscr, built, colors_map)
    except Exception:
        pass


def _init_curses_colors(curses):
    """(Re-)runs curses.init_pair() against the CURRENT term.PALETTE_256/
    BASIC_SLOTS and returns the resulting {slot: attr} colors_map. Curses
    caches color-pair definitions across frames, so this has to be re-run
    -- not just re-read -- whenever the active theme changes (main_loop's
    'c' key), in addition to the one-time call at loop startup."""
    if curses.COLORS >= 256:
        for slot, code in term.PALETTE_256.items():
            curses.init_pair(slot, code, -1)
    else:
        basic = {"GREEN": curses.COLOR_GREEN, "CYAN": curses.COLOR_CYAN, "WHITE": curses.COLOR_WHITE,
                 "RED": curses.COLOR_RED, "YELLOW": curses.COLOR_YELLOW,
                 "BLUE": curses.COLOR_BLUE, "MAGENTA": curses.COLOR_MAGENTA}
        for slot, (name, bold, dim) in term.BASIC_SLOTS.items():
            curses.init_pair(slot, basic.get(name, curses.COLOR_WHITE), -1)
    colors = {}
    for slot in term.PALETTE_256:
        attr = curses.color_pair(slot)
        _, bold, dim = term.BASIC_SLOTS.get(slot, ("WHITE", False, False))
        if bold:
            attr |= curses.A_BOLD
        if dim:
            attr |= curses.A_DIM
        colors[slot] = attr
    return colors


def main_loop(stdscr, state, rate, cluster_ctx=None):
    import curses
    curses.start_color()
    curses.use_default_colors()
    curses.curs_set(0)
    stdscr.nodelay(True)

    colors = _init_curses_colors(curses)

    active_nics_only = False
    sort_nics = False
    show_help = False
    theme_toast_text = None
    theme_toast_ticks = 0
    THEME_TOAST_TICKS = knobs.KnobUI.TOAST_TICKS
    # Phase 4: page 2's interactive power/clock knobs. Constructed here (not
    # in State, which the snapshot path also uses) so the write-capable UI
    # only exists at all in live mode -- see render_dashboard's docstring.
    knob_ui = knobs.KnobUI()
    cluster_ui = cluster_ctx["ui"] if cluster_ctx else None
    # Phase 7: page 4's fabric-test session state + the engine that actually
    # runs one. Both None whenever cluster_ctx is (matches knob_ui's "only
    # exists in live mode, and only where it makes sense" convention) -- the
    # key handling above is guarded on `cluster_ctx` for the same reason
    # page 3's own keys are, so a None fab_ui/fab_engine is never touched.
    fab_ui = fabtest.FabTestUI() if cluster_ctx else None
    fab_engine = fabtest.FabricEngine(cluster_ctx["aggregator"]) if cluster_ctx else None
    while True:
        if fab_ui is not None:
            try:
                names = [v.get("name") for v in cluster_ctx["aggregator"].get_views() if v.get("name")]
                fab_ui.set_nodes(names)
            except Exception:
                pass
        if theme_toast_ticks > 0:
            theme_toast_ticks -= 1
            if theme_toast_ticks <= 0:
                theme_toast_text = None
        theme_toast = (theme_toast_text, 9) if theme_toast_text else None
        # Knobs are strictly local-only (spec): a registry built while
        # looking at a remote node's page 2 would still write to THIS
        # machine's hardware while showing someone else's numbers, so every
        # Phase-4 key below is gated on NOT being in a cluster drilldown --
        # render_dashboard independently skips building the registry for
        # the same reason (belt and suspenders: the key handler can't apply
        # a knob that was never focused because its registry never built).
        in_remote = bool(cluster_ui and cluster_ui.drilldown)
        try:
            stdscr.erase()
            h, _ = stdscr.getmaxyx()
            render_dashboard(stdscr, colors, state, active_nics_only, height_hint=h, page_num=state.page,
                              knob_ui=knob_ui, sort_nics=sort_nics, show_help=show_help,
                              theme_toast=theme_toast, cluster_ctx=cluster_ctx,
                              fab_ui=fab_ui, fab_engine=fab_engine)
            stdscr.refresh()
        except Exception:
            pass

        start_wait = time.time()
        while time.time() - start_wait < rate:
            ch = stdscr.getch()
            # Help overlay swallows every key while shown -- "dismissed by
            # any key" takes priority over what that key would otherwise do
            # (e.g. 'q' while help is up closes help, it doesn't quit).
            if show_help:
                if ch != -1:
                    show_help = False
                    stdscr.clear()
                    break
                time.sleep(0.05)
                continue
            if ch == ord('q'):
                return
            if ch == ord('?'):
                show_help = True
                stdscr.clear()
                break
            # Phase 6 (--cluster only): page 3's own key bindings. '3' is
            # inert without --cluster (cluster_ctx is None, so every branch
            # here is a no-op and the key falls through unhandled below).
            if ch == ord('3') and cluster_ctx:
                if cluster_ui.drilldown:
                    cluster_ui.exit_drilldown()
                state.page = 3
                stdscr.clear()
                break
            # Phase 7 (--cluster only, same "inert without --cluster"
            # pattern as '3' above -- fab_ui/fab_engine are None whenever
            # cluster_ctx is, so every branch below is a no-op then too).
            if ch == ord('4') and cluster_ctx:
                if cluster_ui.drilldown:
                    cluster_ui.exit_drilldown()
                state.page = 4
                stdscr.clear()
                break
            if ch == 27 and cluster_ui and cluster_ui.drilldown:  # Esc: drilldown -> back to page 3
                cluster_ui.exit_drilldown()
                state.page = 3
                stdscr.clear()
                break
            if state.page == 3 and cluster_ctx:
                if ch == curses.KEY_UP:
                    cluster_ui.move(-1, cluster_ui.last_n)
                    break
                if ch == curses.KEY_DOWN:
                    cluster_ui.move(1, cluster_ui.last_n)
                    break
                if ch == ord('a'):
                    cluster_ui.toggle_filter()
                    stdscr.clear()
                    break
                if ch == ord('o'):
                    cluster_ui.cycle_sort()
                    stdscr.clear()
                    break
                if ch in (curses.KEY_ENTER, 10, 13):
                    if cluster_ui.selected_name:
                        cluster_ui.enter_drilldown(cluster_ui.selected_name)
                        state.page = 1
                        stdscr.clear()
                    break
            # Phase 7 (--cluster with 2+ members only): page 4's own key
            # bindings. Mirrors knobs.KnobUI's "confirming swallows every
            # key -- 'y' applies, anything else cancels" convention: the
            # confirming check comes FIRST so a stray keypress while a
            # confirm prompt is up can never fall through to (say) 'm'
            # silently changing the mode out from under an armed prompt.
            if state.page == 4 and cluster_ctx and fab_ui.confirming:
                if ch != -1:
                    if ch in (ord('y'), ord('Y')):
                        spec = fab_ui.confirm_yes()
                        if spec:
                            fab_engine.start(spec["mode"], spec["duration"], fab_ui.nodes,
                                              src=spec["src"], dst=spec["dst"],
                                              progress_cb=fab_ui.on_progress, done_cb=fab_ui.finish)
                    else:
                        fab_ui.cancel()
                    break
            elif state.page == 4 and cluster_ctx and ch == ord(' '):
                # space: running -> stop immediately (no confirm -- abort is
                # always one keypress); idle -> arm a y/N confirm, warning
                # about any node with an active GPU job.
                try:
                    gpu_active = fabtest.gpu_active_nodes(cluster_ctx["aggregator"].get_views())
                except Exception:
                    gpu_active = []
                action = fab_ui.toggle_start_stop(gpu_active)
                if action == "stop":
                    fab_engine.stop(fab_ui.nodes)
                    fab_ui.finish(None, stopped=True)
                break
            elif state.page == 4 and cluster_ctx and ch == ord('m'):
                fab_ui.cycle_mode()
                break
            elif state.page == 4 and cluster_ctx and ch == ord('d'):
                fab_ui.cycle_duration()
                break
            elif state.page == 4 and cluster_ctx and ch == curses.KEY_UP:
                fab_ui.move_pair(-1)
                break
            elif state.page == 4 and cluster_ctx and ch == curses.KEY_DOWN:
                fab_ui.move_pair(1)
                break
            if ch == ord('s'):
                sort_nics = not sort_nics
                stdscr.clear()
                break
            if ch == ord('t'):
                term.USE_FAHRENHEIT = not term.USE_FAHRENHEIT
                break
            if ch in (ord('c'), ord('T')):
                # 'c' is the documented key (footer/help/README); capital
                # 'T' still works as an undocumented alias -- cheap to keep
                # now that 'c' is primary. Global on both pages: this check
                # runs before the page-2 Phase 4 dispatch below, so it wins
                # over that block's OWN 'C'/'c' handling -- which is why
                # that block was narrowed to capital-only 'C' (see its
                # comment) rather than colliding with this key. Cycles to
                # the next theme (wrapping), re-runs curses.init_pair() for
                # it (colors are cached by curses across frames,
                # term.set_theme() alone wouldn't repaint), and forces a
                # full clear/redraw.
                names = list(term.THEMES)
                idx = names.index(term.ACTIVE_THEME) if term.ACTIVE_THEME in names else -1
                next_name = names[(idx + 1) % len(names)]
                term.set_theme(next_name)
                colors = _init_curses_colors(curses)
                theme_toast_text = f"theme: {next_name}"
                theme_toast_ticks = THEME_TOAST_TICKS
                stdscr.clear()
                break
            if ch == ord('u'):
                term.USE_DECIMAL_UNITS = not term.USE_DECIMAL_UNITS
                break
            if ch == ord('n'):
                active_nics_only = not active_nics_only
                stdscr.clear()
                break
            if ch == ord('1'):
                if state.page != 1:
                    state.page = 1
                    stdscr.clear()
                break
            if ch == ord('2'):
                if state.page != 2:
                    state.page = 2
                    stdscr.clear()
                break
            if ch == curses.KEY_RESIZE:
                stdscr.clear()
                break
            # Phase 4 knob controls, page 2 only. Every handler dispatches
            # into knobs.KnobUI's pure state-transition methods against the
            # registry render_dashboard cached on knob_ui THIS tick --
            # nothing here writes hardware directly, and 'y' only reaches
            # apply_knob() through KnobUI.confirm_yes() after an explicit
            # confirm prompt. The "confirming" check comes FIRST and
            # consumes every key while a confirm is armed ('y' applies,
            # anything else -- including Esc, or wandering into another
            # knob's key -- cancels), per spec: "y executes, anything else
            # cancels" must hold for every key, not just the ones below.
            if state.page == 2 and not in_remote and knob_ui.confirming:
                if ch != -1:
                    if ch in (ord('y'), ord('Y')):
                        knob_ui.confirm_yes(knob_ui.last_registry)
                    else:
                        knob_ui.cancel()
                    break
            elif state.page == 2 and not in_remote and ch == 9:  # Tab
                knob_ui.select_next_gpu(knob_ui.n_gpus)
                break
            elif state.page == 2 and not in_remote and ch in (ord('P'), ord('p')):
                knob_ui.focus_power_cycle(knob_ui.last_registry)
                break
            elif state.page == 2 and not in_remote and ch == ord('C'):
                # Capital-only: lowercase 'c' is the global theme-cycle key
                # (handled far above, before this whole Phase 4 block, so it
                # never reaches here) -- keeping this case-sensitive is what
                # lets 'c' cycle themes on page 2 while 'C' still focuses
                # the sm clock-lock knob.
                knob_ui.focus_clock(knob_ui.last_registry, "sm")
                break
            elif state.page == 2 and not in_remote and ch in (ord('M'), ord('m')):
                knob_ui.focus_clock(knob_ui.last_registry, "mem")
                break
            elif state.page == 2 and not in_remote and ch in (ord('R'), ord('r')):
                knob_ui.arm_reset(knob_ui.last_registry)
                break
            elif state.page == 2 and not in_remote and ch in (ord('X'), ord('x')):
                knob_ui.arm_persist(knob_ui.last_registry)
                break
            elif state.page == 2 and not in_remote and ch == curses.KEY_LEFT:
                knob_ui.step(knob_ui.last_registry, -1)
                break
            elif state.page == 2 and not in_remote and ch == curses.KEY_RIGHT:
                knob_ui.step(knob_ui.last_registry, 1)
                break
            elif state.page == 2 and not in_remote and ch in (curses.KEY_ENTER, 10, 13):
                knob_ui.enter(knob_ui.last_registry)
                break
            elif state.page == 2 and not in_remote and ch == 27:  # Esc
                knob_ui.cancel()
                break
            time.sleep(0.05)


def main():
    argv = sys.argv[1:]
    opts = _parse_args(argv)
    if opts["theme_list"]:
        for name in term.THEMES:
            print(name)
        sys.exit(0)
    if opts["help"]:
        print(HELP_TEXT)
        return

    # --theme flag wins over $SPARK_SMI_THEME wins over the "spark" default
    # set_theme() already applied at import. Resolved (and applied) here,
    # BEFORE either rendering path constructs anything that reads the
    # palette -- VirtualCurses() below and main_loop's curses.init_pair()
    # both need the final theme in place first.
    theme_name = opts["theme"] or os.environ.get("SPARK_SMI_THEME") or term.ACTIVE_THEME
    try:
        term.set_theme(theme_name)
    except ValueError as e:
        print(str(e), file=sys.stderr)
        sys.exit(1)

    state = State(opts["rate"])
    state.page = opts["page"]

    # --json: print one sample (+ node metadata) and exit -- the same wire
    # format --serve's /sample and --cluster's pollers use, so `spark-smi
    # --json | python -m json.tool` is also the quickest way to sanity-check
    # what a cluster member will hand its aggregator.
    if opts["json"]:
        payload = cluster.build_json_payload(state)
        print(cluster.dumps(payload))
        return

    # --serve: block forever answering GET /sample + /healthz. Never reaches
    # the render paths below -- this process IS the server, nothing else.
    if opts["serve"] is not None:
        cluster.run_server(state, port=opts["serve"], verbose=opts["serve_verbose"])
        return

    # --cluster: build the aggregator (and its ClusterUI) up front so both
    # the live and snapshot paths below can hand it to render_dashboard --
    # cluster_ctx stays None (page 3 unreachable, key '3' inert, no 3rd tab)
    # whenever --cluster wasn't given at all.
    cluster_ctx = None
    if opts["cluster"] is not _UNSET:
        try:
            entries, cname = cluster.parse_cluster_hosts(opts["cluster"])
        except Exception as e:
            print(f"--cluster: {e}", file=sys.stderr)
            sys.exit(1)
        if not entries:
            print("--cluster: no hosts to poll (empty list/file)", file=sys.stderr)
            sys.exit(1)
        token = os.environ.get("SPARK_SMI_TOKEN") or None
        aggregator = cluster.ClusterAggregator(entries, state, rate=opts["rate"], token=token)
        cluster_ctx = {"name": cname, "aggregator": aggregator, "ui": cluster.ClusterUI()}

    if opts["loop"]:
        try:
            import curses
        except ModuleNotFoundError:
            print("live mode needs curses, which this Python doesn't have.\n"
                  "On Windows:  pip install windows-curses\n"
                  "Snapshot mode works everywhere:  spark-smi [--page 2]")
            sys.exit(1)
        try:
            import locale
            locale.setlocale(locale.LC_ALL, "")
        except Exception:
            pass
        if cluster_ctx:
            cluster_ctx["aggregator"].start()
        try:
            curses.wrapper(main_loop, state, opts["rate"], cluster_ctx)
        except KeyboardInterrupt:
            pass
        finally:
            if cluster_ctx:
                cluster_ctx["aggregator"].stop()
    else:
        if cluster_ctx and state.page in (3, 4):
            # Snapshot mode has no background thread ticking -- one blocking
            # poll round (2s cap, spec) gets every member's freshest sample
            # before the single render. Page 4 in snapshot mode still only
            # ever DISPLAYS results (fab_ui/fab_engine are never constructed
            # below) -- this poll is just for the header/MATRIX node list.
            cluster_ctx["aggregator"].poll_once_sync(budget=2.0)
        v = term.VirtualCurses()
        colors = {i: i for i in term.PALETTE_256}
        render_dashboard(v, colors, state, page_num=state.page, cluster_ctx=cluster_ctx)
        output = v.render()
        # Some Windows consoles/pipes report a non-UTF-8 stdout encoding
        # (cp1252/cp437) even when BAR_STYLE picked "ascii" for the bars --
        # a handful of other glyphs (frame corners, degree signs, legend
        # squares) aren't gated by that check. Never let an encoding gap
        # turn into a traceback: fall back to a best-effort byte write.
        try:
            print(output)
        except UnicodeEncodeError:
            enc = getattr(sys.stdout, "encoding", None) or "ascii"
            try:
                sys.stdout.buffer.write(output.encode(enc, errors="replace") + b"\n")
            except Exception:
                print(output.encode("ascii", errors="replace").decode("ascii"))


if __name__ == "__main__":
    main()
