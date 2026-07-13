# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

SPARK-SMI is a terminal system monitor (TUI) for the NVIDIA DGX Spark (GB10 Grace Blackwell) — hybrid ARM CPU clusters, unified CPU+GPU memory, and MT2910 200G NICs. It is Linux/aarch64-only at runtime: it reads `/sys/class/net/*/speed`, uses `psutil.sensors_temperatures()`, and live mode needs `curses`. It will not run meaningfully on Windows/macOS dev machines — changes there can be reviewed but not exercised.

## Commands

```bash
pip install -r requirements.txt   # deps: psutil, nvidia-ml-py
python spark-smi.py               # snapshot mode: render once, print ANSI to stdout
python spark-smi.py -l            # live mode: curses TUI, 1s refresh (q=quit, t=°C/°F, u=GiB/GB)
```

There are no tests and no linter configured. Packaging is setuptools via `pyproject.toml`; the pip entry point is `spark-smi = spark_smi.__main__:main`.

## Architecture

All real code lives in one file: `spark_smi/_core.py`. The other Python files are shims:
- `spark_smi/__main__.py` executes `_core.py` via `runpy.run_path(..., run_name="__main__")` — so the `if __name__ == "__main__":` block at the bottom of `_core.py` **is** the program entry point, even when installed via pip. Top-level code in `_core.py` runs on import (e.g. the module-level `monitor = NetMonitor()`).
- `spark-smi.py` (repo root) is a backward-compatible launcher that calls the same `main()`.

### Dual rendering backends, one draw function

`render_dashboard(stdscr, colors_map)` is the single UI function and is written against a duck-typed screen interface (`addstr(y, x, text, attr)`, `getmaxyx()`, `erase()`):

- **Live mode (`-l`)**: `stdscr` is a real curses window; `colors_map` maps 1–5 to curses color pairs (green, cyan, white, red, yellow).
- **Snapshot mode (default)**: `stdscr` is `VirtualCurses`, an in-memory character grid; `colors_map` is the identity map `{1:1, ..., 5:5}` and `VirtualCurses.render()` translates those ints to ANSI escape codes.

Any UI change must work through this shared interface — don't call curses-specific APIs inside `render_dashboard`.

Bar rendering (`make_bar`) has three auto-detected tiers, chosen once at import by `_detect_bar_style()`: `"eighths"` (sub-cell resolution via U+2588–U+258F, the default on UTF-8 terminals), `"blocks"` (whole-cell `█░` only, used when `TERM=linux` because the framebuffer console font lacks eighth-blocks), and `"ascii"` (legacy `[|||  ]`, forced by `--ascii`, `SPARK_SMI_ASCII`, or a non-UTF-8 stdout). All tiers return a string exactly `width` chars long so layout math is style-independent. Colors likewise upgrade to an xterm-256 palette (`PALETTE_256`) when the terminal advertises 256-color/truecolor, in both backends.

### Data collection and fallback chain

GPU data (`get_gpu_data`) tries NVML (`pynvml`) first, then falls back per-field to `nvidia-smi --query-gpu` CSV, then degrades to "N/A" — never crash on missing sensors. The `HAS_NVML` flag is set at import. Driver/CUDA info is cached after first fetch (`_CACHED_DRIVER_INFO`) to avoid shelling out every tick.

**Unified-memory special-casing**: if `_is_unified_soc()` matches the GPU name (`GB\d+` — GB10 today, future Grace-Blackwell SoCs) or memory reads as N/A/0, GPU memory is replaced with system RAM (`psutil.virtual_memory()`), "(Unified)" is appended to the name, and fan is forced to "None".

### Hardware detection (all topology is discovered at import, once)

- **CPU clusters** (`detect_cpu_clusters` → module-level `CPU_CLUSTERS`): cores are grouped by type — ARM MIDR (implementer, part) from sysfs, translated to marketing names via `ARM_PART_NAMES`; Intel hybrid via `/sys/devices/cpu_core|cpu_atom` masks; anything else is one "CPU Cores" cluster. Do NOT add `cpu_capacity` or max-frequency to the grouping signature: both vary per-core *within* a cluster (capacity calibration, favored-core boost) and fragment it — they're only used to rank clusters for P/E-style fallback labels. Cores of one type can be non-contiguous (the real DGX Spark is interleaved: A725 on 0–4 & 10–14, X925 on 5–9 & 15–19 — the per-core grid shows absolute core IDs for this reason).
- **Per-core grid**: column count adapts to thread count (4/6/8) and terminal width; the cluster separator sections only appear when 2+ core types exist.
- **NICs** (`NetMonitor._detect_interfaces`): physical interfaces enumerated from `/sys/class/net` (entries with a `device` symlink), psutil fallback elsewhere; labels come from the kernel driver name via `NIC_DRIVER_NAMES` (keep values ≤7 chars so labels fit the 12-char cell) plus negotiated speed; carrier state drives "Link Down". The `n` key in live mode filters to connected NICs (`SHOW_ACTIVE_NICS_ONLY`); rows wrap when NICs exceed one row's width.
  - **Byte counters**: netdevs with an RDMA device (matched via `/sys/class/infiniband/<hca>/device/net/`) are read from the HCA's per-port `port_rcv_data`/`port_xmit_data` counters — these are in **4-byte-word units** (IB spec, so ×4) and, unlike kernel netdev stats, they count RoCE/RDMA traffic (NCCL, GPUDirect) that bypasses the CPU. Everything else falls back to psutil netdev counters. Negative deltas (counter reset on driver reload) clamp to 0.
  - **Physical grouping**: netdevs are grouped by PCI slot (address minus the function digit), so both CX-7 ports render as one row — label `MLX5 2x200G`, throughput summed against summed link speed. A down port reads speed 0 and drops out of the capacity sum (label becomes `MLX5 200G`); "Link Down" appears only when every port in the group is down. Non-PCI devices (USB NICs) group by themselves.
- `MAX_WIDTH = 110` caps the dashboard width; content is centered in wider terminals.

### Other conventions

- Unit/temperature toggles are module globals (`USE_FAHRENHEIT`, `USE_DECIMAL_UNITS`) flipped by keypress in `main_loop`.
- Errors are swallowed broadly (`except: pass`) by design — the dashboard must keep rendering with degraded data rather than crash.
- CLI args are parsed by hand (`"-l" in sys.argv`), not argparse, despite the import. The README documents a `-n <rate>` flag that is **not** implemented; `REFRESH_RATE` is a constant.
- Version is declared in three places and currently disagrees: `pyproject.toml` + `spark_smi/__init__.py` (3.6.0) vs `VERSION` in `_core.py` ("3.5.7-stable", shown in the footer). Keep them in sync when bumping.
