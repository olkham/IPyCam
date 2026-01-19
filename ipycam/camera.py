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

from .__version__ import __version__
from .config import CameraConfig
from .streamer import VideoStreamer, StreamStats
from .onvif import ONVIFService
from .http import IPCameraHTTPHandler
from .discovery import WSDiscoveryServer
from .ptz import PTZController
from .mjpeg import MJPEGStreamer, check_go2rtc_running, check_rtsp_port_available
from .webrtc import NativeWebRTCStreamer, is_webrtc_available
from .rtsp import NativeRTSPServer, is_native_rtsp_available


class ReusableThreadingTCPServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


class IPCamera:
    """
    Pure Python IP Camera
    
    Combines VideoStreamer, ONVIF server, PTZ controller, and Web UI 
    into a complete virtual IP camera solution.
    """
    
    def __init__(self, config: Optional[CameraConfig] = None):
        self.config = config or CameraConfig()
        self.streamer: Optional[VideoStreamer] = None
        self.mjpeg_streamer: Optional[MJPEGStreamer] = None
        self.webrtc_streamer: Optional[NativeWebRTCStreamer] = None
        self.rtsp_server: Optional[NativeRTSPServer] = None  # Native RTSP fallback
        self._use_mjpeg_fallback = False
        self._streaming_mode = 'go2rtc'  # 'go2rtc', 'native_webrtc', or 'mjpeg'
        
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
        self._http_server = ReusableThreadingTCPServer(
            ('', self.config.onvif_port), 
            IPCameraHTTPHandler
        )
        self._http_thread = threading.Thread(target=self._http_server.serve_forever, daemon=True)
        self._http_thread.start()
        print(f"  ONVIF Service: {self.config.onvif_url}")
        print(f"  Web UI: http://{self.config.local_ip}:{self.config.onvif_port}/")
        
        # Always start the MJPEG streamer (available as alternative view even with go2rtc)
        self.mjpeg_streamer = MJPEGStreamer(quality=80)
        self.mjpeg_streamer.start()
        mjpeg_url = f"http://{self.config.local_ip}:{self.config.onvif_port}/{self.config.mjpeg_url}"
        
        # Check if go2rtc is running
        go2rtc_available = check_go2rtc_running(port=self.config.go2rtc_api_port)
        rtsp_available = check_rtsp_port_available(port=self.config.rtsp_port)
        
        if go2rtc_available and rtsp_available:
            # Use go2rtc for streaming (best option: RTSP + WebRTC + MJPEG)
            self._use_mjpeg_fallback = False
            self._streaming_mode = 'go2rtc'
            stream_config = self.config.to_stream_config()
            self.streamer = VideoStreamer(stream_config)
            
            if not self.streamer.start(self.config.main_stream_push_url, self.config.sub_stream_push_url):
                print("  ⚠ Failed to start video streamer, trying native WebRTC fallback...")
                self._try_native_webrtc_fallback(mjpeg_url)
            else:
                print(f"  Main Stream: {self.config.main_stream_rtsp}")
                print(f"  Sub Stream: {self.config.sub_stream_rtsp}")
                print(f"  WebRTC: {self.config.webrtc_url}")
                print(f"  MJPEG Stream: {mjpeg_url}")
        else:
            # go2rtc not available - try native WebRTC as fallback
            self._try_native_webrtc_fallback(mjpeg_url)
        
        self._running = True
        return True
    
    def _try_native_webrtc_fallback(self, mjpeg_url: str):
        """Try to start native RTSP + WebRTC, fall back to MJPEG-only if not available."""
        # Try to start native RTSP server (requires FFmpeg)
        rtsp_started = False
        if is_native_rtsp_available():
            try:
                self.rtsp_server = NativeRTSPServer(port=self.config.rtsp_port)
                
                # Add main stream
                self.rtsp_server.add_stream(
                    name=self.config.main_stream_name,
                    width=self.config.main_width,
                    height=self.config.main_height,
                    fps=self.config.main_fps,
                    bitrate=self.config.main_bitrate
                )
                
                # Add sub stream
                self.rtsp_server.add_stream(
                    name=self.config.sub_stream_name,
                    width=self.config.sub_width,
                    height=self.config.sub_height,
                    fps=self.config.sub_fps,
                    bitrate=self.config.sub_bitrate
                )
                
                if self.rtsp_server.start():
                    rtsp_started = True
                    print("  ✓ Native RTSP server started")
                    print(f"    Main Stream: {self.config.main_stream_rtsp}")
                    print(f"    Sub Stream: {self.config.sub_stream_rtsp}")
                else:
                    print("  ⚠ Failed to start native RTSP server")
                    self.rtsp_server = None
            except Exception as e:
                print(f"  ⚠ Failed to start native RTSP server: {e}")
                self.rtsp_server = None
        else:
            print("  ⚠ Native RTSP unavailable (FFmpeg not found)")
        
        # Try to start native WebRTC
        webrtc_started = False
        if is_webrtc_available():
            try:
                self.webrtc_streamer = NativeWebRTCStreamer(
                    fps=self.config.main_fps,
                    width=self.config.main_width,
                    height=self.config.main_height
                )
                if self.webrtc_streamer.start():
                    webrtc_started = True
                    webrtc_native_url = f"http://{self.config.local_ip}:{self.config.onvif_port}/api/webrtc/offer"
                    print(f"  ✓ Native WebRTC started: {webrtc_native_url}")
                else:
                    self.webrtc_streamer = None
            except Exception as e:
                print(f"  ⚠ Failed to start native WebRTC: {e}")
                self.webrtc_streamer = None
        
        # Determine streaming mode based on what's available
        if rtsp_started and webrtc_started:
            self._use_mjpeg_fallback = False
            self._streaming_mode = 'native_rtsp_webrtc'
            print("  ⚠ go2rtc not detected - using native RTSP + WebRTC fallback")
        elif rtsp_started:
            self._use_mjpeg_fallback = False
            self._streaming_mode = 'native_rtsp'
            print("  ⚠ go2rtc not detected - using native RTSP fallback")
            print("  Note: Install aiortc for native WebRTC: pip install aiortc")
        elif webrtc_started:
            self._use_mjpeg_fallback = False
            self._streaming_mode = 'native_webrtc'
            print("  ⚠ go2rtc not detected - using native WebRTC fallback")
            print("  Note: RTSP unavailable (FFmpeg not found)")
        else:
            # Final fallback: MJPEG only
            self._use_mjpeg_fallback = True
            self._streaming_mode = 'mjpeg'
            print("  ⚠ No RTSP/WebRTC available - using MJPEG-only fallback")
            print("  Note: Install aiortc for native WebRTC: pip install aiortc")
            print("        Or start go2rtc for full functionality: go2rtc --config ipycam/go2rtc.yaml")
        
        print(f"  MJPEG Stream: {mjpeg_url}")
    
    def stop(self):
        """Stop all camera services"""
        self._running = False
        
        if self.ptz:
            self.ptz.stop()
        
        if self.streamer:
            self.streamer.stop()
        
        if self.webrtc_streamer:
            self.webrtc_streamer.stop()
        
        if self.rtsp_server:
            self.rtsp_server.stop()
        
        if self.mjpeg_streamer:
            self.mjpeg_streamer.stop()
        
        if self._discovery:
            self._discovery.stop()
        
        if self._http_server:
            self._http_server.shutdown()
            self._http_server.server_close()
        
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
        
        # Send to MJPEG streamer only when clients are connected
        if self.mjpeg_streamer and self.mjpeg_streamer.client_count > 0:
            self.mjpeg_streamer.stream_frame(frame)
        
        # Send to native WebRTC streamer only if there are active connections
        if self.webrtc_streamer and self.webrtc_streamer.connection_count > 0:
            self.webrtc_streamer.stream_frame(frame)
        
        # Send to native RTSP server if active (fallback mode)
        if self.rtsp_server and self.rtsp_server.is_running:
            # Send to main stream
            self.rtsp_server.stream_frame(self.config.main_stream_name, frame)
            # Send to sub stream (resized)
            if self.config.sub_width != self.config.main_width or self.config.sub_height != self.config.main_height:
                sub_frame = cv2.resize(frame, (self.config.sub_width, self.config.sub_height))
                self.rtsp_server.stream_frame(self.config.sub_stream_name, sub_frame)
            else:
                self.rtsp_server.stream_frame(self.config.sub_stream_name, frame)
        
        # Send to go2rtc streamer if available (not in fallback mode)
        result = True
        if not self._use_mjpeg_fallback and self.streamer:
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
        
        if self._streaming_mode == 'go2rtc':
            return self._running and self.streamer is not None and self.streamer.is_running
        elif self._streaming_mode == 'native_webrtc':
            return self._running and self.webrtc_streamer is not None and self.webrtc_streamer.is_running
        elif self._streaming_mode in ('native_rtsp', 'native_rtsp_webrtc'):
            # Native RTSP mode - check if RTSP server is running
            return self._running and self.rtsp_server is not None and self.rtsp_server.is_running
        else:  # mjpeg fallback
            return self._running and self.mjpeg_streamer is not None and self.mjpeg_streamer.is_running
    
    @property
    def streaming_mode(self) -> str:
        """Get the current streaming mode: 'go2rtc', 'native_rtsp', 'native_rtsp_webrtc', 'native_webrtc', or 'mjpeg'"""
        return self._streaming_mode
    
    @property
    def using_mjpeg_fallback(self) -> bool:
        """Check if the camera is using the native MJPEG fallback"""
        return self._use_mjpeg_fallback
    
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
        
        # Determine source icon and label based on source_type
        source_icons = {
            'camera': '📷',
            'video_file': '🎬',
            'generated': '🔄',
            'rtsp': '📡',
            'screen': '🖥️',
            'custom': '⚙️',
            'unknown': '❓',
        }
        source_labels = {
            'camera': 'Camera',
            'video_file': 'Video File',
            'generated': 'Generated',
            'rtsp': 'RTSP Stream',
            'screen': 'Screen Capture',
            'custom': 'Custom Source',
            'unknown': 'Unknown Source',
        }
        source_icon = source_icons.get(self.config.source_type, '❓')
        source_type_label = source_labels.get(self.config.source_type, 'Unknown Source')
        source_info = self.config.source_info or 'Not specified'
        
        # MJPEG URL
        mjpeg_url = f"http://{self.config.local_ip}:{self.config.onvif_port}/{self.config.mjpeg_url}"
        
        replacements = {
            '{{camera_name}}': self.config.name,
            '{{preview_url}}': preview_url,
            '{{main_rtsp}}': self.config.main_stream_rtsp,
            '{{sub_rtsp}}': self.config.sub_stream_rtsp,
            '{{onvif_url}}': self.config.onvif_url,
            '{{webrtc_url}}': self.config.webrtc_url,
            '{{mjpeg_url}}': mjpeg_url,
            '{{main_stream_name}}': self.config.main_stream_name,
            '{{sub_stream_name}}': self.config.sub_stream_name,
            '{{source_icon}}': source_icon,
            '{{source_type_label}}': source_type_label,
            '{{source_info}}': source_info,
            '{{version}}': __version__,
        }
        
        for key, value in replacements.items():
            html = html.replace(key, value)
        
        return html
