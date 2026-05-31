#!/usr/bin/env python3
"""Real-world end-to-end smoke gate for the shared-runner conversion worker.

This is a MANUAL confidence gate, NOT a pytest test. It drives the new
runner-based conversion worker (:func:`aizk.conversion.processing.worker.run_worker`,
i.e. ``StageRunner`` over ``ConversionStageHandler`` — the path
``aizk-conversion worker`` now runs) against:

* a REAL KaraKeep instance (a small, bounded sample of bookmarks), and
* REAL Docling conversion (subprocess + provider work),

end-to-end, into a THROWAWAY temporary SQLite database. The default
``data/conversion_service.db`` is never touched.

It performs REAL network and provider work (KaraKeep API calls, source
fetches, Docling conversion, and S3 artifact uploads). Do not run it in CI or
the sandbox — run it by hand when you want to confirm the runner-driven
pipeline really converts real content end-to-end after a runtime change.

Run as a Jupyter-style ``# %%`` notebook (VS Code / Jupytext) and execute the
cells top to bottom. Importing/parsing this module is side-effect-free: the
temp-DB setup and the live run live inside cells under the ``__main__`` guard,
so the file can be statically validated without doing any real work.

Required environment variables (set in your shell or ``.env``/direnv):

* ``AIZK_FETCHER__KARAKEEP__API_KEY`` — KaraKeep API token.
* ``AIZK_FETCHER__KARAKEEP__BASE_URL`` — KaraKeep base URL (e.g.
  ``https://karakeep.example.com``).
* S3 / object-store credentials the worker uploads artifacts through:
  ``AIZK_S3_ENDPOINT_URL``, ``AIZK_S3_BUCKET_NAME``, ``AIZK_S3_ACCESS_KEY_ID``,
  ``AIZK_S3_SECRET_ACCESS_KEY`` (and optionally ``AIZK_S3_REGION``). Without a
  reachable bucket the conversion succeeds but the upload step fails and jobs
  land in ``failed_retryable``.

Optional environment variables (only if the converter needs them):

* ``AIZK_CONVERTER__DOCLING__PICTURE_DESCRIPTION_BASE_URL`` and
  ``AIZK_CONVERTER__DOCLING__PICTURE_DESCRIPTION_API_KEY`` — enable VLM picture
  description; leave unset to skip it.
* ``AIZK_CONVERTER__DOCLING__OCR_ENABLED`` /
  ``AIZK_CONVERTER__DOCLING__PICTURE_CLASSIFICATION_ENABLED`` — toggle the
  heavier Docling stages.

Verify the required vars are set before running, e.g.::

    env | grep -E 'AIZK_FETCHER__KARAKEEP__|AIZK_S3_'

The cells below assert that ``AIZK_FETCHER__KARAKEEP__API_KEY`` and
``AIZK_FETCHER__KARAKEEP__BASE_URL`` are present and fail fast with a clear
message if not.
"""

# %%
from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
import tempfile

# %% [markdown]
# ## 1. Isolate to a throwaway temp database (run FIRST)
#
# `AIZK_DATABASE_URL` MUST be set before any `ConversionConfig`/app/worker
# object is constructed, because every config reads the URL at construction time
# and `aizk.conversion.db.get_engine` caches engines by URL. We point it at a
# fresh temp SQLite file so the real `data/conversion_service.db` is never
# touched, then guard that the active config really resolved to the temp file.

# %%
DEFAULT_DB_URL = "sqlite:///./data/conversion_service.db"


def isolate_temp_database() -> tuple[Path, str]:
    """Create a throwaway temp SQLite DB and point ``AIZK_DATABASE_URL`` at it.

    Sets the env var **before** any conversion config/app/worker is built, then
    asserts that a freshly constructed :class:`ConversionConfig` resolves to the
    temp file (and not the default ``data/conversion_service.db``), so a stray
    shell-exported URL or a leaked default cannot send the smoke run at the real
    database.

    Returns:
        The temp DB :class:`~pathlib.Path` and the ``sqlite:///`` URL it set.
    """
    tmp_dir = tempfile.mkdtemp(prefix="aizk_runner_smoke_")
    tmp_db = Path(tmp_dir) / "runner_smoke.db"
    tmp_url = f"sqlite:///{tmp_db}"
    os.environ["AIZK_DATABASE_URL"] = tmp_url

    # Import config only AFTER the env var is set so its default cannot win.
    from aizk.conversion.utilities.config import ConversionConfig

    active_url = ConversionConfig().database_url
    if active_url != tmp_url:
        raise RuntimeError(
            f"Temp-DB isolation failed: ConversionConfig().database_url is {active_url!r}, "
            f"expected {tmp_url!r}. Refusing to run against a non-temp database."
        )
    if active_url == DEFAULT_DB_URL or active_url.endswith("conversion_service.db"):
        raise RuntimeError(
            f"Active database URL {active_url!r} points at the default service DB. "
            "Refusing to run the smoke flow against it."
        )
    return tmp_db, tmp_url


# %% [markdown]
# ## 2. Logging + KaraKeep credential check
#
# The smoke run touches a real KaraKeep instance; fail fast with a clear message
# if the credentials are missing rather than emitting opaque HTTP errors deep in
# the fetch.


# %%
def configure_smoke_logging() -> logging.Logger:
    """Configure INFO logging for the smoke run and return this module's logger."""
    logging.basicConfig(level=logging.INFO)
    logging.getLogger("aizk").setLevel(logging.INFO)
    logging.getLogger("karakeep_client").setLevel(logging.INFO)
    log = logging.getLogger("worker_smoke")
    log.setLevel(logging.INFO)
    return log


def require_karakeep_env() -> tuple[str, str]:
    """Return ``(api_key, base_url)`` for KaraKeep, raising if either is unset.

    Returns:
        The configured KaraKeep API key and base URL.

    Raises:
        RuntimeError: If ``AIZK_FETCHER__KARAKEEP__API_KEY`` or
            ``AIZK_FETCHER__KARAKEEP__BASE_URL`` is missing/blank.
    """
    api_key = os.environ.get("AIZK_FETCHER__KARAKEEP__API_KEY", "").strip()
    base_url = os.environ.get("AIZK_FETCHER__KARAKEEP__BASE_URL", "").strip()
    missing = [
        name
        for name, value in (
            ("AIZK_FETCHER__KARAKEEP__API_KEY", api_key),
            ("AIZK_FETCHER__KARAKEEP__BASE_URL", base_url),
        )
        if not value
    ]
    if missing:
        raise RuntimeError(
            f"Missing required KaraKeep env var(s): {', '.join(missing)}. "
            "Set them (e.g. via .env/direnv) before running the smoke notebook. "
            "Verify with:  env | grep AIZK_FETCHER__KARAKEEP__"
        )
    return api_key, base_url


# %% [markdown]
# ## 3. Fetch a bounded KaraKeep sample
#
# `SAMPLE_SIZE` is the obvious, top-of-cell bound on how many real bookmarks the
# smoke run will convert. Keep it small — every bookmark drives a real Docling
# conversion.

# %%
SAMPLE_SIZE = 3  # bounded sample — keep small; each bookmark drives a real conversion


async def _fetch_bounded_bookmark_ids(api_key: str, base_url: str, sample_size: int) -> list[str]:
    """Fetch up to ``sample_size`` bookmark IDs from a real KaraKeep instance.

    Args:
        api_key: KaraKeep API token.
        base_url: KaraKeep base URL.
        sample_size: Maximum number of bookmark IDs to return (the bound).

    Returns:
        At most ``sample_size`` KaraKeep bookmark IDs from the first page.
    """
    from karakeep_client.karakeep import KarakeepClient

    client = KarakeepClient(api_key=api_key, base_url=base_url)
    page = await client.get_bookmarks_paged(limit=sample_size, include_content=False)
    return [bookmark.id for bookmark in page.bookmarks[:sample_size]]


def fetch_bounded_bookmark_ids(api_key: str, base_url: str, sample_size: int) -> list[str]:
    """Run :func:`_fetch_bounded_bookmark_ids` to completion from a sync cell.

    Notebook kernels already own an event loop, so ``asyncio.run`` would raise.
    This applies :mod:`nest_asyncio` (the constrained compatibility fallback for
    notebooks) and drives the coroutine on the running loop, keeping the driver
    cell free of top-level ``await`` so the whole file stays statically
    parseable. In a plain (loop-less) interpreter it falls back to
    ``asyncio.run``.

    Args:
        api_key: KaraKeep API token.
        base_url: KaraKeep base URL.
        sample_size: Maximum number of bookmark IDs to return (the bound).

    Returns:
        At most ``sample_size`` KaraKeep bookmark IDs.
    """
    coro = _fetch_bounded_bookmark_ids(api_key, base_url, sample_size)
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    import nest_asyncio

    nest_asyncio.apply()
    return loop.run_until_complete(coro)


# %% [markdown]
# ## 4. Submit the sample as conversion jobs (real submit policy path)
#
# Submit through the in-process FastAPI app via `TestClient` so the jobs go
# through the real `POST /v1/jobs` path (source materialization, idempotency,
# capability gate) into the temp DB. The app's lifespan builds its config from
# `AIZK_DATABASE_URL`, which we already pointed at the temp file, and runs
# migrations itself.


# %%
def submit_sample_jobs(bookmark_ids: list[str]) -> list[dict]:
    """Submit ``bookmark_ids`` as conversion jobs through the real API path.

    Builds the conversion FastAPI app and drives ``POST /v1/jobs`` for each
    bookmark via :class:`fastapi.testclient.TestClient`. The app's lifespan runs
    migrations against the temp DB (the same ``run_migrations`` the CLI uses), so
    no separate migration step is needed for the submit side.

    Args:
        bookmark_ids: KaraKeep bookmark IDs to submit.

    Returns:
        The decoded job-response payloads for the submitted jobs.
    """
    from fastapi.testclient import TestClient

    from aizk.conversion.api.main import create_app

    app = create_app()
    responses: list[dict] = []
    # base_url="http://localhost" makes TestClient send `Host: localhost`, which
    # the app's TrustedHostMiddleware allowlists. Default `testserver` is rejected.
    with TestClient(app, base_url="http://localhost") as client:
        for bookmark_id in bookmark_ids:
            resp = client.post(
                "/v1/jobs",
                json={"source_ref": {"kind": "karakeep_bookmark", "bookmark_id": bookmark_id}},
            )
            resp.raise_for_status()
            responses.append(resp.json())
    return responses


# %% [markdown]
# ## 5. Run the shared-runner worker until the sample drains
#
# Build the runner exactly as `run_worker` does (StageRunner over
# ConversionStageHandler, same loop timings), but drive it with
# `run_until_idle()` so the cell terminates once the bounded sample drains
# instead of running the supervised signal loop forever.


# %%
def run_runner_until_drained(max_iterations: int = 100_000) -> None:
    """Drive the shared-runner conversion worker until the temp DB drains.

    Mirrors :func:`aizk.conversion.processing.worker.run_worker`
    (same handler, engine, and loop timings) but calls
    :meth:`~aizk.pipeline.runner.StageRunner.run_until_idle` so this cell
    returns once no work is in flight and none is eligible — rather than running
    the supervised signal loop that only stops on SIGTERM/SIGINT.

    Args:
        max_iterations: Safety bound passed to ``run_until_idle`` so a wedged
            queue cannot loop forever in the notebook.
    """
    from aizk.conversion.db import get_engine
    from aizk.conversion.handler import ConversionStageHandler
    from aizk.conversion.utilities.config import ConversionConfig
    from aizk.conversion.wiring.worker import build_worker_runtime
    from aizk.pipeline.runner import StageRunner

    # ``_LEGACY_POLL_INTERVAL_SECONDS`` / ``_TERMINATION_BUDGET_SECONDS`` mirror
    # the constants ``run_worker`` uses; inlined here so this driver does
    # not depend on the entrypoint module's private names.
    legacy_poll_interval_seconds = 2.0
    termination_budget_seconds = 10.0

    config = ConversionConfig()
    runtime = build_worker_runtime(config)
    handler = ConversionStageHandler(config, runtime=runtime)
    runner = StageRunner(
        handler,
        engine=get_engine(config.database_url),
        drain_timeout=float(config.worker_drain_timeout_seconds),
        poll_interval=legacy_poll_interval_seconds,
        stale_recovery_interval=config.worker_stale_job_check_seconds,
        cancel_grace=termination_budget_seconds,
    )
    runner.run_until_idle(max_iterations=max_iterations)


# %% [markdown]
# ## 6. Inspect observable outcomes
#
# Eyeball that the runner-driven pipeline really converted real content:
# job status counts, terminal outcomes per job, output rows/artifacts written,
# and the `pipeline_events` (stage="conversion") for at least one source.


# %%
def report_outcomes(log: logging.Logger) -> dict[str, int]:
    """Print job status counts, output rows, and per-source conversion events.

    Reads back the temp DB and logs:

    * status counts across all jobs (the terminal-outcome distribution),
    * each job's status + any written :class:`ConversionOutput` artifact summary,
    * the ``pipeline_events`` (``stage="conversion"``) for the first job, so a
      human can trace queued -> claimed -> ... -> succeeded/failed end-to-end.

    Args:
        log: Logger to emit the human-readable report through.

    Returns:
        The status-count mapping (status value -> job count).
    """
    from collections import Counter

    from sqlmodel import Session, select

    from aizk.conversion.datamodel.events import events_for_job
    from aizk.conversion.datamodel.job import ConversionJob
    from aizk.conversion.datamodel.output import ConversionOutput
    from aizk.conversion.db import get_engine
    from aizk.conversion.utilities.config import ConversionConfig

    config = ConversionConfig()
    engine = get_engine(config.database_url)

    status_counts: Counter[str] = Counter()
    with Session(engine) as session:
        jobs = session.exec(select(ConversionJob).order_by(ConversionJob.id)).all()
        log.info("Jobs in temp DB: %d", len(jobs))
        for job in jobs:
            status_value = job.status.value if hasattr(job.status, "value") else str(job.status)
            status_counts[status_value] += 1
            output = session.exec(select(ConversionOutput).where(ConversionOutput.job_id == job.id)).first()
            artifact = (
                f"prefix={output.s3_prefix} markdown={output.markdown_key} figures={output.figure_count}"
                if output
                else "<no output row>"
            )
            log.info(
                "job id=%s status=%s attempts=%s error=%s | %s",
                job.id,
                status_value,
                job.attempts,
                job.error_code,
                artifact,
            )

        log.info("Status counts: %s", dict(status_counts))

        # Conversion event trail for at least one source.
        if jobs:
            first = jobs[0]
            events = events_for_job(session, first.id)
            log.info("pipeline_events for job id=%s (stage=conversion): %d rows", first.id, len(events))
            for event in events:
                log.info(
                    "  event_id=%s attempt=%s %s -> %s kind=%s",
                    event.event_id,
                    event.attempt,
                    event.from_status,
                    event.to_status,
                    event.kind,
                )

    return dict(status_counts)


# %% [markdown]
# ## 7. Drive the full smoke run
#
# Everything above is pure definitions (side-effect-free on import). This final
# cell performs the real run; it is guarded by ``__name__ == "__main__"`` so
# static import/parse never triggers network or provider work.

# %%
if __name__ == "__main__":
    _log = configure_smoke_logging()

    _tmp_db, _tmp_url = isolate_temp_database()
    _log.info("Isolated temp database: %s", _tmp_url)

    _api_key, _base_url = require_karakeep_env()

    # The fetch helper drives the async KaraKeep client to completion (notebook
    # loop-aware), so this cell needs no top-level ``await`` and the file parses.
    _bookmark_ids = fetch_bounded_bookmark_ids(_api_key, _base_url, SAMPLE_SIZE)
    _log.info("Fetched %d bookmark id(s) (bound=%d): %s", len(_bookmark_ids), SAMPLE_SIZE, _bookmark_ids)
    if not _bookmark_ids:
        raise RuntimeError("KaraKeep returned no bookmarks; nothing to convert.")

    _submitted = submit_sample_jobs(_bookmark_ids)
    _log.info("Submitted %d job(s) into the temp DB.", len(_submitted))

    _log.info("Running shared-runner worker until the sample drains...")
    run_runner_until_drained()

    _counts = report_outcomes(_log)
    _log.info("Smoke run complete. Terminal status counts: %s", _counts)
