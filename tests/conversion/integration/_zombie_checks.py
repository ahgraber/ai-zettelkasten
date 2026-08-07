"""Un-reaped-child detection for the subprocess lifecycle tests.

Kept out of ``_subprocess_helpers`` deliberately: that module is stdlib-only
because it is reimported by every spawned child, and this one needs ``psutil``.

The check is scoped to the calling process's own descendants. A zombie belonging
to some other program on the developer's machine — an editor's language server,
say — says nothing about whether the runner reaped the subprocess it spawned, so
a machine-wide scan reports unrelated processes and fails every lifecycle test on
a machine that happens to have one.

Descendants are resolved from a single ``process_iter`` snapshot rather than
``psutil.Process().children()``, whose results depend on psutil's internal
process-table cache: under the call ordering these tests use — a scan before the
act as well as after — ``children()`` omits zombie children entirely and the
assertion silently loses its teeth.

An inspection that cannot run raises :class:`ZombieInspectionError` rather than
reporting a clean tree. "No zombies found" and "could not look" are different
answers, and collapsing them lets every lifecycle assertion pass on a host where
the check never ran.
"""

from __future__ import annotations

import os

import psutil


class ZombieInspectionError(RuntimeError):
    """Process enumeration failed, so no conclusion about un-reaped children is available."""


def _self_check() -> None:
    """Prove the scan detects a deliberately leaked child.

    Run as ``python _zombie_checks.py``. Guards against the failure mode this
    module exists to avoid: a scan that is correctly scoped but silently detects
    nothing, leaving the lifecycle assertions passing for the wrong reason.
    """
    import subprocess
    import sys
    import time

    child = subprocess.Popen([sys.executable, "-c", "pass"])  # noqa: S603
    time.sleep(1.5)
    found = descendant_zombies()
    detected = any(f"pid={child.pid} " in entry for entry in found)
    child.wait()
    print(f"leaked child pid={child.pid}: {'DETECTED' if detected else 'MISSED'} {found}")
    print(f"after reaping: {descendant_zombies()}")
    raise SystemExit(0 if detected else 1)


def descendant_zombies() -> list[str]:
    """Return ``pid=... cmdline=...`` descriptions of this process's un-reaped descendants.

    A zombie is a child its parent has not yet reaped, so anything in this
    process's descendant tree is attributable to the test; anything outside it is
    not. The whole tree is walked, not just direct children, so a leaked
    grandchild is still caught while its parent is alive.

    Returns:
        One description per un-reaped descendant, empty when the tree is clean.

    Raises:
        ZombieInspectionError: If the process table cannot be enumerated, so a
            clean result would mean "did not look" rather than "found nothing".
    """
    try:
        snapshot = [proc.info for proc in psutil.process_iter(["pid", "ppid", "status", "cmdline"], ad_value=None)]
    except (psutil.Error, PermissionError) as exc:
        raise ZombieInspectionError(f"could not enumerate processes to check for un-reaped children: {exc}") from exc

    children_by_parent: dict[int | None, list[dict]] = {}
    for info in snapshot:
        children_by_parent.setdefault(info.get("ppid"), []).append(info)

    descendants: dict[int, dict] = {}
    pending = [os.getpid()]
    while pending:
        for child in children_by_parent.get(pending.pop(), []):
            pid = child["pid"]
            if pid not in descendants:
                descendants[pid] = child
                pending.append(pid)

    return [
        f"pid={info['pid']} cmdline={' '.join(info.get('cmdline') or [])}"
        for info in descendants.values()
        if info.get("status") == psutil.STATUS_ZOMBIE
    ]


if __name__ == "__main__":
    _self_check()
