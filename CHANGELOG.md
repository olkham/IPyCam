# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

Baseline: released version `1.2.0` (see `ipycam/__version__.py`). Everything
below has landed on top of that release but has not been tagged yet.

### Added
- **Authentication (optional)**: HTTP Basic auth for the web UI/REST API/snapshot/MJPEG,
  and WS-Security `UsernameToken` (`PasswordDigest`) for ONVIF. Off by default
  (empty credentials); enable via the web UI "User" tab or `POST /api/credentials`,
  or by setting `username`/`password` in the config file.
- **Local recording**: record the outbound stream to disk (`ipycam/recorder.py`)
  with segment rotation by file size and an optional pre-record ring buffer.
  New config fields `recording_enabled`, `recording_format`, `recording_path`,
  `recording_max_file_mb`, `recording_pre_seconds`. New endpoints
  `POST /api/recording/start`, `POST /api/recording/stop`,
  `GET /api/recording/status`, and `IPCamera.start_recording()`/`stop_recording()`.
  New web UI record button.
- **Display transforms**: `flip` (vertical), `mirror` (horizontal), and
  `rotation` (0/90/180/270) config fields, applied in the frame pipeline
  before the timestamp overlay. New web UI "Display" settings tab.
- **Paged settings UI**: web UI settings reorganized into Display / Stream /
  Identity / User tabs (previously a single flat settings panel).
  Identity edits and display edits save independently per tab.
  New "User" tab manages credentials.
- **Main/sub MJPEG preview switch**: the native MJPEG preview endpoint accepts
  `?stream=sub` to serve the sub-resolution stream (`GET /stream.mjpeg?stream=sub`);
  the web UI exposes a quality toggle for it.
- **Library logging hygiene**: `ipycam` is silent by default (`NullHandler`
  attached at the package root). Applications call the new
  `ipycam.configure_logging(level=...)` helper (`ipycam/logging_config.py`) or
  configure the `ipycam` logger themselves. `python -m ipycam` gained a
  `--log-level {DEBUG,INFO,WARNING,ERROR}` flag.
- **Bounded drop-oldest frame queue** (`ipycam/framequeue.py`, `FrameQueue`/
  `LatestFrameQueue`): shared primitive used to decouple the capture thread
  from every downstream consumer (MJPEG, native RTSP fan-out, recorder).
- CI workflow (`.github/workflows/ci.yml`): runs the test suite on
  ubuntu-latest and windows-latest across Python 3.8-3.12, a dedicated job
  installing the `webrtc` extra, plus non-blocking ruff and mypy checks, on
  every push to `main` and on pull requests.
- Substantially expanded test suite (400+ tests) covering config, ptz, mjpeg,
  onvif, http, camera, streamer, rtsp, webrtc, discovery, framequeue, and
  recorder.

### Changed
- **WebRTC is now an optional extra**: `aiortc`/`aiohttp` are no longer core
  dependencies. Install with `pip install ipycam[webrtc]` for native WebRTC
  streaming; the core install works without them (RTSP/MJPEG/go2rtc paths are
  unaffected).
- **Decoupled frame pipeline**: the capture thread (`IPCamera.stream()`) now
  only ever does non-blocking enqueues into bounded drop-oldest queues; all
  encoding and socket/disk I/O moved onto per-output worker threads (MJPEG
  encode worker + per-client writer threads, native RTSP fan-out worker,
  recorder worker). A single slow MJPEG client, a slow disk, or a stalled
  encoder can no longer stall capture or other clients.
- **go2rtc/FFmpeg resilience**: a dead FFmpeg push process now triggers a
  bounded, backed-off automatic reconnect (`VideoStreamer._reconnect`,
  capped attempts/backoff) instead of tearing down the stream; if reconnects
  are exhausted the camera falls back to serving MJPEG/snapshot instead of
  stopping entirely.
- Tightened `/static/` path handling and removed permissive CORS headers on
  the WebRTC signaling endpoint (the web UI is always same-origin, so no
  cross-origin access is required).
- **Windows webcam capture backend**: the CLI now opens local capture devices
  with the MSMF backend (falling back to DirectShow, then default) instead of
  forcing DirectShow, which was slow to open and read on many webcams. The
  MSMF hardware-transform slowdown is disabled via
  `OPENCV_VIDEOIO_MSMF_ENABLE_HW_TRANSFORMS=0`, set at package import before
  cv2 loads. File-path and URL sources use OpenCV's default backend.

### Fixed
- `python -m ipycam --config custom.json` now saves configuration changes
  back to the custom path instead of silently writing to the default
  `camera_config.json`.
- Snapshot endpoint could occasionally serve a torn/partially-mutated frame
  under concurrent capture; snapshots are now taken from an immutable,
  independently-copied outbound frame.
- WS-Discovery server resource leak on repeated start/stop cycles.
- ONVIF `GetSnapshotUri` could return a stale or dead snapshot URI.

### Security
- HTTP Basic auth (web/REST/snapshot/MJPEG) and ONVIF WS-Security
  `UsernameToken` auth, both optional and off by default (see Added, above).
- Static file serving (`/static/...`) hardened against path traversal
  (percent-encoded `../`, absolute paths, Windows drive letters, and UNC-style
  paths are all rejected before the file is resolved).
- `/api/video/upload` now rejects request bodies above a fixed size cap
  (500 MB) before reading them, closing a memory-exhaustion vector in the
  hand-rolled multipart parser.
- CORS: no `Access-Control-Allow-*` headers are emitted anywhere (the web UI
  is same-origin), preventing arbitrary third-party pages from driving the
  camera API via a visitor's browser.
