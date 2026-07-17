# Delta for conversion-ui

The `conversion-ui` capability retires with this change: the conversion service sheds its HTML UI, and every contract below is carried forward — generalized across stages — by the `operator-console` capability.

## REMOVED Requirements

### Requirement: Display job monitoring table

Removed because: absorbed by operator-console's "Display the task monitor for every registered stage" — the column set, the enriched-title-with-fallback contract, and the large-list load-time scenario carry over with conversion registered as a stage.

### Requirement: Filter and search jobs across the full job set

Removed because: absorbed by operator-console's requirement of the same name — full-set status filter and text search carry over, with conversion's KaraKeep and job-title fields as the stage-declared searchable identifiers.

### Requirement: Retry and cancel jobs via bulk actions

Removed because: absorbed by operator-console's requirement of the same name — bulk retry/cancel with the mixed-eligibility summary and the bulk-action time bound carry over, with eligibility judged by conversion's own action rules.
