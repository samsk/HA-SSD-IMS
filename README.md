# SSD IMS Home Assistant Integration

A custom Home Assistant integration for gathering energy consumption/production data from the SSD IMS portal (ims.ssd.sk) with 15-minute resolution and comprehensive time period coverage.

## Data Characteristics & Capabilities

**Data Freshness**: The SSD IMS portal (ims.ssd.sk) provides day-old data that is published after midnight. This means:
- Data for the current day is not available
- The most recent data available is from yesterday
- Data is updated once daily after midnight
- Integration updates every 60 minutes by default for optimal history tracking

**Data Resolution**: All metering data has 15-minute resolution intervals from the source.

**Time Period Coverage**: The integration provides configurable summary (aggregated) data across multiple time periods for comprehensive energy monitoring and historical analysis.

*Note: The callback-based time period system allows easy extension for additional periods without code changes.*

## Features

- **Energy Monitoring**: Track active and reactive power consumption/supply (day-old data)
- **Configurable Time Periods**: Flexible time period system with 7 built-in periods
- **Enhanced History Tracking**: Optimized for Home Assistant dashboard history visualization
- **Multiple POD Support**: Monitor multiple Points of Delivery simultaneously
- **Custom POD Names**: Set friendly names for your Points of Delivery
- **Comprehensive Sensor Coverage**: Multiple sensors per POD covering all energy metrics and time periods
- **Automatic POD Discovery**: Automatically discovers available points of delivery
- **Callback-Based Architecture**: Extensible time period system for easy customization
- **Efficient Data Fetching**: Smart API usage with period-specific data aggregation
- **Robust Error Handling**: Comprehensive error handling and retry logic

## Supported Sensors

The integration creates **28 sensors per Point of Delivery** covering all combinations of:

**Energy Types:**
- Active Consumption (kWh)
- Active Supply (kWh)
- Idle Consumption (kVARh)
- Idle Supply (kVARh)

**Time Periods:**
- Yesterday (1 day)
- Last 2 Days (2 days)
- Last 3 Days (3 days)
- Last 7 Days (7 days)
- This Week (Monday to tomorrow midnight)
- This Month (1st to tomorrow midnight)
- Last 30 Days (30 days)

**Enhanced History Tracking**: The multiple time periods provide rich historical data for Home Assistant dashboards, enabling detailed energy consumption analysis and trend visualization.

**Sensor Naming Examples:**

With friendly name "house1":
- `sensor.house1_active_consumption_yesterday`
- `sensor.house1_active_consumption_last_2_days`
- `sensor.house1_active_consumption_last_3_days`
- `sensor.house1_active_consumption_last_7_days`
- `sensor.house1_active_consumption_this_week`
- `sensor.house1_active_consumption_this_month`
- `sensor.house1_active_consumption_last_30_days`
- `sensor.house1_active_supply_yesterday`
- `sensor.house1_active_supply_this_week`
- `sensor.house1_idle_consumption_yesterday`
- `sensor.house1_idle_consumption_this_week`
- `sensor.house1_idle_supply_yesterday`
- `sensor.house1_idle_supply_this_week`
- *(... and so on for all combinations)*

With auto-generated name (if no friendly name set):
- `sensor.99XXX1234560000G_active_consumption_yesterday`
- `sensor.99XXX1234560000G_active_consumption_this_week`
- `sensor.99XXX1234560000G_active_supply_yesterday`
- `sensor.99XXX1234560000G_idle_consumption_last_7_days`
- *(... and so on for all combinations)*

## Installation

### Method 1: Manual Installation

1. Download this repository
2. Copy the `custom_components/ssd_ims` folder to your Home Assistant `config/custom_components/` directory
3. Restart Home Assistant
4. Add the integration via the UI

### Method 2: Development Setup

```bash
# Clone the repository
git clone <repository-url>
cd ha-ssd-ims

# Install development dependencies
make install

# Start Home Assistant container
make docker-up

# Deploy integration
make deploy
```

## Configuration

### Via UI (Recommended)

1. Go to **Settings** → **Devices & Services**
2. Click **Add Integration**
3. Search for "SSD IMS"
4. Enter your credentials:
   - **Username**: Your SSD IMS portal username
   - **Password**: Your SSD IMS portal password
   - **Scan Interval**: Update frequency (60 or 120 minutes, default: 60)
5. Select your Points of Delivery (PODs) to monitor
6. Optionally set friendly names for your PODs (e.g., "house1", "garage", "solar")
7. Review the sensor preview and confirm configuration

### Via YAML

```yaml
# configuration.yaml
ssd_ims:
  username: !secret ssd_username
  password: !secret ssd_password
  scan_interval: 60  # minutes (60 or 120)
  point_of_delivery: ["pod_id_1", "pod_id_2"]  # List of POD IDs
  pod_name_mapping:
    "pod_id_1": "house1"
    "pod_id_2": "garage"
```

```yaml
# secrets.yaml
ssd_username: "your_username"
ssd_password: "your_password"
```

## Time Periods Configuration

### Built-in Time Periods

The integration includes 7 configurable time periods designed for comprehensive energy monitoring:

| Period | Duration | Description |
|--------|----------|-------------|
| `yesterday` | 1 day | Previous day totals |
| `last_2_days` | 2 days | Previous 2 days ending yesterday |
| `last_3_days` | 3 days | Previous 3 days ending yesterday |
| `last_7_days` | 7 days | Previous 7 days ending yesterday |
| `this_week` | Variable | Current week from Monday to tomorrow midnight |
| `this_month` | Variable | Current month from 1st to tomorrow midnight |
| `last_30_days` | 30 days | Previous 30 days ending yesterday |

### Callback-Based Architecture

The time periods system uses a flexible callback-based architecture that allows:

- **Easy Extension**: Add new time periods without modifying core code
- **Custom Logic**: Implement complex date calculations for special periods
- **Reusable Components**: Share common date calculation logic
- **Type Safety**: All callbacks follow consistent interfaces

### Adding Custom Time Periods

To add a new time period (e.g., "Last Month"), simply modify `custom_components/ssd_ims/const.py`:

```python
# 1. Add period constant
PERIOD_LAST_MONTH: Final = "last_month"

# 2. Add callback function
def _calculate_last_month_range(now: datetime) -> Tuple[datetime, datetime]:
    """Calculate previous calendar month range."""
    # Custom calculation logic here
    return start_date, end_date

# 3. Add to configuration
PERIOD_LAST_MONTH: {
    "display_name": "Last Month",
    "description": "Previous calendar month",
    "calculate_range": _calculate_last_month_range,
}
```

No changes required in other files - the system automatically picks up new periods!

## API Rate Limiting

### Built-in Protection

The integration includes automatic API rate limiting to prevent abuse and ensure reliable operation:

- **Smart Timing**: Delays are applied between time periods and between PODs
- **Efficient Batching**: No delay for the first request in each batch
- **Automatic**: No user configuration required - works out of the box

### How It Works

The integration makes multiple API calls during each update cycle:
- **7 calls per POD** (one for each time period)
- **Additional random delays** between PODs when monitoring multiple delivery points

### Benefits

- **API Courtesy**: Prevents overwhelming the SSD IMS portal
- **Reliable Operation**: Reduces chance of rate limiting or blocking
- **Randomized Traffic**: Avoids predictable load patterns
- **Maintenance-Free**: No configuration needed - works automatically

## API Integration

The integration connects to the SSD IMS portal API endpoints:

- **Authentication**: `POST https://ims.ssd.sk/api/account/login`
- **POD Discovery**: `GET https://ims.ssd.sk/api/consumption-production/profile-data/get-points-of-delivery`
- **Metering Data**: `POST https://ims.ssd.sk/api/consumption-production/profile-data`
- **Chart Data**: `POST https://ims.ssd.sk/api/consumption-production/profile-data/chart-data`

## Development

### Prerequisites

- Python 3.9+
- Docker and Docker Compose
- Home Assistant 2025.2+

### Setup Development Environment

```bash
# Install dependencies
make install

# Start development environment
make dev-setup

# Run tests
make test

# Format code
make format

# Lint code
make lint
```

### Available Commands

```bash
# Development
make install          # Install dependencies
make format           # Format code
make lint             # Run linting
make clean            # Clean up files

# Testing
make test             # Run all tests
make test-unit        # Run unit tests only
make test-coverage    # Run tests with coverage
make test-watch       # Run tests in watch mode

# Docker
make docker-up        # Start Home Assistant
make docker-down      # Stop Home Assistant
make docker-logs      # Show logs
make docker-shell     # Open shell in container

# Deployment
make build            # Build package
make deploy           # Deploy to Home Assistant
make validate         # Validate configuration
```

### Project Structure

```
ha-ssd-ims/
├── custom_components/ssd_ims/
│   ├── __init__.py           # Main integration
│   ├── config_flow.py        # Configuration flow
│   ├── coordinator.py        # Data coordinator
│   ├── sensor.py            # Sensor platform
│   ├── api_client.py        # API client
│   ├── models.py            # Data models
│   ├── const.py             # Constants
│   └── manifest.json        # Integration manifest
├── tests/                   # Test suite
├── config/                  # Home Assistant config
├── docker-compose.yml       # Development environment
├── Makefile                 # Development commands
├── requirements-test.txt    # Test dependencies
└── README.md               # This file
```

## Testing

The integration includes comprehensive tests:

```bash
# Run all tests
pytest tests/ -v

# Run with coverage
pytest tests/ --cov=custom_components.ssd_ims --cov-report=html

# Run specific test
pytest tests/test_api_client.py::TestSsdImsApiClient::TestAuthentication -v
```

## Troubleshooting

### Common Issues

1. **Authentication Failed**
   - Verify your username and password
   - Check if your account is active
   - Ensure you have access to the SSD IMS portal

2. **No Data Available**
   - Check if your POD has recent metering data
   - Verify the scan interval is appropriate (60-120 minutes)
   - Check Home Assistant logs for errors
   - Note: With 28 sensors per POD, initial data loading may take up to 2 minutes

3. **Connection Errors**
   - Verify internet connectivity
   - Check if the SSD IMS portal is accessible
   - Review network firewall settings

### Logs

Enable debug logging in Home Assistant:

```yaml
# configuration.yaml
logger:
  default: info
  logs:
    custom_components.ssd_ims: debug
```

## Contributing

1. Fork the repository
2. Create a feature branch
3. Make your changes
4. Add tests for new functionality
5. Run the test suite
6. Submit a pull request

## License

This project is licensed under the MIT License - see the LICENSE file for details.

## Support

For issues and questions:

1. Check the troubleshooting section
2. Review Home Assistant logs
3. Open an issue on GitHub
4. Provide detailed information about your setup

## Changelog

### Version 1.1.0
- **Callback-Based Time Periods**: Completely refactored time period system with configurable callbacks
- **Enhanced History Tracking**: 7 built-in time periods for better Home Assistant dashboard visualization
- **Improved Sensor Coverage**: 28 sensors per POD (up from 8) across all time periods
- **Extensible Architecture**: Easy addition of custom time periods without code changes
- **Better Data Organization**: Replaced "last_week" with "last_7_days" for clarity
- **New Time Periods**: Added "This Week", "This Month", "Last 2/3 Days" periods
- **Optimized Updates**: Adjusted scan intervals for better performance (60-120 minutes)
- **Zero Code Duplication**: Eliminated redundant date calculation logic
- **Type Safety**: Consistent callback interfaces with proper type hints
- **Maintenance-Free**: No user configuration needed for API rate limiting

### Version 1.0.0
- Initial release
- Support for multiple Points of Delivery
- Support for all energy metrics (active and reactive power)
- Time period aggregation for yesterday and last week
- Automatic POD discovery
- Custom POD naming functionality
- Enhanced configuration flow with POD selection and naming
- Comprehensive error handling
- Multi-POD support with stable POD IDs
