"""Unit tests for conversion configuration loading."""

import ast
import datetime as dt
import importlib
import importlib.util
from pathlib import Path

from pydantic import ValidationError
import pytest

from aizk.conversion.core.errors import ConfigurationError
from aizk.conversion.settings import (
    ConversionConfig,
    DoclingConverterConfig,
    KarakeepFetcherConfig,
)
from aizk.conversion.wiring.ingress_policy import IngressPolicy
from aizk.pipeline.stage import require_stale_threshold
from aizk.service.auth import AuthSettings
from aizk.service.settings import ServiceSettings
from aizk.storage.settings import ObjectStorageSettings


def test_config_reads_env_vars(monkeypatch):
    worker_concurrency = "8"

    monkeypatch.setenv("AIZK_WORKER_CONCURRENCY", str(worker_concurrency))
    config = ConversionConfig(_env_file=None)
    assert config.worker_concurrency == int(worker_concurrency)


def test_split_settings_classes_each_read_their_own_names(monkeypatch):
    """One environment snapshot lands its values on the class that now declares each field."""
    monkeypatch.setenv("AIZK_S3_BUCKET_NAME", "aizk-test")
    monkeypatch.setenv("AIZK_WORKER_CONCURRENCY", "8")
    monkeypatch.setenv("AIZK_LOG_LEVEL", "DEBUG")

    conversion = ConversionConfig(_env_file=None)
    object_storage = ObjectStorageSettings(_env_file=None)
    service = ServiceSettings(_env_file=None)

    assert conversion.worker_concurrency == 8
    assert object_storage.s3_bucket_name == "aizk-test"
    assert service.log_level == "DEBUG"


def test_fetch_max_response_bytes_reads_env_var(monkeypatch):
    monkeypatch.setenv("AIZK_FETCH_MAX_RESPONSE_BYTES", "12345")

    config = ConversionConfig(_env_file=None)

    assert config.fetch_max_response_bytes == 12345


def test_conversion_config_accepts_explicit_field_name_overrides():
    config = ConversionConfig(_env_file=None, fetch_max_response_bytes=12345, fetch_timeout_seconds=7)

    assert config.fetch_max_response_bytes == 12345
    assert config.fetch_timeout_seconds == 7


def test_worker_max_attempts_reads_its_env_var_apart_from_the_upload_retries(monkeypatch):
    """The attempt cap has its own variable, defaulting to 3; ``retry_max_attempts`` stays the PUT-loop knob."""
    assert ConversionConfig(_env_file=None).worker_max_attempts == 3

    monkeypatch.setenv("AIZK_WORKER_MAX_ATTEMPTS", "5")
    config = ConversionConfig(_env_file=None)

    assert (config.worker_max_attempts, config.retry_max_attempts) == (5, 3)


def test_stale_threshold_defaults_past_the_job_and_drain_timeouts():
    """Unset, the stale threshold exceeds the job timeout plus the drain timeout and the termination allowance."""
    config = ConversionConfig(_env_file=None, worker_job_timeout_seconds=600, worker_drain_timeout_seconds=30)

    require_stale_threshold(
        stale_after=dt.timedelta(minutes=config.worker_stale_job_minutes),
        timeout=dt.timedelta(seconds=config.worker_job_timeout_seconds),
        drain_timeout=config.worker_drain_timeout_seconds,
        cancel_grace=10.0,
    )
    assert ConversionConfig(_env_file=None, worker_stale_job_minutes=45).worker_stale_job_minutes == 45


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


# --- AuthSettings ------------------------------------------------------------


def test_auth_settings_default_is_trust_network(monkeypatch):
    """No env vars set → defaults to trust_network / self."""
    monkeypatch.delenv("AIZK_AUTH_MODE", raising=False)
    monkeypatch.delenv("AIZK_DEFAULT_PRINCIPAL", raising=False)

    settings = AuthSettings(_env_file=None)

    assert settings.auth_mode == "trust_network"
    assert settings.default_principal == "self"


def test_auth_settings_explicit_trust_network_constructs(monkeypatch):
    monkeypatch.setenv("AIZK_AUTH_MODE", "trust_network")
    monkeypatch.setenv("AIZK_DEFAULT_PRINCIPAL", "alice")

    settings = AuthSettings(_env_file=None)

    assert settings.auth_mode == "trust_network"
    assert settings.default_principal == "alice"


@pytest.mark.parametrize("reserved_mode", ["token", "proxy_headers", "oidc"])
def test_auth_settings_rejects_unimplemented_modes(monkeypatch, reserved_mode):
    """Reserved-but-unimplemented modes are rejected with the not-implemented message.

    `ConfigurationError` is a `RuntimeError` subclass, so pydantic propagates it
    rather than wrapping it in `ValidationError` (which only wraps `ValueError`
    and `AssertionError`). Asserting on the typed error here matches the
    project's startup-time `ConfigurationError` precedent.
    """
    monkeypatch.setenv("AIZK_AUTH_MODE", reserved_mode)

    with pytest.raises(ConfigurationError, match="reserved for a future build but not implemented at this cutover"):
        AuthSettings(_env_file=None)


def test_auth_settings_rejects_unknown_mode(monkeypatch):
    """A value outside the literal type is rejected by pydantic before the validator runs."""
    monkeypatch.setenv("AIZK_AUTH_MODE", "lol_anyone_can_in")

    with pytest.raises(ValidationError):
        AuthSettings(_env_file=None)


@pytest.mark.parametrize("blank_value", ["", " ", "\t", "\n", "   \t  "])
def test_auth_settings_rejects_blank_default_principal(monkeypatch, blank_value):
    """Empty/whitespace `AIZK_DEFAULT_PRINCIPAL` is rejected at construction time.

    A blank value would otherwise land in `owner_id` for every backfilled and
    newly-minted row and silently orphan rows from any non-blank principal.
    """
    monkeypatch.setenv("AIZK_DEFAULT_PRINCIPAL", blank_value)

    with pytest.raises(ConfigurationError, match="non-empty, non-whitespace"):
        AuthSettings(_env_file=None)


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
    assert AuthSettings.model_config.get("env_file") is None


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
        "aizk.conversion.wiring.subprocess",
    ],
)
def test_composition_roots_call_guarded_dotenv_loader(module):
    """True process entrypoints must use the shared guarded dotenv loader.

    A CLI reaches the loader through the shared process preamble; the API
    lifespan and the spawned conversion child call it directly. Either spelling
    satisfies the rule. If this test fails after removing or moving a call,
    update the composition root accordingly rather than deleting the test.
    """
    source = _module_path(module)
    assert _calls_named_function(source, "load_process_dotenv_once") or _calls_named_function(
        source, "prepare_process"
    ), f"{module} must load dotenv — it is a process composition root."


@pytest.mark.isolate
def test_docling_converter_config_ignores_env_file_without_session_fixtures(tmp_path):
    """In a fresh process (no session fixtures), DoclingConverterConfig() must ignore .env.

    This is the authoritative check: it runs without the session-level fixture that
    patches model_config["env_file"] = None, so it validates the class default directly.
    """
    import os

    from aizk.conversion.settings import DoclingConverterConfig

    env_file = tmp_path / ".env"
    env_file.write_text("AIZK_CONVERTER__DOCLING__OCR_ENABLED=false\n")
    os.chdir(tmp_path)
    config = DoclingConverterConfig()
    assert config.ocr_enabled is True  # .env must be ignored when env_file=None
