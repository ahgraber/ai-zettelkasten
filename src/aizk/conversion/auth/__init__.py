"""Authentication primitives for the conversion API.

At this build the only implemented auth mode is ``trust_network``: the API
treats every inbound request as authenticated and resolves a single
deployment-wide :class:`Principal` whose ``subject`` is
``AIZK_DEFAULT_PRINCIPAL``. The :class:`Principal` ``subject`` is what lands
in the database as ``owner_id`` on Source, Job, and Output rows; the
``provenance`` discriminator is request-scoped and is intentionally NOT
persisted.

Adding a new auth mode (e.g. ``token``, ``proxy_headers``, ``oidc``) is a
delta on FOUR sites — no schema migration is required:

1. ``Principal.provenance`` literal in ``auth/principal.py`` — widen the
   ``Literal[...]`` to admit the new provenance string.
2. ``AuthSettings.auth_mode`` validator in ``utilities/config.py`` — remove
   the new mode from the "reserved but not implemented" rejection branch.
3. ``get_principal`` resolver in ``api/dependencies.py`` — add a ``case``
   arm in the ``match settings.auth_mode:`` block that constructs the
   :class:`Principal` from request state (headers, cookies, OIDC claims,
   etc.) appropriate to the new mode.
4. Test coverage for the new mode under ``tests/conversion/unit/api/`` and
   ``tests/conversion/unit/utilities/`` mirroring the ``trust_network``
   tests.
"""

from aizk.conversion.auth.principal import Principal

__all__ = ["Principal"]
