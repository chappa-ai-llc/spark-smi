"""Data collectors. Each collector probes hardware capabilities ONCE at
construction into `.caps` and exposes `.sample()` returning plain dict/list
state for pages.py to render -- no rendering logic lives here. Every sysfs
read and subprocess call is guarded; a missing sensor degrades to N/A or a
capability flag, it never raises past this module.
"""
import math
import os
import re
import shutil
import subprocess
import time

import psutil

# --- NVML import strategy ---------------------------------------------
# Force-check for pynvml, but don't crash if the venv isn't active.
try:
    import pynvml
    pynvml.nvmlInit()
    HAS_NVML = True
except Exception:
    HAS_NVML = False


def _is_unified_soc(name):
    """GB10 today; GB\\d+ catches future Grace-Blackwell unified-memory SoCs."""
    return re.search(r"\bGB\d{2,3}\b", str(name)) is not None


# =========================================================================
# CPU
# =========================================================================

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
        with open(path) as f:
            return int(f.read().strip(), base)
    except Exception:
        return None

def _core_type(idx):
    """(implementer, part) from MIDR, identifying the core's type. Capacity
    and frequency are deliberately excluded: both vary per-core within a
    cluster (capacity calibration, favored-core boost) and would fragment it."""
    midr = _read_int(f"/sys/devices/system/cpu/cpu{idx}/regs/identification/midr_el1", 16)
    if midr is None:
        return None
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
            with open(path) as f:
                cores = _parse_cpu_list(f.read().strip())
            if cores:
                clusters.append({"label": label, "cores": cores})
        except Exception:
            pass
    return clusters if len(clusters) >= 2 else None

def detect_cpu_clusters():
    """Groups logical CPUs into clusters of identical core types (ARM MIDR
    part, or Intel P/E masks). Cores of one type may be non-contiguous.
    Returns [{"label": str, "cores": [int, ...]}, ...] ordered by first core;
    a uniform (or undetectable) CPU yields a single "CPU Cores" cluster."""
    try:
        n = psutil.cpu_count(logical=True) or 1
    except Exception:
        n = 1
    groups = {}
    for i in range(n):
        groups.setdefault(_core_type(i), []).append(i)
    if len(groups) == 1:
        hybrid = _x86_hybrid_clusters()
        if hybrid:
            return hybrid
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

def _core_range_str(cores):
    """Compact display for a cluster's cores: contiguous runs joined by
    ' · ', e.g. '05-09 · 15-19' for an interleaved cluster (the real DGX
    Spark: A725 on 0-4 & 10-14, X925 on 5-9 & 15-19), '00-09' for a
    contiguous one."""
    cores = sorted(cores)
    runs = []
    start = prev = cores[0]
    for c in cores[1:]:
        if c == prev + 1:
            prev = c
            continue
        runs.append((start, prev))
        start = prev = c
    runs.append((start, prev))
    parts = [f"{a:02}-{b:02}" if a != b else f"{a:02}" for a, b in runs]
    return " · ".join(parts)

def _cluster_max_freq_ghz(cores):
    """Max cpufreq across the cluster's cores, in GHz. Read once at
    collector construction and cached on the cluster dict -- unlike
    per-tick load, this doesn't change at runtime."""
    freq = 0
    for i in cores:
        freq = max(freq, _read_int(f"/sys/devices/system/cpu/cpu{i}/cpufreq/cpuinfo_max_freq") or 0)
    return freq / 1_000_000.0 if freq else 0.0

def _get_cpu_temp():
    try:
        temps = psutil.sensors_temperatures()
        for k in ['cpu_thermal', 'soc_thermal', 'coretemp', 'thermal_zone0']:
            if k in temps:
                return temps[k][0].current
        return list(temps.values())[0][0].current
    except Exception:
        return "N/A"

def _load_avg():
    try:
        return os.getloadavg()
    except Exception:
        return None


class CpuCollector:
    """Detects CPU cluster topology once at construction (mirrors the old
    module-level CPU_CLUSTERS); samples per-core and per-cluster load, plus
    overall temp and loadavg, each tick."""

    def __init__(self):
        self.clusters = detect_cpu_clusters()
        for cl in self.clusters:
            cl["ghz"] = _cluster_max_freq_ghz(cl["cores"])
            cl["range"] = _core_range_str(cl["cores"])
        self.caps = {"multi_cluster": len(self.clusters) > 1, "loadavg": _load_avg() is not None}

    def sample(self):
        try:
            percpu = psutil.cpu_percent(percpu=True)
        except Exception:
            percpu = []
        rows = []
        for cl in self.clusters:
            loads = [percpu[i] for i in cl["cores"] if i < len(percpu)]
            avg = (sum(loads) / len(loads)) if loads else 0.0
            rows.append({
                "label": cl["label"], "cores": cl["cores"], "loads": loads,
                "avg": avg, "ghz": cl["ghz"], "range": cl["range"],
            })
        return {
            "clusters": rows, "percpu": percpu, "temp": _get_cpu_temp(),
            "loadavg": _load_avg(), "threads": len(percpu),
        }


# =========================================================================
# Memory
# =========================================================================

def _nvml_gpu_alloc_sum():
    """Sum of per-process GPU memory allocation across all GPUs, via NVML
    ONLY (nvmlDeviceGetComputeRunningProcesses / GraphicsRunningProcesses'
    usedGpuMemory, skipping None/NVML_VALUE_NOT_AVAILABLE) -- never derived
    from psutil/system-RAM arithmetic, which would just be inventing a
    number. Typically 0 on GB10 (no compute contexts tracked that way for
    the integrated GPU); callers must hide the gpu-alloc segment when this
    is 0. A pid can appear in both the compute and graphics process lists on
    some drivers -- dedupe per (gpu, pid) so it isn't double-counted."""
    if not HAS_NVML:
        return 0
    total = 0
    try:
        n = pynvml.nvmlDeviceGetCount()
    except Exception:
        return 0
    for i in range(n):
        try:
            h = pynvml.nvmlDeviceGetHandleByIndex(i)
        except Exception:
            continue
        seen = {}
        for getter in (getattr(pynvml, "nvmlDeviceGetComputeRunningProcesses", None),
                       getattr(pynvml, "nvmlDeviceGetGraphicsRunningProcesses", None)):
            if getter is None:
                continue
            try:
                for p in getter(h):
                    used = getattr(p, "usedGpuMemory", None)
                    if used and used != getattr(pynvml, "NVML_VALUE_NOT_AVAILABLE", -1):
                        pid = getattr(p, "pid", None)
                        seen[pid] = max(seen.get(pid, 0), used)
            except Exception:
                pass
        total += sum(seen.values())
    return total


class MemoryCollector:
    """System RAM + swap + (when available) the NVML per-process GPU-alloc
    sum used to split out a gpu-alloc segment on unified-memory SoCs."""

    def __init__(self):
        self.caps = {"nvml_gpu_alloc": HAS_NVML}

    def sample(self):
        try:
            vm = psutil.virtual_memory()
        except Exception:
            vm = None
        try:
            sw = psutil.swap_memory()
        except Exception:
            sw = None
        return {"vm": vm, "swap": sw, "gpu_alloc": _nvml_gpu_alloc_sum()}


# =========================================================================
# GPU
# =========================================================================

# Cache so we don't shell out to nvidia-smi on every render tick.
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
            if isinstance(drv, bytes):
                drv = drv.decode('utf-8')
            driver = drv
            cuda_ver = pynvml.nvmlSystemGetCudaDriverVersion()
            cuda = f"{cuda_ver // 1000}.{(cuda_ver % 1000) // 10}"
            _CACHED_DRIVER_INFO = (driver, cuda)
            return _CACHED_DRIVER_INFO
        except Exception:
            pass
    try:
        res = subprocess.run(["nvidia-smi"], capture_output=True, text=True, timeout=3)
        if res.returncode == 0:
            d = re.search(r"Driver Version:\s*([\d\.]+)", res.stdout)
            c = re.search(r"CUDA Version:\s*([\d\.]+)", res.stdout)
            if d:
                driver = d.group(1)
            if c:
                cuda = c.group(1)
    except Exception:
        pass
    _CACHED_DRIVER_INFO = (driver, cuda)
    return _CACHED_DRIVER_INFO

def _gpu_names():
    """GPU names for capability probing, NVML first then nvidia-smi -L."""
    if HAS_NVML:
        try:
            n = pynvml.nvmlDeviceGetCount()
            names = []
            for i in range(n):
                h = pynvml.nvmlDeviceGetHandleByIndex(i)
                nm = pynvml.nvmlDeviceGetName(h)
                names.append(nm.decode() if isinstance(nm, bytes) else nm)
            return names
        except Exception:
            pass
    if shutil.which("nvidia-smi"):
        try:
            res = subprocess.run(["nvidia-smi", "-L"], capture_output=True, text=True, timeout=1)
            lines = [l for l in res.stdout.strip().split("\n") if l.startswith("GPU")]
            return [re.sub(r"^GPU \d+:\s*", "", l).split(" (UUID")[0] for l in lines]
        except Exception:
            pass
    return []

def _cli_caps_probe(idx):
    """CLI fallback capability probe for one GPU index, used when NVML isn't
    available at all: a field reads "[Not Supported]"/"N/A" in the CSV when
    the GPU/driver doesn't expose it."""
    caps = {"power_draw": False, "fan": False, "power_limit": False, "mem_local": True, "clocks": False, "pcie": True}
    try:
        q = "power.draw,fan.speed,power.limit,memory.total,clocks.sm"
        res = subprocess.run(["nvidia-smi", f"--id={idx}", f"--query-gpu={q}", "--format=csv,noheader,nounits"],
                              capture_output=True, text=True, timeout=1)
        if res.returncode == 0:
            vals = [v.strip() for v in res.stdout.split(",")]
            keys = ["power_draw", "fan", "power_limit", "mem_local", "clocks"]
            for k, v in zip(keys, vals):
                caps[k] = ("N/A" not in v) and ("[Not" not in v) and (v != "")
    except Exception:
        pass
    return caps

def _cli_clock_available(idx):
    """CLI fallback for the "clocks" capability: on GB10, NVML's own clock
    query can fail while `nvidia-smi --query-gpu=clocks.sm` still works
    (verified on hardware). Same pattern as the existing power.draw
    fallback -- try NVML first, only shell out if that failed."""
    try:
        res = subprocess.run(["nvidia-smi", f"--id={idx}", "--query-gpu=clocks.sm", "--format=csv,noheader,nounits"],
                              capture_output=True, text=True, timeout=1)
        if res.returncode == 0:
            v = res.stdout.strip()
            return v not in ("", "N/A") and "[Not" not in v
    except Exception:
        pass
    return False

def _nvml_caps_probe(i, name):
    caps = {"fan": False, "power_draw": False, "power_limit": False, "mem_local": True, "pcie": False, "clocks": False}
    try:
        h = pynvml.nvmlDeviceGetHandleByIndex(i)
    except Exception:
        return caps
    try:
        pynvml.nvmlDeviceGetFanSpeed(h)
        caps["fan"] = True
    except Exception:
        pass
    try:
        pynvml.nvmlDeviceGetPowerUsage(h)
        caps["power_draw"] = True
    except Exception:
        pass
    try:
        pynvml.nvmlDeviceGetPowerManagementLimit(h)
        caps["power_limit"] = True
    except Exception:
        pass
    try:
        mem = pynvml.nvmlDeviceGetMemoryInfo(h)
        caps["mem_local"] = bool(mem.total)
    except Exception:
        caps["mem_local"] = False
    try:
        pynvml.nvmlDeviceGetClockInfo(h, pynvml.NVML_CLOCK_SM)
        caps["clocks"] = True
    except Exception:
        caps["clocks"] = _cli_clock_available(i)
    try:
        pynvml.nvmlDeviceGetPciInfo(h)
        caps["pcie"] = True
    except Exception:
        pass
    return caps

def _collect_gpus():
    """Per-GPU live sample. Tries NVML first, falls back per-field to
    `nvidia-smi --query-gpu` CSV, then degrades to "N/A" -- never crash on
    missing sensors."""
    if not shutil.which("nvidia-smi"):
        return []
    try:
        res_l = subprocess.run(["nvidia-smi", "-L"], capture_output=True, text=True, timeout=1)
        gpu_lines = [line for line in res_l.stdout.strip().split('\n') if line.startswith("GPU")]
        gpus = []

        for i, line in enumerate(gpu_lines):
            gid = str(i)
            gpu = {"id": gid, "name": "Unknown", "temp": "N/A", "util": 0,
                   "mem_used": "N/A", "mem_total": "N/A", "pwr_str": "N/A", "fan": "N/A",
                   "clk_sm": "N/A"}

            nvml_success = False
            if HAS_NVML:
                try:
                    pynvml.nvmlInit()
                    handle = pynvml.nvmlDeviceGetHandleByIndex(i)
                    gpu["name"] = pynvml.nvmlDeviceGetName(handle)
                    if isinstance(gpu["name"], bytes):
                        gpu["name"] = gpu["name"].decode('utf-8')

                    gpu["temp"] = pynvml.nvmlDeviceGetTemperature(handle, pynvml.NVML_TEMPERATURE_GPU)
                    gpu["util"] = pynvml.nvmlDeviceGetUtilizationRates(handle).gpu

                    mem = pynvml.nvmlDeviceGetMemoryInfo(handle)
                    gpu["mem_used"], gpu["mem_total"] = mem.used, mem.total

                    try:
                        gpu["fan"] = f"{pynvml.nvmlDeviceGetFanSpeed(handle)}%"
                    except Exception:
                        gpu["fan"] = "None"

                    try:
                        gpu["pwr_str"] = f"{int(pynvml.nvmlDeviceGetPowerUsage(handle) / 1000.0)} W"
                    except Exception:
                        pass

                    try:
                        gpu["clk_sm"] = pynvml.nvmlDeviceGetClockInfo(handle, pynvml.NVML_CLOCK_SM)
                    except Exception:
                        pass
                    nvml_success = True
                except Exception:
                    nvml_success = False

            if (not nvml_success or gpu["pwr_str"] == "N/A" or gpu["fan"] in ["N/A", "0%"]
                    or gpu["clk_sm"] in ["N/A", None]):
                try:
                    # clocks.sm appended last: verified on GB10 that NVML's
                    # own clock query can fail while this CLI field works.
                    query = "name,temperature.gpu,utilization.gpu,memory.used,memory.total,power.draw,fan.speed,clocks.sm"
                    cmd = ["nvidia-smi", f"--id={gid}", f"--query-gpu={query}", "--format=csv,noheader,nounits"]
                    res = subprocess.run(cmd, capture_output=True, text=True, timeout=1)
                    if res.returncode == 0:
                        r = [x.strip() for x in res.stdout.split(',')]
                        if gpu["name"] == "Unknown":
                            gpu["name"] = r[0]
                        if gpu["temp"] == "N/A":
                            gpu["temp"] = r[1]
                        gpu["util"] = float(r[2]) if "N/A" not in r[2] else gpu["util"]

                        # Memory fallback (MiB in the CSV) so discrete GPUs
                        # without NVML don't fall into the unified-RAM path
                        if gpu["mem_used"] in ["N/A", 0] and "N/A" not in r[3] and "N/A" not in r[4]:
                            gpu["mem_used"] = int(float(r[3]) * 1024 * 1024)
                            gpu["mem_total"] = int(float(r[4]) * 1024 * 1024)

                        if gpu["pwr_str"] == "N/A" and "N/A" not in r[5]:
                            gpu["pwr_str"] = f"{int(float(r[5]))} W"
                        if (gpu["fan"] in ["N/A", "None"]) and "N/A" not in r[6]:
                            gpu["fan"] = f"{r[6]}%"
                        if gpu["clk_sm"] in ["N/A", None] and len(r) > 7 and "N/A" not in r[7] and "[Not" not in r[7]:
                            gpu["clk_sm"] = int(float(r[7]))
                except Exception:
                    pass

            # Unified-memory SoC / architecture special case
            if _is_unified_soc(gpu['name']) or gpu['mem_used'] in ["N/A", 0]:
                sys_ram = psutil.virtual_memory()
                gpu["mem_used"], gpu["mem_total"] = sys_ram.used, sys_ram.total
                if "Unified" not in gpu['name']:
                    gpu['name'] += " (Unified)"
                if _is_unified_soc(gpu['name']):
                    gpu["fan"] = "None"

            gpus.append(gpu)
        return gpus
    except Exception:
        return []


class GpuCollector:
    """Probes per-GPU capability flags once (fan/power_draw/power_limit/
    mem_local/pcie/clocks), via NVML with a CLI fallback. Verified GB10
    facts baked in: fan N/A, ALL power-limit fields N/A, power.draw works,
    memory.total N/A (unified -- mem_local False, system RAM substituted)."""

    def __init__(self):
        self.available = bool(shutil.which("nvidia-smi")) or HAS_NVML
        self.caps = self._probe_caps() if self.available else []

    def _probe_caps(self):
        names = _gpu_names()
        caps = []
        for i, name in enumerate(names):
            if HAS_NVML:
                c = _nvml_caps_probe(i, name)
            else:
                c = _cli_caps_probe(i)
            if _is_unified_soc(name):
                c["fan"] = False
                c["mem_local"] = False
            caps.append(c)
        return caps

    def sample(self):
        return _collect_gpus()

    def driver_info(self):
        return get_driver_info_safe()


# =========================================================================
# Network
# =========================================================================

# Kernel driver -> short vendor tag for NIC labels (kept <=7 chars so
# "<tag> <speed>" fits the 12-char label cell). Final fallback when PCI-ID
# naming below can't resolve anything at all.
NIC_DRIVER_NAMES = {
    "mlx5_core": "MLX5", "mlx4_en": "MLX4", "r8169": "Realtek", "r8125": "Realtek",
    "r8126": "Realtek", "r8127": "Realtek", "r8152": "Realtek",
    "igb": "Intel", "igc": "Intel", "e1000e": "Intel",
    "ixgbe": "Intel", "i40e": "Intel", "ice": "Intel", "tg3": "Brcm",
    "bnxt_en": "Brcm", "atlantic": "Aqtia", "virtio_net": "VirtIO",
}

# --- NIC hardware naming from PCI IDs ----------------------------------
# Built-in vendor/device table (spec: NETWORK data section). Composed as
# "Vendor Model" e.g. "Mellanox ConnectX-7". Hex IDs are lowercase, no "0x".
PCI_VENDOR_NAMES = {
    "15b3": "Mellanox", "10ec": "Realtek", "8086": "Intel", "14e4": "Broadcom",
    "14c3": "MediaTek", "1d6a": "Aquantia", "10de": "NVIDIA",
}
PCI_DEVICE_NAMES = {
    ("15b3", "1021"): "ConnectX-7",
    ("10ec", "8127"): "RTL8127",
}

# Lazily-parsed /usr/share/hwdata/pci.ids (or the Debian-ism .../misc/pci.ids)
# used only when the built-in vendor table above doesn't know the card.
# None = not yet attempted; {} = attempted and absent/unparseable (still a
# valid cache entry, so a missing file only costs one failed open() per run).
_PCI_IDS_CACHE = None

def _load_pci_ids():
    global _PCI_IDS_CACHE
    if _PCI_IDS_CACHE is not None:
        return _PCI_IDS_CACHE
    data = {}
    for path in ("/usr/share/hwdata/pci.ids", "/usr/share/misc/pci.ids"):
        try:
            with open(path, encoding="utf-8", errors="ignore") as f:
                vendor_hex = None
                for line in f:
                    if not line.strip() or line.startswith("#"):
                        continue
                    if line.startswith("\t\t"):
                        continue  # subvendor/subdevice lines -- not needed here
                    if line.startswith("\t"):
                        if vendor_hex is None:
                            continue
                        parts = line.strip().split(None, 1)
                        if len(parts) == 2:
                            data[vendor_hex]["devices"][parts[0].lower()] = parts[1].strip()
                        continue
                    if line[0] in "0123456789abcdefABCDEF":
                        parts = line.strip().split(None, 1)
                        if len(parts) == 2:
                            vendor_hex = parts[0].lower()
                            data[vendor_hex] = {"name": parts[1].strip(), "devices": {}}
            break  # first readable file wins
        except Exception:
            continue
    _PCI_IDS_CACHE = data
    return data

def _pci_ids_lookup(vendor_hex, device_hex):
    """Vendor+device name pair from pci.ids, or None if either is unknown or
    the file is unavailable -- caller falls back to the driver-name label."""
    v = _load_pci_ids().get(vendor_hex)
    if not v:
        return None
    dname = v["devices"].get(device_hex)
    return f"{v['name']} {dname}" if dname else None

def _pci_vendor_device(iface):
    """(vendor_hex, device_hex) lowercase, no "0x", for one netdev's backing
    PCI device -- or (None, None) on any non-PCI/virtual device."""
    try:
        v = _read_text_stripped_hex(f"/sys/class/net/{iface}/device/vendor")
        d = _read_text_stripped_hex(f"/sys/class/net/{iface}/device/device")
        if v and d:
            return v, d
    except Exception:
        pass
    return None, None

def _read_text_stripped_hex(path):
    with open(path) as f:
        return f.read().strip().lower().replace("0x", "")

class NetCollector:
    """Physical-NIC discovery + RDMA-aware, physical-port-grouped throughput.
    Moved verbatim from the pre-2.0 NetMonitor -- rename only, plus a .caps
    probe and poll() -> sample() to match the collector protocol used
    elsewhere in this module."""

    def __init__(self):
        self.interfaces = self._detect_interfaces()
        self.rdma_counters = self._map_rdma_counters()
        self.groups = self._group_physical()
        self.caps = {"rdma": bool(self.rdma_counters), "interfaces": len(self.interfaces)}
        try:
            netdev_stats = psutil.net_io_counters(pernic=True)
        except Exception:
            netdev_stats = {}
        self.prev_bytes = {}
        for name in self.interfaces:
            b = self._read_byte_counters(name, netdev_stats)
            if b is not None:
                self.prev_bytes[name] = b
        self.prev_time = time.time()

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
        except Exception:
            return []

    def _map_rdma_counters(self):
        """RDMA/RoCE traffic (NCCL, GPUDirect) bypasses the kernel network
        stack, so netdev byte counters never see it. The HCA's per-port
        counters under /sys/class/infiniband do count it. Map each netdev
        to those counter files via the shared PCI device's net/ dir."""
        mapping = {}
        ib_root = "/sys/class/infiniband"
        try:
            ib_devs = os.listdir(ib_root)
        except OSError:
            return mapping
        for ib in ib_devs:
            try:
                netdevs = os.listdir(os.path.join(ib_root, ib, "device", "net"))
                ports = sorted(os.listdir(os.path.join(ib_root, ib, "ports")))
            except OSError:
                continue
            for iface in netdevs:
                port = ports[0] if len(ports) == 1 else self._ib_port_for(iface, ports)
                cdir = os.path.join(ib_root, ib, "ports", port, "counters")
                paths = (os.path.join(cdir, "port_rcv_data"),
                         os.path.join(cdir, "port_xmit_data"))
                if all(os.path.exists(p) for p in paths):
                    mapping[iface] = paths
        return mapping

    def _ib_port_for(self, iface, ports):
        # Dual-port HCAs behind one PCI function: netdev dev_port N is IB port N+1
        try:
            with open(f"/sys/class/net/{iface}/dev_port") as f:
                cand = str(int(f.read().strip()) + 1)
            return cand if cand in ports else ports[0]
        except (OSError, ValueError):
            return ports[0]

    def _read_byte_counters(self, iface, netdev_stats):
        """Total (rx, tx) bytes for one netdev. Prefers the RDMA port
        counters -- which are in 4-byte-word units per the InfiniBand spec --
        falling back to kernel netdev counters (blind to RDMA)."""
        paths = self.rdma_counters.get(iface)
        if paths:
            try:
                with open(paths[0]) as f:
                    rx = int(f.read()) * 4
                with open(paths[1]) as f:
                    tx = int(f.read()) * 4
                return (rx, tx)
            except (OSError, ValueError):
                pass
        s = netdev_stats.get(iface)
        return (s.bytes_recv, s.bytes_sent) if s else None

    def _phys_port_key(self, iface):
        """(switch_id, port_name) identity of the physical port behind a
        netdev. On multi-host/socket-direct cards (DGX Spark's CX-7: two
        PCIe x4 links, each exposing one PF per QSFP port) several PFs
        share one physical port and sysfs exposes that here. Reading these
        raises EOPNOTSUPP on ordinary NICs -> None."""
        try:
            with open(f"/sys/class/net/{iface}/phys_switch_id") as f:
                sw = f.read().strip()
            with open(f"/sys/class/net/{iface}/phys_port_name") as f:
                pn = f.read().strip()
            if sw and pn:
                return (sw, pn)
        except OSError:
            pass
        return None

    def _pci_slot(self, iface):
        """Fallback group key: PCI address minus the function digit, so both
        ports of a dual-port card (....:01:00.0/.1) share one key. Non-PCI
        devices (USB dongles etc.) group by themselves."""
        try:
            addr = os.path.basename(os.path.realpath(f"/sys/class/net/{iface}/device"))
            m = re.fullmatch(r"([0-9a-fA-F]{4}:[0-9a-fA-F]{2}:[0-9a-fA-F]{2})\.[0-7]", addr)
            if m:
                return m.group(1)
        except OSError:
            pass
        return iface

    def _group_physical(self):
        """[{"members": [...], "port": "p0"|None}, ...] -- one entry per
        physical NIC port (PFs sharing a phys_switch_id+phys_port_name) or,
        failing that, per PCI slot. "port" is set only for shared physical
        ports, where capacity is the port speed rather than the sum."""
        groups, by_key = [], {}
        for name in self.interfaces:
            phys = self._phys_port_key(name)
            key = ("port",) + phys if phys else ("slot", self._pci_slot(name))
            if key in by_key:
                by_key[key]["members"].append(name)
            else:
                by_key[key] = {"members": [name], "port": phys[1] if phys else None}
                groups.append(by_key[key])
        return groups

    def _driver_label(self, iface):
        try:
            drv = os.path.basename(os.readlink(f"/sys/class/net/{iface}/device/driver"))
            return NIC_DRIVER_NAMES.get(drv, drv[:7])
        except Exception:
            return iface[:7]

    def _nic_hw_label(self, iface):
        """"Vendor Model" hardware label from PCI IDs (spec's NETWORK data
        section), e.g. "Mellanox ConnectX-7". Fallback chain: built-in vendor
        table with unknown device -> "Vendor 0xdead"; unknown vendor -> the
        system's pci.ids database; nothing resolvable at all (non-PCI device,
        or pci.ids absent) -> the pre-existing driver-name label."""
        vendor_hex, device_hex = _pci_vendor_device(iface)
        if vendor_hex:
            vendor_name = PCI_VENDOR_NAMES.get(vendor_hex)
            if vendor_name:
                device_name = PCI_DEVICE_NAMES.get((vendor_hex, device_hex))
                return f"{vendor_name} {device_name}" if device_name else f"{vendor_name} 0x{device_hex}"
            looked_up = _pci_ids_lookup(vendor_hex, device_hex)
            if looked_up:
                return looked_up
        return self._driver_label(iface)

    def _carrier_up(self, iface):
        # Reading carrier raises EINVAL while the interface is admin-down
        try:
            with open(f"/sys/class/net/{iface}/carrier") as f:
                return f.read().strip() == "1"
        except Exception:
            try:
                return psutil.net_if_stats()[iface].isup
            except Exception:
                return False

    def get_interface_speed(self, iface_name):
        """Reads the negotiated link speed (Mbit/s) from sysfs, with a
        psutil fallback for non-sysfs platforms."""
        try:
            with open(f"/sys/class/net/{iface_name}/speed", "r") as f:
                return max(int(f.read().strip()), 0)
        except Exception:
            try:
                return max(psutil.net_if_stats()[iface_name].speed, 0)
            except Exception:
                return 0

    @staticmethod
    def _fmt_speed(mbit):
        return f"{mbit // 1000}G" if mbit >= 1000 else f"{mbit}M"

    def _speed_label(self, speeds):
        """'200G' for one port, '2x200G' when a physical NIC exposes several
        same-speed ports/functions, total ('400G') when speeds are mixed."""
        speeds = [s for s in speeds if s > 0] or speeds
        if len(speeds) == 1:
            return self._fmt_speed(speeds[0])
        if len(set(speeds)) == 1:
            return f"{len(speeds)}x{self._fmt_speed(speeds[0])}"
        return self._fmt_speed(sum(speeds))

    def sample(self):
        """Per physical-port group: primary netdev name (first member,
        regardless of link state), PCI-ID hardware label, PF count, link
        speed, carrier state, and RX/TX rates kept SEPARATE (the RDMA HCA
        counters are read as a (rx, tx) tuple upstream in
        _read_byte_counters -- this is where they stop being summed)."""
        curr_time = time.time()
        dt = max(curr_time - self.prev_time, 0.1)
        try:
            netdev_stats = psutil.net_io_counters(pernic=True)
        except Exception:
            netdev_stats = {}

        rate = {}
        for name in self.interfaces:
            b = self._read_byte_counters(name, netdev_stats)
            if b is None:
                continue
            p = self.prev_bytes.get(name, b)
            drx = max(b[0] - p[0], 0)  # negative delta = counter reset
            dtx = max(b[1] - p[1], 0)
            rate[name] = (drx * 8 / dt, dtx * 8 / dt)
            self.prev_bytes[name] = b

        nics = []
        for g in self.groups:
            primary = g["members"][0]
            base = {
                "name": primary, "hw_label": self._nic_hw_label(primary),
                "pf_count": len(g["members"]),
            }
            live = [m for m in g["members"] if m in rate]
            if not live:
                nics.append({**base, "up": False, "speed_mbit": 0, "speed_str": "down",
                             "rx_bps": 0, "tx_bps": 0, "rx_pct": 0, "tx_pct": 0,
                             "down_reason": "offline", "label": "Offline"})
                continue
            up = any(self._carrier_up(m) for m in live)
            # a down port reads speed 0, so it drops out of the capacity sum
            speeds = [self.get_interface_speed(m) for m in live]
            # PFs sharing one physical port each report the full port speed:
            # capacity is the port itself, not the sum across PFs
            shared_port = g["port"] is not None and len(live) > 1
            total_mbit = max(speeds) if shared_port else sum(speeds)
            if not up or total_mbit == 0:
                nics.append({**base, "up": False, "speed_mbit": 0, "speed_str": "down",
                             "rx_bps": 0, "tx_bps": 0, "rx_pct": 0, "tx_pct": 0,
                             "down_reason": "no carrier", "label": "Link Down"})
                continue
            group_rx = sum(rate[m][0] for m in live)
            group_tx = sum(rate[m][1] for m in live)
            rx_pct = (group_rx / (total_mbit * 1_000_000)) * 100
            tx_pct = (group_tx / (total_mbit * 1_000_000)) * 100
            if shared_port:
                label = f"{base['hw_label']} {g['port']} {self._fmt_speed(total_mbit)}"
            else:
                label = f"{base['hw_label']} {self._speed_label(speeds)}"
            nics.append({
                **base, "up": True, "speed_mbit": total_mbit,
                "speed_str": self._fmt_speed(total_mbit),
                "rx_bps": group_rx, "tx_bps": group_tx,
                "rx_pct": rx_pct, "tx_pct": tx_pct, "label": label,
            })

        self.prev_time = curr_time
        return nics


# =========================================================================
# Disk / storage
# =========================================================================

# Skip virtual/duplicate block devices: loop mounts, ramdisks, device-mapper
# (LVM/crypt -- these are views over a real disk already listed on its own),
# zram (compressed swap, not physical), and optical drives.
_DISK_SKIP_PREFIXES = ("loop", "ram", "dm-", "zram", "sr")

def _read_text(path):
    try:
        with open(path) as f:
            return f.read().strip() or None
    except Exception:
        return None


class DiskCollector:
    """Physical block-device discovery (/sys/block) + throughput from
    /proc/diskstats deltas + temp via matching hwmon + used% via statvfs of
    the largest mounted filesystem on that disk. Every sysfs/proc read is
    guarded; a missing sensor degrades to a hidden field (None), never a
    crash or a fabricated number."""

    def __init__(self):
        self.disks = self._detect_disks()
        self.caps = {"disks": len(self.disks)}
        stats = self._read_diskstats()
        self.prev_sectors = {name: stats[name] for name in self.disks if name in stats}
        self.prev_time = time.time()

    def _detect_disks(self):
        try:
            names = sorted(os.listdir("/sys/block"))
        except Exception:
            return []
        return [n for n in names if not n.startswith(_DISK_SKIP_PREFIXES)]

    def _model(self, name):
        return _read_text(f"/sys/block/{name}/device/model") or "Unknown"

    def _size_bytes(self, name):
        # /sys/block/<dev>/size is always in 512-byte sectors, regardless of
        # the device's actual logical block size.
        sectors = _read_int(f"/sys/block/{name}/size")
        return (sectors or 0) * 512

    def _read_diskstats(self):
        """{name: (sectors_read, sectors_written)} for every device listed in
        /proc/diskstats -- fields 3 and 7 (1-indexed after the device name):
        reads-completed, reads-merged, SECTORS-READ, time-reading,
        writes-completed, writes-merged, SECTORS-WRITTEN, ..."""
        out = {}
        try:
            with open("/proc/diskstats") as f:
                lines = f.readlines()
        except Exception:
            return out
        for line in lines:
            parts = line.split()
            if len(parts) < 10:
                continue
            try:
                out[parts[2]] = (int(parts[5]), int(parts[9]))
            except (ValueError, IndexError):
                continue
        return out

    def _disk_temp(self, name):
        """Walks /sys/class/hwmon for a "nvme"/"drivetemp" device whose
        `device` symlink resolves to the same target as the disk's own
        `device` symlink (the common case for both NVMe controllers and
        SATA/SCSI drivetemp), falling back to an nvme-controller name-prefix
        match (nvme0n1 -> nvme0) for topologies where that doesn't line up.
        Returns None (hidden field) when no match is found."""
        try:
            hwmons = os.listdir("/sys/class/hwmon")
        except Exception:
            return None
        disk_real = None
        try:
            disk_real = os.path.realpath(f"/sys/block/{name}/device")
        except Exception:
            pass
        ctrl_match = re.match(r"(nvme\d+)", name)
        for h in hwmons:
            hpath = f"/sys/class/hwmon/{h}"
            hwname = _read_text(f"{hpath}/name")
            if hwname not in ("nvme", "drivetemp"):
                continue
            matched = False
            try:
                if disk_real and os.path.realpath(f"{hpath}/device") == disk_real:
                    matched = True
            except Exception:
                pass
            if not matched and hwname == "nvme" and ctrl_match:
                try:
                    matched = ctrl_match.group(1) in os.path.realpath(hpath)
                except Exception:
                    pass
            if not matched:
                continue
            try:
                temp_files = sorted(f for f in os.listdir(hpath) if re.fullmatch(r"temp\d+_input", f))
            except Exception:
                continue
            for fn in temp_files:
                val = _read_int(f"{hpath}/{fn}")
                if val is not None:
                    return val / 1000.0
        return None

    def _read_mounts(self):
        """[(device_basename, mountpoint), ...] for every real (/dev/*) mount
        in /proc/mounts, read once per sample."""
        out = []
        try:
            with open("/proc/mounts") as f:
                for line in f:
                    parts = line.split()
                    if len(parts) >= 2 and parts[0].startswith("/dev/"):
                        out.append((os.path.basename(parts[0]), parts[1]))
        except Exception:
            pass
        return out

    def _used_pct(self, name, mounts):
        """statvfs of the LARGEST filesystem mounted from a partition of this
        disk (partition basenames start with the disk name, e.g. nvme0n1p1,
        sda1). None (hidden usage bar) when nothing of this disk is mounted.

        Matches `df`'s percentage, NOT a naive used/total: used blocks are
        f_blocks - f_bfree (all free blocks, including the root-reserved
        slice), but the denominator is used + f_bavail (bavail excludes the
        reserved slice) -- so the reserved-for-root blocks count as "used"
        capacity-wise without being double-counted as "available" too. A
        plain used/total under-reports vs. df whenever a filesystem carries
        a reserved-blocks margin (ext4's default 5%, etc)."""
        best = None
        for base, mp in mounts:
            if base != name and not base.startswith(name):
                continue
            try:
                st = os.statvfs(mp)
                total = st.f_blocks * st.f_frsize
                if not total:
                    continue
                if best is None or total > best[0]:
                    used = st.f_blocks - st.f_bfree
                    denom = used + st.f_bavail
                    # df ceils (87.76 -> 88%); match it so ours never reads
                    # one point lower than the df the user just ran
                    pct = math.ceil((used / denom) * 100) if denom else 0.0
                    best = (total, pct)
            except Exception:
                continue
        return best[1] if best else None

    def sample(self):
        curr_time = time.time()
        dt = max(curr_time - self.prev_time, 0.1)
        stats = self._read_diskstats()
        mounts = self._read_mounts()
        rows = []
        for name in self.disks:
            try:
                cur = stats.get(name)
                prev = self.prev_sectors.get(name, cur)
                if cur is not None:
                    dread = max(cur[0] - (prev[0] if prev else cur[0]), 0)
                    dwrite = max(cur[1] - (prev[1] if prev else cur[1]), 0)
                    read_bps = dread * 512 / dt
                    write_bps = dwrite * 512 / dt
                    self.prev_sectors[name] = cur
                else:
                    read_bps = write_bps = 0
                rows.append({
                    "name": name, "model": self._model(name), "size": self._size_bytes(name),
                    "temp": self._disk_temp(name), "read_bps": read_bps, "write_bps": write_bps,
                    "used_pct": self._used_pct(name, mounts),
                })
            except Exception:
                continue
        self.prev_time = curr_time
        return rows


# =========================================================================
# Thermal zones (collector only this phase -- Phase 3 wires the THERMALS
# page-2 panel and the power-rails display up to it)
# =========================================================================

class ThermalCollector:
    """Every /sys/class/thermal zone plus every named /sys/class/hwmon
    device's temp channels, as one ordered list of {"label", "temp_c",
    "kind"} dicts. Zones sharing a generic type (e.g. several unlabeled
    "acpitz" zones, as on GB10) are relabeled Zone-A, Zone-B... in zone-
    number order so they're distinguishable. An hwmon named "spbm" (GB10's
    system power/board-management rail monitor) gets its labeled channels
    sorted first and its power rails exposed separately via sample_power()
    for Phase 3."""

    def __init__(self):
        self.caps = {"spbm": self._probe_spbm()}

    def _probe_spbm(self):
        try:
            for n in os.listdir("/sys/class/hwmon"):
                if _read_text(f"/sys/class/hwmon/{n}/name") == "spbm":
                    return True
        except Exception:
            pass
        return False

    def _thermal_zones(self):
        try:
            names = os.listdir("/sys/class/thermal")
        except Exception:
            return []
        zones = []
        for n in names:
            if not n.startswith("thermal_zone"):
                continue
            zpath = f"/sys/class/thermal/{n}"
            ztype = _read_text(f"{zpath}/type")
            temp_raw = _read_int(f"{zpath}/temp")
            if ztype is None or temp_raw is None:
                continue
            try:
                idx = int(re.sub(r"\D", "", n) or "0")
            except Exception:
                idx = 0
            zones.append({"idx": idx, "type": ztype, "temp_c": temp_raw / 1000.0})
        zones.sort(key=lambda z: z["idx"])
        return zones

    def _relabel_zones(self, zones):
        counts = {}
        for z in zones:
            counts[z["type"]] = counts.get(z["type"], 0) + 1
        seen = {}
        out = []
        for z in zones:
            if counts[z["type"]] > 1:
                n = seen.get(z["type"], 0)
                seen[z["type"]] = n + 1
                label = f"Zone-{chr(ord('A') + n)}"
            else:
                label = z["type"]
            out.append({"label": label, "temp_c": z["temp_c"], "kind": "thermal"})
        return out

    def _hwmon_entries(self):
        """(entries, spbm_entries) -- temp channels from every named hwmon
        device except "acpitz" (already covered as thermal zones), split out
        separately when the hwmon is "spbm" so callers can list it first."""
        try:
            names = sorted(os.listdir("/sys/class/hwmon"))
        except Exception:
            return [], []
        entries, spbm_entries = [], []
        for n in names:
            hpath = f"/sys/class/hwmon/{n}"
            hwname = _read_text(f"{hpath}/name")
            if not hwname or hwname == "acpitz":
                continue
            try:
                files = os.listdir(hpath)
            except Exception:
                continue
            temp_inputs = sorted(f for f in files if re.fullmatch(r"temp\d+_input", f))
            chans = []
            for fn in temp_inputs:
                idx = re.sub(r"\D", "", fn)
                val = _read_int(f"{hpath}/{fn}")
                if val is None:
                    continue
                label = _read_text(f"{hpath}/temp{idx}_label")
                if not label:
                    label = hwname if len(temp_inputs) == 1 else f"{hwname}{idx}"
                chans.append({"label": label, "temp_c": val / 1000.0, "kind": "hwmon"})
            (spbm_entries if hwname == "spbm" else entries).extend(chans)
        return entries, spbm_entries

    def sample(self):
        zones = self._relabel_zones(self._thermal_zones())
        hwmon_entries, spbm_entries = self._hwmon_entries()
        # Dedupe: an hwmon channel wins over a thermal zone reporting the
        # same name (e.g. some platforms expose both).
        hwmon_labels = {e["label"] for e in hwmon_entries + spbm_entries}
        zones = [z for z in zones if z["label"] not in hwmon_labels]
        return spbm_entries + zones + hwmon_entries

    def sample_power(self):
        """[{"label": rail name, "watts": float}, ...] from the "spbm" hwmon's
        powerN_label/powerN_input channels (microwatts), for Phase 3's
        power-rails display. Empty list when no spbm hwmon exists."""
        out = []
        try:
            names = os.listdir("/sys/class/hwmon")
        except Exception:
            return out
        for n in names:
            hpath = f"/sys/class/hwmon/{n}"
            if _read_text(f"{hpath}/name") != "spbm":
                continue
            try:
                files = os.listdir(hpath)
            except Exception:
                continue
            for fn in sorted(f for f in files if re.fullmatch(r"power\d+_input", f)):
                idx = re.sub(r"\D", "", fn)
                val = _read_int(f"{hpath}/{fn}")
                if val is None:
                    continue
                label = _read_text(f"{hpath}/power{idx}_label") or f"rail{idx}"
                out.append({"label": label, "watts": val / 1_000_000.0})
        return out
