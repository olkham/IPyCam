"""
Shared pytest fixtures for IPyCam tests
"""

import os
import sys
import tempfile
import json
from io import BytesIO
from unittest.mock import MagicMock

import pytest
import numpy as np

# Add the parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ipycam.config import CameraConfig
from ipycam.ptz import PTZController, PTZState, PTZVelocity, PTZPreset


@pytest.fixture
def default_config():
    """Create a default CameraConfig for testing"""
    return CameraConfig()


@pytest.fixture
def custom_config():
    """Create a custom CameraConfig with non-default values"""
    return CameraConfig(
        name="Test Camera",
        manufacturer="TestCorp",
        model="TestModel-X",
        serial_number="TEST-12345",
        firmware_version="2.0.0",
        onvif_port=9080,
        rtsp_port=9554,
        rtmp_port=2935,
        main_width=1280,
        main_height=720,
        main_fps=25,
        main_bitrate="4M",
        sub_width=320,
        sub_height=240,
        sub_fps=15,
        sub_bitrate="500K",
        hw_accel="cpu",
        show_timestamp=False,
        timestamp_position="top-right",
        source_type="video_file",
        source_info="test_video.mp4",
    )


@pytest.fixture
def temp_config_file(custom_config):
    """Create a temporary config file for testing save/load"""
    with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
        config_dict = {
            "name": custom_config.name,
            "manufacturer": custom_config.manufacturer,
            "model": custom_config.model,
            "serial_number": custom_config.serial_number,
            "onvif_port": custom_config.onvif_port,
            "main_width": custom_config.main_width,
            "main_height": custom_config.main_height,
            "main_fps": custom_config.main_fps,
        }
        json.dump(config_dict, f)
        temp_path = f.name

    yield temp_path

    # Cleanup
    if os.path.exists(temp_path):
        os.unlink(temp_path)


@pytest.fixture
def ptz_controller():
    """Create a PTZController for testing"""
    controller = PTZController(
        output_width=1920,
        output_height=1080,
        max_zoom=4.0,
        enable_digital_ptz=True
    )
    yield controller
    controller.stop()


@pytest.fixture
def ptz_controller_no_digital():
    """Create a PTZController with digital PTZ disabled"""
    controller = PTZController(
        output_width=1920,
        output_height=1080,
        max_zoom=4.0,
        enable_digital_ptz=False
    )
    yield controller
    controller.stop()


@pytest.fixture
def sample_frame():
    """Create a sample BGR frame for testing"""
    # Create a 1920x1080 BGR frame with a gradient
    frame = np.zeros((1080, 1920, 3), dtype=np.uint8)
    # Add horizontal gradient (blue channel)
    frame[:, :, 0] = np.tile(np.linspace(0, 255, 1920), (1080, 1)).astype(np.uint8)
    # Add vertical gradient (green channel)
    frame[:, :, 1] = np.tile(np.linspace(0, 255, 1080), (1920, 1)).T.astype(np.uint8)
    # Set red channel to constant
    frame[:, :, 2] = 128
    return frame


@pytest.fixture
def small_frame():
    """Create a small 640x480 BGR frame for testing"""
    frame = np.zeros((480, 640, 3), dtype=np.uint8)
    frame[:, :, 0] = 100  # Blue
    frame[:, :, 1] = 150  # Green
    frame[:, :, 2] = 200  # Red
    return frame


@pytest.fixture
def grayscale_frame():
    """Create a grayscale frame for testing mono image handling"""
    return np.random.randint(0, 256, (480, 640), dtype=np.uint8)


@pytest.fixture
def mock_wfile():
    """Create a mock writable file object for MJPEG client testing"""
    mock = MagicMock()
    mock.write = MagicMock()
    mock.flush = MagicMock()
    return mock


@pytest.fixture
def temp_presets_file():
    """Create a temporary presets file for PTZ testing"""
    with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
        presets = {
            "home": {
                "token": "home",
                "name": "Home",
                "pan": 0.0,
                "tilt": 0.0,
                "zoom": 0.0
            },
            "preset1": {
                "token": "preset1",
                "name": "Corner View",
                "pan": 0.5,
                "tilt": 0.3,
                "zoom": 0.2
            }
        }
        json.dump(presets, f)
        temp_path = f.name

    yield temp_path

    # Cleanup
    if os.path.exists(temp_path):
        os.unlink(temp_path)


@pytest.fixture
def mock_hardware_handler():
    """Create a mock PTZ hardware handler for testing"""
    handler = MagicMock()
    handler.on_continuous_move = MagicMock()
    handler.on_stop = MagicMock()
    handler.on_absolute_move = MagicMock()
    handler.on_relative_move = MagicMock()
    handler.on_goto_preset = MagicMock()
    handler.on_goto_home = MagicMock()
    return handler
