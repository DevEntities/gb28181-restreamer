# src/sip_handler_pjsip.py

import subprocess
import threading
import time
import re
import os
import logging
import json
import tempfile
import signal
import xml.etree.ElementTree as ET
from logger import log
from file_scanner import get_video_catalog, scan_video_files
from media_streamer import MediaStreamer
from recording_manager import get_recording_manager
from gb28181_xml import (
    format_catalog_response,
    format_device_info_response,
    format_keepalive_response,
    format_device_status_response,
    format_media_status_response,
    format_recordinfo_response,
    parse_xml_message,
    parse_recordinfo_query
)
from gb28181_sip_sender import GB28181SIPSender

class SIPClient:
    def __init__(self, config):
        """Initialize SIP client"""
        self.config = config
        self.device_id = config["sip"]["device_id"]
        self.username = config["sip"]["username"]
        self.password = config["sip"]["password"]
        self.server = config["sip"]["server"]
        self.port = config["sip"]["port"]
        self.local_port = config["sip"].get("local_port", 5080)
        self.realm = config["sip"].get("realm", "*")
        self.transport = config["sip"].get("transport", "udp")
        self.stream_directory = config["stream_directory"]
        
        # For handling streams
        self.active_streams = {}
        self.stream_locks = {}
        
        # For storing device catalog
        self.device_catalog = {}
        
        # Rate limiting for catalog responses
        self.last_catalog_response_time = 0
        self.catalog_response_interval = 2  # Minimum 2 seconds between catalog responses
        
        # Set up health monitoring
        self.registration_status = "OFFLINE"
        self.last_keepalive_time = None
        self.last_keepalive_check = time.time()
        self.keepalive_interval = 60  # Seconds between keepalive messages
        self.registration_retry_interval = 30  # Seconds between registration retries
        
        # Streamer connection
        self.streamer = config.get("streamer")
        
        # SIP status
        self.running = False
        self.process = None
        self.last_registration_attempt = 0
        self.registration_failures = 0
        
        # Add missing attributes to prevent errors
        self.registration_attempts = 0
        self.max_registration_attempts = 5
        self.last_keepalive = time.time()
        self.catalog_ready = False
        self.last_catalog_update = 0
        
        # Create thread-safe pipe for communication with PJSUA
        self.pipe_read, self.pipe_write = os.pipe()
        
        # Create SIP message sender
        self.sip_sender = GB28181SIPSender(config)

    def generate_device_catalog(self):
        """Generate device catalog information according to GB28181 standard"""
        scan_video_files(self.config['stream_directory'])
        video_catalog = get_video_catalog()
        catalog_dict = {}
        if not video_catalog:
            log.warning("[SIP] No video files found in catalog")
            return

        # Clear existing catalog
        self.device_catalog = {}
        
        # Create device catalog in GB28181 format
        for i, video_path in enumerate(video_catalog):
            video_name = os.path.basename(video_path)
            # Generate unique channel ID for each video file
            # Format: deviceID + 3-digit suffix (must be a proper SIP ID format)
            channel_id = f"{self.device_id}{i+1:03d}"
            
            self.device_catalog[channel_id] = {
                "device_id": channel_id,
                "name": video_name,
                "path": video_path,
                "channel_id": channel_id,
                "status": "ON",  # ON or OFF
                "manufacturer": "GB28181-Restreamer",
                "model": "Video-File",
                "owner": "gb28181-restreamer",
                "civil_code": "123456",  # Required by GB28181
                "address": f"Video-{i+1}",
                "parental": "0",  # 0 means root device
                "parent_id": self.device_id,  # Use device_id as parent
                "safety_way": "0",  # 0 means no safety
                "register_way": "1",  # 1 means active registration
                "secrecy": "0",  # 0 means not secret
            }
        
        log.info(f"[SIP] Generated catalog with {len(self.device_catalog)} channels")
        self.catalog_ready = True
        self.last_catalog_update = time.time()

    def extract_sdp_from_message(self, msg_text):
        """Extract SDP content from a SIP message with enhanced parsing
        
        This function handles various SDP formats found in GB28181 implementations:
        - Standard SDP format (after empty line)
        - Embedded SDP without proper separators
        - Partial SDP information in log lines
        - Malformed SDP with missing fields
        """
        # Method 1: Find the SDP section which typically starts after an empty line
        sdp_section = re.search(r'\r\n\r\n(v=0.*)', msg_text, re.DOTALL)
        if sdp_section:
            log.debug("[SIP] Found SDP using standard empty line separator")
            return self._validate_and_fix_sdp(sdp_section.group(1))
        
        # Method 2: Look for SDP with alternative separators
        alt_sdp_section = re.search(r'(\r\n|\n)(v=0.*)', msg_text, re.DOTALL)
        if alt_sdp_section:
            log.debug("[SIP] Found SDP using alternative separator")
            return self._validate_and_fix_sdp(alt_sdp_section.group(2))
        
        # Method 3: If we can't find the standard pattern, look for SDP contents more generally
        sdp_start = msg_text.find('v=0')
        if sdp_start != -1:
            log.debug("[SIP] Found SDP using direct v=0 search")
            return self._validate_and_fix_sdp(msg_text[sdp_start:])
        
        # Method 4: Extract key SDP fields to reconstruct a minimal valid SDP
        log.debug("[SIP] No standard SDP found, attempting to extract key fields")
        
        # Extract IP address (c= line)
        c_match = re.search(r'c=IN IP4 (\d+\.\d+\.\d+\.\d+)', msg_text)
        # Extract port (m= line)
        m_match = re.search(r'm=video (\d+)', msg_text)
        # Look for SSRC (y= line in GB28181)
        y_match = re.search(r'y=(\d+)', msg_text)
        # Look for format (f= line in GB28181)
        f_match = re.search(r'f=v/(\d+)/(\d+)', msg_text)
        
        if c_match or m_match or y_match:
            log.debug("[SIP] Reconstructing SDP from partial fields")
            sdp_parts = []
            sdp_parts.append("v=0")
            sdp_parts.append(f"o=- {int(time.time())} 1 IN IP4 127.0.0.1")
            sdp_parts.append("s=GB28181 Stream")
            sdp_parts.append("t=0 0")
            
            # Add connection information if available
            if c_match:
                sdp_parts.append(f"c=IN IP4 {c_match.group(1)}")
            else:
                # Use server IP as fallback
                sdp_parts.append(f"c=IN IP4 {self.server}")
                log.warning("[SIP] No IP found in SDP, using server IP as fallback")
            
            # Add media description if available
            if m_match:
                sdp_parts.append(f"m=video {m_match.group(1)} RTP/AVP 96")
            else:
                # Use default port as fallback
                sdp_parts.append("m=video 9000 RTP/AVP 96")
                log.warning("[SIP] No port found in SDP, using default port 9000")
            
            # Add RTPMAP for H.264
            sdp_parts.append("a=rtpmap:96 H264/90000")
            sdp_parts.append("a=fmtp:96 profile-level-id=42e01f")
            
            # Add SSRC if available (GB28181 specific)
            if y_match:
                sdp_parts.append(f"y={y_match.group(1)}")
            else:
                sdp_parts.append("y=0000000001")
                
            # Add format if available (GB28181 specific)
            if f_match:
                sdp_parts.append(f"f=v/{f_match.group(1)}/{f_match.group(2)}")
            else:
                sdp_parts.append("f=v/2/25")  # Default to H.264 at 25fps
                
            return "\n".join(sdp_parts)
        
        log.error("[SIP] Failed to extract or reconstruct SDP from message")
        return None

    def _validate_and_fix_sdp(self, sdp_text):
        """Validate and fix common SDP issues"""
        if not sdp_text:
            return None
            
        lines = sdp_text.splitlines()
        fixed_lines = []
        has_video = False
        has_rtpmap = False
        has_connection = False
        
        for line in lines:
            line = line.strip()
            if not line:
                continue
                
            # Fix common issues
            if line.startswith('m=video'):
                has_video = True
                # Ensure RTP/AVP is present
                if 'RTP/AVP' not in line:
                    line = line.replace('UDP', 'RTP/AVP')
                # Ensure payload type 96 is present
                if not re.search(r'\s96(\s|$)', line):
                    line = f"{line} 96"
                    
            elif line.startswith('c=IN'):
                has_connection = True
                # Ensure IPv4 is specified
                if 'IP4' not in line:
                    line = line.replace('IN ', 'IN IP4 ')
                    
            elif line.startswith('a=rtpmap:96'):
                has_rtpmap = True
                # Ensure correct format for H.264
                if 'H264' not in line:
                    line = 'a=rtpmap:96 H264/90000'
                
            fixed_lines.append(line)
            
        # Add missing required fields
        if not has_video:
            fixed_lines.append('m=video 9000 RTP/AVP 96')
        if not has_connection:
            fixed_lines.append(f'c=IN IP4 {self.server}')
        if not has_rtpmap:
            fixed_lines.append('a=rtpmap:96 H264/90000')
            fixed_lines.append('a=fmtp:96 profile-level-id=42e01f')
            
        # Ensure GB28181 specific fields
        if not any(l.startswith('y=') for l in fixed_lines):
            fixed_lines.append('y=0000000001')
        if not any(l.startswith('f=') for l in fixed_lines):
            fixed_lines.append('f=v/2/25')
            
        return '\n'.join(fixed_lines)

    def parse_sdp_and_stream(self, sdp_text, callid=None, target_channel=None):
        """Parse SDP offer and start streaming to the specified destination
        
        Enhanced with better error handling and recovery mechanisms.
        """
        try:
            # Extract SDP content if we've been passed a full SIP message
            sdp_content = self.extract_sdp_from_message(sdp_text)
            if not sdp_content:
                log.warning("[SIP] ⚠️ No SDP content found in message, cannot start stream")
                return False
            
            log.debug(f"[SIP] Parsed SDP content: {sdp_content}")
                
            # Extract destination IP (c= line)
            ip_match = re.search(r"c=IN IP4 (\d+\.\d+\.\d+\.\d+)", sdp_content)
            if not ip_match:
                log.warning("[SIP] ⚠️ Failed to extract IP address from SDP")
                # Use the server IP as fallback
                ip = self.server
                log.info(f"[SIP] Using fallback IP: {ip}")
            else:
                ip = ip_match.group(1)
                
            # Extract port (m= line)
            port_match = re.search(r"m=video (\d+)", sdp_content)
            if not port_match:
                log.warning("[SIP] ⚠️ Failed to extract port from SDP")
                # Use a default port as fallback
                port = 9000
                log.info(f"[SIP] Using fallback port: {port}")
            else:
                port = int(port_match.group(1))
            
            # Extract SSRC (y= line in GB28181)
            y_match = re.search(r"y=(\d+)", sdp_content)
            ssrc = y_match.group(1) if y_match else "0000000001"
            
            # Extract video format requirements if available (f= line in GB28181)
            encoder_params = {}
            f_match = re.search(r"f=v/(\d+)/(\d+)", sdp_content)
            if f_match:
                codec_id = f_match.group(1)
                resolution_id = f_match.group(2)
                
                # Map GB28181 codec IDs to GStreamer encoder parameters
                if codec_id == "1":  # MPEG-4
                    encoder_params["codec"] = "mpeg4"
                    encoder_params["profile"] = "simple"
                elif codec_id == "2":  # H.264
                    encoder_params["codec"] = "h264"
                    encoder_params["profile"] = "baseline"
                elif codec_id == "3":  # H.265
                    encoder_params["codec"] = "h265"
                    encoder_params["profile"] = "main"
                    
                # Map GB28181 resolution IDs to actual resolutions
                if resolution_id == "1":  # QCIF
                    encoder_params["width"] = 176
                    encoder_params["height"] = 144
                elif resolution_id == "2":  # CIF
                    encoder_params["width"] = 352
                    encoder_params["height"] = 288
                elif resolution_id == "3":  # 4CIF
                    encoder_params["width"] = 704
                    encoder_params["height"] = 576
                elif resolution_id == "4":  # D1
                    encoder_params["width"] = 720
                    encoder_params["height"] = 576
                elif resolution_id == "5":  # 720P
                    encoder_params["width"] = 1280
                    encoder_params["height"] = 720
                elif resolution_id == "6":  # 1080P
                    encoder_params["width"] = 1920
                    encoder_params["height"] = 1080
                    
                log.info(f"[SIP] Video format request: codec={encoder_params.get('codec', 'h264')}, " +
                         f"resolution={encoder_params.get('width', '?')}x{encoder_params.get('height', '?')}")
            
            # Select video source based on target channel
            video_source = None
            
            # If a specific channel/device was targeted
            if target_channel and self.catalog_ready:
                # Check if this is a known channel ID in our catalog
                if target_channel in self.device_catalog:
                    video_source = self.device_catalog[target_channel]["path"]
                    log.info(f"[SIP] Using channel-specific video source: {video_source}")
                elif target_channel == self.device_id:
                    # If requesting the main device ID, use first available video
                    if self.device_catalog:
                        first_channel = list(self.device_catalog.keys())[0]
                        video_source = self.device_catalog[first_channel]["path"]
                        log.info(f"[SIP] Using first available video: {video_source}")
            
            # If still no video source, select first available one
            if not video_source:
                # If no specific channel or not found, select first available file
                catalog = get_video_catalog()
                if catalog:
                    video_source = catalog[0]
                    log.info(f"[SIP] Using first available video file: {video_source}")
                else:
                    log.error("[SIP] No video files available")
                    return False
                
            # Start the stream using our Media Streamer
            success = self.streamer.start_stream(
                video_path=video_source,
                dest_ip=ip,
                dest_port=port,
                ssrc=ssrc,
                encoder_params=encoder_params
            )
            
            if success:
                # Record this stream in active streams
                stream_info = {
                    "dest_ip": ip,
                    "dest_port": port,
                    "ssrc": ssrc,
                    "video_path": video_source,
                    "start_time": time.time(),
                    "status": "active",
                    "encoder_params": encoder_params
                }
                
                if callid:
                    self.active_streams[callid] = stream_info
                else:
                    # Generate a unique stream ID if no callid
                    stream_id = f"{ip}:{port}:{ssrc}"
                    self.active_streams[stream_id] = stream_info
                
                # Schedule a media status update
                self._schedule_media_status_update(callid if callid else stream_id)
                
                log.info(f"[SIP] Started stream to {ip}:{port} with SSRC {ssrc}")
                return True
            else:
                log.error("[SIP] Failed to start stream")
                return False
                
        except Exception as e:
            log.error(f"[SIP] Error in parse_sdp_and_stream: {e}")
            return False

    def _send_media_status_update(self, stream_id):
        """Send media status update with enhanced monitoring
        
        This method provides detailed status updates about active streams,
        including health metrics and error recovery information.
        """
        try:
            if stream_id not in self.active_streams:
                log.warning(f"[SIP] Cannot update status for unknown stream: {stream_id}")
                return
                
            stream_info = self.active_streams[stream_id]
            
            # Get detailed stream status from media streamer
            stream_status = self.streamer.get_stream_status(stream_id)
            if not stream_status:
                log.warning(f"[SIP] Could not get stream status for {stream_id}")
                return
                
            # Calculate stream metrics
            now = time.time()
            duration = now - stream_info["start_time"]
            
            # Combine all status information
            status_data = {
                **stream_info,
                "stream_id": stream_id,
                "status": "active",
                "duration": duration,
                "stream_status": stream_status,
                "last_update": now,
                "health": {
                    "fps": stream_status.get("fps", 0),
                    "bitrate": stream_status.get("bitrate", 0),
                    "packet_loss": stream_status.get("packet_loss", 0),
                    "jitter": stream_status.get("jitter", 0)
                }
            }
            
            # Check stream health metrics
            health_issues = []
            
            if status_data["health"]["fps"] < 10:
                health_issues.append("Low FPS")
            if status_data["health"]["packet_loss"] > 5:
                health_issues.append("High packet loss")
            if status_data["health"]["jitter"] > 50:
                health_issues.append("High jitter")
                
            if health_issues:
                log.warning(f"[SIP] Stream {stream_id} health issues: {', '.join(health_issues)}")
                status_data["health_issues"] = health_issues
                
                # Attempt recovery if needed
                if len(health_issues) >= 2:
                    self._attempt_stream_recovery(stream_id, status_data)
            
            # Format XML message for media status
            xml_msg = format_media_status_response(self.device_id, status_data)
            
            # Send the XML message via the SIP sender
            self.sip_sender.send_media_status(xml_msg)
            
            # Update stream info with latest status
            self.active_streams[stream_id].update({
                "last_status": status_data,
                "last_update": now
            })
            
            log.debug(f"[SIP] Sent media status update for {stream_id}")
            
        except Exception as e:
            log.error(f"[SIP] Error sending media status update: {e}")
            
    def _attempt_stream_recovery(self, stream_id, status_data):
        """Attempt to recover a problematic stream"""
        try:
            log.info(f"[SIP] Attempting recovery for stream {stream_id}")
            
            # Get current stream info
            stream_info = self.active_streams[stream_id]
            
            # Check if we've attempted recovery recently
            now = time.time()
            if stream_info.get("last_recovery", 0) > now - 60:
                log.info("[SIP] Recovery attempted too recently, skipping")
                return
                
            # Stop the current stream
            self.streamer.stop_stream(stream_id)
            
            # Wait briefly
            time.sleep(2)
            
            # Attempt to restart with original parameters
            success = self.streamer.start_stream(
                video_path=stream_info["video_path"],
                dest_ip=stream_info["dest_ip"],
                dest_port=stream_info["dest_port"],
                ssrc=stream_info["ssrc"],
                encoder_params=stream_info.get("encoder_params", {})
            )
            
            if success:
                log.info(f"[SIP] Successfully recovered stream {stream_id}")
                # Update recovery timestamp
                self.active_streams[stream_id]["last_recovery"] = now
            else:
                log.error(f"[SIP] Failed to recover stream {stream_id}")
                # Mark stream as failed
                self.active_streams[stream_id]["status"] = "failed"
                
        except Exception as e:
            log.error(f"[SIP] Error during stream recovery: {e}")
            
    def handle_catalog_query(self, msg_text):
        """Handle catalog query according to GB28181 protocol"""
        try:
            log.info("[SIP] Processing potential catalog query from platform")
            
            # First, validate this is actually an incoming MESSAGE request, not a response
            if "SIP/2.0" in msg_text and ("200 OK" in msg_text or "180 " in msg_text or "100 " in msg_text):
                log.debug("[SIP] Ignoring SIP response message")
                return None
                
            # Check if this is actually a MESSAGE request (handle pjsua log format)
            is_message_request = False
            if re.search(r'^MESSAGE\s+sip:', msg_text, re.MULTILINE):
                is_message_request = True
            elif re.search(r'MESSAGE from.*?:', msg_text):
                # Handle pjsua log format: "MESSAGE from <sip:...>:"
                is_message_request = True
            
            if not is_message_request:
                log.debug("[SIP] Not a MESSAGE request, ignoring")
                return None
            
            # Extract XML from the message - must be a Query type
            xml_match = re.search(r'<\?xml.*?<\/Query>', msg_text, re.DOTALL)
            if not xml_match:
                log.debug("[SIP] No Query XML content found in message")
                return None
                
            xml_content = xml_match.group(0)
            
            # Ensure this is specifically a Catalog query
            if not re.search(r'<CmdType>\s*Catalog\s*</CmdType>', xml_content, re.IGNORECASE):
                log.debug("[SIP] XML found but not a Catalog query")
                return None
                
            log.info("[SIP] Valid catalog query confirmed, processing...")
            log.debug(f"[SIP] Extracted XML content: {xml_content}")
            
            # Rate limiting: check if we recently sent a catalog response
            current_time = time.time()
            if (current_time - self.last_catalog_response_time) < self.catalog_response_interval:
                time_remaining = self.catalog_response_interval - (current_time - self.last_catalog_response_time)
                log.warning(f"[SIP] Rate limiting: ignoring catalog query (next allowed in {time_remaining:.1f}s)")
                return None
            
            # Extract query SN (serial number)
            sn_match = re.search(r'<SN>(.*?)<\/SN>', xml_content)
            sn = sn_match.group(1) if sn_match else None
            log.debug(f"[SIP] Query SN: {sn}")
            
            # Extract platform ID from message sender
            from_match = re.search(r'From:.*?<sip:(.*?)[@>]', msg_text)
            if not from_match:
                # Handle pjsua log format
                from_match = re.search(r'MESSAGE from.*?<sip:(.*?)[@>]', msg_text)
            
            platform_id = from_match.group(1) if from_match else None
            
            if not platform_id:
                platform_id = self.server  # Use server address as fallback
                log.debug(f"[SIP] Using default platform ID: {platform_id}")
            
            # Make sure device catalog is generated and up-to-date
            if not hasattr(self, 'catalog_ready') or not self.catalog_ready:
                self.generate_device_catalog()
            
            # Make sure we have a catalog to send
            if not hasattr(self, 'device_catalog') or not self.device_catalog:
                log.warning("[SIP] Device catalog is empty, regenerating...")
                self.generate_device_catalog()
                
            # Use the device catalog for channels
            if hasattr(self, 'device_catalog') and self.device_catalog:
                log.info(f"[SIP] Sending catalog with {len(self.device_catalog)} channels")
                
                # Generate catalog response XML directly to ensure it uses the correct tags
                now = time.time()
                xml_items = []
                
                for channel_id, channel_info in self.device_catalog.items():
                    name = str(channel_info.get('name', 'Channel'))
                    manufacturer = str(channel_info.get('manufacturer', 'GB28181-Restreamer'))
                    model = str(channel_info.get('model', 'Camera'))
                    owner = str(channel_info.get('owner', 'gb28181-restreamer'))
                    civil_code = str(channel_info.get('civil_code', '123456'))
                    block = str(channel_info.get('block', '123456'))
                    address = str(channel_info.get('address', 'Local'))
                    parental = str(channel_info.get('parental', '0'))
                    parent_id = str(channel_info.get('parent_id', self.device_id))
                    safety_way = str(channel_info.get('safety_way', '0'))
                    register_way = str(channel_info.get('register_way', '1'))
                    secrecy = str(channel_info.get('secrecy', '0'))
                    status = str(channel_info.get('status', 'ON'))
                    
                    xml_items.append(f"""    <Item>
      <DeviceID>{channel_id}</DeviceID>
      <Name>{name}</Name>
      <Manufacturer>{manufacturer}</Manufacturer>
      <Model>{model}</Model>
      <Owner>{owner}</Owner>
      <CivilCode>{civil_code}</CivilCode>
      <Block>{block}</Block>
      <Address>{address}</Address>
      <Parental>{parental}</Parental>
      <ParentID>{parent_id}</ParentID>
      <SafetyWay>{safety_way}</SafetyWay>
      <RegisterWay>{register_way}</RegisterWay>
      <Secrecy>{secrecy}</Secrecy>
      <Status>{status}</Status>
    </Item>""")
                
                response_xml = f"""<?xml version="1.0" encoding="GB2312"?>
<Response>
  <CmdType>Catalog</CmdType>
  <SN>{sn if sn else int(now)}</SN>
  <DeviceID>{self.device_id}</DeviceID>
  <Result>OK</Result>
  <SumNum>{len(self.device_catalog)}</SumNum>
  <DeviceList Num="{len(self.device_catalog)}">
{"".join(xml_items)}
  </DeviceList>
</Response>
"""
                
                log.debug(f"[SIP] Catalog response XML length: {len(response_xml)}")
                
                # Save the XML for debugging
                with open('catalog_response.xml', 'w') as f:
                    f.write(response_xml)
                    
                log.info(f"[SIP] Saved catalog response to catalog_response.xml")
                
                # Send the response using the SIP sender's send_catalog method
                # Extract the platform SIP URI from the From header if possible
                target_uri = None
                from_header_match = re.search(r'From:.*?<sip:(.*?)[@>]', msg_text)
                if not from_header_match:
                    # Handle pjsua log format
                    from_header_match = re.search(r'MESSAGE from.*?<sip:(.*?)[@>]', msg_text)
                
                if from_header_match:
                    platform_id = from_header_match.group(1)
                    # Use the platform's SIP ID and server address to construct the URI
                    target_uri = f"sip:{platform_id}@{self.server}:{self.port}"
                else:
                    # Fallback to server address
                    target_uri = f"sip:{self.server}:{self.port}"

                success = self.sip_sender.send_catalog(response_xml, target_uri)
                if success:
                    log.info(f"[SIP] Successfully sent catalog response with {len(self.device_catalog)} channels to {target_uri}")
                    # Update rate limiting timestamp
                    self.last_catalog_response_time = current_time
                else:
                    log.error(f"[SIP] Failed to send catalog response to {target_uri}")
            else:
                log.error("[SIP] No channels found in device catalog after regeneration")
                
        except Exception as e:
            log.error(f"[SIP] Error handling catalog query: {e}")
            log.exception(e)

    def handle_device_info_query(self, msg_text):
        """Handle device info query according to GB28181 protocol"""
        log.info("[SIP] Received device info query")
        
        # Prepare device info
        device_info = {
            "device_id": self.device_id,
            "device_name": "GB28181-Restreamer",
            "manufacturer": "GB28181-RestreamerProject",
            "model": "Restreamer-1.0",
            "firmware": "1.0.0",
            "max_camera": len(get_video_catalog()),
            "max_alarm": 0
        }
        
        # Format XML response
        xml_response = format_device_info_response(device_info)
        
        # Send device info using the SIP sender
        self.sip_sender.send_device_info(xml_response)

    def handle_device_control(self, msg_text):
        """Handle device control commands according to GB28181 protocol"""
        # Example control commands include PTZ control, recording control, etc.
        if "PTZ" in msg_text:
            log.info("[SIP] Received PTZ control command (not supported)")
            # Would send appropriate response for unsupported command
        elif "RECORD" in msg_text:
            log.info("[SIP] Received recording control command (not supported)")
            # Would send appropriate response for unsupported command
        else:
            log.info("[SIP] Received unknown control command")
            # Would send appropriate response
            
    def handle_recordinfo_query(self, msg_text):
        """Handle record info query from platform"""
        try:
            query_info = parse_recordinfo_query(msg_text)
            log.info(f"[SIP] RecordInfo query received: {query_info}")
            
            if not query_info:
                log.error("[SIP] Failed to parse record info query")
                return None
                
            device_id = query_info.get("device_id")
            start_time = query_info.get("start_time")
            end_time = query_info.get("end_time")
            
            log.info(f"[SIP] Record info query for device {device_id} from {start_time} to {end_time}")
            
            # Get recording manager instance
            recording_manager = get_recording_manager(self.config)
            if not recording_manager:
                log.error("[SIP] Recording manager not initialized")
                recordings = []
            else:
                # Get recordings in the time range
                recordings = recording_manager.get_recordings_in_range(start_time, end_time)
                
            if not recordings:
                log.info("[SIP] No recordings found in the specified time range")
                recordings = []
                
            # Format response according to GB28181 standard
            response = format_recordinfo_response(
                device_id=device_id,
                records=recordings,
                sn=query_info.get("sn")
            )
            
            # Send response via SIP message
            if response:
                self.sip_sender.send_recordinfo(response)
                log.info(f"[SIP] Sent record info response with {len(recordings)} recordings")
            else:
                log.error("[SIP] Failed to format record info response")
                
        except Exception as e:
            log.error(f"[SIP] Error handling record info query: {e}")
            return None

    def handle_invite(self, msg_text):
        """Handle SIP INVITE message for stream requests"""
        try:
            # Extract Call-ID for tracking this stream
            callid_match = re.search(r"Call-ID:\s*(.+)", msg_text)
            if not callid_match:
                callid_match = re.search(r"i:\s*(.+)", msg_text)  # Short form header
                
            if not callid_match:
                log.warning("[SIP] Failed to extract Call-ID from INVITE message")
                return False
                
            callid = callid_match.group(1).strip()
            log.info(f"[SIP] Processing INVITE with Call-ID: {callid}")
            
            # Extract target channel from message if present - helps select the right video source
            target_channel = None
            channel_match = re.search(r"z:\s*(.+)", msg_text)
            if channel_match:
                target_channel = channel_match.group(1).strip()
                log.info(f"[SIP] INVITE targets channel: {target_channel}")
            
            # Check if this is a playback request with time parameters
            is_playback = False
            start_time = None
            end_time = None
            
            # Look for playback time parameters in the SDP
            playback_match = re.search(r"y=playback:(.+)", msg_text)
            if playback_match:
                playback_info = playback_match.group(1).strip()
                log.info(f"[SIP] Playback request detected: {playback_info}")
                is_playback = True
                
                # Check for time range parameters
                time_range_match = re.search(r"starttime=(\d+T\d+Z);endtime=(\d+T\d+Z)", msg_text)
                if time_range_match:
                    start_time = time_range_match.group(1).strip()
                    end_time = time_range_match.group(2).strip()
                    log.info(f"[SIP] Playback time range: {start_time} to {end_time}")
            
            # Handle the streaming differently based on whether it's a playback request
            if is_playback:
                # This is a historical playback request
                log.info("[SIP] Processing historical playback request")
                
                # Get the recording manager
                recording_manager = get_recording_manager(self.config)
                if not recording_manager:
                    log.error("[SIP] Recording manager not available for playback")
                    return False
                
                # Query for matching recordings
                matching_recordings = recording_manager.query_recordings(
                    device_id=target_channel,
                    start_time=start_time,
                    end_time=end_time
                )
                
                if not matching_recordings:
                    log.warning("[SIP] No matching recordings found for playback request")
                    return False
                
                # Use the first matching recording
                recording = matching_recordings[0]
                log.info(f"[SIP] Selected recording for playback: {recording['name']}")
                
                # Parse SDP to get destination IP/port
                success = self.parse_sdp_and_stream_recording(
                    sdp_text=msg_text,
                    callid=callid,
                    recording_info=recording,
                    start_time=start_time,
                    end_time=end_time
                )
                
                if success:
                    log.info(f"[SIP] Started playback stream for Call-ID: {callid}")
                else:
                    log.error(f"[SIP] Failed to start playback stream for Call-ID: {callid}")
                
                return success
            else:
                # This is a regular live streaming request
                return self.parse_sdp_and_stream(msg_text, callid, target_channel)
                
        except Exception as e:
            log.error(f"[SIP] Error handling INVITE: {e}")
            return False
            
    def parse_sdp_and_stream_recording(self, sdp_text, callid, recording_info, 
                                     start_time=None, end_time=None):
        """Parse SDP offer and start streaming a recording to the specified destination
        
        This method handles playback of recordings with time parameters.
        """
        try:
            # Extract SDP content
            sdp_content = self.extract_sdp_from_message(sdp_text)
            if not sdp_content:
                log.warning("[SIP] ⚠️ No SDP content found in message, cannot start playback")
                return False
                
            log.debug(f"[SIP] Parsed SDP content for playback: {sdp_content}")
                
            # Extract destination IP
            ip_match = re.search(r"c=IN IP4 (\d+\.\d+\.\d+\.\d+)", sdp_content)
            if not ip_match:
                log.warning("[SIP] ⚠️ Failed to extract IP address from SDP")
                ip = self.server
                log.info(f"[SIP] Using fallback IP: {ip}")
            else:
                ip = ip_match.group(1)
                
            # Extract port
            port_match = re.search(r"m=video (\d+)", sdp_content)
            if not port_match:
                log.warning("[SIP] ⚠️ Failed to extract port from SDP")
                port = 9000
                log.info(f"[SIP] Using fallback port: {port}")
            else:
                port = int(port_match.group(1))
            
            # Extract SSRC
            y_match = re.search(r"y=(\d+)", sdp_content)
            ssrc = y_match.group(1) if y_match else "0000000001"
            
            # Start playback of the recording
            success = self.streamer.start_recording_playback(
                recording_info=recording_info,
                dest_ip=ip,
                dest_port=port,
                start_timestamp=start_time,
                end_timestamp=end_time,
                ssrc=ssrc
            )
                
            if success:
                # Record this stream in active streams
                self.active_streams[callid] = {
                    "type": "playback",
                    "recording": recording_info,
                    "dest_ip": ip,
                    "dest_port": port,
                    "ssrc": ssrc,
                    "start_time": time.time(),
                    "status": "active"
                }
                
                # Set up a periodic status update for this stream
                self._schedule_media_status_update(callid)
                
                log.info(f"[SIP] Started playback stream to {ip}:{port}")
                return True
            else:
                log.error("[SIP] Failed to start playback stream")
                return False
                
        except Exception as e:
            log.error(f"[SIP] Error starting playback: {e}")
            return False

    def handle_keepalive(self, msg_text):
        """Handle keepalive messages according to GB28181 protocol"""
        log.info("[SIP] Received keepalive message")
        
        # Format XML response
        xml_response = format_keepalive_response(self.device_id)
        
        # Send keepalive response using the SIP sender
        self.sip_sender.send_keepalive(xml_response)

    def start(self):
        log.info("[SIP] 🚀 Launching GB28181 SIP client...")

        # Generate device catalog on startup
        self.generate_device_catalog()

        # Start SIP message sender
        self.sip_sender.start()

        cfg_path = "/tmp/pjsua.cfg"
        sip = self.config["sip"]

        # Enhanced transport selection
        transport = self._determine_transport()
        log.info(f"[SIP] Using transport: {transport}")

        # Enhanced local port configuration
        local_port_option = f"--local-port {self.local_port} " if hasattr(self, 'local_port') else ""
        
        # Effective SIP domain is the server itself, used for AOR, registrar, and realm
        effective_sip_domain = sip['server']

        # Define proxy using server address and port
        proxy_address = sip['server']
        proxy_port = sip['port']

        proxy_uri = f"sip:{proxy_address}:{proxy_port}"
        if transport == "tcp":
            log.info("[SIP] Configuring proxy with TCP transport.")
            proxy_uri += ";transport=tcp"
        elif transport == "tls": # Placeholder for future TLS support
            log.info("[SIP] Configuring proxy with TLS transport (if implemented).")
            proxy_uri += ";transport=tls"
        # UDP is implicit if no transport parameter is added to the proxy_uri

        # Detailed logging of SIP configuration
        log.info(f"[SIP] Device ID: {sip['device_id']}")
        log.info(f"[SIP] Username: {sip['username']}")
        log.info(f"[SIP] Server: {effective_sip_domain}")
        log.info(f"[SIP] Proxy: {proxy_uri}")
        log.info(f"[SIP] Local port: {self.local_port}")

        # Updated PJSUA configuration for WVP-pro compatibility
        with open(cfg_path, "w") as f:
            f.write(f"""--id sip:{sip['username']}@{effective_sip_domain}
--registrar sip:{effective_sip_domain}:{sip['port']}
--proxy {proxy_uri}
--realm *
--username {sip['username']}
--password {sip['password']}
{local_port_option}--auto-answer 200
--null-audio
--duration 0
--log-level 5
--auto-update-nat=1
--reg-timeout=3600
--rereg-delay={self.registration_retry_interval}
--max-calls=4
--thread-cnt=4
--rtp-port=10000
--dis-codec=speex/16000
--dis-codec=speex/8000
--dis-codec=iLBC
--add-codec=H264
--publish
--use-timer=2
--timer-min-se=90
--timer-se=1800
""")

        def listen_loop():
            restart_count = 0
            max_restarts = 3  # Limit restart attempts
            
            while restart_count < max_restarts:
                try:
                    log.info(f"[SIP] Starting pjsua process (attempt {restart_count + 1}/{max_restarts})")
                    self._start_pjsua_process(cfg_path)
                    self._handle_pjsua_output()
                    
                    # If we reach here, the process exited normally
                    log.warning("[SIP] pjsua process exited normally")
                    break
                    
                except Exception as e:
                    restart_count += 1
                    log.error(f"[SIP] Error in listen loop (attempt {restart_count}/{max_restarts}): {e}")
                    
                    if restart_count >= max_restarts:
                        log.error("[SIP] Maximum restart attempts reached, giving up")
                        self.running = False
                        break
                        
                    log.info(f"[SIP] Waiting 10 seconds before restart attempt {restart_count + 1}")
                    time.sleep(10)  # Longer wait between restarts
                    
            log.warning("[SIP] SIP client listen loop has ended")

        thread = threading.Thread(target=listen_loop, daemon=True)
        thread.start()

        try:
            while True:
                time.sleep(1)
                self._check_registration()
                self._check_keepalive()
                self._check_streams()
        except KeyboardInterrupt:
            self.stop()

    def _determine_transport(self):
        """Determine which transport protocol to use"""
        # Always use UDP for WVP-pro as specified
        transport = self.config["sip"].get("transport", "udp").lower()
        if transport != "udp":
            log.warning("[SIP] WVP-pro requires UDP transport, overriding configured transport")
            transport = "udp"
        return transport

    def _start_pjsua_process(self, cfg_path):
        """Start the PJSUA process with the given configuration"""
        transport = self._determine_transport()
        
        # Kill any existing PJSUA processes
        self._kill_existing_pjsua_processes()
        
        # Create PJSUA configuration
        config = [
            "--id", f"sip:{self.username}@{self.server}",
            "--registrar", f"sip:{self.server}:{self.port}",
            "--realm", "*",
            "--username", self.username,
            "--password", self.password,
            "--local-port", str(self.local_port),
            "--log-level", "3",  # Reduced log level to minimize noise
            "--app-log-level", "3",
            "--log-file", "pjsua.log",
            "--auto-update-nat", "0",
            "--disable-stun",
            "--null-audio",
            "--auto-answer", "200",  # Auto-answer calls
            "--duration", "0",  # No call duration limit
            "--max-calls", "4",  # Match the compiled limit
            "--thread-cnt", "4",  # Use default thread count
        ]
        
        # Set transport based on configuration
        if transport == "udp":
            config.append("--no-tcp")  # Only use UDP
        elif transport == "tcp":
            config.append("--no-udp")  # Only use TCP
        
        try:
            # Ensure any old process is properly terminated
            if hasattr(self, 'process') and self.process:
                try:
                    self.process.terminate()
                    self.process.wait(timeout=5)
                except:
                    pass
            
            self.process = subprocess.Popen(
                ["pjsua"] + config,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                universal_newlines=True,
                bufsize=1,
                preexec_fn=os.setsid  # Create new process group
            )
            log.info(f"[SIP] Started PJSUA process with PID: {self.process.pid}")
            self.running = True
            
        except FileNotFoundError:
            log.error("[SIP] PJSUA not found. Please install pjsua.")
            raise
        except Exception as e:
            log.error(f"[SIP] Failed to start PJSUA: {e}")
            raise

    def _kill_existing_pjsua_processes(self):
        """Kill any existing pjsua processes that might be holding ports"""
        try:
            # Find all pjsua processes
            ps_process = subprocess.run(["ps", "-ef"], capture_output=True, text=True)
            lines = ps_process.stdout.split('\n')
            
            # Get the current process ID to avoid killing parent processes
            current_pid = os.getpid()
            
            for line in lines:
                if 'pjsua' in line and str(current_pid) not in line:
                    try:
                        # Extract PID
                        parts = line.split()
                        if len(parts) > 1:
                            pid = int(parts[1])
                            # Kill the process
                            log.info(f"[SIP] Killing existing pjsua process with PID {pid}")
                            os.kill(pid, signal.SIGKILL)
                    except Exception as e:
                        log.error(f"[SIP] Error killing pjsua process: {e}")
        except Exception as e:
            log.error(f"[SIP] Error checking for existing pjsua processes: {e}")

    def _handle_pjsua_output(self):
        """Handle PJSUA process output with improved error handling"""
        if not self.process:
            log.error("[SIP] No pjsua process to handle output from")
            return

        buffer = ""
        try:
            for line in self.process.stdout:
                if not self.running:
                    break
                    
                print(line.strip())
                
                # Buffer the line for context
                buffer += line
                if len(buffer) > 10000:
                    buffer = buffer[-5000:]  # Keep last 5000 chars
                
                # Enhanced message handling with better error handling
                try:
                    self._process_sip_message(line, buffer)
                except Exception as e:
                    log.error(f"[SIP] Error processing SIP message: {e}")
                    # Don't crash the whole handler for a single message error
                    continue
                    
        except Exception as e:
            log.error(f"[SIP] Error in output handler: {e}")
        finally:
            log.info("[SIP] PJSUA output handler finished")
            self.running = False

    def _process_sip_message(self, line, buffer):
        """Process SIP messages with improved handling"""
        # Registration status handling
        if "Registration successfully sent" in line:
            log.info("[SIP] Registration request sent successfully")
            self.registration_status = "registering"
        elif "Registration complete" in line:
            log.info("[SIP] ✅ Registration completed successfully")
            self.registration_status = "registered"
            self.registration_attempts = 0
        elif "Registration failed" in line:
            log.warning("[SIP] ⚠️ Registration failed")
            self.registration_status = "failed"
            self._handle_registration_failure()
            
        # Process incoming SIP MESSAGE requests containing XML
        if "MESSAGE sip:" in line and "Content-Type: application/MANSCDP+xml" in buffer:
            log.debug("[SIP] Received SIP MESSAGE with XML content")
            self.handle_catalog_query(buffer)
            
        # Process XML content in the message
        xml_match = re.search(r'<\?xml.*?<\/(?:Query|message|Message)>', buffer, re.DOTALL | re.IGNORECASE)
        if xml_match:
            log.debug("[SIP] Found XML in message")
            xml_content = xml_match.group(0)
            
            # Check for catalog query
            if "<CmdType>Catalog</CmdType>" in xml_content:
                log.info("[SIP] Received catalog query")
                self.handle_catalog_query(buffer)
            elif "<CmdType>DeviceInfo</CmdType>" in xml_content:
                log.info("[SIP] Received device info query")
                # Add device info handling here
            elif "<CmdType>DeviceStatus</CmdType>" in xml_content:
                log.info("[SIP] Received device status query")
                # Add device status handling here
            elif "<CmdType>RecordInfo</CmdType>" in xml_content:
                log.info("[SIP] Received record info query")
                # Handle record info query
                
        # Enhanced INVITE handling
        elif "INVITE" in line:
            log.info("[SIP] INVITE detected in log line, processing buffer for full message.")
            # Always pass the buffer for INVITEs, as the line itself is insufficient.
            self.handle_invite(buffer)
                
        # Enhanced MESSAGE handling
        elif "MESSAGE" in line:
            log.info("[SIP] MESSAGE detected in log line, checking content...")
            
            # Check for XML content - case insensitive to catch all variants 
            is_xml_content = False
            content_type_match = re.search(r'Content-Type:\s*(application/MANSCDP\+xml|Application/MANSCDP\+xml)', buffer, re.IGNORECASE)
            if content_type_match:
                log.info("[SIP] MANSCDP+xml content detected")
                is_xml_content = True
            
            if is_xml_content:
                # Look for catalog query first since it's most common
                if re.search(r'<CmdType>\s*Catalog\s*</CmdType>', buffer, re.IGNORECASE):
                    log.info("[SIP] Catalog query received")
                    self.handle_catalog_query(buffer)
                elif re.search(r'<CmdType>\s*DeviceInfo\s*</CmdType>', buffer, re.IGNORECASE):
                    log.info("[SIP] Device info query received")
                    self.handle_device_info_query(buffer)
                elif re.search(r'<CmdType>\s*RecordInfo\s*</CmdType>', buffer, re.IGNORECASE):
                    log.info("[SIP] RecordInfo query received")
                    self.handle_recordinfo_query(buffer)
                elif re.search(r'<CmdType>\s*DeviceControl\s*</CmdType>|<CmdType>\s*Control\s*</CmdType>', buffer, re.IGNORECASE):
                    log.info("[SIP] Device control message received")
                    self.handle_device_control(buffer)
                elif re.search(r'<CmdType>\s*Keepalive\s*</CmdType>', buffer, re.IGNORECASE):
                    log.info("[SIP] Keepalive message received")
                    self.handle_keepalive(buffer)
                else:
                    # If we can't identify the command type, dump the buffer for debugging
                    preview = buffer[:200] + "..." if len(buffer) > 200 else buffer
                    log.warning(f"[SIP] Unknown XML command type in MESSAGE: {preview}")
                    # Try to extract XML content for further analysis
                    xml_match = re.search(r'<\?xml.*?<\/(?:Query|message|Message|Response|Control|Notify)>', buffer, re.DOTALL | re.IGNORECASE)
                    if xml_match:
                        log.debug(f"[SIP] XML content found: {xml_match.group(0)}")
                        # Don't automatically treat unknown messages as catalog queries
                        log.info("[SIP] Ignoring unrecognized XML message type")
                    else:
                        log.debug("[SIP] No recognizable XML structure found in message")
            else:
                # Not XML content or couldn't determine
                log.info("[SIP] MESSAGE without MANSCDP+xml content or with unknown format")
                log.debug(f"[SIP] MESSAGE preview: {buffer[:200]}...")
                
        # Enhanced SUBSCRIBE handling
        elif "SUBSCRIBE" in line:
            if "Event: Catalog" in buffer:
                log.info("[SIP] Catalog subscription request received")
                self.handle_catalog_subscription(buffer)
            elif "Event: Alarm" in buffer:
                log.info("[SIP] Alarm subscription request received")
                # We don't support alarm handling yet, but we'll acknowledge
                self.handle_alarm_subscription(buffer)
                
        # Enhanced SDP handling
        elif "Received SDP" in line or "m=video" in line:
            self.parse_sdp_and_stream(line)

    def _handle_registration_failure(self):
        """Handle registration failures with retry logic"""
        self.registration_attempts += 1
        
        if self.registration_attempts >= self.max_registration_attempts:
            log.error(f"[SIP] ❌ Registration failed after {self.registration_attempts} attempts")
            return
            
        retry_delay = min(30, self.registration_retry_interval * self.registration_attempts)
        log.info(f"[SIP] Will retry registration in {retry_delay} seconds")
        
        # Schedule registration retry
        threading.Timer(retry_delay, self._retry_registration).start()

    def _retry_registration(self):
        """Retry SIP registration"""
        if self.registration_status == "registered":
            return
            
        log.info("[SIP] Retrying registration...")
        self._start_pjsua_process("/tmp/pjsua.cfg")

    def _check_registration(self):
        """Periodically check registration status"""
        if self.registration_status != "registered":
            return
            
        # Check if we need to refresh registration
        if time.time() - self.last_keepalive > 240:  # 4 minutes
            log.info("[SIP] Registration refresh needed")
            self._retry_registration()

    def _check_keepalive(self):
        """Send periodic keepalive messages"""
        if self.registration_status != "registered":
            return
            
        now = time.time()
        if now - self.last_keepalive >= self.keepalive_interval:
            self.last_keepalive = now
            self._send_keepalive()

    def _send_keepalive(self):
        """Send keepalive message to maintain registration"""
        try:
            xml_response = format_keepalive_response(self.device_id)
            self.sip_sender.send_keepalive(xml_response)
            log.debug("[SIP] Sent keepalive message")
        except Exception as e:
            log.error(f"[SIP] Error sending keepalive: {e}")

    def _check_streams(self):
        """Check and maintain active streams with enhanced monitoring"""
        now = time.time()
        for stream_id, stream_info in list(self.active_streams.items()):
            try:
                # Check stream health
                if not self.streamer.check_stream_health(stream_id):
                    log.warning(f"[SIP] Stream {stream_id} appears unhealthy")
                    self._handle_stream_failure(stream_id, stream_info)
                    continue

                # Check stream duration
                duration = now - stream_info["start_time"]
                if duration > 3600:  # 1 hour
                    log.info(f"[SIP] Stream {stream_id} running for {duration:.0f}s")
                    
                # Update stream status
                if duration % 60 < 1:  # Every minute
                    self._send_media_status_update(stream_id)
                    
            except Exception as e:
                log.error(f"[SIP] Error checking stream {stream_id}: {e}")

    def _handle_stream_failure(self, stream_id, stream_info):
        """Handle stream failures with recovery attempts"""
        try:
            # Stop the failed stream
            self.streamer.stop_stream(stream_id)
            
            # Attempt to restart the stream
            success = self.streamer.start_stream(
                video_path=stream_info.get("video_path"),
                dest_ip=stream_info["dest_ip"],
                dest_port=stream_info["dest_port"],
                ssrc=stream_info["ssrc"]
            )
            
            if success:
                log.info(f"[SIP] Successfully restarted stream {stream_id}")
            else:
                log.error(f"[SIP] Failed to restart stream {stream_id}")
                del self.active_streams[stream_id]
                
        except Exception as e:
            log.error(f"[SIP] Error handling stream failure: {e}")

    def stop(self):
        log.info("[SIP] Stopping all streams and SIP client...")
        # Stop the SIP message sender
        self.sip_sender.stop()
        
        # Stop all active media streams
        for callid, stream_info in list(self.active_streams.items()):
            try:
                stream_id = f"{stream_info['dest_ip']}:{stream_info['dest_port']}"
                if stream_info.get('ssrc'):
                    stream_id = f"{stream_id}:{stream_info['ssrc']}"
                
                log.info(f"[SIP] Stopping stream {stream_id} for Call-ID: {callid}")
                self.streamer.stop_stream(stream_id)
            except Exception as e:
                log.error(f"[SIP] Error stopping stream for Call-ID {callid}: {e}")
        
        # Clear active streams
        self.active_streams.clear()
            
        # Properly shutdown the media streamer
        self.streamer.shutdown()
        
        # Terminate the pjsua process
        if self.process:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()

    def _schedule_media_status_update(self, stream_id, delay=5.0):
        """Schedule a media status update after a delay"""
        if not stream_id:
            return
            
        def _update():
            try:
                self._send_media_status_update(stream_id)
            except Exception as e:
                log.error(f"[SIP] Error in scheduled status update: {e}")
                
        threading.Timer(delay, _update).start()

    def handle_catalog_subscription(self, msg_text):
        """Handle catalog subscription requests"""
        try:
            # Extract headers from the message
            call_id_match = re.search(r"Call-ID:\s*(.+)", msg_text, re.IGNORECASE)
            from_match = re.search(r"From:\s*<(.+?)>", msg_text, re.IGNORECASE)
            to_match = re.search(r"To:\s*<(.+?)>", msg_text, re.IGNORECASE)
            via_match = re.search(r"Via:\s*(.+)", msg_text, re.IGNORECASE)
            cseq_match = re.search(r"CSeq:\s*(\d+)\s+SUBSCRIBE", msg_text, re.IGNORECASE)
            event_match = re.search(r"Event:\s*Catalog", msg_text, re.IGNORECASE)
            
            if not all([call_id_match, from_match, to_match, via_match, cseq_match, event_match]):
                log.warning("[SIP] Missing required headers in SUBSCRIBE request")
                return
                
            call_id = call_id_match.group(1).strip()
            from_uri = from_match.group(1).strip()
            to_uri = to_match.group(1).strip()
            via_header = via_match.group(1).strip()
            cseq = cseq_match.group(1).strip()
            
            # Extract SN and DeviceID from the XML body if present
            xml_match = re.search(r'<\?xml.*<\/Query>', msg_text, re.DOTALL)
            if xml_match:
                # Parse the XML
                xml_content = xml_match.group(0)
                root = ET.fromstring(xml_content)
                
                # Extract basic information
                cmd_type = root.find('CmdType').text if root.find('CmdType') is not None else ""
                device_id = root.find('DeviceID').text if root.find('DeviceID') is not None else ""
                sn = root.find('SN').text if root.find('SN') is not None else ""
                
                log.info(f"[SIP] Catalog subscription: CMD={cmd_type}, SN={sn}, DeviceID={device_id}")
            
            # Prepare a NOTIFY with the device catalog
            self.sip_sender.send_notify_catalog(
                call_id=call_id,
                from_uri=from_uri,
                to_uri=to_uri,
                via_header=via_header,
                cseq=cseq
            )
            
            log.info(f"[SIP] Sent catalog notification for subscription: {call_id}")
            
        except Exception as e:
            log.error(f"[SIP] Error handling catalog subscription: {e}")
            
    def handle_alarm_subscription(self, msg_text):
        """Handle alarm subscription requests"""
        try:
            # Extract headers from the message
            call_id_match = re.search(r"Call-ID:\s*(.+)", msg_text, re.IGNORECASE)
            from_match = re.search(r"From:\s*<(.+?)>", msg_text, re.IGNORECASE)
            to_match = re.search(r"To:\s*<(.+?)>", msg_text, re.IGNORECASE)
            via_match = re.search(r"Via:\s*(.+)", msg_text, re.IGNORECASE)
            cseq_match = re.search(r"CSeq:\s*(\d+)\s+SUBSCRIBE", msg_text, re.IGNORECASE)
            
            if not all([call_id_match, from_match, to_match, via_match, cseq_match]):
                log.warning("[SIP] Missing required headers in alarm SUBSCRIBE request")
                return
                
            # We don't yet support alarms, but respond with a 200 OK
            log.info("[SIP] Alarm subscription received (not supported)")
            
        except Exception as e:
            log.error(f"[SIP] Error handling alarm subscription: {e}")
