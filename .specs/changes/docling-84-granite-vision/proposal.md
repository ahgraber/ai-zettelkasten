# Proposal: docling-84-granite-vision

## Intent

Docling has released a series of improvements since we locked to `>=2.75`, and IBM released `granite-4.0-3b-vision` (March 27 2026) — a LoRA-based VLM tuned for structured extraction from document images (charts to CSV/summary, table images to HTML).
This change advances our Docling integration on three fronts:

1. **Bump the version floor to `>=2.84`** to pick up TableFormer v2 (structured table extraction),
   Unicode ligature normalization, PDF hyperlink propagation, and the DocumentFigureClassifier v2.5
   model.
2. **Add `granite-4.0-3b-vision` to the docling-serve deployment** so it is available as a local
   VLM option alongside the existing granite-vision-3.3-2b and Qwen models.
3. **Enable figure classification and task-tagged prompting** — enable `do_picture_classification=True`, then perform a post-conversion enrichment pass that issues task-specific prompts to the configured VLM (`<chart2summary>` for charts, `<tables_html>` for table images) rather than a single generic alt-text prompt for all figure types.
   Unclassified or non-chart/table figures fall back to the existing generic prompt.

The net effect: tables rendered as images in PDFs produce structured HTML rather than prose
descriptions; charts produce natural-language summaries optimized for note-taking; and we are
current with the Docling release stream.

## Scope

### In scope

- Bump `pyproject.toml` dependency: `docling[easyocr,vlm]>=2.84`
- Add `granite-4.0-3b-vision` to `notebooks/docling-serve/download-models.sh`
- Add `docling_enable_picture_classification: bool` config field (default `True`,
  env `DOCLING_ENABLE_PICTURE_CLASSIFICATION`)
- Set `do_picture_classification = config.docling_enable_picture_classification` in the PDF
  pipeline (was hardcoded `False`)
- Replace single-prompt picture description with a two-phase approach for PDF:
  - Phase 1: run conversion with `do_picture_description=False`, `do_picture_classification=True`
  - Phase 2: post-conversion enrichment loop — classify each `PictureItem`, select prompt
    (`<chart2summary>`, `<tables_html>`, or generic alt-text), call the configured VLM API
    directly, inject `PictureDescriptionData` annotations
- Extend `AnnotationPictureSerializer` to include the classification label in the
  `<!-- Figure Description -->` HTML comment block when a classification is present
- Add `docling_enable_picture_classification` to `ManifestConfigSnapshot` and the
  `build_output_config_snapshot` dict (automatically flows into `config_hash` and idempotency key
  via the `docling_` prefix convention)
- Update `.env.example` with the new env var
- Update unit tests: config snapshot contract test, hashing test, serializer test

### Out of scope

- Quality benchmarking granite-4.0 vs OpenRouter / generic VLM
- Headless browser HTML backend (v2.82 Playwright dependency)
- `VlmPipeline` full-page mode (separate architectural decision)
- Multi-language document support
- `<chart2csv>` or `<chart2code>` prompts (natural-language summary is sufficient for note-taking)
- HTML pipeline picture classification (TableFormer and the figure classifier are PDF-only)
- KServe / gRPC transport changes

## Approach

The post-conversion enrichment approach avoids wiring Docling's built-in single-prompt description while gaining classification labels.
After `DocumentConverter.convert()` returns, we iterate `doc.pictures`, inspect each `PictureItem.classifications` (produced by the classifier), select a prompt, and call the VLM via the existing `httpx`-backed API path (same endpoint/credentials as before).
The resulting text is injected as a `PictureDescriptionData` annotation so the downstream `AnnotationPictureSerializer` requires minimal changes — it already reads annotations and emits them as HTML comment blocks.

Because `_docling_config_payload` collects all fields prefixed `docling_` from `ConversionConfig`, adding `docling_enable_picture_classification` automatically enters the config hash and idempotency key with no changes to `hashing.py`.
The `ManifestConfigSnapshot` must be updated explicitly because it uses `extra="forbid"`.

TableFormer v2 is expected to be the default for `ThreadedPdfPipelineOptions` at `>=2.78`; we will
verify this during implementation and add an explicit `table_structure_options.mode` if needed.

## Open Questions

- **Classifier label vocabulary**: The DocumentFigureClassifier v2.5 label set is not documented publicly.
  Implementation must inspect actual `PictureClassificationData` labels at runtime and log them; the prompt-routing table should be lenient (default to generic alt-text for unknown labels).
- **Concurrency of enrichment loop**: The existing `ThreadedPdfPipelineOptions` uses batch sizes of 4.
  The post-hoc enrichment loop is currently sequential per figure.
  If figure count is large, this may be a bottleneck.
  Parallelism is a follow-up concern.
- **HTML pipeline classification**: `ConvertPipelineOptions` does not expose `do_picture_classification`.
  Confirm at implementation time whether enabling it for HTML is feasible; if not, enrichment applies PDF only.
