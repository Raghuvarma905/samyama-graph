"""Reference Dagster assets: load accounts and transfers into Samyama, with checks (INT-10).

    samyama_schema -> accounts -> transfers
                      (check)     (check)

Load it as a code location with this directory on the path:

    dagster dev -f samyama_dagster.py

Where the settings come from -- nothing about a deployment is written here:

- `SAMYAMA_URL` (required) names the server's HTTP endpoint. `SAMYAMA_TOKEN`
  is the bearer token, needed only when the server runs with an auth file. Both
  are resolved in the execution environment: the URL through `dg.EnvVar`,
  and the optional token when the resource creates its client. No token value
  is stored with the code location.
- The source files and batch size are the `source_files` resource. They default
  to the bundled sample; set them per run in the launchpad's resource config.

Each asset's check is blocking: if the accounts in the graph do not match the
file, the transfers are not loaded on top of them.

Retries: an unreachable server is retried with backoff. A refused statement or
credential, or wrong input, fails at once with `allow_retries=False` -- retrying
would read the same files and get the same answer. See
docs/ORCHESTRATION-DAGSTER.md.

Tested against Dagster 1.13 by `tests/test_dagster.py`.
"""

from __future__ import annotations

import functools
import os
from typing import Optional

import dagster as dg

import samyama_pipeline as sp


class SamyamaResource(dg.ConfigurableResource):
    """The server, its credential, and how long one statement may take."""

    url: str
    token: Optional[str] = None
    token_env_var: Optional[str] = None
    timeout_seconds: float = 30.0

    def client(self) -> sp.SamyamaHttp:
        token = self.token
        if token is None and self.token_env_var is not None:
            token = os.environ.get(self.token_env_var)
        return sp.SamyamaHttp(self.url, token=token or None, timeout_seconds=self.timeout_seconds)


class SourceFiles(dg.ConfigurableResource):
    """Where the rows come from, and how many go in one statement."""

    accounts_csv: str = str(sp.SAMPLE_DIR / "accounts.csv")
    transfers_csv: str = str(sp.SAMPLE_DIR / "transfers.csv")
    batch_size: int = 500


RETRY = dg.RetryPolicy(max_retries=3, delay=30, backoff=dg.Backoff.EXPONENTIAL)


def fail_fast_on_permanent_errors(fn):
    """Turn a failure that a retry cannot fix into one Dagster does not retry."""

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except (sp.SamyamaError, sp.SourceDataError) as e:
            if sp.is_retryable(e):
                raise
            raise dg.Failure(description=str(e), allow_retries=False) from e

    return wrapper


@dg.asset(retry_policy=RETRY, group_name="samyama")
@fail_fast_on_permanent_errors
def samyama_schema(samyama: SamyamaResource) -> dg.MaterializeResult:
    return dg.MaterializeResult(metadata={"statements": sp.ensure_schema(samyama.client())})


@dg.asset(deps=[samyama_schema], retry_policy=RETRY, group_name="samyama")
@fail_fast_on_permanent_errors
def accounts(samyama: SamyamaResource, source_files: SourceFiles) -> dg.MaterializeResult:
    rows = sp.read_accounts(source_files.accounts_csv)
    written = sp.load_accounts(samyama.client(), rows, source_files.batch_size)
    return dg.MaterializeResult(metadata={"rows": written, "source": source_files.accounts_csv})


@dg.asset(deps=[accounts], retry_policy=RETRY, group_name="samyama")
@fail_fast_on_permanent_errors
def transfers(samyama: SamyamaResource, source_files: SourceFiles) -> dg.MaterializeResult:
    rows = sp.read_transfers(source_files.transfers_csv)
    written = sp.load_transfers(samyama.client(), rows, source_files.batch_size)
    return dg.MaterializeResult(metadata={"rows": written, "source": source_files.transfers_csv})


def _result(checks: list[sp.Check]) -> dg.AssetCheckResult:
    failed = sp.failures(checks)
    return dg.AssetCheckResult(
        passed=not failed,
        description="; ".join(failed) or "the graph matches the source file",
        metadata={c.name: f"{c.found} of {c.expected}" for c in checks},
    )


@dg.asset_check(asset=accounts, blocking=True, retry_policy=RETRY)
@fail_fast_on_permanent_errors
def accounts_match_source(samyama: SamyamaResource, source_files: SourceFiles) -> dg.AssetCheckResult:
    return _result(sp.check_accounts(samyama.client(), sp.read_accounts(source_files.accounts_csv)))


@dg.asset_check(asset=transfers, blocking=True, retry_policy=RETRY)
@fail_fast_on_permanent_errors
def transfers_match_source(samyama: SamyamaResource, source_files: SourceFiles) -> dg.AssetCheckResult:
    return _result(sp.check_transfers(samyama.client(), sp.read_transfers(source_files.transfers_csv)))


ASSETS = [samyama_schema, accounts, transfers]
CHECKS = [accounts_match_source, transfers_match_source]

def build_definitions() -> dg.Definitions:
    return dg.Definitions(
        assets=ASSETS,
        asset_checks=CHECKS,
        resources={
            "samyama": SamyamaResource(
                url=dg.EnvVar("SAMYAMA_URL"),
                # Resolve optional credentials on workers, where secrets may be
                # injected after the code location builds these definitions.
                token_env_var="SAMYAMA_TOKEN",
            ),
            "source_files": SourceFiles(),
        },
    )


defs = build_definitions()
