"""Unit tests for conversion SQLModel entities."""

from sqlmodel import SQLModel


def test_indexed_columns():
    tables = SQLModel.metadata.tables
    source_table = tables["sources"]
    job_table = tables["conversion_jobs"]
    output_table = tables["conversion_outputs"]

    assert source_table.columns["karakeep_id"].index is True
    assert source_table.columns["source_id"].index is True
    assert source_table.columns["normalized_url"].index is True

    job_single_column_indexes = {tuple(index.columns.keys()) for index in job_table.indexes if len(index.columns) == 1}
    assert ("source_id",) in job_single_column_indexes
    assert ("status",) in job_single_column_indexes
    assert ("idempotency_key",) in job_single_column_indexes
    assert ("earliest_next_attempt_at",) in job_single_column_indexes
    assert ("created_at",) in job_single_column_indexes

    assert output_table.columns["job_id"].index is True
    assert output_table.columns["source_id"].index is True
    assert output_table.columns["markdown_hash_xx64"].index is True
    assert output_table.columns["created_at"].index is True
