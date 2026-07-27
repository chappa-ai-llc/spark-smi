"""Entry point: hand-rolled CLI parsing (no argparse, matching the pre-2.0
convention), the snapshot render path, and the curses live main loop.
Curses-specific calls are confined to this module -- everything else draws
through the duck-typed screen interface shared by VirtualCurses and real
curses windows.
"""
import sys
import time
from collections import deque

from . import collectors
from . import pages
from . import panels
from . import term

try:
    from . import VERSION
except ImportError:
    VERSION = "2.0.0.dev0"
DEFAULT_REFRESH_RATE = 1.0
HIST_LEN = 60

HELP_TEXT = f"""spark-smi {VERSION} -- terminal system monitor for NVIDIA DGX Spark

Usage: spark-smi [options]

  -l, --loop       live mode: curses TUI, refreshed continuously
  -n <secs>        refresh rate in seconds (default: {DEFAULT_REFRESH_RATE:g})
  -p, --page <n>   which page to render in snapshot mode: 1 overview (default)
                   or 2 advanced (GPU detail, NIC/thermal/SMART panels)
  --ascii          force plain-ASCII bars/frames (no UTF-8 box drawing)
  -h, --help       show this help and exit

Snapshot mode (default, no -l) renders once and prints ANSI to stdout.

Live-mode keys: q quit  ·  t toggle C/F  ·  u toggle GiB/GB
                n active-NICs-only  ·  1/2 page
"""


def _parse_args(argv):
    opts = {"loop": False, "rate": DEFAULT_REFRESH_RATE, "help": False, "page": 1}
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
                    opts["page"] = 2 if int(argv[i]) == 2 else 1
                except ValueError:
                    pass
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
        self.cluster_hist = [deque(maxlen=HIST_LEN) for _ in self.cpu.clusters]
        self.gpu_hist = {}
        self.nic_hist = {}

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

        for i, cl in enumerate(cpu.get("clusters") or []):
            if i >= len(self.cluster_hist):
                self.cluster_hist.append(deque(maxlen=HIST_LEN))
            hist = self.cluster_hist[i]
            hist.append(cl.get("avg", 0.0))
            cl["history"] = list(hist)

        for g in gpus:
            hist = self.gpu_hist.setdefault(g.get("id"), deque(maxlen=HIST_LEN))
            try:
                hist.append(float(g.get("util", 0)))
            except Exception:
                hist.append(0.0)
            g["history"] = list(hist)

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
            "nic_asic_temp": nic_asic_temp, "nic_fw": nic_fw,
            "driver": driver, "cuda": cuda, "rate": self.rate,
            "caps": {"gpu": self.gpu.caps, "net": self.net.caps, "has_nvml": collectors.HAS_NVML,
                     "thermal": self.thermal.caps, "smart": self.smart.caps},
        }


def render_dashboard(stdscr, colors_map, state, active_nics_only=False, height_hint=None, page_num=1):
    """Single UI entry point for both backends. Builds the requested page
    (1 overview, 2 advanced) and draws it."""
    try:
        h, w = stdscr.getmaxyx()
    except Exception:
        return
    if h < 8 or w < 40:
        return
    tier = pages.tier_for_width(w)
    draw_w = pages.content_width(tier, w)
    x0 = max(0, (w - draw_w) // 2)

    try:
        sample = state.sample()
    except Exception:
        sample = {"cpu": {}, "mem": {}, "gpus": [], "nics": [], "disks": [], "thermal": [],
                  "power_rails": [], "smart": None, "nic_pf": [], "driver": "Unknown",
                  "cuda": "Unknown", "rate": state.rate, "caps": {}}

    if active_nics_only:
        sample["nics"] = [n for n in sample.get("nics", []) if n.get("up")]

    try:
        if page_num == 2:
            built = pages.build_page2(sample, tier, draw_w, x0, height=height_hint or h)
        else:
            built = pages.build_page1(sample, tier, draw_w, x0, height=height_hint or h)
    except Exception:
        built = []

    try:
        panels.render(stdscr, built, colors_map)
    except Exception:
        pass


def main_loop(stdscr, state, rate):
    import curses
    curses.start_color()
    curses.use_default_colors()
    curses.curs_set(0)
    stdscr.nodelay(True)

    if curses.COLORS >= 256:
        for slot, code in term.PALETTE_256.items():
            curses.init_pair(slot, code, -1)
    else:
        basic = {"GREEN": curses.COLOR_GREEN, "CYAN": curses.COLOR_CYAN, "WHITE": curses.COLOR_WHITE,
                 "RED": curses.COLOR_RED, "YELLOW": curses.COLOR_YELLOW}
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

    active_nics_only = False
    while True:
        try:
            stdscr.erase()
            h, _ = stdscr.getmaxyx()
            render_dashboard(stdscr, colors, state, active_nics_only, height_hint=h, page_num=state.page)
            stdscr.refresh()
        except Exception:
            pass

        start_wait = time.time()
        while time.time() - start_wait < rate:
            ch = stdscr.getch()
            if ch == ord('q'):
                return
            if ch == ord('t'):
                term.USE_FAHRENHEIT = not term.USE_FAHRENHEIT
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
            time.sleep(0.05)


def main():
    argv = sys.argv[1:]
    opts = _parse_args(argv)
    if opts["help"]:
        print(HELP_TEXT)
        return

    state = State(opts["rate"])
    state.page = opts["page"]

    if opts["loop"]:
        import curses
        try:
            import locale
            locale.setlocale(locale.LC_ALL, "")
        except Exception:
            pass
        try:
            curses.wrapper(main_loop, state, opts["rate"])
        except KeyboardInterrupt:
            pass
    else:
        v = term.VirtualCurses()
        colors = {i: i for i in term.PALETTE_256}
        render_dashboard(v, colors, state, page_num=state.page)
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
