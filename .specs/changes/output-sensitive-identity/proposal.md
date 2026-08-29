# Proposal: output-sensitive-identity

## Intent

The 20JUN–27AUG2026 code review found that the admission layer's declared safety properties and its actual behavior diverge: capacity does not bound what the documentation claims, work-unit creation writes no durable event, and the console derives its counts with unbounded corpus scans.
Independently, derivation keys embed upstream run keys as propagation tokens, so an upstream regeneration that produces byte-identical data still invalidates downstream work and re-spends external inference.
Identity typing is inconsistent (`str` scope keys against `Uuid` identities), which forces those unbounded Python-side scans and blocks SQL anti-joins.
This change resolves all of it as one coherent overhaul: an explicit identity paradigm, output-sensitive derivation keys, admission and observability hardening, and a single rebuild cutover (delete the database, re-ingest, replay) that replaces every accumulated data-migration obligation.

## User Stories

### Story: avoid-redundant-inference

As the corpus owner, I want unchanged content to reuse its existing derived outputs when upstream artifacts regenerate, so that re-ingestion and reprocessing do not re-spend external inference on ideas already captured.

### Story: trustworthy-operator-view

As the operator, I want the console's counts, event trails, and capacity claims to reflect what the system actually did and will admit — at any corpus size — so that I can supervise backlog and spend without reading code.

### Story: rebuildable-corpus

As the corpus owner, I want the entire database to be rebuildable from raw inputs under explicit identity rules, so that recovery, re-ingestion, and schema evolution never strand the knowledge graph.

## Scope

**In scope** (capabilities in build-dependency order):

- `pipeline-identity` — the identity paradigm: four-tier identifier taxonomy (surrogate locators, semantic identities, derivation keys, content hashes); minting rules (`source_id` stays minted; cross-rebuild correlation is by source metadata and content hash); output-sensitive derivation keys (consumed data + stage-owned configuration, no upstream keys, no database-local ids); provenance vs. applicability as distinct records; reconciliation; the rebuild ("pave") contract; pending work derived from observable gaps, not completion handoffs.
- `pipeline-stage-runtime` — work-unit **creation** co-commits a durable event carrying origin and principal, in the creating transaction; enqueue primitives report whether they created or reused.
- `pipeline-work-admission` — capacity is backpressure over the actionable backlog, not a spend bound; admission passes are bounded per pass independent of the depth limit; operator status transitions are explicitly capacity-exempt; pending-work derivations bound their work, not just their results, to the requested limit.
- `graph-work-intake` — intake distinguishes created from reused units in its response; intake remains live independent of the admission enablement flags.
- `chunk-contextualization` — run and variant keys fingerprint the consumed inputs (summary text, ordered chunk content, `splitter_version`, owned configuration); an upstream regeneration with equivalent inputs reuses the active run via an applicability record and invokes no model call.
- `entity-extraction` — currentness derives from the ordered input payload and extraction-owned configuration, not from an embedded upstream run key; staleness is resolvable in bounded queries.
- `mention-store` — extraction runs record exact upstream provenance and applicability as stage-owned rows.
- `operator-console` — no delta: the baseline is already parameterized over stage-declared derivations ("SHALL match the stage's staleness derivation"), so exact bounded counts arrive through the admission and extraction deltas; console work is conformance only.
- `schema-migrations` — Alembic history is re-baselined (squashed to one revision matching the final ORM schema) at the pave boundary; version bumps to `0.2.0` to denote the break.
- Schema retypes riding the paradigm: `scope_id`, `graph_chunks.source_id`, `chunk_id`, `mention_id` (and `mention_id_lo`/`mention_id_hi`) become `Uuid`.
- Review remediation conforming to existing contracts (no delta): README capacity-claim correction, `StageDescriptor` pending-capability invariant, `enqueue_backfill` set-typed membership, retry/readmission `attempts` normalization, admission-adapter wiring resolution.
- The pave itself: archive the old database as the rollback path, start fresh on the single baseline migration, and smoke-validate propagation end-to-end; corpus repopulation is ordinary ingestion afterward, monitored through the console.

**Out of scope:**

- A generic DAG framework, engine-owned provenance model, or new orchestration service.
- Key aliases, permanent legacy-key parsing, or dual-format reads.
- In-place data migration of existing derived state (replaced by the pave).
- App-layer authentication, SSRF, or body-cap hardening (deferred separately).
- The Postgres peer backend and object-storage foundation work.
- Bookmark management inside aizk (KaraKeep remains the source).
- The open `keyterm-extraction-foundation` change (coordinated separately).
- Embedding parameters or retrieval scoring.

## Approach

Four phases, each landing green before the next; the single pave is the last act because every pave re-spends the full corpus inference cost.

1. **Admission & observability** — capacity-posture code and clauses, creation events via `(job, created)` returns with the event emitted inside the enqueue primitives, descriptor invariant, small fixes.
   No schema.
2. **Retype** — `scope_id → Uuid` plus the chunk/mention retype; the extraction pending derivation becomes a SQL anti-join with the limit pushed into the query; console counts become exact.
   Schema only; the pave carries the data.
3. **Output-sensitive identity** — all shipped key constructors route through the shared opaque-hash helper (`sha256` over canonical JSON, recorded in design, not in spec); stage-owned provenance and applicability tables; reconciliation that compares consumed-data fingerprints before invoking a producer; staleness rebuilt as applicability anti-joins.
4. **Re-baseline & pave** — squash Alembic to one revision, bump `0.2.0`, archive the old database, start fresh, smoke-validate a few ingested inputs end-to-end via the console.
   The smoke validation is the change's acceptance and executes the key scenarios on real data (fresh chunk minting, FTS writes, applicability path); repopulating the corpus is normal operation afterward.

Mechanism notes parked for `design.md`: per-pass admission ceiling reuses `MAX_BULK_SELECTION`; future non-source scopes mint namespace UUIDs (`uuid5`); applicability rows bind the summary and chunking runs as one input set; the migration/cleanup never invokes the splitter, an LLM, or NER.

## Schema Impact

The tracked API schemas (`conversion-api-openapi`, `graph-api-openapi`) are expected to be **unchanged**: intake already distinguishes 201 (created) from 200 (reused), and no request or response model changes.
The change is internal — database schema and derived-state semantics:

- New stage-owned provenance and applicability tables (extraction run input; contextualization and extraction applicability; chunking/summary conversion-output applicability).
- Column retypes to `Uuid`: `pipeline_runs.scope_id`, contextualization-output and FTS `scope_id`, `graph_chunks.source_id`, `chunk_id`, `mention_id` (+ `mention_id_lo`/`mention_id_hi`).
- `derivation_key` values become opaque fixed-length digests; a `derivation_key_format` version stamp is added.
- Alembic history squashed to a single baseline revision matching the final ORM schema.

Any observed diff in the tracked OpenAPI snapshots at verify time is unplanned and must be explained.
