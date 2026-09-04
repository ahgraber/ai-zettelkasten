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
    """A model accessed as one substitutable dependency through one method.

    Implementations turn a fully-built prompt into model output text. The stage
    makes no model call outside :meth:`generate`, so a recording or deterministic
    implementation observes and drives every invocation.
    """

    def generate(self, prompt: str) -> str:
        """Return the model's text completion for ``prompt``."""
        ...


class PydanticAILLMClient:
    """Production :class:`LLMClient` backed by a ``pydantic-ai`` agent.

    Holds a string-output agent and runs each prompt synchronously, returning the
    agent's text output. The agent (model, instructions, settings) is constructed
    by the composition root and injected, keeping provider details out of the
    contextualization logic.
    """

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
    """Deterministic, recording :class:`LLMClient` for tests.

    Records every prompt it receives at :attr:`prompts` (so a test can assert the
    stage routes all model calls through this one access point) and returns a
    deterministic completion. The default completion is a stable hash of the
    prompt; pass ``responder`` to return canned output — for example a
    referent-naming context blurb — for a specific scenario.
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
    """The resolved model identity a derivation key fingerprints — not the alias that named it.

    ``provider`` is the OpenAI-compatible base URL, ``model`` the model name, and
    ``settings`` sorted ``(name, value)`` generation-setting pairs (empty today).
    An alias swap that resolves to the same triple keeps the same fingerprint; a
    changed resolution under an unchanged alias changes it.
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
