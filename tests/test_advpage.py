"""Page 2's big-screen layout (#14) and the history State keeps for it."""
import os
import sys
import unittest
from collections import deque
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from spark_smi import advpage, app, panels, term  # noqa: E402

G = 1024 ** 3
N = 300


def _sample(n_gpus=2, n_pf=4, n_temp=27, spbm=True, long=True, polls=N):
    gpus = [{"id": "0", "name": "NVIDIA GeForce RTX 3090", "util": 98, "temp": 63, "pwr_str": "348 W",
             "power_limit_mw": 350000, "clk_sm": 1650, "clk_sm_max": 2100, "clk_mem": 9501, "fan": "68%",
             "pcie_gen_cur": 4, "pcie_width_cur": 4, "pcie_gen_max": 4, "pcie_width_max": 4,
             "pcie_rx_kbs": 70000, "pcie_tx_kbs": 3000, "temp_slowdown": 95, "temp_shutdown": 98,
             "throttle": {}, "persistence": "on", "compute_mode": "default", "ecc": "n/a", "vbios": "x",
             "procs": [{"pid": 1, "name": "llama-server", "mem": 23 * G}]},
            {"id": "1", "name": "NVIDIA GB10 (Unified)", "util": 96, "temp": 48, "pwr_str": "13 W",
             "clk_sm": 2400, "clk_sm_max": 3003, "persistence": "on", "procs": []}][:n_gpus]
    caps = [{"fan": True, "power_draw": True, "power_limit": True, "mem_local": True, "pcie": True, "clocks": True},
            {"fan": False, "power_draw": True, "power_limit": False, "mem_local": False, "pcie": False,
             "clocks": True}][:n_gpus]
    pfs = [{"port": f"p{i // 2}", "rdma_dev": f"rocep{i}", "netdev": f"enp{i}np0", "up": True, "speed_str": "200G",
            "speed_mbit": 200000, "rx_bps": 5e9 * i, "tx_bps": 3e9 * i, "cnp_sent": 0, "cnp_handled": 0,
            "ecn_marked": i, "discard": 0} for i in range(n_pf)]
    thermal = [{"label": f"sensor_{i}", "temp_c": 30.0 + i, "kind": "hwmon", "hwname": "spbm"} for i in range(n_temp)]
    rails = ([{"label": "dc_input", "watts": 60.0}, {"label": "soc_pkg", "watts": 33.0},
              {"label": "pl1", "watts": 33.0, "cap_w": 140}, {"label": "syspl1", "watts": 56.0, "cap_w": 231}]
             if spbm else [])
    s = {"gpus": gpus, "caps": {"gpu": caps, "thermal": {"spbm": spbm}, "has_nvml": True},
         "mem": {"vm": SimpleNamespace(total=128 * G, used=60 * G, percent=47.0), "gpu_alloc": 30 * G,
                 "swap": SimpleNamespace(total=64 * G, used=1 * G)},
         "nics": [{"name": "p0", "hw_label": "ConnectX-7", "pf_count": 2}, {"name": "p1", "hw_label": "ConnectX-7",
                                                                            "pf_count": 2}][:max(1, n_pf // 2)],
         "nic_pf": pfs, "nic_fw": {}, "thermal": thermal, "power_rails": rails, "smart": None,
         "pcie_links": [], "driver": "580", "cuda": "13.0", "rate": 1.0, "long": None}
    if long:
        ramp = [float(i % 60) for i in range(polls)]
        s["long"] = {
            "gpu_pwr": {"0": [300.0 + r for r in ramp], "1": [10.0 + r / 10 for r in ramp]},
            "rail": {"dc_input": [50.0 + r for r in ramp]},
            "temp": {f"spbm:sensor_{i}": [30.0 + i + r / 10 for r in ramp] for i in range(n_temp)},
            "adv": {"gpu": {"0": {"clk": [1600.0 + r for r in ramp], "temp": [60.0 + r / 20 for r in ramp],
                                  "pcie_rx": [7e7 + r * 1e6 for r in ramp], "pcie_tx": [3e6] * polls},
                            "1": {"clk": [2400.0] * polls, "temp": [48.0] * polls}},
                    "pf": {p["netdev"]: {"rx": [p["rx_bps"]] * polls, "tx": [p["tx_bps"]] * polls,
                                         "cong": [float(i)] * polls} for i, p in enumerate(pfs)}},
        }
    return s


def _text(built, w, h):
    vc = term.VirtualCurses(size=(w, h))
    panels.render(vc, built, {i: i for i in range(16)})
    return ["".join(c for c, _ in row) for row in vc.grid]


def _row_end(segs):
    """The content column a row's segments reach (col None flows on)."""
    x = 0
    for col, text, _ in segs:
        x = (col if col is not None else x) + len(text)
    return x


class AdvPageTest(unittest.TestCase):
    def setUp(self):
        self._style = term.BAR_STYLE
        term.BAR_STYLE = "eighths"

    def tearDown(self):
        term.BAR_STYLE = self._style

    def _assert_fills(self, built, w, h):
        self.assertIsNotNone(built)
        footer = built[-1]
        self.assertEqual(footer.y, h - 1)
        for p in built[1:-1]:
            self.assertLessEqual(p.y + p.total_height, h - 2, p.title)      # bottom border above the footer
            self.assertLessEqual(p.x + p.w, w)
        bottoms = [p.y + p.total_height for p in built[1:-1]]
        self.assertEqual(max(bottoms), h - 2)       # nothing left blank under the last panels

    def test_fills_the_terminal(self):
        for w, h, layout in ((240, 67, "large"), (380, 75, "xl"), (420, 110, "xl"), (300, 60, "xl")):
            built = advpage.build_page2_large(_sample(), w, h, 0, layout)
            self._assert_fills(built, w, h)

    def test_columns_end_together(self):
        built = advpage.build_page2_large(_sample(), 380, 75, 0, "xl")
        gpu_cols = [p for p in built if any("GPU " in t for t, _ in p.title)]
        self.assertEqual(len(gpu_cols), 2)
        self.assertEqual(len({p.total_height for p in gpu_cols}), 1)

    def test_rows_never_overflow_their_panel(self):
        for w, h in ((380, 75), (240, 67), (300, 60)):
            built = advpage.build_page2_large(_sample(), w, h, 0, "xl" if w >= 300 else "large")
            for p in built[1:-1]:
                for segs, right in p.rows:
                    end = _row_end(segs) + sum(len(t) for t, _ in right)
                    self.assertLessEqual(end, p.w - 2, (p.title, segs[:2]))

    def test_charts(self):
        text = "\n".join(_text(advpage.build_page2_large(_sample(), 380, 75, 0, "xl"), 380, 75))
        for word in ("SM CLOCK", "POWER", "TEMP", "PCIE", "PORTS", "CONGESTION", "THERMALS", "DC_INPUT"):
            self.assertIn(word, text)

    def test_falls_back_to_normal_page(self):
        self.assertIsNone(advpage.build_page2_large(_sample(long=False), 380, 75, 0, "xl"))   # remote drilldown
        self.assertIsNone(advpage.build_page2_large(_sample(n_gpus=0, n_pf=0, n_temp=0), 380, 75))
        self.assertIsNone(advpage.build_page2_large(_sample(), 200, 30))                       # nothing fits
        term.BAR_STYLE = "ascii"
        self.assertIsNone(advpage.build_page2_large(_sample(), 380, 75, 0, "xl"))

    def test_without_spbm_or_nics(self):
        built = advpage.build_page2_large(_sample(spbm=False, n_pf=0), 380, 75, 0, "xl")
        self._assert_fills(built, 380, 75)
        self.assertFalse(any((p.title or [("", 0)])[0][0] == "PORTS" for p in built))

    def test_knob_selection_shows(self):
        ctx = {"registry": [], "selected_gpu": 1, "focus_id": None, "pending": None, "confirming": False}
        built = advpage.build_page2_large(_sample(), 380, 75, 0, "xl", knob_ui=ctx)
        titles = ["".join(t for t, _ in p.title) for p in built]
        self.assertTrue(any(t.startswith("> GPU 1") for t in titles))

    def test_cells_are_exact_width(self):
        for w in (44, 72, 101):
            cell = advpage.area_cell([("A ", 6)], [("peak 9", 3)], list(range(100)), 100, {0: "100%", 3: "0"}, w, 4, 300)
            self.assertEqual({sum(len(t) for t, _ in row) for row in cell}, {w})
            self.assertEqual(len(cell), advpage.cell_rows(4))
            m = advpage.mirror_cell([("B", 6)], [], [1e9] * 50, [2e9] * 50, 1e8, w, 4, 300)
            self.assertEqual({sum(len(t) for t, _ in row) for row in m}, {w})
            self.assertEqual(len(m), advpage.cell_rows(4, True))

    def test_axis_labels_fit(self):
        for v in (0, 5e3, 7.5e7, 1.2e9, 24e9, 512e6):
            self.assertLessEqual(len(advpage._axis_bytes(v)), 4)

    def test_heat_columns(self):
        self.assertEqual(advpage.heat_cols(27, 200, 30), 1)
        self.assertEqual(advpage.heat_cols(27, 200, 14), 2)
        self.assertEqual(advpage.heat_cols(27, 60, 14), 0)      # one column only, too tall


class AdvHistoryTest(unittest.TestCase):
    def _state(self):
        st = SimpleNamespace(adv_hist={})
        st._push = lambda store, key, value: app.State._push(st, store, key, value)
        return st

    def test_series(self):
        st = self._state()
        gpus = [{"id": "0", "clk_sm": 1500, "temp": 60, "pcie_rx_kbs": 100, "pcie_tx_kbs": 50},
                {"id": "1", "clk_sm": "N/A", "temp": "N/A"}]
        pfs = [{"netdev": "enp1", "rx_bps": 1e9, "tx_bps": 2e9, "cnp_sent": 1, "cnp_handled": 2, "ecn_marked": 3},
               {"netdev": "enp2", "rx_bps": 0, "tx_bps": 0, "cnp_sent": None, "cnp_handled": None,
                "ecn_marked": None}]
        out = app.State._adv_history(st, gpus, pfs)
        out = app.State._adv_history(st, gpus, pfs)
        self.assertEqual(out["gpu"]["0"]["clk"], [1500.0, 1500.0])
        self.assertEqual(out["gpu"]["0"]["pcie_rx"], [102400.0] * 2)
        self.assertEqual(out["gpu"]["1"], {})                    # N/A starts no series
        self.assertEqual(out["pf"]["enp1"]["cong"], [6.0, 6.0])
        self.assertNotIn("cong", out["pf"]["enp2"])
        self.assertEqual(len(st.adv_hist[("gpu", "0", "clk")]), 2)
        self.assertIsInstance(st.adv_hist[("gpu", "0", "clk")], deque)


if __name__ == "__main__":
    unittest.main()
