# Exo Pool – Home Assistant Integration

A custom integration to connect your Zodiac iAqualink **Exo** pool system to Home Assistant, providing full control and monitoring of your pool’s features.

## 🆕 What’s New

- **15 Sep 2026**
1) MQTT now reconnects after connection drops, previously causing fallback to REST
2) Race conditions between regular MQTT status updates and changes made in HA are now handled

- **15 Apr 2026**
1) **Real-time updates via AWS IoT MQTT.** The integration now connects to the same AWS IoT shadow endpoint used by the official iAqualink app, giving sub-second state sync instead of REST polling. No additional setup required - it uses credentials already provided by the Zodiac login API.
2) Writes (set points, switches, schedules) now go via MQTT when connected, eliminating 429 rate limit errors on writes.
3) REST polling is kept as a 1-hour fallback in case MQTT disconnects, but under normal operation all data flows through MQTT push.
4) AWS credentials are automatically refreshed before expiry (~hourly).
5) Added `awsiotsdk` as a dependency (installed automatically by HACS).

- **7 Feb 2026**
1) Small retry fix to get around 401 'token expired' errors on schedule write attempts (and associated logging updates).

- **6 Feb 2026**

1) Much better protection against cloud rate-limits.
  1.1) The integration now carefully spaces out API calls and avoids overlapping reads and writes.
  1.2) This greatly reduces “Too Many Requests (429)” errors.
2) Smarter handling of changes
  2.1) When you adjust pH, ORP, or schedules, changes are queued and applied safely.
  2.2) Multiple quick changes are merged together instead of hammering the cloud API.
3) No more read/write collisions
  3.1) The integration will not poll the cloud while a setting change is in progress.
  3.2) A short “settling period” after changes prevents unnecessary follow-up requests.
4) Improved schedule reliability
  4.1) Schedule updates are applied more reliably, even when making multiple edits.
  4.2) Optional delayed confirmation refresh avoids unnecessary cloud traffic.
5) New manual refresh feature - you can force a data refresh (though all safety rails still apply)
6) Optional entities now appear reliably
  6.1) pH and ORP setpoint controls will appear automatically once the device reports support.
  6.2) Temporary startup issues (for example during rate-limits) no longer cause them to disappear permanently.
7) More robust startup behavior
  7.1) If the cloud is temporarily unavailable during startup, the integration now recovers cleanly once connectivity returns.
8) Added a 'AWS Status' to the diagnostics. This sensor indicates whether your Exo unit itself is connected to AWS.

- **11 Jan 2026**

1) Changes to refresh rates, now by default we only refresh data from the API every 5 minutes (this will gracefully reduce if 429s are detected), but temporarily boost the rate to every 10s when a user change (for example PH set point) is made.
2) SWC sensors were incorrect before. Now SWC normal and low levels are settable with the correct switch ('low' from shadow data) reflecting if low mode is enabled. In the future we will hide these levels for systems with an ORP sensor (like me), as the swc levels are all 0. For now though I have left it in for debugging purposes.
3) Added a service `exo_pool.reload` to reload the integration if you ever need it (ideally not with the new refresh timings).

- **20 Oct 2025** - Modifications for SSP (Single Speed Pump) - single speed pumps should now be correctly recognised.
- **23 Sep 2025** - Added experimental climate entity for systems with the heat pump enabled.
- **15 Sep 2025** – Added option to adjust API refresh rate to avoid *“Too Many Requests”* errors.
- **3 Sep 2025** – Added binary_sensors for each schedule plus actions to change schedules.

---

## Installation (via HACS)

1. In Home Assistant, go to **HACS → Integrations**.
2. Search for **Exo Pool** and click **Install**.
3. Restart Home Assistant.
4. Go to **Settings → Devices & Services → Add Integration**, search for **Exo Pool**, and follow the prompts.

---

## Features

- **Automatic Authentication** – Secure login to the iAqualink API using your email and password.
- **System Selection** – Pick your Exo system from multiple pools/devices (filtered to `device_type: "exo"`).
- **Sensors** – Temperature, pH, ORP, ORP Boost Time Remaining, Pump RPM, Error Code, Wi-Fi RSSI.
- **Binary Sensors** – Filter Pump running, Chlorinator running, Error State, Authentication Status, Connected, and one per schedule.
- **Switches** – ORP Boost, Power State, Production, Aux 1, Aux 2, SWC Low.
- **Numbers** – SWC Output, SWC Low Output, Refresh Interval, plus pH/ORP Set Points when supported.
- **Climate (experimental)** – Heat Pump control when Aux 2 is configured for heat mode.
- **Services** – Control and modify schedules (see below).
- **Diagnostics & Dynamic Device Info** – View hardware configuration and live status; serial number and software version update periodically.
- **Real-time MQTT Updates** – Connects to AWS IoT for instant state sync (same protocol as the official app). No MQTT broker or addon required.
- **Configurable Refresh Rate** – The `Refresh Interval` number (300-3600 s) controls the REST fallback poll interval. Under normal MQTT operation this rarely fires.

---

## Schedule Services

Each Exo schedule is exposed as a binary sensor:

- **State**: `on` when active.
- **Attributes**: `schedule`, `enabled`, `start_time`, `end_time`, `type` (`vsp` | `swc` | `aux` | other), and `rpm` (VSP only).
- **Icons**: VSP → pump/pump-off, SWC → water-plus/water-off, AUX → toggle, calendar fallback.

### `exo_pool.set_schedule`
Create or update a schedule’s time range and optional VSP RPM.

```yaml
service: exo_pool.set_schedule
data:
  entity_id: binary_sensor.schedule_filter_pump_2
  start: "11:00"
  end: "23:00"
  rpm: 2000
```

You can also target the device and specify `schedule: sch6` instead of the entity:

```yaml
service: exo_pool.set_schedule
data:
  device_id: 1a2b3c4d5e6f7g8h9i0j
  schedule: sch6
  start: "11:00"
  end: "23:00"
```

### `exo_pool.disable_schedule`
Disable a schedule by setting start and end to `00:00`.

```yaml
service: exo_pool.disable_schedule
data:
  entity_id: binary_sensor.schedule_salt_water_chlorinator_2
```

### `exo_pool.reload`
Reload the integration. If you only have one Exo Pool entry, no data is required.

```yaml
service: exo_pool.reload
```

To target a specific entry or device:

```yaml
service: exo_pool.reload
data:
  entry_id: 8955375327824e14ba89e4b29cc3ec9a
```

---

## Device Actions (Automations)

When creating an automation:
**Device → your Exo Pool device → Actions**: *Set schedule* or *Disable schedule*.
These map directly to the services above.

---

## History

The core iAqualink integration never supported Exo devices (European Zodiac-branded chlorinators). See the long-running discussion: [flz/iaqualink-py#16](https://github.com/flz/iaqualink-py/discussions/16).
After early Node-RED flows and REST template hacks, this dedicated integration was built to provide full native support.

---

## Limitations

- Restricted to **Exo** devices only; use the core iAqualink integration for other hardware.
- Commands (set points, Aux switches, etc.) are near-instant via MQTT. If MQTT is unavailable, writes fall back to REST which may be subject to rate limits.
- Schedule keys, names and endpoints are determined by the device; disabling a schedule is modelled as `00:00–00:00`.
- RPM is only relevant to VSP schedules.
- The heat pump climate entity only appears when Aux 2 is set to heat mode.

---

## Compatibility

Confirmed working with:
- **Exo IQ LS** (dual-link ORP & pH, Zodiac VSP pump).

Have success with other models? Please share!

---

## Development

### Prerequisites

- Docker
- Python 3.9+
- A Zodiac iAqualink account with an eXO device

### Quick start

```bash
git clone https://github.com/benjycov/exo_pool.git
cd exo_pool

# Create .env with your Zodiac credentials
echo "EXO_EMAIL=your@email.com" > .env
echo "EXO_PASSWORD=yourpassword" >> .env

# Start a dev HA instance (auto-onboards, configures integration)
make dev

# Open http://localhost:8125 (login: dev / devdevdev)
```

### Useful commands

```bash
make test       # run unit + integration tests
make logs       # tail the HA container logs
make restart    # restart HA after code changes (volume-mounted, no rebuild)
make stop       # stop the container
```

### Running tests

```bash
pip install pytest pytest-asyncio pytest-timeout awsiotsdk pylint
python3 -m pytest tests/ -v
make lint-dup   # pylint duplicate-code check (across files only, not within one file)
```

`requirements-test.txt` is the human-edited top-level list. `requirements-test.lock` is
the fully pinned, hash-checked lock that CI, the Makefile and this install step all use.
Regenerate it after changing `requirements-test.txt` with `make test-lock-regen`.

### Verifying the MQTT outage-reconnect fix

`scripts/verify_outage_reconnect.py` drives the running dev container through
a simulated WAN outage - only ever against `ha-exo-pool-dev` on port 8125,
never a live instance. It never writes to any entity - only reads state and
blocks/unblocks network traffic. It runs four scenarios:

- **reconnect-from-connected** (issue #2's actual reproduction): MQTT is
  connected, every one of its actual established peers is blocked (and
  topped up if it reconnects to a new one mid-test), and the fix's retry
  chain must re-arm with growing backoff and recover.
- **interrupt-resume-recovers**: the common case - only the connection's
  current peer(s) are blocked, the CRT resumes via another address within
  seconds, the resubscribe fails on stale credentials, and the fix forces a
  refresh to recover.
- **watchdog**: the rare case - a total outbound block on port 443 (not by
  address) keeps resume from ever succeeding, so the fix's own interrupt
  watchdog has to force the reconnect itself after 180s.
- **setup-under-outage**: the same total outbound block, paired with the
  `/etc/hosts` blackhole, while the config entry is reloaded - a different
  code path (setup, not the reconnect chain) - and pins what that does,
  including recovery once the outage clears.

The #12 early-wake-on-reconnect behaviour (a held write retries the instant
MQTT reconnects rather than waiting out the rest of its cooldown) is unit
tested in `tests/test_api_write_manager_mqtt_throttle.py` instead of driven
through this harness - it doesn't need a real device write to prove, and an
earlier version of this harness that did force one against the dev
container's real chlorinator left it stuck off-target for hours. This
harness makes no writes to any entity, ever; a source-scan test in
`tests/test_verify_outage_reconnect.py` fails if a write service call is
ever reintroduced.

Before any scenario runs, `assert_mounted_code_is_loaded()` checks the
container against this checkout: that its mounted
`custom_components/exo_pool` resolves to this repo's copy (not some other
checkout's), and that `State.StartedAt` postdates the newest mtime under
it. Config-entry reloads re-run setup but never re-import Python modules,
so a container that predates an edit - or mounts a different checkout
entirely - keeps running the old code with no error, indistinguishable
from a real regression until you notice the mismatch. Run this script from
the same checkout `docker-compose.dev.yml` mounts into `ha-exo-pool-dev`;
if the mount points elsewhere, repoint it with
`docker compose -p exo_pool -f docker-compose.dev.yml up -d --force-recreate`
run from the checkout you want mounted. Restart with
`docker restart ha-exo-pool-dev` if only the staleness check fires.

Two blocking mechanisms, deliberately not unified: reconnect-from-connected
and interrupt-resume-recovers block by address - read from the container's
actual established TCP connections (`ss -tn state established`, filtered
to public, non-loopback peers on port 443), not a fresh DNS lookup, since
AWS IoT's endpoint rotates continuously and a connection from a few minutes
earlier is routinely pinned to an address no longer in the current DNS
answer. That's also the more realistic simulation of a partial network
failure, which is what those two scenarios model. watchdog and
setup-under-outage need a *guaranteed* outage instead - blocking every
address found doesn't survive the CRT reconnecting to a fresh one faster
than the block can chase it - so they block by port
(`iptables -p tcp --dport 443 -j DROP`) instead: deterministic regardless
of which address gets used next, with a single rule to add and remove
rather than a growing set. This harness can't be made fully deterministic
against a rotating cloud endpoint; reading the actual peer (or blocking by
port where an address list can't keep up) is what makes it reliable rather
than lucky.

```bash
export EXO_HARNESS_TOKEN=<HA long-lived access token for the dev instance>
# Create one at http://localhost:8125/profile/security (dev / devdevdev),
# or reuse the token scripts/dev-setup.py already saved to .dev-token.
python3 scripts/verify_outage_reconnect.py
```

All four scenarios need to block traffic and inspect connections, which
the HA dev image has no tools for. They run a sidecar (`docker run
--network container:ha-exo-pool-dev --cap-add NET_ADMIN ...`) that shares
the dev container's network namespace instead - no changes to the dev
container itself, so no recreate needed. The sidecar uses a local image
(built once, on normal networking, before any outage - `docker create` +
`apk add iptables iproute2` + `docker commit`, reused after that) rather
than installing those packages fresh on every call. That's not just an
optimisation: the port-based total block cuts all outbound HTTPS,
including the port `apk add` itself needs, so a sidecar call that tries to
install packages *during* that block can't ever remove it - it fetches
over the exact port it's supposed to be undoing. Removing the `apk add`
dependency entirely is what makes teardown actually work. The total-block
scenarios also verify the *removal* path specifically (a harmless
`iptables -L -n` right after the block goes up) before relying on it, and
if a rule still can't be removed after a few retries, the run aborts
immediately with the exact command to run by hand
(`docker restart ha-exo-pool-dev`) rather than continuing into a broken
state. It does need `docker run` access and network access to build the
image once. Each scenario skips with a clear message if that's
unavailable; pass `--skip-watchdog` to skip the watchdog one deliberately.

Every scenario is independent: before doing anything destructive it waits
for `binary_sensor.exo_pool_mqtt_connected` to be `on` *and* an established
MQTT peer to actually exist, reloading the entry once and retrying if
either isn't there yet - so a failure anywhere (this run or a stale state
left over from a previous one) can't cascade into failing every scenario
after it. The harness also guarantees the integration is left working when
it exits, reloading the entry if needed - if it can't get MQTT back on, it
says so loudly and tells you to restart the dev container.

---

## Support

- **Bugs / Feature Requests**: [GitHub Issues](https://github.com/benjycov/exo_pool/issues)
- **Q&A / Discussion**: [GitHub Discussions](https://github.com/benjycov/exo_pool/discussions)
