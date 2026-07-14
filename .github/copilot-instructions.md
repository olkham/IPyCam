# IPyCam - Copilot Instructions

## Project Overview
IPyCam is a pure Python virtual IP camera providing ONVIF discovery, RTSP/WebRTC/MJPEG streaming, and PTZ controls. It accepts frames from any source (webcam, video files, generated content) and exposes them as a standard IP camera.

## Architecture

### Component Flow
```
Frame Source → IPCamera.stream() → PTZ transform → VideoStreamer/Fallback → Clients
                    ↓
              ONVIF/HTTP ← ONVIFService ← SOAP Templates (static/soap/*.xml)
```

### Key Modules
- **[camera.py](../ipycam/camera.py)** - Main `IPCamera` orchestrator; manages all subsystems and streaming mode selection
- **[streamer.py](../ipycam/streamer.py)** - FFmpeg-based RTMP push to go2rtc (primary high-performance path)
- **[rtsp.py](../ipycam/rtsp.py)** - Native RTSP server fallback (when go2rtc unavailable)
- **[webrtc.py](../ipycam/webrtc.py)** - Native WebRTC via aiortc (fallback)
- **[mjpeg.py](../ipycam/mjpeg.py)** - MJPEG streaming (always available as lowest common denominator)
- **[ptz.py](../ipycam/ptz.py)** - Digital ePTZ + hardware PTZ callback system
- **[onvif.py](../ipycam/onvif.py)** - ONVIF SOAP request routing using XML templates
- **[config.py](../ipycam/config.py)** - `CameraConfig` dataclass with computed URL properties

### Streaming Mode Hierarchy
The camera automatically selects the best available streaming mode:
1. **go2rtc** (best): Requires external go2rtc process + FFmpeg. Hardware encoding support.
2. **Native RTSP/WebRTC**: Falls back when go2rtc unavailable. Requires FFmpeg for RTSP.
3. **MJPEG-only**: Pure Python, always works, highest CPU usage.

## Development Patterns

### Frame Processing Pipeline
All frames flow through `IPCamera.stream(frame)`:
```python
# In camera.py - the frame goes through:
# 1. PTZ transform (ptz.apply_ptz if enabled)
# 2. Timestamp overlay (if config.show_timestamp)
# 3. Dispatch to active streamer (VideoStreamer, NativeRTSPServer, or MJPEGStreamer)
```

### ONVIF Template System
SOAP responses use XML templates in [static/soap/](../ipycam/static/soap/). Templates use `{{variable}}` placeholders:
```python
# onvif.py renders templates with _render()
response = self._render('get_device_information', 
    manufacturer=self.config.manufacturer,
    model=self.config.model)
```

### PTZ Hardware Integration
To add hardware PTZ support, implement `PTZHardwareHandler` protocol and register:
```python
# See examples/hardware_ptz.py for complete example
class MyServoController:
    def on_continuous_move(self, pan_speed, tilt_speed, zoom_speed): ...
    def on_stop(self): ...
    def on_absolute_move(self, pan, tilt, zoom): ...

camera.ptz.add_hardware_handler(MyServoController())
```

### Configuration Pattern
`CameraConfig` has computed URL properties - don't hardcode URLs:
```python
config.main_stream_rtsp  # rtsp://{local_ip}:{rtsp_port}/{main_stream_name}
config.onvif_url         # http://{local_ip}:{onvif_port}/onvif/device_service
config.to_stream_config() # Convert to VideoStreamer's StreamConfig
```

## Running & Testing

### Quick Start
```bash
pip install -e .
python -m ipycam                    # Run with defaults
python examples/basic_webcam.py     # Stream from webcam
```

### With go2rtc (Recommended for Performance)
```bash
# Terminal 1: Start go2rtc
go2rtc.exe --config ipycam/go2rtc.yaml

# Terminal 2: Start IPyCam
python examples/basic_webcam.py
```

### Test Streams
```bash
ffplay rtsp://localhost:8554/video_main    # Test RTSP
# Web UI: http://localhost:8080/
```

## Dependencies
- **Required**: numpy, opencv-python, aiohttp, aiortc
- **Optional**: go2rtc + FFmpeg (recommended for production)
- **Optional**: FrameSource package for 360° camera support

## Common Tasks

### Adding New ONVIF Actions
1. Create XML template in [static/soap/](../ipycam/static/soap/)
2. Add handler method in [onvif.py](../ipycam/onvif.py)
3. Register in `handle_action()` dispatcher dict

### Adding New HTTP Endpoints
Add routes in [http.py](../ipycam/http.py) `do_GET()`/`do_POST()` methods.

### Modifying Web UI
Static files in [static/](../ipycam/static/) - [index.html](../ipycam/static/index.html), [js/app.js](../ipycam/static/js/app.js), [css/style.css](../ipycam/static/css/style.css)
