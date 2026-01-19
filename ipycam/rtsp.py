#!/usr/bin/env python3
"""
Native Python RTSP Server

Provides RTSP streaming capability when go2rtc is not available.
This is a lightweight RTSP server that streams H.264 video encoded
via software (libx264) using ffmpeg subprocess or raw frames.

Note: This is a fallback solution. For production use, go2rtc is recommended
as it provides better performance and more features.
"""

import socket
import threading
import time
import subprocess
import struct
import hashlib
import base64
import os
import re
from typing import Optional, Dict, List, Callable, Any
from dataclasses import dataclass, field
from collections import deque
from enum import Enum

import numpy as np
import cv2


class RTSPState(Enum):
    """RTSP session states"""
    INIT = "init"
    READY = "ready"
    PLAYING = "playing"
    TEARDOWN = "teardown"


@dataclass
class RTSPSession:
    """Represents an RTSP client session"""
    session_id: str
    client_socket: socket.socket
    client_address: tuple
    state: RTSPState = RTSPState.INIT
    transport: Optional[str] = None
    rtp_port: int = 0
    rtcp_port: int = 0
    rtp_socket: Optional[socket.socket] = None
    sequence_number: int = 0
    ssrc: int = 0
    timestamp: int = 0
    interleaved: bool = False  # TCP interleaved mode
    interleaved_channel: int = 0
    last_activity: float = field(default_factory=time.time)
    stream_name: Optional[str] = None  # Stream name for this session


@dataclass
class RTSPStreamInfo:
    """Information about an RTSP stream"""
    name: str
    width: int
    height: int
    fps: int
    bitrate: str = "4M"


class NativeRTSPServer:
    """
    Native Python RTSP Server
    
    Provides basic RTSP streaming functionality without requiring go2rtc.
    Supports both UDP and TCP interleaved RTP transport.
    
    Limitations compared to go2rtc:
    - Software encoding only (higher CPU usage)
    - Basic RTSP implementation (no advanced features)
    - Single encoder process per stream
    """
    
    def __init__(self, port: int = 8554, host: str = "0.0.0.0"):
        """
        Initialize the RTSP server.
        
        Args:
            port: RTSP server port (default 8554)
            host: Host address to bind to
        """
        self.port = port
        self.host = host
        self._server_socket: Optional[socket.socket] = None
        self._is_running = False
        self._sessions: Dict[str, RTSPSession] = {}
        self._streams: Dict[str, RTSPStreamInfo] = {}
        self._frame_buffers: Dict[str, Optional[np.ndarray]] = {}
        self._frame_locks: Dict[str, threading.Lock] = {}
        self._encoder_processes: Dict[str, subprocess.Popen] = {}
        self._encoder_threads: Dict[str, threading.Thread] = {}
        self._rtp_threads: Dict[str, threading.Thread] = {}
        self._lock = threading.Lock()
        self._accept_thread: Optional[threading.Thread] = None
        
        # Stats
        self._start_time: Optional[float] = None
        self._frame_timestamps: deque = deque(maxlen=150)
        self._total_frames: int = 0

        self.verbose = False
        
    def add_stream(self, name: str, width: int, height: int, fps: int, bitrate: str = "4M") -> bool:
        """
        Add a stream endpoint to the server.
        
        Args:
            name: Stream name (used in RTSP URL, e.g., "video_main")
            width: Video width
            height: Video height
            fps: Frames per second
            bitrate: Target bitrate (e.g., "4M", "1M")
            
        Returns:
            True if stream was added successfully
        """
        with self._lock:
            self._streams[name] = RTSPStreamInfo(
                name=name,
                width=width,
                height=height,
                fps=fps,
                bitrate=bitrate
            )
            self._frame_buffers[name] = None
            self._frame_locks[name] = threading.Lock()
        return True
    
    def start(self) -> bool:
        """Start the RTSP server"""
        if self._is_running:
            return True
            
        try:
            self._server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self._server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self._server_socket.bind((self.host, self.port))
            self._server_socket.listen(5)
            self._server_socket.settimeout(1.0)  # Allow periodic checking for shutdown
            
            self._is_running = True
            self._start_time = time.time()
            
            # Start accept thread
            self._accept_thread = threading.Thread(target=self._accept_loop, daemon=True)
            self._accept_thread.start()
            
            return True
        except Exception as e:
            print(f"Failed to start RTSP server: {e}")
            return False
    
    def stop(self):
        """Stop the RTSP server and clean up all resources"""
        self._is_running = False
        
        # Close all sessions
        with self._lock:
            for session_id, session in list(self._sessions.items()):
                self._close_session(session)
            self._sessions.clear()
        
        # Stop all encoders
        for name, proc in list(self._encoder_processes.items()):
            try:
                if proc.stdin:
                    proc.stdin.close()
                proc.terminate()
                proc.wait(timeout=2)
            except:
                try:
                    proc.kill()
                except:
                    pass
        self._encoder_processes.clear()
        
        # Close server socket
        if self._server_socket:
            try:
                self._server_socket.close()
            except:
                pass
            self._server_socket = None
    
    def stream_frame(self, stream_name: str, frame: np.ndarray) -> bool:
        """
        Send a frame to a specific stream.
        
        Args:
            stream_name: Name of the stream (e.g., "video_main")
            frame: NumPy array in BGR format
            
        Returns:
            True if frame was queued successfully
        """
        if not self._is_running or stream_name not in self._streams:
            return False
        
        # Update frame buffer
        lock = self._frame_locks.get(stream_name)
        if lock:
            with lock:
                self._frame_buffers[stream_name] = frame.copy()
        
        self._total_frames += 1
        self._frame_timestamps.append(time.time())
        
        return True
    
    @property
    def is_running(self) -> bool:
        return self._is_running
    
    @property
    def client_count(self) -> int:
        """Return number of active RTSP sessions"""
        with self._lock:
            return sum(1 for s in self._sessions.values() if s.state == RTSPState.PLAYING)
    
    @property
    def actual_fps(self) -> float:
        """Calculate FPS over a sliding window"""
        if len(self._frame_timestamps) < 2:
            return 0
        
        current_time = time.time()
        cutoff_time = current_time - 5.0
        recent_frames = sum(1 for ts in self._frame_timestamps if ts >= cutoff_time)
        
        if recent_frames < 2:
            return 0
        
        oldest_in_window = next((ts for ts in self._frame_timestamps if ts >= cutoff_time), None)
        if oldest_in_window is None:
            return 0
        
        time_span = current_time - oldest_in_window
        return recent_frames / time_span if time_span > 0 else 0
    
    def get_stream_url(self, stream_name: str, local_ip: str) -> str:
        """Get the RTSP URL for a stream"""
        return f"rtsp://{local_ip}:{self.port}/{stream_name}"
    
    def _accept_loop(self):
        """Accept incoming RTSP connections"""
        while self._is_running and self._server_socket:
            try:
                client_socket, client_address = self._server_socket.accept()
                client_socket.settimeout(30.0)
                
                # Handle client in separate thread
                client_thread = threading.Thread(
                    target=self._handle_client,
                    args=(client_socket, client_address),
                    daemon=True
                )
                client_thread.start()
            except socket.timeout:
                continue
            except Exception as e:
                if self._is_running:
                    print(f"RTSP accept error: {e}")
                break
    
    def _handle_client(self, client_socket: socket.socket, client_address: tuple):
        """Handle an RTSP client connection"""
        session_id = None
        if self.verbose:
            print(f"[RTSP] New client connection from {client_address[0]}:{client_address[1]}")
        
        try:
            while self._is_running:
                # Receive RTSP request
                data = self._receive_rtsp_request(client_socket)
                if not data:
                    break
                
                request_text = data.decode('utf-8', errors='ignore')
                # Log first line of request
                first_line = request_text.split('\r\n')[0] if request_text else ""
                if self.verbose:
                    print(f"[RTSP] <- {first_line}")
                
                # Parse and handle request
                response, session_id = self._handle_rtsp_request(
                    request_text,
                    client_socket,
                    client_address,
                    session_id
                )
                
                if response:
                    # Log response status
                    resp_first_line = response.split('\r\n')[0] if response else ""
                    if self.verbose:
                        print(f"[RTSP] -> {resp_first_line}")
                    client_socket.sendall(response.encode('utf-8'))
                
                # Check if session was terminated
                if session_id:
                    session = self._sessions.get(session_id)
                    if session and session.state == RTSPState.TEARDOWN:
                        break
                        
        except (ConnectionResetError, BrokenPipeError, ConnectionAbortedError, socket.timeout):
            if self.verbose:
                print(f"[RTSP] Client {client_address[0]} disconnected")
        except OSError as e:
            # Handle Windows-specific errors like WinError 10053
            if hasattr(e, 'winerror') and e.winerror in (10053, 10054):  # Connection aborted/reset
                if self.verbose:
                    print(f"[RTSP] Client {client_address[0]} connection closed")
            else:
                if self.verbose:
                    print(f"[RTSP] Client error: {e}")
        except Exception as e:
            print(f"[RTSP] Client error: {e}")
        finally:
            # Clean up session
            if session_id and session_id in self._sessions:
                with self._lock:
                    session = self._sessions.pop(session_id, None)
                    if session:
                        self._close_session(session)
            
            try:
                client_socket.close()
            except:
                pass
    
    def _receive_rtsp_request(self, client_socket: socket.socket) -> Optional[bytes]:
        """Receive a complete RTSP request"""
        data = b""
        while True:
            try:
                chunk = client_socket.recv(4096)
                if not chunk:
                    return None
                data += chunk
                
                # Check if we have a complete request (ends with \r\n\r\n)
                if b"\r\n\r\n" in data:
                    return data
            except socket.timeout:
                if data:
                    return data
                return None
    
    def _handle_rtsp_request(
        self,
        request: str,
        client_socket: socket.socket,
        client_address: tuple,
        session_id: Optional[str]
    ) -> tuple:
        """Parse and handle an RTSP request"""
        lines = request.strip().split('\r\n')
        if not lines:
            return None, session_id
        
        # Parse request line
        request_line = lines[0].split(' ')
        if len(request_line) < 3:
            return self._error_response(400, "Bad Request", 0), session_id
        
        method = request_line[0]
        uri = request_line[1]
        
        # Parse headers
        headers = {}
        for line in lines[1:]:
            if ':' in line:
                key, value = line.split(':', 1)
                headers[key.strip()] = value.strip()
        
        cseq = int(headers.get('CSeq', 0))
        
        # Parse Session header if present (VLC sends this on subsequent requests)
        if 'Session' in headers and not session_id:
            # Extract session ID (may have ;timeout=X suffix)
            session_id = headers['Session'].split(';')[0].strip()
        
        # Extract stream name from URI
        # Handle various URI formats:
        # - rtsp://host/video_main
        # - rtsp://host/video_main/
        # - rtsp://host/video_main/trackID=0
        uri_path = uri.split('?')[0].rstrip('/')
        path_parts = uri_path.split('/')
        
        # Find stream name - skip trackID parts
        stream_name = None
        for part in reversed(path_parts):
            if part and not part.startswith('trackID') and part not in ('', 'rtsp:'):
                stream_name = part
                break
        
        if not stream_name:
            stream_name = path_parts[-1] if path_parts else ""
        
        # Handle different RTSP methods
        if method == 'OPTIONS':
            return self._handle_options(cseq), session_id
        elif method == 'DESCRIBE':
            return self._handle_describe(uri, stream_name, cseq), session_id
        elif method == 'SETUP':
            return self._handle_setup(
                uri, stream_name, headers, cseq,
                client_socket, client_address, session_id
            )
        elif method == 'PLAY':
            return self._handle_play(session_id, cseq)
        elif method == 'PAUSE':
            return self._handle_pause(session_id, cseq)
        elif method == 'TEARDOWN':
            return self._handle_teardown(session_id, cseq)
        elif method == 'GET_PARAMETER':
            return self._handle_get_parameter(session_id, cseq)
        elif method == 'SET_PARAMETER':
            # VLC may send SET_PARAMETER - just acknowledge it
            return self._handle_set_parameter(session_id, cseq)
        elif method == 'ANNOUNCE':
            # Not supported for receiving streams
            return self._error_response(405, "Method Not Allowed", cseq), session_id
        else:
            return self._error_response(501, "Not Implemented", cseq), session_id
    
    def _handle_options(self, cseq: int) -> str:
        """Handle OPTIONS request"""
        return (
            "RTSP/1.0 200 OK\r\n"
            f"CSeq: {cseq}\r\n"
            "Public: OPTIONS, DESCRIBE, SETUP, PLAY, PAUSE, TEARDOWN, GET_PARAMETER, SET_PARAMETER\r\n"
            "\r\n"
        )
    
    def _handle_set_parameter(self, session_id: Optional[str], cseq: int) -> tuple:
        """Handle SET_PARAMETER request"""
        if session_id and session_id in self._sessions:
            self._sessions[session_id].last_activity = time.time()
        
        if session_id:
            return (
                "RTSP/1.0 200 OK\r\n"
                f"CSeq: {cseq}\r\n"
                f"Session: {session_id}\r\n"
                "\r\n"
            ), session_id
        else:
            return (
                "RTSP/1.0 200 OK\r\n"
                f"CSeq: {cseq}\r\n"
                "\r\n"
            ), session_id
    
    def _handle_describe(self, uri: str, stream_name: str, cseq: int) -> str:
        """Handle DESCRIBE request - return SDP"""
        stream_info = self._streams.get(stream_name)
        if not stream_info:
            return self._error_response(404, "Stream Not Found", cseq)
        
        # Generate SDP
        sdp = self._generate_sdp(stream_info, uri)
        
        # VLC-compatible DESCRIBE response with Content-Base
        return (
            "RTSP/1.0 200 OK\r\n"
            f"CSeq: {cseq}\r\n"
            "Content-Type: application/sdp\r\n"
            f"Content-Base: {uri}/\r\n"
            f"Content-Length: {len(sdp)}\r\n"
            "\r\n"
            f"{sdp}"
        )
    
    def _generate_sdp(self, stream_info: RTSPStreamInfo, uri: str) -> str:
        """Generate SDP for the stream"""
        # Parse base URL for control attribute
        # VLC expects proper control URLs
        base_uri = uri.rstrip('/')
        
        # More complete SDP for H.264 video - VLC compatible
        sdp = (
            "v=0\r\n"
            f"o=- {int(time.time())} 1 IN IP4 127.0.0.1\r\n"
            "s=IPyCam Stream\r\n"
            "i=Live video stream\r\n"
            "t=0 0\r\n"
            "a=tool:IPyCam\r\n"
            "a=type:broadcast\r\n"
            "a=control:*\r\n"
            "a=range:npt=0-\r\n"
            "m=video 0 RTP/AVP 96\r\n"
            "c=IN IP4 0.0.0.0\r\n"
            "b=AS:4000\r\n"
            "a=rtpmap:96 H264/90000\r\n"
            "a=fmtp:96 packetization-mode=1\r\n"
            f"a=framerate:{stream_info.fps}\r\n"
            f"a=control:trackID=0\r\n"
        )
        return sdp
    
    def _handle_setup(
        self,
        uri: str,
        stream_name: str,
        headers: dict,
        cseq: int,
        client_socket: socket.socket,
        client_address: tuple,
        session_id: Optional[str]
    ) -> tuple:
        """Handle SETUP request"""
        transport = headers.get('Transport', '')
        
        # Create or get session
        if not session_id:
            session_id = hashlib.md5(f"{client_address}{time.time()}".encode()).hexdigest()[:16]
        
        session = self._sessions.get(session_id)
        if not session:
            session = RTSPSession(
                session_id=session_id,
                client_socket=client_socket,
                client_address=client_address,
                ssrc=int.from_bytes(os.urandom(4), 'big')
            )
            with self._lock:
                self._sessions[session_id] = session
        
        # Parse transport header
        # Check if client prefers TCP interleaved
        if 'TCP' in transport or 'interleaved' in transport:
            # TCP interleaved mode requested
            # Note: We support this but it's less tested than UDP
            session.interleaved = True
            
            # Parse interleaved channels
            interleaved_match = re.search(r'interleaved=(\d+)-(\d+)', transport)
            if interleaved_match:
                session.interleaved_channel = int(interleaved_match.group(1))
            else:
                session.interleaved_channel = 0
            
            if self.verbose:
                print(f"[RTSP] Client requested TCP interleaved mode (channel {session.interleaved_channel})")
            transport_response = f"RTP/AVP/TCP;unicast;interleaved={session.interleaved_channel}-{session.interleaved_channel + 1}"
        else:
            # UDP mode (preferred)
            session.interleaved = False
            
            # Parse client ports
            port_match = re.search(r'client_port=(\d+)-(\d+)', transport)
            if port_match:
                session.rtp_port = int(port_match.group(1))
                session.rtcp_port = int(port_match.group(2))
            else:
                session.rtp_port = 6970
                session.rtcp_port = 6971
            
            # Create UDP socket for RTP
            try:
                session.rtp_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                session.rtp_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                session.rtp_socket.bind(('', 0))  # Bind to any available port
                server_rtp_port = session.rtp_socket.getsockname()[1]
                server_rtcp_port = server_rtp_port + 1
            except Exception as e:
                print(f"Failed to create RTP socket: {e}")
                return self._error_response(500, "Internal Server Error", cseq), session_id
            
            transport_response = f"RTP/AVP;unicast;client_port={session.rtp_port}-{session.rtcp_port};server_port={server_rtp_port}-{server_rtcp_port}"
        
        session.state = RTSPState.READY
        session.transport = transport_response
        
        # Store stream name in session for later use
        session.stream_name = stream_name
        
        return (
            "RTSP/1.0 200 OK\r\n"
            f"CSeq: {cseq}\r\n"
            f"Session: {session_id};timeout=60\r\n"
            f"Transport: {transport_response}\r\n"
            "\r\n"
        ), session_id
    
    def _handle_play(self, session_id: Optional[str], cseq: int) -> tuple:
        """Handle PLAY request"""
        if not session_id or session_id not in self._sessions:
            return self._error_response(454, "Session Not Found", cseq), session_id
        
        session = self._sessions[session_id]
        session.state = RTSPState.PLAYING
        session.last_activity = time.time()
        
        # Start RTP streaming for this session
        stream_name = getattr(session, 'stream_name', None)
        if stream_name:
            self._start_rtp_streaming(session, stream_name)
        
        return (
            "RTSP/1.0 200 OK\r\n"
            f"CSeq: {cseq}\r\n"
            f"Session: {session_id}\r\n"
            "Range: npt=0.000-\r\n"
            "\r\n"
        ), session_id
    
    def _handle_pause(self, session_id: Optional[str], cseq: int) -> tuple:
        """Handle PAUSE request"""
        if not session_id or session_id not in self._sessions:
            return self._error_response(454, "Session Not Found", cseq), session_id
        
        session = self._sessions[session_id]
        session.state = RTSPState.READY
        
        return (
            "RTSP/1.0 200 OK\r\n"
            f"CSeq: {cseq}\r\n"
            f"Session: {session_id}\r\n"
            "\r\n"
        ), session_id
    
    def _handle_teardown(self, session_id: Optional[str], cseq: int) -> tuple:
        """Handle TEARDOWN request"""
        if not session_id or session_id not in self._sessions:
            return self._error_response(454, "Session Not Found", cseq), session_id
        
        session = self._sessions[session_id]
        session.state = RTSPState.TEARDOWN
        
        return (
            "RTSP/1.0 200 OK\r\n"
            f"CSeq: {cseq}\r\n"
            f"Session: {session_id}\r\n"
            "\r\n"
        ), session_id
    
    def _handle_get_parameter(self, session_id: Optional[str], cseq: int) -> tuple:
        """Handle GET_PARAMETER request (keepalive)"""
        if session_id and session_id in self._sessions:
            self._sessions[session_id].last_activity = time.time()
        
        if session_id:
            return (
                "RTSP/1.0 200 OK\r\n"
                f"CSeq: {cseq}\r\n"
                f"Session: {session_id}\r\n"
                "\r\n"
            ), session_id
        else:
            return (
                "RTSP/1.0 200 OK\r\n"
                f"CSeq: {cseq}\r\n"
                "\r\n"
            ), session_id
    
    def _error_response(self, code: int, message: str, cseq: int) -> str:
        """Generate an RTSP error response"""
        return (
            f"RTSP/1.0 {code} {message}\r\n"
            f"CSeq: {cseq}\r\n"
            "\r\n"
        )
    
    def _start_rtp_streaming(self, session: RTSPSession, stream_name: str):
        """Start RTP streaming for a session using FFmpeg"""
        stream_info = self._streams.get(stream_name)
        if not stream_info and self.verbose:
            print(f"[RTSP] Stream '{stream_name}' not found")
            return
        
        if self.verbose:
            print(f"[RTSP] Starting RTP stream for session {session.session_id[:8]}...")
            print(f"[RTSP]   Mode: {'TCP interleaved' if session.interleaved else 'UDP'}")
        if not session.interleaved:
            if self.verbose:
                print(f"[RTSP]   Client: {session.client_address[0]}:{session.rtp_port}")
        
        # Start encoder thread for this session
        thread = threading.Thread(
            target=self._rtp_encoder_loop,
            args=(session, stream_name, stream_info),
            daemon=True
        )
        thread.start()
        
        thread_key = f"{session.session_id}_{stream_name}"
        self._rtp_threads[thread_key] = thread
    
    def _rtp_encoder_loop(self, session: RTSPSession, stream_name: str, stream_info: RTSPStreamInfo):
        """Encoder loop that sends RTP packets"""
        local_rtp_socket = None
        
        try:
            if session.interleaved:
                # TCP interleaved mode - create local UDP socket to receive from FFmpeg
                local_rtp_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                local_rtp_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                local_rtp_socket.bind(('127.0.0.1', 0))
                local_rtp_port = local_rtp_socket.getsockname()[1]
                local_rtp_socket.settimeout(0.1)
                
                ffmpeg_cmd = self._build_ffmpeg_rtp_cmd_tcp_local(stream_info, local_rtp_port)
                if self.verbose:
                    print(f"[RTSP] TCP interleaved: FFmpeg -> localhost:{local_rtp_port} -> client")
            else:
                # UDP mode - FFmpeg sends directly to client
                ffmpeg_cmd = self._build_ffmpeg_rtp_cmd_udp(stream_info, session)
            
            if not ffmpeg_cmd:
                return

            if self.verbose:
                print(f"[RTSP] Starting FFmpeg encoder...")
            
            # Start FFmpeg process
            process = subprocess.Popen(
                ffmpeg_cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, 'CREATE_NO_WINDOW') else 0
            )
            
            encoder_key = f"{session.session_id}_{stream_name}"
            self._encoder_processes[encoder_key] = process
            
            # If TCP interleaved, start a thread to forward RTP packets
            if session.interleaved and local_rtp_socket:
                forward_thread = threading.Thread(
                    target=self._tcp_rtp_forwarder,
                    args=(local_rtp_socket, session),
                    daemon=True
                )
                forward_thread.start()
            
            # Feed frames to encoder
            frame_interval = 1.0 / stream_info.fps
            last_frame_time = time.time()
            frames_written = 0
            
            while self._is_running and session.state == RTSPState.PLAYING:
                # Get latest frame
                lock = self._frame_locks.get(stream_name)
                frame = None
                if lock:
                    with lock:
                        if self._frame_buffers.get(stream_name) is not None:
                            frame = self._frame_buffers[stream_name]
                
                if frame is not None and process.stdin:
                    # Resize if needed
                    if frame.shape[1] != stream_info.width or frame.shape[0] != stream_info.height:
                        frame = cv2.resize(frame, (stream_info.width, stream_info.height))
                    
                    # Write to FFmpeg
                    try:
                        process.stdin.write(frame.tobytes())
                        frames_written += 1
                        if frames_written % 15 == 0:
                            process.stdin.flush()
                    except (BrokenPipeError, OSError) as e:
                        if self.verbose:
                            print(f"[RTSP] FFmpeg pipe error: {e}")
                        break
                
                # Pace frames
                elapsed = time.time() - last_frame_time
                sleep_time = frame_interval - elapsed
                if sleep_time > 0:
                    time.sleep(sleep_time)
                last_frame_time = time.time()
                
                # Check if process is still alive
                if process.poll() is not None:
                    stderr = process.stderr.read().decode('utf-8', errors='ignore') if process.stderr else ""
                    if stderr:
                        if self.verbose:
                            print(f"[RTSP] FFmpeg exited: {stderr[:200]}")
                    break
            
            if self.verbose:
                print(f"[RTSP] Encoder loop ended, wrote {frames_written} frames")
            
        except Exception as e:
            print(f"[RTSP] RTP encoder error: {e}")
        finally:
            # Cleanup
            if local_rtp_socket:
                try:
                    local_rtp_socket.close()
                except:
                    pass
            
            encoder_key = f"{session.session_id}_{stream_name}"
            proc = self._encoder_processes.pop(encoder_key, None)
            if proc:
                try:
                    if proc.stdin:
                        proc.stdin.close()
                    proc.terminate()
                    proc.wait(timeout=2)
                except:
                    try:
                        proc.kill()
                    except:
                        pass
    
    def _build_ffmpeg_rtp_cmd_udp(self, stream_info: RTSPStreamInfo, session: RTSPSession) -> list:
        """Build FFmpeg command for UDP RTP output"""
        client_ip = session.client_address[0]
        
        # Use payload type 96 to match SDP
        # VLC and other clients expect proper RTP packetization
        return [
            "ffmpeg", "-y",
            "-f", "rawvideo",
            "-vcodec", "rawvideo",
            "-s", f"{stream_info.width}x{stream_info.height}",
            "-pix_fmt", "bgr24",
            "-r", str(stream_info.fps),
            "-i", "-",
            "-c:v", "libx264",
            "-preset", "ultrafast",
            "-tune", "zerolatency",
            "-profile:v", "baseline",
            "-level", "3.1",
            "-pix_fmt", "yuv420p",
            "-g", str(stream_info.fps),  # Keyframe every second
            "-b:v", stream_info.bitrate,
            "-maxrate", stream_info.bitrate,
            "-bufsize", "500k",
            "-an",  # No audio
            "-f", "rtp",
            "-payload_type", "96",
            f"rtp://{client_ip}:{session.rtp_port}?localport={session.rtp_socket.getsockname()[1] if session.rtp_socket else 0}"
        ]
    
    def _build_ffmpeg_rtp_cmd_tcp(self, stream_info: RTSPStreamInfo, session: RTSPSession) -> list:
        """Build FFmpeg command for TCP output (piped RTP) - DEPRECATED, use _build_ffmpeg_rtp_cmd_tcp_local"""
        return self._build_ffmpeg_rtp_cmd_tcp_local(stream_info, 0)
    
    def _build_ffmpeg_rtp_cmd_tcp_local(self, stream_info: RTSPStreamInfo, local_port: int) -> list:
        """Build FFmpeg command that outputs RTP to a local UDP port for TCP forwarding"""
        return [
            "ffmpeg", "-y",
            "-f", "rawvideo",
            "-vcodec", "rawvideo",
            "-s", f"{stream_info.width}x{stream_info.height}",
            "-pix_fmt", "bgr24",
            "-r", str(stream_info.fps),
            "-i", "-",
            "-c:v", "libx264",
            "-preset", "ultrafast",
            "-tune", "zerolatency",
            "-profile:v", "baseline",
            "-level", "3.1",
            "-pix_fmt", "yuv420p",
            "-g", str(stream_info.fps),
            "-b:v", stream_info.bitrate,
            "-maxrate", stream_info.bitrate,
            "-bufsize", "500k",
            "-an",
            "-f", "rtp",
            "-payload_type", "96",
            f"rtp://127.0.0.1:{local_port}"
        ]
    
    def _tcp_rtp_forwarder(self, local_socket: socket.socket, session: RTSPSession):
        """Forward RTP packets from local UDP socket to client via TCP interleaved"""
        packets_sent = 0
        try:
            if self.verbose:
                print(f"[RTSP] TCP RTP forwarder started for session {session.session_id[:8]}")
            while self._is_running and session.state == RTSPState.PLAYING:
                try:
                    data, addr = local_socket.recvfrom(2048)
                    if not data:
                        continue
                    
                    # Send as interleaved RTP
                    # Format: $ + channel (1 byte) + length (2 bytes big-endian) + data
                    header = bytes([0x24, session.interleaved_channel]) + struct.pack('>H', len(data))
                    
                    session.client_socket.sendall(header + data)
                    packets_sent += 1
                    
                except socket.timeout:
                    continue
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError) as e:
                    if self.verbose:
                        print(f"[RTSP] TCP forwarder connection error: {e}")
                    break
                    
        except Exception as e:
            print(f"[RTSP] TCP RTP forwarder error: {e}")
        finally:
            print(f"[RTSP] TCP RTP forwarder ended, sent {packets_sent} packets")
    
    def _tcp_rtp_reader(self, process: subprocess.Popen, session: RTSPSession):
        """Read RTP packets from FFmpeg stdout and send via TCP interleaved - DEPRECATED"""
        try:
            while self._is_running and session.state == RTSPState.PLAYING and process.stdout:
                # Read RTP packet (simplified - real implementation would parse RTP)
                data = process.stdout.read(1400)
                if not data:
                    break
                
                # Send as interleaved RTP
                # Format: $ + channel (1 byte) + length (2 bytes big-endian) + data
                header = bytes([0x24, session.interleaved_channel]) + struct.pack('>H', len(data))
                
                try:
                    session.client_socket.sendall(header + data)
                except (BrokenPipeError, ConnectionResetError, OSError):
                    break
                    
        except Exception as e:
            print(f"TCP RTP reader error: {e}")
    
    def _close_session(self, session: RTSPSession):
        """Close and clean up a session"""
        session.state = RTSPState.TEARDOWN
        
        if session.rtp_socket:
            try:
                session.rtp_socket.close()
            except:
                pass
        
        # Stop any encoder processes for this session
        for key in list(self._encoder_processes.keys()):
            if key.startswith(session.session_id):
                proc = self._encoder_processes.pop(key, None)
                if proc:
                    try:
                        proc.terminate()
                        proc.wait(timeout=1)
                    except:
                        try:
                            proc.kill()
                        except:
                            pass


def is_native_rtsp_available() -> bool:
    """
    Check if native RTSP server is available.
    Requires FFmpeg for video encoding.
    
    Returns:
        True if FFmpeg is available
    """
    try:
        result = subprocess.run(
            ["ffmpeg", "-version"],
            capture_output=True,
            timeout=5.0,
            creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, 'CREATE_NO_WINDOW') else 0
        )
        return result.returncode == 0
    except Exception:
        return False
