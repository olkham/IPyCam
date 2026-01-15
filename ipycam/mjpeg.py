#!/usr/bin/env python3
"""
Native Python MJPEG Streamer

Provides a fallback MJPEG stream when go2rtc is not available.
This allows the camera to work without external dependencies, 
though with reduced functionality (no RTSP, WebRTC, etc.)
"""

import io
import time
import threading
import numpy as np
import cv2
from typing import Optional, List
from dataclasses import dataclass
from collections import deque


@dataclass
class MJPEGClient:
    """Represents a connected MJPEG client"""
    wfile: io.BufferedWriter
    connected: bool = True
    frames_sent: int = 0
    

class MJPEGStreamer:
    """
    Native Python MJPEG streamer.
    
    Maintains a list of connected clients and broadcasts frames to all of them.
    This is used as a fallback when go2rtc is not available.
    """
    
    BOUNDARY = b"--frame"
    
    def __init__(self, quality: int = 80):
        """
        Initialize the MJPEG streamer.
        
        Args:
            quality: JPEG encoding quality (1-100)
        """
        self.quality = quality
        self._clients: List[MJPEGClient] = []
        self._lock = threading.Lock()
        self._last_frame: Optional[bytes] = None
        self._frame_count = 0
        self._is_running = False
        
        # Stats tracking
        self._start_time: Optional[float] = None
        self._frame_timestamps: deque = deque(maxlen=150)
        self._window_seconds: float = 5.0  # Calculate FPS over last 5 seconds
        
    def start(self) -> bool:
        """Start the MJPEG streamer"""
        self._is_running = True
        self._start_time = time.time()
        self._frame_count = 0
        self._frame_timestamps.clear()
        return True
    
    def stop(self):
        """Stop the MJPEG streamer and disconnect all clients"""
        self._is_running = False
        with self._lock:
            for client in self._clients:
                client.connected = False
            self._clients.clear()
    
    @property
    def is_running(self) -> bool:
        return self._is_running
    
    @property
    def client_count(self) -> int:
        """Return number of connected clients"""
        with self._lock:
            return len(self._clients)
    
    @property
    def frames_sent(self) -> int:
        """Return total frames sent"""
        return self._frame_count
    
    @property
    def elapsed_time(self) -> float:
        """Return elapsed time since start"""
        if self._start_time is None:
            return 0
        return time.time() - self._start_time
    
    @property
    def actual_fps(self) -> float:
        """Calculate FPS over a sliding window of recent frames"""
        if len(self._frame_timestamps) < 2:
            return 0
        
        current_time = time.time()
        cutoff_time = current_time - self._window_seconds
        
        # Count frames in window
        recent_frames = sum(1 for ts in self._frame_timestamps if ts >= cutoff_time)
        
        if recent_frames < 2:
            return 0
        
        # Find oldest frame in window
        oldest_in_window = next((ts for ts in self._frame_timestamps if ts >= cutoff_time), None)
        if oldest_in_window is None:
            return 0
        
        time_span = current_time - oldest_in_window
        if time_span > 0:
            return recent_frames / time_span
        return 0
    
    def add_client(self, wfile: io.BufferedWriter) -> MJPEGClient:
        """
        Add a new client to receive MJPEG frames.
        
        Args:
            wfile: The writable file object for the client connection
            
        Returns:
            MJPEGClient object
        """
        client = MJPEGClient(wfile=wfile)
        with self._lock:
            self._clients.append(client)
        return client
    
    def remove_client(self, client: MJPEGClient):
        """Remove a client from the broadcast list"""
        client.connected = False
        with self._lock:
            if client in self._clients:
                self._clients.remove(client)
    
    def stream_frame(self, frame: np.ndarray) -> bool:
        """
        Encode and broadcast a frame to all connected clients.
        
        Args:
            frame: NumPy array in BGR format (OpenCV default)
            
        Returns:
            True if at least one client received the frame
        """
        if not self._is_running:
            return False
        
        # Encode frame to JPEG
        encode_params = [int(cv2.IMWRITE_JPEG_QUALITY), self.quality]
        success, jpeg = cv2.imencode('.jpg', frame, encode_params)
        
        if not success:
            return False
        
        jpeg_bytes = jpeg.tobytes()
        self._last_frame = jpeg_bytes
        self._frame_count += 1
        self._frame_timestamps.append(time.time())  # Record timestamp for FPS calculation
        
        # Build MJPEG frame
        frame_data = (
            self.BOUNDARY + b"\r\n"
            b"Content-Type: image/jpeg\r\n"
            b"Content-Length: " + str(len(jpeg_bytes)).encode() + b"\r\n"
            b"\r\n" + jpeg_bytes + b"\r\n"
        )
        
        # Broadcast to all clients
        clients_sent = 0
        with self._lock:
            clients_to_remove = []
            
            for client in self._clients:
                if not client.connected:
                    clients_to_remove.append(client)
                    continue
                    
                try:
                    client.wfile.write(frame_data)
                    client.wfile.flush()
                    client.frames_sent += 1
                    clients_sent += 1
                except (BrokenPipeError, ConnectionResetError, OSError):
                    client.connected = False
                    clients_to_remove.append(client)
            
            # Clean up disconnected clients
            for client in clients_to_remove:
                if client in self._clients:
                    self._clients.remove(client)
        
        return clients_sent > 0
    
    def get_headers(self) -> List[tuple]:
        """
        Get HTTP headers for MJPEG stream response.
        
        Returns:
            List of (header_name, header_value) tuples
        """
        return [
            ('Content-Type', f'multipart/x-mixed-replace; boundary={self.BOUNDARY.decode()[2:]}'),
            ('Cache-Control', 'no-cache, no-store, must-revalidate'),
            ('Pragma', 'no-cache'),
            ('Expires', '0'),
            ('Connection', 'close'),
        ]


def check_go2rtc_running(host: str = "127.0.0.1", port: int = 1984, timeout: float = 1.0) -> bool:
    """
    Check if go2rtc is running by attempting to connect to its API port.
    
    Args:
        host: go2rtc host address
        port: go2rtc API port (default 1984)
        timeout: Connection timeout in seconds
        
    Returns:
        True if go2rtc is running and accessible
    """
    import socket
    import urllib.request
    import urllib.error
    
    # First try a simple socket connection
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        result = sock.connect_ex((host, port))
        sock.close()
        
        if result != 0:
            return False
    except (socket.error, socket.timeout):
        return False
    
    # Then try to hit the API endpoint to confirm it's actually go2rtc
    try:
        url = f"http://{host}:{port}/api"
        req = urllib.request.Request(url, method='GET')
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return response.status == 200
    except (urllib.error.URLError, urllib.error.HTTPError, socket.timeout):
        # Port is open but not responding like go2rtc
        # Could be another service, but we'll accept it if the port is open
        return True
    except Exception:
        return False


def check_rtsp_port_available(host: str = "127.0.0.1", port: int = 8554, timeout: float = 1.0) -> bool:
    """
    Check if the RTSP port is available (go2rtc is listening).
    
    Args:
        host: go2rtc host address
        port: RTSP port (default 8554)
        timeout: Connection timeout in seconds
        
    Returns:
        True if RTSP port is accepting connections
    """
    import socket
    
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        result = sock.connect_ex((host, port))
        sock.close()
        return result == 0
    except (socket.error, socket.timeout):
        return False
