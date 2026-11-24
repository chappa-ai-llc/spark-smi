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
