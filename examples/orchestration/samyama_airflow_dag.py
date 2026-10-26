"""Reference Airflow DAG: load accounts and transfers into Samyama, then check them (INT-10).

    schema -> accounts -> transfers -> validate

Copy this file and `samyama_pipeline.py` into your DAGs folder. Airflow puts
that folder on `sys.path`, which is how the import below resolves.

Where the settings come from -- nothing about a deployment is written here:

- The server and its credential come from an Airflow Connection, by default
  `samyama_default`: host, port, `schema` (`http` or `https`, the convention
  Airflow's own HTTP hook uses) and, if the server has an auth file, the token
  in `password`. Optional `timeout_seconds` in the extras.
- The source files and batch size are DAG params, so a run can be pointed at
  other files without editing the DAG. They default to the bundled sample.

Retries: a step that fails because the server was unreachable is retried, with
backoff. A step that fails because the server refused the statement or the
credential, or because the input is wrong, fails at once -- retrying would read
the same files and get the same answer. See docs/ORCHESTRATION-AIRFLOW.md.

Tested against Airflow 3.3 by `tests/test_airflow.py`.
"""

from __future__ import annotations

import functools
from datetime import timedelta
from urllib.parse import urlsplit

from airflow.sdk import BaseHook, Param, dag, get_current_context, task
from airflow.sdk.exceptions import AirflowFailException

import samyama_pipeline as sp

DEFAULT_CONN_ID = "samyama_default"


def client_from_connection(conn_id: str) -> sp.SamyamaHttp:
    conn = BaseHook.get_connection(conn_id)
    host = conn.host or ""
    if not host:
        raise AirflowFailException(f"connection {conn_id!r} has no host")
    base = host if "://" in host else f"{conn.schema or 'http'}://{host}"
    if conn.port and urlsplit(base).port is None:
        base = f"{base}:{conn.port}"
    timeout = float(conn.extra_dejson.get("timeout_seconds", 30))
    return sp.SamyamaHttp(base, token=conn.password or None, timeout_seconds=timeout)


def fail_fast_on_permanent_errors(fn):
    """Turn a failure that a retry cannot fix into one Airflow does not retry."""

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except (sp.SamyamaError, sp.SourceDataError) as e:
            if sp.is_retryable(e):
                raise
            raise AirflowFailException(str(e)) from e

    return wrapper


def _settings() -> tuple[sp.SamyamaHttp, dict]:
    params = get_current_context()["params"]
    return client_from_connection(params["samyama_conn_id"]), params


@dag(
    dag_id="samyama_accounts_transfers",
    description="Load accounts and transfers from CSV into Samyama, then check the graph matches the files",
    schedule=None,
    catchup=False,
    max_active_runs=1,
    tags=["samyama", "reference"],
    default_args={
        "retries": 3,
        "retry_delay": timedelta(seconds=30),
        "retry_exponential_backoff": True,
    },
    params={
        "samyama_conn_id": Param(DEFAULT_CONN_ID, type="string",
                                 description="Airflow Connection holding the server URL and token"),
        "accounts_csv": Param(str(sp.SAMPLE_DIR / "accounts.csv"), type="string",
                              description="CSV with columns id, holder, country, opened"),
        "transfers_csv": Param(str(sp.SAMPLE_DIR / "transfers.csv"), type="string",
                               description="CSV with columns id, src, dst, amount, at"),
        "batch_size": Param(500, type="integer", minimum=1,
                            description="Rows per statement"),
    },
)
def samyama_accounts_transfers():
    @task
    @fail_fast_on_permanent_errors
    def schema() -> int:
        client, _ = _settings()
        return sp.ensure_schema(client)

    @task
    @fail_fast_on_permanent_errors
    def accounts() -> int:
        client, params = _settings()
        rows = sp.read_accounts(params["accounts_csv"])
        return sp.load_accounts(client, rows, params["batch_size"])

    @task
    @fail_fast_on_permanent_errors
    def transfers() -> int:
        client, params = _settings()
        rows = sp.read_transfers(params["transfers_csv"])
        return sp.load_transfers(client, rows, params["batch_size"])

    @task
    @fail_fast_on_permanent_errors
    def validate() -> dict:
        client, params = _settings()
        checks = sp.check_accounts(client, sp.read_accounts(params["accounts_csv"]))
        checks += sp.check_transfers(client, sp.read_transfers(params["transfers_csv"]))
        failed = sp.failures(checks)
        if failed:
            # The graph disagrees with the files it was loaded from. Retrying
            # re-checks the same graph against the same files.
            raise AirflowFailException("; ".join(failed))
        return {c.name: c.found for c in checks}

    schema() >> accounts() >> transfers() >> validate()


samyama_accounts_transfers()
