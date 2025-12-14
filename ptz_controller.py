#!/usr/bin/env python3
"""
Digital PTZ (Pan-Tilt-Zoom) Controller

Implements ePTZ (electronic/digital PTZ) by cropping and scaling regions
of a larger source frame. This allows PTZ functionality without physical
camera movement.
"""

import threading
import time
import json
from dataclasses import dataclass, asdict
from typing import Dict, Optional
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


class PTZController:
    """
    Digital PTZ controller for ePTZ functionality.
    
    Provides smooth continuous movement, absolute/relative positioning,
    and preset management.
    """
    
    def __init__(self, output_width: int = 1920, output_height: int = 1080, max_zoom: float = 4.0):
        """
        Initialize the PTZ controller.
        
        Args:
            output_width: Output frame width
            output_height: Output frame height
            max_zoom: Maximum zoom factor (e.g., 4.0 = 4x zoom)
        """
        self.output_width = output_width
        self.output_height = output_height
        self.max_zoom = max_zoom
        
        self.state = PTZState()
        self.velocity = PTZVelocity()
        self.presets: Dict[str, PTZPreset] = {}
        
        self._lock = threading.Lock()
        self._movement_thread: Optional[threading.Thread] = None
        self._movement_running = False
        
        # Load presets from file
        self._load_presets()
        
        # Start movement thread
        self._start_movement_thread()
    
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
        with self._lock:
            pan = self.state.pan
            tilt = self.state.tilt
            zoom = self.state.zoom
        
        src_h, src_w = frame.shape[:2]
        
        # Fast path: if no PTZ adjustment and frame matches output size, return as-is
        if (abs(pan) < 0.001 and abs(tilt) < 0.001 and abs(zoom) < 0.001 and
            src_w == self.output_width and src_h == self.output_height):
            return frame
        
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
        output = cv2.resize(cropped, (self.output_width, self.output_height), 
                           interpolation=cv2.INTER_LINEAR)
        
        return output
    
    def _movement_loop(self):
        """Background thread for continuous movement"""
        last_time = time.time()
        
        while self._movement_running:
            current_time = time.time()
            dt = current_time - last_time
            last_time = current_time
            
            with self._lock:
                # Check if any movement is active
                if (abs(self.velocity.pan_speed) < 0.001 and 
                    abs(self.velocity.tilt_speed) < 0.001 and
                    abs(self.velocity.zoom_speed) < 0.001):
                    time.sleep(0.01)
                    continue
                
                # Apply velocity to position
                speed_factor = 1.0  # Units per second at full speed
                
                self.state.pan += self.velocity.pan_speed * speed_factor * dt
                self.state.tilt += self.velocity.tilt_speed * speed_factor * dt
                self.state.zoom += self.velocity.zoom_speed * speed_factor * dt
                
                # Clamp values
                self.state.pan = max(-1.0, min(1.0, self.state.pan))
                self.state.tilt = max(-1.0, min(1.0, self.state.tilt))
                self.state.zoom = max(0.0, min(1.0, self.state.zoom))
            
            time.sleep(0.016)  # ~60Hz update rate
    
    # === ONVIF PTZ Commands ===
    
    def continuous_move(self, pan_speed: float = 0.0, tilt_speed: float = 0.0, 
                       zoom_speed: float = 0.0):
        """Start continuous movement at specified speeds"""
        with self._lock:
            self.velocity.pan_speed = max(-1.0, min(1.0, pan_speed))
            self.velocity.tilt_speed = max(-1.0, min(1.0, tilt_speed))
            self.velocity.zoom_speed = max(-1.0, min(1.0, zoom_speed))
    
    def stop_movement(self, pan_tilt: bool = True, zoom: bool = True):
        """Stop movement"""
        with self._lock:
            if pan_tilt:
                self.velocity.pan_speed = 0.0
                self.velocity.tilt_speed = 0.0
            if zoom:
                self.velocity.zoom_speed = 0.0
    
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
    
    def relative_move(self, pan_delta: float = 0.0, tilt_delta: float = 0.0,
                     zoom_delta: float = 0.0):
        """Move relative to current position"""
        with self._lock:
            self.state.pan = max(-1.0, min(1.0, self.state.pan + pan_delta))
            self.state.tilt = max(-1.0, min(1.0, self.state.tilt + tilt_delta))
            self.state.zoom = max(0.0, min(1.0, self.state.zoom + zoom_delta))
            # Stop any continuous movement
            self.velocity = PTZVelocity()
    
    def goto_home(self):
        """Return to home position (center, no zoom)"""
        self.absolute_move(pan=0.0, tilt=0.0, zoom=0.0)
    
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
