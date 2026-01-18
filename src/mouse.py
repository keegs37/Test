import threading
import time
import importlib
import inspect

try:
    from makcu import create_controller, MouseButton
except Exception:  # makcu must be installed for mouse support
    create_controller = None
    MouseButton = None

from config import config

try:
    import kmNet
except Exception:
    kmNet = None

from config import config

try:
    import kmNet
except Exception:
    kmNet = None

from config import config

try:
    import kmNet
except Exception:
    kmNet = None

from config import config

try:
    import kmNet
except Exception:
    kmNet = None

from config import config

try:
    import kmNet
except Exception:
    kmNet = None

from config import config

try:
    import kmNet
except Exception:
    kmNet = None

button_states = {i: False for i in range(5)}
button_states_lock = threading.Lock()
is_connected = False
_listener_thread = None


def _set_button_state(idx: int, pressed: bool):
    with button_states_lock:
        button_states[idx] = bool(pressed)


def _read_button(fn_name: str):
    if kmNet is None:
        return False
    fn = getattr(kmNet, fn_name, None)
    if callable(fn):
        try:
            return bool(fn())
        except Exception:
            return False
    return False


def connect_to_makcu():
    global is_connected
    if kmNet is None:
        print("[ERROR] kmNet module not available. Ensure kmNet.pyd is installed in your Python env.")
        return False

    ip = str(getattr(config, "kmnet_ip", "")).strip()
    port = str(getattr(config, "kmnet_port", "")).strip()
    mac = str(getattr(config, "kmnet_mac", "")).strip()
    monitor_port = int(getattr(config, "kmnet_monitor_port", 0))

    if not ip or not port or not mac:
        print("[ERROR] kmNet config missing. Set kmnet_ip, kmnet_port, and kmnet_mac in config.")
        return False

    try:
        result = kmNet.init(ip, port, mac)
    except Exception as e:
        print(f"[ERROR] kmNet init failed: {e}")
        return False

    if result != 0:
        print(f"[ERROR] kmNet init failed with code {result}")
        return False

    if monitor_port:
        try:
            kmNet.monitor(monitor_port)
        except Exception as e:
            print(f"[WARN] kmNet monitor failed: {e}")

    is_connected = True
    start_listener()
    return True


def listen_makcu():
    with button_states_lock:
        for i in range(5):
            button_states[i] = False

    callback_enabled = _setup_button_monitoring()

    while is_connected:
        try:
            _set_button_state(0, _read_button("isdown_left"))
            _set_button_state(1, _read_button("isdown_right"))
            _set_button_state(2, _read_button("isdown_middle"))
            _set_button_state(3, _read_button("isdown_side1") or _read_button("isdown_x1"))
            _set_button_state(4, _read_button("isdown_side2") or _read_button("isdown_x2"))
            time.sleep(0.01)
        except Exception:
            time.sleep(0.01)

    with button_states_lock:
        for i in range(5):
            button_states[i] = False


def start_listener():
    global _listener_thread
    if not is_connected:
        return
    if _listener_thread and _listener_thread.is_alive():
        return
    _listener_thread = threading.Thread(target=listen_makcu, daemon=True)
    _listener_thread.start()


def is_button_pressed(idx: int) -> bool:
    with button_states_lock:
        return button_states.get(idx, False)


def test_move():
    if is_connected and kmNet is not None:
        try:
            kmNet.move(100, 100)
        except Exception:
            pass


def lock_button_idx(idx: int):
    # kmNet does not expose button locks; no-op for compatibility.
    return


def unlock_button_idx(idx: int):
    # kmNet does not expose button locks; no-op for compatibility.
    return


def unlock_all_locks():
    return


_mask_applied_idx = None


def mask_manager_tick(selected_idx: int, aimbot_running: bool):
    # kmNet does not expose button lock management; keep interface for callers.
    return


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
                Mouse._listener = _listener_thread
            self._inited = True

    def move(self, x: float, y: float):
        if not is_connected or kmNet is None:
            return
        try:
            kmNet.move(int(x), int(y))
        except Exception:
            pass

    def move_bezier(self, x: float, y: float, segments: int, ctrl_x: float, ctrl_y: float):
        if not is_connected or kmNet is None:
            return
        move_bezier = getattr(kmNet, "move_beizer", None)
        if callable(move_bezier):
            ms = max(1, int(segments) * 10)
            try:
                move_bezier(int(x), int(y), ms, int(ctrl_x), int(ctrl_y), int(ctrl_x), int(ctrl_y))
                return
            except Exception:
                pass
        move_auto = getattr(kmNet, "move_auto", None)
        if callable(move_auto):
            ms = max(1, int(segments) * 10)
            try:
                move_auto(int(x), int(y), ms)
            except Exception:
                pass

    def click(self):
        if not is_connected or kmNet is None:
            return
        try:
            kmNet.left(1)
            kmNet.left(0)
        except Exception:
            pass

    def press(self):
        if not is_connected or kmNet is None:
            return
        try:
            kmNet.left(1)
        except Exception:
            pass

    def release(self):
        if not is_connected or kmNet is None:
            return
        try:
            kmNet.left(0)
        except Exception:
            pass

    @staticmethod
    def mask_manager_tick(selected_idx: int, aimbot_running: bool):
        mask_manager_tick(selected_idx, aimbot_running)

    @staticmethod
    def cleanup():
        global is_connected, _mask_applied_idx, _listener_thread
        _mask_applied_idx = None
        is_connected = False
        if kmNet is not None:
            monitor_port = int(getattr(config, "kmnet_monitor_port", 0))
            if monitor_port:
                try:
                    kmNet.monitor(0)
                except Exception:
                    pass
        Mouse._instance = None
        Mouse._listener = None
        _listener_thread = None
        print("[INFO] Mouse kmNet cleaned up.")
