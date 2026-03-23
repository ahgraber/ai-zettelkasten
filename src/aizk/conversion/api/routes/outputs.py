"""API routes for serving conversion output artifacts from S3."""

from __future__ import annotations

from typing import Annotated

from sqlmodel import Session

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import Response

from aizk.conversion.api.dependencies import get_db_session, get_s3_client
from aizk.conversion.datamodel.output import ConversionOutput
from aizk.conversion.storage.s3_client import S3Client, S3Error, S3NotFoundError

router = APIRouter(prefix="/v1/outputs", tags=["outputs"])

_FIGURE_CONTENT_TYPES: dict[str, str] = {
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "gif": "image/gif",
    "webp": "image/webp",
}


def _get_output_or_404(session: Session, output_id: int) -> ConversionOutput:
    output = session.get(ConversionOutput, output_id)
    if not output:
        raise HTTPException(status_code=404, detail={"error": "output_not_found", "message": "Output not found"})
    return output


def _fetch_or_raise(s3_client: S3Client, key: str) -> bytes:
    try:
        return s3_client.get_object_bytes(key)
    except S3NotFoundError as e:
        raise HTTPException(
            status_code=404, detail={"error": "artifact_not_found", "message": "Artifact not found in storage"}
        ) from e
    except S3Error as e:
        raise HTTPException(status_code=502, detail={"error": "storage_error", "message": "Storage error"}) from e


@router.get("/{output_id}/manifest")
def get_output_manifest(
    output_id: int,
    session: Annotated[Session, Depends(get_db_session)],
    s3_client: Annotated[S3Client, Depends(get_s3_client)],
) -> Response:
    """Return the raw manifest JSON for a conversion output."""
    output = _get_output_or_404(session, output_id)
    data = _fetch_or_raise(s3_client, output.manifest_key)
    return Response(content=data, media_type="application/json")


@router.get("/{output_id}/markdown")
def get_output_markdown(
    output_id: int,
    session: Annotated[Session, Depends(get_db_session)],
    s3_client: Annotated[S3Client, Depends(get_s3_client)],
) -> Response:
    """Return the converted markdown for a conversion output."""
    output = _get_output_or_404(session, output_id)
    data = _fetch_or_raise(s3_client, output.markdown_key)
    return Response(content=data, media_type="text/markdown; charset=utf-8")


@router.get("/{output_id}/figures/{filename}")
def get_output_figure(
    output_id: int,
    filename: str,
    session: Annotated[Session, Depends(get_db_session)],
    s3_client: Annotated[S3Client, Depends(get_s3_client)],
) -> Response:
    """Return a figure image for a conversion output by bare filename."""
    if not filename or "/" in filename:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "invalid_filename", "message": "Filename must be a bare name with no path separators"},
        )
    output = _get_output_or_404(session, output_id)
    if output.figure_count == 0:
        raise HTTPException(status_code=404, detail={"error": "no_figures", "message": "This output has no figures"})
    key = f"{output.s3_prefix}/figures/{filename}"
    data = _fetch_or_raise(s3_client, key)
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    media_type = _FIGURE_CONTENT_TYPES.get(ext, "application/octet-stream")
    return Response(content=data, media_type=media_type)
