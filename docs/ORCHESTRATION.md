# Loading Samyama from Airflow and Dagster

A scheduled pipeline that loads files into the graph and checks the result, in
the two orchestrators most data teams already run. One reference pipeline per
tool, both run against a real server by the test suite:

| Tool | Reference | How-to |
|---|---|---|
| Apache Airflow 3 | [`examples/orchestration/samyama_airflow_dag.py`](../examples/orchestration/samyama_airflow_dag.py) | [ORCHESTRATION-AIRFLOW.md](./ORCHESTRATION-AIRFLOW.md) |
| Dagster | [`examples/orchestration/samyama_dagster.py`](../examples/orchestration/samyama_dagster.py) | [ORCHESTRATION-DAGSTER.md](./ORCHESTRATION-DAGSTER.md) |

Both are thin. What a step *does* is in
[`samyama_pipeline.py`](../examples/orchestration/samyama_pipeline.py), once, so
the two cannot drift into loading the same files two different ways. It is
standard library only, so it adds nothing to an orchestrator's worker image.

---

## What the pipeline does

```
schema  ->  accounts  ->  transfers  ->  validate
```

| Step | What it does |
|---|---|
| schema | A unique constraint on `:Account(id)`. It backs the id lookups with an index, and makes the engine refuse a second account with a taken id. `IF NOT EXISTS`, so repeating it is harmless. |
| accounts | Reads `accounts.csv` (`id, holder, country, opened`) and upserts one `:Account` per row, in batches. |
| transfers | Reads `transfers.csv` (`id, src, dst, amount, at`), checks every account it names exists, then upserts one `:TRANSFER` relationship per row, in batches. |
| validate | Reads the files again and asks the graph whether it holds exactly them. Fails the run if not. |

The sample in `examples/orchestration/sample/` is synthetic: 24 accounts and 60
transfers, generated, describing no real party.

## The three properties every orchestrated load needs

**A retry is safe.** Orchestrators retry on their own initiative -- a worker
restart, a timeout, a node drained for maintenance. Every write is a `MERGE`
keyed on the source row's id, so running a step twice leaves the graph as
running it once. Both test suites run each pipeline twice and compare the graph
with the files.

**Values are bound, never pasted into the statement.** Rows travel as the
`params` of `POST /api/query` (#1463):

```cypher
UNWIND $rows AS r
MATCH (s:Account {id: r.src}), (d:Account {id: r.dst})
MERGE (s)-[t:TRANSFER {id: r.id}]->(d)
SET t.amount = r.amount, t.at = r.at
RETURN count(t) AS written
```

A holder name with a quote in it, or text that looks like Cypher, is stored as
the string it is; the tests load a few such values and read them back. This is
also why the pipelines talk HTTP rather than use the Python SDK: the SDK's
`query()` takes no parameters, so using it would mean building statement text
out of file contents.

**A failure says whether retrying can help.**

| Failure | Retried? | Why |
|---|---|---|
| Server unreachable, timeout, any 5xx (including 503 when the server refuses writes after a failed flush) | Yes, with backoff | It can clear on its own. |
| The server refused the statement or the credential (4xx) | No | It will refuse it again. |
| The input is wrong: a missing column, an empty field, a duplicate id, an amount that is not a number, a transfer naming an account that does not exist | No | A retry reads the same file. |
| Validation found the graph disagrees with the files | No | A retry re-checks the same graph. |

Retrying a permanent failure only delays the page to whoever has to fix it.
Airflow gets `AirflowFailException`; Dagster gets `Failure(allow_retries=False)`.

## What validation checks

Nothing it checks is taken from the load step's own report -- the files are read
again and the graph is asked:

| Check | Catches |
|---|---|
| every account, found by id, holds the file's `holder`, `country`, `opened` | a missed row, a value that did not arrive, a value something else changed |
| no account id is held by two nodes | a writer that used `CREATE` where it should `MERGE` (the constraint stops this once the schema step has run; the check covers graphs where it has not) |
| every transfer runs between the accounts the file names and holds its `id`, `amount`, `at` | a dropped or misdirected edge, a changed value |
| no transfer id from this file is held by two relationships | a transfer whose account changed between two runs: `MERGE` matches on both endpoints and the id, so it adds a second relationship rather than moving the first |

The transfer step also refuses, before writing anything, a file that names an
account the graph does not hold. Without that, the `MATCH` would find no row for
it, the `MERGE` would never run, and the statement would succeed having silently
skipped the transfer.

## Adapting it to your data

Copy `samyama_pipeline.py` and change the statements and the two readers; the
orchestrator files need to change only if you add or rename a step. Keep:

- **`MERGE` on a key that comes from the source**, with a unique constraint on
  it. Without a key a retry duplicates.
- **Both endpoints of a relationship in one comma-separated `MATCH`.** Written
  as two `MATCH` clauses -- which means the same thing -- the planner does not
  look either up by id: it pairs every node with every node and filters. On a
  graph of 1,000 accounts a 200-row batch took 4.5 ms in the comma form and
  timed out after 281 s in the two-clause form (2026-10-19, release build of
  `dev` at `23a96ec`, one laptop, one run each -- the gap is the point, not the
  figures).
- **Named variables on nodes whose properties come from the row.**
  `(:Account {id: r.src})` with no variable is refused in a `MATCH`; write
  `(s:Account {id: r.src})`.
- **Batches.** The default is 500 rows per statement; it is a parameter of both
  pipelines.

## What this does not do

- **A run is not one transaction.** Each batch commits on its own, so while a
  run is in progress -- or after it fails -- readers see a partial load, which
  the next run completes. `POST /api/tx/begin` can make a run atomic, but a
  transaction holds the store's write lock: every other reader and writer waits
  for the whole load, and the transaction is rolled back if it outlives the
  session timeout. That trade is not made for you here.
- **Deletions are not propagated.** A row removed from the file stays in the
  graph. Upserts cannot tell "deleted" from "not in this extract".
- **One graph per server.** The open-source server serves the graph named
  `default`; use a separate server and data directory per dataset.
- **dbt is not covered.** dbt compiles SQL for a SQL warehouse, and there is no
  dbt adapter for Cypher.
- **Airflow 2 is not supported.** The DAG imports from `airflow.sdk`, which is
  Airflow 3's authoring interface.

## How it is tested

`examples/orchestration/tests/`, run by the `orchestration` job in
`.github/workflows/ci.yml` against the release binary the `test` job builds:

```bash
cargo build --release --bin samyama
cd examples/orchestration
pip install -r requirements.txt \
  -c https://raw.githubusercontent.com/apache/airflow/constraints-3.3.2/constraints-3.12.txt
python -m pytest -v tests/
```

Each test starts its own server on spare ports with a fresh data directory, so
no test sees another's graph and nothing touches a server you are running.
`SAMYAMA_BIN` names another binary.

What the tests run: the Airflow DAG through `dag.test()` and the Dagster assets
through `materialize`, in process, with Airflow's own Connection lookup and
Dagster's own resources. What they do **not** run: a deployed scheduler with
separate workers, or any version other than the pinned Airflow 3.3.2 and
Dagster 1.13.26.

The CI job is not yet one of the checks `ci-ok` waits for, so a failure there
shows on a pull request without blocking its merge; whether it should block is
a separate decision, recorded in `tests/ci_gate_enumeration.rs`.
