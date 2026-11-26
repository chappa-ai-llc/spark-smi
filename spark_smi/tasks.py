"""Page 5 -- GPU task manager.

Discovers every process holding a GPU compute/graphics context (NVML first,
`nvidia-smi --query-compute-apps` as the CLI fallback, an optional
/proc/<pid>/fd scan for processes that merely hold /dev/nvidia* open), then
resolves the *terminal session* each one lives in by walking its ancestor
chain and cgroup: a tmux pane, a screen session, a zellij session, a
systemd unit, a docker/podman container, an ssh login, a bare tty -- or
nothing at all, which is how an orphan (parent gone, reparented to init
after its tmux pane closed) is detected.

Three layers, same shape knobs.py / fabtest.py use:

  TaskCollector.sample(gpus)   pure read: plain dicts, one tree root per
                                GPU, session groups below it, processes
                                below those. Never signals anything.
  flatten(tree, sort, flat,     pure: tree -> flat selectable row list the
          filt)                 page builder renders and TaskUI moves over.
  ProcTracker                   per-process history (SM, GPU memory, CPU)
                                and the recently-ended list, fed every tick
                                by State while a screen is drawn. Memory
                                only, never on the cluster wire.
  proc_detail(pid)              pure read of one process's facts for the
                                detail pane (cwd, threads, device files,
                                a fixed list of GPU environment variables).
  TaskUI                        live-mode-only session state (selection,
                                confirm arm, toast); pure state transitions.
  end_process / end_group /     the ONLY functions that signal a process
  open_group_argv               or run a session manager command, and
                                end_* run only after TaskUI.confirm_yes().

Snapshot mode never constructs a TaskUI, so `--page 5` can only ever read.
"""
import os
import re
import shutil
import signal
import subprocess
import time
from collections import deque

import psutil

from . import collectors
from . import knobs

try:
    import pynvml
except Exception:  # pragma: no cover - collectors already decided HAS_NVML
    pynvml = None

_PROCFS = os.path.isdir("/proc") and os.name == "posix"
_TMUX_TTL = 2.0
_DOCKER_TTL = 5.0
_FDSCAN_TTL = 5.0

# Group kinds, in the order groups are listed under a GPU: orphans first
# (they're the actionable thing this page exists for), then attachable
# sessions, then everything else.
KIND_ORDER = ["orphan", "tmux", "screen", "zellij", "docker", "systemd", "ssh", "tty", "none"]


# =========================================================================
# /proc helpers (Linux). Every reader returns None on any failure -- a pid
# can exit between the listing and the read, and that must never propagate.
# =========================================================================

def _read_stat(pid):
    """{"comm","state","ppid","pgid","sid","tty_nr"} from /proc/<pid>/stat,
    parsing comm by its LAST ')' so names containing spaces/parens ("tmux:
    server", "Web Content") don't shift the numeric fields."""
    try:
        with open(f"/proc/{pid}/stat") as f:
            s = f.read()
        l, r = s.index("("), s.rindex(")")
        fields = s[r + 2:].split()
        return {"pid": int(pid), "comm": s[l + 1:r], "state": fields[0], "ppid": int(fields[1]),
                "pgid": int(fields[2]), "sid": int(fields[3]), "tty_nr": int(fields[4])}
    except Exception:
        return None


def _tty_name(tty_nr):
    """Kernel dev_t -> "pts/N" / "ttyN" / None (no controlling terminal)."""
    if not tty_nr:
        return None
    major = (tty_nr >> 8) & 0xFFF
    minor = (tty_nr & 0xFF) | ((tty_nr >> 12) & 0xFFF00)
    if 136 <= major <= 143:
        return f"pts/{minor + (major - 136) * 256}"
    if major == 4:
        return f"tty{minor}" if minor < 64 else f"ttyS{minor - 64}"
    return f"dev {major}:{minor}"


def _read_cgroup_unit(pid):
    """(unit, path, under_user_manager): unit is the last path component of
    the process's (v2, else first v1) cgroup -- "ollama.service",
    "tmux-spawn-<uuid>.scope", "docker-<id>.scope", "session-12.scope"."""
    try:
        with open(f"/proc/{pid}/cgroup") as f:
            lines = [ln.strip() for ln in f if ln.strip()]
    except Exception:
        return None, None, False
    path = None
    for ln in lines:
        if ln.startswith("0::"):
            path = ln[3:]
            break
    if path is None and lines:
        path = lines[0].split(":", 2)[-1]
    if not path:
        return None, None, False
    unit = path.rstrip("/").rsplit("/", 1)[-1]
    return unit, path, ("/user.slice/" in path or "user@" in path)


_CONTAINER_RE = re.compile(r"(?:docker-|libpod-|cri-containerd-)?([0-9a-f]{12,64})(?:\.scope)?$")


def _container_id(unit, path):
    """Docker / podman / containerd id from the cgroup, or None."""
    if not path:
        return None
    if "docker" in path or "libpod" in path or "containerd" in path:
        m = _CONTAINER_RE.search(unit or "")
        if m:
            return m.group(1)
        m = re.search(r"/docker/([0-9a-f]{12,64})", path)
        if m:
            return m.group(1)
    return None


def _uid_name(uid):
    try:
        import pwd
        return pwd.getpwuid(uid).pw_name
    except Exception:
        return str(uid)


# =========================================================================
# Session-manager inventories (each cached for a couple of seconds -- one
# subprocess per tick on page 5 only is fine, one per process is not).
# =========================================================================

class _Cached:
    def __init__(self, ttl, fn):
        self.ttl, self.fn = ttl, fn
        self.ts, self.val = 0.0, None

    def get(self):
        now = time.time()
        if self.val is None or now - self.ts > self.ttl:
            try:
                self.val = self.fn()
            except Exception:
                self.val = {}
            self.ts = now
        return self.val


def _tmux_panes():
    """{pane_pid: {"session","window","window_name","pane","pane_id","tty"}}
    for THIS user's default tmux server (other users' servers aren't
    listable; those panes fall back to a "tmux (server of X)" group via the
    "tmux: server" ancestor comm)."""
    if not shutil.which("tmux"):
        return {}
    fmt = "#{pane_pid}\t#{session_name}\t#{window_index}\t#{window_name}\t#{pane_index}\t#{pane_id}\t#{pane_tty}"
    res = subprocess.run(["tmux", "list-panes", "-a", "-F", fmt], capture_output=True, text=True, timeout=2)
    out = {}
    if res.returncode != 0:
        return out
    for ln in res.stdout.splitlines():
        parts = ln.split("\t")
        if len(parts) < 7:
            continue
        try:
            out[int(parts[0])] = {"session": parts[1], "window": parts[2], "window_name": parts[3],
                                  "pane": parts[4], "pane_id": parts[5], "tty": parts[6].replace("/dev/", "")}
        except ValueError:
            continue
    return out


_SCREEN_RE = re.compile(r"^\s*(\d+)\.(\S+)\s+.*\((Attached|Detached|Multi)")


def _screen_sessions():
    """{pid: {"name", "state"}} from `screen -ls` (exit code is non-zero
    when there are none, so it's parsed regardless)."""
    if not shutil.which("screen"):
        return {}
    res = subprocess.run(["screen", "-ls"], capture_output=True, text=True, timeout=2)
    out = {}
    for ln in res.stdout.splitlines():
        m = _SCREEN_RE.match(ln)
        if m:
            out[int(m.group(1))] = {"name": m.group(2), "state": m.group(3).lower()}
    return out


def _docker_names():
    """{container_id_prefix12: name} via `docker ps` -- only when the docker
    CLI is present; podman users get the bare id."""
    if not shutil.which("docker"):
        return {}
    res = subprocess.run(["docker", "ps", "--format", "{{.ID}}\t{{.Names}}"],
                         capture_output=True, text=True, timeout=3)
    out = {}
    if res.returncode != 0:
        return out
    for ln in res.stdout.splitlines():
        parts = ln.split("\t")
        if len(parts) == 2:
            out[parts[0][:12]] = parts[1]
    return out


# =========================================================================
# GPU process discovery
# =========================================================================

def _nvml_gpu_pids(n_gpus):
    """{gpu_idx: {pid: mem_bytes|None}} via NVML compute+graphics lists."""
    out = {}
    if not collectors.HAS_NVML or pynvml is None:
        return out
    try:
        count = pynvml.nvmlDeviceGetCount()
        n = min(n_gpus, count) if n_gpus else count
    except Exception:
        return out
    for i in range(n):
        try:
            h = pynvml.nvmlDeviceGetHandleByIndex(i)
        except Exception:
            continue
        try:
            procs = collectors._gpu_processes(h)
        except Exception:
            procs = []
        out[i] = {p["pid"]: p.get("mem") for p in procs if p.get("pid") is not None}
    return out


def _cli_gpu_pids():
    """Same shape as _nvml_gpu_pids via nvidia-smi CSV queries (compute apps
    only -- the CLI has no graphics-apps query). Used when NVML is absent."""
    out = {}
    if not shutil.which("nvidia-smi"):
        return out
    try:
        res = subprocess.run(["nvidia-smi", "--query-gpu=index,uuid", "--format=csv,noheader"],
                             capture_output=True, text=True, timeout=5)
        uuid_to_idx = {}
        for ln in res.stdout.splitlines():
            parts = [p.strip() for p in ln.split(",")]
            if len(parts) == 2 and parts[0].isdigit():
                uuid_to_idx[parts[1]] = int(parts[0])
        res = subprocess.run(["nvidia-smi", "--query-compute-apps=pid,used_memory,gpu_uuid",
                              "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=5)
        for ln in res.stdout.splitlines():
            parts = [p.strip() for p in ln.split(",")]
            if len(parts) < 3 or not parts[0].isdigit():
                continue
            idx = uuid_to_idx.get(parts[2])
            if idx is None:
                continue
            mem = None
            try:
                mem = int(float(parts[1])) * 1024 * 1024
            except ValueError:
                pass
            out.setdefault(idx, {})[int(parts[0])] = mem
    except Exception:
        pass
    return out


_NVIDIA_DEV_RE = re.compile(r"^/dev/nvidia(\d+)$")


def _minor_to_index():
    """/dev/nvidiaN minor -> NVML index, via each GPU's UUID in
    /proc/driver/nvidia/gpus/*/information. Falls back to minor == index."""
    out = {}
    uuid_idx = {}
    try:
        if collectors.HAS_NVML and pynvml is not None:
            for i in range(pynvml.nvmlDeviceGetCount()):
                u = pynvml.nvmlDeviceGetUUID(pynvml.nvmlDeviceGetHandleByIndex(i))
                if isinstance(u, bytes):
                    u = u.decode("utf-8", "replace")
                uuid_idx[u] = i
    except Exception:
        pass
    try:
        for d in os.listdir("/proc/driver/nvidia/gpus"):
            try:
                with open(f"/proc/driver/nvidia/gpus/{d}/information") as f:
                    txt = f.read()
            except Exception:
                continue
            mm = re.search(r"Device Minor:\s*(\d+)", txt)
            mu = re.search(r"GPU UUID:\s*(\S+)", txt)
            if mm:
                minor = int(mm.group(1))
                out[minor] = uuid_idx.get(mu.group(1), minor) if mu else minor
    except Exception:
        pass
    return out


def _fd_scan_gpu_pids(minor_map):
    """{gpu_idx: {pid: None}} for every readable process holding a
    /dev/nvidiaN fd. Only the caller's own processes are readable without
    root, and NVML-only tools (this very program's --serve agent) hold the
    device open too -- which is why this is opt-in ('a' key), tagged
    "device open", and never the default source of truth."""
    out = {}
    if not _PROCFS:
        return out
    try:
        pids = [p for p in os.listdir("/proc") if p.isdigit()]
    except Exception:
        return out
    for p in pids:
        fd_dir = f"/proc/{p}/fd"
        try:
            fds = os.listdir(fd_dir)
        except Exception:
            continue
        hit = set()
        for fd in fds:
            try:
                target = os.readlink(f"{fd_dir}/{fd}")
            except Exception:
                continue
            m = _NVIDIA_DEV_RE.match(target)
            if m:
                minor = int(m.group(1))
                hit.add(minor_map.get(minor, minor))
        for idx in hit:
            out.setdefault(idx, {})[int(p)] = None
    return out


# =========================================================================
# TaskCollector
# =========================================================================

class TaskCollector:
    """Probes what's available once (`.caps`), then sample(gpus) builds the
    per-GPU session tree from the current sample's GPU list. Owns a psutil
    Process cache so cpu_percent() deltas span ticks."""

    def __init__(self):
        self.caps = {
            "nvml": bool(collectors.HAS_NVML),
            "cli": bool(shutil.which("nvidia-smi")),
            "procfs": _PROCFS,
            "tmux": bool(shutil.which("tmux")) and _PROCFS,
            "screen": bool(shutil.which("screen")) and _PROCFS,
            "docker": bool(shutil.which("docker")),
            "systemd": os.path.isdir("/run/systemd/system"),
            "fd_scan": _PROCFS,
        }
        self._pcache = {}
        self._tmux = _Cached(_TMUX_TTL, _tmux_panes)
        self._screen = _Cached(_TMUX_TTL, _screen_sessions)
        self._docker = _Cached(_DOCKER_TTL, _docker_names)
        self._minor_map = None
        self._fd_cache = _Cached(_FDSCAN_TTL, self._fd_scan)
        self.last_ts = 0.0

    # --- discovery --------------------------------------------------------

    def _fd_scan(self):
        if self._minor_map is None:
            self._minor_map = _minor_to_index()
        return _fd_scan_gpu_pids(self._minor_map)

    def _gpu_pids(self, n_gpus, include_holders):
        per_gpu = {}
        src = "none"
        if self.caps["nvml"]:
            per_gpu = _nvml_gpu_pids(n_gpus)
            src = "NVML"
        if not per_gpu and self.caps["cli"]:
            per_gpu = _cli_gpu_pids()
            src = "CLI"
        holders = set()
        if include_holders and self.caps["fd_scan"]:
            for idx, pids in (self._fd_cache.get() or {}).items():
                bucket = per_gpu.setdefault(idx, {})
                for pid in pids:
                    if pid not in bucket:
                        bucket[pid] = None
                        holders.add((idx, pid))
        return per_gpu, src, holders

    # --- per-process facts -------------------------------------------------

    def _proc_info(self, pid):
        """Cross-platform basics via psutil (cached Process objects so
        cpu_percent is a real delta), plus /proc facts on Linux."""
        info = {"pid": pid, "name": "", "cmd": "", "user": "?", "cpu": 0.0, "rss": 0,
                "age": None, "status": "", "ppid": None, "alive": True}
        p = self._pcache.get(pid)
        try:
            if p is None or not p.is_running():
                p = psutil.Process(pid)
                self._pcache[pid] = p
            with p.oneshot():
                info["name"] = p.name() or ""
                try:
                    args = p.cmdline() or []
                except Exception:
                    args = []
                try:
                    info["user"] = p.username() or "?"
                except Exception:
                    pass
                try:
                    info["cpu"] = p.cpu_percent(None)
                except Exception:
                    pass
                try:
                    info["rss"] = p.memory_info().rss
                except Exception:
                    pass
                try:
                    info["age"] = max(0.0, time.time() - p.create_time())
                except Exception:
                    pass
                try:
                    info["status"] = p.status()
                except Exception:
                    pass
                try:
                    info["ppid"] = p.ppid()
                except Exception:
                    pass
            if args:
                head = os.path.basename(args[0]) if args[0] else ""
                rest = " ".join(a for a in args[1:] if a)
                info["cmd"] = (head + (" " + rest if rest else "")).strip()
        except psutil.NoSuchProcess:
            info["alive"] = False
        except Exception:
            pass
        if info["user"] and "\\" in info["user"]:
            info["user"] = info["user"].rsplit("\\", 1)[-1]
        if not info["name"]:
            info["name"] = collectors._proc_name(pid) or str(pid)
        return info

    def _chain(self, pid, table):
        """(own stat, [ancestor stats nearest-first]) -- stops at pid 1."""
        chain = []
        seen = set()
        cur = _read_stat(pid)
        if cur is None:
            return None, chain
        table[pid] = cur
        ppid = cur["ppid"]
        while ppid > 0 and ppid not in seen:
            seen.add(ppid)
            st = table.get(ppid) or _read_stat(ppid)
            if st is None:
                break
            table[ppid] = st
            chain.append(st)
            if st["pid"] == 1:
                break
            ppid = st["ppid"]
        return cur, chain

    def _resolve_session(self, pid, info, table):
        """The session group this process belongs to: {"kind","key","label",
        "detail", ...} plus per-process orphan/tty facts. Precedence:
        container > multiplexer pane > systemd unit > ssh > orphan > tty."""
        if not _PROCFS:
            parent = info.get("ppid")
            pname = ""
            try:
                pname = psutil.Process(parent).name() if parent else ""
            except Exception:
                pname = ""
            label = f"spawned by {pname}" if pname else "no terminal session"
            return {"kind": "none", "key": f"none:{pname or '?'}", "label": label, "detail": ""}, {}

        cur, chain = self._chain(pid, table)
        facts = {}
        if cur is None:
            return {"kind": "none", "key": "none:?", "label": "no terminal session", "detail": ""}, facts
        tty = _tty_name(cur["tty_nr"])
        facts["tty"] = tty
        facts["state"] = cur["state"]
        facts["pgid"] = cur["pgid"]
        if tty and tty.startswith("pts/") and not os.path.exists(f"/dev/{tty}"):
            facts["tty_gone"] = True

        unit, cpath, user_scope = _read_cgroup_unit(pid)
        cid = _container_id(unit, cpath)
        if cid:
            names = self._docker.get() or {}
            name = names.get(cid[:12])
            label = f"docker {name}" if name else f"container {cid[:12]}"
            return {"kind": "docker", "key": f"docker:{cid[:12]}", "label": label,
                    "detail": cid[:12] if name else "", "cid": cid, "name": name}, facts

        panes = self._tmux.get() or {}
        screens = self._screen.get() or {}
        # The process itself first: a workload started as a pane's own
        # command (no shell between) IS the pane's process.
        for a in [cur] + chain:
            apid, comm = a["pid"], a["comm"]
            if apid in panes:
                pn = panes[apid]
                target = f"{pn['session']}:{pn['window']}.{pn['pane']}"
                label = f"tmux {pn['session']}:{pn['window']} {pn['window_name']}"
                detail = f"pane {pn['pane']} · {pn['tty']}"
                return {"kind": "tmux", "key": f"tmux:{pn['pane_id']}", "label": label, "detail": detail,
                        "target": target, "pane_id": pn["pane_id"], "session": pn["session"],
                        "window": f"{pn['session']}:{pn['window']}", "attachable": True}, facts
            if comm.startswith("tmux"):
                owner = "?"
                try:
                    owner = _uid_name(os.stat(f"/proc/{apid}").st_uid)
                except Exception:
                    pass
                return {"kind": "tmux", "key": f"tmux:server:{apid}", "label": f"tmux (server of {owner})",
                        "detail": f"pid {apid} · not this user's server", "attachable": False}, facts
            if comm.lower() == "screen" and apid in screens:
                sc = screens[apid]
                return {"kind": "screen", "key": f"screen:{apid}", "label": f"screen {apid}.{sc['name']}",
                        "detail": sc["state"], "spid": apid, "attachable": True}, facts
            if comm == "zellij":
                sess = ""
                try:
                    with open(f"/proc/{apid}/cmdline", "rb") as f:
                        argv = f.read().split(b"\0")
                    sess = os.path.basename(argv[-2].decode("utf-8", "replace")) if len(argv) >= 2 else ""
                except Exception:
                    pass
                return {"kind": "zellij", "key": f"zellij:{apid}", "label": f"zellij {sess}".strip(),
                        "detail": f"pid {apid}", "session": sess, "attachable": bool(sess)}, facts

        if unit and unit.endswith(".service"):
            scope = "--user " if user_scope else ""
            return {"kind": "systemd", "key": f"systemd:{unit}", "label": f"systemd {scope}{unit}",
                    "detail": "", "unit": unit, "user": user_scope}, facts

        for a in chain:
            if a["comm"] in ("sshd", "sshd-session"):
                return {"kind": "ssh", "key": f"ssh:{tty or a['pid']}", "label": f"ssh session {tty or ''}".strip(),
                        "detail": f"sshd pid {a['pid']}"}, facts

        parent = chain[0] if chain else None
        init_like = cur["ppid"] == 1 or (parent is not None and parent["comm"] in ("systemd", "init"))
        managed_scope = bool(unit) and (unit.startswith("app-") or unit.endswith(".service"))
        if init_like and not managed_scope:
            facts["orphan"] = True
            why = "parent gone"
            if unit and unit.startswith("tmux-spawn"):
                why += " · tmux pane closed"
            elif unit and unit.startswith("session-"):
                why += " · login session ended"
            if facts.get("tty_gone"):
                why += f" · {tty} gone"
            # keyed by reason so "tmux pane closed" and "login session ended"
            # orphans land in separate, correctly-labelled groups
            return {"kind": "orphan", "key": f"orphan:{why}", "label": "orphaned", "detail": why}, facts

        if tty:
            shell = parent["comm"] if parent else ""
            return {"kind": "tty", "key": f"tty:{tty}", "label": f"terminal {tty}",
                    "detail": shell}, facts
        shell = parent["comm"] if parent else "?"
        return {"kind": "none", "key": f"none:{shell}", "label": f"spawned by {shell}",
                "detail": f"pid {cur['ppid']}"}, facts

    # --- sample ---------------------------------------------------------------

    def sample(self, gpus, include_holders=False):
        """Tree: {"gpus": [{"idx","id","name","util","groups":[group,...]}],
        "n_procs", "n_orphans", "source", "holders"}. Each group is the
        session dict above plus "procs": [proc,...] and "gpu_mem" (sum)."""
        gpus = list(gpus or [])
        per_gpu, src, holders = self._gpu_pids(len(gpus), include_holders)
        # Per-process SM/ENC/DEC rides on the GPU sample's own procs list
        # (collectors._proc_util), so the tree never asks NVML twice.
        util = {}
        for i, g in enumerate(gpus):
            for pr in g.get("procs") or []:
                if pr.get("pid") is not None and "sm" in pr:
                    util[(i, pr["pid"])] = pr
        table = {}
        proc_cache = {}
        sess_cache = {}
        n_procs = 0
        n_orphans = set()
        roots = []
        idxs = list(range(len(gpus)))
        for extra in sorted(per_gpu):
            if extra not in idxs:
                idxs.append(extra)
        for idx in idxs:
            g = gpus[idx] if idx < len(gpus) else {}
            root = {"idx": idx, "id": g.get("id", str(idx)), "name": g.get("name", f"GPU {idx}"),
                    "util": g.get("util"), "mem_used": g.get("mem_used"), "mem_total": g.get("mem_total"),
                    "enc_util": g.get("enc_util"), "dec_util": g.get("dec_util"), "groups": []}
            groups = {}
            for pid, mem in sorted((per_gpu.get(idx) or {}).items()):
                if pid not in proc_cache:
                    proc_cache[pid] = self._proc_info(pid)
                base = proc_cache[pid]
                if not base["alive"]:
                    continue
                if pid not in sess_cache:
                    sess_cache[pid] = self._resolve_session(pid, base, table)
                sess, facts = sess_cache[pid]
                p = dict(base)
                p.update(facts)
                p["gpu_mem"] = mem
                u = util.get((idx, pid))
                if u is not None:
                    for k in ("sm", "membw", "enc", "dec"):
                        p[k] = u.get(k, 0)
                p["holder"] = (idx, pid) in holders
                p["gpu_idx"] = idx
                p["on_gpus"] = sorted(i for i, b in per_gpu.items() if pid in b)
                if p.get("orphan"):
                    n_orphans.add(pid)
                grp = groups.get(sess["key"])
                if grp is None:
                    grp = dict(sess)
                    grp["procs"] = []
                    grp["gpu_mem"] = 0
                    groups[sess["key"]] = grp
                grp["procs"].append(p)
                if mem:
                    grp["gpu_mem"] += mem
                n_procs += 1
            ordered = sorted(groups.values(),
                             key=lambda gr: (KIND_ORDER.index(gr["kind"]) if gr["kind"] in KIND_ORDER else 99,
                                             -gr["gpu_mem"], gr["label"]))
            for gr in ordered:
                gr["procs"].sort(key=lambda pp: (-(pp.get("gpu_mem") or 0), pp["pid"]))
            root["groups"] = ordered
            roots.append(root)
        # prune the psutil cache of pids that vanished
        live = set(proc_cache)
        for pid in list(self._pcache):
            if pid not in live:
                self._pcache.pop(pid, None)
        self.last_ts = time.time()
        return {"gpus": roots, "n_procs": n_procs, "n_orphans": len(n_orphans), "source": src,
                "holders": bool(include_holders), "caps": dict(self.caps)}


# =========================================================================
# flatten: tree -> selectable rows. Pure.
# =========================================================================

SORTS = ("mem", "sm", "cpu", "age", "pid")
SORT_LABELS = {"mem": "GPU MEM", "sm": "SM%", "cpu": "CPU%", "age": "TIME", "pid": "PID"}


def _sort_key(sort):
    """Process sort key: biggest first, except PID (ascending) and TIME
    (longest-running first)."""
    if sort == "sm":
        return lambda p: (-(p.get("sm") or 0), -(p.get("gpu_mem") or 0), p["pid"])
    if sort == "cpu":
        return lambda p: (-(p.get("cpu") or 0), p["pid"])
    if sort == "age":
        return lambda p: (-(p.get("age") or 0), p["pid"])
    if sort == "pid":
        return lambda p: p["pid"]
    return lambda p: (-(p.get("gpu_mem") or 0), p["pid"])


def matches(proc, group, filt):
    """The `/` filter: a case-insensitive substring of the pid, name,
    command line, user or session label."""
    if not filt:
        return True
    f = filt.lower()
    hay = (str(proc.get("pid")), proc.get("name") or "", proc.get("cmd") or "", proc.get("user") or "",
           (group or {}).get("label") or "", proc.get("node") or "")
    return any(f in str(h).lower() for h in hay)


def flatten(tree, sort="mem", flat=False, filt=""):
    """[{"type": "gpu"|"group"|"proc", ...}] in display order. Group and
    proc rows carry `last` flags the renderer turns into tree glyphs.
    `sort` orders processes (and, unless it is the default "mem", the
    session groups by their first process); `flat` drops the session rows
    and lists each GPU's processes in one sorted run (each row still
    carries its group, so Enter and e still act on the session); `filt`
    keeps only matching processes and their groups."""
    key = _sort_key(sort)
    rows = []
    for root in (tree or {}).get("gpus") or []:
        where = {"remote": True, "node": root.get("node")} if root.get("remote") else {}
        rows.append(dict({"type": "gpu", "gpu": root, "gpu_idx": root["idx"]}, **where))
        groups = []
        for grp in root.get("groups") or []:
            procs = sorted((p for p in grp.get("procs") or [] if matches(p, grp, filt)), key=key)
            if procs:
                groups.append((grp, procs))
        if flat:
            procs = sorted(((p, grp) for grp, ps in groups for p in ps), key=lambda t: key(t[0]))
            for pi, (p, grp) in enumerate(procs):
                rows.append(dict({"type": "proc", "gpu_idx": root["idx"], "group": grp, "proc": p,
                                  "last": pi == len(procs) - 1, "group_last": True, "flat": True}, **where))
            continue
        if sort != "mem":
            groups.sort(key=lambda t: key(t[1][0]))
        for gi, (grp, procs) in enumerate(groups):
            g_last = gi == len(groups) - 1
            rows.append(dict({"type": "group", "gpu_idx": root["idx"], "group": grp, "last": g_last}, **where))
            for pi, p in enumerate(procs):
                rows.append(dict({"type": "proc", "gpu_idx": root["idx"], "group": grp, "proc": p,
                                  "last": pi == len(procs) - 1, "group_last": g_last}, **where))
    return rows


# =========================================================================
# Other nodes' tasks (#14 step 4, D-05). An agent shares its tree only when
# started with --share-tasks (and, for --serve, a cluster token); command
# lines only with --share-args too. The viewer merges what arrives below
# its own GPUs, read-only.
# =========================================================================

READ_ONLY_NOTE = "read-only: {node}'s processes can only be viewed here"
NOT_SHARED_NOTE = "tasks not shared · start its agent with --share-tasks"

_WIRE_ROOT = ("idx", "id", "name", "util", "mem_used", "mem_total", "enc_util", "dec_util")
_WIRE_GROUP = ("kind", "key", "label", "detail", "gpu_mem")
_WIRE_PROC = ("pid", "name", "user", "cpu", "rss", "age", "status", "state", "gpu_mem", "sm", "membw",
              "enc", "dec", "gpu_idx", "on_gpus", "orphan", "holder", "tty", "tty_gone")


def wire_tree(tree, share_args=False):
    """TaskCollector.sample()'s tree, cut to what another node may see:
    the GPU roots, each session's kind/label, each process's numbers. The
    handles end/open act on (tmux targets, unit names, container ids)
    never leave, nor does proc_detail(); the command line only with
    `share_args`."""
    roots = []
    for root in (tree or {}).get("gpus") or []:
        r = {k: root.get(k) for k in _WIRE_ROOT}
        r["groups"] = []
        for grp in root.get("groups") or []:
            g = {k: grp.get(k) for k in _WIRE_GROUP}
            g["procs"] = []
            for p in grp.get("procs") or []:
                q = {k: p[k] for k in _WIRE_PROC if k in p}
                if share_args:
                    q["cmd"] = p.get("cmd") or ""
                g["procs"].append(q)
            r["groups"].append(g)
        roots.append(r)
    t = tree or {}
    return {"shared": True, "args": bool(share_args), "gpus": roots, "n_procs": t.get("n_procs") or 0,
            "n_orphans": t.get("n_orphans") or 0, "source": t.get("source") or "none"}


def _note_root(name, note):
    return {"idx": f"{name}:-", "id": None, "name": "", "node": name, "remote": True, "note": note,
            "groups": []}


def merge_remote(tree, views):
    """The local tree plus, for each other cluster member, its shared GPU
    roots (gpu_idx "node:idx", so the same pid or GPU number on two
    machines stays two rows) or one note row saying why there is nothing.
    Every remote root, group and process carries remote=True and its node
    name; TaskUI and end_*/open_group_argv refuse such rows."""
    out = dict(tree or {})
    roots = list(out.get("gpus") or [])
    nodes = 0
    for v in views or []:
        if v.get("is_local"):
            continue
        name = v.get("name") or "?"
        sample = v.get("sample")
        shared = (sample or {}).get("tasks")
        if not sample:
            roots.append(_note_root(name, f"no sample · {v.get('error') or 'waiting'}"[:60]))
            continue
        if shared is None:
            ver = sample.get("version") or "?"
            roots.append(_note_root(name, f"its agent ({ver}) cannot share tasks · update it"))
            continue
        if not shared.get("shared"):
            roots.append(_note_root(name, NOT_SHARED_NOTE))
            continue
        nodes += 1
        args = bool(shared.get("args"))
        for root in shared.get("gpus") or []:
            r = dict(root)
            r["gpu_no"] = root.get("idx")
            r["idx"] = f"{name}:{root.get('idx')}"
            r["node"], r["remote"] = name, True
            if v.get("stale"):
                r["stale"] = True       # last known list, the node went quiet
            groups = []
            for grp in root.get("groups") or []:
                g = dict(grp)
                g["key"] = f"{name}:{grp.get('key')}"
                g["node"], g["remote"] = name, True
                g["procs"] = []
                for p in grp.get("procs") or []:
                    q = dict(p)
                    q["node"], q["remote"] = name, True
                    if not args:
                        q["cmd"] = ""
                        q["args_hidden"] = True
                    g["procs"].append(q)
                    out["n_procs"] = (out.get("n_procs") or 0) + 1
                    if q.get("orphan"):
                        out["n_orphans"] = (out.get("n_orphans") or 0) + 1
                groups.append(g)
            r["groups"] = groups
            roots.append(r)
    out["gpus"] = roots
    out["nodes"] = nodes
    return out


# =========================================================================
# ProcTracker -- per-process history and the recently-ended list (#14
# step 2). Fed by State.sample() each tick while a screen is drawn, from
# the GPU sample's own procs lists, so the history runs on every page and
# page 5 opens with charts already filled. Local memory only.
# =========================================================================

class ProcTracker:
    ENDED_KEEP_S = 3600
    ENDED_MAX = 30

    def __init__(self, maxlen=3600):
        self.maxlen = maxlen
        self.live = {}       # (gpu id, pid) -> {"sm", "mem" deques, peaks, first}
        self.cpu = {}        # pid -> deque of CPU percent
        self.meta = {}       # pid -> {"name", "cmd", "user", "started"}
        self.ended = deque(maxlen=self.ENDED_MAX)
        self._ps = {}

    def _meta(self, pid, name):
        m = {"name": name or "", "cmd": "", "user": "", "started": None}
        try:
            p = psutil.Process(pid)
            self._ps[pid] = p
            with p.oneshot():
                m["name"] = p.name() or m["name"]
                try:
                    args = p.cmdline() or []
                    if args:
                        m["cmd"] = " ".join([os.path.basename(args[0])] + [a for a in args[1:] if a])
                except Exception:
                    pass
                try:
                    m["user"] = (p.username() or "").rsplit("\\", 1)[-1]
                except Exception:
                    pass
                m["started"] = p.create_time()
            p.cpu_percent(None)
        except Exception:
            pass
        return m

    def update(self, gpus, now=None):
        now = time.time() if now is None else now
        seen, pids = set(), {}
        for g in gpus or []:
            gid = str(g.get("id"))
            for pr in g.get("procs") or []:
                pid = pr.get("pid")
                if pid is None:
                    continue
                key = (gid, pid)
                seen.add(key)
                pids[pid] = pr.get("name")
                rec = self.live.get(key)
                if rec is None:
                    rec = self.live[key] = {"sm": deque(maxlen=self.maxlen), "mem": deque(maxlen=self.maxlen),
                                            "peak_sm": 0, "peak_mem": 0, "first": now, "gpu": gid}
                mem = pr.get("mem") or 0
                rec["mem"].append(float(mem))
                rec["peak_mem"] = max(rec["peak_mem"], mem)
                if "sm" in pr:
                    sm = pr.get("sm") or 0
                    rec["sm"].append(float(sm))
                    rec["peak_sm"] = max(rec["peak_sm"], sm)
        for pid, name in pids.items():
            if pid not in self.meta:
                self.meta[pid] = self._meta(pid, name)
                self.cpu[pid] = deque(maxlen=self.maxlen)
                continue
            try:
                self.cpu[pid].append(float(self._ps[pid].cpu_percent(None)))
            except Exception:
                self.cpu[pid].append(0.0)
        gone = [k for k in self.live if k not in seen]
        ended = {}
        for key in gone:
            rec = self.live.pop(key)
            e = ended.setdefault(key[1], {"gpus": [], "peak_sm": None, "peak_mem": 0, "first": rec["first"]})
            e["gpus"].append(key[0])
            if rec["sm"]:
                e["peak_sm"] = max(e["peak_sm"] or 0, rec["peak_sm"])
            e["peak_mem"] = max(e["peak_mem"], rec["peak_mem"])
            e["first"] = min(e["first"], rec["first"])
        for pid, e in ended.items():
            if pid in pids:            # still on another GPU
                continue
            m = self.meta.pop(pid, None) or {}
            self.cpu.pop(pid, None)
            ps = self._ps.pop(pid, None)
            alive = False
            try:
                alive = ps is not None and ps.is_running()
            except Exception:
                pass
            started = m.get("started") or e["first"]
            self.ended.appendleft({"pid": pid, "name": m.get("name") or "", "cmd": m.get("cmd") or "",
                                   "user": m.get("user") or "", "gpus": sorted(e["gpus"]),
                                   "ran": max(0.0, now - started), "ended": now,
                                   "peak_sm": e["peak_sm"], "peak_mem": e["peak_mem"],
                                   "how": "left the GPU" if alive else "ended"})
        while self.ended and now - self.ended[-1]["ended"] > self.ENDED_KEEP_S:
            self.ended.pop()

    def snapshot(self):
        """Plain lists for the page builder: {"proc": {(gpu id, pid):
        {"sm": [...], "mem": [...]}}, "cpu": {pid: [...]}, "ended": [...]}."""
        return {"proc": {k: {"sm": list(r["sm"]), "mem": list(r["mem"]), "peak_sm": r["peak_sm"],
                             "peak_mem": r["peak_mem"]} for k, r in self.live.items()},
                "cpu": {pid: list(h) for pid, h in self.cpu.items()},
                "ended": list(self.ended)}


# =========================================================================
# proc_detail -- the detail pane's facts for one process. Pure read.
# =========================================================================

# The only environment variables the detail pane shows: device and fabric
# selection. Never the rest of the environment, which can hold secrets.
DETAIL_ENV = ("CUDA_VISIBLE_DEVICES", "NVIDIA_VISIBLE_DEVICES", "CUDA_DEVICE_ORDER",
              "NCCL_IB_HCA", "NCCL_SOCKET_IFNAME")
_DEV_PREFIXES = ("/dev/nvidia", "/dev/infiniband/", "/dev/dri/")
_DETAIL_TTL = 2.0
_detail_cache = {}


def _home_short(path, user_home):
    if path and user_home and (path == user_home or path.startswith(user_home.rstrip("/") + "/")):
        return "~" + path[len(user_home.rstrip("/")):]
    return path


def proc_detail(pid, now=None):
    """{"threads", "ppid", "parent", "uid", "started", "cwd", "env": [(k, v)],
    "devices": [names], "denied": bool} for one pid; fields that cannot be
    read (another user's process) are None, and `denied` says why. Cached
    for 2 s, since the device scan walks every open file."""
    now = time.time() if now is None else now
    hit = _detail_cache.get(pid)
    if hit and now - hit[0] < _DETAIL_TTL:
        return hit[1]
    d = {"threads": None, "ppid": None, "parent": None, "uid": None, "started": None,
         "cwd": None, "env": [], "devices": [], "denied": False}
    try:
        p = psutil.Process(pid)
        with p.oneshot():
            for k, fn in (("threads", p.num_threads), ("ppid", p.ppid), ("started", p.create_time)):
                try:
                    d[k] = fn()
                except Exception:
                    pass
            try:
                d["uid"] = p.uids().real
            except Exception:
                pass
        try:
            d["parent"] = psutil.Process(d["ppid"]).name() if d["ppid"] else None
        except Exception:
            pass
        home = None
        try:
            import pwd
            home = pwd.getpwuid(d["uid"]).pw_dir if d["uid"] is not None else None
        except Exception:
            pass
        try:
            d["cwd"] = _home_short(p.cwd(), home)
        except psutil.AccessDenied:
            d["denied"] = True
        except Exception:
            pass
        try:
            env = p.environ()
            d["env"] = [(k, env[k]) for k in DETAIL_ENV if k in env]
        except psutil.AccessDenied:
            d["denied"] = True
        except Exception:
            pass
        if _PROCFS:
            names = []
            try:
                for fd in os.listdir(f"/proc/{pid}/fd"):
                    try:
                        tgt = os.readlink(f"/proc/{pid}/fd/{fd}")
                    except OSError:
                        continue
                    if tgt.startswith(_DEV_PREFIXES):
                        name = tgt.rsplit("/", 1)[-1]
                        if name not in names:
                            names.append(name)
            except PermissionError:
                d["denied"] = True
            except Exception:
                pass
            d["devices"] = names
    except Exception:
        pass
    if len(_detail_cache) > 64:
        _detail_cache.clear()
    _detail_cache[pid] = (now, d)
    return d


# =========================================================================
# Actions -- the only code here that signals or runs a session manager.
# =========================================================================

def _signal_pid(pid, force):
    """SIGTERM (or SIGKILL) to one pid; on EPERM retries through passwordless
    sudo when knobs' root probe says that's available. Returns (ok, msg)."""
    sig_name = "SIGKILL" if force else "SIGTERM"
    if os.name != "posix":
        try:
            p = psutil.Process(pid)
            (p.kill if force else p.terminate)()
            return True, f"{'killed' if force else 'terminated'} pid {pid}"
        except psutil.NoSuchProcess:
            return False, f"pid {pid} already gone"
        except Exception as e:
            return False, f"blocked: {e}"[:70]
    sig = signal.SIGKILL if force else signal.SIGTERM
    try:
        os.kill(pid, sig)
        return True, f"sent {sig_name} to pid {pid}"
    except ProcessLookupError:
        return False, f"pid {pid} already gone"
    except PermissionError:
        access = knobs._probe_root_access()
        if not access.get("sudo") and not access.get("root"):
            return False, f"blocked: pid {pid} is another user's — no passwordless sudo"
        ok, out, err = knobs._run_cmd(knobs._sudo_wrap(["kill", f"-{sig_name}", str(pid)], access))
        if ok:
            return True, f"sent {sig_name} to pid {pid} (sudo)"
        return False, "blocked: " + knobs._first_line(err or out or "sudo kill failed")
    except Exception as e:
        return False, f"error: {e}"[:70]


def end_process(proc, force=False):
    """Signals ONE process. (ok, msg). Another node's process (#14 step 4)
    is refused: its pid means nothing on this machine."""
    if proc.get("remote"):
        return False, READ_ONLY_NOTE.format(node=proc.get("node") or "another node")
    return _signal_pid(int(proc["pid"]), force)


def _systemctl(args, user_unit):
    cmd = ["systemctl"] + (["--user"] if user_unit else []) + args
    if not user_unit:
        cmd = knobs._sudo_wrap(cmd, knobs._probe_root_access())
    return knobs._run_cmd(cmd)


def end_group(group, force=False):
    """Ends a whole session group through its manager where one exists
    (tmux kill-pane / screen quit / systemctl stop / docker stop), else
    signals every GPU process listed in it. (ok, msg)."""
    if group.get("remote"):
        return False, READ_ONLY_NOTE.format(node=group.get("node") or "another node")
    kind = group.get("kind")
    try:
        if kind == "tmux" and group.get("attachable"):
            if force:
                for p in group.get("procs") or []:
                    _signal_pid(int(p["pid"]), True)
            ok, out, err = knobs._run_cmd(["tmux", "kill-pane", "-t", group["pane_id"]])
            return (ok, f"killed tmux pane {group.get('target')}") if ok else \
                (False, "blocked: " + knobs._first_line(err or out))
        if kind == "screen":
            ok, out, err = knobs._run_cmd(["screen", "-S", str(group["spid"]), "-X", "quit"])
            return (ok, f"quit screen {group['spid']}") if ok else (False, "blocked: " + knobs._first_line(err or out))
        if kind == "systemd":
            ok, out, err = _systemctl(["kill" if force else "stop", group["unit"]] +
                                      (["--signal=SIGKILL"] if force else []), group.get("user"))
            verb = "killed" if force else "stopped"
            return (ok, f"{verb} {group['unit']}") if ok else (False, "blocked: " + knobs._first_line(err or out))
        if kind == "docker" and group.get("cid") and shutil.which("docker"):
            ok, out, err = knobs._run_cmd(["docker", "kill" if force else "stop", group["cid"][:12]])
            verb = "killed" if force else "stopped"
            return (ok, f"{verb} container {group.get('name') or group['cid'][:12]}") if ok else \
                (False, "blocked: " + knobs._first_line(err or out))
    except Exception as e:
        return False, f"error: {e}"[:70]
    # zellij / ssh / tty / orphan / none / non-attachable tmux: signal each
    # listed GPU process (the session itself, if any, is left alone).
    procs = group.get("procs") or []
    if not procs:
        return False, "nothing to end"
    n_ok, last = 0, ""
    for p in procs:
        ok, msg = _signal_pid(int(p["pid"]), force)
        n_ok += 1 if ok else 0
        last = msg
    sig = "SIGKILL" if force else "SIGTERM"
    return n_ok > 0, (f"sent {sig} to {n_ok} process(es)" if n_ok else last)


def open_group_argv(group):
    """(argv, blocking, note) for app.py to run OUTSIDE curses, or
    (None, False, reason). Attaching/tailing is not destructive, so it isn't
    confirm-gated -- but it's still only reachable from live mode."""
    if group.get("remote"):
        return None, False, READ_ONLY_NOTE.format(node=group.get("node") or "another node")
    kind = group.get("kind")
    if kind == "tmux":
        if not group.get("attachable"):
            return None, False, "can't attach: that tmux server belongs to another user"
        if os.environ.get("TMUX"):
            return (["tmux", "switch-client", "-t", group["target"]], False,
                    f"switched to {group['target']}")
        return (["tmux", "attach-session", "-t", group["target"]], True, None)
    if kind == "screen":
        return (["screen", "-x", str(group["spid"])], True, None)
    if kind == "zellij" and group.get("session"):
        return (["zellij", "attach", group["session"]], True, None)
    if kind == "systemd":
        argv = ["journalctl"] + (["--user"] if group.get("user") else []) + \
               ["-u", group["unit"], "-n", "200", "-f"]
        return argv, True, None
    if kind == "docker" and group.get("cid") and shutil.which("docker"):
        return ["docker", "logs", "--tail", "200", "-f", group["cid"][:12]], True, None
    return None, False, f"nothing to open for {group.get('label', 'this group')}"


# =========================================================================
# TaskUI -- live-mode session state, pure transitions (same shape as
# knobs.KnobUI). Never signals anything itself.
# =========================================================================

class TaskUI:
    TOAST_TICKS = 3

    def __init__(self):
        self.selected = 0
        self.confirming = False
        self.confirm_text = None
        self.confirm_warn = None
        self.pending = None          # ("proc"|"group", row, force)
        self.toast = None
        self.toast_ticks = 0
        self.include_holders = False
        self.last_rows = []
        self.sort = "mem"            # `s` cycles tasks.SORTS
        self.flat = False            # `f`: one sorted list per GPU
        self.filter = ""             # `/` text
        self.editing = False         # typing into the filter

    def set_rows(self, rows):
        self.last_rows = list(rows or [])
        target = getattr(self, "_reselect", None)
        self._reselect = None
        if target is not None:
            for i, r in enumerate(self.last_rows):
                if self._same_row(target, r):
                    self.selected = i
                    break
        n = len(self.last_rows)
        if n == 0:
            self.selected = 0
        else:
            self.selected = max(0, min(self.selected, n - 1))
        # pending action targets a row that vanished -> drop the prompt
        if self.confirming and self.pending is not None:
            _, row, _ = self.pending
            if not any(self._same_row(row, r) for r in self.last_rows):
                self.cancel()

    @staticmethod
    def _same_row(a, b):
        if a.get("type") != b.get("type"):
            return False
        # gpu_idx is "node:idx" for another node's GPU (merge_remote), so
        # the same pid on two machines never counts as the same row.
        if a["type"] == "proc":
            return a["proc"]["pid"] == b["proc"]["pid"] and a["gpu_idx"] == b["gpu_idx"]
        if a["type"] == "group":
            return a["group"]["key"] == b["group"]["key"] and a["gpu_idx"] == b["gpu_idx"]
        return a.get("gpu_idx") == b.get("gpu_idx")

    def current(self):
        if not self.last_rows:
            return None
        return self.last_rows[max(0, min(self.selected, len(self.last_rows) - 1))]

    def move(self, delta):
        if self.confirming:
            return
        n = len(self.last_rows)
        if n == 0:
            return
        self.selected = max(0, min(n - 1, self.selected + delta))

    def cancel(self):
        self.confirming = False
        self.confirm_text = None
        self.confirm_warn = None
        self.pending = None

    def _keep_selection(self, fn):
        """Runs a view change, then re-selects the same process or group
        in the rebuilt rows on the next set_rows()."""
        cur = self.current()
        fn()
        self._reselect = cur

    def cycle_sort(self):
        if self.confirming:
            return
        def step():
            self.sort = SORTS[(SORTS.index(self.sort) + 1) % len(SORTS)] if self.sort in SORTS else SORTS[0]
        self._keep_selection(step)

    def toggle_flat(self):
        if self.confirming:
            return
        def step():
            self.flat = not self.flat
        self._keep_selection(step)

    def start_filter(self):
        if self.confirming:
            return
        self.editing = True

    def filter_key(self, ch):
        """One key while typing the filter: printable text appends,
        Backspace deletes, Enter keeps the filter, Esc clears it. Returns
        True while still typing."""
        if ch in (10, 13):
            self.editing = False
        elif ch == 27:
            self.editing = False
            self.filter = ""
        elif ch in (8, 127, 263):     # BS, DEL, curses.KEY_BACKSPACE
            self.filter = self.filter[:-1]
        elif 32 <= ch < 127 and len(self.filter) < 40:
            self.filter += chr(ch)
        self._reselect = self.current()
        return self.editing

    def toggle_holders(self):
        if self.confirming:
            return
        self.include_holders = not self.include_holders

    def _toast(self, text, slot):
        self.toast = (str(text)[:70], slot)
        self.toast_ticks = self.TOAST_TICKS

    def arm_end(self, force=False):
        """'e' (SIGTERM / manager stop) or 'k' (SIGKILL): arms a y/N confirm
        for the selected process or session group. GPU rows can't be ended."""
        if self.confirming:
            return
        row = self.current()
        if row is None:
            return
        if row.get("remote"):
            self._toast(READ_ONLY_NOTE.format(node=row.get("node") or "another node"), 6)
            return
        sig = "SIGKILL" if force else "SIGTERM"
        if row["type"] == "proc":
            p = row["proc"]
            self.confirm_text = f"{'kill' if force else 'end'} pid {p['pid']} {p.get('name', '')} ({sig})? y/N"
            self.confirm_warn = "! terminates a running GPU workload — unsaved work is lost"
        elif row["type"] == "group":
            g = row["group"]
            n = len(g.get("procs") or [])
            via = {"tmux": "kills the tmux pane", "screen": "quits the screen session",
                   "systemd": "systemctl stop" if not force else "systemctl kill",
                   "docker": "docker stop" if not force else "docker kill"}.get(g["kind"])
            if g["kind"] == "tmux" and not g.get("attachable"):
                via = None
            how = via or f"{sig} to {n} process(es)"
            self.confirm_text = f"end {g['label']} — {how}? y/N"
            self.confirm_warn = "! ends every GPU workload in that session — unsaved work is lost"
        else:
            self._toast("select a process or session row to end it", 6)
            return
        self.pending = (row["type"], row, force)
        self.confirming = True

    def confirm_yes(self, end_proc_fn=None, end_group_fn=None):
        """'y' while confirming: dispatches to end_process/end_group (or the
        injected mocks), sets the toast. Returns (ok, msg) or None."""
        if not self.confirming or self.pending is None:
            return None
        kind, row, force = self.pending
        self.cancel()
        fn_p = end_proc_fn or end_process
        fn_g = end_group_fn or end_group
        try:
            if kind == "proc":
                ok, msg = fn_p(row["proc"], force)
            else:
                ok, msg = fn_g(row["group"], force)
        except Exception as e:
            ok, msg = False, f"error: {e}"[:70]
        self._toast(msg, 1 if ok else 4)
        return ok, msg

    def open_spec(self):
        """Enter / 'o': (argv, blocking) for the selected row's session, or
        None after setting an explanatory toast. Not confirm-gated."""
        if self.confirming:
            return None
        row = self.current()
        if row is None:
            return None
        if row.get("remote"):
            self._toast(READ_ONLY_NOTE.format(node=row.get("node") or "another node"), 6)
            return None
        group = row.get("group")
        if group is None:
            self._toast("select a session row (or a process in one) to open it", 6)
            return None
        argv, blocking, note = open_group_argv(group)
        if argv is None:
            self._toast(note or "nothing to open", 6)
            return None
        if note:
            self._toast(note, 1)
        return argv, blocking

    def tick(self):
        if self.toast_ticks > 0:
            self.toast_ticks -= 1
            if self.toast_ticks <= 0:
                self.toast = None

    def render_ctx(self):
        return {
            "selected": self.selected, "confirming": self.confirming,
            "confirm_text": self.confirm_text, "confirm_warn": self.confirm_warn,
            "toast": self.toast, "include_holders": self.include_holders,
            "sort": self.sort, "flat": self.flat, "filter": self.filter, "editing": self.editing,
        }
