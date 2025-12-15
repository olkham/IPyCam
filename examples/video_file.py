#!/usr/bin/env python3
"""
Video File Example

Stream a video file in a loop through IPyCam.
Useful for testing without a physical camera.
"""

import time
import cv2
import sys
from ipycam import IPCamera, CameraConfig


def main():
    if len(sys.argv) < 2:
        print("Usage: python video_file.py <path_to_video_file>")
        print("Example: python video_file.py sample.mp4")
        return
    
    video_path = sys.argv[1]
    
    # Create camera
    config = CameraConfig(name="Video File Camera")
    camera = IPCamera(config)
    
    if not camera.start():
        print("Failed to start camera")
        return
    
    print(f"\n{'='*60}")
    print(f"  Streaming video file: {video_path}")
    print(f"{'='*60}")
    print(f"  Web UI:      http://{config.local_ip}:{config.onvif_port}/")
    print(f"  RTSP Stream: {config.main_stream_rtsp}")
    print(f"{'='*60}\n")
    
    # Open video file
    cap = cv2.VideoCapture(video_path)
    
    if not cap.isOpened():
        print(f"Error: Could not open video file: {video_path}")
        camera.stop()
        return
    
    # Get video properties
    video_fps = cap.get(cv2.CAP_PROP_FPS)
    frame_count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    
    print(f"Video: {video_fps:.1f} fps, {frame_count} frames\n")
    print("Press Ctrl+C to stop\n")
    
    frame_num = 0
    start_time = time.time()
    
    try:
        while camera.is_running:
            ret, frame = cap.read()
            
            # Loop video
            if not ret:
                cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                frame_num = 0
                print("Looping video...")
                continue
            
            # Add frame counter
            cv2.putText(frame, f"Frame {frame_num}/{frame_count}", (10, 30),
                       cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 255), 2)
            
            camera.stream(frame)
            frame_num += 1
            
            # Use video's original FPS for pacing
            target_frame_time = 1.0 / video_fps
            elapsed = time.time() - start_time
            sleep_time = (frame_num * target_frame_time) - elapsed
            
            if sleep_time > 0:
                time.sleep(sleep_time)
    
    except KeyboardInterrupt:
        print("\nShutting down...")
    
    finally:
        cap.release()
        camera.stop()


if __name__ == "__main__":
    main()
