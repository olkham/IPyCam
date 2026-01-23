"""
Tests for CameraConfig
"""

import os
import json
import tempfile
import pytest

from ipycam.config import CameraConfig, get_local_ip
from ipycam.streamer import StreamConfig, HWAccel


class TestGetLocalIP:
    """Tests for get_local_ip helper function"""

    def test_returns_string(self):
        """get_local_ip should return a string"""
        ip = get_local_ip()
        assert isinstance(ip, str)

    def test_returns_valid_ip_format(self):
        """get_local_ip should return a valid IP address format"""
        ip = get_local_ip()
        parts = ip.split('.')
        assert len(parts) == 4
        for part in parts:
            assert part.isdigit()
            assert 0 <= int(part) <= 255


class TestCameraConfigDefaults:
    """Tests for CameraConfig default values"""

    def test_default_name(self, default_config):
        assert default_config.name == "Virtual Camera"

    def test_default_manufacturer(self, default_config):
        assert default_config.manufacturer == "PythonCam"

    def test_default_model(self, default_config):
        assert default_config.model == "VirtualCam-1"

    def test_default_ports(self, default_config):
        assert default_config.onvif_port == 8080
        assert default_config.rtsp_port == 8554
        assert default_config.rtmp_port == 1935
        assert default_config.web_port == 8081
        assert default_config.go2rtc_api_port == 1984

    def test_default_main_stream(self, default_config):
        assert default_config.main_width == 1920
        assert default_config.main_height == 1080
        assert default_config.main_fps == 30
        assert default_config.main_bitrate == "8M"
        assert default_config.main_stream_name == "video_main"

    def test_default_sub_stream(self, default_config):
        assert default_config.sub_width == 640
        assert default_config.sub_height == 360
        assert default_config.sub_fps == 30
        assert default_config.sub_bitrate == "1M"
        assert default_config.sub_stream_name == "video_sub"

    def test_default_overlay_settings(self, default_config):
        assert default_config.show_timestamp is True
        assert default_config.timestamp_format == "%Y-%m-%d %H:%M:%S"
        assert default_config.timestamp_position == "bottom-left"

    def test_default_hw_accel(self, default_config):
        assert default_config.hw_accel == "auto"

    def test_local_ip_auto_detected(self, default_config):
        """local_ip should be auto-detected if not provided"""
        assert default_config.local_ip != ""
        # Should match get_local_ip result
        assert default_config.local_ip == get_local_ip()


class TestCameraConfigCustomValues:
    """Tests for CameraConfig with custom values"""

    def test_custom_name(self, custom_config):
        assert custom_config.name == "Test Camera"

    def test_custom_ports(self, custom_config):
        assert custom_config.onvif_port == 9080
        assert custom_config.rtsp_port == 9554
        assert custom_config.rtmp_port == 2935

    def test_custom_stream_settings(self, custom_config):
        assert custom_config.main_width == 1280
        assert custom_config.main_height == 720
        assert custom_config.main_fps == 25
        assert custom_config.main_bitrate == "4M"

    def test_custom_timestamp_disabled(self, custom_config):
        assert custom_config.show_timestamp is False
        assert custom_config.timestamp_position == "top-right"

    def test_custom_source_info(self, custom_config):
        assert custom_config.source_type == "video_file"
        assert custom_config.source_info == "test_video.mp4"


class TestCameraConfigURLProperties:
    """Tests for CameraConfig URL property methods"""

    def test_main_stream_rtmp(self, default_config):
        expected = f"rtmp://127.0.0.1:{default_config.rtmp_port}/{default_config.main_stream_name}"
        assert default_config.main_stream_rtmp == expected

    def test_sub_stream_rtmp(self, default_config):
        expected = f"rtmp://127.0.0.1:{default_config.rtmp_port}/{default_config.sub_stream_name}"
        assert default_config.sub_stream_rtmp == expected

    def test_main_stream_push_url(self, default_config):
        # Should be same as main_stream_rtmp
        assert default_config.main_stream_push_url == default_config.main_stream_rtmp

    def test_sub_stream_push_url(self, default_config):
        # Should be same as sub_stream_rtmp
        assert default_config.sub_stream_push_url == default_config.sub_stream_rtmp

    def test_main_stream_rtsp(self, default_config):
        expected = f"rtsp://{default_config.local_ip}:{default_config.rtsp_port}/{default_config.main_stream_name}"
        assert default_config.main_stream_rtsp == expected

    def test_sub_stream_rtsp(self, default_config):
        expected = f"rtsp://{default_config.local_ip}:{default_config.rtsp_port}/{default_config.sub_stream_name}"
        assert default_config.sub_stream_rtsp == expected

    def test_onvif_url(self, default_config):
        expected = f"http://{default_config.local_ip}:{default_config.onvif_port}/onvif/device_service"
        assert default_config.onvif_url == expected

    def test_webrtc_url(self, default_config):
        expected = f"http://{default_config.local_ip}:{default_config.go2rtc_api_port}"
        assert default_config.webrtc_url == expected


class TestCameraConfigStreamConversion:
    """Tests for CameraConfig.to_stream_config()"""

    def test_to_stream_config_returns_stream_config(self, default_config):
        stream_config = default_config.to_stream_config()
        assert isinstance(stream_config, StreamConfig)

    def test_to_stream_config_dimensions(self, default_config):
        stream_config = default_config.to_stream_config()
        assert stream_config.width == default_config.main_width
        assert stream_config.height == default_config.main_height
        assert stream_config.fps == default_config.main_fps

    def test_to_stream_config_bitrate(self, default_config):
        stream_config = default_config.to_stream_config()
        assert stream_config.bitrate == default_config.main_bitrate

    def test_to_stream_config_sub_stream(self, default_config):
        stream_config = default_config.to_stream_config()
        assert stream_config.sub_width == default_config.sub_width
        assert stream_config.sub_height == default_config.sub_height
        assert stream_config.sub_bitrate == default_config.sub_bitrate

    def test_to_stream_config_hw_accel_auto(self, default_config):
        stream_config = default_config.to_stream_config()
        assert stream_config.hw_accel == HWAccel.AUTO

    def test_to_stream_config_hw_accel_nvenc(self):
        config = CameraConfig(hw_accel="nvenc")
        stream_config = config.to_stream_config()
        assert stream_config.hw_accel == HWAccel.NVENC

    def test_to_stream_config_hw_accel_qsv(self):
        config = CameraConfig(hw_accel="qsv")
        stream_config = config.to_stream_config()
        assert stream_config.hw_accel == HWAccel.QSV

    def test_to_stream_config_hw_accel_cpu(self):
        config = CameraConfig(hw_accel="cpu")
        stream_config = config.to_stream_config()
        assert stream_config.hw_accel == HWAccel.CPU


class TestCameraConfigSaveLoad:
    """Tests for CameraConfig save/load functionality"""

    def test_save_creates_file(self, custom_config):
        with tempfile.NamedTemporaryFile(suffix='.json', delete=False) as f:
            temp_path = f.name

        try:
            result = custom_config.save(temp_path)
            assert result is True
            assert os.path.exists(temp_path)
        finally:
            if os.path.exists(temp_path):
                os.unlink(temp_path)

    def test_save_creates_valid_json(self, custom_config):
        with tempfile.NamedTemporaryFile(suffix='.json', delete=False) as f:
            temp_path = f.name

        try:
            custom_config.save(temp_path)
            with open(temp_path, 'r') as f:
                data = json.load(f)
            assert isinstance(data, dict)
            assert data['name'] == custom_config.name
            assert data['manufacturer'] == custom_config.manufacturer
        finally:
            if os.path.exists(temp_path):
                os.unlink(temp_path)

    def test_save_excludes_local_ip(self, custom_config):
        """local_ip should not be saved as it's auto-detected"""
        with tempfile.NamedTemporaryFile(suffix='.json', delete=False) as f:
            temp_path = f.name

        try:
            custom_config.save(temp_path)
            with open(temp_path, 'r') as f:
                data = json.load(f)
            assert 'local_ip' not in data
        finally:
            if os.path.exists(temp_path):
                os.unlink(temp_path)

    def test_load_from_file(self, temp_config_file):
        config = CameraConfig.load(temp_config_file)
        assert config.name == "Test Camera"
        assert config.manufacturer == "TestCorp"
        assert config.onvif_port == 9080

    def test_load_nonexistent_file_returns_defaults(self):
        config = CameraConfig.load("nonexistent_config_12345.json")
        # Should return default config
        assert config.name == "Virtual Camera"
        assert config.manufacturer == "PythonCam"

    def test_load_filters_invalid_fields(self):
        """Load should ignore fields not in CameraConfig"""
        with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
            json.dump({
                "name": "Filtered Camera",
                "invalid_field": "should be ignored",
                "another_invalid": 12345,
            }, f)
            temp_path = f.name

        try:
            config = CameraConfig.load(temp_path)
            assert config.name == "Filtered Camera"
            assert not hasattr(config, 'invalid_field')
        finally:
            if os.path.exists(temp_path):
                os.unlink(temp_path)

    def test_roundtrip_save_load(self, custom_config):
        """Config should survive save/load cycle"""
        with tempfile.NamedTemporaryFile(suffix='.json', delete=False) as f:
            temp_path = f.name

        try:
            custom_config.save(temp_path)
            loaded = CameraConfig.load(temp_path)

            assert loaded.name == custom_config.name
            assert loaded.manufacturer == custom_config.manufacturer
            assert loaded.model == custom_config.model
            assert loaded.onvif_port == custom_config.onvif_port
            assert loaded.main_width == custom_config.main_width
            assert loaded.main_fps == custom_config.main_fps
            assert loaded.hw_accel == custom_config.hw_accel
        finally:
            if os.path.exists(temp_path):
                os.unlink(temp_path)


class TestCameraConfigEdgeCases:
    """Edge case tests for CameraConfig"""

    def test_explicit_local_ip(self):
        """If local_ip is provided, it should not be overwritten"""
        config = CameraConfig(local_ip="192.168.1.100")
        assert config.local_ip == "192.168.1.100"

    def test_empty_string_local_ip_gets_auto_detected(self):
        """Empty string local_ip should be auto-detected"""
        config = CameraConfig(local_ip="")
        assert config.local_ip != ""

    def test_different_bitrate_formats(self):
        """Test different bitrate string formats"""
        config_m = CameraConfig(main_bitrate="4M")
        config_k = CameraConfig(main_bitrate="512K")
        config_num = CameraConfig(main_bitrate="1000000")

        assert config_m.main_bitrate == "4M"
        assert config_k.main_bitrate == "512K"
        assert config_num.main_bitrate == "1000000"
