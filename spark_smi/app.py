"""Entry point: hand-rolled CLI parsing (no argparse, matching the pre-2.0
convention), the snapshot render path, and the curses live main loop.
Curses-specific calls are confined to this module -- everything else draws
through the duck-typed screen interface shared by VirtualCurses and real
curses windows.
"""
import json
import os
import shutil
import sys
import time
from collections import deque

from . import advpage
from . import bigpage
from . import cluster
from . import clusterpage
from . import collectors
from . import fabpage
from . import fabtest
from . import knobs
from . import node
from . import pages
from . import panels
from . import shot
from . import taskpage
from . import tasks
from . import term

try:
    from . import VERSION
except ImportError:
    VERSION = "5.5.1"
DEFAULT_REFRESH_RATE = 1.0
HIST_LEN = 60
# Local history kept for the big-screen layouts (#14): up to an hour at 1 s.
# Only HIST_LEN samples go in the sample's own *history keys (and so over
# the cluster wire); the rest rides in sample["long"], which to_wire drops.
HIST_LONG = 3600

HELP_TEXT = f"""spark-smi {VERSION} -- terminal system monitor for NVIDIA DGX Spark

Usage: spark-smi [options]

  -l, --loop       live mode: curses TUI, refreshed continuously
  -n <secs>        refresh rate in seconds (default: {DEFAULT_REFRESH_RATE:g})
  -p, --page <n>   which page to render in snapshot mode: 1 overview (default),
                   2 advanced (GPU detail, PCIE LINKS -- every PCIe endpoint's
                   negotiated link state, regardless of occupant --
                   NIC/thermal/SMART panels), 3 cluster (--cluster), or
                   4 fabric validation (--cluster with 2+ members; renders
                   the last recorded run only -- snapshot mode never starts
                   a test), or
                   5 GPU tasks (every process holding a GPU context, grouped
                   under its GPU by the terminal session it lives in --
                   read-only in snapshot mode)
  --theme <name>   color theme (default: spark; or $SPARK_SMI_THEME)
  --theme list     print the available theme names, one per line, and exit
  --background auto|dark|light
                   which ground the theme is measured against (default auto:
                   ask the terminal, then $COLORFGBG, else dark; or
                   $SPARK_SMI_BACKGROUND). Pictures (--shot) use dark
                   unless this says light
  --ascii          force plain-ASCII bars/frames (no UTF-8 box drawing)
  --shot FILE.svg  snapshot mode: write the page as an SVG picture instead of
                   printing it (complete page, any width; see --size)
  --size COLS[xROWS]
                   snapshot mode: render at this size instead of the
                   terminal's (ROWS defaults to the terminal's height, or
                   with --shot to the whole page)
  --warmup SECS    snapshot mode: sample once a second for SECS first, so
                   history graphs have data (0-120, default 0)
  --graphs MODE    page 1's graph size: auto (default; the big-screen
                   layouts on terminals of 200+ columns and 45+ rows),
                   large (also a one-column big layout from 120 columns),
                   or compact (the 160-column page at any size). The `g`
                   key cycles it live; the choice is remembered
  --history SPAN   how far back the big layouts' charts reach, 10s to 1h
                   (e.g. 90s, 5m, 1h; default 5m, 10m at 300+ columns).
                   The +/- keys step it live; the choice is remembered
  --json           print one sample as JSON (+ node/model/version/ts) and exit
  --serve [PORT]   read-only HTTP sample server (default port {cluster.DEFAULT_PORT});
                   GET /sample, GET /healthz. A token in the file named by
                   $SPARK_SMI_TOKEN_FILE (or, older setups, $SPARK_SMI_TOKEN)
                   is required as the X-Spark-Token header on /sample
  --serve-verbose  log one line per request to stderr (--serve only)
  --node-name NAME show NAME instead of this machine's hostname: in the
                   header, in --json and --serve's payload, and so on every
                   cluster page (or $SPARK_SMI_NODE_NAME; 1-63 letters,
                   digits, '-' or '_')
  --cluster HOSTS  cluster mode: page 3 shows every node in HOSTS (comma-
                   separated "host"/"host:port"/"ssh:host" entries, or
                   "@file" -- one entry per line, # comments). Bare
                   --cluster (no value) tries ~/.config/spark-smi/cluster.
                   With 2+ members, page 4 (fabric validation) becomes
                   available too -- see README's "Fabric validation" section
  -h, --help       show this help and exit
  -V, --version    print the version and exit

Themes: {", ".join(term.THEMES)}, cycled live with the 'c' key.
"terminal" uses your terminal's own 16 colours. $SPARK_SMI_THEME sets the
default when --theme isn't passed; an unknown name prints the valid list
and exits 1. Every theme keeps text at 4.5:1 contrast or better against the
background, on dark and light terminals alike.

Snapshot mode (default, no -l) renders once and prints ANSI to stdout.
`--cluster ... --page 3` (or `4`) does one blocking poll round (2s cap) then
renders the page once. `--page 4` with no --cluster (or fewer than 2
members) prints a short "needs --cluster" note instead of a fabric panel.

Live-mode keys: q quit  ·  t toggle C/F  ·  u toggle GiB/GB  ·  1/2/5 page
                n active-NICs-only  ·  s sort NICs by rate  ·  c cycle theme
                g graph size  ·  +/- history window (page 1)
                S save this screen as SVG (current folder)  ·  ? help overlay

Remembered choices (the g and +/- keys) live in
$XDG_CONFIG_HOME/spark-smi/settings.json (~/.config/spark-smi/settings.json).
                page 2: tab select GPU  ·  P/C/M/R/X knobs
                page 3 (--cluster only): up/down select · enter drill into
                node · esc back · a alerts only (fleet) · o sort col (fleet)
                page 4 (--cluster with 2+ members only, fabric validation):
                space start/stop test · m mode (pair/sweep/burst) · d
                duration · up/down select pair (pair mode) · y confirms a
                run -- generates real RDMA/network traffic between nodes,
                so every run is confirm-gated (see README)
                page 5 (GPU tasks, local only): up/down select · enter/o
                open the session (tmux/screen attach, journalctl/docker
                logs tail) · e end (SIGTERM, or the session's own stop) ·
                k kill (SIGKILL) · a also list bare /dev/nvidia* holders ·
                y confirms an end/kill, anything else cancels
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
            "serve_verbose": False, "cluster": _UNSET, "node_name": None,
            "shot": None, "size": None, "warmup": 0.0, "background": None,
            "graphs": None, "history": None, "version": False, "unknown": None}
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
                    opts["page"] = n if n in (1, 2, 3, 4, 5) else 1
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
        elif a == "--node-name":
            # Taken as given (even "-x"), so main() rejects a bad value with
            # its reason rather than silently reading the next flag as a name.
            opts["node_name"] = argv[i + 1] if i + 1 < len(argv) else ""
            i += 1
        elif a == "--cluster":
            opts["cluster"] = ""  # sentinel: try ~/.config/spark-smi/cluster
            if i + 1 < len(argv) and not argv[i + 1].startswith("-"):
                i += 1
                opts["cluster"] = argv[i]
        elif a == "--background":
            opts["background"] = argv[i + 1] if i + 1 < len(argv) else ""
            i += 1
        elif a == "--shot":
            opts["shot"] = argv[i + 1] if i + 1 < len(argv) else ""
            i += 1
        elif a == "--size":
            opts["size"] = argv[i + 1] if i + 1 < len(argv) else ""
            i += 1
        elif a == "--warmup":
            opts["warmup"] = argv[i + 1] if i + 1 < len(argv) else ""
            i += 1
        elif a == "--graphs":
            opts["graphs"] = argv[i + 1] if i + 1 < len(argv) else ""
            i += 1
        elif a == "--history":
            opts["history"] = argv[i + 1] if i + 1 < len(argv) else ""
            i += 1
        elif a in ("-h", "--help"):
            opts["help"] = True
        elif a in ("-V", "--version"):
            opts["version"] = True
        elif a == "--ascii":
            pass  # read directly from sys.argv by term._detect_bar_style() at import
        elif opts["unknown"] is None:
            # The first thing not understood (a typo'd option, a stray
            # word): main() refuses to run rather than guess (issue #15).
            opts["unknown"] = a
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
        # Page 5: only sampled while page 5 is on screen (render_dashboard),
        # never as part of sample() -- so --serve/--json/pages 1-4 pay
        # nothing for the /proc walk, and nothing about local pids ever
        # goes over the cluster wire format.
        self.tasks = tasks.TaskCollector()
        # Page 5's per-process history and recently-ended list (#14 step 2):
        # fed below from the GPU sample's own procs while a screen is drawn
        # (want_long), so it runs on every page; read only by page 5.
        self.proc_track = tasks.ProcTracker(HIST_LONG)
        self.cluster_hist = [deque(maxlen=HIST_LONG) for _ in self.cpu.clusters]
        self.gpu_hist = {}
        self.nic_hist = {}
        self.disk_hist = {}
        # Page 3's MEMORY rows graph used % per node (sent in the sample).
        self.mem_hist = deque(maxlen=HIST_LONG)
        # Phase 8: per-GPU (util, gen_cur) history for stuck-under-load
        # detection -- separate from gpu_hist (util-only, for sparklines)
        # since this needs the paired gen_cur reading at the same tick.
        self.gpu_pcie_hist = {}
        # Big-screen layouts only (#14): GPU power, per-core load, power
        # rails and temperature sensors, keyed by a stable name.
        self.gpu_pwr_hist = {}
        self.want_long = False
        # Big-screen layouts (#14): the `g` key's graph mode and the +/-
        # keys' history window in seconds (None: the layout's default).
        self.graphs = "auto"
        self.history = None
        self.core_hist = {}
        self.rail_hist = {}
        self.temp_hist = {}
        self.adv_hist = {}       # page 2's big layout: per-GPU and per-PF series

    def _push(self, store, key, value):
        hist = store.get(key)
        if hist is None:
            hist = store[key] = deque(maxlen=HIST_LONG)
        try:
            hist.append(float(value))
        except Exception:
            hist.append(0.0)
        return hist

    def _long_history(self, cpu, gpus, nics, disks, thermal, power_rails, nic_pf=None):
        """Up to HIST_LONG samples per series for the big-screen layouts
        (#14), as plain lists. Local only: cluster.to_wire() drops it."""
        out = {"cpu": [list(h) for h in self.cluster_hist], "mem": list(self.mem_hist),
               "gpu": {}, "gpu_pwr": {}, "nic": {}, "disk": {}, "core": {}, "rail": {}, "temp": {}}
        for g in gpus:
            gid = g.get("id")
            if gid in self.gpu_hist:
                out["gpu"][gid] = list(self.gpu_hist[gid])
            watts = _watts(g.get("pwr_str"))
            if watts is not None:
                out["gpu_pwr"][gid] = list(self._push(self.gpu_pwr_hist, gid, watts))
        for n in nics:
            h = self.nic_hist.get(n.get("name"))
            if h:
                out["nic"][n.get("name")] = {"rx": list(h["rx"]), "tx": list(h["tx"])}
        for d in disks:
            h = self.disk_hist.get(d.get("name"))
            if h:
                out["disk"][d.get("name")] = {"r": list(h["r"]), "w": list(h["w"])}
        for i, load in enumerate(cpu.get("percpu") or []):
            out["core"][i] = list(self._push(self.core_hist, i, load))
        for r in power_rails or []:
            if r.get("label") and r.get("watts") is not None:
                out["rail"][r["label"]] = list(self._push(self.rail_hist, r["label"], r["watts"]))
        for t in thermal or []:
            key = f"{t.get('hwname') or t.get('kind')}:{t.get('label')}"
            if t.get("temp_c") is not None:
                out["temp"][key] = list(self._push(self.temp_hist, key, t["temp_c"]))
        out["adv"] = self._adv_history(gpus, nic_pf)
        return out

    def _adv_history(self, gpus, nic_pf):
        """Page 2's series (#14): per GPU its SM clock (MHz), temperature
        (°C) and PCIe rx/tx (bytes/s); per PF (by netdev) rx/tx (bits/s)
        and congestion (CNP sent + handled + ECN marks, per second). A
        series starts when its value first reads as a number."""
        out = {"gpu": {}, "pf": {}}

        def num(v):
            return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None

        for g in gpus or []:
            gid = g.get("id")
            vals = {"clk": num(g.get("clk_sm")), "temp": num(g.get("temp"))}
            rx, tx = num(g.get("pcie_rx_kbs")), num(g.get("pcie_tx_kbs"))
            if rx is not None and tx is not None:
                vals["pcie_rx"], vals["pcie_tx"] = rx * 1024, tx * 1024
            out["gpu"][gid] = {k: list(self._push(self.adv_hist, ("gpu", gid, k), v))
                               for k, v in vals.items() if v is not None}
        for r in nic_pf or []:
            name = r.get("netdev")
            if not name:
                continue
            cong = [num(r.get(k)) for k in ("cnp_sent", "cnp_handled", "ecn_marked")]
            vals = {"rx": num(r.get("rx_bps")), "tx": num(r.get("tx_bps")),
                    "cong": sum(c for c in cong if c is not None) if any(c is not None for c in cong) else None}
            out["pf"][name] = {k: list(self._push(self.adv_hist, ("pf", name, k), v))
                               for k, v in vals.items() if v is not None}
        return out

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
                self.cluster_hist.append(deque(maxlen=HIST_LONG))
            hist = self.cluster_hist[i]
            hist.append(cl.get("avg", 0.0))
            cl["history"] = list(hist)[-HIST_LEN:]

        # Phase 8 join: PcieCollector's sysfs rows keyed by PCI address, for
        # GPUs whose own NVML pcie_gen_cur/max fields didn't come back (the
        # GB10 unified SoC doesn't reliably expose those via NVML the way a
        # discrete/eGPU does -- see collectors.GpuCollector).
        pcie_by_addr = {p["addr"]: p for p in pcie_links if p.get("addr")}
        unified_pci_addrs = set()

        for idx, g in enumerate(gpus):
            hist = self.gpu_hist.setdefault(g.get("id"), deque(maxlen=HIST_LONG))
            try:
                hist.append(float(g.get("util", 0)))
            except Exception:
                hist.append(0.0)
            g["history"] = list(hist)[-HIST_LEN:]

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
            # Carry the same slot-effective ceiling onto the GPU so page 2's
            # PCIE row judges "degraded" against the slot too. Without this
            # the per-GPU row compares against the device's raw capability
            # and labels an M.2-fed card "(idle downtrain)" while the PCIE
            # LINKS panel calls that identical link "ok" one panel below.
            g["pcie_gen_max_eff"] = gen_max
            width_max_eff = link.get("width_max_eff") if link is not None else None
            g["pcie_width_max_eff"] = width_max_eff if width_max_eff is not None else g.get("pcie_width_max")

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
            hist = self.nic_hist.setdefault(n.get("name"), {"rx": deque(maxlen=HIST_LONG), "tx": deque(maxlen=HIST_LONG)})
            hist["rx"].append(n.get("rx_bps", 0) or 0)
            hist["tx"].append(n.get("tx_bps", 0) or 0)
            n["rx_history"] = list(hist["rx"])[-HIST_LEN:]
            n["tx_history"] = list(hist["tx"])[-HIST_LEN:]
        try:
            self.mem_hist.append(float(mem["vm"].percent))
            mem["history"] = list(self.mem_hist)[-HIST_LEN:]
        except Exception:
            pass
        # Same for block devices: page 1's mirrored R/W history (D-02 item 1).
        for d in disks:
            hist = self.disk_hist.setdefault(d.get("name"), {"r": deque(maxlen=HIST_LONG), "w": deque(maxlen=HIST_LONG)})
            hist["r"].append(d.get("read_bps", 0) or 0)
            hist["w"].append(d.get("write_bps", 0) or 0)
            d["read_history"] = list(hist["r"])[-HIST_LEN:]
            d["write_history"] = list(hist["w"])[-HIST_LEN:]

        try:
            driver, cuda = self.gpu.driver_info()
        except Exception:
            driver, cuda = "Unknown", "Unknown"

        # Only the screen pays for the long copies; --serve/--json don't.
        long = self._long_history(cpu, gpus, nics, disks, thermal, power_rails, nic_pf) if self.want_long else None
        if self.want_long:
            try:
                self.proc_track.update(gpus)
            except Exception:
                pass

        return {
            "long": long,
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
                      fab_ui=None, fab_engine=None, task_ui=None):
    """Single UI entry point for both backends. Builds the requested page
    (1 overview, 2 advanced, 3 cluster, 4 fabric validation -- Phases 6/7,
    both --cluster only -- 5 GPU tasks, always local) and draws it.

    `task_ui` (page 5) is a tasks.TaskUI in live mode only -- None in
    snapshot mode, exactly like knob_ui: with no TaskUI there is no
    selection, no confirm prompt, and nothing that could ever reach
    tasks.end_process()/end_group(). Page 5 always samples the LOCAL
    machine, even inside a cluster drilldown (signals, like knob writes,
    must never be aimed at a remote node's pid numbers).

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
    state.want_long = True
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
        # Big terminals (#14 step 3): dials, topology and a chart column per
        # node across the full width; None falls back to the normal page.
        layout = bigpage.layout_for(w, height_hint or h, state.graphs) if term.BAR_STYLE == "eighths" else None
        built = None
        if layout in ("large", "xl"):
            try:
                built = clusterpage.build_page3_large(ctx, w, height_hint or h, 0, layout, history=state.history,
                                                      theme_toast=theme_toast, has_perf=has_perf)
            except Exception:
                built = None
        if built is None:
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
        # Big terminals (#14): a tall bandwidth chart, every node's fabric
        # traffic and the run history; None falls back to the normal page.
        layout = bigpage.layout_for(w, height_hint or h, state.graphs) if term.BAR_STYLE == "eighths" else None
        built = None
        if layout in ("large", "xl"):
            try:
                runs = fabtest.load_recent_runs(60)
            except Exception:
                runs = []
            try:
                built = fabpage.build_page4_large(ctx, w, height_hint or h, 0, layout, history=state.history,
                                                  runs=runs, theme_toast=theme_toast)
            except Exception:
                built = None
        try:
            if built is None:
                built = pages.build_page4(ctx, tier, draw_w, x0, height=height_hint or h,
                                          theme_toast=theme_toast)
        except Exception as e:
            # never a black screen: a build crash renders as one visible line
            # (the live-series shape bug hid behind a bare `built = []` here)
            err = panels.Panel(0, x0, draw_w, kind="plain")
            err.rows.append(([(f" page 4 render error: {type(e).__name__}: {str(e)[:80]}", 4)], []))
            built = [err]
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

    if page_num == 5:
        try:
            sample = state.sample()
        except Exception:
            sample = dict(_EMPTY_SAMPLE, rate=state.rate)
        include_holders = bool(task_ui is not None and task_ui.include_holders)
        try:
            tree = state.tasks.sample(sample.get("gpus") or [], include_holders=include_holders)
        except Exception:
            tree = {"gpus": [], "n_procs": 0, "n_orphans": 0, "source": "none", "caps": {}}
        view = task_ui.render_ctx() if task_ui is not None else {}
        try:
            rows = tasks.flatten(tree, sort=view.get("sort", "mem"), flat=view.get("flat", False),
                                 filt=view.get("filter", ""))
        except Exception:
            rows = []
        task_ctx = None
        if task_ui is not None:
            task_ui.set_rows(rows)
            task_ui.tick()
            task_ctx = task_ui.render_ctx()
        try:
            track = state.proc_track.snapshot()
        except Exception:
            track = None
        sel = taskpage.selected_proc_row(rows, task_ctx)
        try:
            detail = tasks.proc_detail(sel["proc"]["pid"]) if sel else None
        except Exception:
            detail = None
        # The big layout's charts are braille: other terminals keep the
        # normal page, as page 1 does.
        layout = bigpage.layout_for(w, height_hint or h, state.graphs) if term.BAR_STYLE == "eighths" else None
        big = layout in ("large", "xl")
        try:
            built = taskpage.build_page5(sample, tree, rows, tier, w if big else draw_w, 0 if big else x0,
                                         height=height_hint or h, ui=task_ctx, theme_toast=theme_toast,
                                         cluster_tabs=has_cluster, has_perf=has_perf, track=track,
                                         detail=detail, span=state.history, layout=layout)
        except Exception as e:
            err = panels.Panel(0, x0, draw_w, kind="plain")
            err.rows.append(([(f" page 5 render error: {type(e).__name__}: {str(e)[:80]}", 4)], []))
            built = [err]
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
            built = None
            layout = bigpage.layout_for(w, height_hint or h, getattr(state, "graphs", "auto"))
            if layout in ("large", "xl"):
                # The big layout (#14) needs the local long history, so a
                # remote drilldown keeps the normal page.
                built = advpage.build_page2_large(sample, w, height_hint or h, 0, layout,
                                                  history=getattr(state, "history", None), knob_ui=knob_ctx,
                                                  theme_toast=theme_toast, remote=remote_name,
                                                  cluster_tabs=has_cluster, has_perf=has_perf)
            if built is None:
                built = pages.build_page2(sample, tier, draw_w, x0, height=height_hint or h, knob_ui=knob_ctx,
                                           theme_toast=theme_toast, remote=remote_name, cluster_tabs=has_cluster,
                                           has_perf=has_perf)
        else:
            built = None
            layout = bigpage.layout_for(w, height_hint or h, getattr(state, "graphs", "auto"))
            if layout:
                # The big layouts (#14) use the whole terminal, not the
                # centred 160-column page; None when they can't fit.
                if sort_nics and sample.get("nics"):
                    sample["nics"] = sorted(sample["nics"], reverse=True,
                                            key=lambda n: max(n.get("rx_bps", 0) or 0, n.get("tx_bps", 0) or 0))
                built = bigpage.build_page1_large(sample, w, height_hint or h, 0, layout,
                                                  history=getattr(state, "history", None),
                                                  graphs=getattr(state, "graphs", "auto"), theme_toast=theme_toast,
                                                  remote=remote_name, cluster_tabs=has_cluster, has_perf=has_perf)
            if built is None:
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
    if term.ACTIVE_THEME == term.TERMINAL_THEME:
        # the terminal's own colours: ANSI 0-15 (8-15 as bold 0-7 where the
        # terminal has only 8) and -1, its default foreground
        colors = {}
        for slot, code in term.PALETTE_256.items():
            bold = code >= 8 and curses.COLORS < 16
            curses.init_pair(slot, code - 8 if bold else code, -1)
            colors[slot] = curses.color_pair(slot) | (curses.A_BOLD if bold or slot in (8, 9) else 0)
        return colors
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
        # A_DIM only stands in for missing colours: on 256 colours the inks
        # are measured (term.build_palette), and dimming them again would
        # push dim text under its 4.5:1 floor. Snapshot mode never dimmed.
        if dim and curses.COLORS < 256:
            attr |= curses.A_DIM
        colors[slot] = attr
    return colors


def parse_span(text):
    """'90', '90s', '5m', '1h' -> seconds, within 10 s .. 1 h; else None."""
    t = str(text).strip().lower()
    mult = 1
    if t[-1:] in ("s", "m", "h"):
        mult = {"s": 1, "m": 60, "h": 3600}[t[-1]]
        t = t[:-1]
    try:
        secs = int(float(t) * mult)
    except ValueError:
        return None
    return secs if 10 <= secs <= HIST_LONG else None


def settings_path():
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(base, "spark-smi", "settings.json")


def load_settings():
    """The live-mode choices remembered between runs ({graphs, history}).
    A missing or unreadable file is just no saved choices."""
    try:
        with open(settings_path(), encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_settings(**changes):
    """Merges `changes` into the settings file (owner-only, written whole
    and renamed into place). Never raises: failing to remember a choice
    must not take the dashboard down."""
    try:
        data = load_settings()
        data.update(changes)
        path = settings_path()
        os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
        tmp = path + ".tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=1)
        os.replace(tmp, path)
    except Exception:
        pass


def _watts(text):
    """'348 W' -> 348.0; None for 'N/A' or anything unparsable."""
    try:
        return float(str(text).split()[0])
    except Exception:
        return None


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
    shot_pending = False
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
    # Page 5: the task manager's selection/confirm state. Live-mode only,
    # same reasoning as knob_ui -- the snapshot path never has one, so it
    # can never signal a process.
    task_ui = tasks.TaskUI()

    def _run_outside_curses(argv):
        """Suspends curses, runs an interactive command (tmux attach, screen,
        journalctl -f, docker logs -f) in the terminal, then restores the
        screen. Ctrl-C is meant for the child, so SIGINT is ignored in this
        process for the duration -- otherwise a `journalctl -f` tail would
        take the dashboard down with it."""
        import signal
        import subprocess
        try:
            curses.def_prog_mode()
            curses.endwin()
        except Exception:
            pass
        old = None
        try:
            old = signal.signal(signal.SIGINT, signal.SIG_IGN)
        except Exception:
            pass
        try:
            subprocess.call(argv)
        except Exception:
            pass
        finally:
            if old is not None:
                try:
                    signal.signal(signal.SIGINT, old)
                except Exception:
                    pass
            try:
                curses.reset_prog_mode()
                stdscr.clear()
                stdscr.refresh()
                curses.flushinp()
            except Exception:
                pass

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
            h, w = stdscr.getmaxyx()
            screen = stdscr
            if shot_pending:
                screen = _TeeScreen(stdscr, term.VirtualCurses(size=(w, h)), colors)
            render_dashboard(screen, colors, state, active_nics_only, height_hint=h, page_num=state.page,
                              knob_ui=knob_ui, sort_nics=sort_nics, show_help=show_help,
                              theme_toast=theme_toast, cluster_ctx=cluster_ctx,
                              fab_ui=fab_ui, fab_engine=fab_engine, task_ui=task_ui)
            stdscr.refresh()
        except Exception:
            pass
        if shot_pending:
            # `S`: the frame just drawn, as SVG in the current folder. The
            # toast shows on the next frame, so it is not in the picture.
            shot_pending = False
            name = _live_shot_name(state.page)
            try:
                shot.write_svg(name, screen.virtual.grid)
                theme_toast_text = f"saved {name}"
            except Exception as e:
                theme_toast_text = f"save failed: {getattr(e, 'strerror', None) or e}"
            theme_toast_ticks = THEME_TOAST_TICKS

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
            # Page 5's `/` filter: while typing, every key is text (q
            # included) until Enter keeps it or Esc clears it.
            if state.page == 5 and task_ui.editing:
                if ch != -1:
                    task_ui.filter_key(ch)
                    break
                time.sleep(0.02)
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
            if ch == ord('5'):
                # Page 5 is local-only: leaving a drilldown here keeps the
                # cluster tabs but never shows a remote node's processes.
                if cluster_ui and cluster_ui.drilldown:
                    cluster_ui.exit_drilldown()
                if state.page != 5:
                    state.page = 5
                    task_ui.cancel()
                    stdscr.clear()
                break
            # Page 5 keys. Confirm swallows everything first ('y' acts,
            # anything else cancels) -- the same rule knobs and fabtest use.
            if state.page == 5 and task_ui.confirming:
                if ch != -1:
                    if ch in (ord('y'), ord('Y')):
                        task_ui.confirm_yes()
                    else:
                        task_ui.cancel()
                    break
            elif state.page == 5 and ch == curses.KEY_UP:
                task_ui.move(-1)
                break
            elif state.page == 5 and ch == curses.KEY_DOWN:
                task_ui.move(1)
                break
            elif state.page == 5 and ch == curses.KEY_PPAGE:
                task_ui.move(-10)
                break
            elif state.page == 5 and ch == curses.KEY_NPAGE:
                task_ui.move(10)
                break
            elif state.page == 5 and ch == ord('e'):
                task_ui.arm_end(force=False)
                break
            elif state.page == 5 and ch == ord('k'):
                task_ui.arm_end(force=True)
                break
            elif state.page == 5 and ch == ord('a'):
                task_ui.toggle_holders()
                stdscr.clear()
                break
            elif state.page == 5 and ch == ord('s'):
                task_ui.cycle_sort()
                break
            elif state.page == 5 and ch == ord('f'):
                task_ui.toggle_flat()
                stdscr.clear()
                break
            elif state.page == 5 and ch == ord('/'):
                task_ui.start_filter()
                break
            elif state.page == 5 and ch in (ord('o'), curses.KEY_ENTER, 10, 13):
                spec = task_ui.open_spec()
                if spec:
                    argv, blocking = spec
                    if blocking:
                        _run_outside_curses(argv)
                    else:
                        try:
                            import subprocess
                            subprocess.run(argv, capture_output=True, timeout=5)
                        except Exception:
                            pass
                    stdscr.clear()
                break
            elif state.page == 5 and ch == 27:  # Esc: cancel, else drop a kept filter
                if not task_ui.confirming and task_ui.filter:
                    task_ui.filter = ""
                task_ui.cancel()
                break
            if ch == 27 and cluster_ui and cluster_ui.drilldown:  # Esc: drilldown -> back to page 3
                cluster_ui.exit_drilldown()
                state.page = 3
                stdscr.clear()
                break
            if state.page == 3 and cluster_ctx:
                # ←/→ too: the big layout puts the nodes side by side
                if ch in (curses.KEY_UP, curses.KEY_LEFT):
                    cluster_ui.move(-1, cluster_ui.last_n)
                    break
                if ch in (curses.KEY_DOWN, curses.KEY_RIGHT):
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
            if ch == ord('S'):
                shot_pending = True
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
            if ch == ord('g'):
                # Graph size (#14): auto (big layouts when the terminal is
                # big enough) -> large (also on 120-199 columns) -> compact
                # (today's page everywhere). Remembered between runs.
                modes = list(bigpage.GRAPH_MODES)
                state.graphs = modes[(modes.index(state.graphs) + 1) % len(modes)] \
                    if state.graphs in modes else "auto"
                save_settings(graphs=state.graphs)
                theme_toast_text = f"graphs: {state.graphs}"
                theme_toast_ticks = THEME_TOAST_TICKS
                stdscr.clear()
                break
            if ch in (ord('+'), ord('='), ord('-'), ord('_')):
                layout = bigpage.layout_for(w, h, state.graphs)
                state.history = bigpage.history_step(state.history, 1 if ch in (ord('+'), ord('=')) else -1, layout)
                save_settings(history=state.history)
                theme_toast_text = f"history: {term.fmt_span(state.history)}"
                theme_toast_ticks = THEME_TOAST_TICKS
                stdscr.clear()
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



def _token_or_exit(flag):
    """cluster.read_token(), exiting with the reason when the operator asked
    for a token that cannot be used -- never serve or poll without it."""
    try:
        return cluster.read_token()
    except cluster.TokenError as e:
        print(f"{flag}: {e}", file=sys.stderr)
        sys.exit(1)

def parse_size(text):
    """(cols, rows or None) from "COLS" or "COLSxROWS". Raises ValueError
    with the reason. The floor is render_dashboard's own minimum (40x8)."""
    parts = text.lower().split("x")
    if len(parts) not in (1, 2) or not all(p.isdigit() for p in parts):
        raise ValueError(f"'{text}' is not COLS or COLSxROWS, e.g. 140 or 140x45")
    cols = int(parts[0])
    rows = int(parts[1]) if len(parts) == 2 else None
    if not 40 <= cols <= 400 or (rows is not None and not 8 <= rows <= 500):
        raise ValueError("COLS must be 40-400 and ROWS 8-500")
    return cols, rows


SHOT_ROWS = 500  # --shot without ROWS: tall enough for any page, then trimmed


class _TeeScreen:
    """Draws one frame to the live curses window and to a VirtualCurses grid
    at once, so the live `S` key saves exactly the frame on screen without
    a second render (which would take a second, near-zero-interval sample)."""

    def __init__(self, screen, virtual, colors):
        self.screen, self.virtual = screen, virtual
        self.slot_of = {attr: slot for slot, attr in colors.items()}

    def getmaxyx(self):
        return self.screen.getmaxyx()

    def erase(self):
        self.screen.erase()
        self.virtual.erase()

    def addstr(self, y, x, text, attr=0):
        self.virtual.addstr(y, x, text, self.slot_of.get(attr))
        return self.screen.addstr(y, x, text, attr)


def _live_shot_name(page):
    return time.strftime(f"spark-smi-page{page}-%Y%m%d-%H%M%S.svg")


def main():
    argv = sys.argv[1:]
    opts = _parse_args(argv)
    if opts["unknown"] is not None:
        what = "unknown option" if opts["unknown"].startswith("-") else "unexpected argument"
        print(f"spark-smi: {what} {opts['unknown']!r}; spark-smi --help lists the options", file=sys.stderr)
        sys.exit(1)
    if opts["version"]:
        print(f"spark-smi {VERSION}")
        return
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
    # --background wins over $SPARK_SMI_BACKGROUND wins over "auto". Only a
    # render to a real terminal asks it (OSC 11); pictures and pipes never
    # do, so a --shot looks the same whoever runs it.
    bg_setting = (opts["background"] if opts["background"] is not None
                  else os.environ.get("SPARK_SMI_BACKGROUND") or "auto").lower()
    if bg_setting not in ("auto", "dark", "light"):
        print("--background: give auto, dark or light", file=sys.stderr)
        sys.exit(1)
    if opts["json"] or opts["serve"] is not None:
        pass
    elif opts["shot"] is not None:
        term.set_background(bg_setting == "light", source="setting" if bg_setting != "auto" else "default")
    else:
        light, rgb, source = term.detect_background(bg_setting, ask=opts["loop"] or sys.stdout.isatty())
        term.set_background(light, rgb, source)

    theme_name = opts["theme"] or os.environ.get("SPARK_SMI_THEME") or term.ACTIVE_THEME
    try:
        term.set_theme(theme_name)
    except ValueError as e:
        print(str(e), file=sys.stderr)
        sys.exit(1)

    # --node-name wins over $SPARK_SMI_NODE_NAME; neither means the hostname.
    # Set before anything builds a payload or a header (issue #9).
    try:
        node.set_display_name(node.resolve(opts["node_name"]))
    except ValueError as e:
        source = "--node-name" if opts["node_name"] is not None else "$" + node.ENV_VAR
        print(f"{source}: {e}", file=sys.stderr)
        sys.exit(1)

    size = None
    if opts["size"] is not None:
        try:
            size = parse_size(opts["size"])
        except ValueError as e:
            print(f"--size: {e}", file=sys.stderr)
            sys.exit(1)
    if opts["shot"] is not None and not opts["shot"].lower().endswith(".svg"):
        print("--shot: give a file name ending in .svg", file=sys.stderr)
        sys.exit(1)
    try:
        warmup = float(opts["warmup"])
        if not 0 <= warmup <= 120:
            raise ValueError
    except (TypeError, ValueError):
        print("--warmup: give a number of seconds from 0 to 120", file=sys.stderr)
        sys.exit(1)

    state = State(opts["rate"])
    state.page = opts["page"]
    saved = load_settings()
    graphs = opts["graphs"] if opts["graphs"] is not None else saved.get("graphs", "auto")
    if graphs not in bigpage.GRAPH_MODES:
        if opts["graphs"] is not None:
            print(f"--graphs: give one of {', '.join(bigpage.GRAPH_MODES)}", file=sys.stderr)
            sys.exit(1)
        graphs = "auto"
    state.graphs = graphs
    hist = opts["history"] if opts["history"] is not None else saved.get("history")
    if hist is not None:
        secs = parse_span(hist)
        if secs is None:
            if opts["history"] is not None:
                print("--history: give a window from 10s to 1h, such as 90s, 5m or 1h", file=sys.stderr)
                sys.exit(1)
        state.history = secs

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
        token, source = _token_or_exit("--serve")
        cluster.run_server(state, port=opts["serve"], verbose=opts["serve_verbose"],
                           token=token, token_source=source)
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
        token, _source = _token_or_exit("--cluster")
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
        # --warmup: fill the history ring buffers the way live mode does, one
        # sample a second, so the single render below can draw its graphs.
        state.want_long = True
        for _ in range(int(warmup)):
            state.sample()
            time.sleep(1.0)
        if opts["shot"] is not None:
            cols = size[0] if size else shutil.get_terminal_size((140, 50)).columns
            size = (max(40, min(cols, 400)), (size[1] if size and size[1] else SHOT_ROWS))
        v = term.VirtualCurses(size=size)
        colors = {i: i for i in term.PALETTE_256}
        render_dashboard(v, colors, state, page_num=state.page, cluster_ctx=cluster_ctx)
        if opts["shot"] is not None:
            try:
                shot.write_svg(opts["shot"], v.grid)
            except OSError as e:
                print(f"--shot: cannot write {opts['shot']}: {e.strerror or e}", file=sys.stderr)
                sys.exit(1)
            print(f"wrote {opts['shot']}")
            return
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
