"""Page 3's big-screen layout (#14 step 3) and the viewer-side history."""
import os
import sys
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from spark_smi import cluster, clusterpage, panels, term  # noqa: E402

G = 1024 ** 3


def _sample(util=50, gpus=1, fabric=True, rails=True):
    cl = [{"label": "Cortex-X925", "cores": list(range(10)), "loads": [util] * 10, "avg": util, "ghz": 3.9,
           "history": [float(util)] * 60}]
    gl = [{"id": str(i), "name": "NVIDIA GB10 (Unified)", "util": util, "temp": 55, "pwr_str": "40 W",
           "power_limit_mw": 100000, "history": [float(util)] * 60,
           "procs": [{"pid": 100 + i, "name": "python3", "mem": 4 * G}]} for i in range(gpus)]
    nics = [{"name": "mgmt0", "up": True, "speed_mbit": 10000, "rx_bps": 1e6, "tx_bps": 2e6,
             "rx_history": [1e6] * 60, "tx_history": [2e6] * 60}]
    if fabric:
        nics.append({"name": "fab0", "up": True, "speed_mbit": 200000, "rx_bps": 5e9, "tx_bps": 3e9,
                     "rx_history": [5e9] * 60, "tx_history": [3e9] * 60})
    return {"cpu": {"clusters": cl, "temp": 50},
            "mem": {"vm": SimpleNamespace(total=100 * G, used=40 * G, percent=40.0), "history": [40.0] * 60},
            "gpus": gl, "nics": nics, "nic_pf": [], "disks": [],
            "power_rails": ([{"label": "dc_input", "watts": 80.0}, {"label": "syspl1", "watts": 0, "cap_w": 240}]
                            if rails else [])}


def _views(n, gpus=1, polls=120):
    out = []
    for k in range(n):
        s = _sample(util=10 * k, gpus=gpus if k == 0 else 1)
        h = cluster.NodeHistory()
        for _ in range(polls):
            h.record(s)
        out.append({"name": f"node-{k + 1}", "sample": s, "stale": False, "hist": h})
    return out


def _build(views, w, h, layout="large"):
    ui = cluster.ClusterUI()
    return clusterpage.build_page3_large({"name": "lab", "views": views, "rate": 1.0, "ui": ui}, w, h, 0, layout)


class HistoryTest(unittest.TestCase):
    def test_fabric_nics_prefers_fast_ports(self):
        s = _sample()
        self.assertEqual([n["name"] for n in cluster.fabric_nics(s["nics"])], ["fab0"])
        self.assertEqual([n["name"] for n in cluster.fabric_nics(_sample(fabric=False)["nics"])], ["mgmt0"])
        self.assertEqual(cluster.fabric_nics([{"name": "x", "up": False, "speed_mbit": 200000}]), [])

    def test_node_power_sources(self):
        self.assertEqual(cluster.node_power(_sample()), (80.0, "wall"))
        self.assertEqual(cluster.node_power(_sample(rails=False, gpus=2)), (80.0, "gpu"))
        self.assertEqual(cluster.node_power({"gpus": [{"pwr_str": "N/A"}]}), (None, None))

    def test_history_point(self):
        p = cluster.history_point(_sample(util=30))
        self.assertEqual(p["cpu"], 30)
        self.assertEqual(p["mem"], 40.0)
        self.assertEqual((p["rx"], p["tx"]), (5e9, 3e9))
        self.assertEqual(p["pwr"], 80.0)
        self.assertEqual(p["gpu:0"], 30.0)
        self.assertEqual(p["temp"], 55)

    def test_failed_poll_keeps_time(self):
        h = cluster.NodeHistory(maxlen=5)
        h.record(_sample(util=20))
        h.record(None)
        h.record(_sample(util=30))
        self.assertEqual(h.series("gpu:0"), [20.0, None, 30.0])
        for _ in range(10):
            h.record(None)
        self.assertEqual(len(h.series("cpu")), 5)
        self.assertEqual(h.series("cpu", 2), [None, None])
        self.assertEqual(h.series("nothing"), [])

    def test_member_records_and_views_carry_history(self):
        m = cluster.ClusterMember({"host": "node-2", "kind": "http"}, is_local=False)
        m.set_ok(_sample())
        m.set_err("timeout")
        self.assertEqual(len(m.hist), 2)
        agg = cluster.ClusterAggregator([], None)
        agg.members = [m]
        view = agg.get_views()[0]
        self.assertIs(view["hist"], m.hist)
        agg.stop()

    def test_history_never_on_the_wire(self):
        wire = cluster.to_wire(_sample())
        self.assertNotIn("hist", wire)


class LayoutTest(unittest.TestCase):
    def setUp(self):
        self._style = term.BAR_STYLE
        term.BAR_STYLE = "eighths"

    def tearDown(self):
        term.BAR_STYLE = self._style

    def _assert_fills(self, built, h):
        self.assertIsNotNone(built)
        bottom = 0
        for p in built:
            if p.kind == "plain":
                continue
            bottom = max(bottom, p.y + p.total_height)      # its closing border
        self.assertEqual(bottom, h - 2)
        self.assertEqual(built[-1].y, h - 1)                 # footer on the last row

    def test_fills_the_terminal_exactly(self):
        for n, w, h, layout in ((2, 240, 67, "large"), (4, 240, 67, "large"), (3, 200, 45, "large"),
                                (8, 320, 90, "xl"), (6, 300, 80, "xl"), (1, 220, 50, "large"),
                                (4, 380, 75, "xl"), (12, 420, 110, "xl"), (16, 420, 110, "xl")):
            with self.subTest(n=n, w=w, h=h):
                self._assert_fills(_build(_views(n, gpus=2), w, h, layout), h)

    def test_columns_end_together(self):
        built = _build(_views(2, gpus=2), 240, 67)
        cols = [p for p in built if len(p.title or []) > 1 and p.title[1][0].startswith("node-")]
        self.assertEqual(len(cols), 2)
        self.assertEqual(len({p.total_height for p in cols}), 1)

    def test_column_rows_matches_drawing(self):
        views = _views(1, gpus=2)
        m = clusterpage.pages._cluster_metrics(views[0]["sample"])
        info = clusterpage.node_info(views[0], m)
        v = {"gpu": 5, "cpu": 3, "pwr": 2, "fab": 2, "mem": 2, "temp": 2, "procs": 3, "heat": 0}
        p = clusterpage._build_column(0, 0, 100, info, v, 300, False, [])
        self.assertEqual(len(p.rows), clusterpage.column_rows(info, v))

    def test_silent_node_keeps_a_column(self):
        views = _views(3)
        views[1]["stale"] = True
        built = _build(views, 240, 67)
        self._assert_fills(built, 67)
        titles = ["".join(t for t, _ in p.title or []) for p in built]
        self.assertTrue(any(t.startswith("node-2 ▲ unreachable") for t in titles))

    def test_short_history_falls_back_to_the_sample(self):
        views = _views(2, polls=1)
        m = clusterpage.pages._cluster_metrics(views[0]["sample"])
        info = clusterpage.node_info(views[0], m)
        self.assertEqual(len(info["gpus"][0]["hist"]), 60)
        self.assertEqual(len(info["rx_hist"]), 60)

    def test_falls_back_to_normal_page(self):
        self.assertIsNone(_build(_views(17), 420, 110, "xl"))   # fleet mode
        self.assertIsNone(_build([], 240, 67))
        self.assertIsNone(_build(_views(8), 200, 20))           # nothing fits
        term.BAR_STYLE = "ascii"
        self.assertIsNone(_build(_views(2), 240, 67))

    def test_heat_levels_add_fabric_and_cpu_rows(self):
        views = _views(3)
        infos = [clusterpage.node_info(v, clusterpage.pages._cluster_metrics(v["sample"])) for v in views]
        self.assertEqual(clusterpage.heat_levels(infos), [0, 1, 2, 3])
        labels = [lbl for lbl, _ in clusterpage.heat_rows(infos, 3)]
        self.assertEqual(labels[:4], ["node-1 gpu", "node-2 gpu", "node-3 gpu", "node-1 fab"])
        self.assertEqual(labels[-1], "node-3 cpu")
        fab = dict(clusterpage.heat_rows(infos, 2))["node-1 fab"]
        self.assertAlmostEqual(fab[-1], 5e9 / 200e9 * 100)

    def test_big_screen_opens_extras(self):
        built = _build(_views(4), 380, 75, "xl")
        text = " ".join(t for p in built for row in p.rows for seg in (row[0] if isinstance(row, tuple) else row)
                        for t in ([seg[1]] if len(seg) == 3 else [seg[0]]) if isinstance(t, str))
        self.assertIn("HOTTEST", text)
        self.assertTrue(any((p.title or [("", 0)])[0][0] == "HEAT" for p in built))

    def test_top_processes(self):
        s = _sample(gpus=2)
        s["gpus"][0]["procs"] = [{"pid": 1, "name": "a", "mem": 1 * G}, {"pid": 2, "name": "b", "mem": 5 * G}]
        s["gpus"][1]["procs"] = [{"pid": 1, "name": "a", "mem": 3 * G}, {"pid": 3, "name": "c", "mem": 2 * G}]
        top = clusterpage._top_procs(s, 3)
        self.assertEqual([(p["pid"], p["mem"]) for p in top], [(2, 5 * G), (1, 3 * G), (3, 2 * G)])

    def test_grid(self):
        self.assertEqual(clusterpage.grid_for(4, 240), 4)
        self.assertEqual(clusterpage.grid_for(8, 320), 4)
        self.assertEqual(clusterpage.grid_for(6, 400), 6)
        self.assertEqual(clusterpage.grid_for(4, 200), 2)       # 50-col columns are too narrow: 2 x 2
        self.assertEqual(clusterpage.grid_for(16, 420), 6)      # three rows: 6, 6, 4
        self.assertEqual(clusterpage.grid_for(12, 380), 6)

    def test_dials(self):
        views = _views(2)
        infos = [clusterpage.node_info(v, clusterpage.pages._cluster_metrics(v["sample"])) for v in views]
        labels = [d[3] for d in clusterpage.cluster_dials(infos, 3)]
        self.assertEqual(labels[:2], ["GPU AVG", "SLOWEST GPU"])
        self.assertIn("WALL POWER", labels)
        self.assertIn("FABRIC", labels)
        agents = [d for d in clusterpage.cluster_dials(infos, 3) if d[3] == "AGENTS UP"][0]
        self.assertEqual((agents[1], agents[2]), ("2/3", "1 silent"))

    def test_renders_on_a_screen(self):
        built = _build(_views(4), 240, 67)
        vc = term.VirtualCurses(size=(240, 67))
        panels.render(vc, built, {s: s for s in range(16)})
        text = "\n".join("".join(c for c, _ in row) for row in vc.grid)
        for word in ("CLUSTER", "TOPOLOGY", "FABRIC", "node-4"):
            self.assertIn(word, text)


if __name__ == "__main__":
    unittest.main()
