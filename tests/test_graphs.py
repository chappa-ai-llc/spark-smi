"""Graded history graphs (D-02 item 2): term.sparkline_runs and the
pages helper that adds the peak label."""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from spark_smi import pages, term  # noqa: E402


def text(runs):
    return "".join(t for t, _ in runs)


class SparklineRunsTest(unittest.TestCase):
    def setUp(self):
        p = mock.patch.object(term, "BAR_STYLE", "eighths")
        p.start()
        self.addCleanup(p.stop)

    def test_width_is_exact(self):
        for w in (1, 4, 13, 20):
            self.assertEqual(len(text(term.sparkline_runs([0, 50, 100] * 20, w))), w)

    def test_fixed_scale_and_minimum_dot(self):
        runs = term.sparkline_runs([2, 2, 2, 2], 2)
        cells = text(runs)
        self.assertTrue(all(ord(c) - 0x2800 == 0x40 | 0x80 for c in cells))  # bottom dots only
        self.assertTrue(all(s == term.RAMP_SLOTS[0] for _, s in runs))

    def test_full_scale_is_brightest_and_full(self):
        runs = term.sparkline_runs([100, 100], 1)
        self.assertEqual(runs, [("⣿", term.RAMP_SLOTS[-1])])

    def test_zero_is_blank_track(self):
        self.assertEqual(term.sparkline_runs([0, 0, 0, 0], 2), [("⠀⠀", 7)])

    def test_runs_merge_same_slot(self):
        runs = term.sparkline_runs([100] * 8, 4)
        self.assertEqual(len(runs), 1)

    def test_ramp_slot_steps(self):
        self.assertEqual(term.ramp_slot(0), 11)
        self.assertEqual(term.ramp_slot(0.19), 11)
        self.assertEqual(term.ramp_slot(0.2), 12)
        self.assertEqual(term.ramp_slot(0.99), 15)
        self.assertEqual(term.ramp_slot(1.5), 15)

    def test_not_enough_history_or_glyphs(self):
        self.assertEqual(term.sparkline_runs([50], 5), [])
        with mock.patch.object(term, "BAR_STYLE", "blocks"):
            self.assertEqual(term.sparkline_runs([50, 60], 5), [])


class PeakLabelTest(unittest.TestCase):
    def setUp(self):
        p = mock.patch.object(term, "BAR_STYLE", "eighths")
        p.start()
        self.addCleanup(p.stop)

    def test_peak_of_samples_drawn(self):
        f = pages._Flow(40)
        hist = [99] * 20 + [10] * 40          # 20 cells draw the last 40; the 99s are older
        pages._add_graded_spark(f, hist, 30)
        out = "".join(t for _, t, _ in f.segs)
        self.assertTrue(out.endswith(" peak 10%"), out)

    def test_tight_row_drops_label_then_graph(self):
        f = pages._Flow(12)
        pages._add_graded_spark(f, [10, 20, 30], 30)
        out = "".join(t for _, t, _ in f.segs)
        self.assertNotIn("peak", out)
        self.assertEqual(len(out), 12)
        f = pages._Flow(4)
        pages._add_graded_spark(f, [10, 20, 30], 30)
        self.assertEqual(f.segs, [])


if __name__ == "__main__":
    unittest.main()


class MirrorChartTest(unittest.TestCase):
    def setUp(self):
        p = mock.patch.object(term, "BAR_STYLE", "eighths")
        p.start()
        self.addCleanup(p.stop)

    def rows(self, up, down, width, **kw):
        return ["".join(t for t, _ in r) for r in term.mirror_chart(up, down, width, **kw)]

    def test_shape_and_width(self):
        out = term.mirror_chart([1] * 10, [1] * 10, 7)
        self.assertEqual(len(out), 4)
        self.assertTrue(all(len("".join(t for t, _ in r)) == 7 for r in out))

    def test_up_rises_and_down_hangs_each_to_own_peak(self):
        r = self.rows([100, 100], [5, 5], 1)
        self.assertEqual(r, ["⣿", "⣿", "⣿", "⣿"])        # both halves full: own peaks
        r = self.rows([0, 0], [10, 10], 1, down_floor=40)  # 10 of a 40 floor: 2 of 8 dots
        self.assertEqual(r[2], "⠛")                        # top two dot rows, hanging
        self.assertEqual(r[3], "⠀")
        self.assertEqual(r[1], "⣀")                        # empty up half: the baseline

    def test_floor_keeps_noise_small(self):
        r = self.rows([1, 1], [0, 0], 1, up_floor=1000)
        self.assertEqual(r[1], "⣀")                        # any activity: bottom dots only
        self.assertEqual(r[0], "⠀")

    def test_rows_near_baseline_are_dimmer(self):
        out = term.mirror_chart([100, 100], [100, 100], 1)
        slots = [r[0][1] for r in out]
        self.assertGreater(slots[0], slots[1])
        self.assertGreater(slots[3], slots[2])

    def test_baseline_is_track_coloured_when_empty(self):
        out = term.mirror_chart([0, 0], [0, 0], 2)
        self.assertEqual(out[1], [("⣀⣀", 7)])

    def test_unavailable(self):
        self.assertIsNone(term.mirror_chart([1], [1, 2], 4))
        with mock.patch.object(term, "BAR_STYLE", "ascii"):
            self.assertIsNone(term.mirror_chart([1, 2], [1, 2], 4))


class MirrorLayoutTest(unittest.TestCase):
    def setUp(self):
        p = mock.patch.object(term, "BAR_STYLE", "eighths")
        p.start()
        self.addCleanup(p.stop)

    def state(self, hist=True):
        h = [1e9, 5e9, 2e9] if hist else []
        nic = {"name": "eth0", "hw_label": "Test NIC", "pf_count": 1, "up": True, "speed_mbit": 10000,
               "speed_str": "10G", "rx_bps": 2e9, "tx_bps": 1e9, "rx_pct": 20, "tx_pct": 10,
               "rx_history": h, "tx_history": h}
        disk = {"name": "nvme0", "model": "Disk", "size": 1e12, "read_bps": 1e8, "write_bps": 0,
                "used_pct": 40, "read_history": h, "write_history": h}
        return {"cpu": {"clusters": []}, "mem": {}, "gpus": [], "nics": [nic], "disks": [disk], "caps": {}}

    def net_rows(self, state, height):
        built = pages.build_page1(state, "wide", 130, 0, height=height)
        net = [p for p in built if p.title and p.title[0][0] == "NETWORK"][0]
        disk = [p for p in built if p.title and p.title[0][0] == "STORAGE"][0]
        return len(net.rows), len(disk.rows)

    def test_tall_terminal_mirrors_nics_then_disks(self):
        self.assertEqual(self.net_rows(self.state(), 60), (4, 4))

    def test_short_terminal_keeps_one_row(self):
        self.assertEqual(self.net_rows(self.state(), 12), (1, 1))   # estimate 11: one spare row
        self.assertEqual(self.net_rows(self.state(), 16), (4, 1))   # 5 spare: the NIC, not the disk

    def test_no_history_keeps_one_row(self):
        self.assertEqual(self.net_rows(self.state(hist=False), 60), (1, 1))

    def test_peak_avg_in_the_peaks_unit(self):
        self.assertEqual(pages._peak_avg([1e9, 3e9], "bit"), "peak 3.0 · avg 2.0 Gb/s")
        self.assertEqual(pages._peak_avg([1e6, 3e6], "bit"), "peak 3 · avg 2 Mb/s")
        self.assertEqual(pages._peak_avg([5e8, 0], "byte"), "peak 0.50 · avg 0.25 GB/s")
