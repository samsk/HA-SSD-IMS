"""Constants for SSD IMS integration."""
from typing import Final

# Domain
DOMAIN: Final = "ssd_ims"
NAME: Final = "SSD IMS"

# Configuration
CONF_USERNAME: Final = "username"
CONF_PASSWORD: Final = "password"
CONF_SCAN_INTERVAL: Final = "scan_interval"
CONF_POINT_OF_DELIVERY: Final = (
    "point_of_delivery"  # Now contains stable pod_ids instead of pod_texts
)
CONF_POD_NAME_MAPPING: Final = "pod_name_mapping"

# Defaults
DEFAULT_SCAN_INTERVAL: Final = 60
DEFAULT_POINT_OF_DELIVERY: Final = []

# Scan interval options (in minutes)
SCAN_INTERVAL_OPTIONS: Final = [15, 60]

# API endpoints
API_BASE_URL: Final = "https://ims.ssd.sk/api"
API_LOGIN: Final = f"{API_BASE_URL}/account/login"
API_PODS: Final = f"{API_BASE_URL}/consumption-production/profile-data/get-points-of-delivery"
API_DATA: Final = f"{API_BASE_URL}/consumption-production/profile-data"
API_CHART: Final = f"{API_BASE_URL}/consumption-production/profile-data/chart-data"

# Sensor types
SENSOR_TYPE_ACTUAL_CONSUMPTION: Final = "actual_consumption"
SENSOR_TYPE_ACTUAL_SUPPLY: Final = "actual_supply"
SENSOR_TYPE_IDLE_CONSUMPTION: Final = "idle_consumption"
SENSOR_TYPE_IDLE_SUPPLY: Final = "idle_supply"

SENSOR_TYPES: Final = [
    SENSOR_TYPE_ACTUAL_CONSUMPTION,
    SENSOR_TYPE_ACTUAL_SUPPLY,
    SENSOR_TYPE_IDLE_CONSUMPTION,
    SENSOR_TYPE_IDLE_SUPPLY,
]

# Time periods
PERIOD_YESTERDAY: Final = "yesterday"
PERIOD_LAST_WEEK: Final = "last_week"

TIME_PERIODS: Final = [
    PERIOD_YESTERDAY,
    PERIOD_LAST_WEEK,
]

# POD naming validation
POD_NAME_MAX_LENGTH: Final = 50
POD_NAME_PATTERN: Final = r"^[a-zA-Z0-9_]+$"  # alphanumeric + underscores only
