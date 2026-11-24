"""spark_smi.shot (--shot and the live `S` key) and app.parse_size."""
import os
import sys
import unittest
import xml.etree.ElementTree as ET

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from spark_smi import app, shot, term  # noqa: E402

NS = "{http://www.w3.org/2000/svg}"


def grid_of(lines, slot=3):
    v = term.VirtualCurses(size=(max(len(x) for x in lines) + 4, len(lines) + 3))
    for y, text in enumerate(lines):
        v.addstr(y, 0, text, slot)
    return v.grid


def parse(svg):
    return ET.fromstring(svg)


class Xterm256Test(unittest.TestCase):
    def test_cube_grey_and_base(self):
        self.assertEqual(shot.xterm256(16), "#000000")
        self.assertEqual(shot.xterm256(231), "#ffffff")
        self.assertEqual(shot.xterm256(110), "#87afd7")
        self.assertEqual(shot.xterm256(232), "#080808")
        self.assertEqual(shot.xterm256(255), "#eeeeee")
        self.assertEqual(shot.xterm256(1), "#cd0000")


class SvgTest(unittest.TestCase):
    def test_trimmed_to_content_and_valid(self):
        root = parse(shot.grid_to_svg(grid_of(["abc", "de"])))
        self.assertEqual(root.get("width"), str(shot.PAD * 2 + 3 * shot.CELL_W))
        self.assertEqual(root.get("height"), str(shot.PAD * 2 + 2 * shot.LINE_H))

    def test_text_is_selectable_and_escaped(self):
        root = parse(shot.grid_to_svg(grid_of(["a<b & c"])))
        texts = [t.text for t in root.iter(NS + "text")]
        self.assertEqual(texts, ["a<b & c"])

    def test_slot_colours_and_bold(self):
        v = term.VirtualCurses(size=(20, 3))
        v.addstr(0, 0, "x", 4)
        v.addstr(0, 2, "y", 9)
        root = parse(shot.grid_to_svg(v.grid, colors={4: "#ff0000", 9: "#00ff00"}))
        t = {e.text: e for e in root.iter(NS + "text")}
        self.assertEqual(t["x"].get("fill"), "#ff0000")
        self.assertIsNone(t["x"].get("font-weight"))
        self.assertEqual(t["y"].get("font-weight"), "bold")

    def test_bar_cells_merge_into_one_rect(self):
        root = parse(shot.grid_to_svg(grid_of(["████████"])))
        rects = [r for r in root.iter(NS + "rect")][1:]  # [0] is the background
        self.assertEqual(len(rects), 1)
        self.assertEqual(float(rects[0].get("width")), 8 * shot.CELL_W)

    def test_frame_line_is_one_path(self):
        root = parse(shot.grid_to_svg(grid_of(["╭──────╮", "│      │", "╰──────╯"])))
        self.assertEqual(len(list(root.iter(NS + "text"))), 0)
        paths = [p.get("d") for p in root.iter(NS + "path")]
        self.assertTrue(any(d.count("H") == 2 for d in paths))  # top and bottom runs, merged

    def test_braille_dots_are_drawn_not_typed(self):
        root = parse(shot.grid_to_svg(grid_of(["⣿⠁⠀"])))
        self.assertEqual(len(list(root.iter(NS + "text"))), 0)
        d = [p.get("d") for p in root.iter(NS + "path")][0]
        self.assertEqual(d.count("h0"), 9)  # 8 dots + 1 dot, blank cell adds none

    def test_tree_markers_are_drawn_not_typed(self):
        # page 5: picture fonts may lack these, so they are shapes
        root = parse(shot.grid_to_svg(grid_of(["▾▶◂▸"])))
        self.assertEqual(len(list(root.iter(NS + "text"))), 0)

    def test_empty_grid(self):
        root = parse(shot.grid_to_svg(term.VirtualCurses(size=(40, 8)).grid))
        self.assertEqual(root.get("width"), str(shot.PAD * 2))

    def test_live_palette_used_by_default(self):
        term.set_theme("nord")
        try:
            self.assertEqual(shot.slot_colors()[9], shot.xterm256(term.THEMES["nord"][9]))
        finally:
            term.set_theme("spark")


class ParseSizeTest(unittest.TestCase):
    def test_forms(self):
        self.assertEqual(app.parse_size("140"), (140, None))
        self.assertEqual(app.parse_size("140x45"), (140, 45))
        self.assertEqual(app.parse_size("140X45"), (140, 45))

    def test_rejects(self):
        for bad in ("", "x", "140x", "abc", "39", "401", "140x7", "140x45x2", "-5"):
            with self.assertRaises(ValueError, msg=bad):
                app.parse_size(bad)


class VirtualCursesSizeTest(unittest.TestCase):
    def test_cols_without_rows_keeps_terminal_height(self):
        # --size 120 without --shot: parse_size gives (120, None) (#12).
        from unittest import mock
        with mock.patch.object(term.shutil, "get_terminal_size", return_value=os.terminal_size((80, 33))):
            v = term.VirtualCurses(size=app.parse_size("120"))
            self.assertEqual(v.getmaxyx(), (33, 120))
            self.assertEqual(len(v.grid), 33)
            self.assertEqual(term.VirtualCurses(size=(120, 40)).getmaxyx(), (40, 120))
            self.assertEqual(term.VirtualCurses().getmaxyx(), (33, 80))


class TeeScreenTest(unittest.TestCase):
    def test_draws_to_both_with_slots(self):
        class Live:
            def __init__(self):
                self.calls = []

            def getmaxyx(self):
                return (10, 50)

            def erase(self):
                self.calls.append("erase")

            def addstr(self, y, x, text, attr=0):
                self.calls.append((y, x, text, attr))

        live, virt = Live(), term.VirtualCurses(size=(50, 10))
        tee = app._TeeScreen(live, virt, {3: 0x300, 9: 0x900})
        tee.erase()
        tee.addstr(1, 2, "hi", 0x900)
        self.assertEqual(live.calls, ["erase", (1, 2, "hi", 0x900)])
        self.assertEqual(virt.grid[1][2], ("h", 9))
        self.assertEqual(tee.getmaxyx(), (10, 50))


if __name__ == "__main__":
    unittest.main()
