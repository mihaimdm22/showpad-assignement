"""Input contracts: the CRM export bundle, recorder transcripts and tenant vocabularies."""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, model_validator

REPO_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = REPO_ROOT / "data"


class Participant(BaseModel):
    speaker_id: str
    display_name: str
    email: str | None = None
    affiliation: Literal["internal", "external", "unknown"] = "unknown"


class Turn(BaseModel):
    turn_id: str
    speaker_id: str
    start_ms: int
    end_ms: int
    text: str


class Transcript(BaseModel):
    tenant_id: str
    call_id: str
    title: str
    started_at: datetime
    crm_refs: dict[str, str]
    participants: list[Participant]
    turns: list[Turn]

    @model_validator(mode="after")
    def _consistent(self) -> Transcript:
        speakers = {p.speaker_id for p in self.participants}
        last_start = -1
        for t in self.turns:
            if t.speaker_id not in speakers:
                raise ValueError(f"{self.call_id}/{t.turn_id}: unknown speaker {t.speaker_id}")
            if not (last_start < t.start_ms < t.end_ms):
                raise ValueError(f"{self.call_id}/{t.turn_id}: timestamps not monotonic")
            last_start = t.start_ms
        return self

    def turn(self, turn_id: str) -> Turn | None:
        return next((t for t in self.turns if t.turn_id == turn_id), None)

    def participant(self, speaker_id: str) -> Participant | None:
        return next((p for p in self.participants if p.speaker_id == speaker_id), None)


class Competitor(BaseModel):
    slug: str
    name: str
    aliases: list[str] = []


class PainTheme(BaseModel):
    slug: str
    label: str
    description: str


# Words that make up generic references to "some other vendor" rather than a named competitor.
GENERIC_VENDOR_WORDS = {
    "another",
    "other",
    "others",
    "some",
    "a",
    "an",
    "the",
    "our",
    "their",
    "current",
    "existing",
    "previous",
    "old",
    "provider",
    "providers",
    "vendor",
    "vendors",
    "competitor",
    "competitors",
    "competition",
    "solution",
    "solutions",
    "tool",
    "tools",
    "company",
    "companies",
    "supplier",
    "suppliers",
    "platform",
    "platforms",
    "system",
    "systems",
}


class TenantConfig(BaseModel):
    tenant_id: str
    seller_company: str
    product: str
    competitors: list[Competitor]
    pain_themes: list[PainTheme]
    products: list[str] = []  # the seller's own product names (production: the CRM product catalogue); never competitors

    def competitor_slug(self, spoken_name: str) -> str | None:
        """Map a spoken competitor name to the controlled vocabulary, or None if unknown.
        Exact match on name, slug or alias first; then a unique word-prefix match ("Adaptive" -> "Adaptive Design Solutions")."""
        key = _fold(spoken_name)
        names = {c.slug: {_fold(c.name), _fold(c.slug), *(_fold(a) for a in c.aliases)} for c in self.competitors}
        for slug, keys in names.items():
            if key in keys:
                return slug
        prefixed = {slug for slug, keys in names.items() if key and any(k.startswith(key + " ") for k in keys)}
        return prefixed.pop() if len(prefixed) == 1 else None

    def is_own_product(self, name: str) -> bool:
        key = _fold(name)
        return bool(key) and any(key == _fold(p) or key.startswith(_fold(p) + " ") for p in [self.seller_company, *self.products])

    @staticmethod
    def is_generic_vendor(name: str) -> bool:
        words = _fold(name).split()
        return not words or all(w in GENERIC_VENDOR_WORDS for w in words)


class CrmBundle(BaseModel):
    """A Salesforce-style export: one list of records per object type, plus export metadata."""

    tenant_id: str
    export_ts: datetime
    objects: dict[str, list[dict]]

    def records(self, object_type: str) -> list[dict]:
        return self.objects.get(object_type, [])

    def by_id(self, object_type: str, record_id: str) -> dict | None:
        return next((r for r in self.records(object_type) if r.get("Id") == record_id), None)


def _fold(s: str) -> str:
    """Case, hyphens, punctuation and possessives folded: "Quantum Circuits Inc.'s" -> "quantum circuits inc"."""
    s = s.casefold().replace("’", "'").replace("-", " ")
    s = re.sub(r"'s\b", "", s)
    return " ".join(re.sub(r"[^\w\s]", " ", s).split())


def vendor_key(name: str) -> str:
    """Normalised key for a vendor or product name (case, punctuation and possessives ignored)."""
    return _fold(name)


def load_crm(path: Path = DATA_DIR / "crm" / "kestrel_crm.json") -> CrmBundle:
    raw = json.loads(path.read_text())
    meta = raw.pop("_meta")
    return CrmBundle(tenant_id=meta["tenant_id"], export_ts=meta["export_ts"], objects=raw)


def load_transcript(path: Path) -> Transcript:
    return Transcript.model_validate_json(path.read_text())


def load_transcripts(directory: Path = DATA_DIR / "calls") -> list[Transcript]:
    return [load_transcript(p) for p in sorted(directory.glob("*.json"))]


def load_config(path: Path = DATA_DIR / "tenant" / "kestrel_config.json") -> TenantConfig:
    raw = json.loads(path.read_text())
    raw.pop("_meta", None)
    return TenantConfig.model_validate(raw)


def load_gold(path: Path = DATA_DIR / "gold" / "answer_key.json") -> dict:
    return json.loads(path.read_text())
