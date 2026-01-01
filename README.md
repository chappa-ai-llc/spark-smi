# SPARK-SMI

**A specialized terminal-based system monitor (TUI) for NVIDIA Grace and Grace Blackwell (GB10) architectures.**

SPARK-SMI is a lightweight, zero-dependency (apart from standard Python libraries) monitoring tool designed to visualize the unique topology of NVIDIA "Spark" class systems. It correctly handles hybrid CPU clusters (Performance vs. Efficiency cores) and Unified Memory architectures, providing a clean, responsive dashboard right in your terminal.

## Screenshots

![Main View](screenshots/main_view.png)

## Features

* **Snapshot Mode (Default):** Runs once and prints the dashboard to stdout (preserving colors), just like `nvidia-smi`. Great for logging or quick checks.
* **Interactive Mode (`-l`):** Launches a live, flicker-free TUI that refreshes every second.
* **Hybrid CPU Support:** Correctly identifies and visualizes load across Cortex-X vs Cortex-A clusters.
* **Unified Memory Aware:** Detects Grace Blackwell (GB10) unified memory architectures.
* **NVML Integration:** Uses `nvidia-ml-py` if available for high-speed metrics, with automatic legacy fallback.
* **Robust:** Adapts to window resizing and missing sensors (like DGX fan controllers) without crashing.

## Prerequisites

* Linux OS
* Python 3.6+
* NVIDIA Drivers installed

## Installation

### Option 1: Quick Run (Virtual Environment)
The safest way to run on DGX appliances without touching system libraries.

1.  **Clone the repository:**
    ```bash
    git clone [https://github.com/EvilTCell/spark-smi.git](https://github.com/EvilTCell/spark-smi.git)
    cd spark-smi
    ```

2.  **Set up environment:**
    ```bash
    python3 -m venv venv
    ./venv/bin/pip install -r requirements.txt
    ```

3.  **Run:**
    ```bash
    # Run once (Snapshot)
    ./venv/bin/python3 spark-smi.py

    # Run live (Loop)
    ./venv/bin/python3 spark-smi.py -l
    ```

### Option 2: Install as System Command (Recommended)
This allows you to just type `spark-smi` from any folder.

1.  Follow the "Quick Run" steps above to set up the folder.
2.  Add an alias to your shell configuration (e.g., `~/.bashrc`):
    ```bash
    echo "alias spark-smi='~/spark-smi/venv/bin/python3 ~/spark-smi/spark-smi.py'" >> ~/.bashrc
    source ~/.bashrc
    ```

## Usage

| Command | Action |
| :--- | :--- |
| `spark-smi` | Print a single snapshot and exit |
| `spark-smi -l` | Enter interactive loop mode |
| `spark-smi -n 0.5 -l` | Loop with 0.5s refresh rate |

**Interactive Controls:**
* **`q`**: Quit
* **`t`**: Toggle Temp (C/F)
* **`u`**: Toggle Units (Decimal/Binary)

## Roadmap & Future Goals

- [ ] **Spark Cortex / GB10 Fan Monitoring:** Investigate methods to read chassis fan speeds on Grace Blackwell architectures without requiring `sudo`/root privileges (currently relies on `nvsm` or IPMI which are protected).
- [ ] **REST API Service:** Decouple the monitoring engine from the UI to expose a lightweight HTTP endpoint (JSON). This would allow SPARK-SMI to serve as a data exporter for Grafana, Prometheus, or web dashboards.
- [ ] **Logging Mode:** Add a flag (e.g., `--csv`) to output raw data to stdout for piping into logs instead of rendering the TUI.

## License

MIT License - see the [LICENSE](LICENSE) file for details.
