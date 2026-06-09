# Delta for chunk-contextualization

## ADDED Requirements

### Requirement: A retry of a partially-completed contextualization attempt re-invokes the model only for outputs not already retained

For a work-unit whose prior attempt validated and durably retained a model output for the document summary and/or some of its chunk revisions and then failed before completing, a retry under unchanged derivation-key inputs and versions SHALL NOT re-invoke the model for any summary or chunk revision whose valid output was already retained, and SHALL invoke the model only for those not yet retained.
A retained output is scoped to its source (`aizk_uuid`): a retained output for one source SHALL NOT be reused for a different source, even where an identical derivation key would otherwise match — a case that arises for the document summary, whose derivation key does not itself embed the source.
On successful completion, the persisted generation SHALL be identical — by count, provenance, and derivation-key linkage — to the generation an uninterrupted run under the same inputs and versions would produce.
This guarantee is independent of how far the prior attempt progressed: when every output was already retained, a re-execution SHALL invoke the model zero times.

#### Scenario: A retry invokes the model only for the chunks not yet revised

- **GIVEN** a document whose first contextualization attempt validated and retained the document summary and the revisions for the first K of its N chunks, then failed before revising the remaining chunks
- **WHEN** the work-unit is retried under unchanged derivation-key inputs and versions
- **THEN** the model is invoked only for the N−K not-yet-revised chunks — not for the document summary and not for the first K chunks — and on success exactly one summary and one variant per chunk are persisted, matching an uninterrupted run by count, provenance, and derivation-key linkage

#### Scenario: A first-contextualization summary is reused across the retry so its revision inputs stay stable

- **GIVEN** a document with no prior active summary run, whose first attempt retained the summary and then failed during chunk revision
- **WHEN** the work-unit is retried under unchanged inputs
- **THEN** the model is not re-invoked for the summary, the persisted summary equals the one the first attempt retained, and the retry's chunk revisions are produced against that same summary

#### Scenario: Re-executing an already-completed generation invokes the model zero times

- **GIVEN** a document already contextualized to completion at a given derivation key and versions
- **WHEN** its work-unit is executed again with both unchanged
- **THEN** the model is invoked zero times, no new run and no duplicate summary or variant are created, and the work-unit reaches a succeeded terminal status

#### Scenario: Retained model work for one source is not reused for another

- **GIVEN** two distinct sources with byte-identical Markdown, where one source has validated and retained its document summary from a partial attempt
- **WHEN** the other source is contextualized under the same inputs and versions
- **THEN** the model is invoked for the other source's own summary rather than reusing the first source's retained summary

### Requirement: Only a valid model output is eligible for reuse across attempts

A model output that fails contextualization's validation (the summary-length or chunk-relative revision-length checks) SHALL NOT be retained for cross-attempt reuse; a subsequent retry SHALL re-invoke the model for it, so a transient invalid output cannot become a durable result that fails every retry.
An empty revision — the output the model emits when it judges a chunk already self-contained — is a valid output and SHALL be retained, so a retry SHALL NOT re-invoke the model for an already-self-contained chunk.

#### Scenario: An invalid output is re-invoked rather than replayed

- **GIVEN** a document whose first attempt produces a model revision for one chunk that fails the chunk-relative length validation, failing the attempt
- **WHEN** the work-unit is retried under unchanged inputs
- **THEN** the model is re-invoked for that chunk rather than the invalid output being reused, and if the retry's output for that chunk is valid the work-unit can reach a succeeded terminal status

#### Scenario: An already-self-contained chunk is not re-invoked on retry

- **GIVEN** a document whose first attempt validated and retained an empty (already-self-contained) revision for one chunk and then failed on a later chunk
- **WHEN** the work-unit is retried under unchanged inputs
- **THEN** the model is not re-invoked for the already-self-contained chunk, whose persisted variant on success is the empty revision

### Requirement: Intermediate model work is never observable as committed contextualization state

Model work retained across attempts is internal to contextualization and is not itself a contextualization run, document summary, or contextualized variant.
Retaining it SHALL NOT cause any contextualization run, document summary, or contextualized variant to become active or readable; the generation phase's own use of retained work to avoid re-invoking the model is not such exposure.
Until a contextualization attempt completes successfully, the active run, summary, and variants for the source SHALL remain those of its prior completed generation, or none if the source had no prior generation.

#### Scenario: A partial failure leaves no run, summary, or variant readable

- **GIVEN** a document with no prior contextualization generation, whose attempt obtains the summary and some chunk revisions and then fails before completing
- **WHEN** the source's active run, document summary, and contextualized variants are inspected
- **THEN** no contextualization run, summary, or variant is active or readable for the source

#### Scenario: A partial failure does not disturb a prior completed generation

- **GIVEN** a document with an active contextualization generation, whose re-contextualization under changed inputs obtains a new summary and some revisions and then fails before completing
- **WHEN** the source's active run, summary, and variants are inspected
- **THEN** they remain the prior completed generation's, unchanged from before the failed attempt
