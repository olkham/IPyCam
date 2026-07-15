"""
Tests for ipycam.__main__ (the ``python -m ipycam`` CLI entry point).

infer_source_type() is pure and tested directly. main() drives real IO (argv,
config file, cv2.VideoCapture, IPCamera) so every external dependency is
replaced with a hermetic fake: no real webcam/file/network is ever touched.
"""

import os
import sys
from collections import deque
from unittest.mock import MagicMock

import cv2
import numpy as np
import pytest

import ipycam.__main__ as main_mod
from ipycam.__main__ import infer_source_type, main
from ipycam.config import CameraConfig


# ---------------------------------------------------------------------------
# infer_source_type() -- pure function, no mocking needed.
# ---------------------------------------------------------------------------


class TestInferSourceType:
    def test_video_keyword_means_upload_mode(self):
        assert infer_source_type("video") == ("video_file", "Waiting for upload...")
        assert infer_source_type("VIDEO") == ("video_file", "Waiting for upload...")

    def test_integer_index_is_camera(self):
        assert infer_source_type("0") == ("camera", "Camera Index 0")
        assert infer_source_type("2") == ("camera", "Camera Index 2")

    @pytest.mark.parametrize("scheme", ["rtsp://", "rtmp://", "http://", "https://"])
    def test_url_schemes_are_rtsp_source(self, scheme):
        url = f"{scheme}192.168.1.5/stream"
        assert infer_source_type(url) == ("rtsp", url)

    def test_existing_file_is_video_file(self, tmp_path):
        video = tmp_path / "clip.mp4"
        video.write_bytes(b"not a real mp4 but just needs to exist")
        source_type, info = infer_source_type(str(video))
        assert source_type == "video_file"
        assert info == "clip.mp4"

    def test_nonexistent_path_with_video_extension_is_video_file(self):
        source_type, info = infer_source_type("does_not_exist_yet.mkv")
        assert source_type == "video_file"
        assert info == "does_not_exist_yet.mkv"

    def test_unrecognized_string_falls_back_to_custom(self):
        assert infer_source_type("screen0") == ("custom", "screen0")


# ---------------------------------------------------------------------------
# main() -- fully mocked IPCamera / cv2.VideoCapture / CameraConfig.load.
# ---------------------------------------------------------------------------


class FakeCap:
    """Stand-in for cv2.VideoCapture."""

    def __init__(self, opened=True, frames=None):
        self.opened = opened
        self._frames = list(frames) if frames else []
        self.released = False
        self.set_calls = []

    def isOpened(self):
        return self.opened

    def read(self):
        if self._frames:
            return True, self._frames.pop(0)
        return False, None

    def set(self, prop, val):
        self.set_calls.append((prop, val))

    def release(self):
        self.released = True


class FakeCamera:
    """Stand-in for IPCamera used by main()'s capture loop."""

    def __init__(self, config, is_running_seq=None, start_return=True):
        self.config = config
        self._is_running_seq = deque(is_running_seq if is_running_seq is not None else [True, False])
        self.start_return = start_return
        self.stream_calls = []
        self.stopped = False
        self._video_upload_mode = False
        self._current_video_path = None
        self.notify_video_error_calls = []
        self.notify_video_loaded_calls = []
        # Optional queue of values get_current_video_path() returns in order
        # (simulating a user switching videos via the web UI mid-loop) before
        # falling back to whatever set_current_video_path() last stored.
        self.path_sequence = []

    def start(self):
        return self.start_return

    @property
    def is_running(self):
        if self._is_running_seq:
            return self._is_running_seq.popleft()
        return False

    def stream(self, frame):
        self.stream_calls.append(frame)

    def stop(self):
        self.stopped = True

    def set_video_upload_mode(self, enabled):
        self._video_upload_mode = enabled

    def get_current_video_path(self):
        if self.path_sequence:
            return self.path_sequence.pop(0)
        return self._current_video_path

    def set_current_video_path(self, path):
        self._current_video_path = path

    def notify_video_error(self, msg):
        self.notify_video_error_calls.append(msg)

    def notify_video_loaded(self, path):
        self.notify_video_loaded_calls.append(path)


@pytest.fixture
def patched_config(monkeypatch):
    """CameraConfig.load() returns a real (fast, in-memory) default config."""
    cfg = CameraConfig()

    def fake_load(cls, filepath="camera_config.json"):
        return cfg
    monkeypatch.setattr(CameraConfig, "load", classmethod(fake_load))
    return cfg


@pytest.fixture
def patched_logging(monkeypatch):
    monkeypatch.setattr(main_mod, "configure_logging", MagicMock())


def _install_camera(monkeypatch, camera):
    monkeypatch.setattr(main_mod, "IPCamera", lambda config: camera)


def _install_cap(monkeypatch, cap):
    monkeypatch.setattr(cv2, "VideoCapture", lambda *a, **k: cap)


def test_main_returns_1_when_camera_start_fails(monkeypatch, patched_config, patched_logging):
    camera = FakeCamera(patched_config, start_return=False)
    _install_camera(monkeypatch, camera)
    monkeypatch.setattr(sys, "argv", ["ipycam", "--source", "0"])

    assert main() == 1


def test_main_returns_1_when_cap_not_opened(monkeypatch, patched_config, patched_logging):
    camera = FakeCamera(patched_config)
    _install_camera(monkeypatch, camera)
    _install_cap(monkeypatch, FakeCap(opened=False))
    monkeypatch.setattr(sys, "argv", ["ipycam", "--source", "0"])

    assert main() == 1
    assert camera.stopped is True


def test_main_standard_mode_happy_path_applies_overrides(monkeypatch, patched_config, patched_logging):
    frame = np.zeros((10, 10, 3), dtype=np.uint8)
    camera = FakeCamera(patched_config, is_running_seq=[True, False])
    _install_camera(monkeypatch, camera)
    _install_cap(monkeypatch, FakeCap(opened=True, frames=[frame]))
    monkeypatch.setattr(sys, "argv", [
        "ipycam", "--source", "0", "--width", "800", "--height", "600",
        "--fps", "15", "--no-timestamp", "--timestamp-position", "top-right",
        "--hw", "cpu",
    ])

    assert main() == 0
    assert patched_config.main_width == 800
    assert patched_config.main_height == 600
    assert patched_config.main_fps == 15
    assert patched_config.show_timestamp is False
    assert patched_config.timestamp_position == "top-right"
    assert patched_config.hw_accel == "cpu"
    assert len(camera.stream_calls) == 1
    assert camera.stopped is True


def test_main_standard_mode_camera_read_failure_breaks(monkeypatch, patched_config, patched_logging):
    """A device (int index) read failure must break out (not loop forever)."""
    camera = FakeCamera(patched_config, is_running_seq=[True, True, True])
    _install_camera(monkeypatch, camera)
    _install_cap(monkeypatch, FakeCap(opened=True, frames=[]))  # read() -> (False, None)
    monkeypatch.setattr(sys, "argv", ["ipycam", "--source", "0"])

    assert main() == 0
    assert camera.stream_calls == []
    assert camera.stopped is True


def test_main_standard_mode_video_file_read_failure_loops(monkeypatch, patched_config, tmp_path, patched_logging):
    """A video-file source restarts (seeks to frame 0) instead of breaking."""
    video = tmp_path / "in.mp4"
    video.write_bytes(b"fake")
    camera = FakeCamera(patched_config, is_running_seq=[True, True, False])
    _install_camera(monkeypatch, camera)
    cap = FakeCap(opened=True, frames=[])  # read() always fails -> loop restart
    _install_cap(monkeypatch, cap)
    monkeypatch.setattr(sys, "argv", ["ipycam", "--source", str(video)])

    assert main() == 0
    assert cap.set_calls  # cap.set(CAP_PROP_POS_FRAMES, 0) was called on restart
    assert camera.stopped is True


def test_main_standard_mode_video_switch_success(monkeypatch, patched_config, tmp_path, patched_logging):
    """Simulates a video switch requested (e.g. via the web UI) mid-loop.

    Note: main() itself calls set_current_video_path(abspath(source)) once
    before the loop starts, so the FIRST get_current_video_path() call must
    still report the *original* video (no switch) -- path_sequence supplies
    that, then a second value that differs to trigger the switch branch.
    """
    video1 = tmp_path / "first.mp4"
    video1.write_bytes(b"fake1")
    video2 = tmp_path / "second.mp4"
    video2.write_bytes(b"fake2")

    frame = np.zeros((4, 4, 3), dtype=np.uint8)
    camera = FakeCamera(patched_config, is_running_seq=[True, True, False])
    camera.path_sequence = [str(video1), str(video2)]
    _install_camera(monkeypatch, camera)

    caps = [FakeCap(opened=True, frames=[frame]), FakeCap(opened=True, frames=[frame])]

    def cap_factory(*a, **k):
        return caps.pop(0)
    monkeypatch.setattr(cv2, "VideoCapture", cap_factory)
    monkeypatch.setattr(sys, "argv", ["ipycam", "--source", str(video1)])

    assert main() == 0
    assert camera.notify_video_loaded_calls == [str(video2)]
    assert camera.stopped is True


def test_main_standard_mode_video_switch_failure_reverts(monkeypatch, patched_config, tmp_path, patched_logging):
    video1 = tmp_path / "first.mp4"
    video1.write_bytes(b"fake1")
    video2 = tmp_path / "second.mp4"
    video2.write_bytes(b"fake2")

    frame = np.zeros((4, 4, 3), dtype=np.uint8)
    camera = FakeCamera(patched_config, is_running_seq=[True, True, True, False])
    camera.path_sequence = [str(video1), str(video2)]
    _install_camera(monkeypatch, camera)

    # 1st cap (video1, opened fine, serves one frame), 2nd cap attempt
    # (video2) fails to open, 3rd cap (reverting back to video1) opens fine.
    caps = [FakeCap(opened=True, frames=[frame]), FakeCap(opened=False), FakeCap(opened=True, frames=[])]

    def cap_factory(*a, **k):
        return caps.pop(0) if caps else FakeCap(opened=True)
    monkeypatch.setattr(cv2, "VideoCapture", cap_factory)
    monkeypatch.setattr(sys, "argv", ["ipycam", "--source", str(video1)])

    assert main() == 0
    assert camera.notify_video_error_calls  # error was reported
    assert camera.stopped is True


def test_main_standard_mode_keyboard_interrupt(monkeypatch, patched_config, patched_logging):
    camera = FakeCamera(patched_config, is_running_seq=[True, True, True])

    def raise_interrupt(frame):
        raise KeyboardInterrupt()
    camera.stream = raise_interrupt
    _install_camera(monkeypatch, camera)
    _install_cap(monkeypatch, FakeCap(opened=True, frames=[np.zeros((2, 2, 3), np.uint8)]))
    monkeypatch.setattr(sys, "argv", ["ipycam", "--source", "0"])

    assert main() == 0
    assert camera.stopped is True


def test_main_video_upload_mode_placeholder_then_exit(monkeypatch, patched_config, patched_logging):
    camera = FakeCamera(patched_config, is_running_seq=[True, False])
    _install_camera(monkeypatch, camera)
    monkeypatch.setattr(sys, "argv", ["ipycam", "--source", "video"])

    assert main() == 0
    assert camera._video_upload_mode is True
    assert len(camera.stream_calls) == 1  # placeholder frame streamed once


def test_main_video_upload_mode_valid_video_streams_frames(monkeypatch, patched_config, tmp_path, patched_logging):
    """Covers the happy-path read AND the "loop the video" (ret=False ->
    cap.set(POS_FRAMES, 0); continue) branch inside the inner streaming loop."""
    video = tmp_path / "up.mp4"
    video.write_bytes(b"fake")
    frame = np.zeros((4, 4, 3), dtype=np.uint8)

    # outer-check(T) -> inner-check#1(T): read succeeds -> inner-check#2(T):
    # read fails (frames exhausted) -> "loop video" branch -> inner-check#3(F)
    # ends inner loop -> outer-check#2(F) ends outer loop.
    camera = FakeCamera(patched_config, is_running_seq=[True, True, True, False, False])
    camera.set_current_video_path(str(video))
    _install_camera(monkeypatch, camera)
    cap = FakeCap(opened=True, frames=[frame])
    _install_cap(monkeypatch, cap)
    monkeypatch.setattr(sys, "argv", ["ipycam", "--source", "video"])

    assert main() == 0
    assert camera.notify_video_loaded_calls == [str(video)]
    assert len(camera.stream_calls) == 1
    assert cap.set_calls  # cap.set(CAP_PROP_POS_FRAMES, 0) was hit on the failed read


def test_main_video_upload_mode_open_failure_reports_error(monkeypatch, patched_config, tmp_path, patched_logging):
    video = tmp_path / "bad.mp4"
    video.write_bytes(b"fake")

    camera = FakeCamera(patched_config, is_running_seq=[True, False])
    camera.set_current_video_path(str(video))
    _install_camera(monkeypatch, camera)
    _install_cap(monkeypatch, FakeCap(opened=False))
    monkeypatch.setattr(sys, "argv", ["ipycam", "--source", "video"])

    assert main() == 0
    assert camera.notify_video_error_calls


def test_main_standard_mode_uses_v4l2_backend_on_linux(monkeypatch, patched_config, patched_logging):
    """On Linux (and a non-file, non-webcam-index source is irrelevant here --
    the branch is keyed purely on platform.system()), device sources use the
    V4L2 backend constant instead of DSHOW."""
    camera = FakeCamera(patched_config, is_running_seq=[True, False])
    _install_camera(monkeypatch, camera)
    monkeypatch.setattr(main_mod.platform, "system", lambda: "Linux")

    captured = {}

    def cap_factory(source, backend=None):
        captured["backend"] = backend
        return FakeCap(opened=True, frames=[np.zeros((2, 2, 3), np.uint8)])
    monkeypatch.setattr(cv2, "VideoCapture", cap_factory)
    monkeypatch.setattr(sys, "argv", ["ipycam", "--source", "0"])

    assert main() == 0
    assert captured["backend"] == cv2.CAP_V4L2


def test_main_standard_mode_prefers_msmf_backend_on_windows(monkeypatch, patched_config, patched_logging):
    """On Windows, device sources try the MSMF backend first (DSHOW is slow)."""
    camera = FakeCamera(patched_config, is_running_seq=[True, False])
    _install_camera(monkeypatch, camera)
    monkeypatch.setattr(main_mod.platform, "system", lambda: "Windows")

    captured = []

    def cap_factory(source, backend=None):
        captured.append(backend)
        return FakeCap(opened=True, frames=[np.zeros((2, 2, 3), np.uint8)])
    monkeypatch.setattr(cv2, "VideoCapture", cap_factory)
    monkeypatch.setattr(sys, "argv", ["ipycam", "--source", "0"])

    assert main() == 0
    assert captured[0] == cv2.CAP_MSMF


def test_main_windows_falls_back_to_dshow_when_msmf_fails(monkeypatch, patched_config, patched_logging):
    """If MSMF can't open the device, main() falls back to DSHOW."""
    camera = FakeCamera(patched_config, is_running_seq=[True, False])
    _install_camera(monkeypatch, camera)
    monkeypatch.setattr(main_mod.platform, "system", lambda: "Windows")

    captured = []

    def cap_factory(source, backend=None):
        captured.append(backend)
        if backend == cv2.CAP_MSMF:
            return FakeCap(opened=False)
        return FakeCap(opened=True, frames=[np.zeros((2, 2, 3), np.uint8)])
    monkeypatch.setattr(cv2, "VideoCapture", cap_factory)
    monkeypatch.setattr(sys, "argv", ["ipycam", "--source", "0"])

    assert main() == 0
    assert captured == [cv2.CAP_MSMF, cv2.CAP_DSHOW]


def test_import_disables_msmf_hw_transforms():
    """Importing ipycam sets the MSMF hardware-transform workaround (before cv2
    loads inside the package), so Windows webcam capture isn't throttled."""
    import ipycam  # noqa: F401  -- already imported; asserts the side effect ran
    assert os.environ.get("OPENCV_VIDEOIO_MSMF_ENABLE_HW_TRANSFORMS") == "0"


def test_main_standard_mode_falls_back_to_videowriter_fourcc(monkeypatch, patched_config, patched_logging):
    """When cv2.VideoWriter_fourcc isn't available, main() must fall back to
    cv2.VideoWriter.fourcc instead of raising."""
    camera = FakeCamera(patched_config, is_running_seq=[True, False])
    _install_camera(monkeypatch, camera)
    _install_cap(monkeypatch, FakeCap(opened=True, frames=[np.zeros((2, 2, 3), np.uint8)]))
    monkeypatch.delattr(cv2, "VideoWriter_fourcc", raising=False)
    monkeypatch.setattr(sys, "argv", ["ipycam", "--source", "0"])

    assert main() == 0


def test_main_video_upload_mode_keyboard_interrupt(monkeypatch, patched_config, patched_logging):
    camera = FakeCamera(patched_config, is_running_seq=[True, True])

    call_count = {"n": 0}

    def fake_get_path():
        call_count["n"] += 1
        raise KeyboardInterrupt()
    camera.get_current_video_path = fake_get_path
    _install_camera(monkeypatch, camera)
    monkeypatch.setattr(sys, "argv", ["ipycam", "--source", "video"])

    assert main() == 0
    assert camera.stopped is True
