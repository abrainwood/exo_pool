"""Unit tests for the outage-reconnect verification harness.

Covers only the logic that doesn't require a live container: log-line
parsing, backoff-growth assertion, and teardown ordering. The harness's
docker/HA-API orchestration is exercised by a real run only (see the
script's module docstring).
"""
from __future__ import annotations

import importlib.util
import pathlib
import subprocess
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


def test_strip_ansi_codes_removes_colour_escapes():
    coloured = "\x1b[36m2026-09-10 12:14:00.123\x1b[0m WARNING MQTT reconnect attempt 1\n"

    stripped = harness.strip_ansi_codes(coloured)

    assert stripped == "2026-09-10 12:14:00.123 WARNING MQTT reconnect attempt 1\n"


def test_filter_log_lines_since_excludes_stale_timestamp():
    log_text = (
        "2026-04-21 05:16:44.000 INFO (MainThread) stale line from months ago\n"
        "2026-09-10 12:14:00.500 WARNING (MainThread) MQTT reconnect attempt 1\n"
    )

    filtered = harness.filter_log_lines_since(log_text, "2026-09-10T12:00:00Z")

    assert "stale line from months ago" not in filtered
    assert "MQTT reconnect attempt 1" in filtered


def test_filter_log_lines_since_carries_continuation_lines_with_their_entry():
    log_text = (
        "2026-04-21 05:16:44.000 ERROR (MainThread) stale traceback\n"
        "Traceback (most recent call last): stale continuation\n"
        "2026-09-10 12:14:00.500 ERROR (MainThread) current traceback\n"
        "Traceback (most recent call last): current continuation\n"
    )

    filtered = harness.filter_log_lines_since(log_text, "2026-09-10T12:00:00Z")

    assert "stale continuation" not in filtered
    assert "current continuation" in filtered


def test_container_logs_since_raises_loudly_when_log_file_unreadable():
    def fake_runner(*args, **kwargs):
        return subprocess.CompletedProcess(
            args=args, returncode=1, stdout="", stderr="cat: /config/home-assistant.log: No such file"
        )

    container = harness.Container("ha-exo-pool-dev", runner=fake_runner)

    with pytest.raises(harness.HaLogUnavailableError, match="/config/home-assistant.log"):
        container.logs_since("2026-09-10T12:00:00Z")


def test_resolve_mqtt_entity_id_picks_the_matching_binary_sensor():
    entity_ids = ["sensor.exo_pool_temperature", "binary_sensor.exo_pool_mqtt_connected"]

    resolved = harness.resolve_mqtt_entity_id(entity_ids)

    assert resolved == "binary_sensor.exo_pool_mqtt_connected"


def test_resolve_mqtt_entity_id_lists_exo_candidates_when_no_match():
    entity_ids = ["sensor.exo_pool_temperature", "binary_sensor.other_thing"]

    with pytest.raises(harness.MqttEntityResolutionError, match="sensor.exo_pool_temperature"):
        harness.resolve_mqtt_entity_id(entity_ids)


def test_resolve_mqtt_entity_id_rejects_multiple_matches():
    entity_ids = ["binary_sensor.exo_pool_mqtt_connected", "binary_sensor.exo_pool2_mqtt_connected"]

    with pytest.raises(
        harness.MqttEntityResolutionError,
        match="binary_sensor.exo_pool_mqtt_connected.*binary_sensor.exo_pool2_mqtt_connected",
    ):
        harness.resolve_mqtt_entity_id(entity_ids)


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
