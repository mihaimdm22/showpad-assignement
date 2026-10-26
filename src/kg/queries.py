"""Competency queries behind the use case (deterministic; no LLM at read time).

CQ1 committee             Who is on the buying committee, with what role, and which roles are missing or unengaged?
CQ2 stall                 Why is the opportunity stalled, and what is still open?
CQ3 competitor_precedents What did other accounts in this tenant say about the competitor this deal faces?

Fact rules applied everywhere: only `modality == asserted` claims count as facts; negated and
hypothetical claims are reported separately as "ruled out" / "conditional"; customer evidence
excludes seller-voiced claims; every answer carries its quote and turn.
"""

from __future__ import annotations

import weakref
from collections import defaultdict
from datetime import date

import networkx as nx
from rapidfuzz import fuzz

from kg.graph import edge_props, graph_version, in_nodes, out_nodes, referent


def opportunities(g: nx.MultiDiGraph) -> list[str]:
    return [n for n, d in g.nodes(data=True) if d["label"] == "Opportunity"]


def calls_about(g: nx.MultiDiGraph, opp: str) -> list[str]:
    return in_nodes(g, opp, "ABOUT")


def _call_ids(g: nx.MultiDiGraph, opp: str) -> set[str]:
    return {c.split(":", 1)[1] for c in calls_about(g, opp)}


# Claims grouped by call, built once per graph and rebuilt when the graph changes: its node count (cheap; counting edges
# would walk them all) or its version, which graph.py bumps on every node it adds or removes (a reprocessed call can
# leave the count unchanged). A full scan per lookup was fine on two calls and took hours on the 3,454-call stress
# test: a graph store answers this from an index on call_id.
_CLAIMS_BY_CALL: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()


def _claims_by_call(g: nx.MultiDiGraph) -> dict[str, list[str]]:
    size = (g.number_of_nodes(), graph_version(g))
    cached = _CLAIMS_BY_CALL.get(g)
    if cached is None or cached[0] != size:
        index = defaultdict(list)
        for n, d in g.nodes(data=True):
            if d["label"] == "Assertion":
                index[d["call_id"]].append(n)
        cached = _CLAIMS_BY_CALL[g] = (size, index)
    return cached[1]


def _assertions(g: nx.MultiDiGraph, call_ids: set[str], predicate: str | None = None) -> list[tuple[str, dict]]:
    index = _claims_by_call(g)
    return [
        (n, g.nodes[n])
        for c in sorted(call_ids)
        for n in index.get(c, [])
        if predicate is None or g.nodes[n]["predicate"] == predicate
    ]


def evidence(g: nx.MultiDiGraph, a: str) -> dict:
    turn = out_nodes(g, a, "EVIDENCED_BY")[0]
    t = g.nodes[turn]
    speaker = referent(g, out_nodes(g, turn, "SPOKEN_BY")[0])
    return {
        "call_id": t["call_id"],
        "turn_id": t["turn_id"],
        "quote": edge_props(g, a, turn, "EVIDENCED_BY")["quote"],
        "speaker": g.nodes[speaker]["name"] if speaker else "unknown",
        "at": f"{t['start_ms'] // 60000:02d}:{t['start_ms'] // 1000 % 60:02d}",
    }


def subject_entity(g: nx.MultiDiGraph, a: str) -> str | None:
    subj = out_nodes(g, a, "SUBJECT")
    return referent(g, subj[0]) if subj else None


def _name(g: nx.MultiDiGraph, uid: str | None) -> str:
    return g.nodes[uid].get("name", uid) if uid else "unresolved"


def committee(g: nx.MultiDiGraph, opp: str) -> dict:
    calls = calls_about(g, opp)
    call_ids = _call_ids(g, opp)
    members: dict[str, dict] = defaultdict(
        lambda: {"crm_role": None, "transcript_roles": [], "spoke_on_calls": 0, "invited_to_calls": 0, "mentioned": 0}
    )
    for contact in in_nodes(g, opp, "ON_COMMITTEE_OF"):
        members[contact]["crm_role"] = edge_props(g, contact, opp, "ON_COMMITTEE_OF")["crm_role"]
    for a, d in _assertions(g, call_ids, "committee_role"):
        who = subject_entity(g, a)
        if d["modality"] == "asserted" and who and g.nodes[who]["label"] != "User":
            members[who]["transcript_roles"].append({"role": d["value"], "status": d["status"], **evidence(g, a)})
    for call in calls:
        speakers = {referent(g, m) for t in out_nodes(g, call, "HAS_TURN") for m in out_nodes(g, t, "SPOKEN_BY")}
        for who in speakers:
            if who and g.nodes[who]["label"] in ("Contact", "Person"):
                members[who]["spoke_on_calls"] += 1
        for who in in_nodes(g, call, "INVITED_TO"):
            members[who]["invited_to_calls"] += 1
    for n, d in g.nodes(data=True):
        if d["label"] == "Mention" and d.get("kind") == "person" and d.get("call_id") in call_ids:
            who = referent(g, n)
            if who and g.nodes[who]["label"] in ("Contact", "Person"):
                members[who]["mentioned"] += 1

    out, gaps = {}, []
    for uid, m in members.items():
        node = g.nodes[uid]
        roles = sorted({r["role"] for r in m["transcript_roles"]})
        out[uid] = {
            "name": node.get("name"),
            "title": node.get("title") or node.get("role_hint"),
            "in_crm": node["label"] == "Contact",
            **m,
            "roles": roles,
        }
    buyers = [u for u, m in out.items() if "economic_buyer" in m["roles"] or m["crm_role"] == "Economic Buyer"]
    if not buyers:
        gaps.append({"gap": "no_economic_buyer_identified"})
    for u in buyers:
        if out[u]["spoke_on_calls"] == 0:
            gaps.append({"gap": "economic_buyer_unengaged", "who": u, "name": out[u]["name"]})
    for u, m in out.items():
        if m["in_crm"] and m["roles"] and m["crm_role"] is None:
            gaps.append({"gap": "missing_crm_contact_role", "who": u, "name": m["name"], "suggested_roles": m["roles"]})
        if not m["in_crm"]:
            aliases = [a for a in g.nodes[u].get("aliases", []) if a != m["name"]]
            gaps.append({"gap": "person_not_in_crm", "who": u, "name": m["name"], "hint": m["title"], "aliases": aliases})
    return {"opportunity": opp, "members": out, "gaps": gaps}


def stall(g: nx.MultiDiGraph, opp: str, as_of: date) -> dict:
    o = g.nodes[opp]
    call_ids = _call_ids(g, opp)
    entered = date.fromisoformat(o["stage_entered_at"][:10]) if o.get("stage_entered_at") else None  # CRMs may lack stage history
    objections = defaultdict(list)
    ruled_out, conditional = [], []
    for a, d in _assertions(g, call_ids, "objection"):
        if d.get("speaker_side") == "seller":
            continue
        item = {"category": d["value"], "summary": d.get("summary"), "status": d["status"], **evidence(g, a)}
        if d["modality"] == "asserted":
            objections[d["value"]].append(item)
        elif d["modality"] == "negated":
            ruled_out.append(item)
        else:
            conditional.append(item)
    tasks = [g.nodes[t] for t in out_nodes(g, opp, "HAS_TASK")]
    commitments = []
    for a, d in _assertions(g, call_ids, "commitment"):
        if d["modality"] != "asserted":
            continue
        owner = subject_entity(g, a)
        due = d.get("due_date")
        crm_task = next(
            (t for t in tasks if t.get("owner") == owner and fuzz.token_set_ratio(t["subject"], d["value"]) >= 60), None
        )
        commitments.append(
            {
                "owner": owner,
                "owner_name": _name(g, owner),
                "action": d["value"],
                "due_date": due,
                "explicitness": d.get("explicitness"),
                "status": d["status"],
                "overdue": bool(due) and date.fromisoformat(due) < as_of,
                "crm_task_status": crm_task["status"] if crm_task else None,
                **evidence(g, a),
            }
        )
    competitors = [
        {"competitor": g.nodes[out_nodes(g, a, "OBJECT")[0]]["name"], "stance": d["value"], **evidence(g, a)}
        for a, d in _assertions(g, call_ids, "competitor_stance")
        if d["modality"] == "asserted" and d.get("speaker_side") == "customer" and d["status"] == "committed"
    ]
    return {
        "opportunity": opp,
        "stage": o["stage"],
        "days_in_stage": (as_of - entered).days if entered else None,
        "close_date_pushes": o["close_date_pushes"],
        "close_date": o["close_date"],
        "open_objections": dict(objections),
        "ruled_out": ruled_out,
        "conditional": conditional,
        "open_commitments": sorted(commitments, key=lambda c: c["due_date"] or "9999"),
        "competitors": competitors,
    }


def _customer_fact(d: dict) -> bool:
    return d["modality"] == "asserted" and d.get("speaker_side") == "customer" and d["status"] == "committed"


def quote_in_context(g: nx.MultiDiGraph, a: str) -> str:
    """A claim's quote joined, in spoken order, with the customer's other facts from the same turn. One turn often holds
    two facts ("We ran Veritrack for a year at the Cork site. Implementation took nine months ... we pulled out."): the
    model files them as a competitor stance and a pain point, and a precedent that shows only the first loses the reason."""
    turn = out_nodes(g, a, "EVIDENCED_BY")[0]
    spans = {}
    for n, d in _assertions(g, {g.nodes[a]["call_id"]}):
        same_turn = out_nodes(g, n, "EVIDENCED_BY")[0] == turn
        if same_turn and (n == a or (_customer_fact(d) and d["predicate"] in ("pain_point", "objection", "competitor_stance"))):
            e = edge_props(g, n, turn, "EVIDENCED_BY")
            spans[(e["start"], e["end"])] = e["quote"]
    text, parts, end = g.nodes[turn]["text"], [], -1
    for (start, stop), quote in sorted(spans.items()):
        if start < end:  # overlaps a quote already shown: add only what it extends
            if stop > end:
                parts.append(text[end:stop])
                end = stop
            continue
        if parts:
            parts.append(" " if not text[end:start].strip() else " ... ")
        parts.append(quote)
        end = stop
    return "".join(parts)


def visible_accounts(g: nx.MultiDiGraph, user: str) -> set[str]:
    """Accounts a seller may see, mirroring CRM access: they own a deal on the account or sit on its deal team.
    (A stand-in for the CRM's real sharing rules, which production would sync rather than re-implement.)"""
    opps = set(in_nodes(g, user, "OWNED_BY")) | set(out_nodes(g, user, "ON_DEAL_TEAM"))
    return {acc for o in opps for acc in out_nodes(g, o, "FOR_ACCOUNT")}


def can_see(g: nx.MultiDiGraph, user: str, opp: str) -> bool:
    return any(acc in visible_accounts(g, user) for acc in out_nodes(g, opp, "FOR_ACCOUNT"))


def competitor_precedents(g: nx.MultiDiGraph, opp: str, viewer: str | None = None) -> dict:
    """Same-tenant precedents only. With a viewer, accounts they cannot see in the CRM are reported as a count,
    never by name or quote."""
    tenant = g.nodes[opp]["tenant_id"]
    own_calls = _call_ids(g, opp)
    visible = visible_accounts(g, viewer) if viewer else None
    in_play = set(out_nodes(g, opp, "COMPETES_WITH"))
    for a, d in _assertions(g, own_calls, "competitor_stance"):
        # Any competitor the customer brings up in this deal is worth a look-up, except one they ruled out. (the first version required
        # "evaluating"/"current vendor"; on third-party calls customers mostly compare, so nothing ever triggered.)
        # This only chooses what to look up; the evidence shown from other accounts must still be a committed fact.
        if d["modality"] == "asserted" and d.get("speaker_side") == "customer" and d["value"] != "rejected":
            in_play.update(out_nodes(g, a, "OBJECT"))
    own_pains = {out_nodes(g, a, "OBJECT")[0] for a, d in _assertions(g, own_calls, "pain_point") if _customer_fact(d)}

    results = []
    for comp in sorted(in_play):
        if g.nodes[comp].get("status") == "candidate":
            continue
        precedents, hidden = [], set()
        for a in in_nodes(g, comp, "OBJECT"):
            d = g.nodes[a]
            if d["label"] != "Assertion" or d["tenant_id"] != tenant or d["call_id"] in own_calls or not _customer_fact(d):
                continue
            call = f"Call:{d['call_id']}"
            other_opp = out_nodes(g, call, "ABOUT")[0]
            account = out_nodes(g, other_opp, "FOR_ACCOUNT")[0]
            if visible is not None and account not in visible:
                hidden.add(account)
                continue
            their_pains = {
                out_nodes(g, p, "OBJECT")[0]
                for p, pd in _assertions(g, _call_ids(g, other_opp), "pain_point")
                if _customer_fact(pd)
            }
            precedents.append(
                {
                    "claim": a,
                    "account": account,
                    "account_name": g.nodes[account]["name"],
                    "opportunity": other_opp,
                    "stage": g.nodes[other_opp]["stage"],
                    "stance": d["value"],
                    "sentiment": d.get("sentiment"),
                    "summary": d.get("summary"),
                    "call_date": g.nodes[call]["started_at"][:10],
                    "shared_pains": sorted(g.nodes[p]["slug"] for p in own_pains & their_pains),
                    **evidence(g, a),
                }
            )
        results.append(
            {"competitor": comp, "name": g.nodes[comp]["name"], "precedents": precedents, "hidden_accounts": len(hidden)}
        )
    return {"opportunity": opp, "competitors": results}


STANCE_ORDER = {"past_vendor": 0, "rejected": 1, "current_vendor": 2, "evaluating": 3, "mentioned_only": 4}


def top_precedents(precedents: list[dict], k: int = 3) -> list[dict]:
    """The precedents worth a seller's minute: one per account, ranked by most shared pains, then the most telling stance
    (left it, ruled it out, uses it), then the most recent. On the 3,454-call stress test a deal had 188 precedents at the
    median, so a brief must choose."""
    ranked = sorted(precedents, key=lambda p: p["call_date"], reverse=True)
    ranked.sort(key=lambda p: (-len(p["shared_pains"]), STANCE_ORDER.get(p["stance"], len(STANCE_ORDER))))
    best: dict[str, dict] = {}
    for p in ranked:
        best.setdefault(p["account"], p)
    return list(best.values())[:k]


def deal_brief(g: nx.MultiDiGraph, opp: str, as_of: date, viewer: str | None = None) -> str:
    """The brief a specific seller sees. Defaults to the deal owner; refuses sellers who cannot see the deal."""
    o = g.nodes[opp]
    viewer = viewer or out_nodes(g, opp, "OWNED_BY")[0]
    if not can_see(g, viewer, opp):
        return f"ACCESS DENIED: {g.nodes[viewer]['name']} cannot see '{o['name']}' in the CRM."
    cq1, cq2, cq3 = committee(g, opp), stall(g, opp, as_of), competitor_precedents(g, opp, viewer)
    amount = f"{o['currency']} {o['amount']:,.0f}" if o.get("amount") else ""
    lines = [
        f"DEAL BRIEF  {o['name']}  ({amount})  as of {as_of}  for {g.nodes[viewer]['name']}",
        f"Stage '{cq2['stage']}' for "
        + (f"{cq2['days_in_stage']} days" if cq2["days_in_stage"] is not None else "an unknown time (no stage history)")
        + f"; close date pushed {cq2['close_date_pushes']}x (now {cq2['close_date']}).",
        "",
    ]
    lines.append("Buying committee")
    for m in sorted(cq1["members"].values(), key=lambda m: m["name"] or ""):
        roles = ", ".join(m["roles"]) or "-"
        crm = m["crm_role"] or ("not in CRM" if not m["in_crm"] else "no CRM role")
        lines.append(
            f"  - {m['name']} ({m['title']}): transcript role {roles}; CRM {crm}; "
            f"spoke on {m['spoke_on_calls']} call(s), mentioned {m['mentioned']}x"
        )
    for gap in cq1["gaps"]:
        extra = f" -> add CRM role: {', '.join(gap['suggested_roles'])}" if gap.get("suggested_roles") else ""
        if gap["gap"] == "person_not_in_crm":
            also = f"; also called {', '.join(repr(a) for a in gap['aliases'])}" if gap.get("aliases") else ""
            extra = f" ({gap['hint'] or 'role unknown'}{also}) -> add to CRM"
        who = f": {gap['name']}" if gap.get("name") else ""
        lines.append(f"  ! {gap['gap'].replace('_', ' ')}{who}{extra}")
    lines += ["", "Open objections"]
    for cat, items in cq2["open_objections"].items():
        i = items[0]
        # A short quote can be a fragment ("Proving payback to group is."); the summary says what it means.
        summary = f"{i['summary']} " if i.get("summary") else ""
        lines.append(f'  - {cat}: {summary}Quote: "{i["quote"]}" ({i["speaker"]}, {i["call_id"]} {i["at"]})')
    for i in cq2["ruled_out"]:
        lines.append(f'  x ruled out {i["category"]}: "{i["quote"]}" ({i["speaker"]})')
    lines += ["", "Open commitments"]
    for c in cq2["open_commitments"]:
        flags = ["OVERDUE"] if c["overdue"] else []
        if c["crm_task_status"]:
            flags.append(f"CRM task: {c['crm_task_status']}")
        if c["explicitness"] == "hedged":
            flags.append("hedged")
        lines.append(f"  - {c['owner_name']}: {c['action']} (due {c['due_date'] or 'n/a'}) {' | '.join(flags)}".rstrip())
    lines += [
        "",
        "Competitor precedents in your company's other accounts (internal preparation only: never share with customers)",
    ]
    for comp in cq3["competitors"]:
        if not comp["precedents"] and not comp["hidden_accounts"]:
            lines.append(f"  - {comp['name']}: no precedents yet")
        if comp["hidden_accounts"]:
            lines.append(
                f"  - {comp['name']}: {comp['hidden_accounts']} more account(s) mention it; details restricted by your CRM access"
            )
        shown = top_precedents(comp["precedents"])
        for p in shown:
            shared = f"; shared pain: {', '.join(p['shared_pains'])}" if p["shared_pains"] else ""
            lines.append(
                f'  - {comp["name"]} @ {p["account_name"]} ({p["stance"]}, {p["sentiment"]}): "{quote_in_context(g, p["claim"])}" '
                f"({p['speaker']}, {p['call_date']}){shared}"
            )
        if more := len(comp["precedents"]) - len(shown):
            accounts = len({p["account"] for p in comp["precedents"]}) - len(shown)
            lines.append(f"  - {comp['name']}: {more} more precedent(s) from {accounts} more account(s); ask for the full list")
    return "\n".join(lines)
