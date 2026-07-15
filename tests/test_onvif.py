"""
Tests for ONVIFService
"""

import base64
from datetime import datetime, timezone

import pytest
from unittest.mock import MagicMock, patch

from ipycam.onvif import (
    ONVIFService,
    verify_ws_username_token,
    compute_password_digest,
    _created_within_skew,
)
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

    def test_extract_velocity_non_numeric_values_are_ignored(self, onvif_service):
        """Malformed x/y attributes must not raise -- ValueError is caught and
        the corresponding component stays at its default."""
        body = '''
        <Velocity>
            <tt:PanTilt x="not-a-number" y="also-bad"/>
            <tt:Zoom x="still-bad"/>
        </Velocity>
        '''
        pan, tilt, zoom = onvif_service._extract_velocity(body)
        assert (pan, tilt, zoom) == (0.0, 0.0, 0.0)

    def test_extract_position_non_numeric_values_are_ignored(self, onvif_service):
        body = '''
        <Position>
            <tt:PanTilt x="bad" y="bad"/>
            <tt:Zoom x="bad"/>
        </Position>
        '''
        pan, tilt, zoom = onvif_service._extract_position(body)
        assert (pan, tilt, zoom) == (None, None, None)

    def test_extract_translation_non_numeric_values_are_ignored(self, onvif_service):
        body = '''
        <Translation>
            <tt:PanTilt x="bad" y="bad"/>
            <tt:Zoom x="bad"/>
        </Translation>
        '''
        pan, tilt, zoom = onvif_service._extract_translation(body)
        assert (pan, tilt, zoom) == (0.0, 0.0, 0.0)


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
        # The snapshot is served by the main HTTP server on onvif_port; the URI
        # must advertise that port, NOT the unbound web_port.
        assert f":{default_config.onvif_port}/" in result
        assert f":{default_config.web_port}/" not in result

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

    def test_ptz_stop_zoom_only(self, onvif_service_with_ptz, ptz_controller):
        """PanTilt=false must leave pan/tilt velocity untouched while zoom
        still stops -- the mirror-image case of test_ptz_stop_pan_tilt_only."""
        ptz_controller.continuous_move(pan_speed=0.5, tilt_speed=0.5, zoom_speed=0.5)
        body = '<PanTilt>false</PanTilt><Zoom>true</Zoom>'
        result = onvif_service_with_ptz.ptz_stop(body)
        assert isinstance(result, str)
        assert ptz_controller.velocity.pan_speed == 0.5
        assert ptz_controller.velocity.tilt_speed == 0.5
        assert ptz_controller.velocity.zoom_speed == 0.0

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


def _now_created() -> str:
    """A fresh WS-Security Created timestamp (within the skew window)."""
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def _soap_with_token(username, password, nonce_b64=None, created=None,
                     pw_type="PasswordDigest", digest=None):
    """Build a SOAP envelope carrying a WS-Security UsernameToken."""
    if nonce_b64 is None:
        nonce_b64 = base64.b64encode(b'0123456789abcdef').decode()
    if created is None:
        created = _now_created()
    if digest is None:
        if pw_type == "PasswordText":
            digest = password
        else:
            digest = compute_password_digest(nonce_b64, created, password)
    return f'''<?xml version="1.0"?>
<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope">
  <s:Header>
    <wsse:Security xmlns:wsse="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd">
      <wsse:UsernameToken>
        <wsse:Username>{username}</wsse:Username>
        <wsse:Password Type="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-username-token-profile-1.0#{pw_type}">{digest}</wsse:Password>
        <wsse:Nonce>{nonce_b64}</wsse:Nonce>
        <wsu:Created xmlns:wsu="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-utility-1.0.xsd">{created}</wsu:Created>
      </wsse:UsernameToken>
    </wsse:Security>
  </s:Header>
  <s:Body></s:Body>
</s:Envelope>'''


class TestPasswordDigestMath:
    """Verify the PasswordDigest formula against a hand-computed value."""

    def test_known_digest_value(self):
        # nonce=b'0123456789abcdef', created='2010-01-01T00:00:00Z',
        # password='s3cr3t' -> precomputed with base64(sha1(nonce+created+pw)).
        nonce_b64 = base64.b64encode(b'0123456789abcdef').decode()
        assert nonce_b64 == 'MDEyMzQ1Njc4OWFiY2RlZg=='
        digest = compute_password_digest(nonce_b64, '2010-01-01T00:00:00Z', 's3cr3t')
        assert digest == 'woRgYgbhRgKqEYg3FMd9Qlsp1wg='


class TestVerifyWsUsernameToken:
    """Tests for the pure WS-Security UsernameToken verifier."""

    def test_valid_digest_passes(self):
        body = _soap_with_token("admin", "s3cr3t")
        assert verify_ws_username_token(body, "admin", "s3cr3t") is True

    def test_wrong_password_fails(self):
        body = _soap_with_token("admin", "s3cr3t")
        assert verify_ws_username_token(body, "admin", "different") is False

    def test_wrong_username_fails(self):
        body = _soap_with_token("admin", "s3cr3t")
        assert verify_ws_username_token(body, "root", "s3cr3t") is False

    def test_missing_token_fails(self):
        body = '<s:Envelope><s:Body/></s:Envelope>'
        assert verify_ws_username_token(body, "admin", "s3cr3t") is False

    def test_password_text_fallback(self):
        body = _soap_with_token("admin", "plainpw", pw_type="PasswordText")
        assert verify_ws_username_token(body, "admin", "plainpw") is True

    def test_password_text_wrong_fails(self):
        body = _soap_with_token("admin", "plainpw", pw_type="PasswordText")
        assert verify_ws_username_token(body, "admin", "other") is False

    def test_stale_created_rejected(self):
        # Digest is valid for the old Created, but the timestamp is far outside
        # the allowed skew window, so verification must fail.
        old = '2000-01-01T00:00:00Z'
        body = _soap_with_token("admin", "s3cr3t", created=old)
        assert verify_ws_username_token(body, "admin", "s3cr3t") is False

    def test_ns_tolerant_extraction(self):
        # Different namespace prefixes must still be parsed.
        body = _soap_with_token("admin", "s3cr3t").replace("wsse:", "sec:")
        assert verify_ws_username_token(body, "admin", "s3cr3t") is True

    def test_digest_type_missing_nonce_fails(self):
        """PasswordDigest verification requires both Nonce and Created; a
        token missing the Nonce element must fail closed, not raise."""
        body = '''<?xml version="1.0"?>
<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope">
  <s:Header>
    <wsse:Security xmlns:wsse="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd">
      <wsse:UsernameToken>
        <wsse:Username>admin</wsse:Username>
        <wsse:Password>somedigest==</wsse:Password>
        <wsu:Created xmlns:wsu="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-utility-1.0.xsd">2024-01-01T00:00:00Z</wsu:Created>
      </wsse:UsernameToken>
    </wsse:Security>
  </s:Header>
  <s:Body></s:Body>
</s:Envelope>'''
        assert verify_ws_username_token(body, "admin", "s3cr3t") is False

    def test_digest_type_missing_created_fails(self):
        body = '''<?xml version="1.0"?>
<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope">
  <s:Header>
    <wsse:Security xmlns:wsse="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd">
      <wsse:UsernameToken>
        <wsse:Username>admin</wsse:Username>
        <wsse:Password>somedigest==</wsse:Password>
        <wsse:Nonce>MDEyMzQ1Njc4OWFiY2RlZg==</wsse:Nonce>
      </wsse:UsernameToken>
    </wsse:Security>
  </s:Header>
  <s:Body></s:Body>
</s:Envelope>'''
        assert verify_ws_username_token(body, "admin", "s3cr3t") is False

    def test_undecodable_nonce_fails_instead_of_raising(self):
        """compute_password_digest() base64-decodes the Nonce; a corrupt value
        must be caught and treated as a verification failure, not an
        unhandled exception escaping verify_ws_username_token(). A literal
        ``digest`` placeholder is passed so the test helper itself doesn't
        try (and fail) to compute the expected digest from the bad nonce."""
        body = _soap_with_token(
            "admin", "s3cr3t", nonce_b64="not-valid-base64!!!", digest="placeholder=="
        )
        assert verify_ws_username_token(body, "admin", "s3cr3t") is False


class TestCreatedWithinSkew:
    """Tests for the pure _created_within_skew() timestamp-freshness helper."""

    def test_naive_timestamp_without_z_suffix_is_treated_as_utc(self):
        naive_now = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
        assert _created_within_skew(naive_now) is True

    def test_unparseable_timestamp_defaults_to_true(self):
        # Best-effort: a garbage Created value must not be treated as a
        # freshness failure (the digest/nonce match is still required).
        assert _created_within_skew("not-a-timestamp-at-all") is True


class TestONVIFServiceAuth:
    """Tests for ONVIFService.verify_usernametoken (config-aware wrapper)."""

    def test_auth_disabled_returns_true_regardless(self, onvif_service):
        # default_config has empty credentials -> auth disabled.
        assert onvif_service.config.auth_enabled is False
        assert onvif_service.verify_usernametoken('<no/token/>') is True

    def test_auth_enabled_requires_valid_token(self):
        config = CameraConfig(username="admin", password="s3cr3t")
        service = ONVIFService(config)
        good = _soap_with_token("admin", "s3cr3t")
        bad = _soap_with_token("admin", "wrong")
        assert service.verify_usernametoken(good) is True
        assert service.verify_usernametoken(bad) is False
        assert service.verify_usernametoken('<no/token/>') is False

    def test_get_users_reflects_configured_username(self):
        config = CameraConfig(username="operator", password="pw")
        service = ONVIFService(config)
        result = service.get_users()
        assert 'operator' in result
        assert '<tt:Username>admin</tt:Username>' not in result

    def test_get_users_open_mode_keeps_admin(self, onvif_service):
        result = onvif_service.get_users()
        assert 'admin' in result

    def test_get_users_escapes_username(self):
        config = CameraConfig(username='a<b>&"c', password="pw")
        service = ONVIFService(config)
        result = service.get_users()
        # Raw special characters must be escaped so the SOAP stays well-formed.
        assert '<tt:Username>a<b>' not in result
        assert '&lt;b&gt;' in result


class TestONVIFXMLEscaping:
    """Dynamic values substituted into SOAP templates must be XML-escaped."""

    def test_get_presets_escapes_name_and_token(self, onvif_service_with_ptz,
                                                ptz_controller):
        from ipycam.ptz import PTZPreset

        # Inject directly (set_preset would persist to ptz_presets.json).
        ptz_controller.presets['ptok'] = PTZPreset(
            token='tok<&>"x', name='Bad <name> & stuff',
            pan=0.0, tilt=0.0, zoom=0.0,
        )

        result = onvif_service_with_ptz.ptz_get_presets('')

        # Element text: < > & escaped, raw markup absent.
        assert 'Bad &lt;name&gt; &amp; stuff' in result
        assert '<name>' not in result
        # Attribute value: quotes escaped too, so the attribute cannot be
        # broken out of.
        assert 'token="tok&lt;&amp;&gt;&quot;x"' in result
        assert 'tok<&>"x' not in result

    def test_get_presets_stays_well_formed_xml(self, onvif_service_with_ptz,
                                               ptz_controller):
        import xml.etree.ElementTree as ET

        from ipycam.ptz import PTZPreset

        ptz_controller.presets['p1'] = PTZPreset(
            token='t<1>&"', name='Name <with> & "specials"',
            pan=0.1, tilt=0.2, zoom=0.3,
        )

        result = onvif_service_with_ptz.ptz_get_presets('')

        # The envelope template does not declare the tptz/ptz prefixes, so add
        # dummy declarations purely to let ElementTree validate well-formedness.
        parseable = result.replace(
            '<s:Envelope ',
            '<s:Envelope xmlns:tptz="urn:x-test:tptz" xmlns:ptz="urn:x-test:ptz" ',
            1,
        )
        root = ET.fromstring(parseable)  # raises ParseError if not well-formed

        # Round-trip: the parsed Name text equals the original preset name.
        names = [
            el.text for el in root.iter()
            if el.tag.endswith('}Name') or el.tag == 'Name'
        ]
        assert 'Name <with> & "specials"' in names

    def test_fault_reason_is_escaped(self, onvif_service):
        result = onvif_service.fault('<evil>&payload')
        assert '<evil>' not in result
        assert '&lt;evil&gt;&amp;payload' in result

    def test_handle_action_unknown_action_is_escaped(self, onvif_service):
        # The unknown action name is echoed into the fault reason.
        result = onvif_service.handle_action('<injected/>', '')
        assert '<injected/>' not in result
        assert '&lt;injected/&gt;' in result

    def test_get_device_information_escapes_config_values(self):
        config = CameraConfig(manufacturer='Acme<&>Co', model='M</tds:Model><x>')
        service = ONVIFService(config)
        result = service.get_device_information()
        assert 'Acme&lt;&amp;&gt;Co' in result
        assert 'Acme<&>Co' not in result
        assert '</tds:Model><x>' not in result

    def test_get_scopes_escapes_camera_name(self):
        config = CameraConfig(name='Cam<1>&2')
        service = ONVIFService(config)
        result = service.get_scopes()
        assert 'Cam&lt;1&gt;&amp;2' in result
        assert 'Cam<1>' not in result

    def test_create_probe_match_escapes_camera_name_and_relates_to(self):
        config = CameraConfig(name='Cam<x>&y')
        service = ONVIFService(config)
        result = service.create_probe_match('urn:uuid:1</wsa:RelatesTo><evil/>')
        assert 'Cam&lt;x&gt;&amp;y' in result
        assert 'Cam<x>' not in result
        assert '<evil/>' not in result

    def test_get_users_username_not_double_escaped(self):
        # get_users escapes at the call site; _render must not add a second
        # round of escaping on top of it.
        config = CameraConfig(username='a&b', password='pw')
        service = ONVIFService(config)
        result = service.get_users()
        assert 'a&amp;b' in result
        assert 'a&amp;amp;b' not in result
