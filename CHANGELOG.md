# Changelog

All notable changes to SPARK-SMI are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [4.0.0] — 2026-08-01

A ground-up rebuild. 3.x was a single-file dashboard written against one
machine; 4.0 is a package that discovers the hardware in front of it and
builds its layout from what actually exists. Same package name, same
`spark-smi` command — `pip install -U spark-smi` and everything 3.x showed
is still there, on page 1.

### Added

- **Four pages** instead of one screen. `1` overview, `2` advanced (per-GPU
  detail, PCIe link state, per-PF RDMA counters, thermals, NVMe SMART, power
  rails), `3` cluster, `4` fabric validation.
- **Cluster mode** — `--serve` turns a node into a read-only sample agent
  (`GET /sample`, `GET /healthz`, optional `$SPARK_SMI_TOKEN`), and
  `--cluster host,host,...` shows every node on one screen: a sections view
  up to 8 nodes, a fleet matrix plus alerts panel beyond that. `↑`/`↓` and
  `Enter` drill into any node. Also `--json` for one sample to stdout.
- **Fabric validation (page 4)** — confirm-gated RDMA bandwidth and latency
  tests between nodes, per rail, in three modes (single pair, all-pairs
  sweep, all-nodes burst). Live braille bandwidth chart, NIC ASIC
  temperature correlated against sustained load, all-pairs
  bandwidth/latency matrix, results persisted to
  `~/.local/share/spark-smi/fabric-tests.jsonl`.
- **Power and clock knobs (page 2)** — GPU power limit, SM/memory clock
  locks, clock reset, persistence mode, and `spbm` board rails
  (`PL1`/`PL2`/`SYSPL1`/`SYSPL2`). Every write is gated behind an explicit
  `y`/`N` confirm that now carries a hardware-write warning; snapshot mode
  can never write.
- **PCIe link monitoring** — every endpoint under `/sys/bus/pci/devices`,
  reporting negotiated generation/width against the slot-effective maximum.
  Flags a GPU stuck at low generation *under load* (a known power-safety
  failure mode) while treating idle downtrain as the normal thing it is.
- **10 color themes** cycled live with `c`, or `--theme <name>` /
  `$SPARK_SMI_THEME`. `--theme list` prints them.
- **High-resolution rendering** — sub-cell eighth-block bars, braille
  charts, and a 256-color palette, each auto-detected with graceful
  fallbacks (`blocks` on framebuffer consoles, `--ascii` for anything else).
- **Density tiers** — the layout re-evaluates every frame against terminal
  width (compact / standard / wide) and degrades by dropping content in a
  defined order when height is short. Resizing is live; no restart.
- **Help overlay** (`?`) documenting every binding, and `-n <secs>` to set
  the refresh rate.

### Changed

- Restructured from a single `_core.py` into a package (`term`,
  `collectors`, `panels`, `pages`, `knobs`, `cluster`, `fabtest`, `app`)
  with one screen-agnostic panel engine shared by the curses and snapshot
  backends.
- **Capability-driven rendering.** Collectors probe support once into a
  `.caps` dict and pages consult it. A sensor that doesn't exist produces no
  row, rather than a misleading `N/A` in a field that looks live — the GB10
  has no fan tachometer, so the field is simply absent there and reappears
  on hardware that has one.
- **Universal hardware detection.** CPU clusters are grouped by real silicon
  type (ARM MIDR, Intel hybrid masks); NICs are named from PCI vendor/device
  IDs with a `pci.ids` lookup and driver-name fallback, and multi-port cards
  group by PCI slot; storage, thermal zones, and hwmon devices are
  enumerated rather than assumed. Unlabeled thermal zones become
  Zone-A/Zone-B/…
- Unified-memory SoCs (GB10) show system RAM in place of GPU memory, marked
  `(Unified)`.
- `requires-python` raised to 3.8 (3.6/3.7 were never tested and the code
  uses 3.7+ APIs).

### Fixed

- **RDMA traffic is now visible.** RoCE/NCCL/GPUDirect bypasses the kernel
  netdev counters that `psutil`, `ifconfig`, and 3.x all read, so a
  saturated fabric link reported near-zero. Rates now come from the HCA's
  own `port_rcv_data`/`port_xmit_data` hardware counters where an RDMA
  device exists.
- `gpu-alloc` summed per-process memory across *all* GPUs, including
  discrete cards that allocate from their own VRAM — an RTX 3090 alongside a
  GB10 reported 122.1 GiB allocated on a 121.7 GiB machine.
- A fabric test starting in live mode blanked the screen: the live chart
  series carried raw sampler tuples where the renderer expected floats, and
  the crash guard swallowed it. Page-4 build failures now render a visible
  one-line error instead of nothing.
- Two hardcoded assumptions from the development cluster: rails per node
  were capped at 4, and the page-4 bandwidth chart scaled against a fixed
  200 Gb/s. Both now come from detected hardware.
- Cluster page: node names were truncated to 10 characters, every GPU row
  printed the node's unified allocation (so a 24 GB card read `alloc
  101.7G`), and the FABRIC title named whichever node's NIC sorted first.
- NIC naming consulted `pci.ids` only for *unknown* vendors, so a known
  vendor with an unlisted device fell through to raw hex
  (`Mellanox 0x1013` instead of `Mellanox ConnectX-4`).
- Mellanox ASIC thermal rows were labeled `cx7` unconditionally, mislabeling
  ConnectX-4/6 hardware.
- CPU frequency read only `/sys/.../cpufreq`, reporting a flat `0.00 GHz` on
  platforms that don't expose it (EPYC under Ubuntu 24.04); falls back to
  `/proc/cpuinfo`.
- The per-GPU PCIe row judged degradation against the device's own maximum
  rather than the slot's, labeling an M.2-fed GPU `(idle downtrain)` while
  the PCIE LINKS panel called the same link `ok`.
- The help overlay omitted page 3 entirely and silently truncated bindings
  on short terminals; it now spills into two columns.
- The header rendered a hardcoded `SPARK-SMI 2.0` regardless of version.
- Off-platform: GPU process names showed the pid twice on Windows, and
  several panels printed Linux-only captions or empty boxes.
- Live mode prints an install hint instead of a traceback when `curses` is
  missing, and terminal resizes no longer crash the draw.

### Security

- The sample agent (`--serve`) is read-only and has no code path to any
  write. `$SPARK_SMI_TOKEN`, when set, is required on `/sample`.
- Knobs are always local-only: drilling into a remote node shows its data
  read-only.

---

## [3.6.0] — 2026-04-19

### Added
- MT2910 200G and Realtek 10G NIC bandwidth monitoring.
- Demo GIF, badges, and hardware table in the README.

### Fixed
- Terminal resize crash in live mode; added a `KEY_RESIZE` handler.
- Restored driver and CUDA version to the footer.

## [3.4.0]

### Fixed
- GB10 power reporting, by reverting to CSV parsing.

## [3.2.0]

- Initial public release.

[4.0.0]: https://github.com/chappa-ai-llc/spark-smi/releases/tag/v4.0.0
[3.6.0]: https://github.com/chappa-ai-llc/spark-smi/releases/tag/v3.6.0
