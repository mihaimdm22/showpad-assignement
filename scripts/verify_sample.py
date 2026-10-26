"""Estimate claim precision on the full stress run, which has no answer key: verify a random sample of claims with the
verifier pass (src/kg/verify.py) and report the supported share with 95% Wilson intervals.

Verdicts quote CC BY-NC transcripts, so they stay in .context/external/; only aggregates are published.

Usage:  uv run --with pyarrow python -I scripts/verify_sample.py [--n 200] [--workers 8]
"""

from __future__ import annotations

import argparse
import json
import math
import random
import sys
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import anthropic

sys.path.insert(0, str(Path(__file__).parent))
import stress_full as sf  # noqa: E402
import stress_test as st  # noqa: E402

from kg.extract import PROMPT_VERSION, cost_usd  # noqa: E402
from kg.validate import validate_call  # noqa: E402
from kg.verify import verify_claim  # noqa: E402


def wilson(k: int, n: int) -> tuple[float, float]:
    if not n:
        return (0.0, 0.0)
    p, z = k / n, 1.96
    centre, half = (p + z * z / (2 * n)) / (1 + z * z / n), z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / (1 + z * z / n)
    return (round(centre - half, 3), round(centre + half, 3))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=200)
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--seed", type=int, default=11)
    args = ap.parse_args()

    from kg.cli import _load_env

    _load_env()
    rows, _ = st.load_rows()
    rows = sf.linkable(rows)
    crm, users = sf.tenant_crm(rows)
    transcripts = {t.call_id: t for t in (st.to_transcript(r, set(users)) for r in rows)}
    out_dir = st.RUNS / f"{PROMPT_VERSION}__cascade-claude-haiku-5-5-claude-opus-5-5"
    run = sf.load(out_dir, list(transcripts.values()))

    claims = []
    for call_id, (meta, extraction) in sorted(run.items()):
        t = transcripts[call_id]
        v = validate_call(extraction, t, st.CONFIG)
        names = {p.speaker_id: p.display_name for p in t.participants} | {m.mention_id: m.surface for m in v.mentions}
        claims += [(t, it, names.get(it.subject_ref, ""), meta["model_requested"]) for it in v.items]
    sample = random.Random(args.seed).sample(claims, min(args.n, len(claims)))
    client = anthropic.Anthropic(max_retries=8)

    def one(c):
        t, it, about, model = c
        try:
            v = verify_claim(client, t, it, about)
        except Exception as e:  # noqa: BLE001
            v = {"supported": "error", "reason": f"{type(e).__name__}: {e}"[:200], "usage": {}}
        return {
            "call_id": t.call_id,
            "turn_id": it.turn_id,
            "predicate": it.predicate,
            "value": it.value,
            "modality": it.modality,
            "status": it.status,
            "extracted_by": model,
            "quote": it.quote,
            **v,
        }

    with ThreadPoolExecutor(args.workers) as pool:
        results = list(pool.map(one, sample))
    (st.BASE / "verify-sample.jsonl").write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in results) + "\n")

    def summary(rs):
        n, yes = len(rs), sum(r["supported"] == "yes" for r in rs)
        partly = sum(r["supported"] == "partly" for r in rs)
        return {"n": n, "yes": yes, "partly": partly, "no": sum(r["supported"] == "no" for r in rs), "yes_share": round(yes / n, 3) if n else None, "yes_95ci": wilson(yes, n), "yes_or_partly_share": round((yes + partly) / n, 3) if n else None}  # fmt: skip

    groups = defaultdict(list)
    for r in results:
        groups[("predicate", r["predicate"])].append(r)
        groups[("status", r["status"])].append(r)
        groups[("extracted_by", r["extracted_by"])].append(r)
    cost = sum(cost_usd("claude-opus-5-5", r["usage"]) for r in results if r.get("usage"))
    report = {
        "claims_in_run": len(claims),
        "sample": len(results),
        "errors": sum(r["supported"] == "error" for r in results),
        "overall": summary(results),
        "by": {f"{k}={v}": summary(rs) for (k, v), rs in sorted(groups.items())},
        "verifier_cost_usd": round(cost, 2),
        "claims_by_predicate_in_run": dict(Counter(it.predicate for _, it, _, _ in claims)),
    }
    (st.BASE / "verify-sample-report.json").write_text(json.dumps(report, indent=1))
    print(json.dumps(report, indent=1))


if __name__ == "__main__":
    main()
