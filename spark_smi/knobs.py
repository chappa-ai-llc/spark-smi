"""Phase 4: interactive power/clock control knobs for page 2 (live mode
only -- app.py never constructs a KnobUI for the snapshot path, so this
module is simply unused there and nothing it does can affect `-l`-less runs).

Two halves:
  - build_registry(sample) is a pure function, called fresh every render tick
    from the current sample + capabilities. It returns a plain list of knob
    descriptor dicts (id/kind/gpu_id/label/unit/step/min/max/current/
    writable/reason) -- no state, no writes. pages.py reads these to decide
    what to draw; nothing here draws anything.
  - KnobUI is the small piece of session state main_loop owns across ticks:
    which GPU is selected, which knob (if any) is focused, a pending value
    while stepping, a pending confirm arm, and a short-lived toast after
    apply. Its methods are pure state transitions keyed by short string
    actions (not curses key codes) so they can be driven by a scripted-key
    test harness without curses or real hardware.

apply_knob(knob, value) is the only function that actually writes anything.
Every backend it dispatches to is wrapped in its own try/except AND called
from apply_knob's own try/except -- one bad write must never take the render
loop down (matches the "errors swallowed broadly" convention elsewhere in
this codebase). NOTHING here writes unless KnobUI.confirm_yes() calls it,
which only happens after the user has pressed 'y' at a footer confirm
prompt raised by Enter/R/X -- see app.py's key handling.
"""
import os
import shutil
import subprocess

from . import collectors

# Rail controller ids, in the cycle order 'P' steps through after the
# selected GPU's own power-limit knob (spec: limit -> pl1 -> pl2 -> syspl1
# -> syspl2). Mirrors pages.py's _CONTROLLER_ORDER for the POWER RAILS panel.
_CONTROLLER_ORDER = ["pl1", "pl2", "syspl1", "syspl2"]

# spbm's powerN_cap files have no kernel-exposed minimum (only *_cap_max) --
# this fraction is a software safety floor, NOT a hardware constant, chosen
# to keep the SoC from being driven to a value likely to destabilize it.
# Flagged for the architect to confirm/adjust against real hardware.
RAIL_MIN_FRACTION = 0.25

# Clock-lock knobs have no NVML min/max query wired up (would need
# nvmlDeviceGetSupportedGraphicsClocks/MemoryClocks, a discrete list, not a
# range) -- these floors are conservative software defaults, not
# hardware-derived. The spec calls this out as acceptable ("optimistic ...
# error surfaced on apply"): an out-of-range value just makes nvidia-smi
# reject it, which shows up as a normal toast, not a crash.
_SM_CLOCK_FLOOR_MHZ = 300.0
_MEM_CLOCK_FLOOR_MHZ = 405.0


# =========================================================================
# Registry: pure, read-only, rebuilt every render tick
# =========================================================================

def build_registry(sample):
    """[{id, kind, gpu_id, gpu_idx, label, unit, step, min, max, current,
    writable, reason, ...}, ...] describing every knob available THIS tick.
    Gating mirrors the capability rules in CLAUDE.md/SPEC-2.0.md:
      - gpu_power: only when caps.power_limit is True (GB10 is excluded
        automatically -- verified N/A on unified SoCs) and NVML reported a
        [min, max] constraint and a current value.
      - clk_sm/clk_mem/reset_clocks/persist: only when nvidia-smi exists,
        the GPU isn't a unified SoC (caps.mem_local True), and caps.clocks
        is True (a free proxy for "this driver build's clock APIs work" --
        cheap enough to check for zero extra probing since it's already
        collected).
      - rail_power (pl1/pl2/syspl1/syspl2): only when an spbm hwmon exists
        and that rail reported both a current cap and a cap_max ceiling.
    Never raises -- each GPU/rail is wrapped individually so one bad entry
    can't blank the whole registry."""
    registry = []
    gpus = sample.get("gpus") or []
    gpu_caps = (sample.get("caps") or {}).get("gpu") or []
    has_smi = bool(shutil.which("nvidia-smi"))

    for idx, gpu in enumerate(gpus):
        try:
            caps = gpu_caps[idx] if idx < len(gpu_caps) else {}
            gid = gpu.get("id", str(idx))
            unified = not caps.get("mem_local", True)

            if caps.get("power_limit"):
                lo, hi, cur = gpu.get("power_min_mw"), gpu.get("power_max_mw"), gpu.get("power_limit_mw")
                writable = lo is not None and hi is not None and cur is not None
                registry.append({
                    "id": f"gpu:{gid}:power", "kind": "gpu_power", "gpu_id": gid, "gpu_idx": idx,
                    "label": f"GPU {gid} power limit", "unit": "W", "step": 5.0,
                    "min": (lo / 1000.0) if lo is not None else None,
                    "max": (hi / 1000.0) if hi is not None else None,
                    "current": (cur / 1000.0) if cur is not None else None,
                    "writable": writable, "reason": None if writable else "range unavailable",
                })

            if has_smi and not unified and caps.get("clocks"):
                sm_cur = gpu.get("clk_sm")
                if isinstance(sm_cur, (int, float)):
                    sm_max = gpu.get("clk_sm_max")
                    sm_max = float(sm_max) if isinstance(sm_max, (int, float)) and sm_max > 0 \
                        else max(float(sm_cur), _SM_CLOCK_FLOOR_MHZ)
                    registry.append({
                        "id": f"gpu:{gid}:clk_sm", "kind": "clk_sm", "gpu_id": gid, "gpu_idx": idx,
                        "label": f"GPU {gid} sm clock lock", "unit": "MHz", "step": 100.0,
                        "min": min(_SM_CLOCK_FLOOR_MHZ, sm_max), "max": sm_max,
                        "current": float(sm_cur), "writable": True, "reason": None,
                    })
                mem_cur = gpu.get("clk_mem")
                if isinstance(mem_cur, (int, float)):
                    mem_max = gpu.get("clk_mem_max")
                    mem_max = float(mem_max) if isinstance(mem_max, (int, float)) and mem_max > 0 \
                        else max(float(mem_cur), _MEM_CLOCK_FLOOR_MHZ)
                    registry.append({
                        "id": f"gpu:{gid}:clk_mem", "kind": "clk_mem", "gpu_id": gid, "gpu_idx": idx,
                        "label": f"GPU {gid} mem clock lock", "unit": "MHz", "step": 100.0,
                        "min": min(_MEM_CLOCK_FLOOR_MHZ, mem_max), "max": mem_max,
                        "current": float(mem_cur), "writable": True, "reason": None,
                    })
                registry.append({
                    "id": f"gpu:{gid}:reset", "kind": "reset_clocks", "gpu_id": gid, "gpu_idx": idx,
                    "label": f"GPU {gid} reset clocks", "unit": None, "step": None,
                    "min": None, "max": None, "current": None, "writable": True, "reason": None,
                })
            # Persistence sits outside the clock-lock gate: `nvidia-smi -pm`
            # works on unified SoCs too (GB10 ships with it enabled)
            if has_smi and gpu.get("persistence") is not None:
                registry.append({
                    "id": f"gpu:{gid}:persist", "kind": "persist", "gpu_id": gid, "gpu_idx": idx,
                    "label": f"GPU {gid} persistence", "unit": None, "step": None,
                    "min": None, "max": None,
                    "current": 1.0 if gpu.get("persistence") in ("on", "enabled") else 0.0,
                    "writable": True, "reason": None,
                })
        except Exception:
            continue

    thermal_caps = (sample.get("caps") or {}).get("thermal") or {}
    if thermal_caps.get("spbm"):
        rails = sample.get("power_rails") or []
        access = _probe_root_access()
        for label in _CONTROLLER_ORDER:
            try:
                r = next((r for r in rails if r.get("label") == label), None)
                if r is None or r.get("cap_w") is None or r.get("cap_max_w") is None:
                    continue
                path = r.get("cap_path")
                writable = bool(path) and (access["root"] or access["sudo"]
                                           or _sysfs_writable(path))
                cap_max = r["cap_max_w"]
                registry.append({
                    "id": f"rail:{label}", "kind": "rail_power", "gpu_id": None, "gpu_idx": None,
                    "label": label, "unit": "W", "step": 5.0,
                    "min": max(1.0, cap_max * RAIL_MIN_FRACTION), "max": cap_max,
                    "current": r["cap_w"], "writable": writable,
                    "reason": None if writable else "root required",
                    "cap_path": path,
                })
            except Exception:
                continue

    return registry


# =========================================================================
# Root/sudo probing -- cached once per process, never touches the GPU
# =========================================================================

_ROOT_ACCESS_CACHE = None

def _probe_root_access():
    """{"root": bool, "sudo": bool} -- real root, or passwordless sudo.
    Probed once and cached (a `sudo -n true` subprocess call on every render
    tick would be wasteful and, on a flaky sudo config, laggy). This only
    determines whether a knob CAN be applied later; it never writes
    anything itself, so it's safe to call from the read-only registry build
    too (used to grey out spbm rail knobs before the user ever presses a
    key)."""
    global _ROOT_ACCESS_CACHE
    if _ROOT_ACCESS_CACHE is not None:
        return _ROOT_ACCESS_CACHE
    is_root = False
    try:
        is_root = hasattr(os, "geteuid") and os.geteuid() == 0
    except Exception:
        is_root = False
    sudo_ok = False
    if not is_root:
        try:
            res = subprocess.run(["sudo", "-n", "true"], capture_output=True, timeout=2)
            sudo_ok = res.returncode == 0
        except Exception:
            sudo_ok = False
    _ROOT_ACCESS_CACHE = {"root": is_root, "sudo": sudo_ok}
    return _ROOT_ACCESS_CACHE


def _sysfs_writable(path):
    try:
        return os.access(path, os.W_OK)
    except Exception:
        return False


def _sudo_wrap(cmd, access):
    """Prefixes `sudo -n` only when we're not already root and passwordless
    sudo probed OK; otherwise the bare command is returned so a genuine
    permission failure surfaces naturally as nvidia-smi's own stderr rather
    than being masked by a sudo prompt that would hang (nodelay/-n means it
    fails fast instead)."""
    if access.get("root") or not access.get("sudo"):
        return cmd
    return ["sudo", "-n"] + cmd


def _run_cmd(args):
    try:
        res = subprocess.run(args, capture_output=True, text=True, timeout=5)
        return res.returncode == 0, res.stdout, res.stderr
    except Exception as e:
        return False, "", str(e)


def _first_line(text):
    text = (text or "").strip()
    return text.splitlines()[0][:70] if text else "apply failed"


def _read_int_safe(path):
    try:
        with open(path, "r") as f:
            return int(f.read().strip())
    except Exception:
        return None


def _write_sysfs(path, text):
    """Direct write, falling back to `sudo -n tee` so rail knobs work from a
    normal user session when passwordless sudo is configured (running the
    whole TUI as root just for one sysfs write is a bad trade)."""
    try:
        with open(path, "w") as f:
            f.write(text)
        return True, None
    except PermissionError:
        access = _probe_root_access()
        if access.get("sudo"):
            try:
                res = subprocess.run(["sudo", "-n", "tee", path], input=text.encode(),
                                     capture_output=True, timeout=3)
                if res.returncode == 0:
                    return True, None
                return False, _first_line(res.stderr.decode(errors="replace")) or "sudo tee failed"
            except Exception as e:
                return False, str(e)[:70]
        return False, "permission denied — root required"
    except Exception as e:
        return False, str(e)[:70]


# =========================================================================
# Apply backends -- each individually guarded; apply_knob() adds one more
# outer net. NOTHING in this module is called except from KnobUI.confirm_yes,
# which only runs after an explicit 'y' at a footer confirm prompt.
# =========================================================================

def _nvml_set_power_limit(gid, mw):
    handle = collectors.pynvml.nvmlDeviceGetHandleByIndex(int(gid))
    collectors.pynvml.nvmlDeviceSetPowerManagementLimit(handle, mw)
    return collectors.pynvml.nvmlDeviceGetPowerManagementLimit(handle)


def _cli_read_power_limit_mw(gid):
    ok, out, _err = _run_cmd(["nvidia-smi", f"--id={gid}", "--query-gpu=power.limit",
                               "--format=csv,noheader,nounits"])
    if not ok:
        return None
    try:
        return float(out.strip().split()[0]) * 1000.0
    except Exception:
        return None


def _apply_gpu_power(knob, watts):
    """NVML nvmlDeviceSetPowerManagementLimit when euid==0 and NVML is
    present; otherwise `nvidia-smi -i <id> -pl <watts>` (sudo -n prefixed
    when not root)."""
    gid = knob["gpu_id"]
    mw = int(round(watts * 1000))
    access = _probe_root_access()
    if access["root"] and collectors.HAS_NVML:
        try:
            new_mw = _nvml_set_power_limit(gid, mw)
            new_w = new_mw / 1000.0
            return True, f"applied: GPU {gid} power limit → {new_w:.0f} W", new_w
        except Exception as e:
            return False, _first_line(f"nvml set failed: {e}"), knob.get("current")
    cmd = _sudo_wrap(["nvidia-smi", "-i", str(gid), "-pl", str(int(round(watts)))], access)
    ok, out, err = _run_cmd(cmd)
    if not ok:
        return False, _first_line(err or out), knob.get("current")
    new_mw = _cli_read_power_limit_mw(gid)
    new_w = (new_mw / 1000.0) if new_mw is not None else watts
    return True, f"applied: GPU {gid} power limit → {new_w:.0f} W", new_w


def _apply_rail_power(knob, watts):
    """Writes the microwatt value straight to the spbm powerN_cap sysfs
    file (hwmon convention; verified against the known-good 140 W pl1 cap
    by collectors.ThermalCollector.sample_power's existing /1_000_000
    conversion)."""
    path = knob.get("cap_path")
    if not path:
        return False, "no cap path for this rail", knob.get("current")
    uw = int(round(watts * 1_000_000))
    ok, err = _write_sysfs(path, str(uw))
    if not ok:
        return False, err, knob.get("current")
    new_uw = _read_int_safe(path)
    new_w = (new_uw / 1_000_000.0) if new_uw is not None else watts
    return True, f"applied: {knob['label']} → {new_w:.0f} W", new_w


def _apply_clock_lock(knob, which, mhz):
    gid = knob["gpu_id"]
    flag = "-lgc" if which == "sm" else "-lmc"
    v = int(round(mhz))
    access = _probe_root_access()
    cmd = _sudo_wrap(["nvidia-smi", "-i", str(gid), flag, f"{v},{v}"], access)
    ok, out, err = _run_cmd(cmd)
    if not ok:
        return False, _first_line(err or out), knob.get("current")
    return True, f"applied: GPU {gid} {which} lock → {v} MHz", float(v)


def _apply_reset_clocks(knob):
    gid = knob["gpu_id"]
    access = _probe_root_access()
    cmd = _sudo_wrap(["nvidia-smi", "-i", str(gid), "-rgc", "-rmc"], access)
    ok, out, err = _run_cmd(cmd)
    if not ok:
        return False, _first_line(err or out), None
    return True, f"applied: GPU {gid} clocks reset", None


def _apply_persistence(knob, enable):
    gid = knob["gpu_id"]
    access = _probe_root_access()
    cmd = _sudo_wrap(["nvidia-smi", "-i", str(gid), "-pm", "1" if enable else "0"], access)
    ok, out, err = _run_cmd(cmd)
    if not ok:
        return False, _first_line(err or out), knob.get("current")
    return True, f"applied: GPU {gid} persistence → {'on' if enable else 'off'}", 1.0 if enable else 0.0


def apply_knob(knob, value):
    """The only function that writes anything. Clamps numeric knobs to
    [min, max] and refuses non-positive values, dispatches to the matching
    backend, and always returns (ok, message, new_current) -- it never
    raises past this point, mirroring the "swallow broadly, keep rendering"
    convention used throughout collectors.py."""
    try:
        kind = knob.get("kind")
        if kind in ("gpu_power", "rail_power", "clk_sm", "clk_mem"):
            lo, hi = knob.get("min"), knob.get("max")
            if lo is None or hi is None or value is None:
                return False, "no valid range for this knob", knob.get("current")
            value = max(lo, min(hi, float(value)))
            if value <= 0:
                return False, "refused: non-positive value", knob.get("current")
        if kind == "gpu_power":
            return _apply_gpu_power(knob, value)
        if kind == "rail_power":
            return _apply_rail_power(knob, value)
        if kind == "clk_sm":
            return _apply_clock_lock(knob, "sm", value)
        if kind == "clk_mem":
            return _apply_clock_lock(knob, "mem", value)
        if kind == "reset_clocks":
            return _apply_reset_clocks(knob)
        if kind == "persist":
            return _apply_persistence(knob, bool(value))
        return False, "unknown knob kind", knob.get("current")
    except Exception as e:
        return False, _first_line(f"error: {e}"), knob.get("current")


# =========================================================================
# KnobUI: session state machine, pure transitions keyed by short string
# actions -- app.py's main_loop translates curses key codes into calls on
# this; the simulated-keys test harness drives it the same way, with no
# curses import at all.
# =========================================================================

_NUMERIC_KINDS = ("gpu_power", "rail_power", "clk_sm", "clk_mem")

class KnobUI:
    TOAST_TICKS = 3

    def __init__(self):
        self.selected_gpu = 0
        self.focus_id = None
        self.pending = None
        self.confirming = False
        self.confirm_text = None
        self.toast = None
        self.toast_ticks = 0
        # Cached by app.py's render_dashboard every tick so main_loop's key
        # handling (which runs between renders) always dispatches against
        # the registry the user is actually looking at.
        self.last_registry = []
        self.n_gpus = 0

    @staticmethod
    def _by_id(registry, kid):
        if kid is None:
            return None
        return next((k for k in registry if k.get("id") == kid), None)

    def _gpu_id(self, registry):
        for k in registry:
            if k.get("gpu_idx") == self.selected_gpu:
                return k.get("gpu_id")
        return str(self.selected_gpu)

    def cancel(self):
        self.focus_id = None
        self.pending = None
        self.confirming = False
        self.confirm_text = None

    def select_next_gpu(self, n_gpus):
        if n_gpus <= 0:
            return
        self.selected_gpu = (self.selected_gpu + 1) % n_gpus
        self.cancel()

    def _focus(self, registry, kid):
        knob = self._by_id(registry, kid)
        if knob is None:
            return
        self.focus_id = kid
        self.pending = knob.get("current")
        self.confirming = False
        self.confirm_text = None

    def focus_power_cycle(self, registry):
        """'P': cycles the selected GPU's power-limit knob -> pl1 -> pl2 ->
        syspl1 -> syspl2, skipping any id absent from this tick's registry.
        Landing on this from some other focus (e.g. a clock knob) jumps to
        the first present entry rather than trying to find continuity."""
        gid = self._gpu_id(registry)
        order = [f"gpu:{gid}:power", "rail:pl1", "rail:pl2", "rail:syspl1", "rail:syspl2"]
        present = [kid for kid in order if self._by_id(registry, kid) is not None]
        if not present:
            return
        if self.focus_id in present:
            i = present.index(self.focus_id)
            nxt = present[(i + 1) % len(present)]
        else:
            nxt = present[0]
        self._focus(registry, nxt)

    def focus_clock(self, registry, which):
        """'C'/'M': focus the selected GPU's sm/mem clock-lock knob, if the
        registry has one this tick (absent on unified SoCs / no nvidia-smi)."""
        gid = self._gpu_id(registry)
        kid = f"gpu:{gid}:clk_{which}"
        if self._by_id(registry, kid) is not None:
            self._focus(registry, kid)

    @staticmethod
    def write_warning():
        """Banner shown on EVERY confirm prompt that would write hardware.

        Power limits and clock locks change the operating point of real
        silicon: they can destabilise a running workload, and sustained
        operation outside the vendor's defaults is squarely the operator's
        responsibility. The project ships under the MIT licence's "AS IS,
        without warranty of any kind" terms -- this makes that concrete at
        the moment of the write rather than only in a licence file nobody
        reads mid-session."""
        return "! WARNING: writes hardware - no warranty, at your own risk"

    def _confirm_text_for(self, knob, pending):
        kind = knob.get("kind")
        if kind == "reset_clocks":
            return f"apply {knob['label']}? y/N"
        if kind == "persist":
            return f"apply {knob['label']} → {'on' if pending else 'off'}? y/N"
        unit = knob.get("unit") or ""
        val = pending if pending is not None else knob.get("current")
        try:
            val_txt = f"{float(val):.0f} {unit}".strip()
        except Exception:
            val_txt = str(val)
        return f"apply {knob['label']} → {val_txt}? y/N"

    def arm_reset(self, registry):
        """'R': resets both sm+mem locks -- no value to step, arms confirm
        directly."""
        gid = self._gpu_id(registry)
        knob = self._by_id(registry, f"gpu:{gid}:reset")
        if knob is None:
            return
        self.focus_id = knob["id"]
        self.pending = None
        self.confirming = True
        self.confirm_text = self._confirm_text_for(knob, None)

    def arm_persist(self, registry):
        """'X': toggles persistence -- no value to step, arms confirm
        directly with the flipped target already computed."""
        gid = self._gpu_id(registry)
        knob = self._by_id(registry, f"gpu:{gid}:persist")
        if knob is None:
            return
        target = 0.0 if knob.get("current") else 1.0
        self.focus_id = knob["id"]
        self.pending = target
        self.confirming = True
        self.confirm_text = self._confirm_text_for(knob, target)

    def step(self, registry, direction):
        """Left/Right arrows: steps the pending value by the knob's step,
        clamped to [min, max]. No-op once a confirm is armed (arrows only
        adjust the pending value BEFORE Enter, matching the spec's
        focus -> step -> Enter -> y/N sequence) or for non-numeric knobs."""
        if self.focus_id is None or self.confirming:
            return
        knob = self._by_id(registry, self.focus_id)
        if knob is None or knob.get("kind") not in _NUMERIC_KINDS:
            return
        step = knob.get("step") or 1.0
        lo, hi = knob.get("min"), knob.get("max")
        base = self.pending if self.pending is not None else (knob.get("current") or 0.0)
        val = base + direction * step
        if lo is not None:
            val = max(lo, val)
        if hi is not None:
            val = min(hi, val)
        self.pending = val

    def enter(self, registry):
        """Enter: arms confirm for the focused numeric knob. R/X arm their
        own (non-numeric) knobs directly and don't go through here."""
        if self.focus_id is None or self.confirming:
            return
        knob = self._by_id(registry, self.focus_id)
        if knob is None or knob.get("kind") not in _NUMERIC_KINDS:
            return
        self.confirming = True
        self.confirm_text = self._confirm_text_for(knob, self.pending)

    def _toast(self, text, slot):
        self.toast = (str(text)[:70], slot)
        self.toast_ticks = self.TOAST_TICKS

    def confirm_yes(self, registry, apply_fn=None):
        """'y' while a confirm is armed: dispatches to apply_fn (default
        apply_knob), sets the toast from its result, and clears focus.
        Anything other than 'y' while confirming should call cancel()
        instead -- app.py's key handling does that for every other key.
        Returns (ok, message, new_value) mainly for the test harness; the
        toast is the real UI-facing effect."""
        if not self.confirming or self.focus_id is None:
            return None
        knob = self._by_id(registry, self.focus_id)
        pending = self.pending
        self.cancel()
        if knob is None:
            return None
        if not knob.get("writable", True):
            self._toast(f"blocked: {knob.get('reason') or 'not writable'}", 4)
            return False, knob.get("reason"), knob.get("current")
        value = pending if pending is not None else knob.get("current")
        fn = apply_fn or apply_knob
        try:
            ok, msg, new_val = fn(knob, value)
        except Exception as e:
            ok, msg, new_val = False, _first_line(f"error: {e}"), knob.get("current")
        self._toast(msg, 1 if ok else 4)
        return ok, msg, new_val

    def tick(self):
        """Called once per render frame (not per keypress) to age out the
        toast after ~3 ticks."""
        if self.toast_ticks > 0:
            self.toast_ticks -= 1
            if self.toast_ticks <= 0:
                self.toast = None

    def render_ctx(self):
        """Plain dict handed to pages.build_page2 -- keeps pages.py free of
        any KnobUI-class coupling, consistent with how the rest of this app
        threads plain dicts/lists from collectors into page builders."""
        return {
            "registry": self.last_registry, "selected_gpu": self.selected_gpu,
            "focus_id": self.focus_id, "pending": self.pending,
            "confirming": self.confirming, "confirm_text": self.confirm_text,
            "confirm_warn": self.write_warning() if self.confirming else None,
            "toast": self.toast,
        }
