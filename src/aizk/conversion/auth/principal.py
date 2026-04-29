"""Principal abstraction: who a request belongs to.

Resolved at the API trust boundary by `get_principal` and propagated to
materialization helpers as `principal.subject` (which lands in the database
as `owner_id`). The `provenance` discriminator is request-scoped and is NOT
persisted; it widens in future changes (`token`, `proxy_headers`, `oidc`) so
exhaustive `match` statements in the resolver fail to type-check until each
new mode is handled.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict


class Principal(BaseModel):
    """The authenticated subject of an inbound API request.

    `subject` is the durable identifier persisted as `owner_id` on Source,
    Job, and Output rows. `provenance` is request-scoped and records which
    auth mode produced this Principal; it is intentionally NOT persisted.

    Frozen to prevent accidental mutation downstream of the resolver.
    """

    model_config = ConfigDict(frozen=True)

    subject: str
    provenance: Literal["trust_network"]


__all__ = ["Principal"]
