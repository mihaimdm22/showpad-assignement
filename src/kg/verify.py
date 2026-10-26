"""Verifier pass: a second, independent model call asks whether a claim's quote, read in context, supports the claim as
stated. Quote grounding proves the words were said; this checks they mean what the claim says (a real quote with a
misread meaning, such as a negated objection flipped to asserted, passes grounding).

In production it runs on high-stakes claims (who signs, competitor stance) before the brief states them. Here it also
estimates precision on calls that have no answer key.
"""

from __future__ import annotations

from typing import Literal

import anthropic
from pydantic import BaseModel

from kg.data import REPO_ROOT, Transcript
from kg.extract import SERVER_FALLBACK_MODELS, SIDE
from kg.validate import Item

VERIFY_PROMPT_VERSION = "verify_v1"


class Verdict(BaseModel):
    supported: Literal["yes", "partly", "no"]
    reason: str


def context_window(t: Transcript, turn_id: str, k: int = 2) -> str:
    """The cited turn and k turns either side, with speaker names and sides."""
    names = {p.speaker_id: (p.display_name, SIDE[p.affiliation]) for p in t.participants}
    idx = next(i for i, u in enumerate(t.turns) if u.turn_id == turn_id)
    lines = []
    for u in t.turns[max(0, idx - k) : idx + k + 1]:
        name, side = names[u.speaker_id]
        marker = "  <- cited" if u.turn_id == turn_id else ""
        lines.append(f"[{u.turn_id}] {name} ({side}): {u.text}{marker}")
    return "\n".join(lines)


def claim_text(t: Transcript, it: Item, about: str = "") -> str:
    detail = it.attrs.get("summary") or it.attrs.get("competitor_surface") or ""
    return "\n".join(
        line
        for line in (
            f"type: {it.predicate}",
            f"value: {it.value}",
            f"modality: {it.modality}",
            f"about: {about}" if about else "",
            f"competitor: {it.attrs['competitor_surface']}" if it.attrs.get("competitor_surface") else "",
            f"detail: {detail}" if detail and detail != it.attrs.get("competitor_surface") else "",
            f'quote: "{it.quote}" (turn {it.turn_id})',
        )
        if line
    )


def verify_claim(
    client: anthropic.Anthropic,
    t: Transcript,
    it: Item,
    about: str = "",
    model: str = "claude-opus-5-5",
    effort: str = "low",
) -> dict:
    prompt = (REPO_ROOT / "prompts" / f"{VERIFY_PROMPT_VERSION}.md").read_text()
    user = f"<claim>\n{claim_text(t, it, about)}\n</claim>\n<context>\n{context_window(t, it.turn_id)}\n</context>"
    fallback = {"betas": ["server-side-fallback-2026-07-01"], "fallbacks": "default"} if model in SERVER_FALLBACK_MODELS else {}
    with client.beta.messages.stream(
        model=model,
        max_tokens=8000,
        system=[{"type": "text", "text": prompt, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": user}],
        output_format=Verdict,
        output_config={"effort": effort},
        **fallback,
    ) as stream:
        message = stream.get_final_message()
    if message.parsed_output is None:
        raise RuntimeError(f"verifier gave no verdict (stop_reason={message.stop_reason})")
    u = message.usage
    return {
        **message.parsed_output.model_dump(),
        "usage": {
            "input_tokens": u.input_tokens,
            "output_tokens": u.output_tokens,
            "cache_creation_input_tokens": u.cache_creation_input_tokens or 0,
            "cache_read_input_tokens": u.cache_read_input_tokens or 0,
        },
    }
