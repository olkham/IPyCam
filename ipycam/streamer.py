#!/usr/bin/env python3
"""
Modular Video Streamer Class

A clean, reusable video streamer that accepts frames from any source
and pushes them to an RTMP endpoint (e.g., go2rtc) for RTSP redistribution.
"""

import subprocess
import time
import threading
import numpy as np
from typing import Optional, Tuple, Deque
from dataclasses import dataclass, field
from enum import Enum
from collections import deque


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
        self._ffmpeg_stderr_buffer = []
        
    @property
    def is_running(self) -> bool:
        """Check if the streamer is currently active"""
        return self._is_running and self._ffmpeg_process is not None
    
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
            print("Streamer is already running")
            return False
            
        self._rtmp_url = rtmp_url
        self.stats = StreamStats()
        
        # Try hardware acceleration options
        if self.config.hw_accel == HWAccel.AUTO:
            hw_order = [HWAccel.NVENC, HWAccel.QSV, HWAccel.CPU]
        else:
            hw_order = [self.config.hw_accel]
        
        for hw_type in hw_order:
            print(f"Trying {hw_type.value.upper()}...")
            try:
                # Reset stderr buffer for this attempt
                self._ffmpeg_stderr_buffer = []
                
                self._start_ffmpeg(rtmp_url, rtmp_url_sub, hw_type)
                if self._check_ffmpeg_running():
                    # Warm-up: Send test frames to verify encoder actually works
                    if self._warm_up_encoder():
                        self._active_hw_accel = hw_type.value
                        self._is_running = True
                        print(f"✓ Streamer started with {hw_type.value.upper()} acceleration")
                        print(f"  Primary:   {rtmp_url}")
                        if rtmp_url_sub:
                            print(f"  Substream: {rtmp_url_sub}")
                        return True
                    else:
                        print(f"✗ {hw_type.value.upper()} encoder initialization failed")
                        self._cleanup_ffmpeg()
                else:
                    print(f"✗ {hw_type.value.upper()} failed to start")
                    self._cleanup_ffmpeg()
            except Exception as e:
                print(f"✗ {hw_type.value.upper()} failed: {e}")
                self._cleanup_ffmpeg()
                
        print("✗ Failed to start streamer with any hardware acceleration")
        return False
    
    def stop(self):
        """Stop the streaming pipeline and clean up resources"""
        self._is_running = False
        self._cleanup_ffmpeg()
        print(f"Streamer stopped. Stats: {self.stats.frames_sent} frames, "
              f"{self.stats.actual_fps:.1f} avg fps, "
              f"{self.stats.bitrate_mbps:.2f} Mbps avg")
    
    def stream(self, frame: np.ndarray) -> bool:
        """
        Send a single frame to the stream.
        
        Args:
            frame: NumPy array in BGR format (OpenCV default).
                   Must match the configured width/height or will be resized.
                   
        Returns:
            True if frame was sent successfully, False otherwise
        """
        if not self._is_running or self._ffmpeg_process is None:
            return False
        
        try:
            # Resize if needed
            if frame.shape[1] != self.config.width or frame.shape[0] != self.config.height:
                import cv2
                frame = cv2.resize(frame, (self.config.width, self.config.height))
            
            # Ensure BGR24 format
            if len(frame.shape) == 2:  # Grayscale
                import cv2
                frame = cv2.cvtColor(frame, cv2.COLOR_GRAY2BGR)
            elif frame.shape[2] == 4:  # BGRA
                frame = frame[:, :, :3]
            
            # Send to FFmpeg
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
            
            return True
            
        except BrokenPipeError:
            print("FFmpeg pipe broken")
            self._dump_ffmpeg_error()
            self._is_running = False
            return False
        except Exception as e:
            print(f"Error streaming frame: {e}")
            self._dump_ffmpeg_error()
            self.stats.dropped_frames += 1
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
            "-bufsize", "8M",
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
        
        # Start FFmpeg
        self._ffmpeg_process = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, 'CREATE_NO_WINDOW') else 0
        )
        
        # Store stderr for error checking
        self._ffmpeg_stderr_buffer = []
        
        def read_stderr():
            """Read stderr in background to detect errors early"""
            if self._ffmpeg_process and self._ffmpeg_process.stderr:
                try:
                    for line in iter(self._ffmpeg_process.stderr.readline, b''):
                        if line:
                            self._ffmpeg_stderr_buffer.append(line)
                            # Keep buffer from growing too large
                            if len(self._ffmpeg_stderr_buffer) > 100:
                                self._ffmpeg_stderr_buffer.pop(0)
                except:
                    pass
        
        # Start stderr reader thread
        stderr_thread = threading.Thread(target=read_stderr, daemon=True)
        stderr_thread.start()
    
    def _warm_up_encoder(self) -> bool:
        """Send test frames to verify encoder works before declaring success"""
        try:
            # Create a black test frame
            test_frame = np.zeros((self.config.height, self.config.width, 3), dtype=np.uint8)
            frame_bytes = test_frame.tobytes()
            
            # Send 3 test frames
            for i in range(3):
                if self._ffmpeg_process and self._ffmpeg_process.stdin:
                    self._ffmpeg_process.stdin.write(frame_bytes)
                    self._ffmpeg_process.stdin.flush()
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
                        ]
                        for pattern in error_patterns:
                            if pattern in stderr_text:
                                return False
            
            return True
        except Exception as e:
            print(f"  Warm-up failed: {e}")
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
                        print(f"  Detected error: {pattern}")
                        return False
            
            time.sleep(check_interval)
        
        # After timeout, check one final time if process is still alive
        if self._ffmpeg_process.poll() is not None:
            return False
        
        return True
    
    def _dump_ffmpeg_error(self):
        """Print FFmpeg stderr for debugging"""
        if hasattr(self, '_ffmpeg_stderr_buffer') and self._ffmpeg_stderr_buffer:
            stderr_text = b''.join(self._ffmpeg_stderr_buffer).decode('utf-8', errors='ignore')
            if stderr_text.strip():
                print(f"FFmpeg error output:\n{stderr_text}")
        elif self._ffmpeg_process and self._ffmpeg_process.stderr:
            try:
                # Try to read remaining stderr
                stderr = self._ffmpeg_process.stderr.read()
                if stderr:
                    print(f"FFmpeg error output:\n{stderr.decode('utf-8', errors='ignore')}")
            except Exception as e:
                print(f"Could not read FFmpeg stderr: {e}")
    
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
