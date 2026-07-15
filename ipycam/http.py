#!/usr/bin/env python3
"""HTTP handler for ONVIF and Web UI"""

import os
import re
import json
import hmac
import base64
import logging
import http.server
from typing import Optional, TYPE_CHECKING
from urllib.parse import urlparse, unquote, parse_qs
from dataclasses import asdict

if TYPE_CHECKING:
    from .camera import IPCamera

logger = logging.getLogger(__name__)

# Maximum accepted request body for /api/video/upload. The hand-rolled
# multipart parser buffers the whole body in memory, so anything larger is
# rejected with 413 BEFORE the body is read (memory-exhaustion protection).
MAX_UPLOAD_BYTES = 500 * 1024 * 1024  # 500 MB


class IPCameraHTTPHandler(http.server.BaseHTTPRequestHandler):
    """HTTP handler for ONVIF and Web UI"""
    
    camera: Optional['IPCamera'] = None  # Set by IPCamera    
    
    def log_message(self, format, *args):
        pass  # Suppress logging

    def _check_basic_auth(self) -> bool:
        """Guard the non-ONVIF surface with optional HTTP Basic auth.

        Returns True immediately when auth is disabled (empty credentials =
        open mode, unchanged behaviour). Otherwise validates the
        ``Authorization: Basic <b64>`` header against the configured
        credentials using constant-time comparisons. On any failure it sends a
        401 with a ``WWW-Authenticate`` challenge and returns False so the
        caller can early-return without serving the request.
        """
        config = self.camera.config
        if not config.auth_enabled:
            return True

        auth_header = self.headers.get('Authorization', '') or ''
        if auth_header.startswith('Basic '):
            try:
                decoded = base64.b64decode(
                    auth_header[len('Basic '):].strip()
                ).decode('utf-8')
                user, sep, pw = decoded.partition(':')
                # Compare both fields regardless to keep timing uniform.
                user_ok = hmac.compare_digest(user, config.username)
                pw_ok = hmac.compare_digest(pw, config.password)
                if sep and user_ok and pw_ok:
                    return True
            except Exception:
                pass  # Malformed header -> fall through to 401.

        body = b'Unauthorized'
        self.send_response(401)
        self.send_header('WWW-Authenticate', 'Basic realm="IPyCam"')
        self.send_header('Content-Type', 'text/plain')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        return False

    def _send_json_error(self, status: int, message: str):
        """Send a JSON error response.

        `message` must be a client-safe, generic string. Never pass raw
        exception text/paths here -- log those server-side instead so internal
        details are not leaked to HTTP clients.
        """
        body = json.dumps({'success': False, 'error': message}).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urlparse(self.path).path

        # Static assets are public; everything else below requires auth
        # (a no-op when credentials are unset).
        if path.startswith('/static/'):
            self.serve_static(path)
            return

        if not self._check_basic_auth():
            return

        if path == '/' or path == '/index.html':
            self.serve_web_ui()
        elif path == '/api/config':
            self.serve_config()
        elif path == '/api/stats':
            self.serve_stats()
        elif path == '/api/ptz':
            self.serve_ptz_status()
        elif path == '/api/recording/status':
            self.serve_recording_status()
        elif path == '/api/video/status':
            self.serve_video_status()
        elif path == f'/{self.camera.config.snapshot_url}':
            self.serve_snapshot()
        elif path == f'/{self.camera.config.mjpeg_url}':
            self.serve_mjpeg_stream()
        else:
            self.send_error(404)
    
    def do_POST(self):
        path = urlparse(self.path).path

        # ONVIF authenticates via WS-Security inside handle_onvif; the rest of
        # the POST surface uses HTTP Basic auth (a no-op when creds are unset).
        if path.startswith('/onvif/'):
            self.handle_onvif()
            return

        if not self._check_basic_auth():
            return

        if path == '/api/config':
            self.update_config()
        elif path == '/api/credentials':
            self.update_credentials()
        elif path == '/api/ptz':
            self.update_ptz()
        elif path == '/api/restart':
            self.restart_stream()
        elif path == '/api/recording/start':
            self.start_recording()
        elif path == '/api/recording/stop':
            self.stop_recording()
        elif path == '/api/webrtc/offer':
            self.handle_webrtc_offer()
        elif path == '/api/webrtc/close':
            self.handle_webrtc_close()
        elif path == '/api/video/upload':
            self.handle_video_upload()
        else:
            self.send_error(404)
    
    def handle_onvif(self):
        """Handle ONVIF SOAP requests"""
        try:
            content_length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(content_length).decode('utf-8')
            soap_action = self.headers.get('SOAPAction', '').strip('"')

            # Enforce WS-Security UsernameToken auth at the HTTP boundary (a
            # no-op when credentials are unset). Kept here rather than inside
            # ONVIFService.handle_action so direct handle_action() callers /
            # tests stay unauthenticated.
            if not self.camera.onvif.verify_usernametoken(body):
                fault_bytes = self.camera.onvif.fault(
                    "Sender not authorized"
                ).encode('utf-8')
                self.send_response(401)
                self.send_header('Content-Type', 'application/soap+xml; charset=utf-8')
                self.send_header('Content-Length', len(fault_bytes))
                self.end_headers()
                self.wfile.write(fault_bytes)
                return

            # Detect action from body if header is missing
            if not soap_action:
                # Device and Media service actions
                for action in ['GetDeviceInformation', 'GetSystemDateAndTime', 'GetCapabilities',
                              'GetServices', 'GetServiceCapabilities', 'GetProfiles', 'GetStreamUri', 
                              'GetSnapshotUri', 'GetVideoEncoderConfiguration', 'GetVideoSourceConfiguration',
                              'GetAudioDecoderConfigurations', 'GetScopes', 'GetUsers']:
                    if action in body:
                        soap_action = action
                        break
                
                # PTZ service actions
                if not soap_action:
                    for action in ['GetNodes', 'GetNode', 'GetConfigurations', 'GetConfiguration',
                                  'GetServiceCapabilities', 'GetStatus', 'ContinuousMove', 'Stop',
                                  'AbsoluteMove', 'RelativeMove', 'GotoHomePosition',
                                  'GetPresets', 'SetPreset', 'GotoPreset']:
                        if action in body:
                            soap_action = action
                            break
            
            response = self.camera.onvif.handle_action(soap_action, body)
            
            if response:
                response_bytes = response.encode('utf-8')
                self.send_response(200)
                self.send_header('Content-Type', 'application/soap+xml; charset=utf-8')
                self.send_header('Content-Length', len(response_bytes))
                self.end_headers()
                self.wfile.write(response_bytes)
            else:
                self.send_error(501, "Not Implemented")
        except Exception as e:
            # Log the real error server-side only; clients get a generic 500
            # so exception text/paths are never leaked.
            logger.exception(f"ONVIF Error: {e}")
            self.send_error(500)
    
    def serve_web_ui(self):
        """Serve the configuration web UI"""
        html = self.camera.get_web_ui_html()
        self.send_response(200)
        self.send_header('Content-Type', 'text/html')
        self.end_headers()
        self.wfile.write(html.encode('utf-8'))

    
    def serve_static(self, path: str):
        """Serve static files (CSS, JS)"""
        # Resolve the static directory to a canonical absolute path up front;
        # every served file must live inside it.
        static_dir = os.path.realpath(os.path.join(os.path.dirname(__file__), 'static'))

        # Strip the '/static/' prefix and URL-decode so percent-encoded
        # traversal sequences (e.g. %2e%2e, %2f, %5c) cannot bypass the checks.
        # Normalise backslashes to forward slashes so Windows separators are
        # treated consistently on every platform.
        requested = unquote(path[len('/static/'):]).replace('\\', '/')

        # Reject anything that is not a plain relative path: absolute paths,
        # leading slashes (POSIX roots / UNC shares) and Windows drive letters
        # (e.g. "C:/Windows/...") would otherwise cause os.path.join to discard
        # static_dir entirely and read arbitrary files.
        if (os.path.isabs(requested)
                or requested.startswith('/')
                or (len(requested) >= 2 and requested[1] == ':')):
            self.send_error(403, "Forbidden")
            return

        # Resolve the final path and confirm it is contained within static_dir.
        # realpath collapses any '..' segments; commonpath then verifies
        # containment. commonpath raises ValueError for paths on different
        # drives or mixed absolute/relative -- treat that as "outside".
        file_path = os.path.realpath(os.path.join(static_dir, requested))
        try:
            if os.path.commonpath([static_dir, file_path]) != static_dir:
                self.send_error(403, "Forbidden")
                return
        except ValueError:
            self.send_error(403, "Forbidden")
            return

        if not os.path.isfile(file_path):
            self.send_error(404, "File not found")
            return

        # Determine content type
        content_types = {
            '.css': 'text/css',
            '.js': 'application/javascript',
            '.html': 'text/html',
            '.png': 'image/png',
            '.jpg': 'image/jpeg',
            '.ico': 'image/x-icon',
        }
        ext = os.path.splitext(file_path)[1].lower()
        content_type = content_types.get(ext, 'application/octet-stream')

        try:
            with open(file_path, 'rb') as f:
                content = f.read()
            self.send_response(200)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', len(content))
            self.end_headers()
            self.wfile.write(content)
        except Exception as e:
            logger.error(f"Static file error: {e}")
            self.send_error(500)
    
    def serve_config(self):
        """Serve current config as JSON"""
        config_dict = asdict(self.camera.config)
        # Never expose the stored password over the API.
        config_dict.pop('password', None)
        config_dict['auth_enabled'] = self.camera.config.auth_enabled
        # Add computed properties
        config_dict['main_stream_rtsp'] = self.camera.config.main_stream_rtsp
        config_dict['sub_stream_rtsp'] = self.camera.config.sub_stream_rtsp
        config_dict['webrtc_url'] = self.camera.config.webrtc_url
        # Add streaming mode info
        config_dict['streaming_mode'] = self.camera.streaming_mode
        # Always include full MJPEG URL (use the configured path + the port the
        # main HTTP server actually listens on).
        config_dict['mjpeg_url'] = f"http://{self.camera.config.local_ip}:{self.camera.config.onvif_port}/{self.camera.config.mjpeg_url}"
        # Native WebRTC URL (signaling endpoint)
        config_dict['webrtc_native_url'] = f"http://{self.camera.config.local_ip}:{self.camera.config.onvif_port}/api/webrtc/offer"
        config_dict['webrtc_native_available'] = self.camera.webrtc_streamer is not None
        # Video upload mode info
        config_dict['video_upload_mode'] = self.camera.video_upload_mode
        config_dict['current_video'] = os.path.basename(self.camera.get_current_video_path()) if self.camera.get_current_video_path() else None
        config_dict['video_error'] = self.camera.get_video_error()
        
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(json.dumps(config_dict).encode('utf-8'))
    
    def serve_stats(self):
        """Serve streaming stats as JSON"""
        streaming_mode = self.camera.streaming_mode
        mjpeg = self.camera.mjpeg_streamer
        
        # Base stats that are always available
        stats = {
            'streaming_mode': streaming_mode,
            'is_streaming': True,
            # MJPEG stats (always available since MJPEG is always running)
            'mjpeg_frames_sent': mjpeg.frames_sent if mjpeg else 0,
            'mjpeg_fps': round(mjpeg.actual_fps, 1) if mjpeg else 0,
            'mjpeg_elapsed_time': round(mjpeg.elapsed_time, 1) if mjpeg else 0,
            'mjpeg_clients': mjpeg.client_count if mjpeg else 0,
        }

        # Recording state (always present; recorder is always constructed).
        rec = self.camera.recording_stats
        stats['recording'] = {
            'recording': rec.get('recording', False),
            'file': os.path.basename(rec['file']) if rec.get('file') else None,
            'segments': rec.get('segments', 0),
            'frames_written': rec.get('frames_written', 0),
            'dropped': rec.get('dropped', 0),
            'bytes': rec.get('bytes', 0),
        }
        
        # Add mode-specific stats
        if streaming_mode == 'native_webrtc' and self.camera.webrtc_streamer:
            ws = self.camera.webrtc_streamer.stats
            stats.update({
                'webrtc_frames_sent': ws.frames_sent,
                'webrtc_fps': round(ws.actual_fps, 1),
                'webrtc_elapsed_time': round(ws.elapsed_time, 1),
                'webrtc_connections': self.camera.webrtc_streamer.connection_count,
                'is_streaming': self.camera.webrtc_streamer.is_running,
                # Primary stats for UI (use WebRTC when connections exist, else MJPEG)
                'frames_sent': ws.frames_sent if self.camera.webrtc_streamer.connection_count > 0 else (mjpeg.frames_sent if mjpeg else 0),
                'actual_fps': round(ws.actual_fps, 1) if self.camera.webrtc_streamer.connection_count > 0 else (round(mjpeg.actual_fps, 1) if mjpeg else 0),
                'elapsed_time': round(ws.elapsed_time, 1) if self.camera.webrtc_streamer.connection_count > 0 else (round(mjpeg.elapsed_time, 1) if mjpeg else 0),
                'dropped_frames': 0,
            })
        elif streaming_mode == 'mjpeg' or self.camera.using_mjpeg_fallback:
            stats.update({
                'frames_sent': mjpeg.frames_sent if mjpeg else 0,
                'actual_fps': round(mjpeg.actual_fps, 1) if mjpeg else 0,
                'elapsed_time': round(mjpeg.elapsed_time, 1) if mjpeg else 0,
                'dropped_frames': 0,
                'is_streaming': mjpeg.is_running if mjpeg else False,
            })
        elif self.camera.streamer:
            s = self.camera.streamer.stats
            stats.update({
                'frames_sent': s.frames_sent,
                'actual_fps': round(s.actual_fps, 1),
                'elapsed_time': round(s.elapsed_time, 1),
                'dropped_frames': s.dropped_frames,
                'is_streaming': self.camera.streamer.is_running,
            })
        
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(json.dumps(stats).encode('utf-8'))
    
    def serve_snapshot(self):
        """Serve current frame as JPEG snapshot"""
        # Fetch a thread-safe, independent copy so we never encode a frame that
        # the capture thread is mutating in place (see IPCamera.stream()).
        frame = self.camera.get_snapshot_frame()
        if frame is not None:
            import cv2

            # Encode with decent quality
            encode_params = [int(cv2.IMWRITE_JPEG_QUALITY), 90]
            success, jpeg = cv2.imencode('.jpg', frame, encode_params)

            if success:
                jpeg_bytes = jpeg.tobytes()
                self.send_response(200)
                self.send_header('Content-Type', 'image/jpeg')
                self.send_header('Content-Length', str(len(jpeg_bytes)))
                self.send_header('Cache-Control', 'no-cache, no-store, must-revalidate')
                self.send_header('Pragma', 'no-cache')
                self.send_header('Expires', '0')
                self.end_headers()
                self.wfile.write(jpeg_bytes)
            else:
                self.send_error(500, "Failed to encode snapshot")
        else:
            self.send_error(503, "No frame available")
    
    def _get_requested_mjpeg_stream(self) -> str:
        """Parse the ``?stream=`` query parameter selecting main vs sub.

        Returns 'sub' only when the query parameter is exactly 'sub'
        (case-insensitive); any other value, or its absence, falls back to
        'main' rather than erroring -- this is a preview convenience knob,
        not a validated API contract.
        """
        query = parse_qs(urlparse(self.path).query)
        requested = (query.get('stream', ['main'])[0] or 'main').strip().lower()
        return requested if requested in ('main', 'sub') else 'main'

    def serve_mjpeg_stream(self):
        """Serve live MJPEG stream (native fallback mode).

        Supports an optional ``?stream=main|sub`` query parameter so the web
        UI can preview the lower-resolution sub stream instead of the
        full-resolution main stream. Defaults to 'main'; any unrecognised
        value also falls back to 'main'.
        """
        if not self.camera.mjpeg_streamer:
            self.send_error(503, "MJPEG streaming not available")
            return

        stream = self._get_requested_mjpeg_stream()

        try:
            # Send response headers
            self.send_response(200)
            for header_name, header_value in self.camera.mjpeg_streamer.get_headers():
                self.send_header(header_name, header_value)
            self.end_headers()

            # Register this client with the MJPEG streamer and become its
            # writer: block on the client's own queue and write encoded frames
            # to this socket. This replaces the old busy-wait sleep loop and
            # isolates a slow client to its own connection thread (the encode
            # worker and every other client are unaffected). serve_client
            # removes the client on return.
            client = self.camera.mjpeg_streamer.add_client(self.wfile, stream=stream)
            self.camera.mjpeg_streamer.serve_client(client)

        except (BrokenPipeError, ConnectionResetError):
            pass  # Client disconnected
        finally:
            if self.camera.mjpeg_streamer and 'client' in dir():
                self.camera.mjpeg_streamer.remove_client(client)
    
    def update_config(self):
        """Update camera configuration"""
        try:
            content_length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(content_length).decode('utf-8')
            new_config = json.loads(body)

            # Validate and apply updates (rejects unknown/out-of-range values
            # instead of blindly setattr-ing them onto the config).
            applied, rejected, restart_keys = self.camera.config.apply_updates(new_config)
            restart_needed = len(restart_keys) > 0

            # Recording knobs never restart the stream; instead reconcile the
            # recorder directly (start/stop the worker for recording_enabled,
            # pick up pre-record/format changes). Done before the stream
            # restart below so both can happen in one update.
            if any(k.startswith('recording_') for k in applied):
                self.camera.apply_recording_config()

            # Auto-restart stream if needed
            restarted = False
            if restart_needed and self.camera.is_running:
                restarted = self.camera.restart_stream()

            # Save config back to the file it was loaded from
            saved = self.camera.config.save()

            response = {
                'success': True,
                'applied': applied,
                'rejected': rejected,
                'restart_needed': restart_needed,
                'restarted': restarted,
                'saved': saved,
            }

            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps(response).encode('utf-8'))

        except (ValueError, TypeError, AttributeError, UnicodeDecodeError) as e:
            # Malformed body / bad JSON from the client. Log the detail
            # server-side; the response carries only a generic message.
            logger.warning(f"Config update error: {e}")
            self._send_json_error(400, 'Invalid request')
        except Exception as e:
            logger.exception(f"Config update error: {e}")
            self._send_json_error(500, 'Internal server error')

    def update_credentials(self):
        """Set or clear the HTTP Basic auth / ONVIF WS-Security credential pair.

        A dedicated endpoint because username/password are deliberately NOT
        in EDITABLE_FIELDS (see CameraConfig.set_credentials / the
        EDITABLE_FIELDS comment in config.py) -- the generic /api/config path
        must never be able to change credentials.

        Bootstrapping note: this endpoint sits behind the same
        _check_basic_auth guard as the rest of do_POST. Once auth is enabled,
        changing credentials again requires the CURRENT credentials (a 401 is
        sent before this method ever runs). While auth is disabled, however,
        anyone who can reach the web UI (e.g. anyone on the LAN) can set the
        initial credentials -- this open first-run bootstrap is an accepted
        tradeoff (comparable to a router's unauthenticated first-run setup),
        not an oversight.
        """
        try:
            content_length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(content_length).decode('utf-8')
            data = json.loads(body)

            username = data.get('username', '')
            password = data.get('password', '')

            ok, error = self.camera.config.set_credentials(username, password)
            if not ok:
                self._send_json_error(400, error or 'Invalid credentials')
                return

            saved = self.camera.config.save()

            response = {
                'success': True,
                'auth_enabled': self.camera.config.auth_enabled,
                'saved': saved,
            }
            # NEVER echo the password (or username) back beyond what the
            # client just sent us -- the response only carries booleans.
            body_bytes = json.dumps(response).encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body_bytes)))
            self.end_headers()
            self.wfile.write(body_bytes)

        except (ValueError, TypeError, AttributeError, UnicodeDecodeError) as e:
            logger.warning(f"Credentials update error: {e}")
            self._send_json_error(400, 'Invalid request')
        except Exception as e:
            logger.exception(f"Credentials update error: {e}")
            self._send_json_error(500, 'Internal server error')

    def restart_stream(self):
        """Restart the video stream with current config"""
        try:
            success = self.camera.restart_stream()
            response = {'success': success}

            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps(response).encode('utf-8'))
        except Exception as e:
            logger.exception(f"Stream restart error: {e}")
            self._send_json_error(500, 'Internal server error')

    def _write_json(self, status: int, payload: dict):
        """Serialise ``payload`` as a JSON response with the given status."""
        body = json.dumps(payload).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def start_recording(self):
        """Start recording to disk (POST /api/recording/start).

        Returns {success, recording, file}. A bad path / un-openable codec is
        reported as success=False without leaking internal detail (the recorder
        logs the specifics server-side).
        """
        try:
            ok = self.camera.start_recording()
            stats = self.camera.recording_stats
            self._write_json(200 if ok else 500, {
                'success': bool(ok),
                'recording': stats.get('recording', False),
                'file': stats.get('file'),
                'error': None if ok else 'Failed to start recording',
            })
        except Exception as e:
            logger.exception(f"Recording start error: {e}")
            self._send_json_error(500, 'Internal server error')

    def stop_recording(self):
        """Stop recording (POST /api/recording/stop).

        Returns {success, recording, files} where files are the finalised
        segment paths (empty if nothing was recording).
        """
        try:
            files = self.camera.stop_recording()
            self._write_json(200, {
                'success': True,
                'recording': self.camera.is_recording,
                'files': files,
            })
        except Exception as e:
            logger.exception(f"Recording stop error: {e}")
            self._send_json_error(500, 'Internal server error')

    def serve_recording_status(self):
        """Return recorder state as JSON (GET /api/recording/status)."""
        try:
            stats = self.camera.recording_stats
            self._write_json(200, {'success': True, **stats})
        except Exception as e:
            logger.exception(f"Recording status error: {e}")
            self._send_json_error(500, 'Internal server error')

    def serve_ptz_status(self):
        """Return current PTZ status as JSON"""
        if self.camera.ptz:
            status = self.camera.ptz.get_status()
        else:
            status = {'pan': 0, 'tilt': 0, 'zoom': 0, 'moving': False}
        
        response = json.dumps(status).encode('utf-8')
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(response)))
        self.end_headers()
        self.wfile.write(response)

    def update_ptz(self):
        """Handle PTZ control commands"""
        try:
            content_length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(content_length).decode('utf-8')
            data = json.loads(body)
            
            action = data.get('action', '')
            
            if self.camera.ptz:
                if action == 'zoom':
                    # Relative zoom
                    delta = float(data.get('delta', 0))
                    self.camera.ptz.relative_move(zoom_delta=delta)
                elif action == 'zoom_to':
                    # Absolute zoom
                    value = float(data.get('value', 0))
                    self.camera.ptz.absolute_move(zoom=value)
                elif action == 'home':
                    self.camera.ptz.goto_home()
                elif action == 'move':
                    # Continuous move
                    pan = float(data.get('pan', 0))
                    tilt = float(data.get('tilt', 0))
                    zoom = float(data.get('zoom', 0))
                    self.camera.ptz.continuous_move(pan, tilt, zoom)
                elif action == 'stop':
                    self.camera.ptz.stop_movement()
            
            # Return current status
            status = self.camera.ptz.get_status() if self.camera.ptz else {}
            response = json.dumps({'success': True, **status}).encode('utf-8')
            
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(response)))
            self.end_headers()
            self.wfile.write(response)
        except (ValueError, TypeError, AttributeError, UnicodeDecodeError) as e:
            # Bad JSON / non-numeric values from the client. Log the detail
            # server-side; the response carries only a generic message.
            logger.warning(f"PTZ update error: {e}")
            self._send_json_error(400, 'Invalid request')
        except Exception as e:
            logger.exception(f"PTZ update error: {e}")
            self._send_json_error(500, 'Internal server error')


    def handle_webrtc_offer(self):
        """Handle WebRTC offer for native WebRTC streaming"""
        try:
            if not self.camera.webrtc_streamer:
                self.send_response(503)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({
                    'error': 'Native WebRTC not available'
                }).encode('utf-8'))
                return
            
            content_length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(content_length).decode('utf-8')
            data = json.loads(body)
            
            sdp = data.get('sdp', '')
            type_ = data.get('type', 'offer')
            
            if not sdp:
                self.send_response(400)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({'error': 'Missing SDP'}).encode('utf-8'))
                return
            
            # Handle the WebRTC offer and get the answer
            answer = self.camera.webrtc_streamer.handle_offer_sync(sdp, type_)
            
            response = json.dumps(answer).encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(response)))
            # No CORS headers: the web UI is served from this same origin, so
            # cross-origin access is intentionally not enabled (see do_OPTIONS).
            self.end_headers()
            self.wfile.write(response)

        except Exception as e:
            logger.exception(f"WebRTC offer error: {e}")
            self._send_json_error(500, 'Internal server error')

    def handle_webrtc_close(self):
        """Close WebRTC connections"""
        try:
            if self.camera.webrtc_streamer:
                self.camera.webrtc_streamer.close_connection_sync()
            
            response = json.dumps({'success': True}).encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(response)))
            self.end_headers()
            self.wfile.write(response)

        except Exception as e:
            logger.exception(f"WebRTC close error: {e}")
            self._send_json_error(500, 'Internal server error')

    def serve_video_status(self):
        """Serve video upload mode status as JSON"""
        status = {
            'video_upload_mode': self.camera.video_upload_mode,
            'current_video': os.path.basename(self.camera.get_current_video_path()) if self.camera.get_current_video_path() else None,
            'current_video_path': self.camera.get_current_video_path(),
            'video_error': self.camera.get_video_error(),
            'source_type': self.camera.config.source_type,
            'source_info': self.camera.config.source_info,
        }
        
        response = json.dumps(status).encode('utf-8')
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(response)))
        self.end_headers()
        self.wfile.write(response)

    def handle_video_upload(self):
        """Handle video file upload"""
        if not self.camera.video_upload_mode:
            self.send_response(400)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({
                'error': 'Video upload mode is not enabled. Start with --source video'
            }).encode('utf-8'))
            return
        
        try:
            # Parse multipart form data
            content_type = self.headers.get('Content-Type', '')
            if not content_type.startswith('multipart/form-data'):
                self.send_response(400)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({
                    'error': 'Expected multipart/form-data'
                }).encode('utf-8'))
                return
            
            # Extract boundary
            boundary_match = re.search(r'boundary=(.+?)(?:$|;|\s)', content_type)
            if not boundary_match:
                self.send_response(400)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({
                    'error': 'Missing boundary in Content-Type'
                }).encode('utf-8'))
                return
            
            boundary = boundary_match.group(1).strip('"')
            content_length = int(self.headers.get('Content-Length', 0))

            # Bound the request size BEFORE reading anything: the multipart
            # parser below buffers the entire body in memory, so an unbounded
            # Content-Length is a memory-exhaustion DoS vector.
            if content_length > MAX_UPLOAD_BYTES:
                self._send_json_error(
                    413,
                    f'Upload too large (max {MAX_UPLOAD_BYTES // (1024 * 1024)} MB)'
                )
                return

            # Read the entire body
            body = self.rfile.read(content_length)
            
            # Parse multipart data
            boundary_bytes = ('--' + boundary).encode('utf-8')
            parts = body.split(boundary_bytes)
            
            video_data = None
            filename = None
            
            for part in parts:
                if not part or part == b'--' or part == b'--\r\n':
                    continue
                
                # Split headers from content
                if b'\r\n\r\n' in part:
                    headers_section, content = part.split(b'\r\n\r\n', 1)
                    headers_text = headers_section.decode('utf-8', errors='ignore')
                    
                    # Check if this is the file part
                    if 'filename=' in headers_text:
                        # Extract filename
                        filename_match = re.search(r'filename="?([^";\r\n]+)"?', headers_text)
                        if filename_match:
                            filename = filename_match.group(1).strip()
                            # Remove trailing boundary markers and whitespace
                            if content.endswith(b'\r\n'):
                                content = content[:-2]
                            video_data = content
                            break
            
            if not video_data or not filename:
                self.send_response(400)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({
                    'error': 'No video file found in upload'
                }).encode('utf-8'))
                return
            
            # Validate file extension
            video_extensions = {'.mp4', '.avi', '.mkv', '.mov', '.wmv', '.flv', '.webm', '.m4v', '.mpeg', '.mpg', '.3gp'}
            _, ext = os.path.splitext(filename)
            if ext.lower() not in video_extensions:
                self.send_response(400)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({
                    'error': f'Invalid video format: {ext}. Supported: {", ".join(video_extensions)}'
                }).encode('utf-8'))
                return
            
            # Create videos directory if it doesn't exist
            videos_dir = os.path.join(os.path.dirname(__file__), '..', 'videos')
            videos_dir = os.path.abspath(videos_dir)
            os.makedirs(videos_dir, exist_ok=True)
            
            # Generate unique filename to avoid conflicts
            import time
            timestamp = int(time.time())
            safe_filename = re.sub(r'[^\w\-_\.]', '_', filename)
            final_filename = f"{timestamp}_{safe_filename}"
            filepath = os.path.join(videos_dir, final_filename)
            
            # Save the video file
            with open(filepath, 'wb') as f:
                f.write(video_data)
            
            logger.info(f"  Video uploaded: {final_filename} ({len(video_data)} bytes)")
            
            # Set the new video path - the main loop will pick it up
            previous_video = self.camera.get_current_video_path()
            self.camera.set_current_video_path(filepath)
            
            response = json.dumps({
                'success': True,
                'filename': final_filename,
                'size': len(video_data),
                'path': filepath,
                'previous_video': os.path.basename(previous_video) if previous_video else None
            }).encode('utf-8')
            
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(response)))
            self.end_headers()
            self.wfile.write(response)
            
        except Exception as e:
            # Full details go to the server log only; the client gets a
            # generic error.
            logger.exception(f"Video upload error: {e}")
            self._send_json_error(500, 'Internal server error')

    def do_OPTIONS(self):
        """Handle OPTIONS requests.

        Deliberately emits NO Access-Control-Allow-* headers: the web UI is
        served from the same origin as this API, so it never needs CORS, and a
        wildcard allow-origin would let arbitrary websites drive the camera
        API from a visitor's browser (CSRF-style).
        """
        self.send_response(204)
        self.send_header('Allow', 'GET, POST, OPTIONS')
        self.end_headers()
