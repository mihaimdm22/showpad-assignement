"""Pure parts of the LLM layer: cost accounting and what the verifier is shown (the API calls are covered by runs/)."""

import pytest

from kg.data import load_transcripts
from kg.extract import cost_usd
from kg.validate import Item, locate
from kg.verify import claim_text, context_window


def test_cost_prices_cache_writes_and_reads_separately():
    usage = {"input_tokens": 1_000_000, "cache_creation_input_tokens": 1_000_000, "cache_read_input_tokens": 1_000_000}
    # Opus 5.5: $4 per million input; writes at 1.25x, reads at 0.05x
    assert cost_usd("claude-opus-5-5", usage) == pytest.approx(4 + 5 + 0.2)
    # Haiku 5.5: $0.10 per million input; reads at 0.1x; output $0.50 per million
    assert cost_usd("claude-haiku-5-5", {"cache_read_input_tokens": 1_000_000, "output_tokens": 1_000_000}) == pytest.approx(
        0.01 + 0.5
    )


def test_the_verifier_sees_the_claim_and_the_turns_around_its_quote():
    t = next(t for t in load_transcripts() if t.call_id == "call_aur_20261014")
    quote = "Budget isn't the problem"
    span, _ = locate(quote, t.turn("u038").text)
    it = Item("objection", t.call_id, "u038", quote, span, "asserted", "explicit", "budget", attrs={"summary": "No budget"})
    window = context_window(t, "u038", k=1).splitlines()
    assert [line[:6] for line in window] == ["[u037]", "[u038]", "[u039]"]
    assert window[1].endswith("<- cited") and "(customer)" in window[1]
    text = claim_text(t, it, about="Maggie Dekker")
    assert "modality: asserted" in text and "about: Maggie Dekker" in text and '"Budget isn\'t the problem"' in text
