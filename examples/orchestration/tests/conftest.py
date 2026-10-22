"""A real Samyama server for each test, on spare ports, in a throwaway directory.

The pipelines are tested against the engine, not a mock of it: what INT-10
asks for is an integration that has been run, and the defects worth catching
here -- a statement the engine refuses, a MATCH that silently finds nothing, a
parameter that arrives as the wrong type -- are the ones a mock would agree
with.

The binary is `$SAMYAMA_BIN`, or the repository's release build if that exists.
Neither present is an error, not a skip: a suite that skips when its server is
missing reports green while testing nothing.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import pytest

HERE = Path(__file__).resolve().parent
PIPELINE_DIR = HERE.parent
REPO_ROOT = PIPELINE_DIR.parent.parent

sys.path.insert(0, str(PIPELINE_DIR))

STARTUP_SECONDS = float(os.environ.get("SAMYAMA_STARTUP_SECONDS", "60"))


def samyama_bin() -> Path:
    configured = os.environ.get("SAMYAMA_BIN")
    path = Path(configured) if configured else REPO_ROOT / "target" / "release" / "samyama"
    if not path.is_file() or not os.access(path, os.X_OK):
        pytest.fail(
            f"no Samyama server binary at {path}. Build one with "
            "`cargo build --release --bin samyama`, or set SAMYAMA_BIN.",
            pytrace=False,
        )
    return path


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@dataclass
class Engine:
    url: str
    token: Optional[str]
    log: Path

    def query(self, cypher: str, params: Optional[dict] = None) -> list:
        body = {"query": cypher, "params": params or {}}
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        req = urllib.request.Request(f"{self.url}/api/query", json.dumps(body).encode(), headers)
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read())["records"]
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"HTTP {e.code}: {e.read().decode('utf-8', 'replace')}") from None

    def count(self, cypher: str, params: Optional[dict] = None) -> int:
        return self.query(cypher, params)[0][0]


def _start(tmp_path: Path, auth_line: Optional[str] = None):
    binary = samyama_bin()
    data = tmp_path / "samyama-data"
    data.mkdir()
    http_port, resp_port = free_port(), free_port()
    args = [str(binary), "--host", "127.0.0.1", "--port", str(resp_port),
            "--http-port", str(http_port), "--data-path", str(data)]
    if auth_line:
        auth_file = tmp_path / "credentials"
        auth_file.write_text(auth_line + "\n")
        args += ["--auth-file", str(auth_file)]
    log = tmp_path / "samyama.log"
    # cwd is the scratch directory: the server resolves some defaults against
    # its working directory, and the repository is not the place for them.
    proc = subprocess.Popen(args, cwd=tmp_path, stdout=log.open("w"), stderr=subprocess.STDOUT)
    url = f"http://127.0.0.1:{http_port}"
    deadline = time.monotonic() + STARTUP_SECONDS
    while True:
        if proc.poll() is not None:
            pytest.fail(f"samyama exited with {proc.returncode}:\n{log.read_text()[-4000:]}", pytrace=False)
        try:
            urllib.request.urlopen(f"{url}/api/status", timeout=2).close()
            break
        except urllib.error.HTTPError:
            break  # answering, even if only to say 401
        except (urllib.error.URLError, ConnectionError, TimeoutError):
            if time.monotonic() > deadline:
                proc.kill()
                pytest.fail(f"samyama did not answer on {url} within {STARTUP_SECONDS:.0f}s:\n"
                            f"{log.read_text()[-4000:]}", pytrace=False)
            time.sleep(0.2)
    return proc, url, log


def _stop(proc: subprocess.Popen) -> None:
    proc.terminate()
    try:
        proc.wait(timeout=20)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


@pytest.fixture
def engine(tmp_path):
    proc, url, log = _start(tmp_path)
    try:
        yield Engine(url, None, log)
    finally:
        _stop(proc)


@pytest.fixture
def secured_engine(tmp_path):
    """A server that refuses requests without a token, and the token it accepts."""
    # The server's own subcommand writes the credential, so the test does not
    # restate how a token is hashed. It prints the file line, then the token.
    out = subprocess.run([str(samyama_bin()), "auth-token", "pipeline"],
                         capture_output=True, text=True, check=True, cwd=tmp_path).stdout
    lines = [line for line in out.splitlines() if line and not line.startswith("#")]
    credential, token = lines[0], lines[1]
    # Write, not admin: a pipeline that loads data has no business managing tenants.
    proc, url, log = _start(tmp_path, auth_line=f"{credential}:roles=write")
    try:
        yield Engine(url, token, log)
    finally:
        _stop(proc)


# ---------------------------------------------------------------------------
# Source files, and what the graph should hold after loading them
# ---------------------------------------------------------------------------

import samyama_pipeline as sp  # noqa: E402  (needs the sys.path entry above)


@dataclass
class Source:
    accounts_csv: Path
    transfers_csv: Path

    @property
    def accounts(self) -> list[dict]:
        return sp.read_accounts(self.accounts_csv)

    @property
    def transfers(self) -> list[dict]:
        return sp.read_transfers(self.transfers_csv)


@pytest.fixture
def sample() -> Source:
    return Source(sp.SAMPLE_DIR / "accounts.csv", sp.SAMPLE_DIR / "transfers.csv")


def assert_graph_matches(engine: Engine, source: Source) -> None:
    """The graph holds exactly the source files, read back with plain Cypher.

    Deliberately not `samyama_pipeline.check_*`: the pipelines validate with
    those, and a test that asked them whether they were right would pass
    whenever they were wrong in the same way twice.
    """
    accounts, transfers = source.accounts, source.transfers
    assert engine.count("MATCH (a:Account) RETURN count(a)") == len(accounts)
    assert engine.count("MATCH ()-[t:TRANSFER]->() RETURN count(t)") == len(transfers)

    got_accounts = {
        r[0]: {"holder": r[1], "country": r[2], "opened": r[3]}
        for r in engine.query("MATCH (a:Account) RETURN a.id, a.holder, a.country, a.opened")
    }
    assert got_accounts == {
        a["id"]: {"holder": a["holder"], "country": a["country"], "opened": a["opened"]}
        for a in accounts
    }

    got_transfers = {
        r[0]: (r[1], r[2], r[3], r[4])
        for r in engine.query(
            "MATCH (s:Account)-[t:TRANSFER]->(d:Account) RETURN t.id, s.id, d.id, t.amount, t.at")
    }
    assert got_transfers == {t["id"]: (t["src"], t["dst"], t["amount"], t["at"]) for t in transfers}
