"""LLM extraction: one structured-output call per transcript, saved raw for replay and audit."""

from __future__ import annotations

import hashlib
import json
import time
from datetime import UTC, datetime
from pathlib import Path

import anthropic

from kg.data import REPO_ROOT, CrmBundle, TenantConfig, Transcript
from kg.resolve import shared_first_names
from kg.schema import CallExtraction

PROMPT_VERSION = "extract_v3"  # v3: named vendors only, own products, objection boundary, record each fact once
DEFAULT_MODEL = "claude-opus-5-5"
SIDE = {"internal": "seller", "external": "customer", "unknown": "unknown"}
# USD per million tokens (input, output), first-party API list prices; Haiku's rate is for prompts up to 100K tokens.
PRICES = {"claude-opus-5-5": (4.00, 20.00), "claude-sonnet-5-5": (2.00, 10.00), "claude-haiku-5-5": (0.10, 0.50)}
CACHE_WRITE, CACHE_READ = 1.25, {"claude-opus-5-5": 0.05}  # multiples of the input price; reads are 0.1x elsewhere
# The output contract's version: a hash of its JSON schema, stamped on every run so a schema change is visible in runs/.
SCHEMA_VERSION = hashlib.sha1(json.dumps(CallExtraction.model_json_schema(), sort_keys=True).encode()).hexdigest()[:12]
# Models that accept the server-side refusal fallback; Haiku 5.5 has none, so the parameter is not sent to it.
SERVER_FALLBACK_MODELS = {"claude-opus-5-5", "claude-sonnet-5-5"}


def cost_usd(model: str, usage: dict) -> float:
    price_in, price_out = PRICES[model]
    cached = usage.get("cache_creation_input_tokens", 0) * CACHE_WRITE + usage.get("cache_read_input_tokens", 0) * CACHE_READ.get(
        model, 0.1
    )
    return ((usage.get("input_tokens", 0) + cached) * price_in + usage.get("output_tokens", 0) * price_out) / 1e6


def system_prompt(version: str = PROMPT_VERSION) -> str:
    return (REPO_ROOT / "prompts" / f"{version}.md").read_text()


def user_prompt(t: Transcript, crm: CrmBundle, cfg: TenantConfig) -> str:
    account = crm.by_id("Account", t.crm_refs.get("account_id", "")) or {}
    started = t.started_at
    lines = [
        "<call>",
        f"call_id: {t.call_id}",
        f"title: {t.title}",
        f"date: {started:%Y-%m-%d} ({started:%A}), started {started:%H:%M} UTC{started:%z}",
        f"seller company: {cfg.seller_company}; product: {cfg.product}",
        f"customer account: {account.get('Name', 'unknown')}",
        "</call>",
        "<participants>",
        *(f"{p.speaker_id} | {p.display_name} | {SIDE[p.affiliation]}" for p in t.participants),
        "</participants>",
        "<pain_themes>",
        *(f"{th.slug}: {th.label}. {th.description}" for th in cfg.pain_themes),
        "</pain_themes>",
        f"<known_competitors>{', '.join(c.name for c in cfg.competitors)} (not exhaustive)</known_competitors>",
        f"<seller_products>{', '.join(cfg.products) or 'not listed'}</seller_products>",
        "<transcript>",
    ]
    names = {p.speaker_id: p.display_name for p in t.participants}
    sides = {p.speaker_id: SIDE[p.affiliation] for p in t.participants}
    lines += [f"[{u.turn_id}] {u.speaker_id} {names[u.speaker_id]} ({sides[u.speaker_id]}): {u.text}" for u in t.turns]
    lines.append("</transcript>")
    return "\n".join(lines)


def extract_call(
    client: anthropic.Anthropic,
    t: Transcript,
    crm: CrmBundle,
    cfg: TenantConfig,
    model: str = DEFAULT_MODEL,
    effort: str = "high",
) -> dict:
    """Run one extraction and return {"meta": ..., "extraction": ...} ready to save as JSON."""
    started = time.monotonic()
    # Server-side fallback: if a safety classifier declines, the API retries on a fallback model.
    fallback = {"betas": ["server-side-fallback-2026-07-01"], "fallbacks": "default"} if model in SERVER_FALLBACK_MODELS else {}
    with client.beta.messages.stream(
        model=model,
        max_tokens=64000,
        # The instructions are identical for every call, so they are cached; the call itself follows the breakpoint.
        system=[{"type": "text", "text": system_prompt(), "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": user_prompt(t, crm, cfg)}],
        output_format=CallExtraction,
        output_config={"effort": effort},
        **fallback,
    ) as stream:
        message = stream.get_final_message()

    if message.stop_reason == "refusal":
        raise RuntimeError(f"{t.call_id}: model refused ({message.stop_details})")
    if message.stop_reason == "max_tokens":
        raise RuntimeError(f"{t.call_id}: output truncated at max_tokens")
    if message.parsed_output is None:
        raise RuntimeError(f"{t.call_id}: no parsed output (stop_reason={message.stop_reason})")

    usage = message.usage
    return {
        "meta": {
            "call_id": t.call_id,
            "tenant_id": t.tenant_id,
            "model_requested": model,
            "model_served": message.model,
            "prompt_version": PROMPT_VERSION,
            "schema_version": SCHEMA_VERSION,
            "effort": effort,
            "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "duration_s": round(time.monotonic() - started, 1),
            "request_id": getattr(message, "_request_id", None),
            "stop_reason": message.stop_reason,
            "usage": {
                "input_tokens": usage.input_tokens,
                "output_tokens": usage.output_tokens,
                "cache_creation_input_tokens": usage.cache_creation_input_tokens or 0,
                "cache_read_input_tokens": usage.cache_read_input_tokens or 0,
            },
        },
        "extraction": message.parsed_output.model_dump(),
    }


def escalation_reasons(t: Transcript, crm: CrmBundle) -> list[str]:
    """Why a call should go to the stronger model in a cascade. One rule so far, from the model comparison: cheaper
    settings misread which side a shared first name belongs to (docs/model-comparison.md)."""
    return [f"shared first name: {n}" for n in shared_first_names(crm, t)]


def run_extraction(
    transcripts: list[Transcript],
    crm: CrmBundle,
    cfg: TenantConfig,
    out_dir: Path,
    model: str = DEFAULT_MODEL,
    effort: str = "high",
    escalate_to: str | None = None,
) -> list[Path]:
    client = anthropic.Anthropic()
    if not (client.api_key or client.auth_token or client.credentials):
        raise SystemExit("No Anthropic credentials found: put ANTHROPIC_API_KEY=... in .env (see .env.example).")
    out_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for t in transcripts:
        reasons = escalation_reasons(t, crm) if escalate_to else []
        call_model = escalate_to if reasons else model
        try:
            record = extract_call(client, t, crm, cfg, model=call_model, effort=effort)
        except anthropic.AuthenticationError as e:
            raise SystemExit("Anthropic authentication failed: check ANTHROPIC_API_KEY in .env.") from e
        except anthropic.RateLimitError as e:
            raise SystemExit(f"Rate limited on {t.call_id}; retry later ({e.message}).") from e
        except anthropic.APIStatusError as e:
            raise SystemExit(f"API error on {t.call_id}: {e.status_code} {e.message}") from e
        if escalate_to:
            record["meta"]["escalated_for"] = reasons
        path = out_dir / f"{t.call_id}.json"
        path.write_text(json.dumps(record, indent=2, ensure_ascii=False) + "\n")
        u = record["meta"]["usage"]
        price = f", ${cost_usd(call_model, u):.3f}" if call_model in PRICES else ""
        routed = f" [{call_model}: {'; '.join(reasons)}]" if reasons else ""
        print(
            f"  {t.call_id}{routed}: {u['input_tokens']} in / {u['output_tokens']} out tokens{price}, {record['meta']['duration_s']} s -> {path}"
        )
        written.append(path)
    return written


def load_run(run_dir: Path) -> dict[str, tuple[dict, CallExtraction]]:
    """Read saved raw outputs: call_id -> (meta, parsed extraction)."""
    runs = {}
    for p in sorted(run_dir.glob("call_*.json")):
        record = json.loads(p.read_text())
        runs[record["meta"]["call_id"]] = (record["meta"], CallExtraction.model_validate(record["extraction"]))
    return runs
