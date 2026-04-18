"""Unit tests for conversion SQLModel entities."""

from sqlmodel import SQLModel

from aizk.conversion.datamodel.job import ConversionJob
from aizk.conversion.datamodel.output import ConversionOutput
from aizk.conversion.datamodel.source import Source


def test_indexed_columns():
    tables = SQLModel.metadata.tables
    source_table = tables["sources"]
    job_table = tables["conversion_jobs"]
    output_table = tables["conversion_outputs"]

    assert source_table.columns["karakeep_id"].index is True
    assert source_table.columns["aizk_uuid"].index is True
    assert source_table.columns["normalized_url"].index is True
    assert source_table.columns["source_ref_hash"].index is True

    assert job_table.columns["aizk_uuid"].index is True
    assert job_table.columns["status"].index is True
    assert job_table.columns["idempotency_key"].index is True
    assert job_table.columns["earliest_next_attempt_at"].index is True
    assert job_table.columns["created_at"].index is True

    assert output_table.columns["job_id"].index is True
    assert output_table.columns["aizk_uuid"].index is True
    assert output_table.columns["markdown_hash_xx64"].index is True
    assert output_table.columns["created_at"].index is True


def test_karakeep_id_nullable():
    tables = SQLModel.metadata.tables
    source_table = tables["sources"]
    assert source_table.columns["karakeep_id"].nullable is True


def test_source_ref_columns_present():
    tables = SQLModel.metadata.tables
    source_table = tables["sources"]
    job_table = tables["conversion_jobs"]
    assert "source_ref" in source_table.columns
    assert "source_ref_hash" in source_table.columns
    assert "source_ref" in job_table.columns


def test_job_fk_points_to_sources():
    tables = SQLModel.metadata.tables
    job_table = tables["conversion_jobs"]
    fks = list(job_table.foreign_keys)
    fk_targets = {str(fk.column) for fk in fks}
    assert any("sources.aizk_uuid" in target for target in fk_targets), fk_targets
