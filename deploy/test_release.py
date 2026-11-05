"""Tests for deploy/release.py: python3 -m unittest discover -s deploy"""
import hashlib
import io
import json
import os
import tempfile
import unittest
import urllib.error
import zipfile
from pathlib import Path
from unittest import mock

import release

CHANGELOG = """# Changelog

## [1.2.0] — 2026-11-05

### Added

- Thing.

## [1.1.0] — 2026-01-01

- Older.

[1.1.0]: https://example.com/1.1.0
"""


def make_source(root, init="1.2.0", toml="1.2.0", badge="1.2.0"):
    (root / "spark_smi").mkdir()
    (root / "spark_smi" / "__init__.py").write_text(f'VERSION = "{init}"\n')
    (root / "pyproject.toml").write_text(f'[project]\nversion = "{toml}"\n')
    (root / "README.md").write_text(f"![V](https://x/badge/version-{badge}-blue)\n")
    (root / "CHANGELOG.md").write_text(CHANGELOG)


def make_dist(dist, version="1.2.0", wheel_version=None):
    wv = wheel_version or version
    stem = f"spark_smi-{version}"
    with zipfile.ZipFile(dist / f"{stem}-py3-none-any.whl", "w") as z:
        z.writestr("spark_smi/__init__.py", f'VERSION = "{wv}"\n')
        z.writestr(f"{stem}.dist-info/METADATA", f"Name: spark-smi\nVersion: {wv}\n")
    (dist / f"{stem}.tar.gz").write_bytes(b"sdist")


class Changelog(unittest.TestCase):
    def test_takes_only_its_own_section(self):
        body = release.changelog_section(CHANGELOG, "1.2.0")
        self.assertIn("- Thing.", body)
        self.assertNotIn("Older", body)

    def test_last_section_drops_link_references(self):
        self.assertEqual(release.changelog_section(CHANGELOG, "1.1.0"), "- Older.")

    def test_missing_entry_fails(self):
        with self.assertRaises(release.ReleaseError):
            release.changelog_section(CHANGELOG, "9.9.9")

    def test_version_dots_are_literal(self):
        with self.assertRaises(release.ReleaseError):
            release.changelog_section(CHANGELOG, "1x2x0")


class Source(unittest.TestCase):
    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.root = Path(d.name)

    def test_agreeing_versions_pass(self):
        make_source(self.root)
        self.assertIn("Thing", release.check_source(self.root, "1.2.0"))

    def test_each_disagreeing_literal_fails(self):
        for kw in ({"init": "1.1.0"}, {"toml": "1.1.0"}, {"badge": "1.1.0"}):
            with self.subTest(kw), tempfile.TemporaryDirectory() as d:
                make_source(Path(d), **kw)
                with self.assertRaises(release.ReleaseError):
                    release.check_source(Path(d), "1.2.0")

    def test_bad_version_string_fails(self):
        make_source(self.root)
        with self.assertRaises(release.ReleaseError):
            release.check_source(self.root, "v1.2.0")


class Tokens(unittest.TestCase):
    def test_unset_fails(self):
        with mock.patch.dict(os.environ, clear=True), \
                self.assertRaises(release.ReleaseError):
            release.read_token("PYPI_TOKEN_FILE")

    def test_missing_file_fails(self):
        with mock.patch.dict(os.environ, {"PYPI_TOKEN_FILE": "/nonexistent/t"}), \
                self.assertRaises(release.ReleaseError):
            release.read_token("PYPI_TOKEN_FILE")

    def test_empty_file_fails_and_good_file_is_stripped(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "t"
            p.write_text("\n")
            with mock.patch.dict(os.environ, {"X": str(p)}):
                with self.assertRaises(release.ReleaseError):
                    release.read_token("X")
                p.write_text("pypi-abc\n")
                self.assertEqual(release.read_token("X"), "pypi-abc")


class Dist(unittest.TestCase):
    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.dist = Path(d.name)

    def test_good_dist_passes(self):
        make_dist(self.dist)
        self.assertEqual(len(release.check_dist(self.dist, "1.2.0")), 2)

    def test_wheel_with_other_version_inside_fails(self):
        make_dist(self.dist, wheel_version="1.1.0")
        with self.assertRaises(release.ReleaseError):
            release.check_dist(self.dist, "1.2.0")

    def test_stray_file_set_fails(self):
        make_dist(self.dist)
        make_dist(self.dist, version="1.1.0")
        with self.assertRaises(release.ReleaseError):
            release.check_dist(self.dist, "1.2.0")


class VerifyPypi(unittest.TestCase):
    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.file = Path(d.name) / "a.whl"
        self.file.write_bytes(b"built")
        self.want = {"a.whl": hashlib.sha256(b"built").hexdigest()}

    def test_waits_for_the_index_to_catch_up(self):
        with mock.patch.object(release, "pypi_files", side_effect=[None, self.want]) as m:
            release.verify_pypi([self.file], "1.2.0", tries=3, wait=0)
        self.assertEqual(m.call_count, 2)

    def test_other_bytes_fail(self):
        with mock.patch.object(release, "pypi_files", return_value={"a.whl": "00"}), \
                self.assertRaises(release.ReleaseError):
            release.verify_pypi([self.file], "1.2.0", tries=2, wait=0)

    def test_network_errors_are_retried(self):
        side = [urllib.error.URLError("down"), self.want]
        with mock.patch.object(release, "pypi_files", side_effect=side):
            release.verify_pypi([self.file], "1.2.0", tries=2, wait=0)


class GithubRelease(unittest.TestCase):
    def test_request_shape(self):
        resp = io.BytesIO(b'{"html_url": "https://g/r"}')
        with mock.patch("urllib.request.urlopen", return_value=resp) as m:
            url = release.github_release("tok", "1.2.0", "notes", repo="o/r")
        self.assertEqual(url, "https://g/r")
        req = m.call_args[0][0]
        self.assertEqual(req.full_url, "https://api.github.com/repos/o/r/releases")
        self.assertEqual(req.get_header("Authorization"), "Bearer tok")
        self.assertEqual(json.loads(req.data), {
            "tag_name": "v1.2.0", "name": "SPARK-SMI 1.2.0",
            "body": "notes", "make_latest": "true"})


class ReleaseOrder(unittest.TestCase):
    def test_real_run_stops_when_already_on_pypi(self):
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(release, "REPO", Path(d)), \
                mock.patch.object(release, "pypi_files", return_value={"x": "y"}), \
                mock.patch.object(release, "read_token", return_value="t"), \
                mock.patch.object(release, "run") as run:
            make_source(Path(d))
            with self.assertRaises(release.ReleaseError):
                release.release("1.2.0", dry_run=False)
            run.assert_not_called()

    def test_real_run_needs_both_tokens_before_anything_runs(self):
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(release, "REPO", Path(d)), \
                mock.patch.dict(os.environ, clear=True), \
                mock.patch.object(release, "run") as run:
            make_source(Path(d))
            with self.assertRaises(release.ReleaseError):
                release.release("1.2.0", dry_run=False)
            run.assert_not_called()

    def real_run(self, verify=None):
        """A real run with no network or tools: returns the commands and calls."""
        calls, cmds = [], []

        def fake_run(*cmd, env=None, cwd=None):
            cmds.append(([str(c) for c in cmd], env))
            if "build" in cmd:
                make_dist(Path(cmd[cmd.index("--outdir") + 1]).absolute())
            if "upload" in cmd:
                calls.append("upload")

        def fake_page(*a):
            calls.append("page")
            return "https://g/r"

        def fake_verify(*a):
            calls.append("verify")
            if verify:
                raise verify

        def fake_copy(src, dst, ignore=None):
            Path(dst).mkdir()

        def fake_make_dist_dir(*cmd, **kw):
            if "--outdir" in cmd:
                Path(cmd[cmd.index("--outdir") + 1]).mkdir()
            fake_run(*cmd, **kw)

        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(release, "REPO", Path(d)), \
                mock.patch.object(release, "pypi_files", return_value=None), \
                mock.patch.object(release, "read_token",
                                  side_effect=lambda n: f"secret-{n}"), \
                mock.patch.object(release, "run", side_effect=fake_make_dist_dir), \
                mock.patch.object(release, "github_release", side_effect=fake_page), \
                mock.patch.object(release, "verify_pypi", side_effect=fake_verify), \
                mock.patch("shutil.copytree", side_effect=fake_copy):
            make_source(Path(d))
            try:
                release.release("1.2.0", dry_run=False)
            except release.ReleaseError as e:
                calls.append(f"failed: {e}")
        return calls, cmds

    def test_upload_then_page_then_verify(self):
        calls, _ = self.real_run()
        self.assertEqual(calls, ["upload", "page", "verify"])

    def test_page_is_made_even_when_verify_fails(self):
        calls, _ = self.real_run(verify=release.ReleaseError("bad"))
        self.assertEqual(calls, ["upload", "page", "verify", "failed: bad"])

    def test_only_the_upload_sees_the_pypi_token(self):
        _, cmds = self.real_run()
        with_token = [c for c, env in cmds
                      if env and env.get("TWINE_PASSWORD") == "secret-PYPI_TOKEN_FILE"]
        self.assertEqual(len(with_token), 1)
        self.assertIn("upload", with_token[0])
        self.assertFalse([c for c, env in cmds if not env and "upload" in c])
        self.assertFalse([c for c, _ in cmds if any("secret" in a for a in c)])


if __name__ == "__main__":
    unittest.main()
