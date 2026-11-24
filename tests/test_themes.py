"""Measured themes (D-02 item 3): every theme meets the contrast floors on
dark and light terminals; colour maths; background detection."""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from spark_smi import color, term  # noqa: E402

GROUNDS = {"dark": (term.DARK_BG, False), "light": (term.LIGHT_BG, True),
           "dracula-ish": ((40, 42, 54), False), "solarized-light-ish": ((253, 246, 227), True)}


def ratio(n, bg):
    return color.contrast(color.xterm_rgb(n), bg)


class FloorsTest(unittest.TestCase):
    def _check(self, name, bg, light):
        p = term.build_palette(name, bg, light)
        for s in term.TEXT_SLOTS:
            self.assertGreaterEqual(ratio(p[s], bg), term.TEXT_FLOOR, f"{name} slot {s} on {bg}")
        for s in term.RAMP_SLOTS:
            self.assertGreaterEqual(ratio(p[s], bg), term.MARK_FLOOR, f"{name} ramp {s} on {bg}")
        lo, hi = term.QUIET_BAND
        for s in term.QUIET_SLOTS:
            self.assertTrue(lo <= ratio(p[s], bg) <= hi, f"{name} slot {s} on {bg}: {ratio(p[s], bg):.2f}")
        steps = [ratio(p[s], bg) for s in term.RAMP_SLOTS]
        self.assertEqual(steps, sorted(steps), f"{name} ramp rises")
        self.assertTrue(all(16 <= p[s] <= 255 for s in p), f"{name} uses only 16-255")

    def test_every_theme_on_every_ground(self):
        for name in term.THEMES:
            if name == term.TERMINAL_THEME:
                continue
            for label, (bg, light) in GROUNDS.items():
                with self.subTest(theme=name, ground=label):
                    self._check(name, bg, light)

    def test_four_anchor_theme_is_enough(self):
        with mock.patch.dict(term.THEMES, {"test4": {9: 168, 1: 107, 5: 179, 4: 160}}):
            for label, (bg, light) in GROUNDS.items():
                with self.subTest(ground=label):
                    self._check("test4", bg, light)

    def test_frames_keep_the_old_look_where_it_already_fits(self):
        # nord's old frame colour (240) sits inside the quiet band on dark
        self.assertEqual(term.build_palette("nord")[10], 240)

    def test_terminal_theme_uses_the_terminals_colours(self):
        p = term.build_palette(term.TERMINAL_THEME)
        self.assertTrue(all(-1 <= v <= 15 for v in p.values()))
        self.assertEqual(set(p), set(term.build_palette("spark")))


class ColorTest(unittest.TestCase):
    def test_contrast_extremes(self):
        self.assertAlmostEqual(color.contrast((0, 0, 0), (255, 255, 255)), 21.0, places=1)
        self.assertAlmostEqual(color.contrast((90, 90, 90), (90, 90, 90)), 1.0)

    def test_wcag_reference_value(self):
        # #767676 on white is the classic 4.54:1 grey
        self.assertAlmostEqual(color.contrast((118, 118, 118), (255, 255, 255)), 4.54, places=2)

    def test_oklab_round_trip_and_mix_endpoints(self):
        for rgb in ((255, 0, 0), (12, 200, 99), (128, 128, 128)):
            back = color.from_oklab(color.to_oklab(rgb))
            self.assertTrue(all(abs(a - b) < 0.6 for a, b in zip(rgb, back)), (rgb, back))
        a, b = (10, 20, 30), (200, 100, 50)
        self.assertTrue(all(abs(x - y) < 0.6 for x, y in zip(color.mix(a, b, 0), a)))
        self.assertTrue(all(abs(x - y) < 0.6 for x, y in zip(color.mix(a, b, 1), b)))

    def test_nearest_256_finds_exact_palette_colours(self):
        for n in (16, 74, 110, 196, 231, 240):
            self.assertEqual(color.xterm_rgb(color.nearest_256(color.xterm_rgb(n))), color.xterm_rgb(n))

    def test_nearest_256_honours_floor_and_exclude(self):
        bg = (30, 30, 30)
        n = color.nearest_256((60, 60, 60), bg, 4.5)
        self.assertGreaterEqual(ratio(n, bg), 4.5)
        self.assertNotEqual(color.nearest_256(color.xterm_rgb(74), exclude={74}), 74)

    def test_to_floor_moves_only_lightness_as_far_as_needed(self):
        bg = (255, 255, 255)
        out = color.to_floor((135, 215, 255), bg, 4.5, darken=True)
        self.assertGreaterEqual(color.contrast(out, bg), 4.5)
        self.assertLess(color.contrast(out, bg), 4.8)
        self.assertGreater(out[2], out[0])  # still blue


class BackgroundTest(unittest.TestCase):
    def test_parse_osc11(self):
        self.assertEqual(term.parse_osc11("\033]11;rgb:1e1e/1e1e/1e1e\033\\"), (30, 30, 30))
        self.assertEqual(term.parse_osc11("\033]11;rgb:ff/ff/ff\a"), (255, 255, 255))
        self.assertEqual(term.parse_osc11("\033]11;rgb:f/0/8\a"), (255, 0, 136))
        self.assertIsNone(term.parse_osc11("junk"))
        self.assertIsNone(term.parse_osc11(""))

    def test_parse_colorfgbg(self):
        self.assertFalse(term.parse_colorfgbg("15;0"))
        self.assertTrue(term.parse_colorfgbg("0;15"))
        self.assertTrue(term.parse_colorfgbg("0;default;7"))
        self.assertFalse(term.parse_colorfgbg("7;8"))
        self.assertIsNone(term.parse_colorfgbg("abc"))
        self.assertIsNone(term.parse_colorfgbg(None))

    def test_is_light(self):
        self.assertTrue(term.is_light((253, 246, 227)))
        self.assertFalse(term.is_light((40, 42, 54)))

    def test_detect_order(self):
        self.assertEqual(term.detect_background("light"), (True, None, "setting"))
        with mock.patch.object(term, "query_osc11", return_value=(250, 250, 250)):
            self.assertEqual(term.detect_background("auto"), (True, (250, 250, 250), "terminal"))
        with mock.patch.object(term, "query_osc11", return_value=None), \
                mock.patch.dict(os.environ, {"COLORFGBG": "0;15"}):
            self.assertEqual(term.detect_background("auto"), (True, None, "COLORFGBG"))
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(term.detect_background("auto", ask=False), (False, None, "default"))

    def test_set_background_rebuilds_the_palette(self):
        try:
            term.set_theme("nord")
            dark = dict(term.PALETTE_256)
            term.set_background(True)
            self.assertNotEqual(dark, term.PALETTE_256)
            self.assertGreaterEqual(ratio(term.PALETTE_256[3], term.LIGHT_BG), term.TEXT_FLOOR)
        finally:
            term.set_background(False, source="default")
            term.set_theme("spark")


if __name__ == "__main__":
    unittest.main()
