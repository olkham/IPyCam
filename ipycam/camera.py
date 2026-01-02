#!/usr/bin/env python3
"""
Pure Python IP Camera

A virtual IP camera that:
- Is discoverable via ONVIF (WS-Discovery)
- Provides RTSP streams via go2rtc
- Has a web interface for configuration and live preview
- Accepts frames from any source
- Supports digital PTZ (ePTZ) via ONVIF

Usage:
    from ipycam import IPCamera, CameraConfig
    
    camera = IPCamera()
    camera.start()
    
    while running:
        frame = get_frame()
        camera.stream(frame)
    
    camera.stop()
"""

import os
import time
import threading
import socketserver
from datetime import datetime
import numpy as np
import cv2
from typing import Optional

from .config import CameraConfig
from .streamer import VideoStreamer, StreamStats
from .onvif import ONVIFService
from .http import IPCameraHTTPHandler
from .discovery import WSDiscoveryServer
from .ptz import PTZController


class IPCamera:
    """
    Pure Python IP Camera
    
    Combines VideoStreamer, ONVIF server, PTZ controller, and Web UI 
    into a complete virtual IP camera solution.
    """
    
    def __init__(self, config: Optional[CameraConfig] = None):
        self.config = config or CameraConfig()
        self.streamer: Optional[VideoStreamer] = None
        
        # Initialize PTZ controller
        self.ptz = PTZController(
            output_width=self.config.main_width,
            output_height=self.config.main_height,
            max_zoom=4.0
        )
        
        # Initialize ONVIF service with PTZ
        self.onvif = ONVIFService(self.config, self.ptz)
        
        self._http_server: Optional[socketserver.TCPServer] = None
        self._discovery: Optional[WSDiscoveryServer] = None
        self._last_frame: Optional[np.ndarray] = None
        self._running = False
        self._restarting = False  # Flag to prevent loop exit during restart
        
        # Frame pacing
        self._frame_count = 0
        self._stream_start_time: Optional[float] = None
        self._last_fps = 0
        
    def start(self) -> bool:
        """Start the IP camera (ONVIF, Web UI, and streaming)"""
        print(f"Starting IP Camera: {self.config.name}")
        print(f"  Local IP: {self.config.local_ip}")
        
        # Start WS-Discovery
        self._discovery = WSDiscoveryServer(self.onvif)
        self._discovery.start()
        print(f"  WS-Discovery: listening on port 3702")
        
        # Start HTTP server (ONVIF + Web UI)
        IPCameraHTTPHandler.camera = self
        self._http_server = socketserver.ThreadingTCPServer(
            ('', self.config.onvif_port), 
            IPCameraHTTPHandler
        )
        self._http_thread = threading.Thread(target=self._http_server.serve_forever, daemon=True)
        self._http_thread.start()
        print(f"  ONVIF Service: {self.config.onvif_url}")
        print(f"  Web UI: http://{self.config.local_ip}:{self.config.onvif_port}/")
        
        # Start video streamer
        stream_config = self.config.to_stream_config()
        self.streamer = VideoStreamer(stream_config)
        
        if not self.streamer.start(self.config.main_stream_rtmp, self.config.sub_stream_rtmp):
            print("  ✗ Failed to start video streamer")
            print("    Check that go2rtc is running and configured correctly")
            print("    run using: go2rtc --config ipycam\\go2rtc.yaml")
            return False
        
        print(f"  Main Stream: {self.config.main_stream_rtsp}")
        print(f"  Sub Stream: {self.config.sub_stream_rtsp}")
        print(f"  WebRTC: {self.config.webrtc_url}")
        
        self._running = True
        return True
    
    def stop(self):
        """Stop all camera services"""
        self._running = False
        
        if self.ptz:
            self.ptz.stop()
        
        if self.streamer:
            self.streamer.stop()
        
        if self._discovery:
            self._discovery.stop()
        
        if self._http_server:
            self._http_server.shutdown()
        
        print("IP Camera stopped")
    
    def stream(self, frame: np.ndarray) -> bool:
        """Send a frame to the stream (applies PTZ transform, timestamp, and frame pacing)"""
        # Apply PTZ transform first
        if self.ptz:
            frame = self.ptz.apply_ptz(frame)
        
        # Apply timestamp overlay last (always visible, not affected by PTZ)
        if self.config.show_timestamp:
            frame = self._draw_timestamp(frame)
        
        self._last_frame = frame  # Keep for snapshots (already PTZ-adjusted + timestamp)
        
        # Send frame to streamer
        result = False
        if self.streamer:
            result = self.streamer.stream(frame)
        
        # Frame pacing - maintain target FPS
        self._pace_frame()
        
        return result
    
    def _pace_frame(self):
        """Handle frame pacing to maintain target FPS"""
        # Initialize or reset timing if FPS changed
        if self._stream_start_time is None or self._last_fps != self.config.main_fps:
            self._stream_start_time = time.time()
            self._frame_count = 0
            self._last_fps = self.config.main_fps
        
        self._frame_count += 1
        
        # Calculate expected time for this frame and sleep if ahead
        target_frame_time = 1.0 / self.config.main_fps
        expected_time = self._stream_start_time + (self._frame_count * target_frame_time)
        sleep_time = expected_time - time.time()
        
        if sleep_time > 0:
            time.sleep(sleep_time)
    
    def _draw_timestamp(self, frame: np.ndarray) -> np.ndarray:
        """Draw timestamp overlay on frame"""
        timestamp = datetime.now().strftime(self.config.timestamp_format)
        
        # Font settings
        font = cv2.FONT_HERSHEY_SIMPLEX
        font_scale = 0.6
        thickness = 2
        color = (255, 255, 255)  # White
        shadow_color = (0, 0, 0)  # Black shadow
        
        # Get text size
        (text_w, text_h), baseline = cv2.getTextSize(timestamp, font, font_scale, thickness)
        
        # Calculate position based on setting
        h, w = frame.shape[:2]
        padding = 10
        
        if self.config.timestamp_position == "top-left":
            x, y = padding, text_h + padding
        elif self.config.timestamp_position == "top-right":
            x, y = w - text_w - padding, text_h + padding
        elif self.config.timestamp_position == "bottom-right":
            x, y = w - text_w - padding, h - padding
        else:  # bottom-left (default)
            x, y = padding, h - padding
        
        # Draw shadow for better visibility
        cv2.putText(frame, timestamp, (x + 1, y + 1), font, font_scale, shadow_color, thickness + 1)
        # Draw text
        cv2.putText(frame, timestamp, (x, y), font, font_scale, color, thickness)
        
        return frame
    
    def restart_stream(self) -> bool:
        """Restart the video streamer with current config"""
        print("Restarting video stream...")
        self._restarting = True
        
        try:
            # Stop current streamer
            if self.streamer:
                self.streamer.stop()
                self.streamer = None
            
            # Update PTZ controller dimensions
            if self.ptz:
                self.ptz.output_width = self.config.main_width
                self.ptz.output_height = self.config.main_height
            
            # Create new streamer with updated config
            stream_config = self.config.to_stream_config()
            self.streamer = VideoStreamer(stream_config)
            
            if not self.streamer.start(self.config.main_stream_rtmp, self.config.sub_stream_rtmp):
                print("  ✗ Failed to restart video streamer")
                return False
            
            print(f"  ✓ Stream restarted: {self.config.main_width}x{self.config.main_height}@{self.config.main_fps}fps")
            return True
        finally:
            self._restarting = False
    
    @property
    def is_running(self) -> bool:
        # During restart, streamer is temporarily None - don't exit the loop
        if self._restarting:
            return self._running
        return self._running and self.streamer is not None and self.streamer.is_running
    
    @property
    def stats(self) -> Optional[StreamStats]:
        return self.streamer.stats if self.streamer else None
    
    def get_web_ui_html(self) -> str:
        """Load and render the web UI HTML from template"""
        static_dir = os.path.join(os.path.dirname(__file__), 'static')
        template_path = os.path.join(static_dir, 'index.html')
        
        try:
            with open(template_path, 'r', encoding='utf-8') as f:
                html = f.read()
        except FileNotFoundError:
            return "<html><body><h1>Error: Template not found</h1><p>static/index.html is missing</p></body></html>"
        
        # Replace template variables
        preview_url = f"http://{self.config.local_ip}:{self.config.go2rtc_api_port}/stream.html?src={self.config.main_stream_name}"
        
        replacements = {
            '{{camera_name}}': self.config.name,
            '{{preview_url}}': preview_url,
            '{{main_rtsp}}': self.config.main_stream_rtsp,
            '{{sub_rtsp}}': self.config.sub_stream_rtsp,
            '{{onvif_url}}': self.config.onvif_url,
            '{{webrtc_url}}': self.config.webrtc_url,
            '{{main_stream_name}}': self.config.main_stream_name,
            '{{sub_stream_name}}': self.config.sub_stream_name,
        }
        
        for key, value in replacements.items():
            html = html.replace(key, value)
        
        return html
