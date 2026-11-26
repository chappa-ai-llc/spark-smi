"""The `?` help overlay (#16): never a silently cut list. One column when
everything fits, two when the terminal is wide enough, else one column
that scrolls with the current page's keys first and "N more" markers."""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from spark_smi import pages, panels, term  # noqa: E402


def draw(w, h, page=1, scroll=0):
    scr = term.VirtualCurses(size=(w, h))
    built = pages.build_help_overlay(w, h, page, scroll)
    panels.render(scr, built, {i: i for i in range(32)})
    return ["".join(c for c, _ in row) for row in scr.grid]


def body_texts(page=1):
    return [t.strip() for t, _ in pages.help_lines(page) if t.strip()]


def visible(screen):
    return "\n".join(screen)


class HelpOrderTest(unittest.TestCase):
    def test_current_page_follows_global(self):
        for page in range(1, 6):
            heads = [t for t, slot in pages.help_lines(page) if slot == 9]
            self.assertEqual(heads[0], "Global")
            self.assertTrue(heads[1].startswith(f"Page {page} "), heads)
            self.assertEqual(len(heads), 6)

    def test_every_line_fits_a_column(self):
        for page in range(1, 6):
            for text, _ in pages.help_lines(page):
                self.assertLessEqual(len(text) + 1, pages.HELP_COL_W, text)


class HelpSmallTerminalTest(unittest.TestCase):
    """80x24, the issue's terminal."""

    def test_scrolls_and_says_so(self):
        scr = visible(draw(80, 24, page=3))
        self.assertIn("Page 3 -- cluster", scr)
        self.assertIn("more", scr)
        self.assertIn("PgUp/PgDn scroll", scr)
        self.assertGreater(pages.help_scroll_max(80, 24, 3), 0)

    def test_every_line_reachable_by_scrolling(self):
        w, h = 80, 24
        for page in (1, 5):
            seen = set()
            for s in range(pages.help_scroll_max(w, h, page) + 1):
                seen.update(line.strip(" │|") for line in draw(w, h, page, s))
            text = "\n".join(seen)
            for line in body_texts(page):
                self.assertIn(line, text, f"page {page}: {line!r} never shown")

    def test_markers_count_hidden_lines(self):
        w, h, page = 80, 24, 1
        n = len(pages.help_lines(page))
        top = pages.help_scroll_max(w, h, page)
        first = visible(draw(w, h, page, 0))
        self.assertNotIn("▲", first)
        self.assertIn("▼", first)
        last = visible(draw(w, h, page, top))
        self.assertIn(f"▲ {top} more", last)
        self.assertNotIn("▼", last)
        # Past the end clamps to the last screen.
        self.assertEqual(draw(w, h, page, top + 50), draw(w, h, page, top))
        # Top screen: rows shown + hidden == the whole body.
        start, end = pages._help_window(n, pages._help_geometry(w, h, n)[1], 0)
        self.assertIn(f"▼ {n - end} more", first)

    def test_fits_the_screen(self):
        for w, h in ((80, 24), (60, 12), (100, 30)):
            built = pages.build_help_overlay(w, h, 2, 0)
            p = built[0]
            self.assertLessEqual(p.y + len(p.rows) + 2, h, (w, h))

    def test_ascii_markers(self):
        with mock.patch.object(term, "BAR_STYLE", "ascii"):
            scr = visible(draw(80, 24, 1, 3))
        self.assertIn("^ 3 more", scr)
        self.assertIn("up/down PgUp/PgDn", scr)


class HelpLargeTerminalTest(unittest.TestCase):
    def test_tall_terminal_one_column_no_scroll(self):
        n = len(pages.help_lines(1))
        scr = visible(draw(80, n + 10))
        self.assertIn("press any key to close", scr)
        self.assertNotIn("more", scr)
        self.assertEqual(pages.help_scroll_max(80, n + 10, 1), 0)

    def test_wide_terminal_two_columns(self):
        scr = visible(draw(160, 45, page=4))
        self.assertIn("press any key to close", scr)
        self.assertEqual(pages.help_scroll_max(160, 45, 4), 0)
        for line in body_texts(4):
            self.assertIn(line, scr)

    def test_wide_but_short_scrolls(self):
        # Two columns would still cut the list: scroll instead.
        self.assertGreater(pages.help_scroll_max(160, 20, 1), 0)
        self.assertIn("PgUp/PgDn scroll", visible(draw(160, 20)))


if __name__ == "__main__":
    unittest.main()
