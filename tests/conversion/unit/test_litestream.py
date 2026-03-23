"""Unit tests for Litestream configuration generation and lifecycle."""

from pathlib import Path
from unittest.mock import MagicMock, patch
import yaml

from pyleak import no_thread_leaks
import pytest

from aizk.conversion.utilities.litestream import LitestreamManager, _write_config_file


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


# ---------------------------------------------------------------------------
# LitestreamManager lifecycle
# ---------------------------------------------------------------------------


@pytest.fixture()
def _litestream_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Provide a ConversionConfig wired for Litestream with a fake binary."""
    from aizk.conversion.utilities.config import ConversionConfig

    db_path = tmp_path / "conversion.db"
    config_path = tmp_path / "litestream.yaml"

    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")
    monkeypatch.setenv("LITESTREAM_ENABLED", "true")
    monkeypatch.setenv("LITESTREAM_S3_BUCKET_NAME", "test-bucket")
    monkeypatch.setenv("LITESTREAM_CONFIG_PATH", str(config_path))
    monkeypatch.setenv("LITESTREAM_RESTORE_ON_STARTUP", "false")
    monkeypatch.setenv("LITESTREAM_START_ROLE", "both")
    monkeypatch.setenv("S3_REGION", "us-east-1")
    monkeypatch.setenv("S3_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("S3_SECRET_ACCESS_KEY", "test")
    monkeypatch.setenv("S3_BUCKET_NAME", "test-bucket")
    monkeypatch.setenv("S3_ENDPOINT_URL", "http://localhost:9000")

    return ConversionConfig()


def test_start_stop_lifecycle_no_thread_leaks(_litestream_config, monkeypatch) -> None:
    """start() followed by stop() leaves no leaked threads or zombie process handles."""
    mock_process = MagicMock()
    mock_process.poll.return_value = None  # Process is "running"
    mock_process.pid = 12345
    mock_process.wait.return_value = 0

    with (
        patch(
            "aizk.conversion.utilities.litestream._resolve_litestream_binary",
            return_value="/usr/bin/litestream",
        ),
        patch(
            "aizk.conversion.utilities.litestream.subprocess.Popen",
            return_value=mock_process,
        ),
        patch("aizk.conversion.utilities.litestream.os.killpg") as mock_killpg,
        patch("atexit.register"),
    ):
        manager = LitestreamManager(_litestream_config, role="worker")

        with no_thread_leaks(action="raise"):
            manager.start()
            assert manager._process is mock_process

            manager.stop()

        mock_killpg.assert_called_once()
        mock_process.wait.assert_called_once()


def test_stop_without_start_is_noop(_litestream_config) -> None:
    """Calling stop() before start() does nothing."""
    manager = LitestreamManager(_litestream_config, role="worker")

    with no_thread_leaks(action="raise"):
        manager.stop()  # Should not raise
