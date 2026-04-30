# Proposal: Job Visibility Permissions

## Intent

External security review found that `GET /v1/jobs` returns all jobs to any requestor with no ownership filter.
The `owner_id` column is already persisted and indexed on both `ConversionJob` and `Source` (added by the deployment-trust-model change), and a `Principal` is resolved on every request via `get_principal`.
However, the read routes (`GET /v1/jobs`, `GET /v1/jobs/{job_id}`, `GET /v1/jobs/status-counts`) do not inject the principal dependency, and no route enforces ownership at all — reads or mutations.

This change closes the gap: it wires `get_principal` uniformly to all job routes and
defines the enforcement contract for ownership-scoped visibility so that the policy is
executable the moment a multi-principal auth mode is deployed.

In the current `trust_network` deployment every request resolves to the same `AIZK_DEFAULT_PRINCIPAL`, so all jobs share a single owner and the filter is a no-op.
The value of this change is that the contracts are in place before the first second principal exists, not after.

## Scope

**In scope:**

- Add `get_principal` dependency to `GET /v1/jobs`, `GET /v1/jobs/{job_id}`, and
  `GET /v1/jobs/status-counts` so the principal runs uniformly on all job routes
- Spec the owner-scoped list contract: `GET /v1/jobs` SHALL filter to
  `owner_id = principal.subject` by default
- Spec the owner-check contract on `GET /v1/jobs/{job_id}`: return 404 (not 403)
  for cross-owner access to avoid leaking job existence
- Spec enforcement on mutation routes (retry, cancel, bulk): drop the
  `# noqa: ARG001` placeholders and add the ownership check
- Define what `GET /v1/jobs/status-counts` means in an owner-scoped world
- Document the library-vs-custom decision explicitly so future contributors know
  when that line should be revisited

**Out of scope:**

- Implementing a second auth mode (`token`, `proxy_headers`, `oidc`) — that is the
  four-site expansion described in `auth/__init__.py` and is a separate change
- Role-based access control (admin override, viewer role, cross-owner delegation)
- Exposing `owner_id` in the API request or response schemas
- Changing the `Source`-level ownership model
- Rate limiting or quota enforcement keyed on principal

## Approach

1. **Wire dependency uniformly.**
   Add `principal: Annotated[Principal, Depends(get_principal)]` to the three read handlers.
   Zero behavior change in `trust_network` mode.

2. **Owner-scope list and count.**
   `list_jobs` adds `WHERE owner_id = principal.subject`; `get_job_status_counts` adds the same filter.
   No new query parameter; the scope is implicit in the principal.

3. **Owner-check on get and mutations.** `get_job`, `retry_job`, `cancel_job`, and
   `bulk_job_actions` check `job.owner_id == principal.subject` and return 404 on mismatch.
   404 (not 403) to avoid leaking job existence to cross-principal callers.

4. **No external library.**
   Ownership checks are single-attribute comparisons; no role hierarchy or policy engine is warranted.
   Revisit only when multi-role requirements arrive (see Open Questions).

## Schema Impact

No schema changes — `owner_id` columns already exist.
No new API parameters or response fields.
The effective behavior of `GET /v1/jobs` changes (filtered result set) but the response schema is unchanged.
The OpenAPI schema before/after is identical.

## Open Questions

### Q1: Should `GET /v1/jobs/status-counts` be owner-scoped or always global?

The status-counts endpoint is used by the UI for a dashboard overview.

- **Option A — owner-scoped (matches list behavior):** Count only jobs where `owner_id = principal.subject`.
  Consistent.
  In `trust_network` mode the result is identical to today because there is one owner.
- **Option B — always global:** Keep counts unfiltered.
  Useful for ops dashboards that want a system-wide view.
  Requires a second auth mode with an admin concept to differentiate "who gets global view."
- **Option C — add a `?scope=global` query param with future permission check:** Defaults to owner-scoped; `?scope=global` is accepted only when the principal has an admin role.
  Defers the role question until roles exist.

**Tentative:** Option A (owner-scoped).
Revisit to Option C when an admin role is introduced.

### Q2: What is the right HTTP status for a cross-owner get/mutation?

- **Option A — 404:** Treats the resource as non-existent for this caller.
  Avoids leaking job existence.
  Standard practice for internal APIs without explicit auth UI.
- **Option B — 403:** Truthful about the reason for rejection.
  Leaks that the job ID exists.
  Appropriate when callers need to distinguish "doesn't exist" from "you're not allowed."

**Tentative:** 404.
This is an internal API; the leak risk outweighs the debuggability benefit.

### Q3: When should an external policy library be introduced?

Ownership checks (single attribute, no roles, no delegation) do not warrant a library.
The line should be crossed when **any** of the following become real requirements:

- Multiple principal roles with different visibility rules (e.g., admin sees all)
- Delegated access (principal A grants principal B visibility into their jobs)
- Externalized policy management (runtime policy changes without redeploy)

Until then, inline `if job.owner_id != principal.subject: raise HTTPException(404)`
is the right implementation — it is obvious, auditable, and requires no new dependency.

### Q4: Should bulk actions enforce per-job ownership or all-or-nothing?

`POST /v1/jobs/actions` accepts up to 100 job IDs.

- **Option A — per-job ownership, skip cross-owner jobs silently:**
  Each job is checked independently; cross-owner jobs are returned as `error: job_not_found`
  in the result, consistent with the existing `job_not_found` error path.
- **Option B — all-or-nothing:** If any job ID is cross-owner, reject the entire request
  with 403/404.
- **Option C — fail loudly on cross-owner:** Return an error result for cross-owner jobs
  that distinguishes them from truly not-found jobs (requires revealing existence, see Q2).

**Tentative:** Option A — per-job, surface as `job_not_found` (consistent with Q2's 404
posture and the existing error contract for bulk actions).
