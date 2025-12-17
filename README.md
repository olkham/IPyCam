# IPyCam - Pure Python Virtual IP Camera

A lightweight, pure Python virtual IP camera that provides ONVIF discovery, RTSP streaming, and PTZ controls. Perfect for testing, development, or creating custom camera solutions.

## Features

- **ONVIF Compliance**: Full WS-Discovery support for automatic camera detection
- **RTSP Streaming**: High-performance video streaming via go2rtc
- **PTZ Controls**: Digital Pan-Tilt-Zoom with preset positions
- **Hardware Acceleration**: Automatic detection and use of NVENC, QSV, or CPU encoding
- **Web Interface**: Built-in configuration and live preview
- **Low Latency**: Optimized async frame pipeline for real-time streaming
- **Flexible Input**: Accept frames from any source (webcam, video file, generated content)

## Requirements

- Python 3.8+
- FFmpeg (with hardware encoding support recommended)
- go2rtc (not included)

## Quick Start

### Prerequisites

1. **Install go2rtc**: Download the latest release from [go2rtc releases](https://github.com/AlexxIT/go2rtc/releases)
2. **Start go2rtc** with the IPyCam configuration:
   ```bash
   go2rtc.exe --config ipycam\go2rtc.yaml
   ```
   Keep this running in a separate terminal.

### Installation

Install directly from GitHub:
```bash
pip install git+https://github.com/olkham/IPyCam.git
```

Or install from source:
```bash
git clone https://github.com/olkham/IPyCam.git
cd ipycam
pip install -e .
```

**Optional: 360° Camera Support**

For the `360_ptz.py` example with equirectangular projection:
```bash
pip install "ipycam[camera360] @ git+https://github.com/olkham/IPyCam.git"
```
or install FrameSource separately:
```bash
pip install git+https://github.com/olkham/IPyCam.git
pip install git+https://github.com/olkham/FrameSource.git
```

### Basic Usage

```python
import cv2
from ipycam import IPCamera, CameraConfig

# Create camera with custom config
config = CameraConfig(
    name="My Virtual Camera",
    main_width=1920,
    main_height=1080,
    main_fps=30,
)

camera = IPCamera(config)
camera.start()

# Stream from webcam
cap = cv2.VideoCapture(0)

try:
    while camera.is_running:
        ret, frame = cap.read()
        if ret:
            camera.stream(frame)
except KeyboardInterrupt:
    pass
finally:
    cap.release()
    camera.stop()
```

### Running as a Module

```bash
python -m ipycam
```

Then access:
- **Web UI**: http://localhost:8080/
- **RTSP Main Stream**: rtsp://localhost:8554/video_main
- **RTSP Sub Stream**: rtsp://localhost:8554/video_sub
- **ONVIF Service**: http://localhost:8080/onvif/device_service

### Testing the Stream

Test the RTSP streams using ffplay:
```bash
# Test main stream
ffplay rtsp://localhost:8554/video_main

# Test sub stream
ffplay rtsp://localhost:8554/video_sub
```

## Configuration

Configuration is stored in `camera_config.json`:

```json
{
  "name": "Virtual Camera",
  "manufacturer": "PythonCam",
  "model": "VirtualCam-1",
  "main_width": 1920,
  "main_height": 1080,
  "main_fps": 30,
  "main_bitrate": "4M",
  "sub_width": 640,
  "sub_height": 360,
  "hw_accel": "auto"
}
```

Hardware acceleration options:
- `"auto"` - Try NVENC → QSV → CPU (default)
- `"nvenc"` - NVIDIA GPU encoding
- `"qsv"` - Intel Quick Sync Video
- `"cpu"` - Software encoding (libx264)

## PTZ Controls

The camera includes digital PTZ (Pan-Tilt-Zoom) support:

```python
# Access PTZ through ONVIF or directly
camera.ptz.continuous_move(pan_speed=0.5, tilt_speed=0.0, zoom_speed=0.0)
camera.ptz.stop()
camera.ptz.goto_preset(preset_token="preset1")
```

PTZ presets are stored in `ptz_presets.json`.

## Performance Tips

1. **Use hardware acceleration**: Enable NVENC (NVIDIA) or QSV (Intel) for best performance
2. **Match resolutions**: Set camera input to match streaming resolution to avoid resize overhead
3. **Adjust FPS**: Most webcams are limited to 30fps
4. **Disable PTZ**: Set camera to home position (0,0,0) to skip PTZ transforms

## Architecture

```
ipycam/
├── __init__.py       # Package exports
├── __main__.py       # CLI entry point
├── camera.py         # Main IPCamera class
├── config.py         # CameraConfig dataclass
├── streamer.py       # Video encoding and streaming pipeline
├── ptz.py            # Digital PTZ implementation
├── onvif.py          # ONVIF SOAP service
├── discovery.py      # WS-Discovery server
├── http.py           # HTTP request handler
└── static/           # Web UI and SOAP templates
```

## Troubleshooting

### Camera freezes after a few frames
- Check FFmpeg is installed and in PATH
- Verify go2rtc.exe is running
- Check hardware encoder availability

### Low FPS performance
- Enable hardware acceleration in config
- Reduce resolution or bitrate
- Check CPU/GPU usage
- Ensure webcam supports requested FPS

### ONVIF discovery not working
- Check firewall allows UDP port 3702
- Verify local network allows multicast
- Use ONVIF Device Manager to test

## License

MIT License - see LICENSE file for details

## Credits

- Built with Python, NumPy, and OpenCV
- Uses [go2rtc](https://github.com/AlexxIT/go2rtc) for RTSP streaming
- ONVIF protocol implementation based on WS-Discovery specs
