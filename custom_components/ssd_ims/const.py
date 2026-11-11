"""Constants for SSD IMS integration."""

from datetime import UTC, datetime, timedelta
from typing import Final

# Domain
DOMAIN: Final = "ssd_ims"
NAME: Final = "SSD IMS"

# Configuration
CONF_SCAN_INTERVAL: Final = "scan_interval"
CONF_POINT_OF_DELIVERY: Final = (
    "point_of_delivery"  # Now contains stable pod_ids instead of pod_texts
)
CONF_POD_NAME_MAPPING: Final = "pod_name_mapping"
CONF_ENABLE_HISTORY_IMPORT: Final = "enable_history_import"
CONF_HISTORY_DAYS: Final = "history_days"
# Optional per-kWh prices (incl. VAT) used to compute cost statistics for
# the monthly tariff register readings.
CONF_PRICE_VT: Final = "price_vt"
CONF_PRICE_NT: Final = "price_nt"
# Supplier integration whose per-point price sensors, when installed, take
# precedence over the prices entered in the options. Matched by unique_id
# "ELECTRICITY_<EIC>_price_<vt|nt|jt>" (JT = single-rate tariff); the EIC is
# this integration's POD ID.
SUPPLIER_PRICE_DOMAIN: Final = "sse_ezona"
# SSD operates in Slovakia, so tariff prices and costs are always in euros,
# regardless of the currency configured in Home Assistant.
CURRENCY: Final = "EUR"

# Defaults
DEFAULT_SCAN_INTERVAL: Final = 360  # 6 hours - sensible for daily data
DEFAULT_POINT_OF_DELIVERY: Final[tuple[str, ...]] = ()
DEFAULT_ENABLE_HISTORY_IMPORT: Final = True  # Enable by default
DEFAULT_HISTORY_DAYS: Final = 7  # Import last 7 days of past data by default

# Options - Data is only available up to yesterday and released once per day
# So polling frequently doesn't make sense
# Keys are the selectable intervals in minutes; the UI labels come from the
# "selector.scan_interval" translations, these are only fallbacks.
SCAN_INTERVAL_OPTIONS: Final = {
    5: "5 minutes (debugging only)",
    360: "6 hours (recommended)",
    720: "12 hours",
    1440: "24 hours",
}

# API delay configuration - random between min and max
API_DELAY_MIN: Final = 1  # minimum random delay in seconds
API_DELAY_MAX: Final = 3  # maximum random delay in seconds

# POD cache TTL - the coordinator seeds this cache right after discovery
# (see SsdImsApiClient.set_cached_pods), so this mainly bounds how long a
# long-running statistics backfill can go before PODs are re-discovered.
PODS_CACHE_TTL: Final = timedelta(minutes=60)

# API endpoints
PORTAL_URL: Final = "https://ims.ssd.sk"
API_BASE_URL: Final = f"{PORTAL_URL}/api"
API_LOGIN: Final = f"{API_BASE_URL}/account/login"
API_PODS: Final = (
    f"{API_BASE_URL}/consumption-production/profile-data/get-points-of-delivery"
)
API_CHART: Final = f"{API_BASE_URL}/consumption-production/profile-data/chart-data"
API_REGISTRY: Final = f"{API_BASE_URL}/consumption-production/registry-data"

# Tariff register readings (Registrové údaje) are published roughly once a
# month (00:00 local on the 1st, plus occasional ad-hoc readings). Each
# refresh re-reads the full history from this date — the portal has no
# maximum range for this endpoint and the whole history is only ~12 rows per
# year — and imports whatever is newer than the last imported statistic,
# which keeps the import idempotent. The first import thus covers everything
# since the meter was installed.
REGISTRY_HISTORY_START: Final = datetime(2010, 1, 1, tzinfo=UTC)

# Register-based statistic suffixes (appended to the sanitized POD name)
REGISTER_STAT_CONSUMPTION_VT: Final = "consumption_vt"
REGISTER_STAT_CONSUMPTION_NT: Final = "consumption_nt"
REGISTER_STAT_COST_VT: Final = "cost_vt"
REGISTER_STAT_COST_NT: Final = "cost_nt"

# Hourly cost for the hourly actual_consumption statistic: each hour's kWh x
# the blended price of its register period (VT/NT mix from the registers).
STAT_ACTUAL_CONSUMPTION_COST: Final = "actual_consumption_cost"
# Hours after the latest register reading are priced provisionally with the
# previous period's mix. Each run recomputes the costs from this many
# register periods back, so they're corrected once the closing reading
# (and any ad-hoc reading in between) is published.
COST_RECOMPUTE_PERIODS: Final = 3

# Manual reimport service (domain-level; registered once for all entries)
SERVICE_REIMPORT_STATISTICS: Final = "reimport_statistics"
ATTR_ENTRY_ID: Final = "entry_id"
ATTR_DATE: Final = "date"
# Repair issue while a structurally invalid day blocks forward import
ISSUE_IMPORT_PAUSED: Final = "import_paused"

# Sensor types
SENSOR_TYPE_ACTUAL_CONSUMPTION: Final = "actual_consumption"
SENSOR_TYPE_ACTUAL_SUPPLY: Final = "actual_supply"

SENSOR_TYPES: Final = [
    SENSOR_TYPE_ACTUAL_CONSUMPTION,
    SENSOR_TYPE_ACTUAL_SUPPLY,
]

SENSOR_TYPE_LABELS: Final = {
    SENSOR_TYPE_ACTUAL_CONSUMPTION: "Actual Consumption",
    SENSOR_TYPE_ACTUAL_SUPPLY: "Actual Supply",
}

# POD naming validation
POD_NAME_MAX_LENGTH: Final = 50
