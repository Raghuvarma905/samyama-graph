"""The shared load steps, against a real server and with no orchestrator."""

from __future__ import annotations

import csv
from pathlib import Path

import pytest

import samyama_pipeline as sp
from conftest import Source, assert_graph_matches


def write_csv(path: Path, columns, rows) -> Path:
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(columns)
        w.writerows(rows)
    return path


def load_all(client, source: Source, batch_size: int) -> None:
    sp.ensure_schema(client)
    sp.load_accounts(client, source.accounts, batch_size)
    sp.load_transfers(client, source.transfers, batch_size)


# --- reading -----------------------------------------------------------------

def test_a_duplicate_id_names_both_lines(tmp_path):
    f = write_csv(tmp_path / "a.csv", sp.ACCOUNT_COLUMNS,
                  [["A1", "x", "IN", "2020-01-01"], ["A2", "y", "IN", "2020-01-01"],
                   ["A1", "z", "GB", "2021-01-01"]])
    with pytest.raises(sp.SourceDataError, match=r"a\.csv:4: id 'A1' already on line 2"):
        sp.read_accounts(f)


def test_a_missing_column_is_named(tmp_path):
    f = write_csv(tmp_path / "t.csv", ["id", "src", "dst", "at"], [["T1", "A1", "A2", "x"]])
    with pytest.raises(sp.SourceDataError, match="missing column.*amount"):
        sp.read_transfers(f)


@pytest.mark.parametrize("amount", ["twelve", "nan", "inf"])
def test_an_amount_that_is_not_a_finite_number_is_refused(tmp_path, amount):
    f = write_csv(tmp_path / "t.csv", sp.TRANSFER_COLUMNS, [["T1", "A1", "A2", amount, "x"]])
    with pytest.raises(sp.SourceDataError, match=r"t\.csv:2: amount"):
        sp.read_transfers(f)


def test_an_empty_field_is_refused(tmp_path):
    f = write_csv(tmp_path / "a.csv", sp.ACCOUNT_COLUMNS, [["A1", " ", "IN", "2020-01-01"]])
    with pytest.raises(sp.SourceDataError, match="empty 'holder'"):
        sp.read_accounts(f)


def test_batches_cover_every_row_once():
    rows = [{"id": i} for i in range(23)]
    got = list(sp.batches(rows, 5))
    assert [len(b) for b in got] == [5, 5, 5, 5, 3]
    assert [r for b in got for r in b] == rows
    with pytest.raises(ValueError):
        list(sp.batches(rows, 0))


# --- loading -----------------------------------------------------------------

def test_the_sample_loads_exactly(engine, sample):
    # A batch size that divides neither file, so the last batch of each is short.
    load_all(sp.SamyamaHttp(engine.url), sample, batch_size=7)
    assert_graph_matches(engine, sample)


def test_loading_twice_is_loading_once(engine, sample):
    client = sp.SamyamaHttp(engine.url)
    load_all(client, sample, batch_size=7)
    load_all(client, sample, batch_size=11)
    assert_graph_matches(engine, sample)


def test_values_are_stored_as_given_not_read_as_cypher(engine, tmp_path):
    hostile = [
        "O'Brien",
        'say "hi"',
        "x'}) DETACH DELETE (n) //",
        "back\\slash",
        "$rows",
        "名前 — ünïcödé",
    ]
    accounts = write_csv(tmp_path / "a.csv", sp.ACCOUNT_COLUMNS,
                         [[f"A{i}", h, "IN", "2020-01-01"] for i, h in enumerate(hostile)])
    transfers = write_csv(tmp_path / "t.csv", sp.TRANSFER_COLUMNS,
                          [["T0", "A0", "A1", "1.5", "2026-01-01T00:00:00"]])
    source = Source(accounts, transfers)
    load_all(sp.SamyamaHttp(engine.url), source, batch_size=2)
    assert_graph_matches(engine, source)


def test_a_transfer_to_an_unknown_account_writes_nothing(engine, sample, tmp_path):
    client = sp.SamyamaHttp(engine.url)
    sp.ensure_schema(client)
    sp.load_accounts(client, sample.accounts, 500)

    rows = [[t["id"], t["src"], t["dst"], t["amount"], t["at"]] for t in sample.transfers]
    rows.append(["TX-ORPHAN", sample.accounts[0]["id"], "ACC-DOES-NOT-EXIST", "1.00",
                 "2026-01-01T00:00:00"])
    bad = Source(sample.accounts_csv, write_csv(tmp_path / "t.csv", sp.TRANSFER_COLUMNS, rows))

    with pytest.raises(sp.SourceDataError, match="ACC-DOES-NOT-EXIST"):
        sp.load_transfers(client, bad.transfers, 7)
    # Checked before the first batch, so not even the valid rows went in.
    assert engine.count("MATCH ()-[t:TRANSFER]->() RETURN count(t)") == 0


# --- validating --------------------------------------------------------------

def test_the_checks_pass_on_a_correct_load(engine, sample):
    client = sp.SamyamaHttp(engine.url)
    load_all(client, sample, 500)
    checks = sp.check_accounts(client, sample.accounts) + sp.check_transfers(client, sample.transfers)
    assert sp.failures(checks) == []
    # Not vacuous: the checks that count what is present expected the files'
    # rows, not zero of them. (The duplicate check expects zero by design.)
    present = {c.name: c.expected for c in checks if "present" in c.name}
    assert present == {
        "accounts present with the file's values": len(sample.accounts),
        "transfers present between the file's accounts with its values": len(sample.transfers),
    }


def test_the_checks_see_a_changed_value(engine, sample):
    client = sp.SamyamaHttp(engine.url)
    load_all(client, sample, 500)
    victim = sample.transfers[0]
    engine.query("MATCH ()-[t:TRANSFER {id: $id}]->() SET t.amount = $a",
                 {"id": victim["id"], "a": victim["amount"] + 0.01})
    failed = sp.failures(sp.check_transfers(client, sample.transfers))
    assert len(failed) == 1 and "values" in failed[0], failed


def _create_account(engine, a):
    engine.query("CREATE (:Account {id: $id, holder: $h, country: $c, opened: $o})",
                 {"id": a["id"], "h": a["holder"], "c": a["country"], "o": a["opened"]})


def test_after_the_schema_step_the_engine_refuses_a_duplicate_account(engine, sample):
    client = sp.SamyamaHttp(engine.url)
    load_all(client, sample, 500)
    with pytest.raises(Exception, match="Constraint violation"):
        _create_account(engine, sample.accounts[0])
    assert engine.count("MATCH (a:Account) RETURN count(a)") == len(sample.accounts)


def test_the_schema_step_refuses_a_graph_that_already_holds_a_duplicate(engine, sample):
    _create_account(engine, sample.accounts[0])
    _create_account(engine, sample.accounts[0])
    with pytest.raises(sp.SamyamaRejected, match="duplicate value"):
        sp.ensure_schema(sp.SamyamaHttp(engine.url))


def test_the_checks_see_a_duplicate_node_where_no_constraint_stops_it(engine, sample):
    # A graph some other writer populated, without the schema step.
    client = sp.SamyamaHttp(engine.url)
    sp.load_accounts(client, sample.accounts, 500)
    _create_account(engine, sample.accounts[0])
    failed = sp.failures(sp.check_accounts(client, sample.accounts))
    assert any("more than one node" in f for f in failed), failed


def test_the_checks_see_a_duplicate_edge(engine, sample):
    client = sp.SamyamaHttp(engine.url)
    load_all(client, sample, 500)
    t = sample.transfers[0]
    engine.query("MATCH (s:Account {id: $s}), (d:Account {id: $d}) "
                 "CREATE (s)-[:TRANSFER {id: $id, amount: $a, at: $at}]->(d)",
                 {"s": t["src"], "d": t["dst"], "id": t["id"], "a": t["amount"], "at": t["at"]})
    failed = sp.failures(sp.check_transfers(client, sample.transfers))
    # An exact copy -- same id, endpoints and values -- trips both checks:
    # it is one row too many present, and its id is held twice.
    n = len(sample.transfers)
    assert failed == [
        f"transfers present between the file's accounts with its values: expected {n}, found {n + 1}",
        f"no transfer id held by more than one edge: expected 0, found 1 ({t['id']})",
    ]


def test_a_transfer_whose_account_changed_between_runs_is_reported(engine, sample, tmp_path):
    client = sp.SamyamaHttp(engine.url)
    load_all(client, sample, 500)
    # The same transfer id, re-sent with a different destination.
    moved = dict(sample.transfers[0])
    moved["dst"] = next(a["id"] for a in sample.accounts if a["id"] not in (moved["src"], moved["dst"]))
    rows = [moved] + sample.transfers[1:]
    sp.load_transfers(client, rows, 500)
    failed = sp.failures(sp.check_transfers(client, rows))
    assert failed == [f"no transfer id held by more than one edge: expected 0, found 1 ({moved['id']})"]


# --- failing the right way ---------------------------------------------------

def test_a_refused_statement_is_not_retryable(engine):
    with pytest.raises(sp.SamyamaRejected) as e:
        sp.SamyamaHttp(engine.url).query("THIS IS NOT CYPHER")
    assert e.value.status == 400


def test_an_unreachable_server_is_retryable():
    from conftest import free_port
    client = sp.SamyamaHttp(f"http://127.0.0.1:{free_port()}", timeout_seconds=2)
    with pytest.raises(sp.SamyamaUnavailable) as e:
        client.query("RETURN 1")
    assert sp.is_retryable(e.value)


@pytest.mark.parametrize("error, retryable", [
    (sp.SamyamaUnavailable("HTTP 503: writes refused"), True),
    (sp.SamyamaRejected(400, "syntax"), False),
    (sp.SamyamaRejected(401, "no credential"), False),
    (sp.SamyamaError("wrote 59 transfers for 60 rows"), False),
    (sp.SourceDataError("a.csv:3: empty 'holder'"), False),
])
def test_only_an_unavailable_server_is_worth_retrying(error, retryable):
    assert sp.is_retryable(error) == retryable


@pytest.mark.parametrize("status, retryable", [
    (400, False), (401, False), (403, False), (404, False),
    (408, True), (429, True), (500, True), (503, True),
])
def test_an_http_status_is_classified_as_the_scheduler_needs(status, retryable):
    # The engine does not answer every status on demand, so a stub does: what
    # is under test is the client's reading of the status, not the server.
    import http.server
    import json as _json
    import threading

    class Stub(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            body = _json.dumps({"error": f"stub {status}"}).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Stub)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with pytest.raises(sp.SamyamaError) as e:
            sp.SamyamaHttp(f"http://127.0.0.1:{server.server_port}").query("RETURN 1")
        assert sp.is_retryable(e.value) == retryable
        assert f"stub {status}" in str(e.value), "the server's own message is kept"
    finally:
        server.shutdown()
        server.server_close()


def test_a_missing_or_wrong_token_is_refused_and_the_right_one_works(secured_engine, sample):
    for token in (None, "0" * 64):
        with pytest.raises(sp.SamyamaRejected) as e:
            sp.SamyamaHttp(secured_engine.url, token=token).query("RETURN 1")
        assert e.value.status == 401
    load_all(sp.SamyamaHttp(secured_engine.url, token=secured_engine.token), sample, 500)
    assert_graph_matches(secured_engine, sample)
