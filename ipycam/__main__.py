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
    
    start_time = time.time()
    frame_count = 0
    last_fps = camera.config.main_fps
    
    try:
        while camera.is_running:
            ret, frame = cap.read()

            if not ret:
                cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                continue
            
            # Add timestamp
            timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
            cv2.putText(frame, timestamp, (10, 30),
                       cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 255), 2)
            
            camera.stream(frame)
            frame_count += 1
            
            # Check if FPS changed - reset timing
            if camera.config.main_fps != last_fps:
                last_fps = camera.config.main_fps
                start_time = time.time()
                frame_count = 1
            
            # Precise frame pacing (read FPS dynamically)
            target_frame_time = 1.0 / camera.config.main_fps
            expected_time = start_time + (frame_count * target_frame_time)
            sleep_time = expected_time - time.time()
            if sleep_time > 0:
                time.sleep(sleep_time)
                
    except KeyboardInterrupt:
        print("\nShutting down...")
    finally:
        cap.release()
        camera.stop()
    
    return 0


if __name__ == "__main__":
    exit(main())
