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

from typing import TYPE_CHECKING, Callable, Protocol, runtime_checkable

import xxhash

if TYPE_CHECKING:
    from pydantic_ai import Agent


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
