"""S3 artifact upload and output record creation."""

from __future__ import annotations

from dataclasses import dataclass
import datetime as dt
import errno
import json
import logging
import os
from pathlib import Path
from uuid import UUID

from sqlmodel import Session, select

from aizk.conversion.core.errors import MissingOwnerOnJob, WorkspaceEscape
from aizk.conversion.datamodel.events import (
    ConversionEventKind,
    SucceededPayload,
    record_transition,
)
from aizk.conversion.datamodel.job import ConversionJob, ConversionJobStatus
from aizk.conversion.datamodel.output import ConversionOutput
from aizk.conversion.datamodel.source import Source
from aizk.conversion.db import get_engine
from aizk.conversion.processing.errors import ConversionArtifactsMissingError, SubprocessMetadataInvalid
from aizk.conversion.processing.types import SubprocessMetadata, _utcnow
from aizk.conversion.storage.manifest import (
    generate_manifest_v2,
    save_manifest,
)
from aizk.conversion.storage.s3_client import S3Client, S3Error
from aizk.conversion.utilities.config import ConversionConfig
from aizk.conversion.utilities.paths import (
    figure_paths,
    markdown_path,
    metadata_path,
    read_text_nofollow,
)

logger = logging.getLogger(__name__)


def _upload_nofollow(path: Path, s3_key: str, s3_client: S3Client) -> str:
    """Open ``path`` with ``O_NOFOLLOW`` and upload its contents to S3.

    Eliminates the TOCTOU race between the :func:`_assert_within` containment
    check and the actual file read: if a malicious subprocess replaces the file
    with a symlink after validation, ``O_NOFOLLOW`` causes ``os.open`` to fail
    with ``ELOOP`` rather than following the link.

    Args:
        path: Validated workspace-local path (must already pass
            :func:`~aizk.conversion.utilities.paths._assert_within`).
        s3_key: Destination S3 object key.
        s3_client: Configured :class:`~aizk.conversion.storage.s3_client.S3Client`.

    Returns:
        S3 URI of the uploaded object.

    Raises:
        WorkspaceEscape: If ``path`` is a symlink at open time (``ELOOP``).
        OSError: For other filesystem errors.
        S3UploadError: If the upload fails.
    """
    try:
        fd = os.open(str(path), os.O_RDONLY | os.O_NOFOLLOW)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise WorkspaceEscape(f"Symlink detected at open time — possible TOCTOU: {path}") from exc
        raise
    with os.fdopen(fd, "rb") as f:
        return s3_client.upload_fileobj(f, s3_key)


@dataclass(frozen=True)
class _UploadPlan:
    """Materialized inputs for the S3-PUT phase of upload.

    Built once per job by ``_prepare_upload``; re-executed on retry by
    ``_execute_upload``. S3 PUTs against the same key are idempotent, so
    re-running the plan against S3 on a transient failure is safe.

    On the content-hash dedup shortcut, ``markdown_local`` is ``None`` and
    ``figure_uploads`` is empty: the bytes are already in S3 at
    ``markdown_key`` and the figure prefix from a prior conversion. Only the
    manifest is uploaded, reflecting THIS job's source metadata.
    """

    aizk_uuid: UUID
    owner_id: str
    title: str
    payload_version: int
    s3_prefix_uri: str
    markdown_local: Path | None
    markdown_key: str
    figure_uploads: tuple[tuple[Path, str], ...]
    manifest_local: Path
    manifest_key: str
    markdown_hash_xx64: str
    figure_count: int
    docling_version: str
    pipeline_name: str


def _load_subprocess_metadata(workspace: Path, job_id: int) -> SubprocessMetadata:
    """Read and validate metadata.json from workspace as SubprocessMetadata.

    Raises:
        ConversionArtifactsMissingError: If metadata.json does not exist.
        SubprocessMetadataInvalid: If the JSON does not validate against SubprocessMetadata.
    """
    from pydantic import ValidationError

    metadata_file = metadata_path(workspace)
    if not metadata_file.exists():
        raise ConversionArtifactsMissingError(f"Missing metadata for job {job_id}")
    raw = read_text_nofollow(metadata_file)
    try:
        return SubprocessMetadata.model_validate_json(raw)
    except ValidationError as exc:
        raise SubprocessMetadataInvalid(
            f"metadata.json failed SubprocessMetadata validation for job {job_id}: {exc}"
        ) from exc


def _prepare_upload(job_id: int, workspace: Path, config: ConversionConfig) -> _UploadPlan | None:
    """Build the upload plan; runs exactly once per job, before the retry loop.

    Returns ``None`` when the job is already finalized (content-hash shortcut
    hit, job row missing, or job cancelled) and the retry loop should be skipped.
    """
    engine = get_engine(config.database_url)
    subprocess_meta = _load_subprocess_metadata(workspace, job_id)

    markdown_filename = subprocess_meta.markdown_filename
    markdown_file = markdown_path(workspace, markdown_filename)
    figure_file_paths = figure_paths(workspace, subprocess_meta.figure_files)

    if not markdown_file.exists():
        raise ConversionArtifactsMissingError(f"Missing markdown for job {job_id}")

    if not config.s3_bucket_name:
        raise S3Error("S3 bucket is not configured", "s3_upload_failed")

    from pydantic import TypeAdapter, ValidationError

    from aizk.conversion.core.source_ref import SourceRef as _SourceRef
    from aizk.conversion.core.types import SOURCE_TYPE_BY_KIND

    try:
        terminal_ref: _SourceRef = TypeAdapter(_SourceRef).validate_python(subprocess_meta.terminal_ref)
    except ValidationError as exc:
        raise SubprocessMetadataInvalid(f"Invalid terminal_ref in metadata.json for job {job_id}: {exc}") from exc
    source_type = SOURCE_TYPE_BY_KIND.get(terminal_ref.kind, "other")
    source_meta = subprocess_meta.source_meta.to_source_metadata()

    with Session(engine) as session:
        job = session.get(ConversionJob, job_id)
        if not job:
            return None
        if job.status == ConversionJobStatus.CANCELLED:
            return None
        source = session.exec(select(Source).where(Source.aizk_uuid == job.aizk_uuid)).one()

        # Reuse existing S3 artifacts when the content hash matches a prior output for
        # the same bookmark, avoiding redundant uploads of identical content.
        new_hash = subprocess_meta.markdown_hash_xx64
        prior_output = session.exec(
            select(ConversionOutput)
            .where(ConversionOutput.aizk_uuid == source.aizk_uuid)
            .where(ConversionOutput.markdown_hash_xx64 == new_hash)
            .order_by(ConversionOutput.created_at.desc())
        ).first()

        output_title = subprocess_meta.source_title or job.title

        bucket = config.s3_bucket_name
        prefix = str(source.aizk_uuid)
        manifest_local_path = workspace / "manifest.json"

        submitted_ref_raw = json.loads(job.source_ref) if job.source_ref else None
        submitted_ref = (
            TypeAdapter(_SourceRef).validate_python(submitted_ref_raw) if submitted_ref_raw else terminal_ref
        )
        converter_name = subprocess_meta.config_snapshot.get("converter_name", "docling")
        adapter_snapshot = {k: v for k, v in subprocess_meta.config_snapshot.items() if k != "converter_name"}

        if prior_output is not None:
            # Content-hash dedup shortcut. Markdown + figure bytes are reused from
            # the prior conversion (already in S3 at content-addressed keys). Only
            # the manifest is regenerated and uploaded so the source block reflects
            # THIS job's SubprocessMetadata — serving the prior manifest would
            # publish stale source values for the current job (violates
            # conversion-worker spec § "Persist conversion config and source
            # provenance" and the universal scenario "Manifest values independent
            # of Source-row state").
            #
            # The manifest PUT is routed through ``_execute_upload`` so it inherits
            # the worker's upload retry policy; ``_prepare_upload`` is one-shot and
            # not retried.
            logger.info(
                "Job %s: content hash matches prior output %s; reusing S3 artifacts at %s",
                job_id,
                prior_output.id,
                prior_output.s3_prefix,
            )
            if not job.owner_id:
                raise MissingOwnerOnJob(f"Job {job_id} has no owner_id; refusing to create Output row")

            figure_uris = [
                f"s3://{bucket}/{prefix}/figures/{Path(name).name}" for name in subprocess_meta.figure_files
            ]
            manifest = generate_manifest_v2(
                submitted_ref=submitted_ref,
                terminal_ref=terminal_ref,
                job=job,
                fetched_at=dt.datetime.fromisoformat(subprocess_meta.fetched_at),
                markdown_s3_uri=f"s3://{bucket}/{prior_output.markdown_key}",
                markdown_hash=subprocess_meta.markdown_hash_xx64,
                figure_s3_uris=figure_uris,
                docling_version=subprocess_meta.docling_version,
                pipeline_name=subprocess_meta.pipeline_name,
                converter_name=converter_name,
                adapter_snapshot=adapter_snapshot,
                source_url=source_meta.source_url,
                source_normalized_url=source_meta.normalized_url,
                source_title=subprocess_meta.source_title,
                source_type=source_type,
            )
            save_manifest(manifest, manifest_local_path)

            return _UploadPlan(
                aizk_uuid=source.aizk_uuid,
                owner_id=job.owner_id,
                title=output_title,
                payload_version=job.payload_version,
                s3_prefix_uri=prior_output.s3_prefix,
                markdown_local=None,
                markdown_key=prior_output.markdown_key,
                figure_uploads=(),
                manifest_local=manifest_local_path,
                manifest_key=prior_output.manifest_key,
                markdown_hash_xx64=new_hash,
                figure_count=prior_output.figure_count,
                docling_version=subprocess_meta.docling_version,
                pipeline_name=subprocess_meta.pipeline_name,
            )

        s3_prefix_uri = f"s3://{bucket}/{prefix}/"
        markdown_key = f"{prefix}/{markdown_filename}"
        markdown_uri = f"s3://{bucket}/{markdown_key}"

        figure_uploads: list[tuple[Path, str]] = []
        figure_uris: list[str] = []
        for fig_path in figure_file_paths:
            if not fig_path.exists():
                continue
            fig_key = f"{prefix}/figures/{fig_path.name}"
            figure_uploads.append((fig_path, fig_key))
            figure_uris.append(f"s3://{bucket}/{fig_key}")

        manifest_key = f"{prefix}/manifest.json"

        manifest = generate_manifest_v2(
            submitted_ref=submitted_ref,
            terminal_ref=terminal_ref,
            job=job,
            fetched_at=dt.datetime.fromisoformat(subprocess_meta.fetched_at),
            markdown_s3_uri=markdown_uri,
            markdown_hash=subprocess_meta.markdown_hash_xx64,
            figure_s3_uris=figure_uris,
            docling_version=subprocess_meta.docling_version,
            pipeline_name=subprocess_meta.pipeline_name,
            converter_name=converter_name,
            adapter_snapshot=adapter_snapshot,
            source_url=source_meta.source_url,
            source_normalized_url=source_meta.normalized_url,
            source_title=subprocess_meta.source_title,
            source_type=source_type,
        )
        save_manifest(manifest, manifest_local_path)

        if not job.owner_id:
            raise MissingOwnerOnJob(f"Job {job_id} has no owner_id; refusing to build upload plan")
        return _UploadPlan(
            aizk_uuid=source.aizk_uuid,
            owner_id=job.owner_id,
            title=output_title,
            payload_version=job.payload_version,
            s3_prefix_uri=s3_prefix_uri,
            markdown_local=markdown_file,
            markdown_key=markdown_key,
            figure_uploads=tuple(figure_uploads),
            manifest_local=manifest_local_path,
            manifest_key=manifest_key,
            markdown_hash_xx64=new_hash,
            figure_count=len(figure_uploads),
            docling_version=subprocess_meta.docling_version,
            pipeline_name=subprocess_meta.pipeline_name,
        )


def _execute_upload(plan: _UploadPlan, job_id: int, config: ConversionConfig) -> None:
    """Run the S3 PUTs and finalize the job. Safe to retry: PUTs are key-idempotent.

    On the content-hash dedup shortcut, ``plan.markdown_local`` is ``None`` and
    ``plan.figure_uploads`` is empty — those bytes are already in S3 from a
    prior conversion. Only the manifest is uploaded.
    """
    engine = get_engine(config.database_url)
    s3_client = S3Client(config)

    if plan.markdown_local is not None:
        _upload_nofollow(plan.markdown_local, plan.markdown_key, s3_client)
    for fig_local, fig_key in plan.figure_uploads:
        _upload_nofollow(fig_local, fig_key, s3_client)
    # Upload via _upload_nofollow so a post-write symlink swap can't
    # exfiltrate a host-readable file under the manifest S3 key.
    _upload_nofollow(plan.manifest_local, plan.manifest_key, s3_client)

    with Session(engine) as session:
        job = session.get(ConversionJob, job_id)
        if not job:
            return
        if job.status == ConversionJobStatus.CANCELLED:
            return

        output = ConversionOutput(
            job_id=job_id,
            aizk_uuid=plan.aizk_uuid,
            owner_id=plan.owner_id,
            title=plan.title,
            payload_version=plan.payload_version,
            s3_prefix=plan.s3_prefix_uri,
            markdown_key=plan.markdown_key,
            manifest_key=plan.manifest_key,
            markdown_hash_xx64=plan.markdown_hash_xx64,
            figure_count=plan.figure_count,
            docling_version=plan.docling_version,
            pipeline_name=plan.pipeline_name,
        )
        session.add(output)
        # Flush so the succeeded event's payload can carry the output_id;
        # without this, output.id is None until commit and the audit
        # record loses the link to the artifact row.
        session.flush()

        job.finished_at = _utcnow()
        job.error_code = None
        job.error_message = None
        job.updated_at = _utcnow()
        record_transition(
            session,
            job,
            to_status=ConversionJobStatus.SUCCEEDED,
            kind=ConversionEventKind.SUCCEEDED,
            attempt=job.attempts,
            payload=SucceededPayload(
                output_id=output.id,
                content_hash=plan.markdown_hash_xx64,
            ),
        )
        session.commit()


def _upload_converted(job_id: int, workspace: Path, config: ConversionConfig) -> None:
    """Run the full upload phase (prepare + execute) in one shot.

    Convenience wrapper for direct callers (tests, CLI). The production
    worker calls ``_prepare_upload`` once before its retry loop and
    ``_execute_upload`` inside the loop, so a transient S3 failure on the
    first attempt does not poison the second attempt's local manifest write.
    """
    plan = _prepare_upload(job_id, workspace, config)
    if plan is None:
        return
    _execute_upload(plan, job_id, config)
