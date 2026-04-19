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

class NetMonitor:
    def __init__(self):
        try: self.prev_stats = psutil.net_io_counters(pernic=True)
        except: self.prev_stats = {}
        self.prev_time = time.time()
        # Mapping for DGX Spark: 1-4 are Mellanox (MT2910), 5 is Realtek
        self.mapping = ["enp1s0f0np0", "enp1s0f1np1", "enP2p1s0f0np0", "enP2p1s0f1np1", "enP7s7"]

    def get_interface_speed(self, iface_name):
        """Reads the negotiated link speed from sysfs."""
        try:
            with open(f"/sys/class/net/{iface_name}/speed", "r") as f:
                speed = int(f.read().strip())
                return speed
        except: 
            return 0

    def poll(self):
        curr_time = time.time()
        dt = max(curr_time - self.prev_time, 0.1)
        try: curr_stats = psutil.net_io_counters(pernic=True)
        except: return []
        
        nics = []
        for i, name in enumerate(self.mapping):
            if name in curr_stats:
                s, p = curr_stats[name], self.prev_stats.get(name, curr_stats[name])
                bps = (((s.bytes_recv + s.bytes_sent) - (p.bytes_recv + p.bytes_sent)) * 8) / dt
                
                # Get dynamic speed from system
                speed_mbit = self.get_interface_speed(name)
                limit_bps = speed_mbit * 1_000_000
                
                # Format Speed String (e.g., 200G, 40G, 10G, 1G)
                if speed_mbit >= 1000:
                    speed_display = f"{speed_mbit // 1000}G"
                else:
                    speed_display = f"{speed_mbit}M"

                # Determine Label based on Device Index (1-4: MT2910, 5: Realtek)
                if i < 4:
                    label = f"MT2910 {speed_display}"
                else:
                    label = f"Realtek {speed_display}"
                
                if speed_mbit == 0:
                    label = "Link Down"

                nics.append({"label": label, "usage": (bps / limit_bps) * 100 if limit_bps > 0 else 0})
            else: 
                nics.append({"label": "Offline", "usage": 0})
                
        self.prev_stats, self.prev_time = curr_stats, curr_time
        return nics

monitor = NetMonitor()

class VirtualCurses:
    def __init__(self):
        self.update_dims()
        self.grid = [[(" ", None) for _ in range(self.cols)] for _ in range(self.rows)]
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

def make_bar(percent, width, color_good, color_mid, color_bad):
    if width < 3: return "[]", None
    pct = max(0, min(int(percent), 100))
    inner_w = width - 2
    filled = int((pct / 100.0) * inner_w)
    bar_str = "[" + "|" * filled + " " * (inner_w - filled) + "]"
    c = color_good if pct < 50 else (color_mid if pct < 80 else color_bad)
    return bar_str, c

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
                        
                        # Power and Fan specific fallbacks
                        if gpu["pwr_str"] == "N/A" and "N/A" not in r[5]:
                            gpu["pwr_str"] = f"{int(float(r[5]))}W"
                        if (gpu["fan"] in ["N/A", "None"]) and "N/A" not in r[6]:
                            gpu["fan"] = f"{r[6]}%"
                except:
                    pass

            # 4. GB10 / Architecture Special Cases
            if "GB10" in gpu['name'] or gpu['mem_used'] in ["N/A", 0]:
                sys_ram = psutil.virtual_memory()
                gpu["mem_used"], gpu["mem_total"] = sys_ram.used, sys_ram.total
                if "Unified" not in gpu['name']: gpu['name'] += " (Unified)"
                if "GB10" in gpu['name']: gpu["fan"] = "None"

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
    
    p1 = f"| 0    {'Cortex-X925 (00-09)':<20} | {int(sum(cpu[0:10])/10):>3}%  | {temp_str:<4} {fan_disp:<4} | {'Shared':<9} | {f'RAM:{fmt_mem(ram.used)}/{fmt_mem(ram.total)}':<21} | "
    draw_row_parts(stdscr, y+2, start_x, draw_w, [p1], (ram.percent, colors_map), colors_map, h)
    draw_line(stdscr, y+3, start_x, draw_w, h)
    
    p2 = f"| 1    {'Cortex-A725 (10-19)':<20} | {int(sum(cpu[10:20])/10):>3}%  | {temp_str:<4} {fan_disp:<4} | {'Shared':<9} | {f'Swp:{fmt_mem(swap.used)}/{fmt_mem(swap.total)}':<21} | "
    draw_row_parts(stdscr, y+4, start_x, draw_w, [p2], (swap.percent, colors_map), colors_map, h)
    draw_line(stdscr, y+5, start_x, draw_w, h)
    y += 6
    
    for name, cores, offset in [("Performance Cluster", cpu[0:10], 0), ("Efficiency Cluster", cpu[10:20], 10)]:
        if y >= h: break
        stdscr.addstr(y, start_x, f"| {name} " + "-" * (draw_w - len(name) - 4) + "|")
        y += 1
        cols, col_w = 4, (draw_w - 4) // 4
        for i, p in enumerate(cores):
            r, c = i // cols, i % cols
            if y + r >= h: break
            cx = start_x + 4 + (c * col_w)
            bar, color = make_bar(p, col_w - 9, colors_map[1], colors_map[5], colors_map[4])
            try:
                if c == 0: stdscr.addstr(y + r, start_x, "|")
                stdscr.addstr(y + r, cx, f"{i+offset:02}", colors_map[2])
                stdscr.addstr(y + r, cx+3, bar, color)
                stdscr.addstr(y + r, cx+3+len(bar)+1, f"{int(p)}%", colors_map[3])
                stdscr.addstr(y + r, start_x + draw_w - 1, "|")
            except: pass
        y += 3

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
            if ("GB10" not in gpu['name']) and (fan_val in ["0%", "N/A", "Err", "0"]):
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
            if "GB10" in gpu['name']: display_fan = "None" # Keep GB10 clean
            # --- NEW FAN LOGIC END ---

            mem_str = f"{fmt_mem(gpu['mem_used'])}/{fmt_mem(gpu['mem_total'])}".ljust(20)
        
            # Trailing | is split as a separate part so the bar gains 1 char of width
            # and renders as "| [bar]" with a space between the pipe and bracket
            p_row = f"| {gpu['id']:<4} {gpu['name'][:26]:<26} | {fmt_temp(gpu['temp']):<5} {display_fan:<4} | {gpu['pwr_str']:<11} | {mem_str}"
        
            draw_row_parts(stdscr, y, start_x, draw_w, [p_row, "| "], (gpu['util'], colors_map), colors_map, h, gpu_mode=True)
            draw_line(stdscr, y+1, start_x, draw_w, h)
            y += 2

    nics = monitor.poll()
    if y + 2 < h:
        stdscr.addstr(y, start_x, "|")
        stdscr.addstr(y+1, start_x, "|")
        for i, n in enumerate(nics):
            base_x = start_x + 1 + (i * 21)
            if base_x + 20 < start_x + draw_w:
                stdscr.addstr(y, base_x, f" {i+1} {n['label']:<12} {int(n['usage']):>3}%  ")
                bar, col = make_bar(n['usage'], 19, colors_map[1], colors_map[5], colors_map[4])
                stdscr.addstr(y+1, base_x, " ")
                stdscr.addstr(y+1, base_x + 1, bar, col)
        stdscr.addstr(y, start_x + draw_w - 1, "|")
        stdscr.addstr(y+1, start_x + draw_w - 1, "|")
        draw_line(stdscr, y+2, start_x, draw_w, h)
        y += 3

    if y + 2 < h:
        driver, cuda = get_driver_info_safe()
        l1 = f" Ver: {VERSION} | Kernel: {platform.release()} | Driver: {driver} | CUDA: {cuda}"
        l2 = " Controls: 'q':Quit | 'u':Unit | 't':Temp | Mode: Live"
        stdscr.addstr(y, start_x, "|"); stdscr.addstr(y, start_x+1, l1.ljust(draw_w-2), colors_map[2]); stdscr.addstr(y, start_x+draw_w-1, "|")
        stdscr.addstr(y+1, start_x, "|"); stdscr.addstr(y+1, start_x+1, l2.ljust(draw_w-2), colors_map[3]); stdscr.addstr(y+1, start_x+draw_w-1, "|")
        draw_line(stdscr, y+2, start_x, draw_w, h)

def main_loop(stdscr):
    global USE_FAHRENHEIT, USE_DECIMAL_UNITS
    import curses
    curses.start_color(); curses.use_default_colors(); curses.curs_set(0); stdscr.nodelay(True)
    colors = {i: curses.color_pair(i) for i in range(1, 6)}
    for i, cl in enumerate([curses.COLOR_GREEN, curses.COLOR_CYAN, curses.COLOR_WHITE, curses.COLOR_RED, curses.COLOR_YELLOW], 1):
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
            if ch == curses.KEY_RESIZE: stdscr.clear(); break
            time.sleep(0.05)

if __name__ == "__main__":
    if "-l" in sys.argv or "--loop" in sys.argv:
        import curses
        try: curses.wrapper(main_loop)
        except KeyboardInterrupt: pass
    else:
        v = VirtualCurses()
        render_dashboard(v, {i:i for i in range(1,6)})
        print(v.render())
