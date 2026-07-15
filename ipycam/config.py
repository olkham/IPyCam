#!/usr/bin/env python3
"""Camera configuration dataclass"""

import os
import re
import json
import socket
import logging
import tempfile
from dataclasses import dataclass, asdict
from .__version__ import __version__

try:
    from .streamer import StreamConfig, HWAccel
except ImportError:
    from streamer import StreamConfig, HWAccel

logger = logging.getLogger(__name__)


def get_local_ip() -> str:
    """Get the local IP address of the machine."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(('10.255.255.255', 1))
        ip = s.getsockname()[0]
    except Exception:
        ip = '127.0.0.1'
    finally:
        s.close()
    return ip


def _coerce_bool(value):
    """Coerce common truthy/falsy representations to bool.

    Returns (ok, coerced_value); ok is False if value isn't recognizable as a
    boolean. Shared by every boolean EDITABLE_FIELDS entry so they all accept
    the same set of representations (real bool, 0/1, "true"/"false"/... ).
    """
    if isinstance(value, bool):
        return True, value
    if isinstance(value, (int, float)):
        return True, bool(value)
    if isinstance(value, str):
        s = value.strip().lower()
        if s in ('true', '1', 'yes', 'on'):
            return True, True
        if s in ('false', '0', 'no', 'off'):
            return True, False
    return False, None


DEFAULT_CONFIG_PATH = "camera_config.json"

# Fields the web UI / clients are allowed to modify via apply_updates().
# Identity/network fields (local_ip, firmware_version, serial_number, ports)
# are intentionally excluded so they cannot be overwritten by a POST body.
EDITABLE_FIELDS = frozenset({
    'main_width', 'main_height', 'main_fps', 'main_bitrate', 'main_stream_name',
    'sub_width', 'sub_height', 'sub_fps', 'sub_bitrate', 'sub_stream_name',
    'hw_accel', 'name', 'manufacturer', 'model',
    'show_timestamp', 'timestamp_format', 'timestamp_position',
    'flip', 'mirror', 'rotation',
    # Recording. recording_path is DELIBERATELY excluded: it is a filesystem
    # location, so allowing it through the generic web-API update path would be
    # a path-traversal / arbitrary-write foot-gun. It is set via the config
    # file only (like ports/identity), while the other recording knobs are safe
    # scalars/flags.
    'recording_enabled', 'recording_format', 'recording_max_file_mb',
    'recording_pre_seconds',
})

# Applied fields in this set require the video stream to be restarted.
# NOTE on `rotation`: a 90/270 rotation swaps the frame's width and height
# relative to main_width/main_height, which changes the encoded stream
# resolution (the go2rtc/native-RTSP/WebRTC encoders are all configured for
# the *unrotated* dimensions). Toggling between a "swap" state (90/270) and a
# "no-swap" state (0/180) therefore requires a stream restart to re-init the
# encoder at the new resolution. We restart on ANY rotation change (not just
# swap-changing ones, e.g. 90->270) -- simplest correct choice, and the
# 90<->270 case is rare enough that the extra restart cost doesn't matter.
# flip/mirror never change frame dimensions, so they do NOT need a restart.
RESTART_FIELDS = frozenset({
    'main_width', 'main_height', 'main_fps', 'main_bitrate',
    'sub_width', 'sub_height', 'sub_bitrate', 'hw_accel',
    'rotation',
})

VALID_HW_ACCEL = frozenset({'auto', 'nvenc', 'qsv', 'cpu'})
VALID_TIMESTAMP_POSITIONS = frozenset({
    'top-left', 'top-right', 'bottom-left', 'bottom-right',
})
VALID_ROTATIONS = frozenset({0, 90, 180, 270})
BITRATE_RE = re.compile(r'^\d+[KMG]?$')

# Supported recording container formats (recording_format allowlist). Each maps
# to a (extension, fourcc) pair in ipycam/recorder.py.
VALID_RECORDING_FORMATS = frozenset({'mp4', 'avi'})
# Pre-record buffer bound. The ring buffer holds pre_seconds * fps DECODED
# frames at main resolution, so its memory cost grows quickly (see recorder.py
# for the formula); cap it to keep a mis-set value from exhausting memory.
MAX_RECORDING_PRE_SECONDS = 30
# Max-file-size bounds (MB). Lower bound 1 MB; upper bound 1 TB is a sanity cap.
MIN_RECORDING_MAX_MB = 1
MAX_RECORDING_MAX_MB = 1024 * 1024

# Sane bounds for dimensions/fps.
MAX_WIDTH = 7680
MAX_HEIGHT = 4320
MIN_FPS = 1
MAX_FPS = 120


@dataclass
class CameraConfig:
    """Complete camera configuration"""
    # Identity
    name: str = "Virtual Camera"
    manufacturer: str = "PythonCam"
    model: str = "VirtualCam-1"
    serial_number: str = "PY-000001"
    firmware_version: str = __version__
    
    # Network
    local_ip: str = ""
    onvif_port: int = 8080
    rtsp_port: int = 8554
    rtmp_port: int = 1935
    web_port: int = 8081
    go2rtc_api_port: int = 1984

    # Authentication (optional). Empty credentials => auth disabled (fully
    # open, backward-compatible). When both are set, HTTP Basic auth guards the
    # web/REST surface and WS-Security UsernameToken guards ONVIF.
    # NOTE: intentionally NOT in EDITABLE_FIELDS -- credentials must not be
    # changeable through the generic /api/config apply_updates() path; a
    # dedicated user-settings path handles secure changes.
    username: str = ""
    password: str = ""
    
    # Main stream
    main_width: int = 1920
    main_height: int = 1080
    main_fps: int = 30
    main_bitrate: str = "8M"
    main_stream_name: str = "video_main"
    
    # Sub stream
    sub_width: int = 640
    sub_height: int = 360
    sub_fps: int = 30
    sub_bitrate: str = "1M"
    sub_stream_name: str = "video_sub"
    
    #mjpeg fallback
    mjpeg_url: str = "stream.mjpeg"
    snapshot_url: str = "snapshot.jpg"
    
    # Encoding
    hw_accel: str = "auto"
    
    # Overlay
    show_timestamp: bool = True
    timestamp_format: str = "%Y-%m-%d %H:%M:%S"
    timestamp_position: str = "bottom-left"  # top-left, top-right, bottom-left, bottom-right

    # Display transforms (applied in the frame pipeline; see IPCamera.stream).
    # Defaults are all no-op.
    flip: bool = False      # vertical flip (upside-down)
    mirror: bool = False    # horizontal flip (mirror image)
    rotation: int = 0       # clockwise rotation in degrees: 0, 90, 180, or 270

    # Recording (local disk capture; see ipycam/recorder.py).
    # Defaults = recording off. recording_enabled controls whether the recorder
    # worker / pre-record ring buffer runs at all; an explicit start_recording
    # call can still start the worker on demand even when this is False.
    recording_enabled: bool = False
    recording_format: str = "mp4"          # one of VALID_RECORDING_FORMATS
    recording_path: str = "recordings"     # output dir (config-file only, not web-editable)
    recording_max_file_mb: int = 1024      # rotate to a new segment past this size
    recording_pre_seconds: int = 0         # pre-record ring buffer length (0 = disabled)

    # Source info (for UI display)
    source_type: str = "unknown"  # camera, video_file, generated, rtsp, custom
    source_info: str = ""  # e.g., "Camera 0", "video.mp4", "rtsp://..."
    
    def __post_init__(self):
        if not self.local_ip:
            self.local_ip = get_local_ip()
        # Path this config was loaded from / should be saved back to.
        # Set as a plain instance attribute (NOT a dataclass field) so it is
        # never serialized by asdict() into the saved JSON or /api/config.
        self._config_path = DEFAULT_CONFIG_PATH

    @property
    def auth_enabled(self) -> bool:
        """True when both username and password are configured (non-empty).

        When False, all endpoints behave exactly as before (open access).
        """
        return bool(self.username) and bool(self.password)

    def set_credentials(self, username: str, password: str):
        """Validate and set the username/password credential pair.

        Kept separate from apply_updates()/EDITABLE_FIELDS: credentials guard
        the whole HTTP/ONVIF surface, so they get their own dedicated,
        stricter validation rather than the generic field-update path (see
        the EDITABLE_FIELDS comment above). Does NOT call save() -- the
        caller (the /api/credentials handler) persists on success.

        Rules:
          - Both empty -> clears credentials (auth disabled). Always allowed;
            this is how a user backs out of auth entirely.
          - Otherwise both username and password must be non-empty -- a
            half-set credential (e.g. a username with no password) would be
            confusing and is rejected rather than silently accepted.
          - username is stripped of surrounding whitespace; password is used
            as-is (whitespace may be a meaningful part of a password).

        Returns (ok: bool, error: str | None). On ok=True the fields have
        already been updated on self.
        """
        try:
            u = str(username).strip() if username is not None else ''
            p = str(password) if password is not None else ''
        except Exception:
            return False, 'Invalid credentials'

        if not u and not p:
            self.username = ''
            self.password = ''
            return True, None

        if not u:
            return False, 'Username is required'
        if not p:
            return False, 'Password is required'

        self.username = u
        self.password = p
        return True, None

    @property
    def main_stream_rtmp(self) -> str:
        return f"rtmp://127.0.0.1:{self.rtmp_port}/{self.main_stream_name}"
    
    @property
    def sub_stream_rtmp(self) -> str:
        return f"rtmp://127.0.0.1:{self.rtmp_port}/{self.sub_stream_name}"

    @property
    def main_stream_push_url(self) -> str:
        return f"rtmp://127.0.0.1:{self.rtmp_port}/{self.main_stream_name}"
    
    @property
    def sub_stream_push_url(self) -> str:
        return f"rtmp://127.0.0.1:{self.rtmp_port}/{self.sub_stream_name}"
    
    @property
    def main_stream_rtsp(self) -> str:
        return f"rtsp://{self.local_ip}:{self.rtsp_port}/{self.main_stream_name}"
    
    @property
    def sub_stream_rtsp(self) -> str:
        return f"rtsp://{self.local_ip}:{self.rtsp_port}/{self.sub_stream_name}"
    
    @property
    def onvif_url(self) -> str:
        return f"http://{self.local_ip}:{self.onvif_port}/onvif/device_service"
    
    @property
    def webrtc_url(self) -> str:
        return f"http://{self.local_ip}:{self.go2rtc_api_port}"
    
    def to_stream_config(self) -> StreamConfig:
        """Convert to VideoStreamer StreamConfig"""
        hw = HWAccel.AUTO
        if self.hw_accel == "nvenc":
            hw = HWAccel.NVENC
        elif self.hw_accel == "qsv":
            hw = HWAccel.QSV
        elif self.hw_accel == "cpu":
            hw = HWAccel.CPU
            
        return StreamConfig(
            width=self.main_width,
            height=self.main_height,
            fps=self.main_fps,
            bitrate=self.main_bitrate,
            hw_accel=hw,
            sub_width=self.sub_width,
            sub_height=self.sub_height,
            sub_bitrate=self.sub_bitrate,
        )
    
    def _validate_update(self, key: str, value):
        """Coerce and range-check a single editable field.

        Returns (ok: bool, coerced_value). ok is False if the value is out of
        range, malformed, or cannot be coerced to the field's declared type.
        """
        try:
            if key in ('main_width', 'sub_width'):
                v = int(value)
                return (1 <= v <= MAX_WIDTH), v
            if key in ('main_height', 'sub_height'):
                v = int(value)
                return (1 <= v <= MAX_HEIGHT), v
            if key in ('main_fps', 'sub_fps'):
                v = int(value)
                return (MIN_FPS <= v <= MAX_FPS), v
            if key in ('main_bitrate', 'sub_bitrate'):
                v = str(value).strip()
                return bool(BITRATE_RE.match(v)), v
            if key == 'hw_accel':
                v = str(value).strip().lower()
                return (v in VALID_HW_ACCEL), v
            if key == 'timestamp_position':
                v = str(value).strip()
                return (v in VALID_TIMESTAMP_POSITIONS), v
            if key in ('show_timestamp', 'flip', 'mirror', 'recording_enabled'):
                return _coerce_bool(value)
            if key == 'rotation':
                v = int(value)
                return (v in VALID_ROTATIONS), v
            if key == 'recording_format':
                v = str(value).strip().lower()
                return (v in VALID_RECORDING_FORMATS), v
            if key == 'recording_max_file_mb':
                v = int(value)
                return (MIN_RECORDING_MAX_MB <= v <= MAX_RECORDING_MAX_MB), v
            if key == 'recording_pre_seconds':
                v = int(value)
                return (0 <= v <= MAX_RECORDING_PRE_SECONDS), v
            if key in ('main_stream_name', 'sub_stream_name'):
                v = str(value).strip()
                return bool(v), v
            if key in ('name', 'manufacturer', 'model', 'timestamp_format'):
                v = str(value)
                return bool(v.strip()), v
        except (ValueError, TypeError):
            return False, None
        # Not an editable field type we know how to validate.
        return False, None

    def apply_updates(self, updates: dict):
        """Validate and apply a dict of user-supplied config updates.

        Only fields in EDITABLE_FIELDS are considered; identity/network fields
        are never modifiable through this path. Each accepted value is coerced
        to the field's declared type and range-checked.

        Returns a tuple (applied, rejected, restart_keys):
          - applied:      keys that passed validation and were set
          - rejected:     keys that were unknown/not editable or failed validation
          - restart_keys: subset of applied keys whose value actually changed
                          and which require a stream restart (RESTART_FIELDS)
        """
        applied = []
        rejected = []
        restart_keys = []
        for key, value in updates.items():
            if key not in EDITABLE_FIELDS:
                rejected.append(key)
                continue
            ok, coerced = self._validate_update(key, value)
            if not ok:
                rejected.append(key)
                continue
            old_value = getattr(self, key)
            applied.append(key)
            if old_value != coerced:
                setattr(self, key, coerced)
                if key in RESTART_FIELDS:
                    restart_keys.append(key)
        return applied, rejected, restart_keys

    def save(self, filepath: str = None) -> bool:
        """Save configuration to JSON file atomically.

        Writes to a temp file in the same directory then os.replace()s it into
        place so a crash mid-write cannot corrupt the config. When filepath is
        omitted, saves back to the path this config was loaded from.
        """
        if filepath is None:
            filepath = getattr(self, '_config_path', DEFAULT_CONFIG_PATH)
        tmp_path = None
        try:
            config_dict = asdict(self)
            # Don't save local_ip as it's auto-detected
            config_dict.pop('local_ip', None)
            target_dir = os.path.dirname(os.path.abspath(filepath))
            os.makedirs(target_dir, exist_ok=True)
            fd, tmp_path = tempfile.mkstemp(
                dir=target_dir, prefix='.camera_config_', suffix='.tmp'
            )
            with os.fdopen(fd, 'w', encoding='utf-8') as f:
                json.dump(config_dict, f, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, filepath)
            return True
        except Exception as e:
            logger.error(f"Failed to save config: {e}")
            if tmp_path is not None and os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass
            return False

    @classmethod
    def load(cls, filepath: str = DEFAULT_CONFIG_PATH) -> 'CameraConfig':
        """Load configuration from JSON file, or return defaults if not found.

        The returned config remembers filepath so a later save() with no
        argument writes back to the same file.
        """
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                config_dict = json.load(f)
            # Filter to only valid fields
            valid_fields = {f.name for f in cls.__dataclass_fields__.values()}
            filtered = {k: v for k, v in config_dict.items() if k in valid_fields}
            config = cls(**filtered)
        except FileNotFoundError:
            logger.info(f"Config file '{filepath}' not found, using defaults")
            config = cls()
        except Exception as e:
            logger.error(f"Failed to load config: {e}")
            config = cls()
        config._config_path = filepath
        return config
