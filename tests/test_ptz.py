"""
Tests for PTZController
"""

import time
import pytest
import numpy as np

from ipycam.ptz import PTZController, PTZState, PTZVelocity, PTZPreset


class TestPTZDataClasses:
    """Tests for PTZ data classes"""

    def test_ptz_state_defaults(self):
        state = PTZState()
        assert state.pan == 0.0
        assert state.tilt == 0.0
        assert state.zoom == 0.0

    def test_ptz_state_custom_values(self):
        state = PTZState(pan=0.5, tilt=-0.3, zoom=0.8)
        assert state.pan == 0.5
        assert state.tilt == -0.3
        assert state.zoom == 0.8

    def test_ptz_velocity_defaults(self):
        velocity = PTZVelocity()
        assert velocity.pan_speed == 0.0
        assert velocity.tilt_speed == 0.0
        assert velocity.zoom_speed == 0.0

    def test_ptz_preset_creation(self):
        preset = PTZPreset(
            token="preset1",
            name="Corner View",
            pan=0.5,
            tilt=0.3,
            zoom=0.2
        )
        assert preset.token == "preset1"
        assert preset.name == "Corner View"
        assert preset.pan == 0.5
        assert preset.tilt == 0.3
        assert preset.zoom == 0.2


class TestPTZControllerInitialization:
    """Tests for PTZController initialization"""

    def test_default_initialization(self, ptz_controller):
        assert ptz_controller.output_width == 1920
        assert ptz_controller.output_height == 1080
        assert ptz_controller.max_zoom == 4.0
        assert ptz_controller.enable_digital_ptz is True

    def test_initial_state_is_home(self, ptz_controller):
        assert ptz_controller.state.pan == 0.0
        assert ptz_controller.state.tilt == 0.0
        assert ptz_controller.state.zoom == 0.0

    def test_initial_velocity_is_zero(self, ptz_controller):
        assert ptz_controller.velocity.pan_speed == 0.0
        assert ptz_controller.velocity.tilt_speed == 0.0
        assert ptz_controller.velocity.zoom_speed == 0.0

    def test_is_default_flag_initially_true(self, ptz_controller):
        assert ptz_controller._is_default is True

    def test_default_preset_exists(self, ptz_controller):
        presets = ptz_controller.get_presets()
        assert 'home' in presets


class TestPTZAbsoluteMove:
    """Tests for PTZController.absolute_move()"""

    def test_absolute_move_pan(self, ptz_controller):
        ptz_controller.absolute_move(pan=0.5)
        assert ptz_controller.state.pan == 0.5
        assert ptz_controller.state.tilt == 0.0  # Unchanged
        assert ptz_controller.state.zoom == 0.0  # Unchanged

    def test_absolute_move_tilt(self, ptz_controller):
        ptz_controller.absolute_move(tilt=-0.3)
        assert ptz_controller.state.pan == 0.0  # Unchanged
        assert ptz_controller.state.tilt == -0.3
        assert ptz_controller.state.zoom == 0.0  # Unchanged

    def test_absolute_move_zoom(self, ptz_controller):
        ptz_controller.absolute_move(zoom=0.7)
        assert ptz_controller.state.pan == 0.0  # Unchanged
        assert ptz_controller.state.tilt == 0.0  # Unchanged
        assert ptz_controller.state.zoom == 0.7

    def test_absolute_move_all(self, ptz_controller):
        ptz_controller.absolute_move(pan=0.5, tilt=-0.3, zoom=0.7)
        assert ptz_controller.state.pan == 0.5
        assert ptz_controller.state.tilt == -0.3
        assert ptz_controller.state.zoom == 0.7

    def test_absolute_move_clamps_pan_max(self, ptz_controller):
        ptz_controller.absolute_move(pan=2.0)
        assert ptz_controller.state.pan == 1.0

    def test_absolute_move_clamps_pan_min(self, ptz_controller):
        ptz_controller.absolute_move(pan=-2.0)
        assert ptz_controller.state.pan == -1.0

    def test_absolute_move_clamps_tilt_max(self, ptz_controller):
        ptz_controller.absolute_move(tilt=2.0)
        assert ptz_controller.state.tilt == 1.0

    def test_absolute_move_clamps_tilt_min(self, ptz_controller):
        ptz_controller.absolute_move(tilt=-2.0)
        assert ptz_controller.state.tilt == -1.0

    def test_absolute_move_clamps_zoom_max(self, ptz_controller):
        ptz_controller.absolute_move(zoom=2.0)
        assert ptz_controller.state.zoom == 1.0

    def test_absolute_move_clamps_zoom_min(self, ptz_controller):
        ptz_controller.absolute_move(zoom=-1.0)
        assert ptz_controller.state.zoom == 0.0

    def test_absolute_move_stops_continuous_movement(self, ptz_controller):
        ptz_controller.continuous_move(pan_speed=0.5, tilt_speed=0.5)
        ptz_controller.absolute_move(pan=0.0)
        assert ptz_controller.velocity.pan_speed == 0.0
        assert ptz_controller.velocity.tilt_speed == 0.0

    def test_absolute_move_updates_is_default_flag(self, ptz_controller):
        ptz_controller.absolute_move(pan=0.5)
        assert ptz_controller._is_default is False

        ptz_controller.absolute_move(pan=0.0, tilt=0.0, zoom=0.0)
        assert ptz_controller._is_default is True


class TestPTZRelativeMove:
    """Tests for PTZController.relative_move()"""

    def test_relative_move_pan(self, ptz_controller):
        ptz_controller.relative_move(pan_delta=0.2)
        assert ptz_controller.state.pan == 0.2

        ptz_controller.relative_move(pan_delta=0.3)
        assert ptz_controller.state.pan == 0.5

    def test_relative_move_tilt(self, ptz_controller):
        ptz_controller.relative_move(tilt_delta=-0.1)
        assert ptz_controller.state.tilt == pytest.approx(-0.1)

        ptz_controller.relative_move(tilt_delta=-0.2)
        assert ptz_controller.state.tilt == pytest.approx(-0.3)

    def test_relative_move_zoom(self, ptz_controller):
        ptz_controller.relative_move(zoom_delta=0.3)
        assert ptz_controller.state.zoom == 0.3

        ptz_controller.relative_move(zoom_delta=0.2)
        assert ptz_controller.state.zoom == 0.5

    def test_relative_move_clamps_values(self, ptz_controller):
        ptz_controller.absolute_move(pan=0.9)
        ptz_controller.relative_move(pan_delta=0.5)
        assert ptz_controller.state.pan == 1.0

    def test_relative_move_stops_continuous_movement(self, ptz_controller):
        ptz_controller.continuous_move(pan_speed=0.5)
        ptz_controller.relative_move(pan_delta=0.1)
        assert ptz_controller.velocity.pan_speed == 0.0


class TestPTZContinuousMove:
    """Tests for PTZController.continuous_move()"""

    def test_continuous_move_sets_velocity(self, ptz_controller):
        ptz_controller.continuous_move(pan_speed=0.5, tilt_speed=-0.3, zoom_speed=0.2)
        assert ptz_controller.velocity.pan_speed == 0.5
        assert ptz_controller.velocity.tilt_speed == -0.3
        assert ptz_controller.velocity.zoom_speed == 0.2

    def test_continuous_move_clamps_velocity(self, ptz_controller):
        ptz_controller.continuous_move(pan_speed=2.0, tilt_speed=-2.0, zoom_speed=2.0)
        assert ptz_controller.velocity.pan_speed == 1.0
        assert ptz_controller.velocity.tilt_speed == -1.0
        assert ptz_controller.velocity.zoom_speed == 1.0

    def test_stop_movement_stops_all(self, ptz_controller):
        ptz_controller.continuous_move(pan_speed=0.5, tilt_speed=0.5, zoom_speed=0.5)
        ptz_controller.stop_movement()
        assert ptz_controller.velocity.pan_speed == 0.0
        assert ptz_controller.velocity.tilt_speed == 0.0
        assert ptz_controller.velocity.zoom_speed == 0.0

    def test_stop_movement_pan_tilt_only(self, ptz_controller):
        ptz_controller.continuous_move(pan_speed=0.5, tilt_speed=0.5, zoom_speed=0.5)
        ptz_controller.stop_movement(pan_tilt=True, zoom=False)
        assert ptz_controller.velocity.pan_speed == 0.0
        assert ptz_controller.velocity.tilt_speed == 0.0
        assert ptz_controller.velocity.zoom_speed == 0.5

    def test_stop_movement_zoom_only(self, ptz_controller):
        ptz_controller.continuous_move(pan_speed=0.5, tilt_speed=0.5, zoom_speed=0.5)
        ptz_controller.stop_movement(pan_tilt=False, zoom=True)
        assert ptz_controller.velocity.pan_speed == 0.5
        assert ptz_controller.velocity.tilt_speed == 0.5
        assert ptz_controller.velocity.zoom_speed == 0.0


class TestPTZGoHome:
    """Tests for PTZController.goto_home()"""

    def test_goto_home_resets_position(self, ptz_controller):
        ptz_controller.absolute_move(pan=0.5, tilt=-0.3, zoom=0.7)
        ptz_controller.goto_home()
        assert ptz_controller.state.pan == 0.0
        assert ptz_controller.state.tilt == 0.0
        assert ptz_controller.state.zoom == 0.0

    def test_goto_home_sets_is_default(self, ptz_controller):
        ptz_controller.absolute_move(pan=0.5)
        assert ptz_controller._is_default is False
        ptz_controller.goto_home()
        assert ptz_controller._is_default is True


class TestPTZGetStatus:
    """Tests for PTZController.get_status()"""

    def test_get_status_returns_dict(self, ptz_controller):
        status = ptz_controller.get_status()
        assert isinstance(status, dict)
        assert 'pan' in status
        assert 'tilt' in status
        assert 'zoom' in status
        assert 'moving' in status

    def test_get_status_reflects_position(self, ptz_controller):
        ptz_controller.absolute_move(pan=0.5, tilt=-0.3, zoom=0.7)
        status = ptz_controller.get_status()
        assert status['pan'] == 0.5
        assert status['tilt'] == -0.3
        assert status['zoom'] == 0.7

    def test_get_status_moving_flag_idle(self, ptz_controller):
        status = ptz_controller.get_status()
        assert status['moving'] is False

    def test_get_status_moving_flag_active(self, ptz_controller):
        ptz_controller.continuous_move(pan_speed=0.5)
        status = ptz_controller.get_status()
        assert status['moving'] is True


class TestPTZPresets:
    """Tests for PTZ preset management"""

    def test_set_preset_creates_preset(self, ptz_controller):
        ptz_controller.absolute_move(pan=0.5, tilt=0.3, zoom=0.2)
        token = ptz_controller.set_preset("preset1", "Test Preset")

        assert token == "preset1"
        presets = ptz_controller.get_presets()
        assert "preset1" in presets
        assert presets["preset1"].name == "Test Preset"
        assert presets["preset1"].pan == 0.5

    def test_goto_preset_moves_to_position(self, ptz_controller):
        ptz_controller.absolute_move(pan=0.5, tilt=0.3, zoom=0.2)
        ptz_controller.set_preset("preset1", "Test Preset")

        ptz_controller.goto_home()
        assert ptz_controller.state.pan == 0.0

        result = ptz_controller.goto_preset("preset1")
        assert result is True
        assert ptz_controller.state.pan == 0.5
        assert ptz_controller.state.tilt == 0.3
        assert ptz_controller.state.zoom == 0.2

    def test_goto_preset_nonexistent_returns_false(self, ptz_controller):
        result = ptz_controller.goto_preset("nonexistent")
        assert result is False

    def test_remove_preset_deletes_preset(self, ptz_controller):
        ptz_controller.set_preset("preset1", "Test Preset")
        assert "preset1" in ptz_controller.get_presets()

        result = ptz_controller.remove_preset("preset1")
        assert result is True
        assert "preset1" not in ptz_controller.get_presets()

    def test_remove_preset_nonexistent_returns_false(self, ptz_controller):
        result = ptz_controller.remove_preset("nonexistent")
        assert result is False

    def test_get_presets_returns_copy(self, ptz_controller):
        presets1 = ptz_controller.get_presets()
        presets2 = ptz_controller.get_presets()
        assert presets1 is not presets2


class TestPTZApplyTransform:
    """Tests for PTZController.apply_ptz() frame transformation"""

    def test_apply_ptz_at_default_returns_unchanged(self, ptz_controller, sample_frame):
        result = ptz_controller.apply_ptz(sample_frame)
        # At default position, frame should be unchanged
        assert result is sample_frame

    def test_apply_ptz_with_zoom_returns_different(self, ptz_controller, sample_frame):
        ptz_controller.absolute_move(zoom=0.5)
        result = ptz_controller.apply_ptz(sample_frame)
        # With zoom, frame should be different (cropped and resized)
        assert result is not sample_frame
        assert result.shape == (ptz_controller.output_height, ptz_controller.output_width, 3)

    def test_apply_ptz_with_pan_returns_different(self, ptz_controller, sample_frame):
        ptz_controller.absolute_move(pan=0.5, zoom=0.3)  # Need some zoom to allow panning
        result = ptz_controller.apply_ptz(sample_frame)
        assert result is not sample_frame
        assert result.shape == (ptz_controller.output_height, ptz_controller.output_width, 3)

    def test_apply_ptz_disabled_returns_unchanged(self, ptz_controller_no_digital, sample_frame):
        ptz_controller_no_digital.absolute_move(zoom=0.5)
        result = ptz_controller_no_digital.apply_ptz(sample_frame)
        # Digital PTZ disabled, should return unchanged
        assert result is sample_frame

    def test_apply_ptz_output_dimensions(self, ptz_controller, sample_frame):
        ptz_controller.absolute_move(zoom=0.8)
        result = ptz_controller.apply_ptz(sample_frame)
        assert result.shape[0] == ptz_controller.output_height
        assert result.shape[1] == ptz_controller.output_width


class TestPTZHardwareHandler:
    """Tests for PTZ hardware handler integration"""

    def test_add_hardware_handler(self, ptz_controller, mock_hardware_handler):
        ptz_controller.add_hardware_handler(mock_hardware_handler)
        assert mock_hardware_handler in ptz_controller._hardware_handlers

    def test_add_duplicate_handler_ignored(self, ptz_controller, mock_hardware_handler):
        ptz_controller.add_hardware_handler(mock_hardware_handler)
        ptz_controller.add_hardware_handler(mock_hardware_handler)
        assert ptz_controller._hardware_handlers.count(mock_hardware_handler) == 1

    def test_remove_hardware_handler(self, ptz_controller, mock_hardware_handler):
        ptz_controller.add_hardware_handler(mock_hardware_handler)
        result = ptz_controller.remove_hardware_handler(mock_hardware_handler)
        assert result is True
        assert mock_hardware_handler not in ptz_controller._hardware_handlers

    def test_remove_nonexistent_handler_returns_false(self, ptz_controller, mock_hardware_handler):
        result = ptz_controller.remove_hardware_handler(mock_hardware_handler)
        assert result is False

    def test_continuous_move_notifies_handler(self, ptz_controller, mock_hardware_handler):
        ptz_controller.add_hardware_handler(mock_hardware_handler)
        ptz_controller.continuous_move(pan_speed=0.5, tilt_speed=-0.3, zoom_speed=0.2)
        mock_hardware_handler.on_continuous_move.assert_called_once_with(0.5, -0.3, 0.2)

    def test_stop_notifies_handler(self, ptz_controller, mock_hardware_handler):
        ptz_controller.add_hardware_handler(mock_hardware_handler)
        ptz_controller.stop_movement()
        mock_hardware_handler.on_stop.assert_called_once()

    def test_absolute_move_notifies_handler(self, ptz_controller, mock_hardware_handler):
        ptz_controller.add_hardware_handler(mock_hardware_handler)
        ptz_controller.absolute_move(pan=0.5, tilt=-0.3, zoom=0.2)
        mock_hardware_handler.on_absolute_move.assert_called_once_with(0.5, -0.3, 0.2)

    def test_relative_move_notifies_handler(self, ptz_controller, mock_hardware_handler):
        ptz_controller.add_hardware_handler(mock_hardware_handler)
        ptz_controller.relative_move(pan_delta=0.1, tilt_delta=-0.1, zoom_delta=0.1)
        mock_hardware_handler.on_relative_move.assert_called_once_with(0.1, -0.1, 0.1)

    def test_goto_home_notifies_handler(self, ptz_controller, mock_hardware_handler):
        ptz_controller.add_hardware_handler(mock_hardware_handler)
        ptz_controller.goto_home()
        mock_hardware_handler.on_goto_home.assert_called_once()

    def test_goto_preset_notifies_handler(self, ptz_controller, mock_hardware_handler):
        ptz_controller.add_hardware_handler(mock_hardware_handler)
        ptz_controller.set_preset("test", "Test Preset")
        ptz_controller.absolute_move(pan=0.5, tilt=0.3, zoom=0.2)
        ptz_controller.set_preset("test", "Test Preset")

        mock_hardware_handler.reset_mock()
        ptz_controller.goto_preset("test")
        mock_hardware_handler.on_goto_preset.assert_called_once()
