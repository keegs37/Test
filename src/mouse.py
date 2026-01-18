import threading
import re
import serial
from serial.tools import list_ports
import time
import importlib
import inspect

makcu = None
makcu_lock = threading.Lock()
makcu_device = None
makcu_buttons_enabled = False
button_states = {i: False for i in range(5)}
button_states_lock = threading.Lock()
is_connected = False
last_value = 0
listener_thread = None

SUPPORTED_DEVICES = [
    ("1A86:55D3", "MAKCU"),
    ("1A86:5523", "CH343"),
    ("1A86:7523", "CH340"),
    ("1A86:5740", "CH347"),
    ("10C4:EA60", "CP2102"),
]
MAKCU_BAUD_RATES = [4_000_000, 2_000_000, 115_200]
GENERIC_BAUD_RATES = [115_200, 2_000_000, 4_000_000]
BAUD_CHANGE_COMMAND = bytearray([0xDE, 0xAD, 0x05, 0x00, 0xA5, 0x00, 0x09, 0x3D, 0x00])

def find_com_ports():
    found = []
    for port in list_ports.comports():
        hwid = port.hwid.upper()
        desc = port.description.upper()
        for vidpid, name in SUPPORTED_DEVICES:
            if vidpid in hwid or name.upper() in desc:
                found.append((port.device, name))
                break
    return found

def km_version_ok(ser):
    try:
        ser.reset_input_buffer()
        ser.write(b"km.version()\r")
        ser.flush()
        time.sleep(0.1)
        resp = b""
        start = time.time()
        while time.time() - start < 0.3:
            if ser.in_waiting:
                resp += ser.read(ser.in_waiting)
                if b"km.MAKCU" in resp or b"MAKCU" in resp:
                    return True
            time.sleep(0.01)
        return False
    except Exception as e:
        print(f"[WARN] km_version_ok: {e}")
        return False

def init_kmbox(ser):
    try:
        ser.reset_input_buffer()
        ser.write(b"import km\r\n")
        ser.flush()
        time.sleep(0.01)
        if ser.in_waiting:
            ser.read(ser.in_waiting)
    except Exception as e:
        print(f"[WARN] init_kmbox import: {e}")
    try:
        ser.write(b"km.buttons(1)\r\n")
        ser.flush()
        time.sleep(0.01)
        if ser.in_waiting:
            ser.read(ser.in_waiting)
    except Exception as e:
        print(f"[WARN] init_kmbox buttons: {e}")

def _call_with_supported_kwargs(fn, **kwargs):
    try:
        params = inspect.signature(fn).parameters
    except Exception:
        return fn()
    supported = {k: v for k, v in kwargs.items() if k in params}
    return fn(**supported)

def _get_makcu_lib_device(port, baud):
    try:
        makcu_lib = importlib.import_module("makcu")
    except Exception:
        return None, None
    create_controller = getattr(makcu_lib, "create_controller", None)
    mouse_button = getattr(makcu_lib, "MouseButton", None)
    if not callable(create_controller):
        return None, None
    kwargs = {
        "port": port,
        "baudrate": baud,
        "baud": baud,
        "auto_reconnect": True,
        "debug": False,
    }
    try:
        controller = _call_with_supported_kwargs(create_controller, **kwargs)
    except Exception:
        return None, None
    return controller, mouse_button

def _read_buttons_from_device(dev):
    for attr in ("get_button_states", "get_buttons", "buttons", "read_buttons", "get_button_mask"):
        fn = getattr(dev, attr, None)
        if callable(fn):
            try:
                value = fn()
            except Exception:
                continue
            if isinstance(value, int):
                return value
            if isinstance(value, (list, tuple)) and len(value) >= 5:
                mask = 0
                for i, v in enumerate(value[:5]):
                    if bool(v):
                        mask |= 1 << i
                return mask
            if isinstance(value, dict):
                if "mask" in value and isinstance(value["mask"], int):
                    return value["mask"]
                if "buttons" in value and isinstance(value["buttons"], (list, tuple)):
                    mask = 0
                    for i, v in enumerate(value["buttons"][:5]):
                        if bool(v):
                            mask |= 1 << i
                    return mask
    return None

def _set_button_state(idx, pressed):
    with button_states_lock:
        button_states[idx] = bool(pressed)

def _setup_makcu_button_monitoring(controller, mouse_button):
    global makcu_buttons_enabled
    if controller is None:
        return
    if mouse_button is None:
        return

    mapping = {
        getattr(mouse_button, "LEFT", None): 0,
        getattr(mouse_button, "RIGHT", None): 1,
        getattr(mouse_button, "MIDDLE", None): 2,
        getattr(mouse_button, "MOUSE4", None): 3,
        getattr(mouse_button, "MOUSE5", None): 4,
    }

    def _on_button_event(button, pressed):
        idx = mapping.get(button)
        if idx is None and hasattr(button, "name"):
            name = str(button.name).upper()
            name_map = {"LEFT": 0, "RIGHT": 1, "MIDDLE": 2, "MOUSE4": 3, "MOUSE5": 4}
            idx = name_map.get(name)
        if idx is not None:
            _set_button_state(idx, pressed)

    set_callback = getattr(controller, "set_button_callback", None)
    if callable(set_callback):
        try:
            set_callback(_on_button_event)
        except Exception:
            pass

    enable_monitoring = getattr(controller, "enable_button_monitoring", None)
    if callable(enable_monitoring):
        try:
            enable_monitoring(True)
            makcu_buttons_enabled = True
        except Exception:
            makcu_buttons_enabled = False

def connect_to_makcu():
    global makcu, makcu_device, makcu_buttons_enabled, is_connected
    ports = find_com_ports()
    if not ports:
        print("[ERROR] No supported serial devices found.")
        return False

    for port_name, dev_name in ports:
        for baud in (4_000_000, 115_200):
            makcu_device, mouse_button = _get_makcu_lib_device(port_name, baud)
            if makcu_device:
                _setup_makcu_button_monitoring(makcu_device, mouse_button)
                is_connected = True
                print(f"[INFO] Connected to {dev_name} on {port_name} with makcu library.")
                return True
        if dev_name == "MAKCU":
            for baud in MAKCU_BAUD_RATES:
                print(f"[INFO] Probing MAKCU {port_name} @ {baud} with km.version()...")
                ser = None
                try:
                    ser = serial.Serial(port_name, baud, timeout=0.3)
                    time.sleep(0.1)
                    if km_version_ok(ser):
                        if baud == 115_200:
                            print("[INFO] MAKCU responded at 115200, sending 4M handshake...")
                            ser.write(BAUD_CHANGE_COMMAND)
                            ser.flush()
                            ser.close()
                            time.sleep(0.15)
                            # --- Always cleanup before opening new connection! ---
                            ser4m = None
                            try:
                                ser4m = serial.Serial(port_name, 4_000_000, timeout=0.3)
                                time.sleep(0.1)
                                if km_version_ok(ser4m):
                                    print(f"[INFO] MAKCU handshake successful, switching to 4M on {port_name}.")
                                    ser4m.close()
                                    time.sleep(0.1)
                                    makcu = serial.Serial(port_name, 4_000_000, timeout=0.1)
                                    with makcu_lock:
                                        makcu.write(b"km.buttons(1)\r")
                                        makcu.flush()
                                    is_connected = True
                                    return True
                                else:
                                    print("[WARN] 4M handshake failed, staying at 115200.")
                                    ser4m.close()
                                    time.sleep(0.1)
                                    makcu = serial.Serial(port_name, 115_200, timeout=0.1)
                                    with makcu_lock:
                                        makcu.write(b"km.buttons(1)\r")
                                        makcu.flush()
                                    is_connected = True
                                    return True
                            except Exception as e:
                                print(f"[WARN] Could not switch to 4M: {e}")
                                if ser4m:
                                    try:
                                        ser4m.close()
                                    except:
                                        pass
                                time.sleep(0.1)
                                makcu = serial.Serial(port_name, 115_200, timeout=0.1)
                                with makcu_lock:
                                    makcu.write(b"km.buttons(1)\r")
                                    makcu.flush()
                                is_connected = True
                                return True
                        else:
                            print(f"[INFO] MAKCU responded at {baud}, using it.")
                            ser.close()
                            time.sleep(0.1)
                            makcu = serial.Serial(port_name, baud, timeout=0.1)
                            with makcu_lock:
                                makcu.write(b"km.buttons(1)\r")
                                makcu.flush()
                            is_connected = True
                            return True
                    ser.close()
                    time.sleep(0.1)
                except Exception as e:
                    print(f"[WARN] Failed MAKCU@{baud}: {e}")
                    if ser:
                        try:
                            ser.close()
                        except:
                            pass
                        time.sleep(0.1)
                    if makcu and makcu.is_open:
                        makcu.close()
                    makcu = None
                    is_connected = False
        else:
            for baud in GENERIC_BAUD_RATES:
                print(f"[INFO] Trying {dev_name} {port_name} @ {baud} ...")
                ser = None
                try:
                    makcu_device, mouse_button = _get_makcu_lib_device(port_name, baud)
                    if makcu_device:
                        _setup_makcu_button_monitoring(makcu_device, mouse_button)
                        is_connected = True
                        print(f"[INFO] Connected to {dev_name} on {port_name} with makcu library.")
                        return True
                    ser = serial.Serial(port_name, baud, timeout=0.1)
                    with makcu_lock:
                        init_kmbox(ser)
                    ser.close()
                    time.sleep(0.1)
                    makcu = serial.Serial(port_name, baud, timeout=0.1)
                    is_connected = True
                    print(f"[INFO] Connected to {dev_name} on {port_name} at {baud} baud.")
                    return True
                except Exception as e:
                    print(f"[WARN] Failed {dev_name}@{baud}: {e}")
                    if ser:
                        try:
                            ser.close()
                        except:
                            pass
                        time.sleep(0.1)
                    if makcu and makcu.is_open:
                        makcu.close()
                    makcu = None
                    makcu_device = None
                    makcu_buttons_enabled = False
                    is_connected = False

    print("[ERROR] Could not connect to any supported device.")
    return False


def count_bits(n: int) -> int:
    return bin(n).count("1")

def _apply_button_mask(v: int):
    global last_value
    if not isinstance(v, int) or v < 0 or v > 31:
        return
    changed = last_value ^ v
    if changed:
        with button_states_lock:
            for i in range(5):
                m = 1 << i
                if changed & m:
                    button_states[i] = bool(v & m)
        last_value = v

def _extract_mask_from_line(line: str):
    matches = re.findall(r"\d+", line)
    if not matches:
        return None
    try:
        value = int(matches[-1])
    except ValueError:
        return None
    return value if 0 <= value <= 31 else None

def listen_makcu():
    global last_value
    # start from a clean state
    last_value = 0
    with button_states_lock:
        for i in range(5):
            button_states[i] = False

    ascii_buffer = bytearray()
    last_ascii_time = 0.0

    while is_connected:
        try:
            if makcu_device is not None:
                if not makcu_buttons_enabled:
                    value = _read_buttons_from_device(makcu_device)
                    if value is not None:
                        _apply_button_mask(value)
                time.sleep(0.01)
                continue
            chunk = makcu.read(makcu.in_waiting or 1)  # blocking read (uses port timeout)
            if not chunk:
                continue

            for byte in chunk:
                # direct bitmask (0..31)
                if byte <= 31:
                    if ascii_buffer:
                        ascii_buffer.clear()
                    _apply_button_mask(byte)
                    continue

                # handle ASCII-encoded mask lines like "31\r\n" or "Buttons: 31"
                if byte in (0x0A, 0x0D):
                    if ascii_buffer:
                        line = ascii_buffer.decode("ascii", "ignore")
                        value = _extract_mask_from_line(line)
                        if value is not None:
                            _apply_button_mask(value)
                        ascii_buffer.clear()
                    continue

                # collect printable ASCII to parse line-based outputs
                if 0x20 <= byte <= 0x7E:
                    last_ascii_time = time.time()
                    if 0x30 <= byte <= 0x39 and not ascii_buffer and makcu.in_waiting == 0:
                        value = _extract_mask_from_line(chr(byte))
                        if value is not None:
                            _apply_button_mask(value)
                        continue
                    if len(ascii_buffer) < 64:
                        ascii_buffer.append(byte)
                    continue

                # any other junk resets the ASCII buffer
                if ascii_buffer:
                    line = ascii_buffer.decode("ascii", "ignore")
                    value = _extract_mask_from_line(line)
                    if value is not None:
                        _apply_button_mask(value)
                    ascii_buffer.clear()

            if ascii_buffer and (time.time() - last_ascii_time) > 0.05:
                line = ascii_buffer.decode("ascii", "ignore")
                value = _extract_mask_from_line(line)
                if value is not None:
                    _apply_button_mask(value)
                ascii_buffer.clear()

        except serial.SerialException as e:
            print(f"[ERROR] Listener serial exception: {e}")
            break
        except Exception as e:
            # swallow transient errors but keep running
            print(f"[WARN] Listener error: {e}")
            time.sleep(0.001)

    # ensure clean state on exit
    with button_states_lock:
        for i in range(5):
            button_states[i] = False
    last_value = 0

def start_listener():
    global listener_thread
    if not is_connected:
        return
    if listener_thread and listener_thread.is_alive():
        return
    listener_thread = threading.Thread(target=listen_makcu, daemon=True)
    listener_thread.start()

def is_button_pressed(idx: int) -> bool:
    with button_states_lock:
        return button_states.get(idx, False)

def test_move():
    if is_connected:
        if makcu_device is not None:
            move_fn = getattr(makcu_device, "move", None)
            if callable(move_fn):
                move_fn(100, 100)
                return
        with makcu_lock:
            makcu.write(b"km.move(100,100)\r")
            makcu.flush()

# --------------------------------------------------------------------
# Button Lock / Masking helpers
# --------------------------------------------------------------------

# Index mapping: 0=L, 1=R, 2=M, 3=S4, 4=S5
_LOCK_CMD_BY_IDX = {
    0: "lock_ml",
    1: "lock_mr",
    2: "lock_mm",
    3: "lock_ms1",
    4: "lock_ms2",
}

# State tracked by mask manager (so we only send lock/unlock when needed)
_mask_applied_idx = None

def _send_cmd_no_wait(cmd: str):
    """Send 'km.<cmd>\\r' without waiting for response (listener ignores ASCII)."""
    if not is_connected:
        return
    if makcu_device is not None:
        send_fn = getattr(makcu_device, "send", None)
        if callable(send_fn):
            try:
                send_fn(f"km.{cmd}")
                return
            except Exception:
                pass
        if cmd.startswith("lock_") or cmd.endswith("(1)") or cmd.endswith("(0)"):
            lock_fn = getattr(makcu_device, "lock", None)
            unlock_fn = getattr(makcu_device, "unlock", None)
            if callable(lock_fn) or callable(unlock_fn):
                suffix = cmd.split("(")[-1].rstrip(")")
                enable = suffix == "1"
                target = cmd.split("(")[0]
                target = target.replace("lock_", "").upper()
                if enable and callable(lock_fn):
                    try:
                        lock_fn(target)
                        return
                    except Exception:
                        pass
                if not enable and callable(unlock_fn):
                    try:
                        unlock_fn(target)
                        return
                    except Exception:
                        pass
    with makcu_lock:
        makcu.write(f"km.{cmd}\r".encode("ascii", "ignore"))
        makcu.flush()

def lock_button_idx(idx: int):
    """Lock a single button by index (0..4)."""
    cmd = _LOCK_CMD_BY_IDX.get(idx)
    if cmd is None:
        return
    _send_cmd_no_wait(f"{cmd}(1)")

def unlock_button_idx(idx: int):
    """Unlock a single button by index (0..4)."""
    cmd = _LOCK_CMD_BY_IDX.get(idx)
    if cmd is None:
        return
    _send_cmd_no_wait(f"{cmd}(0)")

def unlock_all_locks():
    """Best-effort unlock of all lockable buttons."""
    for i in range(5):
        unlock_button_idx(i)

def mask_manager_tick(selected_idx: int, aimbot_running: bool):
    """Manage button locks based on selected_idx and aimbot_running state."""
    
    global _mask_applied_idx

    if not is_connected:
        _mask_applied_idx = None
        return

    # clamp invalid index
    if not isinstance(selected_idx, int) or not (0 <= selected_idx <= 4):
        selected_idx = None

    if not aimbot_running:
        if _mask_applied_idx is not None:
            unlock_button_idx(_mask_applied_idx)
            _mask_applied_idx = None
        return

    # running: apply lock for selected_idx
    if selected_idx is None:
        # nothing selected -> make sure nothing is locked
        if _mask_applied_idx is not None:
            unlock_button_idx(_mask_applied_idx)
            _mask_applied_idx = None
        return

    if _mask_applied_idx != selected_idx:
        # switch lock to a new button
        if _mask_applied_idx is not None:
            unlock_button_idx(_mask_applied_idx)
        lock_button_idx(selected_idx)
        _mask_applied_idx = selected_idx

class Mouse:
    _instance = None
    _listener = None
    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self):
        if not hasattr(self, "_inited"):
            if not connect_to_makcu():
                print("[ERROR] Mouse init failed to connect.")
            else:
                Mouse._listener = threading.Thread(target=listen_makcu, daemon=True)
                Mouse._listener.start()
            self._inited = True

    def move(self, x: float, y: float):
        if not is_connected:
            return
        dx, dy = int(x), int(y)
        if makcu_device is not None:
            move_fn = getattr(makcu_device, "move", None)
            if callable(move_fn):
                move_fn(dx, dy)
                return
        with makcu_lock:
            makcu.write(f"km.move({dx},{dy})\r".encode())
            makcu.flush()

    def move_bezier(self, x: float, y: float, segments: int, ctrl_x: float, ctrl_y: float):
        if not is_connected:
            return
        if makcu_device is not None:
            move_fn = getattr(makcu_device, "move_bezier", None)
            if callable(move_fn):
                move_fn(int(x), int(y), int(segments), int(ctrl_x), int(ctrl_y))
                return
            move_fn = getattr(makcu_device, "move_smooth", None)
            if callable(move_fn):
                move_fn(int(x), int(y), int(segments))
                return
        with makcu_lock:
            cmd = f"km.move({int(x)},{int(y)},{int(segments)},{int(ctrl_x)},{int(ctrl_y)})\r"
            makcu.write(cmd.encode())
            makcu.flush()

    def click(self):
        if not is_connected:
            return
        if makcu_device is not None:
            click_fn = getattr(makcu_device, "click", None)
            if callable(click_fn):
                click_fn()
                return
        with makcu_lock:
            makcu.write(b"km.left(1)\r")
            makcu.flush()
            makcu.write(b"km.left(0)\r")
            makcu.flush()

    @staticmethod
    def mask_manager_tick(selected_idx: int, aimbot_running: bool):
        """Static wrapper so callers can do: Mouse.mask_manager_tick(idx, running)."""
        mask_manager_tick(selected_idx, aimbot_running)

    @staticmethod
    def cleanup():
        global is_connected, makcu, makcu_device, _mask_applied_idx, listener_thread
        # Always release any locks before closing port
        try:
            unlock_all_locks()
        except Exception:
            pass
        _mask_applied_idx = None
        listener_thread = None

        is_connected = False
        if makcu_device is not None:
            for fn_name in ("close", "disconnect", "stop"):
                fn = getattr(makcu_device, fn_name, None)
                if callable(fn):
                    try:
                        fn()
                    except Exception:
                        pass
            makcu_device = None
        if makcu and makcu.is_open:
            makcu.close()
        Mouse._instance = None
        Mouse._listener = None
        print("[INFO] Mouse serial cleaned up.")
