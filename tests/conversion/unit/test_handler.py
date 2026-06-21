"""Unit tests for :class:`aizk.conversion.handler.ConversionStageHandler`.

Covers the runner-facing surface (properties, ``scope_key``,
``validate_dependencies``) and :meth:`ConversionStageHandler.map_result`.

The DB-backed claim/recovery surface — ``claim_next`` and ``recover_stale`` —
runs inside a caller-owned (runner-owned) session and never commits, via
:func:`~aizk.conversion.queries.claim_next_in_session` /
:func:`~aizk.conversion.queries.recover_stale_in_session` (the claim
post-increments ``attempts``).
"""

from __future__ import annotations

import datetime
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlmodel import Session, SQLModel, select

from aizk.conversion import handler as repository_mod
from aizk.conversion.core.errors import DenyListDestination, EgressPolicyError
import aizk.conversion.datamodel  # noqa: F401  (registers SQLModel metadata)
from aizk.conversion.datamodel.events import (
    STAGE,
    ConversionEventKind,
    events_for_job,
    parse_payload_lenient,
)
from aizk.conversion.datamodel.job import ConversionJob, ConversionJobStatus
from aizk.conversion.datamodel.source import Source
from aizk.conversion.handler import ConversionStageHandler
from aizk.conversion.processing.errors import (
    ConversionArtifactsMissingError,
    ConversionCancelledError,
    ConversionSubprocessError,
    ConversionTimeoutError,
    JobDataIntegrityError,
    JobErrorDetails,
    PreflightError,
    ReportedChildError,
    SubprocessMetadataInvalid,
)
from aizk.conversion.processing.types import SubprocessMetadata, SupervisionResult
from aizk.conversion.utilities.config import ConversionConfig
from aizk.pipeline.events import PipelineEvent
from aizk.pipeline.handler import Isolation, StageHandler
from aizk.pipeline.lifecycle import RetryClass, TerminalOutcome, WorkUnitStatus


def _all_conversion_events(session: Session) -> list[PipelineEvent]:
    """Return every conversion-stage event row, regardless of job."""
    return list(session.exec(select(PipelineEvent).where(PipelineEvent.stage == STAGE)).all())


def _events_for_job_kind(session: Session, job_id: int, kind: ConversionEventKind) -> list[PipelineEvent]:
    """Return a job's conversion events of a single kind from pipeline_events."""
    return [e for e in events_for_job(session, job_id) if e.kind == kind]


@pytest.fixture()
def config() -> ConversionConfig:
    """Build a hermetic config with explicit timeout/concurrency values.

    Uses ``_env_file=None`` and explicit overrides so the test never reads
    ``.env`` or shell-exported state (hermetic-tests rule).
    """
    return ConversionConfig(
        _env_file=None,
        worker_job_timeout_seconds=123.0,
        worker_concurrency=7,
    )


@pytest.fixture()
def handler(config: ConversionConfig) -> ConversionStageHandler:
    """Build a handler over the hermetic config."""
    return ConversionStageHandler(config)


def test_satisfies_stage_repository_protocol(handler: ConversionStageHandler) -> None:
    """The handler structurally satisfies the runtime-checkable protocol."""
    assert isinstance(handler, StageHandler)


def test_stage_name_is_conversion(handler: ConversionStageHandler) -> None:
    """``stage`` is the stable correlation-spine name for this stage."""
    assert handler.stage == "conversion"


def test_validate_dependencies_runs_all_startup_probes() -> None:
    """The runner's startup gate runs every adapter-declared probe."""
    from types import SimpleNamespace

    calls: list[str] = []
    probes = [lambda: calls.append("s3"), lambda: calls.append("db")]
    runtime = SimpleNamespace(capabilities=SimpleNamespace(startup_probes=probes))
    handler = ConversionStageHandler(ConversionConfig(_env_file=None), runtime=runtime)

    handler.validate_dependencies()

    assert calls == ["s3", "db"]


def test_validate_dependencies_propagates_probe_failure() -> None:
    """A probe that raises blocks work acceptance by propagating the error."""
    from types import SimpleNamespace

    from aizk.conversion.utilities.startup import StartupValidationError

    def _boom() -> None:
        raise StartupValidationError("S3 bucket unreachable")

    runtime = SimpleNamespace(capabilities=SimpleNamespace(startup_probes=[_boom]))
    handler = ConversionStageHandler(ConversionConfig(_env_file=None), runtime=runtime)

    with pytest.raises(StartupValidationError, match="S3 bucket unreachable"):
        handler.validate_dependencies()


def test_scope_key_is_per_job_string(handler: ConversionStageHandler) -> None:
    """``scope_key`` renders the integer job-id handle as a string."""
    assert handler.scope_key(42) == "42"


def test_timeout_reflects_configured_seconds(handler: ConversionStageHandler) -> None:
    """``timeout`` is the configured ``worker_job_timeout_seconds`` as a timedelta."""
    assert handler.timeout == datetime.timedelta(seconds=123.0)


def test_concurrency_limit_reflects_configured_value(handler: ConversionStageHandler) -> None:
    """``concurrency_limit`` is the configured ``worker_concurrency``."""
    assert handler.concurrency_limit == 7


def test_isolation_is_subprocess(handler: ConversionStageHandler) -> None:
    """Conversion runs its unit-of-work in an isolated subprocess."""
    assert handler.isolation is Isolation.SUBPROCESS


def test_map_result_success_is_succeeded(handler: ConversionStageHandler) -> None:
    """A non-exception result maps to a ``SUCCEEDED`` outcome with no retry class."""
    outcome = handler.map_result("any-non-exception-result")

    assert outcome == TerminalOutcome(WorkUnitStatus.SUCCEEDED)
    assert outcome.retry_class is None


def test_map_result_timeout_takes_precedence_over_retryable_attr(
    handler: ConversionStageHandler,
) -> None:
    """A timeout maps to ``TIMED_OUT`` even though its ``retryable`` attr is True.

    Guards that the timeout branch is selected by type, not folded into the
    generic ``FAILED`` path that would consult ``retryable``.
    """
    outcome = handler.map_result(ConversionTimeoutError("too slow", phase="converting"))

    assert outcome == TerminalOutcome(WorkUnitStatus.TIMED_OUT)
    assert outcome.retry_class is None


def test_map_result_cancelled_is_cancelled(handler: ConversionStageHandler) -> None:
    """A cancellation maps to the ``CANCELLED`` terminal outcome."""
    outcome = handler.map_result(ConversionCancelledError("job 1 cancelled"))

    assert outcome == TerminalOutcome(WorkUnitStatus.CANCELLED)
    assert outcome.retry_class is None


@pytest.mark.parametrize(
    ("error", "expected_retry_class"),
    [
        # Retryable conversion errors (retryable=True on the class).
        pytest.param(
            ConversionSubprocessError("subprocess exited 1"),
            RetryClass.RETRYABLE,
            id="subprocess-error-retryable",
        ),
        pytest.param(
            PreflightError("preflight blew up"),
            RetryClass.RETRYABLE,
            id="preflight-error-retryable",
        ),
        # Permanent conversion errors (retryable=False on the class).
        pytest.param(
            ConversionArtifactsMissingError("no artifacts"),
            RetryClass.PERMANENT,
            id="artifacts-missing-permanent",
        ),
        pytest.param(
            JobDataIntegrityError("bad job row"),
            RetryClass.PERMANENT,
            id="job-data-integrity-permanent",
        ),
        pytest.param(
            SubprocessMetadataInvalid("schema-incompatible metadata"),
            RetryClass.PERMANENT,
            id="subprocess-metadata-invalid-permanent",
        ),
        # Egress-policy rejections are a property of the input, never retried.
        pytest.param(
            EgressPolicyError("egress denied"),
            RetryClass.PERMANENT,
            id="egress-policy-permanent",
        ),
        pytest.param(
            DenyListDestination("ip in deny set"),
            RetryClass.PERMANENT,
            id="deny-list-permanent",
        ),
    ],
)
def test_map_result_classifies_conversion_errors_by_retryable_attr(
    handler: ConversionStageHandler,
    error: BaseException,
    expected_retry_class: RetryClass,
) -> None:
    """Conversion errors map to ``FAILED`` with retry class from ``error.retryable``.

    ``map_result`` reads ``bool(getattr(error, "retryable", True))`` to choose
    the ``FAILED_RETRYABLE`` vs ``FAILED_PERM`` branch.
    """
    outcome = handler.map_result(error)

    assert outcome == TerminalOutcome(WorkUnitStatus.FAILED, expected_retry_class)


@pytest.mark.parametrize(
    ("retryable", "expected_retry_class"),
    [
        pytest.param(True, RetryClass.RETRYABLE, id="reported-retryable"),
        pytest.param(False, RetryClass.PERMANENT, id="reported-permanent"),
    ],
)
def test_map_result_reported_child_error_honors_instance_retryable(
    handler: ConversionStageHandler,
    retryable: bool,
    expected_retry_class: RetryClass,
) -> None:
    """``ReportedChildError`` carries a per-instance ``retryable`` the mapping honors.

    Unlike the other conversion errors whose ``retryable`` is a class attribute,
    a reported child error sets ``retryable`` per instance from the subprocess
    report; ``map_result`` must read the instance value.
    """
    error = ReportedChildError("child failed", "some_code", retryable=retryable)

    outcome = handler.map_result(error)

    assert outcome == TerminalOutcome(WorkUnitStatus.FAILED, expected_retry_class)


def test_map_result_generic_exception_defaults_to_retryable(
    handler: ConversionStageHandler,
) -> None:
    """A plain exception lacking ``retryable`` defaults to a retryable failure.

    For an unknown exception the default is to retry rather than mark permanent:
    when in doubt, retry.
    """
    outcome = handler.map_result(OSError("transient disk error"))

    assert outcome == TerminalOutcome(WorkUnitStatus.FAILED, RetryClass.RETRYABLE)


def test_finalize_succeeded_is_noop(handler: ConversionStageHandler) -> None:
    """A SUCCEEDED outcome is a pure no-op (the success write lives in upload).

    ``_execute_upload`` already wrote the SUCCEEDED terminal status + output, so
    ``finalize`` must not touch the session for a SUCCEEDED outcome — no row
    fetch, no transition. A guard session that fails any access proves it.
    """

    class _NoTouchSession:
        def get(self, *_a, **_k):  # pragma: no cover - must never be called
            raise AssertionError("finalize must not read the session on SUCCEEDED")

    # No exception was raised; finalize on SUCCEEDED must short-circuit.
    handler.finalize(_NoTouchSession(), 1, TerminalOutcome(WorkUnitStatus.SUCCEEDED))


# ---------------------------------------------------------------------------
# claim_next / recover_stale — DB-backed (chunk B)
#
# These exercise the real query + transition against an in-memory SQLite engine
# built from ``SQLModel.metadata`` (no ``.env``, no shell state). They assert the
# claim post-increments ``attempts``, recovery resets stale RUNNING jobs, and the
# handler runs inside a caller-owned transaction without committing.
# ---------------------------------------------------------------------------

_STALE_MINUTES = 30


def _utcnow() -> datetime.datetime:
    """Return a timezone-aware UTC timestamp for seeding deterministic rows."""
    return datetime.datetime.now(datetime.timezone.utc)


@pytest.fixture()
def db_config() -> ConversionConfig:
    """Hermetic config carrying an explicit stale threshold for recovery tests."""
    return ConversionConfig(
        _env_file=None,
        worker_stale_job_minutes=_STALE_MINUTES,
    )


@pytest.fixture()
def db_repository(db_config: ConversionConfig) -> ConversionStageHandler:
    """Build a handler over the recovery-aware hermetic config."""
    return ConversionStageHandler(db_config)


@pytest.fixture()
def engine(tmp_path):
    """Provide an isolated SQLite engine with the full conversion schema.

    Built from ``SQLModel.metadata`` (not Alembic) so the test is hermetic and
    self-contained: no ``.env``, no migrations, no shared session state.
    """
    eng = create_engine(f"sqlite:///{tmp_path}/handler.db", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(eng)
    return eng


_KARAKEEP_BOOKMARK_REF = '{"kind":"karakeep_bookmark","bookmark_id":"x"}'


def _create_source(session: Session) -> Source:
    """Build, persist, and return a Source row the seeded jobs reference."""
    src = Source(
        karakeep_id=f"k_{uuid4().hex[:8]}",
        source_ref=_KARAKEEP_BOOKMARK_REF,
        source_ref_hash=uuid4().hex,
        owner_id="self",
    )
    session.add(src)
    session.commit()
    session.refresh(src)
    return src


@pytest.fixture()
def source(engine) -> Source:
    """Persist a Source row the seeded jobs reference via ``aizk_uuid``."""
    with Session(engine) as session:
        return _create_source(session)


def _seed_job(
    engine,
    source: Source,
    *,
    status: ConversionJobStatus,
    queued_at: datetime.datetime | None = None,
    earliest_next_attempt_at: datetime.datetime | None = None,
    started_at: datetime.datetime | None = None,
    attempts: int = 0,
) -> int:
    """Persist one ConversionJob and return its id.

    Exposes precisely the columns the claim/recovery eligibility queries read so
    each test can place a job in (or out of) the eligible set deterministically.
    """
    with Session(engine) as session:
        job = ConversionJob(
            aizk_uuid=source.aizk_uuid,
            owner_id="self",
            title="test",
            payload_version=1,
            status=status,
            attempts=attempts,
            idempotency_key=uuid4().hex,
            queued_at=queued_at,
            earliest_next_attempt_at=earliest_next_attempt_at,
            started_at=started_at,
        )
        session.add(job)
        session.commit()
        session.refresh(job)
        return job.id


def test_claim_next_claims_oldest_eligible_and_increments_attempts(
    db_repository: ConversionStageHandler, engine, source: Source
) -> None:
    """``claim_next`` claims the oldest eligible job, transitions it, increments attempts.

    Seeds (in non-submission order) a younger QUEUED job, an older QUEUED job, a
    retryable job past its retry-wait, a retryable job still within its
    retry-wait, and a RUNNING job. The oldest eligible by ``queued_at`` must be
    claimed, transitioned to RUNNING with ``attempts`` post-incremented, and a
    ``claimed`` event recorded — the ``claim_next_in_session`` contract.
    """
    now = _utcnow()
    # Arrange: oldest eligible is the retryable job past its retry-wait.
    oldest_id = _seed_job(
        engine,
        source,
        status=ConversionJobStatus.FAILED_RETRYABLE,
        queued_at=now - datetime.timedelta(minutes=10),
        earliest_next_attempt_at=now - datetime.timedelta(minutes=1),
        attempts=2,
    )
    _seed_job(
        engine,
        source,
        status=ConversionJobStatus.QUEUED,
        queued_at=now - datetime.timedelta(minutes=5),
    )
    _seed_job(
        engine,
        source,
        status=ConversionJobStatus.QUEUED,
        queued_at=now - datetime.timedelta(minutes=1),
    )
    # Not eligible: retry-wait still in the future.
    _seed_job(
        engine,
        source,
        status=ConversionJobStatus.FAILED_RETRYABLE,
        queued_at=now - datetime.timedelta(minutes=20),
        earliest_next_attempt_at=now + datetime.timedelta(minutes=5),
    )
    # Not eligible: already RUNNING.
    _seed_job(
        engine,
        source,
        status=ConversionJobStatus.RUNNING,
        queued_at=now - datetime.timedelta(minutes=30),
        started_at=now,
    )

    # Act: claim inside a caller-owned transaction, then commit (runner's role).
    with Session(engine) as session:
        claimed = db_repository.claim_next(session)
        session.commit()

    # Assert.
    assert claimed == oldest_id
    with Session(engine) as verify:
        job = verify.get(ConversionJob, oldest_id)
        assert job.status == ConversionJobStatus.RUNNING
        assert job.attempts == 3, "claim post-increments attempts"
        assert job.started_at is not None
        events = events_for_job(verify, oldest_id)
        assert len(events) == 1
        assert events[0].kind == ConversionEventKind.CLAIMED
        assert events[0].from_status == ConversionJobStatus.FAILED_RETRYABLE
        assert events[0].to_status == ConversionJobStatus.RUNNING
        # Event attempt matches the post-incremented value.
        assert events[0].attempt == 3


def test_claim_next_returns_none_when_no_job_eligible(
    db_repository: ConversionStageHandler, engine, source: Source
) -> None:
    """``claim_next`` returns ``None`` when nothing is claimable.

    Only ineligible jobs exist: one RUNNING, one retryable still within its
    retry-wait, and one permanently failed. None should be claimed.
    """
    now = _utcnow()
    _seed_job(engine, source, status=ConversionJobStatus.RUNNING, queued_at=now, started_at=now)
    _seed_job(
        engine,
        source,
        status=ConversionJobStatus.FAILED_RETRYABLE,
        queued_at=now,
        earliest_next_attempt_at=now + datetime.timedelta(minutes=5),
    )
    _seed_job(engine, source, status=ConversionJobStatus.FAILED_PERM, queued_at=now)

    with Session(engine) as session:
        claimed = db_repository.claim_next(session)
        session.commit()

    assert claimed is None


def test_claim_next_does_not_commit(db_repository: ConversionStageHandler, engine, source: Source) -> None:
    """``claim_next`` stages its writes but leaves the commit to the caller.

    A second session opened before the caller commits must still observe the
    pre-claim state — proving the handler runs inside the runner-owned
    transaction and never commits on its own.
    """
    now = _utcnow()
    job_id = _seed_job(engine, source, status=ConversionJobStatus.QUEUED, queued_at=now)

    with Session(engine) as session:
        claimed = db_repository.claim_next(session)
        assert claimed == job_id

        # Uncommitted: a separate connection still sees QUEUED and no event.
        with Session(engine) as other:
            other_job = other.get(ConversionJob, job_id)
            assert other_job.status == ConversionJobStatus.QUEUED
            assert _all_conversion_events(other) == []

        session.rollback()

    # Rolled back: the claim left no durable effect.
    with Session(engine) as verify:
        rolled_back = verify.get(ConversionJob, job_id)
        assert rolled_back.status == ConversionJobStatus.QUEUED
        assert rolled_back.attempts == 0
        assert _all_conversion_events(verify) == []


def test_recover_stale_recovers_only_stranded_running_job(
    db_repository: ConversionStageHandler, engine, source: Source
) -> None:
    """``recover_stale`` reclaims only RUNNING jobs stranded past the stale threshold.

    Seeds a RUNNING job started well past the threshold and a fresh RUNNING job.
    Only the stranded one is transitioned to FAILED_RETRYABLE with a
    ``recovered_stale`` event; the fresh one is untouched.
    """
    now = _utcnow()
    stranded_id = _seed_job(
        engine,
        source,
        status=ConversionJobStatus.RUNNING,
        queued_at=now - datetime.timedelta(hours=2),
        started_at=now - datetime.timedelta(minutes=_STALE_MINUTES + 5),
        attempts=1,
    )
    fresh_id = _seed_job(
        engine,
        source,
        status=ConversionJobStatus.RUNNING,
        queued_at=now - datetime.timedelta(minutes=1),
        started_at=now,
        attempts=1,
    )

    with Session(engine) as session:
        recovered = db_repository.recover_stale(session)
        session.commit()

    assert recovered == [stranded_id]
    with Session(engine) as verify:
        stranded = verify.get(ConversionJob, stranded_id)
        assert stranded.status == ConversionJobStatus.FAILED_RETRYABLE
        # Recovery does not increment attempts (caller-stated attempt convention).
        assert stranded.attempts == 1
        assert stranded.error_code == "worker_stale_running"

        fresh = verify.get(ConversionJob, fresh_id)
        assert fresh.status == ConversionJobStatus.RUNNING

        events = events_for_job(verify, stranded_id)
        assert len(events) == 1
        assert events[0].kind == ConversionEventKind.RECOVERED_STALE
        assert events[0].from_status == ConversionJobStatus.RUNNING
        assert events[0].to_status == ConversionJobStatus.FAILED_RETRYABLE
        assert events[0].attempt == 1
        payload = parse_payload_lenient(events[0].payload_json)
        assert payload.stale_after_minutes == _STALE_MINUTES

        # The fresh running job emitted no recovery event.
        fresh_events = events_for_job(verify, fresh_id)
        assert fresh_events == []


def test_recover_stale_returns_empty_and_does_not_commit(
    db_repository: ConversionStageHandler, engine, source: Source
) -> None:
    """``recover_stale`` returns ``[]`` with nothing stranded and never commits.

    With only a fresh RUNNING job present, no recovery occurs; and the staged
    writes (none here) plus the caller-owned-commit contract mean a separate
    session sees no change until the caller commits.
    """
    now = _utcnow()
    fresh_id = _seed_job(
        engine,
        source,
        status=ConversionJobStatus.RUNNING,
        queued_at=now,
        started_at=now,
        attempts=1,
    )

    with Session(engine) as session:
        recovered = db_repository.recover_stale(session)
        # Uncommitted state is invisible to a separate connection.
        with Session(engine) as other:
            assert other.get(ConversionJob, fresh_id).status == ConversionJobStatus.RUNNING
        session.commit()

    assert recovered == []
    with Session(engine) as verify:
        assert verify.get(ConversionJob, fresh_id).status == ConversionJobStatus.RUNNING
        assert _all_conversion_events(verify) == []


# ---------------------------------------------------------------------------
# execute / cancel / cleanup
#
# ``execute`` runs the conversion unit-of-work middle by reusing the existing
# helpers, with the subprocess + upload boundaries faked. These tests assert the
# invariants ``execute`` must hold: no attempt re-increment
# (claim already did it), the conversion-private UPLOAD_PENDING progress marker
# is written, a forced failure raises the right exception for ``map_result``,
# and ``cancel`` terminates the tracked subprocess group graceful-before-forceful
# (no-op when nothing is running).
# ---------------------------------------------------------------------------


class _FakeProcess:
    """Minimal ``mp.Process`` stand-in for the subprocess boundary.

    Carries a ``pid``/``exitcode`` so ``execute``'s post-supervision checks read
    a finished, successful process by default, and an ``is_alive`` so any code
    that probes liveness sees it terminated.
    """

    def __init__(self, *, pid: int = 4321, exitcode: int | None = 0) -> None:
        self.pid = pid
        self.exitcode = exitcode

    def is_alive(self) -> bool:
        return False


def _write_metadata(workspace, *, markdown_hash: str = "abc123") -> None:
    """Write a valid ``metadata.json`` into ``workspace`` for the success path."""
    meta = SubprocessMetadata(
        pipeline_name="html",
        terminal_ref={"kind": "karakeep_bookmark", "bookmark_id": "x"},
        content_type="html",
        markdown_filename="content.md",
        figure_files=[],
        markdown_hash_xx64=markdown_hash,
        docling_version="0.0.0-test",
        config_snapshot={"converter_name": "docling"},
        fetched_at="2026-01-01T00:00:00+00:00",
        source_meta={"source_url": "https://example.com"},
        document_title="Doc",
        source_title="Doc",
    )
    from aizk.conversion.utilities.paths import metadata_path

    metadata_path(workspace).write_text(meta.model_dump_json())


@pytest.fixture()
def exec_db_path(tmp_path, monkeypatch):
    """Return the file-SQLite URL ``execute`` and the test seed both open.

    ``execute`` resolves its engine through ``get_engine(DatabaseConfig().database_url)``,
    so this points ``AIZK_DATABASE_URL`` at the same file the seed opens; the seed
    and the code under test then share one database. A file-backed SQLite makes the
    seed durable across the separate connections ``execute`` opens internally.
    """
    url = f"sqlite:///{tmp_path}/exec.db"
    monkeypatch.setenv("AIZK_DATABASE_URL", url)
    return url


@pytest.fixture()
def exec_engine(exec_db_path):
    """Provide the shared engine (via ``get_engine``) with the conversion schema.

    Built through ``get_engine`` — not ``create_engine`` — so the engine the
    test seeds with is the same cached engine ``execute`` resolves. The cache
    entry is evicted at teardown so the temp DB does not leak into later tests.
    """
    from aizk.db.engine import _ENGINE_CACHE, get_engine

    eng = get_engine(exec_db_path)
    SQLModel.metadata.create_all(eng)
    try:
        yield eng
    finally:
        eng.dispose()
        _ENGINE_CACHE.pop(exec_db_path, None)


@pytest.fixture()
def exec_config(exec_db_path) -> ConversionConfig:
    """Hermetic config pointing at the shared execute DB with zero retry delay."""
    return ConversionConfig(
        _env_file=None,
        retry_max_attempts=2,
        retry_base_delay_seconds=0,
    )


@pytest.fixture()
def exec_runtime():
    """A fake ``WorkerRuntime`` whose converter never requires the GPU guard."""
    from contextlib import nullcontext
    from unittest.mock import MagicMock

    runtime = MagicMock()
    runtime.resource_guard = nullcontext()
    runtime.capabilities.converter_requires_gpu.return_value = False
    return runtime


@pytest.fixture()
def exec_source(exec_engine) -> Source:
    """Persist a Source row the seeded RUNNING job references via ``aizk_uuid``."""
    with Session(exec_engine) as session:
        return _create_source(session)


def _seed_running_job(exec_engine, source: Source, *, attempts: int = 1) -> int:
    """Seed an already-claimed RUNNING job (as ``claim_next`` would leave it).

    ``execute`` is entered AFTER the claim, so the job arrives RUNNING with
    ``attempts`` already post-incremented and a valid ``source_ref`` set.
    """
    with Session(exec_engine) as session:
        job = ConversionJob(
            aizk_uuid=source.aizk_uuid,
            owner_id="self",
            title="exec test",
            payload_version=1,
            status=ConversionJobStatus.RUNNING,
            attempts=attempts,
            idempotency_key=uuid4().hex,
            source_ref=_KARAKEEP_BOOKMARK_REF,
            started_at=_utcnow(),
        )
        session.add(job)
        session.commit()
        session.refresh(job)
        return job.id


def _patch_supervise(
    monkeypatch: pytest.MonkeyPatch,
    *,
    result: SupervisionResult,
    process: _FakeProcess,
    write_metadata: bool = False,
) -> dict:
    """Patch ``handler._spawn_and_supervise`` to a controllable fake.

    The fake invokes ``on_spawn`` with ``process`` (so per-handle tracking is
    exercised), optionally writes a valid ``metadata.json`` into the workspace,
    and returns ``(process, result, None)``. Records the workspace it saw.
    """
    captured: dict = {}

    def _fake(**kwargs):
        captured["workspace"] = kwargs["workspace"]
        on_spawn = kwargs.get("on_spawn")
        if on_spawn is not None:
            on_spawn(process)
        if write_metadata:
            _write_metadata(kwargs["workspace"])
        return process, result, None

    monkeypatch.setattr(repository_mod, "_spawn_and_supervise", _fake)
    return captured


def test_execute_does_not_reincrement_attempts(
    monkeypatch: pytest.MonkeyPatch,
    exec_engine,
    exec_config: ConversionConfig,
    exec_runtime,
    exec_source: Source,
) -> None:
    """``execute`` must NOT re-increment ``attempts`` — the claim already did.

    The job arrives RUNNING with ``attempts`` already post-incremented by
    ``claim_next`` (the claim does the RUNNING transition + attempt increment);
    ``execute`` must never re-do it. A successful run (subprocess + upload faked)
    leaves ``attempts`` unchanged and emits no second ``claimed`` event.
    """
    job_id = _seed_running_job(exec_engine, exec_source, attempts=2)
    _patch_supervise(
        monkeypatch,
        result=SupervisionResult("converting", None, False, False),
        process=_FakeProcess(exitcode=0),
        write_metadata=True,
    )
    # Stop after the UPLOAD_PENDING marker: a ``None`` plan skips the upload loop
    # (the SUCCEEDED write lives in ``_execute_upload``, out of scope for C1).
    monkeypatch.setattr(repository_mod, "_prepare_upload", lambda *_a, **_k: None)
    monkeypatch.setattr(repository_mod, "_is_job_cancelled", lambda *_a, **_k: False)

    handler = ConversionStageHandler(exec_config, runtime=exec_runtime)
    handler.execute(job_id)

    with Session(exec_engine) as verify:
        job = verify.get(ConversionJob, job_id)
        assert job.attempts == 2, "execute must not re-increment attempts (claim owns it)"
        claimed_events = _events_for_job_kind(verify, job_id, ConversionEventKind.CLAIMED)
        assert claimed_events == [], "execute must not re-emit a CLAIMED event"


def test_execute_writes_upload_pending_with_content_hash(
    monkeypatch: pytest.MonkeyPatch,
    exec_engine,
    exec_config: ConversionConfig,
    exec_runtime,
    exec_source: Source,
) -> None:
    """``execute`` writes the conversion-private RUNNING -> UPLOAD_PENDING marker.

    ``execute`` gets no runner session, so it writes the UPLOAD_PENDING
    transition through conversion's own short-lived session. The transition and
    its event carry the subprocess content hash. The runner only ever sees
    generic ``running``; UPLOAD_PENDING is a conversion-internal progress entry.
    """
    job_id = _seed_running_job(exec_engine, exec_source, attempts=1)
    _patch_supervise(
        monkeypatch,
        result=SupervisionResult("converting", None, False, False),
        process=_FakeProcess(exitcode=0),
        write_metadata=True,
    )
    monkeypatch.setattr(repository_mod, "_prepare_upload", lambda *_a, **_k: None)
    monkeypatch.setattr(repository_mod, "_is_job_cancelled", lambda *_a, **_k: False)

    handler = ConversionStageHandler(exec_config, runtime=exec_runtime)
    handler.execute(job_id)

    with Session(exec_engine) as verify:
        job = verify.get(ConversionJob, job_id)
        assert job.status == ConversionJobStatus.UPLOAD_PENDING
        events = _events_for_job_kind(verify, job_id, ConversionEventKind.UPLOAD_PENDING)
        assert len(events) == 1
        assert events[0].from_status == ConversionJobStatus.RUNNING
        assert events[0].to_status == ConversionJobStatus.UPLOAD_PENDING
        assert events[0].attempt == 1
        payload = parse_payload_lenient(events[0].payload_json)
        assert payload.content_hash == "abc123"


def test_execute_removes_workspace_on_success(
    monkeypatch: pytest.MonkeyPatch,
    exec_engine,
    exec_config: ConversionConfig,
    exec_runtime,
    exec_source: Source,
) -> None:
    """The temporary workspace is gone once ``execute`` completes successfully.

    Conversion scopes the workspace to ``execute`` (a ``TemporaryDirectory``
    spanning the unit-of-work), so it is removed on the succeeded outcome —
    satisfying "clean up temporary workspace on all job outcomes".
    """
    job_id = _seed_running_job(exec_engine, exec_source, attempts=1)
    captured = _patch_supervise(
        monkeypatch,
        result=SupervisionResult("converting", None, False, False),
        process=_FakeProcess(exitcode=0),
        write_metadata=True,
    )
    monkeypatch.setattr(repository_mod, "_prepare_upload", lambda *_a, **_k: None)
    monkeypatch.setattr(repository_mod, "_is_job_cancelled", lambda *_a, **_k: False)

    handler = ConversionStageHandler(exec_config, runtime=exec_runtime)
    handler.execute(job_id)

    assert "workspace" in captured
    assert not captured["workspace"].exists(), "workspace must be removed after a successful execute"


def test_execute_removes_workspace_on_failure(
    monkeypatch: pytest.MonkeyPatch,
    exec_engine,
    exec_config: ConversionConfig,
    exec_runtime,
    exec_source: Source,
) -> None:
    """The temporary workspace is gone even when ``execute`` raises.

    A forced subprocess failure unwinds the ``TemporaryDirectory`` context, so no
    workspace leaks on the failed outcome.
    """
    job_id = _seed_running_job(exec_engine, exec_source, attempts=1)
    captured = _patch_supervise(
        monkeypatch,
        result=SupervisionResult(
            "converting",
            {"event": "failed", "message": "boom", "error_code": "x", "retryable": "false"},
            False,
            False,
        ),
        process=_FakeProcess(exitcode=1),
        write_metadata=False,
    )
    monkeypatch.setattr(repository_mod, "_is_job_cancelled", lambda *_a, **_k: False)

    handler = ConversionStageHandler(exec_config, runtime=exec_runtime)
    with pytest.raises(ReportedChildError):
        handler.execute(job_id)

    assert "workspace" in captured
    assert not captured["workspace"].exists(), "workspace must be removed after a failed execute"


@pytest.mark.parametrize(
    ("result", "process", "expected_exc"),
    [
        pytest.param(
            SupervisionResult("converting", None, False, True),
            _FakeProcess(exitcode=0),
            ConversionTimeoutError,
            id="timed-out",
        ),
        pytest.param(
            SupervisionResult("converting", None, True, False),
            _FakeProcess(exitcode=0),
            ConversionCancelledError,
            id="cancelled",
        ),
        pytest.param(
            SupervisionResult(
                "converting",
                {"event": "failed", "message": "boom", "error_code": "x", "retryable": "false"},
                False,
                False,
            ),
            _FakeProcess(exitcode=1),
            ReportedChildError,
            id="reported-child-error",
        ),
        pytest.param(
            SupervisionResult("converting", None, False, False),
            _FakeProcess(exitcode=1),
            ConversionSubprocessError,
            id="subprocess-nonzero-exit",
        ),
    ],
)
def test_execute_raises_matching_exception_on_failure(
    monkeypatch: pytest.MonkeyPatch,
    exec_engine,
    exec_config: ConversionConfig,
    exec_runtime,
    exec_source: Source,
    result: SupervisionResult,
    process: _FakeProcess,
    expected_exc: type[BaseException],
) -> None:
    """A forced subprocess failure raises the existing conversion exception.

    ``execute`` does NOT write a terminal status; it raises so ``map_result``
    classifies the outcome. Each supervision outcome maps to its exception type.
    """
    job_id = _seed_running_job(exec_engine, exec_source, attempts=1)
    _patch_supervise(monkeypatch, result=result, process=process, write_metadata=False)
    monkeypatch.setattr(repository_mod, "_is_job_cancelled", lambda *_a, **_k: False)

    handler = ConversionStageHandler(exec_config, runtime=exec_runtime)
    with pytest.raises(expected_exc):
        handler.execute(job_id)


def test_execute_propagates_upload_failure_after_retries(
    monkeypatch: pytest.MonkeyPatch,
    exec_engine,
    exec_config: ConversionConfig,
    exec_runtime,
    exec_source: Source,
) -> None:
    """An upload that fails every attempt raises the last error for ``map_result``.

    The retry loop wraps only the idempotent S3 PUTs; after exhausting
    ``retry_max_attempts`` the last exception propagates so ``map_result``
    classifies it. UPLOAD_PENDING is still written first (the marker precedes the
    upload).
    """
    job_id = _seed_running_job(exec_engine, exec_source, attempts=1)
    _patch_supervise(
        monkeypatch,
        result=SupervisionResult("converting", None, False, False),
        process=_FakeProcess(exitcode=0),
        write_metadata=True,
    )
    monkeypatch.setattr(repository_mod, "_is_job_cancelled", lambda *_a, **_k: False)

    attempts_seen = {"count": 0}

    def _failing_execute_upload(*_a, **_k):
        attempts_seen["count"] += 1
        raise ConversionSubprocessError("transient s3 failure")

    monkeypatch.setattr(repository_mod, "_prepare_upload", lambda *_a, **_k: object())
    monkeypatch.setattr(repository_mod, "_execute_upload", _failing_execute_upload)

    handler = ConversionStageHandler(exec_config, runtime=exec_runtime)
    with pytest.raises(ConversionSubprocessError):
        handler.execute(job_id)

    assert attempts_seen["count"] == exec_config.retry_max_attempts, "retry loop exhausts all attempts"
    with Session(exec_engine) as verify:
        job = verify.get(ConversionJob, job_id)
        # UPLOAD_PENDING was written before upload; execute never writes a
        # terminal (FAILED) status — that is finalize's job.
        assert job.status == ConversionJobStatus.UPLOAD_PENDING


def test_execute_upload_phase_rebounds_by_deadline(
    monkeypatch: pytest.MonkeyPatch,
    exec_engine,
    exec_source: Source,
    exec_db_path,
    exec_runtime,
) -> None:
    """The upload phase raises ``ConversionTimeoutError`` once past the deadline.

    The runner-driven ``cancel`` cannot interrupt the in-process upload phase
    after the subprocess has exited, so ``execute`` re-binds it by the same
    ``worker_job_timeout_seconds`` deadline. With a clock advanced past the
    deadline before upload begins, the pre-upload deadline check raises and
    ``_execute_upload`` is never called.
    """
    config = ConversionConfig(
        _env_file=None,
        worker_job_timeout_seconds=10.0,
        retry_max_attempts=2,
        retry_base_delay_seconds=0,
    )
    job_id = _seed_running_job(exec_engine, exec_source, attempts=1)
    _patch_supervise(
        monkeypatch,
        result=SupervisionResult("converting", None, False, False),
        process=_FakeProcess(exitcode=0),
        write_metadata=True,
    )
    monkeypatch.setattr(repository_mod, "_is_job_cancelled", lambda *_a, **_k: False)

    upload_calls = {"count": 0}

    def _should_not_upload(*_a, **_k):
        upload_calls["count"] += 1
        raise AssertionError("upload must not run once the deadline is exceeded")

    monkeypatch.setattr(repository_mod, "_prepare_upload", lambda *_a, **_k: object())
    monkeypatch.setattr(repository_mod, "_execute_upload", _should_not_upload)

    # Deterministic clock: the first call computes the deadline (t=0 -> deadline
    # at t=10); every later call (the upload-phase deadline checks) returns a
    # value past it, so the pre-upload check raises before any attempt runs.
    clock = {"now": 0.0}

    def _fake_monotonic() -> float:
        value = clock["now"]
        clock["now"] = 1000.0  # all subsequent reads are well past the deadline
        return value

    monkeypatch.setattr(repository_mod.time, "monotonic", _fake_monotonic)

    handler = ConversionStageHandler(config, runtime=exec_runtime)
    with pytest.raises(ConversionTimeoutError):
        handler.execute(job_id)

    assert upload_calls["count"] == 0, "deadline check raises before any upload attempt"
    with Session(exec_engine) as verify:
        # UPLOAD_PENDING was written before the deadline check; execute never
        # writes a terminal status (that is finalize's job).
        assert verify.get(ConversionJob, job_id).status == ConversionJobStatus.UPLOAD_PENDING


def test_execute_missing_source_ref_raises_job_data_integrity(
    monkeypatch: pytest.MonkeyPatch,
    exec_engine,
    exec_config: ConversionConfig,
    exec_runtime,
    exec_source: Source,
) -> None:
    """Preflight surfaces a missing ``source_ref`` as ``JobDataIntegrityError``.

    The preflight source-ref fetch is reused from the orchestrator; a job with
    no ``source_ref`` raises the existing permanent-failure exception before any
    subprocess spawns.
    """
    with Session(exec_engine) as session:
        job = ConversionJob(
            aizk_uuid=exec_source.aizk_uuid,
            owner_id="self",
            title="no ref",
            payload_version=1,
            status=ConversionJobStatus.RUNNING,
            attempts=1,
            idempotency_key=uuid4().hex,
            source_ref=None,
            started_at=_utcnow(),
        )
        session.add(job)
        session.commit()
        session.refresh(job)
        job_id = job.id

    spawn_calls = {"count": 0}

    def _should_not_spawn(**_kwargs):
        spawn_calls["count"] += 1
        raise AssertionError("subprocess must not spawn when preflight fails")

    monkeypatch.setattr(repository_mod, "_spawn_and_supervise", _should_not_spawn)

    handler = ConversionStageHandler(exec_config, runtime=exec_runtime)
    with pytest.raises(JobDataIntegrityError):
        handler.execute(job_id)
    assert spawn_calls["count"] == 0


def test_cancel_signals_terminate_event_without_joining(
    exec_config: ConversionConfig,
) -> None:
    """``cancel`` only *signals* (sets the per-handle event); it never joins.

    Single-owner termination: ``cancel`` sets the terminate-event ``execute``
    registered alongside the tracked process and returns promptly. It must NOT
    call ``terminate``/``join`` on the Process — the supervision loop (the single
    owner of the ``mp.Process``) performs the actual terminate/join when it
    observes the event. The tracked process stub fails the test if ``cancel``
    touches its lifecycle methods.
    """
    import threading

    join_calls: list = []
    terminate_calls: list = []

    class _GuardProcess(_FakeProcess):
        def join(self, *_a, **_k):
            join_calls.append(True)

        def terminate(self):
            terminate_calls.append(True)

    handler = ConversionStageHandler(exec_config)
    tracked = _GuardProcess(pid=9999)
    event = threading.Event()
    # Simulate ``execute`` having registered a live subprocess + terminate-event.
    handler._processes[7] = tracked
    handler._terminate_events[7] = event

    handler.cancel(7)

    assert event.is_set(), "cancel signals the per-handle terminate-event"
    assert join_calls == [], "cancel must NOT join the Process"
    assert terminate_calls == [], "cancel must NOT terminate the Process itself"


def test_cancel_is_noop_when_nothing_tracked(
    exec_config: ConversionConfig,
) -> None:
    """``cancel`` is a no-op when no subprocess is running for the handle.

    The runner drives ``cancel`` on timeout / cooperative-cancel / drain; a
    handle with no in-flight subprocess (already finished, or never started)
    must not raise and has no event to set.
    """
    handler = ConversionStageHandler(exec_config)
    handler.cancel(404)  # nothing tracked for handle 404 — must not raise


def test_cleanup_drops_tracked_handle(exec_config: ConversionConfig) -> None:
    """``cleanup`` releases the per-handle tracking entries and is idempotent.

    The runner calls ``cleanup`` on every terminal outcome; it must drop any
    dangling tracking reference (both the process and the terminate-event) and
    tolerate being called when nothing is tracked.
    """
    import threading

    handler = ConversionStageHandler(exec_config)
    handler._processes[3] = _FakeProcess()
    handler._terminate_events[3] = threading.Event()

    handler.cleanup(3)
    assert 3 not in handler._processes
    assert 3 not in handler._terminate_events, "cleanup drops the terminate-event too"

    # Idempotent: a second cleanup (or one for an unknown handle) does not raise.
    handler.cleanup(3)
    handler.cleanup(999)


def test_execute_clears_tracking_after_supervision_returns(
    monkeypatch: pytest.MonkeyPatch,
    exec_engine,
    exec_config: ConversionConfig,
    exec_runtime,
    exec_source: Source,
) -> None:
    """After supervision returns, the per-handle tracking entries are cleared.

    The subprocess is dead once supervision returns (joined by the supervision
    loop, or terminated after a ``cancel`` signal), so a later ``cancel`` for the
    handle must be a no-op rather than signal a dead pid. ``execute`` clears both
    the process and the terminate-event in a ``finally`` regardless of outcome.
    """
    job_id = _seed_running_job(exec_engine, exec_source, attempts=1)
    process = _FakeProcess(exitcode=0)
    _patch_supervise(
        monkeypatch,
        result=SupervisionResult("converting", None, False, False),
        process=process,
        write_metadata=True,
    )
    monkeypatch.setattr(repository_mod, "_prepare_upload", lambda *_a, **_k: None)
    monkeypatch.setattr(repository_mod, "_is_job_cancelled", lambda *_a, **_k: False)

    handler = ConversionStageHandler(exec_config, runtime=exec_runtime)
    handler.execute(job_id)

    assert job_id not in handler._processes, "process tracking cleared once supervision returns"
    assert job_id not in handler._terminate_events, "terminate-event cleared once supervision returns"


# ---------------------------------------------------------------------------
# finalize — the terminal-status write
#
# ``finalize`` runs inside the runner-owned ``BEGIN IMMEDIATE`` session and must
# NOT commit. It writes the failed-terminal statuses:
# FAILED_RETRYABLE (with the exact backoff) / FAILED_PERM (with finished_at),
# TIMED_OUT reusing the retryable path, CANCELLED no-op-when-already-cancelled,
# the egress scrub, and SUCCEEDED as an idempotent no-op (the success write lives
# in ``_execute_upload``). The error fields are bridged from the per-handle stash
# ``execute`` records on failure.
# ---------------------------------------------------------------------------


def _seed_job_at(exec_engine, source: Source, *, status: ConversionJobStatus, attempts: int = 2) -> int:
    """Seed one job at ``status`` with ``attempts`` for finalize tests.

    ``finalize`` is entered after ``execute`` ran; the realistic pre-finalize
    statuses are UPLOAD_PENDING (a failure after the marker) or RUNNING (a cancel
    before the marker). The seeded ``attempts`` is what the claim left on the row;
    ``finalize`` must read it back from the session for the event.
    """
    with Session(exec_engine) as session:
        job = ConversionJob(
            aizk_uuid=source.aizk_uuid,
            owner_id="self",
            title="finalize test",
            payload_version=1,
            status=status,
            attempts=attempts,
            idempotency_key=uuid4().hex,
            source_ref=_KARAKEEP_BOOKMARK_REF,
            started_at=_utcnow(),
        )
        session.add(job)
        session.commit()
        session.refresh(job)
        return job.id


def _failed_events(verify: Session, job_id: int) -> list[PipelineEvent]:
    """Return the FAILED events recorded for ``job_id`` (helper for assertions)."""
    return _events_for_job_kind(verify, job_id, ConversionEventKind.FAILED)


def test_finalize_failed_retryable_sets_backoff_error_fields_and_event(
    exec_config: ConversionConfig, exec_engine, exec_source: Source
) -> None:
    """A retryable FAILED outcome → FAILED_RETRYABLE with backoff + error fields + event.

    The retryable branch sets ``earliest_next_attempt_at =
    now + retry_base_delay_seconds * 2**attempts``, the ``error_*`` fields,
    ``last_error_at``, no ``finished_at``, and a ``failed`` event whose payload
    carries the same scrubbed values. The event ``attempt`` is the row's value.
    """
    config = ConversionConfig(
        _env_file=None,
        retry_base_delay_seconds=5,
    )
    job_id = _seed_job_at(exec_engine, exec_source, status=ConversionJobStatus.UPLOAD_PENDING, attempts=2)
    handler = ConversionStageHandler(config)
    # Bridge: ``execute`` stashed the (scrubbed) details for this handle on failure.
    handler._error_details[job_id] = JobErrorDetails(
        error_code="conversion_subprocess_failed",
        error_message="boom",
        error_detail="trace",
        retryable=True,
        last_phase="converting",
    )

    with Session(exec_engine) as session:
        handler.finalize(session, job_id, TerminalOutcome(WorkUnitStatus.FAILED, RetryClass.RETRYABLE))
        session.commit()

    with Session(exec_engine) as verify:
        job = verify.get(ConversionJob, job_id)
        assert job.status == ConversionJobStatus.FAILED_RETRYABLE
        assert job.finished_at is None, "retryable failures are not finished"
        assert job.error_code == "conversion_subprocess_failed"
        assert job.error_message == "boom"
        assert job.error_detail == "trace"
        assert job.last_error_at is not None
        # Backoff = base * 2**attempts = 5 * 2**2 = 20 seconds past ``last_error_at``
        # (both are stamped from the same ``now`` in finalize, so compare them
        # directly — deterministic and tz-storage-agnostic).
        assert job.earliest_next_attempt_at is not None
        delta = (job.earliest_next_attempt_at - job.last_error_at).total_seconds()
        assert delta == 20, f"backoff = 5 * 2**2 = 20s exactly, got {delta}s"

        events = _failed_events(verify, job_id)
        assert len(events) == 1
        assert events[0].from_status == ConversionJobStatus.UPLOAD_PENDING
        assert events[0].to_status == ConversionJobStatus.FAILED_RETRYABLE
        assert events[0].attempt == 2, "event attempt re-read from the row, not a stale snapshot"
        payload = parse_payload_lenient(events[0].payload_json)
        assert payload.error_code == "conversion_subprocess_failed"
        assert payload.error_message == "boom"
        assert payload.error_detail == "trace"
        assert payload.retryable is True
        assert payload.last_phase == "converting"

    # The bridge SURVIVES finalize: the runner re-invokes finalize on a
    # finalize-time DB error (slot retained), so clearing here would lose the real
    # scrubbed cause. cleanup() is the sole clear point and runs once, after commit.
    assert handler._error_details.get(job_id) is not None, "finalize must not clear the per-handle stash"
    handler.cleanup(job_id)
    assert job_id not in handler._error_details, "cleanup releases the stash after the durable commit"


def test_finalize_bridge_survives_a_rolled_back_retry(
    exec_config: ConversionConfig, exec_engine, exec_source: Source
) -> None:
    """A finalize that fails (rolls back) keeps the error-details bridge for the retry.

    The shared runner retains the slot and re-invokes finalize on a finalize-time DB
    error; the per-handle bridge must persist across that retry (cleanup clears it
    only after the durable commit). Otherwise the retry records the unknown-error
    fallback instead of the real scrubbed cause.
    """
    job_id = _seed_job_at(exec_engine, exec_source, status=ConversionJobStatus.UPLOAD_PENDING, attempts=2)
    handler = ConversionStageHandler(exec_config)
    handler._error_details[job_id] = JobErrorDetails(
        error_code="conversion_subprocess_failed",
        error_message="boom",
        error_detail="trace",
        retryable=True,
        last_phase="converting",
    )

    # First finalize fails durably (simulate the runner's finalize DB error): roll back.
    with Session(exec_engine) as session:
        handler.finalize(session, job_id, TerminalOutcome(WorkUnitStatus.FAILED, RetryClass.RETRYABLE))
        session.rollback()

    # The runner retries finalize on the next reap (slot retained, no cleanup yet).
    with Session(exec_engine) as session:
        handler.finalize(session, job_id, TerminalOutcome(WorkUnitStatus.FAILED, RetryClass.RETRYABLE))
        session.commit()

    with Session(exec_engine) as verify:
        job = verify.get(ConversionJob, job_id)
        assert job.status == ConversionJobStatus.FAILED_RETRYABLE
        assert job.error_code == "conversion_subprocess_failed"
        assert job.error_message == "boom", "the retry recorded the real scrubbed cause, not the fallback"


def test_finalize_failed_permanent_sets_finished_at_and_no_backoff(
    exec_config: ConversionConfig, exec_engine, exec_source: Source
) -> None:
    """A permanent FAILED outcome → FAILED_PERM with finished_at, no backoff.

    The permanent branch sets ``finished_at = now``,
    ``earliest_next_attempt_at = None``, the error fields, and a ``failed`` event
    with ``retryable=False``.
    """
    job_id = _seed_job_at(exec_engine, exec_source, status=ConversionJobStatus.UPLOAD_PENDING, attempts=3)
    handler = ConversionStageHandler(exec_config)
    handler._error_details[job_id] = JobErrorDetails(
        error_code="subprocess_metadata_invalid",
        error_message="schema drift",
        error_detail=None,
        retryable=False,
        last_phase=None,
    )

    with Session(exec_engine) as session:
        handler.finalize(session, job_id, TerminalOutcome(WorkUnitStatus.FAILED, RetryClass.PERMANENT))
        session.commit()

    with Session(exec_engine) as verify:
        job = verify.get(ConversionJob, job_id)
        assert job.status == ConversionJobStatus.FAILED_PERM
        assert job.finished_at is not None, "permanent failures set finished_at"
        assert job.earliest_next_attempt_at is None, "permanent failures clear the retry-wait"
        assert job.error_code == "subprocess_metadata_invalid"

        events = _failed_events(verify, job_id)
        assert len(events) == 1
        assert events[0].to_status == ConversionJobStatus.FAILED_PERM
        assert events[0].attempt == 3
        payload = parse_payload_lenient(events[0].payload_json)
        assert payload.retryable is False


def test_finalize_timed_out_routes_to_failed_retryable(
    exec_config: ConversionConfig, exec_engine, exec_source: Source
) -> None:
    """TIMED_OUT reuses the retryable path → FAILED_RETRYABLE with backoff.

    A timeout is treated as retryable. The runner resolves the outcome as TIMED_OUT
    from its slot state; ``finalize`` routes it to FAILED_RETRYABLE using the
    stashed timeout details and the same backoff.
    """
    config = ConversionConfig(
        _env_file=None,
        retry_base_delay_seconds=1,
    )
    job_id = _seed_job_at(exec_engine, exec_source, status=ConversionJobStatus.UPLOAD_PENDING, attempts=1)
    handler = ConversionStageHandler(config)
    # ``execute`` raised ConversionTimeoutError; its scrubbed details are stashed.
    handler._error_details[job_id] = JobErrorDetails(
        error_code="conversion_timeout",
        error_message="Job timed out during converting",
        error_detail=None,
        retryable=True,
        last_phase="converting",
    )

    with Session(exec_engine) as session:
        handler.finalize(session, job_id, TerminalOutcome(WorkUnitStatus.TIMED_OUT))
        session.commit()

    with Session(exec_engine) as verify:
        job = verify.get(ConversionJob, job_id)
        assert job.status == ConversionJobStatus.FAILED_RETRYABLE, "timeout reuses FAILED_RETRYABLE"
        assert job.earliest_next_attempt_at is not None
        assert job.error_code == "conversion_timeout"

        events = _failed_events(verify, job_id)
        assert len(events) == 1
        assert events[0].to_status == ConversionJobStatus.FAILED_RETRYABLE
        payload = parse_payload_lenient(events[0].payload_json)
        assert payload.retryable is True
        assert payload.last_phase == "converting"


def test_finalize_timed_out_synthesizes_details_when_stash_absent(
    exec_config: ConversionConfig, exec_engine, exec_source: Source
) -> None:
    """TIMED_OUT with no stashed details still records a timeout cause.

    Defensive fallback: when the runner resolved TIMED_OUT but no exception was
    stashed, ``finalize`` synthesizes a ``conversion_timeout`` retryable detail so
    the row still records the cause as FAILED_RETRYABLE.
    """
    job_id = _seed_job_at(exec_engine, exec_source, status=ConversionJobStatus.UPLOAD_PENDING, attempts=1)
    handler = ConversionStageHandler(exec_config)
    # No stash for this handle.

    with Session(exec_engine) as session:
        handler.finalize(session, job_id, TerminalOutcome(WorkUnitStatus.TIMED_OUT))
        session.commit()

    with Session(exec_engine) as verify:
        job = verify.get(ConversionJob, job_id)
        assert job.status == ConversionJobStatus.FAILED_RETRYABLE
        assert job.error_code == "conversion_timeout"


def test_finalize_cancelled_is_noop_when_already_cancelled(
    exec_config: ConversionConfig, exec_engine, exec_source: Source
) -> None:
    """A CANCELLED outcome on an already-CANCELLED job is a no-op.

    Legacy left API-cancelled jobs as-is. ``finalize`` must not re-transition or
    emit a second ``cancelled`` event when the DB already shows CANCELLED.
    """
    job_id = _seed_job_at(exec_engine, exec_source, status=ConversionJobStatus.CANCELLED, attempts=1)

    handler = ConversionStageHandler(exec_config)
    with Session(exec_engine) as session:
        handler.finalize(session, job_id, TerminalOutcome(WorkUnitStatus.CANCELLED))
        session.commit()

    with Session(exec_engine) as verify:
        job = verify.get(ConversionJob, job_id)
        assert job.status == ConversionJobStatus.CANCELLED
        cancelled_events = _events_for_job_kind(verify, job_id, ConversionEventKind.CANCELLED)
        assert cancelled_events == [], "no second cancelled event for an already-cancelled job"


def test_finalize_cancelled_writes_transition_when_runner_drove_cancel(
    exec_config: ConversionConfig, exec_engine, exec_source: Source
) -> None:
    """A runner-driven cancel where the DB is not yet CANCELLED writes CANCELLED.

    The runner can resolve CANCELLED from its slot state (cooperative cancel /
    drain survivor) while the job is still RUNNING in the DB. ``finalize`` then
    writes the CANCELLED terminal transition with a ``cancelled`` event.
    """
    job_id = _seed_job_at(exec_engine, exec_source, status=ConversionJobStatus.RUNNING, attempts=2)

    handler = ConversionStageHandler(exec_config)
    with Session(exec_engine) as session:
        handler.finalize(session, job_id, TerminalOutcome(WorkUnitStatus.CANCELLED))
        session.commit()

    with Session(exec_engine) as verify:
        job = verify.get(ConversionJob, job_id)
        assert job.status == ConversionJobStatus.CANCELLED
        assert job.finished_at is not None
        events = _events_for_job_kind(verify, job_id, ConversionEventKind.CANCELLED)
        assert len(events) == 1
        assert events[0].from_status == ConversionJobStatus.RUNNING
        assert events[0].to_status == ConversionJobStatus.CANCELLED
        assert events[0].attempt == 2, "cancelled event attempt re-read from the row"


def test_finalize_egress_error_message_is_scrubbed(
    exec_config: ConversionConfig, exec_engine, exec_source: Source
) -> None:
    """An egress-policy failure persists the bare code, never the rejected URL.

    Security: the bridge scrubs egress-policy errors at stash time (the rejected
    URL/IP never enters the stash), so ``finalize`` writes ``error_message`` =
    the bare code and ``error_detail`` = ``None`` — the destination is absent from
    every persisted field and from the event payload.
    """
    rejected = "https://attacker.internal/secret-exfil-target"
    job_id = _seed_job_at(exec_engine, exec_source, status=ConversionJobStatus.UPLOAD_PENDING, attempts=1)

    handler = ConversionStageHandler(exec_config)
    # Drive the real bridge: ``execute``'s wrapper stashes via classify_job_error,
    # which applies the egress scrub. Simulate that exact stash entry.
    handler._error_details[job_id] = repository_mod.classify_job_error(DenyListDestination(rejected))

    with Session(exec_engine) as session:
        handler.finalize(session, job_id, TerminalOutcome(WorkUnitStatus.FAILED, RetryClass.PERMANENT))
        session.commit()

    with Session(exec_engine) as verify:
        job = verify.get(ConversionJob, job_id)
        assert job.error_message == "deny_list", "egress message scrubbed to the bare code"
        assert job.error_detail is None, "egress detail dropped"
        assert rejected not in (job.error_message or ""), "rejected URL absent from error_message"
        assert rejected not in (job.error_detail or ""), "rejected URL absent from error_detail"

        events = _failed_events(verify, job_id)
        payload = parse_payload_lenient(events[0].payload_json)
        assert payload.error_message == "deny_list"
        assert payload.error_detail is None
        assert rejected not in events[0].payload_json, "rejected URL absent from the persisted event"


def test_finalize_does_not_commit(exec_config: ConversionConfig, exec_engine, exec_source: Source) -> None:
    """``finalize`` stages writes but leaves the commit to the runner.

    A separate connection opened before the caller commits must still observe the
    pre-finalize status — proving ``finalize`` runs inside the runner-owned
    transaction and never commits on its own.
    """
    job_id = _seed_job_at(exec_engine, exec_source, status=ConversionJobStatus.UPLOAD_PENDING, attempts=1)
    handler = ConversionStageHandler(exec_config)
    handler._error_details[job_id] = JobErrorDetails(
        error_code="conversion_subprocess_failed",
        error_message="boom",
        error_detail=None,
        retryable=True,
        last_phase=None,
    )

    with Session(exec_engine) as session:
        handler.finalize(session, job_id, TerminalOutcome(WorkUnitStatus.FAILED, RetryClass.RETRYABLE))

        # Uncommitted: a separate connection still sees the pre-finalize status.
        with Session(exec_engine) as other:
            assert other.get(ConversionJob, job_id).status == ConversionJobStatus.UPLOAD_PENDING
            assert _failed_events(other, job_id) == []

        session.rollback()

    # Rolled back: finalize left no durable effect.
    with Session(exec_engine) as verify:
        job = verify.get(ConversionJob, job_id)
        assert job.status == ConversionJobStatus.UPLOAD_PENDING
        assert _failed_events(verify, job_id) == []


def test_execute_failure_stashes_scrubbed_details_for_finalize(
    monkeypatch: pytest.MonkeyPatch,
    exec_engine,
    exec_config: ConversionConfig,
    exec_runtime,
    exec_source: Source,
) -> None:
    """The error-details bridge: a failing ``execute`` stashes scrubbed details.

    Drives the real ``execute`` to a forced subprocess failure and asserts the
    wrapper stashed a :class:`JobErrorDetails` under the handle so ``finalize``
    can read it. Proves the bridge end-to-end (not just a hand-set stash).
    """
    job_id = _seed_running_job(exec_engine, exec_source, attempts=1)
    _patch_supervise(
        monkeypatch,
        result=SupervisionResult(
            "converting",
            {"event": "failed", "message": "child boom", "error_code": "child_code", "retryable": "false"},
            False,
            False,
        ),
        process=_FakeProcess(exitcode=1),
        write_metadata=False,
    )
    monkeypatch.setattr(repository_mod, "_is_job_cancelled", lambda *_a, **_k: False)

    handler = ConversionStageHandler(exec_config, runtime=exec_runtime)
    with pytest.raises(ReportedChildError):
        handler.execute(job_id)

    assert job_id in handler._error_details, "execute stashes error details on failure"
    details = handler._error_details[job_id]
    assert details.error_code == "child_code"
    assert details.error_message == "child boom"
    assert details.retryable is False


def test_cleanup_clears_error_details_stash(exec_config: ConversionConfig) -> None:
    """``cleanup`` clears the error-details stash so the bridge dict cannot grow.

    The runner calls ``cleanup`` on every terminal outcome; clearing the stash
    there (in addition to ``finalize``'s read-and-clear) guards against unbounded
    growth on any outcome that never finalizes the failed path.
    """
    handler = ConversionStageHandler(exec_config)
    handler._error_details[5] = JobErrorDetails(
        error_code="x",
        error_message="x",
        error_detail=None,
        retryable=True,
        last_phase=None,
    )

    handler.cleanup(5)
    assert 5 not in handler._error_details, "cleanup drops the stashed error details"
