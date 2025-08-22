# SSD IMS Home Assistant Integration

A custom Home Assistant integration for gathering energy consumption/production data from the SSD IMS portal (ims.ssd.sk) with 15-minute resolution.

## Data Characteristics & Limitations

**Data Freshness**: The SSD IMS portal (ims.ssd.sk) provides only day-old data that is published after midnight. This means:
- Data for the current day is not available
- The most recent data available is from yesterday
- Data is updated once daily after midnight

**Data Resolution**: All metering data has 15-minute resolution intervals.

**Current Functionality**: This component currently provides only summary (aggregated) data for:
- Yesterday (previous day totals)
- This week (current week totals)

*Note: Detailed 15-minute interval data access may be added in future versions.*

## Features

- **Energy Monitoring**: Track active and reactive power consumption/supply (day-old data)
- **Multiple Time Periods**: Data aggregation for yesterday and last week
- **Multiple POD Support**: Monitor multiple Points of Delivery simultaneously
- **Custom POD Names**: Set friendly names for your Points of Delivery
- **8 Sensors per POD**: Complete coverage of all energy metrics across different time periods
- **Automatic POD Discovery**: Automatically discovers available points of delivery
- **Efficient Data Fetching**: Incremental updates with smart caching
- **Robust Error Handling**: Comprehensive error handling and retry logic

## Supported Sensors

The integration creates 8 sensors **per Point of Delivery** covering all combinations of:

**Energy Types:**
- Active Consumption (kWh)
- Active Supply (kWh)
- Idle Consumption (kVARh)
- Idle Supply (kVARh)

**Time Periods:**
- Yesterday
- Last Week

**Sensor Naming Examples:**

With friendly name "house1":
- `sensor.house1_active_consumption_yesterday`
- `sensor.house1_active_consumption_last_week`
- `sensor.house1_active_supply_yesterday`
- `sensor.house1_active_supply_last_week`
- `sensor.house1_idle_consumption_yesterday`
- `sensor.house1_idle_consumption_last_week`
- `sensor.house1_idle_supply_yesterday`
- `sensor.house1_idle_supply_last_week`

With auto-generated name (if no friendly name set):
- `sensor.99XXX1234560000G_active_consumption_yesterday`
- `sensor.99XXX1234560000G_active_consumption_last_week`
- `sensor.99XXX1234560000G_active_supply_yesterday`
- `sensor.99XXX1234560000G_active_supply_last_week`
- `sensor.99XXX1234560000G_idle_consumption_yesterday`
- `sensor.99XXX1234560000G_idle_consumption_last_week`
- `sensor.99XXX1234560000G_idle_supply_yesterday`
- `sensor.99XXX1234560000G_idle_supply_last_week`

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
   - **Scan Interval**: Update frequency (15 or 60 minutes, default: 60)
5. Select your Points of Delivery (PODs) to monitor
6. Optionally set friendly names for your PODs (e.g., "house1", "garage", "solar")
7. Review the sensor preview and confirm configuration

### Via YAML

```yaml
# configuration.yaml
ssd_ims:
  username: !secret ssd_username
  password: !secret ssd_password
  scan_interval: 60  # minutes (15 or 60)
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
   - Verify the scan interval is appropriate
   - Check Home Assistant logs for errors

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
