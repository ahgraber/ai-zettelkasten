"""The single access point through which the graph stage reaches its model.

Contextualization treats the model as a substitutable dependency: both the
per-document summary pass and the per-chunk contextualization pass build a prompt
and call :meth:`LLMClient.generate`. There is exactly one method, so every model
invocation the stage makes passes through one seam — a deterministic test double
(:class:`StubLLMClient`) drops in for the production client
(:class:`PydanticAILLMClient`) without changing any stage logic.

The production client is backed by ``pydantic-ai`` (ADR-004). Output is
non-deterministic, so the stage's contracts (persistence, provenance, input
derivation keys, idempotency, supersession, mode-independence) are exercised against
the stub; the situating-context *quality* of live output is measured by offline
evaluation, not asserted here (see design.md § Verification Waivers).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable, Protocol, runtime_checkable

import xxhash

from aizk.pipeline.identity import fingerprint

if TYPE_CHECKING:
    from pydantic_ai import Agent

    from aizk.graph.config import ContextualizationConfig


@runtime_checkable
class LLMClient(Protocol):
    """Generates model output from a prompt."""

    def generate(self, prompt: str) -> str:
        """Return the model's text completion for ``prompt``."""
        ...


class PydanticAILLMClient:
    """Production :class:`LLMClient` backed by an injected ``pydantic-ai`` agent."""

    def __init__(self, agent: "Agent[None, str]") -> None:
        """Store the injected string-output agent."""
        self._agent = agent

    def generate(self, prompt: str) -> str:
        """Run ``prompt`` through the agent and return its text output."""
        return self._agent.run_sync(prompt).output


def _default_responder(prompt: str) -> str:
    """Return a deterministic placeholder completion derived from the prompt."""
    return f"stub-output:{xxhash.xxh64(prompt.encode('utf-8')).hexdigest()}"


class StubLLMClient:
    """Deterministic recording :class:`LLMClient` for tests.

    Stores prompts in :attr:`prompts` and returns a stable hash-based completion by
    default. ``responder`` supplies custom output.
    """

    def __init__(self, responder: Callable[[str], str] | None = None) -> None:
        """Initialize with an optional custom ``responder``; default is prompt-hash."""
        self.prompts: list[str] = []
        self._responder = responder or _default_responder

    def generate(self, prompt: str) -> str:
        """Record ``prompt`` and return the configured deterministic completion."""
        self.prompts.append(prompt)
        return self._responder(prompt)


@dataclass(frozen=True)
class ModelConfig:
    """Resolved model identity used in derivation fingerprints.

    ``provider`` is the base URL, ``model`` is the model name, and ``settings``
    contains sorted generation-setting pairs. Equivalent resolved identities keep
    the same fingerprint even when aliases differ.
    """

    provider: str
    model: str
    settings: tuple[tuple[str, str], ...] = ()

    @classmethod
    def from_contextualization_config(cls, config: "ContextualizationConfig") -> "ModelConfig":
        """Resolve a :class:`ModelConfig` from the contextualization worker's model endpoint fields."""
        return cls(provider=config.llm_base_url, model=config.llm_model)


def model_config_fingerprint(config: ModelConfig) -> str:
    """Return the fingerprint of a resolved model identity: provider, model, and settings."""
    return fingerprint({"provider": config.provider, "model": config.model, "settings": list(config.settings)})


#: A stub model identity for tests, where a derivation key needs a
#: :class:`ModelConfig` but no model endpoint is configured.
STUB_MODEL_CONFIG = ModelConfig(provider="stub", model="stub")
