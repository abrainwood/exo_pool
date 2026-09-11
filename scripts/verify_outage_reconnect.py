#!/usr/bin/env python3
"""Repeatable harness for the MQTT outage-reconnect fix (issue #2 / PR #4).

Drives the `ha-exo-pool-dev` dev container through a simulated WAN outage
and checks that the fix in PR #4 behaves as designed: the retry chain
re-arms itself with growing backoff instead of dying after one failed
attempt, the exo_pool MQTT-connectivity binary_sensor (resolved at runtime -
see resolve_mqtt_entity_id()) tracks the transport honestly, and the
interrupt watchdog forces a reconnect if resume never arrives.

Usage:
    export EXO_HARNESS_TOKEN=<HA long-lived access token for the dev instance>
    # Create one at http://localhost:8125/profile/security (dev / devdevdev),
    # or reuse the token scripts/dev-setup.py already saved to .dev-token.
    python3 scripts/verify_outage_reconnect.py [--skip-watchdog]

Requires the dev container from `make dev` to be running and the exo_pool
integration already configured in it. Never touches anything but the dev
container on port 8125 - see assert_dev_instance_url().
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Callable
from urllib.parse import urlparse

_LOGGER = logging.getLogger(__name__)

# --- Dev-instance safety -----------------------------------------------

DEV_HA_PORT = 8125
CONTAINER_NAME = "ha-exo-pool-dev"
HA_URL = f"http://localhost:{DEV_HA_PORT}"
TOKEN_FILE = ".dev-token"
TOKEN_ENV_VAR = "EXO_HARNESS_TOKEN"

# On the dev image HA writes here, not to stdout/stderr - `docker logs`
# silently returns a stale, frozen stream instead of failing.
HA_LOG_PATH = "/config/home-assistant.log"


class HaLogUnavailableError(Exception):
    """Raised when the dev container's home-assistant.log can't be read."""


class NotDevInstanceError(Exception):
    """Raised when the target URL is not confirmed to be the dev container."""


def assert_dev_instance_url(url: str) -> None:
    """Hard-fail unless `url` points at the dev container's port - never the live instance."""
    if urlparse(url).port != DEV_HA_PORT:
        raise NotDevInstanceError(
            f"refusing to run against {url!r} - only port {DEV_HA_PORT} ({CONTAINER_NAME}) "
            "is permitted, to guarantee the live HA instance is never touched"
        )


# --- Outage simulation targets ------------------------------------------

# Real hostnames the fix's failure path depends on - see api.py LOGIN_URL/
# REFRESH_URL/DATA_URL_TEMPLATE and IOT_ENDPOINT.
BLACKHOLE_HOSTS = [
    "prod.zodiac-io.com",
    "a1zi08qpbrtjyq-ats.iot.us-east-1.amazonaws.com",
]
IOT_ENDPOINT = "a1zi08qpbrtjyq-ats.iot.us-east-1.amazonaws.com"
HOSTS_MARKER = "# exo-pool-outage-harness"

# Mirrors api.py's own constants so the harness's timeouts track the fix
# instead of drifting from it independently.
MQTT_RETRY_BASE_DELAY = 30.0
INTERRUPT_WATCHDOG_TIMEOUT = 180


# --- HA log-file parsing (unit tested) ------------------------------------

_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;]*m")


def strip_ansi_codes(text: str) -> str:
    """Remove ANSI colour escapes docker's log capture leaves in the text."""
    return _ANSI_ESCAPE_RE.sub("", text)


# HA's file-handler timestamp: "YYYY-MM-DD HH:MM:SS.mmm ". Fixed-width and
# zero-padded, so lexical comparison against a same-format key sorts
# correctly - no need to parse into a datetime.
_LOG_LINE_TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\.\d{3} ")


def filter_log_lines_since(log_text: str, since_iso: str) -> str:
    """Keep only home-assistant.log lines timestamped at/after `since_iso`.

    A line with no leading timestamp (a traceback continuation) inherits the
    previous timestamped line's inclusion decision.
    """
    since_key = time.strftime("%Y-%m-%d %H:%M:%S", time.strptime(since_iso, "%Y-%m-%dT%H:%M:%SZ"))
    kept: list[str] = []
    include = False
    for line in strip_ansi_codes(log_text).splitlines():
        match = _LOG_LINE_TS_RE.match(line)
        if match:
            include = match.group(1) >= since_key
        if include:
            kept.append(line)
    return "\n".join(kept) + ("\n" if kept else "")


# --- Retry-log parsing (unit tested) -------------------------------------

# Anchored on api.py's "_async_refresh_and_reconnect" warning - the only
# externally-observable signal of a scheduled retry's delay.
_RETRY_ATTEMPT_RE = re.compile(
    r"MQTT reconnect attempt (?P<attempt>\d+) to \S+ failed.*?"
    r"retrying in ~(?P<delay>[\d.]+)s"
)


@dataclass(frozen=True)
class RetryAttempt:
    attempt: int
    delay: float


def parse_retry_attempts(log_text: str) -> list[RetryAttempt]:
    """Extract each logged MQTT retry (attempt number, backoff delay) in order."""
    return [
        RetryAttempt(attempt=int(m.group("attempt")), delay=float(m.group("delay")))
        for m in _RETRY_ATTEMPT_RE.finditer(log_text)
    ]


def assert_growing_backoff(attempts: list[RetryAttempt], min_attempts: int = 3) -> None:
    """Raise unless at least `min_attempts` retries were logged with strictly growing delay."""
    if len(attempts) < min_attempts:
        raise AssertionError(
            f"expected at least {min_attempts} retry re-arms, got {len(attempts)}: {attempts}"
        )
    for prev, cur in zip(attempts, attempts[1:min_attempts]):
        if not (cur.delay > prev.delay):
            raise AssertionError(
                f"backoff delay did not grow: attempt {prev.attempt} was {prev.delay}s, "
                f"attempt {cur.attempt} was {cur.delay}s"
            )


# --- Interrupt/watchdog log matching (unit tested) ------------------------

# The colon distinguishes the interrupt event itself from the watchdog's
# own "interrupted Ns ago" line below, which also starts with this prefix.
_CONNECTION_INTERRUPTED_RE = re.compile(r"MQTT connection interrupted: ")
_WATCHDOG_FORCED_RECONNECT_RE = re.compile(
    rf"MQTT connection interrupted {INTERRUPT_WATCHDOG_TIMEOUT}s ago with no resume"
)


def matches_connection_interrupted(log_text: str) -> bool:
    return bool(_CONNECTION_INTERRUPTED_RE.search(log_text))


def matches_watchdog_forced_reconnect(log_text: str) -> bool:
    return bool(_WATCHDOG_FORCED_RECONNECT_RE.search(log_text))


# --- Teardown (unit tested) ----------------------------------------------


class BestEffortTeardown:
    """Runs deferred cleanup actions LIFO, continuing past failures."""

    def __init__(self) -> None:
        self._actions: list[Callable[[], None]] = []

    def defer(self, action: Callable[[], None]) -> None:
        self._actions.append(action)

    def run(self) -> None:
        while self._actions:
            action = self._actions.pop()
            try:
                action()
            except Exception:
                _LOGGER.warning("Teardown action failed - continuing", exc_info=True)


def recovery_failure_message(container_name: str) -> str:
    return (
        "The integration did NOT recover after the simulated outage. Operator "
        f"action required: restart the dev container - `docker restart {container_name}` "
        "or `make restart` - then verify manually that MQTT reconnects."
    )


# --- Scenario failure ------------------------------------------------------


class ScenarioFailure(Exception):
    """Raised when a scenario's assertion doesn't hold."""


# --- Container control -----------------------------------------------------


class Container:
    """Thin wrapper over `docker exec` for one named container."""

    def __init__(self, name: str, runner: Callable[..., subprocess.CompletedProcess] = subprocess.run):
        self.name = name
        self._runner = runner

    def is_running(self) -> bool:
        result = self._runner(
            ["docker", "inspect", "-f", "{{.State.Running}}", self.name],
            capture_output=True, text=True, timeout=10,
        )
        return result.returncode == 0 and result.stdout.strip() == "true"

    def exec(self, cmd: list[str], *, input_text: str | None = None, timeout: int = 20) -> subprocess.CompletedProcess:
        docker_cmd = ["docker", "exec"]
        if input_text is not None:
            docker_cmd.append("-i")
        docker_cmd += [self.name, *cmd]
        return self._runner(
            docker_cmd, input=input_text, capture_output=True, text=True, timeout=timeout,
        )

    def logs_since(self, since_iso: str) -> str:
        """Read HA_LOG_PATH inside the container, filtered to lines since `since_iso`.

        Rotation is untracked: HA only rotates this file on process restart,
        and every scenario here only reloads the config entry, never restarts
        HA, so a run can't straddle a rotation.
        """
        result = self._runner(
            ["docker", "exec", self.name, "cat", HA_LOG_PATH],
            capture_output=True, text=True, timeout=20,
        )
        if result.returncode != 0:
            raise HaLogUnavailableError(
                f"could not read {HA_LOG_PATH} in {self.name}: {result.stderr.strip()}"
            )
        return filter_log_lines_since(result.stdout, since_iso)


def now_utc_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def wait_for_log_pattern(
    container: Container,
    pattern: re.Pattern,
    since_iso: str,
    timeout: float,
    poll_interval: float = 3.0,
    label: str = "log pattern",
) -> str:
    """Poll container logs since `since_iso` until `pattern` is found or `timeout` elapses."""
    deadline = time.monotonic() + timeout
    start = time.monotonic()
    while True:
        text = container.logs_since(since_iso)
        if pattern.search(text):
            return text
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ScenarioFailure(
                f"timed out after {timeout:.0f}s waiting for {label}"
            )
        elapsed = time.monotonic() - start
        print(f"  ... waiting for {label} ({elapsed:.0f}s elapsed, {remaining:.0f}s left)")
        time.sleep(min(poll_interval, remaining))


# --- MQTT entity resolution (unit tested) ---------------------------------

MQTT_ENTITY_DOMAIN = "binary_sensor."
MQTT_ENTITY_SUFFIX = "mqtt_connected"


class MqttEntityResolutionError(RuntimeError):
    """Raised when the exo_pool MQTT-connectivity entity can't be uniquely resolved."""


def resolve_mqtt_entity_id(entity_ids: list[str]) -> str:
    """Pick the `binary_sensor.*mqtt_connected` entity out of `entity_ids`.

    HA prefixes the entity ID with the device name, so a hardcoded ID
    breaks silently on a device rename - match the domain/suffix shape and
    resolve it at runtime instead.
    """
    matches = [
        eid for eid in entity_ids
        if eid.startswith(MQTT_ENTITY_DOMAIN) and eid.endswith(MQTT_ENTITY_SUFFIX)
    ]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        exo_candidates = [eid for eid in entity_ids if "exo" in eid]
        raise MqttEntityResolutionError(
            f"no {MQTT_ENTITY_DOMAIN}*{MQTT_ENTITY_SUFFIX} entity found; "
            f"exo-matching entities seen: {exo_candidates or 'none'}"
        )
    raise MqttEntityResolutionError(
        f"expected exactly one {MQTT_ENTITY_DOMAIN}*{MQTT_ENTITY_SUFFIX} entity, "
        f"found {len(matches)}: {matches}"
    )


# --- HA REST API -------------------------------------------------------


def load_ha_token() -> str:
    token = os.environ.get(TOKEN_ENV_VAR)
    if token:
        return token.strip()
    try:
        with open(TOKEN_FILE) as f:
            token = f.read().strip()
        if token:
            return token
    except FileNotFoundError:
        pass
    raise RuntimeError(
        f"No HA token found. Set {TOKEN_ENV_VAR}, or run scripts/dev-setup.py "
        f"first so it saves one to {TOKEN_FILE}. Create one manually at "
        f"{HA_URL}/profile/security if needed."
    )


def _ha_request(method: str, path: str, token: str, data: dict | None = None, timeout: float = 10.0) -> dict | list | None:
    url = f"{HA_URL}{path}"
    body = json.dumps(data).encode() if data is not None else None
    req = urllib.request.Request(
        url, data=body, method=method,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"HA API {method} {path} failed: HTTP {e.code} {e.read().decode()[:200]}") from e


def get_entity_state(token: str, entity_id: str) -> str:
    state = _ha_request("GET", f"/api/states/{entity_id}", token)
    return state["state"]


def list_entity_ids(token: str) -> list[str]:
    states = _ha_request("GET", "/api/states", token)
    return [s["entity_id"] for s in states or []]


def find_entry_state(entries: list[dict], entry_id: str) -> str:
    """Extract the config-entry `state` field for `entry_id` from a config_entries/entry response."""
    for entry in entries:
        if entry.get("entry_id") == entry_id:
            return entry["state"]
    raise RuntimeError(f"config entry {entry_id} not found")


def get_entry_state(token: str, entry_id: str) -> str:
    entries = _ha_request("GET", "/api/config/config_entries/entry", token)
    return find_entry_state(entries or [], entry_id)


def get_exo_pool_entry_id(token: str) -> str:
    entries = _ha_request("GET", "/api/config/config_entries/entry", token)
    for entry in entries or []:
        if entry.get("domain") == "exo_pool":
            return entry["entry_id"]
    raise RuntimeError("No exo_pool config entry found on the dev instance")


# HA's reload endpoint blocks until setup finishes or fails. Under a
# simulated outage, setup's own internal retries can run well past a
# typical API timeout - this is generous on purpose.
RELOAD_TIMEOUT = 90.0


class ReloadTimedOut(Exception):
    """Raised when a config-entry reload doesn't return before RELOAD_TIMEOUT.

    An expected outcome under a simulated outage, not a harness error.
    """


def reload_entry(token: str, entry_id: str, timeout: float = RELOAD_TIMEOUT) -> None:
    try:
        _ha_request("POST", f"/api/config/config_entries/entry/{entry_id}/reload", token, timeout=timeout)
    except TimeoutError as e:
        raise ReloadTimedOut(f"reload of entry {entry_id} did not return within {timeout:.0f}s") from e
    except urllib.error.URLError as e:
        if isinstance(e.reason, TimeoutError):
            raise ReloadTimedOut(f"reload of entry {entry_id} did not return within {timeout:.0f}s") from e
        raise


# --- Outage simulation -----------------------------------------------------


def blackhole_hosts(container: Container, teardown: BestEffortTeardown, hostnames: list[str]) -> None:
    """Append blackhole entries for `hostnames` to the container's /etc/hosts.

    Registers the restore as the first teardown step so it runs last (LIFO),
    after any dependent steps (e.g. re-arming a reconnect) that assume the
    outage is still active have already torn down.
    """
    backup = container.exec(["cat", "/etc/hosts"])
    if backup.returncode != 0:
        raise RuntimeError(f"could not read /etc/hosts in {container.name}: {backup.stderr}")
    original_hosts = backup.stdout

    def _restore():
        result = container.exec(["sh", "-c", "cat > /etc/hosts"], input_text=original_hosts)
        if result.returncode != 0:
            raise RuntimeError(f"failed to restore /etc/hosts: {result.stderr}")
        _LOGGER.info("Restored /etc/hosts in %s", container.name)

    teardown.defer(_restore)

    entries = "\n".join(f"127.0.0.1 {host} {HOSTS_MARKER}" for host in hostnames)
    append = container.exec(["sh", "-c", f"printf '%s\\n' '{entries}' >> /etc/hosts"])
    if append.returncode != 0:
        raise RuntimeError(f"failed to blackhole hosts in {container.name}: {append.stderr}")


def check_net_admin_capable(container: Container) -> bool:
    """True if the container can actually run iptables (NET_ADMIN present)."""
    result = container.exec(["iptables", "-L", "-n"], timeout=10)
    return result.returncode == 0


def block_iot_endpoint_tcp(container: Container, teardown: BestEffortTeardown, endpoint: str) -> None:
    """Drop outbound TCP to `endpoint` at the IP layer (DNS still resolves)."""
    resolved = container.exec(["getent", "hosts", endpoint])
    if resolved.returncode != 0 or not resolved.stdout.strip():
        raise RuntimeError(f"could not resolve {endpoint} inside {container.name}")
    ip = resolved.stdout.split()[0]

    rule = ["-I", "OUTPUT", "-d", ip, "-p", "tcp", "-j", "DROP"]

    def _unblock():
        result = container.exec(["iptables", "-D", *rule[1:]])
        if result.returncode != 0:
            raise RuntimeError(f"failed to remove iptables DROP rule for {ip}: {result.stderr}")
        _LOGGER.info("Removed iptables DROP rule for %s in %s", ip, container.name)

    teardown.defer(_unblock)

    add = container.exec(["iptables", *rule])
    if add.returncode != 0:
        raise RuntimeError(f"failed to add iptables DROP rule for {ip}: {add.stderr}")


# --- Scenarios ---------------------------------------------------------


def scenario_baseline(token: str, mqtt_entity: str) -> None:
    state = get_entity_state(token, mqtt_entity)
    if state != "on":
        raise ScenarioFailure(f"{mqtt_entity} baseline is {state!r}, expected 'on'")
    print(f"PASS baseline: {mqtt_entity} is on")


def _wait_for_min_retry_attempts(container: Container, since: str, min_attempts: int, timeout: float) -> list[RetryAttempt]:
    deadline = time.monotonic() + timeout
    start = time.monotonic()
    attempts: list[RetryAttempt] = []
    while True:
        attempts = parse_retry_attempts(container.logs_since(since))
        if len(attempts) >= min_attempts:
            return attempts
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return attempts
        elapsed = time.monotonic() - start
        print(
            f"  ... waiting for {min_attempts} retry re-arms, have {len(attempts)} "
            f"({elapsed:.0f}s elapsed, {remaining:.0f}s left)"
        )
        time.sleep(min(5, remaining))


def _wait_for_entity_state(token: str, entity_id: str, expected: str, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    start = time.monotonic()
    while True:
        state = get_entity_state(token, entity_id)
        if state == expected:
            return True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        elapsed = time.monotonic() - start
        print(
            f"  ... waiting for {entity_id} to be {expected!r}, currently {state!r} "
            f"({elapsed:.0f}s elapsed, {remaining:.0f}s left)"
        )
        time.sleep(min(5, remaining))


def _wait_for_entry_state_not_loaded(token: str, entry_id: str, timeout: float) -> str:
    deadline = time.monotonic() + timeout
    start = time.monotonic()
    while True:
        state = get_entry_state(token, entry_id)
        if state != "loaded":
            return state
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return state
        elapsed = time.monotonic() - start
        print(f"  ... waiting for setup to fail under outage, still 'loaded' ({elapsed:.0f}s elapsed, {remaining:.0f}s left)")
        time.sleep(min(5, remaining))


def _enter_retry_chain_from_connected(container: Container, since: str) -> None:
    """Force entry into _async_refresh_and_reconnect from an already-connected state.

    Needs NET_ADMIN - callers must check check_net_admin_capable() first.
    """
    interrupt_teardown = BestEffortTeardown()
    block_iot_endpoint_tcp(container, interrupt_teardown, IOT_ENDPOINT)
    try:
        wait_for_log_pattern(
            container, _CONNECTION_INTERRUPTED_RE, since,
            timeout=60.0, label="connection interrupt",
        )
        wait_for_log_pattern(
            container, _WATCHDOG_FORCED_RECONNECT_RE, since,
            timeout=INTERRUPT_WATCHDOG_TIMEOUT + 60.0, label="watchdog-forced reconnect",
        )
    finally:
        # Unblock now: the DNS blackhole (managed by the caller) is what
        # keeps the forced reconnect failing from here on, so the TCP
        # block has done its job once the watchdog has fired.
        interrupt_teardown.run()


def scenario_reconnect_from_connected(container: Container, token: str, teardown: BestEffortTeardown, mqtt_entity: str) -> bool | None:
    """Issue #2's actual reproduction: MQTT is connected, the network dies
    underneath it, and the retry chain must re-arm and keep going."""
    if not check_net_admin_capable(container):
        print(
            "SKIP reconnect-from-connected: container lacks NET_ADMIN (or iptables) - "
            "cannot force an already-established MQTT connection to interrupt, and "
            "the fix's own credential-refresh timer is up to ~55 minutes away, too "
            "long to wait on. Run with cap-add=NET_ADMIN on the dev container "
            "(see docker-compose.dev.yml) to exercise this scenario."
        )
        return None

    baseline = get_entity_state(token, mqtt_entity)
    if baseline != "on":
        raise ScenarioFailure(f"{mqtt_entity} is {baseline!r} before the outage, expected 'on'")

    since = now_utc_iso()
    blackhole_hosts(container, teardown, BLACKHOLE_HOSTS)
    _enter_retry_chain_from_connected(container, since)

    attempts = _wait_for_min_retry_attempts(container, since, min_attempts=3, timeout=210.0)
    assert_growing_backoff(attempts)
    print(f"PASS retry backoff grows: {attempts[:3]}")

    sensor_state = get_entity_state(token, mqtt_entity)
    if sensor_state != "off":
        raise ScenarioFailure(f"{mqtt_entity} did not flip off during outage (state={sensor_state!r})")
    print(f"PASS sensor flip: {mqtt_entity} is off during outage")

    next_wait_cap = attempts[-1].delay if attempts else MQTT_RETRY_BASE_DELAY
    teardown.run()  # restore DNS - the already-scheduled retry chain keeps running on its own timer

    wait_for_log_pattern(
        container, re.compile(r"MQTT connected - REST fallback interval set to"), since,
        timeout=next_wait_cap * 2 + 60,
        label="reconnect after DNS recovery",
    )
    if not _wait_for_entity_state(token, mqtt_entity, "on", timeout=30.0):
        raise ScenarioFailure(f"{mqtt_entity} did not return to 'on' after recovery")
    print("PASS recovery: reconnected and sensor back on")

    # _reset_mqtt_retry_backoff doesn't log, so proving it fired means
    # forcing one more failure and reading the first delay back off it.
    reset_since = now_utc_iso()
    blackhole_hosts(container, teardown, BLACKHOLE_HOSTS)
    _enter_retry_chain_from_connected(container, reset_since)
    reset_attempts = _wait_for_min_retry_attempts(container, reset_since, min_attempts=1, timeout=60.0)
    if not reset_attempts or reset_attempts[0].delay > MQTT_RETRY_BASE_DELAY * 1.5:
        raise ScenarioFailure(
            f"backoff did not reset to base after recovery: first post-recovery "
            f"attempt was {reset_attempts[0] if reset_attempts else 'missing'}"
        )
    print(f"PASS backoff reset to base: {reset_attempts[0]}")

    teardown.run()
    wait_for_log_pattern(
        container, re.compile(r"MQTT connected - REST fallback interval set to"), reset_since,
        timeout=MQTT_RETRY_BASE_DELAY * 2 + 60,
        label="reconnect after second DNS recovery",
    )
    if not _wait_for_entity_state(token, mqtt_entity, "on", timeout=30.0):
        raise ScenarioFailure(f"{mqtt_entity} did not return to 'on' after the reset-check outage")
    return True


def _ensure_recovered(token: str, entry_id: str, mqtt_entity: str) -> bool:
    """Land the integration back in a working state, reloading if needed.

    Called unconditionally at the end of every run, not just on scenario
    success - a harness that can leave the integration dead is worse than
    no harness.
    """
    if _wait_for_entity_state(token, mqtt_entity, "on", timeout=30.0):
        return True
    for attempt in (1, 2):
        try:
            reload_entry(token, entry_id)
        except ReloadTimedOut:
            pass
        if _wait_for_entity_state(token, mqtt_entity, "on", timeout=120.0):
            return True
    return False


def scenario_setup_under_outage(container: Container, token: str, teardown: BestEffortTeardown, entry_id: str, mqtt_entity: str) -> None:
    """Pins what happens when the entry is reloaded while the network is down.

    This is a different code path from scenario_reconnect_from_connected: a
    reload re-runs setup (get_coordinator -> _connect_mqtt), never
    _async_refresh_and_reconnect, so it can't be used to test the fix's
    retry chain - only to pin setup's own behaviour under an outage.
    """
    since = now_utc_iso()
    blackhole_hosts(container, teardown, BLACKHOLE_HOSTS)

    try:
        reload_entry(token, entry_id)
        outcome = "the reload call returned"
    except ReloadTimedOut:
        outcome = "the reload call timed out"
    print(f"{outcome} - checking the entry's actual state under the outage")

    entry_state = _wait_for_entry_state_not_loaded(token, entry_id, timeout=RELOAD_TIMEOUT)
    if entry_state == "loaded":
        raise ScenarioFailure("setup succeeded despite the simulated outage - expected it to fail")
    print(f"PASS setup fails under outage: entry state is {entry_state!r}")

    teardown.run()
    if not _ensure_recovered(token, entry_id, mqtt_entity):
        raise ScenarioFailure(recovery_failure_message(CONTAINER_NAME))
    print("PASS recovery: entry reloaded and MQTT back on after DNS restored")


def scenario_watchdog(container: Container, token: str, teardown: BestEffortTeardown) -> bool | None:
    """Returns True on pass, False on failure, None if skipped (no NET_ADMIN)."""
    if not check_net_admin_capable(container):
        print(
            "SKIP watchdog: container lacks NET_ADMIN (or iptables) - cannot "
            "block traffic at the IP/TCP layer. Run with --privileged or "
            "cap-add=NET_ADMIN on the dev container to exercise this scenario."
        )
        return None

    since = now_utc_iso()
    block_iot_endpoint_tcp(container, teardown, IOT_ENDPOINT)

    wait_for_log_pattern(
        container, _CONNECTION_INTERRUPTED_RE, since,
        timeout=60.0, label="connection interrupt",
    )
    wait_for_log_pattern(
        container, _WATCHDOG_FORCED_RECONNECT_RE, since,
        timeout=INTERRUPT_WATCHDOG_TIMEOUT + 60, label="watchdog-forced reconnect",
    )
    print("PASS watchdog: forced reconnect after interrupt with no resume")
    teardown.run()
    return True


# --- Entry point -------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--skip-watchdog", action="store_true", help="Skip the separate watchdog (NET_ADMIN) scenario")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    assert_dev_instance_url(HA_URL)

    container = Container(CONTAINER_NAME)
    if not container.is_running():
        print(f"FATAL: container {CONTAINER_NAME!r} is not running. Run `make dev` first.")
        return 1

    token = load_ha_token()

    try:
        mqtt_entity = resolve_mqtt_entity_id(list_entity_ids(token))
        entry_id = get_exo_pool_entry_id(token)
    except (MqttEntityResolutionError, RuntimeError) as e:
        print(f"FATAL: could not resolve the exo_pool entity/entry: {e}")
        return 1

    results: dict[str, str] = {}
    teardown = BestEffortTeardown()

    def _handle_signal(signum, _frame):
        print(f"\nCaught signal {signum} - restoring container state before exit")
        teardown.run()
        sys.exit(130)

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    try:
        try:
            scenario_baseline(token, mqtt_entity)
            results["baseline"] = "PASS"
        except (ScenarioFailure, AssertionError, RuntimeError) as e:
            results["baseline"] = f"FAIL: {e}"

        if results["baseline"] == "PASS":
            try:
                outcome = scenario_reconnect_from_connected(container, token, teardown, mqtt_entity)
                results["reconnect_from_connected"] = "PASS" if outcome else (
                    "SKIP (no NET_ADMIN)" if outcome is None else "FAIL"
                )
            except (ScenarioFailure, AssertionError, RuntimeError) as e:
                results["reconnect_from_connected"] = f"FAIL: {e}"

            try:
                scenario_setup_under_outage(container, token, teardown, entry_id, mqtt_entity)
                results["setup_under_outage"] = "PASS"
            except (ScenarioFailure, AssertionError, RuntimeError) as e:
                results["setup_under_outage"] = f"FAIL: {e}"

            if args.skip_watchdog:
                results["watchdog"] = "SKIP (--skip-watchdog)"
            else:
                try:
                    outcome = scenario_watchdog(container, token, teardown)
                    results["watchdog"] = "PASS" if outcome else (
                        "SKIP (no NET_ADMIN)" if outcome is None else "FAIL"
                    )
                except (ScenarioFailure, AssertionError, RuntimeError) as e:
                    results["watchdog"] = f"FAIL: {e}"
        else:
            results["reconnect_from_connected"] = "SKIP (baseline unhealthy)"
            results["setup_under_outage"] = "SKIP (baseline unhealthy)"
            results["watchdog"] = "SKIP (baseline unhealthy)"
    finally:
        teardown.run()
        if _ensure_recovered(token, entry_id, mqtt_entity):
            results["recovery"] = "PASS"
        else:
            print("\n" + "!" * 70)
            print(recovery_failure_message(CONTAINER_NAME))
            print("!" * 70 + "\n")
            results["recovery"] = "FAIL"

    print("\n--- Summary ---")
    failed = False
    for name, outcome in results.items():
        print(f"{name}: {outcome}")
        if outcome.startswith("FAIL"):
            failed = True
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
