"""Unit tests for the outage-reconnect verification harness.

Covers only the logic that doesn't require a live container: log-line
parsing, backoff-growth assertion, and teardown ordering. The harness's
docker/HA-API orchestration is exercised by a real run only (see the
script's module docstring).
"""
from __future__ import annotations

import importlib.util
import pathlib
import sys

import pytest

_REPO_ROOT = pathlib.Path(__file__).parent.parent
_SCRIPT_PATH = _REPO_ROOT / "scripts" / "verify_outage_reconnect.py"


def _load_harness_module():
    spec = importlib.util.spec_from_file_location("verify_outage_reconnect", _SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


harness = _load_harness_module()


def test_parse_retry_attempts_extracts_attempt_and_delay():
    log_text = (
        "2026-09-10 02:30:23 WARNING (MainThread) [custom_components.exo_pool.api] "
        "MQTT reconnect attempt 1 to a1zi08qpbrtjyq-ats.iot.us-east-1.amazonaws.com "
        "failed: ClientConnectorDNSError - retrying in ~30s\n"
    )

    attempts = harness.parse_retry_attempts(log_text)

    assert attempts == [harness.RetryAttempt(attempt=1, delay=30.0)]


def test_parse_retry_attempts_ignores_unrelated_log_lines():
    log_text = (
        "2026-09-10 02:30:20 INFO (MainThread) [custom_components.exo_pool.api] "
        "Retrying MQTT reconnect after 30s backoff\n"
        "2026-09-10 02:30:23 WARNING (MainThread) [custom_components.exo_pool.api] "
        "REST fallback poll - MQTT is disconnected\n"
        "2026-09-10 02:30:53 WARNING (MainThread) [custom_components.exo_pool.api] "
        "MQTT reconnect attempt 2 to a1zi08qpbrtjyq-ats.iot.us-east-1.amazonaws.com "
        "failed: ClientConnectorDNSError - retrying in ~60s\n"
    )

    attempts = harness.parse_retry_attempts(log_text)

    assert attempts == [harness.RetryAttempt(attempt=2, delay=60.0)]


def test_assert_growing_backoff_rejects_single_retry_then_silence():
    attempts = [harness.RetryAttempt(attempt=1, delay=30.0)]

    with pytest.raises(AssertionError):
        harness.assert_growing_backoff(attempts)


def test_assert_growing_backoff_rejects_flat_delay():
    attempts = [
        harness.RetryAttempt(attempt=1, delay=30.0),
        harness.RetryAttempt(attempt=2, delay=30.0),
        harness.RetryAttempt(attempt=3, delay=30.0),
    ]

    with pytest.raises(AssertionError):
        harness.assert_growing_backoff(attempts)


def test_assert_growing_backoff_accepts_three_growing_delays():
    attempts = [
        harness.RetryAttempt(attempt=1, delay=30.0),
        harness.RetryAttempt(attempt=2, delay=60.0),
        harness.RetryAttempt(attempt=3, delay=120.0),
    ]

    harness.assert_growing_backoff(attempts)


def test_best_effort_teardown_runs_actions_in_lifo_order():
    order = []
    teardown = harness.BestEffortTeardown()
    teardown.defer(lambda: order.append("hosts"))
    teardown.defer(lambda: order.append("firewall"))

    teardown.run()

    assert order == ["firewall", "hosts"]


def test_assert_dev_instance_url_rejects_default_ha_port():
    with pytest.raises(harness.NotDevInstanceError):
        harness.assert_dev_instance_url("http://localhost:8123")


def test_assert_dev_instance_url_accepts_dev_container_port():
    harness.assert_dev_instance_url("http://localhost:8125")


def test_best_effort_teardown_runs_all_actions_even_if_one_raises():
    order = []
    teardown = harness.BestEffortTeardown()
    teardown.defer(lambda: order.append("hosts"))

    def _failing_firewall_restore():
        order.append("firewall")
        raise RuntimeError("iptables not reachable")

    teardown.defer(_failing_firewall_restore)

    teardown.run()

    assert order == ["firewall", "hosts"]
