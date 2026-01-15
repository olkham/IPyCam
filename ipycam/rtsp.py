#!/usr/bin/env python3
"""
Python Native RTSP Server

A lightweight RTSP server that serves video frames directly without
requiring go2rtc. Supports TCP interleaved mode for reliable streaming.

Based on concepts from https://github.com/vladpen/python-rtsp-server
but simplified for serving generated frames rather than proxying.

Features:
- Pure Python (no external dependencies)
- Serves H.264 video stream from numpy frames
- TCP interleaved mode for reliability
- Multiple simultaneous clients
- No external RTSP server required
"""

import asyncio
import struct
import time
import threading
import socket
import re
from collections import deque
from dataclasses import dataclass, field
from fractions import Fraction
from typing import Optional, Dict, Deque, Callable
from random import choices, randrange
import string
import numpy as np

# Try to import av for H.264 encoding
try:
    import av
    AV_AVAILABLE = True
except ImportError:
    AV_AVAILABLE = False


def is_rtsp_server_available() -> bool:
    """Check if the native RTSP server can be used (requires av library)."""
    return AV_AVAILABLE


@dataclass
class RTSPStats:
    """Statistics for RTSP streaming with sliding window FPS calculation"""
    frames_sent: int = 0
    bytes_sent: int = 0
    start_time: float = field(default_factory=time.time)
    _frame_timestamps: Deque[float] = field(default_factory=lambda: deque(maxlen=150))
    _window_seconds: float = 5.0
    
    @property
    def elapsed_time(self) -> float:
        return time.time() - self.start_time
    
    @property
    def actual_fps(self) -> float:
        if len(self._frame_timestamps) < 2:
            return 0
        current_time = time.time()
        cutoff_time = current_time - self._window_seconds
        recent_frames = sum(1 for ts in self._frame_timestamps if ts >= cutoff_time)
        if recent_frames < 2:
            return 0
        oldest_in_window = next((ts for ts in self._frame_timestamps if ts >= cutoff_time), None)
        if oldest_in_window is None:
            return 0
        time_span = current_time - oldest_in_window
        if time_span > 0:
            return recent_frames / time_span
        return 0
    
    def record_frame(self, timestamp: float):
        self._frame_timestamps.append(timestamp)


class H264Encoder:
    """
    H.264 encoder using PyAV.
    Encodes numpy frames to H.264 NAL units.
    """
    
    def __init__(self, width: int, height: int, fps: int = 30, bitrate: str = "2M"):
        self.width = width
        self.height = height
        self.fps = fps
        self.bitrate = bitrate
        self._encoder: Optional[av.CodecContext] = None
        self._output_container = None
        self._pts = 0
        self._sps: Optional[bytes] = None
        self._pps: Optional[bytes] = None
        self._lock = threading.Lock()
        self._initialized = False
        
    def _setup_encoder(self):
        """Initialize the H.264 encoder."""
        if not AV_AVAILABLE:
            raise RuntimeError("PyAV not available")
        
        # Use a simpler approach - create codec context directly
        self._encoder = av.CodecContext.create('libx264', 'w')
        self._encoder.width = self.width
        self._encoder.height = self.height
        self._encoder.pix_fmt = 'yuv420p'
        self._encoder.time_base = Fraction(1, 90000)  # RTP uses 90kHz clock
        self._encoder.framerate = Fraction(self.fps, 1)
        
        # Parse bitrate
        bitrate_val = self.bitrate.upper()
        if bitrate_val.endswith('M'):
            bitrate_int = int(float(bitrate_val[:-1]) * 1_000_000)
        elif bitrate_val.endswith('K'):
            bitrate_int = int(float(bitrate_val[:-1]) * 1_000)
        else:
            bitrate_int = int(bitrate_val)
        
        self._encoder.bit_rate = bitrate_int
        self._encoder.gop_size = self.fps * 2  # Keyframe every 2 seconds (less overhead)
        self._encoder.max_b_frames = 0  # No B-frames for low latency
        self._encoder.thread_count = 4  # Use multiple threads
        self._encoder.thread_type = 'FRAME'  # Frame-level threading
        
        # Set encoding options for maximum speed
        self._encoder.options = {
            'preset': 'ultrafast',
            'tune': 'zerolatency',
            'profile': 'baseline',
            'level': '3.1',
            'crf': '28',  # Lower quality = faster encode
            'x264-params': 'keyint=60:min-keyint=60:scenecut=0:bframes=0:ref=1:me=dia:subme=0:trellis=0:weightp=0:8x8dct=0',
        }
        
        self._encoder.open()
        self._initialized = True
        
        # Extract SPS/PPS from extradata (AVCC format)
        if self._encoder.extradata:
            self._extract_sps_pps(self._encoder.extradata)
    
    def _extract_sps_pps(self, extradata: bytes):
        """Extract SPS and PPS from encoder extradata (AVCC format)."""
        if len(extradata) < 8:
            return
        
        try:
            # AVCC format: version(1) + profile(1) + compat(1) + level(1) + 
            # lengthSizeMinusOne(1) + numSPS(1) + [sps_len(2) + sps_data]... + 
            # numPPS(1) + [pps_len(2) + pps_data]...
            idx = 5
            
            # Number of SPS (lower 5 bits)
            num_sps = extradata[idx] & 0x1F
            idx += 1
            
            for _ in range(num_sps):
                if idx + 2 > len(extradata):
                    break
                sps_len = struct.unpack('>H', extradata[idx:idx+2])[0]
                idx += 2
                if idx + sps_len > len(extradata):
                    break
                self._sps = extradata[idx:idx+sps_len]
                idx += sps_len
            
            # Number of PPS
            if idx < len(extradata):
                num_pps = extradata[idx]
                idx += 1
                
                for _ in range(num_pps):
                    if idx + 2 > len(extradata):
                        break
                    pps_len = struct.unpack('>H', extradata[idx:idx+2])[0]
                    idx += 2
                    if idx + pps_len > len(extradata):
                        break
                    self._pps = extradata[idx:idx+pps_len]
                    idx += pps_len
        except Exception as e:
            print(f"Error extracting SPS/PPS: {e}")
    
    def encode(self, frame: np.ndarray) -> list:
        """
        Encode a numpy frame to H.264 NAL units.
        Returns list of NAL unit bytes (WITHOUT start codes - raw NAL data).
        """
        import cv2
        
        with self._lock:
            if self._encoder is None:
                self._setup_encoder()
            
            # Ensure frame is the right size
            h, w = frame.shape[:2]
            if w != self.width or h != self.height:
                frame = cv2.resize(frame, (self.width, self.height))
            
            # Convert BGR to YUV420p using av
            if len(frame.shape) == 3 and frame.shape[2] == 3:
                av_frame = av.VideoFrame.from_ndarray(frame, format='bgr24')
                av_frame = av_frame.reformat(format='yuv420p')
            else:
                av_frame = av.VideoFrame.from_ndarray(frame, format='gray')
                av_frame = av_frame.reformat(format='yuv420p')
            
            # Set PTS in 90kHz units
            av_frame.pts = self._pts * (90000 // self.fps)
            self._pts += 1
            
            # Encode
            packets = self._encoder.encode(av_frame)
            
            nal_units = []
            for packet in packets:
                data = bytes(packet)
                nal_units.extend(self._parse_nal_units(data))
            
            return nal_units
    
    def _parse_nal_units(self, data: bytes) -> list:
        """Parse NAL units from encoded data (handles both Annex B and AVCC formats)."""
        nal_units = []
        
        if not data:
            return nal_units
        
        # Check if it's Annex B format (starts with start code)
        if data.startswith(b'\x00\x00\x00\x01') or data.startswith(b'\x00\x00\x01'):
            # Annex B format - split by start codes
            # Find all start code positions
            i = 0
            nal_starts = []
            while i < len(data):
                if data[i:i+4] == b'\x00\x00\x00\x01':
                    nal_starts.append((i, 4))
                    i += 4
                elif data[i:i+3] == b'\x00\x00\x01':
                    nal_starts.append((i, 3))
                    i += 3
                else:
                    i += 1
            
            # Extract NAL units
            for j, (start, prefix_len) in enumerate(nal_starts):
                nal_start = start + prefix_len
                if j + 1 < len(nal_starts):
                    nal_end = nal_starts[j + 1][0]
                else:
                    nal_end = len(data)
                
                nal_data = data[nal_start:nal_end]
                # Remove any trailing zeros
                while nal_data and nal_data[-1] == 0:
                    nal_data = nal_data[:-1]
                if nal_data:
                    nal_units.append(nal_data)
        else:
            # Try AVCC format (4-byte length prefix)
            idx = 0
            while idx < len(data):
                if idx + 4 > len(data):
                    break
                nal_len = struct.unpack('>I', data[idx:idx+4])[0]
                idx += 4
                if nal_len == 0 or nal_len > len(data) - idx:
                    # Not valid AVCC, try treating as single NAL
                    if data:
                        nal_units.append(data)
                    break
                nal_data = data[idx:idx+nal_len]
                if nal_data:
                    nal_units.append(nal_data)
                idx += nal_len
        
        return nal_units
    
    def get_sps_pps(self) -> tuple:
        """Get SPS and PPS NAL units (raw, without start codes)."""
        with self._lock:
            if not self._initialized:
                self._setup_encoder()
        return self._sps, self._pps
    
    def close(self):
        """Clean up encoder resources."""
        with self._lock:
            if self._encoder:
                try:
                    self._encoder = None  # type: ignore
                except Exception:
                    pass
                self._encoder = None


class RTSPClient:
    """Represents a connected RTSP client."""
    
    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        self.reader = reader
        self.writer = writer
        self.peername = writer.get_extra_info('peername')
        self.host = self.peername[0] if self.peername else 'unknown'
        self.port = self.peername[1] if self.peername else 0
        self.session_id = ''.join(choices(string.ascii_lowercase + string.digits, k=12))
        self.cseq = 0
        self.playing = False
        self.interleaved_channel = 0
        self.use_tcp = False  # Whether using TCP interleaved mode
        self.udp_client_port = 0  # Client's RTP port for UDP mode
        self.udp_client_rtcp_port = 0  # Client's RTCP port for UDP mode
        self._closed = False
        self._udp_transport = None  # UDP transport for sending RTP packets
        
    async def send_response(self, code: int, reason: str, *headers: str, body: str = ''):
        """Send RTSP response."""
        if self._closed:
            return
        
        response = f'RTSP/1.0 {code} {reason}\r\n'
        response += f'CSeq: {self.cseq}\r\n'
        for header in headers:
            response += f'{header}\r\n'
        if body:
            response += f'Content-Length: {len(body)}\r\n'
        response += '\r\n'
        if body:
            response += body
        
        try:
            self.writer.write(response.encode())
            await self.writer.drain()
        except Exception:
            self._closed = True
    
    async def send_interleaved_data(self, channel: int, data: bytes):
        """Send interleaved RTP data over TCP."""
        if self._closed:
            return
        
        # Interleaved header: $ + channel (1 byte) + length (2 bytes)
        header = struct.pack('>cBH', b'$', channel, len(data))
        try:
            self.writer.write(header + data)
            await self.writer.drain()
        except Exception:
            self._closed = True
    
    async def close(self):
        """Close the client connection."""
        self._closed = True
        try:
            self.writer.close()
            await self.writer.wait_closed()
        except Exception:
            pass


class NativeRTSPServer:
    """
    Native Python RTSP server that serves H.264 video frames.
    
    Usage:
        server = NativeRTSPServer(port=8554, fps=30, width=1920, height=1080)
        server.start()
        
        while running:
            frame = get_frame()
            server.stream_frame(frame)
        
        server.stop()
    """
    
    def __init__(self, port: int = 8554, fps: int = 30, width: int = 1920, 
                 height: int = 1080, bitrate: str = "2M", stream_name: str = "stream"):
        self.port = port
        self.fps = fps
        self.width = width
        self.height = height
        self.bitrate = bitrate
        self.stream_name = stream_name
        
        self._encoder: Optional[H264Encoder] = None
        self._clients: Dict[str, RTSPClient] = {}
        self._clients_lock = threading.Lock()
        
        self._server: Optional[asyncio.Server] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._running = False
        
        self._stats = RTSPStats()
        self._rtp_seq = 0
        self._rtp_timestamp = 0
        self._ssrc = randrange(0, 0xFFFFFFFF)
        
        # Pre-encoded frame buffer (encode once, send to all clients)
        self._encoded_frame: Optional[list] = None  # List of NAL units
        self._encoded_frame_num = 0
        self._frame_lock = threading.Lock()
        self._current_frame: Optional[np.ndarray] = None
        self._last_encoded_id = -1  # Track which frame was last encoded
        
    @property
    def stats(self) -> RTSPStats:
        return self._stats
    
    @property
    def client_count(self) -> int:
        with self._clients_lock:
            return len(self._clients)
    
    @property
    def is_running(self) -> bool:
        return self._running
    
    @property
    def rtsp_url(self) -> str:
        """Get the RTSP URL for this server."""
        hostname = socket.gethostname()
        try:
            local_ip = socket.gethostbyname(hostname)
        except Exception:
            local_ip = "127.0.0.1"
        return f"rtsp://{local_ip}:{self.port}/{self.stream_name}"
    
    def start(self) -> bool:
        """Start the RTSP server."""
        if not AV_AVAILABLE:
            print("  ⚠ Native RTSP requires PyAV: pip install av")
            return False
        
        try:
            # Initialize encoder
            self._encoder = H264Encoder(self.width, self.height, self.fps, self.bitrate)
            
            # Start asyncio event loop in background thread
            self._thread = threading.Thread(target=self._run_server, daemon=True)
            self._thread.start()
            
            # Wait for server to start
            timeout = 5.0
            start = time.time()
            while not self._running and (time.time() - start) < timeout:
                time.sleep(0.1)
            
            return self._running
        except Exception as e:
            print(f"  ⚠ Failed to start RTSP server: {e}")
            return False
    
    def _run_server(self):
        """Run the asyncio server in a background thread."""
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        
        try:
            self._loop.run_until_complete(self._start_server())
            self._running = True
            self._loop.run_forever()
        except Exception as e:
            print(f"RTSP server error: {e}")
        finally:
            self._running = False
            self._loop.close()
    
    async def _start_server(self):
        """Start the TCP server for RTSP connections."""
        self._server = await asyncio.start_server(
            self._handle_client,
            '0.0.0.0',
            self.port
        )
        print(f"  RTSP Server: listening on port {self.port}")
    
    async def _handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        """Handle a new RTSP client connection."""
        client = RTSPClient(reader, writer)
        
        with self._clients_lock:
            self._clients[client.session_id] = client
        
        try:
            while not client._closed:
                data = await reader.read(4096)
                if not data:
                    break
                
                await self._process_request(client, data.decode('utf-8', errors='ignore'))
                
                # If client is playing, start streaming to them
                if client.playing:
                    await self._stream_to_client(client)
        except Exception as e:
            pass
        finally:
            with self._clients_lock:
                self._clients.pop(client.session_id, None)
            await client.close()
    
    async def _process_request(self, client: RTSPClient, request: str):
        """Process an RTSP request."""
        lines = request.split('\r\n')
        if not lines:
            return
        
        # Parse request line
        parts = lines[0].split(' ')
        if len(parts) < 2:
            return
        
        method = parts[0]
        
        # Extract CSeq
        cseq_match = re.search(r'CSeq:\s*(\d+)', request, re.IGNORECASE)
        if cseq_match:
            client.cseq = int(cseq_match.group(1))
        
        if method == 'OPTIONS':
            await client.send_response(
                200, 'OK',
                'Public: OPTIONS, DESCRIBE, SETUP, PLAY, TEARDOWN'
            )
        
        elif method == 'DESCRIBE':
            sdp = self._generate_sdp()
            await client.send_response(
                200, 'OK',
                'Content-Type: application/sdp',
                body=sdp
            )
        
        elif method == 'SETUP':
            # Parse transport header to determine mode
            transport_header = re.search(r'Transport:\s*([^\r\n]+)', request, re.IGNORECASE)
            transport_str = transport_header.group(1) if transport_header else ''
            
            # Check for TCP interleaved mode
            tcp_match = re.search(r'RTP/AVP/TCP', transport_str, re.IGNORECASE)
            interleaved_match = re.search(r'interleaved=(\d+)-(\d+)', transport_str)
            udp_port_match = re.search(r'client_port=(\d+)-(\d+)', transport_str)
            
            if tcp_match or interleaved_match:
                # TCP interleaved mode
                client.use_tcp = True
                if interleaved_match:
                    client.interleaved_channel = int(interleaved_match.group(1))
                    transport = f'Transport: RTP/AVP/TCP;unicast;interleaved={interleaved_match.group(1)}-{interleaved_match.group(2)}'
                else:
                    client.interleaved_channel = 0
                    transport = 'Transport: RTP/AVP/TCP;unicast;interleaved=0-1'
            elif udp_port_match:
                # UDP mode - client specified ports
                client.use_tcp = False
                client.udp_client_port = int(udp_port_match.group(1))
                client.udp_client_rtcp_port = int(udp_port_match.group(2))
                # We'll use server ports 5000-5001 (could make configurable)
                server_rtp_port = 5000
                server_rtcp_port = 5001
                transport = f'Transport: RTP/AVP;unicast;client_port={client.udp_client_port}-{client.udp_client_rtcp_port};server_port={server_rtp_port}-{server_rtcp_port}'
                # Create UDP socket for this client
                await self._setup_udp_for_client(client)
            else:
                # Default to TCP interleaved if no transport specified
                client.use_tcp = True
                client.interleaved_channel = 0
                transport = 'Transport: RTP/AVP/TCP;unicast;interleaved=0-1'
            
            await client.send_response(
                200, 'OK',
                transport,
                f'Session: {client.session_id};timeout=60'
            )
        
        elif method == 'PLAY':
            client.playing = True
            await client.send_response(
                200, 'OK',
                f'Session: {client.session_id}',
                f'RTP-Info: url=rtsp://localhost:{self.port}/{self.stream_name}/track1;seq={self._rtp_seq};rtptime={self._rtp_timestamp}'
            )
        
        elif method == 'TEARDOWN':
            client.playing = False
            await client.send_response(200, 'OK', f'Session: {client.session_id}')
            await client.close()
    
    def _generate_sdp(self) -> str:
        """Generate SDP description for the stream."""
        # Initialize encoder if needed to get SPS/PPS
        if self._encoder is None:
            self._encoder = H264Encoder(self.width, self.height, self.fps)
        
        sps, pps = self._encoder.get_sps_pps()
        
        # Build fmtp line with SPS/PPS if available
        fmtp = 'a=fmtp:96 packetization-mode=1'
        if sps and pps:
            import base64
            sps_b64 = base64.b64encode(sps).decode()
            pps_b64 = base64.b64encode(pps).decode()
            # profile-level-id is bytes 1-3 of SPS (skip NAL header at byte 0)
            profile_level_id = sps[1:4].hex() if len(sps) >= 4 else '42001f'
            fmtp = f'a=fmtp:96 packetization-mode=1;profile-level-id={profile_level_id};sprop-parameter-sets={sps_b64},{pps_b64}'
        
        hostname = socket.gethostname()
        try:
            local_ip = socket.gethostbyname(hostname)
        except Exception:
            local_ip = "127.0.0.1"
        
        sdp = f"""v=0
o=- {randrange(1000000, 9999999)} 1 IN IP4 {local_ip}
s=IPyCam Native RTSP
t=0 0
m=video 0 RTP/AVP 96
c=IN IP4 0.0.0.0
a=rtpmap:96 H264/90000
{fmtp}
a=control:track1
"""
        return sdp.strip()
    
    async def _setup_udp_for_client(self, client: RTSPClient):
        """Setup UDP socket for sending RTP to client."""
        try:
            # Create a UDP socket to send to client
            loop = asyncio.get_event_loop()
            transport, protocol = await loop.create_datagram_endpoint(
                asyncio.DatagramProtocol,
                local_addr=('0.0.0.0', 0)  # Bind to any available port
            )
            client._udp_transport = transport
        except Exception as e:
            print(f"Failed to setup UDP for client: {e}")
            client.use_tcp = True  # Fall back to TCP
    
    async def _stream_to_client(self, client: RTSPClient):
        """Stream video to a playing client."""
        frame_interval = 1.0 / self.fps
        next_frame_time = time.time()
        sent_sps_pps = False
        last_frame_num = -1
        
        while client.playing and not client._closed and self._running:
            # Get pre-encoded frame
            with self._frame_lock:
                nal_units = self._encoded_frame
                frame_num = self._encoded_frame_num
            
            # Only send if we have a new frame
            if nal_units and frame_num != last_frame_num:
                try:
                    # Calculate timestamp for this frame (90kHz clock)
                    timestamp = (self._rtp_timestamp + frame_num * (90000 // self.fps)) & 0xFFFFFFFF
                    
                    # Send SPS/PPS before first frame and periodically (every ~2 seconds)
                    if not sent_sps_pps or (frame_num % (self.fps * 2) == 0):
                        await self._send_sps_pps(client, timestamp)
                        sent_sps_pps = True
                    
                    # Send each NAL unit as RTP packets (all with same timestamp for same frame)
                    for nal in nal_units:
                        await self._send_rtp_packets(client, nal, timestamp)
                    
                    self._stats.frames_sent += 1
                    self._stats.record_frame(time.time())
                    last_frame_num = frame_num
                except Exception as e:
                    print(f"Streaming error: {e}")
                    break
            
            # Short sleep to avoid busy loop, but responsive to new frames
            await asyncio.sleep(0.001)
    
    async def _send_sps_pps(self, client: RTSPClient, timestamp: int):
        """Send SPS and PPS NAL units to initialize decoder."""
        sps, pps = self._encoder.get_sps_pps()
        if sps:
            await self._send_rtp_packets(client, sps, timestamp)
        if pps:
            await self._send_rtp_packets(client, pps, timestamp)
    
    async def _send_rtp_packets(self, client: RTSPClient, nal_unit: bytes, timestamp: Optional[int] = None):
        """Send NAL unit as RTP packets (with fragmentation if needed)."""
        # Remove start code if present (we work with raw NAL data)
        if nal_unit.startswith(b'\x00\x00\x00\x01'):
            nal_unit = nal_unit[4:]
        elif nal_unit.startswith(b'\x00\x00\x01'):
            nal_unit = nal_unit[3:]
        
        if not nal_unit:
            return
        
        MAX_RTP_SIZE = 1400  # Leave room for RTP header
        
        if len(nal_unit) <= MAX_RTP_SIZE:
            # Single NAL unit packet
            rtp_packet = self._create_rtp_packet(nal_unit, marker=True, timestamp=timestamp)
            await self._send_rtp_data(client, rtp_packet)
        else:
            # FU-A fragmentation
            nal_type = nal_unit[0] & 0x1F
            nri = nal_unit[0] & 0x60
            
            fragments = []
            remaining = nal_unit[1:]  # Skip NAL header
            
            while remaining:
                chunk_size = min(MAX_RTP_SIZE - 2, len(remaining))  # -2 for FU indicator and header
                chunk = remaining[:chunk_size]
                remaining = remaining[chunk_size:]
                fragments.append(chunk)
            
            for i, fragment in enumerate(fragments):
                start = (i == 0)
                end = (i == len(fragments) - 1)
                
                # FU indicator: NRI + type 28 (FU-A)
                fu_indicator = nri | 28
                
                # FU header: S/E bits + NAL type
                fu_header = nal_type
                if start:
                    fu_header |= 0x80  # Start bit
                if end:
                    fu_header |= 0x40  # End bit
                
                fu_nal = bytes([fu_indicator, fu_header]) + fragment
                rtp_packet = self._create_rtp_packet(fu_nal, marker=end, timestamp=timestamp)
                await self._send_rtp_data(client, rtp_packet)
    
    async def _send_rtp_data(self, client: RTSPClient, rtp_packet: bytes):
        """Send RTP data to client via TCP or UDP."""
        if client.use_tcp:
            # TCP interleaved mode
            await client.send_interleaved_data(client.interleaved_channel, rtp_packet)
        else:
            # UDP mode
            if client._udp_transport:
                try:
                    client._udp_transport.sendto(rtp_packet, (client.host, client.udp_client_port))
                except Exception:
                    pass
    
    def _create_rtp_packet(self, payload: bytes, marker: bool = False, timestamp: Optional[int] = None) -> bytes:
        """Create an RTP packet with the given payload."""
        # RTP header
        version = 2
        padding = 0
        extension = 0
        cc = 0  # CSRC count
        payload_type = 96  # Dynamic for H.264
        
        first_byte = (version << 6) | (padding << 5) | (extension << 4) | cc
        second_byte = (1 if marker else 0) << 7 | payload_type
        
        self._rtp_seq = (self._rtp_seq + 1) & 0xFFFF
        
        # Use provided timestamp or current timestamp (don't increment here - caller manages)
        ts = timestamp if timestamp is not None else self._rtp_timestamp
        
        header = struct.pack('>BBHII',
            first_byte,
            second_byte,
            self._rtp_seq,
            ts & 0xFFFFFFFF,
            self._ssrc
        )
        
        self._stats.bytes_sent += len(header) + len(payload)
        return header + payload
    
    def stream_frame(self, frame: np.ndarray):
        """Submit a frame for streaming to all connected clients.
        
        Pre-encodes the frame so all clients get the same encoded data.
        Drops frames if encoding can't keep up.
        """
        if not self._encoder or not self._running:
            return
        
        # Only encode if we have clients
        if self.client_count == 0:
            return
        
        # Skip if we're still encoding the previous frame
        if self._frame_lock.locked():
            return
        
        try:
            # Encode frame once (this is the expensive operation)
            nal_units = self._encoder.encode(frame)
            
            if nal_units:
                with self._frame_lock:
                    self._encoded_frame = nal_units
                    self._encoded_frame_num += 1
        except Exception as e:
            print(f"RTSP encode error: {e}")
    
    def stop(self):
        """Stop the RTSP server."""
        self._running = False
        
        if self._encoder:
            self._encoder.close()
            self._encoder = None
        
        # Close all clients
        with self._clients_lock:
            for client in list(self._clients.values()):
                if self._loop and not self._loop.is_closed():
                    asyncio.run_coroutine_threadsafe(client.close(), self._loop)
            self._clients.clear()
        
        # Stop the server
        if self._server and self._loop and not self._loop.is_closed():
            self._loop.call_soon_threadsafe(self._server.close)
        
        # Stop the event loop
        if self._loop and not self._loop.is_closed():
            self._loop.call_soon_threadsafe(self._loop.stop)
        
        # Wait for thread to finish
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)
        
        print("  RTSP server stopped")
