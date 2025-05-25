# GB28181 Restreamer Documentation

## Table of Contents
1. [Project Overview](#project-overview)
2. [System Architecture](#system-architecture)
3. [Installation Guide](#installation-guide)
4. [Configuration](#configuration)
5. [Integration with WVP-GB28181-Pro and ZLMediaKit](#integration-with-wvp-gb28181-pro-and-zlmediakit)
6. [Usage Guide](#usage-guide)
7. [Advanced Features](#advanced-features)
8. [Troubleshooting](#troubleshooting)
9. [API Reference](#api-reference)

## Project Overview

The GB28181 Restreamer is a lightweight, flexible software solution that enables:

1. **RTSP Stream Restreaming**: Converting RTSP video streams into GB28181-compliant streams
2. **Video File Streaming**: Scanning directories for video files (.mp4, .avi) and making them available via GB28181
3. **Multiple Connection Methods**: Supporting both direct SIP connections and local SIP server modes

This software is designed primarily for ARM-based systems like the NVIDIA Jetson Orin NX, making it ideal for embedded video surveillance applications.

Key features:
- Automatic video file discovery and cataloging
- Robust SIP protocol implementation
- Efficient GStreamer-based media streaming
- Automatic recovery from network and connection issues
- Comprehensive configuration options

## System Architecture

The GB28181 Restreamer follows a modular architecture with these core components:

### Key Components:

1. **SIP Client (sip_handler_pjsip.py)**
   - Handles SIP registration with GB28181 platform
   - Processes incoming INVITE requests
   - Manages device catalog and information requests

2. **Media Streamer (media_streamer.py)**
   - Creates GStreamer pipelines for video streaming
   - Manages RTP/SRTP connections
   - Handles video format conversion and encoding

3. **Local SIP Server (local_sip_server.py)**
   - Optional component for testing/development
   - Simulates a GB28181 platform for local testing

4. **XML Message Handling (gb28181_xml.py)**
   - Formats and parses GB28181-compliant XML messages
   - Handles catalog, device info requests

### Integration Diagram

```
┌─────────────────┐      ┌─────────────────┐      ┌─────────────────┐
│                 │      │                 │      │                 │
│ GB28181         │ SIP  │ WVP-GB28181-Pro │ RTMP │  ZLMediaKit     │
│ Restreamer      ├──────►                 ├──────►                 │
│                 │      │                 │      │                 │
└─────────────────┘      └─────────────────┘      └─────────────────┘
        │                                                   │
        │                                                   │
        │                                                   │
        │                ┌─────────────────┐                │
        │                │                 │                │
        └────────────────► Client Browser  ◄────────────────┘
                         │                 │
                         └─────────────────┘
```

## Installation Guide

### Prerequisites

- Python 3.8 or higher
- GStreamer 1.0 with required plugins
- PJSIP/PJSUA for SIP protocol handling

### Installation Steps

1. **Install system dependencies**:

```bash
sudo apt update
sudo apt install -y python3 python3-pip python3-dev \
    gstreamer1.0-tools gstreamer1.0-plugins-base \
    gstreamer1.0-plugins-good gstreamer1.0-plugins-bad \
    gstreamer1.0-plugins-ugly python3-gi vlc \
    build-essential pjsip-tools
```

2. **Clone the repository**:

```bash
git clone https://github.com/your-organization/gb28181-restreamer.git
cd gb28181-restreamer
```

3. **Install Python dependencies**:

```bash
pip3 install -r requirements.txt
```

4. **Prepare directories**:

```bash
mkdir -p logs
mkdir -p sample_videos
```

5. **Verify installation**:

```bash
python3 test_integrated_app.py
```

## Configuration

The GB28181 Restreamer uses JSON-based configuration files located in the `config` directory.

### Main Configuration (config/config.json)

```json
{
  "sip": {
    "device_id": "34020000001320000001",
    "username": "34020000001320000001",
    "password": "12345678",
    "server": "your-gb28181-server-ip",
    "port": 5060,
    "local_port": 5070
  },
  "local_sip": {
    "enabled": true,
    "port": 5060,
    "transport": "tcp"
  },
  "stream_directory": "./sample_videos",
  "rtsp_sources": [
    "rtsp://your-rtsp-server:8554/stream1"
  ]
}
```

**Key Configuration Parameters:**

- **SIP Settings**:
  - `device_id`: GB28181 device ID for this restreamer
  - `username`/`password`: Authentication credentials for GB28181 platform
  - `server`/`port`: GB28181 server address and port
  - `local_port`: Local port for SIP client to use

- **Local SIP Server**:
  - `enabled`: Whether to enable the built-in test server
  - `port`: Port for local SIP server
  - `transport`: Protocol (tcp/udp) for SIP communication

- **Directories and Sources**:
  - `stream_directory`: Path to directory containing video files
  - `rtsp_sources`: List of RTSP URLs to restream

### Streaming Presets (config/streaming_presets.json)

Configure video quality settings and encoding parameters:

```json
{
  "profiles": {
    "low": {
      "bitrate": 500000,
      "width": 640,
      "height": 360,
      "framerate": 15
    },
    "medium": {
      "bitrate": 1000000,
      "width": 1280,
      "height": 720,
      "framerate": 25
    },
    "high": {
      "bitrate": 2000000,
      "width": 1920,
      "height": 1080,
      "framerate": 30
    }
  },
  "default_profile": "medium"
}
```

## Integration with WVP-GB28181-Pro and ZLMediaKit

The GB28181 Restreamer is designed to work seamlessly with WVP-GB28181-Pro and ZLMediaKit, forming a complete video surveillance solution.

### Setting Up with WVP-GB28181-Pro

1. **Install WVP-GB28181-Pro** according to its documentation.

2. **Configure WVP to accept the GB28181 Restreamer**:
   - Log into WVP admin interface (typically at http://your-wvp-server:8080/)
   - Navigate to "设备管理" (Device Management)
   - Add a new device with the same device ID as configured in the Restreamer
   - Set the correct username/password credentials
   - Save the configuration

3. **Configure the Restreamer to connect to WVP**:
   - In your `config.json`, set these parameters:
     ```json
     "sip": {
       "device_id": "34020000001320000001",  // Must match WVP configuration
       "username": "34020000001320000001",   // Must match WVP configuration
       "password": "12345678",               // Must match WVP configuration
       "server": "your-wvp-server-ip",       // WVP server IP address
       "port": 5060                          // WVP SIP port (usually 5060)
     }
     ```

4. **Verify connection**:
   - Start the GB28181 Restreamer
   - Check the restreamer logs for successful registration
   - In WVP interface, the device should show as "在线" (Online)
   - You should be able to see the video catalog in WVP

### Setting Up with ZLMediaKit

1. **Install and Configure ZLMediaKit** according to its documentation.

2. **Configure WVP to forward streams to ZLMediaKit**:
   - In WVP configuration (application.yml), set:
     ```yaml
     media:
       zlm-ip: your-zlmediakit-ip
       zlm-port: 80
       hook-ip: your-wvp-server-ip
       hook-port: 8080
     ```
   - Restart WVP to apply changes

3. **Accessing Video Streams**:
   - In WVP interface, click on a device channel to start viewing
   - This triggers a SIP INVITE to the restreamer
   - The restreamer sends video via RTP to WVP/ZLMediaKit
   - ZLMediaKit makes the stream available via various protocols:
     - RTMP: rtmp://your-zlmediakit-ip/live/[stream-id]
     - HLS: http://your-zlmediakit-ip/live/[stream-id]/hls.m3u8
     - HTTP-FLV: http://your-zlmediakit-ip/live/[stream-id].flv

## Usage Guide

### Running the Application

To start the GB28181 Restreamer:

```bash
cd gb28181-restreamer
python3 src/main.py
```

The application will:
1. Load configuration from config/config.json
2. Scan video files from the configured directory
4. Start the SIP client and register with the GB28181 platform

### Testing with Built-in Tools

The project includes testing tools:

1. **Basic Functionality Test**:
   ```bash
   ./test_integrated_app.py
   ```

2. **Full GB28181 Protocol Test**:
   ```bash
   ./test_gb28181.py
   ```

### Monitoring and Management

The application logs detailed information to the console and log files, including:
- SIP registration status
- Stream connections and health
- Error conditions and recovery attempts

## Advanced Features

### RTSP Stream Handling

The application can connect to RTSP sources and make them available via GB28181:

1. Configure RTSP sources in config.json
2. The application will automatically connect and monitor these streams
3. When GB28181 platforms request video, the RTSP stream is transcoded as needed

Example configuration:
```json
"rtsp_sources": [
  "rtsp://admin:password@192.168.1.100:554/cam/realmonitor?channel=1&subtype=0",
  "rtsp://admin:password@192.168.1.101:554/cam/realmonitor?channel=1&subtype=0"
]
```

The system will:
- Connect to each RTSP source
- Monitor connection health
- Automatically retry failed connections
- Make each stream available as a separate channel in the GB28181 catalog

### Fault Tolerance and Recovery

The system includes comprehensive error handling and recovery:

1. **RTSP Connection Issues**: Automatic retry with exponential backoff
2. **SIP Registration Failures**: Automatic reconnection attempts
3. **Port Conflicts**: Dynamic port selection to avoid conflicts
4. **Stream Health Monitoring**: Detection and recovery of failed streams

## Troubleshooting

### Common Issues

1. **SIP Registration Failures**:
   - Check network connectivity to SIP server
   - Verify device_id, username, and password are correct
   - Ensure the SIP server port is accessible
   - Check the server logs for authentication errors

2. **RTSP Connection Issues**:
   - Verify RTSP server is running and accessible
   - Check network connectivity to RTSP server
   - Ensure RTSP URL is correct in configuration

3. **Port Conflicts**:
   - If you see "Address already in use" errors, check for other services using the configured ports
   - The application will try alternative ports, check logs for "Using alternative port" messages

4. **No Streams Visible in WVP**:
   - Verify SIP registration is successful
   - Check that video files exist in the configured directory
   - Ensure the device appears online in WVP

### Log Analysis

The application generates detailed logs with different severity levels:
- INFO: Normal operation information
- WARNING: Non-critical issues that might need attention
- ERROR: Problems that prevent functionality
- DEBUG: Detailed diagnostic information

Example log analysis:

| Log Message | Interpretation | Action |
|-------------|----------------|--------|
| `Registration failed` | SIP registration to server failed | Check credentials and server connection |
| `RTSP server at x.x.x.x:554 is not available` | Cannot connect to RTSP source | Verify RTSP server is running and URL is correct |
| `Address already in use` | Port conflict detected | Application will try alternative ports |

### Checking Stream Status

To check the status of active streams:
1. Monitor the application logs for periodic status reports
2. Check the GB28181 platform for stream status
3. Use test tools like VLC to verify stream playback

## API Reference

### Configuration Structure

**config.json**
- `sip`: SIP client configuration
- `local_sip`: Local SIP server configuration
- `stream_directory`: Video file directory
- `rtsp_sources`: List of RTSP sources

### XML Message Formats

The application follows the GB28181 standard for XML messages:

**Catalog Response**:
```xml
<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <CmdType>Catalog</CmdType>
    <SN>123456</SN>
    <DeviceID>[DEVICE_ID]</DeviceID>
    <Result>OK</Result>
    <DeviceList Num="[NUMBER_OF_DEVICES]">
        <Item>
            <DeviceID>[CHANNEL_ID]</DeviceID>
            <Name>[CHANNEL_NAME]</Name>
            <Manufacturer>GB28181-Restreamer</Manufacturer>
            <Model>Video-File</Model>
            <Owner>gb28181-restreamer</Owner>
            <CivilCode>123456</CivilCode>
            <Address>[CHANNEL_ADDRESS]</Address>
            <Parental>0</Parental>
            <SafetyWay>0</SafetyWay>
            <RegisterWay>1</RegisterWay>
            <Secrecy>0</Secrecy>
            <Status>ON</Status>
        </Item>
        <!-- More items -->
    </DeviceList>
</Response>
```

**Device Info Response**:
```xml
<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <CmdType>DeviceInfo</CmdType>
    <SN>123456</SN>
    <DeviceID>[DEVICE_ID]</DeviceID>
    <Result>OK</Result>
    <DeviceName>GB28181-Restreamer</DeviceName>
    <Manufacturer>GB28181-RestreamerProject</Manufacturer>
    <Model>Restreamer-1.0</Model>
    <Firmware>1.0.0</Firmware>
    <MaxCamera>[NUMBER_OF_CHANNELS]</MaxCamera>
    <MaxAlarm>0</MaxAlarm>
</Response>
```

### Key Module Functions

**sip_handler_pjsip.py**:
- `handle_invite(msg_text)`: Process incoming INVITE requests
- `parse_sdp_and_stream(sdp_text, callid, target_channel)`: Process SDP and start streaming

**gb28181_xml.py**:
- `format_catalog_response(device_id, device_catalog)`: Generate catalog XML 