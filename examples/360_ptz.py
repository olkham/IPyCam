#!/usr/bin/env python3
"""
360° PTZ Virtual Camera Example

Demonstrates using a 360° equirectangular video as source material for a 
virtual PTZ camera. ONVIF PTZ commands control the viewing angle within 
the 360° sphere, and the equirectangular projection is converted to a 
standard pinhole camera view for streaming.

This creates a virtual PTZ camera from any 360° video without physical motors.
"""

import cv2
import numpy as np
from typing import Tuple
from ipycam import IPCamera, CameraConfig

from frame_source import FrameSourceFactory
from frame_processors.equirectangular360_processor import Equirectangular2PinholeProcessor

def map_value(value: float, from_min: float, from_max: float, to_min: float, to_max: float) -> float:
    """Map a value from one range to another"""
    from_range = from_max - from_min
    to_range = to_max - to_min
    scaled_value = (value - from_min) / from_range
    return to_min + (scaled_value * to_range)

def main():
    """Stream 360° video as virtual PTZ camera"""
    
    # Load configuration
    config = CameraConfig.load("camera_config.json")
    config.source_type = "camera"
    config.source_info = "360° Camera (Webcam Index 1)"
    
    # Create IPyCam instance
    ipycamera = IPCamera(config=config)
    
    # Disable digital PTZ - we're doing our own equirectangular projection
    ipycamera.ptz.enable_digital_ptz = False
    ipycamera.ptz.wrap_pan = True  # Wrap around for 360° panning
    
    if not ipycamera.start():
        print("Failed to start camera")
        return
    
    print(f"\n{'='*60}")
    print(f"  360° Virtual PTZ Camera")
    print(f"{'='*60}")
    print(f"  Web UI: http://{config.local_ip}:{config.onvif_port}/")
    print(f"  RTSP:   {config.main_stream_rtsp}")
    print(f"{'='*60}")
    print("\nUse the web UI PTZ controls to look around the 360° video.")
    print("Pan/Tilt control viewing direction, Zoom adjusts FOV.")
    print("\nPress Ctrl+C to stop\n")
    
    # Open 360° video file
    # video_path = "birds.mp4"
    # cap = FrameSourceFactory.create('video_file', source=video_path, threaded=False, loop=True, connect=True)
    cap = FrameSourceFactory.create('webcam', source=1, threaded=False, loop=True, connect=True)
    
    # Set camera resolution for Insta360 X5 webcam mode
    cap.set_frame_size(2880, 1440)
    cap.set_fps(30)
    
    if not cap.isOpened():
        print(f"Error: Could not open 360° source")
        ipycamera.stop()
        return
   
    # Create and attach equirectangular processor
    processor = Equirectangular2PinholeProcessor(
        output_width=1920,
        output_height=1080)
    
    ipycamera.ptz.state.zoom = 0.3
    
    processor.set_parameter('pitch', 0.0)
    processor.set_parameter('yaw', 0.0)
    processor.set_parameter('roll', 0.0)
    processor.set_parameter('fov', 90.0)  # Initial FOV
        
    try:
        while ipycamera.is_running:
            ret, equirect_frame = cap.read()
            
            if not ret:
                print("Error: Failed to read frame from video")
                break
            
            # Get current PTZ state           
            processor.set_parameter('yaw', map_value(ipycamera.ptz.state.pan, -1.0, 1.0, -180.0, 180.0))
            processor.set_parameter('pitch', map_value(ipycamera.ptz.state.tilt, -1.0, 1.0, -90.0, 90.0))
            processor.set_parameter('fov', map_value(ipycamera.ptz.state.zoom, 0.0, 1.0, 150.0, 20.0))
            # Zoom control for FOV can be added similarly if needed
            projected = processor.process(equirect_frame)
            
            # Stream handles PTZ overlay (status), timestamp, and frame pacing
            ipycamera.stream(projected)
    
    except KeyboardInterrupt:
        print("\nShutting down...")
    
    finally:
        cap.release()
        ipycamera.stop()


if __name__ == "__main__":
    main()
