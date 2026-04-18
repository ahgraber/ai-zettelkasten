"""Lint-style tests that the worker's subprocess signatures stay in sync.

Regression-proof the class of failure surfaced by the PR-6 rename of
``karakeep_payload_path → source_ref_path`` where the function signature
moved but ``test_process_group_creation_called_in_subprocess`` was never
updated. The test continued to pass by NOT being collected, and the call
at runtime would ``TypeError``. A small self-check in CI catches the
next such drift at test time rather than in production.
"""

from __future__ import annotations

import inspect

from aizk.conversion.workers import orchestrator


def test_spawn_args_match_process_job_subprocess_parameters() -> None:
    """``_spawn_conversion_subprocess`` builds the child's positional args.

    The child's ``_process_job_subprocess`` takes positional args in a
    specific order; if either signature drifts without the other being
    updated, the child dies with ``TypeError`` at spawn time. Enforce the
    pair stays in sync by parameter name + order.
    """
    spawn_sig = inspect.signature(orchestrator._spawn_conversion_subprocess)
    target_sig = inspect.signature(orchestrator._process_job_subprocess)

    # Parent's kw-only args (job_id, workspace, source_ref_path) must be a
    # prefix of the child's positional args, plus the status_queue the
    # parent constructs internally.
    spawn_params = [
        name for name, p in spawn_sig.parameters.items() if p.kind != inspect.Parameter.VAR_KEYWORD
    ]
    target_params = list(target_sig.parameters.keys())

    # The child accepts (job_id, workspace_path, source_ref_path, status_queue).
    # The parent accepts (job_id, workspace, source_ref_path) as kw-only and
    # constructs status_queue internally — so parent params + "status_queue"
    # must equal the child's param list (accounting for the workspace name
    # difference: parent is ``workspace`` which maps to child's
    # ``workspace_path``).
    assert target_params == ["job_id", "workspace_path", "source_ref_path", "status_queue"], (
        "_process_job_subprocess signature drifted; update this test AND any "
        "caller that constructs its positional args."
    )
    assert spawn_params == ["job_id", "workspace", "source_ref_path"], (
        "_spawn_conversion_subprocess signature drifted; ensure the "
        "positional args passed to ctx.Process(target=...) still match."
    )


def test_process_job_subprocess_has_no_legacy_kwarg_names() -> None:
    """Lock out the pre-PR-6 kwarg names to surface stale test harnesses."""
    sig = inspect.signature(orchestrator._process_job_subprocess)
    assert "karakeep_payload_path" not in sig.parameters, (
        "_process_job_subprocess must not reintroduce ``karakeep_payload_path``; "
        "the pluggable-pipeline refactor renamed it to ``source_ref_path``. Any "
        "test or helper still using the old name would silently TypeError at "
        "runtime."
    )
