# AIS Faker

A Python-based AIS data simulator that generates realistic NMEA AIVDM messages for testing AIS applications like OpenCPN.

## Features

- **Multi-vessel simulation**: Supports multiple vessels with individual TCP ports
- **Realistic movement**: Simulates circular, linear, and random walk movement patterns
- **NMEA AIVDM messages**: Generates proper AIS Type 1 (position) and Type 5 (static data) messages
- **CSV configuration**: Easy vessel configuration via CSV file
- **TCP streaming**: Each vessel broadcasts on its own TCP port
- **Continuous operation**: 5-second update intervals with varying data

## Quick Start

1. **Configure vessels** in `vessel.csv`:
   ```csv
   client,port,name,mmsi,type,latitude,longitude
   bcm,20003,TB AMAN 05,525701698,Tugboat,-2.984459,104.140689
   test1,20004,TB SRIWIJAYA,525701699,Tugboat,-2.985459,104.141689
   ```

2. **Run the faker**:
   ```bash
   ./run.sh
   # or directly:
   python3 faker.py
   ```

3. **Connect OpenCPN**:
   - Add TCP connection to `localhost:20003` for TB AMAN 05
   - Add TCP connection to `localhost:20004` for TB SRIWIJAYA
   - etc.

## CSV Format

| Column    | Description                    | Example        |
|-----------|--------------------------------|----------------|
| client    | Client identifier              | bcm            |
| port      | TCP port for this vessel       | 20003          |
| name      | Vessel name (max 20 chars)     | TB AMAN 05     |
| mmsi      | MMSI number (9 digits)         | 525701698      |
| type      | Vessel type                    | Tugboat        |
| latitude  | Initial latitude               | -2.984459      |
| longitude | Initial longitude              | 104.140689     |

## Movement Simulation

The faker simulates realistic vessel movement:

- **Circular**: Vessels move in circles around their initial position
- **Linear**: Vessels move in straight lines with occasional course changes
- **Random Walk**: Vessels change direction and speed randomly

Movement parameters:
- Speed: 0.5-5.0 knots (varies over time)
- Course: 0-360° (changes based on movement pattern)
- Heading: Course ± 15° (simulates steering variations)
- Range: Within 1km of initial position

## Message Types

### Type 1 - Position Report (every 5 seconds)
- MMSI, latitude, longitude
- Speed over ground, course over ground
- Heading, navigation status

### Type 5 - Static Data (every 5 minutes)
- MMSI, vessel name, vessel type
- Multi-part message (2 fragments)

## OpenCPN Configuration

1. Open OpenCPN
2. Go to **Options > Connections**
3. Add new connection:
   - **Type**: TCP
   - **Address**: localhost
   - **Port**: (from vessel.csv)
   - **Protocol**: NMEA 0183
   - **Direction**: Receive

## Testing

Test the connection:
```bash
# Connect to a vessel's port
telnet localhost 20003

# You should see NMEA messages like:
!AIVDM,1,1,,A,15NtBQ002tgIRQHJ20Ev6i0H0000,0*5E
!AIVDM,2,1,0,A,55NtBQ00000000000000000000000000000000000000000000000000000000,0*2D
!AIVDM,2,2,0,A,00000000000000000000,2*3D
```

## Logging

The faker logs:
- Vessel loading and server startup
- Client connections/disconnections
- Movement updates (debug level)

Set log level in the script:
```python
logging.basicConfig(level=logging.DEBUG)  # For verbose output
```

## Requirements

- Python 3.7+
- pyais library: `pip install pyais`

## Installation

```bash
# Install the pyais library
pip install pyais

# Or with uv
uv add pyais
```

## Network Access

To allow external connections (not just localhost):
1. Change `'0.0.0.0'` binding in the script (already configured)
2. Ensure firewall allows the TCP ports
3. Connect from remote using your server's IP address

## Troubleshooting

- **Port already in use**: Change port numbers in vessel.csv
- **No movement**: Check vessel is within 1km drift limit
- **OpenCPN not showing**: Verify TCP connection and port number
- **Messages not flowing**: Check firewall and network connectivity