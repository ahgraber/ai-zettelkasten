"""Tests for the wiring package: builders, registration, chain-closure validation."""

from __future__ import annotations

import sys
from typing import ClassVar
from unittest.mock import MagicMock

import pytest

from aizk.conversion.core.errors import ChainNotTerminated
from aizk.conversion.core.registry import ConverterRegistry, FetcherRegistry
from aizk.conversion.core.types import ContentType
from aizk.conversion.wiring.registrations import validate_chain_closure


# ---------------------------------------------------------------------------
# Fake adapter helpers for registry-level tests (no heavy deps)
# ---------------------------------------------------------------------------


class _FakeContentFetcher:
    def fetch(self, ref):
        raise NotImplementedError


def _fake_resolver(resolves_to_set: frozenset[str]) -> object:
    """Return a resolver instance whose class-level resolves_to equals resolves_to_set."""

    class _R:
        resolves_to: ClassVar[frozenset[str]] = resolves_to_set

        def resolve(self, ref):
            raise NotImplementedError

    return _R()


# ---------------------------------------------------------------------------
# Fixture: mock DoclingConverter to avoid xxhash / docling at import time
# ---------------------------------------------------------------------------


@pytest.fixture()
def mock_docling(monkeypatch):
    """Inject a lightweight fake DoclingConverter into sys.modules."""

    class _FakeDoclingConverter:
        supported_formats: ClassVar[frozenset[ContentType]] = frozenset(
            {ContentType.PDF, ContentType.HTML}
        )
        requires_gpu: ClassVar[bool] = True

        def __init__(self, cfg=None):
            self._cfg = cfg

        def convert(self, inp):
            raise NotImplementedError

        def config_snapshot(self):
            return {}

    fake_mod = MagicMock(name="aizk.conversion.adapters.converters.docling")
    fake_mod.DoclingConverter = _FakeDoclingConverter

    monkeypatch.setitem(sys.modules, "aizk.conversion.adapters.converters.docling", fake_mod)
    return _FakeDoclingConverter


# ---------------------------------------------------------------------------
# validate_chain_closure — happy path
# ---------------------------------------------------------------------------


def test_chain_closure_passes_for_valid_chain():
    """One resolver (karakeep_bookmark) terminating in four content fetchers."""
    fr = FetcherRegistry()
    fr.register_resolver(
        "karakeep_bookmark",
        _fake_resolver(frozenset({"arxiv", "github_readme", "url", "inline_html"})),
    )
    fr.register_content_fetcher("arxiv", _FakeContentFetcher())
    fr.register_content_fetcher("github_readme", _FakeContentFetcher())
    fr.register_content_fetcher("url", _FakeContentFetcher())
    fr.register_content_fetcher("inline_html", _FakeContentFetcher())

    # Should not raise.
    validate_chain_closure(fr)


# ---------------------------------------------------------------------------
# validate_chain_closure — missing kind
# ---------------------------------------------------------------------------


def test_chain_closure_raises_for_missing_kind():
    """Resolver declares a kind that is not registered → ChainNotTerminated."""
    fr = FetcherRegistry()
    fr.register_resolver("resolver_a", _fake_resolver(frozenset({"unregistered_kind"})))

    with pytest.raises(ChainNotTerminated) as exc_info:
        validate_chain_closure(fr)

    err = exc_info.value
    assert err.missing_kind == "unregistered_kind"
    assert err.resolver_name == "resolver_a"


# ---------------------------------------------------------------------------
# validate_chain_closure — cycle
# ---------------------------------------------------------------------------


def test_chain_closure_raises_for_cycle():
    """Two resolvers forming a cycle → ChainNotTerminated with cycle_path."""
    fr = FetcherRegistry()
    fr.register_resolver("resolver_a", _fake_resolver(frozenset({"resolver_b"})))
    fr.register_resolver("resolver_b", _fake_resolver(frozenset({"resolver_a"})))

    with pytest.raises(ChainNotTerminated) as exc_info:
        validate_chain_closure(fr)

    err = exc_info.value
    assert err.cycle_path is not None
    # cycle_path should contain both resolver names
    assert "resolver_a" in err.cycle_path
    assert "resolver_b" in err.cycle_path


# ---------------------------------------------------------------------------
# validate_chain_closure — depth exceeded
# ---------------------------------------------------------------------------


def test_chain_closure_raises_when_depth_cap_exceeded():
    """Chain longer than depth_cap → ChainNotTerminated."""
    # resolver_a → resolver_b → content_fetcher_c with depth_cap=1
    # resolver_a at depth=0, resolver_b at depth=1 → depth=1 >= depth_cap=1 → fail
    fr = FetcherRegistry()
    fr.register_resolver("resolver_a", _fake_resolver(frozenset({"resolver_b"})))
    fr.register_resolver("resolver_b", _fake_resolver(frozenset({"content_c"})))
    fr.register_content_fetcher("content_c", _FakeContentFetcher())

    with pytest.raises(ChainNotTerminated) as exc_info:
        validate_chain_closure(fr, depth_cap=1)

    # cycle_path (used for depth exceeded) should reference the long path
    err = exc_info.value
    assert err.cycle_path is not None


# ---------------------------------------------------------------------------
# register_ready_adapters / build_worker_runtime / build_api_runtime
# ---------------------------------------------------------------------------


def test_build_worker_runtime_registers_expected_kinds(mock_docling, monkeypatch):
    """build_worker_runtime returns capabilities with the expected accepted_kinds."""
    from aizk.conversion.wiring.worker import build_worker_runtime

    cfg = MagicMock()
    cfg.worker_gpu_concurrency = 1

    runtime = build_worker_runtime(cfg)
    caps = runtime.capabilities

    expected_kinds = frozenset({"karakeep_bookmark", "arxiv", "github_readme", "url", "inline_html"})
    assert caps.accepted_kinds == expected_kinds


def test_build_worker_runtime_registers_expected_converter_formats(mock_docling, monkeypatch):
    """DoclingConverter formats (PDF, HTML) are reflected in DeploymentCapabilities."""
    from aizk.conversion.wiring.worker import build_worker_runtime

    cfg = MagicMock()
    cfg.worker_gpu_concurrency = 1

    runtime = build_worker_runtime(cfg)
    caps = runtime.capabilities

    assert caps.converter_available(ContentType.PDF)
    assert caps.converter_available(ContentType.HTML)


def test_build_api_runtime_accepted_kinds_match_worker(mock_docling, monkeypatch):
    """API and worker runtimes produce identical accepted_kinds (shared helper)."""
    from aizk.conversion.wiring.api import build_api_runtime
    from aizk.conversion.wiring.worker import build_worker_runtime

    cfg = MagicMock()
    cfg.worker_gpu_concurrency = 1

    worker_runtime = build_worker_runtime(cfg)
    api_runtime = build_api_runtime(cfg)

    assert worker_runtime.capabilities.accepted_kinds == api_runtime.capabilities.accepted_kinds


def test_singlefile_not_in_accepted_kinds(mock_docling, monkeypatch):
    """'singlefile' is not registered by register_ready_adapters."""
    from aizk.conversion.wiring.worker import build_worker_runtime

    cfg = MagicMock()
    cfg.worker_gpu_concurrency = 1

    runtime = build_worker_runtime(cfg)
    assert "singlefile" not in runtime.capabilities.accepted_kinds


# ---------------------------------------------------------------------------
# validate_chain_closure — default wiring
# ---------------------------------------------------------------------------


def test_validate_chain_closure_passes_for_default_wiring(mock_docling, monkeypatch):
    """validate_chain_closure passes when all four resolver-emitted kinds are registered."""
    from aizk.conversion.wiring.registrations import register_ready_adapters

    fr = FetcherRegistry()
    cr = ConverterRegistry()
    cfg = MagicMock()

    # register_ready_adapters calls validate_chain_closure internally
    register_ready_adapters(fr, cr, cfg)


# ---------------------------------------------------------------------------
# Import graph lint
# ---------------------------------------------------------------------------


def test_no_adapter_cross_imports():
    """No adapter module imports from aizk.conversion.adapters (no cross-adapter imports)."""
    import ast
    import pathlib

    adapters_dir = pathlib.Path("src/aizk/conversion/adapters")
    violations = []

    for py_file in sorted(adapters_dir.rglob("*.py")):
        source = py_file.read_text()
        try:
            tree = ast.parse(source, filename=str(py_file))
        except SyntaxError:
            continue

        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                if "aizk.conversion.adapters" in node.module:
                    violations.append(f"{py_file}: imports {node.module!r}")

    assert not violations, (
        "Adapter modules must not import from aizk.conversion.adapters "
        "(cross-adapter imports violate the ports-and-adapters boundary):\n"
        + "\n".join(violations)
    )
