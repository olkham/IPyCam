#!/usr/bin/env python3
"""
Native Python WebRTC Streamer using aiortc

Provides a WebRTC fallback when go2rtc is not available.
Uses aiortc for pure Python WebRTC implementation with video streaming.
"""

import asyncio
import logging
import threading
import time
import fractions
from dataclasses import dataclass, field
from typing import Optional, Dict, Set, Any
from collections import deque

import numpy as np

# aiortc imports - these are optional dependencies
try:
    from aiortc import RTCPeerConnection, RTCSessionDescription, VideoStreamTrack
    from aiortc.contrib.media import MediaRelay
    from av import VideoFrame
    AIORTC_AVAILABLE = True
except ImportError:
    AIORTC_AVAILABLE = False
    RTCPeerConnection = None
    RTCSessionDescription = None
    VideoStreamTrack = None
    MediaRelay = None
    VideoFrame = None

logger = logging.getLogger("ipycam.webrtc")


def check_aiortc_available() -> bool:
    """Check if aiortc is installed and available."""
    return AIORTC_AVAILABLE


def is_webrtc_available() -> bool:
    """
    Check if native WebRTC streaming is available.
    
    Returns:
        True if aiortc is installed and can be used
    """
    return AIORTC_AVAILABLE


@dataclass
class WebRTCStats:
    """Statistics for WebRTC streaming"""
    frames_sent: int = 0
    connections_total: int = 0
    connections_active: int = 0
    start_time: float = field(default_factory=time.time)
    _frame_timestamps: deque = field(default_factory=lambda: deque(maxlen=150))
    _window_seconds: float = 5.0
    
    @property
    def elapsed_time(self) -> float:
        return time.time() - self.start_time
    
    @property
    def actual_fps(self) -> float:
        """Calculate FPS over a sliding window of recent frames"""
        if len(self._frame_timestamps) < 2:
            return 0
        
        current_time = time.time()
        cutoff_time = current_time - self._window_seconds
        recent_frames = sum(1 for ts in self._frame_timestamps if ts >= cutoff_time)
        
        if recent_frames < 2:
            return 0
        
        oldest_in_window = next((ts for ts in self._frame_timestamps if ts >= cutoff_time), None)
        if oldest_in_window is None:
            return 0
        
        time_span = current_time - oldest_in_window
        if time_span > 0:
            return recent_frames / time_span
        return 0
    
    def record_frame(self):
        """Record a frame timestamp for FPS calculation"""
        self._frame_timestamps.append(time.time())
        self.frames_sent += 1


# Placeholder/stub class when aiortc is not available
class _NativeWebRTCStreamerUnavailable:
    """Stub class used when aiortc is not installed."""
    
    def __init__(self, fps: int = 30, width: int = 1920, height: int = 1080):
        raise ImportError(
            "aiortc is not installed. Install it with: pip install aiortc aiohttp"
        )


# Only define the real implementation if aiortc is available
if AIORTC_AVAILABLE:
    
    class SharedFrameBuffer:
        """
        Shared frame buffer that multiple video tracks can read from.
        Updated by the main camera loop, read by WebRTC tracks.
        """
        def __init__(self):
            self._frame = None
            self._frame_lock = threading.Lock()
            self._frame_count = 0
        
        def update(self, frame: np.ndarray):
            """Update the current frame."""
            with self._frame_lock:
                self._frame = frame.copy()
                self._frame_count += 1
        
        def get(self):
            """Get the current frame (or None if not available)."""
            with self._frame_lock:
                return self._frame.copy() if self._frame is not None else None
    
    
    class CameraVideoTrack(VideoStreamTrack):
        """
        A video stream track that reads from a shared frame buffer.
        Each peer connection gets its own track instance.
        """
        
        kind = "video"
        
        def __init__(self, frame_buffer: SharedFrameBuffer, fps: int = 30, width: int = 1920, height: int = 1080):
            super().__init__()
            self._frame_buffer = frame_buffer
            self.fps = fps
            self.width = width
            self.height = height
            self._start_time = None
            self._frame_count = 0
            self._running = True
        
        def stop(self):
            """Stop the video track."""
            self._running = False
        
        async def recv(self):
            """
            Receive the next video frame.
            
            This method is called by aiortc when it needs the next frame to send.
            """
            if self._start_time is None:
                self._start_time = time.time()
            
            # Wait for a frame if none available (non-blocking async wait)
            frame_data = None
            frame_interval = 1.0 / self.fps
            
            while self._running:
                frame_data = self._frame_buffer.get()
                if frame_data is not None:
                    break
                
                # Use asyncio.sleep instead of blocking wait to not block the event loop
                await asyncio.sleep(frame_interval)
            
            if not self._running or frame_data is None:
                # Return a black frame when stopping
                frame_data = np.zeros((self.height, self.width, 3), dtype=np.uint8)
            
            # Convert BGR (OpenCV) to RGB for av/aiortc
            if frame_data.shape[2] == 3:
                frame_rgb = frame_data[:, :, ::-1]  # BGR to RGB
            else:
                frame_rgb = frame_data
            
            # Create VideoFrame from numpy array
            video_frame = VideoFrame.from_ndarray(frame_rgb, format="rgb24")
            
            # Calculate timestamp
            pts = int(self._frame_count * (1 / self.fps) * 90000)  # 90kHz clock
            video_frame.pts = pts
            video_frame.time_base = fractions.Fraction(1, 90000)
            
            self._frame_count += 1
            
            return video_frame

    
    class NativeWebRTCStreamer:
        """
        Native Python WebRTC streamer using aiortc.
        
        This provides WebRTC streaming without requiring go2rtc,
        using pure Python implementation via aiortc library.
        """
        
        def __init__(self, fps: int = 30, width: int = 1920, height: int = 1080):
            """
            Initialize the WebRTC streamer.
            
            Args:
                fps: Target frames per second
                width: Video width
                height: Video height
            """
            self.fps = fps
            self.width = width
            self.height = height
            
            # Shared frame buffer - all tracks read from this
            self._frame_buffer = SharedFrameBuffer()
            self._video_tracks: Set[CameraVideoTrack] = set()
            self._peer_connections: Set = set()
            self._lock = threading.Lock()
            self._is_running = False
            self._loop = None
            self._loop_thread = None
            self._loop_ready = threading.Event()  # Signal when event loop is ready
            
            self.stats = WebRTCStats()
            
        def start(self) -> bool:
            """Start the WebRTC streamer and its event loop."""
            if self._is_running:
                return True
            
            try:
                # Start asyncio event loop in a background thread
                self._loop = asyncio.new_event_loop()
                self._loop_ready.clear()
                self._loop_thread = threading.Thread(
                    target=self._run_event_loop,
                    daemon=True,
                    name="webrtc-event-loop"
                )
                self._loop_thread.start()
                
                # Wait for the event loop to be ready
                if not self._loop_ready.wait(timeout=5.0):
                    raise RuntimeError("Event loop failed to start")
                
                self._is_running = True
                self.stats = WebRTCStats()
                
                logger.info("Native WebRTC streamer started")
                print("  ✓ Native WebRTC event loop started successfully")
                return True
                
            except Exception as e:
                logger.error(f"Failed to start WebRTC streamer: {e}")
                self._cleanup()
                return False
        
        def stop(self):
            """Stop the WebRTC streamer."""
            self._is_running = False
            
            # Stop all video tracks
            with self._lock:
                for track in self._video_tracks:
                    track.stop()
                self._video_tracks.clear()
            
            # Close all peer connections
            if self._loop:
                try:
                    asyncio.run_coroutine_threadsafe(
                        self._close_all_connections(),
                        self._loop
                    ).result(timeout=5.0)
                except Exception:
                    pass
            
            self._cleanup()
            logger.info("Native WebRTC streamer stopped")
        
        def _cleanup(self):
            """Clean up resources."""
            if self._loop:
                self._loop.call_soon_threadsafe(self._loop.stop)
            
            if self._loop_thread and self._loop_thread.is_alive():
                self._loop_thread.join(timeout=2.0)
            
            self._loop = None
            self._loop_thread = None
        
        def _run_event_loop(self):
            """Run the asyncio event loop in a background thread."""
            asyncio.set_event_loop(self._loop)
            # Signal that the loop is ready to accept tasks
            self._loop.call_soon(self._loop_ready.set)
            try:
                self._loop.run_forever()
            finally:
                # Clean up remaining tasks
                pending = asyncio.all_tasks(self._loop)
                for task in pending:
                    task.cancel()
                self._loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
                self._loop.close()
        
        async def _close_all_connections(self):
            """Close all peer connections."""
            with self._lock:
                pcs = list(self._peer_connections)
                tracks = list(self._video_tracks)
            
            # Stop all tracks
            for track in tracks:
                track.stop()
            
            # Close all peer connections
            for pc in pcs:
                try:
                    await pc.close()
                except Exception as e:
                    logger.debug(f"Error closing peer connection: {e}")
            
            with self._lock:
                self._peer_connections.clear()
                self._video_tracks.clear()
                self.stats.connections_active = 0
        
        @property
        def is_running(self) -> bool:
            """Check if the streamer is running."""
            return self._is_running
        
        @property
        def connection_count(self) -> int:
            """Get the number of active peer connections."""
            with self._lock:
                return len(self._peer_connections)
        
        def stream_frame(self, frame: np.ndarray) -> bool:
            """
            Update the current frame to be sent to WebRTC peers.
            
            Args:
                frame: NumPy array in BGR format (OpenCV default)
                
            Returns:
                True if frame was accepted
            """
            if not self._is_running:
                return False
            
            self._frame_buffer.update(frame)
            self.stats.record_frame()
            
            return True
        
        async def handle_offer(self, sdp: str, type_: str = "offer") -> Dict[str, Any]:
            """
            Handle an incoming WebRTC offer and return an answer.
            
            Args:
                sdp: The SDP offer from the client
                type_: The type of the session description (usually "offer")
                
            Returns:
                Dictionary with 'sdp' and 'type' for the answer
            """
            if not self._is_running:
                raise RuntimeError("WebRTC streamer is not running")
            
            logger.debug("Processing WebRTC offer...")
            
            offer = RTCSessionDescription(sdp=sdp, type=type_)
            
            pc = RTCPeerConnection()
            
            # Create a new video track for this connection
            video_track = CameraVideoTrack(
                frame_buffer=self._frame_buffer,
                fps=self.fps,
                width=self.width,
                height=self.height
            )
            
            @pc.on("connectionstatechange")
            async def on_connectionstatechange():
                logger.debug(f"Connection state: {pc.connectionState}")
                if pc.connectionState == "failed" or pc.connectionState == "closed":
                    await self._remove_peer_connection(pc, video_track)
            
            @pc.on("iceconnectionstatechange")
            async def on_iceconnectionstatechange():
                logger.debug(f"ICE connection state: {pc.iceConnectionState}")
                if pc.iceConnectionState == "failed":
                    await self._remove_peer_connection(pc, video_track)
            
            # Add the video track to the peer connection
            pc.addTrack(video_track)
            logger.debug("New video track created and added to peer connection")
            
            # Set remote description
            logger.debug("Setting remote description...")
            await pc.setRemoteDescription(offer)
            
            # Create answer
            logger.debug("Creating answer...")
            answer = await pc.createAnswer()
            logger.debug("Setting local description...")
            await pc.setLocalDescription(answer)
            
            # Store peer connection and track
            with self._lock:
                self._peer_connections.add(pc)
                self._video_tracks.add(video_track)
                self.stats.connections_total += 1
                self.stats.connections_active = len(self._peer_connections)
            
            logger.info(f"New WebRTC connection established (total: {self.connection_count})")
            
            return {
                "sdp": pc.localDescription.sdp,
                "type": pc.localDescription.type
            }
        
        async def _remove_peer_connection(self, pc, video_track=None):
            """Remove a peer connection and its track from the set."""
            with self._lock:
                if pc in self._peer_connections:
                    self._peer_connections.discard(pc)
                if video_track and video_track in self._video_tracks:
                    video_track.stop()
                    self._video_tracks.discard(video_track)
                self.stats.connections_active = len(self._peer_connections)
            
            try:
                await pc.close()
            except Exception:
                pass
            
            logger.debug(f"Peer connection removed (remaining: {self.connection_count})")
        
        def handle_offer_sync(self, sdp: str, type_: str = "offer") -> Dict[str, Any]:
            """
            Synchronous wrapper for handle_offer.
            
            Use this from non-async code (e.g., HTTP handler).
            """
            if not self._is_running or not self._loop:
                raise RuntimeError("WebRTC streamer is not running")
            
            # Wait for event loop to be ready (should already be set)
            if not self._loop_ready.wait(timeout=5.0):
                raise RuntimeError("WebRTC event loop is not ready")
            
            future = asyncio.run_coroutine_threadsafe(
                self.handle_offer(sdp, type_),
                self._loop
            )
            
            # Wait for result with timeout
            try:
                return future.result(timeout=30.0)  # Increased timeout for ICE gathering
            except TimeoutError:
                logger.error("WebRTC offer handling timed out")
                raise RuntimeError("WebRTC offer handling timed out - check network configuration")
        
        async def close_connection(self, pc_id: Optional[str] = None):
            """
            Close a specific peer connection or all connections.
            
            Args:
                pc_id: Optional peer connection ID. If None, closes all.
            """
            if pc_id is None:
                await self._close_all_connections()
            # Note: For specific connection closing, we'd need to track IDs
        
        def close_connection_sync(self, pc_id: Optional[str] = None):
            """Synchronous wrapper for close_connection."""
            if not self._is_running or not self._loop:
                return
            
            future = asyncio.run_coroutine_threadsafe(
                self.close_connection(pc_id),
                self._loop
            )
            
            try:
                future.result(timeout=5.0)
            except Exception:
                pass

else:
    # aiortc not available - use stub class
    NativeWebRTCStreamer = _NativeWebRTCStreamerUnavailable
    CameraVideoTrack = None
