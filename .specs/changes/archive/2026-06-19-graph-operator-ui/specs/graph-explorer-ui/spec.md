# Delta for graph-explorer-ui

## ADDED Requirements

### Requirement: Search returns only active-generation content

For any search, results SHALL be drawn only from each source's active chunking-run chunks and active variant-run contextualized representations; content belonging to a superseded run SHALL NOT appear in results.

#### Scenario: A superseded chunk is not returned

- **GIVEN** a source re-chunked under a new generation, where a term appears only in a chunk that exists in the superseded run and not the active run
- **WHEN** an operator searches for that term
- **THEN** no result is returned for that superseded chunk

#### Scenario: The active chunk is returned

- **GIVEN** the same source, where a term appears in a chunk of the active run
- **WHEN** an operator searches for that term
- **THEN** that active chunk is returned

### Requirement: Search is type-filtered over raw and contextualized text

Search SHALL support restricting matches to the raw chunk text, to the contextualized representation, or to either.
For matching purposes, the contextualized representation of a self-contained chunk (one whose stored revision is empty) SHALL be its raw chunk text.

#### Scenario: A term present only after contextualization

- **GIVEN** a chunk whose raw text says "it" and whose contextualized representation resolves that to "scaled dot-product attention"
- **WHEN** an operator searches "scaled dot-product attention"
- **THEN** the chunk is returned under the "contextualized" and "either" filters and not under the "chunk" filter

#### Scenario: A term present only in the raw chunk

- **GIVEN** a non-self-contained chunk whose raw text contains a term that its contextualized revision rephrased away
- **WHEN** an operator searches that term
- **THEN** the chunk is returned under the "chunk" and "either" filters and not under the "contextualized" filter

#### Scenario: A self-contained chunk matches in the contextualized corpus by its raw text

- **GIVEN** a self-contained chunk (empty revision) whose raw text contains a term
- **WHEN** an operator searches that term under the "contextualized" filter
- **THEN** the chunk is returned, because its contextualized representation is its raw text

### Requirement: Search results are ordered by document relevance then document order

Results SHALL be ordered so that a document with greater relevance to the term precedes one with lesser relevance, and results within a single document SHALL follow that document's chunk order.

#### Scenario: More-relevant document first, chunk order within

- **GIVEN** document A in which the term occurs more often relative to its length than in document B, each with multiple matching chunks
- **WHEN** an operator searches that term
- **THEN** A's matching chunks are ordered ahead of B's, and within each document the matching chunks appear in document (chunk) order

### Requirement: Paired search results highlight the term where it appears

Each search result SHALL present both the raw chunk and its contextualized representation, marking the search term in whichever of the two contains it, so an operator can tell whether a match is in the original text or was introduced by contextualization.
A chunk that matches on both its raw and its contextualized text SHALL appear as a **single** result with both sides marked, not as two separate results.

#### Scenario: A contextualized-only match is marked on the contextualized side

- **GIVEN** a result whose term appears only in the contextualized representation
- **WHEN** the result is displayed
- **THEN** the term is marked in the contextualized text and not in the raw chunk

#### Scenario: A match present in both sides yields one result marked on both

- **GIVEN** a chunk whose term appears in both its raw text and its contextualized representation
- **WHEN** results are displayed
- **THEN** the chunk appears as a single result with the term marked on both the raw and the contextualized side, not as two results

### Requirement: Selecting a result opens its document at the chunk

Selecting a search result SHALL open the document browser for that result's document, positioned at the selected chunk, with the detail panel showing that chunk's contextualized representation.

#### Scenario: Selection navigates into the document browser

- **GIVEN** a search result for a chunk in a given document
- **WHEN** the operator selects it
- **THEN** the document browser for that document opens with that chunk selected and the detail panel showing its contextualized representation

### Requirement: The document browser lists a document's chunks in order with chunking facts

For a selected document, the browser SHALL list the active chunking run's chunks in document order, each showing its heading path, its character span in the source markdown, its character count, and whether it is self-contained.

#### Scenario: Chunks render in order with their chunking facts

- **GIVEN** a document with several persisted chunks in its active chunking run, at least one of which is self-contained
- **WHEN** an operator opens that document in the browser
- **THEN** the chunks are listed in document order, each showing heading path, span, and char count, and the self-contained chunk is marked as such

### Requirement: The detail panel shows the selected chunk's current contextualized representation

For the selected chunk, the detail panel SHALL show its current contextualized representation — the stored revision, or the raw chunk text when the revision is empty — presented distinctly from the raw chunk, together with its provenance: the producing variant run with its version and model profile, and the lineage to the document summary, the chunking generation, and the source markdown.

#### Scenario: A chunk with a non-empty revision

- **GIVEN** a selected chunk whose contextualized revision is non-empty
- **WHEN** its detail panel is shown
- **THEN** the panel shows the revision distinctly from the raw chunk, with the producing run/version/model and the lineage to summary, chunking generation, and source markdown

#### Scenario: A self-contained chunk

- **GIVEN** a selected chunk whose contextualized revision is empty
- **WHEN** its detail panel is shown
- **THEN** the panel shows the raw chunk text as the consumed representation, marked as self-contained, with the same provenance lineage

### Requirement: Search reflects all active content regardless of when it was persisted

Search results SHALL include matching active-generation content whether it was persisted before or after the content search index was established.
Establishing or rebuilding the index SHALL make every existing active chunk and contextualized representation searchable, so the index is a faithful, rebuildable projection of the active content rather than only of content written after it existed.

#### Scenario: Content persisted before the index existed is searchable

- **GIVEN** chunks and contextualized representations that were persisted before the content search index was established
- **WHEN** the index is established (or rebuilt) and an operator searches for a term contained in that pre-existing content
- **THEN** the matching chunk is returned

#### Scenario: A chunk superseded when the index was built but later active is searchable

- **GIVEN** a chunk that was not in the active generation when the index was established, then becomes part of a later active chunking run
- **WHEN** an operator searches for a term in that chunk
- **THEN** the chunk is returned, because the index reflects all committed chunks and currency is decided at query time

### Requirement: The UI surfaces contextualized content only from committed active records, never intermediate processing state

The contextualized representation shown and searched SHALL come only from the active variant run's committed contextualized-chunk records.
A contextualization attempt's retained intermediate model outputs SHALL NOT appear anywhere in the jobs, search, or explorer surfaces, and a chunk whose source has no active variant run SHALL have no contextualized representation in the UI, even when intermediate revision outputs have been retained for an in-progress or failed attempt.

#### Scenario: Retained intermediate outputs are invisible to the UI

- **GIVEN** a source mid-contextualization whose attempt has retained some revision outputs but has not produced an active variant run
- **WHEN** an operator searches and opens that source in the explorer
- **THEN** the source's raw chunks are present but no contextualized representation is shown for them, and none of the retained intermediate revisions appear in search, the explorer, or the job's drill-down

### Requirement: Search input is handled safely at the boundary

The search SHALL treat operator input as literal search terms, not as a query-expression language.
An empty or whitespace-only query SHALL yield an empty result set rather than an error or the entire corpus; input SHALL be bounded to a maximum length; and input containing characters significant to the underlying index query syntax SHALL be matched literally and SHALL never raise an error or fail the page.
Highlighting SHALL mark the operator's search terms in the results.

#### Scenario: Empty query yields an empty result set

- **GIVEN** the search box is empty or contains only whitespace
- **WHEN** the operator submits the search
- **THEN** an empty result set is shown, not an error and not the whole corpus

#### Scenario: Input with query-syntax characters is matched literally without error

- **GIVEN** a search term containing characters significant to the index query syntax (such as quotes, `*`, or boolean operators)
- **WHEN** the operator submits the search
- **THEN** the input is matched as literal text, the page does not error, and any literal match is returned with the term highlighted
