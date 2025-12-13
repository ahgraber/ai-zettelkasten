#!/usr/bin/env python3
# /// script
# dependencies = ["boto3"]
# ///
"""Sample real markdown outputs from S3 and analyze for whitespace patterns.

Usage:
  uv run scripts/sample_whitespace_patterns.py [--batch-size 50] [--offset 0]

Reads S3 credentials from environment:
  S3_ENDPOINT_URL, S3_ACCESS_KEY_ID, S3_SECRET_ACCESS_KEY,
  S3_BUCKET_NAME, S3_REGION

Outputs JSON: list of documents with pattern counts and excerpts.
Documents are sampled stratified by pipeline (html/pdf) and diversity
of source URL to maximize coverage.
"""

import argparse
import json
import os
from pathlib import Path
import re
import sqlite3
import sys

# ── Whitespace pattern detectors ─────────────────────────────────────────────


def _split_code_blocks(text: str) -> list[tuple[bool, str]]:
    """Split text into (is_code, segment) pairs on triple-backtick fences."""
    parts = re.split(r"(```[^\n]*\n[\s\S]*?```)", text)
    result = []
    for i, part in enumerate(parts):
        result.append((i % 2 == 1, part))
    return result


def count_multi_spaces(text: str) -> int:
    """Count occurrences of 2+ consecutive spaces outside code blocks."""
    total = 0
    for is_code, segment in _split_code_blocks(text):
        if not is_code:
            # Also skip inline code
            prose = re.sub(r"`[^`\n]+`", "", segment)
            total += len(re.findall(r" {2,}", prose))
    return total


def count_excess_newlines(text: str) -> int:
    """Count runs of 3+ consecutive newlines."""
    return len(re.findall(r"\n{3,}", text))


def count_trailing_whitespace(text: str) -> int:
    """Count lines with trailing whitespace."""
    return sum(1 for line in text.splitlines() if line != line.rstrip())


def score_document(text: str) -> dict:
    """Return pattern counts and a composite complexity score."""
    multi_spaces = count_multi_spaces(text)
    excess_newlines = count_excess_newlines(text)
    trailing_ws = count_trailing_whitespace(text)
    has_code_blocks = bool(re.search(r"```", text))
    has_tables = bool(re.search(r"^\|", text, re.MULTILINE))
    score = multi_spaces * 2 + excess_newlines * 3 + trailing_ws
    return {
        "multi_spaces": multi_spaces,
        "excess_newlines": excess_newlines,
        "trailing_whitespace": trailing_ws,
        "has_code_blocks": has_code_blocks,
        "has_tables": has_tables,
        "score": score,
    }


def extract_excerpts(text: str, max_excerpts: int = 3) -> list[str]:
    """Extract the most whitespace-interesting excerpts (up to 20 lines each)."""
    excerpts = []

    # Multi-space excerpts: find paragraphs containing 2+ spaces
    for m in re.finditer(r"[^\n]*  +[^\n]*", text):
        start = text.rfind("\n", 0, m.start()) + 1
        end_pos = text.find("\n\n", m.end())
        end = end_pos if end_pos != -1 else min(m.end() + 300, len(text))
        excerpt = text[start:end].strip()
        if excerpt and len(excerpt) > 10:
            excerpts.append(excerpt[:800])
        if len(excerpts) >= max_excerpts:
            break

    # Excess newline excerpts
    if len(excerpts) < max_excerpts:
        for m in re.finditer(r"\n{3,}", text):
            start = max(0, m.start() - 200)
            end = min(len(text), m.end() + 200)
            excerpt = text[start:end].strip()
            if excerpt:
                excerpts.append(excerpt[:800])
            if len(excerpts) >= max_excerpts:
                break

    return excerpts[:max_excerpts]


# ── S3 access ─────────────────────────────────────────────────────────────────


def make_s3_client():
    """Create a boto3 S3 client from environment credentials."""
    import boto3
    from botocore.config import Config

    return boto3.client(
        "s3",
        endpoint_url=os.environ["S3_ENDPOINT_URL"],
        aws_access_key_id=os.environ["S3_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["S3_SECRET_ACCESS_KEY"],
        region_name=os.environ.get("S3_REGION", "us-east-1"),
        config=Config(signature_version="s3v4"),
    )


def fetch_markdown(s3_client, bucket: str, s3_key_full: str) -> str | None:
    """Download markdown from S3. s3_key_full is like 's3://bucket/uuid/output.md'."""
    # Strip s3://bucket/ prefix to get the object key
    prefix = f"s3://{bucket}/"
    key = s3_key_full[len(prefix) :] if s3_key_full.startswith(prefix) else s3_key_full.lstrip("/")

    try:
        resp = s3_client.get_object(Bucket=bucket, Key=key)
        return resp["Body"].read().decode("utf-8", errors="replace")
    except Exception:
        return None


# ── DB sampling ───────────────────────────────────────────────────────────────


def sample_rows(db_path: str, batch_size: int, offset: int) -> list[dict]:
    """Sample rows stratified by pipeline, diversified by domain.

    Returns list of {aizk_uuid, title, pipeline_name, markdown_key}.
    """
    conn = sqlite3.connect(db_path)
    c = conn.cursor()

    # Sample half html, half pdf, ordered randomly, with offset for batching
    half = batch_size // 2
    results = []

    for pipeline in ("html", "pdf"):
        c.execute(
            """
            SELECT aizk_uuid, title, pipeline_name, markdown_key
            FROM conversion_outputs
            WHERE pipeline_name = ?
            ORDER BY aizk_uuid  -- deterministic but pseudo-random by UUID
            LIMIT ? OFFSET ?
            """,
            (pipeline, half, offset),
        )
        for row in c.fetchall():
            results.append(
                {
                    "aizk_uuid": row[0],
                    "title": row[1] or "",
                    "pipeline": row[2],
                    "markdown_key": row[3],
                }
            )

    conn.close()
    return results


# ── Main ──────────────────────────────────────────────────────────────────────


def main():
    """Entry point: parse args, sample documents, and output results."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batch-size", type=int, default=50)
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--db", default="data/conversion_service.db")
    parser.add_argument("--min-score", type=int, default=0, help="Only include documents with score >= this")
    parser.add_argument(
        "--repr",
        action="store_true",
        help="Print excerpts as repr() strings for embedding in test files instead of JSON",
    )
    args = parser.parse_args()

    for var in ("S3_ENDPOINT_URL", "S3_ACCESS_KEY_ID", "S3_SECRET_ACCESS_KEY"):
        if not os.environ.get(var):
            print(f"ERROR: {var} not set", file=sys.stderr)
            sys.exit(1)

    bucket = os.environ.get("S3_BUCKET_NAME", "aizk")
    rows = sample_rows(args.db, args.batch_size, args.offset)
    print(f"Sampled {len(rows)} rows (offset={args.offset})", file=sys.stderr)

    s3 = make_s3_client()

    results = []
    for i, row in enumerate(rows):
        content = fetch_markdown(s3, bucket, row["markdown_key"])
        if content is None:
            print(f"  [{i + 1}/{len(rows)}] SKIP {row['aizk_uuid']} (fetch failed)", file=sys.stderr)
            continue

        patterns = score_document(content)
        if patterns["score"] < args.min_score:
            print(f"  [{i + 1}/{len(rows)}] score={patterns['score']:3d}  {row['title'][:60]}", file=sys.stderr)
            continue

        excerpts = extract_excerpts(content)
        result = {
            "aizk_uuid": row["aizk_uuid"],
            "title": row["title"],
            "pipeline": row["pipeline"],
            "markdown_key": row["markdown_key"],
            "char_count": len(content),
            "patterns": patterns,
            "excerpts": excerpts,
        }
        results.append(result)
        flag = "***" if patterns["score"] > 10 else "   "
        print(
            f"  [{i + 1}/{len(rows)}] {flag} score={patterns['score']:3d}  "
            f"sp={patterns['multi_spaces']:3d}  nl={patterns['excess_newlines']:2d}  "
            f"tr={patterns['trailing_whitespace']:3d}  "
            f"{row['pipeline']:4s}  {row['title'][:50]}",
            file=sys.stderr,
        )

    if args.repr:
        for result in results:
            header = f"# {result['title'][:70]} ({result['pipeline']}, score={result['patterns']['score']}, uuid={result['aizk_uuid']})"
            for excerpt in result["excerpts"]:
                print(header)
                print(repr(excerpt))
                print()
    else:
        print(json.dumps(results, indent=2))

    print(
        f"\nDone: {len(results)}/{len(rows)} documents analyzed, "
        f"{sum(1 for r in results if r['patterns']['score'] > 0)} have whitespace issues",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
