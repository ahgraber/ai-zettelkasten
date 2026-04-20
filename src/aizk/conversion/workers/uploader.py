"""S3 artifact upload and output record creation."""

from __future__ import annotations

import datetime as dt
import json
import logging
from pathlib import Path

from sqlmodel import Session, select

from aizk.conversion.datamodel.job import ConversionJob, ConversionJobStatus
from aizk.conversion.datamodel.output import ConversionOutput
from aizk.conversion.datamodel.source import Source
from aizk.conversion.db import get_engine
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
)
from aizk.conversion.workers.errors import ConversionArtifactsMissingError
from aizk.conversion.workers.types import _utcnow

logger = logging.getLogger(__name__)


def _upload_converted(job_id: int, workspace: Path, config: ConversionConfig) -> None:
    """Upload artifacts to S3 and record conversion output in the DB."""
    engine = get_engine(config.database_url)
    metadata_file = metadata_path(workspace)
    if not metadata_file.exists():
        raise ConversionArtifactsMissingError(f"Missing metadata for job {job_id}")

    metadata = json.loads(metadata_file.read_text())
    markdown_filename = metadata["markdown_filename"]
    markdown_file = markdown_path(workspace, markdown_filename)
    figure_files = metadata.get("figure_files", [])
    figure_file_paths = figure_paths(workspace, figure_files)

    if not markdown_file.exists():
        raise ConversionArtifactsMissingError(f"Missing markdown for job {job_id}")

    with Session(engine) as session:
        job = session.get(ConversionJob, job_id)
        if not job:
            return
        if job.status == ConversionJobStatus.CANCELLED:
            return
        source = session.exec(select(Source).where(Source.aizk_uuid == job.aizk_uuid)).one()

        # Reuse existing S3 artifacts when the content hash matches a prior output for
        # the same bookmark, avoiding redundant uploads of identical content.
        new_hash = metadata["markdown_hash_xx64"]
        prior_output = session.exec(
            select(ConversionOutput)
            .where(ConversionOutput.aizk_uuid == source.aizk_uuid)
            .where(ConversionOutput.markdown_hash_xx64 == new_hash)
            .order_by(ConversionOutput.created_at.desc())
        ).first()

        if prior_output is not None:
            logger.info(
                "Job %s: content hash matches prior output %s; reusing S3 artifacts at %s",
                job_id,
                prior_output.id,
                prior_output.s3_prefix,
            )
            output = ConversionOutput(
                job_id=job.id,
                aizk_uuid=source.aizk_uuid,
                title=source.title or job.title,
                payload_version=job.payload_version,
                s3_prefix=prior_output.s3_prefix,
                markdown_key=prior_output.markdown_key,
                manifest_key=prior_output.manifest_key,
                markdown_hash_xx64=new_hash,
                figure_count=prior_output.figure_count,
                docling_version=metadata["docling_version"],
                pipeline_name=metadata["pipeline_name"],
            )
            session.add(output)
            job.finished_at = _utcnow()
            job.status = ConversionJobStatus.SUCCEEDED
            job.error_code = None
            job.error_message = None
            job.updated_at = _utcnow()
            session.add(job)
            session.commit()
            return

        s3_client = S3Client(config)
        if not s3_client.bucket:
            raise S3Error("S3 bucket is not configured", "s3_upload_failed")

        prefix = str(source.aizk_uuid)
        s3_prefix_uri = f"s3://{s3_client.bucket}/{prefix}/"
        markdown_key = f"{prefix}/{markdown_filename}"
        markdown_uri = s3_client.upload_file(markdown_file, markdown_key)

        figure_uris: list[str] = []
        for fig_path in figure_file_paths:
            if not fig_path.exists():
                continue
            fig_key = f"{prefix}/figures/{fig_path.name}"
            figure_uris.append(s3_client.upload_file(fig_path, fig_key))

        job.finished_at = _utcnow()

        manifest_local_path = workspace / "manifest.json"

        from pydantic import TypeAdapter

        from aizk.conversion.core.source_ref import SourceRef as _SourceRef

        terminal_ref = TypeAdapter(_SourceRef).validate_python(metadata["terminal_ref"])
        submitted_ref_raw = json.loads(job.source_ref) if job.source_ref else None
        submitted_ref = (
            TypeAdapter(_SourceRef).validate_python(submitted_ref_raw) if submitted_ref_raw else terminal_ref
        )

        converter_name = metadata.get("config_snapshot", {}).get("converter_name", "docling")
        adapter_snapshot = {k: v for k, v in metadata.get("config_snapshot", {}).items() if k != "converter_name"}

        manifest = generate_manifest_v2(
            submitted_ref=submitted_ref,
            terminal_ref=terminal_ref,
            job=job,
            fetched_at=dt.datetime.fromisoformat(metadata["fetched_at"]),
            markdown_s3_uri=markdown_uri,
            markdown_hash=metadata["markdown_hash_xx64"],
            figure_s3_uris=figure_uris,
            docling_version=metadata.get("docling_version", "unknown"),
            pipeline_name=metadata["pipeline_name"],
            converter_name=converter_name,
            adapter_snapshot=adapter_snapshot,
            source_url=source.url,
            source_normalized_url=source.normalized_url,
            source_title=source.title or job.title,
            source_type=source.source_type,
        )

        save_manifest(manifest, manifest_local_path)
        manifest_uri = s3_client.upload_file(manifest_local_path, f"{prefix}/manifest.json")

        session.refresh(job)
        if job.status == ConversionJobStatus.CANCELLED:
            return

        output = ConversionOutput(
            job_id=job.id,
            aizk_uuid=source.aizk_uuid,
            title=source.title or job.title,
            payload_version=job.payload_version,
            s3_prefix=s3_prefix_uri,
            markdown_key=markdown_key,
            manifest_key=f"{prefix}/manifest.json",
            markdown_hash_xx64=metadata["markdown_hash_xx64"],
            figure_count=len(figure_uris),
            docling_version=metadata["docling_version"],
            pipeline_name=metadata["pipeline_name"],
        )
        session.add(output)

        job.status = ConversionJobStatus.SUCCEEDED
        job.error_code = None
        job.error_message = None
        job.updated_at = _utcnow()
        session.add(job)
        session.commit()
