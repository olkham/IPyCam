#!/usr/bin/env python3
"""
Pure Python IP Camera

A virtual IP camera that:
- Is discoverable via ONVIF (WS-Discovery)
- Provides RTSP streams via go2rtc
- Has a web interface for configuration and live preview
- Accepts frames from any source

Usage:
    camera = IPCamera()
    camera.start()
    
    while running:
        frame = get_frame()
        camera.stream(frame)
    
    camera.stop()
"""

import socket
import struct
import threading
import http.server
import socketserver
import uuid
import re
import json
import time
import os
import numpy as np
from typing import Optional, Dict, Any, Callable
from dataclasses import dataclass, asdict
from urllib.parse import parse_qs, urlparse

from video_streamer import VideoStreamer, StreamConfig, HWAccel, StreamStats


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
            return cls()
        except Exception as e:
            print(f"Failed to load config: {e}")
            return cls()


class ONVIFService:
    """ONVIF Device and Media Service handler"""
    
    def __init__(self, config: CameraConfig):
        self.config = config
        self.device_uuid = f"urn:uuid:{uuid.uuid4()}"
        self._templates: Dict[str, str] = {}
        self._load_templates()
    
    def _load_templates(self):
        """Load all SOAP templates from static/soap/"""
        soap_dir = os.path.join(os.path.dirname(__file__), 'static', 'soap')
        for filename in os.listdir(soap_dir):
            if filename.endswith('.xml'):
                template_name = filename[:-4]  # Remove .xml
                with open(os.path.join(soap_dir, filename), 'r', encoding='utf-8') as f:
                    self._templates[template_name] = f.read()
    
    def _render(self, template_name: str, **kwargs) -> str:
        """Render a template with the given variables"""
        template = self._templates.get(template_name, '')
        for key, value in kwargs.items():
            template = template.replace(f'{{{{{key}}}}}', str(value))
        return template
    
    def _wrap_envelope(self, body: str) -> str:
        """Wrap body content in SOAP envelope"""
        return self._render('envelope', body=body)

    def handle_action(self, action: str, body: str) -> Optional[str]:
        """Route SOAP actions to handlers"""
        handlers = {
            'GetSystemDateAndTime': self.get_system_date_time,
            'GetDeviceInformation': self.get_device_information,
            'GetCapabilities': self.get_capabilities,
            'GetServices': self.get_services,
            'GetScopes': self.get_scopes,
            'GetProfiles': self.get_profiles,
            'GetStreamUri': lambda: self.get_stream_uri(body),
            'GetSnapshotUri': lambda: self.get_snapshot_uri(body),
            'GetVideoEncoderConfiguration': self.get_video_encoder_configuration,
            'GetVideoSourceConfiguration': self.get_video_source_configuration,
            'GetAudioDecoderConfigurations': self.get_audio_decoder_configurations,
        }
        
        for key, handler in handlers.items():
            if key in action:
                return handler()
        
        return self.fault(f"Action not supported: {action}")
    
    def fault(self, reason: str) -> str:
        return self._render('fault', reason=reason)

    def _bitrate_to_kbps(self, bitrate: str) -> int:
        """Convert bitrate string like '4M' or '512K' to kbps"""
        if bitrate.endswith('M'):
            return int(bitrate[:-1]) * 1000
        elif bitrate.endswith('K'):
            return int(bitrate[:-1])
        return int(bitrate)

    def get_system_date_time(self) -> str:
        now = time.gmtime()
        body = self._render('get_system_date_time',
            hour=now.tm_hour, minute=now.tm_min, second=now.tm_sec,
            year=now.tm_year, month=now.tm_mon, day=now.tm_mday)
        return self._wrap_envelope(body)

    def get_device_information(self) -> str:
        body = self._render('get_device_information',
            manufacturer=self.config.manufacturer,
            model=self.config.model,
            firmware_version=self.config.firmware_version,
            serial_number=self.config.serial_number)
        return self._wrap_envelope(body)

    def get_capabilities(self) -> str:
        device_url = f"http://{self.config.local_ip}:{self.config.onvif_port}/onvif/device_service"
        media_url = f"http://{self.config.local_ip}:{self.config.onvif_port}/onvif/media_service"
        body = self._render('get_capabilities', device_url=device_url, media_url=media_url)
        return self._wrap_envelope(body)

    def get_services(self) -> str:
        device_url = f"http://{self.config.local_ip}:{self.config.onvif_port}/onvif/device_service"
        media_url = f"http://{self.config.local_ip}:{self.config.onvif_port}/onvif/media_service"
        body = self._render('get_services', device_url=device_url, media_url=media_url)
        return self._wrap_envelope(body)

    def get_scopes(self) -> str:
        body = self._render('get_scopes', camera_name=self.config.name)
        return self._wrap_envelope(body)

    def get_profiles(self) -> str:
        body = self._render('get_profiles',
            main_width=self.config.main_width,
            main_height=self.config.main_height,
            main_fps=self.config.main_fps,
            main_bitrate_kbps=self._bitrate_to_kbps(self.config.main_bitrate),
            sub_width=self.config.sub_width,
            sub_height=self.config.sub_height,
            sub_fps=self.config.sub_fps,
            sub_bitrate_kbps=self._bitrate_to_kbps(self.config.sub_bitrate))
        return self._wrap_envelope(body)

    def get_stream_uri(self, body: str) -> str:
        uri = self.config.main_stream_rtsp
        if "Sub" in body or "Profile_2" in body:
            uri = self.config.sub_stream_rtsp
        body = self._render('get_stream_uri', stream_uri=uri)
        return self._wrap_envelope(body)

    def get_snapshot_uri(self, body: str) -> str:
        uri = f"http://{self.config.local_ip}:{self.config.web_port}/snapshot.jpg"
        body = self._render('get_snapshot_uri', snapshot_uri=uri)
        return self._wrap_envelope(body)

    def get_video_encoder_configuration(self) -> str:
        body = self._render('get_video_encoder_configuration',
            main_width=self.config.main_width,
            main_height=self.config.main_height,
            main_fps=self.config.main_fps,
            main_bitrate_kbps=self._bitrate_to_kbps(self.config.main_bitrate))
        return self._wrap_envelope(body)

    def get_video_source_configuration(self) -> str:
        body = self._render('get_video_source_configuration',
            main_width=self.config.main_width,
            main_height=self.config.main_height)
        return self._wrap_envelope(body)

    def get_audio_decoder_configurations(self) -> str:
        body = self._render('get_audio_decoder_configurations')
        return self._wrap_envelope(body)

    def create_probe_match(self, relates_to: str) -> str:
        """Create WS-Discovery ProbeMatch response"""
        message_id = f"urn:uuid:{uuid.uuid4()}"
        return self._render('probe_match',
            message_id=message_id,
            relates_to=relates_to,
            device_uuid=self.device_uuid,
            camera_name=self.config.name,
            onvif_url=self.config.onvif_url)


class IPCameraHTTPHandler(http.server.BaseHTTPRequestHandler):
    """HTTP handler for ONVIF and Web UI"""
    
    
    camera: Optional['IPCamera'] = None  # Set by IPCamera    
    def log_message(self, format, *args):
        pass  # Suppress logging
    
    def do_GET(self):
        path = urlparse(self.path).path
        
        if path == '/' or path == '/index.html':
            self.serve_web_ui()
        elif path.startswith('/static/'):
            self.serve_static(path)
        elif path == '/api/config':
            self.serve_config()
        elif path == '/api/stats':
            self.serve_stats()
        elif path == '/snapshot.jpg':
            self.serve_snapshot()
        else:
            self.send_error(404)
    
    def do_POST(self):
        path = urlparse(self.path).path
        
        if path.startswith('/onvif/'):
            self.handle_onvif()
        elif path == '/api/config':
            self.update_config()
        elif path == '/api/restart':
            self.restart_stream()
        else:
            self.send_error(404)
    
    def handle_onvif(self):
        """Handle ONVIF SOAP requests"""
        try:
            content_length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(content_length).decode('utf-8')
            soap_action = self.headers.get('SOAPAction', '').strip('"')
            
            # Detect action from body if header is missing
            if not soap_action:
                for action in ['GetDeviceInformation', 'GetSystemDateAndTime', 'GetCapabilities',
                              'GetServices', 'GetProfiles', 'GetStreamUri', 'GetSnapshotUri',
                              'GetVideoEncoderConfiguration', 'GetVideoSourceConfiguration',
                              'GetAudioDecoderConfigurations', 'GetScopes']:
                    if action in body:
                        soap_action = action
                        break
            
            response = self.camera.onvif.handle_action(soap_action, body)
            
            if response:
                response_bytes = response.encode('utf-8')
                self.send_response(200)
                self.send_header('Content-Type', 'application/soap+xml; charset=utf-8')
                self.send_header('Content-Length', len(response_bytes))
                self.end_headers()
                self.wfile.write(response_bytes)
            else:
                self.send_error(501, "Not Implemented")
        except Exception as e:
            print(f"ONVIF Error: {e}")
            self.send_error(500, str(e))
    
    def serve_web_ui(self):
        """Serve the configuration web UI"""
        html = self.camera.get_web_ui_html()
        self.send_response(200)
        self.send_header('Content-Type', 'text/html')
        self.end_headers()
        self.wfile.write(html.encode('utf-8'))
    
    def serve_static(self, path: str):
        """Serve static files (CSS, JS)"""
        # Remove /static/ prefix and sanitize path
        relative_path = path[8:]  # Remove '/static/'
        if '..' in relative_path:
            self.send_error(403, "Forbidden")
            return
        
        static_dir = os.path.join(os.path.dirname(__file__), 'static')
        file_path = os.path.join(static_dir, relative_path)
        
        if not os.path.isfile(file_path):
            self.send_error(404, "File not found")
            return
        
        # Determine content type
        content_types = {
            '.css': 'text/css',
            '.js': 'application/javascript',
            '.html': 'text/html',
            '.png': 'image/png',
            '.jpg': 'image/jpeg',
            '.ico': 'image/x-icon',
        }
        ext = os.path.splitext(file_path)[1].lower()
        content_type = content_types.get(ext, 'application/octet-stream')
        
        try:
            with open(file_path, 'rb') as f:
                content = f.read()
            self.send_response(200)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', len(content))
            self.end_headers()
            self.wfile.write(content)
        except Exception as e:
            self.send_error(500, str(e))
    
    def serve_config(self):
        """Serve current config as JSON"""
        config_dict = asdict(self.camera.config)
        # Add computed properties
        config_dict['main_stream_rtsp'] = self.camera.config.main_stream_rtsp
        config_dict['sub_stream_rtsp'] = self.camera.config.sub_stream_rtsp
        config_dict['webrtc_url'] = self.camera.config.webrtc_url
        
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(json.dumps(config_dict).encode('utf-8'))
    
    def serve_stats(self):
        """Serve streaming stats as JSON"""
        stats = {}
        if self.camera.streamer:
            s = self.camera.streamer.stats
            stats = {
                'frames_sent': s.frames_sent,
                'actual_fps': round(s.actual_fps, 1),
                'elapsed_time': round(s.elapsed_time, 1),
                'dropped_frames': s.dropped_frames,
                'is_streaming': self.camera.streamer.is_running,
            }
        
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(json.dumps(stats).encode('utf-8'))
    
    def serve_snapshot(self):
        """Serve current frame as JPEG snapshot"""
        if self.camera._last_frame is not None:
            import cv2
            _, jpeg = cv2.imencode('.jpg', self.camera._last_frame)
            self.send_response(200)
            self.send_header('Content-Type', 'image/jpeg')
            self.end_headers()
            self.wfile.write(jpeg.tobytes())
        else:
            self.send_error(503, "No frame available")
    
    def update_config(self):
        """Update camera configuration"""
        try:
            content_length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(content_length).decode('utf-8')
            new_config = json.loads(body)
            
            # Apply updates
            restart_needed = False
            for key, value in new_config.items():
                if hasattr(self.camera.config, key):
                    old_value = getattr(self.camera.config, key)
                    if old_value != value:
                        setattr(self.camera.config, key, value)
                        # Check if this requires a stream restart
                        if key in ['main_width', 'main_height', 'main_fps', 'main_bitrate',
                                  'sub_width', 'sub_height', 'sub_bitrate', 'hw_accel']:
                            restart_needed = True
            
            # Auto-restart stream if needed
            restarted = False
            if restart_needed and self.camera.is_running:
                restarted = self.camera.restart_stream()
            
            # Save config to file
            self.camera.config.save()
            
            response = {
                'success': True, 
                'restart_needed': restart_needed,
                'restarted': restarted
            }
            
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps(response).encode('utf-8'))
            
        except Exception as e:
            self.send_response(400)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({'error': str(e)}).encode('utf-8'))
    
    def restart_stream(self):
        """Restart the video stream with current config"""
        try:
            success = self.camera.restart_stream()
            response = {'success': success}
            
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps(response).encode('utf-8'))
        except Exception as e:
            self.send_response(500)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({'error': str(e)}).encode('utf-8'))


class WSDiscoveryServer(threading.Thread):
    """WS-Discovery server for ONVIF device discovery"""
    
    def __init__(self, onvif_service: ONVIFService):
        super().__init__(daemon=True)
        self.onvif = onvif_service
        self.running = True
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(('', 3702))
        
        # Join multicast group
        mreq = struct.pack("4s4s", 
                          socket.inet_aton('239.255.255.250'),
                          socket.inet_aton(self.onvif.config.local_ip))
        try:
            self.sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
        except Exception:
            mreq = struct.pack("4sl", socket.inet_aton('239.255.255.250'), socket.INADDR_ANY)
            self.sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
    
    def run(self):
        while self.running:
            try:
                data, addr = self.sock.recvfrom(4096)
                msg = data.decode('utf-8')
                if 'Probe' in msg:
                    # Extract MessageID
                    match = re.search(r'MessageID>(.+?)</', msg)
                    relates_to = match.group(1) if match else f"urn:uuid:{uuid.uuid4()}"
                    response = self.onvif.create_probe_match(relates_to)
                    self.sock.sendto(response.encode('utf-8'), addr)
            except Exception as e:
                if self.running:
                    print(f"Discovery error: {e}")
    
    def stop(self):
        self.running = False


class IPCamera:
    """
    Pure Python IP Camera
    
    Combines VideoStreamer, ONVIF server, and Web UI into a complete
    virtual IP camera solution.
    """
    
    def __init__(self, config: Optional[CameraConfig] = None):
        self.config = config or CameraConfig()
        self.streamer: Optional[VideoStreamer] = None
        self.onvif = ONVIFService(self.config)
        self._http_server: Optional[socketserver.TCPServer] = None
        self._discovery: Optional[WSDiscoveryServer] = None
        self._last_frame: Optional[np.ndarray] = None
        self._running = False
        self._restarting = False  # Flag to prevent loop exit during restart
        
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
            return False
        
        print(f"  Main Stream: {self.config.main_stream_rtsp}")
        print(f"  Sub Stream: {self.config.sub_stream_rtsp}")
        print(f"  WebRTC: {self.config.webrtc_url}")
        
        self._running = True
        return True
    
    def stop(self):
        """Stop all camera services"""
        self._running = False
        
        if self.streamer:
            self.streamer.stop()
        
        if self._discovery:
            self._discovery.stop()
        
        if self._http_server:
            self._http_server.shutdown()
        
        print("IP Camera stopped")
    
    def stream(self, frame: np.ndarray) -> bool:
        """Send a frame to the stream"""
        self._last_frame = frame.copy()  # Keep for snapshots
        if self.streamer:
            return self.streamer.stream(frame)
        return False
    
    def restart_stream(self) -> bool:
        """Restart the video streamer with current config"""
        print("Restarting video stream...")
        self._restarting = True
        
        try:
            # Stop current streamer
            if self.streamer:
                self.streamer.stop()
                self.streamer = None
            
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


# Example usage
if __name__ == "__main__":
    import cv2
    
    # Load config from file, or use defaults if not found
    config = CameraConfig.load("camera_config.json")
    print(f"Loaded config: {config.name} ({config.main_width}x{config.main_height}@{config.main_fps}fps)")
    
    camera = IPCamera(config)
    
    if not camera.start():
        print("Failed to start camera")
        exit(1)
    
    print("\n" + "="*50)
    print("IP Camera is running!")
    print("="*50)
    print(f"\nOpen Web UI: http://{config.local_ip}:{config.onvif_port}/")
    print("Press Ctrl+C to stop\n")
    
    # Open video file as test source
    cap = cv2.VideoCapture(0)
    # Set camera resolution to 1920x1080
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, 1920)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 1080)
    if not cap.isOpened():
        print("Could not open vid2.mkv")
        camera.stop()
        exit(1)
    
    start_time = time.time()
    frame_count = 0
    last_fps = camera.config.main_fps
    
    try:
        while camera.is_running:
            ret, frame = cap.read()

            if not ret:
                cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                continue
            
            # Add timestamp
            timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
            cv2.putText(frame, timestamp, (10, 30),
                       cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 255), 2)
            
            camera.stream(frame)
            frame_count += 1
            
            # Check if FPS changed - reset timing
            if camera.config.main_fps != last_fps:
                last_fps = camera.config.main_fps
                start_time = time.time()
                frame_count = 1
            
            # Precise frame pacing (read FPS dynamically)
            target_frame_time = 1.0 / camera.config.main_fps
            expected_time = start_time + (frame_count * target_frame_time)
            sleep_time = expected_time - time.time()
            if sleep_time > 0:
                time.sleep(sleep_time)
                
    except KeyboardInterrupt:
        print("\nShutting down...")
    finally:
        cap.release()
        camera.stop()
