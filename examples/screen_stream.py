#!/usr/bin/env python3
"""
Basic Screen Capture Example

Demonstrates the simplest way to use IPyCam with a screen capture.
"""

import time
import cv2
from ipycam import IPCamera, CameraConfig
from frame_source import FrameSourceFactory


def main():
    
    # Open screen cap
    cap = FrameSourceFactory.create(
        'screen', 
        x=0, 
        y=0, 
        w=3840, 
        h=2160, 
        fps=30, 
        threaded=True
    )

    if cap.connect():
        cap.start_async()
        print("Screen capture connected successfully.")
        print(f"Frame size: {cap.get_frame_size()}")
    
    wait_for_frame = True
    while wait_for_frame:
        ret, frame = cap.read()
        if frame is not None:
            print(f"Captured frame of shape: {frame.shape}")
            wait_for_frame = False

    config = CameraConfig.load("camera_config.json")
    # Set source info for the UI
    config.source_type = "screen"
    config.source_info = "Screen Capture (3840x2160 @ 30fps)"
    
    # Create camera with default settings
    virt_ip_camera = IPCamera(config=config)
    
    if not virt_ip_camera.start():
        print("Failed to start camera")
        return
    
    print(f"\n{'='*60}")
    print(f"  IPyCam is running!")
    print(f"{'='*60}")
    print(f"  Web UI:      http://{virt_ip_camera.config.local_ip}:{virt_ip_camera.config.onvif_port}/")
    print(f"  RTSP Stream: {virt_ip_camera.config.main_stream_rtsp}")
    print(f"  ONVIF:       {virt_ip_camera.config.onvif_url}")
    print(f"{'='*60}\n")
    print("Press Ctrl+C to stop\n")


    if not cap.is_connected:
        print("Error: Could not open screen capture")
        virt_ip_camera.stop()
        return
            
    try:
        while virt_ip_camera.is_running:
            ret, frame = cap.read()
            
            if not ret:
                print("Error: Failed to read frame from screen capture")
                break
            
            # Stream the frame (handles PTZ, timestamp, and pacing automatically)
            virt_ip_camera.stream(frame)

    
    except KeyboardInterrupt:
        print("\nShutting down...")
    
    finally:
        cap.release()
        virt_ip_camera.stop()
        print("Done!")


if __name__ == "__main__":
    main()
