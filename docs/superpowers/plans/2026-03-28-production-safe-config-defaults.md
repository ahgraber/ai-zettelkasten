# Production-Safe Config Defaults Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Change `api_reload` to default to `False` so production deployments are safe without explicit opt-out.

**Architecture:** Single-field default change in `ConversionConfig` with a test asserting the new default value.

**Tech Stack:** Python, pydantic-settings, pytest

---

## Task 1: Change `api_reload` default and add test

**Files:**

- Modify: `src/aizk/conversion/utilities/config.py:102`

- Modify: `tests/conversion/unit/test_config.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/conversion/unit/test_config.py`:

```python
def test_api_reload_defaults_to_false(monkeypatch):
    monkeypatch.delenv("API_RELOAD", raising=False)
    config = ConversionConfig(_env_file=None)
    assert config.api_reload is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/conversion/unit/test_config.py::test_api_reload_defaults_to_false -v`

Expected: FAIL — `assert True is False` (current default is `True`)

- [ ] **Step 3: Change the default**

In `src/aizk/conversion/utilities/config.py`, line 102, change:

```python
api_reload: bool = Field(default=True, validation_alias="API_RELOAD")
```

to:

```python
api_reload: bool = Field(default=False, validation_alias="API_RELOAD")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/conversion/unit/test_config.py -v`

Expected: All tests PASS, including `test_api_reload_defaults_to_false`

- [ ] **Step 5: Commit**

```bash
git add src/aizk/conversion/utilities/config.py tests/conversion/unit/test_config.py
git commit -m "fix(conversion/config): default api_reload to False for production safety

Development reload requires explicit opt-in via API_RELOAD=true.
Defaults api_reload to False; explicit opt-in required."
```
