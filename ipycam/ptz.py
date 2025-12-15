#!/usr/bin/env python3
"""
Digital PTZ (Pan-Tilt-Zoom) Controller

Implements ePTZ (electronic/digital PTZ) by cropping and scaling regions
of a larger source frame. This allows PTZ functionality without physical
camera movement.

Also supports external hardware controllers via callbacks for controlling
physical motors, servos, gimbals, etc.
"""

import threading
import time
import json
from abc import ABC, abstractmethod
from dataclasses import dataclass, asdict
from typing import Dict, Optional, List, Callable, Protocol, runtime_checkable
import numpy as np
import cv2


@dataclass
class PTZState:
    """Current PTZ position state"""
    pan: float = 0.0    # -1.0 to 1.0 (left to right)
    tilt: float = 0.0   # -1.0 to 1.0 (down to up)  
    zoom: float = 0.0   # 0.0 to 1.0 (wide to tele)


@dataclass
class PTZVelocity:
    """Current PTZ movement velocity"""
    pan_speed: float = 0.0   # -1.0 to 1.0
    tilt_speed: float = 0.0  # -1.0 to 1.0
    zoom_speed: float = 0.0  # -1.0 to 1.0


@dataclass
class PTZPreset:
    """Saved PTZ preset position"""
    token: str
    name: str
    pan: float
    tilt: float
    zoom: float


@runtime_checkable
class PTZHardwareHandler(Protocol):
    """
    Protocol for external hardware PTZ controllers.
    
    Implement this interface to control physical PTZ hardware (motors, servos, 
    gimbals, etc.) in response to ONVIF PTZ commands.
    
    All methods receive normalized values:
    - pan: -1.0 (left) to 1.0 (right)
    - tilt: -1.0 (down) to 1.0 (up)
    - zoom: 0.0 (wide) to 1.0 (telephoto)
    - speeds: -1.0 to 1.0 (negative = reverse direction)
    
    Example implementation:
    
        class ServoController:
            def __init__(self, pan_pin, tilt_pin):
                self.pan_servo = Servo(pan_pin)
                self.tilt_servo = Servo(tilt_pin)
            
            def on_absolute_move(self, pan, tilt, zoom):
                # Convert -1..1 to servo angle 0..180
                if pan is not None:
                    self.pan_servo.angle = (pan + 1) * 90
                if tilt is not None:
                    self.tilt_servo.angle = (tilt + 1) * 90
            
            def on_continuous_move(self, pan_speed, tilt_speed, zoom_speed):
                # Set motor speeds
                pass
            
            def on_stop(self):
                # Stop all motors
                pass
            
            def on_goto_preset(self, token, pan, tilt, zoom):
                # Move to preset position
                self.on_absolute_move(pan, tilt, zoom)
    """
    
    def on_continuous_move(self, pan_speed: float, tilt_speed: float, zoom_speed: float) -> None:
        """Called when continuous movement is requested."""
        ...
    
    def on_stop(self) -> None:
        """Called when movement should stop."""
        ...
    
    def on_absolute_move(self, pan: Optional[float], tilt: Optional[float], zoom: Optional[float]) -> None:
        """Called when absolute position is requested."""
        ...
    
    def on_relative_move(self, pan_delta: float, tilt_delta: float, zoom_delta: float) -> None:
        """Called when relative movement is requested."""
        ...
    
    def on_goto_preset(self, token: str, pan: float, tilt: float, zoom: float) -> None:
        """Called when moving to a preset position."""
        ...
    
    def on_goto_home(self) -> None:
        """Called when returning to home position."""
        ...


class PTZController:
    """
    Digital PTZ controller for ePTZ functionality.
    
    Provides smooth continuous movement, absolute/relative positioning,
    and preset management. Supports external hardware controllers via
    the add_hardware_handler() method.
    """
    
    def __init__(self, output_width: int = 1920, output_height: int = 1080, 
                 max_zoom: float = 4.0, enable_digital_ptz: bool = True):
        """
        Initialize the PTZ controller.
        
        Args:
            output_width: Output frame width
            output_height: Output frame height
            max_zoom: Maximum zoom factor (e.g., 4.0 = 4x zoom)
            enable_digital_ptz: Whether to apply digital PTZ transforms to frames.
                               Set to False if using only hardware PTZ.
        """
        self.output_width = output_width
        self.output_height = output_height
        self.max_zoom = max_zoom
        self.enable_digital_ptz = enable_digital_ptz
        
        self.state = PTZState()
        self.velocity = PTZVelocity()
        self.presets: Dict[str, PTZPreset] = {}
        
        # Fast check flag - True when PTZ is at default position (no transform needed)
        self._is_default = True
        
        self._lock = threading.Lock()
        self._movement_thread: Optional[threading.Thread] = None
        self._movement_running = False
        
        # Hardware handlers for external PTZ control
        self._hardware_handlers: List[PTZHardwareHandler] = []
        
        # Load presets from file
        self._load_presets()
        
        # Start movement thread
        self._start_movement_thread()
    
    # === Hardware Handler Management ===
    
    def add_hardware_handler(self, handler: PTZHardwareHandler) -> None:
        """
        Register an external hardware controller to receive PTZ commands.
        
        Args:
            handler: Object implementing PTZHardwareHandler protocol
            
        Example:
            class MyServoController:
                def on_continuous_move(self, pan_speed, tilt_speed, zoom_speed):
                    # Control servos based on speed
                    pass
                def on_stop(self):
                    pass
                def on_absolute_move(self, pan, tilt, zoom):
                    pass
                def on_relative_move(self, pan_delta, tilt_delta, zoom_delta):
                    pass
                def on_goto_preset(self, token, pan, tilt, zoom):
                    pass
                def on_goto_home(self):
                    pass
            
            ptz = PTZController()
            ptz.add_hardware_handler(MyServoController())
        """
        if handler not in self._hardware_handlers:
            self._hardware_handlers.append(handler)
    
    def remove_hardware_handler(self, handler: PTZHardwareHandler) -> bool:
        """
        Remove a previously registered hardware handler.
        
        Returns:
            True if handler was found and removed, False otherwise
        """
        if handler in self._hardware_handlers:
            self._hardware_handlers.remove(handler)
            return True
        return False
    
    def _notify_hardware(self, method: str, *args, **kwargs) -> None:
        """Notify all hardware handlers of a PTZ event"""
        for handler in self._hardware_handlers:
            try:
                callback = getattr(handler, method, None)
                if callback and callable(callback):
                    callback(*args, **kwargs)
            except Exception as e:
                print(f"Hardware handler error in {method}: {e}")
    
    def _start_movement_thread(self):
        """Start the background movement thread"""
        self._movement_running = True
        self._movement_thread = threading.Thread(target=self._movement_loop, daemon=True)
        self._movement_thread.start()
    
    def stop(self):
        """Stop the PTZ controller"""
        self._movement_running = False
        if self._movement_thread:
            self._movement_thread.join(timeout=1.0)
    
    def apply_ptz(self, frame: np.ndarray) -> np.ndarray:
        """
        Apply PTZ transform to input frame.
        
        Args:
            frame: Input BGR frame (should be at least output_width x output_height)
        
        Returns:
            Transformed frame at output_width x output_height
        """
        # Skip digital PTZ if disabled (hardware-only mode)
        if not self.enable_digital_ptz:
            return frame
        
        # Ultra-fast path: single boolean check when PTZ is at default
        if self._is_default:
            return frame
        
        # Read current state (lock-free, small race acceptable)
        pan = self.state.pan
        tilt = self.state.tilt
        zoom = self.state.zoom
        
        src_h, src_w = frame.shape[:2]
        
        # Calculate crop size based on zoom level
        zoom_factor = 1.0 + zoom * (self.max_zoom - 1.0)
        crop_w = int(src_w / zoom_factor)
        crop_h = int(src_h / zoom_factor)
        
        # Ensure crop doesn't exceed source dimensions
        crop_w = min(crop_w, src_w)
        crop_h = min(crop_h, src_h)
        
        # Calculate max offset (how far we can pan/tilt)
        max_offset_x = (src_w - crop_w) // 2
        max_offset_y = (src_h - crop_h) // 2
        
        # Calculate crop center position
        center_x = src_w // 2 + int(pan * max_offset_x)
        center_y = src_h // 2 - int(tilt * max_offset_y)
        
        # Calculate crop boundaries
        x1 = max(0, center_x - crop_w // 2)
        y1 = max(0, center_y - crop_h // 2)
        x2 = min(src_w, x1 + crop_w)
        y2 = min(src_h, y1 + crop_h)
        
        # Crop and resize
        cropped = frame[y1:y2, x1:x2]
        
        # Only resize if necessary
        if cropped.shape[1] != self.output_width or cropped.shape[0] != self.output_height:
            output = cv2.resize(cropped, (self.output_width, self.output_height), 
                               interpolation=cv2.INTER_LINEAR)
        else:
            output = cropped
        
        return output
    
    def _movement_loop(self):
        """Background thread for continuous movement"""
        last_time = time.time()
        
        while self._movement_running:
            current_time = time.time()
            dt = current_time - last_time
            last_time = current_time
            
            # Quick check for movement without lock (small race acceptable)
            has_movement = (abs(self.velocity.pan_speed) >= 0.001 or 
                           abs(self.velocity.tilt_speed) >= 0.001 or
                           abs(self.velocity.zoom_speed) >= 0.001)
            
            if not has_movement:
                time.sleep(0.05)  # Sleep longer when idle
                continue
            
            # Only acquire lock when actually updating position
            with self._lock:
                # Apply velocity to position
                speed_factor = 1.0  # Units per second at full speed
                
                self.state.pan += self.velocity.pan_speed * speed_factor * dt
                self.state.tilt += self.velocity.tilt_speed * speed_factor * dt
                self.state.zoom += self.velocity.zoom_speed * speed_factor * dt
                
                # Clamp values
                self.state.pan = max(-1.0, min(1.0, self.state.pan))
                self.state.tilt = max(-1.0, min(1.0, self.state.tilt))
                self.state.zoom = max(0.0, min(1.0, self.state.zoom))
                
                # Update default flag
                self._is_default = (abs(self.state.pan) < 0.001 and 
                                   abs(self.state.tilt) < 0.001 and 
                                   abs(self.state.zoom) < 0.001)
            
            time.sleep(0.016)  # ~60Hz update rate
    
    # === ONVIF PTZ Commands ===
    
    def continuous_move(self, pan_speed: float = 0.0, tilt_speed: float = 0.0, 
                       zoom_speed: float = 0.0):
        """Start continuous movement at specified speeds"""
        with self._lock:
            self.velocity.pan_speed = max(-1.0, min(1.0, pan_speed))
            self.velocity.tilt_speed = max(-1.0, min(1.0, tilt_speed))
            self.velocity.zoom_speed = max(-1.0, min(1.0, zoom_speed))
        
        # Notify hardware handlers
        self._notify_hardware('on_continuous_move', pan_speed, tilt_speed, zoom_speed)
    
    def stop_movement(self, pan_tilt: bool = True, zoom: bool = True):
        """Stop movement"""
        with self._lock:
            if pan_tilt:
                self.velocity.pan_speed = 0.0
                self.velocity.tilt_speed = 0.0
            if zoom:
                self.velocity.zoom_speed = 0.0
        
        # Notify hardware handlers
        self._notify_hardware('on_stop')
    
    def absolute_move(self, pan: Optional[float] = None, tilt: Optional[float] = None,
                     zoom: Optional[float] = None):
        """Move to absolute position"""
        with self._lock:
            if pan is not None:
                self.state.pan = max(-1.0, min(1.0, pan))
            if tilt is not None:
                self.state.tilt = max(-1.0, min(1.0, tilt))
            if zoom is not None:
                self.state.zoom = max(0.0, min(1.0, zoom))
            # Stop any continuous movement
            self.velocity = PTZVelocity()
            # Update default flag
            self._is_default = (abs(self.state.pan) < 0.001 and 
                               abs(self.state.tilt) < 0.001 and 
                               abs(self.state.zoom) < 0.001)
        
        # Notify hardware handlers
        self._notify_hardware('on_absolute_move', pan, tilt, zoom)
    
    def relative_move(self, pan_delta: float = 0.0, tilt_delta: float = 0.0,
                     zoom_delta: float = 0.0):
        """Move relative to current position"""
        with self._lock:
            self.state.pan = max(-1.0, min(1.0, self.state.pan + pan_delta))
            self.state.tilt = max(-1.0, min(1.0, self.state.tilt + tilt_delta))
            self.state.zoom = max(0.0, min(1.0, self.state.zoom + zoom_delta))
            # Stop any continuous movement
            self.velocity = PTZVelocity()
            # Update default flag
            self._is_default = (abs(self.state.pan) < 0.001 and 
                               abs(self.state.tilt) < 0.001 and 
                               abs(self.state.zoom) < 0.001)
        
        # Notify hardware handlers
        self._notify_hardware('on_relative_move', pan_delta, tilt_delta, zoom_delta)
    
    def goto_home(self):
        """Return to home position (center, no zoom)"""
        self.absolute_move(pan=0.0, tilt=0.0, zoom=0.0)
        # Notify hardware handlers (in addition to absolute_move notification)
        self._notify_hardware('on_goto_home')
    
    def get_status(self) -> dict:
        """Get current PTZ status"""
        with self._lock:
            return {
                'pan': self.state.pan,
                'tilt': self.state.tilt,
                'zoom': self.state.zoom,
                'moving': (abs(self.velocity.pan_speed) > 0.001 or
                          abs(self.velocity.tilt_speed) > 0.001 or
                          abs(self.velocity.zoom_speed) > 0.001)
            }
    
    # === Preset Management ===
    
    def set_preset(self, token: str, name: str) -> str:
        """Save current position as a preset"""
        with self._lock:
            preset = PTZPreset(
                token=token,
                name=name,
                pan=self.state.pan,
                tilt=self.state.tilt,
                zoom=self.state.zoom
            )
            self.presets[token] = preset
        self._save_presets()
        return token
    
    def goto_preset(self, token: str) -> bool:
        """Go to a saved preset"""
        with self._lock:
            if token not in self.presets:
                return False
            preset = self.presets[token]
            self.state.pan = preset.pan
            self.state.tilt = preset.tilt
            self.state.zoom = preset.zoom
            self.velocity = PTZVelocity()
            # Update default flag
            self._is_default = (abs(self.state.pan) < 0.001 and 
                               abs(self.state.tilt) < 0.001 and 
                               abs(self.state.zoom) < 0.001)
            # Save values for notification outside lock
            pan, tilt, zoom = preset.pan, preset.tilt, preset.zoom
        
        # Notify hardware handlers
        self._notify_hardware('on_goto_preset', token, pan, tilt, zoom)
        return True
    
    def remove_preset(self, token: str) -> bool:
        """Remove a preset"""
        with self._lock:
            if token in self.presets:
                del self.presets[token]
                self._save_presets()
                return True
        return False
    
    def get_presets(self) -> Dict[str, PTZPreset]:
        """Get all presets"""
        with self._lock:
            return dict(self.presets)
    
    def _load_presets(self, filepath: str = "ptz_presets.json"):
        """Load presets from file"""
        try:
            with open(filepath, 'r') as f:
                data = json.load(f)
            for token, preset_data in data.items():
                self.presets[token] = PTZPreset(**preset_data)
        except FileNotFoundError:
            # Create default home preset
            self.presets['home'] = PTZPreset('home', 'Home', 0.0, 0.0, 0.0)
        except Exception as e:
            print(f"Failed to load presets: {e}")
    
    def _save_presets(self, filepath: str = "ptz_presets.json"):
        """Save presets to file"""
        try:
            data = {token: asdict(preset) for token, preset in self.presets.items()}
            with open(filepath, 'w') as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            print(f"Failed to save presets: {e}")
