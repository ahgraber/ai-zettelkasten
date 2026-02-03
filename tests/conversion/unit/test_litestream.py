"""Unit tests for Litestream configuration generation."""

from pathlib import Path
import yaml

import pytest

from aizk.conversion.utilities.litestream import _write_config_file


def test_write_config_file_emits_expected_yaml(tmp_path: Path) -> None:
    db_path = tmp_path / "conversion.db"
    config_path = tmp_path / "litestream.yaml"

    _write_config_file(
        db_path=db_path,
        bucket="aizk",
        config_path=config_path,
        s3_prefix="db",
        s3_region="us-east-1",
        s3_endpoint_url="https://s3.example.com",
        s3_force_path_style=True,
        s3_sign_payload=True,
    )

    payload = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert payload["dbs"][0]["path"] == str(db_path)
    replica = payload["dbs"][0]["replicas"][0]
    assert replica["type"] == "s3"
    assert replica["bucket"] == "aizk"
    assert replica["path"] == "db/conversion.db"
    assert replica["region"] == "us-east-1"
    assert replica["endpoint"] == "https://s3.example.com"
    assert replica["force-path-style"] is True
    assert replica["sign-payload"] is True


def test_write_config_file_omits_optional_fields(tmp_path: Path) -> None:
    db_path = tmp_path / "conversion.db"
    config_path = tmp_path / "litestream.yaml"

    _write_config_file(
        db_path=db_path,
        bucket="aizk",
        config_path=config_path,
        s3_prefix="db",
        s3_region="us-east-1",
        s3_endpoint_url="",
        s3_force_path_style=False,
        s3_sign_payload=False,
    )

    payload = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    replica = payload["dbs"][0]["replicas"][0]
    assert "endpoint" not in replica
    assert "force-path-style" not in replica
    assert "sign-payload" not in replica


def test_write_config_file_requires_absolute_db_path(tmp_path: Path) -> None:
    config_path = tmp_path / "litestream.yaml"

    with pytest.raises(ValueError, match="database path must be absolute"):
        _write_config_file(
            db_path=Path("relative.db"),
            bucket="aizk",
            config_path=config_path,
            s3_prefix="db",
            s3_region="us-east-1",
            s3_endpoint_url="",
            s3_force_path_style=False,
            s3_sign_payload=False,
        )
