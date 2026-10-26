"""The tool layer behind `kg ask`: the model picks tools, but the viewer and every query are fixed in code."""

import json

import pytest
from anthropic import beta_tool

from kg.ask import NOT_VISIBLE, make_tools

TOM, PIETER_DW = "User:005KS00000TOKAFOAA", "User:005KS00000PDEWITAA"
AURELIA, BRENNAN = "006KS00000AURQMSAA", "006KS00000BRNQMSAA"


def test_a_seller_only_finds_the_deals_the_crm_shows_them(built):
    g, as_of = built[0].graph, built[1]

    def names(viewer):
        return [o["account"] for o in json.loads(make_tools(g, as_of, viewer)["find_opportunities"]())]

    assert sorted(names(TOM)) == ["Aurelia Foods Benelux B.V.", "Brennan Packaging Ltd"]
    assert names(PIETER_DW) == ["Aurelia Foods Benelux B.V."]


def test_tools_refuse_a_deal_the_viewer_cannot_see(built):
    tools = make_tools(built[0].graph, built[1], PIETER_DW)
    for name in ("buying_committee", "stall_signals", "competitor_precedents"):
        assert tools[name](BRENNAN) == NOT_VISIBLE


def test_tools_return_the_catalogue_query_with_its_evidence(built):
    tools = make_tools(built[0].graph, built[1], TOM)
    stall = json.loads(tools["stall_signals"](AURELIA))
    assert stall["days_in_stage"] == 58 and "budget" not in stall["open_objections"]
    assert tools["stall_signals"]("006XXXXXXXXXXXXXXX").startswith("Error: unknown opportunity")


@pytest.mark.parametrize("name", ["find_opportunities", "buying_committee", "stall_signals", "competitor_precedents"])
def test_tool_schemas_are_strict_and_documented(built, name):
    schema = beta_tool(make_tools(built[0].graph, built[1], TOM)[name]).to_dict()
    assert schema["description"] and schema["input_schema"]["additionalProperties"] is False


def test_the_precedents_tool_returns_the_best_ten_and_the_total(built):
    result = json.loads(make_tools(built[0].graph, built[1], TOM)["competitor_precedents"](AURELIA))
    veritrack = result["competitors"][0]
    assert veritrack["precedents_total"] == 1 and len(veritrack["precedents"]) == 1
