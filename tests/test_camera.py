"""
Tests for IPCamera snapshot handling (thread-safe frame capture).

These focus on the snapshot data-race fix: IPCamera.stream() stores an
independent copy of the latest frame under a lock, and get_snapshot_frame()
hands back a fresh copy so the HTTP snapshot thread never observes a frame that
the capture thread is mutating in place.
"""

import os
from unittest.mock import MagicMock

import numpy as np
import cv2
import pytest

from ipycam.camera import IPCamera
from ipycam.config import CameraConfig


def make_camera():
    """Construct an unstarted IPCamera with PTZ disabled.

    Disabling PTZ (and timestamp overlay) makes stream() a pass-through so the
    stored frame equals the input frame, keeping the copy assertions
    deterministic and cheap. The PTZ movement thread started in __init__ is
    stopped first.
    """
    config = CameraConfig(show_timestamp=False)
    camera = IPCamera(config)
    camera.ptz.stop()
    camera.ptz = None
    return camera


# ---------------------------------------------------------------------------
# go2rtc/FFmpeg permanent failure must not kill the whole camera loop.
# ---------------------------------------------------------------------------


def test_is_running_falls_back_to_mjpeg_when_go2rtc_streamer_dies_permanently():
    """VideoStreamer.is_running only goes False once its writer thread has
    exhausted its bounded reconnect attempts (see streamer.py _reconnect) --
    i.e. permanent failure, not a transient in-progress reconnect. When that
    happens, IPCamera.is_running must NOT go False and end the caller's
    capture loop (`while camera.is_running: camera.stream(frame)`); it must
    demote to the MJPEG-only fallback instead, since MJPEG/snapshot don't
    depend on the dead FFmpeg push at all.
    """
    camera = make_camera()
    camera._running = True
    camera._streaming_mode = 'go2rtc'

    dead_streamer = MagicMock()
    dead_streamer.is_running = False  # reconnect attempts exhausted
    camera.streamer = dead_streamer

    alive_mjpeg = MagicMock()
    alive_mjpeg.is_running = True
    alive_mjpeg.client_count = 0
    camera.mjpeg_streamer = alive_mjpeg

    assert camera.is_running is True, "camera must keep running (MJPEG fallback), not exit"
    assert camera.streaming_mode == 'mjpeg'
    assert camera.using_mjpeg_fallback is True

    # Once demoted, stream() must stop pushing frames into the dead streamer.
    frame = np.zeros((120, 160, 3), dtype=np.uint8)
    camera.stream(frame)
    dead_streamer.stream.assert_not_called()


def test_is_running_stays_true_during_transient_go2rtc_reconnect():
    """While the streamer is merely mid-reconnect (is_running still True per
    its own transient-vs-permanent contract), the camera must keep reporting
    the go2rtc mode as running -- no premature fallback."""
    camera = make_camera()
    camera._running = True
    camera._streaming_mode = 'go2rtc'

    reconnecting_streamer = MagicMock()
    reconnecting_streamer.is_running = True  # still trying, not exhausted
    camera.streamer = reconnecting_streamer

    assert camera.is_running is True
    assert camera.streaming_mode == 'go2rtc'
    assert camera.using_mjpeg_fallback is False


def test_get_snapshot_frame_none_before_any_frame():
    """No frame streamed yet -> getter returns None (not a stale/empty array)."""
    camera = make_camera()
    assert camera.get_snapshot_frame() is None


def test_snapshot_getter_returns_independent_copy():
    """A returned snapshot is isolated from later mutation of the buffers."""
    camera = make_camera()

    f1 = np.full((120, 160, 3), 10, dtype=np.uint8)
    camera.stream(f1)

    snap1 = camera.get_snapshot_frame()
    assert snap1 is not None
    snap1_ref = snap1.copy()

    # The getter must not hand back the internal buffer itself.
    assert snap1 is not camera._last_frame

    # stream() stored a COPY: mutating the original input frame in place must
    # not change what a subsequent snapshot returns.
    f1[:] = 99
    assert np.array_equal(camera.get_snapshot_frame(), snap1_ref)

    # The getter returned a COPY: mutating the internal buffer in place must not
    # retroactively change an already-returned snapshot.
    with camera._last_frame_lock:
        camera._last_frame[:] = 200
    assert np.array_equal(snap1, snap1_ref)

    # Streaming a fresh frame (simulating the capture thread's next iteration)
    # leaves the previously returned snapshot untouched.
    f2 = np.full((120, 160, 3), 250, dtype=np.uint8)
    camera.stream(f2)
    assert np.array_equal(snap1, snap1_ref)


# ---------------------------------------------------------------------------
# Web UI template escaping (get_web_ui_html)
# ---------------------------------------------------------------------------


class _CameraStub:
    """Bare object carrying only .config -- get_web_ui_html needs nothing else.

    Calling the method unbound on this stub avoids spinning up the PTZ thread
    and ONVIF service that IPCamera.__init__ creates.
    """

    def __init__(self, config):
        self.config = config


def render_web_ui(config):
    return IPCamera.get_web_ui_html(_CameraStub(config))


def test_web_ui_escapes_camera_name():
    """A config name containing <script> must not reach the HTML unescaped."""
    payload = '<script>alert(1)</script>'
    html = render_web_ui(CameraConfig(name=payload))
    assert payload not in html
    assert '&lt;script&gt;alert(1)&lt;/script&gt;' in html


def test_web_ui_escapes_source_info():
    """source_info sits in text AND a title attribute -> both need escaping."""
    payload = '"><img src=x onerror=alert(1)>'
    html = render_web_ui(CameraConfig(source_info=payload))
    assert payload not in html
    assert '<img src=x onerror=' not in html
    # Quote escaped so the title="..." attribute cannot be broken out of.
    assert '&quot;&gt;&lt;img' in html


def test_web_ui_normal_values_still_render():
    """Benign config values appear unaltered (escaping must not mangle them)."""
    config = CameraConfig(name='Lab Camera 3', source_info='video.mp4')
    html = render_web_ui(config)
    assert 'Lab Camera 3' in html
    assert 'video.mp4' in html
    # Stream URLs (no HTML-special chars in defaults) survive substitution.
    assert config.main_stream_rtsp in html


# ---------------------------------------------------------------------------
# Display transforms: flip / mirror / rotation (IPCamera._apply_display_transforms)
# ---------------------------------------------------------------------------


def _corner_frame(h=4, w=6):
    """A small asymmetric BGR frame with a distinct value at each corner, so a
    rotation/flip can be identified by checking which corner value ended up
    in which position.
    """
    frame = np.zeros((h, w, 3), dtype=np.uint8)
    frame[0, 0] = (1, 1, 1)          # top-left
    frame[0, w - 1] = (2, 2, 2)      # top-right
    frame[h - 1, 0] = (3, 3, 3)      # bottom-left
    frame[h - 1, w - 1] = (4, 4, 4)  # bottom-right
    return frame


class TestApplyDisplayTransforms:
    """Tests for IPCamera._apply_display_transforms (rotation/flip/mirror).

    Expected corner mappings below were verified empirically against cv2's
    documented rotate/flip semantics (cv2.ROTATE_90_CLOCKWISE,
    cv2.ROTATE_90_COUNTERCLOCKWISE, cv2.ROTATE_180, cv2.flip codes 0/1/-1)
    before being hard-coded here, so they double as a regression check on
    which cv2 constant/flip-code the implementation uses.
    """

    def test_no_transform_is_fast_path_returns_same_object(self):
        """All transforms at their no-op default -> the exact same frame
        object comes back (no copy, no cv2 call)."""
        camera = make_camera()
        frame = _corner_frame()
        result = camera._apply_display_transforms(frame)
        assert result is frame

    def test_rotation_90_swaps_dimensions_and_corners(self):
        camera = make_camera()
        camera.config.rotation = 90
        frame = _corner_frame(h=4, w=6)
        result = camera._apply_display_transforms(frame)

        assert result.shape == (6, 4, 3)  # (W, H, 3): dimensions swapped
        # Clockwise 90: TL->TR, TR->BR, BR->BL, BL->TL.
        assert tuple(result[0, 0]) == (3, 3, 3)    # orig BL
        assert tuple(result[0, -1]) == (1, 1, 1)   # orig TL
        assert tuple(result[-1, 0]) == (4, 4, 4)   # orig BR
        assert tuple(result[-1, -1]) == (2, 2, 2)  # orig TR

    def test_rotation_270_swaps_dimensions_and_corners(self):
        camera = make_camera()
        camera.config.rotation = 270
        frame = _corner_frame(h=4, w=6)
        result = camera._apply_display_transforms(frame)

        assert result.shape == (6, 4, 3)
        # Counter-clockwise 90 (== clockwise 270): TL->BL, TR->TL, BR->TR, BL->BR.
        assert tuple(result[0, 0]) == (2, 2, 2)    # orig TR
        assert tuple(result[0, -1]) == (4, 4, 4)   # orig BR
        assert tuple(result[-1, 0]) == (1, 1, 1)   # orig TL
        assert tuple(result[-1, -1]) == (3, 3, 3)  # orig BL

    def test_rotation_180_keeps_dimensions_flips_corners(self):
        camera = make_camera()
        camera.config.rotation = 180
        frame = _corner_frame(h=4, w=6)
        result = camera._apply_display_transforms(frame)

        assert result.shape == frame.shape  # no swap at 180
        assert tuple(result[0, 0]) == (4, 4, 4)    # orig BR
        assert tuple(result[0, -1]) == (3, 3, 3)   # orig BL
        assert tuple(result[-1, 0]) == (2, 2, 2)   # orig TR
        assert tuple(result[-1, -1]) == (1, 1, 1)  # orig TL

    def test_flip_is_vertical_inversion(self):
        """flip=True is a vertical flip: top/bottom rows swap, dims unchanged."""
        camera = make_camera()
        camera.config.flip = True
        frame = _corner_frame(h=4, w=6)
        result = camera._apply_display_transforms(frame)

        assert result.shape == frame.shape
        assert tuple(result[0, 0]) == (3, 3, 3)   # orig BL now on top
        assert tuple(result[-1, 0]) == (1, 1, 1)  # orig TL now on bottom
        assert np.array_equal(result, cv2.flip(frame, 0))

    def test_mirror_is_horizontal_inversion(self):
        """mirror=True is a horizontal flip: left/right columns swap."""
        camera = make_camera()
        camera.config.mirror = True
        frame = _corner_frame(h=4, w=6)
        result = camera._apply_display_transforms(frame)

        assert result.shape == frame.shape
        assert tuple(result[0, 0]) == (2, 2, 2)   # orig TR now on left
        assert tuple(result[0, -1]) == (1, 1, 1)  # orig TL now on right
        assert np.array_equal(result, cv2.flip(frame, 1))

    def test_flip_and_mirror_together_equal_180_rotation(self):
        """flip + mirror combined is mathematically identical to a 180-degree
        rotation for ANY frame content (both axes reversed either way)."""
        camera = make_camera()
        frame = np.random.randint(0, 256, (4, 6, 3), dtype=np.uint8)

        camera.config.flip = True
        camera.config.mirror = True
        combined = camera._apply_display_transforms(frame.copy())

        expected = cv2.rotate(frame, cv2.ROTATE_180)
        assert np.array_equal(combined, expected)

    def test_rotation_then_flip_mirror_order(self):
        """Documented order is rotate first, then flip/mirror -- verify the
        composed result matches manually rotating then flipping in that
        order (and NOT the other order, which would generally differ)."""
        camera = make_camera()
        camera.config.rotation = 90
        camera.config.mirror = True
        frame = _corner_frame(h=4, w=6)

        result = camera._apply_display_transforms(frame)
        expected = cv2.flip(cv2.rotate(frame, cv2.ROTATE_90_CLOCKWISE), 1)
        assert np.array_equal(result, expected)


class TestStreamDisplayTransformIntegration:
    """Integration: IPCamera.stream() applies display transforms after PTZ
    but before the timestamp overlay, so a configured rotation is reflected
    in what get_snapshot_frame() returns."""

    def test_stream_with_rotation_90_swaps_snapshot_dimensions(self):
        camera = make_camera()  # PTZ disabled, show_timestamp=False
        camera.config.rotation = 90

        frame = np.zeros((240, 320, 3), dtype=np.uint8)  # H=240, W=320
        camera.stream(frame)

        snap = camera.get_snapshot_frame()
        assert snap is not None
        assert snap.shape == (320, 240, 3)  # dimensions swapped

    def test_stream_with_no_transform_keeps_snapshot_dimensions(self):
        camera = make_camera()

        frame = np.zeros((240, 320, 3), dtype=np.uint8)
        camera.stream(frame)

        snap = camera.get_snapshot_frame()
        assert snap.shape == (240, 320, 3)


# ---------------------------------------------------------------------------
# Step 3.5 additions: start()/stop() service wiring, the go2rtc/native
# fallback ladder, restart_stream(), the remaining is_running branches,
# recording passthroughs, video-upload-mode bookkeeping, _cleanup_old_videos,
# and the get_web_ui_html FileNotFoundError branch.
#
# Every external dependency touched by start() (WS-Discovery, the threaded
# HTTP server, MJPEGStreamer, go2rtc/RTSP-port probes, VideoStreamer, the
# native RTSP server, native WebRTC) is patched out with MagicMocks, so no
# real socket/subprocess/port binding ever happens.
# ---------------------------------------------------------------------------


def _patch_start_dependencies(monkeypatch):
    """Patch every class/function IPCamera.start() touches."""
    mocks = {}

    mocks["discovery_cls"] = MagicMock()
    monkeypatch.setattr("ipycam.camera.WSDiscoveryServer", mocks["discovery_cls"])

    mocks["http_cls"] = MagicMock()
    monkeypatch.setattr("ipycam.camera.ReusableThreadingTCPServer", mocks["http_cls"])

    mocks["mjpeg_cls"] = MagicMock()
    monkeypatch.setattr("ipycam.camera.MJPEGStreamer", mocks["mjpeg_cls"])

    mocks["check_go2rtc"] = MagicMock(return_value=True)
    monkeypatch.setattr("ipycam.camera.check_go2rtc_running", mocks["check_go2rtc"])

    mocks["check_rtsp_port"] = MagicMock(return_value=True)
    monkeypatch.setattr("ipycam.camera.check_rtsp_port_available", mocks["check_rtsp_port"])

    mocks["streamer_cls"] = MagicMock()
    mocks["streamer_cls"].return_value.start.return_value = True
    monkeypatch.setattr("ipycam.camera.VideoStreamer", mocks["streamer_cls"])

    mocks["is_native_rtsp_available"] = MagicMock(return_value=False)
    monkeypatch.setattr("ipycam.camera.is_native_rtsp_available", mocks["is_native_rtsp_available"])

    mocks["rtsp_cls"] = MagicMock()
    mocks["rtsp_cls"].return_value.start.return_value = True
    monkeypatch.setattr("ipycam.camera.NativeRTSPServer", mocks["rtsp_cls"])

    mocks["is_webrtc_available"] = MagicMock(return_value=False)
    monkeypatch.setattr("ipycam.camera.is_webrtc_available", mocks["is_webrtc_available"])

    mocks["webrtc_cls"] = MagicMock()
    mocks["webrtc_cls"].return_value.start.return_value = True
    monkeypatch.setattr("ipycam.camera.NativeWebRTCStreamer", mocks["webrtc_cls"])

    return mocks


def make_camera_for_start(config=None):
    """An IPCamera with its PTZ movement thread stopped immediately (dimension
    assignments still work fine on a stopped controller) and its recorder
    replaced by a MagicMock so start() never spins up a real recorder worker."""
    config = config or CameraConfig()
    camera = IPCamera(config)
    camera.ptz.stop()
    camera.recorder = MagicMock()
    return camera


class TestStartFallbackLadder:
    """IPCamera.start()'s go2rtc -> native RTSP/WebRTC -> MJPEG-only ladder."""

    def test_go2rtc_available_and_streamer_starts(self, monkeypatch):
        mocks = _patch_start_dependencies(monkeypatch)
        camera = make_camera_for_start()

        assert camera.start() is True
        assert camera.streaming_mode == "go2rtc"
        assert camera.using_mjpeg_fallback is False
        mocks["discovery_cls"].return_value.start.assert_called_once()
        mocks["mjpeg_cls"].return_value.start.assert_called_once()
        mocks["streamer_cls"].return_value.start.assert_called_once()

    def test_go2rtc_streamer_start_failure_falls_back_to_native_rtsp_and_webrtc(self, monkeypatch):
        mocks = _patch_start_dependencies(monkeypatch)
        mocks["streamer_cls"].return_value.start.return_value = False
        mocks["is_native_rtsp_available"].return_value = True
        mocks["is_webrtc_available"].return_value = True
        camera = make_camera_for_start()

        assert camera.start() is True
        assert camera.streaming_mode == "native_rtsp_webrtc"
        assert camera.using_mjpeg_fallback is False
        camera._stop_rtsp_fanout()

    def test_go2rtc_unavailable_native_rtsp_only(self, monkeypatch):
        mocks = _patch_start_dependencies(monkeypatch)
        mocks["check_go2rtc"].return_value = False
        mocks["is_native_rtsp_available"].return_value = True
        mocks["is_webrtc_available"].return_value = False
        camera = make_camera_for_start()

        assert camera.start() is True
        assert camera.streaming_mode == "native_rtsp"
        camera._stop_rtsp_fanout()

    def test_go2rtc_not_detected_skips_doomed_push_and_logs_actionable_command(
        self, monkeypatch, caplog
    ):
        """When go2rtc isn't detected up front, the RTMP-push VideoStreamer must
        NOT be constructed/started (skip the doomed attempt, go straight to
        native), and an actionable 'go2rtc --config <packaged yaml>' command
        must be logged so the user can fix their setup."""
        mocks = _patch_start_dependencies(monkeypatch)
        mocks["check_go2rtc"].return_value = False
        mocks["is_native_rtsp_available"].return_value = True
        mocks["is_webrtc_available"].return_value = False
        camera = make_camera_for_start()

        with caplog.at_level("WARNING"):
            assert camera.start() is True

        # The go2rtc push path was never even constructed.
        mocks["streamer_cls"].assert_not_called()
        assert camera.streamer is None
        assert camera.streaming_mode == "native_rtsp"

        # Actionable startup command logged, pointing at the packaged config.
        msgs = " ".join(r.getMessage() for r in caplog.records)
        assert "go2rtc not detected" in msgs
        assert "go2rtc --config" in msgs
        assert "go2rtc.yaml" in msgs

        camera._stop_rtsp_fanout()

    def test_go2rtc_available_constructs_streamer_and_pushes(self, monkeypatch):
        """Sanity counterpart: when go2rtc IS detected, the VideoStreamer push
        path is taken (streamer constructed + started)."""
        mocks = _patch_start_dependencies(monkeypatch)
        camera = make_camera_for_start()

        assert camera.start() is True
        assert camera.streaming_mode == "go2rtc"
        mocks["streamer_cls"].assert_called_once()
        mocks["streamer_cls"].return_value.start.assert_called_once()

    def test_go2rtc_unavailable_native_webrtc_only(self, monkeypatch):
        mocks = _patch_start_dependencies(monkeypatch)
        mocks["check_rtsp_port"].return_value = False
        mocks["is_native_rtsp_available"].return_value = False
        mocks["is_webrtc_available"].return_value = True
        camera = make_camera_for_start()

        assert camera.start() is True
        assert camera.streaming_mode == "native_webrtc"

    def test_nothing_available_falls_back_to_mjpeg_only(self, monkeypatch):
        mocks = _patch_start_dependencies(monkeypatch)
        mocks["check_go2rtc"].return_value = False
        mocks["is_native_rtsp_available"].return_value = False
        mocks["is_webrtc_available"].return_value = False
        camera = make_camera_for_start()

        assert camera.start() is True
        assert camera.streaming_mode == "mjpeg"
        assert camera.using_mjpeg_fallback is True

    def test_native_rtsp_server_start_failure_is_handled(self, monkeypatch):
        mocks = _patch_start_dependencies(monkeypatch)
        mocks["check_go2rtc"].return_value = False
        mocks["is_native_rtsp_available"].return_value = True
        mocks["rtsp_cls"].return_value.start.return_value = False
        mocks["is_webrtc_available"].return_value = False
        camera = make_camera_for_start()

        assert camera.start() is True
        assert camera.rtsp_server is None
        assert camera.streaming_mode == "mjpeg"

    def test_native_rtsp_constructor_exception_is_handled(self, monkeypatch):
        mocks = _patch_start_dependencies(monkeypatch)
        mocks["check_go2rtc"].return_value = False
        mocks["is_native_rtsp_available"].return_value = True
        mocks["rtsp_cls"].side_effect = RuntimeError("ffmpeg missing")
        mocks["is_webrtc_available"].return_value = False
        camera = make_camera_for_start()

        assert camera.start() is True
        assert camera.rtsp_server is None

    def test_native_webrtc_start_failure_is_handled(self, monkeypatch):
        mocks = _patch_start_dependencies(monkeypatch)
        mocks["check_go2rtc"].return_value = False
        mocks["is_native_rtsp_available"].return_value = False
        mocks["is_webrtc_available"].return_value = True
        mocks["webrtc_cls"].return_value.start.return_value = False
        camera = make_camera_for_start()

        assert camera.start() is True
        assert camera.webrtc_streamer is None
        assert camera.streaming_mode == "mjpeg"

    def test_native_webrtc_constructor_exception_is_handled(self, monkeypatch):
        mocks = _patch_start_dependencies(monkeypatch)
        mocks["check_go2rtc"].return_value = False
        mocks["is_native_rtsp_available"].return_value = False
        mocks["is_webrtc_available"].return_value = True
        mocks["webrtc_cls"].side_effect = RuntimeError("aiortc missing")
        camera = make_camera_for_start()

        assert camera.start() is True
        assert camera.webrtc_streamer is None

    def test_recording_enabled_starts_recorder_worker(self, monkeypatch):
        _patch_start_dependencies(monkeypatch)
        config = CameraConfig(recording_enabled=True)
        camera = make_camera_for_start(config)

        camera.start()

        camera.recorder.start.assert_called_once()

    def test_recording_disabled_does_not_start_recorder_worker(self, monkeypatch):
        _patch_start_dependencies(monkeypatch)
        config = CameraConfig(recording_enabled=False)
        camera = make_camera_for_start(config)

        camera.start()

        camera.recorder.start.assert_not_called()


class TestMjpegStreamerSubDimensions:
    """The native MJPEG streamer must be built with the CONFIGURED sub
    resolution so the web UI's Main/Sub preview toggle actually serves the
    user's sub dims (e.g. 640x360) rather than falling back to a per-frame
    half-size sub (see MJPEGStreamer._resolve_sub_size)."""

    def test_start_wires_config_sub_dimensions_into_mjpeg_streamer(self, monkeypatch):
        from ipycam.mjpeg import MJPEGStreamer as RealMJPEGStreamer

        _patch_start_dependencies(monkeypatch)
        # Use the REAL MJPEGStreamer (undo the helper's mock) so the actual
        # wired attributes can be inspected.
        monkeypatch.setattr("ipycam.camera.MJPEGStreamer", RealMJPEGStreamer)

        # Distinct, non-default sub dims that are NOT a half of the main dims,
        # so a pass-through can be told apart from the dynamic-halving fallback.
        config = CameraConfig(
            main_width=1280, main_height=720,
            sub_width=321, sub_height=241,
        )
        camera = make_camera_for_start(config)
        try:
            assert camera.start() is True
            assert camera.mjpeg_streamer is not None
            assert camera.mjpeg_streamer.sub_width == config.sub_width == 321
            assert camera.mjpeg_streamer.sub_height == config.sub_height == 241
        finally:
            camera.stop()


class TestStop:
    def test_stop_calls_stop_on_every_subsystem(self, monkeypatch):
        _patch_start_dependencies(monkeypatch)
        camera = make_camera_for_start()
        camera._running = True
        camera.streamer = MagicMock()
        camera.webrtc_streamer = MagicMock()
        camera.rtsp_server = MagicMock()
        camera.mjpeg_streamer = MagicMock()
        camera._discovery = MagicMock()
        camera._http_server = MagicMock()

        camera.stop()

        assert camera._running is False
        camera.streamer.stop.assert_called_once()
        camera.webrtc_streamer.stop.assert_called_once()
        camera.rtsp_server.stop.assert_called_once()
        camera.mjpeg_streamer.stop.assert_called_once()
        camera.recorder.stop.assert_called_once()
        camera._discovery.stop.assert_called_once()
        camera._http_server.shutdown.assert_called_once()
        camera._http_server.server_close.assert_called_once()

    def test_stop_is_safe_with_no_subsystems_started(self):
        camera = make_camera_for_start()
        camera.stop()  # must not raise
        assert camera._running is False


class TestRestartStream:
    def test_restart_success_replaces_streamer_and_updates_ptz_dims(self, monkeypatch):
        old_streamer = MagicMock()
        camera = make_camera_for_start()
        camera.streamer = old_streamer
        camera.config.main_width = 800
        camera.config.main_height = 600

        new_streamer_cls = MagicMock()
        new_streamer_cls.return_value.start.return_value = True
        monkeypatch.setattr("ipycam.camera.VideoStreamer", new_streamer_cls)

        assert camera.restart_stream() is True
        old_streamer.stop.assert_called_once()
        assert camera.streamer is new_streamer_cls.return_value
        assert camera.ptz.output_width == 800
        assert camera.ptz.output_height == 600
        assert camera._restarting is False

    def test_restart_failure_returns_false(self, monkeypatch):
        camera = make_camera_for_start()
        failing_cls = MagicMock()
        failing_cls.return_value.start.return_value = False
        monkeypatch.setattr("ipycam.camera.VideoStreamer", failing_cls)

        assert camera.restart_stream() is False
        assert camera._restarting is False

    def test_restarting_flag_keeps_is_running_true_even_with_no_streamer(self):
        camera = make_camera_for_start()
        camera._running = True
        camera._restarting = True
        camera.streamer = None
        assert camera.is_running is True


class TestIsRunningOtherModes:
    def test_native_webrtc_mode_running(self):
        camera = make_camera_for_start()
        camera._running = True
        camera._streaming_mode = "native_webrtc"
        camera.webrtc_streamer = MagicMock(is_running=True)
        assert camera.is_running is True

    def test_native_webrtc_mode_not_running(self):
        camera = make_camera_for_start()
        camera._running = True
        camera._streaming_mode = "native_webrtc"
        camera.webrtc_streamer = None
        assert camera.is_running is False

    def test_native_rtsp_mode_running(self):
        camera = make_camera_for_start()
        camera._running = True
        camera._streaming_mode = "native_rtsp"
        camera.rtsp_server = MagicMock(is_running=True)
        assert camera.is_running is True

    def test_native_rtsp_webrtc_mode_running(self):
        camera = make_camera_for_start()
        camera._running = True
        camera._streaming_mode = "native_rtsp_webrtc"
        camera.rtsp_server = MagicMock(is_running=True)
        assert camera.is_running is True

    def test_native_rtsp_mode_server_gone(self):
        camera = make_camera_for_start()
        camera._running = True
        camera._streaming_mode = "native_rtsp"
        camera.rtsp_server = None
        assert camera.is_running is False

    def test_mjpeg_fallback_mode_running(self):
        camera = make_camera_for_start()
        camera._running = True
        camera._streaming_mode = "mjpeg"
        camera.mjpeg_streamer = MagicMock(is_running=True)
        assert camera.is_running is True

    def test_mjpeg_fallback_mode_not_running(self):
        camera = make_camera_for_start()
        camera._running = True
        camera._streaming_mode = "mjpeg"
        camera.mjpeg_streamer = None
        assert camera.is_running is False

    def test_not_running_when_camera_stopped_regardless_of_mode(self):
        camera = make_camera_for_start()
        camera._running = False
        camera._streaming_mode = "mjpeg"
        camera.mjpeg_streamer = MagicMock(is_running=True)
        assert camera.is_running is False


class TestRtspFanoutWorker:
    def test_fanout_resizes_sub_stream_when_dimensions_differ(self):
        """Runs the REAL _rtsp_fanout_loop worker thread (not a re-implemented
        copy of its logic) so the cv2.resize branch is genuinely exercised."""
        camera = make_camera_for_start()
        camera.config.main_width, camera.config.main_height = 320, 240
        camera.config.sub_width, camera.config.sub_height = 160, 120
        server = MagicMock(is_running=True)
        camera.rtsp_server = server
        camera._start_rtsp_fanout()
        try:
            frame = np.zeros((240, 320, 3), dtype=np.uint8)
            camera._rtsp_frame_queue.put(frame)

            import time as _time
            deadline = _time.time() + 2.0
            while _time.time() < deadline and server.stream_frame.call_count < 2:
                _time.sleep(0.01)
            assert server.stream_frame.call_count >= 2

            # Second call is the sub-stream -- must have been resized down.
            sub_call = server.stream_frame.call_args_list[1]
            sub_frame_arg = sub_call.args[1]
            assert sub_frame_arg.shape[:2] == (120, 160)
        finally:
            camera._stop_rtsp_fanout()

    def test_fanout_loop_forwards_main_and_sub_streams(self):
        camera = make_camera_for_start()
        camera.config.sub_width, camera.config.sub_height = camera.config.main_width, camera.config.main_height
        server = MagicMock(is_running=True)
        camera.rtsp_server = server
        camera._start_rtsp_fanout()
        try:
            frame = np.zeros((camera.config.main_height, camera.config.main_width, 3), dtype=np.uint8)
            camera._rtsp_frame_queue.put(frame)

            import time as _time
            deadline = _time.time() + 2.0
            while _time.time() < deadline and server.stream_frame.call_count < 2:
                _time.sleep(0.01)
            assert server.stream_frame.call_count >= 2
        finally:
            camera._stop_rtsp_fanout()

    def test_fanout_loop_survives_stream_frame_exception(self):
        camera = make_camera_for_start()
        server = MagicMock(is_running=True)
        server.stream_frame.side_effect = RuntimeError("boom")
        camera.rtsp_server = server
        camera._start_rtsp_fanout()
        try:
            frame = np.zeros((camera.config.main_height, camera.config.main_width, 3), dtype=np.uint8)
            camera._rtsp_frame_queue.put(frame)
            import time as _time
            _time.sleep(0.2)  # give the worker a chance to hit (and survive) the exception
        finally:
            camera._stop_rtsp_fanout()

    def test_fanout_loop_skips_frame_when_server_not_running(self):
        camera = make_camera_for_start()
        camera.rtsp_server = MagicMock(is_running=False)
        camera._start_rtsp_fanout()
        try:
            frame = np.zeros((camera.config.main_height, camera.config.main_width, 3), dtype=np.uint8)
            camera._rtsp_frame_queue.put(frame)
            import time as _time
            _time.sleep(0.2)  # give the worker a chance to pull and skip it
            camera.rtsp_server.stream_frame.assert_not_called()
        finally:
            camera._stop_rtsp_fanout()

    def test_stream_enqueues_into_rtsp_fanout_when_server_running(self):
        camera = make_camera_for_start()
        camera.rtsp_server = MagicMock(is_running=True)
        frame = np.zeros((240, 320, 3), dtype=np.uint8)
        camera.stream(frame)
        queued = camera._rtsp_frame_queue.get(timeout=0.5)
        assert queued is not None


class TestRecordingPassthroughs:
    def test_start_recording_no_recorder_returns_false(self):
        camera = make_camera_for_start()
        camera.recorder = None
        assert camera.start_recording() is False

    def test_start_recording_delegates(self):
        camera = make_camera_for_start()
        camera.recorder.start_recording.return_value = True
        assert camera.start_recording() is True
        camera.recorder.start_recording.assert_called_once()

    def test_stop_recording_no_recorder_returns_empty_list(self):
        camera = make_camera_for_start()
        camera.recorder = None
        assert camera.stop_recording() == []

    def test_stop_recording_delegates(self):
        camera = make_camera_for_start()
        camera.recorder.stop_recording.return_value = ["seg1.mp4"]
        assert camera.stop_recording() == ["seg1.mp4"]

    def test_is_recording_no_recorder(self):
        camera = make_camera_for_start()
        camera.recorder = None
        assert camera.is_recording is False

    def test_is_recording_delegates(self):
        camera = make_camera_for_start()
        camera.recorder.is_recording = True
        assert camera.is_recording is True

    def test_recording_stats_no_recorder(self):
        camera = make_camera_for_start()
        camera.recorder = None
        stats = camera.recording_stats
        assert stats == {"recording": False, "worker_running": False}

    def test_recording_stats_delegates(self):
        camera = make_camera_for_start()
        camera.recorder.stats.return_value = {"recording": True}
        assert camera.recording_stats == {"recording": True}

    def test_apply_recording_config_no_recorder_is_noop(self):
        camera = make_camera_for_start()
        camera.recorder = None
        camera.apply_recording_config()  # must not raise

    def test_apply_recording_config_starts_worker_when_enabled_and_idle(self):
        camera = make_camera_for_start()
        camera.config.recording_enabled = True
        camera.recorder.is_worker_running = False
        camera.apply_recording_config()
        camera.recorder.reconfigure.assert_called_once()
        camera.recorder.start.assert_called_once()

    def test_apply_recording_config_stops_worker_when_disabled_and_idle(self):
        camera = make_camera_for_start()
        camera.config.recording_enabled = False
        camera.recorder.is_worker_running = True
        camera.recorder.is_recording = False
        camera.apply_recording_config()
        camera.recorder.stop.assert_called_once()

    def test_apply_recording_config_leaves_active_recording_alone(self):
        camera = make_camera_for_start()
        camera.config.recording_enabled = False
        camera.recorder.is_worker_running = True
        camera.recorder.is_recording = True
        camera.apply_recording_config()
        camera.recorder.stop.assert_not_called()


class TestVideoUploadMode:
    def test_set_and_get_video_upload_mode(self):
        camera = make_camera_for_start()
        assert camera.video_upload_mode is False
        camera.set_video_upload_mode(True)
        assert camera.video_upload_mode is True

    def test_current_video_path_defaults_to_none(self):
        camera = make_camera_for_start()
        assert camera.get_current_video_path() is None

    def test_set_current_video_path_tracks_previous(self):
        camera = make_camera_for_start()
        camera.set_current_video_path("/videos/a.mp4")
        assert camera.get_current_video_path() == "/videos/a.mp4"
        assert camera.get_previous_video_path() is None

        camera.set_current_video_path("/videos/b.mp4")
        assert camera.get_current_video_path() == "/videos/b.mp4"
        assert camera.get_previous_video_path() == "/videos/a.mp4"

    def test_set_current_video_path_clears_error(self):
        camera = make_camera_for_start()
        camera._video_error = "boom"
        camera.set_current_video_path("/videos/a.mp4")
        assert camera.get_video_error() is None

    def test_notify_video_error_reverts_to_previous(self):
        camera = make_camera_for_start()
        camera.set_current_video_path("/videos/a.mp4")
        camera.set_current_video_path("/videos/b.mp4")
        camera.notify_video_error("could not open b.mp4")
        assert camera.get_video_error() == "could not open b.mp4"
        assert camera.get_current_video_path() == "/videos/a.mp4"
        assert camera.get_previous_video_path() is None

    def test_notify_video_error_without_previous_keeps_current(self):
        camera = make_camera_for_start()
        camera.set_current_video_path("/videos/a.mp4")
        camera.notify_video_error("could not open a.mp4")
        assert camera.get_current_video_path() == "/videos/a.mp4"

    def test_clear_video_error(self):
        camera = make_camera_for_start()
        camera._video_error = "boom"
        camera.clear_video_error()
        assert camera.get_video_error() is None

    def test_notify_video_loaded_clears_error_and_updates_source_info(self, monkeypatch):
        camera = make_camera_for_start()
        camera._video_error = "old error"
        monkeypatch.setattr(camera, "_cleanup_old_videos", MagicMock())
        camera.notify_video_loaded("/videos/clip.mp4")
        assert camera.get_video_error() is None
        assert camera.config.source_info == "clip.mp4"
        camera._cleanup_old_videos.assert_called_once_with("/videos/clip.mp4")


class TestCleanupOldVideos:
    def test_missing_videos_dir_is_a_noop(self, monkeypatch, tmp_path):
        camera = make_camera_for_start()
        fake_module_file = tmp_path / "pkgdir" / "camera.py"
        monkeypatch.setattr("ipycam.camera.__file__", str(fake_module_file))
        # tmp_path/videos does not exist -> early return, no exception.
        camera._cleanup_old_videos(str(tmp_path / "current.mp4"))

    def test_removes_other_videos_keeps_current_and_non_video_files(self, monkeypatch, tmp_path):
        camera = make_camera_for_start()
        fake_module_file = tmp_path / "pkgdir" / "camera.py"
        videos_dir = tmp_path / "videos"
        videos_dir.mkdir()
        monkeypatch.setattr("ipycam.camera.__file__", str(fake_module_file))

        current = videos_dir / "current.mp4"
        current.write_bytes(b"keep me")
        old1 = videos_dir / "old1.mp4"
        old1.write_bytes(b"delete me")
        old2 = videos_dir / "old2.avi"
        old2.write_bytes(b"delete me too")
        readme = videos_dir / "README.txt"
        readme.write_bytes(b"not a video, keep")

        camera._cleanup_old_videos(str(current))

        remaining = {p.name for p in videos_dir.iterdir()}
        assert remaining == {"current.mp4", "README.txt"}

    def test_os_remove_exception_is_logged_and_swallowed(self, monkeypatch, tmp_path):
        camera = make_camera_for_start()
        fake_module_file = tmp_path / "pkgdir" / "camera.py"
        videos_dir = tmp_path / "videos"
        videos_dir.mkdir()
        monkeypatch.setattr("ipycam.camera.__file__", str(fake_module_file))

        old1 = videos_dir / "old1.mp4"
        old1.write_bytes(b"delete me")

        monkeypatch.setattr("ipycam.camera.os.remove", MagicMock(side_effect=OSError("locked")))

        camera._cleanup_old_videos(str(videos_dir / "current.mp4"))  # must not raise
        assert old1.exists()  # removal failed, file still present

    def test_outer_exception_is_swallowed(self, monkeypatch, tmp_path):
        camera = make_camera_for_start()
        fake_module_file = tmp_path / "pkgdir" / "camera.py"
        videos_dir = tmp_path / "videos"
        videos_dir.mkdir()
        monkeypatch.setattr("ipycam.camera.__file__", str(fake_module_file))
        monkeypatch.setattr(
            "ipycam.camera.os.listdir", MagicMock(side_effect=OSError("permission denied"))
        )

        camera._cleanup_old_videos(str(videos_dir / "current.mp4"))  # must not raise


class TestStreamFanOutAllOutputs:
    """Exercise every conditional fan-out branch in IPCamera.stream() in one
    pass: MJPEG (watched), native WebRTC (peer connected), native RTSP
    (running), recorder (wants frames), and the go2rtc streamer itself."""

    def test_all_fanouts_receive_the_frame_when_active(self):
        camera = make_camera()  # PTZ disabled, show_timestamp=False
        camera.mjpeg_streamer = MagicMock(client_count=1)
        camera.webrtc_streamer = MagicMock(connection_count=1)
        camera.rtsp_server = MagicMock(is_running=True)
        camera.recorder = MagicMock(wants_frames=True)
        camera.streamer = MagicMock()
        camera.streamer.stream.return_value = True
        camera._use_mjpeg_fallback = False

        frame = np.zeros((120, 160, 3), dtype=np.uint8)
        result = camera.stream(frame)

        camera.mjpeg_streamer.stream_frame.assert_called_once()
        camera.webrtc_streamer.stream_frame.assert_called_once()
        camera.recorder.submit.assert_called_once()
        camera.streamer.stream.assert_called_once()
        assert result is True

    def test_fanouts_skipped_when_idle_or_unwatched(self):
        camera = make_camera()
        camera.mjpeg_streamer = MagicMock(client_count=0)
        camera.webrtc_streamer = MagicMock(connection_count=0)
        camera.rtsp_server = None
        camera.recorder = MagicMock(wants_frames=False)
        camera.streamer = None
        camera._use_mjpeg_fallback = True

        frame = np.zeros((120, 160, 3), dtype=np.uint8)
        result = camera.stream(frame)

        camera.mjpeg_streamer.stream_frame.assert_not_called()
        camera.webrtc_streamer.stream_frame.assert_not_called()
        camera.recorder.submit.assert_not_called()
        assert result is True  # default when go2rtc streamer is bypassed


class TestStatsProperty:
    def test_stats_none_without_streamer(self):
        camera = make_camera()
        camera.streamer = None
        assert camera.stats is None

    def test_stats_delegates_to_streamer(self):
        camera = make_camera()
        fake_stats = object()
        camera.streamer = MagicMock(stats=fake_stats)
        assert camera.stats is fake_stats


class TestDrawTimestampPositions:
    """IPCamera._draw_timestamp positions the overlay per config.timestamp_position."""

    def _frame(self):
        return np.zeros((100, 200, 3), dtype=np.uint8)

    def test_top_left(self):
        camera = make_camera()
        camera.config.timestamp_position = "top-left"
        result = camera._draw_timestamp(self._frame())
        assert result.shape == (100, 200, 3)

    def test_top_right(self):
        camera = make_camera()
        camera.config.timestamp_position = "top-right"
        result = camera._draw_timestamp(self._frame())
        assert result.shape == (100, 200, 3)

    def test_bottom_right(self):
        camera = make_camera()
        camera.config.timestamp_position = "bottom-right"
        result = camera._draw_timestamp(self._frame())
        assert result.shape == (100, 200, 3)

    def test_bottom_left_default(self):
        camera = make_camera()
        camera.config.timestamp_position = "bottom-left"
        result = camera._draw_timestamp(self._frame())
        assert result.shape == (100, 200, 3)


class TestGetWebUiHtmlMissingTemplate:
    def test_missing_template_returns_error_html(self, monkeypatch, tmp_path):
        camera = make_camera_for_start()
        fake_module_file = tmp_path / "pkgdir" / "camera.py"  # no static/index.html alongside it
        monkeypatch.setattr("ipycam.camera.__file__", str(fake_module_file))

        html = camera.get_web_ui_html()
        assert "Error: Template not found" in html
        assert "static/index.html is missing" in html
