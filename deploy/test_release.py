"""Tests for deploy/release.py: python3 -m unittest discover -s deploy"""
import hashlib
import base64
import email
import io
import json
import os
import tarfile
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


def metadata(version="1.2.0", extra="", ctype="text/markdown",
             body="# SPARK-SMI\n\nLong text.\n"):
    return ("Metadata-Version: 2.4\nName: spark-smi\n"
            f"Version: {version}\nSummary: A monitor\n"
            "License-Expression: MIT\nLicense-File: LICENSE\n"
            "Classifier: Programming Language :: Python :: 3\n"
            "Classifier: Topic :: System :: Monitoring\n"
            "Requires-Dist: psutil\n"
            "Project-URL: Homepage, https://example.com/spark-smi\n"
            f"Description-Content-Type: {ctype}\n{extra}\n{body}")


def make_dist(dist, version="1.2.0", wheel_version=None, meta=None):
    wv = wheel_version or version
    stem = f"spark_smi-{version}"
    meta = meta if meta is not None else metadata(wv)
    with zipfile.ZipFile(dist / f"{stem}-py3-none-any.whl", "w") as z:
        z.writestr("spark_smi/__init__.py", f'VERSION = "{wv}"\n')
        z.writestr(f"{stem}.dist-info/METADATA", meta)
    with tarfile.open(dist / f"{stem}.tar.gz", "w:gz") as t:
        for name, data in ((f"{stem}/PKG-INFO", meta.encode()),
                           (f"{stem}/spark_smi.egg-info/PKG-INFO", b"Name: other\n")):
            info = tarfile.TarInfo(name)
            info.size = len(data)
            t.addfile(info, io.BytesIO(data))


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


class Fields(unittest.TestCase):
    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.dist = Path(d.name)

    def files(self, **kw):
        make_dist(self.dist, **kw)
        return sorted(self.dist.iterdir())

    def test_wheel_and_sdist_fields(self):
        wheel, sdist = self.files()
        for path, ftype, pyv in ((wheel, "bdist_wheel", "py3"),
                                 (sdist, "sdist", "source")):
            with self.subTest(path.name):
                fields = release.upload_fields(path)
                meta = dict(fields)
                self.assertEqual(fields[:2], [(":action", "file_upload"),
                                              ("protocol_version", "1")])
                self.assertEqual((meta["filetype"], meta["pyversion"]), (ftype, pyv))
                self.assertEqual(meta["sha256_digest"],
                                 hashlib.sha256(path.read_bytes()).hexdigest())
                self.assertEqual(meta["name"], "spark-smi")
                self.assertEqual(meta["license_expression"], "MIT")
                self.assertEqual(meta["description"], "# SPARK-SMI\n\nLong text.\n")
                self.assertEqual([v for k, v in fields if k == "classifiers"],
                                 ["Programming Language :: Python :: 3",
                                  "Topic :: System :: Monitoring"])
                self.assertEqual([v for k, v in fields if k == "project_urls"],
                                 ["Homepage, https://example.com/spark-smi"])
                self.assertEqual([k for k, _ in fields].count("name"), 1)

    def test_unknown_field_stops_the_release(self):
        wheel, _ = self.files(meta=metadata(extra="X-Surprise: yes\n"))
        with self.assertRaises(release.ReleaseError):
            release.upload_fields(wheel)

    def test_folded_header_is_unfolded(self):
        wheel, _ = self.files(meta=metadata(extra="License: MIT\n        line two\n"))
        self.assertEqual(dict(release.upload_fields(wheel))["license"],
                         "MIT\nline two")

    def test_good_metadata_passes(self):
        release.check_metadata(self.files(), "1.2.0")

    def test_bad_metadata_fails(self):
        for kw in ({"meta": metadata(ctype="text/x-rst")},
                   {"meta": metadata(body="")},
                   {"meta": metadata(version="1.1.0")}):
            with self.subTest(kw), tempfile.TemporaryDirectory() as d:
                make_dist(Path(d), **kw)
                with self.assertRaises(release.ReleaseError):
                    release.check_metadata(sorted(Path(d).iterdir()), "1.2.0")


class Multipart(unittest.TestCase):
    def test_round_trips_through_a_mime_parser(self):
        content = bytes(range(256)) * 4
        body, ctype = release.multipart(
            [("name", "spark-smi"), ("classifiers", "A"), ("classifiers", "B"),
             ("summary", "ünïcode")], "f.whl", content)
        msg = email.message_from_bytes(
            f"Content-Type: {ctype}\r\n\r\n".encode() + body)
        parts = [(p.get_param("name", header="content-disposition"),
                  p.get_payload(decode=True)) for p in msg.get_payload()]
        self.assertEqual(parts[:4], [("name", b"spark-smi"), ("classifiers", b"A"),
                                     ("classifiers", b"B"),
                                     ("summary", "ünïcode".encode())])
        self.assertEqual(parts[4], ("content", content))
        self.assertEqual(msg.get_payload()[4].get_filename(), "f.whl")


class Upload(unittest.TestCase):
    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        make_dist(Path(d.name))
        self.wheel = next(Path(d.name).glob("*.whl"))

    def upload(self, *responses):
        opener = mock.Mock()
        opener.open.side_effect = list(responses)
        with mock.patch("urllib.request.build_opener", return_value=opener) as b:
            try:
                release.pypi_upload("pypi-tok", self.wheel, url="https://u/legacy/",
                                    tries=3, wait=0)
            finally:
                self.handlers = b.call_args[0]
                self.reqs = [c[0][0] for c in opener.open.call_args_list]

    @staticmethod
    def http(code):
        return urllib.error.HTTPError("https://u/legacy/", code, "x", {},
                                      io.BytesIO(b"why"))

    def test_request_shape(self):
        self.upload(io.BytesIO(b""))
        req = self.reqs[0]
        self.assertEqual((req.full_url, req.get_method()), ("https://u/legacy/", "POST"))
        self.assertEqual(req.get_header("Authorization"), "Basic " +
                         base64.b64encode(b"__token__:pypi-tok").decode())
        self.assertTrue(req.get_header("Content-type").startswith("multipart/form-data"))
        self.assertIn(self.wheel.read_bytes(), req.data)
        self.assertNotIn(b"pypi-tok", req.data)
        self.assertIn(release._NoRedirect, self.handlers)

    def test_server_error_is_retried(self):
        self.upload(self.http(503), urllib.error.URLError("reset"), io.BytesIO(b""))
        self.assertEqual(len(self.reqs), 3)

    def test_refusal_is_not_retried(self):
        with self.assertRaises(release.ReleaseError) as e:
            self.upload(self.http(403))
        self.assertEqual(len(self.reqs), 1)
        self.assertIn("HTTP 403: why", str(e.exception))

    def test_gives_up_after_the_last_try(self):
        with self.assertRaises(release.ReleaseError):
            self.upload(self.http(502), self.http(502), self.http(502))
        self.assertEqual(len(self.reqs), 3)

    def test_redirects_are_not_followed(self):
        self.assertIsNone(release._NoRedirect().redirect_request(
            None, None, 301, "Moved", {}, "https://elsewhere/"))


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
        calls, cmds, uploads = [], [], []

        def fake_run(*cmd, env=None, cwd=None):
            cmds.append(([str(c) for c in cmd], env))
            if "build" in cmd:
                make_dist(Path(cmd[cmd.index("--outdir") + 1]).absolute())

        def fake_upload(token, path):
            calls.append("upload")
            uploads.append((token, Path(path).name))

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
                mock.patch.object(release, "pypi_upload", side_effect=fake_upload), \
                mock.patch.object(release, "github_release", side_effect=fake_page), \
                mock.patch.object(release, "verify_pypi", side_effect=fake_verify), \
                mock.patch("shutil.copytree", side_effect=fake_copy):
            make_source(Path(d))
            try:
                release.release("1.2.0", dry_run=False)
            except release.ReleaseError as e:
                calls.append(f"failed: {e}")
        self.uploads = uploads
        return calls, cmds

    def test_upload_then_page_then_verify(self):
        calls, _ = self.real_run()
        self.assertEqual(calls, ["upload", "upload", "page", "verify"])

    def test_page_is_made_even_when_verify_fails(self):
        calls, _ = self.real_run(verify=release.ReleaseError("bad"))
        self.assertEqual(calls, ["upload", "upload", "page", "verify", "failed: bad"])

    def test_only_the_upload_sees_the_pypi_token(self):
        _, cmds = self.real_run()
        self.assertEqual(self.uploads, [
            ("secret-PYPI_TOKEN_FILE", "spark_smi-1.2.0-py3-none-any.whl"),
            ("secret-PYPI_TOKEN_FILE", "spark_smi-1.2.0.tar.gz")])
        self.assertFalse([c for c, _ in cmds if any("secret" in a for a in c)])
        self.assertFalse([c for c, env in cmds if env])
        self.assertFalse([c for c, _ in cmds if "twine" in c])


if __name__ == "__main__":
    unittest.main()
