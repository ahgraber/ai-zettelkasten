"""Unit tests for the SQLite backend's Litestream durability.

Covers the `sqlite-replication` spec contracts: config-file emission and the
Litestream manager's eligibility and lifecycle, scoped to the SQLite backend.
"""

from pathlib import Path
from unittest.mock import MagicMock, patch
import yaml

from pyleak import no_thread_leaks
import pytest

from aizk.db.backends.sqlite import LitestreamManager, SqliteDurabilityConfig, _write_config_file


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
# LitestreamManager eligibility (sqlite-replication MODIFIED requirement)
# ---------------------------------------------------------------------------


def _durability(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **overrides: str) -> SqliteDurabilityConfig:
    """Build a durability config with a file-based SQLite backend and sane defaults.

    Points ``AIZK_DATABASE_URL`` at a file-based SQLite database (so the backend
    resolves to SQLite and the path is file-based) unless an override changes it,
    then sets the litestream/S3 env the manager reads. Individual eligibility
    tests override single keys to exercise one disqualifying condition at a time.
    """
    db_url = overrides.pop("database_url", f"sqlite:///{tmp_path / 'conversion.db'}")
    monkeypatch.setenv("AIZK_DATABASE_URL", db_url)
    env = {
        "AIZK_LITESTREAM_ENABLED": "true",
        "AIZK_LITESTREAM_START_ROLE": "both",
        "AIZK_LITESTREAM_S3_BUCKET_NAME": "test-bucket",
        "AIZK_LITESTREAM_CONFIG_PATH": str(tmp_path / "litestream.yaml"),
        "AIZK_LITESTREAM_RESTORE_ON_STARTUP": "false",
        "AIZK_S3_REGION": "us-east-1",
        "AIZK_S3_ACCESS_KEY_ID": "test",
        "AIZK_S3_SECRET_ACCESS_KEY": "test",
        "AIZK_S3_BUCKET_NAME": "test-bucket",
        "AIZK_S3_ENDPOINT_URL": "http://localhost:9000",
        **overrides,
    }
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return SqliteDurabilityConfig(_env_file=None)


def test_replication_disabled_by_config_is_inert(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """SQLite backend active, replication disabled → manager inert, no subprocess, no raise."""
    config = _durability(tmp_path, monkeypatch, AIZK_LITESTREAM_ENABLED="false")
    manager = LitestreamManager(config, role="worker")

    with patch("aizk.db.backends.sqlite.subprocess.Popen") as mock_popen:
        manager.start()

    mock_popen.assert_not_called()
    assert manager._process is None


def test_replication_role_not_included_is_inert(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """SQLite backend active, enabled, but role excluded (and not ``both``) → manager inert."""
    config = _durability(tmp_path, monkeypatch, AIZK_LITESTREAM_START_ROLE="api")
    manager = LitestreamManager(config, role="worker")

    with patch("aizk.db.backends.sqlite.subprocess.Popen") as mock_popen:
        manager.start()

    mock_popen.assert_not_called()
    assert manager._process is None


def test_replication_file_less_sqlite_is_inert(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """SQLite backend active but in-memory (no file path) → manager inert."""
    config = _durability(tmp_path, monkeypatch, database_url="sqlite://")
    manager = LitestreamManager(config, role="worker")

    with patch("aizk.db.backends.sqlite.subprocess.Popen") as mock_popen:
        manager.start()

    mock_popen.assert_not_called()
    assert manager._process is None


# ---------------------------------------------------------------------------
# LitestreamManager lifecycle
# ---------------------------------------------------------------------------


@pytest.fixture()
def _litestream_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SqliteDurabilityConfig:
    """Provide a durability config wired for Litestream against a file-based SQLite DB."""
    return _durability(tmp_path, monkeypatch)


def test_start_stop_lifecycle_no_thread_leaks(_litestream_config, monkeypatch) -> None:
    """start() followed by stop() leaves no leaked threads or zombie process handles."""
    mock_process = MagicMock()
    mock_process.poll.return_value = None  # Process is "running"
    mock_process.pid = 12345
    mock_process.wait.return_value = 0

    with (
        patch(
            "aizk.db.backends.sqlite._resolve_litestream_binary",
            return_value="/usr/bin/litestream",
        ),
        patch(
            "aizk.db.backends.sqlite.subprocess.Popen",
            return_value=mock_process,
        ),
        patch("aizk.db.backends.sqlite.os.killpg") as mock_killpg,
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
