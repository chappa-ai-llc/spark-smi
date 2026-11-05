#!/usr/bin/env python3
"""Publish one spark-smi release to PyPI and GitHub.

The BUILD office runs this on a vX.Y.Z tag and tries each tag once. PyPI
versions are immutable, so every check that can fail runs before the upload;
a failure after it needs a new version, never a re-tag.

    deploy/release.py VERSION             the real release
    deploy/release.py VERSION --dry-run   everything up to the upload

Needs PYPI_TOKEN_FILE and GITHUB_TOKEN_FILE, each the path of a file holding
one token. Only the twine process sees the PyPI token. Standard library only:
the run's build tools go into a throwaway venv, pinned by hash.
"""
import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
LOCK = REPO / "deploy" / "release-requirements.txt"
PROJECT = "spark-smi"
GITHUB_REPO = os.environ.get("RELEASE_GITHUB_REPO", "chappa-ai-llc/spark-smi")
HTTP_TIMEOUT = 30
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


def pypi_files(version):
    """{filename: sha256} PyPI holds for version, or None if it has none."""
    url = f"https://pypi.org/pypi/{PROJECT}/{version}/json"
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
        run(py, "-m", "twine", "check", "--strict", *files)
        if dry_run:
            print(f"dry run ok: {', '.join(p.name for p in files)}")
            return

        env = dict(os.environ, TWINE_USERNAME="__token__",
                   TWINE_PASSWORD=pypi_token)
        run(py, "-m", "twine", "upload", "--non-interactive",
            "--disable-progress-bar", *files, env=env)
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
