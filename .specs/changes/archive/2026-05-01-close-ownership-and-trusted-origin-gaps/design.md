# Design: close-ownership-and-trusted-origin-gaps

## Context

The current specs already establish three important facts:

- `owner_id` exists on `Source`, `ConversionJob`, and `ConversionOutput`
- job read and mutation routes are owner-scoped
- Source identity is shared by `source_ref_hash`, so later principals can reuse the
  same Source row while creating their own Jobs

The remaining review findings sit in the seams around that model:

- output routes still behave as if `output_id` alone is sufficient authorization
- duplicate-submission lookup still behaves as if `idempotency_key` is globally
  unique
- the KaraKeep carve-out is described as operator-trusted but not defined tightly
  enough to rule out string-prefix mistakes

This design keeps the existing trust model and data model intact, then tightens the
boundary conditions that were left implicit.

## Decisions

### Decision: Output authorization keys off `ConversionOutput.owner_id`

**Chosen:** output listing and artifact reads authorize against the Output row's
`owner_id`, not the shared Source row's `owner_id`.

**Rationale:** Source rows are deduplicated by `source_ref_hash` and intentionally
reused across principals.
If output access were keyed off `Source.owner_id`, the first submitter would
implicitly control all later principals' outputs for the same source, which is the
opposite of the Job/Output ownership model.
`ConversionOutput.owner_id` already exists precisely to carry the per-job owner
through worker materialization.

**Consequences:**

- `GET /v1/bookmarks/{aizk_uuid}/outputs` filters outputs by both `aizk_uuid` and
  `owner_id = principal.subject`
- `GET /v1/outputs/{output_id}/...` returns 404 when the row exists but
  `owner_id != principal.subject`
- a caller querying a shared `aizk_uuid` sees only their own outputs, which is the
  intended multi-principal behavior

---

### Decision: Keep the idempotency hash formula; scope uniqueness by owner

**Chosen:** preserve the existing `compute_idempotency_key()` payload and make the
duplicate-submission contract owner-scoped by lookup and uniqueness dimension:
`(owner_id, idempotency_key)`.

**Rationale:** the current key already captures the content identity and
output-affecting converter configuration.
Changing the key material would create unnecessary migration churn, complicate the
historical replay-idempotency story, and leak an authorization concern into what is
otherwise a pure content/config identity hash.
The missing piece is not the hash formula but the uniqueness boundary around it.

**Implementation consequence:** a same-owner replay returns the existing row; a
different owner submitting the same source/config reuses the Source row but creates a
distinct Job row.
To keep that guarantee concurrency-safe, the database uniqueness shape should become
composite on `(owner_id, idempotency_key)` rather than relying on route-level checks
alone.

**Alternatives considered:**

- Encode `owner_id` into the hash payload: workable, but it changes the meaning of
  the key itself and weakens continuity with the existing migration contract
- Keep global uniqueness and special-case cross-owner reads in the route: rejected;
  it leaks the first owner's row and fails under concurrent submissions

---

### Decision: KaraKeep trust is exact-origin plus asset-path, never string-prefix

**Chosen:** treat a URL as operator-trusted KaraKeep infrastructure only when all of
the following are true:

1. the candidate URL and configured `karakeep_base_url` have the same scheme
2. the same normalized hostname
3. the same effective port (explicit or default)
4. a path beginning with `/api/v1/assets/`

Anything else goes through the normal egress path.

**Rationale:** the carve-out exists so worker fetchers can talk to the operator's own
KaraKeep asset endpoints, which are often on private infrastructure.
That does not imply that every string sharing the same prefix is trusted.
Parsing and comparing origin components closes the lookalike-host hole while keeping
the intended private-network deployment working.

**Consequences:**

- `https://karakeep.example.com/api/v1/assets/123` qualifies
- `https://karakeep.example.com.evil.test/api/v1/assets/123` does not qualify
- `https://karakeep.example.com/api/v1/bookmarks/123` does not qualify
- same-origin non-asset URLs derived from external content are treated as ordinary
  outbound URLs and remain subject to the egress gate

## Architecture

```text
submit request
    │
    ├── compute idempotency_key(source_ref_hash, converter, config)
    ├── lookup existing job by (principal.subject, idempotency_key)
    └── if none:
          reuse or create Source by source_ref_hash
          create Job(owner_id=principal.subject, idempotency_key=...)

bookmark outputs request
    │
    └── SELECT outputs
          WHERE aizk_uuid = :aizk_uuid
            AND owner_id = :principal_subject

output artifact request
    │
    └── get output by id
          ├── missing -> 404
          ├── owner mismatch -> 404
          └── owner match -> serve manifest / markdown / figure

fetcher dereference
    │
    ├── parse candidate URL
    ├── exact-match KaraKeep origin + /api/v1/assets/ ?
    │      ├── yes -> KarakeepClient path (trusted infrastructure carve-out)
    │      └── no  -> egress_fetch_bytes / normal validation path
```

## Risks

- **Downgrade risk on multi-owner data:** once two owners can legitimately hold the
  same `idempotency_key`, downgrading back to a global-unique shape becomes
  conditional.
  The migration spec needs to make that explicit rather than pretending the old
  uniqueness model remains reversible.

- **Caller confusion on empty output lists:** owner-scoping `GET /v1/bookmarks/{aizk_uuid}/outputs`
  means a shared Source can exist while a given caller sees `[]`.
  That is expected and should be tested so it is not later "fixed" back into a leak.
