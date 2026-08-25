"""Stage descriptor contract and registry for the operator console.

Every pipeline stage contributes a :class:`StageDescriptor`; the console's generic
routes and templates derive all stage-specific behavior from it, so registering a
stage — rather than copying a route-and-template set — is enough to give it a
monitor, a dashboard entry, filtering, actions, and a drill-down.

The *generic lifecycle vocabulary* the dashboard groups by is
:class:`~aizk.pipeline.lifecycle.WorkUnitStatus`. A stage that persists its own
status enum declares a ``rollup`` mapping every native status onto exactly one
``WorkUnitStatus`` category; graph stages already persist ``WorkUnitStatus`` and
roll up by identity. The rollup is read-side only — no status storage changes.

Capabilities beyond the required set are **optional and feature-detected**: a
descriptor leaves the callable ``None`` and the console omits that surface for the
stage, so a stage is never obliged to have a concept it does not have.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from aizk.pipeline.lifecycle import WorkUnitStatus

if TYPE_CHECKING:
    from sqlmodel import Session

    from aizk.conversion.auth import Principal


#: Graph stages persist :class:`WorkUnitStatus`, so their rollup is the identity map.
#: A stage that persists its own native enum (e.g. conversion) defines its own
#: rollup in its stage module; the core stays free of any stage's native vocabulary.
GRAPH_ROLLUP: dict[WorkUnitStatus, WorkUnitStatus] = {status: status for status in WorkUnitStatus}


@dataclass(frozen=True)
class StageAction:
    """A declared operator action a stage's monitor offers over its work-units.

    ``apply`` dispatches to the stage's own domain helper inside the caller's
    transaction and raises :class:`ValueError` when the unit is ineligible in its
    current status; the monitor counts that as skipped and leaves the unit
    unaltered. Per-action eligibility is thus encoded in ``apply``'s status check —
    the same pattern each stage's JSON API uses — rather than a separate predicate.
    """

    #: Stable action key submitted by the monitor form (e.g. ``"retry"``).
    key: str
    #: Past-tense verb for the applied-units summary (e.g. ``"retried"``).
    applied_label: str
    #: Mutate the unit in the caller's open transaction, or raise ``ValueError``.
    apply: Callable[["Session", Any, "Principal"], None]


@dataclass(frozen=True)
class StageDescriptor:
    """Everything the console needs to operate one pipeline stage.

    The list/count/detail/action callables receive the resolved request
    :class:`~aizk.conversion.auth.Principal` and preserve each stage's own
    principal-scoping contract (conversion scopes to the owner; graph stages, whose
    work-units carry no owner, add none).

    A work-unit (what ``get_unit`` returns and ``list_units`` rows describe) is
    required to expose an integer ``id`` and a ``source_id``; the generic drill-down
    reads both. Everything else stage-specific is projected by the stage's own
    callables and templates.
    """

    #: Stage key used in URLs, navigation, and the dashboard.
    key: str
    #: Human-readable stage name.
    label: str
    #: Query one filtered/searched/paginated page of the stage's work-units.
    #: Signature: ``(session, principal, *, status_filter, search, sort, direction,
    #: limit, offset) -> MonitorPage``. Owns the stage's search over its declared
    #: searchable identifiers.
    list_units: Callable[..., Any]
    #: Count the stage's work-units grouped by native status value:
    #: ``(session, principal) -> dict[str, int]``.
    count_by_status: Callable[["Session", "Principal"], dict[str, int]]
    #: Fetch a single work-unit by id honoring principal-scoping, or ``None``:
    #: ``(session, principal, unit_id) -> unit | None``.
    get_unit: Callable[["Session", "Principal", int], Any | None]
    #: Jinja macro partial (``extra_headers()`` / ``extra_cells(unit)``) the generic
    #: monitor imports to render any stage-specific row columns.
    columns_template: str
    #: Native status values, in display order, for the filter control and legend.
    native_statuses: list[str]
    #: Native status → generic ``WorkUnitStatus`` category (see module rollups).
    rollup: dict[Any, WorkUnitStatus]
    #: The shared ``pipeline_events`` stage string the drill-down reads the unit's
    #: lifecycle trail from (keyed by ``(events_stage, str(unit.id))``).
    events_stage: str
    #: Declared actions only; the monitor offers exactly these and no others.
    actions: list[StageAction]
    #: Optional drill-down detail composer over the unit's runs/artifacts:
    #: ``(session, unit) -> dict``. The event trail renders for every stage; this
    #: adds the stage's runs/artifact section when present.
    detail: Callable[["Session", Any], dict[str, Any]] | None = None
    #: Jinja partial rendering the ``detail`` section; required iff ``detail`` is set.
    detail_template: str | None = None
    #: Count of stage-specific columns ``columns_template`` adds, for the
    #: empty-state cell's ``colspan``.
    extra_columns: int = 0
    #: Optional read-side split of the ``FAILED`` category into
    #: ``(awaiting_retry, permanent)`` for the dashboard:
    #: ``(session, principal) -> tuple[int, int]``. A stage whose failures carry a
    #: retry-scheduling distinction (a scheduled next attempt, or a retryable vs.
    #: permanent native status) declares it; a stage without that concept omits it
    #: and the dashboard shows the ``FAILED`` total unsplit.
    failed_split: Callable[["Session", "Principal"], tuple[int, int]] | None = None
    #: Optional count of sources the stage owes a work-unit but has none for:
    #: ``(session, principal) -> int``. The count sits outside the lifecycle rollup —
    #: it counts no work-unit — so the per-stage unit total is unaffected.
    pending_count: Callable[["Session", "Principal"], int] | None = None
    #: Optional listing of those same pending sources, each as a mapping carrying at
    #: least ``source_id`` and ``title``: ``(session, principal) -> list[dict]``.
    #: Reads the same derivation ``pending_count`` counts, so the listing and the
    #: count cannot disagree about what is pending.
    pending_list: Callable[["Session", "Principal"], list[dict[str, Any]]] | None = None
    #: Optional count of sources whose completed work consumed since-superseded
    #: upstream state: ``(session, principal) -> int``. A stage without a staleness
    #: derivation shows no stale figure and no stale marking on its monitor rows.
    stale_count: Callable[["Session", "Principal"], int] | None = None


#: Module-level ordered registry of stage descriptors, keyed by stage key.
_REGISTRY: dict[str, StageDescriptor] = {}


def register_stage(descriptor: StageDescriptor) -> None:
    """Register a stage descriptor, replacing any prior registration for its key."""
    _REGISTRY[descriptor.key] = descriptor


def registered_stages() -> list[StageDescriptor]:
    """Return the registered descriptors in registration order."""
    return list(_REGISTRY.values())


def get_descriptor(key: str) -> StageDescriptor | None:
    """Return the descriptor for ``key``, or ``None`` when the key is unregistered."""
    return _REGISTRY.get(key)


def rollup_counts(descriptor: StageDescriptor, native_counts: dict[str, int]) -> dict[WorkUnitStatus, int]:
    """Fold a stage's native-status counts onto the generic lifecycle vocabulary.

    ``native_counts`` maps native status *values* to counts (as ``count_by_status``
    returns). Each native value is looked up in the descriptor's rollup and its count
    added to the target category, so no unit is dropped or double-counted.
    """
    generic: dict[WorkUnitStatus, int] = dict.fromkeys(WorkUnitStatus, 0)
    native_by_value = {native.value: native for native in descriptor.rollup}
    for value, count in native_counts.items():
        native = native_by_value.get(value)
        if native is None:
            # Fail closed rather than KeyError-500: a native status with no rollup
            # entry would otherwise be silently miscounted. The exhaustiveness test
            # keeps this unreachable, but a future unmapped status names itself here.
            raise ValueError(f"stage {descriptor.key!r} has no rollup mapping for native status {value!r}")
        generic[descriptor.rollup[native]] += count
    return generic
