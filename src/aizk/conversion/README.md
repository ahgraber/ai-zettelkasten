# Conversion package

The `aizk.conversion` package fetches external content, converts it to Markdown
via Docling, and uploads the result to S3.

## Egress policy

Every outbound network request made by this package flows through the egress gate in [`utilities/egress.py`](utilities/egress.py).
The gate enforces:

- **Deny-set classification** — all resolved IP addresses are checked against a deny list before any connection is opened.
  The primary check is `not address.is_global`; additional explicit checks cover ranges where Python's `ipaddress` stdlib coverage is inconsistent across minor versions (shared address space RFC 6598, cloud-metadata link-local `169.254.169.254`, NAT64 `64:ff9b::/96`, 6to4 `2002::/16`).
- **DNS deadline** — DNS resolution is capped at 2 seconds to prevent
  slow-resolver DoS.
- **Connection pinning** — the IP validated at egress-check time is the IP dialled by the HTTP transport.
  DNS does not run again inside `httpx`, closing the TOCTOU window between classification and connection.
- **Per-hop redirect re-validation** — each HTTP redirect hop is re-checked through the egress gate before the next connection opens.
  Scheme downgrades (`https → http`) are rejected.

All egress-policy failures raise a subclass of `EgressPolicyError` (defined in [`core/errors.py`](core/errors.py)).
These are classified non-retryable: a destination that violates policy will not pass on a retry.

See the [network-egress-policy design doc](../../../.specs/changes/network-egress-policy/design.md)
for full rationale and the IP classification library decision.

## Trust seam: conversion subprocess → parent uploader

Conversion runs inside a spawned subprocess.
When the subprocess finishes it writes two things to the shared workspace directory:

- `metadata.json` — declares `markdown_filename` and `figure_files`.
- The markdown file and any figure files at the declared paths.

The **parent uploader** ([`processing/uploader.py`](processing/uploader.py)) treats these as **untrusted** inputs from a subprocess that may have been compromised.
Before opening any path it passes the filename through `_assert_within(workspace, name)` ([`utilities/paths.py`](utilities/paths.py)), which:

1. Rejects filenames containing path separators (`/`, `\`) or the bare traversal
   component `..`.
2. Composes `workspace / name`, resolves the result (following any symlinks),
   and asserts the resolved path is still inside the workspace root.

Files are then opened with `O_NOFOLLOW` to eliminate the TOCTOU race between
the containment check and the actual open. `ELOOP` at open time is caught and
re-raised as `WorkspaceEscape`.

The Docling converter's local-fetch path is subject to the same containment gate
via the confined backend in [`utilities/docling_backend.py`](utilities/docling_backend.py).
