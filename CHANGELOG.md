# Changelog

All notable changes to SPARK-SMI are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [4.3.0] — 2026-11-24

Page 5: a GPU task manager.

### Added

- **Page 5 — GPU tasks** (`5`, `--page 5`): every process holding a GPU
  context as a tree, one root per GPU, grouped by the session it lives in
  (tmux pane, screen, docker container, systemd unit, ssh login, terminal),
  with orphaned processes flagged. Live mode adds `Enter`/`o` to open the
  session (tmux/screen attach, journal or docker log tail), `e` to end a
  process or session through its own manager, `k` to kill it, and `a` to
  also list bare `/dev/nvidia*` holders. Ending anything asks `y`/`N`
  first; any other key cancels. Snapshot mode is read-only, and page 5
  always shows the local machine, also in cluster mode (#4).
- Unit tests for page 5's session grouping, tree order and confirm gate.
- README: a page-5 screenshot. `--shot` draws page 5's `▾` GPU markers as
  shapes, like the other markers, so pictures don't depend on the font.

### Fixed

- `--size COLS` without ROWS and without `--shot` crashed with a
  `TypeError`; the picture now keeps the terminal's height (#12).
- `?` opens the help overlay on every page; the README listed pages 1 and
  2 only.
- `--ascii` left box-drawing and block glyphs in the header (the `│`
  separators and the `▌1▐` page tab) and on page 4's chart axis; they now
  switch to `|`, `[1]` and `+` with the frames.

### Changed

- README images re-captured with spark-smi 4.2.0 (mirrored history, graded
  graphs, measured nord theme): the demo and resize GIFs and the page 1, 2
  and 3 screenshots, pages 1 and 2 written by `--shot`, page 3 by the `S`
  key.

## [4.2.0] — 2026-11-24

A visual upgrade: mirrored traffic history, graded graphs, themes measured
for contrast on dark and light terminals, and built-in SVG screenshots.

### Added

- `--shot FILE.svg` writes a page as an SVG picture instead of printing it:
  the complete page at any width (`--size COLS[xROWS]`), with frames, bars
  and braille graphs drawn as shapes so they line up in any viewer, and text
  left selectable. `--warmup SECS` samples first so history graphs have
  data. In live mode, `S` saves the current screen to the current folder.
  Standard library only; a page is about 20-45 KB. Idea from tuios's
  screenshot export (MIT, ideas only, no code copied).
- Themes readable on light terminals. spark-smi asks the terminal for its
  background (OSC 11, then `$COLORFGBG`) or takes `--background
  dark|light` / `$SPARK_SMI_BACKGROUND`, and measures the theme against it.
- `--theme terminal` draws with the terminal's own 16 colours.

- Mirrored traffic history on page 1: when the terminal has rows to spare,
  each NETWORK port shows download rising above a baseline and upload
  hanging below it, each half scaled to its own recent peak (never below 1%
  of the link), with the current rate, peak and average beside it. STORAGE
  rows do the same for reads and writes. Short terminals keep one-line
  rows. Idea from tuios's mirrored chart, which credits iptop (both MIT;
  ideas only, no code copied).
- History graphs are graded: each cell brightens with its value in five
  steps mixed from the theme's graph colour, and the window's peak is
  printed beside the graph. Idea from tuios's chart library (MIT, ideas
  only, no code copied).

### Changed

- History graphs use a fixed 0-100% scale instead of stretching to the
  window's own peak, so an idle 2% line no longer fills the graph like a
  busy one; any activity keeps the bottom dot lit.
- Every theme is measured, not hand-picked: text holds 4.5:1 contrast
  against the background (WCAG AA), graph marks 3:1, and frames and bar
  tracks a quiet 1.3-3:1 band. Frames now have their own colour (each
  theme's old frame colour where it already fit), so dim text no longer
  shares it and is easier to read; a few too-dark colours (such as nord's
  red) are a little brighter. A unit test holds every theme to the floors.
  Live mode no longer dims colours a 256-colour terminal shows exactly.
  Idea from tuios's measured palettes (MIT, ideas only, no code copied).

## [4.1.1] — 2026-11-24

TCP and RDMA on one port at the same time now read as their sum.

### Fixed

- Network rates count mixed kernel and RDMA traffic in full. Where a NIC
  reports the IEEE 802.3 MAC byte counters (mlx5 does, on Linux 5.13 or
  later), each physical port's rate comes from them, read over ethtool
  netlink with no root and no subprocess. They count every frame on the
  wire, so TCP and RDMA on one port add up instead of showing the larger of
  the two: on a DGX Spark, 49 Gb/s of iperf3 plus 61 Gb/s of `ib_write_bw`
  now reads 112 Gb/s, not about 60. PFs that share one physical port read
  the same counter and count once. The per-netdev rule stays the floor and
  the fallback. Page 2's per-PF rows use the port counter only where a port
  has a single PF. The NETWORK caption reads **port counters, RDMA +
  kernel** when every RDMA-backed port has them (#11).

### Changed

- README images re-captured with spark-smi 4.1.0 and placeholder node names
  (`--node-name`), so none shows a real host name: the demo and resize GIFs
  and the page 1, 2 and 3 screenshots (#9). The page 4 and three-node
  screenshots are removed until they can be re-captured the same way; their
  descriptions stay.

## [4.1.0] — 2026-11-24

A display name in place of the hostname, so shared screenshots need not show
real host names.

### Added

- `--node-name NAME` and `$SPARK_SMI_NODE_NAME` show NAME instead of the
  machine's hostname in page 1's header, in the `node` field of `--json` and
  `--serve`'s `/sample` and `/healthz`, and so on every node's row on pages 3
  and 4. The flag wins over the variable; with neither, the hostname shows as
  before. A name is 1-63 letters, digits, `-` or `_`; anything else stops
  spark-smi with the reason. The cluster list, local-node detection and
  fabric-test connections keep using real host names (#9).

### Fixed

- `--help` lists page 3 under `--page`, as the README already did.
- The README's HCA-counter section said an RDMA run reads low or zero where
  the HCA counters read 0. Measured since: on that build they count RDMA
  exactly and only miss kernel traffic, so page 4's RDMA tests are right.
  Mixed TCP + RDMA on one port reads as the larger of the two (#11).

## [4.0.4] — 2026-11-24

A fix for network rates that read 0 on some DGX Spark builds.

### Fixed

- NETWORK (page 1), FABRIC (page 3) and the per-port rates (page 2) read
  `0 Mb/s` during real traffic where the driver leaves every HCA port counter
  at 0 (seen with kernel `7.0.0-1025-nvidia` and ConnectX-7 firmware 28.50).
  Each direction now takes the larger of the HCA and kernel netdev deltas, so
  TCP traffic always shows, and RDMA still counts where the HCA counters work
  (#10).

### Changed

- While no HCA counter has moved, page 1's NETWORK caption reads "HCA
  counters read 0, kernel traffic only" instead of claiming RDMA is counted.
- Page 2's per-port columns are named `RX` and `TX` (were `RDMA RX` and
  `RDMA TX`), since they now show port traffic of either kind.
- The README's counter section names the right directory
  (`ports/<n>/counters/`, not `hw_counters/`).

## [4.0.3] — 2026-11-24

A documentation release.

### Changed

- The README's cluster and fabric-test examples, and the code comments, use
  placeholder host names (`node-1`, `example.com`) and a placeholder address.
- The release docs and scripts describe the tag-driven release runner in
  general terms.

## [4.0.2] — 2026-11-24

A hardening release: the cluster shared secret can come from a file.

### Added

- `$SPARK_SMI_TOKEN_FILE` names a file holding the `--serve`/`--cluster`
  shared secret, such as a Docker secret under `/run/secrets` or a systemd
  `LoadCredential` file. Trailing whitespace is stripped (#8).

### Changed

- `--serve` and `--cluster` refuse to start when `$SPARK_SMI_TOKEN_FILE` is
  set but unreadable or empty, or when both token variables are set, instead
  of running without the token. `$SPARK_SMI_TOKEN` still works; `--serve`
  then suggests moving the token to a file (#8).

## [4.0.1] — 2026-11-21

A maintenance release: a tag on `main` now publishes the release, and the
license and dependency metadata are corrected.

### Added

- `deploy/release.sh`: publishes a tagged release to PyPI and GitHub, with its
  build tools pinned by hash. A release runner calls it on each `vX.Y.Z` tag.
  It uploads with the standard library rather than `twine`, so it runs where
  native extensions cannot load (#6).

### Changed

- `requirements.txt` pins psutil 7.2.2 and nvidia-ml-py 13.615.71 by hash,
  generated from the new `requirements.in`, so advisory scanners can check
  them (#5). `pip install spark-smi` still takes the newest versions.
- Package metadata declares the license as the SPDX expression `MIT` with
  `license-files`, replacing the license table and classifier that setuptools
  stops accepting in February 2027. Building from source now needs
  setuptools 77 or newer (pip fetches it on its own).

### Fixed

- README: the Python badge said 3.6+; the package requires 3.8+.
- `LICENSE` names its copyright holder, chappa-ai-llc, in place of a
  leftover placeholder (#7).

### Security

- Security pass: no known advisory affects any pinned package, checked
  against OSV.dev on 2026-11-21. That covers the runtime dependencies
  (psutil 7.2.2, nvidia-ml-py 13.615.71) and the release tools (build 1.6.1,
  setuptools 84.2.1, wheel 0.48.0, packaging 26.3, pyproject-hooks 1.3.3).
  No Java anywhere in the package (CVE-2026-114938 does not apply).
- The release tools' lock is renamed `deploy/requirements-release.txt`
  (was `release-requirements.txt`), so the nightly dependency
  scan, which reads `requirements*.txt`, now covers it too.

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

[4.3.0]: https://github.com/chappa-ai-llc/spark-smi/releases/tag/v4.3.0
[4.2.0]: https://github.com/chappa-ai-llc/spark-smi/releases/tag/v4.2.0
[4.1.1]: https://github.com/chappa-ai-llc/spark-smi/releases/tag/v4.1.1
[4.1.0]: https://github.com/chappa-ai-llc/spark-smi/releases/tag/v4.1.0
[4.0.4]: https://github.com/chappa-ai-llc/spark-smi/releases/tag/v4.0.4
[4.0.3]: https://github.com/chappa-ai-llc/spark-smi/releases/tag/v4.0.3
[4.0.2]: https://github.com/chappa-ai-llc/spark-smi/releases/tag/v4.0.2
[4.0.1]: https://github.com/chappa-ai-llc/spark-smi/releases/tag/v4.0.1
[4.0.0]: https://github.com/chappa-ai-llc/spark-smi/releases/tag/v4.0.0
[3.6.0]: https://github.com/chappa-ai-llc/spark-smi/releases/tag/v3.6.0
