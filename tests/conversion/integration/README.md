# Integration Tests - Worker Lifecycle

These tests verify real subprocess lifecycle management with actual process spawning and signal handling.

**Platform Support**: Linux and macOS only. These tests rely on Unix-specific process group management (`os.setpgrp()`, `os.killpg()`) and signal handling which are not available on Windows.

## Safety Measures

Tests use **pytest-isolate** for complete process isolation:

1. **pytest-isolate**: Each test runs in an isolated subprocess. If the test crashes, signal mishandling occurs, or process cleanup fails, only the isolated subprocess dies—the main pytest process is completely protected.
2. **Custom marker**: Tests can be run selectively using the `integration_lifecycle` marker

## Running the Tests

### Install pytest-isolate

```bash
uv pip install pytest-isolate
```

### Run all integration lifecycle tests

```bash
pytest tests/conversion/integration/test_worker_lifecycle.py
```

### Run with marker

```bash
pytest -m integration_lifecycle
```

### Run specific test

```bash
pytest tests/conversion/integration/test_worker_lifecycle.py::test_real_subprocess_spawned_and_terminated
```

### Skip integration tests (run only unit tests)

```bash
pytest -m "not integration_lifecycle"
```

## How It Works

### 1. pytest-isolate (`@pytest.mark.isolate`)

Each test runs in an isolated subprocess managed by pytest-isolate. If anything goes wrong—including incorrect process group termination—only that isolated subprocess is affected. The main pytest process cannot be killed or crashed.

### 2. Real Process Testing

- Signal-based termination (`os.killpg()`)
- Zombie process prevention
- Timeout and cancellation behavior

## What These Tests Verify

- ✅ Real subprocesses are spawned and can be tracked
- ✅ Cancellation triggers proper SIGTERM/SIGKILL sequence
- ✅ Timeouts terminate subprocesses correctly
- ✅ No zombie processes remain after termination
- ✅ Process groups isolate worker processes from parent

## Troubleshooting

### Tests still crash pytest

1. Ensure pytest-isolate is installed: `uv pip install pytest-isolate`
2. Verify the `@pytest.mark.isolate` marker is present on the test module
3. If crashes still occur, file an issue with pytest-isolate (very unlikely—it's battle-tested)

### Tests hang

- Reduce timeout values in mocked work functions
- Check for infinite loops in subprocess code
- Verify subprocess is actually being terminated

### Tests pass but don't test real behavior

- Check that you're not over-mocking (e.g., mocking `os.killpg`)
- Verify subprocess PID tracking is working
- Use `psutil` to verify process states
