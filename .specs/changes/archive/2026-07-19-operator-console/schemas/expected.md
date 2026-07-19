# Expected Schema Diff: operator-console

## conversion-api-openapi

**Removed paths** (the conversion service sheds its HTML UI):

- `GET /ui/jobs` — the HTML jobs page moves to the operator console app.
- `POST /ui/jobs/actions` — the HTML bulk-action endpoint moves to the console's task monitor.

**Removed component schema** (entailed by the action endpoint's removal):

- `Body_ui_job_actions_ui_jobs_actions_post` — the auto-generated form-body schema for the deleted `POST /ui/jobs/actions` endpoint.

**No other changes expected.**
All JSON API paths (`/v1/*`, `/healthz`, `/readyz`), their operations, parameters, and their component schemas are unchanged.
The root redirect (`GET /`) is already excluded from the schema and stays excluded; its target changes but that is not schema-visible.

The console app (the retitled graph operator app) is not schema-tracked; this change adds no new tracked schemas.
