"""cluster.read_token(): the --serve/--cluster shared secret (issue #8)."""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from spark_smi import cluster  # noqa: E402


class ReadTokenTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)

    def _file(self, text, name="token"):
        path = os.path.join(self.dir.name, name)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        return path

    def test_neither_set_means_no_token(self):
        self.assertEqual(cluster.read_token({}), (None, None))

    def test_empty_variables_count_as_unset(self):
        env = {"SPARK_SMI_TOKEN": "", "SPARK_SMI_TOKEN_FILE": ""}
        self.assertEqual(cluster.read_token(env), (None, None))

    def test_file_is_read_and_trailing_newline_stripped(self):
        env = {"SPARK_SMI_TOKEN_FILE": self._file("s3cret-value\n")}
        self.assertEqual(cluster.read_token(env), ("s3cret-value", "file"))

    def test_env_token_still_works(self):
        self.assertEqual(cluster.read_token({"SPARK_SMI_TOKEN": "abc"}), ("abc", "env"))

    def test_both_set_is_refused(self):
        env = {"SPARK_SMI_TOKEN": "abc", "SPARK_SMI_TOKEN_FILE": self._file("xyz")}
        with self.assertRaises(cluster.TokenError):
            cluster.read_token(env)

    def test_missing_file_fails_closed(self):
        env = {"SPARK_SMI_TOKEN_FILE": os.path.join(self.dir.name, "absent")}
        with self.assertRaisesRegex(cluster.TokenError, "cannot read"):
            cluster.read_token(env)

    def test_blank_file_fails_closed(self):
        env = {"SPARK_SMI_TOKEN_FILE": self._file(" \n\n")}
        with self.assertRaisesRegex(cluster.TokenError, "is empty"):
            cluster.read_token(env)

    def test_directory_fails_closed(self):
        with self.assertRaises(cluster.TokenError):
            cluster.read_token({"SPARK_SMI_TOKEN_FILE": self.dir.name})


if __name__ == "__main__":
    unittest.main()
