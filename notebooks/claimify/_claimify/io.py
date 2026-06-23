"""Document loader + disk cache (karakeep_id -> source_id -> newest conversion_outputs)."""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
import subprocess
from typing import Literal
from uuid import UUID

from pydantic import TypeAdapter
from sqlmodel import Session, select

from _claimify.models import EvalRecord, ExtractionRecord, LoadedDoc
from aizk.conversion.datamodel.output import ConversionOutput
from aizk.conversion.datamodel.source import Source
from aizk.conversion.storage.s3_client import S3Client
from aizk.conversion.utilities.config import ConversionConfig
from aizk.db.config import DatabaseConfig
from aizk.db.engine import get_engine


def resolve_repo_root() -> Path:
    return Path(subprocess.check_output(["git", "rev-parse", "--show-toplevel"], text=True).strip())


DATA_DIR = resolve_repo_root() / "data" / "claimify_demo"
CACHE_DIR = DATA_DIR / "cache"
EXTRACTION_DIR = DATA_DIR / "extraction"
EVALUATION_DIR = DATA_DIR / "evaluation"

for _d in (CACHE_DIR, EXTRACTION_DIR, EVALUATION_DIR):
    _d.mkdir(parents=True, exist_ok=True)


def ensure_punkt_tab() -> None:
    """Download NLTK's punkt_tab once if absent; no-op otherwise.

    Kept out of module import so hermetic tests can import `io` without
    touching the network.
    """
    import nltk

    try:
        nltk.data.find("tokenizers/punkt_tab")
    except LookupError:
        nltk.download("punkt_tab", quiet=True)


def resolve_doc(
    karakeep_id: str,
    session: Session,
    *,
    s3_client: S3Client | None = None,
) -> LoadedDoc:
    """Resolve a KaraKeep bookmark id to a `LoadedDoc`, fetching from cache or S3."""
    bookmark = session.exec(select(Source).where(Source.karakeep_id == karakeep_id)).one_or_none()
    if bookmark is None:
        raise ValueError(f"No bookmark with karakeep_id={karakeep_id!r}")

    output = session.exec(
        select(ConversionOutput)
        .where(ConversionOutput.source_id == bookmark.source_id)
        .order_by(ConversionOutput.created_at.desc())
        .limit(1)
    ).one_or_none()
    if output is None:
        raise ValueError(f"No conversion_outputs for karakeep_id={karakeep_id!r} (source_id={bookmark.source_id})")

    cache_path = CACHE_DIR / f"{bookmark.source_id}.md"
    source: Literal["cache", "s3"]
    if cache_path.exists():
        markdown = cache_path.read_text(encoding="utf-8")
        source = "cache"
    else:
        if s3_client is None:
            s3_client = S3Client(ConversionConfig())
        markdown = s3_client.get_object_bytes(output.markdown_key).decode("utf-8")
        cache_path.write_text(markdown, encoding="utf-8")
        source = "s3"

    return LoadedDoc(
        source_id=bookmark.source_id,
        karakeep_id=karakeep_id,
        title=output.title,
        markdown=markdown,
        source=source,
    )


def load_docs(karakeep_ids: list[str]) -> list[LoadedDoc]:
    """Batch-resolve bookmark ids using a single DB session and S3 client."""
    config = ConversionConfig()
    engine = get_engine(DatabaseConfig().database_url)
    s3_client = S3Client(config)
    with Session(engine) as session:
        return [resolve_doc(kid, session, s3_client=s3_client) for kid in karakeep_ids]


_EXTRACTION_ADAPTER: TypeAdapter[ExtractionRecord] = TypeAdapter(ExtractionRecord)


def extraction_path(doc_uuid: UUID) -> Path:
    return EXTRACTION_DIR / f"{doc_uuid}.jsonl"


def write_extraction_jsonl(doc_uuid: UUID, records: Iterable[ExtractionRecord]) -> Path:
    """Write one JSON line per extraction record; each line carries a `kind` discriminant."""
    path = extraction_path(doc_uuid)
    with path.open("w", encoding="utf-8") as fh:
        for record in records:
            fh.write(_EXTRACTION_ADAPTER.dump_json(record).decode("utf-8"))
            fh.write("\n")
    return path


def read_extraction_jsonl(doc_uuid: UUID) -> list[ExtractionRecord]:
    """Read a JSONL extraction file back into discriminated-union records."""
    path = extraction_path(doc_uuid)
    records: list[ExtractionRecord] = []
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            records.append(_EXTRACTION_ADAPTER.validate_json(line))
    return records


_EVAL_ADAPTER: TypeAdapter[EvalRecord] = TypeAdapter(EvalRecord)


def evaluation_path(doc_uuid: UUID) -> Path:
    return EVALUATION_DIR / f"{doc_uuid}.jsonl"


def write_evaluation_jsonl(doc_uuid: UUID, records: Iterable[EvalRecord]) -> Path:
    """Write one JSON line per eval verdict; each line carries `kind="verdict"`."""
    path = evaluation_path(doc_uuid)
    with path.open("w", encoding="utf-8") as fh:
        for record in records:
            fh.write(_EVAL_ADAPTER.dump_json(record).decode("utf-8"))
            fh.write("\n")
    return path


def read_evaluation_jsonl(doc_uuid: UUID) -> list[EvalRecord]:
    path = evaluation_path(doc_uuid)
    records: list[EvalRecord] = []
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            records.append(_EVAL_ADAPTER.validate_json(line))
    return records
