# Operator Console Specification

> Synced from change `operator-console` on 2026-07-19.

## Requirements

### Requirement: All operator HTML is served from the single console origin

The console SHALL be the only application serving operator HTML: every operator surface — the dashboard, the task monitor and its drill-downs, and the explorer — SHALL be reachable from the console's origin, and no pipeline service SHALL serve an operator HTML page.

#### Scenario: Every operator surface is reachable at the console origin

- **GIVEN** the console app is running
- **WHEN** an operator requests the dashboard, the task monitor for each registered stage, and the explorer
- **THEN** each surface is served from the console origin

#### Scenario: The conversion service serves no operator HTML

- **GIVEN** the conversion service is running
- **WHEN** a request targets its former HTML jobs page path
- **THEN** no HTML page is served

### Requirement: Every console page carries global navigation

Every console page SHALL present navigation linking every operator section — the dashboard, the task monitor, and the explorer — and SHALL mark the current section as active.

#### Scenario: Each page links every operator section

- **GIVEN** any console page
- **WHEN** an operator loads it
- **THEN** the page contains navigation links to the dashboard, the task monitor, and the explorer

#### Scenario: The current section is marked active

- **GIVEN** an operator is viewing a console section
- **WHEN** the page renders
- **THEN** that section's navigation entry is marked as the current one

### Requirement: Console routes enforce the operator API perimeter

Every console route — read views and mutation actions — SHALL resolve the request principal the pipeline JSON APIs require and SHALL be subject to the same trusted-host restriction.
A console route SHALL NOT be reachable under conditions where the corresponding JSON API route would be rejected.

#### Scenario: A console route is rejected on a host the API would reject

- **GIVEN** a request whose `Host` is outside the trusted-host allowlist
- **WHEN** it targets any console route
- **THEN** it is rejected by the same trusted-host restriction the JSON APIs enforce, rather than being served

#### Scenario: Console routes require the same principal as the API

- **GIVEN** the JSON APIs resolve a request principal on their routes
- **WHEN** a console route is served
- **THEN** it resolves the same principal, so the console is not a weaker perimeter than the APIs beside it

### Requirement: Console data access preserves each stage's principal-scoping contract

For any registered stage, the console's listings, counts, drill-downs, and actions SHALL apply the same principal-scoping the stage's own JSON API contract applies to the corresponding operation.
A stage whose API scopes data to the resolved principal SHALL be equally scoped in the console; a stage whose API defines no principal-scoping SHALL NOT gain or lose visibility through the console.

#### Scenario: Conversion rows owned by a foreign principal are absent

- **GIVEN** conversion jobs exist whose `owner_id` differs from the resolved principal's subject
- **WHEN** an operator loads the conversion monitor and the dashboard
- **THEN** the foreign-owned jobs are absent from the listing and from the counts, matching the conversion API's owner-scoped list and count contracts

#### Scenario: A foreign-owned unit is not found through the console

- **GIVEN** a conversion job whose `owner_id` differs from the resolved principal's subject
- **WHEN** an operator requests its drill-down or includes it in a bulk action
- **THEN** the drill-down responds not-found and the bulk action reports the unit as not found without failing the batch, matching the conversion API's cross-owner posture

### Requirement: Console inputs are validated at the boundary

The console SHALL validate operator input before touching any work-unit: an unknown stage key or unknown unit identifier SHALL yield a not-found response; an unrecognized action SHALL be rejected; an empty selection SHALL alter no unit and produce an informative response; and a bulk selection exceeding the configured maximum SHALL be rejected without the action being applied to any unit.

#### Scenario: Unknown stage key is not found

- **GIVEN** a request targeting a stage key absent from the registry
- **WHEN** it reaches the monitor, drill-down, or action route
- **THEN** the console responds not-found without querying any stage's data

#### Scenario: Unknown unit is not found

- **GIVEN** a drill-down request for a unit identifier that does not exist for the stage
- **WHEN** the console handles it
- **THEN** it responds not-found rather than rendering a fabricated unit

#### Scenario: Unrecognized action is rejected

- **GIVEN** a bulk-action submission naming an action the stage does not declare
- **WHEN** the console handles it
- **THEN** it is rejected and no unit's status is altered

#### Scenario: Empty selection alters nothing

- **GIVEN** a bulk-action submission with no units selected
- **WHEN** the console handles it
- **THEN** no unit is altered and the operator receives an informative summary

#### Scenario: Oversized selection is rejected atomically

- **GIVEN** a bulk-action submission selecting more units than the configured maximum
- **WHEN** the console handles it
- **THEN** the submission is rejected and no unit in the selection is altered

### Requirement: Display the task monitor for every registered stage

The console SHALL render a task monitor listing, for any registered stage, that stage's work-units — each showing its unit identifier, its stage-native status, attempt count, queued/started/finished times, and error code.
For each unit it SHALL display a human-readable title: the enriched `Source.title` for the unit's source when that is non-`NULL`, falling back to a stage-declared stable identifier for the unit's source when it is not.

#### Scenario: A graph stage's units render with generic lifecycle statuses

- **GIVEN** contextualization work-units exist
- **WHEN** an operator opens the task monitor for the contextualization stage
- **THEN** a table shows a row per unit with identifier, status, attempt count, queued/started/finished times, and error code

#### Scenario: Conversion units render with their native statuses

- **GIVEN** conversion jobs exist in native statuses such as `UPLOAD_PENDING` and `FAILED_PERM`
- **WHEN** an operator opens the task monitor for the conversion stage
- **THEN** each row displays the stage-native status, not a collapsed generic one

#### Scenario: Title shows the enriched source title when available

- **GIVEN** a unit whose source has `Source.title = "Attention Is All You Need"`
- **WHEN** an operator loads the monitor
- **THEN** the title for that row displays `"Attention Is All You Need"`

#### Scenario: Title falls back to the stage-declared identifier

- **GIVEN** a unit whose `Source.title` is `NULL`
- **WHEN** an operator loads the monitor
- **THEN** the title for that row displays the stage-declared fallback (the submit-time placeholder for conversion; the `source_id` for graph stages)

#### Scenario: Page loads within acceptable time for large unit lists

- **GIVEN** up to 1000 work-units exist for a stage
- **WHEN** the operator loads that stage's monitor
- **THEN** the page renders within 2 seconds

### Requirement: Filter and search jobs across the full job set

For any registered stage, the task monitor SHALL provide a status filter over the stage's native status vocabulary and a text search, both operating across all of that stage's work-units, not only those on the current page.
The text search SHALL match the unit identifier, the source identity, the source title, and each searchable identifier the stage declares.

#### Scenario: Filter by status spans the whole unit set

- **GIVEN** more units of a given status exist than fit on one page
- **WHEN** an operator filters by that status
- **THEN** the filtered results include matching units from beyond the current page and exclude units of other statuses

#### Scenario: Search by source title

- **GIVEN** a unit whose source title is `"Attention Is All You Need"`
- **WHEN** an operator searches for `"attention"`
- **THEN** that unit appears in the filtered results

#### Scenario: Search by a stage-declared identifier

- **GIVEN** a conversion job sourced from a KaraKeep bookmark
- **WHEN** an operator searches the conversion monitor for the bookmark's KaraKeep id
- **THEN** that job appears in the filtered results

#### Scenario: Search term matches no jobs

- **GIVEN** no unit's identifier, source identity, source title, or stage-declared identifier matches the term
- **WHEN** the operator submits the search
- **THEN** the table renders an empty result state rather than a stale or unfiltered list

### Requirement: Retry and cancel jobs via bulk actions

The monitor SHALL offer exactly the actions a stage declares, and no others.
For any declared action, the monitor SHALL allow an operator to select multiple work-units and apply it to all selected.
Eligibility SHALL be judged by the stage's own action rules; the monitor SHALL display a summary distinguishing the units the action was applied to from those skipped as ineligible, altering no ineligible unit's status.

#### Scenario: Retry selected failed jobs

- **GIVEN** an operator has selected one or more retryable failed units
- **WHEN** the operator submits the Retry bulk action
- **THEN** the selected units are returned to a queued state and a confirmation summary is displayed

#### Scenario: Cancel selected active jobs

- **GIVEN** an operator has selected one or more queued or running units
- **WHEN** the operator submits the Cancel bulk action
- **THEN** the system attempts cancellation on the selected units and displays a result summary

#### Scenario: Bulk action with mixed eligibility

- **GIVEN** a selection in which some units are eligible for the chosen action and some are not
- **WHEN** the operator submits the bulk action
- **THEN** the summary distinguishes applied units from those skipped as ineligible, and no ineligible unit's status is altered

#### Scenario: Stage-native eligibility is respected

- **GIVEN** a selection of conversion jobs including one in a native status outside conversion's cancellable set (such as `UPLOAD_PENDING`)
- **WHEN** the operator submits the Cancel bulk action
- **THEN** that job is skipped as ineligible with its status unaltered, while eligible jobs in the selection are cancelled

#### Scenario: Bulk action confirmed within acceptable time

- **GIVEN** an operator submits a retry or cancel bulk action
- **WHEN** the action completes
- **THEN** the result is displayed within 5 seconds of the action being submitted

#### Scenario: A stage declaring no actions offers none

- **GIVEN** a registered stage that declares no actions
- **WHEN** an operator views that stage's monitor
- **THEN** no action controls are offered and its action route accepts no action for that stage

#### Scenario: A stage's declared destructive action removes the selected units

- **GIVEN** the conversion stage, which declares a Delete action, and a selection of jobs in a deletable terminal status alongside one in an active status
- **WHEN** the operator submits the Delete bulk action
- **THEN** the terminal jobs and their conversion outputs are removed, the active job is skipped as ineligible with its status unaltered, and the summary distinguishes the two

### Requirement: Show the per-job stage drill-down

For a selected work-unit of any registered stage, the console SHALL show the unit's lifecycle event trail from the shared event log; the event trail SHALL be shown for every stage.
When the stage declares a detail section — the pipeline runs and/or produced artifacts associated with the unit's source — the drill-down SHALL show it alongside the trail.
The drill-down SHALL reflect the source's current state: a run or artifact that has not been produced SHALL be shown as absent rather than fabricated.

#### Scenario: A completed contextualization unit shows all three stage runs and its event trail

- **GIVEN** a contextualization unit whose source has completed chunking, summary, and contextualization
- **WHEN** an operator opens that unit's drill-down
- **THEN** the chunking, document-summary, and chunk-contextualization runs are shown with their lifecycle status, alongside the unit's lifecycle event trail ending in a succeeded event

#### Scenario: A unit that failed before contextualization shows the reached stages and the gap

- **GIVEN** a contextualization unit whose source was chunked but whose contextualization did not complete
- **WHEN** an operator opens that unit's drill-down
- **THEN** the chunking run is shown, the chunk-contextualization run is shown as absent, and the unit's failure event is surfaced from its event trail

#### Scenario: A conversion unit shows its produced output and event trail

- **GIVEN** a succeeded conversion job with a recorded conversion output
- **WHEN** an operator opens that job's drill-down
- **THEN** the conversion output is shown as the stage's produced artifact, alongside the job's lifecycle event trail

#### Scenario: An extraction unit shows its extraction run detail

- **GIVEN** an extraction unit whose source has an extraction run
- **WHEN** an operator opens that unit's drill-down
- **THEN** the extraction run is shown with its lifecycle status, alongside the unit's lifecycle event trail

#### Scenario: A stage without a declared detail section shows the trail alone

- **GIVEN** a registered stage that declares no detail section
- **WHEN** an operator opens one of its units' drill-down
- **THEN** the lifecycle event trail is shown without a fabricated detail section

### Requirement: Dashboard summarizes per-stage health

The console SHALL provide a dashboard listing, for every registered stage, its work-unit counts grouped by the generic work-unit lifecycle vocabulary.
Each stage-native status SHALL be counted under exactly one generic category — no unit dropped, none double-counted.
Within the failed category, the dashboard SHALL distinguish units awaiting retry from permanently failed units, so repairability is visible at a glance.

#### Scenario: Graph stage counts appear under their own lifecycle statuses

- **GIVEN** contextualization units exist across queued, running, and failed statuses
- **WHEN** an operator loads the dashboard
- **THEN** the contextualization row shows the count of units in each lifecycle category

#### Scenario: Conversion native statuses roll up without loss

- **GIVEN** conversion jobs exist in native statuses including `UPLOAD_PENDING`, `FAILED_RETRYABLE`, and `FAILED_PERM`
- **WHEN** an operator loads the dashboard
- **THEN** every job is counted under exactly one generic category, and the per-stage total equals the number of conversion jobs

#### Scenario: Conversion failed counts distinguish repairability

- **GIVEN** conversion jobs exist in both `FAILED_RETRYABLE` and `FAILED_PERM`
- **WHEN** an operator loads the dashboard
- **THEN** the conversion failed count distinguishes the awaiting-retry jobs from the permanently failed ones

#### Scenario: Graph-stage failed counts distinguish repairability

- **GIVEN** contextualization units exist in `FAILED` both with and without a pending retry wait
- **WHEN** an operator loads the dashboard
- **THEN** the stage's failed count distinguishes the awaiting-retry units from the permanently failed ones

### Requirement: A registered stage is operable without console modification

For any stage registered with the console, that stage SHALL appear in the dashboard and the task monitor — with listing, filtering, its declared actions, and its drill-down derived entirely from its registration — without modification to the console's routes or monitor templates.

#### Scenario: Registering a stage surfaces it across the console

- **GIVEN** a stage registered with the console through the registration seam
- **WHEN** an operator loads the dashboard and the task monitor
- **THEN** the stage appears in both, and its units can be listed, filtered, and drilled into, with no console route or template changed

### Requirement: Console actions are equivalent to the stage's own action pathways

For any registered stage, a retry or cancel applied through the console SHALL produce the same work-unit state transition and the same durable lifecycle event as the stage's own action pathway.

#### Scenario: A console retry equals the conversion API's retry

- **GIVEN** two equivalent conversion jobs in a retryable native status
- **WHEN** one is retried through the console and one through the conversion JSON API
- **THEN** both reach the same status with the same fields cleared and an equivalent durable requeue event recorded

#### Scenario: A console cancel equals the graph stage's cancel

- **GIVEN** two equivalent contextualization units in a cancellable status
- **WHEN** one is cancelled through the console monitor and one through the stage's own cancel pathway
- **THEN** both reach the same terminal status with an equivalent durable cancel event recorded
