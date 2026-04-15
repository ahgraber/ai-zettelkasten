"""Pydantic models for the Claimify demo (extraction + evaluation records)."""

from __future__ import annotations

from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class Section(BaseModel):
    model_config = ConfigDict(frozen=True)

    heading_path: tuple[str, ...]
    content: str
    start_index: int
    end_index: int


class SentenceContext(BaseModel):
    model_config = ConfigDict(frozen=True)

    sentence: str
    preceding: str
    following: str
    excerpt: str
    section_idx: int
    sentence_idx: int


class SelectionResult(BaseModel):
    contains_proposition: bool
    rewritten_sentence: str | None
    reasoning: str


class DisambigResult(BaseModel):
    can_be_disambiguated: bool
    decontextualized_sentence: str | None
    reasoning: str


class AtomicClaim(BaseModel):
    proposition: str
    essential_context: str | None = None


class DecompositionResult(BaseModel):
    claims: list[AtomicClaim]


class ExtractedClaim(BaseModel):
    doc_uuid: UUID
    heading_path: list[str]
    section_idx: int
    sentence_idx: int
    sentence: str
    claim: AtomicClaim
    context_str: str


class SkippedArtifact(BaseModel):
    doc_uuid: UUID
    kind: Literal["table", "code", "image"]
    position: int
    note: str


class FailedExtraction(BaseModel):
    doc_uuid: UUID
    section_idx: int
    sentence_idx: int
    stage: Literal["selection", "disambiguation", "decomposition", "contextualize"]
    error: str


class LoadedDoc(BaseModel):
    aizk_uuid: UUID
    karakeep_id: str
    title: str
    markdown: str
    source: Literal["cache", "s3"]


class InvalidSentencesResult(BaseModel):
    per_sentence_invalid: list[bool]


class ElementResult(BaseModel):
    elements: list[str]


class CoverageResult(BaseModel):
    per_element_covered: list[bool]


class EntailmentResult(BaseModel):
    entailed: bool
    reasoning: str


class DecontextResult(BaseModel):
    c_max_text: str
    reasoning: str


class InvalidClaimsResult(BaseModel):
    per_claim_invalid: list[bool]


class ClaimRecord(BaseModel):
    kind: Literal["claim"] = "claim"
    claim: ExtractedClaim


class SkippedRecord(BaseModel):
    kind: Literal["skipped"] = "skipped"
    artifact: SkippedArtifact


class FailedRecord(BaseModel):
    kind: Literal["failed"] = "failed"
    failure: FailedExtraction


ExtractionRecord = Annotated[
    ClaimRecord | SkippedRecord | FailedRecord,
    Field(discriminator="kind"),
]


class EvalRecord(BaseModel):
    kind: Literal["verdict"] = "verdict"
    doc_uuid: UUID
    section_idx: int
    sentence_idx: int
    claim_idx: int | None
    dimension: str
    model: str
    result_json: dict
    raw: str | None
