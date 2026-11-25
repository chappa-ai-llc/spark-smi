"""Big-screen page 1 (#14): the chart primitives, layout choice, filling
the terminal exactly, and the history/settings plumbing."""
import json
import math
import os
import stat
import sys
import tempfile
import re
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from spark_smi import app, bigpage, cluster, panels, term  # noqa: E402

G = 1024 ** 3


def text(runs):
    return "".join(t for t, _ in runs)


def wave(n, base=50, amp=40, period=37):
    return [base + amp * math.sin(i / period * 2 * math.pi) for i in range(n)]


def sample(n=600, gpus=True, rails=True, long=True):
    """A made-up two-cluster, two-GPU machine with a full long history."""
    clusters = [{"label": "Cortex-X925", "cores": list(range(10)), "loads": [20] * 10, "avg": 20.0,
                 "ghz": 3.9, "range": "00-09", "history": wave(60)},
                {"label": "Cortex-A725", "cores": list(range(10, 20)), "loads": [5] * 10, "avg": 5.0,
                 "ghz": 2.8, "range": "10-19", "history": wave(60, 10, 5)}]
    s = {
        "cpu": {"clusters": clusters, "percpu": [20] * 20, "temp": 50.0, "loadavg": (1.0, 1.0, 1.0)},
        "mem": {"vm": SimpleNamespace(total=128 * G, available=64 * G, used=60 * G, percent=50.0,
                                      cached=10 * G, buffers=0), "swap": None, "gpu_alloc": 0,
                "history": wave(60)},
        "gpus": [], "nics": [], "disks": [], "thermal": [], "power_rails": [],
        "driver": "580.00", "cuda": "13.0", "rate": 1.0,
        "caps": {"gpu": [], "net": {"rdma": True, "port_counting": True}, "has_nvml": True},
    }
    if gpus:
        s["gpus"] = [
            {"id": "0", "name": "NVIDIA GeForce RTX 3090", "util": 90, "temp": 63, "pwr_str": "330 W",
             "power_limit_mw": 350000, "mem_used": 20 * G, "mem_total": 24 * G, "clk_sm": 1600, "fan": "60%",
             "history": wave(60)},
            {"id": "1", "name": "NVIDIA GB10 (Unified)", "util": 10, "temp": 45, "pwr_str": "12 W",
             "clk_sm": 2400, "history": wave(60, 10, 5)},
        ]
        s["caps"]["gpu"] = [{"fan": True, "power_draw": True, "power_limit": True, "mem_local": True, "clocks": True},
                            {"fan": False, "power_draw": True, "power_limit": False, "mem_local": False, "clocks": True}]
    s["nics"] = [
        {"name": "enp1s0f0np0", "hw_label": "Mellanox ConnectX-7", "pf_count": 2, "up": True, "speed_mbit": 200000,
         "speed_str": "200G", "rx_bps": 4e10, "tx_bps": 4e10, "rx_pct": 20, "tx_pct": 20,
         "rx_history": wave(60, 2e10, 2e10), "tx_history": wave(60, 2e10, 2e10)},
        {"name": "enP7s7", "hw_label": "Realtek RTL8127", "up": False, "speed_mbit": 0, "speed_str": "down",
         "label": "Link Down", "rx_bps": 0, "tx_bps": 0},
    ]
    s["disks"] = [{"name": "nvme0n1", "model": "Example NVMe", "size": 4e12, "used_pct": 40, "temp": 41,
                   "read_bps": 1e6, "write_bps": 1e6, "read_history": wave(60, 1e8, 1e8),
                   "write_history": wave(60, 5e7, 5e7)}]
    if rails:
        s["power_rails"] = [{"label": "dc_input", "watts": 120.0, "cap_w": None},
                            {"label": "gpu", "watts": 60.0, "cap_w": None},
                            {"label": "syspl1", "watts": 120.0, "cap_w": 230.0}]
        s["thermal"] = [{"label": f"Zone-{c}", "temp_c": 40.0 + i, "kind": "thermal"} for i, c in enumerate("ABCDEFG")]
    if long:
        s["long"] = {"cpu": [wave(n), wave(n, 10, 5)], "mem": wave(n),
                     "gpu": {"0": wave(n), "1": wave(n, 10, 5)} if gpus else {},
                     "gpu_pwr": {"0": wave(n, 200, 100)} if gpus else {},
                     "nic": {"enp1s0f0np0": {"rx": wave(n, 2e10, 2e10), "tx": wave(n, 2e10, 2e10)}},
                     "disk": {"nvme0n1": {"r": wave(n, 1e8, 1e8), "w": wave(n, 5e7, 5e7)}},
                     "core": {i: wave(n, 20, 20, 11 + i) for i in range(20)},
                     "rail": {"dc_input": wave(n, 120, 20), "gpu": wave(n, 60, 20)},
                     "temp": {}}
    return s


def render(built, w, h):
    vc = term.VirtualCurses(size=(w, h))
    panels.render(vc, built, {i: i for i in range(16)})
    return ["".join(c for c, _ in row).rstrip() for row in vc.grid]


class Eighths(unittest.TestCase):
    def setUp(self):
        p = mock.patch.object(term, "BAR_STYLE", "eighths")
        p.start()
        self.addCleanup(p.stop)
        bigpage._FIT_CACHE.clear()


class PrimitivesTest(Eighths):
    def test_area_chart_size(self):
        rows = term.area_chart(wave(300), 40, 5, window=300)
        self.assertEqual(len(rows), 5)
        self.assertTrue(all(len(text(r)) == 40 for r in rows))

    def test_area_chart_needs_two_samples(self):
        self.assertIsNone(term.area_chart([5], 10, 3))

    def test_dial_size_and_widths(self):
        for w in (14, 16, 18):
            rows = term.dial(0.6, w, "60%", "sub", "LABEL")
            self.assertEqual(len(rows), term.dial_size(w))
            self.assertTrue(all(len(text(r)) == w for r in rows))
            self.assertIn("60%", "".join(text(r) for r in rows))
            self.assertIn("LABEL", text(rows[-1]))

    def test_dial_warn_colours(self):
        rows = term.dial(0.99, 16, "99%", warn=(0.5, 0.9))
        slots = {s for r in rows for _, s in r}
        self.assertIn(4, slots)

    def test_heat_runs_width_and_missing_history(self):
        runs = term.heat_runs([50] * 10, 20, window=40)
        self.assertEqual(len(text(runs)), 20)
        self.assertTrue(text(runs).startswith(" " * 10))   # no history that far back

    def test_time_axis(self):
        for w, span in ((60, 300), (100, 600), (170, 3600), (30, 60)):
            t = text(term.time_axis(w, span))
            self.assertEqual(len(t), w)
            self.assertIn("now", t)
            self.assertNotIn("s", t.replace("now", "")) if span >= 300 else None

    def test_fit_window_pads_old_side(self):
        pts = term.fit_window([1, 1], 4, 4)
        self.assertEqual(pts, [0.0, 0.0, 1.0, 1.0])
        self.assertEqual(len(term.fit_window(range(1000), 600, 37)), 37)


class LayoutChoiceTest(unittest.TestCase):
    def test_thresholds(self):
        self.assertIsNone(bigpage.layout_for(160, 60))
        self.assertEqual(bigpage.layout_for(240, 67), "large")
        self.assertEqual(bigpage.layout_for(320, 90), "xl")
        self.assertIsNone(bigpage.layout_for(240, 30))
        self.assertIsNone(bigpage.layout_for(240, 67, "compact"))
        self.assertEqual(bigpage.layout_for(150, 50, "large"), "tall")
        self.assertIsNone(bigpage.layout_for(150, 50, "auto"))
        self.assertIsNone(bigpage.layout_for(240, 500))   # --shot's "whole page" height

    def test_history_steps(self):
        self.assertEqual(bigpage.history_step(None, 1, "large"), 600)
        self.assertEqual(bigpage.history_step(None, -1, "xl"), 300)
        self.assertEqual(bigpage.history_step(3600, 1), 3600)
        self.assertEqual(bigpage.history_step(60, -1), 60)
        self.assertEqual(bigpage.history_step(90, 1), 300)


class BuildTest(Eighths):
    def check_fills(self, s, w, h, layout):
        built = bigpage.build_page1_large(s, w, h, 0, layout)
        self.assertIsNotNone(built)
        rows = render(built, w, h)
        self.assertTrue(rows[h - 1].strip(), "footer on the last row")
        self.assertTrue(all(len(r) <= w for r in rows))
        # nothing blank between the header and the footer except chart gaps:
        # the frame's left rail runs down every body row
        body = rows[1:h - 1]
        self.assertTrue(all(r[:1] in ("│", "╭", "├", "╰") for r in body), body)
        return rows

    def test_large_fills_terminal(self):
        rows = self.check_fills(sample(), 240, 67, "large")
        self.assertIn("AT A GLANCE", rows[1])
        self.assertTrue(any("GPU 0 POWER" in r for r in rows))

    def test_xl_has_power_thermal_and_heatmap(self):
        rows = self.check_fills(sample(), 320, 90, "xl")
        joined = "\n".join(rows)
        self.assertIn("POWER", joined)
        self.assertIn("THERMAL", joined)
        self.assertIn("per-core load", joined)

    def test_heatmap_lists_every_core(self):
        """Live at 320x90 the heatmap stopped at core 8 of 20 (5.0.0)."""
        for h in (60, 70, 90, 120):
            built = bigpage.build_page1_large(sample(), 320, h, 0, "xl")
            if built is None:
                continue
            joined = "\n".join(render(built, 320, h))
            if "per-core load" not in joined:
                continue
            shown = {i for i in range(20) if re.search(rf"core {i:>2} ", joined)}
            self.assertEqual(shown, set(range(20)), f"height {h}")

    def test_heat_steps(self):
        self.assertEqual(bigpage._heat_steps(20, 106), [7, 10, 20])
        self.assertEqual(bigpage._heat_steps(20, 20), [])
        self.assertEqual(bigpage._heat_steps(0, 106), [])

    def test_no_gpu_no_gpu_column(self):
        rows = self.check_fills(sample(gpus=False), 240, 67, "large")
        self.assertFalse(any("GPU 0" in r for r in rows))

    def test_remote_sample_without_long_history(self):
        self.check_fills(sample(long=False), 240, 67, "large")

    def test_xl_without_rails_falls_back_to_two_columns(self):
        rows = self.check_fills(sample(rails=False), 320, 90, "xl")
        self.assertFalse(any("THERMAL" in r for r in rows))

    def test_too_short_returns_none(self):
        self.assertIsNone(bigpage.build_page1_large(sample(), 240, 20, 0, "large"))

    def test_ascii_tier_returns_none(self):
        with mock.patch.object(term, "BAR_STYLE", "ascii"):
            self.assertIsNone(bigpage.build_page1_large(sample(), 240, 67, 0, "large"))

    def test_fabric_dial_names_its_unit(self):
        """A quiet fabric reads "0 Mb/s", not a unitless "0G" (found live, 5.0.0)."""
        s = sample()
        for n in s["nics"]:
            n["rx_bps"] = n["tx_bps"] = 0
        fab = [d for d in bigpage._dials(s, s["long"]) if d[3] == "FABRIC"]
        self.assertEqual(fab[0][1], "0 Mb/s")
        s["nics"][0]["rx_bps"] = 4e10
        fab = [d for d in bigpage._dials(s, s["long"]) if d[3] == "FABRIC"]
        self.assertEqual(fab[0][1], "40.0 Gb/s")

    def test_history_label_in_strip(self):
        built = bigpage.build_page1_large(sample(), 240, 67, 0, "large", history=1800)
        self.assertIn("history 30m", "\n".join(render(built, 240, 67)))


class PlumbingTest(unittest.TestCase):
    def test_parse_span(self):
        self.assertEqual(app.parse_span("90"), 90)
        self.assertEqual(app.parse_span("5m"), 300)
        self.assertEqual(app.parse_span("1h"), 3600)
        self.assertIsNone(app.parse_span("2h"))
        self.assertIsNone(app.parse_span("5s"))
        self.assertIsNone(app.parse_span("soon"))

    def test_settings_round_trip_owner_only(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": d}):
            self.assertEqual(app.load_settings(), {})
            app.save_settings(graphs="large")
            app.save_settings(history=600)
            self.assertEqual(app.load_settings(), {"graphs": "large", "history": 600})
            mode = stat.S_IMODE(os.stat(app.settings_path()).st_mode)
            self.assertEqual(mode, 0o600)

    def test_unreadable_settings_are_ignored(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": d}):
            os.makedirs(os.path.join(d, "spark-smi"))
            with open(app.settings_path(), "w") as f:
                f.write("{not json")
            self.assertEqual(app.load_settings(), {})

    def test_wire_drops_long_history(self):
        s = sample()
        wire = cluster.to_wire(s)
        self.assertNotIn("long", wire)
        self.assertIn("long", s)              # the local sample keeps it
        json.dumps(wire, default=str)


if __name__ == "__main__":
    unittest.main()
