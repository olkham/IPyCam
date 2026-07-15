#!/usr/bin/env python3
"""
Native Python MJPEG Streamer

Provides a fallback MJPEG stream when go2rtc is not available.
This allows the camera to work without external dependencies, 
though with reduced functionality (no RTSP, WebRTC, etc.)
"""

import io
import time
import logging
import threading
import numpy as np
import cv2
from typing import Optional, List
from dataclasses import dataclass, field
from collections import deque

from .framequeue import FrameQueue

logger = logging.getLogger(__name__)

# Each client buffers a couple of already-encoded frames. Small on purpose:
# a slow client only ever falls a frame or two behind before its OWN oldest
# frames are dropped -- it can never stall the encoder or other clients.
_CLIENT_QUEUE_SIZE = 3

# Fallback sub-stream size used only when the streamer was not given fixed
# sub_width/sub_height AND the incoming frame is too small to reasonably
# halve (see MJPEGStreamer._resolve_sub_size).
_DEFAULT_SUB_SIZE = (640, 360)


@dataclass
class MJPEGClient:
    """Represents a connected MJPEG client.

    Each client owns a bounded, drop-oldest queue of already-encoded MJPEG
    chunks. The streamer's encode worker fans a frame out by ``put``-ing it into
    every client's queue (never blocking); the client's own writer (the HTTP
    connection thread) drains that queue and writes to the socket. A slow client
    therefore only drops ITS OWN frames and cannot block anyone else.
    """
    wfile: io.BufferedWriter
    connected: bool = True
    frames_sent: int = 0
    queue: FrameQueue = field(default_factory=lambda: FrameQueue(maxsize=_CLIENT_QUEUE_SIZE))
    stream: str = 'main'  # 'main' (full resolution) or 'sub' (resized)


class MJPEGStreamer:
    """
    Native Python MJPEG streamer.
    
    Maintains a list of connected clients and broadcasts frames to all of them.
    This is used as a fallback when go2rtc is not available.
    """
    
    BOUNDARY = b"--frame"
    
    def __init__(
        self,
        quality: int = 80,
        queue_size: int = 2,
        sub_width: Optional[int] = None,
        sub_height: Optional[int] = None,
    ):
        """
        Initialize the MJPEG streamer.

        Args:
            quality: JPEG encoding quality (1-100)
            queue_size: Bounded, drop-oldest buffer of raw frames waiting to be
                encoded by the worker thread. Kept tiny so latency stays low.
            sub_width: Optional fixed width (px) to encode the 'sub' stream
                selector at. Defaults to None, which leaves existing
                construction sites unaffected: the sub size is then computed
                dynamically per frame (see ``_resolve_sub_size``). Can also be
                assigned after construction via the ``sub_width`` attribute.
            sub_height: Optional fixed height (px) for the 'sub' stream
                selector. See ``sub_width``.
        """
        self.quality = quality
        self.sub_width = sub_width
        self.sub_height = sub_height
        self._clients: List[MJPEGClient] = []
        self._lock = threading.Lock()
        self._last_frame: Optional[bytes] = None
        self._frame_count = 0
        self._is_running = False

        # Raw frames are handed to a single encode worker via this bounded,
        # drop-oldest queue. stream_frame() only ever enqueues (never encodes or
        # writes to a socket), so the capture thread is fully decoupled from
        # JPEG encoding and from every connected client's socket.
        self._frame_queue: FrameQueue = FrameQueue(maxsize=max(1, queue_size))
        self._worker: Optional[threading.Thread] = None

        # Stats tracking
        self._start_time: Optional[float] = None
        self._frame_timestamps: deque = deque(maxlen=150)
        self._window_seconds: float = 5.0  # Calculate FPS over last 5 seconds

    def start(self) -> bool:
        """Start the MJPEG streamer and its encode worker thread."""
        self._is_running = True
        self._start_time = time.time()
        self._frame_count = 0
        self._frame_timestamps.clear()
        self._frame_queue = FrameQueue(maxsize=self._frame_queue.maxsize)

        # Single worker: dequeue -> JPEG-encode ONCE -> fan the encoded bytes
        # out to every client's own queue. One encode per frame regardless of
        # how many clients are connected.
        self._worker = threading.Thread(
            target=self._encode_loop,
            name="mjpeg-encode-worker",
            daemon=True,
        )
        self._worker.start()
        return True

    def stop(self):
        """Stop the MJPEG streamer, its worker, and disconnect all clients."""
        self._is_running = False

        # Wake the encode worker so it can observe shutdown and exit.
        self._frame_queue.close()
        worker = self._worker
        if worker and worker.is_alive():
            worker.join(timeout=2.0)
        self._worker = None

        with self._lock:
            clients = list(self._clients)
            self._clients.clear()
        # Disconnect each client and wake its writer (blocked on its queue).
        for client in clients:
            client.connected = False
            client.queue.close()
    
    @property
    def is_running(self) -> bool:
        return self._is_running
    
    @property
    def client_count(self) -> int:
        """Return number of connected clients"""
        with self._lock:
            return len(self._clients)
    
    @property
    def frames_sent(self) -> int:
        """Return total frames submitted for streaming"""
        return self._frame_count

    @property
    def frames_dropped(self) -> int:
        """Frames dropped before encoding because the worker fell behind."""
        return self._frame_queue.dropped
    
    @property
    def elapsed_time(self) -> float:
        """Return elapsed time since start"""
        if self._start_time is None:
            return 0
        return time.time() - self._start_time
    
    @property
    def actual_fps(self) -> float:
        """Calculate FPS over a sliding window of recent frames"""
        if len(self._frame_timestamps) < 2:
            return 0
        
        current_time = time.time()
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
    
    def add_client(self, wfile: io.BufferedWriter, stream: str = 'main') -> MJPEGClient:
        """
        Add a new client to receive MJPEG frames.

        Args:
            wfile: The writable file object for the client connection
            stream: Which resolution to serve this client: 'main' (full
                resolution, the default) or 'sub' (resized, lower-resolution).
                Any other value is treated as 'main'.

        Returns:
            MJPEGClient object
        """
        if stream not in ('main', 'sub'):
            stream = 'main'
        client = MJPEGClient(wfile=wfile, stream=stream)
        with self._lock:
            self._clients.append(client)
        return client
    
    def remove_client(self, client: MJPEGClient):
        """Remove a client from the broadcast list"""
        client.connected = False
        # Wake the client's writer if it is blocked waiting for frames.
        client.queue.close()
        with self._lock:
            if client in self._clients:
                self._clients.remove(client)

    def stream_frame(self, frame: np.ndarray) -> bool:
        """
        Submit a frame for streaming. Non-blocking.

        This only enqueues the frame for the encode worker -- it does NOT encode
        or touch any client socket, so it always returns immediately and can
        never be stalled by a slow client. The single worker thread encodes the
        frame once and fans it out to every connected client's own queue.

        Args:
            frame: NumPy array in BGR format (OpenCV default)

        Returns:
            True if there is at least one connected client to receive the frame,
            False otherwise. (The frame is still counted/queued either way.)
        """
        if not self._is_running:
            return False

        # Count + timestamp on submit so frames_sent / actual_fps reflect the
        # producer's cadence; the queue absorbs any encoder backlog.
        self._frame_count += 1
        self._frame_timestamps.append(time.time())

        self._frame_queue.put(frame)

        with self._lock:
            return len(self._clients) > 0

    def _resolve_sub_size(self, frame: np.ndarray) -> tuple:
        """Resolve the (width, height) to encode the 'sub' stream at.

        Uses the fixed ``sub_width``/``sub_height`` attributes when both are
        set (via the constructor, or assigned directly on the instance).
        Otherwise falls back to half the incoming frame's dimensions, and
        if that would be degenerate (a tiny source frame), to a fixed
        640x360 default.
        """
        if self.sub_width and self.sub_height:
            return self.sub_width, self.sub_height
        h, w = frame.shape[:2]
        half_w, half_h = w // 2, h // 2
        if half_w < 16 or half_h < 16:
            return _DEFAULT_SUB_SIZE
        return half_w, half_h

    def _wrap_multipart(self, jpeg_bytes: bytes) -> bytes:
        """Wrap already-encoded JPEG bytes in one multipart/x-mixed-replace chunk."""
        return (
            self.BOUNDARY + b"\r\n"
            b"Content-Type: image/jpeg\r\n"
            b"Content-Length: " + str(len(jpeg_bytes)).encode() + b"\r\n"
            b"\r\n" + jpeg_bytes + b"\r\n"
        )

    def _encode_loop(self):
        """Worker: encode each queued frame once and fan it out to clients.

        The full-resolution ('main') JPEG is encoded exactly once per frame.
        If any connected client has selected the 'sub' stream, the frame is
        ALSO resized and encoded exactly once more (never once per sub
        client) and the right bytes are routed to each client's own queue.
        """
        while self._is_running:
            frame = self._frame_queue.get(timeout=0.5)
            if frame is None:
                continue

            # Encode the main (full-resolution) frame to JPEG exactly once.
            encode_params = [int(cv2.IMWRITE_JPEG_QUALITY), self.quality]
            try:
                success, jpeg = cv2.imencode('.jpg', frame, encode_params)
            except Exception as e:
                logger.error(f"MJPEG encode error: {e}")
                continue
            if not success:
                continue

            jpeg_bytes = jpeg.tobytes()
            self._last_frame = jpeg_bytes
            main_frame_data = self._wrap_multipart(jpeg_bytes)

            with self._lock:
                clients = list(self._clients)

            # Only resize+encode a 'sub' frame if at least one connected
            # client actually wants it -- one encode per frame total, no
            # matter how many sub clients are connected.
            sub_frame_data: Optional[bytes] = None
            if any(c.connected and c.stream == 'sub' for c in clients):
                try:
                    sub_w, sub_h = self._resolve_sub_size(frame)
                    sub_frame = cv2.resize(frame, (sub_w, sub_h), interpolation=cv2.INTER_AREA)
                    sub_success, sub_jpeg = cv2.imencode('.jpg', sub_frame, encode_params)
                    if sub_success:
                        sub_frame_data = self._wrap_multipart(sub_jpeg.tobytes())
                except Exception as e:
                    logger.error(f"MJPEG sub-stream encode error: {e}")

            # Fan out to every client's own bounded queue (drop-oldest, never
            # blocks). A slow client only drops its own frames. A 'sub'
            # client falls back to the main frame if the sub encode failed.
            for client in clients:
                if not client.connected:
                    continue
                if client.stream == 'sub' and sub_frame_data is not None:
                    client.queue.put(sub_frame_data)
                else:
                    client.queue.put(main_frame_data)

    def serve_client(self, client: MJPEGClient):
        """Blocking writer loop for one client (run on its HTTP thread).

        Drains the client's own queue of encoded frames and writes them to its
        socket. Blocks in ``queue.get`` between frames instead of busy-waiting.
        Returns when the client disconnects, its socket breaks, or the streamer
        stops. Any broken-pipe error only removes THIS client.
        """
        try:
            while self._is_running and client.connected:
                data = client.queue.get(timeout=0.5)
                if data is None:
                    continue
                try:
                    client.wfile.write(data)
                    client.wfile.flush()
                    client.frames_sent += 1
                except (BrokenPipeError, ConnectionResetError, OSError):
                    client.connected = False
                    break
        finally:
            self.remove_client(client)

    def get_headers(self) -> List[tuple]:
        """
        Get HTTP headers for MJPEG stream response.
        
        Returns:
            List of (header_name, header_value) tuples
        """
        return [
            ('Content-Type', f'multipart/x-mixed-replace; boundary={self.BOUNDARY.decode()[2:]}'),
            ('Cache-Control', 'no-cache, no-store, must-revalidate'),
            ('Pragma', 'no-cache'),
            ('Expires', '0'),
            ('Connection', 'close'),
        ]


def check_go2rtc_running(host: str = "127.0.0.1", port: int = 1984, timeout: float = 1.0) -> bool:
    """
    Check if go2rtc is running by attempting to connect to its API port.
    
    Args:
        host: go2rtc host address
        port: go2rtc API port (default 1984)
        timeout: Connection timeout in seconds
        
    Returns:
        True if go2rtc is running and accessible
    """
    import socket
    import urllib.request
    import urllib.error

    # First, a cheap TCP connect: if nothing is even listening on the API
    # port, go2rtc definitely is not running.
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        result = sock.connect_ex((host, port))
        sock.close()

        if result != 0:
            logger.debug("go2rtc not detected: TCP connect to %s:%s failed (errno %s)",
                         host, port, result)
            return False
    except (socket.error, socket.timeout) as e:
        logger.debug("go2rtc not detected: socket error connecting to %s:%s: %s",
                     host, port, e)
        return False

    # The port is open. Confirm it's actually go2rtc's HTTP API by hitting
    # /api. A live go2rtc answers here; treat ANY HTTP response (even a
    # non-200 status) as "detected" -- getting an HTTP reply on the API port
    # means the server is up. A running go2rtc must NEVER be reported as
    # "not detected", so we only return False when the request produced no
    # HTTP response at all.
    url = f"http://{host}:{port}/api"
    try:
        req = urllib.request.Request(url, method='GET')
        with urllib.request.urlopen(req, timeout=timeout) as response:
            status = getattr(response, 'status', None)
            logger.debug("go2rtc detected at %s (HTTP %s)", url, status)
            return True
    except urllib.error.HTTPError as e:
        # An HTTP error status is STILL an HTTP response from a live server
        # (go2rtc-shaped), so treat it as detected.
        logger.debug("go2rtc detected at %s (HTTP error status %s)", url, e.code)
        return True
    except (urllib.error.URLError, socket.timeout) as e:
        # The port accepted a TCP connection but did not complete an HTTP
        # response in time. Ambiguous (go2rtc under load, or a different
        # service), but since the port IS open we accept it rather than risk
        # reporting a live go2rtc as down.
        logger.debug("go2rtc API on %s did not answer cleanly (%s); "
                     "accepting because the port is open", url, e)
        return True
    except Exception as e:
        logger.debug("go2rtc detection failed for %s: %s", url, e)
        return False


def check_rtsp_port_available(host: str = "127.0.0.1", port: int = 8554, timeout: float = 1.0) -> bool:
    """
    Check if the RTSP port is available (go2rtc is listening).
    
    Args:
        host: go2rtc host address
        port: RTSP port (default 8554)
        timeout: Connection timeout in seconds
        
    Returns:
        True if RTSP port is accepting connections
    """
    import socket
    
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        result = sock.connect_ex((host, port))
        sock.close()
        return result == 0
    except (socket.error, socket.timeout):
        return False
