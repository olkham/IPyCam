#!/usr/bin/env python3
"""
Basic Webcam Example

Demonstrates the simplest way to use IPyCam with a webcam.
"""

import time
import cv2
from ipycam import IPCamera, CameraConfig


def main():
    
    config = CameraConfig.load("camera_config.json")
    # Create camera with default settings
    camera = IPCamera(config=config)
    
    if not camera.start():
        print("Failed to start camera")
        return
    
    print(f"\n{'='*60}")
    print(f"  IPyCam is running!")
    print(f"{'='*60}")
    print(f"  Web UI:      http://{camera.config.local_ip}:{camera.config.onvif_port}/")
    print(f"  RTSP Stream: {camera.config.main_stream_rtsp}")
    print(f"  ONVIF:       {camera.config.onvif_url}")
    print(f"{'='*60}\n")
    print("Press Ctrl+C to stop\n")
    
    # Open webcam
    cap = cv2.VideoCapture(0)
    
    if not cap.isOpened():
        print("Error: Could not open webcam")
        camera.stop()
        return
    
    # Set webcam resolution (optional)
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, 1920)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 1080)
    
    frame_count = 0
    
    try:
        while camera.is_running:
            ret, frame = cap.read()
            
            if not ret:
                print("Error: Failed to read frame from webcam")
                break
            
            # Stream the frame (handles PTZ, timestamp, and pacing automatically)
            camera.stream(frame)
            frame_count += 1
            
            # Print stats every 5 seconds
            if frame_count % (camera.config.main_fps * 5) == 0:
                if camera.stats:
                    print(f"Stats: {camera.stats.frames_sent} frames, "
                          f"{camera.stats.actual_fps:.1f} fps, "
                          f"{camera.stats.bitrate_mbps:.2f} Mbps")
    
    except KeyboardInterrupt:
        print("\nShutting down...")
    
    finally:
        cap.release()
        camera.stop()
        print("Done!")


if __name__ == "__main__":
    main()
