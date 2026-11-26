"""Layout presets, page 3 views and the settings file (#14 step 5)."""
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from spark_smi import app, bigpage, cluster, clusterpage, term  # noqa: E402
from test_bigpage import render, sample  # noqa: E402
import test_clusterpage  # noqa: E402


def titles(built):
    out = []
    for p in built:
        out.append("".join(t for t, _ in (p.title or [])))
    return out


class Eighths(unittest.TestCase):
    def setUp(self):
        p = mock.patch.object(term, "BAR_STYLE", "eighths")
        p.start()
        self.addCleanup(p.stop)
        bigpage._FIT_CACHE.clear()


class DialPinsTest(Eighths):
    def test_ids(self):
        for ok in ("cpu", "memory", "gpu0.util", "gpu12.power", "system.power", "fabric", "hottest",
                   "disk.temp", "disk.used"):
            self.assertTrue(bigpage.dial_id_ok(ok), ok)
        for bad in ("CPU", "gpu.util", "gpu0", "gpu123.util", "disk", "", None, 3):
            self.assertFalse(bigpage.dial_id_ok(bad), bad)

    def test_pins_pick_and_order(self):
        s = sample()
        labels = [d[3] for d in bigpage._dials(s, s["long"], ["hottest", "gpu1.util", "cpu", "gpu7.util"])]
        # gpu7 has no sensor here: skipped like any missing sensor
        self.assertEqual(labels, ["HOTTEST", "GPU 1 UTIL", "CPU"])
        self.assertEqual(len(bigpage._dials(s, s["long"])), len(bigpage._dials(s, s["long"], None)))

    def test_pinned_strip_on_the_page(self):
        s = sample()
        rows = render(bigpage.build_page1_large(s, 240, 67, 0, "large", pins=["memory"]), 240, 67)
        text = "\n".join(rows)
        self.assertIn("MEMORY", text)
        self.assertNotIn("HOTTEST", text)


class StripTest(Eighths):
    def test_two_rows_split_evenly(self):
        dials = [(0.5, "50%", "x", f"D{i}", None) for i in range(10)]
        p = bigpage._build_dial_strip(1, 0, 240, dials, 20, [], max_rows=2)
        size = term.dial_size(20)
        self.assertEqual(len(p.rows), size * 2 + 1)
        labels = ["".join(t for _, t, _ in segs) for segs, _ in p.rows]
        self.assertEqual(sum(labels[size - 1].count(f"D{i}") for i in range(10)), 5)
        self.assertEqual(sum(labels[-1].count(f"D{i}") for i in range(10)), 5)

    def test_one_row_when_all_fit(self):
        dials = [(0.5, "50%", "x", f"D{i}", None) for i in range(3)]
        p = bigpage._build_dial_strip(1, 0, 240, dials, 20, [], max_rows=2)
        self.assertEqual(len(p.rows), term.dial_size(20))


class Page1PresetTest(Eighths):
    def fills(self, built, w, h):
        rows = render(built, w, h)
        self.assertEqual(len(rows), h)
        self.assertTrue(rows[-1].strip(), "footer on the last row")
        return rows

    def test_auto_has_the_strip(self):
        b = bigpage.build_page1_large(sample(), 240, 67, 0, "large")
        self.assertIn("AT A GLANCE", titles(b))

    def test_graphs_drops_it(self):
        b = bigpage.build_page1_large(sample(), 240, 67, 0, "large", preset="graphs")
        self.assertNotIn("AT A GLANCE", titles(b))
        self.fills(b, 240, 67)

    def test_dials_bigger_and_every_one(self):
        s = sample()
        b = bigpage.build_page1_large(s, 240, 67, 0, "large", preset="dials")
        strip = [p for p in b if "AT A GLANCE" in "".join(t for t, _ in (p.title or []))][0]
        self.assertEqual(len(strip.rows), term.dial_size(bigpage.BIG_DIAL_W["large"]) * 2 + 1)
        text = "\n".join(self.fills(b, 240, 67))
        for d in bigpage._dials(s, s["long"]):
            self.assertIn(d[3], text)
        self.assertIn("layout dials", text)

    def test_dials_on_the_one_column_layout(self):
        b = bigpage.build_page1_large(sample(), 160, 90, 0, "tall", preset="dials")
        self.assertIn("AT A GLANCE", titles(b))

    def test_dense_side_by_side(self):
        b = bigpage.build_page1_large(sample(), 240, 67, 0, "large", preset="dense")
        names = " ".join(titles(b))
        for t in ("CPU", "NETWORK", "THERMALS"):
            self.assertIn(t, names)
        xs = {p.x for p in b if p.kind == "top"}
        self.assertGreaterEqual(len(xs), 2)
        for p in b[1:-1]:
            self.assertLessEqual(p.y + p.total_height, 66, p.title)
        self.fills(b, 240, 67)

    def test_dense_drops_whole_panels_when_short(self):
        b = bigpage.build_page1_large(sample(), 240, 30, 0, "large", preset="dense")
        for p in b[1:-1]:
            self.assertLessEqual(p.y + p.total_height, 29)
        self.assertFalse(b[1].kind == "mid")

    def test_dense_keeps_the_normal_page_on_one_column(self):
        self.assertIsNone(bigpage.build_page1_large(sample(), 160, 60, 0, "tall", preset="dense"))


class Page3ViewTest(Eighths):
    def build(self, n=3, w=200, h=50, **kw):
        views = test_clusterpage._views(n)
        ctx = {"name": "lab", "views": views, "rate": 1.0, "ui": cluster.ClusterUI()}
        return clusterpage.build_page3_large(ctx, w, h, 0, "large", **kw)

    def test_map_view(self):
        b = self.build(view="map")
        names = " ".join(titles(b))
        self.assertIn("TOPOLOGY", names)
        self.assertIn("HEAT", names)
        self.assertEqual(len(render(b, 200, 50)), 50)
        self.assertTrue(render(b, 200, 50)[-1].strip())
        self.assertFalse(any(t.startswith("node-") for t in titles(b)), "no node columns")

    def test_heat_view_fills_with_thick_rows(self):
        b = self.build(view="heat")
        names = titles(b)
        self.assertNotIn("TOPOLOGY", " ".join(names))
        heat = [p for p in b if "HEAT" in "".join(t for t, _ in (p.title or []))][0]
        self.assertEqual(heat.y + heat.total_height, 48)   # border on 48, footer on 49
        labelled = [segs for segs, _ in heat.rows if segs and segs[0][1].strip()]
        self.assertGreater(len(heat.rows), len(labelled) + 1)

    def test_graphs_preset_drops_cluster_dials(self):
        self.assertIn("CLUSTER", titles(self.build()))
        self.assertNotIn("CLUSTER", titles(self.build(preset="graphs")))

    def test_dials_preset_bigger(self):
        b = self.build(w=240, h=67, preset="dials")
        strip = [p for p in b if titles([p]) == ["CLUSTER"]][0]
        self.assertEqual(len(strip.rows), term.dial_size(bigpage.BIG_DIAL_W["large"]))

    def test_views_without_heat_fall_back(self):
        # one GPU in the whole cluster: no heat grid, so map+graphs
        for view in ("heat", "map"):
            b = self.build(n=1, view=view)
            self.assertIsNotNone(b)
            self.assertFalse(any("HEAT" in t or "GPU UTIL" in t for t in titles(b)), view)
            self.assertEqual(titles(b), titles(self.build(n=1)))


class SettingsFileTest(unittest.TestCase):
    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        p = mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": d.name})
        p.start()
        self.addCleanup(p.stop)
        os.makedirs(os.path.dirname(app.settings_path()))

    def write(self, text):
        with open(app.settings_path(), "w") as f:
            f.write(text)

    def test_bad_json_is_reported_and_never_overwritten(self):
        self.write('{"preset": "dials",\n oops}')
        data, problem = app.read_settings()
        self.assertEqual(data, {})
        self.assertIn("line 2", problem)
        app.save_settings(graphs="large")
        with open(app.settings_path()) as f:
            self.assertIn("oops", f.read())

    def test_not_an_object(self):
        self.write("[1, 2]")
        self.assertIn("object", app.read_settings()[1])

    def test_missing_is_no_problem(self):
        os.rmdir(os.path.dirname(app.settings_path()))
        self.assertEqual(app.read_settings(), ({}, None))

    def test_values_checked(self):
        probs = app.settings_problems({"graphs": "huge", "history": 600, "preset": "dense",
                                       "view": "nope", "dials": ["cpu", "gpu"]})
        self.assertEqual(len(probs), 3)
        self.assertTrue(any("graphs" in p for p in probs))
        self.assertTrue(any("view" in p for p in probs))
        self.assertTrue(any("'gpu'" in p for p in probs))
        self.assertEqual(app.settings_problems({"dials": "cpu"}), ["dials must be a list"])
        self.assertEqual(app.settings_problems({}), [])

    def test_dial_pins_keep_the_good_names(self):
        self.assertEqual(app.dial_pins({"dials": ["cpu", "bogus", "fabric"]}), ["cpu", "fabric"])
        self.assertIsNone(app.dial_pins({"dials": ["bogus"]}))
        self.assertIsNone(app.dial_pins({}))

    def test_keys_merge_with_hand_edits(self):
        self.write(json.dumps({"dials": ["cpu"]}))
        app.save_settings(preset="graphs")
        self.assertEqual(app.load_settings(), {"dials": ["cpu"], "preset": "graphs"})


def run_main(argv):
    out, err = io.StringIO(), io.StringIO()
    code = 0
    with mock.patch.object(sys, "argv", ["spark-smi"] + argv), redirect_stdout(out), redirect_stderr(err):
        try:
            app.main()
        except SystemExit as e:
            code = e.code
    return code, out.getvalue(), err.getvalue()


class FlagsTest(unittest.TestCase):
    def test_parsed(self):
        o = app._parse_args(["--preset", "dense", "--view", "heat"])
        self.assertEqual((o["preset"], o["view"], o["unknown"]), ("dense", "heat", None))

    def test_bad_values_exit_1(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": d}):
            code, _, err = run_main(["--preset", "huge", "--json"])
            self.assertEqual(code, 1)
            self.assertIn("--preset", err)
            code, _, err = run_main(["--view", "huge", "--json"])
            self.assertEqual(code, 1)
            self.assertIn("--view", err)

    def test_bad_settings_file_warns_and_runs(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": d}):
            os.makedirs(os.path.join(d, "spark-smi"))
            with open(os.path.join(d, "spark-smi", "settings.json"), "w") as f:
                f.write("{broken")
            code, out, err = run_main(["--size", "120x40"])
            self.assertIn(code, (0, None))
            self.assertIn("not valid JSON", err)
            self.assertTrue(out.strip())
            with open(os.path.join(d, "spark-smi", "settings.json"), "w") as f:
                f.write('{"preset": "huge", "view": "nope"}')
            code, out, err = run_main(["--size", "120x40"])
            self.assertIn(code, (0, None))
            self.assertIn("preset 'huge' unknown (+1 more)", err)


if __name__ == "__main__":
    unittest.main()
