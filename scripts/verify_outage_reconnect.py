#!/usr/bin/env python3
"""Repeatable harness for the MQTT outage-reconnect fix (issue #2 / PR #4).

Drives the `ha-exo-pool-dev` dev container through a simulated WAN outage
and checks that the fix in PR #4 behaves as designed. Scenario list and
what each one asserts: see the README's "Verifying the MQTT
outage-reconnect fix" section.

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
import ipaddress
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


_CONNECTION_RESUMED_RE = re.compile(r"MQTT connection resumed")


def matches_connection_resumed(log_text: str) -> bool:
    return bool(_CONNECTION_RESUMED_RE.search(log_text))


# "after reconnect" (post-resume) vs "after connect" (initial connect()) -
# only the former means credentials went stale mid-session.
_RESUBSCRIBE_FAILED_AFTER_RESUME_RE = re.compile(r"All subscribes failed after reconnect")


def matches_resubscribe_failed_after_resume(log_text: str) -> bool:
    return bool(_RESUBSCRIBE_FAILED_AFTER_RESUME_RE.search(log_text))


_RECONNECT_FAILED_REFRESHING_RE = re.compile(r"MQTT reconnect failed - refreshing credentials")


def matches_reconnect_failed_refreshing(log_text: str) -> bool:
    return bool(_RECONNECT_FAILED_REFRESHING_RE.search(log_text))


_TRANSPORT_RECONNECTED_RE = re.compile(r"MQTT connected - REST fallback interval set to")


def matches_transport_reconnected(log_text: str) -> bool:
    return bool(_TRANSPORT_RECONNECTED_RE.search(log_text))


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


def should_print_tick(elapsed: float, last_print: float | None, print_interval: float) -> bool:
    """True on the first tick (immediate), or every `print_interval` seconds after that."""
    return last_print is None or elapsed - last_print >= print_interval


def now_utc_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


DEFAULT_PRINT_INTERVAL = 20.0


def wait_for_log_pattern(
    container: Container,
    pattern: re.Pattern,
    since_iso: str,
    timeout: float,
    poll_interval: float = 3.0,
    print_interval: float = DEFAULT_PRINT_INTERVAL,
    label: str = "log pattern",
    on_tick: Callable[[], None] | None = None,
) -> str:
    """Poll container logs since `since_iso` until `pattern` is found or `timeout` elapses.

    `on_tick`, if given, runs once per poll - used to top up IP blocks that
    might rotate out from under a wait.
    """
    deadline = time.monotonic() + timeout
    start = time.monotonic()
    last_print: float | None = None
    while True:
        if on_tick is not None:
            on_tick()
        text = container.logs_since(since_iso)
        if pattern.search(text):
            return text
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ScenarioFailure(
                f"timed out after {timeout:.0f}s waiting for {label}"
            )
        elapsed = time.monotonic() - start
        if should_print_tick(elapsed, last_print, print_interval):
            print(f"  ... waiting for {label} ({elapsed:.0f}s elapsed, {remaining:.0f}s left)")
            last_print = elapsed
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


# --- Established-peer selection (unit tested) ------------------------------

_SS_PEER_RE = re.compile(r"(\d{1,3}(?:\.\d{1,3}){3}):(\d+)\s*$")


def select_established_peer_ips(ss_output: str, port: int = 443) -> list[str]:
    """Public IPv4 peers on `port` from `ss -tn state established` output.

    The DNS-resolved pool for a rotating cloud endpoint routinely doesn't
    contain the address a live connection is actually pinned to, so the
    block list has to come from the real established connections instead.
    """
    peers: list[str] = []
    for line in ss_output.splitlines():
        match = _SS_PEER_RE.search(line)
        if not match:
            continue
        ip, peer_port = match.group(1), int(match.group(2))
        if peer_port != port:
            continue
        addr = ipaddress.ip_address(ip)
        if addr.is_loopback or addr.is_private:
            continue
        if ip not in peers:
            peers.append(ip)
    if not peers:
        raise RuntimeError(
            f"no established peers on port {port} found to block; ss output was:\n"
            f"{ss_output.strip() or '(empty)'}"
        )
    return peers


# --- Netns sidecar (unit tested) ------------------------------------------

# The HA image has no iptables/nft/ip - blocking traffic needs a throwaway
# container sharing its network namespace instead.
SIDECAR_IMAGE = "alpine:3.20"

# Built once by ensure_harness_tools_image() before any outage is
# simulated, with iptables/iproute2 already installed - so no sidecar
# call made during or after a block ever depends on apk fetching over
# the network that block might itself be cutting.
HARNESS_TOOLS_IMAGE = "exo-pool-harness-tools:latest"


def build_netns_sidecar_cmd(container_name: str, shell_cmd: str, image: str = HARNESS_TOOLS_IMAGE) -> list[str]:
    return [
        "docker", "run", "--rm",
        "--network", f"container:{container_name}",
        "--cap-add", "NET_ADMIN",
        image, "sh", "-c", shell_cmd,
    ]


def ensure_harness_tools_image(
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    base_image: str = SIDECAR_IMAGE,
    tag: str = HARNESS_TOOLS_IMAGE,
    timeout: float = 120.0,
) -> str:
    """Build `tag` (iptables + iproute2 on `base_image`) if it isn't there yet.

    Runs on normal networking, before any outage is simulated - idempotent,
    so calling this from every scenario's own capability check costs
    nothing once the image already exists.
    """
    inspect = runner(["docker", "image", "inspect", tag], capture_output=True, text=True, timeout=10)
    if inspect.returncode == 0:
        return tag

    create = runner(
        ["docker", "create", base_image, "sh", "-c", "apk add -q iptables iproute2"],
        capture_output=True, text=True, timeout=timeout,
    )
    if create.returncode != 0:
        raise RuntimeError(f"failed to create harness-tools-image builder container: {create.stderr}")
    builder_id = create.stdout.strip()
    try:
        start = runner(["docker", "start", "-a", builder_id], capture_output=True, text=True, timeout=timeout)
        if start.returncode != 0:
            raise RuntimeError(f"failed to install tools into builder container: {start.stderr}")
        commit = runner(["docker", "commit", builder_id, tag], capture_output=True, text=True, timeout=timeout)
        if commit.returncode != 0:
            raise RuntimeError(f"failed to commit {tag}: {commit.stderr}")
    finally:
        runner(["docker", "rm", "-f", builder_id], capture_output=True, text=True, timeout=timeout)
    return tag


def iptables_rule_shell_cmd(ip: str, flag: str) -> str:
    """flag is '-I' to insert the OUTPUT DROP rule for `ip`, '-D' to remove it.

    No `apk add` - runs against the prebuilt HARNESS_TOOLS_IMAGE, so this
    never depends on a network it might itself be cutting.
    """
    return f"iptables {flag} OUTPUT -d {ip} -p tcp -j DROP"


def port_block_shell_cmd(flag: str, port: int) -> str:
    """flag is '-I' to insert an OUTPUT DROP rule for every peer on `port`, '-D' to remove it.

    Blocks by port rather than by address - deterministic against a
    rotating pool, unlike a per-IP rule that a fresh address dodges. No
    `apk add` - runs against the prebuilt HARNESS_TOOLS_IMAGE, so removing
    this rule never depends on the network it just cut.
    """
    return f"iptables {flag} OUTPUT -p tcp --dport {port} -j DROP"


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


def run_netns_sidecar(
    container_name: str,
    shell_cmd: str,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    timeout: float = 60.0,
) -> subprocess.CompletedProcess:
    return runner(build_netns_sidecar_cmd(container_name, shell_cmd), capture_output=True, text=True, timeout=timeout)


def check_net_admin_capable(
    container_name: str, runner: Callable[..., subprocess.CompletedProcess] = subprocess.run
) -> bool:
    """True if the prebuilt harness-tools image can run iptables against `container_name`'s network.

    Builds the image first (idempotent, runs on normal networking, well
    before any outage) - so later blocking calls never depend on apk
    fetching over a network they might be cutting.
    """
    try:
        ensure_harness_tools_image(runner=runner)
    except RuntimeError:
        return False
    result = run_netns_sidecar(container_name, "iptables -L -n", runner=runner, timeout=30.0)
    return result.returncode == 0


def get_established_peer_ips(
    container_name: str,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    port: int = 443,
) -> list[str]:
    """The container's currently-established public peers on `port`, via a netns sidecar's `ss`."""
    result = run_netns_sidecar(container_name, "ss -tn state established", runner=runner, timeout=30.0)
    if result.returncode != 0:
        raise RuntimeError(f"failed to list established connections via netns sidecar: {result.stderr}")
    return select_established_peer_ips(result.stdout, port=port)


def get_established_peer_ips_with_retry(
    container_name: str,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    port: int = 443,
    attempts: int = 3,
    retry_delay: float = 3.0,
) -> list[str]:
    """get_established_peer_ips(), tolerating the peer briefly vanishing mid-reconnect."""
    def _get_peers() -> list[str] | str:
        try:
            return get_established_peer_ips(container_name, runner=runner, port=port)
        except RuntimeError as e:
            return str(e)

    return get_peers_with_retry(_get_peers, attempts=attempts, retry_delay=retry_delay)


class IpBlockSet:
    """Tracks iptables DROP rules added for a rotating set of IPs.

    AWS IoT resolves to a pool of addresses that rotates, so a block must
    be able to top up newly-seen IPs without losing track of what it's
    already blocked, and unblock everything it ever added regardless of
    when that was.
    """

    def __init__(
        self,
        container_name: str,
        teardown: BestEffortTeardown,
        runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    ) -> None:
        self._container_name = container_name
        self._runner = runner
        self._blocked: dict[str, None] = {}
        teardown.defer(self._unblock_all)

    def add(self, ip: str) -> None:
        if ip in self._blocked:
            return
        result = run_netns_sidecar(self._container_name, iptables_rule_shell_cmd(ip, "-I"), runner=self._runner)
        if result.returncode != 0:
            raise RuntimeError(f"failed to add iptables DROP rule for {ip}: {result.stderr}")
        self._blocked[ip] = None
        print(f"Blocked {ip} via netns sidecar")

    def top_up(self, ips: list[str]) -> list[str]:
        """Add rules for any of `ips` not already blocked; returns the newly-added ones."""
        added = [ip for ip in ips if ip not in self._blocked]
        for ip in added:
            self.add(ip)
        return added

    def _unblock_all(self) -> None:
        errors = []
        for ip in list(self._blocked):
            result = run_netns_sidecar(self._container_name, iptables_rule_shell_cmd(ip, "-D"), runner=self._runner)
            if result.returncode != 0:
                errors.append(f"{ip}: {result.stderr}")
            else:
                del self._blocked[ip]
        if errors:
            raise RuntimeError(f"failed to remove {len(errors)} iptables DROP rule(s): {'; '.join(errors)}")


def _default_abort(message: str) -> None:
    print("\n" + "!" * 70)
    print(message)
    print("!" * 70 + "\n")
    sys.exit(1)


def unblock_port_or_abort(
    container_name: str,
    port: int,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    attempts: int = 3,
    retry_delay: float = 5.0,
    sleep: Callable[[float], None] = lambda s: time.sleep(s),
    abort: Callable[[str], None] = _default_abort,
) -> None:
    """Retry removing the total-outage DROP rule; abort the run rather than
    continue if it's still there after all attempts.

    Unlike a hosts entry or a single blocked address, this rule severs all
    outbound traffic on `port` - BestEffortTeardown's continue-past-failure
    is exactly wrong here, since a run that carries on with the rule still
    up leaves the dev container permanently unreachable on that port.
    """
    last_error = ""
    for attempt in range(attempts):
        result = run_netns_sidecar(container_name, port_block_shell_cmd("-D", port), runner=runner)
        if result.returncode == 0:
            print(f"Unblocked outbound TCP port {port} via netns sidecar")
            return
        last_error = result.stderr
        if attempt < attempts - 1:
            sleep(retry_delay)
    abort(
        f"Could not remove the port-{port} outbound DROP rule after {attempts} attempts "
        f"({last_error}). The dev container has NO outbound TCP on this port. "
        f"Operator action required: `docker restart {CONTAINER_NAME}` clears it."
    )


def block_port_total_outage(
    container_name: str,
    teardown: BestEffortTeardown,
    port: int = 443,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> None:
    """Block every outbound connection on `port`, deterministically - by
    port rather than by address, so a rotating pool can't dodge it the way
    per-IP blocking can.

    Before committing, proves the *unblock path itself* still works (a
    harmless `iptables -L -n` over the same sidecar) - the property that
    matters is not whether the harness can still reach HA (a different port
    and direction the block never touches), but whether this block can
    still be undone. Rolls back immediately if it can't.
    """
    def _unblock() -> None:
        unblock_port_or_abort(container_name, port, runner=runner)

    add = run_netns_sidecar(container_name, port_block_shell_cmd("-I", port), runner=runner)
    if add.returncode != 0:
        raise RuntimeError(f"failed to add port-{port} DROP rule: {add.stderr}")
    print(f"Blocked outbound TCP port {port} via netns sidecar (total outage)")

    teardown.defer(_unblock)

    verify = run_netns_sidecar(container_name, "iptables -L -n", runner=runner, timeout=15.0)
    if verify.returncode != 0:
        teardown.run()
        raise RuntimeError(
            f"could not verify the port-{port} block can be undone "
            f"(sidecar iptables -L -n failed) - rolled back: {verify.stderr}"
        )


# --- Scenario precondition (unit tested) -----------------------------------


def precondition_met(sensor_state: str, peer_ips: list[str] | None) -> bool:
    """True only if the sensor reads 'on' AND an established peer actually exists.

    A sensor reading 'on' with no established peer is exactly the
    looks-healthy-but-isn't gap issue #2 was about - both must hold.
    """
    return sensor_state == "on" and bool(peer_ips)


def wait_for_healthy_precondition(
    get_sensor_state: Callable[[], str],
    get_peers: Callable[[], list[str] | str],
    reload: Callable[[], None],
    max_polls: int = 12,
    sleep: Callable[[float], None] = lambda s: time.sleep(s),
    poll_interval: float = 5.0,
) -> list[str]:
    """Poll until precondition_met holds, reloading once and retrying if it never does.

    Returns the validated peer list - callers should block those peers
    directly rather than re-querying a moment later, since a fresh query can
    race a reconnect still in flight and find nothing.

    `get_peers` returns either the peer list or an error-detail string (from
    a failed peer check) - either way it's surfaced in the failure message,
    since "no peers found" alone doesn't say why.
    """
    last_sensor_state = ""
    last_peers: list[str] | str = []
    for attempt in (1, 2):
        for _ in range(max_polls):
            last_sensor_state = get_sensor_state()
            last_peers = get_peers()
            peers = last_peers if isinstance(last_peers, list) else None
            if precondition_met(last_sensor_state, peers):
                return peers
            sleep(poll_interval)
        if attempt == 1:
            reload()
    raise ScenarioFailure(
        f"precondition not met after reload-and-retry: sensor={last_sensor_state!r}, peers={last_peers!r}"
    )


def get_peers_with_retry(
    get_peers: Callable[[], list[str] | str],
    attempts: int = 3,
    sleep: Callable[[float], None] = lambda s: time.sleep(s),
    retry_delay: float = 3.0,
) -> list[str]:
    """Retry a peer lookup a few times before giving up.

    A peer vanishing for one query is a normal reconnect in flight, not a
    fatal condition - only give up once it's still gone after retrying.
    """
    last_result: list[str] | str = []
    for attempt in range(attempts):
        last_result = get_peers()
        if isinstance(last_result, list) and last_result:
            return last_result
        if attempt < attempts - 1:
            sleep(retry_delay)
    detail = last_result if isinstance(last_result, str) else "no peers found"
    raise RuntimeError(detail)


def ensure_scenario_precondition(
    token: str, entry_id: str, mqtt_entity: str, container_name: str, timeout: float = 60.0
) -> list[str]:
    """Real-run wrapper: every scenario calls this before doing anything destructive.

    A prior scenario's failure - this run or a previous one - must not
    silently poison the ones after it. Returns the validated peer list - use
    it directly for the scenario's first block rather than re-querying, since
    a fresh query a moment later can race a reconnect still in flight.
    """
    def _get_peers() -> list[str] | str:
        try:
            return get_established_peer_ips(container_name)
        except RuntimeError as e:
            return str(e)

    return wait_for_healthy_precondition(
        get_sensor_state=lambda: get_entity_state(token, mqtt_entity),
        get_peers=_get_peers,
        reload=lambda: _reload_ignoring_timeout(token, entry_id),
        max_polls=max(1, int(timeout // 5)),
    )


def _reload_ignoring_timeout(token: str, entry_id: str) -> None:
    try:
        reload_entry(token, entry_id)
    except ReloadTimedOut:
        pass


# --- Scenarios ---------------------------------------------------------


def scenario_baseline(token: str, entry_id: str, mqtt_entity: str, container: Container) -> None:
    ensure_scenario_precondition(token, entry_id, mqtt_entity, container.name)
    print(f"PASS baseline: {mqtt_entity} is on with an established MQTT peer")


def _wait_for_min_retry_attempts(container: Container, since: str, min_attempts: int, timeout: float) -> list[RetryAttempt]:
    deadline = time.monotonic() + timeout
    start = time.monotonic()
    last_print: float | None = None
    attempts: list[RetryAttempt] = []
    while True:
        attempts = parse_retry_attempts(container.logs_since(since))
        if len(attempts) >= min_attempts:
            return attempts
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return attempts
        elapsed = time.monotonic() - start
        if should_print_tick(elapsed, last_print, DEFAULT_PRINT_INTERVAL):
            print(
                f"  ... waiting for {min_attempts} retry re-arms, have {len(attempts)} "
                f"({elapsed:.0f}s elapsed, {remaining:.0f}s left)"
            )
            last_print = elapsed
        time.sleep(min(5, remaining))


def _wait_for_entity_state(token: str, entity_id: str, expected: str, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    start = time.monotonic()
    last_print: float | None = None
    while True:
        state = get_entity_state(token, entity_id)
        if state == expected:
            return True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        elapsed = time.monotonic() - start
        if should_print_tick(elapsed, last_print, DEFAULT_PRINT_INTERVAL):
            print(
                f"  ... waiting for {entity_id} to be {expected!r}, currently {state!r} "
                f"({elapsed:.0f}s elapsed, {remaining:.0f}s left)"
            )
            last_print = elapsed
        time.sleep(min(5, remaining))


def _wait_for_entry_state_not_loaded(token: str, entry_id: str, timeout: float) -> str:
    deadline = time.monotonic() + timeout
    start = time.monotonic()
    last_print: float | None = None
    while True:
        state = get_entry_state(token, entry_id)
        if state != "loaded":
            return state
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return state
        elapsed = time.monotonic() - start
        if should_print_tick(elapsed, last_print, DEFAULT_PRINT_INTERVAL):
            print(f"  ... waiting for setup to fail under outage, still 'loaded' ({elapsed:.0f}s elapsed, {remaining:.0f}s left)")
            last_print = elapsed
        time.sleep(min(5, remaining))


def _safe_top_up(block_set: IpBlockSet, container_name: str) -> None:
    """Top up newly-seen peers, tolerating a transient empty state.

    Once blocking has actually taken effect there may genuinely be nothing
    established at the instant of a given poll - that's the point of the
    block, not a failure to report.
    """
    try:
        new_ips = get_established_peer_ips(container_name)
    except RuntimeError:
        return
    added = block_set.top_up(new_ips)
    if added:
        print(f"Topped up newly-seen peer(s): {added}")


def _enter_retry_chain_from_connected(container: Container, since: str, peers: list[str] | None = None) -> None:
    """Force entry into _async_refresh_and_reconnect from an already-connected state.

    Blocks `peers` if given (a caller-validated list, to avoid re-querying
    and racing a reconnect in flight), else resolves its own. Tops up any
    newly-rotated-in peers while waiting. Waits for the retry chain's own
    first attempt rather than the watchdog specifically, since either
    forcing path gets there. Needs NET_ADMIN - callers must check
    check_net_admin_capable() first.
    """
    if peers is None:
        peers = get_established_peer_ips_with_retry(container.name)
    interrupt_teardown = BestEffortTeardown()
    block_set = IpBlockSet(container.name, interrupt_teardown)
    for ip in peers:
        block_set.add(ip)

    def _top_up() -> None:
        _safe_top_up(block_set, container.name)

    try:
        wait_for_log_pattern(
            container, _CONNECTION_INTERRUPTED_RE, since,
            timeout=60.0, label="connection interrupt", on_tick=_top_up,
        )
        wait_for_log_pattern(
            container, _RETRY_ATTEMPT_RE, since,
            timeout=INTERRUPT_WATCHDOG_TIMEOUT + 60.0,
            label="retry chain entry (watchdog or resubscribe-failure)",
            on_tick=_top_up,
        )
    finally:
        interrupt_teardown.run()


def scenario_reconnect_from_connected(container: Container, token: str, teardown: BestEffortTeardown, entry_id: str, mqtt_entity: str) -> bool | None:
    """Issue #2's actual reproduction: MQTT is connected, the network dies
    underneath it, and the retry chain must re-arm and keep going."""
    if not check_net_admin_capable(container.name):
        print(
            "SKIP reconnect-from-connected: could not run iptables against the dev "
            "container's network via a netns sidecar - cannot force an "
            "already-established MQTT connection to interrupt, and the fix's own "
            "credential-refresh timer is up to ~55 minutes away, too long to wait on. "
            "Needs `docker run` access and network access to pull the sidecar image "
            "and its iptables package."
        )
        return None

    peers = ensure_scenario_precondition(token, entry_id, mqtt_entity, container.name)

    since = now_utc_iso()
    blackhole_hosts(container, teardown, BLACKHOLE_HOSTS)
    _enter_retry_chain_from_connected(container, since, peers=peers)

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
        container, _TRANSPORT_RECONNECTED_RE, since,
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
        container, _TRANSPORT_RECONNECTED_RE, reset_since,
        timeout=MQTT_RETRY_BASE_DELAY * 2 + 60,
        label="reconnect after second DNS recovery",
    )
    if not _wait_for_entity_state(token, mqtt_entity, "on", timeout=30.0):
        raise ScenarioFailure(f"{mqtt_entity} did not return to 'on' after the reset-check outage")
    return True


def scenario_interrupt_resume_recovers(container: Container, token: str, teardown: BestEffortTeardown, entry_id: str, mqtt_entity: str) -> bool | None:
    """The common transient-blip path, distinct from the rare watchdog one:
    the connection drops, the CRT resumes via a different address within
    seconds, the resubscribe fails on stale credentials, and the fix forces
    a refresh to recover - observed live on issue #2's actual dev box.

    Blocks only the connection's actual current peer(s), not the whole
    rotating pool - unlike scenario_reconnect_from_connected and
    scenario_watchdog, this scenario wants resume to succeed quickly via
    some other address, not be prevented.
    """
    if not check_net_admin_capable(container.name):
        print(
            "SKIP interrupt-resume-recovers: could not run iptables against the "
            "dev container's network via a netns sidecar - cannot force a brief "
            "interrupt. Needs `docker run` access and network access to pull the "
            "sidecar image and its iptables package."
        )
        return None

    peers = ensure_scenario_precondition(token, entry_id, mqtt_entity, container.name)

    since = now_utc_iso()
    block_set = IpBlockSet(container.name, teardown)
    for ip in peers:
        block_set.add(ip)

    wait_for_log_pattern(
        container, _CONNECTION_INTERRUPTED_RE, since,
        timeout=60.0, label="connection interrupt",
    )
    print("PASS interrupt: MQTT connection interrupted")

    wait_for_log_pattern(
        container, _CONNECTION_RESUMED_RE, since,
        timeout=60.0, label="connection resume via another address",
    )
    print("PASS resume: MQTT connection resumed")

    wait_for_log_pattern(
        container, _RESUBSCRIBE_FAILED_AFTER_RESUME_RE, since,
        timeout=30.0, label="resubscribe failure after resume",
    )
    print("PASS resubscribe fails after resume: credentials treated as possibly stale")

    wait_for_log_pattern(
        container, _RECONNECT_FAILED_REFRESHING_RE, since,
        timeout=10.0, label="forced credential refresh triggered",
    )
    print("PASS forced credential refresh triggered")

    wait_for_log_pattern(
        container, _TRANSPORT_RECONNECTED_RE, since,
        timeout=30.0, label="transport recovery",
    )
    if not _wait_for_entity_state(token, mqtt_entity, "on", timeout=30.0):
        raise ScenarioFailure(f"{mqtt_entity} did not return to 'on' after recovery")
    print("PASS recovery: reconnected via forced credential refresh")

    teardown.run()
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
        _reload_ignoring_timeout(token, entry_id)
        if _wait_for_entity_state(token, mqtt_entity, "on", timeout=120.0):
            return True
    return False


def scenario_setup_under_outage(container: Container, token: str, teardown: BestEffortTeardown, entry_id: str, mqtt_entity: str) -> bool | None:
    """Pins what happens when the entry is reloaded while the network is down.

    This is a different code path from scenario_reconnect_from_connected: a
    reload re-runs setup (get_coordinator -> _connect_mqtt), never
    _async_refresh_and_reconnect, so it can't be used to test the fix's
    retry chain - only to pin setup's own behaviour under an outage. Returns
    True on pass, None if skipped (no NET_ADMIN for the total outbound block).
    """
    if not check_net_admin_capable(container.name):
        print(
            "SKIP setup-under-outage: could not run iptables against the dev "
            "container's network via a netns sidecar - cannot make the API "
            "genuinely unreachable (an /etc/hosts blackhole alone lets a "
            "pooled connection let setup complete anyway). Needs `docker run` "
            "access and network access to pull the sidecar image and its "
            "iptables package."
        )
        return None

    ensure_scenario_precondition(token, entry_id, mqtt_entity, container.name)

    # /etc/hosts only stops *new* DNS resolution - a pooled/keepalive
    # connection can still let setup complete, so pair it with a total
    # outbound block to make the API genuinely unreachable.
    blackhole_hosts(container, teardown, BLACKHOLE_HOSTS)
    block_port_total_outage(container.name, teardown)

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
    return True


def scenario_watchdog(container: Container, token: str, teardown: BestEffortTeardown, entry_id: str, mqtt_entity: str) -> bool | None:
    """The rare path: no address works at all, so resume never comes and
    the fix's own INTERRUPT_WATCHDOG_TIMEOUT has to force the reconnect
    itself. Needs a guaranteed no-resume window, so this blocks by port
    (block_port_total_outage), not by address - per-IP blocking degenerates
    into scenario_interrupt_resume_recovers as the CRT dodges onto a fresh
    address faster than a top-up can chase it. Returns True on pass, False
    on failure, None if skipped (no NET_ADMIN).
    """
    if not check_net_admin_capable(container.name):
        print(
            "SKIP watchdog: could not run iptables against the dev container's "
            "network via a netns sidecar - cannot block traffic at the IP/TCP "
            "layer. Needs `docker run` access and network access to pull the "
            "sidecar image and its iptables package."
        )
        return None

    ensure_scenario_precondition(token, entry_id, mqtt_entity, container.name)

    since = now_utc_iso()
    block_port_total_outage(container.name, teardown)

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

    def _recover_between_scenarios() -> None:
        # A scenario's own failure - this run or a previous one - must not
        # silently poison the ones after it; each scenario's own
        # ensure_scenario_precondition() call retries too, but landing back
        # in a known-good state here keeps that retry cheap.
        teardown.run()
        if not _ensure_recovered(token, entry_id, mqtt_entity):
            print("WARNING: could not recover between scenarios - the next one's own precondition wait will retry")

    try:
        try:
            scenario_baseline(token, entry_id, mqtt_entity, container)
            results["baseline"] = "PASS"
        except (ScenarioFailure, AssertionError, RuntimeError) as e:
            results["baseline"] = f"FAIL: {e}"
        _recover_between_scenarios()

        try:
            outcome = scenario_reconnect_from_connected(container, token, teardown, entry_id, mqtt_entity)
            results["reconnect_from_connected"] = "PASS" if outcome else (
                "SKIP (no NET_ADMIN)" if outcome is None else "FAIL"
            )
        except (ScenarioFailure, AssertionError, RuntimeError) as e:
            results["reconnect_from_connected"] = f"FAIL: {e}"
        _recover_between_scenarios()

        try:
            outcome = scenario_interrupt_resume_recovers(container, token, teardown, entry_id, mqtt_entity)
            results["interrupt_resume_recovers"] = "PASS" if outcome else (
                "SKIP (no NET_ADMIN)" if outcome is None else "FAIL"
            )
        except (ScenarioFailure, AssertionError, RuntimeError) as e:
            results["interrupt_resume_recovers"] = f"FAIL: {e}"
        _recover_between_scenarios()

        try:
            outcome = scenario_setup_under_outage(container, token, teardown, entry_id, mqtt_entity)
            results["setup_under_outage"] = "PASS" if outcome else (
                "SKIP (no NET_ADMIN)" if outcome is None else "FAIL"
            )
        except (ScenarioFailure, AssertionError, RuntimeError) as e:
            results["setup_under_outage"] = f"FAIL: {e}"
        _recover_between_scenarios()

        if args.skip_watchdog:
            results["watchdog"] = "SKIP (--skip-watchdog)"
        else:
            try:
                outcome = scenario_watchdog(container, token, teardown, entry_id, mqtt_entity)
                results["watchdog"] = "PASS" if outcome else (
                    "SKIP (no NET_ADMIN)" if outcome is None else "FAIL"
                )
            except (ScenarioFailure, AssertionError, RuntimeError) as e:
                results["watchdog"] = f"FAIL: {e}"
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
