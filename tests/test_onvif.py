"""
Tests for ONVIFService
"""

import pytest
from unittest.mock import MagicMock, patch

from ipycam.onvif import ONVIFService
from ipycam.config import CameraConfig
from ipycam.ptz import PTZController


@pytest.fixture
def onvif_service(default_config):
    """Create an ONVIFService instance for testing"""
    return ONVIFService(default_config)


@pytest.fixture
def onvif_service_with_ptz(default_config, ptz_controller):
    """Create an ONVIFService instance with PTZ controller"""
    return ONVIFService(default_config, ptz_controller)


class TestONVIFServiceInitialization:
    """Tests for ONVIFService initialization"""

    def test_initialization(self, onvif_service):
        assert onvif_service.config is not None
        assert onvif_service.ptz is None
        assert onvif_service.device_uuid.startswith("urn:uuid:")

    def test_initialization_with_ptz(self, onvif_service_with_ptz, ptz_controller):
        assert onvif_service_with_ptz.ptz is ptz_controller

    def test_templates_loaded(self, onvif_service):
        assert len(onvif_service._templates) > 0
        assert 'envelope' in onvif_service._templates


class TestONVIFServiceHelpers:
    """Tests for ONVIF helper methods"""

    def test_bitrate_to_kbps_megabits(self, onvif_service):
        assert onvif_service._bitrate_to_kbps("4M") == 4000
        assert onvif_service._bitrate_to_kbps("8M") == 8000

    def test_bitrate_to_kbps_kilobits(self, onvif_service):
        assert onvif_service._bitrate_to_kbps("512K") == 512
        assert onvif_service._bitrate_to_kbps("1000K") == 1000

    def test_bitrate_to_kbps_numeric(self, onvif_service):
        assert onvif_service._bitrate_to_kbps("1000") == 1000

    def test_extract_xml_value(self, onvif_service):
        body = '<test:PresetName>MyPreset</test:PresetName>'
        result = onvif_service._extract_xml_value(body, 'PresetName')
        assert result == 'MyPreset'

    def test_extract_xml_value_no_namespace(self, onvif_service):
        body = '<PresetName>MyPreset</PresetName>'
        result = onvif_service._extract_xml_value(body, 'PresetName')
        assert result == 'MyPreset'

    def test_extract_xml_value_not_found(self, onvif_service):
        body = '<Other>Value</Other>'
        result = onvif_service._extract_xml_value(body, 'NotFound')
        assert result is None

    def test_extract_xml_attr(self, onvif_service):
        body = '<tptz:SetPreset PresetToken="preset1">'
        result = onvif_service._extract_xml_attr(body, 'SetPreset', 'PresetToken')
        assert result == 'preset1'

    def test_extract_velocity(self, onvif_service):
        body = '''
        <Velocity>
            <tt:PanTilt x="0.5" y="-0.3"/>
            <tt:Zoom x="0.2"/>
        </Velocity>
        '''
        pan, tilt, zoom = onvif_service._extract_velocity(body)
        assert pan == 0.5
        assert tilt == -0.3
        assert zoom == 0.2

    def test_extract_velocity_partial(self, onvif_service):
        body = '<Velocity><tt:PanTilt x="0.5" y="-0.3"/></Velocity>'
        pan, tilt, zoom = onvif_service._extract_velocity(body)
        assert pan == 0.5
        assert tilt == -0.3
        assert zoom == 0.0

    def test_extract_position(self, onvif_service):
        body = '''
        <Position>
            <tt:PanTilt x="0.5" y="-0.3"/>
            <tt:Zoom x="0.7"/>
        </Position>
        '''
        pan, tilt, zoom = onvif_service._extract_position(body)
        assert pan == 0.5
        assert tilt == -0.3
        assert zoom == 0.7

    def test_extract_translation(self, onvif_service):
        body = '''
        <Translation>
            <tt:PanTilt x="0.1" y="-0.1"/>
            <tt:Zoom x="0.05"/>
        </Translation>
        '''
        pan, tilt, zoom = onvif_service._extract_translation(body)
        assert pan == 0.1
        assert tilt == -0.1
        assert zoom == 0.05


class TestONVIFServiceDeviceInfo:
    """Tests for ONVIF device information methods"""

    def test_get_device_information(self, onvif_service, default_config):
        result = onvif_service.get_device_information()
        assert isinstance(result, str)
        assert default_config.manufacturer in result
        assert default_config.model in result
        assert default_config.serial_number in result

    def test_get_system_date_time(self, onvif_service):
        result = onvif_service.get_system_date_time()
        assert isinstance(result, str)
        # Should contain SOAP envelope
        assert 'Envelope' in result

    def test_get_capabilities(self, onvif_service, default_config):
        result = onvif_service.get_capabilities()
        assert isinstance(result, str)
        assert str(default_config.onvif_port) in result

    def test_get_services(self, onvif_service, default_config):
        result = onvif_service.get_services()
        assert isinstance(result, str)
        assert 'device_service' in result
        assert 'media_service' in result
        assert 'ptz_service' in result

    def test_get_scopes(self, onvif_service, default_config):
        result = onvif_service.get_scopes()
        assert isinstance(result, str)
        assert default_config.name in result

    def test_get_users(self, onvif_service):
        result = onvif_service.get_users()
        assert isinstance(result, str)
        assert 'Envelope' in result


class TestONVIFServiceMediaInfo:
    """Tests for ONVIF media information methods"""

    def test_get_profiles(self, onvif_service, default_config):
        result = onvif_service.get_profiles()
        assert isinstance(result, str)
        assert str(default_config.main_width) in result
        assert str(default_config.main_height) in result

    def test_get_stream_uri_main(self, onvif_service, default_config):
        body = '<ProfileToken>Profile_1</ProfileToken>'
        result = onvif_service.get_stream_uri(body)
        assert isinstance(result, str)
        assert default_config.main_stream_name in result

    def test_get_stream_uri_sub(self, onvif_service, default_config):
        body = '<ProfileToken>Profile_2</ProfileToken>'
        result = onvif_service.get_stream_uri(body)
        assert isinstance(result, str)
        assert default_config.sub_stream_name in result

    def test_get_snapshot_uri(self, onvif_service, default_config):
        result = onvif_service.get_snapshot_uri('')
        assert isinstance(result, str)
        assert default_config.snapshot_url in result

    def test_get_video_encoder_configuration(self, onvif_service, default_config):
        result = onvif_service.get_video_encoder_configuration()
        assert isinstance(result, str)
        assert str(default_config.main_width) in result
        assert str(default_config.main_fps) in result

    def test_get_video_source_configuration(self, onvif_service, default_config):
        result = onvif_service.get_video_source_configuration()
        assert isinstance(result, str)
        assert str(default_config.main_width) in result

    def test_get_audio_decoder_configurations(self, onvif_service):
        result = onvif_service.get_audio_decoder_configurations()
        assert isinstance(result, str)


class TestONVIFServicePTZ:
    """Tests for ONVIF PTZ service methods"""

    def test_ptz_get_nodes(self, onvif_service_with_ptz):
        result = onvif_service_with_ptz.ptz_get_nodes()
        assert isinstance(result, str)
        assert 'Envelope' in result

    def test_ptz_get_node(self, onvif_service_with_ptz):
        result = onvif_service_with_ptz.ptz_get_node()
        assert isinstance(result, str)

    def test_ptz_get_configurations(self, onvif_service_with_ptz):
        result = onvif_service_with_ptz.ptz_get_configurations()
        assert isinstance(result, str)

    def test_ptz_get_service_capabilities(self, onvif_service_with_ptz):
        result = onvif_service_with_ptz.ptz_get_service_capabilities()
        assert isinstance(result, str)

    def test_ptz_get_status(self, onvif_service_with_ptz, ptz_controller):
        result = onvif_service_with_ptz.ptz_get_status('')
        assert isinstance(result, str)
        assert 'GetStatusResponse' in result
        assert 'PTZStatus' in result
        # Check pan/tilt values are rendered
        assert 'x="0.0"' in result

    def test_ptz_get_status_no_controller(self, onvif_service):
        result = onvif_service.ptz_get_status('')
        assert isinstance(result, str)
        assert 'GetStatusResponse' in result
        assert 'PTZStatus' in result

    def test_ptz_continuous_move(self, onvif_service_with_ptz, ptz_controller):
        body = '''
        <Velocity>
            <tt:PanTilt x="0.5" y="-0.3"/>
            <tt:Zoom x="0.2"/>
        </Velocity>
        '''
        result = onvif_service_with_ptz.ptz_continuous_move(body)
        assert isinstance(result, str)
        assert ptz_controller.velocity.pan_speed == 0.5
        assert ptz_controller.velocity.tilt_speed == -0.3
        assert ptz_controller.velocity.zoom_speed == 0.2

    def test_ptz_stop(self, onvif_service_with_ptz, ptz_controller):
        ptz_controller.continuous_move(pan_speed=0.5, tilt_speed=0.5)
        body = '<PanTilt>true</PanTilt><Zoom>true</Zoom>'
        result = onvif_service_with_ptz.ptz_stop(body)
        assert isinstance(result, str)
        assert ptz_controller.velocity.pan_speed == 0.0

    def test_ptz_stop_pan_tilt_only(self, onvif_service_with_ptz, ptz_controller):
        ptz_controller.continuous_move(pan_speed=0.5, tilt_speed=0.5, zoom_speed=0.5)
        body = '<PanTilt>true</PanTilt><Zoom>false</Zoom>'
        result = onvif_service_with_ptz.ptz_stop(body)
        assert ptz_controller.velocity.pan_speed == 0.0
        assert ptz_controller.velocity.zoom_speed == 0.5

    def test_ptz_absolute_move(self, onvif_service_with_ptz, ptz_controller):
        body = '''
        <Position>
            <tt:PanTilt x="0.5" y="-0.3"/>
            <tt:Zoom x="0.7"/>
        </Position>
        '''
        result = onvif_service_with_ptz.ptz_absolute_move(body)
        assert isinstance(result, str)
        assert ptz_controller.state.pan == 0.5
        assert ptz_controller.state.tilt == -0.3
        assert ptz_controller.state.zoom == 0.7

    def test_ptz_relative_move(self, onvif_service_with_ptz, ptz_controller):
        body = '''
        <Translation>
            <tt:PanTilt x="0.1" y="-0.1"/>
            <tt:Zoom x="0.05"/>
        </Translation>
        '''
        result = onvif_service_with_ptz.ptz_relative_move(body)
        assert isinstance(result, str)
        assert ptz_controller.state.pan == 0.1
        assert ptz_controller.state.tilt == -0.1
        assert ptz_controller.state.zoom == 0.05

    def test_ptz_goto_home(self, onvif_service_with_ptz, ptz_controller):
        ptz_controller.absolute_move(pan=0.5, tilt=0.3, zoom=0.7)
        result = onvif_service_with_ptz.ptz_goto_home('')
        assert isinstance(result, str)
        assert ptz_controller.state.pan == 0.0
        assert ptz_controller.state.tilt == 0.0
        assert ptz_controller.state.zoom == 0.0

    def test_ptz_get_presets(self, onvif_service_with_ptz, ptz_controller):
        ptz_controller.set_preset("test", "Test Preset")
        result = onvif_service_with_ptz.ptz_get_presets('')
        assert isinstance(result, str)
        assert 'test' in result
        assert 'Test Preset' in result

    def test_ptz_set_preset(self, onvif_service_with_ptz, ptz_controller):
        ptz_controller.absolute_move(pan=0.5, tilt=0.3, zoom=0.2)
        body = '<PresetName>New Preset</PresetName>'
        result = onvif_service_with_ptz.ptz_set_preset(body)
        assert isinstance(result, str)
        assert 'preset_' in result  # Auto-generated token

    def test_ptz_goto_preset(self, onvif_service_with_ptz, ptz_controller):
        ptz_controller.absolute_move(pan=0.5, tilt=0.3, zoom=0.2)
        ptz_controller.set_preset("mypreset", "My Preset")
        ptz_controller.goto_home()

        body = '<PresetToken>mypreset</PresetToken>'
        result = onvif_service_with_ptz.ptz_goto_preset(body)
        assert isinstance(result, str)
        assert ptz_controller.state.pan == 0.5


class TestONVIFServiceActionHandler:
    """Tests for ONVIF handle_action routing"""

    def test_handle_action_device_information(self, onvif_service):
        result = onvif_service.handle_action('GetDeviceInformation', '')
        assert result is not None
        assert 'Manufacturer' in result or 'manufacturer' in result.lower()

    def test_handle_action_system_date_time(self, onvif_service):
        result = onvif_service.handle_action('GetSystemDateAndTime', '')
        assert result is not None

    def test_handle_action_capabilities(self, onvif_service):
        result = onvif_service.handle_action('GetCapabilities', '')
        assert result is not None

    def test_handle_action_profiles(self, onvif_service):
        result = onvif_service.handle_action('GetProfiles', '')
        assert result is not None

    def test_handle_action_stream_uri(self, onvif_service):
        result = onvif_service.handle_action('GetStreamUri', '<ProfileToken>Profile_1</ProfileToken>')
        assert result is not None

    def test_handle_action_ptz_get_status(self, onvif_service_with_ptz):
        result = onvif_service_with_ptz.handle_action('GetStatus', '')
        assert result is not None

    def test_handle_action_ptz_continuous_move(self, onvif_service_with_ptz):
        body = '<Velocity><tt:PanTilt x="0.5" y="0.0"/></Velocity>'
        result = onvif_service_with_ptz.handle_action('ContinuousMove', body)
        assert result is not None

    def test_handle_action_unsupported(self, onvif_service):
        result = onvif_service.handle_action('UnsupportedAction', '')
        assert result is not None
        assert 'not supported' in result.lower() or 'fault' in result.lower()


class TestONVIFServiceDiscovery:
    """Tests for ONVIF WS-Discovery methods"""

    def test_create_probe_match(self, onvif_service, default_config):
        relates_to = "urn:uuid:test-12345"
        result = onvif_service.create_probe_match(relates_to)
        assert isinstance(result, str)
        assert relates_to in result
        assert onvif_service.device_uuid in result
        assert default_config.name in result


class TestONVIFServiceFault:
    """Tests for ONVIF fault responses"""

    def test_fault_returns_xml(self, onvif_service):
        result = onvif_service.fault("Test error message")
        assert isinstance(result, str)
        assert "Test error message" in result
