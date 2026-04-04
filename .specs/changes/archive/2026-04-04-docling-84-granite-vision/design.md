# Design: docling-84-granite-vision

## Context

- The conversion pipeline builds a `DocumentConverter` once per job in a subprocess, converts the document, then serializes to Markdown.
  Picture description is configured at converter construction time via a single `PictureDescriptionApiOptions` object with one fixed prompt.
- `_docling_config_payload` auto-harvests all `docling_`-prefixed fields from `ConversionConfig`
  into the config hash; adding a new `docling_` field requires only the field declaration.
- `ManifestConfigSnapshot` uses `extra="forbid"` and has a contract test (`test_build_output_config_snapshot_matches_manifest_contract`) that asserts the runtime dict matches the Pydantic model's fields.
  Any new config field must be added to both.
- The `AnnotationPictureSerializer` is defined as a local class inside `_docling_to_markdown()`.
  It already reads `PictureDescriptionData` annotations; extending it to read `PictureClassificationData` is a contained change.
- The enrichment loop must encode its VLM calls within the existing MLflow span if tracing is
  enabled, using the same `trace_model_call` helper used for the current picture description path.

## Decisions

### Decision: Two-phase approach — classify then enrich, not Docling's built-in description

**Chosen:** When picture classification is enabled and a VLM endpoint is configured, set
`do_picture_description=False` in `ThreadedPdfPipelineOptions` and run a post-conversion
`_enrich_picture_descriptions()` loop that inspects classification labels and issues task-specific
prompts.

**Rationale:** Docling's built-in `PictureDescriptionApiOptions` sends a single prompt for every figure.
There is no per-figure prompt hook.
Two-phase avoids a wasteful generic call for every figure before the task-specific one.

**Alternative considered — keep built-in description, patch up in serializer:** Run Docling's description (generic prompt) then re-call the VLM in the serializer for classified figures.
Rejected because (a) it doubles VLM calls for chart/table figures, (b) the serializer is a pure formatting step and adding network I/O there violates separation of concerns.

**Alternative considered — single generic prompt with classification label injected:** Keep `do_picture_description=True`, disable classification-based routing, and simply surface the classifier label in the serializer comment.
Rejected because the task-tagged prompts are the core value of granite-4.0-3b-vision; a generic prompt wastes the model's specialized capabilities.

**Fallback:** When `docling_enable_picture_classification=False`, the code path falls back to the
existing `do_picture_description=True` / `PictureDescriptionApiOptions` approach, preserving prior
behavior exactly.

### Decision: Classification-based prompt routing table defined in converter, not config

**Chosen:** A small `_LABEL_TO_PROMPT` dict in `converter.py` maps known classifier label prefixes to prompt strings.
Unknown labels fall through to the generic prompt.

**Rationale:** The DocumentFigureClassifier v2.5 label vocabulary is not published.
The routing table must be lenient — unknown labels must not raise errors.
A dict in code is easy to extend when labels are confirmed at integration time.
Making it config-driven would require a complex nested env-var structure for minimal benefit.

**Risk:** If the classifier emits label strings that differ from our expected values, all figures silently fall back to the generic prompt.
Mitigation: log the actual label string at DEBUG level for every figure so the vocabulary can be confirmed during first deployment.

### Decision: Enrichment loop is sequential, not parallel

**Chosen:** Figures are enriched one at a time in the post-conversion loop.

**Rationale:** VLM API calls are already rate-limited by the remote server.
The existing Docling threaded pipeline uses `ocr_batch_size=4` / `layout_batch_size=4` for GPU-bound work; picture description is network-bound and not GPU-local.
Sequential is safe and correct; parallelism is a follow-up if per-document figure counts prove large enough to matter.

### Decision: granite-4.0-3b-vision added to download-models.sh, not to docling-compose.yaml

**Chosen:** Add the model to `download-models.sh` alongside the existing granite-vision-3.3-2b entry.
Do not change the compose file's default model or endpoint config.

**Rationale:** The compose file specifies the container image and startup; model selection is done at runtime via `DOCLING_VLM_MODEL` env var pointing to the self-hosted endpoint.
Operators choose which model to serve; the download script ensures the weights are available.
Updating compose would hardcode a model choice for all deployments.

## Architecture

```text
convert_pdf() / convert_html()
│
├─ _create_document_converter(config)
│   └─ ThreadedPdfPipelineOptions
│       do_picture_classification = config.docling_enable_picture_classification
│       do_picture_description    = False   (when classification enabled + VLM configured)
│       do_picture_description    = True    (fallback: classification disabled)
│
├─ DocumentConverter.convert(source)   ← Docling runs layout + OCR + classifier
│
├─ _enrich_picture_descriptions(doc, config)   ← NEW, runs when classification + VLM active
│   for pic in doc.pictures:
│       label = _get_classification_label(pic)   # reads PictureClassificationData
│       prompt = _LABEL_TO_PROMPT.get(label, GENERIC_PROMPT)
│       text   = _call_vlm_api(pic_image, prompt, config)
│       pic.annotations.append(PictureDescriptionData(text=text))
│
├─ _docling_to_markdown(doc)
│   └─ AnnotationPictureSerializer.serialize(item)
│       reads PictureClassificationData  → emits <!-- Figure Type: <label> -->
│       reads PictureDescriptionData     → emits <!-- Figure Description --> block
│
└─ _extract_figures(doc, output_dir)
```

## Risks

**Label vocabulary mismatch:** The DocumentFigureClassifier v2.5 labels may not match the strings we route on.
Mitigation: log every label at DEBUG, default to generic prompt, and update the routing table once labels are confirmed empirically.

**Increased latency:** Two-phase approach adds a sequential VLM call per figure after conversion.
For PDFs with many figures this could be significant.
The existing `docling_picture_timeout` (180s) applies per call.
Mitigation: monitor per-job duration metrics in MLflow; parallelize enrichment loop if needed as a follow-up.

**Contract test breakage:** `test_build_output_config_snapshot_matches_manifest_contract` enforces that the runtime dict and `ManifestConfigSnapshot` fields match exactly.
Adding a new field without updating both will fail this test.
The test is the guard — implement field and model together.
