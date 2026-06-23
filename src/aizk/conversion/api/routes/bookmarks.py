"""API routes for bookmark resources."""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from sqlmodel import Session, select

from fastapi import APIRouter, Depends, Query

from aizk.conversion.api.dependencies import get_db_session, get_principal
from aizk.conversion.api.schemas import OutputResponse
from aizk.conversion.auth import Principal
from aizk.conversion.datamodel.output import ConversionOutput

router = APIRouter(prefix="/v1/bookmarks", tags=["bookmarks"])


@router.get("/{source_id}/outputs", response_model=list[OutputResponse])
def get_bookmark_outputs(
    source_id: UUID,
    session: Annotated[Session, Depends(get_db_session)],
    principal: Annotated[Principal, Depends(get_principal)],
    latest: Annotated[bool, Query()] = False,
) -> list[OutputResponse]:
    """Return conversion outputs for a bookmark ordered by creation time descending.

    Pass ``?latest=true`` to receive only the most recently created output.
    Output rows are scoped to ``ConversionOutput.owner_id`` so a shared Source
    cannot leak another principal's outputs.
    """
    query = (
        select(ConversionOutput)
        .where(ConversionOutput.source_id == source_id)
        .where(ConversionOutput.owner_id == principal.subject)
        .order_by(ConversionOutput.created_at.desc())
    )
    if latest:
        query = query.limit(1)
    outputs = session.exec(query).all()
    return [OutputResponse.model_validate(o, from_attributes=True) for o in outputs]
