// A published TypeScript SDK, run against a server built from this checkout.
//
// Run by `run.py` from a scratch project holding the SDK from npm -- not the
// one in this tree. The same contract as test_python_sdk.py, through the
// TypeScript client: only what that SDK shipped is called, and what a program
// written against it depends on is asserted.
//
// `node --test`, so it needs nothing installed beyond the SDK itself.

import { test, before } from "node:test";
import assert from "node:assert/strict";
import { SamyamaClient } from "samyama-sdk";

const client = new SamyamaClient({ url: process.env.SAMYAMA_URL });

before(async () => {
  await client.query("MATCH (n) DETACH DELETE n");
  await client.query(
    "CREATE (:P {name: 'a', n: 1, x: 1.5, ok: true, tags: ['t1', 't2'], u: 'ünï 名'})" +
      "-[:K {w: 2}]->(:P {name: 'b', n: 2})",
  );
});

test("ping, healthy and status", async () => {
  assert.equal(await client.ping(), "PONG");
  assert.equal(await client.healthy(), true);
  const s = await client.status();
  assert.equal(s.status, "healthy");
  assert.equal(s.version, process.env.SAMYAMA_SERVER_VERSION);
  assert.deepEqual([s.storage.nodes, s.storage.edges], [2, 1]);
});

test("a read returns columns and typed values", async () => {
  const r = await client.queryReadonly(
    "MATCH (p:P {name: 'a'}) " +
      "RETURN p.name AS name, p.n AS n, p.x AS x, p.ok AS ok, p.tags AS tags, p.u AS u, p.missing AS missing",
  );
  assert.deepEqual(r.columns, ["name", "n", "x", "ok", "tags", "u", "missing"]);
  assert.deepEqual(r.records, [["a", 1, 1.5, true, ["t1", "t2"], "ünï 名", null]]);
});

test("nodes and edges come back with their properties", async () => {
  const r = await client.queryReadonly("MATCH (a:P)-[k:K]->(b:P) RETURN a, k, b");
  assert.deepEqual(r.nodes.map((n) => n.properties.name).sort(), ["a", "b"]);
  assert.deepEqual(r.edges.map((e) => [e.type, e.properties.w]), [["K", 2]]);
  const ids = new Set(r.nodes.map((n) => n.id));
  assert.ok(ids.has(r.edges[0].source) && ids.has(r.edges[0].target), "edge endpoints are nodes in the result");
});

test("a write returns what it wrote", async () => {
  const r = await client.query("CREATE (c:P {name: 'c'}) RETURN c.name AS name");
  assert.deepEqual(r.records, [["c"]]);
  assert.deepEqual((await client.queryReadonly("MATCH (p:P) RETURN count(p) AS c")).records, [[3]]);
  await client.query("MATCH (c:P {name: 'c'}) DETACH DELETE c");
});

test("the stream yields rows as objects keyed by column", async () => {
  const names = [];
  for await (const row of client.queryStream("MATCH (p:P) RETURN p.name AS name ORDER BY name")) {
    names.push(row.name);
  }
  assert.deepEqual(names, ["a", "b"]);
});

test("a read-only query refuses a write", async () => {
  await assert.rejects(client.queryReadonly("CREATE (:P {name: 'should-not-exist'})"));
  const r = await client.queryReadonly("MATCH (p:P {name: 'should-not-exist'}) RETURN count(p) AS c");
  assert.deepEqual(r.records, [[0]]);
});

test("a statement the server refuses rejects with its message", async () => {
  await assert.rejects(client.query("THIS IS NOT CYPHER"), (e) => e instanceof Error && e.message.length > 0);
});

test("EXPLAIN returns a plan without running the statement", async () => {
  const plan = await client.explain("CREATE (:P {name: 'explained'})");
  assert.ok(plan.records.length > 0, "a plan has at least one row");
  const r = await client.queryReadonly("MATCH (p:P {name: 'explained'}) RETURN count(p) AS c");
  assert.deepEqual(r.records, [[0]]);
});
