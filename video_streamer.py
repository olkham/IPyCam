#!/usr/bin/env python3
"""
Modular Video Streamer Class

A clean, reusable video streamer that accepts frames from any source
and pushes them to an RTMP endpoint (e.g., go2rtc) for RTSP redistribution.

Usage:
    streamer = VideoStreamer(width=1920, height=1080, fps=30)
    streamer.start("rtmp://127.0.0.1:1935/video")
    
    while running:
        frame = get_frame_from_somewhere()  # Your frame source
        streamer.stream(frame)
    
    streamer.stop()
"""

import subprocess
import time
import threading
import numpy as np
from typing import Optional, Literal, Tuple
from dataclasses import dataclass, field
from enum import Enum


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
    hw_accel: HWAccel = HWAccel.AUTO
    # Substream settings (used when rtmp_url_sub is provided)
    sub_width: int = 640
    sub_height: int = 480
    sub_bitrate: str = "1M"
    
    def __post_init__(self):
        if self.keyframe_interval is None:
            self.keyframe_interval = self.fps


@dataclass
class StreamStats:
    """Statistics for the current stream"""
    frames_sent: int = 0
    bytes_sent: int = 0
    start_time: float = field(default_factory=time.time)
    last_frame_time: float = 0
    dropped_frames: int = 0
    
    @property
    def elapsed_time(self) -> float:
        return time.time() - self.start_time
    
    @property
    def actual_fps(self) -> float:
        if self.elapsed_time > 0:
            return self.frames_sent / self.elapsed_time
        return 0
    
    @property
    def bitrate_mbps(self) -> float:
        if self.elapsed_time > 0:
            return (self.bytes_sent * 8) / (self.elapsed_time * 1_000_000)
        return 0


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
            try:
                self._start_ffmpeg(rtmp_url, rtmp_url_sub, hw_type)
                if self._check_ffmpeg_running():
                    self._active_hw_accel = hw_type.value
                    self._is_running = True
                    print(f"✓ Streamer started with {hw_type.value.upper()} acceleration")
                    print(f"  Primary:   {rtmp_url}")
                    if rtmp_url_sub:
                        print(f"  Substream: {rtmp_url_sub}")
                    return True
                else:
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
            self.stats.frames_sent += 1
            self.stats.bytes_sent += len(frame_bytes)
            self.stats.last_frame_time = time.time()
            
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
            "-flags", "+global_header",
            "-avoid_negative_ts", "make_zero",
            "-fflags", "+genpts+flush_packets",
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
        )
    
    def _check_ffmpeg_running(self) -> bool:
        """Check if FFmpeg started successfully"""
        if self._ffmpeg_process is None:
            return False
        time.sleep(1.0)
        if self._ffmpeg_process.poll() is not None:
            self._dump_ffmpeg_error()
            return False
        return True
    
    def _dump_ffmpeg_error(self):
        """Print FFmpeg stderr for debugging"""
        if self._ffmpeg_process and self._ffmpeg_process.stderr:
            try:
                # Non-blocking read of available stderr
                import select
                if hasattr(select, 'select'):
                    # Unix-like
                    pass
                # On Windows, just try to read what's available
                self._ffmpeg_process.stderr.flush()
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


# Example usage and test
if __name__ == "__main__":
    import cv2
    
    print("Video Streamer Test")
    print("===================")
    
    # Create streamer with custom config
    config = StreamConfig(
        width=1920,
        height=1080,
        fps=60,
        bitrate="4M",
        hw_accel=HWAccel.AUTO,
    )
    
    streamer = VideoStreamer(config)
    
    # Start streaming
    if not streamer.start(
        rtmp_url="rtmp://127.0.0.1:1935/video_main",
        rtmp_url_sub="rtmp://127.0.0.1:1935/video_sub"
    ):
        print("Failed to start streamer. Is go2rtc running?")
        exit(1)
    
    print("\nStreaming from vid2.mkv...")
    print("View at: rtsp://localhost:8554/video")
    print("Press Ctrl+C to stop\n")
    
    # Open video file as test source
    cap = cv2.VideoCapture("vid2.mkv")
    if not cap.isOpened():
        print("Could not open vid2.mkv")
        streamer.stop()
        exit(1)
    
    # Precise frame timing variables
    target_frame_duration = 1.0 / config.fps
    stream_start_time = time.time()
    
    try:
        while streamer.is_running:
            frame_start = time.time()
            
            ret, frame = cap.read()
            if not ret:
                # Loop video
                cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                continue
            
            # Add timestamp overlay
            timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
            cv2.putText(frame, timestamp, (10, 30),
                       cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 255), 2)
            
            # Stream the frame
            if not streamer.stream(frame):
                break
            
            # Print stats every 5 seconds
            if streamer.stats.frames_sent % (config.fps * 5) == 0:
                print(f"Frames: {streamer.stats.frames_sent}, "
                      f"FPS: {streamer.stats.actual_fps:.1f}")
            
            # Precise frame pacing with drift correction
            expected_time = stream_start_time + (streamer.stats.frames_sent * target_frame_duration)
            sleep_time = expected_time - time.time()
            if sleep_time > 0:
                time.sleep(sleep_time)
            
    except KeyboardInterrupt:
        print("\nInterrupted by user")
    finally:
        cap.release()
        streamer.stop()
