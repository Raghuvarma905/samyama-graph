"""The reference Airflow DAG, run by Airflow against a real server.

`dag.test()` runs every task in this process through Airflow's own task
runner: params are resolved, the Connection is looked up through Airflow's
secrets backends, and a task's failure is classified the way a scheduler would
classify it. It needs a metadata database, so the session gets a throwaway one.

Airflow reads its configuration when it is first imported, so nothing here
imports it at module level: the environment is set first, in the fixture.
"""

from __future__ import annotations

import csv
import importlib
import json
import os
import subprocess
import sys

import pytest

from conftest import PIPELINE_DIR, Source, assert_graph_matches

DAG_ID = "samyama_accounts_transfers"


@pytest.fixture(scope="session")
def airflow_dag(tmp_path_factory):
    home = tmp_path_factory.mktemp("airflow-home")
    env = {
        "AIRFLOW_HOME": str(home),
        "AIRFLOW__CORE__LOAD_EXAMPLES": "False",
        "AIRFLOW__CORE__DAGS_FOLDER": str(PIPELINE_DIR),
    }
    os.environ.update(env)
    migrate = subprocess.run([sys.executable, "-m", "airflow", "db", "migrate"],
                             capture_output=True, text=True, env={**os.environ, **env})
    assert migrate.returncode == 0, migrate.stdout[-3000:] + migrate.stderr[-3000:]
    module = importlib.import_module("samyama_airflow_dag")
    return module.samyama_accounts_transfers()


def connect(monkeypatch, engine, conn_id="samyama_default"):
    """Point an Airflow Connection at the server, the way an operator would."""
    host_port = engine.url.split("://", 1)[1]
    host, port = host_port.rsplit(":", 1)
    conn = {"conn_type": "http", "host": host, "port": int(port), "schema": "http"}
    if engine.token:
        conn["password"] = engine.token
    monkeypatch.setenv(f"AIRFLOW_CONN_{conn_id.upper()}", json.dumps(conn))


def run(dag, source: Source, **conf):
    run_conf = {"accounts_csv": str(source.accounts_csv),
                "transfers_csv": str(source.transfers_csv), **conf}
    dr = dag.test(run_conf=run_conf)
    states = {ti.task_id: (ti.state, ti.try_number) for ti in dr.get_task_instances()}
    return dr.state, states


def test_the_dag_loads_the_sample_and_validates_it(airflow_dag, engine, sample, monkeypatch):
    connect(monkeypatch, engine)
    state, tasks = run(airflow_dag, sample, batch_size=7)
    assert state == "success", tasks
    assert {t: s for t, (s, _) in tasks.items()} == {
        "schema": "success", "accounts": "success", "transfers": "success", "validate": "success"}
    assert_graph_matches(engine, sample)


def test_running_the_dag_twice_leaves_the_graph_as_once(airflow_dag, engine, sample, monkeypatch):
    connect(monkeypatch, engine)
    assert run(airflow_dag, sample, batch_size=7)[0] == "success"
    assert run(airflow_dag, sample, batch_size=13)[0] == "success"
    assert_graph_matches(engine, sample)


def test_bad_input_fails_the_run_without_retrying(airflow_dag, engine, sample, monkeypatch, tmp_path):
    connect(monkeypatch, engine)
    bad = tmp_path / "transfers.csv"
    with bad.open("w", newline="") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(["id", "src", "dst", "amount", "at"])
        w.writerow(["TX-1", sample.accounts[0]["id"], "ACC-NOWHERE", "1.00", "2026-01-01T00:00:00"])
    state, tasks = run(airflow_dag, Source(sample.accounts_csv, bad))
    assert state == "failed"
    assert tasks["transfers"] == ("failed", 1), tasks  # one try: the DAG allows three retries
    assert tasks["validate"][0] == "upstream_failed"
    assert engine.count("MATCH ()-[t:TRANSFER]->() RETURN count(t)") == 0


def test_the_token_comes_from_the_connection(airflow_dag, secured_engine, sample, monkeypatch):
    connect(monkeypatch, secured_engine)
    state, tasks = run(airflow_dag, sample)
    assert state == "success", tasks
    assert_graph_matches(secured_engine, sample)


def test_a_connection_without_the_token_fails_without_retrying(airflow_dag, secured_engine, sample,
                                                                monkeypatch):
    connect(monkeypatch, secured_engine.__class__(secured_engine.url, None, secured_engine.log))
    state, tasks = run(airflow_dag, sample)
    assert state == "failed"
    assert tasks["schema"] == ("failed", 1), tasks


def test_the_connection_id_is_a_param(airflow_dag, engine, sample, monkeypatch):
    connect(monkeypatch, engine, conn_id="graph_prod_like")
    state, tasks = run(airflow_dag, sample, samyama_conn_id="graph_prod_like")
    assert state == "success", tasks
    assert_graph_matches(engine, sample)
