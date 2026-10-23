# Loading Samyama from Apache Airflow

How to run the reference DAG,
[`examples/orchestration/samyama_airflow_dag.py`](../examples/orchestration/samyama_airflow_dag.py),
against your server and your files. What the pipeline does, the guarantees it
makes and what it does not do are in [ORCHESTRATION.md](./ORCHESTRATION.md).

Tested with Airflow 3.3.2 on Python 3.12. Airflow 2 is not supported: the DAG
uses Airflow 3's `airflow.sdk`.

---

## 1. A credential for the pipeline

Skip this if your server runs without `--auth-file`.

```bash
samyama auth-token pipeline
```

prints a line for the credential file and, once, the token. Add the line to
the file the server reads with `:roles=write` on the end -- a pipeline that
loads data has no business managing tenants or restoring snapshots:

```text
pipeline:<sha256 of the token>:roles=write
```

Keep the token for step 3. The server stores only its digest.

## 2. Install, and put the files in the DAGs folder

```bash
pip install "apache-airflow==3.3.2" \
  -c https://raw.githubusercontent.com/apache/airflow/constraints-3.3.2/constraints-3.12.txt

cp examples/orchestration/samyama_airflow_dag.py \
   examples/orchestration/samyama_pipeline.py \
   "$AIRFLOW_HOME/dags/"
cp -r examples/orchestration/sample "$AIRFLOW_HOME/dags/"
```

Both `.py` files: the DAG imports `samyama_pipeline`, and Airflow puts the DAGs
folder on `sys.path`. `sample/` is the default input, found next to
`samyama_pipeline.py`; leave it out if every run will name its own files, and a
run that does not fails at once with `no such file`. Nothing else is needed --
`samyama_pipeline` is standard library only, and there is no Samyama provider
package to install.

## 3. A Connection to the server

The DAG reads the server from an Airflow Connection, `samyama_default` unless a
run says otherwise. `schema` is the protocol, the convention Airflow's own HTTP
hook uses; the token, if any, goes in `password`:

```bash
airflow connections add samyama_default \
  --conn-type http \
  --conn-host graph.internal \
  --conn-port 8080 \
  --conn-schema https \
  --conn-password "$SAMYAMA_TOKEN"
```

Any secrets backend Airflow supports works the same way, because the DAG asks
Airflow for the Connection rather than reading a variable of its own. As an
environment variable:

```bash
export AIRFLOW_CONN_SAMYAMA_DEFAULT='{"conn_type": "http", "host": "graph.internal", "port": 8080, "schema": "https", "password": "..."}'
```

Optional: `{"timeout_seconds": 60}` in the Connection's extras sets how long one
statement may take. The default is 30.

## 4. Run it

Once, without the scheduler, to see it work -- with the bundled sample:

```bash
airflow dags test samyama_accounts_transfers
```

With your own files:

```bash
airflow dags test samyama_accounts_transfers \
  -c '{"accounts_csv": "/data/accounts.csv", "transfers_csv": "/data/transfers.csv"}'
```

The same JSON is what you pass to `airflow dags trigger --conf`, or type into
the *Trigger DAG* form in the UI.

| Param | Default | |
|---|---|---|
| `samyama_conn_id` | `samyama_default` | the Connection to use |
| `accounts_csv` | the bundled sample | columns `id, holder, country, opened` |
| `transfers_csv` | the bundled sample | columns `id, src, dst, amount, at` |
| `batch_size` | `500` | rows per statement |

The DAG has `schedule=None`: it runs when triggered. Give it a schedule, or an
asset to wait on, when you deploy it -- that choice belongs to the deployment,
not to a reference.

## 5. When a run fails

The failing task's log names the cause. Two kinds:

- **Retried, with backoff, up to three times**: the server could not be
  reached, timed out, or answered 5xx.
- **Failed at once, no retry**: the server refused the statement or the token,
  the input is wrong (the message names the file and line), or validation found
  the graph disagrees with the files (the message names each check, what it
  expected and what it found).

A failed run can leave part of a load in the graph. Fix the cause and run it
again: every write is an upsert keyed on the source id, so the second run
completes the first rather than duplicating it.

## How this page is tested

`examples/orchestration/tests/test_airflow.py` runs the DAG through
`dag.test()` against a real server: a full load, a second run that must leave
the graph unchanged, a file naming an unknown account (one try, nothing
written), a token taken from the Connection, a missing token (one try), and a
Connection named by the `samyama_conn_id` param. The commands above were run as
written against a local server before this page was published; they are not
re-run by CI.
