"""Import graph lint tests for the conversion package boundary invariants.

Covers:
- No adapter module imports any other adapter module (cross-adapter isolation).
- No module outside aizk/conversion/wiring/ imports both aizk.conversion.core
  and aizk.conversion.adapters.
"""

from __future__ import annotations

import ast
import importlib.util
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_CONVERSION_ROOT = Path(__file__).parents[4] / "src" / "aizk" / "conversion"
_ADAPTERS_ROOT = _CONVERSION_ROOT / "adapters"
_WIRING_ROOT = _CONVERSION_ROOT / "wiring"


def _py_files_under(directory: Path) -> list[Path]:
    """Return all .py files recursively under ``directory``."""
    return sorted(directory.rglob("*.py"))


def _collect_imports(path: Path) -> list[str]:
    """Return a list of absolute module name prefixes imported by ``path``.

    Parses the AST so we catch both ``import X`` and ``from X import Y``
    without executing the module.
    """
    source = path.read_text(encoding="utf-8")
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError:
        return []

    imports: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imports.append(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                imports.append(node.module)
    return imports


def _module_name_from_path(path: Path) -> str:
    """Convert a filesystem path to a dotted module name relative to src/."""
    src_root = _CONVERSION_ROOT.parents[2]  # .../src
    rel = path.relative_to(src_root)
    parts = list(rel.with_suffix("").parts)
    return ".".join(parts)


# ---------------------------------------------------------------------------
# Invariant 1: no adapter imports another adapter
# ---------------------------------------------------------------------------


def _adapter_py_files() -> list[Path]:
    return _py_files_under(_ADAPTERS_ROOT)


@pytest.mark.parametrize("adapter_file", _adapter_py_files(), ids=lambda p: p.name)
def test_adapter_does_not_import_another_adapter(adapter_file: Path) -> None:
    """Adapter modules must not import sibling adapter modules.

    Each adapter in aizk.conversion.adapters.* must import only from
    aizk.conversion.core, aizk.conversion.utilities, or the stdlib/third-party.
    Cross-adapter imports create tight coupling that violates the Ports & Adapters
    boundary.
    """
    imports = _collect_imports(adapter_file)
    violations = [
        imp
        for imp in imports
        if imp.startswith("aizk.conversion.adapters")
        and not adapter_file.is_relative_to(
            _ADAPTERS_ROOT / imp.split(".")[3] if len(imp.split(".")) > 3 else _ADAPTERS_ROOT
        )
    ]
    # Simpler check: any import of aizk.conversion.adapters from inside adapters is a cross-adapter import.
    # We allow a file to import itself (same sub-package __init__), but not siblings.
    cross_adapter = []
    for imp in imports:
        if not imp.startswith("aizk.conversion.adapters"):
            continue
        # Determine which sub-package the import targets (e.g. "converters" or "fetchers.arxiv")
        parts = imp.split(".")  # ["aizk", "conversion", "adapters", ...]
        if len(parts) < 4:
            # Just "aizk.conversion.adapters" — that's the package __init__, allowed.
            continue
        target_subpkg = parts[3]  # e.g. "converters" or "fetchers"
        # Determine which sub-package the *importing* file belongs to.
        try:
            rel = adapter_file.relative_to(_ADAPTERS_ROOT)
            own_subpkg = rel.parts[0]  # e.g. "converters" or "fetchers"
        except ValueError:
            own_subpkg = None

        if own_subpkg != target_subpkg:
            cross_adapter.append(imp)

    assert cross_adapter == [], (
        f"{adapter_file.relative_to(_CONVERSION_ROOT)} imports sibling adapter sub-packages: {cross_adapter}"
    )


# ---------------------------------------------------------------------------
# Invariant 2: only wiring/ may import both core and adapters
# ---------------------------------------------------------------------------


def _non_wiring_conversion_py_files() -> list[Path]:
    """All .py files under aizk/conversion/ that are NOT under wiring/."""
    all_files = _py_files_under(_CONVERSION_ROOT)
    return [f for f in all_files if not f.is_relative_to(_WIRING_ROOT)]


@pytest.mark.parametrize(
    "source_file", _non_wiring_conversion_py_files(), ids=lambda p: str(p.relative_to(_CONVERSION_ROOT))
)
def test_non_wiring_module_does_not_import_both_core_and_adapters(source_file: Path) -> None:
    """Modules outside wiring/ must not import BOTH core and adapters.

    Only the wiring package is the composition root that binds core protocols
    to adapter implementations. Any other module importing both would bypass
    the intended layering and create hidden coupling.
    """
    imports = _collect_imports(source_file)
    imports_core = any(imp.startswith("aizk.conversion.core") for imp in imports)
    imports_adapters = any(imp.startswith("aizk.conversion.adapters") for imp in imports)

    assert not (imports_core and imports_adapters), (
        f"{source_file.relative_to(_CONVERSION_ROOT)} imports BOTH "
        f"aizk.conversion.core and aizk.conversion.adapters — "
        f"only wiring/ modules may do this."
    )
