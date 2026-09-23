"""Contextualization output memo table and its memo-kind values."""

from __future__ import annotations

import datetime
from typing import Literal
from uuid import UUID

from sqlalchemy import CheckConstraint, Column, DateTime, Index, Text, UniqueConstraint, func
from sqlmodel import Field, SQLModel

from aizk.utilities.clock import utcnow

#: The two kinds of contextualization model output the memo retains. The summary
#: output (``summary``) and each per-chunk revision (``revision``) are the only
#: legal discriminator values; a ``CHECK`` constraint on the table and this typed
#: boundary together keep a typo from creating a durable but unreachable entry.
MemoKind = Literal["summary", "revision"]
MEMO_KIND_SUMMARY: MemoKind = "summary"
MEMO_KIND_REVISION: MemoKind = "revision"


class ContextualizationOutputMemo(SQLModel, table=True):
    """Durable memo of validated model output for resuming partial contextualization.

    The key is ``(kind, scope_id, derivation_key)``. ``kind`` distinguishes summary
    and revision outputs; ``output_text`` stores normalized output, and ``''`` is a
    present empty value distinct from a miss. Memo rows are scratch state, not
    product projections, and do not activate runs or outputs.
    """

    __tablename__ = "graph_contextualization_output_memo"
    __table_args__ = (
        UniqueConstraint(
            "kind",
            "scope_id",
            "derivation_key",
            name="uq_graph_contextualization_output_memo_key",
        ),
        Index(
            "ix_graph_contextualization_output_memo_key",
            "kind",
            "scope_id",
            "derivation_key",
        ),
        # Fail closed on an unknown kind so a typo cannot create a durable but
        # unreachable entry; the spec defines only these two discriminator values.
        CheckConstraint(
            f"kind IN ('{MEMO_KIND_SUMMARY}', '{MEMO_KIND_REVISION}')",
            name="ck_graph_contextualization_output_memo_kind",
        ),
    )

    id: int | None = Field(default=None, primary_key=True, nullable=False)
    kind: str = Field(nullable=False)
    scope_id: UUID = Field(nullable=False)
    derivation_key: str = Field(sa_column=Column(Text, nullable=False))
    output_text: str = Field(sa_column=Column(Text, nullable=False))
    created_at: datetime.datetime = Field(
        default_factory=utcnow,
        sa_column=Column(DateTime(), nullable=False, server_default=func.current_timestamp()),
    )
