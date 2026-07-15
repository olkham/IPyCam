"""
Tests for NativeRTSPServer frame handling.

Step 2.2 replaced the defensive ``frame.copy()`` in stream_frame() with a
reference store: the RTSP fan-out worker only ever hands the server the
immutable ``outbound`` frame or a freshly-resized sub-frame, so a second copy
was pure waste. These tests pin the new no-copy contract (the encoder loop is
read-only) without needing FFmpeg.
"""

import numpy as np

from ipycam.rtsp import NativeRTSPServer


def _server_with_stream(name="video_main", w=160, h=120):
    server = NativeRTSPServer(port=0)
    server.add_stream(name=name, width=w, height=h, fps=10)
    server._is_running = True  # allow stream_frame without binding a socket
    return server


def test_stream_frame_stores_reference_not_copy():
    """stream_frame must store the exact object it was given (no memcpy)."""
    server = _server_with_stream()
    frame = np.full((120, 160, 3), 7, dtype=np.uint8)

    assert server.stream_frame("video_main", frame) is True
    assert server._frame_buffers["video_main"] is frame


def test_stream_frame_unknown_stream_rejected():
    """Frames for an unregistered stream are rejected (and stored nowhere)."""
    server = _server_with_stream()
    frame = np.zeros((120, 160, 3), dtype=np.uint8)
    assert server.stream_frame("does_not_exist", frame) is False


def test_stream_frame_not_running_rejected():
    """When the server is not running, stream_frame is a no-op returning False."""
    server = NativeRTSPServer(port=0)
    server.add_stream(name="video_main", width=160, height=120, fps=10)
    # _is_running left False
    assert server.stream_frame("video_main", np.zeros((120, 160, 3), np.uint8)) is False
    assert server._frame_buffers["video_main"] is None


# ---------------------------------------------------------------------------
# Step 3.5 additions: request parsing/dispatch, SDP generation, SETUP
# transport negotiation, session lifecycle, RTP/FFmpeg plumbing, and the
# accept/client-handling loops. Everything below is hermetic: sockets and
# subprocess.Popen are mocked, no real network or ffmpeg is touched.
# ---------------------------------------------------------------------------

import base64
import itertools
import re
import socket as socket_module
import struct
import subprocess
import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from ipycam.rtsp import (
    NativeRTSPServer,
    RTSPSession,
    RTSPState,
    RTSPStreamInfo,
    is_native_rtsp_available,
)


# Known-good base64 SPS/PPS tokens used by the autouse probe stub below so the
# whole suite stays hermetic (never spawns a real ffmpeg to derive them).
FAKE_SPS_B64 = "Z0LAHtoHgUZA"
FAKE_PPS_B64 = "aM4G4g=="

# Capture the real probe at import time (before the autouse stub replaces it) so
# the dedicated probe test can exercise the genuine ffmpeg-command construction.
_REAL_PROBE = NativeRTSPServer._probe_h264_parameter_sets


@pytest.fixture(autouse=True)
def _stub_sprop_probe(monkeypatch):
    """Keep SDP generation hermetic: _generate_sdp lazily probes ffmpeg for the
    H.264 SPS/PPS, so stub that probe for every test. Individual tests override
    this instance-level to exercise the success/failure paths explicitly."""
    monkeypatch.setattr(
        NativeRTSPServer,
        "_probe_h264_parameter_sets",
        lambda self, w, h, fps: (FAKE_SPS_B64, FAKE_PPS_B64),
    )


class SpyLock:
    """A real, working lock that also counts how many times it was entered, so
    tests can assert a write path actually went through the session send_lock."""

    def __init__(self):
        self._lock = threading.Lock()
        self.acquire_count = 0

    def __enter__(self):
        self.acquire_count += 1
        return self._lock.__enter__()

    def __exit__(self, *exc):
        return self._lock.__exit__(*exc)

    def acquire(self, *a, **k):
        self.acquire_count += 1
        return self._lock.acquire(*a, **k)

    def release(self):
        return self._lock.release()


class FakeThread:
    """Stand-in for threading.Thread that records target/args but never
    actually runs anything, so tests can assert *what* would have been
    started without spinning up real background threads."""

    def __init__(self, target=None, args=(), kwargs=None, daemon=None, name=None):
        self.target = target
        self.args = args
        self.kwargs = kwargs or {}
        self.daemon = daemon
        self.name = name
        self._alive = False

    def start(self):
        self._alive = True

    def join(self, timeout=None):
        pass

    def is_alive(self):
        return self._alive


def make_session(session_id="sess1", state=RTSPState.READY, **kwargs):
    return RTSPSession(
        session_id=session_id,
        client_socket=MagicMock(),
        client_address=("10.0.0.5", 5555),
        state=state,
        **kwargs,
    )


# ---------------------------------------------------------------------------
# OPTIONS / error responses
# ---------------------------------------------------------------------------


class TestOptionsAndErrors:
    def test_handle_options_contains_methods_and_cseq(self):
        server = _server_with_stream()
        resp = server._handle_options(42)
        assert resp.startswith("RTSP/1.0 200 OK\r\n")
        assert "CSeq: 42" in resp
        assert "OPTIONS, DESCRIBE, SETUP, PLAY, PAUSE, TEARDOWN" in resp

    def test_error_response_format(self):
        server = _server_with_stream()
        resp = server._error_response(404, "Stream Not Found", 7)
        assert resp == "RTSP/1.0 404 Stream Not Found\r\nCSeq: 7\r\n\r\n"


# ---------------------------------------------------------------------------
# _handle_rtsp_request dispatch + URI/stream-name parsing
# ---------------------------------------------------------------------------


class TestHandleRtspRequestDispatch:
    def test_bad_request_line_too_few_tokens(self):
        server = _server_with_stream()
        resp, sid = server._handle_rtsp_request(
            "BADREQUESTLINE\r\n\r\n", MagicMock(), ("1.2.3.4", 1), None
        )
        assert "400 Bad Request" in resp
        assert sid is None

    def test_missing_cseq_header_defaults_to_zero(self):
        server = _server_with_stream()
        resp, sid = server._handle_rtsp_request(
            "OPTIONS rtsp://host/video_main RTSP/1.0\r\n\r\n", MagicMock(), ("1.2.3.4", 1), None
        )
        assert "CSeq: 0" in resp

    def test_options_dispatch(self):
        server = _server_with_stream()
        resp, sid = server._handle_rtsp_request(
            "OPTIONS rtsp://host/video_main RTSP/1.0\r\nCSeq: 1\r\n\r\n",
            MagicMock(), ("1.2.3.4", 1), None,
        )
        assert "Public: OPTIONS" in resp

    def test_describe_dispatch_known_stream(self):
        server = _server_with_stream()
        resp, sid = server._handle_rtsp_request(
            "DESCRIBE rtsp://host/video_main RTSP/1.0\r\nCSeq: 2\r\n\r\n",
            MagicMock(), ("1.2.3.4", 1), None,
        )
        assert "200 OK" in resp
        assert "application/sdp" in resp

    def test_describe_dispatch_unknown_stream(self):
        server = _server_with_stream()
        resp, sid = server._handle_rtsp_request(
            "DESCRIBE rtsp://host/does_not_exist RTSP/1.0\r\nCSeq: 2\r\n\r\n",
            MagicMock(), ("1.2.3.4", 1), None,
        )
        assert "404 Stream Not Found" in resp

    def test_play_dispatch_unknown_session(self):
        server = _server_with_stream()
        resp, sid = server._handle_rtsp_request(
            "PLAY rtsp://host/video_main RTSP/1.0\r\nCSeq: 3\r\nSession: unknown1\r\n\r\n",
            MagicMock(), ("1.2.3.4", 1), None,
        )
        assert "454 Session Not Found" in resp
        # Session header must be extracted even though the session is unknown.
        assert sid == "unknown1"

    def test_pause_dispatch_unknown_session(self):
        server = _server_with_stream()
        resp, sid = server._handle_rtsp_request(
            "PAUSE rtsp://host/video_main RTSP/1.0\r\nCSeq: 3\r\n\r\n",
            MagicMock(), ("1.2.3.4", 1), None,
        )
        assert "454" in resp

    def test_teardown_dispatch_unknown_session(self):
        server = _server_with_stream()
        resp, sid = server._handle_rtsp_request(
            "TEARDOWN rtsp://host/video_main RTSP/1.0\r\nCSeq: 3\r\n\r\n",
            MagicMock(), ("1.2.3.4", 1), None,
        )
        assert "454" in resp

    def test_get_parameter_dispatch_no_session(self):
        server = _server_with_stream()
        resp, sid = server._handle_rtsp_request(
            "GET_PARAMETER rtsp://host/video_main RTSP/1.0\r\nCSeq: 9\r\n\r\n",
            MagicMock(), ("1.2.3.4", 1), None,
        )
        assert "200 OK" in resp
        assert "Session:" not in resp

    def test_set_parameter_dispatch_no_session(self):
        server = _server_with_stream()
        resp, sid = server._handle_rtsp_request(
            "SET_PARAMETER rtsp://host/video_main RTSP/1.0\r\nCSeq: 9\r\n\r\n",
            MagicMock(), ("1.2.3.4", 1), None,
        )
        assert "200 OK" in resp

    def test_announce_not_allowed(self):
        server = _server_with_stream()
        resp, sid = server._handle_rtsp_request(
            "ANNOUNCE rtsp://host/video_main RTSP/1.0\r\nCSeq: 4\r\n\r\n",
            MagicMock(), ("1.2.3.4", 1), None,
        )
        assert "405 Method Not Allowed" in resp

    def test_unknown_method_not_implemented(self):
        server = _server_with_stream()
        resp, sid = server._handle_rtsp_request(
            "FROB rtsp://host/video_main RTSP/1.0\r\nCSeq: 5\r\n\r\n",
            MagicMock(), ("1.2.3.4", 1), None,
        )
        assert "501 Not Implemented" in resp

    @pytest.mark.parametrize("uri,expected", [
        ("rtsp://host/video_main", "video_main"),
        ("rtsp://host/video_main/", "video_main"),
        ("rtsp://host/video_main/trackID=0", "video_main"),
        ("rtsp://host/video_main?x=1", "video_main"),
    ])
    def test_uri_stream_name_extraction_variants(self, uri, expected):
        server = _server_with_stream()
        resp, sid = server._handle_rtsp_request(
            f"DESCRIBE {uri} RTSP/1.0\r\nCSeq: 2\r\n\r\n",
            MagicMock(), ("1.2.3.4", 1), None,
        )
        # A recognised stream name yields 200; a mis-parsed one would 404.
        assert "200 OK" in resp, f"failed to resolve stream name from uri={uri!r}: {resp!r}"


# ---------------------------------------------------------------------------
# SDP generation
# ---------------------------------------------------------------------------


class TestGenerateSDP:
    def test_sdp_contains_stream_fps_and_h264_mapping(self):
        server = _server_with_stream()
        info = RTSPStreamInfo(name="video_main", width=160, height=120, fps=15)
        sdp = server._generate_sdp(info, "rtsp://host/video_main")
        assert "a=framerate:15" in sdp
        assert "a=rtpmap:96 H264/90000" in sdp
        assert "m=video 0 RTP/AVP 96" in sdp
        assert "a=control:trackID=0" in sdp

    def test_describe_content_length_matches_sdp_body(self):
        server = _server_with_stream()
        resp = server._handle_describe("rtsp://host/video_main", "video_main", 11)
        header, body = resp.split("\r\n\r\n", 1)
        content_length = int(re.search(r"Content-Length: (\d+)", header).group(1))
        assert content_length == len(body)
        assert "Content-Base: rtsp://host/video_main/\r\n" in header


# ---------------------------------------------------------------------------
# SETUP transport negotiation (TCP interleaved vs UDP)
# ---------------------------------------------------------------------------


class TestHandleSetup:
    def test_setup_tcp_interleaved_explicit_channel(self):
        server = _server_with_stream()
        headers = {"Transport": "RTP/AVP/TCP;interleaved=2-3", "CSeq": "1"}
        resp, sid = server._handle_setup(
            "rtsp://host/video_main", "video_main", headers, 1,
            MagicMock(), ("1.2.3.4", 1), None,
        )
        assert "interleaved=2-3" in resp
        session = server._sessions[sid]
        assert session.interleaved is True
        assert session.interleaved_channel == 2
        assert session.interleaved_channel_rtcp == 3
        assert session.stream_name == "video_main"

    def test_setup_tcp_interleaved_default_channel(self):
        server = _server_with_stream()
        headers = {"Transport": "RTP/AVP/TCP;unicast"}
        resp, sid = server._handle_setup(
            "rtsp://host/video_main", "video_main", headers, 1,
            MagicMock(), ("1.2.3.4", 1), None,
        )
        assert "interleaved=0-1" in resp
        assert server._sessions[sid].interleaved_channel == 0
        assert server._sessions[sid].interleaved_channel_rtcp == 1

    def test_setup_udp_explicit_client_ports(self, monkeypatch):
        server = _server_with_stream()
        udp_sock = MagicMock()
        udp_sock.getsockname.return_value = ("0.0.0.0", 55000)
        monkeypatch.setattr("ipycam.rtsp.socket.socket", lambda *a, **k: udp_sock)

        headers = {"Transport": "RTP/AVP;unicast;client_port=7000-7001"}
        resp, sid = server._handle_setup(
            "rtsp://host/video_main", "video_main", headers, 1,
            MagicMock(), ("1.2.3.4", 1), None,
        )
        assert "client_port=7000-7001" in resp
        assert "server_port=55000-55001" in resp
        session = server._sessions[sid]
        assert session.interleaved is False
        assert session.rtp_port == 7000
        assert session.rtcp_port == 7001

    def test_setup_udp_default_client_ports(self, monkeypatch):
        server = _server_with_stream()
        udp_sock = MagicMock()
        udp_sock.getsockname.return_value = ("0.0.0.0", 9000)
        monkeypatch.setattr("ipycam.rtsp.socket.socket", lambda *a, **k: udp_sock)

        headers = {"Transport": "RTP/AVP;unicast"}
        resp, sid = server._handle_setup(
            "rtsp://host/video_main", "video_main", headers, 1,
            MagicMock(), ("1.2.3.4", 1), None,
        )
        session = server._sessions[sid]
        assert session.rtp_port == 6970
        assert session.rtcp_port == 6971

    def test_setup_udp_socket_failure_returns_500(self, monkeypatch):
        server = _server_with_stream()
        monkeypatch.setattr(
            "ipycam.rtsp.socket.socket",
            MagicMock(side_effect=OSError("no sockets available")),
        )
        headers = {"Transport": "RTP/AVP;unicast"}
        resp, sid = server._handle_setup(
            "rtsp://host/video_main", "video_main", headers, 1,
            MagicMock(), ("1.2.3.4", 1), None,
        )
        assert "500 Internal Server Error" in resp
        # A newly-created session must not leak in _sessions when socket setup fails.
        assert sid not in server._sessions

    def test_setup_reuses_existing_session_id(self):
        server = _server_with_stream()
        existing = make_session(session_id="existing1", state=RTSPState.READY)
        server._sessions["existing1"] = existing
        headers = {"Transport": "RTP/AVP/TCP;interleaved=0-1"}
        resp, sid = server._handle_setup(
            "rtsp://host/video_main", "video_main", headers, 1,
            MagicMock(), ("1.2.3.4", 1), "existing1",
        )
        assert sid == "existing1"
        assert server._sessions["existing1"] is existing


# ---------------------------------------------------------------------------
# PLAY / PAUSE / TEARDOWN / GET_PARAMETER / SET_PARAMETER (session-aware)
# ---------------------------------------------------------------------------


class TestSessionAwareHandlers:
    def test_play_valid_session_starts_rtp_and_updates_state(self):
        server = _server_with_stream()
        session = make_session(state=RTSPState.READY, stream_name="video_main")
        server._sessions[session.session_id] = session
        server._start_rtp_streaming = MagicMock()

        resp, sid = server._handle_play(session.session_id, 5)

        assert "200 OK" in resp
        assert "Range: npt=0.000-" in resp
        assert session.state == RTSPState.PLAYING
        server._start_rtp_streaming.assert_called_once_with(session, "video_main")

    def test_play_valid_session_without_stream_name_skips_rtp_start(self):
        server = _server_with_stream()
        session = make_session(state=RTSPState.READY, stream_name=None)
        server._sessions[session.session_id] = session
        server._start_rtp_streaming = MagicMock()

        server._handle_play(session.session_id, 5)

        server._start_rtp_streaming.assert_not_called()

    def test_pause_valid_session(self):
        server = _server_with_stream()
        session = make_session(state=RTSPState.PLAYING)
        server._sessions[session.session_id] = session
        resp, sid = server._handle_pause(session.session_id, 6)
        assert "200 OK" in resp
        assert session.state == RTSPState.READY

    def test_teardown_valid_session(self):
        server = _server_with_stream()
        session = make_session(state=RTSPState.PLAYING)
        server._sessions[session.session_id] = session
        resp, sid = server._handle_teardown(session.session_id, 7)
        assert "200 OK" in resp
        assert session.state == RTSPState.TEARDOWN

    def test_get_parameter_updates_last_activity_for_known_session(self):
        server = _server_with_stream()
        session = make_session(state=RTSPState.PLAYING)
        session.last_activity = 0.0
        server._sessions[session.session_id] = session
        resp, sid = server._handle_get_parameter(session.session_id, 8)
        assert "200 OK" in resp
        assert f"Session: {session.session_id}" in resp
        assert session.last_activity > 0.0

    def test_get_parameter_unknown_session_still_echoes_session_header(self):
        server = _server_with_stream()
        resp, sid = server._handle_get_parameter("ghost-session", 8)
        assert "200 OK" in resp
        assert "Session: ghost-session" in resp

    def test_set_parameter_updates_last_activity_for_known_session(self):
        server = _server_with_stream()
        session = make_session(state=RTSPState.PLAYING)
        session.last_activity = 0.0
        server._sessions[session.session_id] = session
        resp, sid = server._handle_set_parameter(session.session_id, 9)
        assert "200 OK" in resp
        assert session.last_activity > 0.0

    def test_set_parameter_no_session_id(self):
        server = _server_with_stream()
        resp, sid = server._handle_set_parameter(None, 9)
        assert "200 OK" in resp
        assert "Session:" not in resp


# ---------------------------------------------------------------------------
# FFmpeg command builders
# ---------------------------------------------------------------------------


class TestBuildFfmpegCommands:
    def test_udp_cmd_contains_dimensions_bitrate_and_client_port(self):
        server = _server_with_stream()
        info = RTSPStreamInfo(name="video_main", width=320, height=240, fps=20, bitrate="2M")
        session = make_session()
        session.rtp_port = 7000
        session.rtp_socket = MagicMock()
        session.rtp_socket.getsockname.return_value = ("0.0.0.0", 8888)

        cmd = server._build_ffmpeg_rtp_cmd_udp(info, session)

        assert cmd[0] == "ffmpeg"
        assert "320x240" in cmd
        assert "2M" in cmd
        assert any("rtp://10.0.0.5:7000?localport=8888" == c for c in cmd)

    def test_udp_cmd_localport_zero_when_no_rtp_socket(self):
        server = _server_with_stream()
        info = RTSPStreamInfo(name="video_main", width=320, height=240, fps=20)
        session = make_session()
        session.rtp_port = 7000
        session.rtp_socket = None

        cmd = server._build_ffmpeg_rtp_cmd_udp(info, session)
        assert any("localport=0" in c for c in cmd)

    def test_tcp_local_cmd_targets_localhost_port(self):
        server = _server_with_stream()
        info = RTSPStreamInfo(name="video_main", width=160, height=120, fps=10)
        cmd = server._build_ffmpeg_rtp_cmd_tcp_local(info, 6000)
        assert cmd[-1] == "rtp://127.0.0.1:6000"

    def test_deprecated_tcp_cmd_delegates_to_local_with_port_zero(self):
        server = _server_with_stream()
        info = RTSPStreamInfo(name="video_main", width=160, height=120, fps=10)
        session = make_session()
        cmd = server._build_ffmpeg_rtp_cmd_tcp(info, session)
        assert cmd[-1] == "rtp://127.0.0.1:0"


# ---------------------------------------------------------------------------
# is_native_rtsp_available()
# ---------------------------------------------------------------------------


class TestIsNativeRtspAvailable:
    def test_true_when_ffmpeg_present(self, monkeypatch):
        monkeypatch.setattr(
            "ipycam.rtsp.subprocess.run",
            lambda *a, **k: MagicMock(returncode=0),
        )
        assert is_native_rtsp_available() is True

    def test_false_when_ffmpeg_returns_nonzero(self, monkeypatch):
        monkeypatch.setattr(
            "ipycam.rtsp.subprocess.run",
            lambda *a, **k: MagicMock(returncode=1),
        )
        assert is_native_rtsp_available() is False

    def test_false_when_ffmpeg_missing(self, monkeypatch):
        def raise_not_found(*a, **k):
            raise FileNotFoundError("no ffmpeg")
        monkeypatch.setattr("ipycam.rtsp.subprocess.run", raise_not_found)
        assert is_native_rtsp_available() is False


# ---------------------------------------------------------------------------
# Server properties: client_count / actual_fps / get_stream_url / add_stream
# ---------------------------------------------------------------------------


class TestServerProperties:
    def test_client_count_counts_only_playing_sessions(self):
        server = _server_with_stream()
        server._sessions["a"] = make_session("a", state=RTSPState.PLAYING)
        server._sessions["b"] = make_session("b", state=RTSPState.READY)
        server._sessions["c"] = make_session("c", state=RTSPState.PLAYING)
        assert server.client_count == 2

    def test_actual_fps_zero_with_fewer_than_two_timestamps(self):
        server = _server_with_stream()
        assert server.actual_fps == 0

    def test_actual_fps_zero_when_all_timestamps_outside_window(self):
        server = _server_with_stream()
        old = time.time() - 30
        server._frame_timestamps.append(old)
        server._frame_timestamps.append(old + 0.1)
        assert server.actual_fps == 0

    def test_actual_fps_positive_with_recent_timestamps(self):
        server = _server_with_stream()
        now = time.time()
        # Append in chronological order (oldest first), matching real usage
        # (stream_frame() appends "now" on every call as time advances).
        for i in reversed(range(5)):
            server._frame_timestamps.append(now - (0.1 * i))
        assert server.actual_fps > 0

    def test_get_stream_url_format(self):
        server = NativeRTSPServer(port=8554)
        assert server.get_stream_url("video_main", "192.168.1.10") == "rtsp://192.168.1.10:8554/video_main"

    def test_add_stream_initializes_buffers_and_locks(self):
        server = NativeRTSPServer(port=0)
        assert server.add_stream("cam1", 640, 480, 30, bitrate="1M") is True
        assert server._streams["cam1"].width == 640
        assert server._frame_buffers["cam1"] is None
        assert "cam1" in server._frame_locks


# ---------------------------------------------------------------------------
# start() / stop() lifecycle (socket + thread creation mocked out)
# ---------------------------------------------------------------------------


class TestStartStop:
    def test_start_binds_listens_and_launches_accept_thread(self, monkeypatch):
        server = NativeRTSPServer(port=8554, host="0.0.0.0")
        mock_sock = MagicMock()
        monkeypatch.setattr("ipycam.rtsp.socket.socket", lambda *a, **k: mock_sock)
        monkeypatch.setattr("ipycam.rtsp.threading.Thread", FakeThread)

        assert server.start() is True
        assert server.is_running is True
        mock_sock.bind.assert_called_once_with(("0.0.0.0", 8554))
        mock_sock.listen.assert_called_once_with(5)
        mock_sock.settimeout.assert_called_once_with(1.0)
        assert server._accept_thread.target == server._accept_loop

    def test_start_twice_is_noop(self, monkeypatch):
        server = NativeRTSPServer(port=8554)
        monkeypatch.setattr("ipycam.rtsp.socket.socket", lambda *a, **k: MagicMock())
        monkeypatch.setattr("ipycam.rtsp.threading.Thread", FakeThread)
        assert server.start() is True
        assert server.start() is True  # already running -> short-circuit True

    def test_start_failure_returns_false(self, monkeypatch):
        server = NativeRTSPServer(port=8554)

        def raise_error(*a, **k):
            raise OSError("address in use")
        monkeypatch.setattr("ipycam.rtsp.socket.socket", raise_error)

        assert server.start() is False
        assert server.is_running is False

    def test_stop_closes_sessions_encoders_and_socket(self, monkeypatch):
        server = NativeRTSPServer(port=8554)
        mock_server_sock = MagicMock()
        server._server_socket = mock_server_sock
        server._is_running = True

        session = make_session(state=RTSPState.PLAYING)
        session.rtp_socket = MagicMock()
        server._sessions[session.session_id] = session

        proc = MagicMock()
        proc.stdin = MagicMock()
        server._encoder_processes["enc1"] = proc

        server.stop()

        assert server._is_running is False
        assert server._sessions == {}
        assert server._encoder_processes == {}
        proc.terminate.assert_called_once()
        proc.wait.assert_called_once()
        mock_server_sock.close.assert_called_once()
        assert server._server_socket is None

    def test_stop_encoder_terminate_failure_falls_back_to_kill(self):
        server = NativeRTSPServer(port=8554)
        proc = MagicMock()
        proc.stdin = MagicMock()
        proc.terminate.side_effect = Exception("boom")
        server._encoder_processes["enc1"] = proc

        server.stop()  # must not raise
        proc.kill.assert_called_once()

    def test_stop_server_socket_close_exception_is_swallowed(self):
        server = NativeRTSPServer(port=8554)
        mock_sock = MagicMock()
        mock_sock.close.side_effect = Exception("already closed")
        server._server_socket = mock_sock

        server.stop()  # must not raise
        assert server._server_socket is None

    def test_stop_when_never_started_is_noop(self):
        server = NativeRTSPServer(port=8554)
        server.stop()  # must not raise
        assert server._is_running is False


# ---------------------------------------------------------------------------
# _close_session
# ---------------------------------------------------------------------------


class TestCloseSession:
    def test_close_session_closes_rtp_socket_and_stops_matching_encoders(self):
        server = _server_with_stream()
        session = make_session(state=RTSPState.PLAYING)
        session.rtp_socket = MagicMock()
        proc = MagicMock()
        server._encoder_processes[f"{session.session_id}_video_main"] = proc
        server._encoder_processes["unrelated_key"] = MagicMock()

        server._close_session(session)

        assert session.state == RTSPState.TEARDOWN
        session.rtp_socket.close.assert_called_once()
        proc.terminate.assert_called_once()
        assert f"{session.session_id}_video_main" not in server._encoder_processes
        assert "unrelated_key" in server._encoder_processes

    def test_close_session_rtp_socket_close_exception_is_swallowed(self):
        server = _server_with_stream()
        session = make_session(state=RTSPState.PLAYING)
        session.rtp_socket = MagicMock()
        session.rtp_socket.close.side_effect = Exception("already closed")

        server._close_session(session)  # must not raise
        assert session.state == RTSPState.TEARDOWN

    def test_close_session_encoder_terminate_failure_falls_back_to_kill(self):
        server = _server_with_stream()
        session = make_session(state=RTSPState.PLAYING)
        proc = MagicMock()
        proc.terminate.side_effect = Exception("boom")
        server._encoder_processes[f"{session.session_id}_video_main"] = proc

        server._close_session(session)  # must not raise
        proc.kill.assert_called_once()

    def test_close_session_no_rtp_socket_is_safe(self):
        server = _server_with_stream()
        session = make_session(state=RTSPState.PLAYING)
        assert session.rtp_socket is None
        server._close_session(session)  # must not raise
        assert session.state == RTSPState.TEARDOWN


# ---------------------------------------------------------------------------
# _receive_rtsp_request
# ---------------------------------------------------------------------------


class TestReceiveRtspRequest:
    def test_full_request_in_single_recv(self):
        server = _server_with_stream()
        sock = MagicMock()
        sock.recv.return_value = b"OPTIONS rtsp://h/s RTSP/1.0\r\nCSeq: 1\r\n\r\n"
        data = server._receive_rtsp_request(sock)
        assert data.endswith(b"\r\n\r\n")

    def test_request_split_across_multiple_chunks(self):
        server = _server_with_stream()
        sock = MagicMock()
        chunks = [b"OPTIONS rtsp://h/s RTSP/1.0\r\n", b"CSeq: 1\r\n", b"\r\n"]
        sock.recv.side_effect = chunks
        data = server._receive_rtsp_request(sock)
        assert data == b"".join(chunks)

    def test_connection_closed_immediately_returns_none(self):
        server = _server_with_stream()
        sock = MagicMock()
        sock.recv.return_value = b""
        assert server._receive_rtsp_request(sock) is None

    def test_timeout_with_partial_data_returns_partial(self):
        server = _server_with_stream()
        sock = MagicMock()
        sock.recv.side_effect = [b"partial-no-terminator", socket_module.timeout()]
        data = server._receive_rtsp_request(sock)
        assert data == b"partial-no-terminator"

    def test_timeout_with_no_data_returns_none(self):
        server = _server_with_stream()
        sock = MagicMock()
        sock.recv.side_effect = socket_module.timeout()
        assert server._receive_rtsp_request(sock) is None


# ---------------------------------------------------------------------------
# _accept_loop
# ---------------------------------------------------------------------------


class TestAcceptLoop:
    def test_timeout_is_ignored_until_stopped(self):
        server = _server_with_stream()
        server._server_socket = MagicMock()
        calls = {"n": 0}

        def fake_accept():
            calls["n"] += 1
            if calls["n"] >= 2:
                server._is_running = False
            raise socket_module.timeout()
        server._server_socket.accept.side_effect = fake_accept

        server._accept_loop()
        assert calls["n"] == 2

    def test_successful_accept_spawns_client_thread(self, monkeypatch):
        monkeypatch.setattr("ipycam.rtsp.threading.Thread", FakeThread)
        server = _server_with_stream()
        server._server_socket = MagicMock()
        client_sock = MagicMock()
        calls = {"n": 0}

        def fake_accept():
            calls["n"] += 1
            if calls["n"] == 1:
                return client_sock, ("9.9.9.9", 4321)
            server._is_running = False
            raise socket_module.timeout()
        server._server_socket.accept.side_effect = fake_accept

        server._accept_loop()

        client_sock.settimeout.assert_called_once_with(30.0)

    def test_unexpected_exception_breaks_loop_without_raising(self):
        server = _server_with_stream()
        server._server_socket = MagicMock()
        server._server_socket.accept.side_effect = RuntimeError("kaboom")

        server._accept_loop()  # must not raise


# ---------------------------------------------------------------------------
# _handle_client
# ---------------------------------------------------------------------------


class TestHandleClient:
    def test_options_request_gets_response_then_disconnect(self):
        server = _server_with_stream()
        sock = MagicMock()
        sock.recv.side_effect = [
            b"OPTIONS rtsp://h/video_main RTSP/1.0\r\nCSeq: 1\r\n\r\n",
            b"",  # client disconnects
        ]
        server._handle_client(sock, ("1.2.3.4", 1))

        sent = b"".join(c.args[0] for c in sock.sendall.call_args_list)
        assert b"200 OK" in sent
        sock.close.assert_called_once()

    def test_setup_play_teardown_sequence_ends_loop_and_cleans_session(self, monkeypatch):
        monkeypatch.setattr("ipycam.rtsp.socket.socket", lambda *a, **k: MagicMock())
        server = _server_with_stream()
        server._start_rtp_streaming = MagicMock()  # avoid spawning a real encoder thread
        sock = MagicMock()
        sock.recv.side_effect = [
            b"SETUP rtsp://h/video_main RTSP/1.0\r\nCSeq: 1\r\nTransport: RTP/AVP;unicast;client_port=7000-7001\r\n\r\n",
            b"PLAY rtsp://h/video_main RTSP/1.0\r\nCSeq: 2\r\nSession: PLACEHOLDER\r\n\r\n",
            b"TEARDOWN rtsp://h/video_main RTSP/1.0\r\nCSeq: 3\r\nSession: PLACEHOLDER\r\n\r\n",
        ]

        # Session id is generated inside SETUP -- patch recv to substitute the
        # real id once we learn it, by wrapping the side_effect list lazily.
        session_holder = {}

        original_setup = server._handle_setup

        def spy_setup(*a, **k):
            resp, sid = original_setup(*a, **k)
            session_holder["sid"] = sid
            return resp, sid
        server._handle_setup = spy_setup

        def recv_side_effect(bufsize):
            calls = recv_side_effect.calls
            recv_side_effect.calls += 1
            if calls == 0:
                return b"SETUP rtsp://h/video_main RTSP/1.0\r\nCSeq: 1\r\nTransport: RTP/AVP;unicast;client_port=7000-7001\r\n\r\n"
            elif calls == 1:
                sid = session_holder["sid"]
                return f"PLAY rtsp://h/video_main RTSP/1.0\r\nCSeq: 2\r\nSession: {sid}\r\n\r\n".encode()
            elif calls == 2:
                sid = session_holder["sid"]
                return f"TEARDOWN rtsp://h/video_main RTSP/1.0\r\nCSeq: 3\r\nSession: {sid}\r\n\r\n".encode()
            return b""
        recv_side_effect.calls = 0

        sock.recv.side_effect = recv_side_effect

        server._handle_client(sock, ("1.2.3.4", 1))

        sid = session_holder["sid"]
        # Session must have been popped and cleaned up after TEARDOWN.
        assert sid not in server._sessions
        sock.close.assert_called_once()

    def test_connection_reset_error_is_swallowed(self):
        server = _server_with_stream()
        sock = MagicMock()
        sock.recv.side_effect = ConnectionResetError()
        server._handle_client(sock, ("1.2.3.4", 1))  # must not raise
        sock.close.assert_called_once()

    def test_broken_pipe_error_is_swallowed(self):
        server = _server_with_stream()
        sock = MagicMock()
        sock.recv.side_effect = BrokenPipeError()
        server._handle_client(sock, ("1.2.3.4", 1))
        sock.close.assert_called_once()

    def test_windows_connection_aborted_oserror_is_swallowed(self):
        server = _server_with_stream()
        sock = MagicMock()
        err = OSError("connection aborted")
        err.winerror = 10053
        sock.recv.side_effect = err
        server._handle_client(sock, ("1.2.3.4", 1))
        sock.close.assert_called_once()

    def test_other_oserror_is_swallowed(self):
        server = _server_with_stream()
        sock = MagicMock()
        err = OSError("some other error")
        err.winerror = 99999
        sock.recv.side_effect = err
        server._handle_client(sock, ("1.2.3.4", 1))
        sock.close.assert_called_once()

    def test_generic_exception_is_logged_and_swallowed(self):
        server = _server_with_stream()
        sock = MagicMock()
        sock.recv.side_effect = RuntimeError("unexpected")
        server._handle_client(sock, ("1.2.3.4", 1))
        sock.close.assert_called_once()

    def test_verbose_logging_paths_do_not_raise(self):
        server = _server_with_stream()
        server.verbose = True
        sock = MagicMock()
        sock.recv.side_effect = [
            b"OPTIONS rtsp://h/video_main RTSP/1.0\r\nCSeq: 1\r\n\r\n",
            b"",
        ]
        server._handle_client(sock, ("1.2.3.4", 1))
        sock.close.assert_called_once()


# ---------------------------------------------------------------------------
# _start_rtp_streaming
# ---------------------------------------------------------------------------


class TestStartRtpStreaming:
    def test_spawns_encoder_thread_with_expected_target_and_args(self, monkeypatch):
        monkeypatch.setattr("ipycam.rtsp.threading.Thread", FakeThread)
        server = _server_with_stream()
        session = make_session(state=RTSPState.PLAYING)

        server._start_rtp_streaming(session, "video_main")

        key = f"{session.session_id}_video_main"
        thread = server._rtp_threads[key]
        assert thread.target == server._rtp_encoder_loop
        assert thread.args[0] is session
        assert thread.args[1] == "video_main"
        assert thread.is_alive() is True

    def test_missing_stream_with_verbose_returns_early(self, monkeypatch):
        monkeypatch.setattr("ipycam.rtsp.threading.Thread", FakeThread)
        server = _server_with_stream()
        server.verbose = True
        session = make_session(state=RTSPState.PLAYING)

        server._start_rtp_streaming(session, "does_not_exist")

        assert not server._rtp_threads


# ---------------------------------------------------------------------------
# _rtp_encoder_loop
# ---------------------------------------------------------------------------


class TestRtpEncoderLoop:
    def test_udp_mode_writes_frames_flushes_every_15_and_cleans_up(self, monkeypatch):
        server = _server_with_stream(w=4, h=4)
        server.verbose = True
        info = server._streams["video_main"]
        session = make_session(state=RTSPState.PLAYING)
        session.client_address = ("10.0.0.9", 6000)
        session.rtp_port = 7000
        session.interleaved = False

        frame = np.zeros((4, 4, 3), dtype=np.uint8)
        server._frame_buffers["video_main"] = frame
        # The loop now writes only when the buffer version advances. Present a
        # fresh version on every read so it writes each pass, exercising the
        # flush-every-15 behaviour just as before.
        server._frame_versions = MagicMock()
        server._frame_versions.get.side_effect = itertools.count(1)

        proc = MagicMock()
        proc.stdin = MagicMock()
        proc.stderr = MagicMock()
        proc.stderr.read.return_value = b"ffmpeg exiting"
        # 14 "still alive" polls, then a 15th that reports the process died.
        proc.poll.side_effect = [None] * 14 + [1]

        monkeypatch.setattr("ipycam.rtsp.subprocess.Popen", lambda *a, **k: proc)

        server._rtp_encoder_loop(session, "video_main", info)

        assert proc.stdin.write.call_count == 15
        proc.stdin.flush.assert_called()  # the 15th write triggers a flush
        assert proc.terminate.called
        assert proc.wait.called
        key = f"{session.session_id}_video_main"
        assert key not in server._encoder_processes

    def test_udp_mode_resizes_mismatched_frame(self, monkeypatch):
        server = _server_with_stream(w=4, h=4)
        info = server._streams["video_main"]
        session = make_session(state=RTSPState.PLAYING)
        session.rtp_port = 7000

        # Frame is a different size than the configured stream -> must resize.
        big_frame = np.zeros((8, 8, 3), dtype=np.uint8)
        server._frame_buffers["video_main"] = big_frame

        proc = MagicMock()
        proc.stdin = MagicMock()
        proc.poll.return_value = 1  # die immediately after first write

        monkeypatch.setattr("ipycam.rtsp.subprocess.Popen", lambda *a, **k: proc)

        server._rtp_encoder_loop(session, "video_main", info)

        written = proc.stdin.write.call_args[0][0]
        assert len(written) == 4 * 4 * 3  # resized down to the stream's 4x4

    def test_tcp_interleaved_mode_creates_local_socket_and_forwarder_thread(self, monkeypatch):
        monkeypatch.setattr("ipycam.rtsp.threading.Thread", FakeThread)
        server = _server_with_stream(w=4, h=4)
        info = server._streams["video_main"]
        session = make_session(state=RTSPState.PLAYING)
        session.interleaved = True
        session.interleaved_channel = 0

        local_sock = MagicMock()
        local_sock.getsockname.return_value = ("127.0.0.1", 6500)
        monkeypatch.setattr("ipycam.rtsp.socket.socket", lambda *a, **k: local_sock)

        proc = MagicMock()
        proc.stdin = MagicMock()
        proc.poll.return_value = 1  # exit after one pass

        monkeypatch.setattr("ipycam.rtsp.subprocess.Popen", lambda *a, **k: proc)

        server._rtp_encoder_loop(session, "video_main", info)

        # Two local sockets are now created (RTP + RTCP), both closed on cleanup.
        assert local_sock.close.call_count == 2
        key = f"{session.session_id}_video_main"
        forward_thread = server._rtp_threads.get(key) if key in server._rtp_threads else None
        # The forwarder FakeThread isn't tracked in _rtp_threads (that's only
        # populated by _start_rtp_streaming); just confirm no exception and
        # cleanup ran.
        assert key not in server._encoder_processes

    def test_broken_pipe_on_write_breaks_loop_cleanly(self, monkeypatch):
        server = _server_with_stream(w=4, h=4)
        info = server._streams["video_main"]
        session = make_session(state=RTSPState.PLAYING)
        session.rtp_port = 7000
        server._frame_buffers["video_main"] = np.zeros((4, 4, 3), dtype=np.uint8)

        proc = MagicMock()
        proc.stdin = MagicMock()
        proc.stdin.write.side_effect = BrokenPipeError()
        proc.poll.return_value = None

        monkeypatch.setattr("ipycam.rtsp.subprocess.Popen", lambda *a, **k: proc)

        server._rtp_encoder_loop(session, "video_main", info)  # must not raise
        assert proc.stdin.write.call_count == 1

    def test_empty_ffmpeg_cmd_short_circuits_without_spawning_process(self, monkeypatch):
        server = _server_with_stream(w=4, h=4)
        info = server._streams["video_main"]
        session = make_session(state=RTSPState.PLAYING)
        session.interleaved = False
        server._build_ffmpeg_rtp_cmd_udp = MagicMock(return_value=None)

        popen_mock = MagicMock()
        monkeypatch.setattr("ipycam.rtsp.subprocess.Popen", popen_mock)

        server._rtp_encoder_loop(session, "video_main", info)

        popen_mock.assert_not_called()

    def test_outer_exception_before_popen_is_caught(self, monkeypatch):
        server = _server_with_stream(w=4, h=4)
        info = server._streams["video_main"]
        session = make_session(state=RTSPState.PLAYING)
        session.interleaved = True

        monkeypatch.setattr(
            "ipycam.rtsp.socket.socket",
            MagicMock(side_effect=OSError("cannot bind local socket")),
        )
        server._rtp_encoder_loop(session, "video_main", info)  # must not raise


# ---------------------------------------------------------------------------
# _tcp_rtp_forwarder
# ---------------------------------------------------------------------------


class TestTcpRtpForwarder:
    def test_forwards_one_packet_as_interleaved_frame(self):
        server = _server_with_stream()
        server.verbose = True
        session = make_session(state=RTSPState.PLAYING)
        session.interleaved_channel = 0

        local_sock = MagicMock()

        def fake_recvfrom(bufsize):
            session.state = RTSPState.TEARDOWN  # ensure the loop exits after this
            return b"abcd", ("127.0.0.1", 1)
        local_sock.recvfrom.side_effect = fake_recvfrom

        server._tcp_rtp_forwarder(local_sock, session)

        expected_header = bytes([0x24, 0]) + struct.pack(">H", 4)
        session.client_socket.sendall.assert_called_once_with(expected_header + b"abcd")

    def test_empty_datagram_is_skipped(self):
        server = _server_with_stream()
        session = make_session(state=RTSPState.PLAYING)
        local_sock = MagicMock()
        calls = {"n": 0}

        def fake_recvfrom(bufsize):
            calls["n"] += 1
            if calls["n"] == 1:
                return b"", ("127.0.0.1", 1)
            session.state = RTSPState.TEARDOWN
            raise socket_module.timeout()
        local_sock.recvfrom.side_effect = fake_recvfrom

        server._tcp_rtp_forwarder(local_sock, session)
        session.client_socket.sendall.assert_not_called()

    def test_timeout_continues_until_stopped(self):
        server = _server_with_stream()
        session = make_session(state=RTSPState.PLAYING)
        local_sock = MagicMock()
        calls = {"n": 0}

        def fake_recvfrom(bufsize):
            calls["n"] += 1
            if calls["n"] >= 2:
                session.state = RTSPState.TEARDOWN
            raise socket_module.timeout()
        local_sock.recvfrom.side_effect = fake_recvfrom

        server._tcp_rtp_forwarder(local_sock, session)
        assert calls["n"] >= 2

    def test_connection_error_breaks_loop(self):
        server = _server_with_stream()
        server.verbose = True
        session = make_session(state=RTSPState.PLAYING)
        local_sock = MagicMock()
        local_sock.recvfrom.side_effect = BrokenPipeError()

        server._tcp_rtp_forwarder(local_sock, session)  # must not raise


# ---------------------------------------------------------------------------
# _tcp_rtp_reader (deprecated helper, still shipped)
# ---------------------------------------------------------------------------


class TestTcpRtpReader:
    def test_reads_and_forwards_until_empty(self):
        server = _server_with_stream()
        session = make_session(state=RTSPState.PLAYING)
        proc = MagicMock()
        proc.stdout = MagicMock()
        proc.stdout.read.side_effect = [b"data1234", b""]

        server._tcp_rtp_reader(proc, session)

        expected_header = bytes([0x24, session.interleaved_channel]) + struct.pack(">H", 8)
        session.client_socket.sendall.assert_called_once_with(expected_header + b"data1234")

    def test_connection_error_on_sendall_breaks_without_raising(self):
        server = _server_with_stream()
        session = make_session(state=RTSPState.PLAYING)
        proc = MagicMock()
        proc.stdout = MagicMock()
        proc.stdout.read.return_value = b"data"
        session.client_socket.sendall.side_effect = BrokenPipeError()

        server._tcp_rtp_reader(proc, session)  # must not raise


# ---------------------------------------------------------------------------
# Additional verbose-logging / nested-exception-fallback branches
# ---------------------------------------------------------------------------


class TestVerboseAndNestedFallbackBranches:
    def test_handle_client_verbose_connection_reset(self):
        server = _server_with_stream()
        server.verbose = True
        sock = MagicMock()
        sock.recv.side_effect = ConnectionResetError()
        server._handle_client(sock, ("1.2.3.4", 1))
        sock.close.assert_called_once()

    def test_handle_client_verbose_windows_aborted_oserror(self):
        server = _server_with_stream()
        server.verbose = True
        sock = MagicMock()
        err = OSError("aborted")
        err.winerror = 10054
        sock.recv.side_effect = err
        server._handle_client(sock, ("1.2.3.4", 1))
        sock.close.assert_called_once()

    def test_handle_client_verbose_other_oserror(self):
        server = _server_with_stream()
        server.verbose = True
        sock = MagicMock()
        err = OSError("other")
        err.winerror = 1
        sock.recv.side_effect = err
        server._handle_client(sock, ("1.2.3.4", 1))
        sock.close.assert_called_once()

    def test_handle_client_close_exception_is_swallowed(self):
        server = _server_with_stream()
        sock = MagicMock()
        sock.recv.return_value = b""
        sock.close.side_effect = Exception("already closed")
        server._handle_client(sock, ("1.2.3.4", 1))  # must not raise

    def test_setup_tcp_interleaved_verbose_logs(self):
        server = _server_with_stream()
        server.verbose = True
        headers = {"Transport": "RTP/AVP/TCP;interleaved=4-5"}
        resp, sid = server._handle_setup(
            "rtsp://host/video_main", "video_main", headers, 1,
            MagicMock(), ("1.2.3.4", 1), None,
        )
        assert "interleaved=4-5" in resp

    def test_stream_name_fallback_when_all_path_parts_filtered(self):
        """A degenerate URI whose only path component is filtered out (here,
        the literal 'rtsp:' left after rstrip('/')) falls back to using that
        filtered part rather than leaving stream_name unset."""
        server = _server_with_stream()
        resp, sid = server._handle_rtsp_request(
            "SETUP rtsp:// RTSP/1.0\r\nCSeq: 1\r\nTransport: RTP/AVP/TCP;interleaved=0-1\r\n\r\n",
            MagicMock(), ("1.2.3.4", 1), None,
        )
        session = server._sessions[sid]
        assert session.stream_name == "rtsp:"

    def test_start_rtp_streaming_verbose_logs_udp_mode(self, monkeypatch):
        monkeypatch.setattr("ipycam.rtsp.threading.Thread", FakeThread)
        server = _server_with_stream()
        server.verbose = True
        session = make_session(state=RTSPState.PLAYING)
        session.interleaved = False
        session.rtp_port = 7000

        server._start_rtp_streaming(session, "video_main")

        key = f"{session.session_id}_video_main"
        assert key in server._rtp_threads

    def test_tcp_interleaved_verbose_and_local_socket_close_exception(self, monkeypatch):
        server = _server_with_stream(w=4, h=4)
        server.verbose = True
        info = server._streams["video_main"]
        session = make_session(state=RTSPState.PLAYING)
        session.interleaved = True
        session.interleaved_channel = 0

        local_sock = MagicMock()
        local_sock.getsockname.return_value = ("127.0.0.1", 6500)
        local_sock.close.side_effect = Exception("already closed")
        monkeypatch.setattr("ipycam.rtsp.socket.socket", lambda *a, **k: local_sock)
        monkeypatch.setattr("ipycam.rtsp.threading.Thread", FakeThread)

        proc = MagicMock()
        proc.stdin = MagicMock()
        proc.poll.return_value = 1
        monkeypatch.setattr("ipycam.rtsp.subprocess.Popen", lambda *a, **k: proc)

        server._rtp_encoder_loop(session, "video_main", info)  # must not raise

    def test_broken_pipe_verbose_logs(self, monkeypatch):
        server = _server_with_stream(w=4, h=4)
        server.verbose = True
        info = server._streams["video_main"]
        session = make_session(state=RTSPState.PLAYING)
        session.rtp_port = 7000
        server._frame_buffers["video_main"] = np.zeros((4, 4, 3), dtype=np.uint8)

        proc = MagicMock()
        proc.stdin = MagicMock()
        proc.stdin.write.side_effect = BrokenPipeError()
        proc.poll.return_value = None
        monkeypatch.setattr("ipycam.rtsp.subprocess.Popen", lambda *a, **k: proc)

        server._rtp_encoder_loop(session, "video_main", info)  # must not raise

    def test_encoder_cleanup_terminate_and_kill_both_fail(self, monkeypatch):
        server = _server_with_stream(w=4, h=4)
        info = server._streams["video_main"]
        session = make_session(state=RTSPState.PLAYING)
        session.rtp_port = 7000
        server._frame_buffers["video_main"] = np.zeros((4, 4, 3), dtype=np.uint8)

        proc = MagicMock()
        proc.stdin = MagicMock()
        proc.poll.return_value = 1
        proc.terminate.side_effect = Exception("terminate failed")
        proc.kill.side_effect = Exception("kill failed too")
        monkeypatch.setattr("ipycam.rtsp.subprocess.Popen", lambda *a, **k: proc)

        server._rtp_encoder_loop(session, "video_main", info)  # must not raise
        proc.kill.assert_called_once()

    def test_stop_encoder_terminate_and_kill_both_fail(self):
        server = NativeRTSPServer(port=8554)
        proc = MagicMock()
        proc.stdin = MagicMock()
        proc.terminate.side_effect = Exception("terminate failed")
        proc.kill.side_effect = Exception("kill failed too")
        server._encoder_processes["enc1"] = proc

        server.stop()  # must not raise
        proc.kill.assert_called_once()

    def test_close_session_terminate_and_kill_both_fail(self):
        server = _server_with_stream()
        session = make_session(state=RTSPState.PLAYING)
        proc = MagicMock()
        proc.terminate.side_effect = Exception("terminate failed")
        proc.kill.side_effect = Exception("kill failed too")
        server._encoder_processes[f"{session.session_id}_video_main"] = proc

        server._close_session(session)  # must not raise
        proc.kill.assert_called_once()


# ---------------------------------------------------------------------------
# Fix 1: per-session send_lock serializes ALL writes to client_socket
# (RTP/RTCP forwarder frames + RTSP responses must never interleave).
# ---------------------------------------------------------------------------


class TestSendLockSerialization:
    def test_session_has_send_lock_by_default(self):
        session = make_session()
        # A usable lock: acquirable and releasable.
        assert session.send_lock.acquire() is True
        session.send_lock.release()

    def test_forwarder_write_goes_through_send_lock(self):
        server = _server_with_stream()
        session = make_session(state=RTSPState.PLAYING)
        session.interleaved_channel = 0
        spy = SpyLock()
        session.send_lock = spy

        local_sock = MagicMock()

        def fake_recvfrom(bufsize):
            session.state = RTSPState.TEARDOWN  # exit after this one datagram
            return b"abcd", ("127.0.0.1", 1)
        local_sock.recvfrom.side_effect = fake_recvfrom

        server._tcp_rtp_forwarder(local_sock, session)

        # The single interleaved frame was written under the session send_lock.
        assert spy.acquire_count == 1
        expected_header = bytes([0x24, 0]) + struct.pack(">H", 4)
        session.client_socket.sendall.assert_called_once_with(expected_header + b"abcd")

    def test_rtsp_response_write_goes_through_session_send_lock(self):
        server = _server_with_stream()
        session = make_session(session_id="locksess", state=RTSPState.PLAYING)
        spy = SpyLock()
        session.send_lock = spy
        server._sessions["locksess"] = session

        sock = session.client_socket  # forwarder + handler share this socket
        sock.recv.side_effect = [
            b"GET_PARAMETER rtsp://h/video_main RTSP/1.0\r\nCSeq: 1\r\nSession: locksess\r\n\r\n",
            b"",  # disconnect
        ]

        server._handle_client(sock, ("1.2.3.4", 1))

        # The keepalive response for a known session went out under its lock.
        assert spy.acquire_count >= 1
        assert sock.sendall.called

    def test_response_write_without_session_does_not_require_lock(self):
        # OPTIONS before any SETUP: no session exists yet, so no forwarder can
        # race us -> the response is written directly (still delivered).
        server = _server_with_stream()
        sock = MagicMock()
        sock.recv.side_effect = [
            b"OPTIONS rtsp://h/video_main RTSP/1.0\r\nCSeq: 1\r\n\r\n",
            b"",
        ]
        server._handle_client(sock, ("1.2.3.4", 1))
        sent = b"".join(c.args[0] for c in sock.sendall.call_args_list)
        assert b"200 OK" in sent

    def test_forwarder_and_response_serialize_on_the_same_lock(self):
        """A held forwarder write must block a concurrent response write (and
        vice-versa): both acquire the *same* per-session lock instance."""
        server = _server_with_stream()
        session = make_session(session_id="ser1", state=RTSPState.PLAYING)
        server._sessions["ser1"] = session

        order = []
        real_lock = session.send_lock

        # Forwarder grabs the lock and holds it while we attempt the response.
        with real_lock:
            order.append("forwarder-holds")
            got_it = real_lock.acquire(blocking=False)
            # Same reentrant-unsafe Lock -> a second acquire must fail while held.
            assert got_it is False
            order.append("response-blocked")
        # Once released, the response path can take it.
        assert real_lock.acquire(blocking=False) is True
        real_lock.release()
        assert order == ["forwarder-holds", "response-blocked"]


# ---------------------------------------------------------------------------
# Fix 2: RTCP forwarding over the RTCP interleaved channel (channel B).
# ---------------------------------------------------------------------------


class TestRtcpForwarding:
    def test_rtcp_forwarder_wraps_with_channel_b_and_send_lock(self):
        server = _server_with_stream()
        session = make_session(state=RTSPState.PLAYING)
        session.interleaved_channel = 0
        session.interleaved_channel_rtcp = 1
        spy = SpyLock()
        session.send_lock = spy

        local_sock = MagicMock()

        def fake_recvfrom(bufsize):
            session.state = RTSPState.TEARDOWN
            return b"RTCPPKT", ("127.0.0.1", 1)
        local_sock.recvfrom.side_effect = fake_recvfrom

        server._tcp_rtp_forwarder(local_sock, session, channel=session.interleaved_channel_rtcp)

        expected_header = bytes([0x24, 1]) + struct.pack(">H", 7)
        session.client_socket.sendall.assert_called_once_with(expected_header + b"RTCPPKT")
        assert spy.acquire_count == 1

    def test_tcp_local_cmd_includes_rtcpport_when_provided(self):
        server = _server_with_stream()
        info = RTSPStreamInfo(name="video_main", width=160, height=120, fps=10)
        cmd = server._build_ffmpeg_rtp_cmd_tcp_local(info, 6000, 6001)
        assert cmd[-1] == "rtp://127.0.0.1:6000?rtcpport=6001"

    def test_tcp_local_cmd_omits_rtcpport_when_absent(self):
        server = _server_with_stream()
        info = RTSPStreamInfo(name="video_main", width=160, height=120, fps=10)
        cmd = server._build_ffmpeg_rtp_cmd_tcp_local(info, 6000)
        assert cmd[-1] == "rtp://127.0.0.1:6000"

    def test_interleaved_encoder_binds_two_sockets_and_starts_two_forwarders(self, monkeypatch):
        started = []

        class RecordingThread(FakeThread):
            def start(self):
                started.append((self.target, self.args))
                super().start()

        monkeypatch.setattr("ipycam.rtsp.threading.Thread", RecordingThread)
        server = _server_with_stream(w=4, h=4)
        info = server._streams["video_main"]
        session = make_session(state=RTSPState.PLAYING)
        session.interleaved = True
        session.interleaved_channel = 0
        session.interleaved_channel_rtcp = 1

        local_sock = MagicMock()
        local_sock.getsockname.return_value = ("127.0.0.1", 6500)
        monkeypatch.setattr("ipycam.rtsp.socket.socket", lambda *a, **k: local_sock)

        proc = MagicMock()
        proc.stdin = MagicMock()
        proc.poll.return_value = 1  # exit after first pass
        monkeypatch.setattr("ipycam.rtsp.subprocess.Popen", lambda *a, **k: proc)

        server._rtp_encoder_loop(session, "video_main", info)

        forwarders = [args for tgt, args in started if tgt == server._tcp_rtp_forwarder]
        assert len(forwarders) == 2
        channels = {args[2] for args in forwarders}
        assert channels == {0, 1}  # RTP channel A + RTCP channel B


# ---------------------------------------------------------------------------
# Fix 3: sprop-parameter-sets in SDP (out-of-band SPS/PPS).
# ---------------------------------------------------------------------------


class TestSpropParameterSets:
    def test_sdp_includes_sprop_when_probe_succeeds(self, monkeypatch):
        server = _server_with_stream()
        monkeypatch.setattr(
            server, "_probe_h264_parameter_sets",
            lambda w, h, fps: ("Z0LAHtoHgUZA", "aM4G4g=="),
        )
        info = server._streams["video_main"]
        sdp = server._generate_sdp(info, "rtsp://host/video_main")

        assert "packetization-mode=1" in sdp
        assert "sprop-parameter-sets=Z0LAHtoHgUZA,aM4G4g==" in sdp
        m = re.search(r"sprop-parameter-sets=([^\r\n]+)", sdp)
        assert len(m.group(1).split(",")) == 2  # exactly SPS + PPS

    def test_sprop_is_cached_on_stream_info(self, monkeypatch):
        server = _server_with_stream()
        calls = {"n": 0}

        def probe(w, h, fps):
            calls["n"] += 1
            return ("AAA", "BBB")
        monkeypatch.setattr(server, "_probe_h264_parameter_sets", probe)

        info = server._streams["video_main"]
        first = server._get_sprop_parameter_sets(info)
        second = server._get_sprop_parameter_sets(info)
        assert first == second == "AAA,BBB"
        assert calls["n"] == 1  # probed once, cached thereafter

    def test_describe_succeeds_and_omits_sprop_when_probe_fails(self, monkeypatch):
        server = _server_with_stream()

        def boom(w, h, fps):
            raise RuntimeError("ffmpeg not available")
        monkeypatch.setattr(server, "_probe_h264_parameter_sets", boom)

        resp = server._handle_describe("rtsp://host/video_main", "video_main", 3)
        assert "200 OK" in resp
        assert "sprop-parameter-sets" not in resp
        # Content-Length still consistent with the (shorter) body.
        header, body = resp.split("\r\n\r\n", 1)
        cl = int(re.search(r"Content-Length: (\d+)", header).group(1))
        assert cl == len(body)

    def test_extract_sps_pps_from_annexb_4byte_start_codes(self):
        server = _server_with_stream()
        sps = bytes([0x67, 0x42, 0x00, 0x1f])  # NAL type 7 (0x67 & 0x1F == 7)
        pps = bytes([0x68, 0xce, 0x38, 0x80])  # NAL type 8 (0x68 & 0x1F == 8)
        data = b"\x00\x00\x00\x01" + sps + b"\x00\x00\x00\x01" + pps
        sps_b64, pps_b64 = server._extract_sps_pps(data)
        assert base64.b64decode(sps_b64) == sps
        assert base64.b64decode(pps_b64) == pps

    def test_extract_sps_pps_from_mixed_start_codes_with_leading_garbage(self):
        server = _server_with_stream()
        sps = bytes([0x67, 0x64, 0x00, 0x1f])
        pps = bytes([0x68, 0xee, 0x3c, 0x80])
        # AUD (type 9) with 3-byte code, then SPS/PPS with 4-byte codes.
        aud = bytes([0x09, 0x10])
        data = b"\x00\x00\x01" + aud + b"\x00\x00\x00\x01" + sps + b"\x00\x00\x00\x01" + pps
        sps_b64, pps_b64 = server._extract_sps_pps(data)
        assert base64.b64decode(sps_b64) == sps
        assert base64.b64decode(pps_b64) == pps

    def test_extract_sps_pps_raises_when_missing(self):
        server = _server_with_stream()
        # Only a coded slice (type 1), no SPS/PPS.
        data = b"\x00\x00\x00\x01" + bytes([0x41, 0x00])
        with pytest.raises(ValueError):
            server._extract_sps_pps(data)

    def test_probe_uses_fixed_encoder_params(self, monkeypatch):
        """The probe must encode with the same libx264/baseline/level params as
        the streaming encoder so the derived SPS/PPS match the live stream."""
        server = _server_with_stream()
        captured = {}

        class FakeProc:
            def communicate(self, input=None, timeout=None):
                return (b"", b"")

        def fake_popen(cmd, *a, **k):
            captured["cmd"] = cmd
            return FakeProc()

        monkeypatch.setattr("ipycam.rtsp.subprocess.Popen", fake_popen)
        # Restore the genuine probe (the autouse fixture stubbed it out).
        monkeypatch.setattr(NativeRTSPServer, "_probe_h264_parameter_sets", _REAL_PROBE)
        # _extract_sps_pps would raise on empty output; stub it out.
        monkeypatch.setattr(server, "_extract_sps_pps", lambda data: ("S", "P"))

        sps, pps = server._probe_h264_parameter_sets(160, 120, 15)
        cmd = captured["cmd"]
        assert "libx264" in cmd
        assert "baseline" in cmd
        assert "3.1" in cmd
        assert "yuv420p" in cmd
        assert "160x120" in cmd
        assert (sps, pps) == ("S", "P")


# ---------------------------------------------------------------------------
# Fix 4: GOP raised to ~2x fps + bufsize scaled with bitrate (smoothness).
# ---------------------------------------------------------------------------


class TestGopAndBufsize:
    def test_udp_cmd_uses_raised_gop(self):
        server = _server_with_stream()
        info = RTSPStreamInfo(name="video_main", width=320, height=240, fps=20, bitrate="2M")
        session = make_session()
        session.rtp_port = 7000
        session.rtp_socket = None
        cmd = server._build_ffmpeg_rtp_cmd_udp(info, session)
        gi = cmd.index("-g")
        assert cmd[gi + 1] == "40"  # 2 * fps

    def test_tcp_local_cmd_uses_raised_gop(self):
        server = _server_with_stream()
        info = RTSPStreamInfo(name="video_main", width=160, height=120, fps=10, bitrate="4M")
        cmd = server._build_ffmpeg_rtp_cmd_tcp_local(info, 6000)
        gi = cmd.index("-g")
        assert cmd[gi + 1] == "20"  # 2 * fps

    def test_bufsize_scales_with_bitrate(self):
        server = _server_with_stream()
        assert server._bufsize_for("4M") == "8M"
        assert server._bufsize_for("2M") == "4M"
        assert server._bufsize_for("1M") == "2M"
        # Unparseable input falls back rather than raising.
        assert server._bufsize_for("garbage") == "2M"

    def test_builders_use_scaled_bufsize(self):
        server = _server_with_stream()
        info = RTSPStreamInfo(name="video_main", width=160, height=120, fps=10, bitrate="4M")
        session = make_session()
        session.rtp_port = 7000
        session.rtp_socket = None
        udp = server._build_ffmpeg_rtp_cmd_udp(info, session)
        tcp = server._build_ffmpeg_rtp_cmd_tcp_local(info, 6000)
        for cmd in (udp, tcp):
            bi = cmd.index("-bufsize")
            assert cmd[bi + 1] == "8M"

    def test_gop_size_never_below_one(self):
        server = _server_with_stream()
        assert server._gop_size(0) == 1
        assert server._gop_size(15) == 30


# ---------------------------------------------------------------------------
# Fix 4: version-driven pacing -- encoder writes only when the buffer advances.
# ---------------------------------------------------------------------------


class TestVersionDrivenPacing:
    def test_stream_frame_bumps_version(self):
        server = _server_with_stream()
        assert server._frame_versions["video_main"] == 0
        server.stream_frame("video_main", np.zeros((120, 160, 3), np.uint8))
        assert server._frame_versions["video_main"] == 1
        server.stream_frame("video_main", np.zeros((120, 160, 3), np.uint8))
        assert server._frame_versions["video_main"] == 2

    def test_encoder_skips_write_when_version_unchanged(self, monkeypatch):
        """A static single-slot buffer (version never changes) must be written
        at most once, not re-written every loop pass (the old double-pace bug)."""
        server = _server_with_stream(w=4, h=4)
        info = server._streams["video_main"]
        session = make_session(state=RTSPState.PLAYING)
        session.rtp_port = 7000
        server._frame_buffers["video_main"] = np.zeros((4, 4, 3), np.uint8)
        # version stays 0 across iterations
        monkeypatch.setattr("ipycam.rtsp.time.sleep", lambda *_: None)

        proc = MagicMock()
        proc.stdin = MagicMock()
        proc.stderr = MagicMock()
        proc.stderr.read.return_value = b""
        # 3 alive polls then death -> loop runs a few times but frame is stale.
        proc.poll.side_effect = [None, None, None, 1]
        monkeypatch.setattr("ipycam.rtsp.subprocess.Popen", lambda *a, **k: proc)

        server._rtp_encoder_loop(session, "video_main", info)

        assert proc.stdin.write.call_count == 1  # written once, not per-iteration
