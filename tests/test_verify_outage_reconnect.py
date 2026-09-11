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
    assert calls[0][-1] == "apk add -q iptables && iptables -I OUTPUT -d 3.226.158.32 -p tcp -j DROP"


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
        "apk add -q iptables && iptables -D OUTPUT -d 3.226.158.32 -p tcp -j DROP",
        "apk add -q iptables && iptables -D OUTPUT -d 34.206.242.80 -p tcp -j DROP",
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

    assert removed == ["apk add -q iptables && iptables -D OUTPUT -d 3.226.158.32 -p tcp -j DROP"]


def test_build_netns_sidecar_cmd_shares_the_target_containers_network():
    argv = harness.build_netns_sidecar_cmd("ha-exo-pool-dev", "echo hi")

    assert argv == [
        "docker", "run", "--rm",
        "--network", "container:ha-exo-pool-dev",
        "--cap-add", "NET_ADMIN",
        harness.SIDECAR_IMAGE, "sh", "-c", "echo hi",
    ]


def test_iptables_rule_shell_cmd_builds_the_insert_form():
    cmd = harness.iptables_rule_shell_cmd("10.0.0.5", "-I")

    assert cmd == "apk add -q iptables && iptables -I OUTPUT -d 10.0.0.5 -p tcp -j DROP"


def test_iptables_rule_shell_cmd_builds_the_delete_form():
    cmd = harness.iptables_rule_shell_cmd("10.0.0.5", "-D")

    assert cmd == "apk add -q iptables && iptables -D OUTPUT -d 10.0.0.5 -p tcp -j DROP"


def test_check_net_admin_capable_true_when_sidecar_iptables_succeeds():
    def fake_runner(*args, **kwargs):
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")

    assert harness.check_net_admin_capable("ha-exo-pool-dev", runner=fake_runner) is True


def test_check_net_admin_capable_false_when_sidecar_iptables_fails():
    def fake_runner(*args, **kwargs):
        return subprocess.CompletedProcess(args=args, returncode=127, stdout="", stderr="sh: iptables: not found")

    assert harness.check_net_admin_capable("ha-exo-pool-dev", runner=fake_runner) is False


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
