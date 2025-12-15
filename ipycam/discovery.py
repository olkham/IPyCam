#!/usr/bin/env python3
"""WS-Discovery server for ONVIF device discovery"""

import socket
import struct
import threading
import re
import uuid

from .onvif import ONVIFService


class WSDiscoveryServer(threading.Thread):
    """WS-Discovery server for ONVIF device discovery"""
    
    def __init__(self, onvif_service: ONVIFService):
        super().__init__(daemon=True)
        self.onvif = onvif_service
        self.running = True
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(('', 3702))
        
        # Join multicast group
        mreq = struct.pack("4s4s", 
                          socket.inet_aton('239.255.255.250'),
                          socket.inet_aton(self.onvif.config.local_ip))
        try:
            self.sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
        except Exception:
            mreq = struct.pack("4sl", socket.inet_aton('239.255.255.250'), socket.INADDR_ANY)
            self.sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
    
    def run(self):
        while self.running:
            try:
                data, addr = self.sock.recvfrom(4096)
                msg = data.decode('utf-8')
                if 'Probe' in msg:
                    # Extract MessageID
                    match = re.search(r'MessageID>(.+?)</', msg)
                    relates_to = match.group(1) if match else f"urn:uuid:{uuid.uuid4()}"
                    response = self.onvif.create_probe_match(relates_to)
                    self.sock.sendto(response.encode('utf-8'), addr)
            except Exception as e:
                if self.running:
                    print(f"Discovery error: {e}")
    
    def stop(self):
        self.running = False
