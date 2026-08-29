# Delta for entity-extraction

## ADDED Requirements

### Requirement: Extraction is keyed by its consumed input payload

An extraction run's derivation key SHALL fingerprint the ordered input payload the run consumes — for each chunk in document order, the chunk's stable identity facts, the selected input kind, and the selected input text — together with the extraction-owned configuration: the extractor and materializer versions and the input policy.
The key SHALL NOT embed the selected upstream run's identity or derivation key.
When the selected upstream run changes but the ordered input payload and extraction-owned configuration are unchanged, the active extraction run SHALL be reusable through an applicability relation without invoking the extractor.
A change to any ordered input item or to the extraction-owned configuration SHALL change the key.

Serves: avoid-redundant-inference

#### Scenario: A superseded upstream with an unchanged payload is reused without extraction

- **GIVEN** an active extraction run and a superseding upstream run whose ordered chunk identities, input kinds, and input texts are byte-equivalent to what the extraction consumed
- **WHEN** the stage reconciles the source
- **THEN** the active extraction run is retained with an applicability relation to the new upstream, and the extractor is invoked zero times

#### Scenario: A changed input text changes the key

- **GIVEN** an active extraction run and current inputs in which any chunk's selected input text differs
- **WHEN** the extraction key for the current inputs is computed
- **THEN** it differs from the active run's key

#### Scenario: A changed input policy changes the key

- **GIVEN** an active extraction run and a changed extraction input policy
- **WHEN** the extraction key for the current inputs is computed
- **THEN** it differs from the active run's key

#### Scenario: Duplicate chunk text at distinct positions is distinct payload

- **GIVEN** a document containing two chunks with identical text at different positions
- **WHEN** the ordered input payload is fingerprinted
- **THEN** each position is a distinct ordered item, and removing or reordering one changes the key

#### Scenario: An input-kind change with identical text changes the key

- **GIVEN** a chunk whose selected input text is identical while its input kind flips between raw and contextualized (an empty variant replaced by a non-empty revision equal to the raw text)
- **WHEN** the extraction key for the current inputs is computed
- **THEN** it differs from the active run's key

## MODIFIED Requirements

### Requirement: A stale extraction is identifiable

> Previously: a source was stale whenever the upstream state its active extraction run consumed had been superseded, even when the superseding generation carried byte-identical content; the comparison resolved upstream derivation keys run by run and ignored extraction-configuration changes.

For any source with extraction output, the stage SHALL identify the source as stale when re-running extraction would read a different input payload than its active extraction run consumed, or when the currently-resolved extraction configuration differs from the configuration that produced the run.
A superseded upstream whose current input payload is byte-equivalent to what the run consumed, under unchanged extraction configuration, SHALL NOT make the source stale; such a source is reconciled by recording applicability, without extractor invocation.
Staleness SHALL be classifiable for the corpus from persisted state alone, without invoking any producer and without resolving upstream state run by run.
Staleness SHALL NOT make the source pending: no staleness condition triggers automatic re-admission.

Serves: avoid-redundant-inference, trustworthy-operator-view

#### Scenario: A re-chunked source is stale

- **GIVEN** a source extracted from a chunking generation later superseded by an active chunking run whose chunk content differs
- **WHEN** staleness is evaluated
- **THEN** the source is stale

#### Scenario: A newly contextualized source is stale

- **GIVEN** a source extracted from raw chunk text before an active contextualization run later provided variants for its chunks
- **WHEN** staleness is evaluated
- **THEN** the source is stale

#### Scenario: A current source is not stale

- **GIVEN** a source whose active extraction run consumed the source's current active upstream state
- **WHEN** staleness is evaluated
- **THEN** the source is not stale

#### Scenario: An equivalent regenerated upstream is not stale

- **GIVEN** a source whose upstream generation was superseded by one with a byte-equivalent input payload, reconciled by an applicability relation
- **WHEN** staleness is evaluated
- **THEN** the source is not stale

#### Scenario: An unchanged payload with changed extraction configuration is stale

- **GIVEN** a source whose active extraction run's input payload is unchanged while the currently-resolved extraction configuration differs
- **WHEN** staleness is evaluated
- **THEN** the source is stale

### Requirement: Re-admission of an extracted source is explicit and operator-initiated

> Previously: eligibility was limited to terminal work-units whose source was stale, and processing always performed a fresh extraction superseding the prior output.

The stage SHALL declare a re-admission action through which an operator requeues a source's extraction; after the action, the source SHALL have a queued extraction work-unit.
Eligibility SHALL be limited to work-units in a terminal status whose source is not current — stale or needs-reconciliation.
Processing a re-admitted source SHALL read the source's current active inputs and the currently-resolved extraction configuration: when the resulting candidate key equals the active run's key, processing SHALL record the applicability relation and succeed without invoking the extractor; when it differs, processing SHALL perform a fresh extraction superseding the prior output under the existing supersession contract.
Re-admission SHALL occur only through explicit operator action.

Serves: avoid-redundant-inference, trustworthy-operator-view

<!-- modified-removes: A non-stale unit is ineligible for re-admission -->

#### Scenario: A re-admitted stale source is re-extracted against current inputs

- **GIVEN** a stale source with a succeeded extraction work-unit
- **WHEN** an operator applies the re-admission action
- **THEN** the source has a queued extraction work-unit, and its processing reads the current active inputs and supersedes the prior output

#### Scenario: A re-admitted needs-reconciliation source records applicability without extraction

- **GIVEN** a needs-reconciliation source — its upstream regenerated with a byte-equivalent payload under unchanged configuration — with a succeeded extraction work-unit
- **WHEN** an operator applies the re-admission action
- **THEN** processing records the applicability relation to the current upstream, the active run is retained, and the extractor is invoked zero times

#### Scenario: A current source is ineligible for re-admission

- **GIVEN** a terminal extraction work-unit whose source is current
- **WHEN** re-admission is attempted on it
- **THEN** the unit is skipped as ineligible and its status is unaltered
