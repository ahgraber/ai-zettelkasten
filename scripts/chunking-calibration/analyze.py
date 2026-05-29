"""Summarize the calibration artifacts produced by ``measure.py``.

Reads ``regions.parquet``, ``per_doc_metrics.parquet``, and ``memory.json``
from ``--in`` (default: ``<repo>/data/chunking-calibration``) and emits a
markdown report to stdout (or to ``--out`` if provided) covering:

- Decile distributions (p10/p25/p50/p75/p90/p95/p99) of char_count and
  tiktoken cl100k counts, split by region_kind.
- Fit-share tables across candidate character budgets for both heading-body
  and top-level-block (paragraph) regions, plus the smallest budget at which
  each fit threshold (75% / 90% / 95% / 99%) is met.
- Parse-latency deciles, total wall-clock, and a linear regression of
  parse_latency_ms vs doc_size_chars.
- Peak tracemalloc allocation and peak RSS (normalized by platform).
- Outlier documents flagged for inspection.
"""

from __future__ import annotations

import argparse
from io import StringIO
import json
import logging
from pathlib import Path
import sys

from setproctitle import setproctitle

import numpy as np
import pandas as pd

logger = logging.getLogger("chunking-calibration.analyze")

# This script lives at <repo>/scripts/chunking-calibration/analyze.py; it reads
# the artifacts measure.py wrote to the gitignored <repo>/data/chunking-calibration.
_DEFAULT_IN_DIR = Path(__file__).resolve().parents[2] / "data" / "chunking-calibration"

DECILES = [0.10, 0.25, 0.50, 0.75, 0.90, 0.95, 0.99]
BUDGETS = [1024, 1536, 2048, 3072, 4096, 6144, 8192, 12288, 16384]
FIT_THRESHOLDS = [0.75, 0.90, 0.95, 0.99]


def _quantile_row(series: pd.Series) -> dict[str, float]:
    """Return a dict mapping `pNN` to the quantile of `series`."""
    if series.empty:
        return {f"p{int(q * 100)}": float("nan") for q in DECILES}
    qs = series.quantile(DECILES)
    return {f"p{int(q * 100)}": float(qs.loc[q]) for q in DECILES}


def _quantile_table(df: pd.DataFrame, group_col: str, value_col: str) -> pd.DataFrame:
    """Build a quantile table for ``value_col`` grouped by ``group_col``."""
    rows: list[dict[str, object]] = []
    for group, sub in df.groupby(group_col):
        row: dict[str, object] = {group_col: group, "count": int(len(sub))}
        row.update(_quantile_row(sub[value_col]))
        rows.append(row)
    return pd.DataFrame(rows)


def _fit_share_table(regions: pd.DataFrame) -> pd.DataFrame:
    """Compute share of regions that fit within each candidate budget."""
    counts = regions["char_count"]
    n = len(counts)
    rows: list[dict[str, object]] = []
    for budget in BUDGETS:
        fit = int((counts <= budget).sum())
        rows.append(
            {
                "budget_chars": budget,
                "fit_count": fit,
                "total": n,
                "fit_share": fit / n if n else float("nan"),
            }
        )
    return pd.DataFrame(rows)


def _smallest_budget_at_threshold(fit_table: pd.DataFrame, threshold: float) -> int | None:
    """Smallest budget in `fit_table` whose `fit_share >= threshold`, or None."""
    qualifying = fit_table[fit_table["fit_share"] >= threshold]
    if qualifying.empty:
        return None
    return int(qualifying.iloc[0]["budget_chars"])


def _latency_regression(per_doc: pd.DataFrame) -> dict[str, float]:
    """OLS fit of `parse_latency_ms` against `doc_size_chars`."""
    successful = per_doc[per_doc["error"] == ""]
    if len(successful) < 2:
        return {"slope_ms_per_char": float("nan"), "intercept_ms": float("nan"), "r_squared": float("nan")}
    x = successful["doc_size_chars"].to_numpy(dtype=float)
    y = successful["parse_latency_ms"].to_numpy(dtype=float)
    slope, intercept = np.polyfit(x, y, 1)
    y_pred = slope * x + intercept
    ss_res = float(np.sum((y - y_pred) ** 2))
    ss_tot = float(np.sum((y - y.mean()) ** 2))
    r_squared = 1.0 - ss_res / ss_tot if ss_tot > 0 else float("nan")
    return {
        "slope_ms_per_char": float(slope),
        "intercept_ms": float(intercept),
        "r_squared": float(r_squared),
    }


def _memory_summary(memory: dict[str, object]) -> dict[str, object]:
    """Reduce memory snapshots to peak tracemalloc and peak RSS."""
    snapshots = memory.get("snapshots", [])
    peak_tracemalloc = max((int(s["tracemalloc_peak_bytes"]) for s in snapshots), default=0)
    peak_rss_raw = max((int(s["ru_maxrss_raw"]) for s in snapshots), default=0)
    platform = snapshots[-1]["platform"] if snapshots else sys.platform
    # macOS reports ru_maxrss in bytes; Linux reports in kilobytes.
    peak_rss_bytes = peak_rss_raw if platform == "darwin" else peak_rss_raw * 1024
    return {
        "peak_tracemalloc_bytes": peak_tracemalloc,
        "peak_tracemalloc_mb": peak_tracemalloc / (1024 * 1024),
        "peak_rss_bytes": peak_rss_bytes,
        "peak_rss_mb": peak_rss_bytes / (1024 * 1024),
        "platform": platform,
    }


def _format_quantile_table(table: pd.DataFrame) -> str:
    """Render a quantile DataFrame as a markdown table."""
    return table.to_markdown(index=False, floatfmt=".1f")


def _format_fit_share(fit_table: pd.DataFrame) -> str:
    """Render the fit-share DataFrame as a markdown table."""
    rendered = fit_table.copy()
    rendered["fit_share"] = (rendered["fit_share"] * 100).round(2).astype(str) + " %"
    return rendered.to_markdown(index=False)


def _flag_outliers(per_doc: pd.DataFrame, regions: pd.DataFrame) -> pd.DataFrame:
    """Return a DataFrame of outlier documents worth inspecting.

    Includes documents whose parse failed, whose parse latency exceeds the 99th
    percentile, or whose largest heading body exceeds the 99th-percentile
    heading-body size.
    """
    rows: list[dict[str, object]] = []
    errors = per_doc[per_doc["error"] != ""]
    for _, row in errors.iterrows():
        rows.append({"doc_path": row["doc_path"], "reason": "parse_error", "detail": row["error"]})

    successful = per_doc[per_doc["error"] == ""]
    if not successful.empty:
        latency_p99 = float(successful["parse_latency_ms"].quantile(0.99))
        slow = successful[successful["parse_latency_ms"] > latency_p99].nlargest(10, "parse_latency_ms")
        for _, row in slow.iterrows():
            rows.append(
                {
                    "doc_path": row["doc_path"],
                    "reason": "slow_parse",
                    "detail": f"{row['parse_latency_ms']:.1f}ms over p99={latency_p99:.1f}ms",
                }
            )

    heading_bodies = regions[regions["region_kind"] == "heading_body"]
    if not heading_bodies.empty:
        section_p99 = float(heading_bodies["char_count"].quantile(0.99))
        per_doc_max = heading_bodies.groupby("doc_path")["char_count"].max().reset_index()
        big = per_doc_max[per_doc_max["char_count"] > section_p99].nlargest(10, "char_count")
        for _, row in big.iterrows():
            rows.append(
                {
                    "doc_path": row["doc_path"],
                    "reason": "large_heading_body",
                    "detail": f"{int(row['char_count'])} chars over p99={section_p99:.0f}",
                }
            )

    return pd.DataFrame(rows)


def _budget_str(budget: int | None) -> str:
    """Return a human-readable budget string, or a fallback when no budget qualifies."""
    return f"{budget} chars" if budget is not None else "no candidate budget qualifies"


def _render_fit_section(
    buf: "StringIO",
    regions: "pd.DataFrame",
    region_kind: str,
    section_title: str,
    description: str,
    threshold_heading: str,
) -> None:
    """Write a fit-share section (table + smallest-budget thresholds) into buf."""
    subset = regions[regions["region_kind"] == region_kind]
    fit = _fit_share_table(subset)
    buf.write(f"{section_title}\n\n")
    buf.write(f"{description}\n\n")
    buf.write(_format_fit_share(fit))
    buf.write("\n\n")
    buf.write(f"{threshold_heading}\n\n")
    for threshold in FIT_THRESHOLDS:
        budget = _smallest_budget_at_threshold(fit, threshold)
        buf.write(f"- ≥ {int(threshold * 100)}% fit: **{_budget_str(budget)}**\n")
    buf.write("\n")


def render_report(
    regions: pd.DataFrame,
    per_doc: pd.DataFrame,
    memory: dict[str, object],
) -> str:
    """Build the full markdown report from raw artifacts."""
    buf = StringIO()
    buf.write("# Chunking calibration findings\n\n")
    buf.write(f"- Corpus: {int(len(per_doc))} documents\n")
    buf.write(f"- Regions captured: {int(len(regions))}\n")
    buf.write(f"- Total wall-clock: {float(memory.get('total_elapsed_seconds', 0.0)):.1f}s\n\n")

    buf.write("## Region char_count deciles\n\n")
    char_table = _quantile_table(regions, group_col="region_kind", value_col="char_count")
    buf.write(_format_quantile_table(char_table))
    buf.write("\n\n")

    buf.write("## Region tiktoken_cl100k_count deciles\n\n")
    tok_table = _quantile_table(regions, group_col="region_kind", value_col="tiktoken_cl100k_count")
    buf.write(_format_quantile_table(tok_table))
    buf.write("\n\n")

    _render_fit_section(
        buf,
        regions,
        region_kind="heading_body",
        section_title="## Heading-body fit-share by char budget",
        description="How often a heading body fits whole (no paragraph split needed).",
        threshold_heading="### Smallest budget meeting heading-body fit threshold",
    )
    _render_fit_section(
        buf,
        regions,
        region_kind="paragraph",
        section_title="## Paragraph (block) fit-share by char budget",
        description="How often a single top-level block fits whole (sentence fallback fires when it does not).",
        threshold_heading="### Smallest budget meeting paragraph fit threshold",
    )

    buf.write("## Parse latency\n\n")
    successful = per_doc[per_doc["error"] == ""]
    latency_qs = _quantile_row(successful["parse_latency_ms"])
    buf.write("Per-document parse latency (ms):\n\n")
    for label, value in latency_qs.items():
        buf.write(f"- {label}: {value:.2f}\n")
    buf.write(f"\nTotal wall-clock: {float(memory.get('total_elapsed_seconds', 0.0)):.1f}s\n")
    buf.write(f"Successful parses: {int(len(successful))} / {int(len(per_doc))}\n\n")

    regression = _latency_regression(per_doc)
    buf.write("Linear fit `parse_latency_ms = slope * doc_size_chars + intercept`:\n\n")
    buf.write(f"- slope: {regression['slope_ms_per_char']:.6g} ms / char\n")
    buf.write(f"- intercept: {regression['intercept_ms']:.3f} ms\n")
    buf.write(f"- R²: {regression['r_squared']:.4f}\n\n")

    mem = _memory_summary(memory)
    buf.write("## Memory\n\n")
    buf.write(f"- peak tracemalloc: {mem['peak_tracemalloc_mb']:.1f} MiB ({mem['peak_tracemalloc_bytes']} bytes)\n")
    buf.write(f"- peak RSS ({mem['platform']}): {mem['peak_rss_mb']:.1f} MiB ({mem['peak_rss_bytes']} bytes)\n\n")

    outliers = _flag_outliers(per_doc, regions)
    buf.write("## Outliers flagged for inspection\n\n")
    if outliers.empty:
        buf.write("_None_\n")
    else:
        buf.write(outliers.to_markdown(index=False))
        buf.write("\n")

    return buf.getvalue()


def _build_arg_parser() -> argparse.ArgumentParser:
    """Build the CLI argument parser."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--in",
        dest="in_dir",
        type=Path,
        default=_DEFAULT_IN_DIR,
        help="Input directory containing regions.parquet, per_doc_metrics.parquet, memory.json.",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="Output path for the markdown report (default: stdout).",
    )
    parser.add_argument("--log-level", default="INFO", help="Logging level (default: INFO).")
    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint."""
    setproctitle("chunking-calibration-analyze")
    args = _build_arg_parser().parse_args(argv)
    logging.basicConfig(
        level=args.log_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    in_dir: Path = args.in_dir.resolve()
    regions_path = in_dir / "regions.parquet"
    per_doc_path = in_dir / "per_doc_metrics.parquet"
    memory_path = in_dir / "memory.json"
    for path in (regions_path, per_doc_path, memory_path):
        if not path.is_file():
            logger.error("missing input artifact: %s", path)
            return 2

    regions = pd.read_parquet(regions_path)
    per_doc = pd.read_parquet(per_doc_path)
    memory = json.loads(memory_path.read_text(encoding="utf-8"))

    report = render_report(regions, per_doc, memory)
    if args.out is None:
        sys.stdout.write(report)
    else:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(report, encoding="utf-8")
        logger.info("wrote report to %s", args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
