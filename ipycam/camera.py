#!/usr/bin/env python3
"""
Pure Python IP Camera

A virtual IP camera that:
- Is discoverable via ONVIF (WS-Discovery)
- Provides RTSP streams via go2rtc
- Has a web interface for configuration and live preview
- Accepts frames from any source
- Supports digital PTZ (ePTZ) via ONVIF

Usage:
    from ipycam import IPCamera, CameraConfig
    
    camera = IPCamera()
    camera.start()
    
    while running:
        frame = get_frame()
        camera.stream(frame)
    
    camera.stop()
"""

import logging
import os
import time
import threading
import socketserver
from datetime import datetime
from html import escape as html_escape
import numpy as np
import cv2
from typing import Optional

from .__version__ import __version__
from .config import CameraConfig
from .framequeue import FrameQueue
from .streamer import VideoStreamer, StreamStats
from .onvif import ONVIFService
from .http import IPCameraHTTPHandler
from .discovery import WSDiscoveryServer
from .ptz import PTZController
from .mjpeg import MJPEGStreamer, check_go2rtc_running, check_rtsp_port_available
from .webrtc import NativeWebRTCStreamer, is_webrtc_available
from .rtsp import NativeRTSPServer, is_native_rtsp_available
from .recorder import VideoRecorder

logger = logging.getLogger(__name__)


class ReusableThreadingTCPServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


class IPCamera:
    """
    Pure Python IP Camera
    
    Combines VideoStreamer, ONVIF server, PTZ controller, and Web UI 
    into a complete virtual IP camera solution.
    """
    
    def __init__(self, config: Optional[CameraConfig] = None):
        self.config = config or CameraConfig()
        self.streamer: Optional[VideoStreamer] = None
        self.mjpeg_streamer: Optional[MJPEGStreamer] = None
        self.webrtc_streamer: Optional[NativeWebRTCStreamer] = None
        self.rtsp_server: Optional[NativeRTSPServer] = None  # Native RTSP fallback
        self._use_mjpeg_fallback = False
        self._streaming_mode = 'go2rtc'  # 'go2rtc', 'native_webrtc', or 'mjpeg'
        
        # Initialize PTZ controller
        self.ptz = PTZController(
            output_width=self.config.main_width,
            output_height=self.config.main_height,
            max_zoom=4.0
        )
        
        # Initialize ONVIF service with PTZ
        self.onvif = ONVIFService(self.config, self.ptz)
        
        self._http_server: Optional[socketserver.TCPServer] = None
        self._discovery: Optional[WSDiscoveryServer] = None

        # Local disk recorder. Always constructed; its worker only runs while
        # recording or maintaining a pre-record buffer. Frames are handed to it
        # via a bounded drop-oldest queue on a worker thread (same fan-out
        # pattern as MJPEG/RTSP), so a slow disk drops frames instead of
        # stalling the capture loop.
        self.recorder: Optional[VideoRecorder] = VideoRecorder(self.config)

        # Native-RTSP fan-out: the per-frame sub-stream resize is moved OFF the
        # capture thread onto this worker so stream() only enqueues.
        self._rtsp_frame_queue: FrameQueue = FrameQueue(maxsize=2)
        self._rtsp_worker: Optional[threading.Thread] = None
        self._rtsp_worker_running = False

        self._last_frame: Optional[np.ndarray] = None
        # Guards _last_frame: the capture thread writes it while the HTTP
        # snapshot endpoint reads it from another thread.
        self._last_frame_lock = threading.Lock()
        self._running = False
        self._restarting = False  # Flag to prevent loop exit during restart
        
        # Frame pacing
        self._frame_count = 0
        self._stream_start_time: Optional[float] = None
        self._last_fps = 0
        
        # Video upload mode
        self._video_upload_mode = False
        self._current_video_path: Optional[str] = None
        self._previous_video_path: Optional[str] = None
        self._video_error: Optional[str] = None
        self._video_lock = threading.Lock()
        
    def start(self) -> bool:
        """Start the IP camera (ONVIF, Web UI, and streaming)"""
        logger.info(f"Starting IP Camera: {self.config.name}")
        logger.info(f"  Local IP: {self.config.local_ip}")

        # Start WS-Discovery
        self._discovery = WSDiscoveryServer(self.onvif)
        self._discovery.start()
        logger.info(f"  WS-Discovery: listening on port 3702")

        # Start HTTP server (ONVIF + Web UI)
        IPCameraHTTPHandler.camera = self
        self._http_server = ReusableThreadingTCPServer(
            ('', self.config.onvif_port),
            IPCameraHTTPHandler
        )
        self._http_thread = threading.Thread(target=self._http_server.serve_forever, daemon=True)
        self._http_thread.start()
        logger.info(f"  ONVIF Service: {self.config.onvif_url}")
        logger.info(f"  Web UI: http://{self.config.local_ip}:{self.config.onvif_port}/")
        
        # Always start the MJPEG streamer (available as alternative view even with go2rtc)
        self.mjpeg_streamer = MJPEGStreamer(
            quality=80,
            sub_width=self.config.sub_width,
            sub_height=self.config.sub_height,
        )
        self.mjpeg_streamer.start()
        mjpeg_url = f"http://{self.config.local_ip}:{self.config.onvif_port}/{self.config.mjpeg_url}"
        
        # Detect go2rtc BEFORE attempting the RTMP push. Probing the API port
        # (:1984) and RTSP port (:8554) up front means we never fire a doomed
        # FFmpeg push at a non-existent RTMP listener: if go2rtc isn't there we
        # go straight to the native RTSP/WebRTC fallback (faster and clearer).
        go2rtc_available = check_go2rtc_running(port=self.config.go2rtc_api_port)
        rtsp_available = check_rtsp_port_available(port=self.config.rtsp_port)

        if go2rtc_available and rtsp_available:
            # go2rtc is up: push to it (best option: RTSP + WebRTC + MJPEG).
            # VideoStreamer now guarantees a CPU fallback, so a failing HW
            # encoder no longer kills this path.
            self._use_mjpeg_fallback = False
            self._streaming_mode = 'go2rtc'
            stream_config = self.config.to_stream_config()
            self.streamer = VideoStreamer(stream_config)

            if not self.streamer.start(self.config.main_stream_push_url, self.config.sub_stream_push_url):
                logger.warning("  [WARN] Failed to start video streamer, trying native WebRTC fallback...")
                self._try_native_webrtc_fallback(mjpeg_url)
            else:
                logger.info(f"  Main Stream: {self.config.main_stream_rtsp}")
                logger.info(f"  Sub Stream: {self.config.sub_stream_rtsp}")
                logger.info(f"  WebRTC: {self.config.webrtc_url}")
                logger.info(f"  MJPEG Stream: {mjpeg_url}")
        else:
            # go2rtc not (fully) detected - skip the doomed RTMP push entirely
            # and go straight to the native fallback. Log the EXACT command to
            # launch go2rtc with the packaged config so the user can fix it.
            cfg_path = os.path.join(os.path.dirname(__file__), 'go2rtc.yaml')
            if not go2rtc_available:
                logger.warning(
                    '  [WARN] go2rtc not detected on :%s - start it with: '
                    'go2rtc --config "%s" '
                    '(or IPyCam will use the native RTSP/WebRTC fallback)',
                    self.config.go2rtc_api_port, cfg_path,
                )
            else:
                logger.warning(
                    '  [WARN] go2rtc API is up but its RTSP port :%s is not accepting '
                    'connections - start it with: go2rtc --config "%s" '
                    '(using native RTSP/WebRTC fallback meanwhile)',
                    self.config.rtsp_port, cfg_path,
                )
            self._try_native_webrtc_fallback(mjpeg_url)

        # Start the recorder worker (pre-record ring buffer) when recording is
        # enabled in config. An explicit start_recording() can still start it
        # on demand later even if this is disabled.
        if self.recorder is not None and self.config.recording_enabled:
            self.recorder.start()
            logger.info("  Recording: enabled (pre-record %ss, path %s)",
                        self.config.recording_pre_seconds, self.config.recording_path)

        self._running = True
        return True
    
    def _try_native_webrtc_fallback(self, mjpeg_url: str):
        """Try to start native RTSP + WebRTC, fall back to MJPEG-only if not available."""
        # Try to start native RTSP server (requires FFmpeg)
        rtsp_started = False
        if is_native_rtsp_available():
            try:
                self.rtsp_server = NativeRTSPServer(port=self.config.rtsp_port)
                
                # Add main stream
                self.rtsp_server.add_stream(
                    name=self.config.main_stream_name,
                    width=self.config.main_width,
                    height=self.config.main_height,
                    fps=self.config.main_fps,
                    bitrate=self.config.main_bitrate
                )
                
                # Add sub stream
                self.rtsp_server.add_stream(
                    name=self.config.sub_stream_name,
                    width=self.config.sub_width,
                    height=self.config.sub_height,
                    fps=self.config.sub_fps,
                    bitrate=self.config.sub_bitrate
                )
                
                if self.rtsp_server.start():
                    rtsp_started = True
                    self._start_rtsp_fanout()
                    logger.info("  [OK] Native RTSP server started")
                    logger.info(f"    Main Stream: {self.config.main_stream_rtsp}")
                    logger.info(f"    Sub Stream: {self.config.sub_stream_rtsp}")
                else:
                    logger.warning("  [WARN] Failed to start native RTSP server")
                    self.rtsp_server = None
            except Exception as e:
                logger.warning(f"  [WARN] Failed to start native RTSP server: {e}")
                self.rtsp_server = None
        else:
            logger.warning("  [WARN] Native RTSP unavailable (FFmpeg not found)")
        
        # Try to start native WebRTC
        webrtc_started = False
        if is_webrtc_available():
            try:
                self.webrtc_streamer = NativeWebRTCStreamer(
                    fps=self.config.main_fps,
                    width=self.config.main_width,
                    height=self.config.main_height
                )
                if self.webrtc_streamer.start():
                    webrtc_started = True
                    webrtc_native_url = f"http://{self.config.local_ip}:{self.config.onvif_port}/api/webrtc/offer"
                    logger.info(f"  [OK] Native WebRTC started: {webrtc_native_url}")
                else:
                    self.webrtc_streamer = None
            except Exception as e:
                logger.warning(f"  [WARN] Failed to start native WebRTC: {e}")
                self.webrtc_streamer = None

        # Determine streaming mode based on what's available
        if rtsp_started and webrtc_started:
            self._use_mjpeg_fallback = False
            self._streaming_mode = 'native_rtsp_webrtc'
            logger.warning("  [WARN] go2rtc not detected - using native RTSP + WebRTC fallback")
        elif rtsp_started:
            self._use_mjpeg_fallback = False
            self._streaming_mode = 'native_rtsp'
            logger.warning("  [WARN] go2rtc not detected - using native RTSP fallback")
            logger.warning("  Note: Install aiortc for native WebRTC: pip install aiortc")
        elif webrtc_started:
            self._use_mjpeg_fallback = False
            self._streaming_mode = 'native_webrtc'
            logger.warning("  [WARN] go2rtc not detected - using native WebRTC fallback")
            logger.warning("  Note: RTSP unavailable (FFmpeg not found)")
        else:
            # Final fallback: MJPEG only
            self._use_mjpeg_fallback = True
            self._streaming_mode = 'mjpeg'
            logger.warning("  [WARN] No RTSP/WebRTC available - using MJPEG-only fallback")
            logger.warning("  Note: Install aiortc for native WebRTC: pip install aiortc")
            logger.warning("        Or start go2rtc for full functionality: go2rtc --config ipycam/go2rtc.yaml")

        logger.info(f"  MJPEG Stream: {mjpeg_url}")
    
    def stop(self):
        """Stop all camera services"""
        self._running = False
        
        if self.ptz:
            self.ptz.stop()
        
        if self.streamer:
            self.streamer.stop()
        
        if self.webrtc_streamer:
            self.webrtc_streamer.stop()

        if self.rtsp_server:
            self._stop_rtsp_fanout()
            self.rtsp_server.stop()
        
        if self.mjpeg_streamer:
            self.mjpeg_streamer.stop()

        # Finalise any active recording and JOIN the recorder worker (no thread
        # leak, no truncated file).
        if self.recorder:
            self.recorder.stop()

        if self._discovery:
            self._discovery.stop()
        
        if self._http_server:
            self._http_server.shutdown()
            self._http_server.server_close()
        
        logger.info("IP Camera stopped")
    
    def stream(self, frame: np.ndarray) -> bool:
        """Send a frame to the stream (applies PTZ transform, display
        transforms, timestamp, and frame pacing)"""
        # Apply PTZ transform first
        if self.ptz:
            frame = self.ptz.apply_ptz(frame)

        # Apply display transforms (rotate/flip/mirror) AFTER PTZ but BEFORE
        # the timestamp overlay, so the timestamp is drawn readable and
        # correctly positioned on the frame's final (post-transform)
        # orientation instead of getting rotated/flipped itself.
        frame = self._apply_display_transforms(frame)

        # Apply timestamp overlay last (always visible, not affected by PTZ)
        if self.config.show_timestamp:
            frame = self._draw_timestamp(frame)
        
        # ---- OUTBOUND FRAME IMMUTABILITY CONTRACT ---------------------------
        # Make ONE independent copy per iteration (already PTZ-adjusted +
        # timestamp) and hand the SAME object to snapshots and to every async
        # output queue.
        #
        # This copy is essential: the caller's `frame` is reused/mutated in
        # place on the next iteration, so sharing that reference would let a
        # consumer observe a torn frame.
        #
        # `outbound`, by contrast, is IMMUTABLE BY CONTRACT -- it is a fresh
        # buffer each call and NO consumer may mutate it in place. Every
        # consumer either only READS it (MJPEG imencode; go2rtc
        # resize/cvtColor/tobytes; native-RTSP encoder tobytes + sub-stream
        # resize) or takes its OWN copy (get_snapshot_frame; the WebRTC encoder,
        # where av.VideoFrame.from_ndarray copies pixels into its own buffer).
        # Because of this contract the downstream single-slot buffers store a
        # REFERENCE to `outbound` instead of re-copying it (see
        # SharedFrameBuffer.update and NativeRTSPServer.stream_frame), which is
        # this step's whole point: one copy here, zero elsewhere.
        #
        # The next iteration allocates a new buffer and leaves this one stable
        # for whatever is still reading it. Anyone adding a consumer that
        # mutates the frame in place MUST copy first, or restore the defensive
        # copy at that consumer, or this contract breaks.
        # ---------------------------------------------------------------------
        outbound = frame.copy()
        with self._last_frame_lock:
            self._last_frame = outbound

        # Fan out with NON-BLOCKING enqueues only -- nothing below may block on
        # encoding or socket/pipe I/O (that all happens on per-output workers).

        # MJPEG: cheap enqueue into the encode worker (only when watched).
        if self.mjpeg_streamer and self.mjpeg_streamer.client_count > 0:
            self.mjpeg_streamer.stream_frame(outbound)

        # Native WebRTC: this `connection_count > 0` check is the SINGLE
        # authoritative "is anyone watching?" gate for the WebRTC path. When no
        # peer is connected we skip it entirely, so SharedFrameBuffer does zero
        # work (no store, no copy) while WebRTC is idle. When a peer IS
        # connected, stream_frame just stores a REFERENCE to the immutable
        # outbound frame under a lock (no copy); the per-peer isolation happens
        # in the encoder via av.VideoFrame.from_ndarray. Lock-guarded reference
        # store => effectively non-blocking, safe to stay inline.
        if self.webrtc_streamer and self.webrtc_streamer.connection_count > 0:
            self.webrtc_streamer.stream_frame(outbound)

        # Native RTSP: enqueue once; the fan-out worker does the sub-stream
        # resize and per-stream writes off the capture thread.
        if self.rtsp_server and self.rtsp_server.is_running:
            self._rtsp_frame_queue.put(outbound)

        # Recorder: enqueue the immutable outbound frame ONLY while recording or
        # maintaining a pre-record buffer (wants_frames gates this to zero cost
        # when idle). All encoding/disk I/O happens on the recorder worker; this
        # is a non-blocking, drop-oldest enqueue that can never stall capture.
        if self.recorder is not None and self.recorder.wants_frames:
            self.recorder.submit(outbound)

        # go2rtc: enqueue into the streamer's writer thread (non-blocking).
        result = True
        if not self._use_mjpeg_fallback and self.streamer:
            result = self.streamer.stream(outbound)

        # Frame pacing - maintain target FPS
        self._pace_frame()

        return result

    def _start_rtsp_fanout(self):
        """Start the native-RTSP fan-out worker (resize + per-stream writes)."""
        self._rtsp_frame_queue = FrameQueue(maxsize=self._rtsp_frame_queue.maxsize)
        self._rtsp_worker_running = True
        self._rtsp_worker = threading.Thread(
            target=self._rtsp_fanout_loop,
            name="rtsp-fanout-worker",
            daemon=True,
        )
        self._rtsp_worker.start()

    def _stop_rtsp_fanout(self):
        """Signal and join the native-RTSP fan-out worker (with timeout)."""
        self._rtsp_worker_running = False
        self._rtsp_frame_queue.close()
        worker = self._rtsp_worker
        if worker and worker.is_alive():
            worker.join(timeout=2.0)
        self._rtsp_worker = None

    def _rtsp_fanout_loop(self):
        """Worker: forward frames to the native RTSP server's main/sub streams.

        Does the per-frame sub-stream resize here instead of on the capture
        thread. rtsp_server.stream_frame itself only does a locked single-slot
        buffer copy, so this worker never blocks on encoding either.
        """
        while self._rtsp_worker_running:
            frame = self._rtsp_frame_queue.get(timeout=0.5)
            if frame is None:
                continue
            server = self.rtsp_server
            if not server or not server.is_running:
                continue
            try:
                server.stream_frame(self.config.main_stream_name, frame)
                if (self.config.sub_width != self.config.main_width
                        or self.config.sub_height != self.config.main_height):
                    sub_frame = cv2.resize(frame, (self.config.sub_width, self.config.sub_height))
                    server.stream_frame(self.config.sub_stream_name, sub_frame)
                else:
                    server.stream_frame(self.config.sub_stream_name, frame)
            except Exception as e:
                logger.error(f"RTSP fan-out error: {e}")

    def get_snapshot_frame(self) -> Optional[np.ndarray]:
        """Return a safe, independent copy of the latest frame for snapshots.

        Thread-safe: the HTTP snapshot endpoint runs on a different thread from
        the capture loop that calls stream(). Returns a fresh copy so the caller
        can encode it without racing the capture thread, or None if no frame has
        been streamed yet.
        """
        with self._last_frame_lock:
            if self._last_frame is None:
                return None
            return self._last_frame.copy()

    def _pace_frame(self):
        """Handle frame pacing to maintain target FPS"""
        # Initialize or reset timing if FPS changed
        if self._stream_start_time is None or self._last_fps != self.config.main_fps:
            self._stream_start_time = time.time()
            self._frame_count = 0
            self._last_fps = self.config.main_fps
        
        self._frame_count += 1
        
        # Calculate expected time for this frame and sleep if ahead
        target_frame_time = 1.0 / self.config.main_fps
        expected_time = self._stream_start_time + (self._frame_count * target_frame_time)
        sleep_time = expected_time - time.time()
        
        if sleep_time > 0:
            time.sleep(sleep_time)
    
    def _apply_display_transforms(self, frame: np.ndarray) -> np.ndarray:
        """Apply the configured rotation/flip/mirror display transforms.

        Order: ROTATE first, then FLIP/MIRROR. Rotating first means flip and
        mirror always act on the frame's final on-screen orientation (what
        the viewer sees), matching how those two settings are described to
        users, rather than on the sensor's native orientation.

        flip (vertical) and mirror (horizontal) are collapsed into a single
        cv2.flip call (flipCode -1) when both are active, instead of two
        separate passes.

        Fast path: when rotation == 0 and neither flip nor mirror is set (the
        common case), this returns the SAME frame object unchanged -- no
        copy, no cv2 call, just the attribute checks below.
        """
        cfg = self.config

        if cfg.rotation == 90:
            frame = cv2.rotate(frame, cv2.ROTATE_90_CLOCKWISE)
        elif cfg.rotation == 180:
            frame = cv2.rotate(frame, cv2.ROTATE_180)
        elif cfg.rotation == 270:
            frame = cv2.rotate(frame, cv2.ROTATE_90_COUNTERCLOCKWISE)

        if cfg.flip and cfg.mirror:
            frame = cv2.flip(frame, -1)  # both axes in one pass
        elif cfg.flip:
            frame = cv2.flip(frame, 0)   # vertical flip
        elif cfg.mirror:
            frame = cv2.flip(frame, 1)   # horizontal flip

        return frame

    def _draw_timestamp(self, frame: np.ndarray) -> np.ndarray:
        """Draw timestamp overlay on frame"""
        timestamp = datetime.now().strftime(self.config.timestamp_format)
        
        # Font settings
        font = cv2.FONT_HERSHEY_SIMPLEX
        font_scale = 0.6
        thickness = 2
        color = (255, 255, 255)  # White
        shadow_color = (0, 0, 0)  # Black shadow
        
        # Get text size
        (text_w, text_h), baseline = cv2.getTextSize(timestamp, font, font_scale, thickness)
        
        # Calculate position based on setting
        h, w = frame.shape[:2]
        padding = 10
        
        if self.config.timestamp_position == "top-left":
            x, y = padding, text_h + padding
        elif self.config.timestamp_position == "top-right":
            x, y = w - text_w - padding, text_h + padding
        elif self.config.timestamp_position == "bottom-right":
            x, y = w - text_w - padding, h - padding
        else:  # bottom-left (default)
            x, y = padding, h - padding
        
        # Draw shadow for better visibility
        cv2.putText(frame, timestamp, (x + 1, y + 1), font, font_scale, shadow_color, thickness + 1)
        # Draw text
        cv2.putText(frame, timestamp, (x, y), font, font_scale, color, thickness)
        
        return frame
    
    def restart_stream(self) -> bool:
        """Restart the video streamer with current config"""
        logger.info("Restarting video stream...")
        self._restarting = True
        
        try:
            # Stop current streamer
            if self.streamer:
                self.streamer.stop()
                self.streamer = None
            
            # Update PTZ controller dimensions
            if self.ptz:
                self.ptz.output_width = self.config.main_width
                self.ptz.output_height = self.config.main_height
            
            # Create new streamer with updated config
            stream_config = self.config.to_stream_config()
            self.streamer = VideoStreamer(stream_config)
            
            if not self.streamer.start(self.config.main_stream_rtmp, self.config.sub_stream_rtmp):
                logger.error("  [FAIL] Failed to restart video streamer")
                return False

            logger.info(f"  [OK] Stream restarted: {self.config.main_width}x{self.config.main_height}@{self.config.main_fps}fps")
            return True
        finally:
            self._restarting = False
    
    @property
    def is_running(self) -> bool:
        # During restart, streamer is temporarily None - don't exit the loop
        if self._restarting:
            return self._running

        if self._streaming_mode == 'go2rtc':
            if self.streamer is not None and not self.streamer.is_running:
                # The go2rtc/FFmpeg push has stopped PERMANENTLY: VideoStreamer
                # only reports is_running == False once its writer thread has
                # exhausted its bounded reconnect attempts (a transient
                # reconnect-in-progress keeps reporting True -- see
                # VideoStreamer.is_running / _reconnect). A dead FFmpeg process
                # must not take the whole camera down: fall back to serving the
                # outputs that don't depend on it (MJPEG/snapshot) instead of
                # reporting not-running and ending the caller's capture loop.
                logger.warning("  [WARN] go2rtc video streamer stopped permanently (FFmpeg "
                               "reconnect exhausted) - falling back to MJPEG-only streaming")
                self._use_mjpeg_fallback = True
                self._streaming_mode = 'mjpeg'
                return self._running and self.mjpeg_streamer is not None and self.mjpeg_streamer.is_running
            return self._running and self.streamer is not None and self.streamer.is_running
        elif self._streaming_mode == 'native_webrtc':
            return self._running and self.webrtc_streamer is not None and self.webrtc_streamer.is_running
        elif self._streaming_mode in ('native_rtsp', 'native_rtsp_webrtc'):
            # Native RTSP mode - check if RTSP server is running
            return self._running and self.rtsp_server is not None and self.rtsp_server.is_running
        else:  # mjpeg fallback
            return self._running and self.mjpeg_streamer is not None and self.mjpeg_streamer.is_running
    
    @property
    def streaming_mode(self) -> str:
        """Get the current streaming mode: 'go2rtc', 'native_rtsp', 'native_rtsp_webrtc', 'native_webrtc', or 'mjpeg'"""
        return self._streaming_mode
    
    @property
    def using_mjpeg_fallback(self) -> bool:
        """Check if the camera is using the native MJPEG fallback"""
        return self._use_mjpeg_fallback
    
    @property
    def stats(self) -> Optional[StreamStats]:
        return self.streamer.stats if self.streamer else None

    # ---- Recording passthroughs -----------------------------------------
    def start_recording(self) -> bool:
        """Begin recording to disk. Returns True on success.

        Lazily starts the recorder worker if needed, so this works even when
        recording_enabled is False in config. Fails gracefully (returns False,
        logs) on a bad path / un-openable codec -- the camera keeps running.
        """
        if self.recorder is None:
            return False
        return self.recorder.start_recording()

    def stop_recording(self) -> list:
        """Finalise the current recording; returns the segment file path(s)."""
        if self.recorder is None:
            return []
        return self.recorder.stop_recording()

    @property
    def is_recording(self) -> bool:
        return self.recorder is not None and self.recorder.is_recording

    @property
    def recording_stats(self) -> dict:
        """Recorder state snapshot (recording bool, file, bytes, drops, ...)."""
        if self.recorder is None:
            return {'recording': False, 'worker_running': False}
        return self.recorder.stats()

    def apply_recording_config(self) -> None:
        """Reconcile the recorder with the current config after an update.

        Called after /api/config applies a recording_* field. Toggling
        recording_enabled starts/stops the recorder worker directly (it never
        needs a stream restart); other knobs (pre-record length, format) are
        picked up via reconfigure().
        """
        if self.recorder is None:
            return
        self.recorder.reconfigure()
        if self.config.recording_enabled and not self.recorder.is_worker_running:
            self.recorder.start()
        elif (not self.config.recording_enabled
              and self.recorder.is_worker_running
              and not self.recorder.is_recording):
            # Disabled and idle -> shut the worker down. An in-progress
            # recording is left running until it is explicitly stopped.
            self.recorder.stop()

    # Video upload mode methods
    def set_video_upload_mode(self, enabled: bool):
        """Enable or disable video upload mode"""
        self._video_upload_mode = enabled
    
    @property
    def video_upload_mode(self) -> bool:
        """Check if video upload mode is enabled"""
        return self._video_upload_mode
    
    def get_current_video_path(self) -> Optional[str]:
        """Get the current video file path"""
        with self._video_lock:
            return self._current_video_path
    
    def set_current_video_path(self, path: Optional[str]):
        """Set the current video file path"""
        with self._video_lock:
            if self._current_video_path:
                self._previous_video_path = self._current_video_path
            self._current_video_path = path
            self._video_error = None
    
    def get_previous_video_path(self) -> Optional[str]:
        """Get the previous video file path"""
        with self._video_lock:
            return self._previous_video_path
    
    def notify_video_loaded(self, path: str):
        """Notify that a video was successfully loaded - cleanup old videos"""
        with self._video_lock:
            self._video_error = None
            self.config.source_info = os.path.basename(path)
        
        # Clean up old videos in the videos folder
        self._cleanup_old_videos(path)
    
    def notify_video_error(self, error: str):
        """Notify that a video failed to load"""
        with self._video_lock:
            self._video_error = error
            # Revert to previous video if available
            if self._previous_video_path:
                self._current_video_path = self._previous_video_path
                self._previous_video_path = None
    
    def get_video_error(self) -> Optional[str]:
        """Get the last video error"""
        with self._video_lock:
            return self._video_error
    
    def clear_video_error(self):
        """Clear the video error"""
        with self._video_lock:
            self._video_error = None
    
    def _cleanup_old_videos(self, current_path: str):
        """Remove old videos from the videos folder, keeping only the current one"""
        try:
            videos_dir = os.path.join(os.path.dirname(__file__), '..', 'videos')
            videos_dir = os.path.abspath(videos_dir)
            
            if not os.path.isdir(videos_dir):
                return
            
            current_path = os.path.abspath(current_path)
            video_extensions = {'.mp4', '.avi', '.mkv', '.mov', '.wmv', '.flv', '.webm', '.m4v', '.mpeg', '.mpg', '.3gp'}
            
            for filename in os.listdir(videos_dir):
                filepath = os.path.join(videos_dir, filename)
                _, ext = os.path.splitext(filename)
                
                # Skip non-video files and the current video
                if ext.lower() not in video_extensions:
                    continue
                if os.path.abspath(filepath) == current_path:
                    continue
                
                try:
                    os.remove(filepath)
                    logger.info(f"  Cleaned up old video: {filename}")
                except Exception as e:
                    logger.warning(f"  Warning: Could not remove old video {filename}: {e}")

        except Exception as e:
            logger.warning(f"  Warning: Video cleanup failed: {e}")
    
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
        
        # Determine source icon and label based on source_type
        source_icons = {
            'camera': '📷',
            'video_file': '🎬',
            'generated': '🔄',
            'rtsp': '📡',
            'screen': '🖥️',
            'custom': '⚙️',
            'unknown': '❓',
        }
        source_labels = {
            'camera': 'Camera',
            'video_file': 'Video File',
            'generated': 'Generated',
            'rtsp': 'RTSP Stream',
            'screen': 'Screen Capture',
            'custom': 'Custom Source',
            'unknown': 'Unknown Source',
        }
        source_icon = source_icons.get(self.config.source_type, '❓')
        source_type_label = source_labels.get(self.config.source_type, 'Unknown Source')
        source_info = self.config.source_info or 'Not specified'
        
        # MJPEG URL
        mjpeg_url = f"http://{self.config.local_ip}:{self.config.onvif_port}/{self.config.mjpeg_url}"
        
        replacements = {
            '{{camera_name}}': self.config.name,
            '{{preview_url}}': preview_url,
            '{{main_rtsp}}': self.config.main_stream_rtsp,
            '{{sub_rtsp}}': self.config.sub_stream_rtsp,
            '{{onvif_url}}': self.config.onvif_url,
            '{{webrtc_url}}': self.config.webrtc_url,
            '{{mjpeg_url}}': mjpeg_url,
            '{{main_stream_name}}': self.config.main_stream_name,
            '{{sub_stream_name}}': self.config.sub_stream_name,
            '{{source_icon}}': source_icon,
            '{{source_type_label}}': source_type_label,
            '{{source_info}}': source_info,
            '{{version}}': __version__,
        }
        
        # HTML-escape every substituted value: several of them (name,
        # source_info, stream names, ...) are config/user-controlled and would
        # otherwise allow stored XSS in the web UI. All placeholders sit in
        # HTML text or quoted-attribute contexts (never inside <script>), so
        # html.escape (which also escapes quotes) is safe for URLs too --
        # browsers decode entities in href/src attributes.
        for key, value in replacements.items():
            html = html.replace(key, html_escape(str(value)))

        return html
