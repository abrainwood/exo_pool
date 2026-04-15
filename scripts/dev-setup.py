#!/usr/bin/env python3
"""
Automated Home Assistant dev instance setup for exo_pool.

Usage:
    python3 scripts/dev-setup.py

Creates the HA account, completes onboarding, adds the exo_pool
integration using credentials from .env, and configures debug logging.
Saves the auth token to .dev-token for future use.

Requires EXO_EMAIL and EXO_PASSWORD in .env (or environment).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

HA_URL = "http://localhost:8125"
TOKEN_FILE = ".dev-token"
ENV_FILE = ".env"
DEV_PASSWORD = "devdevdev"  # NOSONAR - intentional dev-only credential, not a real secret
DEV_USER = {"name": "Developer", "username": "dev", "password": DEV_PASSWORD, "language": "en"}
HOME_LOCATION = {
    "latitude": -33.701,
    "longitude": 151.209,
    "country": "AU",
    "time_zone": "Australia/Sydney",
    "elevation": 200,
    "unit_system": "metric",
    "currency": "AUD",
    "language": "en",
}
LOGGER_CONFIG = {
    "default": "warning",
    "logs": {"custom_components.exo_pool": "debug"},
}


def _load_env() -> dict:
    """Load key=value pairs from .env file."""
    env = {}
    try:
        with open(ENV_FILE) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" in line:
                    k, v = line.split("=", 1)
                    env[k.strip()] = v.strip()
    except FileNotFoundError:
        pass
    return env


def _request(method: str, path: str, data: dict | None = None,
             token: str | None = None, form: bool = False) -> dict | None:
    url = f"{HA_URL}{path}"
    headers = {}
    body = None

    if token:
        headers["Authorization"] = f"Bearer {token}"

    if data is not None:
        if form:
            body = urllib.parse.urlencode(data).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        else:
            body = json.dumps(data).encode()
            headers["Content-Type"] = "application/json"

    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req) as resp:
            raw = resp.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        print(f"  HTTP {e.code} on {method} {path}: {raw[:200]}")
        return None


def wait_for_ha(timeout: int = 120) -> None:
    print("Waiting for HA to start...", end="", flush=True)
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            req = urllib.request.Request(f"{HA_URL}/api/")
            urllib.request.urlopen(req, timeout=2)
            print(" ready")
            return
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                print(" ready")
                return
            print(".", end="", flush=True)
            time.sleep(2)
        except OSError:
            print(".", end="", flush=True)
            time.sleep(2)
    print("\nTimed out waiting for HA")
    sys.exit(1)


def _login(token_file: str = TOKEN_FILE) -> str:
    """Log in with existing credentials and return a fresh auth token."""
    print("Logging in with existing credentials...")
    flow = _request("POST", "/auth/login_flow", {
        "client_id": f"{HA_URL}/",
        "handler": ["homeassistant", None],
        "redirect_uri": f"{HA_URL}/",
    })
    if not flow or "flow_id" not in flow:
        print("Failed to start login flow")
        sys.exit(1)

    result = _request("POST", f"/auth/login_flow/{flow['flow_id']}", {
        "username": DEV_USER["username"],
        "password": DEV_USER["password"],
        "client_id": f"{HA_URL}/",
    })
    if not result or result.get("type") != "create_entry":
        print(f"Login failed: {result}")
        sys.exit(1)

    auth_code = result["result"]
    token_resp = _request("POST", "/auth/token", {
        "client_id": f"{HA_URL}/",
        "grant_type": "authorization_code",
        "code": auth_code,
    }, form=True)
    if not token_resp or "access_token" not in token_resp:
        print("Failed to get token")
        sys.exit(1)

    token = token_resp["access_token"]
    with open(token_file, "w") as f:
        f.write(token)
    print(f"Token saved to {token_file}")
    return token


def onboard(token_file: str = TOKEN_FILE) -> str:
    """Create account, get token, complete onboarding. Returns auth token."""
    try:
        with open(token_file) as f:
            token = f.read().strip()
        resp = _request("GET", "/api/", token=token)
        if resp is not None:
            print("Using saved token")
            return token
    except FileNotFoundError:
        pass

    result = _request("GET", "/api/onboarding")
    if not result:
        return _login(token_file)

    remaining = [step["step"] for step in result if not step.get("done", False)]
    if "user" not in remaining:
        return _login(token_file)

    print("Creating dev account...")
    payload = {**DEV_USER, "client_id": f"{HA_URL}/"}
    resp = _request("POST", "/api/onboarding/users", payload)
    if not resp or "auth_code" not in resp:
        print("Failed to create user")
        sys.exit(1)

    auth_code = resp["auth_code"]

    print("Getting auth token...")
    token_resp = _request("POST", "/auth/token", {
        "client_id": f"{HA_URL}/",
        "grant_type": "authorization_code",
        "code": auth_code,
    }, form=True)
    if not token_resp or "access_token" not in token_resp:
        print("Failed to get token")
        sys.exit(1)

    token = token_resp["access_token"]

    print("Completing onboarding...")
    _request("POST", "/api/onboarding/core_config", HOME_LOCATION, token=token)
    _request("POST", "/api/onboarding/analytics", {}, token=token)

    with open(token_file, "w") as f:
        f.write(token)
    print(f"Token saved to {token_file}")
    return token


def configure_logging(token: str) -> None:
    """Set up debug logging for exo_pool via the HA API."""
    print("Configuring debug logging for exo_pool...")
    _request("POST", "/api/services/logger/set_level",
             {"custom_components.exo_pool": "debug"}, token=token)


def _extract_system_options(schema: list) -> list:
    """Extract system options from a config flow data_schema."""
    for field in schema:
        if field.get("name") == "system":
            opts = field.get("options", {})
            return list(opts.keys()) if isinstance(opts, dict) else list(opts)
        if "options" in field:
            opts = field["options"]
            return list(opts.keys()) if isinstance(opts, dict) else list(opts)
    return []


def add_integration(token: str, email: str, password: str) -> bool:
    """Add the exo_pool integration via config flow."""
    entries = _request("GET", "/api/config/config_entries/entry", token=token)
    if entries:
        for entry in entries:
            if entry.get("domain") == "exo_pool":
                print("Integration already configured")
                return True

    print("Adding exo_pool integration...")

    flow = _request("POST", "/api/config/config_entries/flow",
                     {"handler": "exo_pool"}, token=token)
    if not flow or "flow_id" not in flow:
        print("Failed to init config flow")
        return False

    flow_id = flow["flow_id"]

    print("  Submitting credentials...")
    result = _request("POST", f"/api/config/config_entries/flow/{flow_id}",
                       {"email": email, "password": password}, token=token)
    if not result:
        print("  Failed to submit credentials")
        return False

    if result.get("type") == "create_entry":
        print(f"  Integration added: {result.get('title', 'exo_pool')}")
        return True

    if result.get("type") != "form" or result.get("step_id") != "select_system":
        print(f"  Unexpected flow result: {json.dumps(result)[:500]}")
        return False

    options = _extract_system_options(result.get("data_schema", []))
    if not options:
        print(f"  No systems found in flow response: {json.dumps(result)[:500]}")
        return False

    system = options[0]
    print(f"  Selecting system: {system}")
    result = _request("POST", f"/api/config/config_entries/flow/{flow_id}",
                       {"system": system}, token=token)
    if result and result.get("type") == "create_entry":
        print(f"  Integration added: {result.get('title', 'exo_pool')}")
        return True

    print(f"  Unexpected flow result: {json.dumps(result)[:500]}")
    return False


def start_docker() -> None:
    """Start the HA dev container."""
    result = subprocess.run(["docker", "ps", "-q", "-f", "name=ha-exo-pool-dev"],
                            capture_output=True, text=True)
    if result.stdout.strip():
        print("HA container already running")
        return

    print("Starting HA container...")
    subprocess.run(
        ["docker", "compose", "-f", "docker-compose.dev.yml", "up", "-d"],
        capture_output=True,
    )


def main() -> None:
    env = _load_env()
    email = os.environ.get("EXO_EMAIL") or env.get("EXO_EMAIL")
    password = os.environ.get("EXO_PASSWORD") or env.get("EXO_PASSWORD")

    if not email or not password:
        print("Set EXO_EMAIL and EXO_PASSWORD in .env or environment")
        sys.exit(1)

    start_docker()
    wait_for_ha()
    token = onboard()
    configure_logging(token)
    add_integration(token, email, password)

    print(f"\nHA dev instance ready at {HA_URL}")
    print(f"Login: {DEV_USER['username']} / {DEV_USER['password']}")
    print("\nUseful commands:")
    print("  make logs       # watch logs")
    print("  make restart    # restart after code changes")
    print("  make stop       # stop container")
    print("  make test       # run tests")


if __name__ == "__main__":
    main()
