#!/usr/bin/env python3
"""ONVIF Device and Media Service handler"""

import os
import re
import time
import uuid
from typing import Dict, Optional, TYPE_CHECKING

from camera_config import CameraConfig

if TYPE_CHECKING:
    from ptz_controller import PTZController


class ONVIFService:
    """ONVIF Device and Media Service handler"""
    
    def __init__(self, config: CameraConfig, ptz_controller: Optional['PTZController'] = None):
        self.config = config
        self.ptz = ptz_controller
        self.device_uuid = f"urn:uuid:{uuid.uuid4()}"
        self._templates: Dict[str, str] = {}
        self._load_templates()
    
    def _load_templates(self):
        """Load all SOAP templates from static/soap/"""
        soap_dir = os.path.join(os.path.dirname(__file__), 'static', 'soap')
        for filename in os.listdir(soap_dir):
            if filename.endswith('.xml'):
                template_name = filename[:-4]  # Remove .xml
                with open(os.path.join(soap_dir, filename), 'r', encoding='utf-8') as f:
                    self._templates[template_name] = f.read()
    
    def _render(self, template_name: str, **kwargs) -> str:
        """Render a template with the given variables"""
        template = self._templates.get(template_name, '')
        for key, value in kwargs.items():
            template = template.replace(f'{{{{{key}}}}}', str(value))
        return template
    
    def _wrap_envelope(self, body: str) -> str:
        """Wrap body content in SOAP envelope"""
        return self._render('envelope', body=body)

    def handle_action(self, action: str, body: str) -> Optional[str]:
        """Route SOAP actions to handlers"""
        handlers = {
            'GetSystemDateAndTime': self.get_system_date_time,
            'GetDeviceInformation': self.get_device_information,
            'GetCapabilities': self.get_capabilities,
            'GetServices': self.get_services,
            'GetScopes': self.get_scopes,
            'GetUsers': self.get_users,
            'GetProfiles': self.get_profiles,
            'GetStreamUri': lambda: self.get_stream_uri(body),
            'GetSnapshotUri': lambda: self.get_snapshot_uri(body),
            'GetVideoEncoderConfiguration': self.get_video_encoder_configuration,
            'GetVideoSourceConfiguration': self.get_video_source_configuration,
            'GetAudioDecoderConfigurations': self.get_audio_decoder_configurations,
            # PTZ handlers
            'GetNodes': self.ptz_get_nodes,
            'GetNode': self.ptz_get_node,
            'GetConfigurations': self.ptz_get_configurations,
            'GetConfiguration': self.ptz_get_configurations,
            'GetServiceCapabilities': self.ptz_get_service_capabilities,
            'GetStatus': lambda: self.ptz_get_status(body),
            'ContinuousMove': lambda: self.ptz_continuous_move(body),
            'Stop': lambda: self.ptz_stop(body),
            'AbsoluteMove': lambda: self.ptz_absolute_move(body),
            'RelativeMove': lambda: self.ptz_relative_move(body),
            'GotoHomePosition': lambda: self.ptz_goto_home(body),
            'GetPresets': lambda: self.ptz_get_presets(body),
            'SetPreset': lambda: self.ptz_set_preset(body),
            'GotoPreset': lambda: self.ptz_goto_preset(body),
        }
        
        for key, handler in handlers.items():
            if key in action:
                return handler()
        
        return self.fault(f"Action not supported: {action}")
    
    def fault(self, reason: str) -> str:
        return self._render('fault', reason=reason)

    def _bitrate_to_kbps(self, bitrate: str) -> int:
        """Convert bitrate string like '4M' or '512K' to kbps"""
        if bitrate.endswith('M'):
            return int(bitrate[:-1]) * 1000
        elif bitrate.endswith('K'):
            return int(bitrate[:-1])
        return int(bitrate)

    def get_system_date_time(self) -> str:
        now = time.gmtime()
        body = self._render('get_system_date_time',
            hour=now.tm_hour, minute=now.tm_min, second=now.tm_sec,
            year=now.tm_year, month=now.tm_mon, day=now.tm_mday)
        return self._wrap_envelope(body)

    def get_device_information(self) -> str:
        body = self._render('get_device_information',
            manufacturer=self.config.manufacturer,
            model=self.config.model,
            firmware_version=self.config.firmware_version,
            serial_number=self.config.serial_number)
        return self._wrap_envelope(body)

    def get_capabilities(self) -> str:
        device_url = f"http://{self.config.local_ip}:{self.config.onvif_port}/onvif/device_service"
        media_url = f"http://{self.config.local_ip}:{self.config.onvif_port}/onvif/media_service"
        ptz_url = f"http://{self.config.local_ip}:{self.config.onvif_port}/onvif/ptz_service"
        body = self._render('get_capabilities', device_url=device_url, media_url=media_url, ptz_url=ptz_url)
        return self._wrap_envelope(body)

    def get_services(self) -> str:
        device_url = f"http://{self.config.local_ip}:{self.config.onvif_port}/onvif/device_service"
        media_url = f"http://{self.config.local_ip}:{self.config.onvif_port}/onvif/media_service"
        ptz_url = f"http://{self.config.local_ip}:{self.config.onvif_port}/onvif/ptz_service"
        body = self._render('get_services', device_url=device_url, media_url=media_url, ptz_url=ptz_url)
        return self._wrap_envelope(body)

    def get_scopes(self) -> str:
        body = self._render('get_scopes', camera_name=self.config.name)
        return self._wrap_envelope(body)

    def get_users(self) -> str:
        body = self._render('get_users')
        return self._wrap_envelope(body)
        return self._wrap_envelope(body)

    def get_profiles(self) -> str:
        body = self._render('get_profiles',
            main_width=self.config.main_width,
            main_height=self.config.main_height,
            main_fps=self.config.main_fps,
            main_bitrate_kbps=self._bitrate_to_kbps(self.config.main_bitrate),
            sub_width=self.config.sub_width,
            sub_height=self.config.sub_height,
            sub_fps=self.config.sub_fps,
            sub_bitrate_kbps=self._bitrate_to_kbps(self.config.sub_bitrate))
        return self._wrap_envelope(body)

    def get_stream_uri(self, body: str) -> str:
        uri = self.config.main_stream_rtsp
        if "Sub" in body or "Profile_2" in body:
            uri = self.config.sub_stream_rtsp
        body = self._render('get_stream_uri', stream_uri=uri)
        return self._wrap_envelope(body)

    def get_snapshot_uri(self, body: str) -> str:
        uri = f"http://{self.config.local_ip}:{self.config.web_port}/snapshot.jpg"
        body = self._render('get_snapshot_uri', snapshot_uri=uri)
        return self._wrap_envelope(body)

    def get_video_encoder_configuration(self) -> str:
        body = self._render('get_video_encoder_configuration',
            main_width=self.config.main_width,
            main_height=self.config.main_height,
            main_fps=self.config.main_fps,
            main_bitrate_kbps=self._bitrate_to_kbps(self.config.main_bitrate))
        return self._wrap_envelope(body)

    def get_video_source_configuration(self) -> str:
        body = self._render('get_video_source_configuration',
            main_width=self.config.main_width,
            main_height=self.config.main_height)
        return self._wrap_envelope(body)

    def get_audio_decoder_configurations(self) -> str:
        body = self._render('get_audio_decoder_configurations')
        return self._wrap_envelope(body)

    def create_probe_match(self, relates_to: str) -> str:
        """Create WS-Discovery ProbeMatch response"""
        message_id = f"urn:uuid:{uuid.uuid4()}"
        return self._render('probe_match',
            message_id=message_id,
            relates_to=relates_to,
            device_uuid=self.device_uuid,
            camera_name=self.config.name,
            onvif_url=self.config.onvif_url)

    # === PTZ Service Handlers ===
    
    def _extract_xml_value(self, body: str, tag: str) -> Optional[str]:
        """Extract value from XML tag, handling namespaces"""
        # Match with or without namespace prefix
        pattern = rf'<(?:\w+:)?{tag}[^>]*>([^<]*)</(?:\w+:)?{tag}>'
        match = re.search(pattern, body)
        return match.group(1) if match else None
    
    def _extract_xml_attr(self, body: str, tag: str, attr: str) -> Optional[str]:
        """Extract attribute value from XML tag"""
        pattern = rf'<(?:\w+:)?{tag}[^>]*{attr}="([^"]*)"'
        match = re.search(pattern, body)
        return match.group(1) if match else None
    
    def _extract_velocity(self, body: str) -> tuple:
        """Extract pan, tilt, zoom velocity from SOAP body"""
        pan_speed = 0.0
        tilt_speed = 0.0
        zoom_speed = 0.0
        
        # Look for PanTilt x="..." y="..."
        pt_match = re.search(r'<(?:\w+:)?PanTilt[^>]*x="([^"]*)"[^>]*y="([^"]*)"', body)
        if pt_match:
            try:
                pan_speed = float(pt_match.group(1))
                tilt_speed = float(pt_match.group(2))
            except ValueError:
                pass
        
        # Look for Zoom x="..."
        zoom_match = re.search(r'<(?:\w+:)?Zoom[^>]*x="([^"]*)"', body)
        if zoom_match:
            try:
                zoom_speed = float(zoom_match.group(1))
            except ValueError:
                pass
        
        return pan_speed, tilt_speed, zoom_speed
    
    def _extract_position(self, body: str) -> tuple:
        """Extract pan, tilt, zoom position from SOAP body"""
        pan = None
        tilt = None
        zoom = None
        
        # Look for Position > PanTilt x="..." y="..."
        pt_match = re.search(r'<(?:\w+:)?Position[^>]*>.*?<(?:\w+:)?PanTilt[^>]*x="([^"]*)"[^>]*y="([^"]*)"', body, re.DOTALL)
        if pt_match:
            try:
                pan = float(pt_match.group(1))
                tilt = float(pt_match.group(2))
            except ValueError:
                pass
        
        # Look for Position > Zoom x="..."
        zoom_match = re.search(r'<(?:\w+:)?Position[^>]*>.*?<(?:\w+:)?Zoom[^>]*x="([^"]*)"', body, re.DOTALL)
        if zoom_match:
            try:
                zoom = float(zoom_match.group(1))
            except ValueError:
                pass
        
        return pan, tilt, zoom
    
    def _extract_translation(self, body: str) -> tuple:
        """Extract pan, tilt, zoom translation from SOAP body"""
        pan = 0.0
        tilt = 0.0
        zoom = 0.0
        
        # Look for Translation > PanTilt x="..." y="..."
        pt_match = re.search(r'<(?:\w+:)?Translation[^>]*>.*?<(?:\w+:)?PanTilt[^>]*x="([^"]*)"[^>]*y="([^"]*)"', body, re.DOTALL)
        if pt_match:
            try:
                pan = float(pt_match.group(1))
                tilt = float(pt_match.group(2))
            except ValueError:
                pass
        
        # Look for Translation > Zoom x="..."
        zoom_match = re.search(r'<(?:\w+:)?Translation[^>]*>.*?<(?:\w+:)?Zoom[^>]*x="([^"]*)"', body, re.DOTALL)
        if zoom_match:
            try:
                zoom = float(zoom_match.group(1))
            except ValueError:
                pass
        
        return pan, tilt, zoom

    def ptz_get_nodes(self) -> str:
        """Handle GetNodes request - returns list of PTZ nodes"""
        body = self._render('ptz_get_nodes')
        return self._wrap_envelope(body)

    def ptz_get_node(self) -> str:
        """Handle GetNode request - returns single PTZ node details"""
        body = self._render('ptz_get_node')
        return self._wrap_envelope(body)

    def ptz_get_service_capabilities(self) -> str:
        """Handle GetServiceCapabilities request for PTZ service"""
        body = self._render('ptz_get_service_capabilities')
        return self._wrap_envelope(body)

    def ptz_get_configurations(self) -> str:
        """Handle GetConfigurations request"""
        body = self._render('ptz_get_configurations')
        return self._wrap_envelope(body)
    
    def ptz_get_status(self, body: str) -> str:
        """Handle GetStatus request"""
        if self.ptz:
            status = self.ptz.get_status()
            pan = status['pan']
            tilt = status['tilt']
            zoom = status['zoom']
            moving = 'MOVING' if status['moving'] else 'IDLE'
        else:
            pan, tilt, zoom = 0.0, 0.0, 0.0
            moving = 'IDLE'
        
        response = self._render('ptz_get_status',
            pan=pan, tilt=tilt, zoom=zoom, move_status=moving)
        return self._wrap_envelope(response)
    
    def ptz_continuous_move(self, body: str) -> str:
        """Handle ContinuousMove request"""
        pan_speed, tilt_speed, zoom_speed = self._extract_velocity(body)
        
        if self.ptz:
            self.ptz.continuous_move(pan_speed, tilt_speed, zoom_speed)
        
        response = self._render('ptz_continuous_move')
        return self._wrap_envelope(response)
    
    def ptz_stop(self, body: str) -> str:
        """Handle Stop request"""
        # Check for PanTilt and Zoom stop flags
        pan_tilt = True
        zoom = True
        
        pt_stop = self._extract_xml_value(body, 'PanTilt')
        if pt_stop and pt_stop.lower() == 'false':
            pan_tilt = False
        
        zoom_stop = self._extract_xml_value(body, 'Zoom')
        if zoom_stop and zoom_stop.lower() == 'false':
            zoom = False
        
        if self.ptz:
            self.ptz.stop_movement(pan_tilt, zoom)
        
        response = self._render('ptz_stop')
        return self._wrap_envelope(response)
    
    def ptz_absolute_move(self, body: str) -> str:
        """Handle AbsoluteMove request"""
        pan, tilt, zoom = self._extract_position(body)
        
        if self.ptz:
            self.ptz.absolute_move(pan, tilt, zoom)
        
        response = self._render('ptz_absolute_move')
        return self._wrap_envelope(response)
    
    def ptz_relative_move(self, body: str) -> str:
        """Handle RelativeMove request"""
        pan, tilt, zoom = self._extract_translation(body)
        
        if self.ptz:
            self.ptz.relative_move(pan, tilt, zoom)
        
        response = self._render('ptz_relative_move')
        return self._wrap_envelope(response)
    
    def ptz_goto_home(self, body: str) -> str:
        """Handle GotoHomePosition request"""
        if self.ptz:
            self.ptz.goto_home()
        
        response = self._render('ptz_goto_home')
        return self._wrap_envelope(response)
    
    def ptz_get_presets(self, body: str) -> str:
        """Handle GetPresets request"""
        preset_items = ""
        
        if self.ptz:
            presets = self.ptz.get_presets()
            for token, preset in presets.items():
                preset_items += f"""
      <tptz:Preset token="{preset.token}">
        <tt:Name>{preset.name}</tt:Name>
        <tt:PTZPosition>
          <tt:PanTilt x="{preset.pan}" y="{preset.tilt}"/>
          <tt:Zoom x="{preset.zoom}"/>
        </tt:PTZPosition>
      </tptz:Preset>"""
        
        response = self._render('ptz_get_presets', presets=preset_items)
        return self._wrap_envelope(response)
    
    def ptz_set_preset(self, body: str) -> str:
        """Handle SetPreset request"""
        preset_name = self._extract_xml_value(body, 'PresetName') or 'Preset'
        preset_token = self._extract_xml_attr(body, 'SetPreset', 'PresetToken')
        
        if not preset_token:
            # Generate new token
            preset_token = f"preset_{uuid.uuid4().hex[:8]}"
        
        if self.ptz:
            self.ptz.set_preset(preset_token, preset_name)
        
        response = self._render('ptz_set_preset', preset_token=preset_token)
        return self._wrap_envelope(response)
    
    def ptz_goto_preset(self, body: str) -> str:
        """Handle GotoPreset request"""
        preset_token = self._extract_xml_value(body, 'PresetToken')
        
        if self.ptz and preset_token:
            self.ptz.goto_preset(preset_token)
        
        response = self._render('ptz_goto_preset')
        return self._wrap_envelope(response)
