"""Cluster mode (Phase 6): JSON wire format for one node's sample, a
read-only HTTP sample server (--serve), --cluster host-list parsing, and the
background aggregator that polls every member once per tick without ever
blocking the render thread. Stdlib only (json/threading/urllib/http.server/
subprocess) -- no new dependencies, matching the rest of the app.

Wire format: State.sample() is already plain dicts/lists/primitives EXCEPT
mem.vm/mem.swap, which are psutil named tuples -- to_wire()/from_wire() are
the only two functions that know that; every other collector field round-
trips through json.dumps/json.loads unchanged. build_json_payload() is what
`--json`, `--serve`'s /sample, and the aggregator's HTTP/ssh polls all
produce/consume, so a node polling itself over HTTP sees exactly the same
shape it would have gotten from a local State.sample() call.
"""
import hmac
import http.server
import re
import socket
import json
import os
import platform
import subprocess
import sys
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

try:
    from . import VERSION
except ImportError:
    VERSION = "4.0.0a1"

DEFAULT_PORT = 8817
POLL_TIMEOUT = 0.75          # per-member urllib timeout (spec)
SSH_CONNECT_TIMEOUT = 3.0    # ssh bootstrap fallback -- much slower than HTTP
SSH_TOTAL_TIMEOUT = 5.0
STALE_MULTIPLIER = 3         # a member is "stale" after 3 * rate seconds
CACHE_TTL = 0.5              # --serve response cache (spec: no poll-storm hammering)
DEFAULT_CONFIG_PATH = os.path.expanduser(os.path.join("~", ".config", "spark-smi", "cluster"))


def local_hostname():
    return (_safe(platform.node) or "localhost").split(".")[0]


def _safe(fn, default=None):
    try:
        return fn()
    except Exception:
        return default


def _read_text_stripped(path):
    try:
        with open(path, "rb") as f:
            data = f.read()
        text = data.decode("utf-8", "ignore").replace("\x00", "").strip()
        return text or None
    except Exception:
        return None


def machine_model():
    """Same device-tree/DMI probe pages.py's header uses -- duplicated here
    (rather than imported) because it's one tiny, dependency-free helper and
    importing pages.py from cluster.py just for this would risk a cycle
    (pages.py has no reason to import cluster.py, but app.py imports both)."""
    m = (_read_text_stripped("/proc/device-tree/model")
         or _read_text_stripped("/sys/devices/virtual/dmi/id/product_name"))
    return m.replace("_", " ") if m else None


# =========================================================================
# Wire format
# =========================================================================

def _tuple_to_dict(obj):
    if obj is None:
        return None
    if hasattr(obj, "_asdict"):
        try:
            return dict(obj._asdict())
        except Exception:
            return None
    if isinstance(obj, dict):
        return obj
    return None


def to_wire(sample):
    """sample() -> a JSON-safe plain dict. mem.vm/mem.swap (psutil named
    tuples) are the only fields in State.sample() that aren't already plain
    dicts/lists/primitives -- everything else passes through untouched."""
    out = dict(sample)
    mem = dict(out.get("mem") or {})
    mem["vm"] = _tuple_to_dict(mem.get("vm"))
    mem["swap"] = _tuple_to_dict(mem.get("swap"))
    out["mem"] = mem
    return out


def from_wire(payload):
    """Reverse of to_wire(): reconstructs mem.vm/mem.swap as SimpleNamespace
    so pages.py's build_page1/build_page2 -- written against psutil's named-
    tuple attribute access (vm.total, vm.percent, getattr(vm, "cached", 0),
    ...) -- work UNMODIFIED against a remote sample. This is the whole point
    of the wire format being "the same shape sample() already produces"."""
    sample = dict(payload)
    mem = dict(sample.get("mem") or {})
    vm, swap = mem.get("vm"), mem.get("swap")
    mem["vm"] = SimpleNamespace(**vm) if isinstance(vm, dict) else None
    mem["swap"] = SimpleNamespace(**swap) if isinstance(swap, dict) else None
    sample["mem"] = mem
    return sample


def build_json_payload(state):
    """The --json / HTTP /sample wire payload: State.sample() plus node
    metadata, merged at the TOP level (payload["node"] alongside
    payload["cpu"], not nested under a "meta" key) so every consumer --
    --json's stdout, /sample's response body, the cluster aggregator -- sees
    one flat dict."""
    sample = state.sample()
    payload = to_wire(sample)
    payload["node"] = local_hostname()
    payload["model"] = machine_model()
    payload["version"] = VERSION
    payload["ts"] = time.time()
    return payload


def dumps(payload):
    """json.dumps with the default=str safety net the spec calls for -- a
    guard against some future collector field sneaking in a non-primitive
    (e.g. a Path); nothing in the current sample() shape needs it."""
    return json.dumps(payload, default=str)


# =========================================================================
# HTTP server (--serve)
# =========================================================================

class _SampleCache:
    """Serializes the same sample for CACHE_TTL seconds so a poll storm from
    many cluster members can't hammer the local collectors -- collection
    still happens on whichever request thread misses the cache
    (ThreadingHTTPServer), but at most once per CACHE_TTL window."""

    def __init__(self, state):
        self.state = state
        self.lock = threading.Lock()
        self._body = None
        self._ts = 0.0

    def get(self):
        with self.lock:
            now = time.time()
            if self._body is None or now - self._ts >= CACHE_TTL:
                payload = build_json_payload(self.state)
                self._body = dumps(payload).encode("utf-8")
                self._ts = now
            return self._body


def _make_handler(cache, token, verbose):
    class Handler(http.server.BaseHTTPRequestHandler):
        server_version = f"spark-smi/{VERSION}"
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):
            if verbose:
                print(f"{self.address_string()} - {fmt % args}", file=sys.stderr)

        def _authorized(self):
            if not token:
                return True
            got = self.headers.get("X-Spark-Token", "")
            return hmac.compare_digest(got, token)

        def _send_json(self, code, obj_or_bytes):
            body = obj_or_bytes if isinstance(obj_or_bytes, bytes) else dumps(obj_or_bytes).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except Exception:
                pass

        def do_GET(self):
            try:
                path = self.path.split("?", 1)[0]
                if path == "/healthz":
                    self._send_json(200, {"ok": True, "node": local_hostname(), "version": VERSION})
                    return
                if path == "/sample":
                    if not self._authorized():
                        self._send_json(403, {"error": "forbidden"})
                        return
                    self._send_json(200, cache.get())
                    return
                self._send_json(404, {"error": "not found"})
            except Exception:
                try:
                    self._send_json(500, {"error": "internal"})
                except Exception:
                    pass

        # Read-only server: every other method is rejected outright.
        def _method_not_allowed(self):
            self._send_json(405, {"error": "method not allowed"})

        do_POST = do_PUT = do_DELETE = do_PATCH = do_HEAD = _method_not_allowed

    return Handler


def run_server(state, port=DEFAULT_PORT, verbose=False, host=""):
    """Blocks serving GET /sample + /healthz until Ctrl-C. Read-only: any
    other method/path gets 404/405. Optional shared-secret auth via
    $SPARK_SMI_TOKEN (header X-Spark-Token, constant-time compare -> 403
    when set and missing/wrong). Logs one line per request to stderr only
    when `verbose` (--serve-verbose)."""
    token = os.environ.get("SPARK_SMI_TOKEN") or None
    cache = _SampleCache(state)
    handler = _make_handler(cache, token, verbose)
    httpd = http.server.ThreadingHTTPServer((host, port), handler)
    httpd.daemon_threads = True
    print(f"spark-smi --serve: listening on :{port} (node {local_hostname()})"
          + (" [token required]" if token else " [no token -- open on this port]"), file=sys.stderr)
    try:
        httpd.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


# =========================================================================
# --cluster host-list parsing
# =========================================================================

def _parse_entry(raw):
    raw = raw.strip()
    if raw.startswith("ssh:"):
        return {"kind": "ssh", "host": raw[4:], "port": None, "raw": raw}
    if ":" in raw:
        host, _, port_s = raw.rpartition(":")
        try:
            port = int(port_s)
        except ValueError:
            host, port = raw, DEFAULT_PORT
    else:
        host, port = raw, DEFAULT_PORT
    return {"kind": "http", "host": host, "port": port, "raw": raw}


def parse_cluster_hosts(value):
    """`value` is the --cluster CLI argument (may be None/""  for a bare
    --cluster): a comma-separated host list, "@path/to/file" (one entry per
    line, "#" comments, blank lines skipped), or nothing -- in which case
    ~/.config/spark-smi/cluster is tried. Entry forms: "host" (poll
    http://host:8817/sample), "host:port", "ssh:host" (bootstrap fallback:
    `ssh -o BatchMode=yes host spark-smi --json` per tick -- fine for small
    clusters, not a substitute for --serve at any real scale). Returns
    (entries, cluster_name); cluster_name is the @file stem, or "cluster"
    for an inline list / the config-file fallback. Raises on an explicit
    @file/config path that doesn't exist or can't be read -- caller decides
    how to report that (main() prints and exits)."""
    name = "cluster"
    lines = []
    value = value.strip() if value else value
    if value and value.startswith("@"):
        path = value[1:]
        name = os.path.splitext(os.path.basename(path))[0] or "cluster"
        with open(path, encoding="utf-8") as f:
            lines = f.readlines()
    elif value:
        lines = value.split(",")
    else:
        path = DEFAULT_CONFIG_PATH
        if not os.path.exists(path):
            raise FileNotFoundError(
                f"--cluster given with no hosts and no config file at {path}")
        with open(path, encoding="utf-8") as f:
            lines = f.readlines()
    entries = []
    for line in lines:
        line = line.split("#", 1)[0].strip()
        if line:
            entries.append(_parse_entry(line))
    return entries, name


# =========================================================================
# Aggregator: background polling, never blocks the render thread
# =========================================================================

class ClusterMember:
    def __init__(self, entry, is_local):
        self.entry = entry
        self.is_local = is_local
        self.name = entry["host"]
        self.sample = None       # freshest known parsed sample dict, or None
        self.last_ok_ts = 0.0
        self.error = None
        self.last_seen_summary = None  # short text kept around after going stale
        self._lock = threading.Lock()

    def snapshot(self):
        with self._lock:
            return self.sample, self.last_ok_ts, self.error, self.last_seen_summary

    def set_ok(self, sample):
        with self._lock:
            self.sample = sample
            self.last_ok_ts = time.time()
            self.error = None
            self.last_seen_summary = _summarize(sample)

    def set_err(self, msg):
        with self._lock:
            self.error = msg


def _summarize(sample):
    """One-line "last seen" text for a member that's since gone stale, e.g.
    "gpu 71% · 342 W · asic 66C" (mock_cluster_fleet's sparky-07 row).
    Best-effort -- any missing field is just dropped, never a crash."""
    try:
        gpus = sample.get("gpus") or []
        bits = []
        if gpus:
            # busiest GPU, not gpus[0]: eGPUs enumerate as index 0 on the DGX
            # Spark, which would make a stale-node summary quote the wrong chip
            g = max(gpus, key=lambda g: g.get("util") or 0)
            util = g.get("util")
            if util is not None:
                bits.append(f"gpu {int(util)}%")
            pwr = g.get("pwr_str")
            if pwr and pwr != "N/A":
                bits.append(pwr)
        asic = sample.get("nic_asic_temp")
        if asic is not None:
            bits.append(f"asic {int(asic)}C")
        return " · ".join(bits) or None
    except Exception:
        return None


_LOCAL_DOMAINS_CACHE = None


def _local_domains():
    """Candidate DNS suffixes for short-name entries, best source first:
    reverse-PTR of our own primary IP (the only source that told the truth
    on the reference network — getfqdn() said 'localhost' and the DHCP
    search domain pointed at an AD zone that doesn't hold these hosts),
    then getfqdn, then resolv.conf search domains. Cached — three probes
    per process is enough."""
    global _LOCAL_DOMAINS_CACHE
    if _LOCAL_DOMAINS_CACHE is not None:
        return _LOCAL_DOMAINS_CACHE
    domains = []
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(1.0)
        s.connect(("8.8.8.8", 80))
        own_ip = s.getsockname()[0]
        s.close()
        ptr = socket.gethostbyaddr(own_ip)[0]
        if "." in ptr:
            domains.append(ptr.split(".", 1)[1])
    except Exception:
        pass
    try:
        fqdn = socket.getfqdn()
        if "." in fqdn and fqdn != "localhost.localdomain":
            domains.append(fqdn.split(".", 1)[1])
    except Exception:
        pass
    try:
        with open("/etc/resolv.conf") as f:
            for line in f:
                parts = line.split()
                if parts and parts[0] in ("search", "domain"):
                    domains.extend(p for p in parts[1:] if "." in p or p.isalpha())
    except Exception:
        pass
    seen, out = set(), []
    for d in domains:
        if d and d not in seen:
            seen.add(d)
            out.append(d)
    _LOCAL_DOMAINS_CACHE = out
    return out


def _resolve_host(member):
    """The host to actually connect to. On the first DNS failure of a
    dot-less entry, tries entry+local-domain candidates once and caches
    whichever resolves (member.resolved_host)."""
    cached = getattr(member, "resolved_host", None)
    if cached:
        return cached
    host = member.entry["host"]
    try:
        socket.getaddrinfo(host, None)
        member.resolved_host = host
        return host
    except Exception:
        pass
    if "." not in host:
        for dom in _local_domains():
            cand = f"{host}.{dom}"
            try:
                socket.getaddrinfo(cand, None)
                member.resolved_host = cand
                return cand
            except Exception:
                continue
    return host  # let the poll fail and report DNS


def _short_error(e):
    """Compress an exception into the word a human debugging 'offline'
    actually needs: dns / refused / timeout / http NNN / ..."""
    s = str(e)
    if isinstance(e, OSError) and isinstance(getattr(e, "reason", None), Exception):
        s = str(e.reason)
    low = s.lower()
    if "name resolution" in low or "getaddrinfo" in low or "name or service" in low:
        return "dns"
    if "refused" in low:
        return "refused"
    if "timed out" in low or "timeout" in low:
        return "timeout"
    m = re.search(r"HTTP Error (\d+)", s)
    if m:
        return f"http {m.group(1)}"
    return s[:40]


def _poll_http(member, token):
    entry = member.entry
    url = f"http://{_resolve_host(member)}:{entry['port']}/sample"
    req = urllib.request.Request(url)
    if token:
        req.add_header("X-Spark-Token", token)
    try:
        with urllib.request.urlopen(req, timeout=POLL_TIMEOUT) as resp:
            body = resp.read()
        payload = json.loads(body.decode("utf-8"))
        member.set_ok(from_wire(payload))
    except Exception as e:
        member.set_err(_short_error(e))


def _poll_ssh(member):
    entry = dict(member.entry)
    entry["host"] = _resolve_host(member)
    try:
        res = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "-o", f"ConnectTimeout={int(SSH_CONNECT_TIMEOUT)}",
             entry["host"], "spark-smi", "--json"],
            capture_output=True, text=True, timeout=SSH_TOTAL_TIMEOUT)
        if res.returncode != 0:
            member.set_err((res.stderr or "ssh failed").strip()[:200] or f"ssh exit {res.returncode}")
            return
        payload = json.loads(res.stdout)
        member.set_ok(from_wire(payload))
    except Exception as e:
        member.set_err(str(e))


def _poll_local(member, local_state):
    try:
        member.set_ok(local_state.sample())
    except Exception as e:
        member.set_err(str(e))


class ClusterAggregator:
    """Owns the member list and a background thread pool that polls every
    non-local member once per tick (`rate` seconds). The render thread only
    ever calls get_views() -- it never blocks on the network; it draws
    whatever's freshest and marks a member stale once its last_ok_ts is more
    than STALE_MULTIPLIER * rate seconds old (or it's never answered)."""

    def __init__(self, entries, local_state, rate=1.0, token=None):
        self.rate = max(0.1, rate)
        self.token = token
        self.local_state = local_state
        here = local_hostname()
        self.members = [ClusterMember(e, is_local=(e["host"].split(".")[0] == here)) for e in entries]
        self._pool = ThreadPoolExecutor(max_workers=max(1, min(16, len(self.members) or 1)))
        self._stop = threading.Event()
        self._thread = None

    def _submit(self, m):
        if m.is_local:
            return self._pool.submit(_poll_local, m, self.local_state)
        if m.entry["kind"] == "ssh":
            return self._pool.submit(_poll_ssh, m)
        return self._pool.submit(_poll_http, m, self.token)

    def poll_once_sync(self, budget=2.0):
        """One blocking round, every member polled concurrently, capped at
        `budget` seconds total -- used by snapshot mode's `--page 3`."""
        futures = [self._submit(m) for m in self.members]
        deadline = time.time() + budget
        for f in futures:
            try:
                f.result(timeout=max(0.05, deadline - time.time()))
            except Exception:
                pass

    def _run(self):
        while not self._stop.is_set():
            for m in self.members:
                self._submit(m)
            self._stop.wait(self.rate)

    def start(self):
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, daemon=True, name="spark-smi-cluster-poll")
            self._thread.start()

    def stop(self):
        self._stop.set()
        try:
            self._pool.shutdown(wait=False, cancel_futures=True)
        except TypeError:
            self._pool.shutdown(wait=False)

    def get_views(self):
        """[{name, sample, last_ok_ts, error, stale, is_local,
        last_seen_summary}, ...] in member order -- the only thing
        pages.build_page3 (and app.py's drilldown lookup) reads."""
        now = time.time()
        out = []
        for m in self.members:
            sample, last_ok, err, last_seen = m.snapshot()
            stale = (sample is None) or ((now - last_ok) > (STALE_MULTIPLIER * self.rate))
            # Display name: prefer the node's own short hostname from its
            # payload over the connection string ("sparky-2", not the
            # truncated "sparky-2.chappa.ai" the cluster list happened to use)
            name = m.name
            if sample and sample.get("node"):
                name = str(sample["node"]).split(".")[0]
            elif any(c.isalpha() for c in name):
                # No payload node name (the local member's sample isn't wire-
                # wrapped): still shorten an FQDN entry -- but never split a
                # bare IP address on its dots
                name = name.split(".")[0]
            out.append({"name": name, "sample": sample, "last_ok_ts": last_ok, "error": err,
                        "stale": stale, "is_local": m.is_local, "last_seen_summary": last_seen})
        return out

    def get_member_sample(self, name):
        """Accepts either the cluster-list entry name or the payload's short
        hostname (views() displays the latter when a sample has arrived)."""
        for m in self.members:
            sample, _, _, _ = m.snapshot()
            payload_name = str(sample.get("node", "")).split(".")[0] if sample else None
            if name in (m.name, payload_name):
                return sample
        return None


# =========================================================================
# ClusterUI: live-mode key-driven state for page 3 (mirrors knobs.KnobUI's
# "pure state transitions, no curses" convention so it's testable headless)
# =========================================================================

SORT_MODES = ["name", "gpu", "power", "alerts"]


class ClusterUI:
    def __init__(self):
        self.selected = 0
        self.scroll = 0
        self.filter_alerts = False
        self.sort_idx = 0
        self.drilldown = False
        self.drilldown_node = None
        self.drilldown_page = 1
        # Cached by pages.build_page3 every render tick (same convention as
        # knobs.KnobUI.last_registry/n_gpus): main_loop's key handling runs
        # BETWEEN renders and has no view of build_page3's own
        # filter/sort/windowing, so it reads these instead of recomputing
        # them -- last_n is the post-filter/sort node count (what ↑/↓ should
        # clamp against), selected_name is the currently-selected row's node
        # name (what Enter drills into).
        self.last_n = 0
        self.selected_name = None

    @property
    def sort_mode(self):
        return SORT_MODES[self.sort_idx]

    def cycle_sort(self):
        self.sort_idx = (self.sort_idx + 1) % len(SORT_MODES)

    def toggle_filter(self):
        self.filter_alerts = not self.filter_alerts
        self.selected = 0
        self.scroll = 0

    def move(self, delta, n):
        if n <= 0:
            return
        self.selected = max(0, min(n - 1, self.selected + delta))

    def clamp_scroll(self, n, visible):
        if visible <= 0:
            return
        if self.selected < self.scroll:
            self.scroll = self.selected
        elif self.selected >= self.scroll + visible:
            self.scroll = self.selected - visible + 1
        self.scroll = max(0, min(max(0, n - visible), self.scroll))

    def enter_drilldown(self, node_name):
        self.drilldown = True
        self.drilldown_node = node_name
        self.drilldown_page = 1

    def exit_drilldown(self):
        self.drilldown = False
        self.drilldown_node = None
