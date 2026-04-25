"""Unit tests for conversion configuration loading."""

import ast
import importlib
import importlib.util
from pathlib import Path

from pydantic import ValidationError
import pytest

from aizk.conversion.utilities.config import ConversionConfig, DoclingConverterConfig, KarakeepFetcherConfig
from aizk.conversion.wiring.ingress_policy import IngressPolicy


def test_config_reads_env_vars(monkeypatch):
    database_url = "sqlite:///./test.db"
    s3_bucket_name = "aizk-test"
    worker_concurrency = "8"
    mlflow_tracing_enabled = "true"
    mlflow_tracking_uri = "http://mlflow:5000"
    mlflow_experiment_name = "aizk-conversion"

    monkeypatch.setenv("DATABASE_URL", database_url)
    monkeypatch.setenv("S3_BUCKET_NAME", s3_bucket_name)
    monkeypatch.setenv("WORKER_CONCURRENCY", str(worker_concurrency))
    monkeypatch.setenv("MLFLOW_TRACING_ENABLED", mlflow_tracing_enabled)
    monkeypatch.setenv("MLFLOW_TRACKING_URI", mlflow_tracking_uri)
    monkeypatch.setenv("MLFLOW_EXPERIMENT_NAME", mlflow_experiment_name)
    config = ConversionConfig(_env_file=None)
    assert config.database_url == database_url
    assert config.s3_bucket_name == s3_bucket_name
    assert config.worker_concurrency == int(worker_concurrency)
    assert config.mlflow_tracing_enabled is True
    assert config.mlflow_tracking_uri == mlflow_tracking_uri
    assert config.mlflow_experiment_name == mlflow_experiment_name


def test_api_reload_defaults_to_false(monkeypatch):
    monkeypatch.delenv("API_RELOAD", raising=False)
    config = ConversionConfig(_env_file=None)
    assert config.api_reload is False


def test_fetch_max_response_bytes_reads_env_var(monkeypatch):
    monkeypatch.setenv("FETCH_MAX_RESPONSE_BYTES", "12345")

    config = ConversionConfig(_env_file=None)

    assert config.fetch_max_response_bytes == 12345


def test_conversion_config_accepts_explicit_field_name_overrides():
    config = ConversionConfig(_env_file=None, fetch_max_response_bytes=12345, fetch_timeout_seconds=7)

    assert config.fetch_max_response_bytes == 12345
    assert config.fetch_timeout_seconds == 7


def test_docling_config_reads_new_env_vars(monkeypatch):
    monkeypatch.setenv("AIZK_CONVERTER__DOCLING__OCR_ENABLED", "false")
    monkeypatch.setenv("AIZK_CONVERTER__DOCLING__PDF_MAX_PAGES", "100")
    monkeypatch.setenv("AIZK_CONVERTER__DOCLING__PICTURE_DESCRIPTION_BASE_URL", "")
    monkeypatch.setenv("AIZK_CONVERTER__DOCLING__PICTURE_DESCRIPTION_API_KEY", "")

    config = DoclingConverterConfig(_env_file=None)

    assert config.ocr_enabled is False
    assert config.pdf_max_pages == 100


def test_docling_old_env_var_has_no_effect(monkeypatch):
    """Old DOCLING_* env vars must have no effect — no compatibility shim."""
    monkeypatch.setenv("DOCLING_ENABLE_OCR", "false")  # opposite of default True
    monkeypatch.delenv("AIZK_CONVERTER__DOCLING__OCR_ENABLED", raising=False)
    monkeypatch.setenv("AIZK_CONVERTER__DOCLING__PICTURE_DESCRIPTION_BASE_URL", "")
    monkeypatch.setenv("AIZK_CONVERTER__DOCLING__PICTURE_DESCRIPTION_API_KEY", "")

    config = DoclingConverterConfig(_env_file=None)

    assert config.ocr_enabled is True  # default, old var ignored


def test_docling_config_placeholder_expansion(monkeypatch):
    monkeypatch.setenv("_OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.setenv("AIZK_CONVERTER__DOCLING__PICTURE_DESCRIPTION_BASE_URL", "${_OPENROUTER_BASE_URL}")
    monkeypatch.setenv("AIZK_CONVERTER__DOCLING__PICTURE_DESCRIPTION_API_KEY", "$OPENROUTER_API_KEY")

    config = DoclingConverterConfig(_env_file=None)

    assert config.picture_description_base_url == "https://openrouter.ai/api/v1"
    assert config.picture_description_api_key == "test-key"
    assert config.is_picture_description_enabled() is True


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("AIZK_CONVERTER__DOCLING__PICTURE_DESCRIPTION_BASE_URL", "${AIZK_TEST_MISSING_BASE_URL}"),
        ("AIZK_CONVERTER__DOCLING__PICTURE_DESCRIPTION_API_KEY", "$AIZK_TEST_MISSING_API_KEY"),
    ],
)
def test_docling_config_rejects_unresolved_placeholders(monkeypatch, field, value):
    monkeypatch.setenv("AIZK_CONVERTER__DOCLING__PICTURE_DESCRIPTION_BASE_URL", "")
    monkeypatch.setenv("AIZK_CONVERTER__DOCLING__PICTURE_DESCRIPTION_API_KEY", "")
    monkeypatch.delenv("AIZK_TEST_MISSING_BASE_URL", raising=False)
    monkeypatch.delenv("AIZK_TEST_MISSING_API_KEY", raising=False)
    monkeypatch.setenv(field, value)
    with pytest.raises(ValidationError, match="contains unresolved env placeholder syntax"):
        DoclingConverterConfig(_env_file=None)


def test_karakeep_config_reads_new_env_vars(monkeypatch):
    monkeypatch.setenv("AIZK_FETCHER__KARAKEEP__BASE_URL", "http://kk:3000")
    monkeypatch.setenv("AIZK_FETCHER__KARAKEEP__API_KEY", "mytoken")

    config = KarakeepFetcherConfig(_env_file=None)

    assert config.base_url == "http://kk:3000"
    assert config.api_key == "mytoken"


# --- Settings hermeticity (H7) -----------------------------------------------


def test_all_settings_classes_declare_env_file_none():
    """Every BaseSettings subclass in the conversion package must declare env_file=None.

    The composition root (wiring builders and CLI commands) is the only permitted
    site that loads .env via python-dotenv before constructing settings.
    """
    assert ConversionConfig.model_config.get("env_file") is None
    assert DoclingConverterConfig.model_config.get("env_file") is None
    assert KarakeepFetcherConfig.model_config.get("env_file") is None
    assert IngressPolicy.model_config.get("env_file") is None


def _calls_named_function(source: Path, function_name: str) -> bool:
    """Return True if the module source contains any call to the named function."""
    tree = ast.parse(source.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name) and func.id == function_name:
                return True
            if isinstance(func, ast.Attribute) and func.attr == function_name:
                return True
    return False


def _module_path(dotted: str) -> Path:
    spec = importlib.util.find_spec(dotted)
    assert spec is not None and spec.origin is not None, f"Cannot locate module {dotted!r}"
    return Path(spec.origin)


@pytest.mark.parametrize(
    "module",
    [
        "aizk.conversion.wiring.api",
        "aizk.conversion.wiring.worker",
    ],
)
def test_builders_do_not_call_load_dotenv(module):
    """Wiring builders and the FastAPI app setup must NOT call load_dotenv().

    The composition root is the only permitted call site.  If a builder calls
    load_dotenv() it will re-inject dotenv vars after monkeypatch.delenv() and
    break test isolation.
    """
    assert not _calls_named_function(_module_path(module), "load_dotenv"), (
        f"{module} must not call load_dotenv() — dotenv loading belongs only at "
        "process composition roots (cli.py:main, _do_convert)."
    )
    assert not _calls_named_function(_module_path(module), "load_process_dotenv_once"), (
        f"{module} must not call load_process_dotenv_once() — builders must stay pure."
    )


@pytest.mark.parametrize(
    "module",
    [
        "aizk.conversion.cli",
        "aizk.conversion.api.main",
        "aizk.conversion.workers.orchestrator",
    ],
)
def test_composition_roots_call_guarded_dotenv_loader(module):
    """True process entrypoints must use the shared guarded dotenv loader.

    If this test fails after removing or moving a call, update the composition
    root accordingly rather than deleting the test.
    """
    assert _calls_named_function(_module_path(module), "load_process_dotenv_once"), (
        f"{module} must call load_process_dotenv_once() — it is a process composition root."
    )


def test_guarded_dotenv_loader_calls_python_dotenv_once(monkeypatch):
    import aizk.conversion.utilities.dotenv as dotenv_utils

    calls: list[bool] = []
    monkeypatch.setattr(dotenv_utils, "_DOTENV_LOADED", False)
    monkeypatch.setattr(dotenv_utils, "load_dotenv", lambda: calls.append(True))

    dotenv_utils.load_process_dotenv_once()
    dotenv_utils.load_process_dotenv_once()

    assert calls == [True]


@pytest.mark.isolate
def test_docling_converter_config_ignores_env_file_without_session_fixtures(tmp_path):
    """In a fresh process (no session fixtures), DoclingConverterConfig() must ignore .env.

    This is the authoritative check: it runs without the session-level fixture that
    patches model_config["env_file"] = None, so it validates the class default directly.
    """
    import os

    from aizk.conversion.utilities.config import DoclingConverterConfig

    env_file = tmp_path / ".env"
    env_file.write_text("AIZK_CONVERTER__DOCLING__OCR_ENABLED=false\n")
    os.chdir(tmp_path)
    config = DoclingConverterConfig()
    assert config.ocr_enabled is True  # .env must be ignored when env_file=None
