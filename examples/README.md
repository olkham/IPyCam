# IPyCam Examples

This folder contains example scripts demonstrating various ways to use IPyCam.

## Prerequisites

Install IPyCam:
```bash
pip install ipycam
```

Or from source:
```bash
cd ..
pip install -e .
```

Make sure go2rtc is running (usually `go2rtc.exe` or `./go2rtc` in the parent directory).

## Examples

### 1. Basic Webcam (`basic_webcam.py`)
The simplest example - stream from your webcam with default settings.

```bash
python basic_webcam.py
```

**What it demonstrates:**
- Creating an IPCamera with default configuration
- Opening and streaming from a webcam
- Adding timestamp overlays
- Basic frame pacing

---

### 2. Custom Configuration (`custom_config.py`)
Shows how to customize camera settings and save/load configurations.

```bash
python custom_config.py
```

**What it demonstrates:**
- Creating custom CameraConfig
- Saving/loading configuration files
- Setting resolution, bitrate, and hardware acceleration
- Custom overlays

---

### 3. Video File Streaming (`video_file.py`)
Stream a video file in a loop (useful for testing without a physical camera).

```bash
python video_file.py path/to/your/video.mp4
```

**What it demonstrates:**
- Streaming from a video file instead of a webcam
- Looping video content
- Handling different frame rates

---

### 4. PTZ Demo (`ptz_demo.py`)
Automated demo of PTZ (Pan-Tilt-Zoom) controls.

```bash
python ptz_demo.py
```

**What it demonstrates:**
- Continuous PTZ movement (pan, tilt, zoom)
- Absolute positioning
- Relative movement
- Saving and loading PTZ presets
- Displaying PTZ status overlays

---

### 5. Generated Content (`generated_content.py`)
Stream procedurally generated content (animated gradient).

```bash
python generated_content.py
```

**What it demonstrates:**
- Streaming generated frames instead of camera input
- Creating frames with NumPy
- Animation and effects

---

### 6. Hardware PTZ (`hardware_ptz.py`)
Connect external hardware (motors, servos, gimbals) to ONVIF PTZ commands.

```bash
python hardware_ptz.py
```

**What it demonstrates:**
- Implementing the `PTZHardwareHandler` protocol
- Registering hardware handlers with the PTZ controller
- Converting normalized PTZ values to hardware-specific values
- Running digital PTZ alongside hardware PTZ
- Hardware-only mode (disable digital PTZ)

**Example handlers included:**
- `PrintingPTZHandler` - Debug handler that prints all commands
- `ServoExample` - Template for RC servo control (Raspberry Pi, etc.)
- `StepperMotorExample` - Template for stepper motor control

---

### 7. 360° Virtual PTZ (`360_ptz.py`)
Stream a 360° equirectangular video as a virtual PTZ camera.

```bash
python 360_ptz.py
```

**What it demonstrates:**
- Equirectangular to pinhole projection
- Using 360° video as camera source
- Virtual PTZ without physical camera movement
- Real-time spherical coordinate transformations
- PTZ controls mapped to viewing angles (pan → yaw, tilt → pitch, zoom → FOV)

**Requirements:**
- A 360° video file in equirectangular format (place in project root as `360_recording_20251215_215722.mp4` or update the path in the script)

---

## Accessing the Streams

Once any example is running, you can access:

- **Web UI**: http://localhost:8080/ (control panel with live preview)
- **RTSP Stream**: rtsp://localhost:8554/video_main (VLC, FFmpeg, etc.)
- **ONVIF**: http://localhost:8080/onvif/device_service (ONVIF clients)

### Test with VLC
```bash
vlc rtsp://localhost:8554/video_main
```

### Test with FFplay
```bash
ffplay rtsp://localhost:8554/video_main
```

### ONVIF Clients
Test discovery and PTZ control with:
- ONVIF Device Manager (Windows)
- tinyCam Pro (Android)
- Any ONVIF-compliant app

## Tips

1. **Hardware Acceleration**: Set `hw_accel="nvenc"` (NVIDIA) or `"qsv"` (Intel) for better performance
2. **Resolution Matching**: Set your camera/video to match the streaming resolution to avoid resizing
3. **FPS**: Most webcams are limited to 30fps
4. **PTZ Performance**: PTZ is disabled by default (home position) for maximum FPS

## Troubleshooting

**Camera won't start:**
- Make sure go2rtc is running
- Check that ports 8080, 8554, 1935, 3702 are available
- Verify FFmpeg is installed and in PATH

**Low FPS:**
- Enable hardware acceleration in config
- Reduce resolution or bitrate
- Check if webcam supports the requested FPS

**No video in stream:**
- Wait a few seconds for FFmpeg to initialize
- Check go2rtc logs
- Verify video source is working (test with `cv2.VideoCapture`)

## Next Steps

- Check the main [README.md](../README.md) for detailed documentation
- Explore the [ipycam package source](../ipycam/) to understand internals
- Create your own custom applications!
