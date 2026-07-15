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


class TestCameraConfigAtomicSave:
    """Tests for atomic save behavior and path tracking"""

    def test_atomic_save_valid_and_roundtrips(self, custom_config, tmp_path):
        """Atomic save should produce a valid JSON file that load round-trips"""
        target = tmp_path / "config.json"
        assert custom_config.save(str(target)) is True
        assert target.exists()

        # File is valid JSON
        with open(target, 'r') as f:
            data = json.load(f)
        assert data['name'] == custom_config.name

        # And it round-trips through load
        loaded = CameraConfig.load(str(target))
        assert loaded.name == custom_config.name
        assert loaded.main_width == custom_config.main_width
        assert loaded.main_fps == custom_config.main_fps
        assert loaded.hw_accel == custom_config.hw_accel

    def test_save_leaves_no_temp_files(self, custom_config, tmp_path):
        """Atomic save must clean up its temp file after replacing target"""
        target = tmp_path / "config.json"
        custom_config.save(str(target))
        leftovers = [p.name for p in tmp_path.iterdir() if p.name != "config.json"]
        assert leftovers == []

    def test_config_path_not_in_asdict(self, default_config):
        """The path-tracking attribute must never leak into asdict()"""
        from dataclasses import asdict
        data = asdict(default_config)
        assert '_config_path' not in data

    def test_config_path_not_in_saved_json(self, custom_config, tmp_path):
        """The path-tracking attribute must never leak into the saved JSON"""
        target = tmp_path / "config.json"
        custom_config.save(str(target))
        with open(target, 'r') as f:
            data = json.load(f)
        assert '_config_path' not in data

    def test_save_writes_back_to_loaded_path(self, tmp_path):
        """save() with no arg must write back to the path load() read from"""
        src = tmp_path / "custom.json"
        with open(src, 'w') as f:
            json.dump({"name": "Custom Cam", "main_width": 1280}, f)

        config = CameraConfig.load(str(src))
        assert config._config_path == str(src)

        # Mutate and save with NO argument
        config.name = "Edited Cam"
        assert config.save() is True

        # It must have persisted to the original custom path, not the default
        with open(src, 'r') as f:
            data = json.load(f)
        assert data['name'] == "Edited Cam"


class TestCameraConfigAuth:
    """Tests for the optional authentication fields / auth_enabled property"""

    def test_default_credentials_empty(self, default_config):
        assert default_config.username == ""
        assert default_config.password == ""

    def test_auth_disabled_by_default(self, default_config):
        """Empty credentials => auth disabled (backward-compatible open mode)"""
        assert default_config.auth_enabled is False

    def test_auth_enabled_requires_both(self):
        assert CameraConfig(username="admin").auth_enabled is False
        assert CameraConfig(password="pw").auth_enabled is False
        assert CameraConfig(username="admin", password="pw").auth_enabled is True

    def test_credentials_not_editable_via_apply_updates(self, default_config):
        """Credentials must never be settable through the generic update path"""
        applied, rejected, _ = default_config.apply_updates({
            'username': 'hacker',
            'password': 'guessed',
        })
        assert 'username' in rejected
        assert 'password' in rejected
        assert applied == []
        assert default_config.username == ""
        assert default_config.password == ""

    def test_credentials_roundtrip_save_load(self, tmp_path):
        config = CameraConfig(username="admin", password="s3cr3t")
        target = tmp_path / "config.json"
        assert config.save(str(target)) is True

        # Persisted to JSON like any other field
        with open(target, 'r') as f:
            data = json.load(f)
        assert data['username'] == "admin"
        assert data['password'] == "s3cr3t"

        loaded = CameraConfig.load(str(target))
        assert loaded.username == "admin"
        assert loaded.password == "s3cr3t"
        assert loaded.auth_enabled is True


class TestCameraConfigApplyUpdates:
    """Tests for CameraConfig.apply_updates validation"""

    def test_accepts_valid_values(self, default_config):
        applied, rejected, restart_keys = default_config.apply_updates({
            'main_width': 1280,
            'main_height': 720,
            'main_fps': 25,
            'hw_accel': 'nvenc',
            'name': 'New Name',
        })
        assert set(applied) == {'main_width', 'main_height', 'main_fps', 'hw_accel', 'name'}
        assert rejected == []
        assert default_config.main_width == 1280
        assert default_config.main_fps == 25
        assert default_config.hw_accel == 'nvenc'
        assert default_config.name == 'New Name'
        # Dimension/fps/hw changes require a restart
        assert set(restart_keys) == {'main_width', 'main_height', 'main_fps', 'hw_accel'}

    def test_rejects_zero_fps(self, default_config):
        applied, rejected, _ = default_config.apply_updates({'main_fps': 0})
        assert 'main_fps' in rejected
        assert 'main_fps' not in applied
        assert default_config.main_fps == 30  # unchanged

    def test_rejects_negative_dimensions(self, default_config):
        applied, rejected, _ = default_config.apply_updates({
            'main_width': -1920,
            'main_height': -1080,
        })
        assert 'main_width' in rejected
        assert 'main_height' in rejected
        assert default_config.main_width == 1920
        assert default_config.main_height == 1080

    def test_rejects_oversized_dimensions(self, default_config):
        applied, rejected, _ = default_config.apply_updates({
            'main_width': 99999,
            'main_height': 99999,
        })
        assert 'main_width' in rejected
        assert 'main_height' in rejected

    def test_rejects_bad_hw_accel(self, default_config):
        applied, rejected, _ = default_config.apply_updates({'hw_accel': 'magic'})
        assert 'hw_accel' in rejected
        assert default_config.hw_accel == 'auto'

    def test_rejects_bad_bitrate(self, default_config):
        applied, rejected, _ = default_config.apply_updates({'main_bitrate': 'lots'})
        assert 'main_bitrate' in rejected
        assert default_config.main_bitrate == '8M'

    def test_accepts_valid_bitrate(self, default_config):
        applied, rejected, _ = default_config.apply_updates({'main_bitrate': '4M'})
        assert 'main_bitrate' in applied
        assert default_config.main_bitrate == '4M'

    def test_rejects_bad_timestamp_position(self, default_config):
        applied, rejected, _ = default_config.apply_updates({'timestamp_position': 'middle'})
        assert 'timestamp_position' in rejected

    def test_rejects_protected_identity_and_network_fields(self, default_config):
        original_ip = default_config.local_ip
        applied, rejected, _ = default_config.apply_updates({
            'local_ip': '1.2.3.4',
            'firmware_version': '9.9.9',
            'serial_number': 'HACKED',
            'onvif_port': 1,
        })
        assert set(rejected) == {'local_ip', 'firmware_version', 'serial_number', 'onvif_port'}
        assert applied == []
        assert default_config.local_ip == original_ip
        assert default_config.serial_number == "PY-000001"

    def test_mix_of_valid_and_invalid(self, default_config):
        applied, rejected, restart_keys = default_config.apply_updates({
            'main_fps': 60,       # valid
            'main_width': 0,      # invalid
            'name': 'Cam',        # valid, no restart
        })
        assert 'main_fps' in applied
        assert 'name' in applied
        assert 'main_width' in rejected
        assert default_config.main_fps == 60
        assert default_config.main_width == 1920
        assert 'main_fps' in restart_keys
        assert 'name' not in restart_keys

    def test_unchanged_value_not_in_restart_keys(self, default_config):
        """A valid value equal to the current one shouldn't trigger a restart"""
        applied, rejected, restart_keys = default_config.apply_updates({
            'main_fps': 30,  # same as default
        })
        assert 'main_fps' in applied
        assert restart_keys == []


class TestCameraConfigDisplayTransformDefaults:
    """Tests for the flip/mirror/rotation dataclass defaults (no-op)"""

    def test_defaults_are_no_op(self, default_config):
        assert default_config.flip is False
        assert default_config.mirror is False
        assert default_config.rotation == 0


class TestCameraConfigDisplayTransformUpdates:
    """Tests for apply_updates() validation of flip/mirror/rotation"""

    def test_accepts_flip_true(self, default_config):
        applied, rejected, restart_keys = default_config.apply_updates({'flip': True})
        assert 'flip' in applied
        assert rejected == []
        assert default_config.flip is True
        assert 'flip' not in restart_keys

    def test_accepts_mirror_true(self, default_config):
        applied, rejected, restart_keys = default_config.apply_updates({'mirror': True})
        assert 'mirror' in applied
        assert rejected == []
        assert default_config.mirror is True
        assert 'mirror' not in restart_keys

    def test_accepts_flip_mirror_alternate_truthy_forms(self, default_config):
        """flip/mirror accept the same 0/1 and string forms as show_timestamp"""
        applied, rejected, _ = default_config.apply_updates({
            'flip': 'true', 'mirror': 1,
        })
        assert set(applied) == {'flip', 'mirror'}
        assert rejected == []
        assert default_config.flip is True
        assert default_config.mirror is True

    def test_rejects_bad_flip_mirror_values(self, default_config):
        applied, rejected, _ = default_config.apply_updates({
            'flip': 'sideways', 'mirror': [],
        })
        assert 'flip' in rejected
        assert 'mirror' in rejected
        assert default_config.flip is False
        assert default_config.mirror is False

    def test_accepts_rotation_90(self, default_config):
        applied, rejected, restart_keys = default_config.apply_updates({'rotation': 90})
        assert 'rotation' in applied
        assert rejected == []
        assert default_config.rotation == 90
        assert 'rotation' in restart_keys

    def test_accepts_all_valid_rotation_values(self, default_config):
        for value in (0, 90, 180, 270):
            config = CameraConfig()
            applied, rejected, _ = config.apply_updates({'rotation': value})
            assert 'rotation' in applied
            assert rejected == []
            assert config.rotation == value

    def test_accepts_rotation_as_numeric_string(self, default_config):
        applied, rejected, _ = default_config.apply_updates({'rotation': '180'})
        assert 'rotation' in applied
        assert default_config.rotation == 180

    def test_rejects_invalid_rotation_value(self, default_config):
        applied, rejected, _ = default_config.apply_updates({'rotation': 45})
        assert 'rotation' in rejected
        assert default_config.rotation == 0

    def test_rejects_non_numeric_rotation(self, default_config):
        applied, rejected, _ = default_config.apply_updates({'rotation': 'diagonal'})
        assert 'rotation' in rejected
        assert default_config.rotation == 0

    def test_rotation_unchanged_not_in_restart_keys(self, default_config):
        applied, rejected, restart_keys = default_config.apply_updates({'rotation': 0})
        assert 'rotation' in applied
        assert restart_keys == []

    def test_rotation_change_between_any_values_triggers_restart(self, default_config):
        """Simplest-correct restart policy: ANY changed rotation value
        restarts, including 90 -> 270 which doesn't change the swap state."""
        _, _, restart_keys_1 = default_config.apply_updates({'rotation': 90})
        assert 'rotation' in restart_keys_1
        _, _, restart_keys_2 = default_config.apply_updates({'rotation': 270})
        assert 'rotation' in restart_keys_2

    def test_flip_mirror_not_in_restart_fields_rotation_is(self):
        from ipycam.config import RESTART_FIELDS
        assert 'flip' not in RESTART_FIELDS
        assert 'mirror' not in RESTART_FIELDS
        assert 'rotation' in RESTART_FIELDS

    def test_flip_mirror_rotation_in_editable_fields(self):
        from ipycam.config import EDITABLE_FIELDS
        assert {'flip', 'mirror', 'rotation'} <= EDITABLE_FIELDS


class TestCameraConfigRecording:
    """Tests for the recording fields, defaults, and apply_updates validation
    (step 4.4)."""

    def test_recording_defaults_off(self, default_config):
        assert default_config.recording_enabled is False
        assert default_config.recording_format == "mp4"
        assert default_config.recording_path == "recordings"
        assert default_config.recording_max_file_mb == 1024
        assert default_config.recording_pre_seconds == 0

    def test_recording_editable_fields_registered(self):
        from ipycam.config import EDITABLE_FIELDS
        assert {'recording_enabled', 'recording_format',
                'recording_max_file_mb', 'recording_pre_seconds'} <= EDITABLE_FIELDS

    def test_recording_path_is_not_editable(self):
        """recording_path is a filesystem location -> never web-editable."""
        from ipycam.config import EDITABLE_FIELDS
        assert 'recording_path' not in EDITABLE_FIELDS

    def test_recording_fields_do_not_restart_stream(self):
        from ipycam.config import RESTART_FIELDS
        assert RESTART_FIELDS.isdisjoint({
            'recording_enabled', 'recording_format',
            'recording_max_file_mb', 'recording_pre_seconds',
        })

    def test_recording_path_rejected_via_apply_updates(self, default_config):
        applied, rejected, _ = default_config.apply_updates(
            {'recording_path': '/etc'}
        )
        assert 'recording_path' in rejected
        assert default_config.recording_path == "recordings"

    def test_accepts_valid_format(self, default_config):
        for fmt in ('mp4', 'avi'):
            applied, rejected, _ = default_config.apply_updates({'recording_format': fmt})
            assert 'recording_format' in applied
            assert default_config.recording_format == fmt

    def test_format_is_lowercased(self, default_config):
        applied, rejected, _ = default_config.apply_updates({'recording_format': 'MP4'})
        assert 'recording_format' in applied
        assert default_config.recording_format == 'mp4'

    def test_rejects_unknown_format(self, default_config):
        applied, rejected, _ = default_config.apply_updates({'recording_format': 'mkv'})
        assert 'recording_format' in rejected
        assert default_config.recording_format == 'mp4'

    def test_accepts_pre_seconds_in_range(self, default_config):
        for v in (0, 1, 15, 30):
            applied, rejected, _ = default_config.apply_updates({'recording_pre_seconds': v})
            assert 'recording_pre_seconds' in applied
            assert default_config.recording_pre_seconds == v

    def test_rejects_pre_seconds_over_cap(self, default_config):
        applied, rejected, _ = default_config.apply_updates({'recording_pre_seconds': 31})
        assert 'recording_pre_seconds' in rejected
        assert default_config.recording_pre_seconds == 0

    def test_rejects_negative_pre_seconds(self, default_config):
        applied, rejected, _ = default_config.apply_updates({'recording_pre_seconds': -1})
        assert 'recording_pre_seconds' in rejected

    def test_accepts_pre_seconds_numeric_string(self, default_config):
        applied, rejected, _ = default_config.apply_updates({'recording_pre_seconds': '5'})
        assert 'recording_pre_seconds' in applied
        assert default_config.recording_pre_seconds == 5

    def test_accepts_valid_max_mb(self, default_config):
        applied, rejected, _ = default_config.apply_updates({'recording_max_file_mb': 256})
        assert 'recording_max_file_mb' in applied
        assert default_config.recording_max_file_mb == 256

    def test_rejects_zero_max_mb(self, default_config):
        applied, rejected, _ = default_config.apply_updates({'recording_max_file_mb': 0})
        assert 'recording_max_file_mb' in rejected
        assert default_config.recording_max_file_mb == 1024

    def test_rejects_negative_max_mb(self, default_config):
        applied, rejected, _ = default_config.apply_updates({'recording_max_file_mb': -5})
        assert 'recording_max_file_mb' in rejected

    def test_rejects_absurd_max_mb(self, default_config):
        applied, rejected, _ = default_config.apply_updates({'recording_max_file_mb': 99_999_999})
        assert 'recording_max_file_mb' in rejected

    def test_accepts_enabled_toggle(self, default_config):
        applied, rejected, _ = default_config.apply_updates({'recording_enabled': True})
        assert 'recording_enabled' in applied
        assert default_config.recording_enabled is True

    def test_enabled_accepts_truthy_string_forms(self, default_config):
        applied, rejected, _ = default_config.apply_updates({'recording_enabled': 'on'})
        assert 'recording_enabled' in applied
        assert default_config.recording_enabled is True

    def test_recording_roundtrip_save_load(self, tmp_path):
        config = CameraConfig(
            recording_enabled=True,
            recording_format='avi',
            recording_path='/tmp/recs',
            recording_max_file_mb=512,
            recording_pre_seconds=5,
        )
        target = tmp_path / "config.json"
        assert config.save(str(target)) is True
        loaded = CameraConfig.load(str(target))
        assert loaded.recording_enabled is True
        assert loaded.recording_format == 'avi'
        assert loaded.recording_path == '/tmp/recs'
        assert loaded.recording_max_file_mb == 512
        assert loaded.recording_pre_seconds == 5


class TestCameraConfigSetCredentials:
    """Tests for CameraConfig.set_credentials (dedicated /api/credentials path)"""

    def test_happy_path_sets_both_and_enables_auth(self, default_config):
        ok, err = default_config.set_credentials('admin', 'secret')
        assert ok is True
        assert err is None
        assert default_config.username == 'admin'
        assert default_config.password == 'secret'
        assert default_config.auth_enabled is True

    def test_setting_again_overwrites_previous_credentials(self, default_config):
        default_config.set_credentials('admin', 'secret')
        ok, err = default_config.set_credentials('root', 'newpass')
        assert ok is True
        assert err is None
        assert default_config.username == 'root'
        assert default_config.password == 'newpass'
        assert default_config.auth_enabled is True

    def test_clearing_both_disables_auth(self, default_config):
        default_config.set_credentials('admin', 'secret')
        ok, err = default_config.set_credentials('', '')
        assert ok is True
        assert err is None
        assert default_config.username == ''
        assert default_config.password == ''
        assert default_config.auth_enabled is False

    def test_rejects_empty_username_with_nonempty_password(self, default_config):
        ok, err = default_config.set_credentials('', 'secret')
        assert ok is False
        assert err
        # Rejected -- nothing changed.
        assert default_config.username == ''
        assert default_config.password == ''
        assert default_config.auth_enabled is False

    def test_rejects_nonempty_username_with_empty_password(self, default_config):
        ok, err = default_config.set_credentials('admin', '')
        assert ok is False
        assert err
        assert default_config.username == ''
        assert default_config.password == ''

    def test_rejected_update_does_not_clobber_existing_credentials(self, default_config):
        """A rejected call must leave previously-set credentials intact."""
        default_config.set_credentials('admin', 'secret')
        ok, err = default_config.set_credentials('', 'oops')
        assert ok is False
        assert default_config.username == 'admin'
        assert default_config.password == 'secret'

    def test_username_is_stripped_password_is_not(self, default_config):
        ok, err = default_config.set_credentials('  admin  ', '  secret  ')
        assert ok is True
        assert default_config.username == 'admin'
        assert default_config.password == '  secret  '
