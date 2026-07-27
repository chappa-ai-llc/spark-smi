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

from . import collectors
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
  -p, --page <n>   which page to render in snapshot mode: 1 overview (default)
                   or 2 advanced (GPU detail, NIC/thermal/SMART panels)
  --theme <name>   color theme (default: spark; or $SPARK_SMI_THEME)
  --theme list     print the available theme names, one per line, and exit
  --ascii          force plain-ASCII bars/frames (no UTF-8 box drawing)
  -h, --help       show this help and exit

Themes: {", ".join(term.THEMES)}, cycled live with the 'c' key.
$SPARK_SMI_THEME sets the default when --theme isn't passed; an unknown name
prints the valid list and exits 1.

Snapshot mode (default, no -l) renders once and prints ANSI to stdout.

Live-mode keys: q quit  ·  t toggle C/F  ·  u toggle GiB/GB  ·  1/2 page
                n active-NICs-only  ·  s sort NICs by rate  ·  c cycle theme
                ? help overlay
                page 2: tab select GPU  ·  P/C/M/R/X knobs
"""


def _parse_args(argv):
    opts = {"loop": False, "rate": DEFAULT_REFRESH_RATE, "help": False, "page": 1,
            "theme": None, "theme_list": False}
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
        elif a == "--theme":
            i += 1
            if i < len(argv):
                if argv[i] == "list":
                    opts["theme_list"] = True
                else:
                    opts["theme"] = argv[i]
        elif a == "--themes":
            opts["theme_list"] = True
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


def render_dashboard(stdscr, colors_map, state, active_nics_only=False, height_hint=None, page_num=1,
                      knob_ui=None, sort_nics=False, show_help=False, theme_toast=None):
    """Single UI entry point for both backends. Builds the requested page
    (1 overview, 2 advanced) and draws it.

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
    switch, or None the rest of the time."""
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

    knob_ctx = None
    if knob_ui is not None and page_num == 2:
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
                                       theme_toast=theme_toast)
        else:
            built = pages.build_page1(sample, tier, draw_w, x0, height=height_hint or h, sort_nics=sort_nics,
                                       theme_toast=theme_toast)
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


def main_loop(stdscr, state, rate):
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
    while True:
        if theme_toast_ticks > 0:
            theme_toast_ticks -= 1
            if theme_toast_ticks <= 0:
                theme_toast_text = None
        theme_toast = (theme_toast_text, 9) if theme_toast_text else None
        try:
            stdscr.erase()
            h, _ = stdscr.getmaxyx()
            render_dashboard(stdscr, colors, state, active_nics_only, height_hint=h, page_num=state.page,
                              knob_ui=knob_ui, sort_nics=sort_nics, show_help=show_help,
                              theme_toast=theme_toast)
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
            if state.page == 2 and knob_ui.confirming:
                if ch != -1:
                    if ch in (ord('y'), ord('Y')):
                        knob_ui.confirm_yes(knob_ui.last_registry)
                    else:
                        knob_ui.cancel()
                    break
            elif state.page == 2 and ch == 9:  # Tab
                knob_ui.select_next_gpu(knob_ui.n_gpus)
                break
            elif state.page == 2 and ch in (ord('P'), ord('p')):
                knob_ui.focus_power_cycle(knob_ui.last_registry)
                break
            elif state.page == 2 and ch == ord('C'):
                # Capital-only: lowercase 'c' is the global theme-cycle key
                # (handled far above, before this whole Phase 4 block, so it
                # never reaches here) -- keeping this case-sensitive is what
                # lets 'c' cycle themes on page 2 while 'C' still focuses
                # the sm clock-lock knob.
                knob_ui.focus_clock(knob_ui.last_registry, "sm")
                break
            elif state.page == 2 and ch in (ord('M'), ord('m')):
                knob_ui.focus_clock(knob_ui.last_registry, "mem")
                break
            elif state.page == 2 and ch in (ord('R'), ord('r')):
                knob_ui.arm_reset(knob_ui.last_registry)
                break
            elif state.page == 2 and ch in (ord('X'), ord('x')):
                knob_ui.arm_persist(knob_ui.last_registry)
                break
            elif state.page == 2 and ch == curses.KEY_LEFT:
                knob_ui.step(knob_ui.last_registry, -1)
                break
            elif state.page == 2 and ch == curses.KEY_RIGHT:
                knob_ui.step(knob_ui.last_registry, 1)
                break
            elif state.page == 2 and ch in (curses.KEY_ENTER, 10, 13):
                knob_ui.enter(knob_ui.last_registry)
                break
            elif state.page == 2 and ch == 27:  # Esc
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
