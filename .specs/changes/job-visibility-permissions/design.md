# Design: Job Visibility Permissions

## Context

The `owner_id` column is already persisted and indexed on `ConversionJob` (and `Source`), added by the deployment-trust-model change.
The `Principal` abstraction and `get_principal` dependency are already implemented; they run uniformly on mutation routes but are absent from the three read routes (`list_jobs`, `get_job`, `get_job_status_counts`).

In the current `trust_network` deployment every request resolves to the same `AIZK_DEFAULT_PRINCIPAL`, so ownership enforcement is a no-op: every job's `owner_id` equals `principal.subject` for every request.
No behavior visible to today's callers changes.
The value of this change is establishing the correct contract before a second principal exists.

The auth extensibility contract in `auth/__init__.py` documents a four-site expansion
path for new auth modes; this change does not touch any of those four sites.

## Decisions

### Decision: Wire `get_principal` to read routes now, not at auth-mode time

**Chosen:** Add `principal: Annotated[Principal, Depends(get_principal)]` to `list_jobs`,
`get_job`, and `get_job_status_counts` in this change, with the ownership filter active.

**Rationale:** The filter is a no-op in `trust_network` mode (single owner), so there is no behavioral risk.
Deferring to the auth-mode change couples two independent concerns and creates a window where a newly deployed auth mode has no enforcement.
Wiring now closes that window and makes the read routes structurally parallel to the mutation routes.

**Alternatives considered:**

- Defer to the auth-mode change: enforcement and wiring land together, but the
  auth-mode change becomes load-bearing for a security property it should not own.

---

### Decision: 404, not 403, on cross-owner access

**Chosen:** Return HTTP 404 with `error: job_not_found` when `job.owner_id != principal.subject`,
indistinguishable from a genuinely absent job id.

**Rationale:** This is an internal API with no authentication UI.
The 404 posture prevents callers from using the API as an oracle to enumerate job ids owned by other principals (probe for 403 → id exists, probe for 404 → id absent or cross-owner).
The debuggability cost is low because the only realistic multi-principal scenario in the near term is an operator running maintenance under a different principal name, not an end-user who needs meaningful error messages.

**Alternatives considered:**

- 403 Forbidden: truthful, aids debugging, but leaks job existence.
  Appropriate if the API gains an authentication UI where users distinguish "doesn't exist" from "no access."
  Revisit at that point.

---

### Decision: Owner-scope `status-counts` rather than keeping it global

**Chosen:** `GET /v1/jobs/status-counts` filters to `owner_id = principal.subject`.

**Rationale:** Consistent with the list endpoint.
In `trust_network` mode the result is unchanged.
A global counts view (for an ops dashboard) requires a concept of an admin principal that does not exist yet; building that concept into this endpoint now would be speculative.

**Alternatives considered:**

- Always global: breaks the ownership model for the counts surface; any principal
  can infer the system-wide queue depth regardless of what they own.
- `?scope=global` query param with permission check: cleaner long-term but requires a role or flag that does not exist.
  Defer to the change that introduces admin roles.

---

### Decision: No external policy library

**Chosen:** Inline `if job.owner_id != principal.subject: raise HTTPException(status_code=404, ...)` at each enforcement point.
Owner-scope list filter as a `.where()` clause.

**Rationale:** All checks are single-attribute ownership comparisons.
No role hierarchy, no delegation, no externalized policy management.
A library (Casbin, OPA, Oso, Cedar) adds a dependency, an ops surface, and a learning curve for zero expressive benefit at this complexity level.

**When to revisit:** Introduce a library when _any_ of the following become real requirements: (1) multiple principal roles with different visibility rules, (2) delegated access across principals, (3) runtime policy changes without redeploy.
Until then the inline pattern is the right call.

**Alternatives considered:**

- Casbin / OPA / Oso / Cedar: warranted for role hierarchies or externalized policy;
  over-engineered for ownership-only checks.

---

### Decision: Bulk actions use per-job ownership, surface cross-owner as `job_not_found`

**Chosen:** Each job in a bulk request is checked independently.
Cross-owner jobs return `status: "error", error: "job_not_found"` in the per-job result; owned jobs proceed normally.
The bulk operation is not aborted by cross-owner entries.

**Rationale:** Consistent with the 404 posture (existence not leaked) and with the existing `job_not_found` error path that already handles truly absent job ids the same way.
Partial success is already the design of the bulk endpoint; cross-owner jobs are just another failure mode in the same shape.

**Alternatives considered:**

- All-or-nothing rejection: simpler to reason about atomicity but breaks the existing
  partial-success contract and makes the bulk endpoint unusable if a stale job id list
  contains one cross-owner id.
- Distinct error code for cross-owner (e.g., `job_access_denied`): reveals existence,
  violates the 404 posture.

## Architecture

```text
Inbound request
      │
      ▼
get_principal (already runs on all routes after this change)
      │
      ├── GET /v1/jobs
      │     └── SELECT ... WHERE owner_id = :subject   ← filter added
      │
      ├── GET /v1/jobs/status-counts
      │     └── SELECT count(*) WHERE owner_id = :subject   ← filter added
      │
      ├── GET /v1/jobs/{id}
      │     └── get(id) → check owner_id == subject → 404 if mismatch
      │
      ├── POST /v1/jobs/{id}/retry
      │     └── get(id) → check owner_id == subject → 404 if mismatch → retry
      │
      ├── POST /v1/jobs/{id}/cancel
      │     └── get(id) → check owner_id == subject → 404 if mismatch → cancel
      │
      └── POST /v1/jobs/actions  (bulk)
            └── for each id:
                  get(id) → missing → error:job_not_found
                           → owner mismatch → error:job_not_found   ← same path
                           → owned → action → success
```

## Risks

- **trust_network behavior change perception:** The filter is a no-op today, but if `AIZK_DEFAULT_PRINCIPAL` is misconfigured across instances (e.g., two workers running with different principal values), existing jobs could become invisible.
  Mitigation: the config validation at startup already requires `AIZK_DEFAULT_PRINCIPAL` to be set; document that this value must be stable across restarts and deployments.

- **Future admin-role gap:** There is no mechanism for an operator to query all jobs across owners (e.g., for maintenance or debugging).
  Until an admin role or a separate ops endpoint exists, operators must query the database directly.
  Mitigation: acceptable for the current internal-only deployment; document the limitation.
