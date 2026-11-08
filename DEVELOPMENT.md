# Development

Developer documentation for the SSD IMS Home Assistant integration. User documentation is in Slovak in [README.md](README.md).

## Prerequisites

- Python 3.14+
- Docker and Docker Compose
- Home Assistant itself isn't needed: `make install` puts the version pinned in `requirements.txt` into `.venv`

## Setup

```bash
# Clone the repository
git clone https://github.com/baracudaz/ssd-ims.git
cd ssd-ims

# Create a .venv and install dependencies (needed for make test/lint/format below)
make install

# Start a Home Assistant container with this integration mounted, for manual/UI testing
make docker-up

# View logs
make docker-logs
```

The container uses the `stable` Home Assistant image. `make docker-restart` keeps the image that is already downloaded; to move to a new Home Assistant release, run `docker compose pull homeassistant && docker compose up -d homeassistant`.

## Tests and linting

```bash
make test           # run all tests
make test-coverage  # with coverage report
make check          # ruff format --check + ruff check (what CI runs)
make dev            # format, lint and test
```

The test harness is `pytest-homeassistant`, which Home Assistant core publishes with the same version as `homeassistant`; bump both together in `requirements.txt`.

## Portal API

The integration uses the SSD IMS portal API:

| Endpoint | Purpose |
|----------|---------|
| `/api/account/login` | Authentication |
| `/api/consumption-production/profile-data/get-points-of-delivery` | POD discovery |
| `/api/consumption-production/profile-data/chart-data` | Energy data retrieval |
| `/api/consumption-production/registry-data` | Tariff register readings (VT/NT, peak demand) |

## Contributing

1. Fork the repository
2. Create a feature branch
3. Make your changes with tests
4. Submit a pull request

Release notes go into `CHANGELOG.md` (in English) under `## Version X.Y.Z`, together with the `version` bump in `custom_components/ssd_ims/manifest.json`.
