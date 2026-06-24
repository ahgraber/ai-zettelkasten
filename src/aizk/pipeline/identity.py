"""The pipeline-identity grammar: how every stage names, identifies, and invalidates derived state.

This module is the standing reference a new pipeline stage cites instead of
re-deriving its own identity-and-provenance conventions. It defines the shared
:func:`derivation_key` helper; the grammar it documents is enforced by code here
and by the run primitive in :mod:`aizk.pipeline.run`.

Five identifier roles
----------------------
Every derived, persisted row participates in five distinct roles. Keeping them
separate is what makes the pipeline duplicate-free, portable, and traceable:

1. **Source identity** (``source_id``) — the durable identity of the source a
   derivation chain belongs to. One canonical name across the data model and
   public interfaces.
2. **Run** (``pipeline_runs.id``, scoped by ``(stage, scope_id)``) — one
   generation of a stage's outputs for a scope. At most one run per
   ``(stage, scope_id)`` is active (:func:`aizk.pipeline.run.record_run`).
3. **Derivation key** — a fingerprint of the *semantic* inputs that determined a
   run's outputs (content fingerprints, producer/prompt/model/config versions,
   and the upstream runs' derivation keys). It decides reuse-versus-supersede and
   is computed by :func:`derivation_key`.
4. **Output identity** — the stable surrogate identity of a persisted derived
   row: assigned once, never recomputed, not encoding the row's content, and not
   embedding a database-local identifier. Cross-generation reuse of that
   surrogate is governed by a producer's sameness-key.
5. **Provenance pointers** — explicit references to the upstream runs/rows a
   derivation consumed, distinct from both the derivation key and the identity.

Suffix convention (SHOULD)
--------------------------
A name communicates its role: ``_id`` is a pointable identity, ``_key`` a
computed matching fingerprint, ``_hash`` a content fingerprint. A run's scope
reference is an identity, hence ``scope_id``.

Surrogate identity
------------------
A derived row's identity is a surrogate — never a content hash and never a
database-local id. Content fingerprints survive as observable columns (e.g.
``content_hash``) for change-detection, never as the identity, so identity
stability does not depend on a producer being deterministic.

Semantic derivation key
-----------------------
The derivation key embeds the *upstream* derivation key (not the upstream run's
local id), so an upstream input change propagates downstream automatically and
portably: the same logical content computes the same key on any backend. No
database-local identifier is ever admitted as an input — that is the invariant
:func:`derivation_key` exists to keep.

Lazy invalidation
-----------------
A producer-version change marks prior generations logically stale (a cheap
derivation-key comparison) without eagerly recomputing them; recompute is a
separate, deliberate action, and a large-blast-radius reprocessing is gated by an
explicit human confirmation. Each derived row records its producer version, so a
version-heterogeneous corpus is valid and queryable.
"""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence


def derivation_key(
    *,
    inputs: "Mapping[str, object]",
    upstream_keys: "Sequence[str]" = (),
) -> str:
    """Compute a stage run's derivation key: a stable hash over semantic inputs only.

    The derivation key is the fingerprint a stage compares to decide whether to
    reuse its active run or supersede it with a new generation. It is a function
    of *semantic* inputs alone, so the same logical derivation computes the same
    key on any backend (the portability invariant) and an upstream change
    propagates downstream automatically.

    Args:
        inputs: The semantic determinants of the derived output — content
            fingerprints (e.g. a ``content_hash``), producer/prompt/model/config
            versions, and any other content-derived discriminators. Values must
            be JSON-serializable; nested mappings are canonicalized by key.
            **Caller obligation:** MUST NOT contain any database-local identifier
            (a ``run_id``, an autoincrement row id, or a surrogate row identity) —
            admitting one would make the key non-portable. The helper hashes what
            it is given and cannot enforce this by inspection (a ``run_id`` is
            indistinguishable by value from a version int); use an upstream run's
            ``derivation_key`` via ``upstream_keys`` rather than its id, and keep
            ids out of ``inputs``.
        upstream_keys: The ``derivation_key`` of each upstream run this
            derivation consumed, in a caller-defined stable order. Embedding the
            upstream key (rather than the upstream run's local id) is what makes
            invalidation propagate down the chain portably.

    Returns:
        A hex-encoded SHA-256 digest over the canonical serialization of
        ``inputs`` and ``upstream_keys``.
    """
    canonical = json.dumps(
        {"inputs": dict(inputs), "upstream": list(upstream_keys)},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
