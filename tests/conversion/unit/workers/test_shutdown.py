"""Unit tests for worker graceful shutdown."""

from __future__ import annotations

import signal

import pytest

from aizk.conversion.workers import shutdown


@pytest.fixture(autouse=True)
def _reset_shutdown_state():
    """Ensure clean shutdown state for each test."""
    shutdown.reset()
    yield
    shutdown.reset()


class TestShutdownEvent:
    def test_not_requested_initially(self):
        assert shutdown.is_shutdown_requested() is False

    def test_request_shutdown_sets_flag(self):
        shutdown.request_shutdown()
        assert shutdown.is_shutdown_requested() is True

    def test_immediate_shutdown_false_after_first_signal(self):
        shutdown.request_shutdown()
        assert shutdown.is_immediate_shutdown() is False

    def test_reset_clears_state(self):
        shutdown.request_shutdown()
        shutdown.reset()
        assert shutdown.is_shutdown_requested() is False
        assert shutdown.is_immediate_shutdown() is False


class TestSignalHandler:
    def test_first_signal_sets_shutdown_event(self):
        shutdown._handle_signal(signal.SIGTERM, None)
        assert shutdown.is_shutdown_requested() is True
        assert shutdown.is_immediate_shutdown() is False

    def test_second_signal_sets_immediate_shutdown(self):
        shutdown._handle_signal(signal.SIGTERM, None)
        shutdown._handle_signal(signal.SIGTERM, None)
        assert shutdown.is_immediate_shutdown() is True

    def test_sigint_behaves_like_sigterm(self):
        shutdown._handle_signal(signal.SIGINT, None)
        assert shutdown.is_shutdown_requested() is True

    def test_mixed_signals_count_toward_immediate(self):
        shutdown._handle_signal(signal.SIGTERM, None)
        shutdown._handle_signal(signal.SIGINT, None)
        assert shutdown.is_immediate_shutdown() is True


class TestRegisterSignalHandlers:
    def test_registers_both_signals(self, monkeypatch):
        registered = {}

        def _fake_signal(signum, handler):
            registered[signum] = handler
            return signal.SIG_DFL

        monkeypatch.setattr(signal, "signal", _fake_signal)
        shutdown.register_signal_handlers()

        assert signal.SIGTERM in registered
        assert signal.SIGINT in registered
        assert registered[signal.SIGTERM] is shutdown._handle_signal
        assert registered[signal.SIGINT] is shutdown._handle_signal
