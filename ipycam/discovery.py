#!/usr/bin/env python3
"""WS-Discovery server for ONVIF device discovery"""

import socket
import struct
import threading
import re
import uuid
import logging

# Handle both package and direct execution
try:
    from .onvif import ONVIFService
except ImportError:
    from onvif import ONVIFService

logger = logging.getLogger(__name__)


class WSDiscoveryServer(threading.Thread):
    """WS-Discovery server for ONVIF device discovery"""

    MULTICAST_ADDR = '239.255.255.250'
    MULTICAST_PORT = 3702
    # Action URI used by genuine WS-Discovery Probe *requests* (as opposed to
    # the ProbeMatch *response* action, which also contains the substring
    # "Probe" and must not be treated as an incoming probe).
    PROBE_ACTION_URI = 'http://schemas.xmlsoap.org/ws/2005/04/discovery/Probe'

    def __init__(self, onvif_service: ONVIFService):
        super().__init__(daemon=True)
        self.onvif = onvif_service
        self.running = True
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(('', self.MULTICAST_PORT))
        # Wake up periodically so the run loop can notice stop() even while
        # blocked waiting for a datagram.
        self.sock.settimeout(1.0)

        # Join multicast group
        mreq = struct.pack("4s4s",
                          socket.inet_aton(self.MULTICAST_ADDR),
                          socket.inet_aton(self.onvif.config.local_ip))
        try:
            self.sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
        except Exception:
            mreq = struct.pack("4sl", socket.inet_aton(self.MULTICAST_ADDR), socket.INADDR_ANY)
            self.sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)

    def _is_probe_request(self, msg: str) -> bool:
        """Return True only for genuine WS-Discovery Probe requests.

        This deliberately excludes ProbeMatch responses broadcast by other
        cameras/devices on the same multicast segment (which also contain
        the substring "Probe"), and excludes any unrelated payload that
        merely happens to contain the word "Probe" somewhere.
        """
        if 'ProbeMatch' in msg:
            return False
        if self.PROBE_ACTION_URI in msg:
            return True
        # Fall back to looking for an actual <Probe> element, tolerating any
        # (or no) namespace prefix, e.g. <d:Probe/>, <wsd:Probe>, <Probe>.
        return bool(re.search(r'<(?:[\w.-]+:)?Probe[\s/>]', msg))

    def _build_announcement(self, action: str) -> str:
        """Build a WS-Discovery Hello/Bye SOAP message.

        Reuses the same endpoint/scope/address values that
        ONVIFService.create_probe_match() uses for ProbeMatch responses.
        """
        message_id = f"urn:uuid:{uuid.uuid4()}"
        return (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<soap:Envelope xmlns:soap="http://www.w3.org/2003/05/soap-envelope" '
            'xmlns:wsa="http://schemas.xmlsoap.org/ws/2004/08/addressing" '
            'xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery" '
            'xmlns:dn="http://www.onvif.org/ver10/network/wsdl" '
            'xmlns:tds="http://www.onvif.org/ver10/device/wsdl">'
            '<soap:Header>'
            '<wsa:To>urn:schemas-xmlsoap-org:ws:2005:04:discovery</wsa:To>'
            f'<wsa:Action>http://schemas.xmlsoap.org/ws/2005/04/discovery/{action}</wsa:Action>'
            f'<wsa:MessageID>{message_id}</wsa:MessageID>'
            '</soap:Header>'
            '<soap:Body>'
            f'<d:{action}>'
            f'<d:EndpointReference><wsa:Address>{self.onvif.device_uuid}</wsa:Address></d:EndpointReference>'
            '<d:Types>dn:NetworkVideoTransmitter tds:Device</d:Types>'
            f'<d:Scopes>onvif://www.onvif.org/type/video_encoder onvif://www.onvif.org/Profile/Streaming '
            f'onvif://www.onvif.org/name/{self.onvif.config.name}</d:Scopes>'
            f'<d:XAddrs>{self.onvif.config.onvif_url}</d:XAddrs>'
            '<d:MetadataVersion>1</d:MetadataVersion>'
            f'</d:{action}>'
            '</soap:Body>'
            '</soap:Envelope>'
        )

    def _send_announcement(self, action: str) -> None:
        """Best-effort multicast of a Hello/Bye announcement.

        Never raises - failures here shouldn't prevent the run loop from
        starting or stop() from tearing down the socket.
        """
        sock = self.sock
        if sock is None:
            return
        try:
            message = self._build_announcement(action)
            sock.sendto(message.encode('utf-8'), (self.MULTICAST_ADDR, self.MULTICAST_PORT))
        except Exception as e:
            logger.warning(f"Discovery {action} announcement failed: {e}")

    def run(self):
        # Announce presence proactively so clients can discover the device
        # passively, not only in response to an active Probe.
        self._send_announcement('Hello')
        while self.running:
            try:
                data, addr = self.sock.recvfrom(4096)
            except socket.timeout:
                # Just here to re-check self.running periodically.
                continue
            except Exception:
                # Most likely the socket was closed by stop(). Exit quietly
                # if that's expected, otherwise report the unexpected error.
                if self.running:
                    logger.error("Discovery error: socket unavailable")
                break

            try:
                # Never let a malformed/spoofed datagram raise or spam stderr.
                msg = data.decode('utf-8', errors='ignore')
                if self._is_probe_request(msg):
                    # Extract MessageID
                    match = re.search(r'MessageID>(.+?)</', msg)
                    relates_to = match.group(1) if match else f"urn:uuid:{uuid.uuid4()}"
                    response = self.onvif.create_probe_match(relates_to)
                    self.sock.sendto(response.encode('utf-8'), addr)
            except Exception as e:
                if self.running:
                    logger.error(f"Discovery error: {e}")

    def stop(self):
        self.running = False
        self._send_announcement('Bye')
        # Swap-and-clear so a repeated stop() call is a no-op instead of a
        # double-close, and so the run loop stops touching the socket once
        # it's gone.
        sock, self.sock = self.sock, None
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass


if __name__ == "__main__":
    from config import CameraConfig
    from onvif import ONVIFService
    from ptz import PTZController

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(name)s %(levelname)s: %(message)s")

    config = CameraConfig.load()

    # Create PTZ controller (required by ONVIFService)
    ptz = PTZController(
        output_width=config.main_width,
        output_height=config.main_height,
        max_zoom=4.0
    )

    onvif_service = ONVIFService(config, ptz)
    discovery_server = WSDiscoveryServer(onvif_service)
    discovery_server.start()

    logger.info("WS-Discovery server running. Press Ctrl+C to stop.")
    logger.info(f"Listening on 239.255.255.250:3702")
    logger.info(f"Camera endpoint: {config.onvif_url}")
    try:
        while True:
            pass
    except KeyboardInterrupt:
        logger.info("Stopping WS-Discovery server...")
        discovery_server.stop()
        ptz.stop()
