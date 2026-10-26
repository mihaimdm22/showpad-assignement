"""End-to-end test of the deterministic half (validate -> resolve -> graph -> queries -> eval) on a hand-written
ideal extraction. A real LLM run is scored by `kg eval`; this test pins the plumbing so it can't silently regress."""

from pathlib import Path

import networkx as nx

from kg import queries
from kg.data import REPO_ROOT, CrmBundle, load_config, load_crm, load_gold, load_transcripts
from kg.evaluate import evaluate
from kg.extract import load_run
from kg.graph import (
    EDGE_TYPES,
    NODE_TYPES,
    build_graph,
    load_call_layer,
    merge_candidate_competitors,
    out_nodes,
    referent,
    remove_call,
)

AURELIA = "Opportunity:006KS00000AURQMSAA"


def test_ideal_extraction_scores_perfectly(built):
    build, as_of = built
    r = evaluate(build, load_gold(), as_of)
    assert r["speakers"]["accuracy"] == 1.0
    assert r["mentions"]["wrong_links"] == 0
    assert r["facts"]["required_recall"] == 1.0
    assert r["facts"]["fp"] == 0
    assert r["must_not"]["committed_violations"] == 0
    assert r["cq"]["passed"] == r["cq"]["total"], [c for c in r["cq"]["checks"] if not c["ok"]]


def test_negated_budget_is_ruled_out_not_open(built):
    build, as_of = built
    s = queries.stall(build.graph, AURELIA, as_of)
    assert "budget" not in s["open_objections"]
    assert [i["category"] for i in s["ruled_out"]] == ["budget"]


def test_hypothetical_commitment_is_not_open(built):
    build, as_of = built
    actions = [c["action"] for c in queries.stall(build.graph, AURELIA, as_of)["open_commitments"]]
    assert not any("ninety days" in a for a in actions)


def test_overdue_commitment_is_matched_to_crm_task(built):
    build, as_of = built
    pack = next(c for c in queries.stall(build.graph, AURELIA, as_of)["open_commitments"] if "security pack" in c["action"])
    assert pack["overdue"] and pack["crm_task_status"] == "Not Started"


def test_seller_voiced_competitor_claim_is_never_customer_evidence(built):
    build, _ = built
    cited = {p["turn_id"] for c in queries.competitor_precedents(build.graph, AURELIA)["competitors"] for p in c["precedents"]}
    assert cited == {"u004"}


def test_unidentified_dial_in_and_self_introduction_share_one_provisional_person(built):
    g = built[0].graph
    speaker = referent(g, "Mention:call_aur_20261014/spk_6")
    assert g.nodes[speaker]["label"] == "Person" and g.nodes[speaker]["name"] == "Ravi"


def test_deal_brief_tells_the_story(built):
    build, as_of = built
    brief = queries.deal_brief(build.graph, AURELIA, as_of)
    for expected in ("economic buyer unengaged: Henrik Aalders", "ruled out budget", "Brennan Packaging Ltd", "58 days"):
        assert expected in brief


TOM, PIETER_DW = "User:005KS00000TOKAFOAA", "User:005KS00000PDEWITAA"


def test_precedents_follow_crm_visibility(built):
    """Everything stays inside one organisation, and inside it a seller only sees accounts the CRM lets them see."""
    build, as_of = built
    owner_view = queries.competitor_precedents(build.graph, AURELIA, viewer=TOM)["competitors"][0]
    assert [p["account_name"] for p in owner_view["precedents"]] == ["Brennan Packaging Ltd"]
    team_view = queries.competitor_precedents(build.graph, AURELIA, viewer=PIETER_DW)["competitors"][0]
    assert team_view["precedents"] == [] and team_view["hidden_accounts"] == 1
    brief = queries.deal_brief(build.graph, AURELIA, as_of, viewer=PIETER_DW)
    assert "Brennan" not in brief and "nine months" not in brief and "details restricted" in brief


def test_brief_is_refused_to_a_seller_who_cannot_see_the_deal(built):
    build, as_of = built
    brief = queries.deal_brief(build.graph, "Opportunity:006KS00000BRNQMSAA", as_of, viewer=PIETER_DW)
    assert brief.startswith("ACCESS DENIED")


def test_precedents_are_marked_internal_only(built):
    build, as_of = built
    assert "never share with customers" in queries.deal_brief(build.graph, AURELIA, as_of)


def test_brief_survives_an_opportunity_without_stage_history(built):
    build, as_of = built
    g = build.graph.copy()
    g.nodes[AURELIA]["stage_entered_at"] = None
    assert queries.stall(g, AURELIA, as_of)["days_in_stage"] is None
    assert "an unknown time" in queries.deal_brief(g, AURELIA, as_of)


def test_any_customer_mention_puts_a_competitor_in_play(built):
    """Not only "evaluating": a customer who merely compares to a vendor still triggers the precedent look-up."""
    g = built[0].graph.copy()
    g.remove_edge(AURELIA, "Competitor:veritrack", key="COMPETES_WITH")
    evaluating = next(
        n for n, d in g.nodes(data=True) if d.get("predicate") == "competitor_stance" and d.get("value") == "evaluating"
    )
    g.nodes[evaluating]["value"] = "mentioned_only"
    names = [c["name"] for c in queries.competitor_precedents(g, AURELIA)["competitors"]]
    assert names == ["Veritrack"]


def test_a_short_unknown_vendor_name_merges_into_the_one_longer_name_it_starts():
    g = nx.MultiDiGraph()
    for name in ("Adaptive", "Adaptive Design Solutions", "Adaptive Circuits"):
        g.add_node(f"Competitor:candidate/{name}", label="Competitor", name=name, status="candidate")
    g.add_node("Assertion:a1", label="Assertion")
    g.add_edge("Assertion:a1", "Competitor:candidate/Adaptive", key="OBJECT", type="OBJECT")
    merge_candidate_competitors(g)
    assert "Competitor:candidate/Adaptive" in g  # two longer names start with it: ambiguous, left alone

    g.remove_node("Competitor:candidate/Adaptive Circuits")
    merge_candidate_competitors(g)
    target = "Competitor:candidate/Adaptive Design Solutions"
    assert "Competitor:candidate/Adaptive" not in g and g.nodes[target]["aliases"] == ["Adaptive"]
    assert out_nodes(g, "Assertion:a1", "OBJECT") == [target]


BRENNAN_CALL = "call_brn_20260930"


def test_deleting_a_call_removes_everything_derived_from_it(built):
    """A deletion request (or a re-extraction) removes the call's turns, mentions, claims and the unknown people only it
    mentioned. CRM records stay."""
    g = built[0].graph.copy()
    assert remove_call(g, BRENNAN_CALL) > 0
    assert not [n for n, d in g.nodes(data=True) if d.get("call_id") == BRENNAN_CALL or BRENNAN_CALL in n]
    assert "Person:t_kestrel/chevonne" not in g and "Account:001KS00000BRNPKGAA" in g
    assert all(not c["precedents"] for c in queries.competitor_precedents(g, AURELIA)["competitors"])


def test_another_tenants_claims_never_surface(built):
    """A decoy tenant's customer says the same thing about the same competitor; it must never reach Kestrel's sellers."""
    g = built[0].graph.copy()
    decoy = {"tenant_id": "t_other"}
    g.add_node("Account:decoy", label="Account", name="Decoy Foods", **decoy)
    g.add_node("Opportunity:decoy", label="Opportunity", name="Decoy deal", stage="Proposal", **decoy)
    g.add_node("Call:decoy", label="Call", started_at="2026-10-01T09:00:00+00:00", **decoy)
    g.add_node("Turn:decoy/u001", label="Turn", call_id="decoy", **decoy)
    g.add_node(
        "Assertion:decoy",
        label="Assertion",
        call_id="decoy",
        predicate="competitor_stance",
        value="past_vendor",
        modality="asserted",
        speaker_side="customer",
        status="committed",
        **decoy,
    )
    g.add_edge("Opportunity:decoy", "Account:decoy", key="FOR_ACCOUNT", type="FOR_ACCOUNT")
    g.add_edge("Call:decoy", "Opportunity:decoy", key="ABOUT", type="ABOUT")
    g.add_edge("Assertion:decoy", "Turn:decoy/u001", key="EVIDENCED_BY", type="EVIDENCED_BY", quote="We dropped Veritrack.")
    g.add_edge("Assertion:decoy", "Competitor:veritrack", key="OBJECT", type="OBJECT")
    accounts = [p["account_name"] for c in queries.competitor_precedents(g, AURELIA)["competitors"] for p in c["precedents"]]
    assert accounts == ["Brennan Packaging Ltd"]
    assert "Decoy" not in queries.deal_brief(g, AURELIA, built[1])


def test_loading_the_same_call_twice_changes_nothing(built):
    build, _ = built
    g = build.graph.copy()
    before = (g.number_of_nodes(), g.number_of_edges())
    t = next(t for t in load_transcripts() if t.call_id == BRENNAN_CALL)
    report = build.reports[BRENNAN_CALL]
    load_call_layer(g, load_crm(), t, report.validated, report.resolution, report.meta)
    assert (g.number_of_nodes(), g.number_of_edges()) == before


def test_a_contact_added_to_the_crm_later_is_linked_without_re_extraction():
    """Ravi was not in the CRM. Once a seller adds him, rebuilding from the saved extraction links him: no LLM call."""
    crm = load_crm()
    objects = {k: list(v) for k, v in crm.objects.items()}
    objects["Contact"].append(
        {
            "Id": "003KS00000RAVIXXAA",
            "AccountId": "001KS00000AURBNLAA",
            "FirstName": "Ravi",
            "LastName": "Kumar",
            "Title": "Enterprise Architect",
            "Email": None,
        }
    )
    later = CrmBundle(tenant_id=crm.tenant_id, export_ts=crm.export_ts, objects=objects)
    run = load_run(Path(__file__).parent / "fixtures" / "ideal_run")
    g = build_graph(later, load_config(), load_transcripts(), run).graph
    assert referent(g, "Mention:call_aur_20261014/spk_6") == "Contact:003KS00000RAVIXXAA"


def test_the_graph_uses_exactly_the_documented_ontology(built):
    g = built[0].graph
    assert {d["label"] for _, d in g.nodes(data=True)} == NODE_TYPES
    assert {d["type"] for _, _, d in g.edges(data=True)} <= EDGE_TYPES  # the ideal run has no "not sure" CANDIDATE edges
    design = (REPO_ROOT / "docs" / "DESIGN.md").read_text()
    assert [t for t in sorted(NODE_TYPES | EDGE_TYPES) if f"`{t}`" not in design] == []


def test_a_brief_shows_the_most_useful_precedent_per_account_first():
    def p(account, stance, pains, date):
        return {"account": account, "stance": stance, "shared_pains": pains, "call_date": date}

    found = [
        p("A", "mentioned_only", [], "2026-10-01"),
        p("A", "past_vendor", ["audit_prep_effort"], "2026-05-01"),
        p("B", "current_vendor", [], "2026-09-01"),
        p("C", "past_vendor", [], "2026-08-01"),
        p("D", "evaluating", [], "2026-10-02"),
    ]
    top = queries.top_precedents(found, k=3)
    assert [(x["account"], x["stance"]) for x in top] == [("A", "past_vendor"), ("C", "past_vendor"), ("B", "current_vendor")]


def test_the_claim_index_notices_a_reprocessed_call_even_at_the_same_node_count(built):
    """Reprocessing removes a call and loads a new extraction of it. If the graph ends up the same size, a cached index
    of claims by call must still be rebuilt, or queries read the deleted claims."""
    from kg.graph import _node
    from kg.resolve import resolve_call
    from kg.validate import validate_call

    g = built[0].graph.copy()
    brennan = "Opportunity:006KS00000BRNQMSAA"
    for i in range(400):
        _node(g, f"Concept:filler/{i}", "Concept")
    queries.stall(g, brennan, built[1])  # builds the index at this size
    size = g.number_of_nodes()
    remove_call(g, BRENNAN_CALL)
    crm, t = load_crm(), next(t for t in load_transcripts() if t.call_id == BRENNAN_CALL)
    meta, extraction = load_run(REPO_ROOT / "runs" / "extract_v3__claude-opus-5-5")[BRENNAN_CALL]
    v = validate_call(extraction, t, load_config())
    load_call_layer(g, crm, t, v, resolve_call(crm, t, v), meta)  # a different extraction of the same call
    fillers = sorted(n for n in g if n.startswith("Concept:filler/"))
    g.remove_nodes_from(fillers[: g.number_of_nodes() - size])  # same node count as when the index was built
    assert g.number_of_nodes() == size
    claims = {n for n, d in g.nodes(data=True) if d["label"] == "Assertion" and d["call_id"] == BRENNAN_CALL}
    stalled = queries.stall(g, brennan, built[1])  # reads the index again: stale, it would hold deleted claims
    live_turns = {out_nodes(g, c, "EVIDENCED_BY")[0].split("/")[-1] for c in claims}
    assert {o["turn_id"] for objs in stalled["open_objections"].values() for o in objs} <= live_turns
