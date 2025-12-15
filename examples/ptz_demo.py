#!/usr/bin/env python3
"""
PTZ Demo Example

Demonstrates how to control PTZ (Pan-Tilt-Zoom) programmatically.
The camera will automatically pan, tilt, and zoom through various positions.
"""

import time
import cv2
from ipycam import IPCamera, CameraConfig


def demo_ptz_sequence(camera):
    """Run a demo PTZ sequence"""
    ptz = camera.ptz
    
    print("\n--- PTZ Demo Sequence ---\n")
    
    # 1. Go to home position
    print("1. Going to home position (0, 0, 0)...")
    ptz.goto_home()
    time.sleep(2)
    
    # 2. Pan right
    print("2. Panning right...")
    ptz.continuous_move(pan_speed=0.5, tilt_speed=0.0)
    time.sleep(3)
    ptz.stop_movement()
    time.sleep(1)
    
    # 3. Pan left
    print("3. Panning left...")
    ptz.continuous_move(pan_speed=-0.5, tilt_speed=0.0)
    time.sleep(3)
    ptz.stop_movement()
    time.sleep(1)
    
    # 4. Tilt up
    print("4. Tilting up...")
    ptz.continuous_move(pan_speed=0.0, tilt_speed=0.5)
    time.sleep(3)
    ptz.stop_movement()
    time.sleep(1)
    
    # 5. Tilt down
    print("5. Tilting down...")
    ptz.continuous_move(pan_speed=0.0, tilt_speed=-0.5)
    time.sleep(3)
    ptz.stop_movement()
    time.sleep(1)
    
    # 6. Zoom in
    print("6. Zooming in...")
    ptz.continuous_move(zoom_speed=0.3)
    time.sleep(4)
    ptz.stop_movement()
    time.sleep(1)
    
    # 7. Absolute position
    print("7. Moving to absolute position (0.5, 0.3, 0.5)...")
    ptz.absolute_move(pan=0.5, tilt=0.3, zoom=0.5)
    time.sleep(2)
    
    # 8. Relative move
    print("8. Relative move (-0.3, -0.3, 0)...")
    ptz.relative_move(pan_delta=-0.3, tilt_delta=-0.3)
    time.sleep(2)
    
    # 9. Save preset
    print("9. Saving current position as preset...")
    ptz.set_preset("demo_position", "Demo Position")
    time.sleep(1)
    
    # 10. Return home and go to preset
    print("10. Going home then to saved preset...")
    ptz.goto_home()
    time.sleep(2)
    ptz.goto_preset("demo_position")
    time.sleep(2)
    
    # 11. Return to home
    print("11. Returning to home position...")
    ptz.goto_home()
    time.sleep(2)
    
    print("\n--- Demo Complete ---\n")


def main():
    # Create camera
    config = CameraConfig(
        name="PTZ Demo Camera",
        main_width=1920,
        main_height=1080,
    )
    camera = IPCamera(config)
    
    if not camera.start():
        print("Failed to start camera")
        return
    
    print(f"\n{'='*60}")
    print(f"  PTZ Demo")
    print(f"{'='*60}")
    print(f"  Web UI: http://{config.local_ip}:{config.onvif_port}/")
    print(f"  RTSP:   {config.main_stream_rtsp}")
    print(f"{'='*60}\n")
    print("Starting PTZ demo in 3 seconds...")
    print("You can also control PTZ via:")
    print("  - Web UI controls")
    print("  - ONVIF clients (tinyCam, ONVIF Device Manager, etc.)")
    print("  - Direct API calls\n")
    
    # Open webcam
    cap = cv2.VideoCapture(0)
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, 1920)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 1080)
    
    if not cap.isOpened():
        print("Error: Could not open webcam")
        camera.stop()
        return
    
    time.sleep(3)
    
    # Start PTZ demo in background
    import threading
    demo_thread = threading.Thread(target=demo_ptz_sequence, args=(camera,), daemon=True)
    demo_thread.start()
    
    frame_count = 0
    start_time = time.time()
    
    try:
        while camera.is_running:
            ret, frame = cap.read()
            if not ret:
                break
            
            # Get PTZ status for overlay
            status = camera.ptz.get_status()
            
            # Add PTZ status overlay
            cv2.putText(frame, f"Pan: {status['pan']:+.2f}", (10, 30),
                       cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 255), 2)
            cv2.putText(frame, f"Tilt: {status['tilt']:+.2f}", (10, 60),
                       cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 255), 2)
            cv2.putText(frame, f"Zoom: {status['zoom']:+.2f}", (10, 90),
                       cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 255), 2)
            
            if status['moving']:
                cv2.putText(frame, "MOVING", (10, 120),
                           cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 0), 2)
            
            camera.stream(frame)
            frame_count += 1
            
            # Frame pacing
            target_frame_time = 1.0 / config.main_fps
            elapsed = time.time() - start_time
            sleep_time = (frame_count * target_frame_time) - elapsed
            
            if sleep_time > 0:
                time.sleep(sleep_time)
    
    except KeyboardInterrupt:
        print("\nShutting down...")
    
    finally:
        cap.release()
        camera.stop()


if __name__ == "__main__":
    main()
