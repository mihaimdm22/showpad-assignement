"""Guards on the inputs: no test-set leakage into the prompt, schema inside API limits, gold consistent with data."""

import json
import re

from kg.data import DATA_DIR, REPO_ROOT, load_config, load_crm, load_gold, load_transcripts
from kg.evaluate import eval_facts
from kg.extract import load_run, user_prompt
from kg.graph import build_graph
from kg.schema import CallExtraction


def _words(s: str) -> list[str]:
    return re.findall(r"[a-z0-9']+", s.lower())


def test_no_prompt_version_shares_a_four_word_phrase_with_the_test_transcripts():
    p = [w for f in sorted((DATA_DIR.parent / "prompts").glob("*.md")) for w in _words(f.read_text()) + ["|"]]
    prompt_grams = {" ".join(p[i : i + 4]) for i in range(len(p) - 3)}
    leaks = {
        (" ".join(w[i : i + 4]), t.turn_id)
        for call in load_transcripts()
        for t in call.turns
        for w in [_words(t.text)]
        for i in range(len(w) - 3)
        if " ".join(w[i : i + 4]) in prompt_grams
    }
    assert not leaks


def test_extraction_schema_stays_inside_structured_output_limits():
    schema = json.dumps(CallExtraction.model_json_schema())
    for unsupported in ("anyOf", "minimum", "maximum", "minLength", "maxLength"):
        assert unsupported not in schema


def test_user_prompt_contains_every_turn_and_the_call_date():
    crm, cfg = load_crm(), load_config()
    for t in load_transcripts():
        prompt = user_prompt(t, crm, cfg)
        assert all(f"[{u.turn_id}]" in prompt for u in t.turns)
        assert f"{t.started_at:%Y-%m-%d} ({t.started_at:%A})" in prompt


def test_gold_references_existing_turns_and_crm_records():
    crm, gold = load_crm(), load_gold()
    turns = {(t.call_id, u.turn_id) for t in load_transcripts() for u in t.turns}
    ids = {r["Id"] for kind in ("Contact", "User", "Account", "Opportunity") for r in crm.records(kind)}
    for section in ("mentions", "facts", "must_not"):
        for item in gold[section]:
            tids = item.get("turn_ids") or [item["turn_id"]]
            assert all((item["call_id"], tid) in turns for tid in tids), item
    for ref in [m["expected"] for m in gold["mentions"]] + [s["expected"] for s in gold["speakers"]]:
        assert ref is None or ref.split(":", 1)[1] in ids, ref


def test_gold_mention_surfaces_appear_verbatim_in_their_turns():
    turns = {(t.call_id, u.turn_id): u.text for t in load_transcripts() for u in t.turns}
    for m in load_gold()["mentions"]:
        assert m["surface"].lower() in turns[(m["call_id"], m["turn_id"])].lower(), m


def test_mock_data_is_synthetic():
    for path in DATA_DIR.rglob("*.json"):
        text = path.read_text()
        emails = re.findall(r"[\w.+-]+@[\w.-]+", text)
        assert all(e.endswith(".example") for e in emails), (path, emails)


def test_adjudicated_facts_are_optional_explained_and_left_out_of_the_strict_score():
    """Facts added after the runs must never make the pre-registered (strict) score look better."""
    gold = load_gold()
    added = [f for f in gold["facts"] if f.get("adjudicated")]
    assert added and all(f.get("optional") and len(f["adjudicated"]) > 20 for f in added)
    assert all(r["reason"] for r in gold["adjudication"]["rejected"])
    crm = load_crm()
    run = load_run(REPO_ROOT / "runs" / "extract_v3__claude-opus-5-5")
    g = build_graph(crm, load_config(), load_transcripts(), run).graph
    strict, adjudicated = eval_facts(g, gold, strict=True), eval_facts(g, gold)
    assert strict["required_recall"] == adjudicated["required_recall"]
    assert strict["fp"] - adjudicated["fp"] == adjudicated["tp_optional"] - strict["tp_optional"]
