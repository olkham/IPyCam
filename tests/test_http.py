"""
Tests for IPCameraHTTPHandler.serve_static path-traversal protection.

serve_static is exercised in isolation. The handler is an
http.server.BaseHTTPRequestHandler subclass whose __init__ immediately tries
to service a socket, so we build an instance with __new__ (bypassing __init__)
and stub out the response-writing helpers with MagicMocks -- matching the
MagicMock style used in tests/conftest.py and tests/test_mjpeg.py.
"""

import base64
import json
import os
from email.message import Message
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from ipycam.config import CameraConfig
from ipycam.http import IPCameraHTTPHandler, MAX_UPLOAD_BYTES


# Canonical absolute path to the package's static directory.
STATIC_DIR = os.path.realpath(
    os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        'ipycam', 'static'
    )
)


def make_handler():
    """Build a handler without running BaseHTTPRequestHandler.__init__."""
    handler = IPCameraHTTPHandler.__new__(IPCameraHTTPHandler)
    handler.camera = MagicMock()
    handler.send_error = MagicMock()
    handler.send_response = MagicMock()
    handler.send_header = MagicMock()
    handler.end_headers = MagicMock()
    handler.wfile = MagicMock()
    return handler


def response_status(handler):
    """Numeric status passed to send_response, or None if not called."""
    if handler.send_response.called:
        return handler.send_response.call_args[0][0]
    return None


def error_status(handler):
    """Numeric status passed to send_error, or None if not called."""
    if handler.send_error.called:
        return handler.send_error.call_args[0][0]
    return None


# Each of these must be refused without serving any file outside static_dir.
BLOCKED_PATHS = [
    '/static//etc/passwd',                     # absolute path escapes via os.path.join
    '/static/../config.py',                    # classic parent-dir traversal
    '/static/%2e%2e/%2e%2e/config.py',         # URL-encoded traversal
    '/static/C:/Windows/win.ini',              # Windows drive-letter escape
    r'/static/\\server\share\x',               # Windows UNC path escape
    '/static/js/../../config.py',              # traversal that starts inside static
]


@pytest.mark.parametrize('path', BLOCKED_PATHS)
def test_blocked_paths_are_refused(path):
    """Traversal / absolute / drive / UNC requests are refused (403 or 404)."""
    handler = make_handler()
    with patch('builtins.open', MagicMock()) as mock_open:
        handler.serve_static(path)

    # Refused, never served successfully.
    assert error_status(handler) in (403, 404), (
        f"{path!r} should be blocked, got error={error_status(handler)} "
        f"response={response_status(handler)}"
    )
    assert response_status(handler) != 200
    # And crucially: no file outside static_dir was ever opened.
    mock_open.assert_not_called()
    handler.wfile.write.assert_not_called()


def test_encoded_traversal_does_not_serve_config():
    """The URL-encoded traversal must not leak config.py contents."""
    handler = make_handler()
    handler.serve_static('/static/%2e%2e/%2e%2e/config.py')
    assert response_status(handler) != 200
    handler.wfile.write.assert_not_called()


def test_nonexistent_safe_path_returns_404():
    """A safe, in-directory path that does not exist yields 404 (not 403)."""
    handler = make_handler()
    with patch('builtins.open', MagicMock()) as mock_open:
        handler.serve_static('/static/does_not_exist.css')
    assert error_status(handler) == 404
    assert response_status(handler) != 200
    mock_open.assert_not_called()


def test_legitimate_static_file_is_served():
    """A real file inside static_dir is served with 200 and its bytes."""
    real_file = os.path.join(STATIC_DIR, 'js', 'app.js')
    assert os.path.isfile(real_file), "test fixture js/app.js must exist"
    with open(real_file, 'rb') as f:
        expected = f.read()

    handler = make_handler()
    handler.serve_static('/static/js/app.js')

    assert response_status(handler) == 200
    handler.send_error.assert_not_called()
    handler.wfile.write.assert_called_once_with(expected)

    # Correct content-type header for a .js file.
    header_calls = {c.args[0]: c.args[1] for c in handler.send_header.call_args_list}
    assert header_calls.get('Content-Type') == 'application/javascript'


# ---------------------------------------------------------------------------
# HTTP Basic auth guard (_check_basic_auth)
# ---------------------------------------------------------------------------


def make_auth_handler(username="", password="", auth_header=None):
    """Build a handler with a config carrying the given credentials.

    ``self.headers`` is a real email.message.Message (what BaseHTTPRequestHandler
    uses), optionally carrying an Authorization header.
    """
    handler = make_handler()
    handler.camera.config.username = username
    handler.camera.config.password = password
    # auth_enabled mirrors CameraConfig.auth_enabled semantics.
    handler.camera.config.auth_enabled = bool(username) and bool(password)

    headers = Message()
    if auth_header is not None:
        headers['Authorization'] = auth_header
    handler.headers = headers
    return handler


def basic_header(user, pw):
    token = base64.b64encode(f"{user}:{pw}".encode('utf-8')).decode('ascii')
    return f"Basic {token}"


def header_values(handler, name):
    return [c.args[1] for c in handler.send_header.call_args_list if c.args[0] == name]


def test_check_basic_auth_disabled_passes_without_header():
    """Open mode (empty creds): guard is a no-op, returns True, sends nothing."""
    handler = make_auth_handler()  # no creds
    assert handler._check_basic_auth() is True
    handler.send_response.assert_not_called()


def test_check_basic_auth_missing_header_returns_401():
    handler = make_auth_handler("admin", "pw")
    assert handler._check_basic_auth() is False
    assert response_status(handler) == 401
    # Challenge header present.
    assert 'Basic realm="IPyCam"' in header_values(handler, 'WWW-Authenticate')


def test_check_basic_auth_correct_credentials_pass():
    handler = make_auth_handler("admin", "pw", basic_header("admin", "pw"))
    assert handler._check_basic_auth() is True
    handler.send_response.assert_not_called()


def test_check_basic_auth_wrong_password_returns_401():
    handler = make_auth_handler("admin", "pw", basic_header("admin", "nope"))
    assert handler._check_basic_auth() is False
    assert response_status(handler) == 401


def test_check_basic_auth_wrong_username_returns_401():
    handler = make_auth_handler("admin", "pw", basic_header("root", "pw"))
    assert handler._check_basic_auth() is False
    assert response_status(handler) == 401


def test_check_basic_auth_malformed_header_returns_401():
    handler = make_auth_handler("admin", "pw", "Basic not-valid-base64!!")
    assert handler._check_basic_auth() is False
    assert response_status(handler) == 401


def test_check_basic_auth_password_with_colon():
    """Passwords may contain ':' -- only the first ':' splits user from pass."""
    handler = make_auth_handler("admin", "a:b:c", basic_header("admin", "a:b:c"))
    assert handler._check_basic_auth() is True


def test_do_get_protected_route_guarded_when_auth_enabled():
    """A protected GET route returns 401 (no handler invoked) without creds."""
    handler = make_auth_handler("admin", "pw")
    handler.path = '/api/config'
    handler.serve_config = MagicMock()
    handler.do_GET()
    assert response_status(handler) == 401
    handler.serve_config.assert_not_called()


def test_do_get_static_open_even_when_auth_enabled():
    """Static assets stay public even with auth configured (no 401)."""
    handler = make_auth_handler("admin", "pw")
    handler.path = '/static/js/app.js'
    handler.serve_static = MagicMock()
    handler.do_GET()
    handler.serve_static.assert_called_once_with('/static/js/app.js')


def test_do_post_protected_route_guarded_when_auth_enabled():
    handler = make_auth_handler("admin", "pw")
    handler.path = '/api/ptz'
    handler.update_ptz = MagicMock()
    handler.do_POST()
    assert response_status(handler) == 401
    handler.update_ptz.assert_not_called()


def test_do_get_proceeds_with_valid_credentials():
    handler = make_auth_handler("admin", "pw", basic_header("admin", "pw"))
    handler.path = '/api/config'
    handler.serve_config = MagicMock()
    handler.do_GET()
    handler.serve_config.assert_called_once()
    # No 401 was emitted.
    assert 401 not in [c.args[0] for c in handler.send_response.call_args_list]


# ---------------------------------------------------------------------------
# do_GET / do_POST routing (each path -> the right handler method, in an
# otherwise-open / auth-disabled handler so the dispatch logic itself is
# isolated from the auth guard tested above).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path,method_name", [
    ('/', 'serve_web_ui'),
    ('/index.html', 'serve_web_ui'),
    ('/api/config', 'serve_config'),
    ('/api/stats', 'serve_stats'),
    ('/api/ptz', 'serve_ptz_status'),
    ('/api/recording/status', 'serve_recording_status'),
    ('/api/video/status', 'serve_video_status'),
])
def test_do_get_dispatches_each_route(path, method_name):
    handler = make_auth_handler()
    handler.path = path
    setattr(handler, method_name, MagicMock())
    handler.do_GET()
    getattr(handler, method_name).assert_called_once()


def test_do_get_dispatches_snapshot_route():
    handler = make_auth_handler()
    handler.camera.config.snapshot_url = 'snapshot.jpg'
    handler.path = '/snapshot.jpg'
    handler.serve_snapshot = MagicMock()
    handler.do_GET()
    handler.serve_snapshot.assert_called_once()


def test_do_get_dispatches_mjpeg_route():
    handler = make_auth_handler()
    handler.camera.config.mjpeg_url = 'stream.mjpeg'
    handler.path = '/stream.mjpeg'
    handler.serve_mjpeg_stream = MagicMock()
    handler.do_GET()
    handler.serve_mjpeg_stream.assert_called_once()


def test_do_get_unknown_path_returns_404():
    handler = make_auth_handler()
    handler.path = '/this/route/does/not/exist'
    handler.do_GET()
    assert error_status(handler) == 404


@pytest.mark.parametrize("path,method_name", [
    ('/api/config', 'update_config'),
    ('/api/credentials', 'update_credentials'),
    ('/api/ptz', 'update_ptz'),
    ('/api/restart', 'restart_stream'),
    ('/api/recording/start', 'start_recording'),
    ('/api/recording/stop', 'stop_recording'),
    ('/api/webrtc/offer', 'handle_webrtc_offer'),
    ('/api/webrtc/close', 'handle_webrtc_close'),
    ('/api/video/upload', 'handle_video_upload'),
])
def test_do_post_dispatches_each_route(path, method_name):
    handler = make_auth_handler()
    handler.path = path
    setattr(handler, method_name, MagicMock())
    handler.do_POST()
    getattr(handler, method_name).assert_called_once()


def test_do_post_unknown_path_returns_404():
    handler = make_auth_handler()
    handler.path = '/this/route/does/not/exist'
    handler.do_POST()
    assert error_status(handler) == 404


def test_do_post_onvif_prefix_bypasses_basic_auth():
    """/onvif/* is authenticated via WS-Security inside handle_onvif, not the
    HTTP Basic-auth guard -- it must dispatch even with auth enabled and no
    Authorization header."""
    handler = make_auth_handler("admin", "pw")  # auth enabled, no header set
    handler.path = '/onvif/device_service'
    handler.handle_onvif = MagicMock()
    handler.do_POST()
    handler.handle_onvif.assert_called_once()
    assert response_status(handler) != 401


# ---------------------------------------------------------------------------
# serve_snapshot (thread-safe frame access via the camera getter)
# ---------------------------------------------------------------------------


def test_serve_snapshot_no_frame_returns_503():
    """No frame available -> 503, and it goes through the safe getter."""
    handler = make_handler()
    handler.camera.get_snapshot_frame = MagicMock(return_value=None)
    handler.serve_snapshot()
    handler.camera.get_snapshot_frame.assert_called_once()
    assert error_status(handler) == 503


def test_serve_snapshot_uses_getter_and_encodes_quality_90():
    """serve_snapshot reads via get_snapshot_frame() and encodes at quality 90."""
    import cv2

    handler = make_handler()
    frame = np.zeros((8, 8, 3), dtype=np.uint8)
    handler.camera.get_snapshot_frame = MagicMock(return_value=frame)

    fake_jpeg = np.frombuffer(b'jpegdata', dtype=np.uint8)
    with patch('cv2.imencode', return_value=(True, fake_jpeg)) as mock_enc:
        handler.serve_snapshot()

    # The safe getter is used (not a direct read of _last_frame).
    handler.camera.get_snapshot_frame.assert_called_once()
    assert response_status(handler) == 200
    handler.wfile.write.assert_called_once_with(b'jpegdata')

    # JPEG quality 90 preserved.
    encode_params = mock_enc.call_args[0][2]
    assert int(cv2.IMWRITE_JPEG_QUALITY) in encode_params
    assert 90 in encode_params


def test_serve_snapshot_encode_failure_returns_500():
    handler = make_handler()
    frame = np.zeros((8, 8, 3), dtype=np.uint8)
    handler.camera.get_snapshot_frame = MagicMock(return_value=frame)
    with patch('cv2.imencode', return_value=(False, None)):
        handler.serve_snapshot()
    assert error_status(handler) == 500
    handler.wfile.write.assert_not_called()


def test_serve_web_ui_writes_camera_generated_html():
    handler = make_handler()
    handler.camera.get_web_ui_html = MagicMock(return_value='<html>hello</html>')
    handler.serve_web_ui()
    assert response_status(handler) == 200
    assert written_body(handler) == b'<html>hello</html>'
    assert header_values(handler, 'Content-Type') == ['text/html']


# ---------------------------------------------------------------------------
# CORS hardening: no wildcard Access-Control-Allow-Origin anywhere
# ---------------------------------------------------------------------------


def sent_header_names(handler):
    return [c.args[0] for c in handler.send_header.call_args_list]


def written_body(handler):
    """All bytes written to wfile, concatenated."""
    return b''.join(c.args[0] for c in handler.wfile.write.call_args_list)


def make_body_handler(body: bytes, extra_headers=None):
    """Handler with a request body available on rfile + matching headers."""
    handler = make_handler()
    headers = Message()
    headers['Content-Length'] = str(len(body))
    for name, value in (extra_headers or {}).items():
        headers[name] = value
    handler.headers = headers
    handler.rfile = MagicMock()
    handler.rfile.read = MagicMock(return_value=body)
    return handler


def test_do_options_emits_no_cors_headers():
    """Preflight must not answer with Access-Control-Allow-* (wildcard CSRF)."""
    handler = make_handler()
    handler.do_OPTIONS()
    for name in sent_header_names(handler):
        assert not name.startswith('Access-Control-'), (
            f"do_OPTIONS must not emit CORS headers, got {name!r}"
        )
    # Still answers the request (same-origin needs no CORS headers at all).
    assert response_status(handler) in (200, 204)


def test_webrtc_offer_emits_no_cors_headers():
    """The WebRTC answer must not carry Access-Control-Allow-Origin: *."""
    body = json.dumps({'sdp': 'v=0...', 'type': 'offer'}).encode('utf-8')
    handler = make_body_handler(body)
    handler.camera.webrtc_streamer = MagicMock()
    handler.camera.webrtc_streamer.handle_offer_sync = MagicMock(
        return_value={'sdp': 'v=0-answer', 'type': 'answer'}
    )

    handler.handle_webrtc_offer()

    assert response_status(handler) == 200
    for name in sent_header_names(handler):
        assert not name.startswith('Access-Control-')


# ---------------------------------------------------------------------------
# Generic error responses: exception details never reach the client
# ---------------------------------------------------------------------------


def test_update_config_internal_error_is_generic():
    """A server-side exception yields a generic 500 without the raw message."""
    secret = 'SECRET-DETAIL C:/private/path RuntimeError'
    handler = make_body_handler(b'{}')
    handler.camera.config.apply_updates = MagicMock(
        side_effect=RuntimeError(secret)
    )

    handler.update_config()

    assert response_status(handler) == 500
    body = written_body(handler)
    assert b'SECRET-DETAIL' not in body
    assert b'RuntimeError' not in body
    payload = json.loads(body.decode('utf-8'))
    assert payload['success'] is False
    assert payload['error'] == 'Internal server error'


def test_update_config_bad_json_is_generic_400():
    """Malformed JSON: 400 with a generic message, no parser detail echoed."""
    handler = make_body_handler(b'this is {not json')

    handler.update_config()

    assert response_status(handler) == 400
    body = written_body(handler)
    # json.JSONDecodeError messages look like 'Expecting value: line 1 ...'.
    assert b'Expecting' not in body
    assert b'line 1' not in body
    payload = json.loads(body.decode('utf-8'))
    assert payload['success'] is False
    assert payload['error'] == 'Invalid request'


def test_update_ptz_bad_value_is_generic_400():
    """A non-numeric PTZ value: 400 without the float() exception text."""
    body = json.dumps({'action': 'zoom', 'delta': 'not-a-number'}).encode()
    handler = make_body_handler(body)

    handler.update_ptz()

    assert response_status(handler) == 400
    written = written_body(handler)
    assert b'could not convert' not in written
    assert b'not-a-number' not in written
    payload = json.loads(written.decode('utf-8'))
    assert payload['error'] == 'Invalid request'


# ---------------------------------------------------------------------------
# update_ptz action dispatch (zoom / zoom_to / home / move / stop)
# ---------------------------------------------------------------------------


def test_update_ptz_relative_zoom():
    body = json.dumps({'action': 'zoom', 'delta': 0.3}).encode()
    handler = make_body_handler(body)
    handler.camera.ptz.get_status.return_value = {'pan': 0, 'tilt': 0, 'zoom': 0.3, 'moving': False}

    handler.update_ptz()

    handler.camera.ptz.relative_move.assert_called_once_with(zoom_delta=0.3)
    assert response_status(handler) == 200


def test_update_ptz_absolute_zoom_to():
    body = json.dumps({'action': 'zoom_to', 'value': 0.7}).encode()
    handler = make_body_handler(body)

    handler.update_ptz()

    handler.camera.ptz.absolute_move.assert_called_once_with(zoom=0.7)


def test_update_ptz_home():
    body = json.dumps({'action': 'home'}).encode()
    handler = make_body_handler(body)

    handler.update_ptz()

    handler.camera.ptz.goto_home.assert_called_once()


def test_update_ptz_continuous_move():
    body = json.dumps({'action': 'move', 'pan': 0.1, 'tilt': -0.2, 'zoom': 0.3}).encode()
    handler = make_body_handler(body)

    handler.update_ptz()

    handler.camera.ptz.continuous_move.assert_called_once_with(0.1, -0.2, 0.3)


def test_update_ptz_stop():
    body = json.dumps({'action': 'stop'}).encode()
    handler = make_body_handler(body)

    handler.update_ptz()

    handler.camera.ptz.stop_movement.assert_called_once()


def test_update_ptz_no_controller_returns_bare_success():
    body = json.dumps({'action': 'stop'}).encode()
    handler = make_body_handler(body)
    handler.camera.ptz = None

    handler.update_ptz()

    assert response_status(handler) == 200
    assert json.loads(written_body(handler).decode('utf-8')) == {'success': True}


# ---------------------------------------------------------------------------
# serve_stats mode-specific branches (native_webrtc / go2rtc streamer)
# ---------------------------------------------------------------------------


def _stats_handler(streaming_mode, using_mjpeg_fallback=False):
    handler = make_handler()
    handler.camera.streaming_mode = streaming_mode
    handler.camera.using_mjpeg_fallback = using_mjpeg_fallback
    handler.camera.recording_stats = {'recording': False}
    handler.camera.mjpeg_streamer = MagicMock(
        frames_sent=10, actual_fps=5.0, elapsed_time=2.0, client_count=0,
    )
    return handler


def test_serve_stats_native_webrtc_with_active_connection_uses_webrtc_counts():
    handler = _stats_handler('native_webrtc')
    handler.camera.webrtc_streamer = MagicMock(
        connection_count=2, is_running=True,
        stats=MagicMock(frames_sent=50, actual_fps=25.0, elapsed_time=5.0),
    )

    handler.serve_stats()

    payload = json.loads(written_body(handler).decode('utf-8'))
    assert payload['webrtc_connections'] == 2
    assert payload['frames_sent'] == 50


def test_serve_stats_native_webrtc_with_no_connection_falls_back_to_mjpeg_counts():
    handler = _stats_handler('native_webrtc')
    handler.camera.webrtc_streamer = MagicMock(
        connection_count=0, is_running=False,
        stats=MagicMock(frames_sent=50, actual_fps=25.0, elapsed_time=5.0),
    )

    handler.serve_stats()

    payload = json.loads(written_body(handler).decode('utf-8'))
    assert payload['webrtc_connections'] == 0
    assert payload['frames_sent'] == 10  # mjpeg, not webrtc, since no peer connected


def test_serve_stats_go2rtc_mode_uses_streamer_stats():
    handler = _stats_handler('go2rtc')
    handler.camera.webrtc_streamer = None
    handler.camera.streamer = MagicMock(
        is_running=True,
        stats=MagicMock(frames_sent=200, actual_fps=29.9, elapsed_time=20.0, dropped_frames=3),
    )

    handler.serve_stats()

    payload = json.loads(written_body(handler).decode('utf-8'))
    assert payload['frames_sent'] == 200
    assert payload['dropped_frames'] == 3


def test_handle_onvif_error_is_generic_500():
    """An ONVIF handler exception: bare 500, exception text not in response."""
    secret = 'supersecret-onvif-detail'
    handler = make_body_handler(b'', extra_headers={'SOAPAction': '"GetProfiles"'})
    handler.camera.onvif.verify_usernametoken = MagicMock(return_value=True)
    handler.camera.onvif.handle_action = MagicMock(
        side_effect=RuntimeError(secret)
    )

    handler.handle_onvif()

    handler.send_error.assert_called_once()
    args = handler.send_error.call_args.args
    assert args[0] == 500
    # No exception text passed through as the error reason/body.
    assert secret not in repr(handler.send_error.call_args)
    assert secret.encode() not in written_body(handler)


def test_handle_onvif_ws_security_failure_returns_401_fault():
    handler = make_body_handler(b'<soap/>', extra_headers={'SOAPAction': '"GetProfiles"'})
    handler.camera.onvif.verify_usernametoken = MagicMock(return_value=False)
    handler.camera.onvif.fault = MagicMock(return_value='<fault>Sender not authorized</fault>')

    handler.handle_onvif()

    assert response_status(handler) == 401
    assert b'Sender not authorized' in written_body(handler)
    handler.camera.onvif.handle_action.assert_not_called()


def test_handle_onvif_detects_device_action_from_body_when_header_missing():
    handler = make_body_handler(b'<GetDeviceInformation/>')  # no SOAPAction header
    handler.camera.onvif.verify_usernametoken = MagicMock(return_value=True)
    handler.camera.onvif.handle_action = MagicMock(return_value='<response/>')

    handler.handle_onvif()

    handler.camera.onvif.handle_action.assert_called_once_with(
        'GetDeviceInformation', '<GetDeviceInformation/>'
    )
    assert response_status(handler) == 200
    assert header_values(handler, 'Content-Type') == ['application/soap+xml; charset=utf-8']


def test_handle_onvif_detects_ptz_action_from_body_when_header_missing():
    handler = make_body_handler(b'<ContinuousMove/>')
    handler.camera.onvif.verify_usernametoken = MagicMock(return_value=True)
    handler.camera.onvif.handle_action = MagicMock(return_value='<response/>')

    handler.handle_onvif()

    handler.camera.onvif.handle_action.assert_called_once_with(
        'ContinuousMove', '<ContinuousMove/>'
    )
    assert response_status(handler) == 200


def test_handle_onvif_no_action_detected_falls_back_to_empty_string():
    handler = make_body_handler(b'<SomeUnrecognizedTag/>')
    handler.camera.onvif.verify_usernametoken = MagicMock(return_value=True)
    handler.camera.onvif.handle_action = MagicMock(return_value=None)

    handler.handle_onvif()

    handler.camera.onvif.handle_action.assert_called_once_with('', '<SomeUnrecognizedTag/>')
    assert error_status(handler) == 501


def test_handle_onvif_explicit_soap_action_header_takes_precedence():
    """When SOAPAction IS present, the body-sniffing loop is skipped
    entirely -- even if the body also contains a different recognized tag."""
    handler = make_body_handler(
        b'<GetDeviceInformation/>', extra_headers={'SOAPAction': '"GetProfiles"'}
    )
    handler.camera.onvif.verify_usernametoken = MagicMock(return_value=True)
    handler.camera.onvif.handle_action = MagicMock(return_value='<response/>')

    handler.handle_onvif()

    handler.camera.onvif.handle_action.assert_called_once_with(
        'GetProfiles', '<GetDeviceInformation/>'
    )


def test_webrtc_offer_error_is_generic_500():
    handler = make_body_handler(b'{"sdp": "v=0", "type": "offer"}')
    handler.camera.webrtc_streamer = MagicMock()
    handler.camera.webrtc_streamer.handle_offer_sync = MagicMock(
        side_effect=RuntimeError('aiortc exploded at /some/path.py')
    )

    handler.handle_webrtc_offer()

    assert response_status(handler) == 500
    body = written_body(handler)
    assert b'aiortc exploded' not in body
    assert json.loads(body.decode('utf-8'))['error'] == 'Internal server error'


# ---------------------------------------------------------------------------
# Upload size bound (MAX_UPLOAD_BYTES -> 413 before reading the body)
# ---------------------------------------------------------------------------


def make_upload_handler(content_length: int):
    handler = make_handler()
    handler.camera.video_upload_mode = True
    headers = Message()
    headers['Content-Type'] = 'multipart/form-data; boundary=testboundary'
    headers['Content-Length'] = str(content_length)
    handler.headers = headers
    handler.rfile = MagicMock()
    return handler


def test_upload_over_limit_rejected_413_without_reading_body():
    """Oversized Content-Length: 413 and the body is never read into memory."""
    handler = make_upload_handler(MAX_UPLOAD_BYTES + 1)

    handler.handle_video_upload()

    assert response_status(handler) == 413
    handler.rfile.read.assert_not_called()
    payload = json.loads(written_body(handler).decode('utf-8'))
    assert payload['success'] is False
    assert 'error' in payload


def test_upload_at_limit_is_not_rejected_for_size():
    """Exactly MAX_UPLOAD_BYTES is allowed through the size gate."""
    handler = make_upload_handler(MAX_UPLOAD_BYTES)
    # Body won't parse as valid multipart, but the size gate must pass:
    handler.rfile.read = MagicMock(return_value=b'--testboundary--\r\n')

    handler.handle_video_upload()

    assert response_status(handler) != 413
    handler.rfile.read.assert_called_once()


# ---------------------------------------------------------------------------
# serve_mjpeg_stream: ?stream=main|sub query-parameter parsing/validation
# (step 4.2 -- native MJPEG main/sub preview selector)
# ---------------------------------------------------------------------------


def make_mjpeg_streamer_mock():
    """A MagicMock standing in for camera.mjpeg_streamer with sane defaults."""
    mjpeg = MagicMock()
    mjpeg.get_headers = MagicMock(return_value=[
        ('Content-Type', 'multipart/x-mixed-replace; boundary=frame'),
    ])
    mjpeg.add_client = MagicMock(return_value=MagicMock())
    mjpeg.serve_client = MagicMock()
    mjpeg.remove_client = MagicMock()
    return mjpeg


def test_get_requested_mjpeg_stream_defaults_to_main_without_query():
    handler = make_handler()
    handler.path = '/stream.mjpeg'
    assert handler._get_requested_mjpeg_stream() == 'main'


def test_get_requested_mjpeg_stream_parses_sub():
    handler = make_handler()
    handler.path = '/stream.mjpeg?stream=sub'
    assert handler._get_requested_mjpeg_stream() == 'sub'


def test_get_requested_mjpeg_stream_is_case_insensitive():
    handler = make_handler()
    handler.path = '/stream.mjpeg?stream=SUB'
    assert handler._get_requested_mjpeg_stream() == 'sub'


def test_get_requested_mjpeg_stream_invalid_value_falls_back_to_main():
    """An unrecognised ?stream= value falls back to 'main' (not a 400)."""
    handler = make_handler()
    handler.path = '/stream.mjpeg?stream=ultra4k'
    assert handler._get_requested_mjpeg_stream() == 'main'


def test_get_requested_mjpeg_stream_empty_value_falls_back_to_main():
    handler = make_handler()
    handler.path = '/stream.mjpeg?stream='
    assert handler._get_requested_mjpeg_stream() == 'main'


def test_serve_mjpeg_stream_default_registers_main_client():
    """No query string: add_client is called with stream='main'."""
    handler = make_handler()
    handler.path = '/stream.mjpeg'
    mjpeg = make_mjpeg_streamer_mock()
    handler.camera.mjpeg_streamer = mjpeg

    handler.serve_mjpeg_stream()

    mjpeg.add_client.assert_called_once_with(handler.wfile, stream='main')
    mjpeg.serve_client.assert_called_once()


def test_serve_mjpeg_stream_sub_query_registers_sub_client():
    """?stream=sub is threaded through to add_client's stream selector."""
    handler = make_handler()
    handler.path = '/stream.mjpeg?stream=sub'
    mjpeg = make_mjpeg_streamer_mock()
    handler.camera.mjpeg_streamer = mjpeg

    handler.serve_mjpeg_stream()

    mjpeg.add_client.assert_called_once_with(handler.wfile, stream='sub')


def test_serve_mjpeg_stream_invalid_query_falls_back_to_main_client():
    """An invalid ?stream= value still yields a working 'main' registration."""
    handler = make_handler()
    handler.path = '/stream.mjpeg?stream=bogus'
    mjpeg = make_mjpeg_streamer_mock()
    handler.camera.mjpeg_streamer = mjpeg

    handler.serve_mjpeg_stream()

    mjpeg.add_client.assert_called_once_with(handler.wfile, stream='main')
    # Not an error response -- the stream is still served, just at 'main'.
    handler.send_error.assert_not_called()


def test_serve_mjpeg_stream_unavailable_returns_503_regardless_of_query():
    handler = make_handler()
    handler.path = '/stream.mjpeg?stream=sub'
    handler.camera.mjpeg_streamer = None

    handler.serve_mjpeg_stream()

    assert error_status(handler) == 503


# ---------------------------------------------------------------------------
# /api/credentials (step 4.3 -- dedicated credentials endpoint;
# username/password are intentionally excluded from apply_updates())
# ---------------------------------------------------------------------------


def make_credentials_handler(body: bytes):
    """Handler with a real CameraConfig (so set_credentials() actually runs)
    and a stubbed-out save() so no file is touched on disk."""
    handler = make_body_handler(body)
    handler.camera.config = CameraConfig()
    handler.camera.config.save = MagicMock(return_value=True)
    return handler


def test_update_credentials_sets_and_enables_auth():
    body = json.dumps({'username': 'admin', 'password': 'secret'}).encode()
    handler = make_credentials_handler(body)

    handler.update_credentials()

    assert response_status(handler) == 200
    payload = json.loads(written_body(handler).decode('utf-8'))
    assert payload['success'] is True
    assert payload['auth_enabled'] is True
    assert handler.camera.config.username == 'admin'
    assert handler.camera.config.password == 'secret'
    handler.camera.config.save.assert_called_once()


def test_update_credentials_clearing_both_disables_auth():
    body = json.dumps({'username': '', 'password': ''}).encode()
    handler = make_credentials_handler(body)
    handler.camera.config.username = 'admin'
    handler.camera.config.password = 'secret'

    handler.update_credentials()

    assert response_status(handler) == 200
    payload = json.loads(written_body(handler).decode('utf-8'))
    assert payload['success'] is True
    assert payload['auth_enabled'] is False
    assert handler.camera.config.username == ''
    assert handler.camera.config.password == ''


def test_update_credentials_empty_username_nonempty_password_rejected():
    body = json.dumps({'username': '', 'password': 'secret'}).encode()
    handler = make_credentials_handler(body)

    handler.update_credentials()

    assert response_status(handler) == 400
    payload = json.loads(written_body(handler).decode('utf-8'))
    assert payload['success'] is False
    # Rejected -- credentials must remain unset.
    assert handler.camera.config.username == ''
    assert handler.camera.config.password == ''
    handler.camera.config.save.assert_not_called()


def test_update_credentials_nonempty_username_empty_password_rejected():
    body = json.dumps({'username': 'admin', 'password': ''}).encode()
    handler = make_credentials_handler(body)

    handler.update_credentials()

    assert response_status(handler) == 400
    assert handler.camera.config.username == ''
    assert handler.camera.config.password == ''


def test_update_credentials_password_never_echoed_in_response():
    body = json.dumps({'username': 'admin', 'password': 'topsecret123'}).encode()
    handler = make_credentials_handler(body)

    handler.update_credentials()

    assert b'topsecret123' not in written_body(handler)


# ---------------------------------------------------------------------------
# Recording control endpoints (step 4.4 -- /api/recording/{start,stop,status})
# ---------------------------------------------------------------------------


def test_recording_start_route_invokes_camera_and_returns_json():
    handler = make_handler()
    handler.camera.start_recording = MagicMock(return_value=True)
    handler.camera.recording_stats = {'recording': True, 'file': '/tmp/rec_000.mp4'}

    handler.start_recording()

    handler.camera.start_recording.assert_called_once()
    assert response_status(handler) == 200
    payload = json.loads(written_body(handler).decode('utf-8'))
    assert payload['success'] is True
    assert payload['recording'] is True
    assert payload['file'] == '/tmp/rec_000.mp4'


def test_recording_start_failure_returns_500_generic():
    """A graceful recorder failure (bad path/codec) reports success=False."""
    handler = make_handler()
    handler.camera.start_recording = MagicMock(return_value=False)
    handler.camera.recording_stats = {'recording': False, 'file': None}

    handler.start_recording()

    assert response_status(handler) == 500
    payload = json.loads(written_body(handler).decode('utf-8'))
    assert payload['success'] is False
    assert payload['recording'] is False


def test_recording_stop_route_returns_files():
    handler = make_handler()
    handler.camera.stop_recording = MagicMock(
        return_value=['/tmp/rec_000.mp4', '/tmp/rec_001.mp4']
    )
    handler.camera.is_recording = False

    handler.stop_recording()

    handler.camera.stop_recording.assert_called_once()
    assert response_status(handler) == 200
    payload = json.loads(written_body(handler).decode('utf-8'))
    assert payload['success'] is True
    assert payload['recording'] is False
    assert payload['files'] == ['/tmp/rec_000.mp4', '/tmp/rec_001.mp4']


def test_recording_status_route_returns_stats():
    handler = make_handler()
    handler.camera.recording_stats = {
        'recording': True, 'file': '/tmp/rec_000.mp4', 'segments': 1,
        'frames_written': 42, 'dropped': 3, 'bytes': 12345,
    }

    handler.serve_recording_status()

    assert response_status(handler) == 200
    payload = json.loads(written_body(handler).decode('utf-8'))
    assert payload['success'] is True
    assert payload['recording'] is True
    assert payload['frames_written'] == 42
    assert payload['dropped'] == 3


def test_recording_start_error_is_generic_500():
    handler = make_handler()
    handler.camera.start_recording = MagicMock(
        side_effect=RuntimeError('disk exploded at C:/secret/path')
    )

    handler.start_recording()

    assert response_status(handler) == 500
    body = written_body(handler)
    assert b'disk exploded' not in body
    assert json.loads(body.decode('utf-8'))['error'] == 'Internal server error'


def test_recording_start_route_behind_auth():
    """POST /api/recording/start returns 401 without creds when auth enabled."""
    handler = make_auth_handler("admin", "pw")
    handler.path = '/api/recording/start'
    handler.start_recording = MagicMock()
    handler.do_POST()
    assert response_status(handler) == 401
    handler.start_recording.assert_not_called()


def test_recording_stop_route_behind_auth():
    handler = make_auth_handler("admin", "pw")
    handler.path = '/api/recording/stop'
    handler.stop_recording = MagicMock()
    handler.do_POST()
    assert response_status(handler) == 401
    handler.stop_recording.assert_not_called()


def test_recording_start_route_dispatched_with_valid_creds():
    handler = make_auth_handler("admin", "pw", basic_header("admin", "pw"))
    handler.path = '/api/recording/start'
    handler.start_recording = MagicMock()
    handler.do_POST()
    handler.start_recording.assert_called_once()


def test_recording_status_route_behind_auth_and_dispatch():
    # Guarded when auth enabled...
    handler = make_auth_handler("admin", "pw")
    handler.path = '/api/recording/status'
    handler.serve_recording_status = MagicMock()
    handler.do_GET()
    assert response_status(handler) == 401
    handler.serve_recording_status.assert_not_called()

    # ...and dispatched with valid creds.
    handler2 = make_auth_handler("admin", "pw", basic_header("admin", "pw"))
    handler2.path = '/api/recording/status'
    handler2.serve_recording_status = MagicMock()
    handler2.do_GET()
    handler2.serve_recording_status.assert_called_once()


def test_stats_includes_recording_state():
    """GET /api/stats surfaces recorder state alongside streaming stats."""
    handler = make_handler()
    handler.camera.streaming_mode = 'mjpeg'
    handler.camera.using_mjpeg_fallback = True
    mjpeg = MagicMock()
    mjpeg.frames_sent = 100
    mjpeg.actual_fps = 30.0
    mjpeg.elapsed_time = 10.0
    mjpeg.client_count = 0
    mjpeg.is_running = True
    handler.camera.mjpeg_streamer = mjpeg
    handler.camera.webrtc_streamer = None
    handler.camera.recording_stats = {
        'recording': True, 'file': '/recs/cam_000.mp4', 'segments': 2,
        'frames_written': 55, 'dropped': 1, 'bytes': 999,
    }

    handler.serve_stats()

    assert response_status(handler) == 200
    payload = json.loads(written_body(handler).decode('utf-8'))
    assert 'recording' in payload
    rec = payload['recording']
    assert rec['recording'] is True
    # File is reported by basename only (no server paths leaked).
    assert rec['file'] == 'cam_000.mp4'
    assert rec['segments'] == 2
    assert rec['frames_written'] == 55
    assert rec['dropped'] == 1


def test_serve_config_never_includes_password_after_credentials_set():
    """End-to-end: once credentials are set, GET /api/config must still
    never carry the password (serve_config pops it -- see http.py)."""
    handler = make_handler()
    handler.camera.config = CameraConfig(username='admin', password='topsecret123')
    handler.camera.streaming_mode = 'mjpeg'
    handler.camera.webrtc_streamer = None
    handler.camera.video_upload_mode = False
    handler.camera.get_current_video_path = MagicMock(return_value=None)
    handler.camera.get_video_error = MagicMock(return_value=None)

    handler.serve_config()

    body = written_body(handler)
    assert b'topsecret123' not in body
    payload = json.loads(body.decode('utf-8'))
    assert 'password' not in payload
    assert payload['auth_enabled'] is True
    assert payload['username'] == 'admin'
