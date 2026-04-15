"""Anthropic-style contextual retrieval with OpenRouter cache_control.

Full document goes in the system prompt, section in the user message. Reusing
the same agent across sections of one doc keeps the system prompt identical,
so OpenRouter's automatic prompt caching (Anthropic-tier: opt-in via top-level
`cache_control`; OpenAI/DeepSeek/Grok/Moonshot/Groq: automatic) amortizes cost.
"""

from __future__ import annotations

from pydantic import BaseModel
from pydantic_ai import Agent, RunContext
from pydantic_ai.models.openai import OpenAIChatModel, OpenAIChatModelSettings
from pydantic_ai.providers.openrouter import OpenRouterProvider

from _claimify.models import LoadedDoc, Section

CONTEXTUALIZE_SYSTEM_TEMPLATE = (
    "You situate a chunk within the full document for retrieval.\n<document>\n{full_markdown}\n</document>"
)

CONTEXTUALIZE_USER_TEMPLATE = (
    "<chunk>\n{chunk}\n</chunk>\n"
    "Please give a short succinct context to situate this chunk within the "
    "overall document for the purposes of improving search retrieval of the "
    "chunk. Answer only with the succinct context and nothing else."
)


class SectionContext(BaseModel):
    context: str


def _is_anthropic_model(model: str) -> bool:
    return model.startswith("anthropic/") or model.startswith("claude")


def make_context_agent(model: str, *, api_key: str | None = None) -> Agent[str, SectionContext]:
    """Build a contextualizer agent wired to OpenRouter.

    `deps` is the full document markdown; it is rendered into the system prompt
    on every run so a single agent can be reused across docs while keeping the
    system prompt stable per doc (cache-friendly).
    """
    provider = OpenRouterProvider(api_key=api_key)
    llm = OpenAIChatModel(model, provider=provider)
    settings = (
        OpenAIChatModelSettings(extra_body={"cache_control": {"type": "ephemeral"}})
        if _is_anthropic_model(model)
        else None
    )
    agent: Agent[str, SectionContext] = Agent(
        llm,
        output_type=SectionContext,
        deps_type=str,
        model_settings=settings,
    )

    @agent.system_prompt
    def _render_system(ctx: RunContext[str]) -> str:
        return CONTEXTUALIZE_SYSTEM_TEMPLATE.format(full_markdown=ctx.deps)

    return agent


async def contextualize_section(
    agent: Agent[str, SectionContext],
    doc: LoadedDoc,
    section: Section,
) -> str:
    """Return a short situating context string for `section` within `doc`."""
    user = CONTEXTUALIZE_USER_TEMPLATE.format(chunk=section.content)
    result = await agent.run(user, deps=doc.markdown)
    return result.output.context
