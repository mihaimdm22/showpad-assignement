"""Entity resolution: link speakers and person mentions to CRM Contacts and Users.

The LLM reads context (which side a person is on, the role the conversation gives them);
this module does the matching against the CRM with explainable scores, so every link can be
audited and thresholds can be tuned without re-prompting.

Decision rule per mention:
  LINK       top score >= LINK_MIN and margin over the runner-up >= MARGIN_MIN
  CANDIDATE  top score >= CANDIDATE_MIN  (kept as scored candidate edges for review)
  NIL        otherwise: a provisional, unresolved Person node is created
"""

from __future__ import annotations

import re
import unicodedata
from collections import Counter
from dataclasses import dataclass, field

from rapidfuzz.distance import JaroWinkler, Levenshtein

from kg.data import CrmBundle, Transcript
from kg.validate import ValidatedCall, ValidMention

# CANDIDATE_MIN was 0.5 before the break-it experiments: Jaro-Winkler gives unrelated names 0.5-0.6, which produced noise such as
# "Hendrik Ahlders -> maybe Joris van den Berg?". A "maybe" now needs a genuine resemblance.
LINK_MIN, MARGIN_MIN, CANDIDATE_MIN = 0.85, 0.15, 0.7
SAME_NAME_MIN = 0.9  # two or more people at or above this name score = a same-name collision needing a second clue
ATTENDED_BONUS = 0.05  # small prior for people who were actually on the call

# A tiny nickname table for the demo; production would use a full name-variant resource.
NICKNAMES = {
    "maggie": "margaret",
    "meg": "margaret",
    "peggy": "margaret",
    "liz": "elizabeth",
    "beth": "elizabeth",
    "kate": "katherine",
    "bob": "robert",
    "bill": "william",
    "jim": "james",
    "mike": "michael",
    "tom": "thomas",
    "dave": "david",
    "sue": "susan",
    "susie": "susan",
}
PARTICLES = {"vd": "van den", "v.d.": "van der", "vdb": "van den berg"}
TITLE_ABBREV = {
    "cfo": "chief financial officer",
    "ceo": "chief executive officer",
    "coo": "chief operating officer",
    "cto": "chief technology officer",
    "cio": "chief information officer",
    "vp": "vice president",
}
STOP = {"the", "our", "their", "my", "your", "of", "and", "a", "an", "at", "group's"}


def fold(s: str) -> str:
    s = unicodedata.normalize("NFKD", s)
    s = "".join(ch for ch in s if not unicodedata.combining(ch)).casefold()
    s = re.sub(r"[^\w\s.'-]", " ", s)
    words = [PARTICLES.get(w, w) for w in s.split()]
    return " ".join(" ".join(words).split())


@dataclass
class Candidate:
    uid: str  # "Contact:<Id>" or "User:<Id>"
    first: str
    last: str
    title: str
    side: str  # customer | seller
    attended: bool = False


@dataclass
class Link:
    target: str | None  # entity uid, or None
    decision: str  # linked | candidate | nil
    score: float
    method: str
    candidates: list[tuple[str, float]] = field(default_factory=list)


@dataclass
class Stranger:
    """A person mentioned in a call who is not in the CRM; several mentions can be the same stranger."""

    name: str
    role_hint: str
    aliases: list[str]


@dataclass
class CallResolution:
    call_id: str
    speakers: dict[str, Link]
    mentions: dict[str, Link]
    strangers: dict[str, Stranger] = field(default_factory=dict)  # mention_id -> merged stranger, for "nil" mentions


def build_pool(crm: CrmBundle, t: Transcript) -> list[Candidate]:
    """Candidate set for one call: contacts on the call's account and its invitees, plus all internal users.
    A person attended if a participant has their email or, when the recorder gives no email, their exact name."""
    account_id = t.crm_refs.get("account_id")
    event = crm.by_id("Event", t.crm_refs.get("event_id", "")) or {}
    invitees = set(event.get("EventWhoIds", []))
    attended_emails = {p.email.casefold() for p in t.participants if p.email}
    attended_names = {fold(p.display_name) for p in t.participants}
    pool = []
    for c in crm.records("Contact"):
        if c["AccountId"] == account_id or c["Id"] in invitees:
            pool.append(
                Candidate(
                    f"Contact:{c['Id']}",
                    c["FirstName"],
                    c["LastName"],
                    c.get("Title") or "",
                    "customer",
                    attended=(c.get("Email") or "").casefold() in attended_emails
                    or fold(f"{c['FirstName']} {c['LastName']}") in attended_names,
                )
            )
    for u in crm.records("User"):
        first, _, last = u["Name"].partition(" ")
        pool.append(
            Candidate(
                f"User:{u['Id']}",
                first,
                last,
                u.get("Title") or "",
                "seller",
                attended=(u.get("Email") or "").casefold() in attended_emails or fold(u["Name"]) in attended_names,
            )
        )
    return pool


def context_pool(crm: CrmBundle, t: Transcript, pool: list[Candidate]) -> list[Candidate]:
    """The people this call is plausibly about: the account's contacts, everyone on the call, and the deal's owner and
    team. Mentions are matched here first (blocking); a tenant with 90 sellers otherwise turns every "Thanks, Liam"
    into a tie with sellers who were never on the call."""
    opp_id = t.crm_refs.get("opportunity_id", "")
    opp = crm.by_id("Opportunity", opp_id) or {}
    deal_users = {opp.get("OwnerId")} | {
        m["UserId"] for m in crm.records("OpportunityTeamMember") if m["OpportunityId"] == opp_id
    }
    return [c for c in pool if c.side == "customer" or c.attended or c.uid.removeprefix("User:") in deal_users]


def shared_first_names(crm: CrmBundle, t: Transcript) -> list[str]:
    """First names held by two or more people this call could plausibly be about: the account's contacts, the people on
    the call, and the deal's owner and team. This is the case cheaper models misread (two "Pieter"s, one per side), and it
    is known before extraction, so it can route the call to a stronger model."""
    counts = Counter(fold(c.first) for c in context_pool(crm, t, build_pool(crm, t)) if c.first)
    return sorted(n for n, k in counts.items() if k > 1)


def _first_sim(token: str, first: str) -> float:
    a, b = fold(token), fold(first)
    if a == b:
        return 1.0
    if NICKNAMES.get(a) == b:
        return 0.95
    return JaroWinkler.normalized_similarity(a, b)


def _last_sim(token: str, last: str) -> float:
    """Surnames by edit distance: Jaro-Winkler rewards shared letters, which made "Almeida" and "Moreira" look alike
    (0.71) and left a seller's exact full name tied with a colleague's. A transcription variant (Ahlders / Aalders)
    stays close. An initial ("Sanne V.") matches the surname's first letter."""
    a, b = fold(token).rstrip("."), fold(last)
    if a == b:
        return 1.0
    if len(a) == 1:
        return 0.9 if b.startswith(a) else 0.0
    return Levenshtein.normalized_similarity(a, b)


def name_score(surface: str, c: Candidate) -> float:
    tokens = [w for w in fold(surface).split() if w not in STOP]
    if not tokens:
        return 0.0
    if len(tokens) == 1:
        return max(_first_sim(tokens[0], c.first), 0.8 * JaroWinkler.normalized_similarity(tokens[0], fold(c.last)))
    first, last = tokens[0], " ".join(tokens[1:])
    return 0.5 * _first_sim(first, c.first) + 0.5 * _last_sim(last, c.last)


def title_score(hint: str, title: str) -> float:
    """Share of the hint's words that appear in the CRM title (after expanding CFO, VP, ...)."""

    def words(s: str) -> set[str]:
        out = set()
        for w in fold(s).split():
            out.update(TITLE_ABBREV.get(w, w).split())
        return out - STOP

    h, t = words(hint), words(title)
    if not h or not t:
        return 0.0
    hits = sum(1 for w in h if w in t or any(JaroWinkler.normalized_similarity(w, x) >= 0.9 for x in t))
    return hits / len(h)


def same_name_collision(m: ValidMention, pool: list[Candidate]) -> list[Candidate]:
    """People who all fit the spoken name equally well (e.g. two 'Pieter's), on either side."""
    if m.mention_type == "title_only":
        return []
    fits = [c for c in pool if name_score(m.surface, c) >= SAME_NAME_MIN]
    return fits if len(fits) >= 2 else []


def score_candidates(m: ValidMention, pool: list[Candidate]) -> list[tuple[str, float]]:
    collided = same_name_collision(m, pool)
    if collided:
        # The name alone cannot separate these people, and the LLM's side guess is a single soft hint,
        # so it is not allowed to break the tie. Only a second clue (the role or title the conversation
        # gives the person) can; without one they score equally and the decision becomes "candidate".
        titles = {c.uid: title_score(m.role_hint, c.title) if m.role_hint else 0.0 for c in collided}
        clue = any(titles.values())  # a role hint that matches nobody's title is no clue at all
        scored = []
        for c in collided:
            s = 0.5 * name_score(m.surface, c) + 0.5 * titles[c.uid] if clue else name_score(m.surface, c)
            if c.attended:
                s = min(1.0, s + ATTENDED_BONUS)
            scored.append((c.uid, round(s, 3)))
        return sorted(scored, key=lambda x: -x[1])

    allowed = {"customer": {"customer"}, "seller": {"seller"}}.get(m.side, {"customer", "seller"})
    scored = []
    for c in pool:
        if c.side not in allowed:
            continue
        if m.mention_type == "title_only":
            s = title_score(m.role_hint or m.surface, c.title)
        elif m.role_hint:
            s = 0.75 * name_score(m.surface, c) + 0.25 * title_score(m.role_hint, c.title)
        else:
            s = name_score(m.surface, c)
        if c.attended and s > 0:
            s = min(1.0, s + ATTENDED_BONUS)
        scored.append((c.uid, round(s, 3)))
    return sorted(scored, key=lambda x: -x[1])


def decide(scored: list[tuple[str, float]], method: str) -> Link:
    if not scored or scored[0][1] < CANDIDATE_MIN:
        return Link(None, "nil", scored[0][1] if scored else 0.0, method, scored[:3])
    top, runner = scored[0][1], (scored[1][1] if len(scored) > 1 else 0.0)
    if top >= LINK_MIN and top - runner >= MARGIN_MIN:
        return Link(scored[0][0], "linked", top, method, scored[:3])
    return Link(None, "candidate", top, method, [s for s in scored[:3] if s[1] >= CANDIDATE_MIN])


def resolve_speakers(crm: CrmBundle, t: Transcript, pool: list[Candidate]) -> dict[str, Link]:
    by_email = {
        (r.get("Email") or "").casefold(): f"{kind}:{r['Id']}"
        for kind in ("Contact", "User")
        for r in crm.records(kind)
        if r.get("Email")
    }
    out = {}
    for p in t.participants:
        if p.email and p.email.casefold() in by_email:
            out[p.speaker_id] = Link(by_email[p.email.casefold()], "linked", 1.0, "email")
            continue
        side = {"internal": "seller", "external": "customer"}.get(p.affiliation, "unknown")
        if side == "unknown":  # anonymous dial-in: only a self-introduction can identify it
            out[p.speaker_id] = Link(None, "nil", 0.0, "unidentified_dial_in")
            continue
        # The recorder's side is a fact, not the model's guess, so it filters the pool before any name is compared.
        probe = ValidMention("", "", p.display_name, (0, 0), "full_name", side, "", "")
        out[p.speaker_id] = decide(score_candidates(probe, [c for c in pool if c.side == side]), "display_name")
    return out


def resolve_mention(m: ValidMention, context: list[Candidate], pool: list[Candidate], method: str | None = None) -> Link:
    """Match within the call's context first; widen to everyone only if nobody in the context fits at all. An ambiguity
    inside the context stays "not sure": widening never breaks a tie."""
    for candidates, suffix in ((context, ""), (pool, "_widened")):
        how = method or ("same_name_needs_second_clue" if same_name_collision(m, candidates) else "name_title_side")
        link = decide(score_candidates(m, candidates), how + suffix)
        if link.decision != "nil":
            return link
    return link


def resolve_call(crm: CrmBundle, t: Transcript, v: ValidatedCall) -> CallResolution:
    pool = build_pool(crm, t)
    context = context_pool(crm, t, pool)
    speakers = resolve_speakers(crm, t, pool)
    mentions: dict[str, Link] = {}
    for m in v.mentions:
        if m.mention_type == "self_introduction" and m.self_speaker_id:
            spk = speakers.get(m.self_speaker_id)
            if spk and spk.target:
                mentions[m.mention_id] = Link(spk.target, "linked", spk.score, "self_introduction")
                continue
            link = resolve_mention(m, context, pool, "self_introduction")
            mentions[m.mention_id] = link
            if link.target:  # the self-introduction identifies a speaker the recorder could not
                speakers[m.self_speaker_id] = Link(link.target, "linked", link.score, "self_introduction")
            continue
        mentions[m.mention_id] = resolve_mention(m, context, pool)
    strangers = merge_strangers([m for m in v.mentions if mentions[m.mention_id].decision == "nil"])
    return CallResolution(t.call_id, speakers, mentions, strangers)


def _same_stranger(a: ValidMention, b: ValidMention) -> bool:
    """Two not-in-CRM mentions in one call refer to the same person if the conversation gives them the same role,
    or if their first names are near-identical (a transcription variant such as Hendrik / Henrik)."""
    if a.self_speaker_id and b.self_speaker_id and a.self_speaker_id != b.self_speaker_id:
        return False  # two different speakers introduced themselves
    ha, hb = set(fold(a.role_hint).split()) - STOP, set(fold(b.role_hint).split()) - STOP
    if ha and hb and (ha <= hb or hb <= ha):
        return True
    if "title_only" in (a.mention_type, b.mention_type):
        return False
    ta, tb = [w for w in fold(a.surface).split() if w not in STOP], [w for w in fold(b.surface).split() if w not in STOP]
    return bool(ta and tb) and JaroWinkler.normalized_similarity(ta[0], tb[0]) >= SAME_NAME_MIN


def merge_strangers(nil_mentions: list[ValidMention]) -> dict[str, Stranger]:
    """Group a call's unresolved mentions into people (union-find), so one unknown person becomes one node."""
    parent = {m.mention_id: m.mention_id for m in nil_mentions}

    def root(x: str) -> str:
        while parent[x] != x:
            x = parent[x]
        return x

    for i, a in enumerate(nil_mentions):
        for b in nil_mentions[i + 1 :]:
            if _same_stranger(a, b):
                parent[root(b.mention_id)] = root(a.mention_id)

    groups: dict[str, list[ValidMention]] = {}
    for m in nil_mentions:
        groups.setdefault(root(m.mention_id), []).append(m)
    rank = {"full_name": 0, "self_introduction": 1, "first_name_only": 2, "title_only": 3}
    out = {}
    for members in groups.values():
        best = min(members, key=lambda m: (rank.get(m.mention_type, 9), -len(m.surface)))
        aliases = list(dict.fromkeys(m.surface for m in members))
        stranger = Stranger(best.surface, max((m.role_hint for m in members), key=len), aliases)
        for m in members:
            out[m.mention_id] = stranger
    return out
