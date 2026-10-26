"""Questions in plain language: Claude picks queries from the catalogue as tools and answers from their results.

The model never writes a query and never chooses whose permissions apply. The viewer is fixed by the caller, and each
tool runs one deterministic query from `queries.py` with that viewer's CRM visibility. Answers can only cite what the
tools returned, so every statement traces back to a quote and a turn.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import date

import anthropic
import networkx as nx
from anthropic import beta_tool

from kg import queries
from kg.data import REPO_ROOT
from kg.extract import DEFAULT_MODEL, SERVER_FALLBACK_MODELS
from kg.graph import out_nodes

ASK_PROMPT_VERSION = "ask_v2"  # v2: text inside tool results is content, never an instruction
NOT_VISIBLE = "Error: this deal is not visible to you in the CRM."


def _dump(result: object) -> str:
    return json.dumps(result, default=str, ensure_ascii=False)


def make_tools(g: nx.MultiDiGraph, as_of: date, viewer: str | None = None) -> dict[str, Callable[..., str]]:
    """The tool functions, bound to one graph, date and viewer. Plain functions, so they are testable without the API."""

    def visible(opportunity_id: str) -> tuple[str, str]:
        """(graph ID, "") for a deal the viewer may see, else ("", the error to return to the model)."""
        uid = opportunity_id if opportunity_id.startswith("Opportunity:") else f"Opportunity:{opportunity_id}"
        if uid not in g or g.nodes[uid]["label"] != "Opportunity":
            return "", "Error: unknown opportunity ID; use find_opportunities to look it up."
        if viewer is not None and not queries.can_see(g, viewer, uid):
            return "", NOT_VISIBLE
        return uid, ""

    def find_opportunities(text: str = "") -> str:
        """List the deals you can see, optionally filtered by words in the deal or account name.

        Args:
            text: Words to look for in the deal or account name; empty lists every visible deal.
        """
        found = []
        for o in queries.opportunities(g):
            account = out_nodes(g, o, "FOR_ACCOUNT")[0]
            owner = out_nodes(g, o, "OWNED_BY")
            label = f"{g.nodes[o]['name']} {g.nodes[account]['name']}".casefold()
            if (viewer is None or queries.can_see(g, viewer, o)) and all(w in label for w in text.casefold().split()):
                found.append(
                    {
                        "opportunity_id": o.removeprefix("Opportunity:"),
                        "name": g.nodes[o]["name"],
                        "account": g.nodes[account]["name"],
                        "stage": g.nodes[o]["stage"],
                        "owner": g.nodes[owner[0]]["name"] if owner and owner[0] in g else None,
                        "is_closed": g.nodes[o]["is_closed"],
                    }
                )
        return _dump(found)

    def buying_committee(opportunity_id: str) -> str:
        """Who is on the deal's buying committee, with what role and evidence, and which roles are missing or unengaged.

        Args:
            opportunity_id: The CRM opportunity ID, for example 006KS00000AURQMSAA.
        """
        uid, error = visible(opportunity_id)
        return error or _dump(queries.committee(g, uid))

    def stall_signals(opportunity_id: str) -> str:
        """Why the deal may be stuck: time in stage, close-date pushes, open and ruled-out objections, open commitments.

        Args:
            opportunity_id: The CRM opportunity ID, for example 006KS00000AURQMSAA.
        """
        uid, error = visible(opportunity_id)
        return error or _dump(queries.stall(g, uid, as_of))

    def competitor_precedents(opportunity_id: str) -> str:
        """What customers in other accounts of this company said about the competitors this deal faces. Accounts you
        cannot see in the CRM are counted, never named.

        Args:
            opportunity_id: The CRM opportunity ID, for example 006KS00000AURQMSAA.
        """
        uid, error = visible(opportunity_id)
        if error:
            return error
        result = queries.competitor_precedents(g, uid, viewer=viewer)
        for c in result["competitors"]:  # a deal can have hundreds; the model gets the ten best and the count
            c["precedents_total"] = len(c["precedents"])
            c["precedents"] = queries.top_precedents(c["precedents"], 10)
        return _dump(result)

    return {f.__name__: f for f in (find_opportunities, buying_committee, stall_signals, competitor_precedents)}


def ask(
    question: str,
    g: nx.MultiDiGraph,
    as_of: date,
    viewer: str | None = None,
    model: str = DEFAULT_MODEL,
    effort: str = "medium",
) -> tuple[str, list[str]]:
    """Answer one question. Returns the answer and the tool calls made, for display and audit."""
    client = anthropic.Anthropic()
    who = g.nodes[viewer]["name"] if viewer and viewer in g else "an administrator (no CRM visibility filter)"
    system = (REPO_ROOT / "prompts" / f"{ASK_PROMPT_VERSION}.md").read_text() + f"\nToday is {as_of}. The seller is {who}.\n"
    fallback = {"betas": ["server-side-fallback-2026-07-01"], "fallbacks": "default"} if model in SERVER_FALLBACK_MODELS else {}
    runner = client.beta.messages.tool_runner(
        model=model,
        max_tokens=16000,
        system=system,
        tools=[beta_tool(f) for f in make_tools(g, as_of, viewer).values()],
        messages=[{"role": "user", "content": question}],
        output_config={"effort": effort},
        max_iterations=8,
        **fallback,
    )
    calls, final = [], None
    for message in runner:
        final = message
        calls += [f"{b.name}({json.dumps(b.input)})" for b in message.content if b.type == "tool_use"]
    # Only a finished turn is an answer: a refusal, a cut-off at max_tokens, or the tool loop hitting max_iterations
    # (stop_reason "tool_use") must not reach the seller as if it were complete.
    if final is None or final.stop_reason not in ("end_turn", "stop_sequence"):
        raise RuntimeError(f"no complete answer (stop_reason={final and final.stop_reason})")
    return "".join(b.text for b in final.content if b.type == "text"), calls
