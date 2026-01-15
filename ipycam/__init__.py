"""
IPyCam - Pure Python Virtual IP Camera

A virtual IP camera that:
- Is discoverable via ONVIF (WS-Discovery)
- Provides RTSP streams via go2rtc
- Has a web interface for configuration and live preview
- Accepts frames from any source
- Supports digital PTZ (ePTZ) via ONVIF

Usage:
    from ipycam import IPCamera, CameraConfig
    
    config = CameraConfig(name="My Camera")
    camera = IPCamera(config)
    camera.start()
    
    while camera.is_running:
        frame = get_frame()  # Your frame source
        camera.stream(frame)
    
    camera.stop()
"""

from .__version__ import __version__

from .config import CameraConfig
from .camera import IPCamera
from .streamer import VideoStreamer, StreamConfig, StreamStats, HWAccel
from .ptz import PTZController, PTZState, PTZPreset, PTZHardwareHandler, PTZVelocity
from .onvif import ONVIFService
from .discovery import WSDiscoveryServer
from .http import IPCameraHTTPHandler
from .mjpeg import MJPEGStreamer, check_go2rtc_running, check_rtsp_port_available
from .webrtc import NativeWebRTCStreamer, WebRTCStats, is_webrtc_available
from .rtsp import NativeRTSPServer, RTSPStats, is_rtsp_server_available

__all__ = [
    # Main classes
    "IPCamera",
    "CameraConfig",
    # Streaming
    "VideoStreamer",
    "StreamConfig",
    "StreamStats",
    "HWAccel",
    # MJPEG fallback
    "MJPEGStreamer",
    "check_go2rtc_running",
    "check_rtsp_port_available",
    # Native WebRTC fallback
    "NativeWebRTCStreamer",
    "WebRTCStats",
    "is_webrtc_available",
    # Native RTSP fallback
    "NativeRTSPServer",
    "RTSPStats",
    "is_rtsp_server_available",
    # PTZ
    "PTZController",
    "PTZState",
    "PTZPreset",
    "PTZVelocity",
    "PTZHardwareHandler",
    # ONVIF
    "ONVIFService",
    "WSDiscoveryServer",
    "IPCameraHTTPHandler",
]
