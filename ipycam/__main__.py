#!/usr/bin/env python3
"""
IPyCam - Run as a module

Usage:
    python -m ipycam [options]

Examples:
    python -m ipycam
    python -m ipycam --config camera_config.json
    python -m ipycam --camera 0
    python -m ipycam --camera rtsp://192.168.1.100/stream
    python -m ipycam --no-timestamp
"""

import argparse
import cv2
from . import IPCamera, CameraConfig


def main():
    parser = argparse.ArgumentParser(
        prog='ipycam',
        description='IPyCam - Pure Python Virtual IP Camera',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python -m ipycam                              # Use webcam with default config
  python -m ipycam --config custom.json         # Use custom config file
  python -m ipycam --camera 1                   # Use second webcam
  python -m ipycam --camera video.mp4           # Stream from video file
  python -m ipycam --camera rtsp://...          # Stream from RTSP source
  python -m ipycam --no-timestamp               # Disable timestamp overlay
  python -m ipycam --width 1280 --height 720    # Override resolution
  python -m ipycam --fps 60                     # Override FPS
        """
    )
    
    parser.add_argument(
        '--config', '-c',
        type=str,
        default='camera_config.json',
        help='Path to config file (default: camera_config.json)'
    )
    
    parser.add_argument(
        '--camera',
        type=str,
        default='0',
        help='Camera source: device index (0), file path, or URL (default: 0)'
    )
    
    parser.add_argument(
        '--width',
        type=int,
        help='Override frame width'
    )
    
    parser.add_argument(
        '--height',
        type=int,
        help='Override frame height'
    )
    
    parser.add_argument(
        '--fps',
        type=int,
        help='Override target FPS'
    )
    
    parser.add_argument(
        '--no-timestamp',
        action='store_true',
        help='Disable timestamp overlay'
    )
    
    parser.add_argument(
        '--timestamp-position',
        choices=['top-left', 'top-right', 'bottom-left', 'bottom-right'],
        help='Timestamp position'
    )
    
    args = parser.parse_args()
    
    # Load config from file, or use defaults if not found
    config = CameraConfig.load(args.config)
    
    # Apply command-line overrides
    if args.width:
        config.main_width = args.width
    if args.height:
        config.main_height = args.height
    if args.fps:
        config.main_fps = args.fps
    if args.no_timestamp:
        config.show_timestamp = False
    if args.timestamp_position:
        config.timestamp_position = args.timestamp_position
    
    print(f"Loaded config: {config.name} ({config.main_width}x{config.main_height}@{config.main_fps}fps)")
    
    camera = IPCamera(config)
    
    if not camera.start():
        print("Failed to start camera")
        return 1
    
    print("\n" + "="*50)
    print("IP Camera is running!")
    print("="*50)
    print(f"\nOpen Web UI: http://{config.local_ip}:{config.onvif_port}/")
    print(f"Snapshot URL: http://{config.local_ip}:{config.onvif_port}/snapshot.jpg")
    print("Press Ctrl+C to stop\n")
    
    # Open camera source
    # Try to parse as int (device index), otherwise treat as path/URL
    try:
        camera_source = int(args.camera)
    except ValueError:
        camera_source = args.camera
    
    print(f"Opening camera source: {camera_source}")
    cap = cv2.VideoCapture(camera_source)
    
    # Set resolution if using webcam (device index)
    if isinstance(camera_source, int):
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, config.main_width)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, config.main_height)
    
    if not cap.isOpened():
        print(f"Error: Could not open camera source: {camera_source}")
        camera.stop()
        return 1
    
    try:
        while camera.is_running:
            ret, frame = cap.read()

            if not ret:
                # Loop video files, exit on camera failure
                if isinstance(camera_source, str):
                    cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                    continue
                else:
                    print("Error: Failed to read from camera")
                    break
            
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
