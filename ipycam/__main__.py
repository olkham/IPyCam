#!/usr/bin/env python3
"""
IPyCam - Run as a module

Usage:
    python -m ipycam [options]

Examples:
    python -m ipycam
    python -m ipycam --config camera_config.json
    python -m ipycam --source 0
    python -m ipycam --source rtsp://192.168.1.100/stream
    python -m ipycam --no-timestamp
"""

import argparse
import os
import platform
import cv2
from . import IPCamera, CameraConfig


def infer_source_type(source_arg: str) -> tuple[str, str]:
    """
    Infer source_type and source_info from the camera argument.
    
    Returns:
        tuple of (source_type, source_info)
    """
    # Special case: 'video' without a file path means video upload mode
    if source_arg.lower() == 'video':
        return ("video_file", "Waiting for upload...")
    
    # Try to parse as integer (webcam index)
    try:
        index = int(source_arg)
        return ("camera", f"Camera Index {index}")
    except ValueError:
        pass
    
    # Check if it's a URL
    source_lower = source_arg.lower()
    if source_lower.startswith(('rtsp://', 'rtmp://', 'http://', 'https://')):
        return ("rtsp", source_arg)
    
    # Check if it's a file
    if os.path.isfile(source_arg):
        filename = os.path.basename(source_arg)
        return ("video_file", filename)
    
    # Could be a file path that doesn't exist yet, or other source
    # Check by extension
    _, ext = os.path.splitext(source_arg)
    video_extensions = {'.mp4', '.avi', '.mkv', '.mov', '.wmv', '.flv', '.webm', '.m4v', '.mpeg', '.mpg', '.3gp'}
    if ext.lower() in video_extensions:
        filename = os.path.basename(source_arg)
        return ("video_file", filename)
    
    # Default to custom if we can't determine
    return ("custom", source_arg)


def main():
    parser = argparse.ArgumentParser(
        prog='ipycam',
        description='IPyCam - Pure Python Virtual IP Camera',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python -m ipycam                              # Use webcam with default config
  python -m ipycam --config custom.json         # Use custom config file
  python -m ipycam --source 1                   # Use second webcam
  python -m ipycam --source video.mp4           # Stream from video file
  python -m ipycam --source rtsp://...          # Stream from RTSP source
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
        '--source',
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
    
    parser.add_argument(
        '--hw',
        choices=['auto', 'nvenc', 'qsv', 'cpu'],
        default=None,
        help='Hardware acceleration: auto, nvenc, qsv, or cpu'
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
    if args.hw:
        config.hw_accel = args.hw
    
    # Infer and set source type from source argument
    source_type, source_info = infer_source_type(args.source)
    config.source_type = source_type
    config.source_info = source_info
    
    print(f"Loaded config: {config.name} ({config.main_width}x{config.main_height}@{config.main_fps}fps)")
    
    camera = IPCamera(config)
    
    if not camera.start():
        print("Failed to start camera")
        return 1
    
    print("\n" + "="*50)
    print("IP Camera is running!")
    print("="*50)
    print(f"\nOpen Web UI: http://{config.local_ip}:{config.onvif_port}/")
    print(f"Snapshot URL: http://{config.local_ip}:{config.onvif_port}/{config.snapshot_url}")
    print("Press Ctrl+C to stop\n")
    
    # Check if this is video upload mode (--source video without a file)
    is_video_upload_mode = args.source.lower() == 'video'
    
    if is_video_upload_mode:
        # Video upload mode - no source file, wait for upload via web UI
        print("Video upload mode - no video file specified")
        print("Upload a video file via the web UI to start streaming\n")
        camera.set_video_upload_mode(True)
        
        try:
            # Main loop that handles video switching
            while camera.is_running:
                video_path = camera.get_current_video_path()
                
                if video_path and os.path.isfile(video_path):
                    # We have a video file to play
                    print(f"Opening video source: {video_path}")
                    cap = cv2.VideoCapture(video_path)
                    
                    if not cap.isOpened():
                        print(f"Error: Could not open video: {video_path}")
                        camera.notify_video_error(f"Could not open video: {os.path.basename(video_path)}")
                        import time
                        time.sleep(0.5)
                        continue
                    
                    # Update config with video info
                    config.source_info = os.path.basename(video_path)
                    camera.notify_video_loaded(video_path)
                    
                    # Stream video frames
                    while camera.is_running and camera.get_current_video_path() == video_path:
                        ret, frame = cap.read()
                        if not ret:
                            # Loop the video
                            cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                            continue
                        camera.stream(frame)
                    
                    cap.release()
                    print(f"Video source closed: {video_path}")
                else:
                    # No video yet, generate a placeholder frame
                    import numpy as np
                    placeholder = np.zeros((config.main_height, config.main_width, 3), dtype=np.uint8)
                    # Dark blue background
                    placeholder[:] = (30, 20, 10)
                    # Add text
                    text = "Upload a video to start"
                    font = cv2.FONT_HERSHEY_SIMPLEX
                    font_scale = 1.5
                    thickness = 2
                    (text_w, text_h), _ = cv2.getTextSize(text, font, font_scale, thickness)
                    x = (config.main_width - text_w) // 2
                    y = (config.main_height + text_h) // 2
                    cv2.putText(placeholder, text, (x, y), font, font_scale, (100, 100, 100), thickness)
                    
                    camera.stream(placeholder)
                    import time
                    time.sleep(1.0 / config.main_fps)
                    
        except KeyboardInterrupt:
            print("\nShutting down...")
        finally:
            camera.stop()
        
        return 0
    
    # Standard mode - open camera source
    # Try to parse as int (device index), otherwise treat as path/URL
    try:
        camera_source = int(args.source)
    except ValueError:
        camera_source = args.source
    
    print(f"Opening camera source: {camera_source}")
    # Prefer V4L2 on Linux for consistent FPS control
    if isinstance(camera_source, int) and platform.system().lower() == "linux":
        cap = cv2.VideoCapture(camera_source, cv2.CAP_V4L2)
    else:
        cap = cv2.VideoCapture(camera_source)
    
    # Set resolution if using webcam (device index)
    if isinstance(camera_source, int):
        # Request MJPEG + low buffer for better FPS on Pi
        fourcc_func = getattr(cv2, "VideoWriter_fourcc", None)
        if fourcc_func is None:
            fourcc_func = cv2.VideoWriter.fourcc
        cap.set(cv2.CAP_PROP_FOURCC, fourcc_func(*"MJPG"))
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, config.main_width)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, config.main_height)
        cap.set(cv2.CAP_PROP_FPS, config.main_fps)
    
    # Store initial video path if it's a video file
    if source_type == "video_file" and os.path.isfile(args.source):
        camera.set_video_upload_mode(True)
        camera.set_current_video_path(os.path.abspath(args.source))
    
    if not cap.isOpened():
        print(f"Error: Could not open camera source: {camera_source}")
        camera.stop()
        return 1
    
    try:
        while camera.is_running:
            # Check if video source changed (for video file mode)
            if source_type == "video_file":
                new_video = camera.get_current_video_path()
                current_source = os.path.abspath(camera_source) if isinstance(camera_source, str) else None
                
                if new_video and new_video != current_source:
                    # Video source changed - switch to new video
                    print(f"Switching to new video: {new_video}")
                    cap.release()
                    cap = cv2.VideoCapture(new_video)
                    
                    if not cap.isOpened():
                        print(f"Error: Could not open new video: {new_video}")
                        camera.notify_video_error(f"Could not open video: {os.path.basename(new_video)}")
                        # Revert to previous video
                        if current_source and os.path.isfile(current_source):
                            cap = cv2.VideoCapture(current_source)
                        continue
                    
                    camera_source = new_video
                    config.source_info = os.path.basename(new_video)
                    camera.notify_video_loaded(new_video)
            
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
