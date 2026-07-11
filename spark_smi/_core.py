import time
import subprocess
import psutil
import argparse
import shutil
import re
import platform
import os
import sys
from datetime import datetime

# --- NVML Import Strategy ---
# Force-check for pynvml, but don't crash if the venv isn't active
try:
    import pynvml
    pynvml.nvmlInit()
    HAS_NVML = True
except Exception:
    HAS_NVML = False

# --- Configuration ---
VERSION = "3.5.7-stable"
REFRESH_RATE = 1.0
MAX_WIDTH = 110

# Toggles
USE_FAHRENHEIT = False
USE_DECIMAL_UNITS = False
SHOW_ACTIVE_NICS_ONLY = False

# --- Terminal Capability Detection ---
# Three bar-rendering tiers:
#   "eighths" - sub-cell resolution via U+2588..U+258F (8 steps per cell)
#   "blocks"  - whole-cell blocks only; the Linux framebuffer console font
#               (CP437 heritage) has "█" and "░" but NOT the eighth-blocks
#   "ascii"   - legacy [|||   ] style for non-UTF-8 locales or --ascii
def _detect_bar_style():
    if "--ascii" in sys.argv or os.environ.get("SPARK_SMI_ASCII"):
        return "ascii"
    enc = (getattr(sys.stdout, "encoding", "") or "").lower()
    if "utf" not in enc:
        return "ascii"
    if os.environ.get("TERM", "") == "linux":
        return "blocks"
    return "eighths"

BAR_STYLE = _detect_bar_style()

def _detect_256color():
    term = os.environ.get("TERM", "")
    return "256color" in term or os.environ.get("COLORTERM") in ("truecolor", "24bit")

HAS_256COLOR = _detect_256color()

# Logical color slots (1=good, 2=accent, 3=text, 4=bad, 5=warn) mapped to
# xterm-256 indices when available, else the basic 8-color equivalents.
PALETTE_256 = {1: 42, 2: 45, 3: 252, 4: 196, 5: 214}

# --- CPU Topology Detection ---
# (implementer, part) from MIDR_EL1 -> marketing name. ARM Ltd is 0x41.
ARM_PART_NAMES = {
    (0x41, 0xd03): "Cortex-A53",  (0x41, 0xd05): "Cortex-A55",
    (0x41, 0xd07): "Cortex-A57",  (0x41, 0xd08): "Cortex-A72",
    (0x41, 0xd09): "Cortex-A73",  (0x41, 0xd0a): "Cortex-A75",
    (0x41, 0xd0b): "Cortex-A76",  (0x41, 0xd0d): "Cortex-A77",
    (0x41, 0xd41): "Cortex-A78",  (0x41, 0xd44): "Cortex-X1",
    (0x41, 0xd46): "Cortex-A510", (0x41, 0xd47): "Cortex-A710",
    (0x41, 0xd48): "Cortex-X2",   (0x41, 0xd4d): "Cortex-A715",
    (0x41, 0xd4e): "Cortex-X3",   (0x41, 0xd80): "Cortex-A520",
    (0x41, 0xd81): "Cortex-A720", (0x41, 0xd82): "Cortex-X4",
    (0x41, 0xd85): "Cortex-X925", (0x41, 0xd87): "Cortex-A725",
    (0x41, 0xd0c): "Neoverse-N1", (0x41, 0xd40): "Neoverse-V1",
    (0x41, 0xd49): "Neoverse-N2", (0x41, 0xd4f): "Neoverse-V2",
    (0x4e, 0x004): "Carmel",
}

def _read_int(path, base=10):
    try:
        with open(path) as f: return int(f.read().strip(), base)
    except Exception: return None

def _core_type(idx):
    """(implementer, part) from MIDR, identifying the core's type. Capacity
    and frequency are deliberately excluded: both vary per-core within a
    cluster (capacity calibration, favored-core boost) and would fragment it."""
    midr = _read_int(f"/sys/devices/system/cpu/cpu{idx}/regs/identification/midr_el1", 16)
    if midr is None: return None
    return ((midr >> 24) & 0xFF, (midr >> 4) & 0xFFF)

def _core_perf_key(cores):
    """Ranking key for a cluster: max capacity, then max frequency."""
    cap = freq = 0
    for i in cores:
        base = f"/sys/devices/system/cpu/cpu{i}"
        cap = max(cap, _read_int(f"{base}/cpu_capacity") or 0)
        freq = max(freq, _read_int(f"{base}/cpufreq/cpuinfo_max_freq") or 0)
    return (cap, freq)

def _parse_cpu_list(s):
    """Parses kernel cpulist syntax, e.g. '0-15,22,24-31'."""
    out = []
    for chunk in s.split(','):
        chunk = chunk.strip()
        if '-' in chunk:
            a, b = chunk.split('-')
            out.extend(range(int(a), int(b) + 1))
        elif chunk:
            out.append(int(chunk))
    return out

def _x86_hybrid_clusters():
    """Intel hybrid CPUs expose P/E masks instead of cpu_capacity."""
    clusters = []
    for path, label in [("/sys/devices/cpu_core/cpus", "P-Core"),
                        ("/sys/devices/cpu_atom/cpus", "E-Core")]:
        try:
            with open(path) as f: cores = _parse_cpu_list(f.read().strip())
            if cores: clusters.append({"label": label, "cores": cores})
        except Exception: pass
    return clusters if len(clusters) >= 2 else None

def detect_cpu_clusters():
    """Groups logical CPUs into clusters of identical core types (ARM MIDR
    part, or Intel P/E masks). Cores of one type may be non-contiguous.
    Returns [{"label": str, "cores": [int, ...]}, ...] ordered by first core;
    a uniform (or undetectable) CPU yields a single "CPU Cores" cluster."""
    try: n = psutil.cpu_count(logical=True) or 1
    except Exception: n = 1
    groups = {}
    for i in range(n):
        groups.setdefault(_core_type(i), []).append(i)
    if len(groups) == 1:
        hybrid = _x86_hybrid_clusters()
        if hybrid: return hybrid
        return [{"label": "CPU Cores", "cores": list(range(n))}]
    # Rank core types fastest-first so nameless clusters get P/E-style labels
    perf_order = sorted(groups, key=lambda t: _core_perf_key(groups[t]), reverse=True)
    rank = {t: i for i, t in enumerate(perf_order)}
    clusters = []
    for t in sorted(groups, key=lambda t: min(groups[t])):
        name = ARM_PART_NAMES.get(t) if t else None
        if not name:
            name = "P-Core" if rank[t] == 0 else ("E-Core" if len(groups) == 2 else f"Tier-{rank[t] + 1}")
        clusters.append({"label": name, "cores": groups[t]})
    return clusters

CPU_CLUSTERS = detect_cpu_clusters()

def _core_range_str(cores):
    """Compact display for a cluster's cores: '(00-09)' when contiguous,
    'x10' when interleaved with another cluster."""
    if cores[-1] - cores[0] == len(cores) - 1:
        return f"({cores[0]:02}-{cores[-1]:02})"
    return f"x{len(cores)}"

# Kernel driver -> short vendor tag for NIC labels (kept <=7 chars so
# "<tag> <speed>" fits the 12-char label cell)
NIC_DRIVER_NAMES = {
    "mlx5_core": "MLX5", "mlx4_en": "MLX4", "r8169": "Realtek", "r8125": "Realtek",
    "r8126": "Realtek", "r8127": "Realtek", "r8152": "Realtek",
    "igb": "Intel", "igc": "Intel", "e1000e": "Intel",
    "ixgbe": "Intel", "i40e": "Intel", "ice": "Intel", "tg3": "Brcm",
    "bnxt_en": "Brcm", "atlantic": "Aqtia", "virtio_net": "VirtIO",
}

class NetMonitor:
    def __init__(self):
        try: self.prev_stats = psutil.net_io_counters(pernic=True)
        except: self.prev_stats = {}
        self.prev_time = time.time()
        self.interfaces = self._detect_interfaces()

    def _detect_interfaces(self):
        """Physical NICs only: on Linux, anything in /sys/class/net with a
        backing device (skips lo, veth, bridges, tunnels). Elsewhere, fall
        back to psutil minus the obvious virtual names."""
        sys_net = "/sys/class/net"
        try:
            if os.path.isdir(sys_net):
                return [n for n in sorted(os.listdir(sys_net), key=str.lower)
                        if os.path.exists(os.path.join(sys_net, n, "device"))]
            stats = psutil.net_if_stats()
            return [n for n in sorted(stats, key=str.lower)
                    if not n.lower().startswith(("lo", "veth", "docker", "br-", "virbr", "tun", "tap"))]
        except:
            return []

    def _driver_label(self, iface):
        try:
            drv = os.path.basename(os.readlink(f"/sys/class/net/{iface}/device/driver"))
            return NIC_DRIVER_NAMES.get(drv, drv[:7])
        except:
            return iface[:7]

    def _carrier_up(self, iface):
        # Reading carrier raises EINVAL while the interface is admin-down
        try:
            with open(f"/sys/class/net/{iface}/carrier") as f:
                return f.read().strip() == "1"
        except:
            try: return psutil.net_if_stats()[iface].isup
            except: return False

    def get_interface_speed(self, iface_name):
        """Reads the negotiated link speed (Mbit/s) from sysfs, with a
        psutil fallback for non-sysfs platforms."""
        try:
            with open(f"/sys/class/net/{iface_name}/speed", "r") as f:
                return max(int(f.read().strip()), 0)
        except:
            try: return max(psutil.net_if_stats()[iface_name].speed, 0)
            except: return 0

    def poll(self):
        curr_time = time.time()
        dt = max(curr_time - self.prev_time, 0.1)
        try: curr_stats = psutil.net_io_counters(pernic=True)
        except: return []

        nics = []
        for name in self.interfaces:
            if name not in curr_stats:
                nics.append({"label": "Offline", "usage": 0, "up": False})
                continue
            s, p = curr_stats[name], self.prev_stats.get(name, curr_stats[name])
            bps = (((s.bytes_recv + s.bytes_sent) - (p.bytes_recv + p.bytes_sent)) * 8) / dt

            up = self._carrier_up(name)
            speed_mbit = self.get_interface_speed(name)
            limit_bps = speed_mbit * 1_000_000

            if speed_mbit >= 1000:
                speed_display = f"{speed_mbit // 1000}G"
            else:
                speed_display = f"{speed_mbit}M"

            if not up or speed_mbit == 0:
                label, up = "Link Down", False
            else:
                label = f"{self._driver_label(name)} {speed_display}"

            nics.append({"label": label, "usage": (bps / limit_bps) * 100 if limit_bps > 0 else 0, "up": up})

        self.prev_stats, self.prev_time = curr_stats, curr_time
        return nics

monitor = NetMonitor()

class VirtualCurses:
    def __init__(self):
        self.update_dims()
        self.grid = [[(" ", None) for _ in range(self.cols)] for _ in range(self.rows)]
        if HAS_256COLOR:
            self.colors = {k: f"\033[38;5;{v}m" for k, v in PALETTE_256.items()}
            self.colors[0] = "\033[0m"
        else:
            self.colors = {1: "\033[32m", 2: "\033[36m", 3: "\033[37m", 4: "\033[31m", 5: "\033[33m", 0: "\033[0m"}
    def update_dims(self):
        try: self.cols, self.rows = shutil.get_terminal_size()
        except: self.cols, self.rows = 120, 50
    def getmaxyx(self):
        self.update_dims()
        return self.rows, self.cols
    def erase(self):
        self.update_dims()
        self.grid = [[(" ", None) for _ in range(self.cols)] for _ in range(self.rows)]
    def addstr(self, y, x, text, attr=None):
        if 0 <= y < self.rows and 0 <= x < self.cols:
            for i, char in enumerate(str(text)):
                if x + i < self.cols: self.grid[y][x + i] = (char, attr)
    def render(self):
        output = []
        last_row = max((r for r in range(self.rows) if any(c[0] != " " for c in self.grid[r])), default=0)
        for r in range(last_row + 1):
            row_str, current_fmt = "", None
            for c, fmt in self.grid[r]:
                if fmt != current_fmt:
                    row_str += self.colors.get(fmt, self.colors[0])
                    current_fmt = fmt
                row_str += c
            output.append(row_str + self.colors[0])
        return "\n".join(output)

def fmt_temp(celsius_val):
    try:
        c = float(celsius_val)
        return f"{int((c * 9/5) + 32)}F" if USE_FAHRENHEIT else f"{int(c)}C"
    except: return "N/A"

def fmt_mem(bytes_val):
    if bytes_val in [None, "N/A"]: return "N/A"
    div = 1000.0 if USE_DECIMAL_UNITS else 1024.0
    s_m, s_g = ("MB", "GB") if USE_DECIMAL_UNITS else ("MiB", "GiB")
    try:
        if bytes_val > (div**3): return f"{bytes_val/(div**3):.1f}{s_g}"
        return f"{int(bytes_val/(div**2))}{s_m}"
    except: return "N/A"

# Partial-cell fill characters, 1/8th to 7/8ths (U+258F down to U+2589)
_EIGHTHS = ["▏", "▎", "▍", "▌", "▋", "▊", "▉"]

def make_bar(percent, width, color_good, color_mid, color_bad):
    if width < 3: return "[]", None
    pct = max(0.0, min(float(percent), 100.0))
    c = color_good if pct < 50 else (color_mid if pct < 80 else color_bad)
    if BAR_STYLE == "ascii":
        inner_w = width - 2
        filled = int((pct / 100.0) * inner_w)
        return "[" + "|" * filled + " " * (inner_w - filled) + "]", c
    if BAR_STYLE == "blocks":
        filled = int(round((pct / 100.0) * width))
        return "█" * filled + "░" * (width - filled), c
    # "eighths": 8 resolution steps per character cell
    total_eighths = int(round((pct / 100.0) * width * 8))
    full, rem = divmod(total_eighths, 8)
    bar_str = "█" * full
    if rem and full < width:
        bar_str += _EIGHTHS[rem - 1]
    return bar_str + "░" * (width - len(bar_str)), c

def get_cpu_temp():
    try:
        temps = psutil.sensors_temperatures()
        for k in ['cpu_thermal', 'soc_thermal', 'coretemp', 'thermal_zone0']:
            if k in temps: return temps[k][0].current
        return list(temps.values())[0][0].current
    except: return "N/A"

def get_system_fan():
    if shutil.which("sensors"):
        try:
            res = subprocess.run(["sensors"], capture_output=True, text=True, timeout=0.5)
            highest = 0
            for line in res.stdout.splitlines():
                if "RPM" in line:
                    m = re.search(r'(\d+)', line)
                    if m and int(m.group(1)) > highest: highest = int(m.group(1))
            if highest > 0: return f"{highest} RPM"
        except: pass
    return "Err"

# Cache so we don't shell out to nvidia-smi on every render tick
_CACHED_DRIVER_INFO = None

def get_driver_info_safe():
    global _CACHED_DRIVER_INFO
    if _CACHED_DRIVER_INFO is not None:
        return _CACHED_DRIVER_INFO
    driver, cuda = "Unknown", "Unknown"
    if HAS_NVML:
        try:
            pynvml.nvmlInit()
            drv = pynvml.nvmlSystemGetDriverVersion()
            if isinstance(drv, bytes): drv = drv.decode('utf-8')
            driver = drv
            cuda_ver = pynvml.nvmlSystemGetCudaDriverVersion()
            cuda = f"{cuda_ver // 1000}.{(cuda_ver % 1000) // 10}"
            _CACHED_DRIVER_INFO = (driver, cuda)
            return _CACHED_DRIVER_INFO
        except: pass
    try:
        res = subprocess.run(["nvidia-smi"], capture_output=True, text=True, timeout=3)
        if res.returncode == 0:
            d = re.search(r"Driver Version:\s*([\d\.]+)", res.stdout)
            c = re.search(r"CUDA Version:\s*([\d\.]+)", res.stdout)
            if d: driver = d.group(1)
            if c: cuda = c.group(1)
    except: pass
    _CACHED_DRIVER_INFO = (driver, cuda)
    return _CACHED_DRIVER_INFO

def _is_unified_soc(name):
    """GB10 today; GB\\d+ catches future Grace-Blackwell unified-memory SoCs."""
    return re.search(r"\bGB\d{2,3}\b", str(name)) is not None

def get_gpu_data():
    if not shutil.which("nvidia-smi"): return []
    try:
        res_l = subprocess.run(["nvidia-smi", "-L"], capture_output=True, text=True, timeout=1)
        gpu_lines = [line for line in res_l.stdout.strip().split('\n') if line.startswith("GPU")]
        gpus = []

        for i, line in enumerate(gpu_lines):
            gid = str(i)
            # 1. Initialize with defaults
            gpu = {"id": gid, "name": "Unknown", "temp": "N/A", "util": 0, "mem_used": "N/A", "mem_total": "N/A", "pwr_str": "N/A", "fan": "N/A"}
            
            # 2. NVML Logic (The original robust way)
            nvml_success = False
            if HAS_NVML:
                try:
                    pynvml.nvmlInit()
                    handle = pynvml.nvmlDeviceGetHandleByIndex(i)
                    gpu["name"] = pynvml.nvmlDeviceGetName(handle)
                    if isinstance(gpu["name"], bytes): gpu["name"] = gpu["name"].decode('utf-8')
                    
                    gpu["temp"] = pynvml.nvmlDeviceGetTemperature(handle, pynvml.NVML_TEMPERATURE_GPU)
                    gpu["util"] = pynvml.nvmlDeviceGetUtilizationRates(handle).gpu
                    
                    mem = pynvml.nvmlDeviceGetMemoryInfo(handle)
                    gpu["mem_used"], gpu["mem_total"] = mem.used, mem.total
                    
                    try:
                        gpu["fan"] = f"{pynvml.nvmlDeviceGetFanSpeed(handle)}%"
                    except:
                        gpu["fan"] = "None"

                    try:
                        gpu["pwr_str"] = f"{int(pynvml.nvmlDeviceGetPowerUsage(handle) / 1000.0)}W"
                    except:
                        pass
                    nvml_success = True
                except:
                    nvml_success = False

            # 3. CLI Fallback (If NVML failed or is missing data)
            if not nvml_success or gpu["pwr_str"] == "N/A" or gpu["fan"] in ["N/A", "0%"]:
                try:
                    # Query only what we need to avoid N/A poisoning the CSV
                    query = "name,temperature.gpu,utilization.gpu,memory.used,memory.total,power.draw,fan.speed"
                    cmd = ["nvidia-smi", f"--id={gid}", f"--query-gpu={query}", "--format=csv,noheader,nounits"]
                    res = subprocess.run(cmd, capture_output=True, text=True, timeout=1)
                    if res.returncode == 0:
                        r = [x.strip() for x in res.stdout.split(',')]
                        if gpu["name"] == "Unknown": gpu["name"] = r[0]
                        if gpu["temp"] == "N/A": gpu["temp"] = r[1]
                        gpu["util"] = float(r[2]) if "N/A" not in r[2] else gpu["util"]

                        # Memory fallback (MiB in the CSV) so discrete GPUs
                        # without NVML don't fall into the unified-RAM path
                        if gpu["mem_used"] in ["N/A", 0] and "N/A" not in r[3] and "N/A" not in r[4]:
                            gpu["mem_used"] = int(float(r[3]) * 1024 * 1024)
                            gpu["mem_total"] = int(float(r[4]) * 1024 * 1024)

                        # Power and Fan specific fallbacks
                        if gpu["pwr_str"] == "N/A" and "N/A" not in r[5]:
                            gpu["pwr_str"] = f"{int(float(r[5]))}W"
                        if (gpu["fan"] in ["N/A", "None"]) and "N/A" not in r[6]:
                            gpu["fan"] = f"{r[6]}%"
                except:
                    pass

            # 4. Unified-memory SoC / Architecture Special Cases
            if _is_unified_soc(gpu['name']) or gpu['mem_used'] in ["N/A", 0]:
                sys_ram = psutil.virtual_memory()
                gpu["mem_used"], gpu["mem_total"] = sys_ram.used, sys_ram.total
                if "Unified" not in gpu['name']: gpu['name'] += " (Unified)"
                if _is_unified_soc(gpu['name']): gpu["fan"] = "None"

            gpus.append(gpu)
        return gpus
    except:
        return []

def draw_line(stdscr, y, x, width, h):
    if y >= h or width < 2: return
    try: stdscr.addstr(y, x, "+" + "-" * (width - 2) + "+")
    except: pass

def draw_row_parts(stdscr, y, x, width, parts, graph_data, colors_map, h, gpu_mode=False):
    if y >= h or width < 5: return
    try:
        curr_x = x
        for p in parts:
            stdscr.addstr(y, curr_x, p)
            curr_x += len(p)
        if graph_data:
            pct_val, _ = graph_data
            reserved = 7 if gpu_mode else 8
            space_left = (x + width - 1) - curr_x
            if space_left > 5:
                bar, color = make_bar(pct_val, space_left - reserved, colors_map[1], colors_map[5], colors_map[4])
                stdscr.addstr(y, curr_x, bar, color)
                stdscr.addstr(y, curr_x + len(bar), f" {int(pct_val):>3}% ")
        if x + width - 1 < stdscr.getmaxyx()[1]:
            stdscr.addstr(y, x + width - 1, "|")
    except: pass

def render_dashboard(stdscr, colors_map):
    h, w = stdscr.getmaxyx()
    draw_w = min(w, MAX_WIDTH)
    start_x = max(0, (w - draw_w) // 2)
    if h < 10 or draw_w < 40: return 
    
    now = datetime.now().strftime("%H:%M:%S")
    stdscr.addstr(0, start_x, f"SPARK-SMI  {now}", colors_map[3])
    stdscr.addstr(0, start_x+draw_w-10, f"Ref: {REFRESH_RATE}s", colors_map[2])
    draw_line(stdscr, 1, start_x, draw_w, h)
    
    y, cpu = 2, psutil.cpu_percent(percpu=True)
    temp_str, ram, swap, fan_disp = fmt_temp(get_cpu_temp()), psutil.virtual_memory(), psutil.swap_memory(), (get_system_fan())[:4]
    
    stdscr.addstr(y, start_x, f"| CPU  {platform.node():<20} | Load  | Temp Fan  | {'Power(W)':<9} | {'RAM / Swap':<21} | Util")
    stdscr.addstr(y, start_x + draw_w - 1, "|")
    draw_line(stdscr, y+1, start_x, draw_w, h)
    y += 2

    # One summary row per detected cluster; at least 2 rows so the RAM and
    # Swap bars both get a home even on uniform (single-cluster) CPUs
    mem_rows = [(f"RAM:{fmt_mem(ram.used)}/{fmt_mem(ram.total)}", ram.percent),
                (f"Swp:{fmt_mem(swap.used)}/{fmt_mem(swap.total)}", swap.percent)]
    for r in range(max(len(CPU_CLUSTERS), 2)):
        if r < len(CPU_CLUSTERS):
            cl = CPU_CLUSTERS[r]
            loads = [cpu[i] for i in cl["cores"] if i < len(cpu)]
            load_str = f"{int(sum(loads)/len(loads)) if loads else 0:>3}%"
            label = f"{cl['label']} {_core_range_str(cl['cores'])}"[:20]
            id_str, t_str, f_str, pwr = str(r), temp_str, fan_disp, "Shared"
        else:
            load_str, label, id_str, t_str, f_str, pwr = "    ", "", "", "", "", ""
        mem_txt, mem_pct = mem_rows[r] if r < len(mem_rows) else ("", None)
        p_row = f"| {id_str:<4} {label:<20} | {load_str}  | {t_str:<4} {f_str:<4} | {pwr:<9} | {mem_txt:<21} | "
        graph = (mem_pct, colors_map) if mem_pct is not None else None
        draw_row_parts(stdscr, y, start_x, draw_w, [p_row], graph, colors_map, h)
        draw_line(stdscr, y+1, start_x, draw_w, h)
        y += 2

    # Per-core grids, one section per cluster. Column count adapts to the
    # thread count and terminal width so big CPUs stay on screen.
    total_threads = len(cpu)
    desired_cols = 4 if total_threads <= 24 else (6 if total_threads <= 64 else 8)
    id_w = 3 if total_threads > 100 else 2
    cols = max(1, min(desired_cols, (draw_w - 4) // (id_w + 12)))
    col_w = (draw_w - 4) // cols
    for cl in CPU_CLUSTERS:
        if y >= h: break
        name = f"{cl['label']} Cluster" if len(CPU_CLUSTERS) > 1 else cl['label']
        stdscr.addstr(y, start_x, f"| {name} " + "-" * (draw_w - len(name) - 4) + "|")
        y += 1
        for j, core_idx in enumerate(cl["cores"]):
            r, c = j // cols, j % cols
            if y + r >= h: break
            p = cpu[core_idx] if core_idx < len(cpu) else 0
            cx = start_x + 4 + (c * col_w)
            bar, color = make_bar(p, col_w - (id_w + 7), colors_map[1], colors_map[5], colors_map[4])
            try:
                if c == 0: stdscr.addstr(y + r, start_x, "|")
                stdscr.addstr(y + r, cx, f"{core_idx:0{id_w}}", colors_map[2])
                stdscr.addstr(y + r, cx + id_w + 1, bar, color)
                stdscr.addstr(y + r, cx + id_w + 1 + len(bar) + 1, f"{int(p)}%", colors_map[3])
                stdscr.addstr(y + r, start_x + draw_w - 1, "|")
            except: pass
        y += (len(cl["cores"]) + cols - 1) // cols

    gpus = get_gpu_data()
    if y < h:
        stdscr.addstr(y, start_x, f"| GPU  {'Name':<26} | Temp  Fan  | {'Power (W)':<11} | {'Memory-Usage':<20}| GPU-Util")
        stdscr.addstr(y, start_x + draw_w - 1, "|")
        draw_line(stdscr, y+1, start_x, draw_w, h)
        y += 2
        # ... inside render_dashboard, find the 'for gpu in gpus' loop ...
        for gpu in gpus:
            if y >= h: break
        
            # --- NEW FAN LOGIC START ---
            # 1. Start with what was collected in get_gpu_data
            fan_val = str(gpu.get('fan', 'N/A'))
        
            # 2. Universal Logic: If it's a standard GPU and reporting 0% or N/A,
            # hammer the CLI specifically for the fan to bypass VENV/Driver lag
            if (not _is_unified_soc(gpu['name'])) and (fan_val in ["0%", "N/A", "Err", "0"]):
                try:
                    f_res = subprocess.run(
                        ["nvidia-smi", f"--id={gpu['id']}", "--query-gpu=fan.speed", "--format=csv,noheader,nounits"],
                        capture_output=True, text=True, timeout=0.5
                    )
                    if f_res.returncode == 0 and f_res.stdout.strip().isdigit():
                        fan_val = f"{f_res.stdout.strip()}%"
                except:
                    pass
        
            # 3. Final Formatting for the UI
            display_fan = fan_val[:4] if len(fan_val) > 4 else fan_val
            if _is_unified_soc(gpu['name']): display_fan = "None" # Keep unified SoCs clean
            # --- NEW FAN LOGIC END ---

            mem_str = f"{fmt_mem(gpu['mem_used'])}/{fmt_mem(gpu['mem_total'])}".ljust(20)
        
            # Trailing | is split as a separate part so the bar gains 1 char of width
            # and renders as "| [bar]" with a space between the pipe and bracket
            p_row = f"| {gpu['id']:<4} {gpu['name'][:26]:<26} | {fmt_temp(gpu['temp']):<5} {display_fan:<4} | {gpu['pwr_str']:<11} | {mem_str}"
        
            draw_row_parts(stdscr, y, start_x, draw_w, [p_row, "| "], (gpu['util'], colors_map), colors_map, h, gpu_mode=True)
            draw_line(stdscr, y+1, start_x, draw_w, h)
            y += 2

    nics = monitor.poll()
    if SHOW_ACTIVE_NICS_ONLY:
        nics = [n for n in nics if n.get("up")]
    per_row = max(1, (draw_w - 2) // 21)
    drew_nics = False
    for chunk_start in range(0, len(nics), per_row):
        if y + 2 >= h: break
        drew_nics = True
        stdscr.addstr(y, start_x, "|")
        stdscr.addstr(y+1, start_x, "|")
        for i, n in enumerate(nics[chunk_start:chunk_start + per_row]):
            base_x = start_x + 1 + (i * 21)
            if base_x + 20 < start_x + draw_w:
                stdscr.addstr(y, base_x, f"{chunk_start+i+1:>2} {n['label'][:12]:<12} {int(n['usage']):>3}%  ")
                bar, col = make_bar(n['usage'], 19, colors_map[1], colors_map[5], colors_map[4])
                stdscr.addstr(y+1, base_x, " ")
                stdscr.addstr(y+1, base_x + 1, bar, col)
        stdscr.addstr(y, start_x + draw_w - 1, "|")
        stdscr.addstr(y+1, start_x + draw_w - 1, "|")
        y += 2
    if drew_nics and y < h:
        draw_line(stdscr, y, start_x, draw_w, h)
        y += 1

    if y + 2 < h:
        driver, cuda = get_driver_info_safe()
        l1 = f" Ver: {VERSION} | Kernel: {platform.release()} | Driver: {driver} | CUDA: {cuda}"
        l2 = " Controls: 'q':Quit | 'u':Unit | 't':Temp | 'n':NICs | Mode: Live"
        stdscr.addstr(y, start_x, "|"); stdscr.addstr(y, start_x+1, l1.ljust(draw_w-2), colors_map[2]); stdscr.addstr(y, start_x+draw_w-1, "|")
        stdscr.addstr(y+1, start_x, "|"); stdscr.addstr(y+1, start_x+1, l2.ljust(draw_w-2), colors_map[3]); stdscr.addstr(y+1, start_x+draw_w-1, "|")
        draw_line(stdscr, y+2, start_x, draw_w, h)

def main_loop(stdscr):
    global USE_FAHRENHEIT, USE_DECIMAL_UNITS, SHOW_ACTIVE_NICS_ONLY
    import curses
    curses.start_color(); curses.use_default_colors(); curses.curs_set(0); stdscr.nodelay(True)
    colors = {i: curses.color_pair(i) for i in range(1, 6)}
    if curses.COLORS >= 256:
        palette = [PALETTE_256[i] for i in range(1, 6)]
    else:
        palette = [curses.COLOR_GREEN, curses.COLOR_CYAN, curses.COLOR_WHITE, curses.COLOR_RED, curses.COLOR_YELLOW]
    for i, cl in enumerate(palette, 1):
        curses.init_pair(i, cl, -1)
    
    while True:
        try:
            stdscr.erase()
            render_dashboard(stdscr, colors)
            stdscr.refresh()
        except Exception: pass
        
        start_wait = time.time()
        while time.time() - start_wait < REFRESH_RATE:
            ch = stdscr.getch()
            if ch == ord('q'): return
            if ch == ord('t'): USE_FAHRENHEIT = not USE_FAHRENHEIT; break
            if ch == ord('u'): USE_DECIMAL_UNITS = not USE_DECIMAL_UNITS; break
            if ch == ord('n'): SHOW_ACTIVE_NICS_ONLY = not SHOW_ACTIVE_NICS_ONLY; stdscr.clear(); break
            if ch == curses.KEY_RESIZE: stdscr.clear(); break
            time.sleep(0.05)

if __name__ == "__main__":
    if "-l" in sys.argv or "--loop" in sys.argv:
        import curses
        # curses needs the locale set to emit multi-byte UTF-8 block chars
        try:
            import locale
            locale.setlocale(locale.LC_ALL, "")
        except Exception: pass
        try: curses.wrapper(main_loop)
        except KeyboardInterrupt: pass
    else:
        v = VirtualCurses()
        render_dashboard(v, {i:i for i in range(1,6)})
        print(v.render())
