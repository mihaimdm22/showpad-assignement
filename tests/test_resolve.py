"""Entity resolution on the planted traps: each row is (mention as the LLM describes it) -> expected decision."""

from pathlib import Path

import pytest

from kg.data import CrmBundle, load_config, load_crm, load_transcripts
from kg.extract import load_run
from kg.resolve import (
    Candidate,
    build_pool,
    context_pool,
    decide,
    merge_strangers,
    name_score,
    resolve_call,
    resolve_mention,
    resolve_speakers,
    score_candidates,
    shared_first_names,
)
from kg.validate import ValidMention, validate_call

HENRIK, PIETER_J, PIETER_DW = "Contact:003KS00000HAALDRAA", "Contact:003KS00000PJANSNAA", "User:005KS00000PDEWITAA"


@pytest.fixture(scope="module")
def pools():
    crm = load_crm()
    calls = {t.call_id: t for t in load_transcripts()}
    return crm, calls, {cid: build_pool(crm, t) for cid, t in calls.items()}


@pytest.mark.parametrize(
    "call, surface, mention_type, side, hint, decision, target",
    [
        ("call_aur_20261014", "the group CFO", "title_only", "customer", "CFO", "linked", HENRIK),
        ("call_aur_20261014", "Hendrik Ahlders", "full_name", "customer", "", "linked", HENRIK),  # transcription misspelling
        ("call_aur_20261014", "Henrik", "first_name_only", "customer", "", "linked", HENRIK),
        ("call_aur_20261014", "Maggie", "first_name_only", "customer", "", "linked", "Contact:003KS00000MDEKKRAA"),  # nickname
        ("call_aur_20261014", "Pieter", "first_name_only", "customer", "procurement", "linked", PIETER_J),
        # Two people are called Pieter: the side guess alone must not pick one (it was wrong in the v1 run) ...
        ("call_aur_20261014", "Pieter", "first_name_only", "seller", "", "candidate", None),
        ("call_aur_20261014", "Pieter", "first_name_only", "unknown", "", "candidate", None),
        # ... but a second clue does: the role the conversation gives them matches only one CRM title.
        ("call_aur_20261014", "Pieter", "first_name_only", "seller", "solutions engineer", "linked", PIETER_DW),
        (
            "call_aur_20261014",
            "Pieter",
            "first_name_only",
            "seller",
            "procurement",
            "linked",
            PIETER_J,
        ),  # wrong side guess, right clue
        ("call_aur_20261014", "Ravi", "self_introduction", "customer", "enterprise architect", "nil", None),  # not in CRM
        # Phonetic misspelling of Siobhán: no CRM name resembles it in spelling, so it is never guessed. It becomes an
        # unknown person for review. Matching it would need phonetic comparison (a known limitation).
        ("call_brn_20260930", "Chevonne", "first_name_only", "customer", "", "nil", None),
    ],
)
def test_mention_traps(pools, call, surface, mention_type, side, hint, decision, target):
    _, _, pool = pools
    link = decide(score_candidates(ValidMention("p", "u", surface, (0, 0), mention_type, side, hint, ""), pool[call]), "test")
    assert (link.decision, link.target) == (decision, target)


def test_speakers_resolve_by_email_then_display_name(pools):
    crm, calls, pool = pools
    t = calls["call_aur_20261014"]
    s = resolve_speakers(crm, t, pool[t.call_id])
    assert s["spk_3"].method == "email" and s["spk_3"].target == "Contact:003KS00000MDEKKRAA"
    assert s["spk_4"].method == "display_name" and s["spk_4"].target == "Contact:003KS00000JVDBRGAA"  # "Joris vd Berg", no email
    assert s["spk_6"].decision == "nil"  # anonymous dial-in


def test_one_unknown_person_named_five_ways_becomes_one_stranger_when_missing_from_crm():
    """Break-it check: delete the CFO from the CRM. Every way the call names him must still collapse into one person."""
    crm = load_crm()
    crm.objects["Contact"] = [c for c in crm.records("Contact") if c["Id"] != HENRIK.split(":")[1]]
    t = next(c for c in load_transcripts() if c.call_id == "call_aur_20261014")
    _, extraction = load_run(Path(__file__).parent / "fixtures" / "ideal_run")[t.call_id]
    v = validate_call(extraction, t, load_config())
    r = resolve_call(crm, t, v)
    cfo = {m.mention_id for m in v.mentions if m.surface in ("the group CFO", "Hendrik Ahlders", "the CFO", "Henrik")}
    assert {r.mentions[i].decision for i in cfo} == {"nil"}
    assert len({id(r.strangers[i]) for i in cfo}) == 1
    assert r.strangers[next(iter(cfo))].name == "Hendrik Ahlders"
    ravi = next(m.mention_id for m in v.mentions if m.surface == "Ravi")
    assert r.strangers[ravi].name == "Ravi"  # a different unknown person stays separate


def test_two_people_who_introduce_themselves_are_never_merged():
    a = ValidMention("p1", "u1", "Alex", (0, 0), "self_introduction", "customer", "engineer", "spk_7")
    b = ValidMention("p2", "u2", "Alex", (0, 0), "self_introduction", "customer", "engineer", "spk_8")
    s = merge_strangers([a, b])
    assert s["p1"] is not s["p2"]


def test_a_shared_first_name_is_flagged_before_extraction(pools):
    """The cascade's trigger: cheaper models misread which Pieter is meant, so this call goes to the stronger model."""
    crm, calls, _ = pools
    assert shared_first_names(crm, calls["call_aur_20261014"]) == ["pieter"]
    assert shared_first_names(crm, calls["call_brn_20260930"]) == []


def _crm_with_users(*names: str):
    """The mock CRM plus extra sellers who are on no call and no deal, as in a tenant with dozens of sellers."""
    crm = load_crm()
    objects = {k: list(v) for k, v in crm.objects.items()}
    for i, name in enumerate(names):
        objects["User"].append({"Id": f"005KS0000EXTRA{i:02d}AA", "Name": name, "Title": "Account Executive", "Email": None})
    return CrmBundle(tenant_id=crm.tenant_id, export_ts=crm.export_ts, objects=objects)


def _mention(surface: str, side: str, mention_type: str = "first_name_only") -> ValidMention:
    return ValidMention("m1", "u010", surface, (0, len(surface)), mention_type, side, "", "")


def test_a_first_name_is_matched_against_the_call_before_the_whole_sales_team():
    crm = _crm_with_users("Sanne Bakker")
    t = next(t for t in load_transcripts() if t.call_id == "call_aur_20261014")
    pool = build_pool(crm, t)
    link = resolve_mention(_mention("Sanne", "customer"), context_pool(crm, t, pool), pool)
    assert link.target == "Contact:003KS00000SVISSRAA" and link.decision == "linked"


def test_a_colleague_who_is_not_on_the_call_is_still_found_by_widening():
    crm = _crm_with_users("Kees Smit")
    t = next(t for t in load_transcripts() if t.call_id == "call_aur_20261014")
    pool = build_pool(crm, t)
    link = resolve_mention(_mention("Kees", "seller"), context_pool(crm, t, pool), pool)
    assert link.target == "User:005KS0000EXTRA00AA" and link.method.endswith("_widened")


def test_a_speakers_side_from_the_recorder_filters_the_pool():
    """A customer on the call shares her exact name with a seller elsewhere in the company, and the recorder gives
    names but no emails."""
    crm = _crm_with_users("Sanne Visser")
    t = next(t for t in load_transcripts() if t.call_id == "call_aur_20261014")
    sanne = next(p for p in t.participants if p.display_name.startswith("Sanne"))
    t = t.model_copy(
        update={
            "participants": [
                p.model_copy(update={"email": None, "display_name": "Sanne Visser"}) if p is sanne else p for p in t.participants
            ]
        }
    )
    links = resolve_speakers(crm, t, build_pool(crm, t))
    assert links[sanne.speaker_id].target == "Contact:003KS00000SVISSRAA"


def test_different_surnames_do_not_tie_but_a_transcription_variant_still_matches():
    """From the full stress test: "Lucas Almeida" tied with a colleague "Lucas Moreira" under Jaro-Winkler surnames."""
    moreira = Candidate("User:x", "Lucas", "Moreira", "", "seller")
    aalders = Candidate("Contact:y", "Henrik", "Aalders", "Group CFO", "customer")
    assert name_score("Lucas Almeida", moreira) < 0.75
    assert name_score("Hendrik Ahlders", aalders) >= 0.85
    assert name_score("Sanne V.", Candidate("Contact:z", "Sanne", "Visser", "", "customer")) >= 0.9
