# Loading Samyama from Dagster

How to run the reference assets,
[`examples/orchestration/samyama_dagster.py`](../examples/orchestration/samyama_dagster.py),
against your server and your files. What the pipeline does, the guarantees it
makes and what it does not do are in [ORCHESTRATION.md](./ORCHESTRATION.md).

Tested with Dagster 1.13.26 on Python 3.12.

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

## 2. Install, and put the files in your code location

```bash
pip install "dagster==1.13.26" "dagster-webserver==1.13.26"
cp examples/orchestration/samyama_dagster.py examples/orchestration/samyama_pipeline.py my_project/
cp -r examples/orchestration/sample my_project/
```

Both `.py` files: `samyama_dagster` imports `samyama_pipeline`, which is
standard library only. `sample/` is the default input, found next to
`samyama_pipeline.py`; leave it out if every run will name its own files. There
is no Samyama integration package to install.

## 3. Point it at the server

| Variable | |
|---|---|
| `SAMYAMA_URL` | required -- the server's HTTP endpoint, e.g. `https://graph.internal:8080` |
| `SAMYAMA_TOKEN` | the token from step 1; leave unset for a server without an auth file |

Both are read through `dg.EnvVar`, so the token is resolved when a run starts
and is not stored with the code location or shown in the UI.

## 4. Run it

```bash
export SAMYAMA_URL=http://localhost:8080
cd my_project
dagster dev -f samyama_dagster.py
```

and materialize the `samyama` group from the UI, or without the UI:

```bash
dagster asset materialize -f samyama_dagster.py --select '*'
```

The source files and batch size are the `source_files` resource. They default
to the bundled sample; point a run at your own in the launchpad's config:

```yaml
resources:
  source_files:
    config:
      accounts_csv: /data/accounts.csv
      transfers_csv: /data/transfers.csv
      batch_size: 500
```

| Asset | Depends on | Check |
|---|---|---|
| `samyama_schema` | | |
| `accounts` | `samyama_schema` | `accounts_match_source` |
| `transfers` | `accounts` | `transfers_match_source` |

The checks are **blocking**: if `accounts_match_source` fails, `transfers` is
not loaded on top of accounts that do not match their file. Each check's
metadata records, per condition, how many it expected and how many it found.

No schedule or sensor is defined; add one when you deploy it -- that choice
belongs to the deployment, not to a reference.

## 5. When a run fails

- **Retried, with exponential backoff, up to three times**: the server could
  not be reached, timed out, or answered 5xx.
- **Failed at once, no retry** (`Failure(allow_retries=False)`): the server
  refused the statement or the token, or the input is wrong -- the message
  names the file and line.
- **A check failed**: the graph disagrees with the files. The check's
  description names each condition that failed.

A failed run can leave part of a load in the graph. Fix the cause and
materialize again: every write is an upsert keyed on the source id, so the
second run completes the first rather than duplicating it.

## How this page is tested

`examples/orchestration/tests/test_dagster.py` materializes the assets and
checks against a real server: a full load with both checks passing, a second
run that must leave the graph unchanged, a file naming an unknown account (the
step fails, is not retried, writes nothing), a failing accounts check that must
stop `transfers` from loading, a token from the resource, a missing token, and
the shipped definitions reading `SAMYAMA_URL` and `SAMYAMA_TOKEN` from the
environment. The commands above were run as written against a local server
before this page was published; they are not re-run by CI.
