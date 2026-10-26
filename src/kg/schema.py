"""The extraction contract: what the LLM must return for one call.

This is a *wire* model, shaped for the LLM, not the graph schema. It is flat and typed per
concept so constrained decoding is easy and the output is easy to review. The graph loader
(`graph.py`) translates it into the ontology: Mention and Assertion nodes with evidence edges.

Kept inside the structured-outputs limits: no numeric or length constraints, no recursion,
no optional or union fields. Empty string means "not stated".
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

Modality = Literal["asserted", "negated", "hypothetical"]
Support = Literal["explicit", "implied"]

COMPETITOR_STANCES = ("current_vendor", "evaluating", "past_vendor", "rejected", "mentioned_only")
OBJECTION_CATEGORIES = (
    "price",
    "budget",
    "security_compliance",
    "roi_payback",
    "timing",
    "authority",
    "competitor",
    "implementation_risk",
    "integration",
    "other",
)
COMMITTEE_ROLES = (
    "economic_buyer",
    "champion",
    "decision_maker",
    "technical_evaluator",
    "influencer",
    "end_user",
    "procurement",
    "blocker",
)


class Evidence(BaseModel):
    turn_id: str = Field(description="ID of the single turn that contains the quote, e.g. 'u017'.")
    quote: str = Field(description="Shortest span copied character-for-character from that turn that supports the item.")


class PersonMention(BaseModel):
    mention_id: str = Field(description="Local ID for this mention: 'p1', 'p2', ... unique within the call.")
    turn_id: str = Field(description="Turn in which the person is referred to.")
    surface: str = Field(description="The exact words used to refer to the person, e.g. 'the group CFO', 'Pieter'.")
    mention_type: Literal["full_name", "first_name_only", "title_only", "self_introduction"]
    side: Literal["customer", "seller", "unknown"] = Field(
        description="Which organisation the referred person works for, judged from context."
    )
    role_hint: str = Field(
        description="Job title or function the conversation attributes to the person, e.g. 'CFO', 'procurement'. Empty if none."
    )
    self_speaker_id: str = Field(
        description="If the speaker is introducing themself, their speaker ID (e.g. 'spk_6'); otherwise empty."
    )


class PainPoint(BaseModel):
    evidence: Evidence
    theme: str = Field(description="One pain-theme slug from the provided list, or 'other'.")
    summary: str = Field(description="One sentence in the customer's terms, including any number they gave.")
    modality: Modality
    support: Support


class CompetitorMention(BaseModel):
    evidence: Evidence
    competitor: str = Field(description="Competitor name as spoken.")
    stance: Literal["current_vendor", "evaluating", "past_vendor", "rejected", "mentioned_only"]
    sentiment: Literal["positive", "negative", "neutral", "mixed"]
    summary: str
    modality: Modality
    support: Support


class Objection(BaseModel):
    evidence: Evidence
    category: Literal[
        "price",
        "budget",
        "security_compliance",
        "roi_payback",
        "timing",
        "authority",
        "competitor",
        "implementation_risk",
        "integration",
        "other",
    ]
    summary: str
    modality: Modality
    support: Support


class Commitment(BaseModel):
    evidence: Evidence
    owner_ref: str = Field(description="Who will do it: a speaker ID ('spk_2') or a person mention ID ('p3').")
    action: str = Field(description="What will be done, as a short imperative phrase.")
    due_text: str = Field(description="The deadline as spoken, e.g. 'by Friday'. Empty if none.")
    due_date: str = Field(
        description="The deadline as YYYY-MM-DD resolved against the call date. Empty if none or not resolvable."
    )
    explicitness: Literal["explicit", "hedged"]
    modality: Modality


class CommitteeRole(BaseModel):
    evidence: Evidence
    person_ref: str = Field(description="Who holds the role: a speaker ID ('spk_3') or a person mention ID ('p1').")
    role: Literal[
        "economic_buyer",
        "champion",
        "decision_maker",
        "technical_evaluator",
        "influencer",
        "end_user",
        "procurement",
        "blocker",
    ]
    modality: Modality
    support: Support


class CallExtraction(BaseModel):
    person_mentions: list[PersonMention]
    pain_points: list[PainPoint]
    competitor_mentions: list[CompetitorMention]
    objections: list[Objection]
    commitments: list[Commitment]
    committee_roles: list[CommitteeRole]
