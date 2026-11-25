"""Command line: --version, and nothing unknown is silently skipped (issue #15)."""
import io
import os
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from spark_smi import VERSION, app  # noqa: E402


def run_main(argv):
    out, err = io.StringIO(), io.StringIO()
    code = 0
    with mock.patch.object(sys, "argv", ["spark-smi"] + argv), redirect_stdout(out), redirect_stderr(err):
        try:
            app.main()
        except SystemExit as e:
            code = e.code
    return code, out.getvalue(), err.getvalue()


class ParseTest(unittest.TestCase):
    def test_known_options_leave_nothing_unknown(self):
        argv = ["-l", "-n", "2", "--page", "3", "--theme", "nord", "--json", "--serve", "8817",
                "--serve-verbose", "--node-name", "node-1", "--cluster", "a,b", "--background", "dark",
                "--shot", "p.svg", "--size", "240x67", "--warmup", "5", "--graphs", "large",
                "--history", "10m", "--ascii", "--themes", "-h", "-V"]
        self.assertIsNone(app._parse_args(argv)["unknown"])

    def test_first_unknown_is_kept(self):
        self.assertEqual(app._parse_args(["-l", "--cluser", "a,b"])["unknown"], "--cluser")
        self.assertEqual(app._parse_args(["two"])["unknown"], "two")
        self.assertEqual(app._parse_args(["-p", "2", "extra"])["unknown"], "extra")


class MainTest(unittest.TestCase):
    def test_version(self):
        for flag in ("-V", "--version"):
            code, out, _ = run_main([flag])
            self.assertIn(code, (0, None))
            self.assertEqual(out, f"spark-smi {VERSION}\n")

    def test_unknown_option_stops_with_one_line(self):
        code, out, err = run_main(["--cluser", "a,b"])
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertEqual(len(err.strip().splitlines()), 1)
        self.assertIn("unknown option '--cluser'", err)

    def test_stray_word_is_unexpected_argument(self):
        code, _, err = run_main(["overview"])
        self.assertEqual(code, 1)
        self.assertIn("unexpected argument 'overview'", err)

    def test_help_lists_version(self):
        self.assertIn("-V, --version", app.HELP_TEXT)


if __name__ == "__main__":
    unittest.main()
