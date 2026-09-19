"""Unit tests for the outage-reconnect verification harness.

Covers only the logic that doesn't require a live container: log-line
parsing, backoff-growth assertion, and teardown ordering. The harness's
docker/HA-API orchestration is exercised by a real run only (see the
script's module docstring).
"""
from __future__ import annotations

import concurrent.futures
import importlib.util
import os
import pathlib
import re
import subprocess
import sys
from datetime import datetime, timezone

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


def test_matches_connection_interrupted_on_the_interrupt_warning():
    log_text = (
        "2026-09-10 12:14:00.500 WARNING (MainThread) [custom_components.exo_pool.mqtt_client] "
        "MQTT connection interrupted: AWS_ERROR_MQTT_TIMEOUT\n"
    )

    assert harness.matches_connection_interrupted(log_text) is True


def test_matches_connection_interrupted_ignores_the_watchdog_line():
    log_text = (
        "2026-09-10 12:17:00.500 WARNING (MainThread) [custom_components.exo_pool.mqtt_client] "
        "MQTT connection interrupted 180s ago with no resume - forcing reconnect\n"
    )

    assert harness.matches_connection_interrupted(log_text) is False


def test_matches_watchdog_forced_reconnect_on_the_watchdog_line():
    log_text = (
        "2026-09-10 12:17:00.500 WARNING (MainThread) [custom_components.exo_pool.mqtt_client] "
        "MQTT connection interrupted 180s ago with no resume - forcing reconnect\n"
    )

    assert harness.matches_watchdog_forced_reconnect(log_text) is True


def test_matches_watchdog_forced_reconnect_ignores_the_interrupt_warning():
    log_text = (
        "2026-09-10 12:14:00.500 WARNING (MainThread) [custom_components.exo_pool.mqtt_client] "
        "MQTT connection interrupted: AWS_ERROR_MQTT_TIMEOUT\n"
    )

    assert harness.matches_watchdog_forced_reconnect(log_text) is False


def test_find_entry_state_returns_the_matching_entry_state():
    entries = [
        {"entry_id": "aaa", "domain": "exo_pool", "state": "setup_retry"},
        {"entry_id": "bbb", "domain": "other", "state": "loaded"},
    ]

    assert harness.find_entry_state(entries, "aaa") == "setup_retry"


def test_find_entry_state_raises_when_entry_id_not_present():
    entries = [{"entry_id": "aaa", "domain": "exo_pool", "state": "loaded"}]

    with pytest.raises(RuntimeError, match="zzz"):
        harness.find_entry_state(entries, "zzz")


def test_harness_image_failure_message_names_the_error_and_says_fatal():
    message = harness.harness_image_failure_message("pull access denied for exo-pool-harness-tools")

    assert message.startswith("FATAL:")
    assert "pull access denied for exo-pool-harness-tools" in message


def test_recovery_failure_message_names_the_container_and_both_restart_paths():
    message = harness.recovery_failure_message("ha-exo-pool-dev")

    assert "docker restart ha-exo-pool-dev" in message
    assert "make restart" in message


def test_reload_entry_translates_a_request_timeout_into_reload_timed_out(monkeypatch):
    def _timing_out_request(*args, **kwargs):
        raise TimeoutError("timed out")

    monkeypatch.setattr(harness, "_ha_request", _timing_out_request)

    with pytest.raises(harness.ReloadTimedOut):
        harness.reload_entry("token", "entry123")


def test_reload_entry_translates_a_wrapped_url_error_timeout_too(monkeypatch):
    import urllib.error

    def _wrapped_timeout_request(*args, **kwargs):
        raise urllib.error.URLError(TimeoutError("timed out"))

    monkeypatch.setattr(harness, "_ha_request", _wrapped_timeout_request)

    with pytest.raises(harness.ReloadTimedOut):
        harness.reload_entry("token", "entry123")


def test_reload_entry_lets_a_non_timeout_url_error_propagate(monkeypatch):
    import urllib.error

    def _connection_refused_request(*args, **kwargs):
        raise urllib.error.URLError(ConnectionRefusedError("refused"))

    monkeypatch.setattr(harness, "_ha_request", _connection_refused_request)

    with pytest.raises(urllib.error.URLError):
        harness.reload_entry("token", "entry123")


def test_select_established_peer_ips_extracts_a_public_443_peer():
    ss_output = (
        "State  Recv-Q Send-Q  Local Address:Port   Peer Address:Port  Process\n"
        "ESTAB  0      0       172.17.0.3:52344      34.196.232.7:443\n"
    )

    peers = harness.select_established_peer_ips(ss_output)

    assert peers == ["34.196.232.7"]


def test_select_established_peer_ips_excludes_rfc1918_peers_on_the_matching_port():
    ss_output = (
        "State  Recv-Q Send-Q  Local Address:Port   Peer Address:Port  Process\n"
        "ESTAB  0      0       172.17.0.3:52344      34.196.232.7:443\n"
        "ESTAB  0      0       172.17.0.3:41230      192.168.65.1:443\n"
    )

    peers = harness.select_established_peer_ips(ss_output)

    assert peers == ["34.196.232.7"]


def test_select_established_peer_ips_excludes_loopback_peers_on_the_matching_port():
    ss_output = (
        "State  Recv-Q Send-Q  Local Address:Port   Peer Address:Port  Process\n"
        "ESTAB  0      0       127.0.0.1:52344       127.0.0.1:443\n"
    )

    with pytest.raises(RuntimeError):
        harness.select_established_peer_ips(ss_output)


def test_select_established_peer_ips_excludes_non_matching_port():
    ss_output = (
        "State  Recv-Q Send-Q  Local Address:Port   Peer Address:Port  Process\n"
        "ESTAB  0      0       172.17.0.3:52344      34.196.232.7:8123\n"
    )

    with pytest.raises(RuntimeError):
        harness.select_established_peer_ips(ss_output)


def test_select_established_peer_ips_dedupes_repeated_peers():
    ss_output = (
        "State  Recv-Q Send-Q  Local Address:Port   Peer Address:Port  Process\n"
        "ESTAB  0      0       172.17.0.3:52344      34.196.232.7:443\n"
        "ESTAB  0      0       172.17.0.3:52346      34.196.232.7:443\n"
    )

    peers = harness.select_established_peer_ips(ss_output)

    assert peers == ["34.196.232.7"]


def test_select_established_peer_ips_raises_a_clear_error_when_nothing_matches():
    ss_output = "State  Recv-Q Send-Q  Local Address:Port   Peer Address:Port  Process\n"

    with pytest.raises(RuntimeError, match="443"):
        harness.select_established_peer_ips(ss_output)


def test_select_established_peer_ips_error_includes_the_raw_ss_output():
    ss_output = (
        "State  Recv-Q Send-Q  Local Address:Port   Peer Address:Port  Process\n"
        "ESTAB  0      0       172.17.0.3:52344      192.168.65.1:8123\n"
    )

    with pytest.raises(RuntimeError, match="192.168.65.1:8123"):
        harness.select_established_peer_ips(ss_output)


def test_wait_for_healthy_precondition_returns_immediately_when_already_met():
    sleeps = []
    reloads = []

    peers = harness.wait_for_healthy_precondition(
        get_sensor_state=lambda: "on",
        get_peers=lambda: ["34.196.232.7"],
        reload=lambda: reloads.append(1),
        max_polls=3,
        sleep=sleeps.append,
    )

    assert peers == ["34.196.232.7"]
    assert sleeps == []
    assert reloads == []


def test_wait_for_healthy_precondition_reloads_once_then_succeeds():
    sensor_states = iter(["off", "off", "on"])
    peer_results = iter([[], [], ["34.196.232.7"]])
    reloads = []

    peers = harness.wait_for_healthy_precondition(
        get_sensor_state=lambda: next(sensor_states),
        get_peers=lambda: next(peer_results),
        reload=lambda: reloads.append(1),
        max_polls=2,
        sleep=lambda s: None,
    )

    assert peers == ["34.196.232.7"]
    assert reloads == [1]


def test_wait_for_healthy_precondition_raises_with_detail_when_never_met():
    with pytest.raises(harness.ScenarioFailure, match="off.*192.168.65.1:8123"):
        harness.wait_for_healthy_precondition(
            get_sensor_state=lambda: "off",
            get_peers=lambda: "no established peers on port 443 found to block; ss output was:\n192.168.65.1:8123",
            reload=lambda: None,
            max_polls=1,
            sleep=lambda s: None,
        )


def test_get_peers_with_retry_returns_immediately_when_already_present():
    sleeps = []

    peers = harness.get_peers_with_retry(get_peers=lambda: ["34.196.232.7"], attempts=3, sleep=sleeps.append)

    assert peers == ["34.196.232.7"]
    assert sleeps == []


def test_get_peers_with_retry_retries_then_succeeds():
    results = iter(["no established peers on port 443 found to block", [], ["34.196.232.7"]])
    sleeps = []

    peers = harness.get_peers_with_retry(
        get_peers=lambda: next(results), attempts=3, sleep=sleeps.append, retry_delay=3.0
    )

    assert peers == ["34.196.232.7"]
    assert sleeps == [3.0, 3.0]


def test_get_peers_with_retry_gives_up_after_all_attempts_with_detail():
    with pytest.raises(RuntimeError, match="192.168.65.1:8123"):
        harness.get_peers_with_retry(
            get_peers=lambda: "no established peers on port 443 found to block; ss output was:\n192.168.65.1:8123",
            attempts=2,
            sleep=lambda s: None,
        )


def test_precondition_met_true_when_sensor_on_and_peer_present():
    assert harness.precondition_met("on", ["34.196.232.7"]) is True


def test_precondition_met_false_when_peer_absent():
    assert harness.precondition_met("on", []) is False


def test_precondition_met_false_when_peer_check_failed():
    assert harness.precondition_met("on", None) is False


def test_precondition_met_false_when_sensor_off_despite_peer_present():
    assert harness.precondition_met("off", ["34.196.232.7"]) is False


def test_matches_connection_resumed_on_the_resume_line():
    log_text = (
        "2026-09-11 09:12:29.100 INFO (MainThread) [custom_components.exo_pool.mqtt_client] "
        "MQTT connection resumed (rc=0, session_present=False)\n"
    )

    assert harness.matches_connection_resumed(log_text) is True


def test_matches_resubscribe_failed_after_resume_on_the_resume_path_failure():
    log_text = (
        "2026-09-11 09:12:50.100 WARNING (MainThread) [custom_components.exo_pool.mqtt_client] "
        "All subscribes failed after reconnect - credentials may have expired\n"
    )

    assert harness.matches_resubscribe_failed_after_resume(log_text) is True


def test_matches_resubscribe_failed_after_resume_ignores_the_initial_connect_failure():
    log_text = (
        "2026-09-11 09:00:00.100 WARNING (MainThread) [custom_components.exo_pool.mqtt_client] "
        "All subscribes failed after connect - credentials may have expired\n"
    )

    assert harness.matches_resubscribe_failed_after_resume(log_text) is False


def test_matches_reconnect_failed_refreshing_on_the_forced_refresh_line():
    log_text = (
        "2026-09-11 09:12:50.200 WARNING (MainThread) [custom_components.exo_pool.api] "
        "MQTT reconnect failed - refreshing credentials\n"
    )

    assert harness.matches_reconnect_failed_refreshing(log_text) is True


def test_matches_reconnect_failed_refreshing_ignores_unrelated_lines():
    log_text = (
        "2026-09-11 09:12:50.200 WARNING (MainThread) [custom_components.exo_pool.api] "
        "REST fallback poll - MQTT is disconnected\n"
    )

    assert harness.matches_reconnect_failed_refreshing(log_text) is False


def test_matches_transport_reconnected_on_the_recovery_line():
    log_text = (
        "2026-09-11 09:12:52.400 INFO (MainThread) [custom_components.exo_pool.api] "
        "MQTT connected - REST fallback interval set to 1800s\n"
    )

    assert harness.matches_transport_reconnected(log_text) is True


def test_should_print_tick_true_on_the_first_tick():
    assert harness.should_print_tick(elapsed=0.0, last_print=None, print_interval=20.0) is True


def test_should_print_tick_false_before_the_interval_elapses():
    assert harness.should_print_tick(elapsed=10.0, last_print=0.0, print_interval=20.0) is False


def test_should_print_tick_true_after_the_interval_elapses():
    assert harness.should_print_tick(elapsed=25.0, last_print=0.0, print_interval=20.0) is True


def test_ip_block_set_add_issues_an_insert_rule():
    calls = []

    def fake_runner(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(args=argv, returncode=0, stdout="", stderr="")

    block_set = harness.IpBlockSet("ha-exo-pool-dev", harness.BestEffortTeardown(), runner=fake_runner)
    block_set.add("3.226.158.32")

    assert len(calls) == 1
    assert calls[0][-1] == "iptables -I OUTPUT -d 3.226.158.32 -p tcp -j DROP"


def test_ip_block_set_add_is_idempotent_for_an_already_blocked_ip():
    calls = []

    def fake_runner(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(args=argv, returncode=0, stdout="", stderr="")

    block_set = harness.IpBlockSet("ha-exo-pool-dev", harness.BestEffortTeardown(), runner=fake_runner)
    block_set.add("3.226.158.32")
    block_set.add("3.226.158.32")

    assert len(calls) == 1


def test_ip_block_set_top_up_only_adds_new_ips_and_returns_them():
    calls = []

    def fake_runner(argv, **kwargs):
        calls.append(argv[-1])
        return subprocess.CompletedProcess(args=argv, returncode=0, stdout="", stderr="")

    block_set = harness.IpBlockSet("ha-exo-pool-dev", harness.BestEffortTeardown(), runner=fake_runner)
    block_set.add("3.226.158.32")

    added = block_set.top_up(["3.226.158.32", "34.206.242.80"])

    assert added == ["34.206.242.80"]
    assert len(calls) == 2


def test_ip_block_set_teardown_removes_every_blocked_ip():
    calls = []

    def fake_runner(argv, **kwargs):
        calls.append(argv[-1])
        return subprocess.CompletedProcess(args=argv, returncode=0, stdout="", stderr="")

    teardown = harness.BestEffortTeardown()
    block_set = harness.IpBlockSet("ha-exo-pool-dev", teardown, runner=fake_runner)
    block_set.add("3.226.158.32")
    block_set.add("34.206.242.80")
    calls.clear()

    teardown.run()

    assert sorted(calls) == [
        "iptables -D OUTPUT -d 3.226.158.32 -p tcp -j DROP",
        "iptables -D OUTPUT -d 34.206.242.80 -p tcp -j DROP",
    ]


def test_ip_block_set_teardown_removes_the_rest_even_if_one_ip_fails():
    removed = []

    def fake_runner(argv, **kwargs):
        cmd = argv[-1]
        if "-D" in cmd and "34.206.242.80" in cmd:
            return subprocess.CompletedProcess(args=argv, returncode=1, stdout="", stderr="rule not found")
        if "-D" in cmd:
            removed.append(cmd)
        return subprocess.CompletedProcess(args=argv, returncode=0, stdout="", stderr="")

    teardown = harness.BestEffortTeardown()
    block_set = harness.IpBlockSet("ha-exo-pool-dev", teardown, runner=fake_runner)
    block_set.add("34.206.242.80")
    block_set.add("3.226.158.32")

    teardown.run()

    assert removed == ["iptables -D OUTPUT -d 3.226.158.32 -p tcp -j DROP"]


def test_unblock_port_or_abort_succeeds_immediately():
    calls = []
    aborted = []

    def fake_runner(argv, **kwargs):
        calls.append(argv[-1])
        return subprocess.CompletedProcess(args=argv, returncode=0, stdout="", stderr="")

    harness.unblock_port_or_abort(
        "ha-exo-pool-dev", 443, runner=fake_runner, sleep=lambda s: None, abort=aborted.append,
    )

    assert calls == ["iptables -D OUTPUT -p tcp --dport 443 -j DROP"]
    assert aborted == []


def test_unblock_port_or_abort_retries_then_succeeds():
    results = iter([1, 1, 0])
    sleeps = []
    aborted = []

    def fake_runner(argv, **kwargs):
        rc = next(results)
        return subprocess.CompletedProcess(args=argv, returncode=rc, stdout="", stderr="Resource busy")

    harness.unblock_port_or_abort(
        "ha-exo-pool-dev", 443, runner=fake_runner, attempts=3,
        sleep=sleeps.append, retry_delay=5.0, abort=aborted.append,
    )

    assert sleeps == [5.0, 5.0]
    assert aborted == []


def test_unblock_port_or_abort_aborts_with_the_restart_command_after_all_attempts_fail():
    def fake_runner(argv, **kwargs):
        return subprocess.CompletedProcess(args=argv, returncode=1, stdout="", stderr="Resource busy")

    aborted = []

    harness.unblock_port_or_abort(
        "ha-exo-pool-dev", 443, runner=fake_runner, attempts=2,
        sleep=lambda s: None, abort=aborted.append,
    )

    assert len(aborted) == 1
    assert "docker restart ha-exo-pool-dev" in aborted[0]
    assert "port-443" in aborted[0]


def test_block_port_total_outage_issues_the_insert_rule_and_verifies_undo_works():
    calls = []

    def fake_runner(argv, **kwargs):
        calls.append(argv[-1])
        return subprocess.CompletedProcess(args=argv, returncode=0, stdout="", stderr="")

    teardown = harness.BestEffortTeardown()
    harness.block_port_total_outage("ha-exo-pool-dev", teardown, port=443, runner=fake_runner)

    assert calls == [
        "iptables -I OUTPUT -p tcp --dport 443 -j DROP",
        "iptables -L -n",
    ]


def test_block_port_total_outage_teardown_removes_the_rule():
    calls = []

    def fake_runner(argv, **kwargs):
        calls.append(argv[-1])
        return subprocess.CompletedProcess(args=argv, returncode=0, stdout="", stderr="")

    teardown = harness.BestEffortTeardown()
    harness.block_port_total_outage("ha-exo-pool-dev", teardown, port=443, runner=fake_runner)
    calls.clear()

    teardown.run()

    assert calls == ["iptables -D OUTPUT -p tcp --dport 443 -j DROP"]


def test_block_port_total_outage_rolls_back_when_undo_verification_fails():
    calls = []

    def fake_runner(argv, **kwargs):
        cmd = argv[-1]
        calls.append(cmd)
        if cmd == "iptables -L -n":
            return subprocess.CompletedProcess(args=argv, returncode=1, stdout="", stderr="Operation not permitted")
        return subprocess.CompletedProcess(args=argv, returncode=0, stdout="", stderr="")

    teardown = harness.BestEffortTeardown()
    with pytest.raises(RuntimeError, match="rolled back"):
        harness.block_port_total_outage("ha-exo-pool-dev", teardown, port=443, runner=fake_runner)

    assert calls == [
        "iptables -I OUTPUT -p tcp --dport 443 -j DROP",
        "iptables -L -n",
        "iptables -D OUTPUT -p tcp --dport 443 -j DROP",
    ]


def test_block_mqtt_via_rest_allowlist_inserts_drop_then_accept_ahead_of_it():
    calls = []

    def fake_runner(argv, **kwargs):
        calls.append(argv[-1])
        return subprocess.CompletedProcess(args=argv, returncode=0, stdout="", stderr="")

    teardown = harness.BestEffortTeardown()
    harness.block_mqtt_via_rest_allowlist(
        "ha-exo-pool-dev", teardown, rest_ips=["45.60.157.189"], port=443, runner=fake_runner,
    )

    assert calls == [
        "iptables -I OUTPUT -p tcp --dport 443 -j DROP",
        "iptables -L -n",
        "iptables -I OUTPUT -d 45.60.157.189 -p tcp -j ACCEPT",
    ]


def test_block_mqtt_via_rest_allowlist_teardown_removes_accept_and_drop_rules():
    calls = []

    def fake_runner(argv, **kwargs):
        calls.append(argv[-1])
        return subprocess.CompletedProcess(args=argv, returncode=0, stdout="", stderr="")

    teardown = harness.BestEffortTeardown()
    harness.block_mqtt_via_rest_allowlist(
        "ha-exo-pool-dev", teardown, rest_ips=["45.60.157.189"], port=443, runner=fake_runner,
    )
    calls.clear()

    teardown.run()

    assert sorted(calls) == sorted([
        "iptables -D OUTPUT -d 45.60.157.189 -p tcp -j ACCEPT",
        "iptables -D OUTPUT -p tcp --dport 443 -j DROP",
    ])


def test_block_mqtt_via_rest_allowlist_still_tears_down_drop_when_accept_fails():
    calls = []

    def fake_runner(argv, **kwargs):
        calls.append(argv[-1])
        if argv[-1].endswith("-j ACCEPT"):
            return subprocess.CompletedProcess(args=argv, returncode=1, stdout="", stderr="rule exists")
        return subprocess.CompletedProcess(args=argv, returncode=0, stdout="", stderr="")

    teardown = harness.BestEffortTeardown()
    with pytest.raises(RuntimeError, match="ACCEPT"):
        harness.block_mqtt_via_rest_allowlist(
            "ha-exo-pool-dev", teardown, rest_ips=["45.60.157.189"], port=443, runner=fake_runner,
        )
    calls.clear()

    teardown.run()

    assert "iptables -D OUTPUT -p tcp --dport 443 -j DROP" in calls


def test_ensure_harness_tools_image_skips_build_when_already_present():
    calls = []

    def fake_runner(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(args=argv, returncode=0, stdout="", stderr="")

    tag = harness.ensure_harness_tools_image(runner=fake_runner)

    assert tag == harness.HARNESS_TOOLS_IMAGE
    assert calls == [["docker", "image", "inspect", harness.HARNESS_TOOLS_IMAGE]]


def test_ensure_harness_tools_image_builds_it_when_missing():
    calls = []

    def fake_runner(argv, **kwargs):
        calls.append(argv)
        if argv[:3] == ["docker", "image", "inspect"]:
            return subprocess.CompletedProcess(args=argv, returncode=1, stdout="", stderr="No such image")
        if argv[:2] == ["docker", "create"]:
            return subprocess.CompletedProcess(args=argv, returncode=0, stdout="builder123\n", stderr="")
        return subprocess.CompletedProcess(args=argv, returncode=0, stdout="", stderr="")

    tag = harness.ensure_harness_tools_image(runner=fake_runner, base_image="alpine:3.20", tag="exo-pool-harness-tools:latest")

    assert tag == "exo-pool-harness-tools:latest"
    assert calls == [
        ["docker", "image", "inspect", "exo-pool-harness-tools:latest"],
        ["docker", "create", "alpine:3.20", "sh", "-c", "apk add -q iptables iproute2"],
        ["docker", "start", "-a", "builder123"],
        ["docker", "commit", "builder123", "exo-pool-harness-tools:latest"],
        ["docker", "rm", "-f", "builder123"],
    ]


def test_ensure_harness_tools_image_stops_after_a_failed_create():
    calls = []

    def fake_runner(argv, **kwargs):
        calls.append(argv)
        if argv[:3] == ["docker", "image", "inspect"]:
            return subprocess.CompletedProcess(args=argv, returncode=1, stdout="", stderr="No such image")
        if argv[:2] == ["docker", "create"]:
            return subprocess.CompletedProcess(args=argv, returncode=1, stdout="", stderr="daemon unreachable")
        return subprocess.CompletedProcess(args=argv, returncode=0, stdout="", stderr="")

    with pytest.raises(RuntimeError, match="daemon unreachable"):
        harness.ensure_harness_tools_image(runner=fake_runner)

    assert [c[:2] for c in calls] == [["docker", "image"], ["docker", "create"]]


def test_build_netns_sidecar_cmd_shares_the_target_containers_network():
    argv = harness.build_netns_sidecar_cmd("ha-exo-pool-dev", "echo hi")

    assert argv == [
        "docker", "run", "--rm",
        "--network", "container:ha-exo-pool-dev",
        "--cap-add", "NET_ADMIN",
        harness.HARNESS_TOOLS_IMAGE, "sh", "-c", "echo hi",
    ]


def test_iptables_rule_shell_cmd_builds_the_insert_form():
    cmd = harness.iptables_rule_shell_cmd("10.0.0.5", "-I")

    assert cmd == "iptables -I OUTPUT -d 10.0.0.5 -p tcp -j DROP"


def test_iptables_rule_shell_cmd_builds_the_delete_form():
    cmd = harness.iptables_rule_shell_cmd("10.0.0.5", "-D")

    assert cmd == "iptables -D OUTPUT -d 10.0.0.5 -p tcp -j DROP"


def test_port_block_shell_cmd_builds_the_insert_form():
    cmd = harness.port_block_shell_cmd("-I", 443)

    assert cmd == "iptables -I OUTPUT -p tcp --dport 443 -j DROP"


def test_port_block_shell_cmd_builds_the_delete_form():
    cmd = harness.port_block_shell_cmd("-D", 443)

    assert cmd == "iptables -D OUTPUT -p tcp --dport 443 -j DROP"


def test_ensuring_the_image_upfront_lets_the_first_sidecar_call_of_a_run_succeed():
    build_calls = []

    def fake_runner(argv, **kwargs):
        if argv[:3] == ["docker", "image", "inspect"]:
            rc = 0 if build_calls else 1
            return subprocess.CompletedProcess(args=argv, returncode=rc, stdout="", stderr="No such image")
        if argv[:2] == ["docker", "create"]:
            build_calls.append(argv)
            return subprocess.CompletedProcess(args=argv, returncode=0, stdout="builder123\n", stderr="")
        if argv[:2] in (["docker", "start"], ["docker", "commit"], ["docker", "rm"]):
            return subprocess.CompletedProcess(args=argv, returncode=0, stdout="", stderr="")
        return subprocess.CompletedProcess(
            args=argv, returncode=0,
            stdout="State  Recv-Q Send-Q  Local Address:Port   Peer Address:Port  Process\n"
                   "ESTAB  0      0       172.17.0.3:52344      34.196.232.7:443\n",
            stderr="",
        )

    # main() builds the image once, before scenario_baseline's precondition -
    # the actual first sidecar call of a run - ever runs.
    harness.ensure_harness_tools_image(runner=fake_runner)

    peers = harness.get_established_peer_ips("ha-exo-pool-dev", runner=fake_runner)

    assert peers == ["34.196.232.7"]
    assert len(build_calls) == 1


def test_get_established_peer_ips_runs_ss_with_no_apk_install():
    calls = []

    def fake_runner(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(
            args=argv, returncode=0,
            stdout="State  Recv-Q Send-Q  Local Address:Port   Peer Address:Port  Process\n"
                   "ESTAB  0      0       172.17.0.3:52344      34.196.232.7:443\n",
            stderr="",
        )

    peers = harness.get_established_peer_ips("ha-exo-pool-dev", runner=fake_runner)

    assert peers == ["34.196.232.7"]
    assert calls[-1][-1] == "ss -tn state established"


class _FakeExecContainer:

    def __init__(self, name, result):
        self.name = name
        self._result = result
        self.calls = []

    def exec(self, cmd, **kwargs):
        self.calls.append(cmd)
        return self._result


def test_resolve_host_ips_parses_getent_ahosts_output():
    container = _FakeExecContainer(
        "ha-exo-pool-dev",
        subprocess.CompletedProcess(
            args=[], returncode=0,
            stdout="45.60.157.189   STREAM prod.zodiac-io.com\n45.60.157.189   DGRAM\n45.60.157.189   RAW\n",
            stderr="",
        ),
    )

    ips = harness.resolve_host_ips(container, "prod.zodiac-io.com")

    assert ips == ["45.60.157.189"]
    assert container.calls == [["getent", "ahosts", "prod.zodiac-io.com"]]


def test_resolve_host_ips_deduplicates_and_returns_every_resolved_address():
    container = _FakeExecContainer(
        "ha-exo-pool-dev",
        subprocess.CompletedProcess(
            args=[], returncode=0,
            stdout=(
                "45.60.157.189   STREAM prod.zodiac-io.com\n"
                "45.60.157.189   DGRAM\n"
                "104.20.1.1      STREAM\n"
                "104.20.1.1      DGRAM\n"
            ),
            stderr="",
        ),
    )

    ips = harness.resolve_host_ips(container, "prod.zodiac-io.com")

    assert ips == ["45.60.157.189", "104.20.1.1"]


def test_resolve_host_ips_excludes_ipv6_addresses():
    container = _FakeExecContainer(
        "ha-exo-pool-dev",
        subprocess.CompletedProcess(
            args=[], returncode=0,
            stdout="2606:4700::1  STREAM\n45.60.157.189   STREAM prod.zodiac-io.com\n",
            stderr="",
        ),
    )

    ips = harness.resolve_host_ips(container, "prod.zodiac-io.com")

    assert ips == ["45.60.157.189"]


def test_resolve_host_ips_raises_when_getent_fails():
    container = _FakeExecContainer(
        "ha-exo-pool-dev",
        subprocess.CompletedProcess(args=[], returncode=2, stdout="", stderr="not found"),
    )

    with pytest.raises(RuntimeError, match="prod.zodiac-io.com"):
        harness.resolve_host_ips(container, "prod.zodiac-io.com")


def test_check_net_admin_capable_true_when_sidecar_iptables_succeeds():
    def fake_runner(*args, **kwargs):
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")

    assert harness.check_net_admin_capable("ha-exo-pool-dev", runner=fake_runner) is True


def test_check_net_admin_capable_false_when_sidecar_iptables_fails():
    def fake_runner(*args, **kwargs):
        return subprocess.CompletedProcess(args=args, returncode=127, stdout="", stderr="sh: iptables: not found")

    assert harness.check_net_admin_capable("ha-exo-pool-dev", runner=fake_runner) is False


def test_check_net_admin_capable_builds_the_image_before_probing():
    calls = []

    def fake_runner(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(args=argv, returncode=0, stdout="", stderr="")

    assert harness.check_net_admin_capable("ha-exo-pool-dev", runner=fake_runner) is True
    assert calls[0] == ["docker", "image", "inspect", harness.HARNESS_TOOLS_IMAGE]
    assert calls[-1][-1] == "iptables -L -n"


def test_check_net_admin_capable_false_when_probe_fails_despite_image_present():
    def fake_runner(argv, **kwargs):
        if argv[:3] == ["docker", "image", "inspect"]:
            return subprocess.CompletedProcess(args=argv, returncode=0, stdout="", stderr="")
        return subprocess.CompletedProcess(args=argv, returncode=1, stdout="", stderr="Operation not permitted")

    assert harness.check_net_admin_capable("ha-exo-pool-dev", runner=fake_runner) is False


def test_resolve_swc_output_entity_id_picks_the_matching_number():
    entity_ids = ["number.exo_pool_swc_low_output", "number.exo_pool_swc_output"]

    resolved = harness.resolve_swc_output_entity_id(entity_ids)

    assert resolved == "number.exo_pool_swc_output"


def test_resolve_swc_output_entity_id_lists_exo_candidates_when_no_match():
    entity_ids = ["sensor.exo_pool_temperature", "number.other_thing"]

    with pytest.raises(harness.SwcOutputEntityResolutionError, match="sensor.exo_pool_temperature"):
        harness.resolve_swc_output_entity_id(entity_ids)


def test_resolve_swc_output_entity_id_rejects_multiple_matches():
    entity_ids = ["number.exo_pool_swc_output", "number.exo_pool2_swc_output"]

    with pytest.raises(
        harness.SwcOutputEntityResolutionError,
        match="number.exo_pool_swc_output.*number.exo_pool2_swc_output",
    ):
        harness.resolve_swc_output_entity_id(entity_ids)


def test_matches_write_via_rest_fallback_true_for_matching_key():
    log_text = (
        "2026-09-19 10:00:00.100 INFO (MainThread) [custom_components.exo_pool.api] "
        "Writing pool:swc via REST fallback\n"
    )

    assert harness.matches_write_via_rest_fallback(log_text, "pool:swc") is True


def test_matches_write_via_rest_fallback_false_for_a_different_key():
    log_text = (
        "2026-09-19 10:00:00.100 INFO (MainThread) [custom_components.exo_pool.api] "
        "Writing heating:sp via REST fallback\n"
    )

    assert harness.matches_write_via_rest_fallback(log_text, "pool:swc") is False


def test_matches_write_held_true_for_matching_key():
    log_text = (
        "2026-09-19 10:00:05.100 INFO (MainThread) [custom_components.exo_pool.api] "
        "Write pool:swc held behind cooldown: 42.0s remaining (post_write)\n"
    )

    assert harness.matches_write_held(log_text, "pool:swc") is True


def test_matches_write_held_false_for_a_different_key():
    log_text = (
        "2026-09-19 10:00:05.100 INFO (MainThread) [custom_components.exo_pool.api] "
        "Write heating:sp held behind cooldown: 42.0s remaining (post_write)\n"
    )

    assert harness.matches_write_held(log_text, "pool:swc") is False


def test_matches_write_woken_early_true_for_matching_key():
    log_text = (
        "2026-09-19 10:00:20.100 INFO (MainThread) [custom_components.exo_pool.api] "
        "Write pool:swc woken early by MQTT reconnect\n"
    )

    assert harness.matches_write_woken_early(log_text, "pool:swc") is True


def test_matches_write_woken_early_false_for_a_different_key():
    log_text = (
        "2026-09-19 10:00:20.100 INFO (MainThread) [custom_components.exo_pool.api] "
        "Write heating:sp woken early by MQTT reconnect\n"
    )

    assert harness.matches_write_woken_early(log_text, "pool:swc") is False


def test_call_service_posts_entity_id_and_extra_data(monkeypatch):
    calls = []

    def fake_ha_request(method, path, token, data=None, timeout=10.0):
        calls.append((method, path, token, data))

    monkeypatch.setattr(harness, "_ha_request", fake_ha_request)

    harness.call_service("tok", "number", "set_value", "number.exo_pool_swc_output", {"value": 46})

    assert calls == [
        ("POST", "/api/services/number/set_value", "tok", {"entity_id": "number.exo_pool_swc_output", "value": 46})
    ]


def test_call_service_uses_a_generous_timeout_for_a_write_that_falls_back_to_rest(monkeypatch):
    calls = []

    def fake_ha_request(method, path, token, data=None, timeout=10.0):
        calls.append(timeout)

    monkeypatch.setattr(harness, "_ha_request", fake_ha_request)

    harness.call_service("tok", "number", "set_value", "number.exo_pool_swc_output", {"value": 46})

    assert calls == [60.0]


def test_set_number_value_calls_the_number_set_value_service(monkeypatch):
    calls = []

    def fake_call_service(token, domain, service, entity_id, data=None):
        calls.append((token, domain, service, entity_id, data))

    monkeypatch.setattr(harness, "call_service", fake_call_service)

    harness.set_number_value("tok", "number.exo_pool_swc_output", 46)

    assert calls == [("tok", "number", "set_value", "number.exo_pool_swc_output", {"value": 46})]


class _FakeContainerLogs:
    def __init__(self, name: str, texts: list[str]):
        self.name = name
        self._texts = iter(texts)
        self._last = ""

    def logs_since(self, since_iso: str) -> str:
        try:
            self._last = next(self._texts)
        except StopIteration:
            pass
        return self._last


def test_wait_for_early_wake_or_fail_returns_when_pattern_appears_before_deadline():
    container = _FakeContainerLogs(
        "ha-exo-pool-dev",
        texts=[
            "no match yet\n",
            "Write pool:swc woken early by MQTT reconnect\n",
        ],
    )
    clock = [0.0]
    sleeps = []

    def fake_sleep(seconds):
        sleeps.append(seconds)
        clock[0] += seconds

    text = harness.wait_for_early_wake_or_fail(
        container, "2026-09-19T10:00:00Z", deadline=100.0, key="pool:swc",
        poll_interval=5.0, now=lambda: clock[0], sleep=fake_sleep,
    )

    assert "woken early" in text
    assert sleeps == [5.0]


def test_wait_for_early_wake_or_fail_rejects_a_write_that_only_waits_out_the_cooldown():
    container = _FakeContainerLogs(
        "ha-exo-pool-dev",
        texts=["no match yet\n"] * 10 + ["Writing pool:swc via REST fallback\n"] * 10,
    )
    clock = [0.0]

    def fake_sleep(seconds):
        clock[0] += seconds

    with pytest.raises(harness.ScenarioFailure, match="pool:swc"):
        harness.wait_for_early_wake_or_fail(
            container, "2026-09-19T10:00:00Z", deadline=10.0, key="pool:swc",
            poll_interval=5.0, now=lambda: clock[0], sleep=fake_sleep,
        )


class _ImmediateExecutor:
    def submit(self, fn, *args, **kwargs):
        future = concurrent.futures.Future()
        try:
            future.set_result(fn(*args, **kwargs))
        except Exception as exc:
            future.set_exception(exc)
        return future

    def shutdown(self, wait=True):
        pass


def _forced_held_write_fixture(monkeypatch, staged_logs, original_value=50):
    current = [original_value]
    calls = []
    swc_entity = "number.exo_pool_swc_output"

    def fake_ha_request(method, path, token, data=None, timeout=10.0):
        calls.append((method, path, data))
        if method == "GET" and path == f"/api/states/{swc_entity}":
            return {"state": str(current[0])}
        if method == "POST" and path == "/api/services/number/set_value":
            current[0] = data["value"]
            return None
        raise AssertionError(f"unexpected request {method} {path}")

    monkeypatch.setattr(harness, "_ha_request", fake_ha_request)
    monkeypatch.setattr(harness, "check_net_admin_capable", lambda name: True)
    monkeypatch.setattr(
        harness, "ensure_scenario_precondition", lambda *a, **k: ["34.196.232.7"]
    )
    monkeypatch.setattr(harness, "resolve_host_ips", lambda container, hostname: ["45.60.157.189"])

    def fake_block(container_name, teardown, rest_ips, port=443, **kwargs):
        calls.append(("block", tuple(rest_ips), None))
        teardown.defer(lambda: calls.append(("unblock", None, None)))

    monkeypatch.setattr(harness, "block_mqtt_via_rest_allowlist", fake_block)

    container = _FakeContainerLogs("ha-exo-pool-dev", staged_logs)
    return container, swc_entity, current, calls


def test_scenario_forced_held_write_wakes_early_and_restores_the_original_value(monkeypatch):
    container, swc_entity, current, calls = _forced_held_write_fixture(
        monkeypatch,
        staged_logs=[
            "MQTT connection interrupted: AWS_ERROR_MQTT_TIMEOUT\n",
            "Writing pool:swc via REST fallback\n",
            "Write pool:swc held behind cooldown: 40.0s remaining (post_write)\n",
            "Write pool:swc woken early by MQTT reconnect\n",
        ],
    )
    teardown = harness.BestEffortTeardown()

    outcome = harness.scenario_forced_held_write(
        container, "tok", teardown, "entry1", "binary_sensor.exo_pool_mqtt_connected", swc_entity,
        make_executor=_ImmediateExecutor,
    )

    assert outcome is True
    assert current[0] == 50
    assert ("block", ("45.60.157.189",), None) in calls
    assert ("unblock", None, None) in calls
    posted_values = [data["value"] for method, path, data in calls if method == "POST"]
    assert posted_values[:2] == [51, 50]


def test_scenario_forced_held_write_first_write_points_away_from_the_upper_bound(monkeypatch):
    container, swc_entity, current, calls = _forced_held_write_fixture(
        monkeypatch,
        staged_logs=[
            "MQTT connection interrupted: AWS_ERROR_MQTT_TIMEOUT\n",
            "Writing pool:swc via REST fallback\n",
            "Write pool:swc held behind cooldown: 40.0s remaining (post_write)\n",
            "Write pool:swc woken early by MQTT reconnect\n",
        ],
        original_value=100,
    )
    teardown = harness.BestEffortTeardown()

    harness.scenario_forced_held_write(
        container, "tok", teardown, "entry1", "binary_sensor.exo_pool_mqtt_connected", swc_entity,
        make_executor=_ImmediateExecutor,
    )

    posted_values = [data["value"] for method, path, data in calls if method == "POST"]
    assert posted_values[:2] == [99, 100]


def test_scenario_forced_held_write_unblocks_only_after_observing_held(monkeypatch):
    container, swc_entity, current, calls = _forced_held_write_fixture(
        monkeypatch,
        staged_logs=[
            "MQTT connection interrupted: AWS_ERROR_MQTT_TIMEOUT\n",
            "Writing pool:swc via REST fallback\n",
            "Write pool:swc held behind cooldown: 40.0s remaining (post_write)\n",
            "Write pool:swc woken early by MQTT reconnect\n",
        ],
    )
    read_count = [0]
    real_logs_since = container.logs_since

    def counting_logs_since(since_iso):
        read_count[0] += 1
        return real_logs_since(since_iso)

    container.logs_since = counting_logs_since
    unblock_read_counts = []
    calls.clear()

    def fake_block(container_name, teardown, rest_ips, port=443, **kwargs):
        teardown.defer(lambda: unblock_read_counts.append(read_count[0]))

    monkeypatch.setattr(harness, "block_mqtt_via_rest_allowlist", fake_block)
    teardown = harness.BestEffortTeardown()

    harness.scenario_forced_held_write(
        container, "tok", teardown, "entry1", "binary_sensor.exo_pool_mqtt_connected", swc_entity,
        make_executor=_ImmediateExecutor,
    )

    reads_before_held_line_seen = 3
    assert unblock_read_counts == [reads_before_held_line_seen]


def test_scenario_forced_held_write_unblocks_before_restoring_on_a_signal(monkeypatch):
    container, swc_entity, current, calls = _forced_held_write_fixture(
        monkeypatch,
        staged_logs=[
            "MQTT connection interrupted: AWS_ERROR_MQTT_TIMEOUT\n",
            "Writing pool:swc via REST fallback\n",
        ],
    )
    teardown = harness.BestEffortTeardown()

    def fake_signal_during_held_wait(seconds):
        teardown.run()
        raise SystemExit(130)

    with pytest.raises(SystemExit):
        harness.scenario_forced_held_write(
            container, "tok", teardown, "entry1", "binary_sensor.exo_pool_mqtt_connected", swc_entity,
            make_executor=_ImmediateExecutor, held_wait_sleep=fake_signal_during_held_wait,
        )

    unblock_index = calls.index(("unblock", None, None))
    restore_index = max(
        i for i, c in enumerate(calls) if c[0] == "POST" and c[2].get("value") == 50
    )
    assert unblock_index < restore_index


def test_scenario_forced_held_write_fails_when_original_state_is_non_numeric(monkeypatch):
    container, swc_entity, current, calls = _forced_held_write_fixture(monkeypatch, staged_logs=[])
    monkeypatch.setattr(harness, "get_entity_state", lambda token, entity_id: "unavailable")
    teardown = harness.BestEffortTeardown()

    with pytest.raises(harness.ScenarioFailure, match="unavailable"):
        harness.scenario_forced_held_write(
            container, "tok", teardown, "entry1", "binary_sensor.exo_pool_mqtt_connected", swc_entity,
            make_executor=_ImmediateExecutor,
        )

    assert calls == []


def test_scenario_forced_held_write_fails_when_the_restore_does_not_take(monkeypatch):
    container, swc_entity, current, calls = _forced_held_write_fixture(
        monkeypatch,
        staged_logs=[
            "MQTT connection interrupted: AWS_ERROR_MQTT_TIMEOUT\n",
            "Writing pool:swc via REST fallback\n",
            "Write pool:swc held behind cooldown: 40.0s remaining (post_write)\n",
            "Write pool:swc woken early by MQTT reconnect\n",
        ],
    )
    real_get_entity_state = harness.get_entity_state
    reads_so_far = [0]

    def get_entity_state_that_goes_unavailable_after_the_first_read(token, entity_id):
        reads_so_far[0] += 1
        if reads_so_far[0] == 1:
            return real_get_entity_state(token, entity_id)
        return "unavailable"

    monkeypatch.setattr(harness, "get_entity_state", get_entity_state_that_goes_unavailable_after_the_first_read)
    teardown = harness.BestEffortTeardown()

    with pytest.raises(harness.ScenarioFailure, match=f"{swc_entity}.*unavailable"):
        harness.scenario_forced_held_write(
            container, "tok", teardown, "entry1", "binary_sensor.exo_pool_mqtt_connected", swc_entity,
            make_executor=_ImmediateExecutor,
        )


def test_scenario_forced_held_write_fails_and_still_restores_when_early_wake_never_fires(monkeypatch):
    container, swc_entity, current, calls = _forced_held_write_fixture(
        monkeypatch,
        staged_logs=[
            "MQTT connection interrupted: AWS_ERROR_MQTT_TIMEOUT\n",
            "Writing pool:swc via REST fallback\n",
            "Write pool:swc held behind cooldown: 40.0s remaining (post_write)\n",
            "Writing pool:swc via REST fallback\n",
        ],
    )
    monkeypatch.setattr(harness, "POST_WRITE_COOLDOWN_SECONDS", 0.0)
    teardown = harness.BestEffortTeardown()
    clock = [0.0]

    with pytest.raises(harness.ScenarioFailure, match="pool:swc"):
        harness.scenario_forced_held_write(
            container, "tok", teardown, "entry1", "binary_sensor.exo_pool_mqtt_connected", swc_entity,
            make_executor=_ImmediateExecutor,
            early_wake_now=lambda: clock[0], early_wake_sleep=lambda s: clock.__setitem__(0, clock[0] + s),
        )

    teardown.run()

    assert current[0] == 50
    assert ("unblock", None, None) in calls


def test_scenario_forced_held_write_names_resumed_time_and_cooldown_left_on_early_wake_failure(monkeypatch):
    container, swc_entity, current, calls = _forced_held_write_fixture(
        monkeypatch,
        staged_logs=[
            "MQTT connection interrupted: AWS_ERROR_MQTT_TIMEOUT\n",
            "Writing pool:swc via REST fallback\n",
            "Write pool:swc held behind cooldown: 40.0s remaining (post_write)\n",
            "2026-09-19 10:00:05.000 INFO (MainThread) [custom_components.exo_pool.mqtt_client] "
            "MQTT connection resumed\n",
        ],
    )
    monkeypatch.setattr(harness, "POST_WRITE_COOLDOWN_SECONDS", 10.0)
    teardown = harness.BestEffortTeardown()
    clock = [0.0]

    def fake_sleep(seconds):
        clock[0] += seconds

    with pytest.raises(harness.ScenarioFailure, match=r"2026-09-19 10:00:05.*cooldown remained"):
        harness.scenario_forced_held_write(
            container, "tok", teardown, "entry1", "binary_sensor.exo_pool_mqtt_connected", swc_entity,
            make_executor=_ImmediateExecutor,
            early_wake_now=lambda: clock[0], early_wake_sleep=fake_sleep,
        )


def test_scenario_forced_held_write_ignores_a_held_line_for_a_different_key(monkeypatch):
    container, swc_entity, current, calls = _forced_held_write_fixture(
        monkeypatch,
        staged_logs=[
            "MQTT connection interrupted: AWS_ERROR_MQTT_TIMEOUT\n",
            "Writing pool:swc via REST fallback\n",
            "Write heating:sp held behind cooldown: 10.0s remaining (post_write)\n",
            "Write pool:swc held behind cooldown: 40.0s remaining (post_write)\n",
            "Write pool:swc woken early by MQTT reconnect\n",
        ],
    )
    read_count = [0]
    real_logs_since = container.logs_since

    def counting_logs_since(since_iso):
        read_count[0] += 1
        return real_logs_since(since_iso)

    container.logs_since = counting_logs_since
    unblock_read_counts = []
    calls.clear()

    def fake_block(container_name, teardown, rest_ips, port=443, **kwargs):
        teardown.defer(lambda: unblock_read_counts.append(read_count[0]))

    monkeypatch.setattr(harness, "block_mqtt_via_rest_allowlist", fake_block)
    teardown = harness.BestEffortTeardown()
    clock = [0.0]

    harness.scenario_forced_held_write(
        container, "tok", teardown, "entry1", "binary_sensor.exo_pool_mqtt_connected", swc_entity,
        make_executor=_ImmediateExecutor,
        held_wait_now=lambda: clock[0], held_wait_sleep=lambda s: clock.__setitem__(0, clock[0] + s),
        early_wake_now=lambda: clock[0], early_wake_sleep=lambda s: clock.__setitem__(0, clock[0] + s),
    )

    reads_before_correctly_keyed_held_line_seen = 4
    assert unblock_read_counts == [reads_before_correctly_keyed_held_line_seen]


def test_scenario_forced_held_write_fails_if_never_seen_held_before_unblocking(monkeypatch):
    container, swc_entity, current, calls = _forced_held_write_fixture(
        monkeypatch,
        staged_logs=[
            "MQTT connection interrupted: AWS_ERROR_MQTT_TIMEOUT\n",
            "Writing pool:swc via REST fallback\n",
        ],
    )
    teardown = harness.BestEffortTeardown()
    clock = [0.0]

    def fake_now():
        return clock[0]

    def fake_sleep(seconds):
        clock[0] += seconds

    with pytest.raises(harness.ScenarioFailure, match="held behind cooldown"):
        harness.scenario_forced_held_write(
            container, "tok", teardown, "entry1", "binary_sensor.exo_pool_mqtt_connected", swc_entity,
            make_executor=_ImmediateExecutor,
            held_wait_now=fake_now, held_wait_sleep=fake_sleep,
        )

    assert ("unblock", None, None) in calls


def test_assert_mounted_code_is_loaded_raises_when_container_predates_newest_mtime(tmp_path):
    source_dir = tmp_path / "exo_pool"
    source_dir.mkdir()
    edited_after_start = source_dir / "api.py"
    edited_after_start.write_text("# edited after the container started\n")
    newer_mtime = 1_800_000_100.0
    os.utime(edited_after_start, (newer_mtime, newer_mtime))

    def fake_runner(argv, **kwargs):
        if "State.StartedAt" in argv[-2]:
            return subprocess.CompletedProcess(args=argv, returncode=0, stdout="2026-09-11T11:14:00.000000000Z\n", stderr="")
        return subprocess.CompletedProcess(args=argv, returncode=0, stdout=f"{source_dir}\n", stderr="")

    container = harness.Container("ha-exo-pool-dev", runner=fake_runner)

    with pytest.raises(harness.StaleContainerError, match="docker restart ha-exo-pool-dev"):
        harness.assert_mounted_code_is_loaded(container, expected_source=source_dir)


def test_assert_mounted_code_is_loaded_does_not_raise_when_mount_is_fresh_and_matching(tmp_path):
    source_dir = tmp_path / "exo_pool"
    source_dir.mkdir()
    older_mtime = datetime(2026, 9, 1, tzinfo=timezone.utc).timestamp()
    (source_dir / "api.py").write_text("# fine\n")
    os.utime(source_dir / "api.py", (older_mtime, older_mtime))

    def fake_runner(argv, **kwargs):
        if "State.StartedAt" in argv[-2]:
            return subprocess.CompletedProcess(args=argv, returncode=0, stdout="2026-09-19T11:14:00.000000000Z\n", stderr="")
        return subprocess.CompletedProcess(args=argv, returncode=0, stdout=f"{source_dir}\n", stderr="")

    container = harness.Container("ha-exo-pool-dev", runner=fake_runner)

    harness.assert_mounted_code_is_loaded(container, expected_source=source_dir)


def test_assert_mounted_code_is_loaded_raises_when_mount_does_not_match_expected_repo(tmp_path):
    wrong_source = tmp_path / "other-checkout" / "exo_pool"
    wrong_source.mkdir(parents=True)
    (wrong_source / "api.py").write_text("# elsewhere\n")
    expected_source = tmp_path / "this-checkout" / "exo_pool"

    def fake_runner(argv, **kwargs):
        if "State.StartedAt" in argv[-2]:
            return subprocess.CompletedProcess(args=argv, returncode=0, stdout="2026-09-19T11:14:00.000000000Z\n", stderr="")
        return subprocess.CompletedProcess(args=argv, returncode=0, stdout=f"{wrong_source}\n", stderr="")

    container = harness.Container("ha-exo-pool-dev", runner=fake_runner)

    with pytest.raises(harness.StaleContainerError, match=f"{re.escape(str(wrong_source))}.*{re.escape(str(expected_source))}"):
        harness.assert_mounted_code_is_loaded(container, expected_source=expected_source)


def test_parse_docker_timestamp_handles_no_fractional_seconds():
    assert harness._parse_docker_timestamp("2026-09-19T06:40:31Z") == pytest.approx(
        datetime(2026, 9, 19, 6, 40, 31, tzinfo=timezone.utc).timestamp()
    )


def test_container_mount_source_raises_when_destination_not_mounted():
    def fake_runner(argv, **kwargs):
        return subprocess.CompletedProcess(args=argv, returncode=0, stdout="\n", stderr="")

    container = harness.Container("ha-exo-pool-dev", runner=fake_runner)

    with pytest.raises(RuntimeError, match="/config/custom_components/exo_pool"):
        container.mount_source("/config/custom_components/exo_pool")


def test_assert_mounted_code_is_loaded_raises_when_no_py_files_found(tmp_path):
    source_dir = tmp_path / "exo_pool"
    source_dir.mkdir()

    def fake_runner(argv, **kwargs):
        if "State.StartedAt" in argv[-2]:
            return subprocess.CompletedProcess(args=argv, returncode=0, stdout="2026-09-19T11:14:00.000000000Z\n", stderr="")
        return subprocess.CompletedProcess(args=argv, returncode=0, stdout=f"{source_dir}\n", stderr="")

    container = harness.Container("ha-exo-pool-dev", runner=fake_runner)

    with pytest.raises(RuntimeError, match="no .py files"):
        harness.assert_mounted_code_is_loaded(container, expected_source=source_dir)


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
