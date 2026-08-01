# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

SPARK-SMI is a terminal system monitor (TUI) born on the NVIDIA DGX Spark (GB10 Grace Blackwell) and generalized in 2.0: hardware is discovered at runtime (CPU clusters, GPUs incl. eGPUs, NICs by PCI ID, disks, thermal zones) and the layout builds itself from what exists — no sensor, no row. Full fidelity needs Linux (sysfs, hwmon, curses for live mode); on Windows/macOS dev machines snapshot mode still renders a degraded psutil+nvidia-smi view, which is enough to exercise layout changes but not the Linux collectors.

## Commands

```bash
pip install -r requirements.txt   # deps: psutil, nvidia-ml-py
python spark-smi.py               # snapshot mode: render once, print ANSI to stdout
python spark-smi.py -l            # live mode: curses TUI, 1s refresh (q=quit, t=°C/°F, u=GiB/GB, ?=help)
python spark-smi.py -n 2 -l       # live mode at a 2s refresh rate instead of the 1s default
python spark-smi.py --page 2      # snapshot the advanced page instead of the overview
python spark-smi.py --ascii       # force plain-ASCII bars/frames regardless of terminal detection
python spark-smi.py --theme nord  # one of 10 themes; `c` cycles them in live mode
python spark-smi.py --serve       # read-only sample agent on :8817 (GET /sample, /healthz)
python spark-smi.py -l --cluster a,b,c   # pages 3 (fleet) and 4 (fabric tests) need this
python spark-smi.py --json        # one sample as JSON to stdout, then exit
```

There are no tests and no linter configured — verification is by rendering
against real hardware over ssh (see `scratchpad/`, gitignored, for the
deploy/record helpers). Packaging is setuptools via `pyproject.toml`; the pip
entry point is `spark-smi = spark_smi.__main__:main`.

Note for anything user-facing: the DGX Spark's own Ubuntu 24.04 marks system
Python externally managed (PEP 668), so a bare `pip install spark-smi` is
REFUSED there. pipx or a venv is the documented path.

## Architecture

2.0 replaced the original single-file `_core.py` with a small package. Each
module has one job:

```
spark_smi/
  __init__.py     VERSION (single source of truth; pyproject.toml's [project]
                  version is kept in sync with this literal by hand — dynamic
                  versioning from this file is not wired up) and __version__.
  term.py         Terminal capability detection (BAR_STYLE eighths/blocks/
                  ascii, 256-color palette), VirtualCurses, make_bar/seg_bar/
                  core_strip/sparkline, fmt_temp/fmt_mem/fmt_rate, knob_arrows.
  collectors.py   CpuCollector, MemoryCollector, GpuCollector, NetCollector,
                  DiskCollector, ThermalCollector, SmartCollector. Each probes
                  a `.caps` dict once at construction and exposes `.sample()`
                  returning plain dicts/lists — no rendering, no curses.
  panels.py       Screen-agnostic panel engine (below) — the only place frame
                  glyphs and row/segment drawing happen.
  pages.py        build_page1(state, tier, width, x0, height, sort_nics) and
                  build_page2(state, tier, width, x0, height, knob_ui) turn a
                  sampled state dict into a list of positioned panels.Panel
                  objects. build_help_overlay(term_w, term_h) builds the `?`
                  key's overlay panel, appended after the page so it paints
                  on top.
  knobs.py        Phase-4 page-2 power/clock knobs: build_registry(sample) is
                  a pure function describing what's writable this tick;
                  KnobUI holds the live-mode-only session state (selected
                  GPU/knob, pending value, confirm arm, toast) and its methods
                  are pure state transitions; apply_knob() is the only thing
                  that ever writes hardware, and only after KnobUI.confirm_yes().
  cluster.py      Cluster mode: the to_wire/from_wire sample codec (psutil
                  named tuples don't survive JSON), the read-only --serve
                  HTTP agent (GET /sample, GET /healthz, optional
                  $SPARK_SMI_TOKEN), and ClusterAggregator — a background
                  poller the render loop never blocks on. Also local-domain
                  inference (reverse-PTR first) so short cluster hostnames
                  resolve. Has NO code path that writes anything.
  fabtest.py      Page-4 fabric validation: rail discovery + subnet-matched
                  src<->dst pairing, FabricEngine (drives ib_write_bw/-lat
                  over BatchMode ssh, or iperf3 where there's no RDMA
                  device), FabTestUI (the same pure-state-transition +
                  confirm-arm shape knobs.py uses), and results persisted to
                  ~/.local/share/spark-smi/fabric-tests.jsonl. Measurements
                  come from HCA counters sampled during the run (median of
                  steady state), never from perftest's own summary line.
  app.py          main(), hand-rolled CLI parsing, the State class (owns the
                  long-lived collectors + history ring buffers), the snapshot
                  render path, and the curses main_loop/key handling. The
                  ONLY module allowed to `import curses`.
  __main__.py     from .app import main; main() — the runpy hack is gone.
```
`spark-smi.py` (repo root) is a tiny launcher: it puts the repo dir on
`sys.path` and calls `spark_smi.app.main()`.

### Dual rendering backends, one draw function

`render_dashboard(stdscr, colors_map, state, ...)` (app.py) is the single UI
entry point, built on top of `panels.render()`, both written against a
duck-typed screen interface (`addstr(y, x, text, attr)`, `getmaxyx()`,
`erase()`):

- **Live mode (`-l`)**: `stdscr` is a real curses window; `colors_map` maps
  the logical color slots to curses color pairs.
- **Snapshot mode (default)**: `stdscr` is `term.VirtualCurses`, an in-memory
  character grid; `colors_map` is the identity map and
  `VirtualCurses.render()` translates slot ints to ANSI escape codes.

Any UI change must work through this shared interface — don't call
curses-specific APIs inside `panels.py`, `pages.py`, or `render_dashboard`;
real curses calls belong only in `app.py`'s `main_loop`.

Bar rendering (`term.make_bar`) has three auto-detected tiers, chosen once at
import by `_detect_bar_style()`: `"eighths"` (sub-cell resolution via
U+2588–U+258F, the default on UTF-8 terminals), `"blocks"` (whole-cell `█░`
only, used when `TERM=linux` because the framebuffer console font lacks
eighth-blocks), and `"ascii"` (legacy `[|||  ]`, forced by `--ascii`,
`SPARK_SMI_ASCII`, or a non-UTF-8 stdout). All tiers return a string exactly
`width` chars long so layout math is style-independent; `panels.py`'s frame
glyphs and `term.bar_brackets()`/`term.knob_arrows()` switch to ASCII
equivalents the same way. Colors likewise upgrade to an xterm-256 palette
(`term.PALETTE_256`) when the terminal advertises 256-color/truecolor, in
both backends.

### Panel engine (`panels.py`)

`pages.py` builds a plain list of `Panel(y, x, w, title, title_right, kind)`
objects once per frame — no drawing happens there. `kind='top'` opens a fresh
bordered frame; `kind='mid'` continues the previous panel's frame with a
`├─┤` separator instead of a new box (how CPU+MEMORY and NETWORK+STORAGE
share one border); `kind='plain'` is a borderless flowing line (header,
footer, the CLI's help overlay uses `'top'` instead since it needs its own
box). Each row is `(col, text, slot)` segments — `col=None` flows after the
previous segment — plus an optional `right` group flushed against the right
edge. `panels.render(screen, panels, colors_map)` draws the whole list in
order and clips to the screen's current bounds every call, so a resize
mid-frame can't crash the draw; panels later in the list draw over earlier
ones at the same coordinates, which is how the help overlay paints on top of
the already-built page.

### Capability model

Each collector probes hardware support **once**, into a `.caps` dict, e.g.
`GpuCollector` per GPU: `{fan, power_draw, power_limit, mem_local, pcie,
clocks}`. `mem_local=False` means unified memory (GB10 today: any GPU name
matching `_is_unified_soc()`'s `GB\d{2,3}` heuristic) — GPU memory is
replaced with `psutil.virtual_memory()`, "(Unified)" is appended to the name,
and `fan` is forced off. Pages consult `.caps` to include/exclude fields
outright on page 1, or show a dimmed "unsupported" note on page 2 — never a
bare misleading "N/A" in a field that looks like it should have a live
number. `knobs.build_registry()` extends this to *writability*: a knob only
enters the registry when its underlying API is both readable and has a valid
range/current value, and each entry carries a `reason` string for why it's
grayed out otherwise (e.g. "root required" for an unwritable `spbm`
`powerN_cap` file).

### Density tiers

`pages.tier_for_width(w)` re-evaluates every frame: `compact` (<84 cols),
`standard` (84–110), `wide` (>110, content capped at 160 and centered on
wider terminals). Height degradation, applied in `build_page1`/`build_page2`
when a `height` hint is given, drops content in this order: sparklines →
memory legend row → NIC rows collapse to one summary line.

### Data collection and fallback chain

GPU data (`GpuCollector.sample`) tries NVML (`pynvml`) first, then falls back
per-field to `nvidia-smi --query-gpu` CSV, then degrades to "N/A" — never
crash on missing sensors. `collectors.HAS_NVML` is set at import. Driver/CUDA
info is cached after first fetch to avoid shelling out every tick.

### Hardware detection (all topology is discovered once, at collector construction)

- **CPU clusters** (`collectors.detect_cpu_clusters`, called once by
  `CpuCollector.__init__`): cores are grouped by type — ARM MIDR
  (implementer, part) from sysfs, translated to marketing names via
  `ARM_PART_NAMES`; Intel hybrid via `/sys/devices/cpu_core|cpu_atom` masks;
  anything else is one "CPU Cores" cluster. Do NOT add `cpu_capacity` or
  max-frequency to the grouping signature: both vary per-core *within* a
  cluster (capacity calibration, favored-core boost) and fragment it —
  they're only used to rank clusters for P/E-style fallback labels. Cores of
  one type can be non-contiguous (the real DGX Spark is interleaved: A725 on
  0–4 & 10–14, X925 on 5–9 & 15–19 — the per-core grid shows absolute core
  IDs for this reason).
- **Per-core grid**: column count adapts to thread count and terminal width;
  cluster separator sections only appear when 2+ core types exist.
- **NICs** (`NetCollector._detect_interfaces` and friends): physical
  interfaces enumerated from `/sys/class/net` (entries with a `device`
  symlink), psutil fallback elsewhere; hardware labels come from PCI vendor/
  device IDs (`PCI_VENDOR_NAMES`, then `/usr/share/hwdata/pci.ids` via
  `_pci_ids_lookup`), then the kernel driver name (`NIC_DRIVER_NAMES`, kept
  ≤7 chars so labels fit the cell) as a last resort; carrier state drives
  "Link Down". The `n` key filters to connected NICs; the `s` key
  (page 1 only, `build_page1`'s `sort_nics` param) reorders rows by
  `max(rx_bps, tx_bps)` descending, recomputed fresh every frame, toggling
  back to detection order.
  - **Byte counters**: netdevs with an RDMA device (matched via
    `/sys/class/infiniband/<hca>/device/net/`) are read from the HCA's
    per-port `port_rcv_data`/`port_xmit_data` counters — these are in
    **4-byte-word units** (IB spec, so ×4) and, unlike kernel netdev stats,
    they count RoCE/RDMA traffic (NCCL, GPUDirect) that bypasses the CPU.
    Everything else falls back to psutil netdev counters. Negative deltas
    (counter reset on driver reload) clamp to 0. Rate math uses its own
    `time.time()` delta each collector keeps internally — it is NOT derived
    from `-n`'s configured refresh rate, so an inaccurate `-n` value never
    skews a rate reading, only how often one is taken.
  - **Physical grouping**: netdevs are grouped by PCI slot (address minus the
    function digit), so a multi-port card renders as one row per physical
    port with a `×N PF` marker, throughput summed against summed link speed
    (or the single port speed when PFs share one port). A down port drops out
    of the capacity sum; "Link Down" appears only when every port in the
    group is down. Non-PCI devices (USB NICs) group by themselves.
  - Page 2 adds per-PF detail (`NetCollector.sample_pf_detail`): RDMA RX/TX,
    CNP sent/handled, ECN marks, buffer discards from each HCA's
    `hw_counters`, and ASIC temperature from the NIC's own hwmon device.
- **Storage** (`DiskCollector`): every physical block device under
  `/sys/block` (loop/ram/dm skipped), model/size from sysfs, temperature
  from a matching hwmon (NVMe), R/W rates from `/proc/diskstats` deltas,
  used% from `statvfs` of the largest mounted partition.
- **Thermal zones** (`ThermalCollector`): every `/sys/class/thermal` zone
  plus every named `/sys/class/hwmon` device's temp channels. Zones sharing a
  generic type (unlabeled `acpitz` on GB10) are relabeled Zone-A/Zone-B/… in
  zone order. An hwmon named `spbm` (the out-of-tree DGX Spark board-power
  driver — see README's "Optional: spbm driver" section) gets its labeled
  channels listed first and its power rails exposed via `sample_power()` for
  the page-2 POWER RAILS panel and `knobs.py`'s rail knobs.
- Content width is capped (`pages.content_width`, 160 at the `wide` tier) and
  centered in wider terminals.

### Knob safety flow (page 2, live mode only)

`knobs.build_registry(sample)` is rebuilt fresh every render tick from the
current sample + `.caps` — it never holds state and never writes anything.
`app.py` constructs exactly one `knobs.KnobUI()` in `main_loop` (never in
`State`, which the snapshot path also uses — snapshot mode never constructs
a `KnobUI` and therefore never even builds a registry, guaranteeing it can't
write). Stepping a value (`←`/`→`) only edits `KnobUI`'s in-memory pending
value; only `Enter`/`R`/`X` arm a confirm prompt, and only pressing `y` at
that prompt calls `knobs.apply_knob()` — every other key (including wandering
into a different knob's key) cancels. `apply_knob()` is the sole function
that shells out to `nvidia-smi` or writes an `spbm` sysfs file, and every
backend it dispatches to is wrapped in its own try/except so one bad write
can't take the render loop down.

### Other conventions

- Unit/temperature toggles are module globals in `term.py`
  (`USE_FAHRENHEIT`, `USE_DECIMAL_UNITS`) flipped by keypress in `main_loop`.
- Errors are swallowed broadly (bare `except Exception: pass`) by design —
  the dashboard must keep rendering with degraded data rather than crash.
- CLI args are parsed by hand (`app.py`'s `_parse_args`), not argparse. `-n
  <secs>` sets the refresh rate end-to-end: it's the `rate` argument passed
  to `curses.wrapper(main_loop, state, rate)`, which drives the inner
  `while time.time() - start_wait < rate` redraw-wait loop directly, and is
  also stored on `State.rate` for the footer's `{rate:g}s` display in
  snapshot mode. It does not affect `NetCollector`/`DiskCollector` delta
  math, which times itself independently (see "Byte counters" above).
- Version lives in one place, `spark_smi/__init__.py`'s `VERSION`;
  `pyproject.toml`'s `[project] version` is kept in sync with that literal by
  hand (not dynamically read — dynamic versioning was judged not worth the
  build-backend complexity yet). `app.py` and `pages.py` both `from . import
  VERSION` (with a hardcoded fallback string if the import ever fails) —
  `pages.py`'s footer renders it as `v{VERSION}` alongside driver/CUDA/rate,
  and `app.py`'s `--help`/help-overlay text embeds it too. Keep the
  `__init__.py` literal and the `pyproject.toml` literal identical when
  bumping.
