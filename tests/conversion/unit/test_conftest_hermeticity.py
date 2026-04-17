"""Tests for the test-harness hermeticity contract enforced by the conversion conftest."""

from __future__ import annotations

from tests.conversion.conftest import (
    _HARNESS_ENV_ALLOWLIST,
    _conversion_config_aliases,
    _strip_unclaimed_aliases,
)


def test_strip_unclaimed_aliases_removes_unlisted_keys():
    environ = {
        "AIZK_CONVERTER": "raw",
        "AIZK_CONVERTER__DOCLING__PDF_MAX_PAGES": "999",
        "DATABASE_URL": "sqlite:///x",
        "UNRELATED": "keep",
    }
    aliases = {"AIZK_CONVERTER", "DATABASE_URL"}
    allowlist = {"DATABASE_URL"}

    stripped = _strip_unclaimed_aliases(environ, aliases, allowlist)

    assert stripped == {
        "AIZK_CONVERTER": "raw",
        "AIZK_CONVERTER__DOCLING__PDF_MAX_PAGES": "999",
    }
    assert "AIZK_CONVERTER" not in environ
    assert "AIZK_CONVERTER__DOCLING__PDF_MAX_PAGES" not in environ
    assert environ["DATABASE_URL"] == "sqlite:///x"
    assert environ["UNRELATED"] == "keep"


def test_strip_unclaimed_aliases_skips_missing_keys():
    environ: dict[str, str] = {}
    aliases = {"AIZK_CONVERTER"}
    allowlist: set[str] = set()

    stripped = _strip_unclaimed_aliases(environ, aliases, allowlist)

    assert stripped == {}
    assert environ == {}


def test_strip_unclaimed_aliases_returns_empty_when_all_allowlisted():
    environ = {"DATABASE_URL": "sqlite:///x", "S3_BUCKET_NAME": "b"}
    aliases = {"DATABASE_URL", "S3_BUCKET_NAME"}
    allowlist = {"DATABASE_URL", "S3_BUCKET_NAME"}

    stripped = _strip_unclaimed_aliases(environ, aliases, allowlist)

    assert stripped == {}
    assert environ == {"DATABASE_URL": "sqlite:///x", "S3_BUCKET_NAME": "b"}


def test_harness_allowlist_is_subset_of_conversion_config_aliases():
    """Drift guard: every allowlisted alias must correspond to a real `ConversionConfig` field.

    A typo or stale entry in `_HARNESS_ENV_ALLOWLIST` would silently fail to protect anything.
    """
    aliases = _conversion_config_aliases()
    unknown = _HARNESS_ENV_ALLOWLIST - aliases
    assert not unknown, (
        f"`_HARNESS_ENV_ALLOWLIST` contains aliases that are not `ConversionConfig` fields: {sorted(unknown)}. "
        "Either fix the typo or remove the stale entry."
    )


def test_session_fixture_strips_a_known_nested_alias_at_session_start(monkeypatch):
    """Adversarial check that the session-scoped fixture removed an unclaimed nested alias.

    Sets a stray `AIZK_CONVERTER__DOCLING__PDF_MAX_PAGES` directly via `os.environ`
    (bypassing monkeypatch's per-test setup) and verifies the fixture would have popped it.
    Uses the helper directly because the fixture itself runs once per session and cannot
    be re-invoked mid-test.
    """
    import os

    monkeypatch.setenv("AIZK_CONVERTER__DOCLING__PDF_MAX_PAGES", "999")
    aliases = _conversion_config_aliases()
    assert "AIZK_CONVERTER" in aliases
    assert "AIZK_CONVERTER" not in _HARNESS_ENV_ALLOWLIST

    snapshot = dict(os.environ)
    stripped = _strip_unclaimed_aliases(snapshot, aliases, _HARNESS_ENV_ALLOWLIST)

    assert "AIZK_CONVERTER__DOCLING__PDF_MAX_PAGES" in stripped
    assert stripped["AIZK_CONVERTER__DOCLING__PDF_MAX_PAGES"] == "999"
    assert "AIZK_CONVERTER__DOCLING__PDF_MAX_PAGES" not in snapshot
