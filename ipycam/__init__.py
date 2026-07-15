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

import logging
import os

# OpenCV on Windows defaults to the Media Foundation (MSMF) capture backend
# with hardware frame transforms enabled, which makes VideoCapture slow to open
# and laggy to read on many webcams. Disabling that transform restores normal
# throughput. It MUST be set before cv2 is imported -- the submodule imports
# below (e.g. .config -> .streamer) pull cv2 in -- so it lives at the very top
# of the package. setdefault() leaves any value the user already set untouched,
# and the variable is a no-op on non-Windows platforms.
os.environ.setdefault("OPENCV_VIDEOIO_MSMF_ENABLE_HW_TRANSFORMS", "0")

from .__version__ import __version__
from .logging_config import configure_logging

# Library hygiene: silent by default. Applications that want to see log
# output call configure_logging() (or configure the "ipycam" logger
# themselves); without that, no handler exists further up the tree so
# nothing is emitted (standard library practice, see the logging HOWTO).
logging.getLogger("ipycam").addHandler(logging.NullHandler())

from .config import CameraConfig
from .camera import IPCamera
from .framequeue import FrameQueue, LatestFrameQueue
from .streamer import VideoStreamer, StreamConfig, StreamStats, HWAccel
from .ptz import PTZController, PTZState, PTZPreset, PTZHardwareHandler, PTZVelocity
from .onvif import ONVIFService
from .discovery import WSDiscoveryServer
from .http import IPCameraHTTPHandler
from .mjpeg import MJPEGStreamer, check_go2rtc_running, check_rtsp_port_available
from .webrtc import NativeWebRTCStreamer, WebRTCStats, is_webrtc_available
from .rtsp import NativeRTSPServer, is_native_rtsp_available
from .recorder import VideoRecorder

__all__ = [
    # Logging
    "configure_logging",
    # Main classes
    "IPCamera",
    "CameraConfig",
    # Frame plumbing
    "FrameQueue",
    "LatestFrameQueue",
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
    "is_native_rtsp_available",
    # Recording
    "VideoRecorder",
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
