#!/usr/bin/env python3
"""Camera configuration dataclass"""

import json
import socket
from dataclasses import dataclass, asdict

try:
    from .streamer import StreamConfig, HWAccel
except ImportError:
    from streamer import StreamConfig, HWAccel


def get_local_ip() -> str:
    """Get the local IP address of the machine."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(('10.255.255.255', 1))
        ip = s.getsockname()[0]
    except Exception:
        ip = '127.0.0.1'
    finally:
        s.close()
    return ip


@dataclass
class CameraConfig:
    """Complete camera configuration"""
    # Identity
    name: str = "Virtual Camera"
    manufacturer: str = "PythonCam"
    model: str = "VirtualCam-1"
    serial_number: str = "PY-000001"
    firmware_version: str = "1.0.0"
    
    # Network
    local_ip: str = ""
    onvif_port: int = 8080
    rtsp_port: int = 8554
    rtmp_port: int = 1935
    web_port: int = 8081
    go2rtc_api_port: int = 1984
    
    # Main stream
    main_width: int = 1920
    main_height: int = 1080
    main_fps: int = 60
    main_bitrate: str = "8M"
    main_stream_name: str = "video_main"
    
    # Sub stream
    sub_width: int = 640
    sub_height: int = 360
    sub_fps: int = 30
    sub_bitrate: str = "1M"
    sub_stream_name: str = "video_sub"
    
    # Encoding
    hw_accel: str = "auto"
    
    # Overlay
    show_timestamp: bool = True
    timestamp_format: str = "%Y-%m-%d %H:%M:%S"
    timestamp_position: str = "bottom-left"  # top-left, top-right, bottom-left, bottom-right
    
    def __post_init__(self):
        if not self.local_ip:
            self.local_ip = get_local_ip()
    
    @property
    def main_stream_rtmp(self) -> str:
        return f"rtmp://127.0.0.1:{self.rtmp_port}/{self.main_stream_name}"
    
    @property
    def sub_stream_rtmp(self) -> str:
        return f"rtmp://127.0.0.1:{self.rtmp_port}/{self.sub_stream_name}"
    
    @property
    def main_stream_rtsp(self) -> str:
        return f"rtsp://{self.local_ip}:{self.rtsp_port}/{self.main_stream_name}"
    
    @property
    def sub_stream_rtsp(self) -> str:
        return f"rtsp://{self.local_ip}:{self.rtsp_port}/{self.sub_stream_name}"
    
    @property
    def onvif_url(self) -> str:
        return f"http://{self.local_ip}:{self.onvif_port}/onvif/device_service"
    
    @property
    def webrtc_url(self) -> str:
        return f"http://{self.local_ip}:{self.go2rtc_api_port}"
    
    def to_stream_config(self) -> StreamConfig:
        """Convert to VideoStreamer StreamConfig"""
        hw = HWAccel.AUTO
        if self.hw_accel == "nvenc":
            hw = HWAccel.NVENC
        elif self.hw_accel == "qsv":
            hw = HWAccel.QSV
        elif self.hw_accel == "cpu":
            hw = HWAccel.CPU
            
        return StreamConfig(
            width=self.main_width,
            height=self.main_height,
            fps=self.main_fps,
            bitrate=self.main_bitrate,
            hw_accel=hw,
            sub_width=self.sub_width,
            sub_height=self.sub_height,
            sub_bitrate=self.sub_bitrate,
        )
    
    def save(self, filepath: str = "camera_config.json") -> bool:
        """Save configuration to JSON file"""
        try:
            config_dict = asdict(self)
            # Don't save local_ip as it's auto-detected
            config_dict.pop('local_ip', None)
            with open(filepath, 'w', encoding='utf-8') as f:
                json.dump(config_dict, f, indent=2)
            return True
        except Exception as e:
            print(f"Failed to save config: {e}")
            return False
    
    @classmethod
    def load(cls, filepath: str = "camera_config.json") -> 'CameraConfig':
        """Load configuration from JSON file, or return defaults if not found"""
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                config_dict = json.load(f)
            # Filter to only valid fields
            valid_fields = {f.name for f in cls.__dataclass_fields__.values()}
            filtered = {k: v for k, v in config_dict.items() if k in valid_fields}
            return cls(**filtered)
        except FileNotFoundError:
            print(f"Config file '{filepath}' not found, using defaults")
            return cls()
        except Exception as e:
            print(f"Failed to load config: {e}")
            return cls()
