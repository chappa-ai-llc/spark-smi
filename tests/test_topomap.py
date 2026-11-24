"""Page 3's cluster map (spark_smi/topomap.py): links found from shared
networks, the link cache for nodes that stop answering, and the drawing's
shape at several sizes and node counts."""
import os
import sys
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from spark_smi import cluster, pages, panels, term, topomap  # noqa: E402

G = 1024 ** 3


def rail(port, ip, prefix=24, rx=0, tx=0, up=True):
    return {"port": port, "rdma_dev": "roce" + port, "netdev": "en" + port, "ip": ip,
            "prefix": prefix, "up": up, "speed_mbit": 200000, "rx_bps": rx, "tx_bps": tx,
            "cnp_sent": 0, "cnp_handled": 0}


def view(name, rails=(), stale=False, gpus=("NVIDIA GB10 (Unified)",)):
    sample = {
        "cpu": {"clusters": [{"label": "Cortex-X925", "cores": list(range(4)), "loads": [5] * 4,
                              "avg": 5.0, "ghz": 3.9, "history": [5.0] * 10}], "temp": 50},
        "mem": {"vm": SimpleNamespace(total=100 * G, used=40 * G, percent=40.0), "gpu_alloc": 0,
                "history": [40.0] * 10},
        "gpus": [{"id": i, "name": n, "util": 20, "temp": 50, "pwr_str": "30 W", "history": [20.0] * 10}
                 for i, n in enumerate(gpus)],
        "nics": [], "nic_pf": list(rails), "disks": [], "power_rails": [],
    }
    return {"name": name, "stale": stale, "sample": None if stale else sample}


def two_sparks(tx=0):
    a = view("node-1", [rail("p0", "10.0.0.1", tx=tx), rail("p1", "10.0.1.1")])
    b = view("node-2", [rail("p0", "10.0.0.2", rx=tx), rail("p1", "10.0.1.2")])
    return [a, b]


def pairs(views):
    return [(v, pages._cluster_metrics(v.get("sample"))) for v in views]


def text(row):
    return "".join(t for t, _ in row)


class GraphTest(unittest.TestCase):
    def test_direct_link_bundles_rails(self):
        links, buses = topomap.build_graph(two_sparks())
        self.assertEqual(list(links), [("node-1", "node-2")])
        self.assertEqual(len(links[("node-1", "node-2")]), 2)
        self.assertEqual(buses, {})

    def test_three_on_one_network_is_a_switch(self):
        vs = [view(f"n{i}", [rail("p0", f"172.16.0.{i + 10}", prefix=16)]) for i in range(3)]
        links, buses = topomap.build_graph(vs)
        self.assertEqual(links, {})
        self.assertEqual(list(buses), [("n0", "n1", "n2")])

    def test_prefix_decides_the_network(self):
        # /25 splits .1 and .200 apart; /24 would join them
        a = view("a", [rail("p0", "10.9.9.1", prefix=25)])
        b = view("b", [rail("p0", "10.9.9.200", prefix=25)])
        self.assertEqual(topomap.build_graph([a, b]), ({}, {}))
        self.assertEqual(topomap.net_key({"ip": "10.9.9.200"}), "10.9.9.0/24")   # no prefix: /24

    def test_rows_without_address_are_ignored(self):
        a = view("a", [rail("p0", None)])
        b = view("b", [rail("p0", None)])
        self.assertEqual(topomap.build_graph([a, b]), ({}, {}))

    def test_rates_follow_the_drawn_direction(self):
        links, _ = topomap.build_graph(two_sparks(tx=40e9))
        r = links[("node-1", "node-2")][0]
        self.assertEqual(topomap._rail_rates(r, "a")[:2], (40e9, 0.0))
        self.assertEqual(topomap._rail_rates(r, "b")[:2], (0.0, 40e9))

    def test_short_rate(self):
        self.assertEqual(topomap.short_rate(52.04e9), "52.0G")
        self.assertEqual(topomap.short_rate(830e6), "830M")
        self.assertEqual(topomap.short_rate(120e9), "120G")
        self.assertEqual(topomap.short_rate(0), "0")
        self.assertEqual(topomap.short_rate(None), "—")


class CacheTest(unittest.TestCase):
    def test_link_kept_while_a_member_is_unreachable(self):
        cache = {}
        vs = two_sparks()
        topomap._merge_cache(*topomap.build_graph(vs), vs, cache)
        vs[1] = view("node-2", stale=True)
        links, _ = topomap._merge_cache(*topomap.build_graph(vs), vs, cache)
        rails = links[("node-1", "node-2")]
        self.assertEqual(len(rails), 2)
        self.assertEqual(topomap._rail_rates(rails[0], "a")[3], "unknown")

    def test_link_dropped_when_both_answer_without_it(self):
        cache = {}
        vs = two_sparks()
        topomap._merge_cache(*topomap.build_graph(vs), vs, cache)
        vs = [view("node-1"), view("node-2")]
        links, _ = topomap._merge_cache(*topomap.build_graph(vs), vs, cache)
        self.assertEqual(links, {})
        self.assertEqual(cache["links"], {})


class RenderTest(unittest.TestCase):
    def setUp(self):
        p = mock.patch.object(term, "BAR_STYLE", "eighths")
        p.start()
        self.addCleanup(p.stop)

    def check_shape(self, out, width):
        self.assertIsNotNone(out)
        for row in out["rows"]:
            self.assertEqual(len(text(row)), width)

    def test_two_nodes_side_by_side_with_rail_rows(self):
        out = topomap.render(pairs(two_sparks(tx=52e9)), 116, 20, selected_name="node-1")
        self.check_shape(out, 116)
        body = "\n".join(text(r) for r in out["rows"])
        self.assertIn("node-1", body)
        self.assertIn("node-2", body)
        self.assertIn("▸52.0G ◂0", body)
        self.assertIn("idle", body)
        self.assertEqual((out["links"], out["rails"]), (1, 2))
        self.assertEqual(len(out["rows"]), 6)     # GPU, CPU, MEM, NET rows + 2 borders
        self.assertIn("NET", body)

    def test_extra_rows_drop_first_when_tight(self):
        out = topomap.render(pairs(two_sparks()), 116, 5)
        body = "\n".join(text(r) for r in out["rows"])
        self.assertEqual(len(out["rows"]), 5)
        self.assertNotIn("NET", body)
        self.assertIn("CPU", body)

    def test_selected_box_uses_the_accent(self):
        out = topomap.render(pairs(two_sparks()), 116, 20, selected_name="node-2")
        top = out["rows"][0]
        self.assertTrue(any("node-2" in t and s == 9 for t, s in top))
        self.assertIn((" node-1 ", 8), top)

    def test_ring_routes_the_closing_link_below(self):
        names = ["a", "b", "c", "d"]
        rails = {n: [] for n in names}
        for k, (i, j) in enumerate([(0, 1), (1, 2), (2, 3), (3, 0)]):
            rails[names[i]].append(rail("p0", f"10.{k}.0.1"))
            rails[names[j]].append(rail("p0", f"10.{k}.0.2"))
        vs = [view(n, rails[n]) for n in names]
        for width in (80, 118, 156):
            out = topomap.render(pairs(vs), width, 30)
            self.check_shape(out, width)
            self.assertEqual(out["links"], 4)
            self.assertGreater(len(out["rows"]), 5)   # at least one track under the boxes
            # no two lines leave boxes in the same column of one track row
            for row in out["rows"]:
                self.assertNotIn("││", text(row))

    def test_switch_draws_one_bus(self):
        vs = [view(f"gb-{i}", [rail("p0", f"172.16.0.{i + 10}", tx=10e9)]) for i in range(6)]
        out = topomap.render(pairs(vs), 156, 30)
        self.check_shape(out, 156)
        self.assertEqual(out["buses"], 1)
        self.assertIn("switch", text(out["rows"][-1]))

    def test_nothing_when_it_cannot_fit(self):
        self.assertIsNone(topomap.render(pairs(two_sparks()), 116, 3))
        self.assertIsNone(topomap.render(pairs(two_sparks()), 40, 20))
        self.assertIsNone(topomap.render(pairs(two_sparks()[:1]), 116, 20))

    def test_ascii_mode_draws_no_box_glyphs(self):
        with mock.patch.object(term, "BAR_STYLE", "ascii"):
            out = topomap.render(pairs(two_sparks(tx=52e9)), 116, 20)
        body = "".join(text(r) for r in out["rows"])
        self.assertFalse(set(body) & set("─│╭╮╰╯├┤┬┴┼━┃▸◂"))


class Page3Test(unittest.TestCase):
    def setUp(self):
        p = mock.patch.object(term, "BAR_STYLE", "eighths")
        p.start()
        self.addCleanup(p.stop)

    def build(self, cols, rows, views):
        tier = pages.tier_for_width(cols)
        w = pages.content_width(tier, cols)
        ctx = {"name": "lab", "views": views, "rate": 1, "ui": cluster.ClusterUI()}
        return pages.build_page3(ctx, tier, w, 0, height=rows, has_perf=True)

    def titles(self, built):
        return [p.title[0][0] for p in built if p.title]

    def test_map_opens_in_spare_height(self):
        built = self.build(122, 31, two_sparks())
        self.assertEqual(self.titles(built)[0], "TOPOLOGY")
        last = built[-1]
        self.assertLessEqual(last.y, 30)          # the footer stays on screen

    def test_short_terminal_keeps_todays_page(self):
        built = self.build(122, 22, two_sparks())
        self.assertNotIn("TOPOLOGY", self.titles(built))
        self.assertEqual(self.titles(built)[0], "CPU")

    def test_graphs_reach_the_right_edge(self):
        built = self.build(122, 22, two_sparks())
        grid = term.VirtualCurses(size=(122, 22))
        panels.render(grid, built, {s: s for s in range(16)})
        cpu_row = "".join(c for c, _ in grid.grid[2])
        # the history graph and its peak label end next to the right border
        self.assertTrue(cpu_row.rstrip().endswith("│"))
        self.assertIn("peak", cpu_row[-14:])


if __name__ == "__main__":
    unittest.main()
