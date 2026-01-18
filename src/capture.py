import time
import numpy as np
import mss
import cv2
import dxcam
from config import config
import ctypes
import subprocess
import sys

# NDI imports
from cyndilib.wrapper.ndi_recv import RecvColorFormat, RecvBandwidth
from cyndilib.finder import Finder
from cyndilib.receiver import Receiver
from cyndilib.video_frame import VideoFrameSync
from cyndilib.audio_frame import AudioFrameSync


def get_region():
    """Center capture region for MSS mode."""
    left = (config.screen_width - config.region_size) // 2
    top = (config.screen_height - config.region_size) // 2
    right = left + config.region_size
    bottom = top + config.region_size
    return (left, top, right, bottom)


class MSSCamera:
    def __init__(self, region):
        self.region = region
        self.sct = mss.mss()
        self.monitor = {
            "top": region[1],
            "left": region[0],
            "width": region[2] - region[0],
            "height": region[3] - region[1],
        }
        self.running = True

    def get_latest_frame(self):
        img = np.array(self.sct.grab(self.monitor))
        if img.shape[2] == 4:
            img = cv2.cvtColor(img, cv2.COLOR_BGRA2BGR)
        return img

    def stop(self):
        self.running = False
        self.sct.close()


class NDICamera:
    def __init__(self):
        self.finder = Finder()
        self.finder.set_change_callback(self.on_finder_change)
        self.finder.open()

        self.receiver = Receiver(
            color_format=RecvColorFormat.RGBX_RGBA,
            bandwidth=RecvBandwidth.highest,
        )
        self.video_frame = VideoFrameSync()
        self.audio_frame = AudioFrameSync()
        self.receiver.frame_sync.set_video_frame(self.video_frame)
        self.receiver.frame_sync.set_audio_frame(self.audio_frame)

        # --------------------------------------------------------------

        self.available_sources = []     
        self.desired_source_name = None
        self._pending_index = None
        self._pending_connect = False
        self._last_connect_try = 0.0
        self._retry_interval = 0.5
        # ---------------------------------------------------------------

        self.connected = False
        self._source_name = None
        self._size_checked = False
        self._allowed_sizes = {128,160,192,224,256,288,320,352,384,416,448,480,512,544,576,608,640}

        # prime the initial list so select_source(0) works immediately
        try:
            self.available_sources = self.finder.get_source_names() or []
        except Exception:
            self.available_sources = []

    def select_source(self, name_or_index):
        # guard against early calls
        if self.available_sources is None:
            self.available_sources = []

        self._pending_connect = True
        if isinstance(name_or_index, int):
            self._pending_index = name_or_index
            if 0 <= name_or_index < len(self.available_sources):
                self.desired_source_name = self.available_sources[name_or_index]
            else:
                print(f"[NDI] Will connect to index {name_or_index} when sources are ready.")
                return
        else:
            self.desired_source_name = str(name_or_index)

        if self.desired_source_name in self.available_sources:
            self._try_connect_throttled()

    def on_finder_change(self):
        self.available_sources = self.finder.get_source_names() or []
        print("[NDI] Found sources:", self.available_sources)

        if self._pending_index is not None and 0 <= self._pending_index < len(self.available_sources):
            self.desired_source_name = self.available_sources[self._pending_index]

        if self._pending_connect and not self.connected and self.desired_source_name in self.available_sources:
            self._try_connect_throttled()

    def _try_connect_throttled(self):
        now = time.time()
        if now - self._last_connect_try < self._retry_interval:
            return
        self._last_connect_try = now
        if self.desired_source_name:
            self.connect_to_source(self.desired_source_name)


    def connect_to_source(self, source_name):
        source = self.finder.get_source(source_name)
        if not source:
            print(f"[NDI] Source '{source_name}' not available (get_source returned None).")
            return
        self.receiver.set_source(source)
        self._source_name = source.name
        print(f"[NDI] set_source -> {self._source_name}")
        for _ in range(200):
            if self.receiver.is_connected():
                self.connected = True
                self._pending_connect = False
                print("[NDI] Receiver reports CONNECTED.")
                break
            time.sleep(0.01)
        else:
            print("[NDI] Timeout: receiver never reported connected.")
            self.connected = False
        self._size_checked = False

    # ---- one-time size verdict logging ----
    def _log_size_verdict_once(self, w, h):
        if self._size_checked:
            return
        self._size_checked = True

        name = self._source_name or "NDI Source"
        if w == h and w in self._allowed_sizes:
            print(f"[NDI] Source {name}: {w}x{h} ✔ allowed (no resize).")
            return

        target = min(w, h)
        allowed = sorted(self._allowed_sizes)
        down = max((s for s in allowed if s <= target), default=None)
        up   = min((s for s in allowed if s >= target), default=None)
        if down is None and up is None:
            suggest = 640
        elif down is None:
            suggest = up
        elif up is None:
            suggest = down
        else:
            suggest = down if (target - down) <= (up - target) else up

        if w != h:
            print(
                f"[NDI][FOV WARNING] Source {name}: input {w}x{h} is not square. "
                f"Nearest allowed square: {suggest}x{suggest}. "
                f"Consider a center crop to {suggest}x{suggest} for stable colors & model sizing."
            )
        else:
            print(
                f"[NDI][FOV WARNING] Source {name}: {w}x{h} not in allowed set. "
                f"Nearest allowed: {suggest}x{suggest}. "
                f"Consider a center ROI of {suggest}x{suggest} to avoid interpolation artifacts."
            )
    def list_sources(self, refresh=True):
        """
        Return a list of NDI source names. If refresh=True, query the Finder.
        Never raises; always returns a list.
        """
        if refresh:
            try:
                self.available_sources = self.finder.get_source_names() or []
            except Exception:
                # keep whatever we had, but make sure it's a list
                self.available_sources = self.available_sources or []
        return list(self.available_sources)
    
    
    def maintain_connection(self):
        
        if self.connected and not self.receiver.is_connected():
            self.connected = False
            self._pending_connect = True
        # try reconnect if source is present
        if self._pending_connect and self.desired_source_name in self.available_sources:
            self._try_connect_throttled()

    def switch_source(self, name_or_index):
        self.connected = False
        self._pending_connect = True
        self.select_source(name_or_index)

    def get_latest_frame(self):
        if not self.receiver.is_connected():
            time.sleep(0.002)
            return None

        self.receiver.frame_sync.capture_video()
        if min(self.video_frame.xres, self.video_frame.yres) == 0:
            time.sleep(0.002)
            return None
        config.ndi_width, config.ndi_height = self.video_frame.xres, self.video_frame.yres

        # one-time verdict/log about resolution
        self._log_size_verdict_once(config.ndi_width, config.ndi_height)

        # Copy frame to own memory to avoid "cannot write with view active"
        frame = np.frombuffer(self.video_frame, dtype=np.uint8).copy()
        frame = frame.reshape((self.video_frame.yres, self.video_frame.xres, 4))
        frame = cv2.cvtColor(frame, cv2.COLOR_RGBA2BGR)

        return frame

    def stop(self):
        try:
            # detach first so sender-side frees up immediately
            try: self.receiver.set_source(None)
            except Exception: pass
            self.finder.close()
        except Exception as e:
            print(f"[NDI] stop() error: {e}")





class DXGICamera:
    def __init__(self, region=None, target_fps=None):
        self.region = region
        self.camera = dxcam.create(output_idx=0, output_color="BGRA")  # stable default
        # Use config.target_fps if available, else fallback
        fps = int(getattr(config, "target_fps", 240) if target_fps is None else target_fps)
        self.camera.start(target_fps=fps)  # <-- start the capture thread here
        self.running = True

    def get_latest_frame(self):
        frame = self.camera.get_latest_frame()
        if frame is None:
            return None
        # Convert BGRA -> BGR once
        if frame.shape[2] == 4:
            frame = cv2.cvtColor(frame, cv2.COLOR_BGRA2BGR)

        if self.region:
            x1, y1, x2, y2 = self.region
            frame = frame[y1:y2, x1:x2]
        return frame

    def stop(self):
        self.running = False
        try:
            self.camera.stop()
        except Exception:
            pass


class CaptureCardCamera:
    """Capture Card camera for reading frames from a capture device."""

    def __init__(self, config, region=None):
        self.frame_width = int(getattr(config, "capture_width", 1920))
        self.frame_height = int(getattr(config, "capture_height", 1080))
        self.target_fps = float(getattr(config, "capture_fps", 240))
        self.device_index = int(getattr(config, "capture_device_index", 0))
        self.fourcc_pref = list(getattr(config, "capture_fourcc_preference", ["NV12", "YUY2", "MJPG"]))
        self.config = config
        self.cap = None
        self.running = True

        preferred_backends = [cv2.CAP_DSHOW, cv2.CAP_MSMF, cv2.CAP_ANY]
        for backend in preferred_backends:
            cap = cv2.VideoCapture(self.device_index, backend)
            if not cap.isOpened():
                cap.release()
                continue
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, float(self.frame_width))
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, float(self.frame_height))
            cap.set(cv2.CAP_PROP_FPS, float(self.target_fps))

            for fourcc in self.fourcc_pref:
                try:
                    fourcc_code = cv2.VideoWriter_fourcc(*fourcc)
                    cap.set(cv2.CAP_PROP_FOURCC, fourcc_code)
                    print(f"[CaptureCard] Set fourcc to {fourcc}")
                    break
                except Exception as e:
                    print(f"[CaptureCard] Failed to set fourcc {fourcc}: {e}")
            self.cap = cap
            print(f"[CaptureCard] Successfully opened camera {self.device_index} with backend {backend}")
            print(f"[CaptureCard] Resolution: {self.frame_width}x{self.frame_height}, FPS: {self.target_fps}")
            break

        if self.cap is None or not self.cap.isOpened():
            raise RuntimeError(f"Failed to open capture card at device index {self.device_index}")

    def get_latest_frame(self):
        if not self.cap or not self.cap.isOpened():
            return None
        ret, frame = self.cap.read()
        if not ret or frame is None:
            return None

        base_w = int(getattr(self.config, "capture_width", 1920))
        base_h = int(getattr(self.config, "capture_height", 1080))
        config.ndi_width = base_w
        config.ndi_height = base_h
        config.main_pc_width = base_w
        config.main_pc_height = base_h

        range_x = int(getattr(self.config, "capture_range_x", 128))
        range_y = int(getattr(self.config, "capture_range_y", 128))
        if range_x < 128:
            range_x = max(128, getattr(self.config, "region_size", 200))
        if range_y < 128:
            range_y = max(128, getattr(self.config, "region_size", 200))

        offset_x = int(getattr(self.config, "capture_offset_x", 0))
        offset_y = int(getattr(self.config, "capture_offset_y", 0))

        center_x = base_w // 2
        center_y = base_h // 2

        left = center_x - range_x // 2 + offset_x
        top = center_y - range_y // 2 + offset_y
        right = left + range_x
        bottom = top + range_y

        left = max(0, min(left, base_w))
        top = max(0, min(top, base_h))
        right = max(left, min(right, base_w))
        bottom = max(top, min(bottom, base_h))

        frame = frame[top:bottom, left:right]
        return frame

    def stop(self):
        self.running = False
        if self.cap:
            self.cap.release()
            self.cap = None


def _get_pnp_camera_names():
    if sys.platform != "win32":
        return []
    commands = [
        ["powershell", "-NoProfile", "-Command", "Get-PnpDevice -Class Camera | Select-Object -ExpandProperty FriendlyName"],
        ["powershell", "-NoProfile", "-Command", "Get-PnpDevice -Class Image | Select-Object -ExpandProperty FriendlyName"],
    ]
    for cmd in commands:
        try:
            output = subprocess.check_output(cmd, text=True, stderr=subprocess.DEVNULL)
        except Exception:
            continue
        names = [line.strip() for line in output.splitlines() if line.strip()]
        if names:
            return names
    return []

def list_capture_devices(max_devices=20):
    devices = []
    try:
        cap_get_desc = ctypes.windll.avicap32.capGetDriverDescriptionA
    except Exception:
        cap_get_desc = None

    if cap_get_desc:
        for idx in range(max_devices):
            name_buf = ctypes.create_string_buffer(256)
            desc_buf = ctypes.create_string_buffer(256)
            if cap_get_desc(idx, name_buf, 256, desc_buf, 256):
                try:
                    name = name_buf.value.decode("utf-8", "ignore").strip()
                    desc = desc_buf.value.decode("utf-8", "ignore").strip()
                except Exception:
                    name = ""
                    desc = ""
                if name:
                    devices.append((idx, name, desc))

    pnp_names = _get_pnp_camera_names()
    for name in pnp_names:
        devices.append((-1, name, "PnP"))

    seen = set()
    unique = []
    for idx, name, desc in devices:
        key = (idx, name, desc)
        if key in seen:
            continue
        seen.add(key)
        unique.append((idx, name, desc))
    return unique

def get_capture_card_region(config):
    base_w = int(getattr(config, "capture_width", getattr(config, "screen_width", 1920)))
    base_h = int(getattr(config, "capture_height", getattr(config, "screen_height", 1080)))

    range_x = int(getattr(config, "capture_range_x", 0))
    range_y = int(getattr(config, "capture_range_y", 0))
    if range_x <= 0:
        range_x = getattr(config, "region_size", 200)
    if range_y <= 0:
        range_y = getattr(config, "region_size", 200)

    offset_x = int(getattr(config, "capture_offset_x", 0))
    offset_y = int(getattr(config, "capture_offset_y", 0))

    left = (base_w - range_x) // 2 + offset_x
    top = (base_h - range_y) // 2 + offset_y
    right = left + range_x
    bottom = top + range_y

    left = max(0, min(left, base_w))
    top = max(0, min(top, base_h))
    right = max(left, min(right, base_w))
    bottom = max(top, min(bottom, base_h))

    return (left, top, right, bottom)

def validate_capture_card_config(config):
    try:
        device_index = int(getattr(config, "capture_device_index", 0))
        if device_index < 0 or device_index > 10:
            return False, f"Device index {device_index} is out of valid range (0-10)"

        width = int(getattr(config, "capture_width", 1920))
        height = int(getattr(config, "capture_height", 1080))
        if width < 320 or width > 7680:
            return False, f"Capture width {width} is out of valid range (320-7680)"
        if height < 240 or height > 4320:
            return False, f"Capture height {height} is out of valid range (240-4320)"

        fps = float(getattr(config, "capture_fps", 240))
        if fps < 1 or fps > 300:
            return False, f"Capture FPS {fps} is out of valid range (1-300)"

        fourcc_list = getattr(config, "capture_fourcc_preference", ["NV12", "YUY2", "MJPG"])
        if not isinstance(fourcc_list, list) or len(fourcc_list) == 0:
            return False, "FourCC preference must be a non-empty list"

        return True, None
    except Exception as e:
        return False, f"Configuration validation error: {str(e)}"

def create_capture_card_camera(config, region=None):
    is_valid, error_msg = validate_capture_card_config(config)
    if not is_valid:
        raise ValueError(f"Invalid capture card configuration: {error_msg}")
    return CaptureCardCamera(config, region)




def get_camera():
    """Factory function to return the right camera based on config."""
    if config.capturer_mode.lower() == "mss":
        region = get_region()
        cam = MSSCamera(region)
        return cam, region
    elif config.capturer_mode.lower() == "ndi":
        cam = NDICamera()
        return cam, None
    elif config.capturer_mode.lower() == "dxgi":
        region = get_region()
        cam = DXGICamera(region)
        return cam, region
    elif config.capturer_mode.lower() == "capture":
        cam = create_capture_card_camera(config)
        return cam, None
    else:
        raise ValueError(f"Unknown capturer_mode: {config.capturer_mode}")
