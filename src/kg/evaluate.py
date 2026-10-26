"""Score a built graph against the hand-written gold answer key.

Four views, each answering a different question an interviewer will ask:
  speakers / mentions  Does entity resolution link people to the right CRM records (and abstain when unsure)?
  facts                Precision and recall of extracted claims, per predicate.
  must_not             Did any trap (negation, hypothetical, non-competitor) become a fact?
  cq                   Do the three use-case queries return the expected answers?
"""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import date

import networkx as nx
from rapidfuzz import fuzz

from kg import queries
from kg.graph import BuildResult, referent
from kg.resolve import fold


def _uid(ref: str | None) -> str | None:
    if ref is None:
        return None
    kind, _, rid = ref.partition(":")
    return f"{kind.capitalize()}:{rid}"


def _is_nil(g: nx.MultiDiGraph, target: str | None) -> bool:
    return target is None or g.nodes[target]["label"] == "Person"


def eval_speakers(g: nx.MultiDiGraph, gold: dict) -> dict:
    rows = []
    for s in gold["speakers"]:
        node = f"Mention:{s['call_id']}/{s['speaker_id']}"
        got, want = referent(g, node), _uid(s["expected"])
        ok = _is_nil(g, got) if want is None else got == want
        rows.append({**s, "got": got, "correct": ok})
    return {"accuracy": sum(r["correct"] for r in rows) / len(rows), "rows": rows}


def eval_mentions(g: nx.MultiDiGraph, gold: dict) -> dict:
    mentions = [(n, d) for n, d in g.nodes(data=True) if d["label"] == "Mention" and d.get("kind") == "person"]
    rows, outcomes = [], Counter()
    for gm in gold["mentions"]:
        cands = [
            (n, d)
            for n, d in mentions
            if d["call_id"] == gm["call_id"]
            and d["turn_id"] == gm["turn_id"]
            and (
                fold(gm["surface"]) in fold(d["surface"])
                or fold(d["surface"]) in fold(gm["surface"])
                or fuzz.ratio(fold(gm["surface"]), fold(d["surface"])) >= 80
            )
        ]
        want = _uid(gm["expected"])
        if not cands:
            outcome, got = "missed", None
        else:
            node = cands[0][0]
            got = referent(g, node)
            candidates = [d for _, d, k in g.out_edges(node, keys=True) if str(k).startswith("CANDIDATE")]
            if want is None:
                outcome = "correct" if _is_nil(g, got) and not candidates else "wrong"
            elif got == want:
                outcome = "correct"
            elif _is_nil(g, got):
                outcome = "abstained_ok" if gm.get("abstain_ok") else "abstained"
            else:
                outcome = "wrong"
        outcomes[outcome] += 1
        rows.append(
            {"id": gm["id"], "surface": gm["surface"], "trap": gm["trap"], "expected": want, "got": got, "outcome": outcome}
        )
    return {
        "accuracy": (outcomes["correct"] + outcomes["abstained_ok"]) / len(rows),
        "wrong_links": outcomes["wrong"],
        "outcomes": dict(outcomes),
        "rows": rows,
    }


def _features(g: nx.MultiDiGraph, a: str, d: dict) -> dict:
    return {
        "node": a,
        "call_id": d["call_id"],
        "turn_id": d["turn_id"],
        "predicate": d["predicate"],
        "value": d["value"],
        "modality": d["modality"],
        "status": d["status"],
        "competitor": d.get("competitor", ""),
        "competitor_surface": d.get("competitor_surface", ""),
        "entity": queries.subject_entity(g, a),
        "due_date": d.get("due_date", ""),
        "explicitness": d.get("explicitness", ""),
        "quote": queries.evidence(g, a)["quote"],
    }


def _matches(f: dict, x: dict) -> bool:
    if f["call_id"] != x["call_id"] or f["predicate"] != x["predicate"] or x["turn_id"] not in f["turn_ids"]:
        return False
    p = f["predicate"]
    if p == "pain_point":
        return x["value"] in f["theme_any_of"]
    if p == "competitor_stance":
        return x["competitor"] == f["competitor"] and ("stance" not in f or x["value"] == f["stance"])
    if p == "objection":
        return "category" not in f or x["value"] == f["category"]
    if p == "commitment":
        if "owner" in f and x["entity"] != _uid(f["owner"]):
            return False
        if "due_date_between" in f:
            lo, hi = f["due_date_between"]
            if not (x["due_date"] and lo <= x["due_date"] <= hi):
                return False
        return "explicitness" not in f or x["explicitness"] == f["explicitness"]
    if p == "committee_role":
        return x["entity"] == _uid(f["subject"]) and x["value"] == f["role"]
    return False


def eval_facts(g: nx.MultiDiGraph, gold: dict, strict: bool = False) -> dict:
    """Asserted claims only. Required facts drive recall; matches to optional facts are never false positives.
    strict=True scores against the key as written before the prompt, ignoring facts added later by adjudication."""
    facts = [f for f in gold["facts"] if not (strict and f.get("adjudicated"))]
    extracted = [_features(g, n, d) for n, d in g.nodes(data=True) if d["label"] == "Assertion" and d["modality"] == "asserted"]
    used: set[str] = set()
    matched_required, missed = [], []
    for f in sorted(facts, key=lambda f: f.get("optional", False)):  # required facts claim matches first
        hit = next((x for x in extracted if x["node"] not in used and _matches(f, x)), None)
        if hit:
            used.add(hit["node"])
            if not f.get("optional"):
                matched_required.append(f)
        elif not f.get("optional"):
            near = [
                x
                for x in extracted
                if x["call_id"] == f["call_id"] and x["predicate"] == f["predicate"] and x["turn_id"] in f["turn_ids"]
            ]
            missed.append(
                {
                    "id": f["id"],
                    "predicate": f["predicate"],
                    "turn_ids": f["turn_ids"],
                    "near_miss": [{k: x[k] for k in ("turn_id", "value", "competitor", "entity", "due_date")} for x in near],
                }
            )
    false_pos = [x for x in extracted if x["node"] not in used]

    per = defaultdict(lambda: {"tp": 0, "fp": 0, "fn": 0, "matched": 0})
    for f in matched_required:
        per[f["predicate"]]["tp"] += 1
    for m in missed:
        per[m["predicate"]]["fn"] += 1
    for x in extracted:
        per[x["predicate"]]["matched" if x["node"] in used else "fp"] += 1
    for c in per.values():
        c["precision"] = round(c["matched"] / (c["matched"] + c["fp"]), 3) if c["matched"] + c["fp"] else None
        c["recall"] = round(c["tp"] / (c["tp"] + c["fn"]), 3) if c["tp"] + c["fn"] else None
    tp, fn, fp = len(matched_required), len(missed), len(false_pos)
    return {
        "required_recall": round(tp / (tp + fn), 3) if tp + fn else None,
        "precision": round(len(used) / (len(used) + fp), 3) if used or fp else None,
        "tp_required": tp,
        "tp_optional": len(used) - tp,
        "fn": fn,
        "fp": fp,
        "per_predicate": dict(per),
        "missed": missed,
        "false_positives": [
            {k: x[k] for k in ("call_id", "turn_id", "predicate", "value", "competitor", "entity", "status", "quote")}
            for x in false_pos
        ],
    }


def _violates(rule: dict, x: dict) -> bool:
    return (
        x["call_id"] == rule["call_id"]
        and x["predicate"] == rule["predicate"]
        and x["turn_id"] in rule["turn_ids"]
        and x["modality"] == "asserted"
        and ("category" not in rule or x["value"] == rule["category"])
        and ("competitor" not in rule or x["competitor"] == rule["competitor"])
        and ("stance" not in rule or x["value"] == rule["stance"])
        and ("competitor_surface" not in rule or fold(x["competitor_surface"]) == fold(rule["competitor_surface"]))
    )


def eval_must_not(g: nx.MultiDiGraph, gold: dict, llm_items: dict[str, list]) -> dict:
    rows = []
    assertions = [_features(g, n, d) for n, d in g.nodes(data=True) if d["label"] == "Assertion"]
    for rule in gold["must_not"]:
        in_graph = [x for x in assertions if _violates(rule, x)]
        rows.append(
            {
                "id": rule["id"],
                "reason": rule["reason"],
                "llm_output_violation": any(_violates(rule, x) for x in llm_items.get(rule["call_id"], [])),
                "committed_fact_violation": any(x["status"] == "committed" for x in in_graph),
                "in_review_queue": any(x["status"] == "needs_review" for x in in_graph),
            }
        )
    return {
        "committed_violations": sum(r["committed_fact_violation"] for r in rows),
        "llm_output_violations": sum(r["llm_output_violation"] for r in rows),
        "rows": rows,
    }


def eval_cq(g: nx.MultiDiGraph, gold: dict, as_of: date) -> dict:
    checks = []
    c1 = gold["cq"]["CQ1_committee"]
    r1 = queries.committee(g, f"Opportunity:{c1['opportunity']}")
    for ref, exp in c1["members"].items():
        m = r1["members"].get(_uid(ref), {})
        checks.append(
            ("CQ1", f"{ref} transcript roles {exp['transcript_roles']}", set(exp["transcript_roles"]) <= set(m.get("roles", [])))
        )
        if "spoke_on_a_call" in exp:
            checks.append(
                (
                    "CQ1",
                    f"{ref} spoke_on_a_call={exp['spoke_on_a_call']}",
                    (m.get("spoke_on_calls", 0) > 0) == exp["spoke_on_a_call"],
                )
            )
    for gap in c1["gaps"]:
        checks.append(("CQ1", f"gap {gap}", any(x["gap"] == gap for x in r1["gaps"])))

    c2 = gold["cq"]["CQ2_stall"]
    r2 = queries.stall(g, f"Opportunity:{c2['opportunity']}", as_of)
    checks.append(("CQ2", f"days_in_stage == {c2['days_in_stage']}", r2["days_in_stage"] == c2["days_in_stage"]))
    checks.append(("CQ2", f"close_date_pushes == {c2['close_date_pushes']}", r2["close_date_pushes"] == c2["close_date_pushes"]))
    for cat in c2["open_objection_categories_include"]:
        checks.append(("CQ2", f"open objection {cat}", cat in r2["open_objections"]))
    for cat in c2["open_objection_categories_exclude"]:
        checks.append(("CQ2", f"no open objection {cat}", cat not in r2["open_objections"]))
    owners = {c["owner"] for c in r2["open_commitments"]}
    for ref in c2["open_commitment_owners_include"]:
        checks.append(("CQ2", f"open commitment owned by {ref}", _uid(ref) in owners))
    checks.append(
        (
            "CQ2",
            f"overdue commitment owned by {c2['overdue_commitment_owner']}",
            any(c["overdue"] and c["owner"] == _uid(c2["overdue_commitment_owner"]) for c in r2["open_commitments"]),
        )
    )

    c3 = gold["cq"]["CQ3_competitor_precedents"]
    r3 = queries.competitor_precedents(g, f"Opportunity:{c3['opportunity']}")
    comp = next((c for c in r3["competitors"] if c["competitor"] == f"Competitor:{c3['competitor']}"), {"precedents": []})
    accounts = {p["account"] for p in comp["precedents"]}
    checks.append(
        ("CQ3", f"precedent accounts == {c3['expected_accounts']}", accounts == {f"Account:{a}" for a in c3["expected_accounts"]})
    )
    checks.append(
        ("CQ3", f"stance {c3['expected_stance']}", any(p["stance"] == c3["expected_stance"] for p in comp["precedents"]))
    )
    for theme in c3["shared_pain_themes_include"]:
        checks.append(("CQ3", f"shared pain {theme}", any(theme in p["shared_pains"] for p in comp["precedents"])))
    cited = {f"{p['call_id']}/{p['turn_id']}" for c in r3["competitors"] for p in c["precedents"]}
    for turn in c3["must_exclude_turns"]:
        checks.append(("CQ3", f"excludes seller-voiced {turn}", turn not in cited))
    return {
        "passed": sum(ok for *_, ok in checks),
        "total": len(checks),
        "checks": [{"cq": cq, "check": name, "ok": ok} for cq, name, ok in checks],
    }


def llm_features(build: BuildResult) -> dict[str, list]:
    """What the LLM said (after quote grounding, before status/vocabulary decisions), to show what the gates caught."""
    return {
        call_id: [
            {
                "call_id": call_id,
                "turn_id": it.turn_id,
                "predicate": it.predicate,
                "value": it.value,
                "modality": it.modality,
                "competitor": it.attrs.get("competitor", ""),
                "competitor_surface": it.attrs.get("competitor_surface", ""),
            }
            for it in rep.validated.items
        ]
        for call_id, rep in build.reports.items()
    }


def validation_summary(build: BuildResult) -> dict:
    items = [it for rep in build.reports.values() for it in rep.validated.items]
    return {
        "items_loaded": len(items),
        "committed": sum(it.status == "committed" for it in items),
        "needs_review": sum(it.status == "needs_review" for it in items),
        "by_modality": dict(Counter(it.modality for it in items)),
        "flags": dict(Counter(f for it in items for f in it.flags)),
        "rejected": dict(Counter(r.reason for rep in build.reports.values() for r in rep.validated.rejections)),
        "rejections": [
            {"call_id": c, "source": r.source, "reason": r.reason, "detail": r.detail}
            for c, rep in build.reports.items()
            for r in rep.validated.rejections
        ],
    }


def evaluate(build: BuildResult, gold: dict, as_of: date) -> dict:
    g = build.graph
    return {
        "validation": validation_summary(build),
        "speakers": eval_speakers(g, gold),
        "mentions": eval_mentions(g, gold),
        "facts": eval_facts(g, gold),
        "facts_strict": eval_facts(g, gold, strict=True),
        "must_not": eval_must_not(g, gold, llm_features(build)),
        "cq": eval_cq(g, gold, as_of),
    }


def format_report(r: dict) -> str:
    v, f, m, s, n, cq = r["validation"], r["facts"], r["mentions"], r["speakers"], r["must_not"], r["cq"]
    pct = lambda x: f"{x:.0%}" if x is not None else "n/a"  # noqa: E731
    lines = [
        "EVALUATION vs data/gold/answer_key.json",
        f"Validation gate   {v['items_loaded']} items loaded ({v['committed']} committed, {v['needs_review']} needs review); "
        f"rejected {sum(v['rejected'].values())} {v['rejected'] or ''}".rstrip(),
        f"Speakers          {pct(s['accuracy'])} linked correctly",
        f"Mentions (ER)     {pct(m['accuracy'])} correct or correctly abstained; wrong links: {m['wrong_links']}; {m['outcomes']}",
        f"Facts             required recall {pct(f['required_recall'])}, precision {pct(f['precision'])} "
        f"(tp {f['tp_required']} required + {f['tp_optional']} optional, fn {f['fn']}, fp {f['fp']})",
        f"                  strict key (as written before the prompt): precision {pct(r['facts_strict']['precision'])}, "
        f"fp {r['facts_strict']['fp']}",
    ]
    for p, c in sorted(f["per_predicate"].items()):
        lines.append(
            f"  {p:18} precision {pct(c['precision']):>5}  recall {pct(c['recall']):>5}  (tp {c['tp']}, fp {c['fp']}, fn {c['fn']})"
        )
    lines.append(
        f"Must-not traps    {n['committed_violations']} became committed facts; {n['llm_output_violations']} appeared in LLM output"
    )
    for row in n["rows"]:
        if row["llm_output_violation"] or row["committed_fact_violation"]:
            where = (
                "COMMITTED"
                if row["committed_fact_violation"]
                else ("held in review queue" if row["in_review_queue"] else "not loaded")
            )
            lines.append(f"  {row['id']} {row['reason']} -> {where}")
    lines.append(f"Use-case queries  {cq['passed']}/{cq['total']} checks pass")
    lines += [f"  FAIL {c['cq']} {c['check']}" for c in cq["checks"] if not c["ok"]]
    for miss in f["missed"]:
        lines.append(f"  missed {miss['id']} {miss['predicate']} {miss['turn_ids']} near: {miss['near_miss']}")
    for x in f["false_positives"]:
        lines.append(
            f'  extra  {x["call_id"]}/{x["turn_id"]} {x["predicate"]}={x["value"]} {x["competitor"]} [{x["status"]}] "{x["quote"]}"'
        )
    for row in m["rows"]:
        if row["outcome"] not in ("correct", "abstained_ok"):
            lines.append(
                f"  ER {row['id']} {row['surface']!r} ({row['trap']}): expected {row['expected']}, got {row['got']} [{row['outcome']}]"
            )
    return "\n".join(lines)
