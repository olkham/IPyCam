#!/usr/bin/env python3
"""
Custom Configuration Example

Shows how to customize camera settings and use configuration files.
"""

import time
import cv2
from ipycam import IPCamera, CameraConfig


def main():
    # Create custom configuration
    config = CameraConfig(
        name="My Custom Camera",
        manufacturer="MyCompany",
        model="CustomCam-Pro",
        serial_number="CC-12345",
        
        # Source info for UI display
        source_type="camera",
        source_info="Camera Index 0",
        
        # Stream settings
        main_width=1280,
        main_height=720,
        main_fps=30,
        main_bitrate="2M",
        
        # Substream settings
        sub_width=640,
        sub_height=360,
        sub_bitrate="512K",
        
        # Hardware acceleration
        hw_accel="auto",  # Options: "auto", "nvenc", "qsv", "cpu"
        
        # Network ports (optional, uses defaults if not specified)
        # onvif_port=8080,
        # rtsp_port=8554,
        # web_port=8081,
    )
    
    # Save configuration for future use
    config.save("my_camera_config.json")
    print(f"Saved configuration to my_camera_config.json")
    
    # Or load existing config:
    # config = CameraConfig.load("my_camera_config.json")
    
    # Create camera with custom config
    camera = IPCamera(config)
    
    if not camera.start():
        print("Failed to start camera")
        return
    
    print(f"\n{'='*60}")
    print(f"  Camera: {config.name}")
    print(f"  Resolution: {config.main_width}x{config.main_height} @ {config.main_fps}fps")
    print(f"  Bitrate: {config.main_bitrate}")
    print(f"  Hardware: {config.hw_accel}")
    print(f"{'='*60}")
    print(f"  Web UI:      http://{config.local_ip}:{config.onvif_port}/")
    print(f"  Main Stream: {config.main_stream_rtsp}")
    print(f"  Sub Stream:  {config.sub_stream_rtsp}")
    print(f"{'='*60}\n")
    
    # Open video source
    cap = cv2.VideoCapture(0)
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, config.main_width)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, config.main_height)
    
    if not cap.isOpened():
        print("Error: Could not open video source")
        camera.stop()
        return
    
    frame_count = 0
    
    try:
        while camera.is_running:
            ret, frame = cap.read()
            if not ret:
                break
            
            # Add custom overlay
            cv2.putText(frame, f"{config.name}", (10, 30),
                       cv2.FONT_HERSHEY_SIMPLEX, 1.0, (0, 255, 0), 2)
            
            # Stream handles PTZ, timestamp, and frame pacing automatically
            camera.stream(frame)
            frame_count += 1
    
    except KeyboardInterrupt:
        print("\nShutting down...")
    
    finally:
        cap.release()
        camera.stop()


if __name__ == "__main__":
    main()
