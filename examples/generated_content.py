#!/usr/bin/env python3
"""
Generated Content Example

Shows how to stream procedurally generated content instead of a camera.
Creates a moving color gradient animation.
"""

import time
import numpy as np
import cv2
from ipycam import IPCamera, CameraConfig


def generate_gradient_frame(width, height, offset):
    """Generate a moving gradient frame"""
    # Create coordinate grids
    x = np.linspace(0, 1, width)
    y = np.linspace(0, 1, height)
    xx, yy = np.meshgrid(x, y)
    
    # Animated gradient
    t = offset * 0.02
    r = np.sin(xx * 3 + t) * 127 + 128
    g = np.sin(yy * 3 + t + 2) * 127 + 128
    b = np.sin((xx + yy) * 2 + t + 4) * 127 + 128
    
    # Stack channels and convert to uint8
    frame = np.stack([b, g, r], axis=-1).astype(np.uint8)
    
    return frame


def main():
    # Create camera with 720p resolution
    config = CameraConfig(
        name="Generated Content Camera",
        main_width=1280,
        main_height=720,
        main_fps=30,
    )
    
    camera = IPCamera(config)
    
    if not camera.start():
        print("Failed to start camera")
        return
    
    print(f"\n{'='*60}")
    print(f"  Streaming generated content")
    print(f"{'='*60}")
    print(f"  Web UI:      http://{config.local_ip}:{config.onvif_port}/")
    print(f"  RTSP Stream: {config.main_stream_rtsp}")
    print(f"{'='*60}\n")
    print("Press Ctrl+C to stop\n")
    
    frame_count = 0
    
    try:
        while camera.is_running:
            # Generate frame
            frame = generate_gradient_frame(config.main_width, config.main_height, frame_count)
            
            # Add text overlay
            text = f"Frame: {frame_count}"
            cv2.putText(frame, text, (20, 50),
                       cv2.FONT_HERSHEY_SIMPLEX, 1.5, (255, 255, 255), 3)
            
            # Stream handles PTZ, timestamp, and frame pacing automatically
            camera.stream(frame)
            frame_count += 1
            
            # Print stats every 150 frames
            if frame_count % 150 == 0:
                if camera.stats:
                    print(f"Stats: {camera.stats.frames_sent} frames, "
                          f"{camera.stats.actual_fps:.1f} fps")
    
    except KeyboardInterrupt:
        print("\nShutting down...")
    
    finally:
        camera.stop()


if __name__ == "__main__":
    main()
