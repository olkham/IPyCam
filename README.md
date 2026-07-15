# IPyCam - Pure Python Virtual IP Camera

A lightweight, pure Python virtual IP camera that provides ONVIF discovery, RTSP streaming, and PTZ controls. Perfect for testing, development, or creating custom camera solutions.

## 📺 Demo Video
https://github.com/user-attachments/assets/1d3afd23-1ab8-40f6-876c-3f3f9a0089fe

[YouTube](https://www.youtube.com/watch?v=9AkqjnZRQTE)

## Features

- **ONVIF Compliance**: Full WS-Discovery support for automatic camera detection
- **RTSP Streaming**: High-performance video streaming via go2rtc (optional) or native Python fallback
- **WebRTC Support**: Direct browser streaming with native Python implementation (optional extra)
- **PTZ Controls**: Digital Pan-Tilt-Zoom with preset positions
- **Hardware Acceleration**: Automatic detection and use of NVENC, QSV, or CPU encoding (with go2rtc)
- **Native Fallback**: Pure Python streaming (WebRTC, MJPEG) when go2rtc is not available
- **Optional Authentication**: HTTP Basic auth for the web UI/REST API and WS-Security for ONVIF, off by default
- **Local Recording**: record the stream to disk (mp4/avi) with an optional pre-record buffer
- **Display Transforms**: flip, mirror, and 90/180/270 rotation, applied live
- **Web Interface**: Built-in configuration (tabbed settings), live preview with main/sub stream switching, and recording control
- **Decoupled Frame Pipeline**: capture never blocks on encoding, disk I/O, or slow clients (bounded drop-oldest queues + per-output worker threads)
- **Silent-by-Default Logging**: the library emits nothing unless the host application opts in
- **Flexible Input**: Accept frames from any source (webcam, video file, generated content)

## Requirements

- Python 3.8+
- **Optional**: FFmpeg + go2rtc for hardware-accelerated encoding (recommended for high performance)
- **Optional**: `pip install ipycam[webrtc]` (pulls in `aiortc` + `aiohttp`) for native Python WebRTC streaming

> **Note**: IPyCam can run without go2rtc using pure Python streaming. However, go2rtc + FFmpeg provides significantly better performance, especially for high-resolution streams.
>
> **Note**: `aiortc`/`aiohttp` are no longer installed by default -- the core package installs without them. Install the `webrtc` extra only if you need the native (non-browser, non-go2rtc) WebRTC path.

## Quick Start

Clone the repo:
```bash
git clone https://github.com/olkham/IPyCam.git
```

### Setup Scripts (Windows and Linux)

If you prefer a guided setup, use the provided scripts. They install dependencies and prepare the environment for running the examples.

**Windows (PowerShell or Command Prompt):**
```bat
setup.bat
```

**Linux/macOS (bash):**
```bash
chmod +x setup.sh
./setup.sh
```

> **Tip**: Run the script from the project root (the folder that contains `setup.bat` and `setup.sh`).


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

**Optional: Enhanced Streaming Performance (Recommended)**

For hardware-accelerated encoding with go2rtc:

1. **Install go2rtc**: Download from [go2rtc releases](https://github.com/AlexxIT/go2rtc/releases)
2. **Start go2rtc** with IPyCam configuration:
   ```bash
   go2rtc.exe --config ipycam\go2rtc.yaml
   ```
   Keep this running in a separate terminal.

Without go2rtc, IPyCam will automatically fall back to native Python streaming.

**Optional: Native WebRTC Support**

For the pure-Python native WebRTC streaming path (used when go2rtc isn't running and you still want direct browser streaming):
```bash
pip install "ipycam[webrtc]"
```
This pulls in `aiortc` and `aiohttp`. Without this extra, IPyCam still works fine via go2rtc/RTSP/MJPEG -- native WebRTC is simply unavailable and the web UI falls back to MJPEG preview.

**Optional: 360° Camera Support**

For the `360_ptz.py` example with equirectangular projection:
```bash
pip install "ipycam[camera360]"
```
or install FrameSource separately:
```bash
pip install framesource
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

The library itself is silent by default (see [Logging](#logging)); the CLI enables console logging for you and supports `--log-level {DEBUG,INFO,WARNING,ERROR}` (default `INFO`) to control verbosity, e.g.:

```bash
python -m ipycam --log-level DEBUG
```

`python -m ipycam --config custom.json` loads (and, on any config change made via the web UI, saves back to) that specific file instead of the default `camera_config.json`.

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

Configuration is stored in `camera_config.json` (or whatever path is passed to `--config` / `CameraConfig.load()` -- changes made via the web UI are saved back to that same file):

```json
{
  "name": "Virtual Camera",
  "manufacturer": "PythonCam",
  "model": "VirtualCam-1",
  "username": "",
  "password": "",
  "main_width": 1920,
  "main_height": 1080,
  "main_fps": 30,
  "main_bitrate": "8M",
  "sub_width": 640,
  "sub_height": 360,
  "sub_bitrate": "1M",
  "hw_accel": "auto",
  "flip": false,
  "mirror": false,
  "rotation": 0,
  "recording_enabled": false,
  "recording_format": "mp4",
  "recording_path": "recordings",
  "recording_max_file_mb": 1024,
  "recording_pre_seconds": 0
}
```

Hardware acceleration options (go2rtc only):
- `"auto"` - Try NVENC → QSV → CPU (default)
- `"nvenc"` - NVIDIA GPU encoding
- `"qsv"` - Intel Quick Sync Video
- `"cpu"` - Software encoding (libx264)

Display transforms (`flip`/`mirror`/`rotation`) are applied to every outbound frame (after PTZ, before the timestamp overlay) regardless of streaming mode:
- `flip` - vertical flip (upside-down)
- `mirror` - horizontal flip (mirror image)
- `rotation` - clockwise rotation in degrees: `0`, `90`, `180`, or `270`

Recording fields are covered in [Recording](#recording) below. `username`/`password` are covered in [Authentication](#authentication).

## Authentication

Authentication is **optional and off by default**. With empty `username`/`password`, every endpoint behaves exactly as before (fully open).

To enable it, set both a username and password -- either:
- In the web UI, under the **User** settings tab, or
- Via `POST /api/credentials` with a JSON body `{"username": "...", "password": "..."}`, or
- Directly in the config file (`username`/`password` fields).

Once both are set:
- **HTTP Basic auth** protects the web UI, the REST API (`/api/*`), the snapshot endpoint, and the MJPEG stream.
- **ONVIF** requests must carry a WS-Security `UsernameToken` (`PasswordDigest`); unauthenticated SOAP requests get a fault response.
- `/static/*` assets remain unauthenticated (CSS/JS/images only -- no camera data).

Setting both fields back to empty strings disables auth again.

> **Security note**: IPyCam binds to `0.0.0.0` by default (reachable from any device on your network). If you're running it on an untrusted network, enabling authentication is recommended.

## Logging

The `ipycam` library is silent by default -- it attaches only a `NullHandler` to its logger tree, so importing/using it produces no console output. To see log messages, either:

```python
from ipycam import configure_logging
import logging

configure_logging(level=logging.INFO)  # or DEBUG for more detail
```

or configure the `"ipycam"` logger yourself with the standard `logging` module. `python -m ipycam` calls `configure_logging()` for you and exposes it via `--log-level`.

## Web Interface

The built-in web UI (`http://<host>:8080/` by default) provides:
- A live preview with a stream switcher (go2rtc RTC main/sub, native WebRTC, or native MJPEG) and, in native MJPEG mode, a main/sub quality toggle
- A record button that starts/stops local recording via the same API described in [Recording](#recording)
- Tabbed settings: **Display** (timestamp, flip/mirror/rotation), **Stream** (main/sub resolution, FPS, bitrate, hardware acceleration, read-only network ports), **Identity** (name/manufacturer/model), and **User** (authentication credentials)
- Each settings tab saves independently; fields that require a stream restart (e.g. resolution, FPS, rotation) trigger one automatically

## Recording

IPyCam can record the outbound stream to disk independently of any RTSP/WebRTC/MJPEG client. Recording runs on its own worker thread, so a slow disk drops frames from the recorder's internal queue rather than affecting the live stream.

Configure via `camera_config.json`:
- `recording_enabled` - if `true`, the recorder (and its pre-record buffer, if configured) starts running as soon as the camera starts
- `recording_format` - `"mp4"` or `"avi"`
- `recording_path` - output directory (config-file only; not editable via the web UI/API, to avoid a path-traversal foot-gun)
- `recording_max_file_mb` - segment rotates to a new file once it exceeds this size
- `recording_pre_seconds` - seconds of **pre-record** buffer (0-30) to include before the trigger

Control at runtime via the web UI Record button, or the REST API:
```bash
curl -X POST http://localhost:8080/api/recording/start
curl -X POST http://localhost:8080/api/recording/stop
curl http://localhost:8080/api/recording/status
```
or directly from Python:
```python
camera.start_recording()
# ... later ...
files = camera.stop_recording()  # returns the finalized segment file path(s)
```

> **Memory caveat**: the pre-record buffer holds `recording_pre_seconds * main_fps` full decoded frames at main resolution in memory. At high resolution/FPS this can be large (e.g. ~5 seconds at 30fps/1080p is several GB), which is why `recording_pre_seconds` is capped at 30. Keep it as low as your use case allows.

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
5. **The capture loop never blocks on output**: MJPEG encoding, native RTSP fan-out, recording, and go2rtc/WebRTC delivery all run on their own worker threads behind bounded drop-oldest queues, so a slow client, a slow disk, or a stalled encoder degrades only that consumer (extra frame drops there) rather than the whole camera
6. **A dead go2rtc/FFmpeg process auto-recovers**: IPyCam retries with a bounded backoff and, if reconnection is exhausted, falls back to serving MJPEG/snapshot instead of taking the camera down

## Architecture

```
ipycam/
├── __init__.py          # Package exports
├── __main__.py          # CLI entry point
├── camera.py            # Main IPCamera class (frame pipeline orchestration)
├── config.py            # CameraConfig dataclass
├── streamer.py          # go2rtc/FFmpeg encoding and streaming pipeline
├── ptz.py               # Digital PTZ implementation
├── onvif.py             # ONVIF SOAP service (incl. WS-Security auth)
├── discovery.py         # WS-Discovery server
├── http.py              # HTTP request handler (web UI, REST API, auth)
├── mjpeg.py             # Native MJPEG streaming fallback
├── rtsp.py              # Native RTSP server fallback
├── webrtc.py            # Native WebRTC streaming (optional `webrtc` extra)
├── framequeue.py        # Bounded drop-oldest frame queue (pipeline primitive)
├── recorder.py          # Local disk recording (segments + pre-record buffer)
├── logging_config.py    # configure_logging() helper (library is silent by default)
└── static/              # Web UI and SOAP templates
```

## Development

### Running Tests

IPyCam includes a comprehensive test suite (400+ tests) using pytest. To run the tests:

```bash
# Install dev dependencies (add the webrtc extra too for full WebRTC coverage)
pip install -e ".[dev,webrtc]"

# Run all tests
pytest

# Run with verbose output
pytest -v

# Run with coverage report
pytest --cov=ipycam --cov-report=term-missing

# Run specific test file
pytest tests/test_config.py
pytest tests/test_ptz.py
pytest tests/test_mjpeg.py
pytest tests/test_onvif.py
```

Tests that exercise the `webrtc` extra are skipped automatically when `aiortc` isn't installed, so `pip install -e ".[dev]"` alone is sufficient for most changes. See [CONTRIBUTING.md](CONTRIBUTING.md) for more on the dev workflow, linting/type-checking, and CI.

The test suite covers:
- **CameraConfig**: Configuration serialization, URL generation, hardware acceleration settings, auth/display/recording field validation
- **PTZController**: Positioning, presets, hardware handler callbacks, frame transforms
- **MJPEGStreamer**: Client management, frame streaming, statistics, main/sub stream selection
- **ONVIFService**: SOAP response generation, PTZ command parsing, device info, WS-Security UsernameToken auth
- **IPCameraHTTPHandler (http)**: REST/config endpoints, HTTP Basic auth, static-file path-traversal hardening, recording endpoints, video upload limits
- **IPCamera (camera)**: frame pipeline orchestration, streaming-mode selection/fallback, recording integration
- **VideoStreamer (streamer)**: go2rtc/FFmpeg lifecycle, reconnect/backoff behavior
- **NativeRTSPServer (rtsp)**: native RTSP fallback streaming
- **NativeWebRTCStreamer (webrtc)**: native WebRTC signaling and streaming (skipped without the `webrtc` extra)
- **WSDiscoveryServer (discovery)**: WS-Discovery responses and lifecycle
- **FrameQueue (framequeue)**: bounded drop-oldest queue semantics
- **VideoRecorder (recorder)**: segment rotation, pre-record ring buffer, start/stop lifecycle

CI (`.github/workflows/ci.yml`) runs this suite on ubuntu-latest and windows-latest across Python 3.8-3.12 on every push to `main` and every pull request, plus non-blocking ruff/mypy checks.

## Troubleshooting

### Camera freezes after a few frames
- **With go2rtc**: Check FFmpeg is installed and in PATH, verify go2rtc.exe is running. A dead FFmpeg process is retried automatically (bounded backoff); if it can't recover, the camera falls back to MJPEG-only rather than freezing -- look for a "falling back to MJPEG-only streaming" warning in the log
- **Native mode**: Install PyAV (`pip install av`) for RTSP or `pip install "ipycam[webrtc]"` for native WebRTC
- Check hardware encoder availability (go2rtc only)

### Low FPS performance
- **Recommended**: Use go2rtc with hardware acceleration
- **Native mode**: Reduce `main_width`, `main_height`, and `main_fps` in config
- Check CPU/GPU usage
- Ensure webcam supports requested FPS
- Check `GET /api/stats` (and `GET /api/recording/status` for recording) for per-output frame/drop counts if a specific consumer seems to be lagging

### ONVIF discovery not working
- Check firewall allows UDP port 3702
- Verify local network allows multicast
- Use ONVIF Device Manager to test
- If authentication is enabled, make sure the ONVIF client is configured with WS-Security UsernameToken credentials matching `username`/`password`

## License

MIT License - see LICENSE file for details

## Credits

- Built with Python, NumPy, and OpenCV
- Uses [go2rtc](https://github.com/AlexxIT/go2rtc) for RTSP streaming
- ONVIF protocol implementation based on WS-Discovery specs
