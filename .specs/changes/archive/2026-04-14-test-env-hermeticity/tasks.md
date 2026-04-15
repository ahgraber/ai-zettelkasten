# Tasks: Test Environment Hermeticity

## Harness

- [x] Add an autouse, session-scoped fixture to [tests/conversion/conftest.py](../../../tests/conversion/conftest.py) that sets `ConversionConfig.model_config["env_file"] = None` for the duration of the session.
- [x] Extend the same fixture to enumerate `ConversionConfig.model_fields`, collect each field's `validation_alias`, and `os.environ.pop(alias, None)` for every alias not in the harness allowlist (the set of vars that `set_test_env` explicitly sets).
  Restore original values at session teardown.
- [x] Define the harness allowlist co-located with `set_test_env` in the same conftest so the two fixtures stay in sync.
- [x] Verify the env-file path: the previously-failing `test_submit_job_idempotency_key_disables_picture_description_without_api_key` now passes on a workstation with a populated `.env` that overrides `DOCLING_PICTURE_DESCRIPTION_MODEL`.
- [x] Verify the shell-env path: with `DOCLING_PICTURE_DESCRIPTION_MODEL=something-custom` exported in the shell that launches pytest, the same test still passes.

## Direct-construction cleanup

- [x] Update all `ConversionConfig()` and `ConversionConfig(<kwargs>)` sites in [tests/conversion/unit/test_hashing.py](tests/conversion/unit/test_hashing.py) to pass `_env_file=None`.
- [x] Update the `ConversionConfig()` call in [tests/conversion/unit/test_s3_client.py](tests/conversion/unit/test_s3_client.py) to pass `_env_file=None`.
- [x] Update both `ConversionConfig(fetch_timeout_seconds=5)` calls in [tests/conversion/unit/test_fetcher.py](tests/conversion/unit/test_fetcher.py) to pass `_env_file=None`.
- [x] Update the `ConversionConfig()` call in [tests/conversion/unit/test_error_tracebacks.py](tests/conversion/unit/test_error_tracebacks.py) to pass `_env_file=None`.
- [x] Update the `ConversionConfig()` call in [tests/conversion/unit/test_litestream.py](tests/conversion/unit/test_litestream.py) to pass `_env_file=None`.
- [x] Update the `ConversionConfig()` call in [tests/conversion/unit/test_startup.py](tests/conversion/unit/test_startup.py) to pass `_env_file=None`.

## Regression coverage

- [x] Add unit tests in [tests/conversion/unit/test_conftest_hermeticity.py](../../../tests/conversion/unit/test_conftest_hermeticity.py) for the strip helper, the allowlist drift guard, and an adversarial scenario that introduces a stray `DOCLING_*` alias and asserts the helper would have removed it.

## Verification

- [x] Run the full conversion test suite on a workstation with a populated `.env` and confirm all tests pass.
- [x] Run the full conversion test suite with the local `.env` temporarily moved aside and confirm the same outcome — no test depends on `.env` presence or contents.
- [x] Run the full conversion test suite with a stray `DOCLING_*` variable exported in the shell and confirm the same outcome — no test depends on shell env-var state.
