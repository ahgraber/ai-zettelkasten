# Tasks: docling-84-granite-vision

## Group 1: Dependencies and Deployment

- [ ] **1.1** Bump `pyproject.toml` dependency to `docling[easyocr,vlm]>=2.84` and run
  `uv lock` to update the lockfile.

- [ ] **1.2** Add `granite-4.0-3b-vision` to `notebooks/docling-serve/download-models.sh`:
  download `ibm-granite/granite-4.0-3b-vision` with `huggingface-cli download` into
  `${MODELS_DIR}/ibm-granite--granite-4.0-3b-vision`, matching the existing entry style.

- [ ] **1.3** Verify TableFormer v2 is active: after bumping the dependency, confirm that `ThreadedPdfPipelineOptions` at `>=2.84` defaults to TableFormer v2.
  If an explicit `table_structure_options.mode` flag is required, set it in `_create_document_converter()`.

## Group 2: Configuration

- [ ] **2.1** Add `docling_enable_picture_classification: bool` field to `ConversionConfig`
  in `src/aizk/conversion/utilities/config.py`:

  - Default: `True`
  - `validation_alias="DOCLING_ENABLE_PICTURE_CLASSIFICATION"`

- [ ] **2.2** Add `docling_enable_picture_classification: bool` field to
  `ManifestConfigSnapshot` in `src/aizk/conversion/storage/manifest.py`:

  - Description: `"Whether figure classification and task-tagged prompting was active"`
  - Keep `extra="forbid"` intact.

- [ ] **2.3** Add `DOCLING_ENABLE_PICTURE_CLASSIFICATION=true` to `.env.example`.

## Group 3: Pipeline Options

- [ ] **3.1** In `_create_document_converter()` (`src/aizk/conversion/workers/converter.py`),
  replace the hardcoded `pdf_pipeline_opts.do_picture_classification = False` with:

  ```python
  pdf_pipeline_opts.do_picture_classification = config.docling_enable_picture_classification
  ```

- [ ] **3.2** In `_create_document_converter()`, make `do_picture_description` conditional:

  - When `config.docling_enable_picture_classification` is `True` **and** `picture_opts` is set:
    set `do_picture_description = False` (enrichment loop handles it)
  - Otherwise: preserve existing behavior (`do_picture_description = bool(picture_opts)`,
    `picture_description_options = picture_opts`)
  - Apply the same logic to both the PDF and HTML pipeline options.

## Group 4: Post-conversion Enrichment

- [ ] **4.1** Add `_LABEL_TO_PROMPT: dict[str, str]` module-level constant in `converter.py`
  mapping known classifier labels to prompt strings:

  - Chart-type labels (e.g. `"chart"`, `"bar_chart"`, `"line_chart"`, `"pie_chart"`) → `"<chart2summary>"`
  - Table-type labels (e.g. `"table"`) → `"<tables_html>"`
  - All others → use the existing generic alt-text prompt string
  - Add a comment noting the label vocabulary is empirically confirmed from
    DocumentFigureClassifier v2.5; log unknown labels at DEBUG.

- [ ] **4.2** Implement `_get_classification_label(pic: PictureItem) -> str | None` helper:

  - Iterate `pic.annotations`, find the first `PictureClassificationData`, return its top label
    string (or `None` if absent).
  - Log at DEBUG: `"Figure %s classification: %s"` so the actual label vocabulary is visible.

- [ ] **4.3** Implement `_call_vlm_api(image: PIL.Image.Image, prompt: str, config: ConversionConfig) -> str` helper:

  - Encode the image as base64 PNG.
  - POST to `{config.chat_completions_base_url}/chat/completions` with the configured model,
    API key, and prompt, using `httpx` with `timeout=config.docling_picture_timeout`.
  - Return the response content string; raise `DoclingError` (retryable) on HTTP/timeout error.

- [ ] **4.4** Implement `_enrich_picture_descriptions(doc: DoclingDocument, config: ConversionConfig) -> None`:

  - If not `config.is_picture_description_enabled()`: return immediately.
  - For each `pic` in `doc.pictures`:
    - Call `_get_classification_label(pic)` to get label
    - Resolve prompt from `_LABEL_TO_PROMPT` (default to generic alt-text)
    - Get `PIL.Image.Image` via `pic.get_image(doc)` (skip if `None`)
    - Call `_call_vlm_api(image, prompt, config)`
    - Append `PictureDescriptionData(text=result)` to `pic.annotations`
  - Wrap the entire loop in the `trace_model_call` context manager (same span name as current:
    `"llm.chat.completions.docling_picture_description"`) if MLflow tracing is enabled.

- [ ] **4.5** Call `_enrich_picture_descriptions(doc, config)` in `convert_pdf()` and
  `convert_html()` after `converter.convert()` returns and before `_docling_to_markdown()`.

## Group 5: Serializer

- [ ] **5.1** In `AnnotationPictureSerializer.serialize()`, before the existing annotation loop, check for a `PictureClassificationData` annotation on `item`.
  If present, prepend a `<!-- Figure Type: <label> -->` comment to `text_parts` (before the description block).

## Group 6: Startup Logging

- [ ] **6.1** In `src/aizk/conversion/utilities/startup.py`, add picture classification
  to the optional-feature summary:
  - Enabled when `config.docling_enable_picture_classification` is `True`
    **and** `config.is_picture_description_enabled()` is `True`
  - Disabled with reason `"DOCLING_ENABLE_PICTURE_CLASSIFICATION=false"` when the flag is off
  - Disabled with reason `"picture description not enabled"` when the VLM endpoint is absent
    (regardless of the classification flag)

## Group 7: Tests

- [ ] **7.1** Update `tests/conversion/unit/test_manifest.py`: add `docling_enable_picture_classification`
  to the fixture's `ManifestConfigSnapshot` and assert it round-trips through `generate_manifest`.

- [ ] **7.2** Update the contract test
  (`test_build_output_config_snapshot_matches_manifest_contract` in `test_hashing.py`):
  add `docling_enable_picture_classification` to the expected field set.

- [ ] **7.3** Add unit test for `_get_classification_label`: verify it returns the top label from
  a `PictureClassificationData` annotation and `None` when no annotation is present.

- [ ] **7.4** Add unit test for `_enrich_picture_descriptions` with a mocked VLM API:

  - Assert chart-labeled figure receives `<chart2summary>` prompt
  - Assert table-labeled figure receives `<tables_html>` prompt
  - Assert unclassified figure receives the generic prompt
  - Assert `PictureDescriptionData` is appended to each figure's annotations

- [ ] **7.5** Add unit test for `AnnotationPictureSerializer` with both
  `PictureClassificationData` and `PictureDescriptionData` present: assert
  `<!-- Figure Type: ... -->` appears before the description block in the output.

- [ ] **7.6** Add unit test for `AnnotationPictureSerializer` with no classification annotation:
  assert output is unchanged from current behavior (no `Figure Type` comment).
