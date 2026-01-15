#!/usr/bin/env python3
"""HTTP handler for ONVIF and Web UI"""

import os
import json
import logging
import http.server
from typing import Optional, TYPE_CHECKING
from urllib.parse import urlparse
from dataclasses import asdict

if TYPE_CHECKING:
    from .camera import IPCamera

# Set up logging for WebRTC module (INFO level by default)
webrtc_logger = logging.getLogger("ipycam.webrtc")
webrtc_logger.setLevel(logging.INFO)


class IPCameraHTTPHandler(http.server.BaseHTTPRequestHandler):
    """HTTP handler for ONVIF and Web UI"""
    
    camera: Optional['IPCamera'] = None  # Set by IPCamera    
    
    def log_message(self, format, *args):
        pass  # Suppress logging
    
    def do_GET(self):
        path = urlparse(self.path).path
        
        if path == '/' or path == '/index.html':
            self.serve_web_ui()
        elif path == '/webrtc.html':
            self.serve_webrtc_page()
        elif path.startswith('/static/'):
            self.serve_static(path)
        elif path == '/api/config':
            self.serve_config()
        elif path == '/api/stats':
            self.serve_stats()
        elif path == '/api/ptz':
            self.serve_ptz_status()
        elif path == '/api/webrtc/status':
            self.serve_webrtc_status()
        elif path == '/snapshot.jpg':
            self.serve_snapshot()
        elif path == f'/{self.camera.config.mjpeg_url}':
            self.serve_mjpeg_stream()
        else:
            self.send_error(404)
    
    def do_POST(self):
        path = urlparse(self.path).path
        
        if path.startswith('/onvif/'):
            self.handle_onvif()
        elif path == '/api/config':
            self.update_config()
        elif path == '/api/ptz':
            self.update_ptz()
        elif path == '/api/restart':
            self.restart_stream()
        elif path == '/api/webrtc/offer':
            self.handle_webrtc_offer()
        elif path == '/api/webrtc/close':
            self.handle_webrtc_close()
        else:
            self.send_error(404)
    
    def handle_onvif(self):
        """Handle ONVIF SOAP requests"""
        try:
            content_length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(content_length).decode('utf-8')
            soap_action = self.headers.get('SOAPAction', '').strip('"')
            
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
            print(f"ONVIF Error: {e}")
            self.send_error(500, str(e))
    
    def serve_web_ui(self):
        """Serve the configuration web UI"""
        html = self.camera.get_web_ui_html()
        self.send_response(200)
        self.send_header('Content-Type', 'text/html')
        self.end_headers()
        self.wfile.write(html.encode('utf-8'))
    
    def serve_webrtc_page(self):
        """Serve the native WebRTC viewer page"""
        static_dir = os.path.join(os.path.dirname(__file__), 'static')
        file_path = os.path.join(static_dir, 'webrtc.html')
        
        try:
            with open(file_path, 'rb') as f:
                content = f.read()
            self.send_response(200)
            self.send_header('Content-Type', 'text/html')
            self.send_header('Content-Length', len(content))
            self.end_headers()
            self.wfile.write(content)
        except FileNotFoundError:
            self.send_error(404, "WebRTC viewer not found")
    
    def serve_static(self, path: str):
        """Serve static files (CSS, JS)"""
        # Remove /static/ prefix and sanitize path
        relative_path = path[8:]  # Remove '/static/'
        if '..' in relative_path:
            self.send_error(403, "Forbidden")
            return
        
        static_dir = os.path.join(os.path.dirname(__file__), 'static')
        file_path = os.path.join(static_dir, relative_path)
        
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
            self.send_error(500, str(e))
    
    def serve_config(self):
        """Serve current config as JSON"""
        config_dict = asdict(self.camera.config)
        # Add computed properties
        config_dict['main_stream_rtsp'] = self.camera.config.main_stream_rtsp
        config_dict['sub_stream_rtsp'] = self.camera.config.sub_stream_rtsp
        config_dict['webrtc_url'] = self.camera.config.webrtc_url
        # Add streaming mode info
        config_dict['streaming_mode'] = self.camera.streaming_mode
        # Always include full MJPEG URL
        config_dict['mjpeg_url'] = f"http://{self.camera.config.local_ip}:{self.camera.config.onvif_port}/stream.mjpeg"
        # Native WebRTC URL (signaling endpoint)
        config_dict['webrtc_native_url'] = f"http://{self.camera.config.local_ip}:{self.camera.config.onvif_port}/api/webrtc/offer"
        config_dict['webrtc_native_available'] = self.camera.webrtc_streamer is not None
        
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(json.dumps(config_dict).encode('utf-8'))
    
    def serve_stats(self):
        """Serve streaming stats as JSON"""
        stats = {}
        streaming_mode = self.camera.streaming_mode
        
        if streaming_mode == 'native_webrtc' and self.camera.webrtc_streamer:
            # Native WebRTC mode stats
            ws = self.camera.webrtc_streamer.stats
            mjpeg = self.camera.mjpeg_streamer
            stats = {
                'frames_sent': ws.frames_sent,
                'actual_fps': round(ws.actual_fps, 1),
                'elapsed_time': round(ws.elapsed_time, 1),
                'dropped_frames': 0,
                'is_streaming': self.camera.webrtc_streamer.is_running,
                'streaming_mode': 'native_webrtc',
                'webrtc_connections': self.camera.webrtc_streamer.connection_count,
                'mjpeg_clients': mjpeg.client_count if mjpeg else 0,
            }
        elif streaming_mode == 'mjpeg' or self.camera.using_mjpeg_fallback:
            # MJPEG-only mode stats
            mjpeg = self.camera.mjpeg_streamer
            stats = {
                'frames_sent': mjpeg.frames_sent if mjpeg else 0,
                'actual_fps': round(mjpeg.actual_fps, 1) if mjpeg else 0,
                'elapsed_time': round(mjpeg.elapsed_time, 1) if mjpeg else 0,
                'dropped_frames': 0,
                'is_streaming': mjpeg.is_running if mjpeg else False,
                'streaming_mode': 'mjpeg',
                'mjpeg_clients': mjpeg.client_count if mjpeg else 0,
            }
        elif self.camera.streamer:
            # go2rtc mode stats
            s = self.camera.streamer.stats
            mjpeg = self.camera.mjpeg_streamer
            stats = {
                'frames_sent': s.frames_sent,
                'actual_fps': round(s.actual_fps, 1),
                'elapsed_time': round(s.elapsed_time, 1),
                'dropped_frames': s.dropped_frames,
                'is_streaming': self.camera.streamer.is_running,
                'streaming_mode': 'go2rtc',
                'mjpeg_clients': mjpeg.client_count if mjpeg else 0,
            }
        
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(json.dumps(stats).encode('utf-8'))
    
    def serve_snapshot(self):
        """Serve current frame as JPEG snapshot"""
        if self.camera._last_frame is not None:
            import cv2
            
            # Encode with decent quality
            encode_params = [int(cv2.IMWRITE_JPEG_QUALITY), 90]
            success, jpeg = cv2.imencode('.jpg', self.camera._last_frame, encode_params)
            
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
    
    def serve_mjpeg_stream(self):
        """Serve live MJPEG stream (native fallback mode)"""
        if not self.camera.mjpeg_streamer:
            self.send_error(503, "MJPEG streaming not available")
            return
        
        try:
            # Send response headers
            self.send_response(200)
            for header_name, header_value in self.camera.mjpeg_streamer.get_headers():
                self.send_header(header_name, header_value)
            self.end_headers()
            
            # Register this client with the MJPEG streamer
            client = self.camera.mjpeg_streamer.add_client(self.wfile)
            
            # Keep connection alive while client is connected
            # The actual frame writing happens in the camera's stream() method
            while client.connected and self.camera.is_running:
                import time
                time.sleep(0.1)
                
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
            
            # Apply updates
            restart_needed = False
            for key, value in new_config.items():
                if hasattr(self.camera.config, key):
                    old_value = getattr(self.camera.config, key)
                    if old_value != value:
                        setattr(self.camera.config, key, value)
                        # Check if this requires a stream restart
                        if key in ['main_width', 'main_height', 'main_fps', 'main_bitrate',
                                  'sub_width', 'sub_height', 'sub_bitrate', 'hw_accel']:
                            restart_needed = True
            
            # Auto-restart stream if needed
            restarted = False
            if restart_needed and self.camera.is_running:
                restarted = self.camera.restart_stream()
            
            # Save config to file
            self.camera.config.save()
            
            response = {
                'success': True, 
                'restart_needed': restart_needed,
                'restarted': restarted
            }
            
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps(response).encode('utf-8'))
            
        except Exception as e:
            self.send_response(400)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({'error': str(e)}).encode('utf-8'))
    
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
            self.send_response(500)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({'error': str(e)}).encode('utf-8'))

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
        except Exception as e:
            self.send_response(400)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({'error': str(e)}).encode('utf-8'))

    def serve_webrtc_status(self):
        """Return native WebRTC streamer status"""
        status = {
            'available': self.camera.webrtc_streamer is not None,
            'running': False,
            'connections': 0,
            'frames_sent': 0,
            'actual_fps': 0,
        }
        
        if self.camera.webrtc_streamer:
            status['running'] = self.camera.webrtc_streamer.is_running
            status['connections'] = self.camera.webrtc_streamer.connection_count
            status['frames_sent'] = self.camera.webrtc_streamer.stats.frames_sent
            status['actual_fps'] = round(self.camera.webrtc_streamer.stats.actual_fps, 1)
        
        response = json.dumps(status).encode('utf-8')
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(response)))
        self.end_headers()
        self.wfile.write(response)

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
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            self.wfile.write(response)
            
        except Exception as e:
            print(f"WebRTC offer error: {e}")
            self.send_response(500)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({'error': str(e)}).encode('utf-8'))

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
            self.send_response(500)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({'error': str(e)}).encode('utf-8'))

    def do_OPTIONS(self):
        """Handle CORS preflight requests"""
        self.send_response(200)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.send_header('Access-Control-Max-Age', '86400')
        self.end_headers()
