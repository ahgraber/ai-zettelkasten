"""Unit tests for build_worker_runtime, build_api_runtime, and build_test_runtime.

Covers:
- build_worker_runtime registers all expected fetcher kinds and converter formats.
- DeploymentCapabilities.registered_kinds contains every registered kind.
- build_api_runtime returns SubmissionCapabilities matching IngressPolicy.
- Worker and API capabilities intentionally diverge for default wiring.
- build_api_runtime raises ConfigurationError when IngressPolicy references an unregistered kind.
- build_test_runtime returns empty registries; test may register its own fakes.
- build_test_runtime respects an explicit IngressPolicy.
"""

from __future__ import annotations

from typing import Any, ClassVar

import pytest

from aizk.conversion.core.errors import ConfigurationError
from aizk.conversion.core.registry import ConverterRegistry, FetcherRegistry
from aizk.conversion.core.types import ContentType, ConversionArtifacts, ConversionInput
from aizk.conversion.utilities.config import ConversionConfig
from aizk.conversion.wiring.api import ApiRuntime, build_api_runtime
from aizk.conversion.wiring.capabilities import DeploymentCapabilities, SubmissionCapabilities
from aizk.conversion.wiring.ingress_policy import IngressPolicy
from aizk.conversion.wiring.testing import TestRuntime, build_test_runtime
from aizk.conversion.wiring.worker import WorkerRuntime, build_worker_runtime

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_CFG = ConversionConfig(_env_file=None)

# Expected kinds that register_ready_adapters wires (in Stage 5):
# 1 resolver:  karakeep_bookmark
# 4 fetchers:  arxiv, github_readme, url, inline_html
_EXPECTED_REGISTERED_KINDS = frozenset({"karakeep_bookmark", "arxiv", "github_readme", "url", "inline_html"})

# Docling handles these two content types:
_EXPECTED_CONVERTER_FORMATS = frozenset({ContentType.PDF, ContentType.HTML})


# ---------------------------------------------------------------------------
# build_worker_runtime
# ---------------------------------------------------------------------------


def test_build_worker_runtime_returns_worker_runtime_dataclass():
    rt = build_worker_runtime(_CFG)
    assert isinstance(rt, WorkerRuntime)


def test_build_worker_runtime_orchestrator_is_present():
    # Import after building so sys.modules[orchestrator] is the same object the
    # builder used.  The test_orchestrator import-graph test deletes orchestrator
    # from sys.modules, which would cause isinstance() to fail if we import the
    # class before calling build_worker_runtime.
    rt = build_worker_runtime(_CFG)
    assert type(rt.orchestrator).__name__ == "Orchestrator"


def test_build_worker_runtime_resource_guard_is_present():
    from aizk.conversion.core.protocols import ResourceGuard

    rt = build_worker_runtime(_CFG)
    assert isinstance(rt.resource_guard, ResourceGuard)


def test_build_worker_runtime_capabilities_registered_kinds_equals_expected():
    rt = build_worker_runtime(_CFG)
    assert rt.capabilities.registered_kinds == _EXPECTED_REGISTERED_KINDS


def test_build_worker_runtime_all_expected_kinds_in_registered_kinds():
    rt = build_worker_runtime(_CFG)
    for kind in _EXPECTED_REGISTERED_KINDS:
        assert kind in rt.capabilities.registered_kinds, f"Expected {kind!r} in registered_kinds"


def test_build_worker_runtime_singlefile_absent_from_registered_kinds():
    rt = build_worker_runtime(_CFG)
    assert "singlefile" not in rt.capabilities.registered_kinds


def test_build_worker_runtime_deployment_capabilities_converter_available_for_pdf():
    rt = build_worker_runtime(_CFG)
    assert rt.capabilities.converter_available(ContentType.PDF)


def test_build_worker_runtime_deployment_capabilities_converter_available_for_html():
    rt = build_worker_runtime(_CFG)
    assert rt.capabilities.converter_available(ContentType.HTML)


def test_build_worker_runtime_deployment_capabilities_no_converter_for_csv():
    rt = build_worker_runtime(_CFG)
    assert not rt.capabilities.converter_available(ContentType.CSV)


# ---------------------------------------------------------------------------
# build_api_runtime
# ---------------------------------------------------------------------------


def test_build_api_runtime_returns_api_runtime_dataclass():
    policy = IngressPolicy(_env_file=None, accepted_submission_kinds=frozenset({"karakeep_bookmark"}))
    rt = build_api_runtime(_CFG, ingress_policy=policy)
    assert isinstance(rt, ApiRuntime)


def test_build_api_runtime_capabilities_is_submission_capabilities():
    policy = IngressPolicy(_env_file=None, accepted_submission_kinds=frozenset({"karakeep_bookmark"}))
    rt = build_api_runtime(_CFG, ingress_policy=policy)
    assert isinstance(rt.capabilities, SubmissionCapabilities)


def test_build_api_runtime_accepted_submission_kinds_matches_ingress_policy():
    policy = IngressPolicy(_env_file=None, accepted_submission_kinds=frozenset({"karakeep_bookmark"}))
    rt = build_api_runtime(_CFG, ingress_policy=policy)
    assert rt.capabilities.accepted_submission_kinds == frozenset({"karakeep_bookmark"})


def test_build_api_runtime_default_ingress_policy_has_karakeep_bookmark():
    """build_api_runtime() with no explicit policy must default to karakeep_bookmark."""
    rt = build_api_runtime(_CFG, ingress_policy=IngressPolicy(_env_file=None))
    assert "karakeep_bookmark" in rt.capabilities.accepted_submission_kinds


def test_build_api_runtime_exposes_configured_converter_name():
    policy = IngressPolicy(_env_file=None, accepted_submission_kinds=frozenset({"karakeep_bookmark"}))
    rt = build_api_runtime(_CFG, ingress_policy=policy)

    assert rt.converter_name == _CFG.worker_converter_name


def test_build_api_runtime_exposes_submission_config_snapshot():
    policy = IngressPolicy(_env_file=None, accepted_submission_kinds=frozenset({"karakeep_bookmark"}))
    rt = build_api_runtime(_CFG, ingress_policy=policy)

    assert rt.converter_config_snapshot == rt.docling_config_snapshot


# ---------------------------------------------------------------------------
# Worker vs API capability divergence
# ---------------------------------------------------------------------------


def test_worker_and_api_capabilities_differ_for_default_wiring():
    """Worker registered_kinds is the full 5-kind set; API accepted is only karakeep_bookmark."""
    worker_rt = build_worker_runtime(_CFG)
    policy = IngressPolicy(_env_file=None, accepted_submission_kinds=frozenset({"karakeep_bookmark"}))
    api_rt = build_api_runtime(_CFG, ingress_policy=policy)

    worker_kinds = worker_rt.capabilities.registered_kinds
    api_kinds = api_rt.capabilities.accepted_submission_kinds

    assert worker_kinds == _EXPECTED_REGISTERED_KINDS
    assert api_kinds == frozenset({"karakeep_bookmark"})
    assert worker_kinds != api_kinds, "Worker registered_kinds and API accepted_kinds must differ"


def test_api_accepted_kinds_is_strict_subset_of_worker_registered_kinds():
    worker_rt = build_worker_runtime(_CFG)
    policy = IngressPolicy(_env_file=None, accepted_submission_kinds=frozenset({"karakeep_bookmark"}))
    api_rt = build_api_runtime(_CFG, ingress_policy=policy)

    assert api_rt.capabilities.accepted_submission_kinds < worker_rt.capabilities.registered_kinds


# ---------------------------------------------------------------------------
# accepted_submission_kinds ⊆ registered_kinds invariant
# ---------------------------------------------------------------------------


def test_build_api_runtime_raises_configuration_error_for_unregistered_kind():
    """IngressPolicy referencing a kind not wired by register_ready_adapters must raise."""
    bad_policy = IngressPolicy(_env_file=None, accepted_submission_kinds=frozenset({"singlefile"}))
    with pytest.raises(ConfigurationError, match="singlefile"):
        build_api_runtime(_CFG, ingress_policy=bad_policy)


def test_build_api_runtime_raises_configuration_error_for_invented_kind():
    """A completely unknown kind in IngressPolicy must also raise."""
    bad_policy = IngressPolicy(_env_file=None, accepted_submission_kinds=frozenset({"does_not_exist"}))
    with pytest.raises(ConfigurationError):
        build_api_runtime(_CFG, ingress_policy=bad_policy)


def test_build_api_runtime_raises_configuration_error_names_offending_kinds():
    """The ConfigurationError message must name the unregistered kind(s)."""
    bad_policy = IngressPolicy(_env_file=None, accepted_submission_kinds=frozenset({"singlefile", "made_up"}))
    with pytest.raises(ConfigurationError) as exc_info:
        build_api_runtime(_CFG, ingress_policy=bad_policy)
    msg = str(exc_info.value)
    # At least one of the offending kinds must appear in the message.
    assert "singlefile" in msg or "made_up" in msg


# ---------------------------------------------------------------------------
# build_test_runtime
# ---------------------------------------------------------------------------


def test_build_test_runtime_returns_test_runtime_dataclass():
    rt = build_test_runtime(_CFG)
    assert isinstance(rt, TestRuntime)


def test_build_test_runtime_registries_are_empty_by_default():
    rt = build_test_runtime(_CFG)
    assert rt.fetcher_registry.registered_kinds() == frozenset()


def test_build_test_runtime_capabilities_is_deployment_capabilities():
    rt = build_test_runtime(_CFG)
    assert isinstance(rt.capabilities, DeploymentCapabilities)


def test_build_test_runtime_default_ingress_policy_accepted_submission_kinds():
    """Default test runtime should have karakeep_bookmark as the accepted kind."""
    rt = build_test_runtime(_CFG)
    assert "karakeep_bookmark" in rt.ingress_policy.accepted_submission_kinds


def test_build_test_runtime_respects_explicit_ingress_policy():
    policy = IngressPolicy(_env_file=None, accepted_submission_kinds=frozenset({"arxiv", "url"}))
    rt = build_test_runtime(_CFG, ingress_policy=policy)
    assert rt.ingress_policy.accepted_submission_kinds == frozenset({"arxiv", "url"})


def test_build_test_runtime_allows_registering_fake_fetcher():
    """Tests can register fake fetchers on the returned registries."""

    class _FakePdfFetcher:
        produces: ClassVar[frozenset[ContentType]] = frozenset({ContentType.PDF})

        def fetch(self, ref: Any) -> ConversionInput:
            return ConversionInput(content=b"x", content_type=ContentType.PDF)

    rt = build_test_runtime(_CFG)
    rt.fetcher_registry.register_content_fetcher("url", _FakePdfFetcher())
    assert "url" in rt.fetcher_registry.registered_kinds()


def test_build_test_runtime_capabilities_reflect_registered_fakes():
    """DeploymentCapabilities.registered_kinds updates as fakes are registered."""

    class _FakePdfFetcher:
        produces: ClassVar[frozenset[ContentType]] = frozenset({ContentType.PDF})

        def fetch(self, ref: Any) -> ConversionInput:
            return ConversionInput(content=b"x", content_type=ContentType.PDF)

    rt = build_test_runtime(_CFG)
    assert "url" not in rt.capabilities.registered_kinds

    rt.fetcher_registry.register_content_fetcher("url", _FakePdfFetcher())
    # capabilities reads live from registry; the new kind must be reflected.
    assert "url" in rt.capabilities.registered_kinds
