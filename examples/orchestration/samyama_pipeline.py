"""The load both reference pipelines run, with no orchestrator in it (INT-10).

`samyama_airflow_dag.py` and `samyama_dagster.py` are thin: they decide when a
step runs, where its settings come from, and what a failure means to their
scheduler. What a step *does* lives here, once, so the two pipelines cannot
drift into loading the same files two different ways.

Three properties every orchestrated load needs, and how this module gets each:

- **A retry is safe.** Every write is a `MERGE` keyed on the source row's id,
  so running a step twice leaves the graph as running it once. An orchestrator
  retries on its own initiative -- a worker restart, a timeout -- and a load
  that duplicates on retry corrupts the graph the first time that happens.
- **Values are bound, never interpolated.** Rows travel in the `params` of
  `POST /api/query` (#1463), so a holder name containing a quote, or text that
  looks like Cypher, is stored as the string it is.
- **A failure says whether retrying can help.** `SamyamaUnavailable` (no
  connection, a timeout, a 5xx, a 408 or 429) might clear on its own; anything
  else -- `SamyamaRejected` (the server refused the statement or the
  credential), `SourceDataError` (the input is wrong), a load the server
  reported inconsistently -- will not, and a scheduler that retries it only
  delays the page to a human. `is_retryable` is the one place that decides.

Standard library only: the module runs inside any orchestrator's worker
without adding a dependency to it.
"""

from __future__ import annotations

import csv
import json
import math
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator, Optional, Sequence

#: The sample files the pipelines read when they are not told otherwise.
SAMPLE_DIR = Path(__file__).resolve().parent / "sample"


class SamyamaError(Exception):
    """A failure talking to the server. Retrying will not help unless it is
    the `SamyamaUnavailable` subclass."""


class SamyamaUnavailable(SamyamaError):
    """The server could not be reached, failed, or asked the client to come
    back later; a retry may succeed."""


#: Client errors that say "not now" rather than "not this": a request timeout,
#: and a quota or rate limit.
RETRYABLE_4XX = frozenset({408, 429})


def is_retryable(error: BaseException) -> bool:
    """Whether a step that raised `error` is worth running again.

    Only an unavailable server is. A refused statement or credential, wrong
    input, or a load the server reported inconsistently gets the same answer
    the next time.
    """
    return isinstance(error, SamyamaUnavailable)


class SamyamaRejected(SamyamaError):
    """The server refused the request; a retry will get the same answer."""

    def __init__(self, status: int, message: str):
        super().__init__(f"HTTP {status}: {message}")
        self.status = status
        self.message = message


class SourceDataError(ValueError):
    """The input files are wrong; a retry will read the same files."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """API redirects must not forward write credentials to a different server."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


@dataclass
class SamyamaHttp:
    """A minimal client for `POST /api/query`.

    The Python SDK's `query` takes no parameters, so a pipeline built on it
    would have to splice values into the statement text. This posts the
    statement and its parameters separately, which is the property a loader
    that reads untrusted files cannot do without.
    """

    url: str
    token: Optional[str] = None
    timeout_seconds: float = 30.0

    def __post_init__(self) -> None:
        if not self.url:
            raise ValueError("a Samyama URL is required, e.g. http://localhost:8080")
        self.url = self.url.rstrip("/")

    def query(self, cypher: str, params: Optional[dict] = None, read_only: bool = False) -> dict:
        body: dict[str, Any] = {"query": cypher}
        if params:
            body["params"] = params
        if read_only:
            # The server refuses the statement if it writes (#1628).
            body["read_only"] = True
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        request = urllib.request.Request(
            f"{self.url}/api/query",
            data=json.dumps(body).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.build_opener(_NoRedirect()).open(
                    request, timeout=self.timeout_seconds) as response:
                return json.loads(response.read())
        except urllib.error.HTTPError as e:
            message = _error_message(e)
            if e.code >= 500 or e.code in RETRYABLE_4XX:
                # 503 is the server refusing writes after a failed flush (#1274):
                # it can clear, so it is retryable along with every other 5xx.
                raise SamyamaUnavailable(f"HTTP {e.code}: {message}") from e
            raise SamyamaRejected(e.code, message) from e
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            raise SamyamaUnavailable(f"{self.url}: {e}") from e

    def scalar(self, cypher: str, params: Optional[dict] = None) -> Any:
        """The single value a one-row, one-column read returns."""
        records = self.query(cypher, params, read_only=True)["records"]
        if len(records) != 1 or len(records[0]) != 1:
            raise SamyamaError(f"expected one value, got {records!r} for: {cypher}")
        return records[0][0]


def _error_message(e: urllib.error.HTTPError) -> str:
    raw = e.read().decode("utf-8", "replace")
    try:
        return json.loads(raw).get("error", raw)
    except (ValueError, AttributeError):
        return raw or e.reason


# --------------------------------------------------------------------------
# Reading the source files
# --------------------------------------------------------------------------

ACCOUNT_COLUMNS = ("id", "holder", "country", "opened")
TRANSFER_COLUMNS = ("id", "src", "dst", "amount", "at")


def read_accounts(path: str | Path) -> list[dict]:
    rows = _read_csv(path, ACCOUNT_COLUMNS)
    _reject_duplicate_ids(path, rows)
    return [{"id": r["id"], "holder": r["holder"], "country": r["country"], "opened": r["opened"]}
            for _, r in rows]


def read_transfers(path: str | Path) -> list[dict]:
    rows = _read_csv(path, TRANSFER_COLUMNS)
    _reject_duplicate_ids(path, rows)
    out = []
    for line, r in rows:
        try:
            amount = float(r["amount"])
        except ValueError:
            raise SourceDataError(f"{path}:{line}: amount {r['amount']!r} is not a number") from None
        if not math.isfinite(amount):
            raise SourceDataError(f"{path}:{line}: amount {r['amount']!r} is not finite")
        out.append({"id": r["id"], "src": r["src"], "dst": r["dst"], "amount": amount, "at": r["at"]})
    return out


def _read_csv(path: str | Path, columns: Sequence[str]) -> list[tuple[int, dict]]:
    path = Path(path)
    if not path.is_file():
        raise SourceDataError(f"{path}: no such file")
    with path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        missing = [c for c in columns if c not in (reader.fieldnames or [])]
        if missing:
            raise SourceDataError(f"{path}: missing column(s) {', '.join(missing)}")
        rows = []
        # Line 1 is the header, so the first data row is line 2.
        for line, row in enumerate(reader, start=2):
            for c in columns:
                if row.get(c) is None or row[c].strip() == "":
                    raise SourceDataError(f"{path}:{line}: empty {c!r}")
            rows.append((line, row))
    return rows


def _reject_duplicate_ids(path: str | Path, rows: list[tuple[int, dict]]) -> None:
    # Two rows with one id would MERGE into one node or edge, and the second
    # row's values would silently win. Which one is right is a question for
    # whoever owns the file, not for the loader.
    first_seen: dict[str, int] = {}
    for line, r in rows:
        if r["id"] in first_seen:
            raise SourceDataError(f"{path}:{line}: id {r['id']!r} already on line {first_seen[r['id']]}")
        first_seen[r["id"]] = line


def batches(rows: Sequence[dict], size: int) -> Iterator[list[dict]]:
    if size < 1:
        raise ValueError(f"batch size must be at least 1, got {size}")
    for start in range(0, len(rows), size):
        yield list(rows[start:start + size])


# --------------------------------------------------------------------------
# The steps
# --------------------------------------------------------------------------

#: Run before any load. A unique constraint rather than a plain index: it backs
#: the `{id: ...}` lookups below with an index the same way, and it also makes
#: the engine refuse a second `:Account` with an id that is already taken, so a
#: writer that uses CREATE where it should MERGE fails instead of duplicating.
#: `IF NOT EXISTS` makes the step safe to repeat. Over a graph that already
#: holds a duplicate id the statement is refused, naming the value.
SCHEMA_STATEMENTS = (
    "CREATE CONSTRAINT IF NOT EXISTS FOR (a:Account) REQUIRE a.id IS UNIQUE",
)

UPSERT_ACCOUNTS = """
UNWIND $rows AS r
MERGE (a:Account {id: r.id})
SET a.holder = r.holder, a.country = r.country, a.opened = r.opened
RETURN count(a) AS written
"""

FOUND_ACCOUNTS = """
UNWIND $ids AS id
MATCH (a:Account {id: id})
RETURN collect(a.id) AS found
"""

# Both endpoints in ONE comma-separated MATCH. Written as two MATCH clauses --
# which means the same thing -- the planner does not look each one up by id: it
# builds every pair of accounts and then filters, so the cost grows with the
# square of the number of accounts in the graph rather than with the rows.
UPSERT_TRANSFERS = """
UNWIND $rows AS r
MATCH (s:Account {id: r.src}), (d:Account {id: r.dst})
MERGE (s)-[t:TRANSFER {id: r.id}]->(d)
SET t.amount = r.amount, t.at = r.at
RETURN count(t) AS written
"""

#: Each account, found by its id, holding the values the file holds.
CHECK_ACCOUNTS = """
UNWIND $rows AS r
MATCH (a:Account {id: r.id})
WHERE a.holder = r.holder AND a.country = r.country AND a.opened = r.opened
RETURN count(a) AS matched
"""

# Each transfer, between the accounts the file names, holding its values. The
# endpoints are named variables: the engine refuses a property map that is not
# a literal on an anonymous node (`(:Account {id: r.src})`).
CHECK_TRANSFERS = """
UNWIND $rows AS r
MATCH (s:Account {id: r.src})-[t:TRANSFER]->(d:Account {id: r.dst})
WHERE t.id = r.id AND t.amount = r.amount AND t.at = r.at
RETURN count(t) AS matched
"""

# Edge ids have no index to look them up by, so this is one pass over the
# TRANSFER edges, grouped by id -- linear in the edges, where asking id by id
# would be linear in the edges once per id.
DUPLICATED_TRANSFER_IDS = """
MATCH ()-[t:TRANSFER]->()
WITH t.id AS id, count(t) AS n
WHERE n > 1
RETURN collect(id) AS duplicated
"""


DUPLICATED_ACCOUNT_IDS = """
MATCH (a:Account)
WITH a.id AS id, count(a) AS n
WHERE n > 1
RETURN collect(id) AS duplicated
"""


def ensure_schema(client: SamyamaHttp) -> int:
    for statement in SCHEMA_STATEMENTS:
        client.query(statement)
    return len(SCHEMA_STATEMENTS)


def load_accounts(client: SamyamaHttp, rows: Sequence[dict], batch_size: int) -> int:
    written = 0
    for batch in batches(rows, batch_size):
        written += client.query(UPSERT_ACCOUNTS, {"rows": batch})["records"][0][0]
    if written != len(rows):
        raise SamyamaError(f"wrote {written} accounts for {len(rows)} rows")
    return written


def load_transfers(client: SamyamaHttp, rows: Sequence[dict], batch_size: int) -> int:
    # Checked before anything is written. A transfer whose account does not
    # exist would otherwise match nothing and be dropped without a word: the
    # MATCH finds no row, so the MERGE never runs and the statement succeeds.
    endpoints = sorted({r["src"] for r in rows} | {r["dst"] for r in rows})
    found = set(client.scalar(FOUND_ACCOUNTS, {"ids": endpoints})) if endpoints else set()
    missing = [i for i in endpoints if i not in found]
    if missing:
        raise SourceDataError(
            f"{len(missing)} account(s) referenced by transfers do not exist: "
            + ", ".join(sorted(missing)[:20])
            + (" ..." if len(missing) > 20 else "")
        )
    written = 0
    for batch in batches(rows, batch_size):
        written += client.query(UPSERT_TRANSFERS, {"rows": batch})["records"][0][0]
    if written != len(rows):
        raise SamyamaError(f"wrote {written} transfers for {len(rows)} rows")
    return written


@dataclass
class Check:
    name: str
    expected: int
    found: int
    detail: str = field(default="")

    @property
    def passed(self) -> bool:
        return self.found == self.expected


def check_accounts(client: SamyamaHttp, rows: Sequence[dict]) -> list[Check]:
    ids = {r["id"] for r in rows}
    duplicated = sorted(ids.intersection(client.scalar(DUPLICATED_ACCOUNT_IDS)))
    return [
        Check("accounts present with the file's values", len(rows),
              client.scalar(CHECK_ACCOUNTS, {"rows": list(rows)})),
        # Count duplicates by ID: an extra copy of A must not cancel a missing B.
        Check("no account id held by more than one node", 0, len(duplicated),
              ", ".join(duplicated[:20])),
    ]


def check_transfers(client: SamyamaHttp, rows: Sequence[dict]) -> list[Check]:
    ids = {r["id"] for r in rows}
    # Only this file's ids count against it: a duplicate some other load wrote
    # is that load's to answer for.
    duplicated = sorted(ids.intersection(client.scalar(DUPLICATED_TRANSFER_IDS)))
    return [
        Check("transfers present between the file's accounts with its values", len(rows),
              client.scalar(CHECK_TRANSFERS, {"rows": list(rows)})),
        # MERGE matches on both endpoints and the id, so a source row whose
        # account changed between two runs adds a second edge with the old
        # id rather than moving the first. This is what sees it.
        Check("no transfer id held by more than one edge", 0, len(duplicated),
              ", ".join(duplicated[:20])),
    ]


def failures(checks: Sequence[Check]) -> list[str]:
    return [f"{c.name}: expected {c.expected}, found {c.found}" + (f" ({c.detail})" if c.detail else "")
            for c in checks if not c.passed]
