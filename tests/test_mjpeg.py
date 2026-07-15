"""
Tests for MJPEGStreamer and helper functions
"""

import threading
import time
from io import BytesIO
from unittest.mock import MagicMock, patch

import pytest
import numpy as np
import cv2

from ipycam.mjpeg import (
    MJPEGStreamer,
    MJPEGClient,
    check_go2rtc_running,
    check_rtsp_port_available,
)


# ---------------------------------------------------------------------------
# Helpers for the async streamer design.
#
# After the decoupling rework, stream_frame() is a non-blocking enqueue: a
# single worker thread encodes each frame once, and each client's frames are
# written by its OWN writer (the HTTP connection thread in production). These
# helpers reproduce that writer in-thread and let tests wait for the async
# encode+deliver pipeline to catch up.
# ---------------------------------------------------------------------------


def _wait(pred, timeout=2.0, interval=0.005):
    """Poll pred() until it is truthy or timeout elapses. Returns final value."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return True
        time.sleep(interval)
    return pred()


def _serve_in_thread(streamer, client):
    """Run streamer.serve_client(client) on a daemon thread (the HTTP writer)."""
    t = threading.Thread(target=streamer.serve_client, args=(client,), daemon=True)
    t.start()
    return t


class TestMJPEGClient:
    """Tests for MJPEGClient dataclass"""

    def test_mjpeg_client_creation(self, mock_wfile):
        client = MJPEGClient(wfile=mock_wfile)
        assert client.wfile is mock_wfile
        assert client.connected is True
        assert client.frames_sent == 0

    def test_mjpeg_client_custom_values(self, mock_wfile):
        client = MJPEGClient(wfile=mock_wfile, connected=False, frames_sent=100)
        assert client.connected is False
        assert client.frames_sent == 100


class TestMJPEGStreamerInitialization:
    """Tests for MJPEGStreamer initialization"""

    def test_default_quality(self):
        streamer = MJPEGStreamer()
        assert streamer.quality == 80

    def test_custom_quality(self):
        streamer = MJPEGStreamer(quality=50)
        assert streamer.quality == 50

    def test_initial_state(self):
        streamer = MJPEGStreamer()
        assert streamer._is_running is False
        assert streamer._last_frame is None
        assert streamer._frame_count == 0
        assert streamer.client_count == 0


class TestMJPEGStreamerStartStop:
    """Tests for MJPEGStreamer start/stop"""

    def test_start_sets_running(self):
        streamer = MJPEGStreamer()
        result = streamer.start()
        assert result is True
        assert streamer.is_running is True
        assert streamer._start_time is not None

    def test_start_resets_counters(self):
        streamer = MJPEGStreamer()
        streamer._frame_count = 100
        streamer.start()
        assert streamer._frame_count == 0

    def test_stop_clears_running(self):
        streamer = MJPEGStreamer()
        streamer.start()
        streamer.stop()
        assert streamer.is_running is False

    def test_stop_disconnects_clients(self, mock_wfile):
        streamer = MJPEGStreamer()
        streamer.start()
        client = streamer.add_client(mock_wfile)
        assert client.connected is True

        streamer.stop()
        assert client.connected is False
        assert streamer.client_count == 0


class TestMJPEGStreamerClientManagement:
    """Tests for MJPEGStreamer client management"""

    def test_add_client(self, mock_wfile):
        streamer = MJPEGStreamer()
        streamer.start()
        client = streamer.add_client(mock_wfile)
        assert isinstance(client, MJPEGClient)
        assert client.wfile is mock_wfile
        assert streamer.client_count == 1

    def test_add_multiple_clients(self):
        streamer = MJPEGStreamer()
        streamer.start()

        mock1 = MagicMock()
        mock2 = MagicMock()
        mock3 = MagicMock()

        streamer.add_client(mock1)
        streamer.add_client(mock2)
        streamer.add_client(mock3)

        assert streamer.client_count == 3

    def test_remove_client(self, mock_wfile):
        streamer = MJPEGStreamer()
        streamer.start()
        client = streamer.add_client(mock_wfile)
        assert streamer.client_count == 1

        streamer.remove_client(client)
        assert streamer.client_count == 0
        assert client.connected is False

    def test_remove_nonexistent_client(self, mock_wfile):
        streamer = MJPEGStreamer()
        streamer.start()
        client = MJPEGClient(wfile=mock_wfile)
        # Should not raise
        streamer.remove_client(client)


class TestMJPEGStreamerFrameStreaming:
    """Tests for MJPEGStreamer frame streaming"""

    def test_stream_frame_not_running(self, small_frame):
        streamer = MJPEGStreamer()
        # Not started
        result = streamer.stream_frame(small_frame)
        assert result is False

    def test_stream_frame_no_clients(self, small_frame):
        streamer = MJPEGStreamer()
        streamer.start()
        # No clients connected
        result = streamer.stream_frame(small_frame)
        assert result is False
        # But frame count should still increment
        assert streamer.frames_sent == 1

    def test_stream_frame_with_client(self, small_frame, mock_wfile):
        # Adapted for the async design: stream_frame() enqueues, the worker
        # encodes, and the client's own writer (serve_client) delivers.
        streamer = MJPEGStreamer()
        streamer.start()
        client = streamer.add_client(mock_wfile)
        writer = _serve_in_thread(streamer, client)

        result = streamer.stream_frame(small_frame)
        assert result is True  # a connected client exists to receive it
        assert streamer.frames_sent == 1  # counted synchronously on submit
        assert _wait(lambda: client.frames_sent >= 1)
        mock_wfile.write.assert_called()
        mock_wfile.flush.assert_called()

        streamer.stop()
        writer.join(timeout=2.0)

    def test_stream_frame_stores_last_frame(self, small_frame):
        # _last_frame is now set by the encode worker (async), so wait for it.
        streamer = MJPEGStreamer()
        streamer.start()
        streamer.stream_frame(small_frame)
        assert _wait(lambda: streamer._last_frame is not None)
        streamer.stop()

    def test_stream_frame_disconnected_client_removed(self, small_frame, mock_wfile):
        # A broken write now surfaces on the client's writer thread, which
        # marks the client disconnected and removes it.
        streamer = MJPEGStreamer()
        streamer.start()
        client = streamer.add_client(mock_wfile)
        mock_wfile.write.side_effect = BrokenPipeError()
        writer = _serve_in_thread(streamer, client)

        streamer.stream_frame(small_frame)
        assert _wait(lambda: streamer.client_count == 0)
        assert client.connected is False

        streamer.stop()
        writer.join(timeout=2.0)

    def test_stream_frame_multiple_clients_partial_failure(self, small_frame):
        streamer = MJPEGStreamer()
        streamer.start()

        mock1 = MagicMock()
        mock2 = MagicMock()
        mock2.write.side_effect = ConnectionResetError()

        c1 = streamer.add_client(mock1)
        c2 = streamer.add_client(mock2)
        w1 = _serve_in_thread(streamer, c1)
        w2 = _serve_in_thread(streamer, c2)

        result = streamer.stream_frame(small_frame)
        assert result is True  # there were connected clients at submit time
        # The failed client is removed; the healthy one survives and receives.
        assert _wait(lambda: streamer.client_count == 1)
        assert _wait(lambda: mock1.write.called)
        assert c1.connected is True
        assert c2.connected is False

        streamer.stop()
        w1.join(timeout=2.0)
        w2.join(timeout=2.0)

    def test_stream_frame_format(self, small_frame, mock_wfile):
        streamer = MJPEGStreamer()
        streamer.start()
        client = streamer.add_client(mock_wfile)
        writer = _serve_in_thread(streamer, client)

        streamer.stream_frame(small_frame)
        assert _wait(lambda: mock_wfile.write.called)

        # Get what was written
        call_args = mock_wfile.write.call_args[0][0]
        assert call_args.startswith(MJPEGStreamer.BOUNDARY)
        assert b"Content-Type: image/jpeg" in call_args
        assert b"Content-Length:" in call_args

        streamer.stop()
        writer.join(timeout=2.0)


class TestMJPEGStreamerAsyncIsolation:
    """The decoupling contract: stream_frame never blocks and clients are isolated."""

    def test_stream_frame_returns_immediately_with_slow_client(self, small_frame):
        """A stalled client must not slow down stream_frame or the fast client."""
        streamer = MJPEGStreamer()
        streamer.start()

        slow = MagicMock()
        slow.write.side_effect = lambda data: time.sleep(0.5)  # simulated stall
        fast = MagicMock()

        c_slow = streamer.add_client(slow)
        c_fast = streamer.add_client(fast)
        w_slow = _serve_in_thread(streamer, c_slow)
        w_fast = _serve_in_thread(streamer, c_fast)

        # Even with a client whose writes take 0.5s each, stream_frame returns
        # essentially instantly (it only enqueues).
        start = time.time()
        for _ in range(5):
            streamer.stream_frame(small_frame)
        elapsed = time.time() - start
        assert elapsed < 0.05, f"stream_frame blocked ({elapsed:.3f}s)"

        # And the fast client keeps receiving frames despite the slow one.
        assert _wait(lambda: fast.write.called, timeout=2.0)

        streamer.stop()
        w_slow.join(timeout=2.0)
        w_fast.join(timeout=2.0)

    def test_broken_pipe_client_removed_without_affecting_others(self, small_frame):
        """A broken client drops out; the healthy client stays and receives."""
        streamer = MJPEGStreamer()
        streamer.start()

        broken = MagicMock()
        broken.write.side_effect = BrokenPipeError()
        good = MagicMock()

        c_broken = streamer.add_client(broken)
        c_good = streamer.add_client(good)
        w_broken = _serve_in_thread(streamer, c_broken)
        w_good = _serve_in_thread(streamer, c_good)

        # Keep feeding frames until the broken client is gone and the good one
        # has received at least one frame.
        def pump():
            streamer.stream_frame(small_frame)
            return streamer.client_count == 1 and good.write.called

        assert _wait(pump, timeout=2.0)
        assert c_broken.connected is False
        assert c_good.connected is True
        assert c_good in streamer._clients

        streamer.stop()
        w_broken.join(timeout=2.0)
        w_good.join(timeout=2.0)

    def test_worker_stops_cleanly_on_stop(self):
        """The encode worker thread is joined and gone after stop()."""
        streamer = MJPEGStreamer()
        streamer.start()
        worker = streamer._worker
        assert worker is not None and worker.is_alive()

        streamer.stop()
        assert not worker.is_alive()
        assert streamer._worker is None

    def test_frames_dropped_stat_exposed(self, small_frame):
        """The frame-drop counter is surfaced for stats."""
        streamer = MJPEGStreamer(queue_size=1)
        # Not started -> worker not draining; overflow the tiny queue.
        streamer._is_running = True  # allow stream_frame to enqueue
        for _ in range(10):
            streamer.stream_frame(small_frame)
        assert streamer.frames_dropped > 0
        streamer._is_running = False


def _extract_jpeg(frame_data: bytes) -> bytes:
    """Pull the raw JPEG bytes out of one multipart chunk built by _wrap_multipart."""
    _, _, rest = frame_data.partition(b"\r\n\r\n")
    return rest[:-2] if rest.endswith(b"\r\n") else rest


class TestMJPEGStreamerSubStreamSelector:
    """Tests for the per-client main/sub stream selector (step 4.2)."""

    def test_add_client_default_stream_is_main(self, mock_wfile):
        streamer = MJPEGStreamer()
        client = streamer.add_client(mock_wfile)
        assert client.stream == 'main'

    def test_add_client_with_sub_stream(self, mock_wfile):
        streamer = MJPEGStreamer()
        client = streamer.add_client(mock_wfile, stream='sub')
        assert client.stream == 'sub'

    def test_add_client_invalid_stream_falls_back_to_main(self, mock_wfile):
        streamer = MJPEGStreamer()
        client = streamer.add_client(mock_wfile, stream='not-a-real-stream')
        assert client.stream == 'main'

    def test_sub_client_gets_resized_frame_main_client_gets_full_size(self, small_frame):
        # small_frame is 480x640x3 (h, w, c). With no fixed sub_width/height,
        # the encode worker falls back to half the incoming frame's size.
        streamer = MJPEGStreamer()
        streamer.start()

        main_client = streamer.add_client(MagicMock(), stream='main')
        sub_client = streamer.add_client(MagicMock(), stream='sub')

        streamer.stream_frame(small_frame)

        main_data = main_client.queue.get(timeout=2.0)
        sub_data = sub_client.queue.get(timeout=2.0)
        assert main_data is not None
        assert sub_data is not None

        main_decoded = cv2.imdecode(np.frombuffer(_extract_jpeg(main_data), dtype=np.uint8), cv2.IMREAD_COLOR)
        sub_decoded = cv2.imdecode(np.frombuffer(_extract_jpeg(sub_data), dtype=np.uint8), cv2.IMREAD_COLOR)

        assert main_decoded.shape[:2] == (480, 640)  # full resolution, unchanged
        assert sub_decoded.shape[:2] == (240, 320)   # half-size dynamic fallback

        streamer.stop()

    def test_fixed_sub_size_constructor_params_are_used(self, small_frame):
        """Explicit sub_width/sub_height override the dynamic half-size default."""
        streamer = MJPEGStreamer(sub_width=160, sub_height=90)
        streamer.start()

        sub_client = streamer.add_client(MagicMock(), stream='sub')
        streamer.stream_frame(small_frame)

        sub_data = sub_client.queue.get(timeout=2.0)
        sub_decoded = cv2.imdecode(np.frombuffer(_extract_jpeg(sub_data), dtype=np.uint8), cv2.IMREAD_COLOR)
        assert sub_decoded.shape[:2] == (90, 160)

        streamer.stop()

    def test_sub_encode_happens_once_per_frame_with_multiple_sub_clients(self, small_frame):
        """Multiple sub clients share ONE resize+encode, not one each."""
        streamer = MJPEGStreamer()
        streamer.start()

        sub1 = streamer.add_client(MagicMock(), stream='sub')
        sub2 = streamer.add_client(MagicMock(), stream='sub')
        main_client = streamer.add_client(MagicMock(), stream='main')

        real_imencode = cv2.imencode
        calls = []

        def counting_imencode(*args, **kwargs):
            calls.append(1)
            return real_imencode(*args, **kwargs)

        with patch('cv2.imencode', side_effect=counting_imencode):
            streamer.stream_frame(small_frame)
            assert _wait(
                lambda: sub1.queue.qsize() > 0 and sub2.queue.qsize() > 0 and main_client.queue.qsize() > 0,
                timeout=2.0,
            )
            time.sleep(0.1)  # let the worker fully settle before counting

        # Exactly one main-resolution encode + one sub-resolution encode for
        # this single frame, regardless of how many sub clients are connected.
        assert len(calls) == 2

        sub1_data = sub1.queue.get(timeout=1.0)
        sub2_data = sub2.queue.get(timeout=1.0)
        # Both sub clients received the SAME encoded bytes (one shared encode).
        assert sub1_data == sub2_data

        streamer.stop()

    def test_no_sub_clients_skips_sub_encode_entirely(self, small_frame):
        """When nobody has selected 'sub', only the main JPEG is encoded."""
        streamer = MJPEGStreamer()
        streamer.start()
        main_client = streamer.add_client(MagicMock(), stream='main')

        real_imencode = cv2.imencode
        calls = []

        def counting_imencode(*args, **kwargs):
            calls.append(1)
            return real_imencode(*args, **kwargs)

        with patch('cv2.imencode', side_effect=counting_imencode):
            streamer.stream_frame(small_frame)
            assert _wait(lambda: main_client.queue.qsize() > 0, timeout=2.0)
            time.sleep(0.1)

        assert len(calls) == 1
        streamer.stop()


class TestMJPEGStreamerStats:
    """Tests for MJPEGStreamer statistics"""

    def test_frames_sent(self, small_frame):
        streamer = MJPEGStreamer()
        streamer.start()

        for _ in range(5):
            streamer.stream_frame(small_frame)

        assert streamer.frames_sent == 5

    def test_elapsed_time(self):
        streamer = MJPEGStreamer()
        assert streamer.elapsed_time == 0

        streamer.start()
        time.sleep(0.1)
        elapsed = streamer.elapsed_time
        assert elapsed >= 0.1

    def test_actual_fps_no_frames(self):
        streamer = MJPEGStreamer()
        streamer.start()
        assert streamer.actual_fps == 0

    def test_actual_fps_with_frames(self, small_frame):
        streamer = MJPEGStreamer()
        streamer.start()

        # Stream some frames
        for _ in range(10):
            streamer.stream_frame(small_frame)
            time.sleep(0.05)  # ~20 fps

        fps = streamer.actual_fps
        # Should be roughly around 20 fps (with some tolerance)
        assert fps > 0


class TestMJPEGStreamerHeaders:
    """Tests for MJPEGStreamer.get_headers()"""

    def test_get_headers_returns_list(self):
        streamer = MJPEGStreamer()
        headers = streamer.get_headers()
        assert isinstance(headers, list)

    def test_get_headers_content_type(self):
        streamer = MJPEGStreamer()
        headers = streamer.get_headers()
        header_dict = dict(headers)
        assert 'Content-Type' in header_dict
        assert 'multipart/x-mixed-replace' in header_dict['Content-Type']
        assert 'boundary=frame' in header_dict['Content-Type']

    def test_get_headers_cache_control(self):
        streamer = MJPEGStreamer()
        headers = streamer.get_headers()
        header_dict = dict(headers)
        assert 'Cache-Control' in header_dict
        assert 'no-cache' in header_dict['Cache-Control']


class TestCheckGo2rtcRunning:
    """Tests for check_go2rtc_running helper function"""

    def test_returns_false_when_not_running(self):
        # Use a port that's unlikely to be in use
        result = check_go2rtc_running(port=59999, timeout=0.5)
        assert result is False

    def test_returns_false_with_invalid_host(self):
        # Invalid host should return False (TEST-NET-1 per RFC 5737)
        result = check_go2rtc_running(host="192.0.2.1", port=1984, timeout=0.5)
        assert result is False

    def test_accepts_custom_parameters(self):
        # Test that custom parameters are accepted
        result = check_go2rtc_running(host="127.0.0.1", port=59999, timeout=0.1)
        assert result is False


class TestCheckGo2rtcDetectionMocked:
    """check_go2rtc_running with the socket / HTTP layer mocked out, so a live
    go2rtc is reliably detected and a closed port is reliably rejected -- a
    running go2rtc must NEVER be reported as 'not detected'."""

    @staticmethod
    def _fake_socket(connect_result):
        sock = MagicMock()
        sock.connect_ex.return_value = connect_result
        return sock

    def test_returns_true_for_live_go2rtc_api(self):
        """TCP connect succeeds + go2rtc's /api answers 200 -> detected."""
        sock = self._fake_socket(0)
        response = MagicMock()
        response.status = 200
        response.__enter__.return_value = response
        with patch("socket.socket", return_value=sock), \
             patch("urllib.request.urlopen", return_value=response):
            assert check_go2rtc_running(port=1984, timeout=0.2) is True

    def test_returns_true_for_non_200_http_response(self):
        """A go2rtc-shaped HTTP *error* status is still a live server."""
        import urllib.error

        sock = self._fake_socket(0)
        http_error = urllib.error.HTTPError(
            url="http://127.0.0.1:1984/api", code=404, msg="NF", hdrs=None, fp=None
        )
        with patch("socket.socket", return_value=sock), \
             patch("urllib.request.urlopen", side_effect=http_error):
            assert check_go2rtc_running(port=1984, timeout=0.2) is True

    def test_returns_true_when_port_open_but_http_incomplete(self):
        """Port open but the HTTP request never completes cleanly -> still
        accepted (never report a live go2rtc as down just because /api was
        slow/odd)."""
        import urllib.error

        sock = self._fake_socket(0)
        with patch("socket.socket", return_value=sock), \
             patch("urllib.request.urlopen", side_effect=urllib.error.URLError("timeout")):
            assert check_go2rtc_running(port=1984, timeout=0.2) is True

    def test_returns_false_when_port_closed(self):
        """Nothing listening on the API port -> not detected."""
        sock = self._fake_socket(1)  # non-zero == connect failed
        with patch("socket.socket", return_value=sock):
            assert check_go2rtc_running(port=1984, timeout=0.2) is False


class TestCheckRTSPPortAvailable:
    """Tests for check_rtsp_port_available helper function"""

    def test_returns_false_when_not_available(self):
        # Use a port that's unlikely to be in use
        result = check_rtsp_port_available(port=59998, timeout=0.5)
        assert result is False

    def test_returns_false_with_invalid_host(self):
        # Invalid host should return False (TEST-NET-1 per RFC 5737)
        result = check_rtsp_port_available(host="192.0.2.1", port=8554, timeout=0.5)
        assert result is False

    def test_accepts_custom_parameters(self):
        # Test that custom parameters are accepted
        result = check_rtsp_port_available(host="127.0.0.1", port=59998, timeout=0.1)
        assert result is False
