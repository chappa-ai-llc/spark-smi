import time
import subprocess
import csv
import io
import psutil
import argparse
import shutil
import re
import platform
import os
import sys
from datetime import datetime

# --- NVML Import Strategy ---
try:
    import pynvml
    HAS_NVML = True
except ImportError:
    HAS_NVML = False

# --- Configuration ---
VERSION = "3.2.0-shipping"
REFRESH_RATE = 1.0
MAX_WIDTH = 110
GRAPH_WIDTH = 30 

# Toggles
USE_FAHRENHEIT = False
USE_DECIMAL_UNITS = False 

# --- Virtual Curses for Snapshot Mode ---
class VirtualCurses:
    """Emulates a curses window but writes to a string buffer with ANSI colors."""
    def __init__(self):
        self.rows = 50
        self.cols = 120
        try:
            self.cols, self.rows = shutil.get_terminal_size()
        except: pass
        self.grid = [[(" ", None) for _ in range(self.cols)] for _ in range(self.rows)]
        self.colors = {
            1: "\033[32m", # Green
            2: "\033[36m", # Cyan
            3: "\033[37m", # White
            4: "\033[31m", # Red
            5: "\033[33m", # Yellow
            0: "\033[0m"   # Reset
        }

    def getmaxyx(self):
        return self.rows, self.cols

    def erase(self):
        self.grid = [[(" ", None) for _ in range(self.cols)] for _ in range(self.rows)]

    def addstr(self, y, x, text, attr=None):
        if y >= self.rows or x >= self.cols: return
        
        text_safe = str(text)
        for i, char in enumerate(text_safe):
            if x + i < self.cols:
                self.grid[y][x + i] = (char, attr)

    def refresh(self):
        pass 

    def render(self):
        output = []
        last_row = 0
        for r in range(self.rows):
            if any(c[0] != " " for c in self.grid[r]):
                last_row = r
        
        for r in range(last_row + 1):
            row_str = ""
            current_fmt = None
            for c, fmt in self.grid[r]:
                if fmt != current_fmt:
                    if fmt in self.colors: row_str += self.colors[fmt]
                    else: row_str += self.colors[0]
                    current_fmt = fmt
                row_str += c
            row_str += self.colors[0] 
            output.append(row_str)
        return "\n".join(output)

# --- Formatters ---
def fmt_temp(celsius_val):
    try:
        c = float(celsius_val)
        if USE_FAHRENHEIT: return f"{int((c * 9/5) + 32)}F"
        return f"{int(c)}C"
    except: return "N/A"

def fmt_mem(bytes_val):
    if bytes_val is None: return "N/A"
    try:
        div = 1000.0 if USE_DECIMAL_UNITS else 1024.0
        s_m = "MB" if USE_DECIMAL_UNITS else "MiB"
        s_g = "GB" if USE_DECIMAL_UNITS else "GiB"
        if bytes_val > (div**3): return f"{bytes_val/(div**3):.1f}{s_g}"
        else: return f"{int(bytes_val/(div**2))}{s_m}"
    except: return "N/A"

def make_bar(percent, width, color_good, color_mid, color_bad):
    if width < 3: return "[]", None
    try: pct = int(percent)
    except: pct = 0
    inner_w = width - 2
    filled = int((pct / 100.0) * inner_w)
    filled = max(0, min(filled, inner_w))
    bar_str = "[" + "|" * filled + " " * (inner_w - filled) + "]"
    c = color_good
    if pct > 50: c = color_mid
    if pct > 80: c = color_bad
    return bar_str, c

# --- Data Fetching ---
def get_cpu_temp():
    try:
        temps = psutil.sensors_temperatures()
        if not temps: return "N/A"
        for k in ['cpu_thermal', 'soc_thermal', 'coretemp', 'thermal_zone0']:
            if k in temps: return temps[k][0].current
        return list(temps.values())[0][0].current
    except: return "N/A"

def get_system_fan():
    if shutil.which("nvsm"):
        try:
            cmd = ["sudo", "-n", "nvsm", "show", "fans"] 
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=1)
            if res.returncode == 0:
                match = re.search(r"(\d+)\s*RPM", res.stdout, re.IGNORECASE)
                if match: return f"{match.group(1)} RPM"
        except: pass
    if shutil.which("ipmitool"):
        try:
            cmd = ["ipmitool", "sdr", "type", "Fan"]
            res = subprocess.run(cmd, capture_output=True, text=True)
            if res.returncode == 0:
                highest = 0
                for line in res.stdout.splitlines():
                    if "RPM" in line:
                        try:
                            val = int(line.split('|')[1].strip().split()[0])
                            if val > highest: highest = val
                        except: continue
                if highest > 0: return f"{highest} RPM"
        except: pass
    if shutil.which("sensors"):
        try:
            res = subprocess.run(["sensors"], capture_output=True, text=True)
            highest = 0
            for line in res.stdout.splitlines():
                if "RPM" in line:
                    try:
                        m = re.search(r'(\d+)', line)
                        if m:
                            val = int(m.group(1))
                            if val > highest: highest = val
                    except: continue
            if highest > 500: return f"{highest} RPM"
        except: pass
    return "Err"

def get_driver_info_safe():
    driver, cuda = "Unknown", "Unknown"
    if HAS_NVML:
        try:
            pynvml.nvmlInit()
            driver = pynvml.nvmlSystemGetDriverVersion().decode('utf-8')
            cuda_ver = pynvml.nvmlSystemGetCudaDriverVersion()
            cuda = f"{cuda_ver // 1000}.{(cuda_ver % 1000) // 10}"
            return driver, cuda
        except: pass
    try:
        res = subprocess.run(["nvidia-smi"], capture_output=True, text=True)
        if res.returncode == 0:
            out = res.stdout
            d = re.search(r"Driver Version:\s*([\d\.]+)", out)
            c = re.search(r"CUDA Version:\s*([\d\.]+)", out)
            if d: driver = d.group(1)
            if c: cuda = c.group(1)
    except: pass
    return driver, cuda

def query_single_gpu(gpu_id, fields):
    cmd = ["nvidia-smi", f"--id={gpu_id}", f"--query-gpu={fields}", "--format=csv,noheader,nounits"]
    return subprocess.run(cmd, capture_output=True, text=True, check=True)

def get_gpu_data(current_driver, current_cuda):
    if not shutil.which("nvidia-smi"): return "Error: 'nvidia-smi' not found.", current_driver, current_cuda
    try:
        cmd = ["nvidia-smi", "-L"]
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        gpu_ids = [line.split(":")[0].replace("GPU","").strip() for line in result.stdout.strip().split('\n') if line.startswith("GPU")]
    except: return "Error listing GPUs", current_driver, current_cuda

    gpus = []
    new_driver, new_cuda = current_driver, current_cuda
    fallback_fan = get_system_fan()

    if new_driver == "Unknown":
        d, c = get_driver_info_safe()
        if d != "Unknown": new_driver, new_cuda = d, c

    for gid in gpu_ids:
        gpu = {"id": gid, "name": "Unknown", "temp": "N/A", "util": "0", "mem_used": "N/A", "mem_total": "N/A", "pwr_str": "N/A", "fan": "N/A"}
        if HAS_NVML:
            try:
                pynvml.nvmlInit()
                handle = pynvml.nvmlDeviceGetHandleByIndex(int(gid))
                gpu["name"] = pynvml.nvmlDeviceGetName(handle)
                if isinstance(gpu["name"], bytes): gpu["name"] = gpu["name"].decode('utf-8')
                gpu["temp"] = pynvml.nvmlDeviceGetTemperature(handle, pynvml.NVML_TEMPERATURE_GPU)
                gpu["util"] = pynvml.nvmlDeviceGetUtilizationRates(handle).gpu
                mem = pynvml.nvmlDeviceGetMemoryInfo(handle)
                gpu["mem_used"], gpu["mem_total"] = mem.used, mem.total
                try:
                    draw = int(pynvml.nvmlDeviceGetPowerUsage(handle) / 1000.0)
                    limit = int(pynvml.nvmlDeviceGetEnforcedPowerLimit(handle) / 1000.0)
                    gpu["pwr_str"] = f"{draw}/{limit}W"
                except:
                    try: gpu["pwr_str"] = f"{int(pynvml.nvmlDeviceGetPowerUsage(handle)/1000.0)} W"
                    except: gpu["pwr_str"] = "N/A"
                try: gpu["fan"] = f"{pynvml.nvmlDeviceGetFanSpeed(handle)}%"
                except: pass
            except: pass
        
        fan_val = str(gpu["fan"])
        if "N/A" in fan_val or "Not Supported" in fan_val: gpu["fan"] = fallback_fan
        is_unified = "GB10" in gpu['name'] or gpu['mem_used'] == "N/A"
        if is_unified:
            sys_ram = psutil.virtual_memory()
            gpu["mem_used"], gpu["mem_total"] = sys_ram.used, sys_ram.total
            if "Unified" not in gpu['name']: gpu['name'] += " (Unified)"
        gpus.append(gpu)
    return gpus, new_driver, new_cuda

# --- Drawing Logic (Shared) ---
def draw_line(stdscr, y, x, width):
    try: stdscr.addstr(y, x, "+" + "-" * (width - 2) + "+")
    except: pass

def draw_row_parts(stdscr, y, x, width, parts, graph_data=None, colors_map=None):
    try:
        curr_x = x
        for p in parts:
            stdscr.addstr(y, curr_x, p)
            curr_x += len(p)
        if graph_data:
            pct_val, col_tuple = graph_data
            reserved, space_left = 8, (x + width - 1) - curr_x
            graph_w = space_left - reserved
            if graph_w > 2:
                bar, color = make_bar(pct_val, graph_w, colors_map[1], colors_map[5], colors_map[4])
                stdscr.addstr(y, curr_x, bar, color)
                stdscr.addstr(y, curr_x + len(bar), f" {pct_val:>5.1f}%")
        stdscr.addstr(y, x + width - 1, "|")
    except: pass

def render_dashboard(stdscr, colors_map, driver, cuda, is_loop=False):
    # Responsive Width
    h, w = stdscr.getmaxyx()
    draw_w = min(w, MAX_WIDTH)
    start_x = max(0, (w - draw_w) // 2)
    
    # 1. Header
    now = datetime.now().strftime("%H:%M:%S")
    header_right = f"Ref: {REFRESH_RATE}s" if is_loop else "Tip: 'spark-smi -l' for live mode"
    try:
        stdscr.addstr(0, start_x, f"SPARK-SMI  {now}", colors_map[3])
        # Align right
        stdscr.addstr(0, start_x+draw_w-len(header_right), header_right, colors_map[2])
        draw_line(stdscr, 1, start_x, draw_w)
    except: pass
    
    # 2. CPU
    y = 2
    cpu = psutil.cpu_percent(percpu=True)
    count = len(cpu)
    if count == 20:
        c1n, c2n = "Cortex-X925 (00-09)", "Cortex-A725 (10-19)"
        l1, l2 = sum(cpu[0:10])/10, sum(cpu[10:20])/10
    else:
        mid = count // 2
        c1n, c2n = "Cluster 0", "Cluster 1"
        l1, l2 = sum(cpu[:mid])/mid, sum(cpu[mid:])/(count-mid)
        
    temp_str = fmt_temp(get_cpu_temp())
    ram, swap = psutil.virtual_memory(), psutil.swap_memory()
    sys_fan = get_system_fan()
    fan_disp = sys_fan[:4] if len(sys_fan) > 4 else sys_fan
    
    header = f"| CPU  {'Name':<20} | Load  | Temp Fan  | {'Power(W)':<9} | {'RAM / Swap':<21} | Util"
    header += " " * (draw_w - len(header) - 1) + "|"
    try:
        stdscr.addstr(y, start_x, header); draw_line(stdscr, y+1, start_x, draw_w)
    except: pass
    
    # CPU Rows
    p1 = f"| 0    {c1n:<20} | {int(l1):>3}%  | {temp_str:<4} {fan_disp:<4} | {'Shared':<9} | {f'RAM:{fmt_mem(ram.used)}/{fmt_mem(ram.total)}':<21} | "
    draw_row_parts(stdscr, y+2, start_x, draw_w, [p1], (ram.percent, colors_map), colors_map)
    draw_line(stdscr, y+3, start_x, draw_w)
    p2 = f"| 1    {c2n:<20} | {int(l2):>3}%  | {temp_str:<4} {fan_disp:<4} | {'Shared':<9} | {f'Swp:{fmt_mem(swap.used)}/{fmt_mem(swap.total)}':<21} | "
    draw_row_parts(stdscr, y+4, start_x, draw_w, [p2], (swap.percent, colors_map), colors_map)
    draw_line(stdscr, y+5, start_x, draw_w)
    y += 6
    
    # 3. CPU Graphs
    splits = []
    if count == 20: splits = [("Performance Cluster", cpu[0:10], 0), ("Efficiency Cluster", cpu[10:20], 10)]
    else: splits = [("CPU Cores", cpu, 0)]
    
    for name, cores, offset in splits:
        try: stdscr.addstr(y, start_x, f"| {name} " + "-" * (draw_w - len(name) - 4) + "|")
        except: pass
        y += 1
        cols = 4
        col_w = (draw_w - 4) // cols
        for i, p in enumerate(cores):
            r, c = i // cols, i % cols
            curr_row_y = y + r
            cx = start_x + 2 + (c * col_w)
            bar, color = make_bar(p, col_w - 9, colors_map[1], colors_map[5], colors_map[4])
            try:
                if c == 0: stdscr.addstr(curr_row_y, start_x, "|")
                stdscr.addstr(curr_row_y, cx, f"{i+offset:02}", colors_map[2])
                stdscr.addstr(curr_row_y, cx+3, bar, color)
                stdscr.addstr(curr_row_y, cx+3+len(bar)+1, f"{int(p)}%", colors_map[3])
                if c == cols - 1 or i == len(cores) - 1: stdscr.addstr(curr_row_y, start_x + draw_w - 1, "|")
            except: pass
        y += (len(cores) + cols - 1) // cols
        
    # 4. GPU Table
    gpus, driver, cuda = get_gpu_data(driver, cuda)
    header = f"| GPU  {'Name':<26} | Temp  Fan | {'Power (W)':<11} | {'Memory-Usage':<19} | GPU-Util"
    header += " " * (draw_w - len(header) - 1) + "|"
    try: stdscr.addstr(y, start_x, header); draw_line(stdscr, y+1, start_x, draw_w)
    except: pass
    y += 2
    
    for gpu in gpus:
        name = (gpu['name'][:26]).ljust(26)
        temp = fmt_temp(gpu['temp'])
        fan = str(gpu['fan'])
        if "RPM" in fan: fan = fan.replace("RPM","")[:4]
        elif "%" in fan: fan = fan
        elif fan.isdigit(): fan = f"{fan}%"
        else: fan = "Err"
        if len(fan) > 4: fan = fan[:4]
        
        pwr = str(gpu.get('pwr_str', 'N/A'))
        if len(pwr) > 11: pwr = pwr[:11]
        
        try: util = float(gpu['util'])
        except: util = 0.0
        try: mem_str = f"{fmt_mem(gpu['mem_used'])} / {fmt_mem(gpu['mem_total'])}"
        except: mem_str = "N/A"
        
        p1 = f"| {gpu['id']:<4} {name} | {temp:<5} {fan:<4} | {pwr:<11} | {mem_str:<19} | "
        draw_row_parts(stdscr, y, start_x, draw_w, [p1], (util, colors_map), colors_map)
        draw_line(stdscr, y+1, start_x, draw_w)
        y += 2
        
    # 5. Footer
    k_ver = platform.release()
    line1 = f" Ver: {VERSION} | Kernel: {k_ver} | Driver: {driver} | CUDA: {cuda}"
    if len(line1) > draw_w - 2: line1 = line1[:draw_w-4] + ".."
    line2 = " Controls: 'q':Quit | 'u':Unit | 't':Temp | Mode: " + ("NVML" if HAS_NVML else "Legacy")
    
    try:
        stdscr.addstr(y, start_x, "|"); stdscr.addstr(y, start_x+1, line1.ljust(draw_w-2), colors_map[2]); stdscr.addstr(y, start_x+draw_w-1, "|")
        stdscr.addstr(y+1, start_x, "|"); stdscr.addstr(y+1, start_x+1, line2.ljust(draw_w-2), colors_map[3]); stdscr.addstr(y+1, start_x+draw_w-1, "|")
        draw_line(stdscr, y+2, start_x, draw_w)
    except: pass
    
    return driver, cuda

def main_loop(stdscr):
    global REFRESH_RATE, USE_FAHRENHEIT, USE_DECIMAL_UNITS
    import curses
    curses.start_color()
    curses.use_default_colors()
    curses.curs_set(0)
    stdscr.nodelay(True)
    
    # Curses color map
    colors = {
        1: curses.color_pair(1), 2: curses.color_pair(2),
        3: curses.color_pair(3), 4: curses.color_pair(4), 5: curses.color_pair(5)
    }
    # Init pairs
    for i, c in enumerate([curses.COLOR_GREEN, curses.COLOR_CYAN, curses.COLOR_WHITE, curses.COLOR_RED, curses.COLOR_YELLOW], 1):
        curses.init_pair(i, c, curses.COLOR_BLACK)
    
    driver, cuda = get_driver_info_safe()
    
    while True:
        stdscr.erase()
        driver, cuda = render_dashboard(stdscr, colors, driver, cuda, is_loop=True)
        stdscr.refresh()
        
        start = time.time()
        while time.time() - start < REFRESH_RATE:
            k = stdscr.getch()
            if k == ord('q'): return
            if k == ord('u'): USE_DECIMAL_UNITS = not USE_DECIMAL_UNITS
            if k == ord('t'): USE_FAHRENHEIT = not USE_FAHRENHEIT
            time.sleep(0.05)

def main_snapshot():
    # ANSI color map for snapshot
    colors = {
        1: 1, 2: 2, 3: 3, 4: 4, 5: 5
    }
    vscr = VirtualCurses()
    driver, cuda = get_driver_info_safe()
    render_dashboard(vscr, colors, driver, cuda, is_loop=False)
    print(vscr.render())

if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("-l", "--loop", action="store_true", help="Loop mode (Interactive)")
    p.add_argument("-n", "--interval", type=float, default=1.0)
    args = p.parse_args()
    REFRESH_RATE = args.interval
    
    if args.loop:
        import curses
        curses.wrapper(main_loop)
    else:
        main_snapshot()
