"""Stress test on third-party sales calls: SalesTranscriptQA (EndgameLabs), derived from Salesforce CRMArena-Pro.

Licence: CC BY-NC 4.0. The data is downloaded at runtime into .context/ (gitignored) and never committed.

What it tests, on calls we did not write:
  - a different seller, product and vocabulary (an un-configured tenant: no competitor list, generic pain themes)
  - a CRM slice rebuilt from the Salesforce IDs attached to each call (accounts, contacts, opportunities)
  - several calls per deal, so cross-call queries have something to join
  - ASR-style noise: the same call lower-cased, without punctuation, with fillers, anonymous speakers, a misspelled name

Usage:  uv run --with pyarrow python -I scripts/stress_test.py [--deals 4] [--per-deal 3] [--noisy 2] [--model claude-opus-5-5] [--effort high] [--prompt extract_v2]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import re
import time
import urllib.request
from collections import Counter, defaultdict
from datetime import datetime

import anthropic
from rapidfuzz import fuzz

from kg import queries
from kg.data import REPO_ROOT, Competitor, CrmBundle, PainTheme, TenantConfig, Transcript, vendor_key
from kg.extract import DEFAULT_MODEL, PROMPT_VERSION, cost_usd, extract_call
from kg.graph import build_graph
from kg.schema import CallExtraction

BASE = REPO_ROOT / ".context" / "external"
DATA = BASE / "stq"
RUNS = BASE / "stq-runs"
HF = "https://huggingface.co/datasets/EndgameLabs/SalesTranscriptQA/resolve/main/"
TENANT = "t_techpulse"
COMPETITION = re.compile(r"\b(competitor|competing|alternative vendor|currently using|switch(ed)? from|versus|vs\.)\b", re.I)
LINE = re.compile(r"^\[([^\]]+)\] ([^:]+): (.*)$")

# Written from the product description only (AI-powered EDA / PCB / power-optimisation software), not from the calls.
CONFIG = TenantConfig(
    tenant_id=TENANT,
    seller_company="TechPulse Solutions",
    product="AI-powered electronic design automation (EDA), PCB design and power-optimisation software",
    competitors=[],  # a new tenant on day one: every competitor name goes to review
    # Stand-in for the CRM product catalogue (Product2), which the dataset does not ship: names the sellers call "our X"
    # in at least 10 of the 4,033 corpus calls, minus generic phrases ("our AI-powered EDA") and one name the corpus also
    # uses for a competitor (Adaptive Design Solutions). In production this list is read from the CRM, not from calls.
    products=[
        "SecureFlow Suite",
        "AI Cirku-Tech",
        "CloudLink Designer",
        "PulseSim Pro",
        "EcoPCB Creator",
        "OptiPower Manager",
        "OptiPower Max",
        "DesignWave Automation",
        "AIOptics Vision",
        "CircuitAI Innovator",
        "AI DesignShift",
        "QuantumPCB Modeler",
        "CircuitMaster Analyzer",
        "OptiEnergy Suite",
        "SecureAnalytics Pro",
        "TrainEDU Suite",
        "SecureData Nexus",
        "EduFlow Academy",
        "EcoPower Convert",
        "DevVision IDE",
        "EduTech Lab",
        "EduTech Advance",
        "CloudInnovate Space",
        "SecureTrack Pro",
        "CollabCircuit Hub",
        "CircuitSync Pro",
        "CollabDesign Studio",
        "SimulateX Edge",
    ],
    pain_themes=[
        PainTheme(
            slug="integration_effort",
            label="Integration effort",
            description="Hard to integrate with existing tools, systems or workflows.",
        ),
        PainTheme(
            slug="support_quality",
            label="Support quality",
            description="Poor support, slow response or weak SLAs from a current provider.",
        ),
        PainTheme(
            slug="cost_pressure", label="Cost pressure", description="Budget limits, high cost or unclear return on investment."
        ),
        PainTheme(
            slug="scale_performance",
            label="Scale and performance",
            description="Current tools do not scale or perform as projects grow.",
        ),
        PainTheme(
            slug="security_compliance",
            label="Security and compliance",
            description="Data security, privacy or regulatory requirements.",
        ),
        PainTheme(slug="time_to_market", label="Time to market", description="Slow design or development cycles delay products."),
        PainTheme(
            slug="skills_adoption", label="Skills and adoption", description="Training, adoption or change-management burden."
        ),
    ],
)


def fetch() -> None:
    DATA.mkdir(parents=True, exist_ok=True)
    for name in ("b2b-corpus.parquet", "b2b-test.parquet", "LICENSE-DATA.txt", "README.md"):
        if not (DATA / name).exists():
            urllib.request.urlretrieve(HF + name, DATA / name)


def load_rows() -> tuple[list[dict], list[dict]]:
    import pyarrow.parquet as pq  # only needed here; installed on demand by `uv run --with pyarrow`

    return pq.read_table(DATA / "b2b-corpus.parquet").to_pylist(), pq.read_table(DATA / "b2b-test.parquet").to_pylist()


def safe_id(call_id: str) -> str:
    return "call_" + call_id.replace(":", "_")


def select(rows: list[dict], qa: list[dict], n_deals: int, per_deal: int) -> list[dict]:
    """Deterministic: deals (opportunities) with several calls, as the dataset's multi-call questions use them,
    one deal per account, preferring deals whose calls mention competition."""
    by_call = {r["call_id"]: r for r in rows}
    deals = sorted(
        {
            by_call[c]["metadata"]["opportunity_id"]
            for q in qa
            if q["question_class"] == "multi_call"
            for c in q["supporting_call_ids"]
            if c in by_call
        }
    )
    picked, accounts = [], set()
    for opp in deals:
        calls = sorted((r for r in rows if r["metadata"]["opportunity_id"] == opp), key=lambda r: r["metadata"]["created_at"])
        acc = calls[0]["metadata"]["account_id"]
        if len(calls) < 2 or acc in accounts or not any(COMPETITION.search(r["dialogue"]) for r in calls):
            continue
        picked += calls[:per_deal]
        accounts.add(acc)
        if len(accounts) == n_deals:
            break
    return picked


def to_transcript(r: dict, sellers: set[str], noisy: bool = False, rng: random.Random | None = None) -> Transcript:
    lines = [m.groups() for m in map(LINE.match, r["dialogue"].splitlines()) if m]
    t0 = datetime.fromisoformat(lines[0][0].strip())
    names = list(dict.fromkeys(name.strip() for _, name, _ in lines))
    label = {n: (f"Speaker {i + 1}" if noisy else n) for i, n in enumerate(names)}
    turns, last = [], -1
    for i, (ts, name, text) in enumerate(lines):
        start = max(int((datetime.fromisoformat(ts.strip()) - t0).total_seconds() * 1000), last + 1)
        last = start
        if noisy:
            text = add_noise(text, r["metadata"]["contact_name"], rng)
        turns.append(
            {
                "turn_id": f"u{i + 1:03d}",
                "speaker_id": f"spk_{names.index(name.strip()) + 1}",
                "start_ms": start,
                "end_ms": start + 1000,
                "text": text,
            }
        )
    for a, b in zip(turns, turns[1:], strict=False):
        a["end_ms"] = max(a["start_ms"] + 1, b["start_ms"] - 1)
    md = r["metadata"]
    participants = [
        {
            "speaker_id": f"spk_{i + 1}",
            "display_name": label[n],
            "email": None,
            # The call's CRM contact is the customer even when a seller elsewhere has the same name (the corpus reuses
            # names); everyone else is a seller if their name appears as a seller on any call of these accounts.
            "affiliation": "unknown" if noisy else ("external" if n == md["contact_name"] or n not in sellers else "internal"),
        }
        for i, n in enumerate(names)
    ]
    return Transcript.model_validate(
        {
            "tenant_id": TENANT,
            "call_id": safe_id(r["call_id"]) + ("_noisy" if noisy else ""),
            "title": md["opportunity_name"],
            "started_at": md["created_at"].replace("+0000", "+00:00"),
            "participants": participants,
            "turns": turns,
            "crm_refs": {"opportunity_id": md["opportunity_id"], "account_id": md["account_id"]},
        }
    )


def add_noise(text: str, contact_name: str, rng: random.Random) -> str:
    """Speech-to-text style damage: lower case, punctuation stripped, fillers, the contact's first name misspelled."""
    first = contact_name.split()[0]
    if len(first) > 3:
        text = re.sub(re.escape(first), first[0] + first[2] + first[1] + first[3:], text)  # swap two letters
    words = re.sub(r"[.,;:!?’']", "", text).lower().split()
    out = []
    for w in words:
        out.append(w)
        if rng.random() < 0.06:
            out.append(rng.choice(["um", "uh", "like", "you know"]))
    return " ".join(out)


def build_crm(rows: list[dict], accounts: set[str], sellers_by_opp: dict[str, Counter]) -> tuple[CrmBundle, dict[str, str]]:
    """CRM slice from the record IDs on every corpus call of the selected accounts (not only the sampled calls)."""
    acc, con, opp = {}, {}, {}
    for r in rows:
        md = r["metadata"]
        if md["account_id"] not in accounts:
            continue
        acc[md["account_id"]] = {"Id": md["account_id"], "Name": md["account_name"]}
        if md.get("contact_id"):
            first, _, last = md["contact_name"].partition(" ")
            con[md["contact_id"]] = {
                "Id": md["contact_id"],
                "AccountId": md["account_id"],
                "FirstName": first,
                "LastName": last,
                "Title": None,
                "Email": None,
            }
        opp[md["opportunity_id"]] = {"Id": md["opportunity_id"], "AccountId": md["account_id"], "Name": md["opportunity_name"]}
    users = {}
    for name in sorted({n for c in sellers_by_opp.values() for n in c}):
        users[name] = {
            "Id": "005STQ" + hashlib.sha1(name.encode()).hexdigest()[:12].upper(),
            "Name": name,
            "Title": "Sales Representative",
            "Email": None,
        }
    for o in opp.values():
        owner = sellers_by_opp.get(o["Id"], Counter()).most_common(1)
        o.update(
            {
                "OwnerId": users[owner[0][0]]["Id"] if owner else next(iter(users.values()))["Id"],
                "StageName": "Unknown",
                "IsClosed": False,
                "IsWon": False,
            }
        )
    objects = {
        "Account": list(acc.values()),
        "Contact": list(con.values()),
        "Opportunity": list(opp.values()),
        "User": list(users.values()),
    }
    return CrmBundle(tenant_id=TENANT, export_ts=datetime.now().isoformat(), objects=objects), {
        n: u["Id"] for n, u in users.items()
    }


def run_extractions(
    transcripts: list[Transcript], crm: CrmBundle, model: str, variant: str, cache_only: bool = False, effort: str = "high"
) -> dict:
    out_dir = RUNS / variant
    out_dir.mkdir(parents=True, exist_ok=True)
    client, run = anthropic.Anthropic(), {}
    for t in transcripts:
        path = out_dir / f"{t.call_id}.json"
        if not path.exists() and cache_only:  # an older prompt version is re-scored from its saved outputs, never re-run
            continue
        if not path.exists():
            record = extract_call(client, t, crm, CONFIG, model=model, effort=effort)
            path.write_text(json.dumps(record, indent=1, ensure_ascii=False))
            u = record["meta"]["usage"]
            print(f"  extracted {t.call_id}: {u['input_tokens']} in / {u['output_tokens']} out, {record['meta']['duration_s']} s")
        record = json.loads(path.read_text())
        run[t.call_id] = (record["meta"], CallExtraction.model_validate(record["extraction"]))
    return run


def duplicates(items) -> dict:
    """Over-extraction signals: one statement stored as two facts."""
    by_turn = defaultdict(set)
    for it in items:
        by_turn[it.turn_id].add(it.predicate)
    commitments = [it for it in items if it.predicate == "commitment"]
    repeated = sum(
        any(o.subject_ref == c.subject_ref and fuzz.token_set_ratio(o.value, c.value) >= 80 for o in commitments[:i])
        for i, c in enumerate(commitments)
    )
    roles = defaultdict(set)
    for it in items:
        if it.predicate == "committee_role":
            roles[it.subject_ref].add(it.value)
    return {
        "pain_and_objection_same_turn": sum({"pain_point", "objection"} <= p for p in by_turn.values()),
        "repeated_commitments": repeated,
        "economic_buyer_also_decision_maker": sum({"economic_buyer", "decision_maker"} <= r for r in roles.values()),
    }


def approval_simulation(crm, transcripts, run) -> dict:
    """Day 2 of a new tenant: an admin approves every competitor name the review queue proposed. How many deals then get
    a precedent from another account?"""
    g = build_graph(crm, CONFIG, transcripts, run).graph
    names = sorted({d["name"] for _, d in g.nodes(data=True) if d["label"] == "Competitor" and d.get("status") == "candidate"})
    approved = CONFIG.model_copy(
        update={"competitors": [Competitor(slug=vendor_key(n).replace(" ", "_"), name=n) for n in names]}
    )
    g = build_graph(crm, approved, transcripts, run).graph
    deals = {}
    for opp in queries.opportunities(g):
        found = [c for c in queries.competitor_precedents(g, opp)["competitors"] if c["precedents"]]
        if found:
            deals[g.nodes[opp]["name"]] = {c["name"]: len(c["precedents"]) for c in found}
    return {"approved_names": names, "deals_with_precedents": deals}


def report(transcripts, crm, run, rows_by_safe_id, qa, users_by_name, prompt_version) -> dict:
    transcripts = [t for t in transcripts if t.call_id in run]
    build = build_graph(crm, CONFIG, transcripts, run)
    g = build.graph
    per_call, totals = [], Counter()
    for t in transcripts:
        rep = build.reports[t.call_id]
        v, r, meta = rep.validated, rep.resolution, rep.meta
        items = v.items
        src = rows_by_safe_id[t.call_id.removesuffix("_noisy")]
        contact_uid = f"Contact:{src['metadata']['contact_id']}"
        speaker_targets = {spk: link.target for spk, link in r.speakers.items()}
        contact_spoke = contact_uid in speaker_targets.values()
        seller_ok = any(tg and tg.startswith("User:") for tg in speaker_targets.values())
        mention_decisions = Counter(link.decision for link in r.mentions.values())
        claims_turns = {it.turn_id for it in items}
        lines = src["dialogue"].splitlines()
        ev_turns = {
            f"u{i + 1:03d}"
            for q in qa
            for e in q["evidence"]
            if e["call_id"] == src["call_id"]
            for i in range(e["line_start"] - 1, e["line_end"])
            if 0 <= i < len(lines)
        }
        u = meta.get("usage", {})
        cost = cost_usd(meta.get("model_requested", DEFAULT_MODEL), u)
        row = {
            "call": t.call_id,
            "words": sum(len(x.text.split()) for x in t.turns),
            "claims": len(items),
            "by_type": dict(Counter(it.predicate for it in items)),
            "rejected": dict(Counter(x.reason for x in v.rejections)),
            "needs_review": sum(it.status == "needs_review" for it in items),
            "flags": dict(Counter(f for it in items for f in it.flags)),
            "modality": dict(Counter(it.modality for it in items)),
            "unknown_competitors": sorted(
                {it.attrs.get("competitor_surface") for it in items if "unknown_competitor" in it.flags}
            ),
            "themes_other": sum(it.predicate == "pain_point" and it.value == "other" for it in items),
            "seller_speaker_linked": seller_ok,
            "contact_speaker_linked": contact_spoke,
            "mentions": dict(mention_decisions),
            "qa_evidence_turns": len(ev_turns),
            "qa_evidence_turns_with_claims": len(ev_turns & claims_turns),
            "cost_usd": round(cost, 4),
            "seconds": meta.get("duration_s"),
            **duplicates(items),
        }
        per_call.append(row)
        totals.update({k: row[k] for k in ("claims", "needs_review", "qa_evidence_turns", "qa_evidence_turns_with_claims")})
        totals.update({k: row[k] for k in duplicates([])})
        totals.update({f"rejected_{k}": n for k, n in row["rejected"].items()})
        totals.update(row["by_type"])
        totals["cost_usd_x10000"] += round(cost * 10000)
    # Cross-call, on clean calls only (a noisy copy is the same call, not a second one)
    clean_ids = {t.call_id for t in transcripts if not t.call_id.endswith("_noisy")}
    g = build_graph(
        crm, CONFIG, [t for t in transcripts if t.call_id in clean_ids], {k: v for k, v in run.items() if k in clean_ids}
    ).graph
    cross = []
    for opp in queries.opportunities(g):
        calls = queries.calls_about(g, opp)
        if len(calls) >= 2:
            cq1 = queries.committee(g, opp)
            cross.append(
                {
                    "opportunity": g.nodes[opp]["name"],
                    "calls": len(calls),
                    "members": len(cq1["members"]),
                    "gaps": [x["gap"] for x in cq1["gaps"]],
                }
            )
    clean = [t for t in transcripts if t.call_id in clean_ids]
    return {
        "model": next(iter(run.values()))[0].get("model_served"),
        "prompt_version": prompt_version,
        "calls": {"clean": len(clean_ids), "noisy": len(transcripts) - len(clean_ids)},
        "graph": {"nodes": g.number_of_nodes(), "edges": g.number_of_edges()},
        "per_call": per_call,
        "totals": dict(totals),
        "cross_call": cross,
        "approval_simulation": approval_simulation(crm, clean, {k: v for k, v in run.items() if k in clean_ids}),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--deals", type=int, default=4)
    ap.add_argument("--per-deal", type=int, default=3)
    ap.add_argument("--noisy", type=int, default=2)
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--effort", default="high", choices=["low", "medium", "high", "xhigh", "max"])
    ap.add_argument("--prompt", default=PROMPT_VERSION, help="an older version is re-scored from cached outputs only")
    args = ap.parse_args()

    from kg.cli import _load_env

    _load_env()  # ANTHROPIC_API_KEY from .env, as the CLI does
    fetch()
    rows, qa = load_rows()
    sample = select(rows, qa, args.deals, args.per_deal)
    accounts = {r["metadata"]["account_id"] for r in sample}
    sellers_by_opp: dict[str, Counter] = defaultdict(Counter)
    for r in rows:
        if r["metadata"]["account_id"] in accounts:
            contact = r["metadata"]["contact_name"]
            for m in map(LINE.match, r["dialogue"].splitlines()):
                if m and m.group(2).strip() != contact:
                    sellers_by_opp[r["metadata"]["opportunity_id"]][m.group(2).strip()] += 1
    crm, users_by_name = build_crm(rows, accounts, sellers_by_opp)
    sellers = set(users_by_name)
    print(
        f"Sample: {len(sample)} calls from {len(accounts)} accounts; CRM slice: {len(crm.records('Contact'))} contacts, "
        f"{len(crm.records('Opportunity'))} opportunities, {len(crm.records('User'))} sellers"
    )
    rng = random.Random(7)
    clean = [to_transcript(r, sellers) for r in sample]
    noisy = [to_transcript(r, sellers, noisy=True, rng=rng) for r in sample[: args.noisy]]
    started = time.monotonic()
    variant = f"{args.prompt}__{args.model}" + ("" if args.effort == "high" else f"__{args.effort}")
    run = run_extractions(clean + noisy, crm, args.model, variant, cache_only=args.prompt != PROMPT_VERSION, effort=args.effort)
    rows_by_safe_id = {safe_id(r["call_id"]): r for r in sample}
    result = report(clean + noisy, crm, run, rows_by_safe_id, qa, users_by_name, args.prompt)
    result["wall_seconds"] = round(time.monotonic() - started, 1)
    (BASE / f"stress-report__{variant}__{args.deals}deals.json").write_text(json.dumps(result, indent=1))
    print(
        json.dumps({k: result[k] for k in ("model", "calls", "graph", "totals", "cross_call", "approval_simulation")}, indent=1)
    )
    for row in result["per_call"]:
        print(row)


if __name__ == "__main__":
    main()
