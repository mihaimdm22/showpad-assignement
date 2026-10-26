"""Validation gate between raw LLM output and the graph.

Every extracted item must be grounded: its quote has to be found in the turn it cites, its
person references must exist, and its vocabulary values must be known. Items that fail are
rejected with a reason code. Items that pass but are uncertain (implied, fuzzy quote,
unknown competitor or theme) are kept with status `needs_review` instead of `committed`.
Modality (asserted / negated / hypothetical) is kept as-is: queries decide what counts as fact.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date

from rapidfuzz import fuzz

from kg.data import TenantConfig, Transcript
from kg.schema import CallExtraction

FUZZY_QUOTE_MIN = 90  # rapidfuzz partial-ratio score needed to accept a near-verbatim quote


@dataclass
class ValidMention:
    mention_id: str
    turn_id: str
    surface: str
    span: tuple[int, int]
    mention_type: str
    side: str
    role_hint: str
    self_speaker_id: str


@dataclass
class Item:
    """A grounded claim, ready to become an Assertion node."""

    predicate: str  # pain_point | competitor_stance | objection | commitment | committee_role
    call_id: str
    turn_id: str
    quote: str
    span: tuple[int, int]
    modality: str
    support: str
    value: str  # theme slug, stance, category, action or role
    subject_ref: str = ""  # speaker ID or mention ID for commitments and committee roles
    attrs: dict = field(default_factory=dict)
    flags: list[str] = field(default_factory=list)
    source: str = ""  # e.g. "objections[2]" in the raw output

    @property
    def status(self) -> str:
        return "needs_review" if self.flags or self.support == "implied" else "committed"


@dataclass
class Rejection:
    source: str
    reason: str
    detail: str


@dataclass
class ValidatedCall:
    call_id: str
    mentions: list[ValidMention]
    items: list[Item]
    rejections: list[Rejection]


def _norm(s: str) -> str:
    s = s.replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    return re.sub(r"\s+", " ", s).strip()


def locate(quote: str, text: str) -> tuple[tuple[int, int] | None, bool]:
    """Find `quote` in `text`. Returns (char span, is_fuzzy) or (None, False) if not grounded."""
    q, t = _norm(quote), _norm(text)
    if not q:
        return None, False
    for hay, needle in ((t, q), (t.casefold(), q.casefold())):
        i = hay.find(needle)
        if i >= 0:
            return (i, i + len(needle)), False
    al = fuzz.partial_ratio_alignment(q.casefold(), t.casefold())
    if al is not None and al.score >= FUZZY_QUOTE_MIN:
        return (al.dest_start, al.dest_end), True
    return None, False


def validate_call(x: CallExtraction, t: Transcript, cfg: TenantConfig) -> ValidatedCall:
    rejections: list[Rejection] = []
    mentions: list[ValidMention] = []
    speaker_ids = {p.speaker_id for p in t.participants}

    for i, m in enumerate(x.person_mentions):
        src = f"person_mentions[{i}]"
        turn = t.turn(m.turn_id)
        if turn is None:
            rejections.append(Rejection(src, "unknown_turn", m.turn_id))
            continue
        span, _ = locate(m.surface, turn.text)
        if span is None:
            rejections.append(Rejection(src, "surface_not_in_turn", f"{m.surface!r} not in {m.turn_id}"))
            continue
        if m.self_speaker_id and m.self_speaker_id not in speaker_ids:
            rejections.append(Rejection(src, "unknown_ref", m.self_speaker_id))
            continue
        mentions.append(
            ValidMention(m.mention_id, m.turn_id, m.surface, span, m.mention_type, m.side, m.role_hint, m.self_speaker_id)
        )

    refs = speaker_ids | {m.mention_id for m in mentions}
    items: list[Item] = []

    def ground(src: str, ev) -> tuple[tuple[int, int], list[str]] | None:
        turn = t.turn(ev.turn_id)
        if turn is None:
            rejections.append(Rejection(src, "unknown_turn", ev.turn_id))
            return None
        span, fuzzy = locate(ev.quote, turn.text)
        if span is None:
            rejections.append(Rejection(src, "quote_not_in_turn", f"{ev.quote!r} not in {ev.turn_id}"))
            return None
        return span, (["quote_fuzzy"] if fuzzy else [])

    themes = {th.slug for th in cfg.pain_themes}
    for i, p in enumerate(x.pain_points):
        src = f"pain_points[{i}]"
        if (g := ground(src, p.evidence)) is None:
            continue
        span, flags = g
        if p.theme not in themes:
            flags.append("unknown_theme")
        items.append(
            Item(
                "pain_point",
                t.call_id,
                p.evidence.turn_id,
                p.evidence.quote,
                span,
                p.modality,
                p.support,
                p.theme,
                attrs={"summary": p.summary},
                flags=flags,
                source=src,
            )
        )

    for i, c in enumerate(x.competitor_mentions):
        src = f"competitor_mentions[{i}]"
        if cfg.is_own_product(c.competitor):  # the model cannot know the seller's catalogue unless told; never a competitor
            rejections.append(Rejection(src, "seller_own_product", c.competitor))
            continue
        if cfg.is_generic_vendor(c.competitor):  # "another provider" is not a named competitor
            rejections.append(Rejection(src, "generic_competitor_name", c.competitor))
            continue
        if (g := ground(src, c.evidence)) is None:
            continue
        span, flags = g
        slug = cfg.competitor_slug(c.competitor)
        if slug is None:
            flags.append("unknown_competitor")
        items.append(
            Item(
                "competitor_stance",
                t.call_id,
                c.evidence.turn_id,
                c.evidence.quote,
                span,
                c.modality,
                c.support,
                c.stance,
                attrs={
                    "competitor": slug or "",
                    "competitor_surface": c.competitor,
                    "sentiment": c.sentiment,
                    "summary": c.summary,
                },
                flags=flags,
                source=src,
            )
        )

    for i, o in enumerate(x.objections):
        src = f"objections[{i}]"
        if (g := ground(src, o.evidence)) is None:
            continue
        span, flags = g
        items.append(
            Item(
                "objection",
                t.call_id,
                o.evidence.turn_id,
                o.evidence.quote,
                span,
                o.modality,
                o.support,
                o.category,
                attrs={"summary": o.summary},
                flags=flags,
                source=src,
            )
        )

    for i, c in enumerate(x.commitments):
        src = f"commitments[{i}]"
        if c.owner_ref not in refs:
            rejections.append(Rejection(src, "unknown_ref", c.owner_ref))
            continue
        if (g := ground(src, c.evidence)) is None:
            continue
        span, flags = g
        due = c.due_date
        if due:
            try:
                date.fromisoformat(due)
            except ValueError:
                flags.append("bad_due_date")
                due = ""
        items.append(
            Item(
                "commitment",
                t.call_id,
                c.evidence.turn_id,
                c.evidence.quote,
                span,
                c.modality,
                "explicit" if c.explicitness == "explicit" else "implied",
                c.action,
                subject_ref=c.owner_ref,
                attrs={"due_date": due, "due_text": c.due_text, "explicitness": c.explicitness},
                flags=flags,
                source=src,
            )
        )

    for i, r in enumerate(x.committee_roles):
        src = f"committee_roles[{i}]"
        if r.person_ref not in refs:
            rejections.append(Rejection(src, "unknown_ref", r.person_ref))
            continue
        if (g := ground(src, r.evidence)) is None:
            continue
        span, flags = g
        items.append(
            Item(
                "committee_role",
                t.call_id,
                r.evidence.turn_id,
                r.evidence.quote,
                span,
                r.modality,
                r.support,
                r.role,
                subject_ref=r.person_ref,
                flags=flags,
                source=src,
            )
        )

    return ValidatedCall(t.call_id, mentions, items, rejections)
