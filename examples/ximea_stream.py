#!/usr/bin/env python3
"""
Basic Webcam Example

Demonstrates the simplest way to use IPyCam with a webcam.
"""

import time
import cv2
from ipycam import IPCamera, CameraConfig
from frame_source import FrameSourceFactory


def main():
    
    # Open webcam
    # cap = cv2.VideoCapture(0)
    cap = FrameSourceFactory.create('ximea', threaded=True, is_mono=True)

    if cap.connect():
        cap.start_async()
        print("Ximea connected successfully.")
        print(f"Exposure: {cap.get_exposure()}")
        print(f"Gain: {cap.get_gain()}")
        print(f"Frame size: {cap.get_frame_size()}")
    

    for _ in range(10):
        ret, frame = cap.read()
        if ret or frame is not None:
            print(f"Captured frame of shape: {frame.shape}")
            # cv2.imshow("Ximea", frame) # type: ignore
            # if cv2.waitKey(1) & 0xFF == ord('q'):
                # break

    config = CameraConfig.load("camera_config.json")
    # Set source info for the UI
    config.source_type = "camera"
    config.source_info = "Camera Index 0"
    
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
        print("Error: Could not open Ximea camera")
        virt_ip_camera.stop()
        return
            
    try:
        while virt_ip_camera.is_running:
            ret, frame = cap.read()
            
            if not ret:
                print("Error: Failed to read frame from Ximea camera")
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
