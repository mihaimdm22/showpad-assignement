"""Graph builder: CRM records + validated, resolved extractions -> one labelled property graph.

Ontology v0 (the POC's subset of the ontology in docs/DESIGN.md §2.1):

  CRM layer         Account, Contact, User, Opportunity, Task, Competitor
                    Contact-WORKS_AT->Account, Opportunity-FOR_ACCOUNT->Account, Opportunity-OWNED_BY->User,
                    User-ON_DEAL_TEAM->Opportunity,
                    Contact-ON_COMMITTEE_OF{role, role_source:'crm'}->Opportunity, Opportunity-COMPETES_WITH->Competitor,
                    Opportunity-HAS_TASK->Task
  Lexical layer     Call, Turn            Call-ABOUT->Opportunity, Call-HAS_TURN->Turn, Contact-INVITED_TO->Call
  Mention layer     Mention{kind: speaker|person}, Person (provisional, not in CRM)
                    Turn-SPOKEN_BY->Mention, Mention-EVIDENCED_BY->Turn,
                    Mention-REFERS_TO{score, method}->Contact|User|Person, Mention-CANDIDATE{score}->Contact|User
  Claim layer       Assertion{predicate, value, modality, status, speaker_side, ...}, Concept{scheme}
                    Assertion-EVIDENCED_BY{quote, span}->Turn, Assertion-SUBJECT->Mention,
                    Assertion-OBJECT->Competitor|Concept

Claims are reified as Assertion nodes (not plain edges) so each carries its quote, modality,
status and provenance, and can be reviewed, superseded or deleted on its own.
"""

from __future__ import annotations

import hashlib
import json
import weakref
from dataclasses import dataclass
from datetime import datetime
from itertools import pairwise
from pathlib import Path

import networkx as nx

from kg.data import CrmBundle, TenantConfig, Transcript, vendor_key
from kg.resolve import CallResolution, fold, resolve_call
from kg.schema import CallExtraction
from kg.validate import ValidatedCall, validate_call

CRM_ROLE_MAP = {
    "Decision Maker": "decision_maker",
    "Economic Buyer": "economic_buyer",
    "Economic Decision Maker": "economic_buyer",
    "Technical Buyer": "technical_evaluator",
    "Business User": "end_user",
    "Evaluator": "influencer",
    "Influencer": "influencer",
    "Executive Sponsor": "champion",
}
SIDE = {"internal": "seller", "external": "customer", "unknown": "unknown"}
# The ontology as implemented: 12 node types and 16 edge types. docs/DESIGN.md §2.1 names the same set (a test checks both).
NODE_TYPES = {
    *("Account", "Contact", "User", "Opportunity", "Task", "Competitor"),
    *("Call", "Turn", "Mention", "Person", "Assertion", "Concept"),
}
EDGE_TYPES = {
    *("WORKS_AT", "FOR_ACCOUNT", "OWNED_BY", "ON_DEAL_TEAM", "ON_COMMITTEE_OF", "COMPETES_WITH", "HAS_TASK"),
    *("ABOUT", "HAS_TURN", "INVITED_TO", "SPOKEN_BY", "REFERS_TO", "CANDIDATE", "EVIDENCED_BY", "SUBJECT", "OBJECT"),
}


@dataclass
class CallReport:
    validated: ValidatedCall
    resolution: CallResolution
    meta: dict


@dataclass
class BuildResult:
    graph: nx.MultiDiGraph
    reports: dict[str, CallReport]


# A per-graph counter bumped whenever this module adds or removes nodes. Indexes derived from the graph (the claims-by-call
# index in queries.py) key on it, so they notice a reprocessed call even when the node count comes back the same.
_VERSIONS: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()


def graph_version(g: nx.MultiDiGraph) -> int:
    return _VERSIONS.get(g, 0)


def _changed(g: nx.MultiDiGraph) -> None:
    _VERSIONS[g] = _VERSIONS.get(g, 0) + 1


def _node(g: nx.MultiDiGraph, uid: str, label: str, **props) -> str:
    if uid in g:
        g.nodes[uid].update(props)
    else:
        g.add_node(uid, label=label, **props)
        _changed(g)
    return uid


def _edge(g: nx.MultiDiGraph, src: str, dst: str, etype: str, **props) -> None:
    g.add_edge(src, dst, key=etype, type=etype, **props)


def _hash(*parts: str) -> str:
    return hashlib.sha1("|".join(parts).encode()).hexdigest()[:12]


def load_crm_layer(g: nx.MultiDiGraph, crm: CrmBundle, cfg: TenantConfig) -> None:
    t = crm.tenant_id
    for c in cfg.competitors:
        _node(g, f"Competitor:{c.slug}", "Competitor", tenant_id=t, slug=c.slug, name=c.name, status="committed")
    for th in cfg.pain_themes:
        _node(g, f"Concept:pain_theme/{th.slug}", "Concept", tenant_id=t, scheme="pain_theme", slug=th.slug, name=th.label)
    for a in crm.records("Account"):
        _node(
            g,
            f"Account:{a['Id']}",
            "Account",
            tenant_id=t,
            name=a["Name"],
            industry=a.get("Industry"),
            country=a.get("BillingCountry"),
        )
    for u in crm.records("User"):
        _node(g, f"User:{u['Id']}", "User", tenant_id=t, name=u["Name"], title=u.get("Title"), email=u.get("Email"))
    for c in crm.records("Contact"):
        uid = _node(
            g,
            f"Contact:{c['Id']}",
            "Contact",
            tenant_id=t,
            name=f"{c['FirstName']} {c['LastName']}",
            title=c.get("Title"),
            email=c.get("Email"),
        )
        _edge(g, uid, f"Account:{c['AccountId']}", "WORKS_AT")

    history = crm.records("OpportunityHistory")
    for o in crm.records("Opportunity"):
        rows = sorted((h for h in history if h["OpportunityId"] == o["Id"]), key=lambda h: h["CreatedDate"])
        pushes = sum(
            1 for prev, cur in pairwise(rows) if cur["StageName"] == prev["StageName"] and cur["CloseDate"] > prev["CloseDate"]
        )
        stage_rows = [h for h in rows if h["StageName"] == o["StageName"]]
        stage_entered = stage_rows[0]["CreatedDate"] if stage_rows else o.get("LastStageChangeDate")
        uid = _node(
            g,
            f"Opportunity:{o['Id']}",
            "Opportunity",
            tenant_id=t,
            name=o["Name"],
            stage=o["StageName"],
            amount=o.get("Amount"),
            currency=o.get("CurrencyIsoCode"),
            close_date=o.get("CloseDate"),
            is_closed=o.get("IsClosed", False),
            is_won=o.get("IsWon", False),
            next_step=o.get("NextStep"),
            stage_entered_at=stage_entered,
            close_date_pushes=pushes,
        )
        _edge(g, uid, f"Account:{o['AccountId']}", "FOR_ACCOUNT")
        _edge(g, uid, f"User:{o['OwnerId']}", "OWNED_BY")
    for r in crm.records("OpportunityContactRole"):
        _edge(
            g,
            f"Contact:{r['ContactId']}",
            f"Opportunity:{r['OpportunityId']}",
            "ON_COMMITTEE_OF",
            role=CRM_ROLE_MAP.get(r["Role"], "other"),
            crm_role=r["Role"],
            is_primary=r.get("IsPrimary", False),
            role_source="crm",
        )
    for tm in crm.records("OpportunityTeamMember"):  # deal team: drives who may see the account (with the owner)
        _edge(g, f"User:{tm['UserId']}", f"Opportunity:{tm['OpportunityId']}", "ON_DEAL_TEAM", role=tm.get("TeamMemberRole"))
    for oc in crm.records("OpportunityCompetitor"):
        slug = cfg.competitor_slug(oc["CompetitorName"]) or fold(oc["CompetitorName"]).replace(" ", "_")
        _node(g, f"Competitor:{slug}", "Competitor", tenant_id=t, slug=slug, name=oc["CompetitorName"])
        _edge(
            g,
            f"Opportunity:{oc['OpportunityId']}",
            f"Competitor:{slug}",
            "COMPETES_WITH",
            source="crm",
            strengths=oc.get("Strengths"),
        )
    for task in crm.records("Task"):
        uid = _node(
            g,
            f"Task:{task['Id']}",
            "Task",
            tenant_id=t,
            subject=task["Subject"],
            due_date=task.get("ActivityDate"),
            status=task.get("Status"),
            owner=f"User:{task['OwnerId']}" if task.get("OwnerId") else None,
        )
        if task.get("WhatId", "").startswith("006"):
            _edge(g, f"Opportunity:{task['WhatId']}", uid, "HAS_TASK")


def load_call_layer(g: nx.MultiDiGraph, crm: CrmBundle, t: Transcript, v: ValidatedCall, r: CallResolution, meta: dict) -> None:
    tid = t.tenant_id
    call = _node(
        g,
        f"Call:{t.call_id}",
        "Call",
        tenant_id=tid,
        title=t.title,
        started_at=t.started_at.isoformat(),
        model=meta.get("model_served"),
        prompt_version=meta.get("prompt_version"),
    )
    if opp := t.crm_refs.get("opportunity_id"):
        _edge(g, call, f"Opportunity:{opp}", "ABOUT", method="crm_event", confidence=1.0)
    event = crm.by_id("Event", t.crm_refs.get("event_id", "")) or {}
    for who in event.get("EventWhoIds", []):
        _edge(g, f"Contact:{who}", call, "INVITED_TO")

    participants = {p.speaker_id: p for p in t.participants}
    speaker_node = {}
    for p in t.participants:
        uid = _node(
            g,
            f"Mention:{t.call_id}/{p.speaker_id}",
            "Mention",
            tenant_id=tid,
            kind="speaker",
            surface=p.display_name,
            speaker_id=p.speaker_id,
            side=SIDE[p.affiliation],
        )
        speaker_node[p.speaker_id] = uid
    for i, u in enumerate(t.turns):
        turn = _node(
            g,
            f"Turn:{t.call_id}/{u.turn_id}",
            "Turn",
            tenant_id=tid,
            call_id=t.call_id,
            turn_id=u.turn_id,
            idx=i,
            speaker_id=u.speaker_id,
            start_ms=u.start_ms,
            text=u.text,
        )
        _edge(g, call, turn, "HAS_TURN")
        _edge(g, turn, speaker_node[u.speaker_id], "SPOKEN_BY")

    mention_node = {}
    for m in v.mentions:
        uid = _node(
            g,
            f"Mention:{t.call_id}/{m.mention_id}",
            "Mention",
            tenant_id=tid,
            kind="person",
            surface=m.surface,
            mention_type=m.mention_type,
            side=m.side,
            role_hint=m.role_hint,
            call_id=t.call_id,
            turn_id=m.turn_id,
        )
        _edge(g, uid, f"Turn:{t.call_id}/{m.turn_id}", "EVIDENCED_BY", start=m.span[0], end=m.span[1])
        mention_node[m.mention_id] = uid

    def provisional(name: str, role_hint: str = "", aliases: list[str] | None = None) -> str:
        uid = f"Person:{tid}/{fold(name).replace(' ', '_')}"
        known = g.nodes[uid].get("aliases", []) if uid in g else []
        return _node(
            g,
            uid,
            "Person",
            tenant_id=tid,
            name=name,
            role_hint=role_hint,
            status="unresolved",
            aliases=list(dict.fromkeys(known + (aliases or [name]))),
        )

    def attach(mention_uid: str, link, fallback_name: str, role_hint: str = "", stranger=None) -> None:
        if link.decision == "linked":
            _edge(g, mention_uid, link.target, "REFERS_TO", score=link.score, method=link.method)
        elif link.decision == "candidate":
            for target, score in link.candidates:
                g.add_edge(mention_uid, target, key=f"CANDIDATE:{target}", type="CANDIDATE", score=score)
        elif stranger is not None:  # several not-in-CRM mentions merged into one person
            person = provisional(stranger.name, stranger.role_hint, stranger.aliases)
            _edge(g, mention_uid, person, "REFERS_TO", score=0.0, method="merged_stranger")
        else:
            _edge(g, mention_uid, provisional(fallback_name, role_hint), "REFERS_TO", score=0.0, method="nil_provisional")

    for m in v.mentions:
        attach(mention_node[m.mention_id], r.mentions[m.mention_id], m.surface, m.role_hint, r.strangers.get(m.mention_id))
    # An unidentified speaker who introduced themself shares the self-introduction's referent.
    self_intro = {m.self_speaker_id: m for m in v.mentions if m.mention_type == "self_introduction" and m.self_speaker_id}
    for spk, link in r.speakers.items():
        if link.decision == "nil" and spk in self_intro:
            m = self_intro[spk]
            target = next((d for _, d, k in g.out_edges(mention_node[m.mention_id], keys=True) if k == "REFERS_TO"), None)
            if target:
                _edge(g, speaker_node[spk], target, "REFERS_TO", score=0.0, method="self_introduction")
                continue
        attach(speaker_node[spk], link, participants[spk].display_name)

    for it in v.items:
        subject = it.subject_ref
        key = _hash(t.call_id, it.predicate, it.turn_id, it.value, subject, it.attrs.get("competitor_surface", ""))
        speaker = t.turn(it.turn_id).speaker_id
        a = _node(
            g,
            f"Assertion:{key}",
            "Assertion",
            tenant_id=tid,
            predicate=it.predicate,
            value=it.value,
            modality=it.modality,
            support=it.support,
            status=it.status,
            flags=list(it.flags),
            speaker_side=SIDE[participants[speaker].affiliation],
            call_id=t.call_id,
            turn_id=it.turn_id,
            source=it.source,
            model=meta.get("model_served"),
            prompt_version=meta.get("prompt_version"),
            **{k: v_ for k, v_ in it.attrs.items() if v_ not in (None, "")},
        )
        _edge(g, a, f"Turn:{t.call_id}/{it.turn_id}", "EVIDENCED_BY", quote=it.quote, start=it.span[0], end=it.span[1])
        if subject:
            _edge(g, a, speaker_node.get(subject) or mention_node[subject], "SUBJECT")
        if it.predicate == "competitor_stance":
            slug = it.attrs.get("competitor")
            target = (
                f"Competitor:{slug}"
                if slug
                else _node(
                    g,
                    f"Competitor:candidate/{vendor_key(it.attrs['competitor_surface']).replace(' ', '_')}",
                    "Competitor",
                    tenant_id=tid,
                    name=it.attrs["competitor_surface"],
                    status="candidate",
                )
            )
            _edge(g, a, target, "OBJECT")
        elif it.predicate == "pain_point":
            slug = it.value if f"Concept:pain_theme/{it.value}" in g else "other"
            _edge(
                g,
                a,
                _node(
                    g,
                    f"Concept:pain_theme/{slug}",
                    "Concept",
                    tenant_id=tid,
                    scheme="pain_theme",
                    slug=slug,
                    name=slug.replace("_", " "),
                ),
                "OBJECT",
            )
        elif it.predicate == "objection":
            _edge(
                g,
                a,
                _node(
                    g,
                    f"Concept:objection_category/{it.value}",
                    "Concept",
                    tenant_id=tid,
                    scheme="objection_category",
                    slug=it.value,
                    name=it.value.replace("_", " "),
                ),
                "OBJECT",
            )


def build_graph(
    crm: CrmBundle, cfg: TenantConfig, transcripts: list[Transcript], run: dict[str, tuple[dict, CallExtraction]]
) -> BuildResult:
    g = nx.MultiDiGraph(tenant_id=crm.tenant_id, as_of=crm.export_ts.date().isoformat())
    load_crm_layer(g, crm, cfg)
    reports = {}
    for t in transcripts:
        if t.call_id not in run:
            continue
        meta, extraction = run[t.call_id]
        v = validate_call(extraction, t, cfg)
        r = resolve_call(crm, t, v)
        load_call_layer(g, crm, t, v, r, meta)
        reports[t.call_id] = CallReport(v, r, meta)
    merge_candidate_competitors(g)
    return BuildResult(g, reports)


def merge_candidate_competitors(g: nx.MultiDiGraph) -> None:
    """Merge an unknown competitor name into the one other name it is a word-prefix of
    ("Adaptive" -> "Adaptive Design Solutions"). Ambiguous prefixes (two possible targets) are left alone."""
    competitors = {n: vendor_key(d["name"]) for n, d in g.nodes(data=True) if d["label"] == "Competitor"}
    for n, key in list(competitors.items()):
        if g.nodes[n].get("status") != "candidate":
            continue
        longer = [m for m, k in competitors.items() if m != n and m in g and k.startswith(key + " ")]
        if len(longer) != 1:
            continue
        target = longer[0]
        for a in in_nodes(g, n, "OBJECT"):
            _edge(g, a, target, "OBJECT")
        g.nodes[target]["aliases"] = sorted({*g.nodes[target].get("aliases", []), g.nodes[n]["name"]})
        g.remove_node(n)
        _changed(g)


def remove_call(g: nx.MultiDiGraph, call_id: str) -> int:
    """Erase one call and everything derived from it: turns, mentions, claims, and the provisional people or candidate
    competitors only this call supported. CRM records and vocabularies stay. Serves deletion requests, and clears a call
    before it is re-extracted. Returns the number of nodes removed."""
    doomed = {f"Call:{call_id}"} | {
        n for n, d in g.nodes(data=True) if d.get("call_id") == call_id or n.startswith(f"Mention:{call_id}/")
    }
    g.remove_nodes_from(doomed)
    orphans = [
        n
        for n, d in g.nodes(data=True)
        if (d["label"] == "Person" or (d["label"] == "Competitor" and d.get("status") == "candidate")) and g.in_degree(n) == 0
    ]
    g.remove_nodes_from(orphans)
    _changed(g)
    return len(doomed) + len(orphans)


def export_json(g: nx.MultiDiGraph, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = nx.node_link_data(g, edges="edges")
    path.write_text(json.dumps(data, indent=1, default=lambda o: o.isoformat() if isinstance(o, datetime) else str(o)) + "\n")


# --- read helpers shared by queries and evaluation -------------------------------------------


def out_nodes(g: nx.MultiDiGraph, uid: str, etype: str) -> list[str]:
    return [d for _, d, k in g.out_edges(uid, keys=True) if k == etype]


def in_nodes(g: nx.MultiDiGraph, uid: str, etype: str) -> list[str]:
    return [s for s, _, k in g.in_edges(uid, keys=True) if k == etype]


def edge_props(g: nx.MultiDiGraph, src: str, dst: str, etype: str) -> dict:
    return g.get_edge_data(src, dst, key=etype) or {}


def referent(g: nx.MultiDiGraph, mention_uid: str) -> str | None:
    targets = out_nodes(g, mention_uid, "REFERS_TO")
    return targets[0] if targets else None
