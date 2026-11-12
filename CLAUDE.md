# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A Home Assistant custom integration (installed through HACS, which Home Assistant 2026.11+ has built in as the Marketplace — same catalog, same `hacs.json`) for the SSD IMS energy portal (`https://ims.ssd.sk`). It logs into the portal, discovers Points of Delivery (PODs), and imports hourly consumption/supply data into Home Assistant's long-term statistics for the Energy dashboard. All integration code lives in `custom_components/ssd_ims/`.

## Commands

```bash
make install         # create .venv and install requirements.txt (pins all dev/test deps)
make format           # ruff format .
make lint             # ruff check . --fix
make check            # format --check + lint, no auto-fix (what CI runs)
make test             # pytest tests/ -v --asyncio-mode=auto
make test-coverage    # pytest with coverage report (term + htmlcov/)
make dev              # format + lint + test

make docker-up        # start a real Home Assistant instance in Docker with this
                       # integration mounted, for manual/UI testing
make docker-logs
make docker-down
```

Run a single test: `.venv/bin/python -m pytest tests/test_api_client.py::TestSsdImsApiClient::TestAuthentication::test_successful_authentication -v`

`asyncio_mode = "auto"` is set in `pytest.ini`, so async test functions don't need `@pytest.mark.asyncio`. The test harness is `pytest-homeassistant`, published by Home Assistant core with the same version as `homeassistant` (bump both together); it loads automatically and needs `-p no:respx` in `pytest.ini`, otherwise every test fails with "httpx was already imported". The older community `pytest-homeassistant-custom-component` is discontinued — never install both. There is no `pyproject.toml` in this repo — `requirements.txt` is the single, pinned source of truth for dev/test dependencies, and neither Home Assistant's integration loader nor HACS reads project-level Python packaging metadata (they only read `custom_components/ssd_ims/manifest.json` and `hacs.json`), so there was nothing else it would have been needed for.

`scripts/develop` runs Home Assistant directly (non-Docker) against `./config`, with `custom_components/` on `PYTHONPATH`.

CI (`.github/workflows/lint.yml`) runs `ruff check .` and `ruff format . --check` on every push/PR to `master`. `.github/workflows/validate.yml` runs `hassfest` and the HACS validation action — both check integration structure/metadata (`manifest.json`, `hacs.json`), not code logic.

## Architecture

Standard Home Assistant config-entry integration, coordinator-based:

- **`api_client.py`** — `SsdImsApiClient` wraps the SSD IMS REST API (login, POD discovery, chart/metering data). Handles session-cookie auth (`SsdAccessToken`), transparent re-authentication when a session expires (detected via 401 or an HTML response body where JSON was expected), and retry-with-backoff for network errors. Caches the POD list for `PODS_CACHE_TTL` (5 min).
- **`coordinator.py`** — `SsdImsDataCoordinator` (`DataUpdateCoordinator`) is the only thing that talks to the API client at runtime. Each refresh:
  1. Discovers PODs if not already cached on the coordinator.
  2. For each configured POD and each enabled sensor type, walks day-by-day from the last imported statistic (or `history_days` back, on first run) through yesterday, fetching chart data and pushing hourly-aggregated sums into HA's external statistics via `async_add_external_statistics` (statistic id: `ssd_ims:<sanitized_pod_name>_<sensor_type>`).
  3. Fetches "yesterday" chart data per POD for the yesterday-total sensors.
  4. Reads back cumulative sums from HA's statistics store for the `_total` sensors.
  - **Smart polling / daily slot**: first fetch after local 03:05 + per-instance jitter (0–30 min). Incomplete yesterday retries on the configured scan interval (6/12/24 h) with no cap. Once complete, wait until tomorrow's same slot and `_last_successful_data_date` skips further API calls that calendar day. Setup does not await a download; a background refresh runs after platforms load. `ssd_ims.refresh` forces an immediate fetch. Empty unpublished yesterday shows `0` on Yesterday sensors (optional unknown via `CONF_UNPUBLISHED_AS_UNKNOWN`).
- **`__init__.py`** — `async_setup_entry` authenticates, builds the coordinator, does the first refresh, then forwards to the `sensor` platform. `async_migrate_entry` handles a v1→v2 migration converting old session-scoped POD identifiers into stable POD IDs (see below).
- **`sensor.py`** — Creates 5 entities per configured POD: `Actual Consumption Yesterday`, `Actual Supply Yesterday`, `Actual Consumption Total`, `Actual Supply Total` (all read from `coordinator.data`), and `Last Update`. Entities don't poll themselves — they're `CoordinatorEntity` and just read whatever the coordinator last computed.
- **`config_flow.py`** — Multi-step UI flow: credentials → POD selection → per-POD friendly naming → update interval/history-import settings. Also implements `async_step_reauth` (triggered by `ConfigEntryAuthFailed`) and `async_step_reconfigure` (same step sequence, pre-filled with current values, reusing the same account). `SsdImsOptionsFlow` only exposes the scan interval and pushes changes live via `coordinator.update_config`.
- **`diagnostics.py`** — Redacts `CONF_USERNAME`/`CONF_PASSWORD`; exposes discovered PODs and coordinator data.
- **`helpers.py`** — `sanitize_name` (used to build statistic IDs / unique IDs from user-supplied POD names) and `calculate_yesterday_range`.
- **`models.py`** — Pydantic models for the portal's JSON responses.

### Stable POD IDs

The portal's session-scoped POD identifier (`pod.value`, sent to the API) changes between logins. `PointOfDelivery.id` extracts a stable 16–20 char alphanumeric ID by parsing it out of `pod.text` (e.g. `"99XXX1234560000G (Rodinný dom)"`). Config entry data and entity unique IDs key off `pod.id`, never `pod.value`. The API client resolves `pod.id` → current `pod.value` on demand via the cached POD list before each request. `async_migrate_entry` in `__init__.py` converts config entries created before this scheme existed.

**Exception: external statistic IDs are keyed off the friendly POD name, not `pod.id`.** `SsdImsDataCoordinator._build_statistic_id` derives `ssd_ims:<sanitized_pod_name>_<sensor_type>` from `CONF_POD_NAME_MAPPING` (falling back to `pod.id` when no friendly name is set). This is a deliberate, currently-accepted limitation: an earlier pod_id-keyed design (with a rename-on-upgrade migration path for existing friendly-name-based installs) was abandoned after discovering that Home Assistant core's statistics-rename primitive (`recorder.statistics.async_update_statistics_metadata`) silently no-ops for external, non-recorder-source statistics like ours — it filters the underlying `UPDATE` by the recorder's own hardcoded domain, so the query matches zero rows and nothing actually happens, without raising an error. Since no published version of this integration has ever used a pod_id-keyed scheme, there's nothing to migrate away from, so the simpler (if less robust) friendly-name-based scheme was kept. Consequence: renaming a POD's friendly name (via initial setup or Reconfigure) changes its statistic_id and starts a new series — the config flow doesn't warn about this.

### Data flow gotcha

The portal only ever has data through "yesterday", published overnight — the first fetch is around 03:05+jitter; the configured scan interval is the incomplete-day retry cadence. The 5-minute `SCAN_INTERVAL_OPTIONS` entry is debug-only and offered in the UI only when `SSD_IMS_DEBUG` is set.

## Conventions

- Python 3.14+ (the pinned `homeassistant` release in `requirements.txt` requires it; the integration code itself has no such floor — it runs inside whatever Python the end user's Home Assistant core provides), Pydantic v2 for API response models, `ruff` for lint/format (no separate mypy/pylint config in this repo despite `.github/copilot-instructions.md` referencing them — that file documents Home Assistant core's own conventions and quality-scale process; only the general HA integration patterns it describes apply here, not its literal tool invocations).
- Config-critical data (credentials, POD selection, naming) lives in `ConfigEntry.data`; `scan_interval` and `unpublished_as_unknown` are adjustable post-setup via `ConfigEntry.options`/the options flow.
- Polling interval is user-configurable in this integration's config flow (`SCAN_INTERVAL_OPTIONS`) — this is an intentional exception to the usual HA guidance against configurable scan intervals, because the source data's daily publish cadence makes it a meaningful, bounded choice for the user.
- Documentation language: `README.md` is user documentation in **Slovak** (the Marketplace/HACS page shows only `README.md`, and users are in Slovakia); keep statistic/entity IDs, code terms and HA menu names as they appear in the Slovak HA UI. `DEVELOPMENT.md`, `CHANGELOG.md`, issue templates, commits and PRs stay in English. When a change affects users, update the Slovak README.
- `CHANGELOG.md` is maintained by hand per release, grouped under `## Version X.Y.Z` with bolded change-type prefixes (`**New feature**`, `**Bug fix**`, `**Internal**`, `**Metadata**`, `**Docs**`, `**Maintenance**`). Bump `manifest.json` `"version"` alongside changelog entries.
