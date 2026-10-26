"""A published Python SDK, run against a server built from this checkout.

Run by `run.py`, inside a virtualenv holding the SDK from PyPI -- not the one in
this tree. It calls only what that SDK shipped, and asserts what a program
written against it depends on: the methods exist and take the same arguments,
results come back in the same shape and with the same types, a refused write
is refused, and an error is an exception.

The surface is the remote client's core, the part docs/VERSIONING.md promises.
Embedded-mode methods (algorithms, vectors) run in-process and never talk to a
server, so a server change cannot break them.
"""

from __future__ import annotations

import os

import pytest
from samyama import SamyamaClient

URL = os.environ["SAMYAMA_URL"]


@pytest.fixture(scope="module")
def client():
    c = SamyamaClient.connect(URL)
    c.query("MATCH (n) DETACH DELETE n")
    c.query(
        "CREATE (:P {name: 'a', n: 1, x: 1.5, ok: true, tags: ['t1', 't2'], u: 'ünï 名'})"
        "-[:K {w: 2}]->(:P {name: 'b', n: 2})"
    )
    return c


def test_ping_and_status(client):
    assert client.ping()
    status = client.status()
    assert status.status == "healthy"
    assert status.version == os.environ["SAMYAMA_SERVER_VERSION"]
    assert (status.nodes, status.edges) == (2, 1)


def test_a_read_returns_columns_and_typed_values(client):
    r = client.query_readonly(
        "MATCH (p:P {name: 'a'}) "
        "RETURN p.name AS name, p.n AS n, p.x AS x, p.ok AS ok, p.tags AS tags, "
        "p.u AS u, p.missing AS missing"
    )
    assert r.columns == ["name", "n", "x", "ok", "tags", "u", "missing"]
    assert len(r) == 1
    assert r.records == [["a", 1, 1.5, True, ["t1", "t2"], "ünï 名", None]]
    row = r.records[0]
    assert type(row[1]) is int and type(row[2]) is float and type(row[3]) is bool


def test_nodes_and_edges_come_back_with_their_properties(client):
    r = client.query_readonly("MATCH (a:P)-[k:K]->(b:P) RETURN a, k, b")
    assert len(r) == 1
    names = sorted(n["properties"]["name"] for n in r.nodes)
    assert names == ["a", "b"]
    assert [e["properties"]["w"] for e in r.edges] == [2]


def test_a_write_returns_what_it_wrote(client):
    r = client.query("CREATE (c:P {name: 'c'}) RETURN c.name AS name")
    assert r.records == [["c"]]
    assert client.query_readonly("MATCH (p:P) RETURN count(p) AS c").records == [[3]]
    client.query("MATCH (c:P {name: 'c'}) DETACH DELETE c")


def test_stream_yields_rows_as_mappings(client):
    rows = list(client.query_stream("MATCH (p:P) RETURN p.name AS name ORDER BY name"))
    assert [r["name"] for r in rows] == ["a", "b"]


def test_a_read_only_query_refuses_a_write(client):
    with pytest.raises(RuntimeError):
        client.query_readonly("CREATE (:P {name: 'should-not-exist'})")
    assert client.query_readonly("MATCH (p:P {name: 'should-not-exist'}) RETURN count(p)").records == [[0]]


def test_a_statement_the_server_refuses_raises(client):
    with pytest.raises(RuntimeError):
        client.query("THIS IS NOT CYPHER")


def test_graphs_are_listed(client):
    assert "default" in client.list_graphs()
