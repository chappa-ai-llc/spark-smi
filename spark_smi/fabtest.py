"""Phase 7: page-4 fabric validation -- rail discovery, ssh-driven RDMA
stress tests (perftest's ib_write_bw/ib_write_lat, with an iperf3 fallback
for rails with no RDMA device), the persisted results log, and the
FabTestUI session state machine.

This is the ONLY module in the app that ever shells out to make a REMOTE
node generate traffic (collectors.py/cluster.py only ever READ). Like
knobs.py's confirm-gated hardware writes, nothing here runs unless
FabTestUI.confirm_yes() has already gone through an explicit arm -> y/N
sequence, and app.py only ever constructs a FabricEngine/FabTestUI in
main_loop -- never in State, which the snapshot path also uses -- so
`spark-smi --page 4` (no -l) can only ever render the LAST recorded run; it
has no path to start a new one. Same guarantee shape as knobs.KnobUI.

Measurement does NOT rely on parsing perftest's own output: the cluster
aggregator (cluster.py) already polls every member's sample() once a
second, and NetCollector.sample_pf_detail's per-rail tx_bps (the same RDMA
HCA counters page 2 already shows) IS the bandwidth signal used for the live
chart and the recorded result -- see _summarize_series(). Perftest's own
summary line is parsed defensively, best-effort, and kept alongside as
"engine_gbps" purely for cross-checking; a garbled or missing engine reading
never blocks the sample-based result. A dead ssh, a missing perftest/iperf3
binary, or unparseable stdout must never crash a run: every rail that can't
be measured is recorded with a "failed: <reason>" note and the rest of the
run continues.

pages.py never imports this module (same boundary knobs.py/cluster.py keep
with pages.py already): app.py gathers everything this engine knows into
plain dicts/lists (FabTestUI.render_ctx(), FabricEngine.status(),
load_last_run(), load_matrix()) before handing them to pages.build_page4.
"""
import json
import os
import re
import subprocess
import threading
import time

from . import cluster

# --- perftest port ranges -------------------------------------------------
BW_BASE_PORT = 18515      # rail i's bandwidth test uses BW_BASE_PORT + i
LAT_PORT = 18525          # one shared port for the post-bandwidth latency probe
MAX_RAILS = 4             # verified real topology: 4 rails/node (CLAUDE.md)

# --- timings ---------------------------------------------------------------
SERVER_START_DELAY = 1.0   # server needs a moment to bind before the client dials
SERVER_STOP_GRACE = 5.0    # extra seconds the server's own -D gets over the client's
CLIENT_WAIT_SLACK = 20.0   # extra seconds allowed on top of `duration` before giving up

DURATIONS = (10, 30, 60)   # 'd' key cycle, seconds/pair
MODES = ("pair", "sweep", "burst")

RESULTS_DIR = os.path.expanduser(os.path.join("~", ".local", "share", "spark-smi"))
RESULTS_PATH = os.path.join(RESULTS_DIR, "fabric-tests.jsonl")

SSH_OPTS = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=5"]

# Processes matched by the final teardown sweep (bracket-first-char trick so
# `pkill -f` doesn't match its own /proc/self/cmdline containing the pattern).
_KILL_PATTERNS = ("ib_write_bw", "ib_write_lat", "iperf3")


def _local_name():
    return cluster.local_hostname()


def _is_local(host):
    return bool(host) and host.split(".")[0] == _local_name()


# =========================================================================
# Rail discovery + src<->dst pairing
# =========================================================================

def node_rails(sample):
    """A node's fabric rails, straight from its sample's nic_pf list
    (collectors.NetCollector.sample_pf_detail -- already carries
    rdma_dev/netdev/port plus Phase 7's "ip" and "asic_temp" fields).
    Filters to rows that have something dial-able (an IP) since a rail with
    neither an RDMA device nor an IP can't be tested by either backend.
    Works identically against a local State.sample() or a cluster member's
    from_wire()'d remote sample -- same wire shape either way."""
    try:
        rows = sample.get("nic_pf") or []
    except Exception:
        return []
    return [r for r in rows if r.get("ip")][:MAX_RAILS]


def _subnet24(ip):
    try:
        parts = str(ip).split(".")
        return ".".join(parts[:3]) if len(parts) == 4 else None
    except Exception:
        return None


def pair_rails(src_rails, dst_rails):
    """[(src_rail, dst_rail_or_None), ...], one entry per src rail. Real
    topology (verified, CLAUDE.md): each rail pair lives on its own /24
    subnet (sparky-1 enp1s0f0np0=192.168.175.11 <-> sparky-2's .175.12,
    etc.) -- so the primary match is "same /24 network". Any src rail that
    doesn't find a subnet match falls back to positional pairing against
    whatever dst rails are left, both sides sorted by (port, rdma_dev) --
    keeps pairing deterministic even against a node whose rails aren't on
    matching subnets (plain-ethernet fallback nodes, mixed topologies)."""
    src_rails = list(src_rails)[:MAX_RAILS]
    dst_sorted = sorted(dst_rails, key=lambda r: (r.get("port") or "", r.get("rdma_dev") or r.get("netdev") or ""))
    used_ids = set()
    pairs, unmatched = [], []
    for sr in src_rails:
        sub = _subnet24(sr.get("ip"))
        match = None
        if sub:
            for dr in dst_sorted:
                if id(dr) in used_ids:
                    continue
                if _subnet24(dr.get("ip")) == sub:
                    match = dr
                    break
        if match is not None:
            used_ids.add(id(match))
            pairs.append((sr, match))
        else:
            unmatched.append(sr)
    leftovers = [dr for dr in dst_sorted if id(dr) not in used_ids]
    for sr, dr in zip(unmatched, leftovers):
        used_ids.add(id(dr))
        pairs.append((sr, dr))
    for sr in unmatched[len(leftovers):]:
        pairs.append((sr, None))
    order = {id(sr): i for i, sr in enumerate(src_rails)}
    pairs.sort(key=lambda p: order.get(id(p[0]), 0))
    return pairs


def rail_short_label(rail):
    """Best-effort 'p0f0'-style single-rail label (port + a digit pulled
    from the netdev name) -- used only as a LAST-RESORT fallback (e.g. the
    "no matching dst rail" failure path, where a rail dict in isolation is
    all there is to label). NOT collision-safe across a list: DGX Spark's
    two identical ConnectX-7 cards each expose their own ports as sysfs
    phys_port_name "p0"/"p1" (port naming resets per card/ASIC), so a plain
    port+netdev-digit label collides between the two cards' matching ports.
    assign_rail_labels() below is what actually labels a run's rails and IS
    collision-safe -- prefer it whenever more than one rail is in scope."""
    if not rail:
        return "?"
    port = rail.get("port") or ""
    netdev = rail.get("netdev") or rail.get("dev") or ""
    m = re.search(r"f(\d+)", netdev)
    if port and m:
        return f"{port}f{m.group(1)}"
    return port or (netdev[:6] if netdev else (rail.get("rdma_dev") or "?"))


def assign_rail_labels(rails):
    """['p0f0', 'p0f1', ...], one label per rail in `rails`' order --
    collision-safe (unlike rail_short_label() above): groups by each rail's
    "port" value and numbers occurrences within that group in arrival order,
    which is exactly how the real topology's two identical cards' rails
    disambiguate (both expose a first port as sysfs "p0" -- this labels
    them "p0f0"/"p0f1" by ARRIVAL order rather than trying to parse a
    PCI-domain-specific digit out of the netdev name, which would need to
    assume a naming convention that hasn't been verified against real
    hardware yet -- see this module's end-of-report note). Falls back to a
    truncated netdev/rdma_dev when a rail has no "port" at all."""
    counts, labels = {}, []
    for r in rails:
        port = (r or {}).get("port") or ((r or {}).get("netdev") or (r or {}).get("rdma_dev") or "?")[:4]
        i = counts.get(port, 0)
        counts[port] = i + 1
        labels.append(f"{port}f{i}")
    return labels


def gpu_active_nodes(views):
    """Node names (from cluster.ClusterAggregator.get_views()) whose latest
    sample shows an active GPU job -- >10% util on any GPU, or a nonempty
    compute/graphics process list -- fed into the space-key confirm prompt's
    "GPU jobs active on <nodes>" warning. Best-effort: a missing/malformed
    sample is just skipped, never a crash."""
    out = []
    for v in views or []:
        try:
            sample = v.get("sample")
            if not sample:
                continue
            gpus = sample.get("gpus") or []
            busy = any((g.get("util") or 0) > 10 or g.get("procs") for g in gpus)
            if busy:
                out.append(v.get("name"))
        except Exception:
            continue
    return out


# =========================================================================
# Process layer -- the ONLY functions that spawn ssh/perftest/iperf3. Kept
# small and isolated so tests can monkeypatch _popen/_run without touching
# any orchestration logic below.
# =========================================================================

def _popen(host, remote_cmd):
    """Spawns `remote_cmd` locally (when `host` IS this machine) or via
    `ssh -o BatchMode=yes host <cmd>` otherwise. Returns a Popen with
    stdout+stderr merged and captured as text, or None if even starting the
    process failed (missing ssh binary, host resolution failure, ...) --
    callers treat None exactly like "the process died immediately"."""
    try:
        if _is_local(host):
            argv = remote_cmd if isinstance(remote_cmd, list) else remote_cmd.split()
        else:
            cmd = remote_cmd if isinstance(remote_cmd, str) else " ".join(remote_cmd)
            argv = ["ssh"] + SSH_OPTS + [host, cmd]
        return subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    except Exception:
        return None


def _kill(proc):
    if proc is None:
        return
    try:
        proc.kill()
    except Exception:
        pass
    try:
        proc.wait(timeout=2)
    except Exception:
        pass


def _pkill_remote(host, pattern):
    """Final teardown sweep for one host: `pkill -f '[x]attern'` (bracket
    trick on the first char, so pkill's own cmdline -- which contains the
    literal pattern text as an argv -- doesn't match itself), run locally or
    over ssh depending on `host`. Best-effort; a dead/unreachable host just
    means there's nothing left running there anyway."""
    if not pattern:
        return
    bracketed = f"[{pattern[0]}]{pattern[1:]}"
    try:
        if _is_local(host):
            subprocess.run(["pkill", "-f", bracketed], capture_output=True, timeout=3)
        else:
            subprocess.run(["ssh"] + SSH_OPTS + [host, f"pkill -f '{bracketed}'"],
                            capture_output=True, timeout=5)
    except Exception:
        pass


def kill_all_on(hosts):
    """Sweeps every known fabtest process pattern on every given host --
    FabricEngine.stop()'s final teardown, and also run at the start of a
    fresh run so a previous session's crashed/orphaned server doesn't hold
    a port."""
    for host in hosts or []:
        for pattern in _KILL_PATTERNS:
            _pkill_remote(host, pattern)


# =========================================================================
# Perftest / iperf3 output parsing -- best-effort, secondary to the
# sample-based measurement above. Column positions follow perftest's
# --report_gbits fixed-width summary table; never raises, None on anything
# that doesn't look right. Flagged for the architect to double check against
# a real ib_write_bw/ib_write_lat run (see this module's end-of-report note).
# =========================================================================

def _parse_bw_gbits(stdout):
    """Last plausible "BW average [Gb/sec]" figure from ib_write_bw
    --report_gbits output: data rows are whitespace-separated numbers,
    #bytes #iterations BW_peak BW_average [MsgRate] -- column index 3."""
    if not stdout:
        return None
    best = None
    try:
        for line in stdout.splitlines():
            line = line.strip()
            if not line or not line[0].isdigit():
                continue
            nums = re.findall(r"[\d.]+", line)
            if len(nums) >= 4:
                best = float(nums[3])
    except Exception:
        return None
    return best


def _parse_lat_usec(stdout):
    """Best-effort "typical" latency figure from ib_write_lat's summary
    table (data row columns: #bytes #iterations t_min t_max t_typical ...
    -- column index 4), or None on anything unparseable."""
    if not stdout:
        return None
    best = None
    try:
        for line in stdout.splitlines():
            line = line.strip()
            if not line or not line[0].isdigit():
                continue
            nums = re.findall(r"[\d.]+", line)
            if len(nums) >= 5:
                best = float(nums[4])
    except Exception:
        return None
    return best


def _parse_iperf3_gbits(stdout):
    try:
        data = json.loads(stdout)
        bps = data["end"]["sum_received"]["bits_per_second"]
        return bps / 1e9
    except Exception:
        return None


# =========================================================================
# One rail's bandwidth job: server on dst, client on src, both over ssh
# (or local subprocess when that side IS this machine).
# =========================================================================

class _RailJob:
    """Server+client process handles for one rail's bandwidth test, plus
    whatever perftest/iperf3's own summary parsed out to (engine_gbps) --
    the sample-based series is tracked separately by the caller."""

    def __init__(self, rail_idx, src_rail, dst_rail):
        self.rail_idx = rail_idx
        self.src_rail = src_rail
        self.dst_rail = dst_rail
        self.server_proc = None
        self.client_proc = None
        self.engine_gbps = None
        self.failed_reason = None

    def run_bw(self, src_host, dst_host, duration):
        dst_dev, src_dev = self.dst_rail.get("rdma_dev"), self.src_rail.get("rdma_dev")
        dst_ip = self.dst_rail.get("ip")
        port = BW_BASE_PORT + self.rail_idx
        if src_dev and dst_dev and dst_ip:
            engine = "ib_write_bw"
            server_cmd = f"ib_write_bw -d {dst_dev} -p {port} --report_gbits -D {duration + int(SERVER_STOP_GRACE)}"
            client_cmd = f"ib_write_bw -d {src_dev} -p {port} --report_gbits -D {duration} {dst_ip}"
        elif dst_ip:
            engine = "iperf3"
            server_cmd = f"iperf3 -s -1 -p {port}"
            client_cmd = f"iperf3 -c {dst_ip} -p {port} -t {duration} -J"
        else:
            self.failed_reason = "no rdma device or IP on this rail"
            return
        self.server_proc = _popen(dst_host, server_cmd)
        if self.server_proc is None:
            self.failed_reason = f"failed to start server on {dst_host}"
            return
        time.sleep(SERVER_START_DELAY)
        self.client_proc = _popen(src_host, client_cmd)
        if self.client_proc is None:
            self.failed_reason = f"failed to start client on {src_host}"
            _kill(self.server_proc)
            return
        out = None
        try:
            out, _ = self.client_proc.communicate(timeout=duration + SERVER_START_DELAY + CLIENT_WAIT_SLACK)
        except Exception:
            self.failed_reason = self.failed_reason or "client timed out"
        _kill(self.server_proc)
        _kill(self.client_proc)
        if out:
            self.engine_gbps = _parse_iperf3_gbits(out) if engine == "iperf3" else _parse_bw_gbits(out)
            if self.engine_gbps is None and self.failed_reason is None:
                self.failed_reason = None  # engine parse failing alone isn't fatal -- sample series is authoritative

    def stop(self):
        _kill(self.client_proc)
        _kill(self.server_proc)


def _run_latency(src_host, dst_host, src_dev, dst_ip):
    if not (src_dev and dst_ip):
        return None
    server = _popen(dst_host, f"ib_write_lat -p {LAT_PORT}")
    if server is None:
        return None
    time.sleep(SERVER_START_DELAY)
    client = _popen(src_host, f"ib_write_lat -d {src_dev} -p {LAT_PORT} {dst_ip}")
    if client is None:
        _kill(server)
        return None
    out = None
    try:
        out, _ = client.communicate(timeout=20)
    except Exception:
        pass
    _kill(server)
    _kill(client)
    return _parse_lat_usec(out) if out else None


# =========================================================================
# Sample-based measurement -- the authoritative bandwidth/temp signal (see
# module docstring). Runs in its own thread per rail, reading the SAME
# non-blocking aggregator.get_views() the render thread/page 3 already use.
# =========================================================================

def _sample_rail_series(aggregator, src_name, rail_dev, duration, samples_out, stop_event):
    """Appends (ts, tx_bps, asic_temp) to `samples_out` about once a second
    for ~duration+2s (or until `stop_event`), reading the src node's nic_pf
    row matching `rail_dev` out of the aggregator's freshest cached poll.
    Never raises; a missing sample/row for a given tick just isn't
    recorded (a brief poll gap doesn't kill the whole series)."""
    end = time.time() + duration + 2
    while time.time() < end and not stop_event.is_set():
        try:
            views = aggregator.get_views()
            v = next((v for v in views if v.get("name") == src_name), None)
            sample = v.get("sample") if v else None
            row = None
            if sample:
                row = next((r for r in (sample.get("nic_pf") or []) if r.get("rdma_dev") == rail_dev), None)
            if row is not None:
                samples_out.append((time.time(), row.get("tx_bps") or 0.0, row.get("asic_temp")))
        except Exception:
            pass
        stop_event.wait(1.0)


def _summarize_series(samples):
    """(avg_gbps, asic_start, asic_max, full_series_gbps) from raw
    (ts, tx_bps, asic_temp) samples. avg_gbps drops the first 2 and last 1
    sample of the window (ramp-up/tail-off aren't steady state) per spec;
    asic_start/asic_max look at the WHOLE window including those edges --
    a thermal peak right at the very end of a run is still real. Returns
    (None, None, None, []) for an empty window."""
    if not samples:
        return None, None, None, []
    temps = [s[2] for s in samples if s[2] is not None]
    asic_start = temps[0] if temps else None
    asic_max = max(temps) if temps else None
    full_series = [max(0.0, s[1]) / 1e9 for s in samples]
    # Steady state only: the sampler starts before traffic does, so leading
    # zeros/ramp samples are dilution, not measurement (verified live: mean
    # over the raw window read 42 while the engine and the plateau said 52).
    # Skip everything below 5% of peak, drop one more ramp sample and the
    # tail-off, then take the median for outlier robustness.
    peak = max(full_series) if full_series else 0.0
    steady = [v for v in full_series if v >= peak * 0.05]
    steady = steady[1:-1] if len(steady) > 3 else steady
    avg = None
    if steady:
        srt = sorted(steady)
        mid = len(srt) // 2
        avg = srt[mid] if len(srt) % 2 else (srt[mid - 1] + srt[mid]) / 2.0
    return avg, asic_start, asic_max, full_series


def _member_sample(aggregator, name):
    try:
        return aggregator.get_member_sample(name)
    except Exception:
        return None


def _member_host(aggregator, name):
    """ssh/connect host for a member. Views display the payload's SHORT
    hostname ("sparky-2"), which may not resolve in DNS -- the cluster-list
    entry host ("sparky-2.chappa.ai") is what ssh must use. Accepts either
    form, mirroring get_member_sample's matching."""
    try:
        for m in aggregator.members:
            sample, _, _, _ = m.snapshot()
            payload = str(sample.get("node", "")).split(".")[0] if sample else None
            if name in (m.name, payload):
                # prefer the aggregator's DNS-resolved host (handles short
                # names auto-suffixed with the local domain)
                return getattr(m, "resolved_host", None) or m.name
    except Exception:
        pass
    return name


# =========================================================================
# Orchestration: one pair (all paired rails in parallel) / a full sweep
# (every ordered pair, sequential) / a burst (every node -> its ring
# successor, concurrent).
# =========================================================================

def run_pair(aggregator, src_name, dst_name, duration, stop_event=None, live_series=None,
             on_start=None):
    """Runs src_name -> dst_name: pairs the two nodes' rails (pair_rails),
    launches each paired rail's bandwidth job + sample-series reader in its
    own thread, waits for them, then probes latency once on rail 0. Returns
    a FabRun-shaped dict (see append_run()). Never raises -- every rail that
    can't be paired/measured is recorded with a "failed: <reason>" note and
    the rest of the run still completes.

    `live_series` (optional): a dict this function CLEARS then fills with
    {rail_idx: [gbps, ...]} as samples arrive, for a caller (FabricEngine)
    that wants to show a live-updating chart while the run is in flight --
    purely a side channel, the returned run dict already carries the full
    series too."""
    stop_event = stop_event or threading.Event()
    if live_series is not None:
        live_series.clear()
    if on_start:
        try:
            on_start(src_name, dst_name)
        except Exception:
            pass

    src_sample = _member_sample(aggregator, src_name)
    dst_sample = _member_sample(aggregator, dst_name)
    if not src_sample or not dst_sample:
        return {"ts": time.time(), "mode": "pair", "src": src_name, "dst": dst_name,
                "duration": duration, "rails": [], "lat_us": None,
                "notes": "failed: no sample available for src and/or dst"}

    src_host = _member_host(aggregator, src_name)
    dst_host = _member_host(aggregator, dst_name)
    pairs = pair_rails(node_rails(src_sample), node_rails(dst_sample))
    if not pairs:
        return {"ts": time.time(), "mode": "pair", "src": src_name, "dst": dst_name,
                "duration": duration, "rails": [], "lat_us": None,
                "notes": "failed: no rails discovered on src"}

    jobs, threads_list, sample_lists = {}, [], {}
    for i, (sr, dr) in enumerate(pairs):
        if dr is None:
            continue
        job = _RailJob(i, sr, dr)
        jobs[i] = job
        samples = live_series.setdefault(i, []) if live_series is not None else []
        sample_lists[i] = samples

        def _worker(job=job, sr=sr, samples=samples):
            stopper = threading.Event()
            sampler = threading.Thread(target=_sample_rail_series,
                                        args=(aggregator, src_name, sr.get("rdma_dev"), duration, samples, stopper),
                                        daemon=True)
            sampler.start()
            job.run_bw(src_host, dst_host, duration)
            stopper.set()
            sampler.join(timeout=2)

        t = threading.Thread(target=_worker, daemon=True)
        threads_list.append(t)
        t.start()

    for t in threads_list:
        t.join(timeout=duration + SERVER_START_DELAY + CLIENT_WAIT_SLACK + 5)

    lat_us = None
    if pairs and pairs[0][1] is not None and not stop_event.is_set():
        sr0, dr0 = pairs[0]
        lat_us = _run_latency(src_host, dst_host, sr0.get("rdma_dev"), dr0.get("ip"))

    rails_out = []
    rail_labels = assign_rail_labels([sr for sr, _dr in pairs])
    for i, (sr, dr) in enumerate(pairs):
        label = rail_labels[i]
        if dr is None:
            rails_out.append({"dev": label, "gbps": None, "engine_gbps": None,
                               "asic_start": None, "asic_max": None, "series": [],
                               "notes": "failed: no matching rail on destination"})
            continue
        job = jobs.get(i)
        avg, asic_start, asic_max, series = _summarize_series(sample_lists.get(i, []))
        notes = f"failed: {job.failed_reason}" if job and job.failed_reason and avg is None else None
        rails_out.append({"dev": label, "gbps": avg, "engine_gbps": job.engine_gbps if job else None,
                           "asic_start": asic_start, "asic_max": asic_max, "series": series, "notes": notes})

    return {"ts": time.time(), "mode": "pair", "src": src_name, "dst": dst_name,
            "duration": duration, "rails": rails_out, "lat_us": lat_us, "notes": None}


def run_sweep(aggregator, node_names, duration, stop_event=None, on_progress=None, live_series=None):
    """Every ordered (src, dst) pair among `node_names`, sequential. Calls
    `on_progress(i, n, src, dst)` right before each pair starts -- app.py
    wires this to FabTestUI.on_progress() so the FABRIC TEST panel's
    "RUNNING - pair k of n" tracks along live. Returns the list of FabRun
    dicts (caller decides whether/how to persist each one)."""
    stop_event = stop_event or threading.Event()
    ordered = [(s, d) for s in node_names for d in node_names if s != d]
    runs = []
    n = len(ordered)
    for i, (s, d) in enumerate(ordered):
        if stop_event.is_set():
            break
        if on_progress:
            try:
                on_progress(i, n, s, d)
            except Exception:
                pass
        runs.append(run_pair(aggregator, s, d, duration, stop_event=stop_event, live_series=live_series))
    return runs


def run_burst(aggregator, node_names, duration, stop_event=None, on_progress=None):
    """Every node pushes to its ring successor SIMULTANEOUSLY (node[i] ->
    node[(i+1) % n]); aggregate = sum of every pair's best measured
    per-rail sum. Concurrent, so there's no single "live chart" pair here
    (see pages.build_page4's BANDWIDTH-panel handling of mode=="burst") --
    `on_progress` is called once per pair at launch so callers can at least
    mark every ring pair as "running" (e.g. the MATRIX panel)."""
    stop_event = stop_event or threading.Event()
    n = len(node_names)
    ring = [(node_names[i], node_names[(i + 1) % n]) for i in range(n)] if n > 1 else []
    results = {}
    threads = []

    def _one(s, d):
        if on_progress:
            try:
                on_progress(s, d)
            except Exception:
                pass
        results[(s, d)] = run_pair(aggregator, s, d, duration, stop_event=stop_event)

    for s, d in ring:
        t = threading.Thread(target=_one, args=(s, d), daemon=True)
        threads.append(t)
        t.start()
    for t in threads:
        t.join(timeout=duration + SERVER_START_DELAY + CLIENT_WAIT_SLACK + 10)

    per_pair, agg_gbps = [], 0.0
    for s, d in ring:
        r = results.get((s, d))
        if not r:
            continue
        pair_total = sum((rr.get("gbps") or 0.0) for rr in r.get("rails") or [])
        agg_gbps += pair_total
        per_pair.append({"src": s, "dst": d, "gbps": pair_total, "rails": r.get("rails"), "lat_us": r.get("lat_us")})

    return {"ts": time.time(), "mode": "burst", "src": None, "dst": None,
            "duration": duration, "pairs": per_pair, "aggregate_gbps": agg_gbps}


# =========================================================================
# Persisted results log
# =========================================================================

def _ensure_dir(path):
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
    except Exception:
        pass


def append_run(run, path=None):
    path = path or RESULTS_PATH
    _ensure_dir(path)
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(run, default=str) + "\n")
    except Exception:
        pass


def _tail_lines(path, min_lines, block=65536):
    """Reads backward from EOF until at least `min_lines` newlines have been
    seen (or the file's exhausted), returning the decoded text -- avoids
    loading a long-lived install's entire history just to find the tail."""
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        size = f.tell()
        data, pos = b"", size
        while pos > 0 and data.count(b"\n") < min_lines:
            step = min(block, pos)
            pos -= step
            f.seek(pos)
            data = f.read(step) + data
    return data.decode("utf-8", "ignore")


def load_last_run(path=None):
    """The most recent (JSON-parseable) line in the results file, or None
    when the file's absent/empty/unreadable -- feeds the FABRIC
    TEST/BANDWIDTH/RAILS panels' idle-state "last run" display."""
    path = path or RESULTS_PATH
    try:
        text = _tail_lines(path, min_lines=5)
    except Exception:
        return None
    for line in reversed([l for l in text.splitlines() if l.strip()]):
        try:
            return json.loads(line)
        except Exception:
            continue
    return None


def load_matrix(path=None, max_lines=4000):
    """{(src, dst): {"gbps": best-rail Gb/s, "lat_us": ...}} from the most
    recent mode=="pair" result for each ordered pair found in a bounded tail
    scan of the results file (run_sweep appends one mode=="pair" line per
    pair it completes, so a sweep's history IS this panel's matrix data --
    later lines simply overwrite earlier ones for the same (src, dst)).
    Missing pairs are just absent from the dict -- MATRIX shows "no data"
    for them, never a fabricated number."""
    path = path or RESULTS_PATH
    out = {}
    try:
        text = _tail_lines(path, min_lines=max_lines)
    except Exception:
        return out
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            run = json.loads(line)
        except Exception:
            continue
        if run.get("mode") != "pair":
            continue
        s, d = run.get("src"), run.get("dst")
        if not s or not d:
            continue
        rails = run.get("rails") or []
        best = max((r.get("gbps") for r in rails if r.get("gbps") is not None), default=None)
        out[(s, d)] = {"gbps": best, "lat_us": run.get("lat_us")}
    return out


# =========================================================================
# FabTestUI: pure session-state transitions, no curses, no network -- same
# convention as knobs.KnobUI. app.py's main_loop owns the only instance;
# the snapshot path never constructs one.
# =========================================================================

class FabTestUI:
    TOAST_TICKS = 3

    def __init__(self):
        self.mode_idx = MODES.index("sweep")
        self.duration_idx = DURATIONS.index(30)
        self.state = "idle"          # idle | confirm | running
        self.confirming = False
        self.confirm_text = None
        self.pair_idx = 0
        self.nodes = []
        self.progress = None         # {"i", "n", "src", "dst", "mode"}
        self.last_result = None
        self.toast = None
        self.toast_ticks = 0

    @property
    def mode(self):
        return MODES[self.mode_idx]

    @property
    def duration(self):
        return DURATIONS[self.duration_idx]

    def set_nodes(self, names):
        self.nodes = list(names or [])
        if not self.nodes:
            self.pair_idx = 0
        else:
            self.pair_idx %= len(self.nodes)

    def current_pair(self):
        if len(self.nodes) < 2:
            return None, None
        src = self.nodes[self.pair_idx % len(self.nodes)]
        dst = self.nodes[(self.pair_idx + 1) % len(self.nodes)]
        return src, dst

    def cycle_mode(self):
        if self.state == "running":
            return
        self.mode_idx = (self.mode_idx + 1) % len(MODES)
        self._cancel_confirm()

    def cycle_duration(self):
        if self.state == "running":
            return
        self.duration_idx = (self.duration_idx + 1) % len(DURATIONS)
        self._cancel_confirm()

    def move_pair(self, delta):
        if self.state == "running" or self.mode != "pair" or len(self.nodes) < 2:
            return
        self.pair_idx = (self.pair_idx + delta) % len(self.nodes)
        self._cancel_confirm()

    def _cancel_confirm(self):
        self.confirming = False
        self.confirm_text = None
        if self.state == "confirm":
            self.state = "idle"

    def arm(self, gpu_active=None):
        """space, from idle: arms a y/N confirm prompt. `gpu_active`
        (optional list of node names) is folded into the prompt text as a
        warning -- app.py computes it from the aggregator's latest views via
        gpu_active_nodes(); this method doesn't sample anything itself."""
        if self.state == "running":
            return
        text = f"run {self.mode}"
        if self.mode == "pair":
            src, dst = self.current_pair()
            if src:
                text += f" {src} → {dst}"
            else:
                text = "need 2+ nodes to run a pair test"
                self.confirming, self.confirm_text, self.state = False, None, "idle"
                return
        text += f" for {self.duration}s? y/N"
        if gpu_active:
            text = f"GPU jobs active on {', '.join(gpu_active)} — {text}"
        self.confirming = True
        self.confirm_text = text
        self.state = "confirm"

    def toggle_start_stop(self, gpu_active=None):
        """space: from running -> returns "stop" (app.py tears the engine
        down immediately, no confirm -- abort must always be one keypress);
        from idle -> arms a confirm; while a confirm is already up, this key
        does nothing further ('y' is what actually starts a run)."""
        if self.state == "running":
            return "stop"
        if not self.confirming:
            self.arm(gpu_active)
        return None

    def confirm_yes(self):
        """'y' while confirming: returns {"mode","duration","src","dst"} for
        app.py to actually hand to a FabricEngine, and flips to "running".
        This class never touches subprocess/ssh itself."""
        if not self.confirming:
            return None
        self._cancel_confirm()
        self.state = "running"
        src, dst = self.current_pair() if self.mode == "pair" else (None, None)
        self.progress = {"i": 0, "n": 0, "src": src, "dst": dst, "mode": self.mode}
        return {"mode": self.mode, "duration": self.duration, "src": src, "dst": dst}

    def cancel(self):
        """Any key other than 'y' while a confirm is armed."""
        self._cancel_confirm()

    def on_progress(self, i, n, src, dst):
        self.progress = {"i": i, "n": n, "src": src, "dst": dst, "mode": self.mode}

    def finish(self, result, stopped=False):
        self.state = "idle"
        self.progress = None
        if result is not None:
            self.last_result = result
        self._toast("test stopped" if stopped else "test complete", 5 if stopped else 1)

    def _toast(self, text, slot):
        self.toast = (str(text)[:70], slot)
        self.toast_ticks = self.TOAST_TICKS

    def tick(self):
        if self.toast_ticks > 0:
            self.toast_ticks -= 1
            if self.toast_ticks <= 0:
                self.toast = None

    def render_ctx(self):
        return {
            "mode": self.mode, "duration": self.duration, "state": self.state,
            "confirming": self.confirming, "confirm_text": self.confirm_text,
            "pair_idx": self.pair_idx, "nodes": list(self.nodes), "toast": self.toast,
        }


# =========================================================================
# FabricEngine: owns the background thread that actually runs a test.
# app.py constructs exactly one of these in main_loop (never in State) --
# mirrors KnobUI's "the write-capable object only exists at all in live
# mode" guarantee. Nothing here runs until FabTestUI.confirm_yes() has
# handed app.py a start spec.
# =========================================================================

class FabricEngine:
    def __init__(self, aggregator, results_path=None):
        self.aggregator = aggregator
        self.results_path = results_path or RESULTS_PATH
        self._thread = None
        self._stop_event = threading.Event()
        self._active_series = {}
        self._progress = None
        self._start_ts = None
        self._progress_cb = None
        self._done_cb = None

    def is_running(self):
        return self._thread is not None and self._thread.is_alive()

    def status(self):
        return {
            "running": self.is_running(),
            "progress": dict(self._progress) if self._progress else None,
            # live series entries are raw (ts, tx_bps, asic) sampler tuples;
            # normalize to the same plain Gb/s floats a recorded run's series
            # uses so the chart consumes one shape (live-mode black screen bug)
            "series": {k: [x if isinstance(x, (int, float))
                           else max(0.0, (x[1] or 0)) / 1e9
                           for x in list(v)]
                       for k, v in self._active_series.items()},
            "elapsed": (time.time() - self._start_ts) if self._start_ts else 0.0,
        }

    def start(self, mode, duration, node_names, src=None, dst=None, progress_cb=None, done_cb=None):
        """Kicks off a background thread running the requested mode. Sweeps
        every known test process off every node first (a previous crashed
        session's orphaned server would otherwise hold a port). Returns
        False without doing anything if a run is already in flight."""
        if self.is_running():
            return False
        self._stop_event = threading.Event()
        self._active_series = {}
        self._progress = {"i": 0, "n": 0, "src": src, "dst": dst, "mode": mode}
        self._start_ts = time.time()
        self._progress_cb = progress_cb
        self._done_cb = done_cb
        # ssh targets are the cluster-entry hosts, not the display names
        hosts = [_member_host(self.aggregator, n) for n in node_names]
        self._hosts = hosts
        kill_all_on(hosts)

        def _on_progress(i, n, s, d):
            self._progress = {"i": i, "n": n, "src": s, "dst": d, "mode": mode}
            if self._progress_cb:
                try:
                    self._progress_cb(i, n, s, d)
                except Exception:
                    pass

        def _run():
            result, stopped = None, False
            try:
                if mode == "pair" and src and dst:
                    result = run_pair(self.aggregator, src, dst, duration,
                                       stop_event=self._stop_event, live_series=self._active_series)
                    if not self._stop_event.is_set():
                        append_run(result, self.results_path)
                elif mode == "burst":
                    result = run_burst(self.aggregator, node_names, duration,
                                        stop_event=self._stop_event, on_progress=lambda s, d: None)
                    if not self._stop_event.is_set():
                        append_run(result, self.results_path)
                else:  # sweep
                    runs = run_sweep(self.aggregator, node_names, duration, stop_event=self._stop_event,
                                      on_progress=_on_progress, live_series=self._active_series)
                    for r in runs:
                        if not self._stop_event.is_set():
                            append_run(r, self.results_path)
                    result = runs[-1] if runs else None
                stopped = self._stop_event.is_set()
            except Exception as e:
                result = {"ts": time.time(), "mode": mode, "notes": f"failed: {e}"[:200]}
            finally:
                kill_all_on(hosts)
                if self._done_cb:
                    try:
                        self._done_cb(result, stopped)
                    except Exception:
                        pass

        self._thread = threading.Thread(target=_run, daemon=True, name="spark-smi-fabtest")
        self._thread.start()
        return True

    def stop(self, node_names=None):
        """Immediate teardown: sets the stop flag (checked between pairs in
        a sweep/burst, and before the post-bandwidth latency probe) AND
        sweeps every known process pattern off every node right away --
        the sweep is what actually cuts a bandwidth job short mid-window,
        since the stop flag alone wouldn't be noticed until the current
        ib_write_bw/iperf3 client naturally exits."""
        self._stop_event.set()
        if node_names:
            hosts = [_member_host(self.aggregator, n) for n in node_names]
        else:
            hosts = getattr(self, "_hosts", None) or []
        kill_all_on(hosts)
