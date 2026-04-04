# Delta Spec: conversion-worker — docling-84-granite-vision

## MODIFIED: Requirement: Convert documents to Markdown and extract figures

Previous behavior: The PDF pipeline runs with `do_picture_classification=False` (hardcoded).
All figures are described with a single generic alt-text prompt via Docling's built-in `PictureDescriptionApiOptions` if a chat completions endpoint is configured.

New behavior: The PDF pipeline runs with `do_picture_classification` controlled by `DOCLING_ENABLE_PICTURE_CLASSIFICATION` (default: `True`).
When picture description is enabled, the pipeline sets `do_picture_description=False` and performs a post-conversion enrichment pass: each figure is inspected for its classification label, an appropriate prompt is selected, and the VLM API is called directly to inject a `PictureDescriptionData` annotation.

Prompt routing by classification label:

- Label resolves to chart type → `<chart2summary>` prompt
- Label resolves to table type → `<tables_html>` prompt
- Label absent, unrecognized, or photo/logo → generic alt-text prompt (existing text)

### Scenario: Chart figure described with chart2summary prompt

- **GIVEN** a PDF figure is classified as a chart type by DocumentFigureClassifier
- **WHEN** the post-conversion enrichment pass runs
- **THEN** the enrichment loop calls the VLM with a `<chart2summary>` prompt for that figure
- **AND** the resulting description is injected as a `PictureDescriptionData` annotation

#### Scenario: Table-image figure described with tables_html prompt

- **GIVEN** a PDF figure is classified as a table type by DocumentFigureClassifier
- **WHEN** the post-conversion enrichment pass runs
- **THEN** the enrichment loop calls the VLM with a `<tables_html>` prompt for that figure
- **AND** the resulting description is injected as a `PictureDescriptionData` annotation

#### Scenario: Unclassified or photo figure uses generic prompt

- **GIVEN** a PDF figure has no classification label, or is classified as photograph/logo/other
- **WHEN** the post-conversion enrichment pass runs
- **THEN** the enrichment loop calls the VLM with the existing generic alt-text prompt
- **AND** the resulting description is injected as a `PictureDescriptionData` annotation

#### Scenario: Picture classification disabled via config

- **GIVEN** `DOCLING_ENABLE_PICTURE_CLASSIFICATION=false`
- **WHEN** the PDF pipeline is configured
- **THEN** `do_picture_classification=False` and the enrichment pass falls back to the existing
  single-prompt Docling built-in description (no classification-based routing)

## MODIFIED: Requirement: Serialize Markdown output with figure annotations

Previous behavior: `AnnotationPictureSerializer` appends a `<!-- Figure Description -->` HTML
comment block for each `PictureDescriptionData` annotation, with no figure type label.

New behavior: When a `PictureClassificationData` annotation is present on the `PictureItem`,
the serializer includes the top classification label as a `<!-- Figure Type: <label> -->` comment
immediately before the description block, enabling downstream consumers to filter or route by
figure type.

### Scenario: Classification label included in serialized output

- **GIVEN** a `PictureItem` has both a `PictureClassificationData` and a `PictureDescriptionData` annotation
- **WHEN** the item is serialized to Markdown
- **THEN** the output contains `<!-- Figure Type: <label> -->` followed by the description block

#### Scenario: No classification label when classifier disabled

- **GIVEN** `do_picture_classification=False` and no `PictureClassificationData` annotation exists
- **WHEN** the item is serialized to Markdown
- **THEN** the output contains only the description block, unchanged from prior behavior

## MODIFIED: Requirement: Persist conversion config in the manifest

Previous behavior: `ManifestConfigSnapshot` captures `docling_pdf_max_pages`,
`docling_enable_ocr`, `docling_enable_table_structure`, `docling_vlm_model`,
`docling_picture_timeout`, and `picture_description_enabled`.

New behavior: `ManifestConfigSnapshot` additionally captures `docling_enable_picture_classification`
(bool), reflecting whether figure classification and task-tagged prompting was active for this
conversion.

### Scenario: Manifest captures picture classification flag

- **GIVEN** a conversion completes with `docling_enable_picture_classification=True`
- **WHEN** the manifest is written
- **THEN** the config snapshot section includes `"docling_enable_picture_classification": true`

## MODIFIED: Requirement: Create conversion jobs with idempotency protection

Previous behavior: The config hash component of the idempotency key includes all `docling_`-prefixed
config fields: `docling_pdf_max_pages`, `docling_enable_ocr`, `docling_enable_table_structure`,
`docling_vlm_model`, `docling_picture_timeout`.

New behavior: The config hash additionally includes `docling_enable_picture_classification`, because
enabling or disabling classification changes both which prompt is used per figure and what
annotation metadata appears in the Markdown output.

### Scenario: Key differs when picture classification enabled vs disabled

- **GIVEN** two conversion submissions for the same bookmark with identical other config
- **WHEN** one has `DOCLING_ENABLE_PICTURE_CLASSIFICATION=true` and the other `false`
- **THEN** the two submissions produce different idempotency keys and are treated as distinct jobs

## ADDED: Requirement: Load picture classification configuration from environment

The system SHALL expose `DOCLING_ENABLE_PICTURE_CLASSIFICATION` as a boolean environment variable
(default: `True`) that controls whether the DocumentFigureClassifier runs during PDF conversion and
whether the post-conversion enrichment pass performs classification-based prompt routing.

### Scenario: Classification enabled by default

- **GIVEN** `DOCLING_ENABLE_PICTURE_CLASSIFICATION` is not set
- **WHEN** the worker starts
- **THEN** the PDF pipeline runs with `do_picture_classification=True`

#### Scenario: Classification disabled via environment

- **GIVEN** `DOCLING_ENABLE_PICTURE_CLASSIFICATION=false`
- **WHEN** the worker starts
- **THEN** the PDF pipeline runs with `do_picture_classification=False` and the enrichment pass
  does not attempt classification-based routing

## MODIFIED: Requirement: Log optional feature status summary on startup

Previous behavior: Startup summary logs picture descriptions, MLflow tracing, and Litestream replication.

New behavior: Startup summary additionally logs whether picture classification is enabled or
disabled (and if disabled, the reason: config flag vs no picture description endpoint).

### Scenario: Picture classification enabled in startup summary

- **GIVEN** `DOCLING_ENABLE_PICTURE_CLASSIFICATION=true` and a chat completions endpoint is configured
- **WHEN** the process starts
- **THEN** the startup summary log entry lists picture classification as enabled

#### Scenario: Picture classification disabled due to config flag

- **GIVEN** `DOCLING_ENABLE_PICTURE_CLASSIFICATION=false`
- **WHEN** the process starts
- **THEN** the startup summary log entry lists picture classification as disabled with
  reason "DOCLING_ENABLE_PICTURE_CLASSIFICATION=false"

#### Scenario: Picture classification implicitly disabled due to no VLM endpoint

- **GIVEN** no chat completions endpoint is configured (picture description is disabled)
- **WHEN** the process starts
- **THEN** the startup summary log entry lists picture classification as disabled with
  reason "picture description not enabled"
