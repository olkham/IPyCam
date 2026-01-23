"""
Tests for MJPEGStreamer and helper functions
"""

import time
from io import BytesIO
from unittest.mock import MagicMock, patch

import pytest
import numpy as np

from ipycam.mjpeg import (
    MJPEGStreamer,
    MJPEGClient,
    check_go2rtc_running,
    check_rtsp_port_available,
)


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
        streamer = MJPEGStreamer()
        streamer.start()
        client = streamer.add_client(mock_wfile)

        result = streamer.stream_frame(small_frame)
        assert result is True
        assert streamer.frames_sent == 1
        assert client.frames_sent == 1
        mock_wfile.write.assert_called()
        mock_wfile.flush.assert_called()

    def test_stream_frame_stores_last_frame(self, small_frame):
        streamer = MJPEGStreamer()
        streamer.start()
        streamer.stream_frame(small_frame)
        assert streamer._last_frame is not None

    def test_stream_frame_disconnected_client_removed(self, small_frame, mock_wfile):
        streamer = MJPEGStreamer()
        streamer.start()
        client = streamer.add_client(mock_wfile)

        # Simulate connection error
        mock_wfile.write.side_effect = BrokenPipeError()

        result = streamer.stream_frame(small_frame)
        assert result is False
        assert streamer.client_count == 0
        assert client.connected is False

    def test_stream_frame_multiple_clients_partial_failure(self, small_frame):
        streamer = MJPEGStreamer()
        streamer.start()

        mock1 = MagicMock()
        mock2 = MagicMock()
        mock2.write.side_effect = ConnectionResetError()

        streamer.add_client(mock1)
        streamer.add_client(mock2)

        result = streamer.stream_frame(small_frame)
        assert result is True  # At least one client succeeded
        assert streamer.client_count == 1  # Failed client removed

    def test_stream_frame_format(self, small_frame, mock_wfile):
        streamer = MJPEGStreamer()
        streamer.start()
        streamer.add_client(mock_wfile)
        streamer.stream_frame(small_frame)

        # Get what was written
        call_args = mock_wfile.write.call_args[0][0]
        assert call_args.startswith(MJPEGStreamer.BOUNDARY)
        assert b"Content-Type: image/jpeg" in call_args
        assert b"Content-Length:" in call_args


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
