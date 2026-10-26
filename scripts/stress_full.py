"""Full-corpus stress test: every SalesTranscriptQA call that carries CRM IDs, through the production configuration
(prompt v3, Haiku with Opus for calls whose candidate pool has a shared first name, prompt caching), then measurements
of the graph at that size.

Data: SalesTranscriptQA (Endgame Labs), from Salesforce CRMArena-Pro, CC BY-NC 4.0, downloaded into .context/ and never
committed. Outputs (raw extractions, failures, report) stay in .context/external/; only aggregate numbers are published.

Usage:  uv run --with pyarrow python -I scripts/stress_full.py [--limit 100] [--workers 16] [--report-only]
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import threading
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import anthropic

sys.path.insert(0, str(Path(__file__).parent))
import stress_test as st  # noqa: E402  (shared loaders: corpus, CRM slice, transcript conversion, tenant config)

from kg import queries  # noqa: E402
from kg.data import Competitor, vendor_key  # noqa: E402
from kg.extract import PROMPT_VERSION, cost_usd, escalation_reasons, extract_call  # noqa: E402
from kg.graph import build_graph, in_nodes  # noqa: E402
from kg.schema import CallExtraction  # noqa: E402


def linkable(rows: list[dict]) -> list[dict]:
    """Calls the CRM can place: account and opportunity IDs present. Ordered by time so --limit takes a stable prefix."""
    ok = [r for r in rows if r["metadata"].get("account_id") and r["metadata"].get("opportunity_id")]
    return sorted(ok, key=lambda r: (r["metadata"]["created_at"], r["call_id"]))


def tenant_crm(rows: list[dict]):
    accounts = {r["metadata"]["account_id"] for r in rows}
    sellers_by_opp: dict[str, Counter] = defaultdict(Counter)
    for r in rows:
        contact = r["metadata"]["contact_name"]
        for m in map(st.LINE.match, r["dialogue"].splitlines()):
            if m and m.group(2).strip() != contact:
                sellers_by_opp[r["metadata"]["opportunity_id"]][m.group(2).strip()] += 1
    return st.build_crm(rows, accounts, sellers_by_opp)


def extract_all(transcripts, crm, out_dir: Path, model: str, escalate_to: str, workers: int) -> list[dict]:
    """Concurrent, resumable extraction. A failed call is recorded with its error type, not retried forever: failures
    are one of the things this test measures."""
    out_dir.mkdir(parents=True, exist_ok=True)
    todo = [t for t in transcripts if not (out_dir / f"{t.call_id}.json").exists()]
    client = anthropic.Anthropic(max_retries=8)
    failures, lock, done, started = [], threading.Lock(), 0, time.monotonic()

    def one(t):
        reasons = escalation_reasons(t, crm)
        chosen = escalate_to if reasons else model
        try:
            record = extract_call(client, t, crm, st.CONFIG, model=chosen)
        except Exception as e:  # noqa: BLE001  (every failure kind is a result here)
            return {"call_id": t.call_id, "model": chosen, "error": type(e).__name__, "detail": str(e)[:300]}
        record["meta"]["escalated_for"] = reasons
        (out_dir / f"{t.call_id}.json").write_text(json.dumps(record, ensure_ascii=False))
        return None

    print(f"extracting {len(todo)} calls ({len(transcripts) - len(todo)} cached) with {workers} workers", flush=True)
    with ThreadPoolExecutor(workers) as pool:
        for fut in as_completed(pool.submit(one, t) for t in todo):
            failure = fut.result()
            with lock:
                done += 1
                if failure:
                    failures.append(failure)
                if done % 50 == 0 or done == len(todo):
                    rate = done / (time.monotonic() - started)
                    print(f"  {done}/{len(todo)} done, {len(failures)} failed, {rate * 60:.0f} calls/min", flush=True)
    with (out_dir / "failures.jsonl").open("a") as f:
        for x in failures:
            f.write(json.dumps(x) + "\n")
    return failures


def load(out_dir: Path, transcripts) -> dict:
    run = {}
    for t in transcripts:
        p = out_dir / f"{t.call_id}.json"
        if p.exists():
            record = json.loads(p.read_text())
            run[t.call_id] = (record["meta"], CallExtraction.model_validate(record["extraction"]))
    return run


def pct(values: list[float], q: float) -> float:
    return round(statistics.quantiles(values, n=100)[q - 1], 3) if len(values) > 1 else round(values[0], 3)


def query_latency(g, opps: list[str], as_of) -> dict:
    timings = defaultdict(list)
    for opp in opps:
        for name, fn in (
            ("committee", lambda o: queries.committee(g, o)),
            ("stall", lambda o: queries.stall(g, o, as_of)),
            ("competitor_precedents", lambda o: queries.competitor_precedents(g, o)),
            ("deal_brief", lambda o: queries.deal_brief(g, o, as_of)),
        ):
            t0 = time.perf_counter()
            fn(opp)
            timings[name].append(time.perf_counter() - t0)
    return {k: {"p50_s": pct(v, 50), "p95_s": pct(v, 95), "max_s": round(max(v), 3), "n": len(v)} for k, v in timings.items()}


def report(transcripts, crm, run, failures, qa, rows_by_id, latency_sample: int) -> dict:
    t0 = time.perf_counter()
    build = build_graph(crm, st.CONFIG, transcripts, run)
    build_s = time.perf_counter() - t0
    g, as_of = build.graph, crm.export_ts.date()

    metas = [m for m, _ in run.values()]
    by_model = Counter(m["model_requested"] for m in metas)
    cost = defaultdict(float)
    for m in metas:
        cost[m["model_requested"]] += cost_usd(m["model_requested"], m["usage"])
    usage = Counter()
    for m in metas:
        usage.update({k: v for k, v in m["usage"].items()})

    items = [it for r in build.reports.values() for it in r.validated.items]
    rejections = Counter(x.reason for r in build.reports.values() for x in r.validated.rejections)
    speakers, mentions = Counter(), Counter()
    for t in transcripts:
        rep = build.reports.get(t.call_id)
        if not rep:
            continue
        for p in t.participants:
            link = rep.resolution.speakers.get(p.speaker_id)
            side = "seller" if p.affiliation == "internal" else "customer"
            speakers[f"{side}_{'linked' if link and link.target else 'unlinked'}"] += 1
        mentions.update(link.decision for link in rep.resolution.mentions.values())

    labels = Counter(d["label"] for _, d in g.nodes(data=True))
    edge_types = Counter(d["type"] for _, _, d in g.edges(data=True))
    hubs = sorted(
        ((g.in_degree(n), d["label"], d.get("name") or d.get("slug") or n) for n, d in g.nodes(data=True)), reverse=True
    )

    candidates = Counter()  # customer-voiced claims per proposed competitor name (the tenant has no list yet)
    for n, d in g.nodes(data=True):
        if d["label"] == "Competitor" and d.get("status") == "candidate":
            claims = [a for a in in_nodes(g, n, "OBJECT") if g.nodes[a].get("speaker_side") == "customer"]
            if claims:
                candidates[d["name"]] = len(claims)
    themes = Counter(it.value for it in items if it.predicate == "pain_point")

    opps = [o for o in queries.opportunities(g) if queries.calls_about(g, o)]
    eb = sum(any(r.get("role") == "economic_buyer" for m in queries.committee(g, o)["members"].values() for r in m["transcript_roles"]) for o in opps)  # fmt: skip
    open_obj = Counter(cat for o in opps for cat in queries.stall(g, o, as_of)["open_objections"])

    claim_turns = {(it.call_id, it.turn_id) for it in items}
    ev = set()
    for q in qa:
        for e in q["evidence"]:
            r = rows_by_id.get(e["call_id"])
            if r is not None and st.safe_id(e["call_id"]) in run:
                ev |= {(st.safe_id(e["call_id"]), f"u{i + 1:03d}") for i in range(e["line_start"] - 1, e["line_end"])}

    # Day 2: an admin approves every competitor name customers raised in at least three calls.
    approved = sorted(n for n, k in candidates.items() if k >= 3)
    cfg2 = st.CONFIG.model_copy(
        update={"competitors": [Competitor(slug=vendor_key(n).replace(" ", "_"), name=n) for n in approved]}
    )
    g2 = build_graph(crm, cfg2, transcripts, run).graph
    with_prec, prec_total, comp_accounts = 0, 0, defaultdict(set)
    for o in opps:
        found = [c for c in queries.competitor_precedents(g2, o)["competitors"] if c["precedents"]]
        with_prec += bool(found)
        prec_total += sum(len(c["precedents"]) for c in found)
        for c in found:
            comp_accounts[c["name"]].update(p["account"] for p in c["precedents"])

    sample = opps[:: max(1, len(opps) // latency_sample)][:latency_sample]
    return {
        "calls": {
            "in_scope": len(transcripts),
            "extracted": len(run),
            "failed": len(failures),
            "failures_by_type": dict(Counter(f["error"] for f in failures)),
            "by_model": dict(by_model),
            "escalated": sum(bool(m.get("escalated_for")) for m in metas),
        },
        "cost_usd": {k: round(v, 2) for k, v in cost.items()} | {"total": round(sum(cost.values()), 2)},
        "cost_per_call_usd": round(sum(cost.values()) / max(1, len(run)), 4),
        "tokens": dict(usage),
        "cache_read_share_of_input": round(
            usage["cache_read_input_tokens"]
            / max(1, usage["input_tokens"] + usage["cache_read_input_tokens"] + usage["cache_creation_input_tokens"]),
            3,
        ),
        "seconds_per_call": {
            "p50": pct([m["duration_s"] for m in metas], 50),
            "p95": pct([m["duration_s"] for m in metas], 95),
        },
        "graph": {
            "nodes": g.number_of_nodes(),
            "edges": g.number_of_edges(),
            "build_seconds": round(build_s, 1),
            "nodes_by_type": dict(labels.most_common()),
            "edges_by_type": dict(edge_types.most_common()),
            "top_in_degree": [f"{lab} {name}: {deg}" for deg, lab, name in hubs[:8]],
        },
        "claims": {
            "kept": len(items),
            "per_call": round(len(items) / max(1, len(run)), 2),
            "by_predicate": dict(Counter(it.predicate for it in items)),
            "by_modality": dict(Counter(it.modality for it in items)),
            "needs_review": sum(it.status == "needs_review" for it in items),
            "flags": dict(Counter(f for it in items for f in it.flags)),
            "rejected": dict(rejections),
            "pain_theme_other_share": round(themes.get("other", 0) / max(1, sum(themes.values())), 3),
        },
        "entity_resolution": {
            "speakers": dict(speakers),
            "mentions": dict(mentions),
            "provisional_people": labels.get("Person", 0),
        },
        "vocabulary": {"candidate_competitors_distinct": len(candidates), "top": candidates.most_common(15)},
        "deals": {
            "opportunities_with_calls": len(opps),
            "economic_buyer_claimed": eb,
            "open_objections_by_category": dict(open_obj.most_common()),
        },
        "qa_evidence_coverage": {"turns": len(ev), "with_claims": len(ev & claim_turns)},
        "approval_simulation": {
            "approved_names": len(approved),
            "deals_with_precedents": with_prec,
            "precedents": prec_total,
            "top_competitors_by_accounts": sorted(((len(v), k) for k, v in comp_accounts.items()), reverse=True)[:10],
        },
        # Measured after approval, when competitors are in play and precedents exist (the expensive case).
        "query_latency_networkx": query_latency(g2, sample, as_of),
        "brief_lines": {
            "p50": pct([len(queries.deal_brief(g2, o, as_of).splitlines()) for o in sample], 50),
            "max": max(len(queries.deal_brief(g2, o, as_of).splitlines()) for o in sample),
        },
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="only the first N calls (a pilot)")
    ap.add_argument("--workers", type=int, default=16)
    ap.add_argument("--model", default="claude-haiku-5-5")
    ap.add_argument("--escalate-to", default="claude-opus-5-5")
    ap.add_argument("--report-only", action="store_true")
    ap.add_argument("--latency-sample", type=int, default=60)
    args = ap.parse_args()

    from kg.cli import _load_env

    _load_env()
    st.fetch()
    rows, qa = st.load_rows()
    rows = linkable(rows)
    crm, users = tenant_crm(rows)  # the whole tenant's CRM, even for a pilot
    scope = rows[: args.limit] if args.limit else rows
    sellers = set(users)
    transcripts = [st.to_transcript(r, sellers) for r in scope]
    print(
        f"tenant CRM: {len(crm.records('Account'))} accounts, {len(crm.records('Contact'))} contacts, "
        f"{len(crm.records('Opportunity'))} opportunities, {len(users)} sellers; calls in scope: {len(transcripts)}",
        flush=True,
    )
    out_dir = st.RUNS / f"{PROMPT_VERSION}__cascade-{args.model}-{args.escalate_to}"
    started = time.monotonic()
    failures = [] if args.report_only else extract_all(transcripts, crm, out_dir, args.model, args.escalate_to, args.workers)
    if args.report_only and (out_dir / "failures.jsonl").exists():
        done = {p.stem for p in out_dir.glob("call_*.json")}
        failures = [
            x
            for x in map(json.loads, (out_dir / "failures.jsonl").read_text().splitlines())
            if x["call_id"] not in done and any(t.call_id == x["call_id"] for t in transcripts)
        ]
        failures = list({x["call_id"]: x for x in failures}.values())
    extract_s = time.monotonic() - started
    run = load(out_dir, transcripts)
    rows_by_id = {r["call_id"]: r for r in scope}
    result = report(transcripts, crm, run, failures, qa, rows_by_id, args.latency_sample)
    result["extraction_wall_seconds"] = round(extract_s, 1)
    name = f"stress-full__{len(transcripts)}calls.json"
    (st.BASE / name).write_text(json.dumps(result, indent=1, default=str))
    print(json.dumps(result, indent=1, default=str))


if __name__ == "__main__":
    main()
