# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

SPARK-SMI is a terminal system monitor (TUI) born on the NVIDIA DGX Spark (GB10 Grace Blackwell) and generalized in 2.0: hardware is discovered at runtime (CPU clusters, GPUs incl. eGPUs, NICs by PCI ID, disks, thermal zones) and the layout builds itself from what exists — no sensor, no row. Full fidelity needs Linux (sysfs, hwmon, curses for live mode); on Windows/macOS dev machines snapshot mode still renders a degraded psutil+nvidia-smi view, which is enough to exercise layout changes but not the Linux collectors.

## Commands

```bash
pip install -r requirements.txt   # deps: psutil, nvidia-ml-py, pinned by hash
python spark-smi.py               # snapshot mode: render once, print ANSI to stdout
python spark-smi.py -l            # live mode: curses TUI, 1s refresh (q=quit, t=°C/°F, u=GiB/GB, ?=help)
python spark-smi.py -n 2 -l       # live mode at a 2s refresh rate instead of the 1s default
python spark-smi.py --page 2      # snapshot the advanced page instead of the overview
python spark-smi.py --page 5      # GPU tasks page (read-only here; live mode adds open/end/kill)
python spark-smi.py --ascii       # force plain-ASCII bars/frames regardless of terminal detection
python spark-smi.py --theme nord  # one of 10 themes; `c` cycles them in live mode
python spark-smi.py --serve       # read-only sample agent on :8817 (GET /sample, /healthz)
python spark-smi.py -l --cluster a,b,c   # pages 3 (fleet) and 4 (fabric tests) need this
python spark-smi.py --json        # one sample as JSON to stdout, then exit
python spark-smi.py --page 2 --size 130 --warmup 5 --shot p2.svg   # page as SVG
```

The monitor's rendering has no tests and no linter is configured — verification is by
rendering against real hardware over ssh (see `scratchpad/`, gitignored, for
the deploy/record helpers). Unit tests cover the release script and a few
pure pieces (cluster token reader, node names, the network counter rules,
the ethtool netlink codec, themes, graphs, SVG shots, page 3's topology map,
page 5's session grouping and confirm gate, and the big-screen page 1): `python3 -m unittest discover -s deploy` and
`python3 -m unittest discover -s tests` (the second needs psutil installed). Packaging is setuptools via
`pyproject.toml`; the pip entry point is `spark-smi = spark_smi.__main__:main`.

Note for anything user-facing: the DGX Spark's own Ubuntu 24.04 marks system
Python externally managed (PEP 668), so a bare `pip install spark-smi` is
REFUSED there. pipx or a venv is the documented path.

## Releasing

A `vX.Y.Z` tag on `main` is the release: a release runner calls
`deploy/release.sh X.Y.Z` once per tag, with the PyPI and GitHub tokens in
files named by `PYPI_TOKEN_FILE` and `GITHUB_TOKEN_FILE` (issue #6).

1. Bump `VERSION` in `spark_smi/__init__.py` (and the fallback literals in
   `app.py`, `pages.py` and `cluster.py`), `version` in `pyproject.toml` and
   the README badge. Add `## [X.Y.Z]` and its link line to `CHANGELOG.md`.
   The script stops before the upload if any of these disagree with the tag.
2. Merge to `main`, because the README's images load from `main`.
3. Check it, tag it, push the tag:

   ```bash
   deploy/release.sh X.Y.Z --dry-run   # set TMPDIR to an exec-allowed dir if /tmp is noexec
   git tag vX.Y.Z
   git push origin vX.Y.Z
   ```

The run builds in a throwaway venv, checks the metadata, uploads over PyPI's
upload API with the standard library, publishes the GitHub release, then
confirms PyPI holds the same bytes. It does not use `twine`: the releaser's
temp directory cannot execute files, so twine's native dependencies (`nh3`,
`cryptography`) cannot load there. Keep the lock pure Python for the same
reason. PyPI versions are immutable: after a failed run, fix it and tag a
newer version, never re-tag.

`deploy/requirements-release.txt` pins the build tools by hash. After changing
the `.in` file, regenerate it from `deploy/` with pip-tools, on the same Python
minor version as the releaser (3.13):

```bash
pip-compile --generate-hashes --allow-unsafe --strip-extras --no-emit-index-url --output-file requirements-release.txt requirements-release.in
```

`requirements.txt` pins the runtime dependencies by hash for from-source
installs and for advisory scanners; `pyproject.toml` keeps them open for pip
users. After changing `requirements.in`, regenerate it the same way from the
repo root (drop `--allow-unsafe`, and use `requirements.in`/`requirements.txt`).

## Public repo

This repo, its Issues, commits and release notes are public. State problems
without naming the maintainer's private hosts, accounts, home paths, network
addresses or internal infrastructure; use placeholders such as `node-1` and
`example.com`. Private details go on the owner-only project board.

The board's source is the gitignored `scratchpad/board.json` (`.build-board.json`
points the client at it). Never commit that file. A fresh clone won't have it:
`build_board.py push` then stops with an error instead of emptying the board.
Rebuild it from the board's hosted copy first, because a push replaces the
phases and retires any file card the source leaves out.

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
                  objects (build_page5 draws the tasks tree it is handed;
                  pages 3-4 have their own builders). build_help_overlay(term_w, term_h) builds the `?`
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
                  token from read_token(): $SPARK_SMI_TOKEN_FILE, else
                  $SPARK_SMI_TOKEN), and ClusterAggregator — a background
                  poller the render loop never blocks on. Also local-domain
                  inference (reverse-PTR first) so short cluster hostnames
                  resolve. Has NO code path that writes anything.
  color.py        Colour maths: xterm-256 RGB, OKLab mix, WCAG contrast,
                  nearest xterm colour under a contrast floor. Stdlib only.
  shot.py         SVG pictures of a VirtualCurses grid (--shot, live `S`):
                  box/block/braille glyphs drawn as merged shapes, other
                  text as selectable <text>. Stdlib only.
  ethtool.py      Physical-port MAC byte counters over ethtool generic
                  netlink (stdlib socket only); port_byte_reader(iface)
                  returns None wherever the kernel/driver can't report them.
  fabtest.py      Page-4 fabric validation: rail discovery + subnet-matched
                  src<->dst pairing, FabricEngine (drives ib_write_bw/-lat
                  over BatchMode ssh, or iperf3 where there's no RDMA
                  device), FabTestUI (the same pure-state-transition +
                  confirm-arm shape knobs.py uses), and results persisted to
                  ~/.local/share/spark-smi/fabric-tests.jsonl. Measurements
                  come from HCA counters sampled during the run (median of
                  steady state), never from perftest's own summary line.
  tasks.py        Page-5 GPU task manager: TaskCollector (GPU pids from
                  NVML/nvidia-smi, optional /dev/nvidia* holders, each
                  grouped by session: docker > tmux/screen/zellij pane >
                  systemd unit > ssh > orphan > tty), flatten() (tree ->
                  rows), TaskUI (pure state transitions + y-only confirm,
                  the knobs.py shape), and end_process()/end_group()/
                  open_group_argv() -- the only code that signals or runs
                  a session manager. Live mode only constructs TaskUI.
  bigpage.py      Page 1's big-screen layouts (#14): layout_for(w, h,
                  graphs) picks large (200+ cols) / xl (300+) / tall (`g`
                  large, 120+), build_page1_large() fills the terminal
                  exactly -- every chart starts at a minimum height and
                  grows a row at a time (search cached on the page's
                  shape). Dial strip, charts with axes, POWER & THERMAL
                  and per-core heatmap at xl. Reads State's sample["long"]
                  (up to HIST_LONG samples, never sent over the wire).
  topomap.py      Page 3's TOPOLOGY map: build_graph() finds links from
                  the nodes' nic_pf networks (ip + prefix; 2 members = a
                  cable, 3+ = a switch bus), render() lays boxes out in one
                  or two rows, neighbours' rails across the gaps, other
                  links on tracks beneath, into rows of (text, slot) runs.
                  Pure drawing; ClusterUI.topo_cache keeps a silent node's
                  links. build_page3 calls it only with spare height.
  node.py         Node identity: hostname() (real short hostname — local-
                  member detection and fabtest's local-vs-ssh choice) and
                  display_name() (--node-name / $SPARK_SMI_NODE_NAME, else
                  the hostname — header, --json/--serve payload "node").
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
wider terminals). On page 1, `bigpage.layout_for()` takes over at 200+
columns and 45+ rows (the `g` key's graph mode can turn it off or extend
it down to 120 columns); see `bigpage.py`. History: State keeps
`HIST_LONG` (3600) samples per series but exposes only `HIST_LEN` (60) in
the sample's own `*history` keys -- the long buffers ride in
`sample["long"]`, built only when `state.want_long` (a screen is drawn),
and `cluster.to_wire()` drops them. Height degradation, applied in `build_page1`/`build_page2`
when a `height` hint is given, drops content in this order: sparklines →
memory legend row → NIC rows collapse to one summary line. The other way,
spare height opens mirrored history rows (`term.mirror_chart`, 4 rows each,
`pages.MIRROR_ROWS`): NICs first, then disks (needs rx/tx_history and
read/write_history, which `State.sample` keeps; snapshot only with --warmup).

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
    `/sys/class/infiniband/<hca>/device/net/`) are read from BOTH the HCA's
    per-port `port_rcv_data`/`port_xmit_data` counters — these are in
    **4-byte-word units** (IB spec, so ×4) and, unlike kernel netdev stats,
    they count RoCE/RDMA traffic (NCCL, GPUDirect) that bypasses the CPU —
    and the netdev counters; `NetCollector._rates()` takes the larger delta
    per direction, because some driver/firmware builds count only RDMA in
    the HCA counters, never kernel traffic (issues #10, #11).
    `caps["net"]["rdma_counting"]` turns True once any HCA counter reads
    non-zero. Everything else uses psutil netdev counters.
  - **Port MAC counters** (issue #11, `ethtool.py`): per physical port, the
    IEEE 802.3 `OctetsReceivedOK`/`OctetsTransmittedOK` read over ethtool
    generic netlink (`ETHTOOL_MSG_STATS_GET`, eth-mac group; stdlib socket,
    no subprocess, no root, ~0.2 ms) count RDMA and kernel traffic alike.
    PFs sharing a `phys_switch_id`+`phys_port_name` report the SAME counter,
    so readers are keyed by port ("unit"), read once per port, and
    `NetCollector._combine()` counts each port once as max(MAC delta, sum of
    its PFs' per-netdev rates). Page 2 uses a port's MAC counter only for a
    PF that is that port's sole PF. `caps["net"]["port_counting"]` is True
    when every RDMA-backed port has one; page 1's caption picks from
    port_counting → rdma_counting → "kernel traffic only".
  - Negative deltas (counter reset on driver reload) clamp to 0. Rate math uses its own
    `time.time()` delta each collector keeps internally — it is NOT derived
    from `-n`'s configured refresh rate, so an inaccurate `-n` value never
    skews a rate reading, only how often one is taken.
  - **Physical grouping**: netdevs are grouped by physical port
    (`phys_switch_id`+`phys_port_name`), else by PCI slot (address minus the
    function digit), so a multi-port card renders as one row per physical
    port with a `×N PF` marker, throughput summed against summed link speed
    (or the single port speed when PFs share one port). A down port drops out
    of the capacity sum; "Link Down" appears only when every port in the
    group is down. Non-PCI devices (USB NICs) group by themselves.
  - Page 2 adds per-PF detail (`NetCollector.sample_pf_detail`): RX/TX (same
    two-source rule),
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

- **Colour slots** (term.py's table): 1-9 as before, 10 frames and bar
  brackets, 11-15 the graph ramp. Slot 6 is dim TEXT now, not the frame.
  `term.build_palette()` derives the live palette from THEMES (design
  intent) by measuring against the background (`term.BACKGROUND`, set by
  `detect_background`/`set_background`); never hand-edit a palette to pass
  a floor -- `tests/test_themes.py` enforces the floors.
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
