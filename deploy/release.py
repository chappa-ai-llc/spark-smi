#!/usr/bin/env python3
"""Publish one spark-smi release to PyPI and GitHub.

A release runner calls this on a vX.Y.Z tag and tries each tag once. PyPI
versions are immutable, so every check that can fail runs before the upload;
a failure after it needs a new version, never a re-tag.

    deploy/release.py VERSION             the real release
    deploy/release.py VERSION --dry-run   everything up to the upload

Needs PYPI_TOKEN_FILE and GITHUB_TOKEN_FILE, each the path of a file holding
one token. Standard library only. The build tools (build, setuptools, wheel,
all pure Python) go into a throwaway venv, pinned by hash. The upload itself is
done here, over PyPI's upload API, because the release runner has no temp
directory that may execute files, and twine's native dependencies cannot load
there (issue #6).

Optional settings, for a fork or a test index:
    RELEASE_GITHUB_REPO      owner/name of the GitHub repo (default chappa-ai-llc/spark-smi)
    RELEASE_PYPI_UPLOAD_URL  upload endpoint (default https://upload.pypi.org/legacy/)
    RELEASE_PYPI_JSON_URL    JSON API base (default https://pypi.org/pypi)
"""
import argparse
import base64
import email.parser
import email.policy
import hashlib
import json
import os
import re
import secrets
import shutil
import tarfile
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
LOCK = REPO / "deploy" / "requirements-release.txt"
PROJECT = "spark-smi"
GITHUB_REPO = os.environ.get("RELEASE_GITHUB_REPO", "chappa-ai-llc/spark-smi")
PYPI_UPLOAD_URL = os.environ.get("RELEASE_PYPI_UPLOAD_URL",
                                 "https://upload.pypi.org/legacy/")
PYPI_JSON_URL = os.environ.get("RELEASE_PYPI_JSON_URL",
                               "https://pypi.org/pypi").rstrip("/")
USER_AGENT = "spark-smi-release (+https://github.com/chappa-ai-llc/spark-smi)"
HTTP_TIMEOUT = 30
UPLOAD_TIMEOUT = 300
UPLOAD_TRIES = 3
UPLOAD_WAIT = 10

# Core metadata header -> upload form field, as twine sends them
# (twine/package.py, _RAW_TO_PACKAGE_METADATA). Every value of a multi-use
# header is sent as its own form field of the same name. A header outside
# these two maps stops the release before anything is uploaded.
SINGLE_FIELDS = {
    "metadata-version": "metadata_version", "name": "name",
    "version": "version", "summary": "summary", "keywords": "keywords",
    "home-page": "home_page", "download-url": "download_url",
    "author": "author", "author-email": "author_email",
    "maintainer": "maintainer", "maintainer-email": "maintainer_email",
    "license": "license", "license-expression": "license_expression",
    "requires-python": "requires_python",
    "description-content-type": "description_content_type",
    "description": "description",
}
MULTI_FIELDS = {
    "platform": "platform", "supported-platform": "supported_platform",
    "classifier": "classifiers", "requires": "requires",
    "provides": "provides", "obsoletes": "obsoletes",
    "requires-dist": "requires_dist", "provides-dist": "provides_dist",
    "obsoletes-dist": "obsoletes_dist",
    "requires-external": "requires_external", "project-url": "project_urls",
    "provides-extra": "provides_extra", "dynamic": "dynamic",
    "license-file": "license_file",
}
VERIFY_TRIES = 10
VERIFY_WAIT = 15


class ReleaseError(Exception):
    pass


def literal(path, pattern):
    """First group of pattern in a file, or a ReleaseError naming the file."""
    m = re.search(pattern, Path(path).read_text(encoding="utf-8"), re.M)
    if not m:
        raise ReleaseError(f"no version found in {path}")
    return m.group(1)


def changelog_section(text, version):
    """The body of the CHANGELOG entry for version, without its heading."""
    m = re.search(rf"^## \[{re.escape(version)}\][^\n]*\n(.*?)(?=^## |\Z)",
                  text, re.M | re.S)
    if not m:
        raise ReleaseError(f"CHANGELOG.md has no entry for {version}")
    lines = [l for l in m.group(1).splitlines()
             if not re.match(r"\[[^\]]+\]: ", l)]
    body = "\n".join(lines).strip()
    if not body:
        raise ReleaseError(f"CHANGELOG.md entry for {version} is empty")
    return body


def check_source(root, version):
    """The tag, both version literals and the README badge must all agree."""
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise ReleaseError(f"not a release version: {version!r}")
    found = {
        "spark_smi/__init__.py": literal(root / "spark_smi" / "__init__.py",
                                         r'^VERSION = "([^"]+)"'),
        "pyproject.toml": literal(root / "pyproject.toml",
                                  r'^version = "([^"]+)"'),
        "README.md badge": literal(root / "README.md",
                                   r"badge/version-([0-9.]+)-"),
    }
    wrong = {k: v for k, v in found.items() if v != version}
    if wrong:
        raise ReleaseError(f"version is not {version} in: {wrong}")
    return changelog_section((root / "CHANGELOG.md").read_text(encoding="utf-8"),
                             version)


def read_token(env_name):
    path = os.environ.get(env_name)
    if not path:
        raise ReleaseError(f"{env_name} is not set")
    try:
        token = Path(path).read_text(encoding="utf-8").strip()
    except OSError as e:
        raise ReleaseError(f"{env_name}: cannot read the file: {e.strerror}")
    if not token:
        raise ReleaseError(f"{env_name}: the file is empty")
    return token


def run(*cmd, env=None, cwd=None):
    print("+", " ".join(str(c) for c in cmd), flush=True)
    subprocess.run([str(c) for c in cmd], check=True, env=env, cwd=cwd)


def check_dist(dist, version):
    """Exactly one wheel and one sdist, and the wheel carries this version."""
    stem = f"{PROJECT.replace('-', '_')}-{version}"
    wheels = sorted(dist.glob("*.whl"))
    sdists = sorted(dist.glob("*.tar.gz"))
    if [p.name for p in wheels] != [f"{stem}-py3-none-any.whl"] or \
            [p.name for p in sdists] != [f"{stem}.tar.gz"]:
        raise ReleaseError(f"unexpected files in dist: "
                           f"{sorted(p.name for p in dist.iterdir())}")
    with zipfile.ZipFile(wheels[0]) as z:
        init = z.read("spark_smi/__init__.py").decode("utf-8")
        meta = z.read(f"{stem}.dist-info/METADATA").decode("utf-8")
    if f'VERSION = "{version}"' not in init:
        raise ReleaseError("the wheel's spark_smi/__init__.py has another VERSION")
    if f"\nVersion: {version}\n" not in meta:
        raise ReleaseError("the wheel's METADATA has another Version")
    return wheels + sdists


def dist_metadata(path):
    """The core metadata of a wheel (METADATA) or sdist (PKG-INFO)."""
    path = Path(path)
    if path.name.endswith(".whl"):
        with zipfile.ZipFile(path) as z:
            names = [n for n in z.namelist()
                     if re.fullmatch(r"[^/]+\.dist-info/METADATA", n)]
            raw = z.read(names[0]) if len(names) == 1 else None
    elif path.name.endswith(".tar.gz"):
        with tarfile.open(path) as t:
            names = [m for m in t.getmembers()
                     if re.fullmatch(r"[^/]+/PKG-INFO", m.name) and m.isfile()]
            raw = t.extractfile(names[0]).read() if len(names) == 1 else None
    else:
        raise ReleaseError(f"not a wheel or sdist: {path.name}")
    if raw is None:
        raise ReleaseError(f"{path.name}: no single metadata file")
    return email.parser.Parser(policy=email.policy.compat32).parsestr(
        raw.decode("utf-8"))


def upload_fields(path):
    """The form fields PyPI's upload API takes for one file, file excluded."""
    path = Path(path)
    msg = dist_metadata(path)
    fields = []
    for key, value in msg.items():
        k = key.lower()
        # Folded header lines (a long License text) come back as one value.
        value = re.sub(r"\r?\n[ \t]+", "\n", value).strip()
        if k in SINGLE_FIELDS:
            if any(f == SINGLE_FIELDS[k] for f, _ in fields):
                raise ReleaseError(f"{path.name}: {key} appears twice")
            fields.append((SINGLE_FIELDS[k], value))
        elif k in MULTI_FIELDS:
            fields.append((MULTI_FIELDS[k], value))
        else:
            raise ReleaseError(f"{path.name}: unknown metadata field {key!r}")
    body = msg.get_payload()
    if body and body.strip():
        if any(f == "description" for f, _ in fields):
            raise ReleaseError(f"{path.name}: description in header and body")
        fields.append(("description", body))
    meta = dict(fields)
    for need in ("metadata_version", "name", "version"):
        if need not in meta:
            raise ReleaseError(f"{path.name}: metadata has no {need}")
    if path.name.endswith(".whl"):
        filetype, pyversion = "bdist_wheel", path.name[:-4].split("-")[-3]
    else:
        filetype, pyversion = "sdist", "source"
    fields = [(f, v) for f, v in fields if f != "name"]
    fields[:0] = [
        (":action", "file_upload"), ("protocol_version", "1"),
        ("name", re.sub(r"[^A-Za-z0-9.]+", "-", meta["name"])),
        ("filetype", filetype), ("pyversion", pyversion),
        ("sha256_digest", hashlib.sha256(path.read_bytes()).hexdigest()),
    ]
    return fields


def check_metadata(files, version):
    """What PyPI needs to accept and render the files, checked before upload.

    Stands in for `twine check --strict`: the same name and version in every
    file, a summary, and a Markdown long description PyPI renders as such.
    """
    for path in files:
        meta = dict(upload_fields(path))
        found = (meta["name"].lower(), meta["version"])
        if found != (PROJECT, version):
            raise ReleaseError(f"{path.name}: metadata says {found}")
        if not meta.get("summary"):
            raise ReleaseError(f"{path.name}: metadata has no summary")
        if not meta.get("description", "").strip():
            raise ReleaseError(f"{path.name}: metadata has no long description")
        ctype = meta.get("description_content_type", "")
        if not ctype.startswith("text/markdown"):
            raise ReleaseError(f"{path.name}: long description type is "
                               f"{ctype or 'unset'}, not text/markdown")


def multipart(fields, filename, content):
    """A multipart/form-data body: the fields, then the file as `content`."""
    while True:
        boundary = secrets.token_hex(16)
        if boundary.encode() not in content and \
                not any(boundary in v for _, v in fields):
            break
    parts = []
    for name, value in fields:
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; '
                     f'name="{name}"\r\n\r\n'.encode("utf-8")
                     + value.encode("utf-8") + b"\r\n")
    parts.append(f'--{boundary}\r\nContent-Disposition: form-data; '
                 f'name="content"; filename="{filename}"\r\n'
                 f'Content-Type: application/octet-stream\r\n\r\n'
                 .encode("utf-8") + content + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode("utf-8"))
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A redirected upload fails instead of being resent as a GET elsewhere."""

    def redirect_request(self, *args, **kwargs):
        return None


def pypi_upload(token, path, url=None, tries=UPLOAD_TRIES, wait=UPLOAD_WAIT):
    """Upload one file. A server error is retried: PyPI answers 200 to a
    second upload of the very same bytes, so a retry cannot double-publish."""
    path = Path(path)
    body, ctype = multipart(upload_fields(path), path.name, path.read_bytes())
    auth = base64.b64encode(f"__token__:{token}".encode("utf-8")).decode("ascii")
    opener = urllib.request.build_opener(_NoRedirect)
    for attempt in range(1, tries + 1):
        req = urllib.request.Request(
            url or PYPI_UPLOAD_URL, data=body, method="POST",
            headers={"Authorization": f"Basic {auth}", "Content-Type": ctype,
                     "User-Agent": USER_AGENT})
        print(f"uploading {path.name} (try {attempt} of {tries})", flush=True)
        try:
            with opener.open(req, timeout=UPLOAD_TIMEOUT):
                return
        except urllib.error.HTTPError as e:
            detail = f"HTTP {e.code}: {e.read().decode('utf-8', 'replace')[:300]}"
            if e.code < 500:
                raise ReleaseError(f"PyPI refused {path.name}: {detail}")
        except (urllib.error.URLError, OSError) as e:
            detail = str(e)
        print(f"upload failed: {detail}", flush=True)
        if attempt < tries:
            time.sleep(wait)
    raise ReleaseError(f"PyPI upload of {path.name} failed: {detail}")


def pypi_files(version):
    """{filename: sha256} PyPI holds for version, or None if it has none."""
    url = f"{PYPI_JSON_URL}/{PROJECT}/{version}/json"
    try:
        with urllib.request.urlopen(url, timeout=HTTP_TIMEOUT) as r:
            data = json.load(r)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise
    return {u["filename"]: u["digests"]["sha256"] for u in data["urls"]}


def verify_pypi(files, version, tries=VERIFY_TRIES, wait=VERIFY_WAIT):
    """PyPI must hold exactly the bytes we built. Its index lags the upload."""
    want = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in files}
    for attempt in range(tries):
        try:
            got = pypi_files(version)
        except (urllib.error.URLError, OSError) as e:
            print(f"PyPI check failed: {e}", flush=True)
            got = None
        if got == want:
            return
        if attempt < tries - 1:
            time.sleep(wait)
    raise ReleaseError(f"PyPI does not show the built files: want {want}, got {got}")


def github_release(token, version, body, repo=GITHUB_REPO):
    """Publish the GitHub release for the existing tag and mark it Latest."""
    payload = {"tag_name": f"v{version}", "name": f"SPARK-SMI {version}",
               "body": body, "make_latest": "true"}
    req = urllib.request.Request(
        f"https://api.github.com/repos/{repo}/releases",
        data=json.dumps(payload).encode("utf-8"), method="POST",
        headers={"Authorization": f"Bearer {token}",
                 "Accept": "application/vnd.github+json",
                 "X-GitHub-Api-Version": "2022-11-28",
                 "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r:
            return json.load(r)["html_url"]
    except urllib.error.HTTPError as e:
        raise ReleaseError(f"GitHub release failed: HTTP {e.code}: "
                           f"{e.read().decode('utf-8', 'replace')[:300]}")


def release(version, dry_run):
    notes = check_source(REPO, version)
    pypi_token = github_token = None
    if not dry_run:
        pypi_token = read_token("PYPI_TOKEN_FILE")
        github_token = read_token("GITHUB_TOKEN_FILE")
    if pypi_files(version) is not None:
        if not dry_run:
            raise ReleaseError(f"{PROJECT} {version} is already on PyPI")
        print(f"note: {version} is already on PyPI; a real run would stop here")

    with tempfile.TemporaryDirectory(prefix="spark-smi-release-") as tmp:
        tmp = Path(tmp)
        src, dist, venv = tmp / "src", tmp / "dist", tmp / "venv"
        # A clean copy, so a leftover build/ can never reach the package.
        shutil.copytree(REPO, src, ignore=shutil.ignore_patterns(
            ".git", "scratchpad", "build", "dist", "*.egg-info", "__pycache__"))

        run(sys.executable, "-m", "venv", venv)
        py = venv / "bin" / "python"
        run(py, "-m", "pip", "install", "--quiet", "--require-hashes",
            "--only-binary", ":all:", "-r", LOCK)
        run(py, "-m", "build", "--no-isolation", "--outdir", dist, src)
        files = check_dist(dist, version)
        check_metadata(files, version)
        if dry_run:
            print(f"dry run ok: {', '.join(p.name for p in files)}")
            return

        for path in files:
            pypi_upload(pypi_token, path)
        # The upload cannot be undone, so the release page comes next, whatever
        # the check below finds. A bad check still fails the run and alerts.
        print("released:", github_release(github_token, version, notes), flush=True)
        verify_pypi(files, version)
        print(f"PyPI holds the built files for {PROJECT} {version}")


def main():
    ap = argparse.ArgumentParser(description="Publish a spark-smi release.")
    ap.add_argument("version", help="X.Y.Z, without the leading v")
    ap.add_argument("--dry-run", action="store_true",
                    help="stop before the upload; needs no tokens")
    args = ap.parse_args()
    try:
        release(args.version, args.dry_run)
    except (ReleaseError, subprocess.CalledProcessError) as e:
        print(f"release failed: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
