#!/usr/bin/env python3
"""ONVIF Device and Media Service handler"""

import os
import time
import uuid
from typing import Dict, Optional
from camera_config import CameraConfig


class ONVIFService:
    """ONVIF Device and Media Service handler"""
    
    def __init__(self, config: CameraConfig):
        self.config = config
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
            'GetProfiles': self.get_profiles,
            'GetStreamUri': lambda: self.get_stream_uri(body),
            'GetSnapshotUri': lambda: self.get_snapshot_uri(body),
            'GetVideoEncoderConfiguration': self.get_video_encoder_configuration,
            'GetVideoSourceConfiguration': self.get_video_source_configuration,
            'GetAudioDecoderConfigurations': self.get_audio_decoder_configurations,
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
        body = self._render('get_capabilities', device_url=device_url, media_url=media_url)
        return self._wrap_envelope(body)

    def get_services(self) -> str:
        device_url = f"http://{self.config.local_ip}:{self.config.onvif_port}/onvif/device_service"
        media_url = f"http://{self.config.local_ip}:{self.config.onvif_port}/onvif/media_service"
        body = self._render('get_services', device_url=device_url, media_url=media_url)
        return self._wrap_envelope(body)

    def get_scopes(self) -> str:
        body = self._render('get_scopes', camera_name=self.config.name)
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
