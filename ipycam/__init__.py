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

__version__ = "1.0.0"

from .config import CameraConfig
from .camera import IPCamera
from .streamer import VideoStreamer, StreamConfig, StreamStats, HWAccel
from .ptz import PTZController, PTZState, PTZPreset, PTZHardwareHandler, PTZVelocity
from .onvif import ONVIFService
from .discovery import WSDiscoveryServer
from .http import IPCameraHTTPHandler

__all__ = [
    # Main classes
    "IPCamera",
    "CameraConfig",
    # Streaming
    "VideoStreamer",
    "StreamConfig",
    "StreamStats",
    "HWAccel",
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
