"""The reference Dagster assets and checks, run by Dagster against a real server.

`materialize` executes the assets and their checks in this process through
Dagster's own executor, with the resources an operator would configure.
"""

from __future__ import annotations

import csv

import dagster as dg

import samyama_dagster as sd
from conftest import Source, assert_graph_matches


def resources(engine, source: Source, batch_size: int = 500, token=None) -> dict:
    return {
        "samyama": sd.SamyamaResource(url=engine.url, token=token),
        "source_files": sd.SourceFiles(accounts_csv=str(source.accounts_csv),
                                       transfers_csv=str(source.transfers_csv),
                                       batch_size=batch_size),
    }


def run(res: dict) -> dg.ExecuteInProcessResult:
    return dg.materialize([*sd.ASSETS, *sd.CHECKS], resources=res, raise_on_error=False)


def checks(result: dg.ExecuteInProcessResult) -> dict:
    return {e.check_name: e.passed for e in result.get_asset_check_evaluations()}


def materialized(result: dg.ExecuteInProcessResult) -> set:
    return {e.asset_key.to_user_string() for e in result.get_asset_materialization_events()}


def test_the_assets_load_the_sample_and_the_checks_pass(engine, sample):
    result = run(resources(engine, sample, batch_size=7))
    assert result.success
    assert materialized(result) == {"samyama_schema", "accounts", "transfers"}
    assert checks(result) == {"accounts_match_source": True, "transfers_match_source": True}
    assert_graph_matches(engine, sample)


def test_materializing_twice_leaves_the_graph_as_once(engine, sample):
    assert run(resources(engine, sample, batch_size=7)).success
    assert run(resources(engine, sample, batch_size=13)).success
    assert_graph_matches(engine, sample)


def test_bad_input_fails_the_asset_without_retrying(engine, sample, tmp_path):
    bad = tmp_path / "transfers.csv"
    with bad.open("w", newline="") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(["id", "src", "dst", "amount", "at"])
        w.writerow(["TX-1", sample.accounts[0]["id"], "ACC-NOWHERE", "1.00", "2026-01-01T00:00:00"])
    result = run(resources(engine, Source(sample.accounts_csv, bad)))
    assert not result.success
    assert "transfers" not in materialized(result)
    failures = [e for e in result.all_events if e.event_type_value == "STEP_FAILURE"]
    assert [e.step_key for e in failures] == ["transfers"]
    assert "ACC-NOWHERE" in failures[0].event_specific_data.error.message
    assert not [e for e in result.all_events if e.event_type_value == "STEP_UP_FOR_RETRY"]
    assert engine.count("MATCH ()-[t:TRANSFER]->() RETURN count(t)") == 0


def test_a_failed_accounts_check_blocks_the_transfers(engine, sample, monkeypatch):
    # Make the graph disagree with the file between the load and its check:
    # the check must fail, and being blocking, stop `transfers` from loading.
    real = sd.sp.load_accounts

    def load_then_corrupt(client, rows, batch_size):
        written = real(client, rows, batch_size)
        client.query("MATCH (a:Account {id: $id}) SET a.holder = 'tampered'", {"id": rows[0]["id"]})
        return written

    monkeypatch.setattr(sd.sp, "load_accounts", load_then_corrupt)
    result = run(resources(engine, sample))
    assert not result.success
    assert checks(result) == {"accounts_match_source": False}
    assert "transfers" not in materialized(result)
    assert engine.count("MATCH ()-[t:TRANSFER]->() RETURN count(t)") == 0


def test_the_token_comes_from_the_resource(secured_engine, sample):
    result = run(resources(secured_engine, sample, token=secured_engine.token))
    assert result.success
    assert_graph_matches(secured_engine, sample)


def test_a_resource_without_the_token_fails_without_retrying(secured_engine, sample):
    result = run(resources(secured_engine, sample, token=None))
    assert not result.success
    failures = [e for e in result.all_events if e.event_type_value == "STEP_FAILURE"]
    assert [e.step_key for e in failures] == ["samyama_schema"]
    assert "401" in failures[0].event_specific_data.error.message
    assert not [e for e in result.all_events if e.event_type_value == "STEP_UP_FOR_RETRY"]


def test_the_definitions_read_the_server_from_the_environment(engine, sample, monkeypatch):
    # The shipped definitions, not a test-built copy: the URL arrives through EnvVar.
    monkeypatch.delenv("SAMYAMA_TOKEN", raising=False)
    monkeypatch.setenv("SAMYAMA_URL", engine.url)
    job = sd.build_definitions().resolve_implicit_global_asset_job_def()
    result = job.execute_in_process(raise_on_error=False)
    assert result.success
    assert_graph_matches(engine, sample)


def test_the_definitions_read_the_token_from_the_environment(secured_engine, sample, monkeypatch):
    monkeypatch.setenv("SAMYAMA_URL", secured_engine.url)
    monkeypatch.setenv("SAMYAMA_TOKEN", secured_engine.token)
    job = sd.build_definitions().resolve_implicit_global_asset_job_def()
    result = job.execute_in_process(raise_on_error=False)
    assert result.success
    assert_graph_matches(secured_engine, sample)
