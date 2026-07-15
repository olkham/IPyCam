#!/usr/bin/env python3
"""
Modular Video Streamer Class

A clean, reusable video streamer that accepts frames from any source
and pushes them to an RTMP endpoint (e.g., go2rtc) for RTSP redistribution.
"""

import subprocess
import time
import logging
import threading
import numpy as np
from typing import Optional, Tuple, Deque
from dataclasses import dataclass, field
from enum import Enum
from collections import deque

from .framequeue import FrameQueue

logger = logging.getLogger(__name__)


class HWAccel(Enum):
    AUTO = "auto"
    NVENC = "nvenc"
    QSV = "qsv"
    CPU = "cpu"


@dataclass
class StreamConfig:
    """Configuration for a video stream"""
    width: int = 1920
    height: int = 1080
    fps: int = 30
    bitrate: str = "4M"
    keyframe_interval: Optional[int] = None  # Defaults to fps (1 second)
    hw_accel: HWAccel = HWAccel.QSV
    # Substream settings (used when rtmp_url_sub is provided)
    sub_width: int = 640
    sub_height: int = 480
    sub_bitrate: str = "1M"
    
    def __post_init__(self):
        if self.keyframe_interval is None:
            self.keyframe_interval = self.fps


@dataclass
class StreamStats:
    """Statistics for the current stream with sliding window FPS calculation"""
    frames_sent: int = 0
    bytes_sent: int = 0
    start_time: float = field(default_factory=time.time)
    last_frame_time: float = 0
    dropped_frames: int = 0
    # Sliding window for FPS calculation (stores timestamps)
    _frame_timestamps: Deque[float] = field(default_factory=lambda: deque(maxlen=150))
    _window_seconds: float = 5.0  # Calculate FPS over last 5 seconds
    
    @property
    def elapsed_time(self) -> float:
        return time.time() - self.start_time
    
    @property
    def actual_fps(self) -> float:
        """Calculate FPS over a sliding window of recent frames"""
        if len(self._frame_timestamps) < 2:
            return 0
        
        current_time = time.time()
        # Find frames within the window
        cutoff_time = current_time - self._window_seconds
        
        # Count frames in window
        recent_frames = sum(1 for ts in self._frame_timestamps if ts >= cutoff_time)
        
        if recent_frames < 2:
            return 0
        
        # Find oldest frame in window
        oldest_in_window = next((ts for ts in self._frame_timestamps if ts >= cutoff_time), None)
        if oldest_in_window is None:
            return 0
        
        time_span = current_time - oldest_in_window
        if time_span > 0:
            return recent_frames / time_span
        return 0
    
    @property
    def bitrate_mbps(self) -> float:
        if self.elapsed_time > 0:
            return (self.bytes_sent * 8) / (self.elapsed_time * 1_000_000)
        return 0
    
    def record_frame(self, timestamp: float):
        """Record a frame timestamp for FPS calculation"""
        self._frame_timestamps.append(timestamp)


class VideoStreamer:
    """
    A modular video streamer that accepts frames and pushes them to RTMP.
    
    This class handles the FFmpeg pipeline setup and frame transmission,
    allowing you to send frames from any source (camera, OpenCV, generated, etc.)
    """
    
    # Reconnect tuning. Class attributes (not constants baked into the method)
    # so tests can shrink them per-instance/per-class to keep exhaustive-retry
    # and backoff-interruption tests fast and deterministic.
    RECONNECT_INITIAL_BACKOFF = 1.0   # seconds before the first retry
    RECONNECT_MAX_BACKOFF = 8.0       # backoff doubles up to this cap
    RECONNECT_MAX_ATTEMPTS = 4        # bounded -- then give up permanently
    RECONNECT_CHECK_TIMEOUT = 2.0     # passed to _check_ffmpeg_running()
    RECONNECT_WARMUP_TIMEOUT = 5.0    # passed to _warm_up_encoder()

    def __init__(self, config: Optional[StreamConfig] = None):
        """
        Initialize the streamer with optional configuration.

        Args:
            config: StreamConfig object with stream parameters.
                   If None, uses defaults (1920x1080 @ 30fps)
        """
        self.config = config or StreamConfig()
        self._ffmpeg_process: Optional[subprocess.Popen] = None
        self._is_running = False
        self._lock = threading.Lock()
        self.stats = StreamStats()
        self._active_hw_accel: Optional[str] = None
        self._rtmp_url: Optional[str] = None
        self._rtmp_url_sub: Optional[str] = None
        self._ffmpeg_stderr_buffer = []
        self._stderr_thread: Optional[threading.Thread] = None

        # Frames are handed to a dedicated writer thread through this bounded,
        # drop-oldest queue. The blocking stdin.write to FFmpeg happens on the
        # writer thread only: when FFmpeg back-pressures (the OS pipe buffer
        # fills) the writer blocks, the queue drops frames, and the capture
        # thread is never frozen.
        self._frame_queue: FrameQueue = FrameQueue(maxsize=2)
        self._writer_thread: Optional[threading.Thread] = None
        self._writer_running = False

        # Set by stop() (and checked between reconnect attempts / during the
        # backoff sleep) so a shutdown request interrupts a reconnecting
        # writer promptly instead of waiting out the remaining backoff/attempts.
        self._shutdown_event = threading.Event()

        # Number of times the writer thread has successfully restarted FFmpeg
        # after a broken pipe / process death. Reset per start(); NOT reset
        # between individual reconnect attempts within a single outage (each
        # outage's own attempt counter is local to _reconnect()).
        self.reconnect_count = 0

    @property
    def is_running(self) -> bool:
        """Whether the streamer is started and has not permanently failed.

        This stays True across a transient reconnect: on a broken pipe the
        writer thread retries FFmpeg in the background (see _reconnect) while
        the FrameQueue keeps absorbing/dropping incoming frames, so a caller
        driving a loop off this flag (e.g. IPCamera.is_running) is not tripped
        up by the momentary process replacement. It only goes False once
        stop() is called, or the writer exhausts its bounded reconnect
        attempts and gives up permanently.
        """
        return self._is_running
    
    @property
    def frame_size(self) -> Tuple[int, int]:
        """Return expected frame size as (width, height)"""
        return (self.config.width, self.config.height)
    
    @property
    def expected_frame_bytes(self) -> int:
        """Return expected number of bytes per frame (BGR24)"""
        return self.config.width * self.config.height * 3
    
    def start(self, rtmp_url: str, rtmp_url_sub: Optional[str] = None) -> bool:
        """
        Start the streaming pipeline to the specified RTMP URL.
        
        Args:
            rtmp_url: Primary RTMP destination (e.g., "rtmp://127.0.0.1:1935/video")
            rtmp_url_sub: Optional secondary RTMP destination for a substream
            
        Returns:
            True if started successfully, False otherwise
        """
        if self._is_running:
            logger.warning("Streamer is already running")
            return False
            
        self._rtmp_url = rtmp_url
        self._rtmp_url_sub = rtmp_url_sub
        self.stats = StreamStats()
        self.reconnect_count = 0

        # Try hardware acceleration options.
        #
        # AUTO tries every encoder and already ends with CPU. A SPECIFIC
        # hardware request (nvenc/qsv) ALSO gets a guaranteed CPU (libx264)
        # last-resort appended: if the requested HW encoder is unavailable or
        # its warm-up fails, we degrade to software instead of killing the
        # whole go2rtc push path (a QSV failure must not take streaming down).
        # An explicit CPU request stays CPU-only.
        if self.config.hw_accel == HWAccel.AUTO:
            hw_order = [HWAccel.NVENC, HWAccel.QSV, HWAccel.CPU]
        elif self.config.hw_accel == HWAccel.CPU:
            hw_order = [HWAccel.CPU]
        else:
            hw_order = [self.config.hw_accel, HWAccel.CPU]

        for hw_type in hw_order:
            # When we reach the appended CPU last-resort because a SPECIFIC
            # hardware encoder was requested (not AUTO, not CPU) and it failed,
            # make the software fallback explicit so the log explains WHY the
            # encoder changed.
            if (hw_type == HWAccel.CPU
                    and self.config.hw_accel not in (HWAccel.AUTO, HWAccel.CPU)):
                logger.warning(
                    "%s hardware encoder unavailable/failed - falling back to "
                    "CPU (libx264) software encoding",
                    self.config.hw_accel.value.upper(),
                )
            logger.info(f"Trying {hw_type.value.upper()}...")

            # Quick availability check first (prevents hanging)
            if not self._check_hw_encoder_available(hw_type):
                logger.info(f"[--] {hw_type.value.upper()} not available")
                continue

            logger.info(f"[OK] {hw_type.value.upper()} available")

            try:
                logger.info(f"Starting with {hw_type.value.upper()}...")
                # Reset stderr buffer for this attempt
                self._ffmpeg_stderr_buffer = []

                self._start_ffmpeg(rtmp_url, rtmp_url_sub, hw_type)
                if self._check_ffmpeg_running():
                    # Warm-up: Send test frames to verify encoder actually works
                    if self._warm_up_encoder():
                        self._active_hw_accel = hw_type.value
                        self._is_running = True
                        self._start_writer()
                        logger.info(f"[OK] Streamer started with {hw_type.value.upper()} acceleration")
                        logger.info(f"  Primary: {rtmp_url}")
                        if rtmp_url_sub:
                            logger.info(f"  Substream: {rtmp_url_sub}")
                        return True
                    else:
                        logger.warning(f"[FAIL] {hw_type.value.upper()} encoder initialization failed")
                        self._cleanup_ffmpeg()
                else:
                    logger.warning(f"[FAIL] {hw_type.value.upper()} failed to start")
                    self._cleanup_ffmpeg()
            except Exception as e:
                logger.warning(f"[FAIL] {hw_type.value.upper()} failed: {e}")
                self._cleanup_ffmpeg()

        logger.error("[FAIL] Failed to start streamer with any hardware acceleration")
        return False
    
    def stop(self):
        """Stop the streaming pipeline and clean up resources"""
        self._is_running = False
        # Wake up a writer that may be sleeping in a reconnect backoff (or
        # blocked between attempts) so it abandons reconnecting promptly
        # instead of running out the clock on its own.
        self._shutdown_event.set()
        # Stop and join the writer thread BEFORE tearing down FFmpeg so it can
        # never write to a closing pipe.
        self._stop_writer()
        self._cleanup_ffmpeg()
        # Joined AFTER the process is terminated: that closes its stderr pipe,
        # which is what makes the reader thread's readline() return b'' (EOF)
        # and the thread exit.
        self._join_stderr_thread()
        logger.info(f"Streamer stopped. Stats: {self.stats.frames_sent} frames, "
                    f"{self.stats.actual_fps:.1f} avg fps, "
                    f"{self.stats.bitrate_mbps:.2f} Mbps avg"
                    + (f", {self.reconnect_count} reconnect(s)" if self.reconnect_count else ""))

    def _start_writer(self):
        """Start the background thread that drains the frame queue to FFmpeg."""
        self._frame_queue = FrameQueue(maxsize=self._frame_queue.maxsize)
        self._shutdown_event.clear()
        self._writer_running = True
        self._writer_thread = threading.Thread(
            target=self._write_loop,
            name="go2rtc-stdin-writer",
            daemon=True,
        )
        self._writer_thread.start()

    def _stop_writer(self):
        """Signal and join the writer thread (with timeout)."""
        self._writer_running = False
        self._frame_queue.close()  # wake the writer if it is blocked on get()
        writer = self._writer_thread
        if writer and writer.is_alive():
            writer.join(timeout=3.0)
        self._writer_thread = None

    def _join_stderr_thread(self, timeout: float = 2.0):
        """Join the stderr-reader thread for the (now terminated) process."""
        thread = self._stderr_thread
        if thread and thread.is_alive():
            thread.join(timeout=timeout)
        self._stderr_thread = None

    def stream(self, frame: np.ndarray) -> bool:
        """
        Submit a single frame to the stream. Non-blocking.

        The frame is enqueued for the writer thread; the potentially-blocking
        write to FFmpeg's stdin happens there, never on the caller's thread. If
        the queue is full (writer/FFmpeg cannot keep up) the oldest frame is
        dropped and ``dropped_frames`` is incremented.

        Args:
            frame: NumPy array in BGR format (OpenCV default).
                   Must match the configured width/height or will be resized
                   (on the writer thread).

        Returns:
            True if the frame was accepted onto the queue, False if the streamer
            is not running.
        """
        if not self._is_running or self._ffmpeg_process is None:
            return False

        if not self._frame_queue.put(frame):
            # Queue was full -> oldest frame evicted to make room.
            self.stats.dropped_frames += 1

        return True

    def _write_loop(self):
        """Writer thread: resize/convert each queued frame and write to FFmpeg.

        All the heavy/blocking work (resize, tobytes, stdin.write) lives here so
        the capture thread stays free. A full OS pipe buffer only blocks THIS
        thread; the bounded queue drops stale frames in the meantime.
        """
        import cv2
        while self._writer_running:
            frame = self._frame_queue.get(timeout=0.5)
            if frame is None:
                continue

            try:
                # Resize if needed
                if frame.shape[1] != self.config.width or frame.shape[0] != self.config.height:
                    frame = cv2.resize(frame, (self.config.width, self.config.height))

                # Ensure BGR24 format
                if len(frame.shape) == 2:  # Grayscale
                    frame = cv2.cvtColor(frame, cv2.COLOR_GRAY2BGR)
                elif frame.shape[2] == 4:  # BGRA
                    frame = frame[:, :, :3]

                frame_bytes = frame.tobytes()

                with self._lock:
                    if self._ffmpeg_process and self._ffmpeg_process.stdin:
                        self._ffmpeg_process.stdin.write(frame_bytes)

                        # Periodic flush for low latency
                        if self.stats.frames_sent % 15 == 0:
                            self._ffmpeg_process.stdin.flush()

                # Update stats
                current_time = time.time()
                self.stats.frames_sent += 1
                self.stats.bytes_sent += len(frame_bytes)
                self.stats.last_frame_time = current_time
                self.stats.record_frame(current_time)

            except BrokenPipeError:
                logger.warning("FFmpeg pipe broken")
                self._dump_ffmpeg_error()
                # Bounded, backed-off reconnect. The FrameQueue keeps
                # absorbing/dropping frames from stream() the whole time --
                # this thread is the only one that ever blocks, and only on
                # itself.
                if self._writer_running and self._reconnect():
                    continue
                self._is_running = False
                break
            except Exception as e:
                logger.error(f"Error streaming frame: {e}")
                self._dump_ffmpeg_error()
                self.stats.dropped_frames += 1

    def _reconnect(self) -> bool:
        """Attempt a bounded, backed-off restart of FFmpeg after the writer
        thread observes the process/pipe has died.

        Runs synchronously ON THE WRITER THREAD (the only thread that ever
        writes to stdin), so there is no lock held across the restart -- the
        per-frame lock in _write_loop only ever guards a single stdin.write.
        Reads/writes of ``self._ffmpeg_process`` here are plain attribute
        assignments (pointer swaps), which is all that's needed since the only
        other readers (``is_running``, ``stream()``) just check identity/None,
        never dereference concurrently with a close.

        Uses ``self._shutdown_event`` (set by stop()) instead of plain
        ``time.sleep`` for the backoff wait, and rechecks it between attempts,
        so a stop() call during reconnect is picked up promptly rather than
        waiting out the remaining backoff/attempts.

        Returns:
            True if FFmpeg came back up and passed its warm-up check within
            ``RECONNECT_MAX_ATTEMPTS`` (bumping ``reconnect_count``). False if
            attempts were exhausted or a shutdown was requested meanwhile --
            the caller (the BrokenPipeError handler in ``_write_loop``) is
            responsible for then setting ``_is_running = False`` permanently.
        """
        self._cleanup_ffmpeg()  # reap the dead process before retrying

        backoff = self.RECONNECT_INITIAL_BACKOFF
        for attempt in range(1, self.RECONNECT_MAX_ATTEMPTS + 1):
            if not self._writer_running or self._shutdown_event.is_set():
                logger.warning("  Reconnect abandoned: streamer is stopping")
                return False

            logger.warning(f"  Reconnecting to FFmpeg (attempt {attempt}/{self.RECONNECT_MAX_ATTEMPTS}) "
                           f"in {backoff:.1f}s...")
            if self._shutdown_event.wait(backoff):
                # stop() set the event while we were waiting -- bail out now.
                logger.warning("  Reconnect abandoned: stop() requested")
                return False
            if not self._writer_running:
                return False

            try:
                hw_type = HWAccel(self._active_hw_accel) if self._active_hw_accel else HWAccel.CPU
                self._ffmpeg_stderr_buffer = []
                self._start_ffmpeg(self._rtmp_url, self._rtmp_url_sub, hw_type)
                if (self._check_ffmpeg_running(timeout=self.RECONNECT_CHECK_TIMEOUT)
                        and self._warm_up_encoder(timeout=self.RECONNECT_WARMUP_TIMEOUT)):
                    self.reconnect_count += 1
                    logger.info(f"  [OK] FFmpeg reconnected (attempt {attempt})")
                    return True
                logger.warning(f"  [FAIL] Reconnect attempt {attempt} did not come up")
            except Exception as e:
                logger.warning(f"  [FAIL] Reconnect attempt {attempt} raised: {e}")

            self._cleanup_ffmpeg()
            backoff = min(backoff * 2, self.RECONNECT_MAX_BACKOFF)

        logger.error(f"  [FAIL] FFmpeg reconnect exhausted after {self.RECONNECT_MAX_ATTEMPTS} attempts; giving up")
        return False

    def _check_hw_encoder_available(self, hw_type: HWAccel) -> bool:
        """Quick check if a hardware encoder is available"""
        if hw_type == HWAccel.CPU:
            return True
        
        encoder_name = {
            HWAccel.NVENC: "h264_nvenc",
            HWAccel.QSV: "h264_qsv",
        }.get(hw_type)
        
        if not encoder_name:
            return True
        
        try:
            # Just check if the encoder is compiled in - don't try to init it
            # The actual init can hang on Windows, so we'll let streaming handle failures
            list_cmd = ["ffmpeg", "-hide_banner", "-encoders"]
            list_result = subprocess.run(
                list_cmd,
                capture_output=True,
                timeout=5.0,
                creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, 'CREATE_NO_WINDOW') else 0
            )
            return encoder_name in list_result.stdout.decode('utf-8', errors='ignore')
        except Exception:
            return False
    
    def _start_ffmpeg(self, rtmp_url: str, rtmp_url_sub: Optional[str], hw_type: HWAccel):
        """Start FFmpeg with the specified hardware acceleration"""
        
        hw_configs = {
            HWAccel.NVENC: {
                "codec": "h264_nvenc",
                "extra_input": [],
                "extra_encode": [
                    "-gpu", "0",
                    "-preset", "p1",
                    "-rc", "cbr",
                    "-bf", "0",
                ],
            },
            HWAccel.QSV: {
                "codec": "h264_qsv",
                "extra_input": [],
                "extra_encode": [
                    "-preset", "faster",
                    "-global_quality", "20",
                    "-look_ahead", "0",
                    "-bf", "0",
                ],
            },
            HWAccel.CPU: {
                "codec": "libx264",
                "extra_input": [],
                "extra_encode": [
                    "-preset", "faster",
                    "-crf", "20",
                    "-tune", "zerolatency",
                    "-bf", "0",
                ],
            },
        }
        
        config = hw_configs[hw_type]
        
        # Build FFmpeg command
        cmd = [
            "ffmpeg",
            "-y",
            "-f", "rawvideo",
            "-vcodec", "rawvideo",
            "-s", f"{self.config.width}x{self.config.height}",
            "-pix_fmt", "bgr24",
            "-r", str(self.config.fps),
        ]
        
        # Hardware-specific input args
        cmd.extend(config["extra_input"])
        cmd.extend(["-i", "-"])
        
        # Encoding settings
        cmd.extend([
            "-c:v", config["codec"],
            "-pix_fmt", "yuv420p",
        ])
        cmd.extend(config["extra_encode"])
        
        # Common output settings
        cmd.extend([
            "-g", str(self.config.keyframe_interval),
            "-b:v", self.config.bitrate,
            "-maxrate", self.config.bitrate,
            "-bufsize", "1M",  # Smaller buffer for faster startup
            "-avoid_negative_ts", "make_zero",
            "-fflags", "+genpts+flush_packets",
        ])

        if rtmp_url.startswith("rtsp://"):
             cmd.extend([
                "-f", "rtsp",
                "-rtsp_transport", "tcp",
                rtmp_url,
            ])
        else:
            cmd.extend([
                "-flags", "+global_header",
                "-f", "flv",
                rtmp_url,
            ])
        
        # Add substream output if requested
        if rtmp_url_sub:
            cmd.extend([
                "-c:v", config["codec"],
                "-pix_fmt", "yuv420p",
            ])
            cmd.extend(config["extra_encode"])
            
            cmd.extend([
                "-s", f"{self.config.sub_width}x{self.config.sub_height}",
                "-b:v", self.config.sub_bitrate,
                "-g", str(self.config.keyframe_interval),
            ])

            if rtmp_url_sub.startswith("rtsp://"):
                cmd.extend([
                    "-f", "rtsp",
                    "-rtsp_transport", "tcp",
                    rtmp_url_sub,
                ])
            else:
                cmd.extend([
                    "-flags", "+global_header",
                    "-f", "flv",
                    rtmp_url_sub,
                ])
        
        # Start FFmpeg. stdout is intentionally DEVNULL: nothing ever consumes
        # it, and if it were a PIPE instead, FFmpeg could fill the OS pipe
        # buffer and block forever on a write to its own stdout -- a latent
        # deadlock. stderr IS consumed (by the reader thread below), so PIPE
        # is safe there.
        self._ffmpeg_process = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, 'CREATE_NO_WINDOW') else 0
        )

        # Store stderr for error checking
        self._ffmpeg_stderr_buffer = []

        def read_stderr():
            """Read stderr in background to detect errors early.

            Exits on EOF (readline() returns b'' once the process dies and its
            stderr pipe closes), so it never outlives the process it reads
            from. The handle is stored on self so stop() can join it.
            """
            if self._ffmpeg_process and self._ffmpeg_process.stderr:
                try:
                    for line in iter(self._ffmpeg_process.stderr.readline, b''):
                        if line:
                            self._ffmpeg_stderr_buffer.append(line)
                            # Keep buffer from growing too large
                            if len(self._ffmpeg_stderr_buffer) > 100:
                                self._ffmpeg_stderr_buffer.pop(0)
                except Exception:
                    pass

        # Start stderr reader thread (handle stored so stop() can join it).
        self._stderr_thread = threading.Thread(
            target=read_stderr, name="ffmpeg-stderr-reader", daemon=True
        )
        self._stderr_thread.start()
    
    def _warm_up_encoder(self, timeout: float = 5.0) -> bool:
        """Send test frames to verify encoder works before declaring success"""
        try:
            # Create a black test frame
            test_frame = np.zeros((self.config.height, self.config.width, 3), dtype=np.uint8)
            frame_bytes = test_frame.tobytes()
            
            start_time = time.time()
            frames_sent = 0
            
            # Send test frames with timeout
            while frames_sent < 3 and (time.time() - start_time) < timeout:
                if self._ffmpeg_process and self._ffmpeg_process.stdin:
                    try:
                        self._ffmpeg_process.stdin.write(frame_bytes)
                        self._ffmpeg_process.stdin.flush()
                        frames_sent += 1
                    except (BrokenPipeError, OSError):
                        return False
                    
                    time.sleep(0.05)
                    
                    # Check if process died
                    if self._ffmpeg_process.poll() is not None:
                        return False
                    
                    # Check for encoder errors in stderr
                    if self._ffmpeg_stderr_buffer:
                        stderr_text = b''.join(self._ffmpeg_stderr_buffer).decode('utf-8', errors='ignore').lower()
                        error_patterns = [
                            'driver does not support',
                            'could not open encoder',
                            'error while opening encoder',
                            'error sending frames',
                            'conversion failed',
                            'cannot load',
                            'no nvenc capable',
                            'mfx_load_plugin',
                        ]
                        for pattern in error_patterns:
                            if pattern in stderr_text:
                                return False
                else:
                    return False
            
            if frames_sent < 3:
                return False  # Timed out
            
            return True
        except Exception as e:
            logger.error(f"  Warm-up failed: {e}")
            return False
    
    def _check_ffmpeg_running(self, timeout: float = 2.0) -> bool:
        """Check if FFmpeg started successfully with timeout"""
        if self._ffmpeg_process is None:
            return False
        
        # Check periodically for errors
        start_time = time.time()
        check_interval = 0.2
        
        while time.time() - start_time < timeout:
            # Check if process has exited with error
            if self._ffmpeg_process.poll() is not None:
                self._dump_ffmpeg_error()
                return False
            
            # Check stderr buffer for critical errors
            if hasattr(self, '_ffmpeg_stderr_buffer'):
                stderr_text = b''.join(self._ffmpeg_stderr_buffer).decode('utf-8', errors='ignore').lower()
                
                # Look for specific error patterns that indicate failure
                error_patterns = [
                    'no nvenc capable devices found',
                    'driver does not support',
                    'required nvenc api version',
                    'minimum required nvidia driver',
                    'could not open encoder',
                    'error while opening encoder',
                    'cannot load',
                    'invalid encoder',
                    'unknown encoder',
                    'encoder not found',
                    'unsupported codec',
                    'init failed',
                    'initialization failed',
                    'conversion failed',
                    'nothing was written into output file',
                ]
                
                for pattern in error_patterns:
                    if pattern in stderr_text:
                        logger.warning(f"  Detected error: {pattern}")
                        return False
            
            time.sleep(check_interval)
        
        # After timeout, check one final time if process is still alive
        if self._ffmpeg_process.poll() is not None:
            return False
        
        return True
    
    def _dump_ffmpeg_error(self):
        """Log FFmpeg stderr for debugging"""
        if hasattr(self, '_ffmpeg_stderr_buffer') and self._ffmpeg_stderr_buffer:
            stderr_text = b''.join(self._ffmpeg_stderr_buffer).decode('utf-8', errors='ignore')
            if stderr_text.strip():
                logger.error(f"FFmpeg error output:\n{stderr_text}")
        elif self._ffmpeg_process and self._ffmpeg_process.stderr:
            try:
                # Try to read remaining stderr
                stderr = self._ffmpeg_process.stderr.read()
                if stderr:
                    logger.error(f"FFmpeg error output:\n{stderr.decode('utf-8', errors='ignore')}")
            except Exception as e:
                logger.error(f"Could not read FFmpeg stderr: {e}")
    
    def _cleanup_ffmpeg(self):
        """Clean up FFmpeg process"""
        if self._ffmpeg_process:
            try:
                if self._ffmpeg_process.stdin:
                    self._ffmpeg_process.stdin.close()
                self._ffmpeg_process.terminate()
                self._ffmpeg_process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self._ffmpeg_process.kill()
            except Exception:
                pass
            finally:
                self._ffmpeg_process = None
