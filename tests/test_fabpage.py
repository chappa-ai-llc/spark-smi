"""Page 4's big-screen layout (#14) and the run history it reads."""
import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from spark_smi import cluster, fabpage, fabtest, panels, term  # noqa: E402

N = 300


def _hist(n=N, rx=50e9, tx=30e9):
    h = cluster.NodeHistory()
    for i in range(n):
        sample = {"nics": [{"name": "p0", "up": True, "speed_mbit": 400000,
                            "rx_bps": rx * (i % 10) / 10, "tx_bps": tx}]}
        h.record(sample)
    return h


def _pair(src="node-1", dst="node-2", ts=1764100000, rails=4, gbps=105.0):
    return {"ts": ts, "mode": "pair", "src": src, "dst": dst, "duration": 30, "lat_us": 2.31,
            "rails": [{"dev": f"r{i}", "gbps": gbps - i, "asic_start": 50, "asic_max": 58,
                       "series": [gbps - i] * 30} for i in range(rails)]}


def _burst(ts=1764100100):
    return {"ts": ts, "mode": "burst", "duration": 30, "aggregate_gbps": 1640.0,
            "pairs": [{"src": "node-1", "dst": "node-2"}, {"src": "node-2", "dst": "node-1"}]}


def _ctx(n_members=2, last=True, hist=True, stale=()):
    names = [f"node-{i + 1}" for i in range(n_members)]
    views = [{"name": n, "stale": n in stale, "hist": _hist() if hist else None} for n in names]
    matrix = {(s, d): {"gbps": 105.0, "lat_us": 2.3} for s in names for d in names if s != d}
    return {"name": "lab", "views": views, "rate": 1.0, "fab": None, "engine_status": None,
            "last_run": _pair() if last else None, "matrix": matrix}


def _text(built, w, h):
    vc = term.VirtualCurses(size=(w, h))
    panels.render(vc, built, {i: i for i in range(16)})
    return ["".join(c for c, _ in row) for row in vc.grid]


def _row_end(segs):
    x = 0
    for col, text, _ in segs:
        x = (col if col is not None else x) + len(text)
    return x


class FabPageTest(unittest.TestCase):
    def setUp(self):
        self._style = term.BAR_STYLE
        term.BAR_STYLE = "eighths"

    def tearDown(self):
        term.BAR_STYLE = self._style

    def _assert_fills(self, built, w, h):
        self.assertIsNotNone(built)
        self.assertEqual(built[-1].y, h - 1)
        for p in built[1:-1]:
            self.assertLessEqual(p.y + p.total_height, h - 2, p.title)
            self.assertLessEqual(p.x + p.w, w)
            for segs, right in p.rows:
                end = _row_end(segs) + sum(len(t) for t, _ in right)
                self.assertLessEqual(end, p.w - 2, (p.title, segs[:2]))
        self.assertEqual(max(p.y + p.total_height for p in built[1:-1]), h - 2)

    def test_fills_the_terminal(self):
        runs = [_pair(ts=1764100000 + i) for i in range(40)] + [_burst()]
        for w, h, layout in ((240, 67, "large"), (380, 75, "xl"), (420, 110, "xl"), (200, 45, "large")):
            for n in (2, 4, 8) if h > 45 else (2, 4):
                built = fabpage.build_page4_large(_ctx(n), w, h, 0, layout, runs=runs)
                self._assert_fills(built, w, h)

    def test_without_results_or_history(self):
        built = fabpage.build_page4_large(_ctx(last=False, hist=False), 380, 75, 0, "xl", runs=[])
        self._assert_fills(built, 380, 75)
        text = "\n".join(_text(built, 380, 75))
        self.assertIn("no bandwidth samples", text)
        self.assertIn("no fabric tests recorded yet", text)

    def test_panels(self):
        built = fabpage.build_page4_large(_ctx(stale=("node-2",)), 380, 75, 0, "xl", runs=[_pair(), _burst()])
        text = "\n".join(_text(built, 380, 75))
        for word in ("FABRIC TEST", "BANDWIDTH", "RAILS", "FABRIC NOW", "MATRIX", "RUN HISTORY", "silent"):
            self.assertIn(word, text)

    def test_chart_grows_with_height(self):
        def rows(h, runs):
            built = fabpage.build_page4_large(_ctx(), 380, h, 0, "xl", runs=runs)
            bw = next(p for p in built if p.title and p.title[0][0] == "BANDWIDTH")
            hist = next(p for p in built if p.title and p.title[0][0] == "RUN HISTORY")
            chart = sum(1 for segs, _ in bw.rows if segs and segs[0][1].rstrip()[-1:] in ("┤", "│"))
            shown = sum(1 for segs, _ in hist.rows if segs)
            return chart, shown
        many = [_pair()] * 60
        chart, shown = rows(48, many)
        self.assertEqual(shown, fabpage.HISTORY_MIN)                    # history keeps its minimum
        self.assertLess(chart, fabpage._MAX["chart"])
        self.assertEqual(rows(75, many)[0], fabpage._MAX["chart"])
        self.assertEqual(rows(110, many)[0], int(110 * fabpage.CHART_SHARE))
        chart, shown = rows(110, many[:12])         # rows the history cannot use go to the chart
        self.assertEqual(shown, 12)
        self.assertGreater(chart, int(110 * fabpage.CHART_SHARE))

    def test_run_rows(self):
        pair = "".join(t for t, _ in fabpage._run_row(_pair()))
        self.assertIn("node-1 → node-2", pair)
        self.assertIn("Σ 414.0 Gb/s", pair)
        self.assertIn("4 rails", pair)
        self.assertIn("lat 2.31 µs", pair)
        burst = "".join(t for t, _ in fabpage._run_row(_burst()))
        self.assertIn("burst 2 pairs", burst)
        self.assertIn("1640.0 Gb/s", burst)
        empty = "".join(t for t, _ in fabpage._run_row({"mode": "pair", "rails": [{"dev": "r0", "notes": "x"}]}))
        self.assertIn("no result", empty)

    def test_falls_back_to_normal_page(self):
        self.assertIsNone(fabpage.build_page4_large(_ctx(1), 380, 75, 0, "xl"))      # no peer
        self.assertIsNone(fabpage.build_page4_large(_ctx(), 380, 20, 0, "xl"))       # nothing fits
        self.assertIsNone(fabpage.build_page4_large(_ctx(), 100, 75, 0, "large"))    # too narrow
        term.BAR_STYLE = "ascii"
        self.assertIsNone(fabpage.build_page4_large(_ctx(), 380, 75, 0, "xl"))

    def test_toast_in_footer(self):
        built = fabpage.build_page4_large(_ctx(), 380, 75, 0, "xl", theme_toast=("history: 10m", 9))
        self.assertIn("history: 10m", built[-1].rows[0][0][0][0])


class RecentRunsTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "fabric-tests.jsonl")

    def tearDown(self):
        shutil.rmtree(self.dir)

    def test_newest_first_bad_lines_skipped(self):
        with open(self.path, "w") as f:
            for i in range(5):
                f.write(json.dumps(_pair(ts=i)) + "\n")
                f.write("{not json\n")
            f.write("[1, 2]\n\n")
        runs = fabtest.load_recent_runs(3, path=self.path)
        self.assertEqual([r["ts"] for r in runs], [4, 3, 2])
        self.assertEqual(len(fabtest.load_recent_runs(60, path=self.path)), 5)

    def test_missing_file(self):
        self.assertEqual(fabtest.load_recent_runs(5, path=os.path.join(self.dir, "none.jsonl")), [])


if __name__ == "__main__":
    unittest.main()
