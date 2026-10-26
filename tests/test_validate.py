"""The validation gate: what happens to hallucinated, ungrounded or out-of-vocabulary LLM output."""

import pytest

from kg.data import load_config, load_transcripts
from kg.schema import CallExtraction
from kg.validate import locate, validate_call


@pytest.fixture(scope="module")
def aurelia():
    return next(t for t in load_transcripts() if t.call_id == "call_aur_20261014")


def extraction(**lists) -> CallExtraction:
    empty = dict(person_mentions=[], pain_points=[], competitor_mentions=[], objections=[], commitments=[], committee_roles=[])
    return CallExtraction.model_validate({**empty, **lists})


def objection(turn_id: str, quote: str, modality: str = "asserted", category: str = "price") -> dict:
    return {
        "evidence": {"turn_id": turn_id, "quote": quote},
        "category": category,
        "summary": "",
        "modality": modality,
        "support": "explicit",
    }


def test_quote_not_in_cited_turn_is_rejected(aurelia):
    v = validate_call(extraction(objections=[objection("u018", "the price is far too high for us")]), aurelia, load_config())
    assert not v.items and v.rejections[0].reason == "quote_not_in_turn"


def test_quote_from_a_different_turn_is_rejected(aurelia):
    v = validate_call(extraction(objections=[objection("u019", "about thirty percent below yours")]), aurelia, load_config())
    assert v.rejections[0].reason == "quote_not_in_turn"


def test_unknown_turn_is_rejected(aurelia):
    v = validate_call(extraction(objections=[objection("u999", "anything")]), aurelia, load_config())
    assert v.rejections[0].reason == "unknown_turn"


def test_near_verbatim_quote_is_kept_but_sent_to_review(aurelia):
    v = validate_call(extraction(objections=[objection("u018", "its about thirty percent below yours")]), aurelia, load_config())
    assert v.items[0].flags == ["quote_fuzzy"] and v.items[0].status == "needs_review"


def test_out_of_vocabulary_competitor_goes_to_review(aurelia):
    sap = {
        "evidence": {"turn_id": "u031", "quote": "we'd integrate with our SAP"},
        "competitor": "SAP",
        "stance": "current_vendor",
        "sentiment": "neutral",
        "summary": "",
        "modality": "asserted",
        "support": "explicit",
    }
    v = validate_call(extraction(competitor_mentions=[sap]), aurelia, load_config())
    assert v.items[0].flags == ["unknown_competitor"] and v.items[0].status == "needs_review"


def test_commitment_with_unknown_owner_is_rejected(aurelia):
    c = {
        "evidence": {"turn_id": "u039", "quote": "I'll send two pricing options"},
        "owner_ref": "p42",
        "action": "send",
        "due_text": "",
        "due_date": "",
        "explicitness": "explicit",
        "modality": "asserted",
    }
    v = validate_call(extraction(commitments=[c]), aurelia, load_config())
    assert v.rejections[0].reason == "unknown_ref"


def test_negated_claim_is_kept_with_its_modality(aurelia):
    v = validate_call(
        extraction(objections=[objection("u038", "Budget isn't the problem", "negated", "budget")]), aurelia, load_config()
    )
    assert v.items[0].modality == "negated" and v.items[0].status == "committed"


def test_locate_returns_span_in_turn_text():
    span, fuzzy = locate("thirty percent", "it's about thirty percent below yours")
    assert span == (11, 25) and not fuzzy


def competitor(name: str, turn_id: str = "u018", quote: str = "about thirty percent below yours") -> dict:
    return {
        "evidence": {"turn_id": turn_id, "quote": quote},
        "competitor": name,
        "stance": "evaluating",
        "sentiment": "neutral",
        "summary": "",
        "modality": "asserted",
        "support": "explicit",
    }


@pytest.mark.parametrize(
    ("name", "reason"),
    [
        ("Kestrel QMS", "seller_own_product"),
        ("Kestrel Systems", "seller_own_product"),
        ("another provider", "generic_competitor_name"),
    ],
)
def test_own_products_and_generic_vendors_are_never_competitors(aurelia, name, reason):
    v = validate_call(extraction(competitor_mentions=[competitor(name)]), aurelia, load_config())
    assert not v.items and v.rejections[0].reason == reason


@pytest.mark.parametrize(
    ("spoken", "slug"),
    [("Veritrack's", "veritrack"), ("VeriTrack.", "veritrack"), ("Quali", "qualisphere"), ("Trace", "tracewise"), ("Wise", None)],
)
def test_competitor_names_fold_punctuation_and_unique_word_prefixes(spoken, slug):
    assert load_config().competitor_slug(spoken) == slug


def test_a_person_mention_whose_name_is_not_in_its_turn_is_rejected(aurelia):
    ghost = {
        "mention_id": "p1",
        "turn_id": "u010",
        "surface": "Wilhelmina",
        "mention_type": "first_name_only",
        "side": "customer",
        "role_hint": "",
        "self_speaker_id": "",
    }
    v = validate_call(extraction(person_mentions=[ghost]), aurelia, load_config())
    assert not v.mentions and v.rejections[0].reason == "surface_not_in_turn"


def test_an_unparseable_due_date_is_flagged_not_trusted(aurelia):
    c = {
        "evidence": {"turn_id": "u039", "quote": "I'll send two pricing options"},
        "owner_ref": "spk_1",
        "action": "send",
        "due_text": "by Friday",
        "due_date": "Friday the twenty-third",
        "explicitness": "explicit",
        "modality": "asserted",
    }
    item = validate_call(extraction(commitments=[c]), aurelia, load_config()).items[0]
    assert item.flags == ["bad_due_date"] and item.attrs["due_date"] == "" and item.status == "needs_review"


def test_a_pain_outside_the_tenants_themes_goes_to_review(aurelia):
    pain = {
        "evidence": {"turn_id": "u018", "quote": "about thirty percent below yours"},
        "theme": "invented_theme",
        "summary": "",
        "modality": "asserted",
        "support": "explicit",
    }
    item = validate_call(extraction(pain_points=[pain]), aurelia, load_config()).items[0]
    assert item.flags == ["unknown_theme"] and item.status == "needs_review"
