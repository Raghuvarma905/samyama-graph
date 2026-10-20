#!/usr/bin/env python3
"""API-09 — the SDKs already published still work against this build.

docs/VERSIONING.md promises that the SDKs of the previous minor release work
against a server of the current one. This is the check that keeps the promise:
it installs the **published** Python and TypeScript SDKs -- from PyPI and npm,
exactly as a user has them -- and runs a suite through each against a server
built from this checkout.

    python3 sdk/compat/run.py --bin target/release/samyama

Which versions: the latest published patch of each of the two most recent
minor releases at or below the version of record (`Cargo.toml`). Two, not
one, because the version of record does not say which release this build will
become. Right after `1.12.0` ships, `dev` still reads `1.12.0` and is heading
for `1.13.0`, whose N-1 is `1.12`; a build that becomes `1.12.1` must still
serve `1.11`. Testing both covers either. Nothing here names a version: they
are read from the manifest and the package indexes on every run, so the check
moves forward with each release by itself.

A version that cannot be found is a failure, not a skip. A compatibility check
that passes because it found nothing to test is the failure mode of every
check of this kind.

Standard library only, so it runs before anything is installed.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import tomllib
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent

PYPI_PACKAGE = "samyama"
NPM_PACKAGE = "samyama-sdk"
MINORS_TESTED = 2

Version = tuple[int, int, int]


def parse(v: str) -> Version | None:
    """`X.Y.Z` as a tuple; `None` for anything else (pre-releases included)."""
    m = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)", v)
    return (int(m[1]), int(m[2]), int(m[3])) if m else None


def show(v: Version) -> str:
    return ".".join(map(str, v))


def version_of_record() -> Version:
    manifest = tomllib.loads((REPO / "Cargo.toml").read_text())
    raw = manifest["workspace"]["package"]["version"]
    v = parse(raw)
    if v is None:
        sys.exit(f"Cargo.toml: workspace version {raw!r} is not X.Y.Z")
    return v


def fetch_json(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=60) as r:
        return json.loads(r.read())


def pypi_versions() -> list[Version]:
    releases = fetch_json(f"https://pypi.org/pypi/{PYPI_PACKAGE}/json")["releases"]
    out = []
    for raw, files in releases.items():
        v = parse(raw)
        # A release whose every file is yanked is withdrawn, not published.
        if v and files and not all(f.get("yanked") for f in files):
            out.append(v)
    return out


def npm_versions() -> list[Version]:
    versions = fetch_json(f"https://registry.npmjs.org/{NPM_PACKAGE}")["versions"]
    return [v for raw, meta in versions.items()
            if (v := parse(raw)) and not meta.get("deprecated")]


def latest_of_recent_minors(published: list[Version], ceiling: Version, n: int) -> list[Version]:
    """The newest patch of each of the `n` newest minors at or below `ceiling`."""
    newest: dict[tuple[int, int], Version] = {}
    for v in published:
        if v[:2] <= ceiling[:2]:
            key = v[:2]
            newest[key] = max(newest.get(key, v), v)
    return [newest[k] for k in sorted(newest, reverse=True)[:n]]


def resolve() -> dict[str, list[Version]]:
    ceiling = version_of_record()
    chosen = {
        "python": latest_of_recent_minors(pypi_versions(), ceiling, MINORS_TESTED),
        "typescript": latest_of_recent_minors(npm_versions(), ceiling, MINORS_TESTED),
    }
    for sdk, versions in chosen.items():
        if len(versions) < MINORS_TESTED:
            sys.exit(f"{sdk}: found {len(versions)} published minor(s) at or below "
                     f"{show(ceiling)}, need {MINORS_TESTED}: {[show(v) for v in versions]}")
    return chosen


# ---------------------------------------------------------------------------
# A server per SDK version, on spare ports, in a throwaway directory
# ---------------------------------------------------------------------------

def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Server:
    def __init__(self, binary: Path, workdir: Path):
        data = workdir / "data"
        data.mkdir(parents=True)
        self.http_port = free_port()
        self.log = workdir / "samyama.log"
        self.proc = subprocess.Popen(
            [str(binary), "--host", "127.0.0.1", "--port", str(free_port()),
             "--http-port", str(self.http_port), "--data-path", str(data)],
            cwd=workdir, stdout=self.log.open("w"), stderr=subprocess.STDOUT)
        self.url = f"http://127.0.0.1:{self.http_port}"
        deadline = time.monotonic() + 60
        while True:
            if self.proc.poll() is not None:
                sys.exit(f"samyama exited with {self.proc.returncode}:\n{self.log.read_text()[-3000:]}")
            try:
                with urllib.request.urlopen(f"{self.url}/api/status", timeout=2) as r:
                    self.version = json.loads(r.read())["version"]
                return
            except (urllib.error.URLError, ConnectionError, TimeoutError):
                if time.monotonic() > deadline:
                    self.stop()
                    sys.exit(f"samyama did not answer on {self.url}:\n{self.log.read_text()[-3000:]}")
                time.sleep(0.2)

    def stop(self) -> None:
        self.proc.terminate()
        try:
            self.proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            self.proc.kill()


def run(cmd: list[str], **kw) -> int:
    print("  $", " ".join(cmd), flush=True)
    return subprocess.run(cmd, **kw).returncode


def check_python(version: Version, binary: Path, scratch: Path) -> bool:
    venv = scratch / "venv"
    if run([sys.executable, "-m", "venv", str(venv)]) != 0:
        return False
    py = venv / ("Scripts" if os.name == "nt" else "bin") / "python"
    if run([str(py), "-m", "pip", "install", "-q", f"{PYPI_PACKAGE}=={show(version)}", "pytest"]) != 0:
        return False
    server = Server(binary, scratch / "server")
    try:
        env = {**os.environ, "SAMYAMA_URL": server.url, "SAMYAMA_SERVER_VERSION": server.version,
               "SAMYAMA_SDK_VERSION": show(version)}
        # -p no:cacheprovider: nothing written into the repository.
        return run([str(py), "-m", "pytest", "-q", "-p", "no:cacheprovider",
                    str(HERE / "test_python_sdk.py")], env=env) == 0
    finally:
        server.stop()


def check_typescript(version: Version, binary: Path, scratch: Path) -> bool:
    project = scratch / "node"
    project.mkdir()
    (project / "package.json").write_text(json.dumps({"private": True, "type": "module"}))
    if run(["npm", "install", "--silent", "--no-audit", "--no-fund",
            f"{NPM_PACKAGE}@{show(version)}"], cwd=project) != 0:
        return False
    # The test imports the SDK by name, so it has to sit where Node resolves
    # `node_modules` from: next to the installed package, not in the repo.
    test = project / "test_typescript_sdk.mjs"
    shutil.copy(HERE / "test_typescript_sdk.mjs", test)
    server = Server(binary, scratch / "server")
    try:
        env = {**os.environ, "SAMYAMA_URL": server.url, "SAMYAMA_SERVER_VERSION": server.version,
               "SAMYAMA_SDK_VERSION": show(version)}
        return run(["node", "--test", str(test)], cwd=project, env=env) == 0
    finally:
        server.stop()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--bin", type=Path, default=REPO / "target" / "release" / "samyama",
                    help="server binary built from this checkout")
    ap.add_argument("--resolve-only", action="store_true",
                    help="print the versions that would be tested, and stop")
    args = ap.parse_args()

    chosen = resolve()
    print(f"version of record: {show(version_of_record())}")
    for sdk, versions in chosen.items():
        print(f"{sdk}: testing published {', '.join(show(v) for v in versions)}")
    if args.resolve_only:
        return 0
    if not args.bin.is_file():
        sys.exit(f"no server binary at {args.bin}; build it with "
                 "`cargo build --release --bin samyama` or pass --bin")

    results: list[tuple[str, str, bool]] = []
    for sdk, check in (("python", check_python), ("typescript", check_typescript)):
        for version in chosen[sdk]:
            print(f"\n== {sdk} SDK {show(version)} against this build", flush=True)
            with tempfile.TemporaryDirectory(prefix=f"compat-{sdk}-") as tmp:
                results.append((sdk, show(version), check(version, args.bin.resolve(), Path(tmp))))

    print("\nAPI-09 previous-SDK compatibility")
    for sdk, version, ok in results:
        print(f"  {'PASS' if ok else 'FAIL'}  {sdk} {version}")
    return 0 if all(ok for *_, ok in results) else 1


if __name__ == "__main__":
    sys.exit(main())
