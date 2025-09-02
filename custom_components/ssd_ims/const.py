"""Constants for SSD IMS integration."""
from datetime import datetime, timedelta
from typing import Final, Tuple

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
CONF_ENABLE_SUPPLY_SENSORS: Final = "enable_supply_sensors"
CONF_ENABLE_IDLE_SENSORS: Final = "enable_idle_sensors"

# Defaults
DEFAULT_SCAN_INTERVAL: Final = 60
DEFAULT_POINT_OF_DELIVERY: Final = []
DEFAULT_ENABLE_SUPPLY_SENSORS: Final = True
DEFAULT_ENABLE_IDLE_SENSORS: Final = False

# Options
SCAN_INTERVAL_OPTIONS: Final = {
    60: "1 hour",
    120: "2 hours",
    180: "3 hours",
    240: "4 hours",
}

# API delay configuration - random between min and max
API_DELAY_MIN: Final = 1  # minimum random delay in seconds
API_DELAY_MAX: Final = 3  # maximum random delay in seconds

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


# Date range calculation callbacks
def _calculate_days_range(now: datetime, days: int) -> Tuple[datetime, datetime]:
    """Calculate date range for a given number of days ending yesterday."""
    yesterday_end = now.replace(hour=0, minute=0, second=0, microsecond=0)
    period_start = yesterday_end - timedelta(days=days)
    return period_start, yesterday_end


def _calculate_this_week_range(now: datetime) -> Tuple[datetime, datetime]:
    """Calculate date range for current week from Monday to tomorrow midnight."""
    # Find the most recent Monday
    days_since_monday = now.weekday()  # Monday = 0, Sunday = 6
    monday_start = now.replace(
        hour=0, minute=0, second=0, microsecond=0
    ) - timedelta(days=days_since_monday)
    
    # Tomorrow midnight (end of tomorrow)
    tomorrow_end = now.replace(
        hour=0, minute=0, second=0, microsecond=0
    ) + timedelta(days=2)
    
    return monday_start, tomorrow_end


def _calculate_this_month_range(now: datetime) -> Tuple[datetime, datetime]:
    """Calculate date range for current month from 1st to tomorrow midnight."""
    # First day of current month
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    
    # Tomorrow midnight
    tomorrow_end = now.replace(
        hour=0, minute=0, second=0, microsecond=0
    ) + timedelta(days=2)
    
    return month_start, tomorrow_end

# Time periods configuration
PERIOD_YESTERDAY: Final = "yesterday"
PERIOD_LAST_2_DAYS: Final = "last_2_days"
PERIOD_LAST_3_DAYS: Final = "last_3_days"
PERIOD_LAST_7_DAYS: Final = "last_7_days"
PERIOD_THIS_WEEK: Final = "this_week"
PERIOD_THIS_MONTH: Final = "this_month"
PERIOD_LAST_30_DAYS: Final = "last_30_days"

# Time periods configuration with callbacks for date range calculation
TIME_PERIODS_CONFIG: Final = {
    PERIOD_YESTERDAY: {
        "display_name": "Yesterday",
        "description": "Previous day",
        "calculate_range": lambda now: _calculate_days_range(now, 1),
    },
    PERIOD_LAST_2_DAYS: {
        "display_name": "Last 2 Days", 
        "description": "Previous 2 days ending yesterday",
        "calculate_range": lambda now: _calculate_days_range(now, 2),
    },
    PERIOD_LAST_3_DAYS: {
        "display_name": "Last 3 Days",
        "description": "Previous 3 days ending yesterday",
        "calculate_range": lambda now: _calculate_days_range(now, 3),
    },
    PERIOD_LAST_7_DAYS: {
        "display_name": "Last 7 Days",
        "description": "Previous 7 days ending yesterday",
        "calculate_range": lambda now: _calculate_days_range(now, 7),
    },
    PERIOD_THIS_WEEK: {
        "display_name": "This Week",
        "description": "Current week from Monday to tomorrow midnight",
        "calculate_range": _calculate_this_week_range,
    },
    PERIOD_THIS_MONTH: {
        "display_name": "This Month",
        "description": "Current month from 1st to tomorrow midnight",
        "calculate_range": _calculate_this_month_range,
    },
    PERIOD_LAST_30_DAYS: {
        "display_name": "Last 30 Days",
        "description": "Previous 30 days ending yesterday",
        "calculate_range": lambda now: _calculate_days_range(now, 30),
    },
}

# List of time periods (keys from config)
TIME_PERIODS: Final = list(TIME_PERIODS_CONFIG.keys())

# POD naming validation
POD_NAME_MAX_LENGTH: Final = 50
POD_NAME_PATTERN: Final = r"^[a-zA-Z0-9_]+$"  # alphanumeric + underscores only
