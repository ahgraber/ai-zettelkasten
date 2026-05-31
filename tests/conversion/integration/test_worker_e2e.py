"""End-to-end proof: the runner entrypoint drives a real conversion job.

Proves the runner-driven worker entrypoint
(:func:`aizk.conversion.processing.worker.run_worker`, the path the
CLI ``worker`` command runs) drives a **real seeded QUEUED job** through the
generic :class:`~aizk.pipeline.runner.StageRunner` +
:class:`~aizk.conversion.handler.ConversionStageHandler` to a terminal outcome:

* ``test_worker_drives_job_to_succeeded`` — claim -> real conversion
  subprocess via the adapter's ``execute`` -> upload -> ``SUCCEEDED``, with the
  transition events present in ``pipeline_events`` (stage="conversion").
* ``test_worker_drains_inflight_on_shutdown`` — a ``request_shutdown``
  during processing drains the in-flight unit and the runner returns cleanly
  (exit code 0).

It spawns a real ``mp.Process`` (the real subprocess + signal model) and runs the
real thread pool, exercising the production boundaries. The
entrypoint's shutdown controller is injected so the test can request a graceful
shutdown without delivering a process signal (the runner's own ``shutdown=``
seam, surfaced on the entrypoint). Runs isolated under ``integration_lifecycle``
(incompatible with xdist).
"""

from __future__ import annotations

import threading
import time

from pyleak import no_thread_leaks
import pytest
from sqlmodel import Session, select

from aizk.conversion.core.source_ref import KarakeepBookmarkRef, compute_source_ref_hash
from aizk.conversion.datamodel.events import ConversionEventKind, events_for_job
from aizk.conversion.datamodel.job import ConversionJob, ConversionJobStatus
from aizk.conversion.datamodel.output import ConversionOutput
from aizk.conversion.datamodel.source import Source as Bookmark
from aizk.conversion.processing import subproc, worker
from aizk.conversion.utilities.config import ConversionConfig
from aizk.pipeline.shutdown import ShutdownController
from tests.conversion.integration import _subprocess_helpers

pytestmark = [
    pytest.mark.isolate,  # Requires pytest-isolate: pip install pytest-isolate
    pytest.mark.integration_lifecycle,  # Custom marker for selective running
]

# Spawn-safe subprocess target (the child reimports its defining module). Writes
# valid markdown + metadata; honors ``WORKER_TEST_SLEEP_SECONDS`` for the drain
# case via the shared helper's sleep, keeping the unit in flight long enough to
# observe the drain path.
_success_target = _subprocess_helpers.process_job_subprocess_success


def _seed_queued_job(session: Session, *, bookmark_id: str, idempotency_key: str) -> int:
    """Seed a Source + a QUEUED conversion job the runner will claim and run."""
    ref = KarakeepBookmarkRef(kind="karakeep_bookmark", bookmark_id=bookmark_id)
    bookmark = Bookmark(
        karakeep_id=bookmark_id,
        source_ref=ref.model_dump_json(),
        source_ref_hash=compute_source_ref_hash(ref),
        owner_id="self",
        url="https://example.com/test",
        normalized_url="https://example.com/test",
        title="Runner E2E",
        content_type="html",
        source_type="web",
    )
    session.add(bookmark)
    session.commit()
    session.refresh(bookmark)

    job = ConversionJob(
        aizk_uuid=bookmark.aizk_uuid,
        owner_id="self",
        title="Runner E2E Job",
        idempotency_key=idempotency_key,
        status=ConversionJobStatus.QUEUED,
        source_ref=ref.model_dump_json(),
    )
    session.add(job)
    session.commit()
    session.refresh(job)
    assert job.id is not None
    return job.id


def _make_fake_runtime():
    """A runtime whose converter never requires the GPU guard."""
    from unittest.mock import MagicMock

    from aizk.conversion.wiring.worker import WorkerRuntime

    fake_caps = MagicMock()
    fake_caps.converter_requires_gpu.return_value = False
    return WorkerRuntime(
        coordinator=MagicMock(),
        resource_guard=MagicMock(__enter__=MagicMock(return_value=None), __exit__=MagicMock(return_value=False)),
        capabilities=fake_caps,
    )


def _install_memory_s3(monkeypatch) -> dict[str, bytes]:
    """Replace S3Client with an in-memory store; the in-process upload phase uses it."""
    storage: dict[str, bytes] = {}

    def _init_s3_client(self, config):
        self.config = config
        self.bucket = config.s3_bucket_name
        self.client = None

    def _upload_file(self, local_path, s3_key: str) -> str:
        storage[s3_key] = local_path.read_bytes()
        return f"s3://{self.bucket}/{s3_key}"

    def _upload_fileobj(self, file_obj, s3_key: str) -> str:
        storage[s3_key] = file_obj.read()
        return f"s3://{self.bucket}/{s3_key}"

    def _get_object_bytes(self, s3_key: str) -> bytes:
        return storage[s3_key]

    monkeypatch.setattr("aizk.conversion.storage.s3_client.S3Client.__init__", _init_s3_client)
    monkeypatch.setattr("aizk.conversion.storage.s3_client.S3Client.upload_file", _upload_file)
    monkeypatch.setattr("aizk.conversion.storage.s3_client.S3Client.upload_fileobj", _upload_fileobj)
    monkeypatch.setattr("aizk.conversion.storage.s3_client.S3Client.get_object_bytes", _get_object_bytes)
    return storage


def _wait_for(predicate, *, timeout_seconds: float, interval_seconds: float = 0.05) -> bool:
    """Poll ``predicate`` until it is truthy or the timeout elapses."""
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval_seconds)
    return False


def _patch_worker_infra(monkeypatch) -> tuple[dict[str, bytes], list[int]]:
    """Install shared patches for worker e2e tests.

    Installs the in-memory S3 backend, stubs out the subprocess target,
    the cancellation check, the runtime builder, and the forced-exit seam.
    Returns ``(storage, exit_calls)`` so per-test assertions can inspect them.
    """
    storage = _install_memory_s3(monkeypatch)
    monkeypatch.setattr(subproc, "_process_job_subprocess", _success_target)
    monkeypatch.setattr(subproc, "_is_job_cancelled", lambda _job_id, _engine: False)
    monkeypatch.setattr("aizk.conversion.wiring.worker.build_worker_runtime", lambda _cfg: _make_fake_runtime())
    exit_calls: list[int] = []
    monkeypatch.setattr("aizk.pipeline.shutdown.force_exit", lambda code: exit_calls.append(code))
    return storage, exit_calls


def test_worker_drives_job_to_succeeded(monkeypatch, db_session: Session) -> None:
    """The runner entrypoint drives a real QUEUED job through to SUCCEEDED.

    Proves the runner + adapter path end to end: a real ``mp.Process``
    subprocess runs the conversion (claim -> execute -> upload), the job reaches
    ``SUCCEEDED``, a ``ConversionOutput`` row is written, the uploaded markdown
    matches the converted content, and the ``pipeline_events`` log
    (stage="conversion") carries the claimed -> upload_pending -> succeeded
    transitions.
    Once SUCCEEDED is observed, a ``request_shutdown`` lets the runner drain (no
    in-flight work) and return cleanly (exit code 0).
    """
    monkeypatch.setenv("AIZK_WORKER_JOB_TIMEOUT_SECONDS", "30")
    monkeypatch.setenv("WORKER_TEST_SUCCESS_MARKDOWN", "# Runner Drove This\n")
    monkeypatch.setenv("WORKER_TEST_SUCCESS_BOOKMARK_ID", "bm_runner_succeed")

    storage, exit_calls = _patch_worker_infra(monkeypatch)

    job_id = _seed_queued_job(db_session, bookmark_id="bm_runner_succeed", idempotency_key="succeed0" * 8)
    config = ConversionConfig(_env_file=None)
    engine = db_session.get_bind()

    shutdown = ShutdownController()
    exit_code: dict[str, int | None] = {"value": None}

    def _drive() -> None:
        exit_code["value"] = worker.run_worker(config, shutdown=shutdown)

    with no_thread_leaks(action="raise", grace_period=1.0):
        driver = threading.Thread(target=_drive, name="runner-worker-driver")
        driver.start()

        def _job_succeeded() -> bool:
            with Session(engine) as session:
                job = session.get(ConversionJob, job_id)
                return job is not None and job.status == ConversionJobStatus.SUCCEEDED

        assert _wait_for(_job_succeeded, timeout_seconds=30.0), "runner drove the job to SUCCEEDED"

        # Job is terminal; request graceful shutdown so run() drains (nothing in
        # flight) and returns cleanly.
        shutdown.request_shutdown()
        driver.join(timeout=15.0)
        assert not driver.is_alive(), "runner loop returned after shutdown"

    assert exit_code["value"] == 0, "clean drain returns exit code 0"
    assert exit_calls == [], "no forced exit on a clean drain"

    with Session(engine) as session:
        job = session.get(ConversionJob, job_id)
        assert job is not None
        assert job.status == ConversionJobStatus.SUCCEEDED

        output = session.exec(select(ConversionOutput).where(ConversionOutput.job_id == job_id)).one()
        assert storage[output.markdown_key].decode("utf-8") == "# Runner Drove This\n"

        kinds = [event.kind for event in events_for_job(session, job_id)]
        assert ConversionEventKind.CLAIMED.value in kinds, "claim transition recorded"
        assert ConversionEventKind.UPLOAD_PENDING.value in kinds, "upload-pending transition recorded"
        assert ConversionEventKind.SUCCEEDED.value in kinds, "succeeded transition recorded"
        assert all(event.stage == "conversion" for event in events_for_job(session, job_id))


def test_worker_drains_inflight_on_shutdown(monkeypatch, db_session: Session) -> None:
    """A shutdown during processing drains the in-flight unit; runner exits 0.

    Seeds a QUEUED job whose subprocess sleeps, drives the runner entrypoint,
    waits until the unit is in flight, then ``request_shutdown``. The runner
    stops claiming and drains the in-flight subprocess within the drain timeout
    (the subprocess finishes its sleep), finalizes it to ``SUCCEEDED``, and
    returns cleanly (exit code 0) without forcing exit.
    """
    monkeypatch.setenv("AIZK_WORKER_JOB_TIMEOUT_SECONDS", "30")
    # Short drain so the test does not wait the production default (300s); the
    # sleeping subprocess finishes well within it.
    monkeypatch.setenv("AIZK_WORKER_DRAIN_TIMEOUT_SECONDS", "20")
    monkeypatch.setenv("WORKER_TEST_SLEEP_SECONDS", "1")
    monkeypatch.setenv("WORKER_TEST_SUCCESS_BOOKMARK_ID", "bm_runner_drain")

    storage, exit_calls = _patch_worker_infra(monkeypatch)

    job_id = _seed_queued_job(db_session, bookmark_id="bm_runner_drain", idempotency_key="draining" * 8)
    config = ConversionConfig(_env_file=None)
    engine = db_session.get_bind()

    shutdown = ShutdownController()
    exit_code: dict[str, int | None] = {"value": None}

    def _drive() -> None:
        exit_code["value"] = worker.run_worker(config, shutdown=shutdown)

    with no_thread_leaks(action="raise", grace_period=1.0):
        driver = threading.Thread(target=_drive, name="runner-worker-driver")
        driver.start()

        # Wait until the job is claimed (in flight) before signalling shutdown so
        # the drain path — not the idle-shutdown path — is what is under test.
        def _job_running() -> bool:
            with Session(engine) as session:
                job = session.get(ConversionJob, job_id)
                return job is not None and job.status != ConversionJobStatus.QUEUED

        assert _wait_for(_job_running, timeout_seconds=15.0), "runner claimed the job (now in flight)"

        shutdown.request_shutdown()
        driver.join(timeout=25.0)
        assert not driver.is_alive(), "runner drained and returned after shutdown"

    assert exit_code["value"] == 0, "graceful drain returns exit code 0"
    assert exit_calls == [], "no forced exit when the in-flight unit drains in time"

    with Session(engine) as session:
        job = session.get(ConversionJob, job_id)
        assert job is not None
        # The in-flight unit drained to completion (not stranded RUNNING).
        assert job.status == ConversionJobStatus.SUCCEEDED, "in-flight unit drained to its terminal outcome"
        output = session.exec(select(ConversionOutput).where(ConversionOutput.job_id == job_id)).one()
        assert output.markdown_key in storage
