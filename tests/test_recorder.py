"""
Tests for VideoRecorder (step 4.4 -- local disk recording).

These are deliberately realistic: they drive a real ``cv2.VideoWriter`` to a
temp directory (tmp_path) and reopen the result with ``cv2.VideoCapture`` to
prove the file is non-empty and decodable. The recorder runs its own worker
thread, so the helpers below feed frames through the public ``submit()`` API and
wait for the worker to catch up (polling public stats / small white-box hooks)
rather than sleeping blindly.

Frame-content trick: each synthetic frame is a SOLID gray fill whose value
encodes an ordinal. Solid fills survive lossy MJPEG/MPEG-4 compression almost
exactly, so the per-frame mean read back from the file maps cleanly back to the
ordinal -- which lets the pre-record test prove that the earlier (pre-roll)
frames really landed at the START of the recording.
"""

import os
import time
import threading

import numpy as np
import cv2
import pytest

from ipycam.config import CameraConfig
from ipycam.recorder import VideoRecorder


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def gray_frame(value: int, w: int = 64, h: int = 48) -> np.ndarray:
    """A solid BGR frame filled with ``value`` (0-255); survives compression."""
    return np.full((h, w, 3), value & 0xFF, dtype=np.uint8)


def wait_until(pred, timeout: float = 5.0, interval: float = 0.01) -> bool:
    """Poll ``pred`` until it is truthy or ``timeout`` elapses."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return True
        time.sleep(interval)
    return bool(pred())


def read_back(path: str):
    """Decode a video file into a list of frames via cv2.VideoCapture."""
    cap = cv2.VideoCapture(path)
    frames = []
    try:
        while True:
            ret, frame = cap.read()
            if not ret:
                break
            frames.append(frame)
    finally:
        cap.release()
    return frames


def make_config(tmp_path, **overrides) -> CameraConfig:
    """Small-resolution recording config rooted at tmp_path."""
    cfg = CameraConfig(
        main_width=64,
        main_height=48,
        main_fps=10,
        show_timestamp=False,
    )
    cfg.recording_path = str(tmp_path)
    cfg.recording_format = overrides.pop('recording_format', 'mp4')
    cfg.recording_pre_seconds = overrides.pop('recording_pre_seconds', 0)
    cfg.recording_max_file_mb = overrides.pop('recording_max_file_mb', 1024)
    for k, v in overrides.items():
        setattr(cfg, k, v)
    return cfg


@pytest.fixture
def stop_recorder():
    """Ensure any recorder created in a test is stopped (worker joined)."""
    created = []

    def _track(rec):
        created.append(rec)
        return rec

    yield _track

    for rec in created:
        try:
            rec.stop()
        except Exception:
            pass


# --------------------------------------------------------------------------- #
# 1. Basic record -> non-empty, openable file with ~correct count & dims
# --------------------------------------------------------------------------- #

def test_record_produces_openable_file_with_expected_frames(tmp_path, stop_recorder):
    cfg = make_config(tmp_path, recording_format='mp4')
    rec = stop_recorder(VideoRecorder(cfg, queue_size=512))
    rec.start()

    assert rec.start_recording() is True
    assert rec.is_recording is True

    n = 20
    for i in range(n):
        rec.submit(gray_frame(i * 10))
    assert wait_until(lambda: rec.stats()['frames_written'] >= n)

    files = rec.stop_recording()
    assert rec.is_recording is False
    assert len(files) == 1
    path = files[0]
    assert os.path.isfile(path)
    assert os.path.getsize(path) > 0

    frames = read_back(path)
    # Frame count should match closely (codecs occasionally clip a frame).
    assert abs(len(frames) - n) <= 2, f"expected ~{n} frames, got {len(frames)}"
    # Dimensions preserved (H, W, C).
    assert frames[0].shape[0] == 48
    assert frames[0].shape[1] == 64


def test_max_bytes_derived_from_config_mb(tmp_path, stop_recorder):
    """start_recording translates recording_max_file_mb into a byte budget."""
    cfg = make_config(tmp_path, recording_max_file_mb=7)
    rec = stop_recorder(VideoRecorder(cfg, queue_size=8))
    rec.start()
    assert rec.start_recording() is True
    assert rec._max_bytes == 7 * 1024 * 1024
    rec.stop_recording()


# --------------------------------------------------------------------------- #
# 2. Pre-record ring buffer -> pre-roll frames present at the start
# --------------------------------------------------------------------------- #

def test_pre_record_buffer_prepends_preroll_frames(tmp_path, stop_recorder):
    # pre_seconds=1 * fps=10 => ring holds the last 10 frames.
    cfg = make_config(tmp_path, recording_format='mp4', recording_pre_seconds=1)
    rec = stop_recorder(VideoRecorder(cfg, queue_size=512))
    rec.start()

    # Feed 10 LOW-value frames BEFORE recording -> these fill the ring buffer.
    ring_n = 10
    for i in range(ring_n):
        rec.submit(gray_frame(i))  # values 0..9 (all low)
    # Wait for the worker to move all of them into the ring.
    assert wait_until(lambda: len(rec._ring) >= ring_n)

    assert rec.start_recording() is True  # flushes the ring into segment 0

    # Feed 5 HIGH-value live frames AFTER recording started.
    live_n = 5
    for j in range(live_n):
        rec.submit(gray_frame(200 + j * 10))  # values 200..240 (all high)
    assert wait_until(lambda: rec.stats()['frames_written'] >= ring_n + live_n)

    files = rec.stop_recording()
    assert len(files) == 1
    frames = read_back(files[0])

    total = len(frames)
    assert ring_n + live_n - 2 <= total <= ring_n + live_n, (
        f"expected ~{ring_n + live_n} frames, got {total}"
    )

    means = [float(f.mean()) for f in frames]
    # Pre-roll made it in AND landed first: the recording STARTS with a low
    # (pre-roll) frame and ENDS with a high (live) frame.
    assert means[0] < 40, f"first frame should be pre-roll (low), got {means[0]}"
    assert means[-1] > 150, f"last frame should be live (high), got {means[-1]}"
    # The early portion is dominated by pre-roll (low) frames.
    assert min(means[:ring_n]) < 20


def test_no_preroll_when_pre_seconds_zero(tmp_path, stop_recorder):
    """pre_seconds=0 => ring disabled, wants_frames only true while recording."""
    cfg = make_config(tmp_path, recording_pre_seconds=0)
    rec = stop_recorder(VideoRecorder(cfg, queue_size=64))
    rec.start()
    # Worker running but not recording and no pre-buffer -> no frames wanted.
    assert rec.wants_frames is False
    rec.submit(gray_frame(5))  # dropped on the floor (gated out)
    time.sleep(0.05)
    assert rec.stats()['frames_written'] == 0

    assert rec.start_recording() is True
    assert rec.wants_frames is True
    rec.stop_recording()


# --------------------------------------------------------------------------- #
# 3. Size-based rotation -> >= 2 segments, each openable
# --------------------------------------------------------------------------- #

def test_rotation_creates_multiple_segments(tmp_path, stop_recorder):
    # avi/MJPG so the file grows incrementally on disk (no big trailer), which
    # makes the os.path.getsize() rollover poll observe growth promptly.
    cfg = make_config(tmp_path, recording_format='avi')
    # Poll the size every frame so a tiny cap rolls over quickly.
    rec = stop_recorder(VideoRecorder(cfg, queue_size=512, size_poll_frames=1))
    rec.start()
    assert rec.start_recording() is True

    # Force a tiny byte budget (config's MB minimum is 1 MB, far too large to
    # roll over on a handful of small frames). Set BEFORE feeding so it governs
    # every write; this is the only white-box poke and it exercises the exact
    # same rollover path a real 1 MB cap would.
    rec._max_bytes = 40_000

    # Random-noise frames compress poorly -> each segment fills fast.
    rng = np.random.default_rng(1234)
    n = 120
    for _ in range(n):
        rec.submit(rng.integers(0, 256, (48, 64, 3), dtype=np.uint8))
    assert wait_until(lambda: rec.stats()['frames_written'] >= n, timeout=10.0)

    files = rec.stop_recording()
    assert len(files) >= 2, f"expected >=2 segments, got {len(files)}: {files}"
    for path in files:
        assert os.path.isfile(path)
        assert os.path.getsize(path) > 0
        assert len(read_back(path)) >= 1, f"segment not decodable: {path}"

    # Segment filenames are sequence-numbered and distinct.
    assert len(set(files)) == len(files)


# --------------------------------------------------------------------------- #
# 4. Slow-disk backpressure -> submit stays fast, frames DROP (never blocks)
# --------------------------------------------------------------------------- #

class _SlowWriter:
    """Stand-in VideoWriter whose write() sleeps to simulate a slow disk."""

    def __init__(self, *args, **kwargs):
        self.count = 0

    def isOpened(self):
        return True

    def write(self, frame):
        self.count += 1
        time.sleep(0.05)  # 50 ms per frame -> the worker falls behind fast

    def release(self):
        pass


def test_slow_disk_drops_frames_without_blocking_submit(tmp_path, stop_recorder, monkeypatch):
    cfg = make_config(tmp_path)
    # Tiny queue so backpressure shows up as drops almost immediately.
    rec = stop_recorder(VideoRecorder(cfg, queue_size=2))
    monkeypatch.setattr('ipycam.recorder.cv2.VideoWriter', _SlowWriter)
    rec.start()
    assert rec.start_recording() is True

    max_submit = 0.0
    for i in range(60):
        t0 = time.perf_counter()
        rec.submit(gray_frame(i))
        dt = time.perf_counter() - t0
        max_submit = max(max_submit, dt)

    # The enqueue path must never block on the slow writer.
    assert max_submit < 0.02, f"submit() blocked for {max_submit * 1000:.1f} ms"
    # And the pressure showed up as DROPS, not as a stalled producer.
    assert wait_until(lambda: rec.dropped > 0, timeout=2.0)
    assert rec.dropped > 0

    rec.stop_recording()


def test_camera_stream_stays_responsive_under_slow_recorder(tmp_path, monkeypatch):
    """End-to-end gate: IPCamera.stream() enqueues to the recorder without
    blocking even when the recorder's writer is glacial."""
    from ipycam.camera import IPCamera

    # High fps so IPCamera.stream()'s intentional frame-pacing sleep is
    # negligible -- this test isolates recorder enqueue latency, not pacing.
    cfg = make_config(tmp_path, main_fps=240)
    cfg.name = "SlowDiskCam"
    monkeypatch.setattr('ipycam.recorder.cv2.VideoWriter', _SlowWriter)

    camera = IPCamera(cfg)
    camera.ptz.stop()
    camera.ptz = None
    try:
        # Only the recorder is exercised here (no network servers started).
        camera.recorder.start()
        assert camera.start_recording() is True

        max_stream = 0.0
        for i in range(40):
            t0 = time.perf_counter()
            camera.stream(gray_frame(i))
            max_stream = max(max_stream, time.perf_counter() - t0)

        assert max_stream < 0.05, f"camera.stream() blocked for {max_stream*1000:.1f} ms"
        assert wait_until(lambda: camera.recorder.dropped > 0, timeout=2.0)
    finally:
        camera.stop_recording()
        camera.recorder.stop()


# --------------------------------------------------------------------------- #
# 5. Bad path / bad codec -> graceful failure, no crash
# --------------------------------------------------------------------------- #

def test_start_recording_bad_path_fails_gracefully(tmp_path, stop_recorder):
    # Point recording_path at a FILE, not a directory: makedirs() will refuse.
    bogus = tmp_path / "not_a_dir"
    bogus.write_text("i am a file")
    cfg = make_config(tmp_path)
    cfg.recording_path = str(bogus)

    rec = stop_recorder(VideoRecorder(cfg, queue_size=8))
    rec.start()

    assert rec.start_recording() is False  # graceful, no exception
    assert rec.is_recording is False
    # Recorder still usable afterwards (worker alive, no crash).
    assert rec.is_worker_running is True


class _UnopenableWriter:
    """A VideoWriter whose isOpened() is always False (missing codec)."""

    def __init__(self, *args, **kwargs):
        pass

    def isOpened(self):
        return False

    def write(self, frame):  # pragma: no cover - never reached
        raise AssertionError("write() must not be called on an unopened writer")

    def release(self):
        pass


def test_start_recording_bad_codec_fails_gracefully(tmp_path, stop_recorder, monkeypatch):
    cfg = make_config(tmp_path)
    rec = stop_recorder(VideoRecorder(cfg, queue_size=8))
    monkeypatch.setattr('ipycam.recorder.cv2.VideoWriter', _UnopenableWriter)
    rec.start()

    assert rec.start_recording() is False
    assert rec.is_recording is False
    # No stray file handle / segment recorded.
    assert rec.stats()['segments'] == 0
    assert rec.is_worker_running is True


# --------------------------------------------------------------------------- #
# 6. Lifecycle -> worker not alive after stop, file finalized/openable
# --------------------------------------------------------------------------- #

def test_worker_not_alive_after_stop_and_file_finalized(tmp_path):
    cfg = make_config(tmp_path)
    rec = VideoRecorder(cfg, queue_size=512)
    rec.start()
    assert rec.is_worker_running is True

    assert rec.start_recording() is True
    for i in range(15):
        rec.submit(gray_frame(i * 8))
    assert wait_until(lambda: rec.stats()['frames_written'] >= 15)
    files = rec.stop_recording()

    rec.stop()  # stops + joins the worker

    assert rec.is_worker_running is False
    # No leaked recorder worker thread.
    assert not any(t.name == "recorder-worker" and t.is_alive()
                   for t in threading.enumerate())
    # File finalized and decodable after full shutdown.
    assert len(files) == 1
    assert os.path.isfile(files[0])
    assert len(read_back(files[0])) >= 1


def test_stop_recording_when_not_recording_is_noop(tmp_path, stop_recorder):
    cfg = make_config(tmp_path)
    rec = stop_recorder(VideoRecorder(cfg, queue_size=8))
    rec.start()
    assert rec.stop_recording() == []  # no-op, no crash
    assert rec.is_recording is False


def test_stop_finalizes_active_recording(tmp_path):
    """recorder.stop() must finalize an in-progress recording, not orphan it."""
    cfg = make_config(tmp_path)
    rec = VideoRecorder(cfg, queue_size=512)
    rec.start()
    assert rec.start_recording() is True
    for i in range(12):
        rec.submit(gray_frame(i * 8))
    assert wait_until(lambda: rec.stats()['frames_written'] >= 12)

    segments = list(rec.stats()['segment_files'])
    rec.stop()  # should stop_recording() internally

    assert rec.is_recording is False
    assert rec.is_worker_running is False
    assert segments and os.path.isfile(segments[0])
    assert len(read_back(segments[0])) >= 1
