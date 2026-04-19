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


class InvalidSentenceVerdict(BaseModel):
    """Per-sentence invalidity verdict from the invalid_sentences prompt."""

    is_invalid: bool
    reasoning: str


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


class InvalidClaimVerdict(BaseModel):
    """Per-claim invalidity verdict from the invalid_claims prompt."""

    is_invalid: bool
    reasoning: str


class InvalidClaimsResult(BaseModel):
    per_claim_invalid: list[bool]


ExtractionPhase = Literal[
    "contextualize",
    "selection",
    "disambiguation",
    "decomposition",
]
EvaluationPhase = Literal[
    "invalid_sentence",
    "element",
    "coverage",
    "entailment",
    "decontextualization",
    "invalid_claim",
]


class UsageSample(BaseModel):
    """Per-LLM-call usage accounting.

    Token fields come from `pydantic_ai.usage.RunUsage`; `cost_usd` is pulled
    from the final `ModelResponse.provider_details` when OpenRouter is asked
    to include usage (`extra_body={"usage": {"include": True}}`).
    """

    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cost_usd: float | None = None
    requests: int = 1

    @classmethod
    def zero(cls, model: str = "stub") -> UsageSample:
        """Empty sample for tests and stub runners."""
        return cls(model=model)


class UsageRecord(BaseModel):
    kind: Literal["usage"] = "usage"
    doc_uuid: UUID
    section_idx: int
    sentence_idx: int | None
    claim_idx: int | None
    phase: ExtractionPhase
    usage: UsageSample


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
    ClaimRecord | SkippedRecord | FailedRecord | UsageRecord,
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
    usage: UsageSample | None = None
