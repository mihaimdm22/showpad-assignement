"""Shared fixture: the graph built from the hand-written ideal extraction (no LLM involved)."""

from pathlib import Path

import pytest

from kg.data import load_config, load_crm, load_transcripts
from kg.extract import load_run
from kg.graph import build_graph

FIXTURE = Path(__file__).parent / "fixtures" / "ideal_run"


@pytest.fixture(scope="module")
def built():
    crm = load_crm()
    return build_graph(crm, load_config(), load_transcripts(), load_run(FIXTURE)), crm.export_ts.date()
