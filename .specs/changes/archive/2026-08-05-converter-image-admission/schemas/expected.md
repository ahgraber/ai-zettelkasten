# Expected Schema Diff: Converter Image Admission

## conversion-api-openapi

**Expected diff: none.**

This change touches only the conversion worker's document-preparation phase (`utilities/html_prefetch.py`), its converter configuration (`processing/converter.py`), and the removal of `utilities/docling_backend.py`.
None of these are reachable from `aizk.conversion.api.main.create_app`, and no request or response model changes.

At verify time, `schemas/after/conversion-api-openapi.json` is expected to be byte-identical to `schemas/before/conversion-api-openapi.json`.
Any difference indicates unintended API surface drift and should block the change.
