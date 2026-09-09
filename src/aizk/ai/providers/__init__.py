"""The model-provider clients this tree talks to, and their resolved configuration.

Two clients live here: the single-prompt client the contextualization stage calls
through :mod:`aizk.ai.providers.llm`, and the OpenAI batch client in
:mod:`aizk.ai.providers.batch`. Both take a resolved endpoint from the caller, so
a stage's settings never reach this package and this package never reaches a
stage.

It is a home for those clients, not a shared rate-limit, budget, retry, or
tracing layer; a consumer that needs one of those owns it until a change
introduces a shared one.
"""

from __future__ import annotations
