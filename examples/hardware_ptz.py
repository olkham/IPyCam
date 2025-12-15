#!/usr/bin/env python3
"""
Hardware PTZ Example

Demonstrates how to attach external hardware controllers (motors, servos, 
gimbals, etc.) to receive PTZ commands from ONVIF clients.

The PTZHardwareHandler protocol defines callbacks for all PTZ operations.
Implement the methods you need and register your handler with the PTZ controller.
"""

import time
import cv2
from typing import Optional
from ipycam import IPCamera, CameraConfig, PTZHardwareHandler


class PrintingPTZHandler:
    """
    Example hardware handler that prints all PTZ commands.
    Replace this with your actual hardware control code.
    """
    
    def on_continuous_move(self, pan_speed: float, tilt_speed: float, zoom_speed: float) -> None:
        """Called when continuous movement starts"""
        print(f"[HW] Continuous move: pan={pan_speed:+.2f}, tilt={tilt_speed:+.2f}, zoom={zoom_speed:+.2f}")
    
    def on_stop(self) -> None:
        """Called when movement should stop"""
        print("[HW] Stop all movement")
    
    def on_absolute_move(self, pan: Optional[float], tilt: Optional[float], zoom: Optional[float]) -> None:
        """Called for absolute positioning"""
        print(f"[HW] Absolute move: pan={pan}, tilt={tilt}, zoom={zoom}")
    
    def on_relative_move(self, pan_delta: float, tilt_delta: float, zoom_delta: float) -> None:
        """Called for relative movement"""
        print(f"[HW] Relative move: Δpan={pan_delta:+.2f}, Δtilt={tilt_delta:+.2f}, Δzoom={zoom_delta:+.2f}")
    
    def on_goto_preset(self, token: str, pan: float, tilt: float, zoom: float) -> None:
        """Called when moving to a preset"""
        print(f"[HW] Goto preset '{token}': pan={pan:.2f}, tilt={tilt:.2f}, zoom={zoom:.2f}")
    
    def on_goto_home(self) -> None:
        """Called when returning to home position"""
        print("[HW] Goto home")


class ServoExample:
    """
    Example showing how you might control servos.
    This is pseudocode - replace with your actual servo library.
    """
    
    def __init__(self):
        # Example: Using RPi.GPIO or pigpio for Raspberry Pi
        # self.pan_servo = Servo(pin=17)
        # self.tilt_servo = Servo(pin=18)
        self.pan_angle = 90  # 0-180 degrees
        self.tilt_angle = 90
        print("[Servo] Initialized (simulation mode)")
    
    def on_continuous_move(self, pan_speed: float, tilt_speed: float, zoom_speed: float) -> None:
        # For continuous movement, you'd typically start a loop
        # that keeps adjusting position based on speed
        print(f"[Servo] Would move at speeds: pan={pan_speed}, tilt={tilt_speed}")
    
    def on_stop(self) -> None:
        print("[Servo] Motors stopped")
    
    def on_absolute_move(self, pan: Optional[float], tilt: Optional[float], zoom: Optional[float]) -> None:
        # Convert normalized -1..1 to servo angle 0..180
        if pan is not None:
            self.pan_angle = int((pan + 1) * 90)
            print(f"[Servo] Pan servo -> {self.pan_angle}°")
            # self.pan_servo.angle = self.pan_angle
        
        if tilt is not None:
            self.tilt_angle = int((tilt + 1) * 90)
            print(f"[Servo] Tilt servo -> {self.tilt_angle}°")
            # self.tilt_servo.angle = self.tilt_angle
    
    def on_relative_move(self, pan_delta: float, tilt_delta: float, zoom_delta: float) -> None:
        # Adjust by delta (convert to degrees: delta * 90)
        if pan_delta != 0:
            self.pan_angle = max(0, min(180, self.pan_angle + int(pan_delta * 90)))
            print(f"[Servo] Pan servo adjusted to {self.pan_angle}°")
        
        if tilt_delta != 0:
            self.tilt_angle = max(0, min(180, self.tilt_angle + int(tilt_delta * 90)))
            print(f"[Servo] Tilt servo adjusted to {self.tilt_angle}°")
    
    def on_goto_preset(self, token: str, pan: float, tilt: float, zoom: float) -> None:
        self.on_absolute_move(pan, tilt, zoom)
    
    def on_goto_home(self) -> None:
        self.pan_angle = 90
        self.tilt_angle = 90
        print("[Servo] Servos centered (home position)")


class StepperMotorExample:
    """
    Example for stepper motor control.
    This is pseudocode - replace with your actual motor driver.
    """
    
    def __init__(self):
        # Example: Using AccelStepper or similar
        # self.pan_stepper = Stepper(step_pin=17, dir_pin=18)
        # self.tilt_stepper = Stepper(step_pin=22, dir_pin=23)
        self.pan_position = 0  # Steps from center
        self.tilt_position = 0
        self.steps_per_unit = 1000  # Steps for full -1 to 1 range
        print("[Stepper] Initialized (simulation mode)")
    
    def on_continuous_move(self, pan_speed: float, tilt_speed: float, zoom_speed: float) -> None:
        # Set motor speed and direction
        print(f"[Stepper] Running at speed: pan={pan_speed}, tilt={tilt_speed}")
        # self.pan_stepper.set_speed(pan_speed * 1000)  # steps/sec
        # self.tilt_stepper.set_speed(tilt_speed * 1000)
    
    def on_stop(self) -> None:
        print("[Stepper] Motors stopped")
        # self.pan_stepper.stop()
        # self.tilt_stepper.stop()
    
    def on_absolute_move(self, pan: Optional[float], tilt: Optional[float], zoom: Optional[float]) -> None:
        if pan is not None:
            target_steps = int(pan * self.steps_per_unit)
            print(f"[Stepper] Pan motor -> {target_steps} steps")
            # self.pan_stepper.move_to(target_steps)
            self.pan_position = target_steps
        
        if tilt is not None:
            target_steps = int(tilt * self.steps_per_unit)
            print(f"[Stepper] Tilt motor -> {target_steps} steps")
            # self.tilt_stepper.move_to(target_steps)
            self.tilt_position = target_steps
    
    def on_relative_move(self, pan_delta: float, tilt_delta: float, zoom_delta: float) -> None:
        if pan_delta != 0:
            delta_steps = int(pan_delta * self.steps_per_unit)
            self.pan_position += delta_steps
            print(f"[Stepper] Pan motor moved {delta_steps} steps (now at {self.pan_position})")
        
        if tilt_delta != 0:
            delta_steps = int(tilt_delta * self.steps_per_unit)
            self.tilt_position += delta_steps
            print(f"[Stepper] Tilt motor moved {delta_steps} steps (now at {self.tilt_position})")
    
    def on_goto_preset(self, token: str, pan: float, tilt: float, zoom: float) -> None:
        self.on_absolute_move(pan, tilt, zoom)
    
    def on_goto_home(self) -> None:
        print("[Stepper] Homing motors...")
        self.pan_position = 0
        self.tilt_position = 0


def main():
    """Demo: Hardware PTZ with digital PTZ fallback"""
    
    config = CameraConfig.load("camera_config.json")
    camera = IPCamera(config=config)
    
    # Add hardware handlers - you can add multiple!
    # Commands will be sent to ALL registered handlers
    
    # Option 1: Simple printing handler for debugging
    printer = PrintingPTZHandler()
    camera.ptz.add_hardware_handler(printer)
    
    # Option 2: Add a servo controller (uncomment to use)
    # servo = ServoExample()
    # camera.ptz.add_hardware_handler(servo)
    
    # Option 3: Add a stepper motor controller (uncomment to use)
    # stepper = StepperMotorExample()
    # camera.ptz.add_hardware_handler(stepper)
    
    # Note: Digital PTZ is still enabled by default
    # To disable digital PTZ (hardware only), set:
    # camera.ptz.enable_digital_ptz = False
    
    if not camera.start():
        print("Failed to start camera")
        return
    
    print(f"\n{'='*60}")
    print(f"  Hardware PTZ Demo")
    print(f"{'='*60}")
    print(f"  Web UI: http://{config.local_ip}:{config.onvif_port}/")
    print(f"  RTSP:   {config.main_stream_rtsp}")
    print(f"{'='*60}")
    print("\nUse the web UI or an ONVIF client to control PTZ.")
    print("Hardware commands will be printed to the console.")
    print("\nPress Ctrl+C to stop\n")
    
    # Open webcam
    cap = cv2.VideoCapture(0)
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, 1920)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 1080)
    
    if not cap.isOpened():
        print("Error: Could not open webcam")
        camera.stop()
        return
    
    try:
        while camera.is_running:
            ret, frame = cap.read()
            if not ret:
                break
            
            camera.stream(frame)
            
            # Frame pacing
            time.sleep(1.0 / config.main_fps)
    
    except KeyboardInterrupt:
        print("\nShutting down...")
    
    finally:
        cap.release()
        camera.stop()


def hardware_only_example():
    """
    Demo: Hardware-only PTZ (no digital cropping)
    
    Use this when you have a physical PTZ camera and don't
    need digital PTZ simulation.
    """
    
    config = CameraConfig.load("camera_config.json")
    camera = IPCamera(config=config)
    
    # Disable digital PTZ - frames pass through unchanged
    camera.ptz.enable_digital_ptz = False
    
    # Add your hardware controller
    camera.ptz.add_hardware_handler(PrintingPTZHandler())
    
    # ... rest of setup ...
    print("Hardware-only mode: Digital PTZ disabled")


if __name__ == "__main__":
    main()
