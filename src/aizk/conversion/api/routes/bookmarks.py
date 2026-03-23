"""API routes for bookmark resources."""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from sqlmodel import Session, select

from fastapi import APIRouter, Depends, Query

from aizk.conversion.api.dependencies import get_db_session
from aizk.conversion.api.schemas import OutputResponse
from aizk.conversion.datamodel.output import ConversionOutput

router = APIRouter(prefix="/v1/bookmarks", tags=["bookmarks"])


@router.get("/{aizk_uuid}/outputs", response_model=list[OutputResponse])
def get_bookmark_outputs(
    aizk_uuid: UUID,
    session: Annotated[Session, Depends(get_db_session)],
    latest: Annotated[bool, Query()] = False,
) -> list[OutputResponse]:
    """Return conversion outputs for a bookmark ordered by creation time descending.

    Pass ``?latest=true`` to receive only the most recently created output.
    """
    query = (
        select(ConversionOutput)
        .where(ConversionOutput.aizk_uuid == aizk_uuid)
        .order_by(ConversionOutput.created_at.desc())
    )
    if latest:
        query = query.limit(1)
    outputs = session.exec(query).all()
    return [OutputResponse.model_validate(o, from_attributes=True) for o in outputs]
