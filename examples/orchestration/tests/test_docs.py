"""The Cypher the orchestration docs show is the Cypher the pipelines run.

CH-DOC-EXEC checks that a documented statement parses. It cannot see that the
statement on the page is no longer the one in `samyama_pipeline.py` -- and a
reader copies the page, not the module.
"""

from __future__ import annotations

import re

import pytest

import samyama_pipeline as sp
from conftest import REPO_ROOT

DOCS = ["ORCHESTRATION.md", "ORCHESTRATION-AIRFLOW.md", "ORCHESTRATION-DAGSTER.md"]


def normalise(text: str) -> str:
    return " ".join(text.split())


def pipeline_statements() -> set[str]:
    return {normalise(v) for k, v in vars(sp).items()
            if k.isupper() and isinstance(v, str) and ("MATCH" in v or "MERGE" in v)} | {
        normalise(s) for s in sp.SCHEMA_STATEMENTS}


@pytest.mark.parametrize("doc", DOCS)
def test_every_cypher_block_is_a_statement_the_pipeline_runs(doc):
    text = (REPO_ROOT / "docs" / doc).read_text(encoding="utf-8")
    blocks = re.findall(r"```cypher\n(.*?)```", text, flags=re.S)
    known = pipeline_statements()
    for block in blocks:
        assert normalise(block) in known, (
            f"docs/{doc} shows a statement samyama_pipeline.py does not run:\n{block}")


def test_the_overview_shows_at_least_one_statement():
    # Without this, deleting every block would pass the test above.
    text = (REPO_ROOT / "docs" / "ORCHESTRATION.md").read_text(encoding="utf-8")
    assert "```cypher" in text
