#!/usr/bin/env python3
"""
IPyCam - Run as a module

Usage:
    python -m ipycam

This starts the IP camera with default settings, using the webcam as input.
"""

import time
import cv2

from . import IPCamera, CameraConfig


def main():
    # Load config from file, or use defaults if not found
    config = CameraConfig.load("camera_config.json")
    print(f"Loaded config: {config.name} ({config.main_width}x{config.main_height}@{config.main_fps}fps)")
    
    camera = IPCamera(config)
    
    if not camera.start():
        print("Failed to start camera")
        return 1
    
    print("\n" + "="*50)
    print("IP Camera is running!")
    print("="*50)
    print(f"\nOpen Web UI: http://{config.local_ip}:{config.onvif_port}/")
    print("Press Ctrl+C to stop\n")
    
    # Open webcam as default test source
    cap = cv2.VideoCapture(0)
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, 1920)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 1080)
    
    if not cap.isOpened():
        print("Could not open webcam")
        camera.stop()
        return 1
    
    try:
        while camera.is_running:
            ret, frame = cap.read()

            if not ret:
                cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                continue
            
            # Stream handles PTZ, timestamp, and frame pacing automatically
            camera.stream(frame)
                
    except KeyboardInterrupt:
        print("\nShutting down...")
    finally:
        cap.release()
        camera.stop()
    
    return 0


if __name__ == "__main__":
    exit(main())
