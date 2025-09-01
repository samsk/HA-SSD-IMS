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
   - Enable debug logging (see Debug Logging section below)
   - Check Home Assistant logs for errors
   - Note: With 28 sensors per POD, initial data loading may take up to 2 minutes

3. **Connection Errors**
   - Verify internet connectivity
   - Check if the SSD IMS portal is accessible
   - Review network firewall settings

### Debug Logging

Enable comprehensive debug logging for the SSD IMS integration:

```yaml
# configuration.yaml
logger:
  default: info
  logs:
    custom_components.ssd_ims: debug
```

**What debug logging shows:**
- API authentication attempts and responses
- POD discovery and data fetching operations  
- Time period calculations and date ranges
- Rate limiting delays and API call timing
- Error details and retry attempts
- Sensor updates and data processing

**Viewing logs:**
1. Go to **Settings** → **System** → **Logs**
2. Filter by "ssd_ims" to see integration-specific entries
3. Or check `home-assistant.log` file directly

### Browser Console Debugging

For advanced debugging of the SSD IMS portal API, you can inspect and test chart-data responses directly in your browser:

#### Getting Chart Data via Console

1. **Login to SSD IMS Portal:**
   - Open https://ims.ssd.sk in your browser
   - Login with your credentials
   - Navigate to consumption/production data page

2. **Open Developer Tools:**
   - Press `F12` or right-click → "Inspect Element"
   - Go to **Console** tab

3. **Get Chart Data:**
```javascript
// ⚠️ EDIT THIS: Replace with your POD ID (stable 16-20 char identifier)
var TARGET_POD_ID = "99XXX1234560000G";

// Dynamic yesterday calculation with automatic session POD lookup
(() => {
  var now = new Date();
  var yesterday = new Date(now);
  yesterday.setDate(now.getDate() - 1);
  
  var dateFrom = yesterday.toISOString().split('T')[0];
  var dateTo = yesterday.toISOString().split('T')[0];
  
  console.log(`Getting yesterday's data: ${dateFrom} for POD: ${TARGET_POD_ID}`);
  
  // First get POD session data
  fetch('/api/consumption-production/profile-data/get-points-of-delivery', {
    method: 'GET',
    headers: { 'X-Requested-With': 'XMLHttpRequest' }
  })
  .then(response => {
    if (response.status === 401) {
      throw new Error('❌ Not logged in - please login to SSD IMS portal first');
    }
    if (!response.ok) {
      throw new Error(`❌ HTTP ${response.status}: ${response.statusText}`);
    }
    return response.json();
  })
  .then(pods => {
    // Find our target POD in the session data
    var targetPod = pods.find(pod => pod.text.includes(TARGET_POD_ID));
    if (!targetPod) {
      throw new Error(`❌ POD ${TARGET_POD_ID} not found. Available: ${pods.map(p => p.text).join(', ')}`);
    }
    
    console.log(`✅ Found POD session data: ${targetPod.text} (Session ID: ${targetPod.value})`);
    
    // Now get chart data with session POD ID
    return fetch('/api/consumption-production/profile-data/chart-data', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'X-Requested-With': 'XMLHttpRequest'
      },
      body: JSON.stringify({
        pointOfDeliveryId: targetPod.value,
        validFromDate: dateFrom + "T00:00:00",
        validToDate: dateTo + "T23:59:59",
        pointOfDeliveryText: targetPod.text
      })
    });
  })
  .then(response => {
    if (response.status === 401) {
      throw new Error('❌ Not logged in - please login to SSD IMS portal first');
    }
    if (!response.ok) {
      throw new Error(`❌ HTTP ${response.status}: ${response.statusText}`);
    }
    return response.json();
  })
  .then(data => {
    console.log('Yesterday chart data:', data);
    var jsonString = JSON.stringify(data, null, 2);
    navigator.clipboard.writeText(jsonString)
      .then(() => console.log('✅ Data copied to clipboard'))
      .catch(err => {
        console.warn('❌ Clipboard failed:', err.message);
        console.log('📋 Chart data (copy manually):');
        console.log(jsonString);
      });
  })
  .catch(error => console.error('Error:', error));
})();
```

**Alternative Simple Approach (if you prefer):**
```javascript
// ⚠️ EDIT THESE: Replace with your values
var TARGET_POD_ID = "99XXX1234560000G";
var START_DATE = "2024-01-15";
var END_DATE = "2024-01-21";

// Simple version with automatic session POD lookup
fetch('/api/consumption-production/profile-data/get-points-of-delivery', {
  method: 'GET',
  headers: { 'X-Requested-With': 'XMLHttpRequest' }
})
.then(response => {
  if (response.status === 401) {
    throw new Error('❌ Not logged in - please login to SSD IMS portal first');
  }
  if (!response.ok) {
    throw new Error(`❌ HTTP ${response.status}: ${response.statusText}`);
  }
  return response.json();
})
.then(pods => {
  var targetPod = pods.find(pod => pod.text.includes(TARGET_POD_ID));
  if (!targetPod) {
    throw new Error(`❌ POD ${TARGET_POD_ID} not found. Available: ${pods.map(p => p.text).join(', ')}`);
  }
  
  return fetch('/api/consumption-production/profile-data/chart-data', {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'X-Requested-With': 'XMLHttpRequest'
    },
    body: JSON.stringify({
      pointOfDeliveryId: targetPod.value,
      validFromDate: START_DATE + "T00:00:00",
      validToDate: END_DATE + "T23:59:59",
      pointOfDeliveryText: targetPod.text
    })
  });
})
.then(response => {
  if (response.status === 401) {
    throw new Error('❌ Not logged in - please login to SSD IMS portal first');
  }
  if (!response.ok) {
    throw new Error(`❌ HTTP ${response.status}: ${response.statusText}`);
  }
  return response.json();
})
.then(data => {
  console.log('Chart data:', data);
  navigator.clipboard.writeText(JSON.stringify(data, null, 2))
    .then(() => console.log('✅ Copied to clipboard'))
    .catch(() => console.log('📋 Copy failed, data printed above'));
})
.catch(error => console.error('Error:', error));
```

#### Finding Your POD ID and Session Data

```javascript
// Get all available PODs with session data needed for chart-data requests
fetch('/api/consumption-production/profile-data/get-points-of-delivery', {
  method: 'GET',
  headers: {
    'X-Requested-With': 'XMLHttpRequest'
  }
})
.then(response => {
  if (response.status === 401) {
    throw new Error('❌ Not logged in - please login to SSD IMS portal first');
  }
  if (!response.ok) {
    throw new Error(`❌ HTTP ${response.status}: ${response.statusText}`);
  }
  return response.json();
})
.then(pods => {
  console.log('Available PODs with session data:');
  pods.forEach(pod => {
    console.log(`POD: ${pod.text}`);
    console.log(`  - Session ID: ${pod.value}`);
    console.log(`  - Use in chart-data requests:`);
    console.log(`    pointOfDeliveryId: "${pod.value}"`);
    console.log(`    pointOfDeliveryText: "${pod.text}"`);
    console.log('---');
  });
})
.catch(error => console.error('Error:', error));
```

**Tips for Console Debugging:**
- Ensure you're logged into the portal before running scripts
- Check Network tab to see actual API calls made by the portal
- Use `JSON.stringify(data, null, 2)` for pretty-printed output
- Copy responses to external tools for analysis
- Monitor rate limiting by spacing requests apart



## Contributing

1. Fork the repository
2. Create a feature branch
3. Make your changes
4. Add tests for new functionality
5. Run the test suite
6. Submit a pull request

## License

This project is licensed under the AGPLv3 License - see the LICENSE file for details.

## Support

For issues and questions:

1. Check the troubleshooting section
2. Review Home Assistant logs
3. Open an issue on GitHub
4. Provide detailed information about your setup

## Changelog

### Version 1.1.2
- **Fixed Data Fetching Issues**: Added missing `X-Requested-With: XMLHttpRequest` header required by SSD IMS API
- **Enhanced Request Headers**: Added proper `Accept` header for JSON responses
- **Improved Timeout Handling**: Increased API timeout from 30s to 60s for slow responses
- **Robust Retry Mechanism**: Added exponential backoff retry (1s, 2s, 4s) for network failures
- **Better Session Detection**: Enhanced authentication failure detection with 401 status code handling
- **Detailed Error Messages**: Specific error handling for 401, 403, 404, 500 HTTP status codes
- **Enhanced Debugging**: Added comprehensive logging for API responses and data analysis
- **Network Resilience**: Separate handling for network errors vs authentication failures

### Version 1.1.1
- **Enhanced Error Messages**: Dramatically improved Pydantic validation error messages with detailed context
- **Better Debugging**: Added comprehensive data analysis for validation failures showing exact problematic values
- **Smart Error Detection**: Automatic identification of None values, wrong types, and data corruption
- **Contextual Logging**: Enhanced logging shows data structure overview and field-by-field analysis
- **Debug Documentation**: Added browser console debugging section with dynamic date selection and data anonymization
- **Developer Experience**: Validation errors now show exact field names, indices, and surrounding data context

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
