# Orchestration reference pipelines

Loading Samyama from Airflow and Dagster (INT-10). Start with
[`docs/ORCHESTRATION.md`](../../docs/ORCHESTRATION.md).

| File | |
|---|---|
| `samyama_pipeline.py` | The load steps both pipelines run. Standard library only. |
| `samyama_airflow_dag.py` | Airflow 3 DAG — [how to run it](../../docs/ORCHESTRATION-AIRFLOW.md). |
| `samyama_dagster.py` | Dagster assets and checks — [how to run them](../../docs/ORCHESTRATION-DAGSTER.md). |
| `sample/` | Synthetic accounts and transfers, the default input. |
| `requirements.txt` | The orchestrator versions the tests were run against. |
| `tests/` | Runs both pipelines against a real server: `python -m pytest tests/`. |

The sample is not in `data/` because the repository ignores every directory of
that name.
