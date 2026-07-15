#!/usr/bin/env python3
"""
Local video recorder for IPyCam.

Records the camera's outbound frames to disk as segmented video files, off the
capture thread, following the same fan-out pattern as the MJPEG/RTSP outputs:
``IPCamera.stream()`` only ever *enqueues* an (immutable) outbound frame into a
bounded, drop-oldest :class:`~ipycam.framequeue.FrameQueue`; a dedicated worker
thread drains that queue and does all the encoding / disk I/O. A slow disk
therefore causes frame *drops* in the recorder queue -- never a stall of the
capture loop.

Features
--------
* **Encoding** via ``cv2.VideoWriter`` (no new dependency). ``recording_format``
  maps to an (extension, fourcc) pair; open failures (e.g. a missing codec) are
  logged and abort the recording gracefully instead of crashing.
* **Pre-record ring buffer**: while the worker is running with
  ``recording_pre_seconds > 0`` it keeps the most recent ``N = pre_seconds *
  fps`` outbound frames in a ``collections.deque(maxlen=N)``. When recording
  starts, that buffer is flushed into the first segment before live frames
  continue, so the recording begins ``pre_seconds`` *before* the trigger.
* **Max file size + rotation**: the current segment's on-disk size is polled
  (``os.path.getsize``) every few frames; when it exceeds
  ``recording_max_file_mb`` the writer is closed and a new, sequence-numbered
  segment is opened seamlessly.

Memory cost of the pre-record buffer
------------------------------------
The ring buffer holds references to ``N = pre_seconds * fps`` *decoded* frames
at the main-stream resolution. Each frame costs roughly ``width * height * 3``
bytes, so::

    ring_bytes  ~=  pre_seconds * fps * width * height * 3

For example 5 s at 30 fps and 1920x1080 is ~5.6 GB (150 frames x ~6.2 MB).
``recording_pre_seconds`` is therefore capped (see
``CameraConfig`` / ``MAX_RECORDING_PRE_SECONDS``) and defaults to ``0`` (off);
a warning is logged at start when the estimated ring memory is large.

Immutable-outbound-frame contract
---------------------------------
Both the ring buffer and the recorder queue store *references* to the outbound
frame produced by ``IPCamera.stream()`` -- they never copy it. This relies on
that frame being immutable by contract (a fresh buffer per capture iteration
that no consumer mutates in place; see the big comment in
``IPCamera.stream``). ``cv2.VideoWriter.write`` only reads its input, so this
holds; anyone adding a mutating consumer upstream must copy first.
"""

import os
import re
import logging
import threading
from collections import deque
from datetime import datetime
from typing import List, Optional

import numpy as np
import cv2

logger = logging.getLogger(__name__)


# recording_format -> (file extension, fourcc code). Kept small and to
# well-supported OpenCV/FFmpeg codecs so a default install can actually open
# the writer. mp4 uses the MPEG-4 Part 2 'mp4v' codec (universally available in
# OpenCV builds); avi uses Motion-JPEG.
RECORDING_FORMATS = {
    'mp4': ('.mp4', 'mp4v'),
    'avi': ('.avi', 'MJPG'),
}
_DEFAULT_FORMAT = 'mp4'

# How often (in frames) to poll the current segment's on-disk size for the
# max-file-size rollover check. VideoWriter does not expose the byte count, so
# we stat the file. Every-N-frames keeps the syscall cost negligible.
_DEFAULT_SIZE_POLL_FRAMES = 30

# Bounded, drop-oldest recorder queue. Larger than the 2-slot live-output
# queues so a brief disk hiccup drops nothing, but still bounded so a
# persistently slow disk drops frames instead of growing without limit. Each
# buffered frame costs ~width*height*3 bytes.
_DEFAULT_QUEUE_SIZE = 8

# Warn once at start when the estimated pre-record ring memory exceeds this.
_RING_MEMORY_WARN_BYTES = 512 * 1024 * 1024  # 512 MB


class VideoRecorder:
    """Records outbound frames to segmented video files on a worker thread.

    The recorder is *always constructed* but does no work until its worker is
    running AND either a recording is active or a pre-record buffer is being
    maintained. ``wants_frames`` gates the capture thread so that, when the
    recorder is idle, ``IPCamera.stream()`` does not even enqueue.

    Args:
        config: The live :class:`CameraConfig`. Recording settings
            (``recording_format``, ``recording_path``, ``recording_max_file_mb``,
            ``recording_pre_seconds``) and ``main_fps`` / ``main_width`` /
            ``main_height`` / ``rotation`` are read from it.
        queue_size: Bounded recorder queue depth (drop-oldest).
        size_poll_frames: Poll the segment file size every this many frames for
            the rollover check.
    """

    def __init__(
        self,
        config,
        queue_size: int = _DEFAULT_QUEUE_SIZE,
        size_poll_frames: int = _DEFAULT_SIZE_POLL_FRAMES,
    ):
        from .framequeue import FrameQueue  # local import to avoid cycles

        self.config = config
        self._FrameQueue = FrameQueue
        self._frame_queue = FrameQueue(maxsize=max(1, queue_size))
        self._queue_size = max(1, queue_size)
        self._size_poll_frames = max(1, size_poll_frames)

        # Worker lifecycle
        self._worker: Optional[threading.Thread] = None
        self._worker_running = False

        # Everything below is guarded by _lock. In particular ALL VideoWriter
        # operations (open / write / release) happen under this lock, so the
        # writer is never touched by two threads at once -- the worker writes
        # live frames, while start_recording()/stop_recording() (called from
        # the HTTP thread) open/flush/finalise it.
        self._lock = threading.Lock()

        # Pre-record ring buffer (references to immutable outbound frames).
        self._pre_seconds = 0
        self._ring: deque = deque()

        # Recording state
        self._recording = False
        self._writer: Optional[cv2.VideoWriter] = None
        self._current_file: Optional[str] = None
        self._out_dir: Optional[str] = None
        self._ext = '.mp4'
        self._fourcc_str = 'mp4v'
        self._safe_name = 'recording'
        self._fps = 30
        self._frame_size = (0, 0)          # (width, height) of the writer
        self._base_timestamp: Optional[datetime] = None
        self._segment_index = 0
        self._segments: List[str] = []
        self._frames_written = 0
        self._frames_in_segment = 0
        self._bytes_written = 0
        self._max_bytes = 0

    # ------------------------------------------------------------------ #
    # Worker lifecycle
    # ------------------------------------------------------------------ #
    def start(self) -> None:
        """Start the recorder worker thread and (re)build the ring buffer.

        Idempotent: calling start() on an already-running recorder is a no-op.
        The worker maintains the pre-record ring buffer whenever
        ``recording_pre_seconds > 0``; a recording is begun separately via
        :meth:`start_recording`.
        """
        with self._lock:
            if self._worker_running:
                return
            self._rebuild_ring_locked()
            self._frame_queue = self._FrameQueue(maxsize=self._queue_size)
            self._worker_running = True

        self._worker = threading.Thread(
            target=self._record_loop,
            name="recorder-worker",
            daemon=True,
        )
        self._worker.start()
        logger.info(
            "Recorder worker started (pre_record=%ss, format=%s, path=%s)",
            self._pre_seconds, getattr(self.config, 'recording_format', _DEFAULT_FORMAT),
            getattr(self.config, 'recording_path', 'recordings'),
        )

    def stop(self) -> None:
        """Finalise any active recording, then stop and JOIN the worker."""
        # Finalise the file first so it is never left truncated/corrupt.
        self.stop_recording()

        self._worker_running = False
        self._frame_queue.close()  # wake the worker if blocked on get()
        worker = self._worker
        if worker and worker.is_alive():
            worker.join(timeout=3.0)
        self._worker = None
        logger.info("Recorder worker stopped")

    @property
    def is_worker_running(self) -> bool:
        return self._worker_running

    def reconfigure(self) -> None:
        """Re-read recording settings from config (ring size, etc.).

        Safe to call while the worker runs. The ring buffer is only rebuilt
        when NOT recording (rebuilding mid-recording is meaningless -- the ring
        is not maintained while recording anyway).
        """
        with self._lock:
            if not self._recording:
                self._rebuild_ring_locked()

    def _rebuild_ring_locked(self) -> None:
        """(Re)create the pre-record ring buffer from current config."""
        pre_seconds = int(getattr(self.config, 'recording_pre_seconds', 0) or 0)
        fps = max(1, int(getattr(self.config, 'main_fps', 30) or 30))
        self._pre_seconds = max(0, pre_seconds)
        maxlen = self._pre_seconds * fps
        self._ring = deque(maxlen=maxlen if maxlen > 0 else None)
        if maxlen > 0:
            w = int(getattr(self.config, 'main_width', 0) or 0)
            h = int(getattr(self.config, 'main_height', 0) or 0)
            est = maxlen * max(1, w) * max(1, h) * 3
            if est >= _RING_MEMORY_WARN_BYTES:
                logger.warning(
                    "Recorder pre-record buffer may use ~%.0f MB (%d frames of "
                    "%dx%d). Reduce recording_pre_seconds if memory is a concern.",
                    est / (1024 * 1024), maxlen, w, h,
                )

    # ------------------------------------------------------------------ #
    # Capture-thread facing API (must stay non-blocking / cheap)
    # ------------------------------------------------------------------ #
    @property
    def wants_frames(self) -> bool:
        """True when the capture thread should enqueue outbound frames.

        Cheap and lock-free (reads of independent bool/int attributes are
        atomic in CPython). False => ``IPCamera.stream()`` skips the recorder
        entirely (zero cost when idle).
        """
        return self._worker_running and (self._recording or self._pre_seconds > 0)

    def submit(self, frame: np.ndarray) -> None:
        """Enqueue an outbound frame for the recorder worker. Non-blocking.

        Stores a REFERENCE to the immutable outbound frame (no copy) -- see the
        module docstring's immutable-outbound-frame contract. If the queue is
        full (a slow disk fell behind) the oldest frame is dropped.
        """
        if not self.wants_frames:
            return
        self._frame_queue.put(frame)

    # ------------------------------------------------------------------ #
    # Recording control (may be called from the HTTP thread)
    # ------------------------------------------------------------------ #
    def start_recording(self) -> bool:
        """Begin recording. Returns True on success, False on graceful failure.

        Lazily starts the worker if it is not already running (so an explicit
        API start works even when ``recording_enabled`` is False). Opens the
        first segment writer, then flushes the pre-record ring buffer into it
        before live frames continue. A bad path or un-openable codec logs an
        error and returns False without raising -- the camera keeps running.
        """
        if not self._worker_running:
            self.start()

        with self._lock:
            if self._recording:
                logger.info("Recorder: start_recording ignored (already recording)")
                return True

            out_dir = self._resolve_output_dir()
            if out_dir is None:
                return False

            fmt = str(getattr(self.config, 'recording_format', _DEFAULT_FORMAT) or _DEFAULT_FORMAT).lower()
            self._ext, self._fourcc_str = RECORDING_FORMATS.get(fmt, RECORDING_FORMATS[_DEFAULT_FORMAT])
            self._safe_name = self._sanitize_name(getattr(self.config, 'name', 'recording'))
            self._out_dir = out_dir
            self._fps = max(1, int(getattr(self.config, 'main_fps', 30) or 30))
            self._frame_size = self._infer_frame_size_locked()

            max_mb = int(getattr(self.config, 'recording_max_file_mb', 0) or 0)
            self._max_bytes = max_mb * 1024 * 1024 if max_mb > 0 else 0

            self._base_timestamp = datetime.now()
            self._segment_index = 0
            self._segments = []
            self._frames_written = 0
            self._frames_in_segment = 0
            self._bytes_written = 0

            if not self._open_writer_locked():
                self._writer = None
                self._current_file = None
                return False

            # Flush the pre-record ring buffer into the FIRST segment (no
            # rollover mid-flush -- rotation only applies to live frames).
            if self._pre_seconds > 0 and self._ring:
                for f in list(self._ring):
                    self._write_frame_locked(f, allow_rotate=False)
                self._ring.clear()

            self._recording = True
            logger.info("Recording started: %s", self._current_file)
            return True

    def stop_recording(self) -> List[str]:
        """Finalise the current recording and return the segment file paths.

        Safe to call from any thread and when not recording (returns ``[]``).
        Closes the writer cleanly so the file is never left corrupt.
        """
        with self._lock:
            if not self._recording:
                return []
            self._recording = False
            self._close_writer_locked()
            segments = list(self._segments)
            logger.info(
                "Recording stopped: %d segment(s), %d frames, last=%s",
                len(segments), self._frames_written, self._current_file,
            )
            return segments

    @property
    def is_recording(self) -> bool:
        return self._recording

    @property
    def dropped(self) -> int:
        """Frames dropped because the recorder queue was full (slow disk)."""
        return self._frame_queue.dropped

    def stats(self) -> dict:
        """Snapshot of recorder state for the HTTP status/stats endpoints."""
        with self._lock:
            return {
                'recording': self._recording,
                'worker_running': self._worker_running,
                'file': self._current_file,
                'bytes': self._bytes_written,
                'segments': len(self._segments),
                'segment_files': list(self._segments),
                'frames_written': self._frames_written,
                'dropped': self._frame_queue.dropped,
                'pre_record_seconds': self._pre_seconds,
                'format': self._fourcc_str,
            }

    # ------------------------------------------------------------------ #
    # Worker loop
    # ------------------------------------------------------------------ #
    def _record_loop(self) -> None:
        """Worker: drain the queue -> write live frames OR fill the ring.

        All disk I/O happens here (or, for the one-time ring flush, on the
        thread that called start_recording). The capture thread only ever
        enqueues, so a slow disk drops frames in the queue instead of stalling
        capture.
        """
        while self._worker_running:
            frame = self._frame_queue.get(timeout=0.5)
            if frame is None:
                continue
            with self._lock:
                if self._recording:
                    self._write_frame_locked(frame)
                elif self._pre_seconds > 0:
                    self._ring.append(frame)

    # ------------------------------------------------------------------ #
    # Writer helpers (ALL called with self._lock held)
    # ------------------------------------------------------------------ #
    def _write_frame_locked(self, frame: np.ndarray, allow_rotate: bool = True) -> None:
        if self._writer is None:
            return
        try:
            fh, fw = frame.shape[:2]
            if (fw, fh) != self._frame_size:
                frame = cv2.resize(frame, self._frame_size)
            if frame.ndim == 2:  # grayscale -> BGR
                frame = cv2.cvtColor(frame, cv2.COLOR_GRAY2BGR)
            elif frame.shape[2] == 4:  # BGRA -> BGR
                frame = frame[:, :, :3]
            self._writer.write(frame)
            self._frames_written += 1
            self._frames_in_segment += 1
        except Exception as e:
            logger.error("Recorder: frame write error: %s", e)
            return

        if (allow_rotate and self._max_bytes > 0
                and self._frames_in_segment % self._size_poll_frames == 0):
            self._maybe_rotate_locked()

    def _maybe_rotate_locked(self) -> None:
        """Poll the current segment size and roll over if it exceeds the cap."""
        if not self._current_file:
            return
        try:
            size = os.path.getsize(self._current_file)
        except OSError:
            return
        self._bytes_written = size
        if size >= self._max_bytes:
            logger.info(
                "Recorder: segment %d reached %d bytes (>= %d) -- rotating",
                self._segment_index, size, self._max_bytes,
            )
            self._close_writer_locked()
            self._segment_index += 1
            if not self._open_writer_locked():
                logger.error("Recorder: failed to open next segment -- stopping recording")
                self._recording = False

    def _open_writer_locked(self) -> bool:
        """Open a VideoWriter for the current segment. Returns success."""
        assert self._base_timestamp is not None
        filename = (
            f"{self._safe_name}_{self._base_timestamp:%Y%m%d_%H%M%S}"
            f"_{self._segment_index:03d}{self._ext}"
        )
        path = os.path.join(self._out_dir, filename)
        try:
            fourcc = cv2.VideoWriter_fourcc(*self._fourcc_str)
            writer = cv2.VideoWriter(path, fourcc, float(self._fps), self._frame_size)
        except Exception as e:
            logger.error("Recorder: failed to create VideoWriter: %s", e)
            return False
        if not writer.isOpened():
            logger.error(
                "Recorder: VideoWriter could not open (codec '%s', size %s, path '%s'). "
                "Check the codec is available.",
                self._fourcc_str, self._frame_size, path,
            )
            try:
                writer.release()
            except Exception:
                pass
            return False
        self._writer = writer
        self._current_file = path
        self._frames_in_segment = 0
        self._segments.append(path)
        logger.info("Recorder: opened segment %d: %s", self._segment_index, path)
        return True

    def _close_writer_locked(self) -> None:
        if self._writer is not None:
            try:
                self._writer.release()
            except Exception as e:
                logger.error("Recorder: writer release error: %s", e)
            self._writer = None
        if self._current_file:
            try:
                self._bytes_written = os.path.getsize(self._current_file)
            except OSError:
                pass

    def _infer_frame_size_locked(self) -> tuple:
        """Best-effort (width, height) for the writer.

        Prefers an actual buffered frame (the newest ring frame) so the writer
        matches the true outbound size, including any display rotation.
        Falls back to the configured main dimensions, swapped for a 90/270
        rotation. Any frame whose size differs is resized before writing, so
        this only needs to be close, not exact.
        """
        if self._ring:
            h, w = self._ring[-1].shape[:2]
            return (int(w), int(h))
        w = int(getattr(self.config, 'main_width', 1280) or 1280)
        h = int(getattr(self.config, 'main_height', 720) or 720)
        if int(getattr(self.config, 'rotation', 0) or 0) in (90, 270):
            w, h = h, w
        return (w, h)

    def _resolve_output_dir(self) -> Optional[str]:
        """Resolve, sanitise, create and validate the recording directory.

        Returns the absolute directory path, or None on failure (logged). The
        configured ``recording_path`` is resolved to an absolute path and
        created if missing; a non-writable or un-creatable location fails
        gracefully rather than raising.
        """
        raw = getattr(self.config, 'recording_path', 'recordings') or 'recordings'
        try:
            raw = str(raw)
            if '\x00' in raw:
                logger.error("Recorder: invalid recording_path (null byte)")
                return None
            out_dir = os.path.abspath(os.path.expanduser(raw))
            os.makedirs(out_dir, exist_ok=True)
            if not os.path.isdir(out_dir) or not os.access(out_dir, os.W_OK):
                logger.error("Recorder: recording_path is not a writable directory: %s", out_dir)
                return None
            return out_dir
        except Exception as e:
            logger.error("Recorder: could not prepare recording_path '%s': %s", raw, e)
            return None

    @staticmethod
    def _sanitize_name(name) -> str:
        """Make a config name safe for use inside a filename."""
        safe = re.sub(r'[^\w\-]', '_', str(name)).strip('_')
        return safe or 'recording'
