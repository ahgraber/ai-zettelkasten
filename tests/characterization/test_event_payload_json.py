"""Pin the exact persisted JSON of every event payload kind.

``pipeline_events.payload_json`` stores ``payload.model_dump_json()`` verbatim, so
field order, field names, null handling, and datetime rendering are all part of the
durable record. One fixture file per event module, keyed by the kind's stored value,
records the constructor inputs beside the exact serialized string.

Completeness is asserted in both directions: a kind with no fixture and a fixture
naming no kind both fail, so adding or removing a kind is a reviewed fixture diff.

The fixtures are hand-written and there is no regeneration path.
"""

from __future__ import annotations

import datetime
import importlib
import json
from pathlib import Path
from typing import Any

import pytest

_FIXTURE_DIR = Path(__file__).parent / "fixtures" / "events"

#: Fixture stem → (module holding the payload classes, name of that module's kind enum).
_EVENT_MODULES: dict[str, tuple[str, str]] = {
    "conversion": ("aizk.conversion.datamodel.events", "ConversionEventKind"),
    "contextualization": ("aizk.contextualization.events", "GraphEventKind"),
    "extraction": ("aizk.extraction.events", "ExtractionEventKind"),
}

#: Marker wrapping an ISO-8601 timestamp in a fixture, so the test hands the payload
#: a real ``datetime`` and can assert the value it pins is timezone-aware.
_DATETIME_MARKER = "__datetime__"


def _coerce(value: Any) -> Any:
    """Turn fixture markers into the Python values a payload constructor expects."""
    if isinstance(value, dict) and set(value) == {_DATETIME_MARKER}:
        moment = datetime.datetime.fromisoformat(value[_DATETIME_MARKER])
        assert moment.tzinfo is not None, "event fixtures pin timezone-aware datetimes"
        return moment
    if isinstance(value, dict):
        return {key: _coerce(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_coerce(item) for item in value]
    return value


def _load(stem: str) -> dict[str, Any]:
    """Read one event module's fixture file."""
    return json.loads((_FIXTURE_DIR / f"{stem}.json").read_text(encoding="utf-8"))


def _kind_values(stem: str) -> list[str]:
    """Return the stored value of every kind the module declares."""
    module_path, enum_name = _EVENT_MODULES[stem]
    kind_enum = getattr(importlib.import_module(module_path), enum_name)
    return [member.value for member in kind_enum]


_CASES = [
    pytest.param(stem, kind, id=f"{stem}-{kind}") for stem in sorted(_EVENT_MODULES) for kind in sorted(_load(stem))
]


@pytest.mark.parametrize("stem", sorted(_EVENT_MODULES))
def test_every_kind_has_a_fixture_and_every_fixture_names_a_kind(stem: str) -> None:
    """The fixture's keys are exactly the module's declared kinds."""
    assert sorted(_load(stem)) == sorted(_kind_values(stem))


@pytest.mark.parametrize(("stem", "kind"), _CASES)
def test_payload_serializes_to_the_pinned_json(stem: str, kind: str) -> None:
    """Constructing the payload from the recorded inputs reproduces the pinned JSON."""
    entry = _load(stem)[kind]
    module = importlib.import_module(_EVENT_MODULES[stem][0])
    payload_class = getattr(module, entry["payload_class"])
    payload = payload_class(**{name: _coerce(value) for name, value in entry["inputs"].items()})
    assert payload.kind == kind
    assert payload.model_dump_json() == entry["model_dump_json"]
