"""The command line end to end on the hand-written fixture: no API key, outputs written to a temporary folder."""

import shutil
import sys
from pathlib import Path

from kg import cli

FIXTURE = Path(__file__).parent / "fixtures" / "ideal_run"


def test_demo_builds_briefs_and_scores_the_fixture(tmp_path, monkeypatch, capsys):
    run = tmp_path / "run"
    shutil.copytree(FIXTURE, run)  # `kg eval` writes eval.json into the run it scores; never into the tracked fixture
    monkeypatch.setattr(cli, "OUT", tmp_path)
    monkeypatch.setattr(sys, "argv", ["kg", "demo", "--run", str(run)])
    cli.main()
    out = capsys.readouterr().out
    assert "DEAL BRIEF" in out and "EVALUATION" in out and "21/21 checks pass" in out
    assert (tmp_path / "graph.json").stat().st_size > 0 and (tmp_path / "graph.html").stat().st_size > 0


def test_a_seller_without_access_is_refused_the_brief(monkeypatch, capsys):
    monkeypatch.setattr(
        sys, "argv", ["kg", "brief", "--run", str(FIXTURE), "--opp", "006KS00000BRNQMSAA", "--as", "005KS00000PDEWITAA"]
    )
    cli.main()
    assert capsys.readouterr().out.startswith("ACCESS DENIED")


def test_without_a_run_argument_the_cli_uses_the_run_the_readme_quotes():
    assert cli._run_dir(None).name == cli.DEFAULT_RUN


def test_transcript_text_cannot_inject_markup_into_the_graph_view(tmp_path):
    import networkx as nx

    from kg.viz import write_html

    g = nx.MultiDiGraph()
    g.add_node("Call:c1", label="Call", title="c1")
    g.add_node("Turn:c1/u001", label="Turn")
    g.add_node(
        "Assertion:a1",
        label="Assertion",
        predicate="objection",
        value="price",
        modality="asserted",
        status="committed",
        speaker_side="customer",
        call_id="c1",
    )
    g.add_edge(
        "Assertion:a1", "Turn:c1/u001", key="EVIDENCED_BY", type="EVIDENCED_BY", quote="</script><script>alert(1)</script>"
    )
    write_html(g, tmp_path / "g.html")
    html = (tmp_path / "g.html").read_text()
    assert html.count("</script>") == 2 and "alert(1)" in html  # the CDN tag and the data block; the quote stays data
