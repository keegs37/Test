import threading
import time
import importlib
import importlib.util

from config import config

_macku_spec = importlib.util.find_spec("macku")
_macku_module = importlib.import_module("macku") if _macku_spec is not None else None

_kmnet_spec = importlib.util.find_spec("kmNet")
kmNet = importlib.import_module("kmNet") if _kmnet_spec is not None else None

button_states = {i: False for i in range(5)}
button_states_lock = threading.Lock()
is_connected = False
_listener_thread = None
_active_backend = None
_macku_controller = None


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


def _resolve_macku_button(name: str):
    if _macku_module is None:
        return None
    mouse_button_enum = getattr(_macku_module, "MouseButton", None)
    if mouse_button_enum is None:
        return None
    for candidate in (name, name.upper(), f"{name.upper()}_BUTTON"):
        if hasattr(mouse_button_enum, candidate):
            return getattr(mouse_button_enum, candidate)
    return None


def _macku_read_button(idx: int) -> bool:
    if _macku_controller is None:
        return False
    name_map = {0: "left", 1: "right", 2: "middle", 3: "side1", 4: "side2"}
    button_name = name_map.get(idx, "left")
    button_token = _resolve_macku_button(button_name) or button_name
    for method_name in ("is_pressed", "is_button_pressed", "get_button_state"):
        method = getattr(_macku_controller, method_name, None)
        if callable(method):
            try:
                return bool(method(button_token))
            except Exception:
                continue
    return False


def _macku_move(dx: float, dy: float):
    if _macku_controller is None:
        return
    for method_name in ("move", "move_rel", "move_relative", "mouse_move"):
        method = getattr(_macku_controller, method_name, None)
        if callable(method):
            try:
                method(int(dx), int(dy))
                return
            except Exception:
                continue


def _macku_click():
    if _macku_controller is None:
        return
    for method_name in ("click", "left_click", "mouse_click"):
        method = getattr(_macku_controller, method_name, None)
        if callable(method):
            try:
                method()
                return
            except Exception:
                continue


def _macku_press():
    if _macku_controller is None:
        return
    for method_name in ("press", "left_press", "mouse_down"):
        method = getattr(_macku_controller, method_name, None)
        if callable(method):
            try:
                method()
                return
            except Exception:
                continue


def _macku_release():
    if _macku_controller is None:
        return
    for method_name in ("release", "left_release", "mouse_up"):
        method = getattr(_macku_controller, method_name, None)
        if callable(method):
            try:
                method()
                return
            except Exception:
                continue


def _connect_kmnet():
    global is_connected
    if is_connected:
        return True
    if kmNet is None:
        print("[ERROR] kmNet module not available. Ensure kmNet.pyd is installed in your Python env.")
        config.makcu_status_msg = "kmNet module missing"
        return False

    ip = str(getattr(config, "kmnet_ip", "")).strip()
    port = str(getattr(config, "kmnet_port", "")).strip()
    mac = str(getattr(config, "kmnet_mac", "")).strip()
    monitor_port = int(getattr(config, "kmnet_monitor_port", 0))

    if not ip or not port or not mac:
        print("[ERROR] kmNet config missing. Set kmnet_ip, kmnet_port, and kmnet_mac in config.")
        config.makcu_status_msg = "kmNet config missing"
        return False

    try:
        result = kmNet.init(ip, port, mac)
    except Exception as e:
        print(f"[ERROR] kmNet init failed: {e}")
        config.makcu_status_msg = f"kmNet init failed: {e}"
        return False

    if result != 0:
        print(f"[ERROR] kmNet init failed with code {result}")
        config.makcu_status_msg = f"kmNet init failed ({result})"
        return False

    if monitor_port:
        try:
            kmNet.monitor(monitor_port)
        except Exception as e:
            print(f"[WARN] kmNet monitor failed: {e}")

    is_connected = True
    config.makcu_status_msg = "Connected (kmNet)"
    start_listener()
    return True


def _connect_macku():
    global is_connected, _macku_controller
    if is_connected:
        return True
    if _macku_module is None:
        print("[ERROR] macku module not available. Install the macku Python library.")
        config.makcu_status_msg = "macku module missing"
        return False

    controller = None
    for attr in ("create_controller", "Controller", "Macku", "Device"):
        factory = getattr(_macku_module, attr, None)
        if callable(factory):
            try:
                controller = factory()
                break
            except Exception:
                continue
    if controller is None:
        print("[ERROR] macku controller factory not found.")
        config.makcu_status_msg = "macku controller not found"
        return False

    connect_fn = getattr(controller, "connect", None)
    if callable(connect_fn):
        try:
            if connect_fn() is False:
                config.makcu_status_msg = "macku connect failed"
                return False
        except Exception as e:
            print(f"[ERROR] macku connect failed: {e}")
            config.makcu_status_msg = f"macku connect failed: {e}"
            return False

    _macku_controller = controller
    is_connected = True
    config.makcu_status_msg = "Connected (macku)"
    start_listener()
    return True


def connect_to_makcu():
    device = str(getattr(config, "input_device", "kmnet")).strip().lower()
    global _active_backend
    if device == "macku":
        _active_backend = "macku"
        return _connect_macku()
    _active_backend = "kmnet"
    return _connect_kmnet()


def listen_makcu():
    with button_states_lock:
        for i in range(5):
            button_states[i] = False

    _setup_button_monitoring()

    while is_connected:
        try:
            if _active_backend == "macku":
                for i in range(5):
                    _set_button_state(i, _macku_read_button(i))
            else:
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


def _setup_button_monitoring() -> bool:
    if not is_connected or _active_backend != "kmnet" or kmNet is None:
        return False
    monitor_port = int(getattr(config, "kmnet_monitor_port", 0))
    if not monitor_port:
        return False
    try:
        kmNet.monitor(monitor_port)
    except Exception as e:
        print(f"[WARN] kmNet monitor failed: {e}")
        return False
    return True


def is_button_pressed(idx: int) -> bool:
    with button_states_lock:
        return button_states.get(idx, False)


def test_move():
    if is_connected:
        if _active_backend == "macku":
            _macku_move(100, 100)
            return
        if kmNet is not None:
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
        if not is_connected:
            return
        if _active_backend == "macku":
            _macku_move(x, y)
            return
        if kmNet is None:
            return
        try:
            kmNet.move(int(x), int(y))
        except Exception:
            pass

    def move_bezier(self, x: float, y: float, segments: int, ctrl_x: float, ctrl_y: float):
        if not is_connected:
            return
        if _active_backend == "macku":
            _macku_move(x, y)
            return
        if kmNet is None:
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
        if not is_connected:
            return
        if _active_backend == "macku":
            _macku_click()
            return
        if kmNet is None:
            return
        try:
            kmNet.left(1)
            kmNet.left(0)
        except Exception:
            pass

    def press(self):
        if not is_connected:
            return
        if _active_backend == "macku":
            _macku_press()
            return
        if kmNet is None:
            return
        try:
            kmNet.left(1)
        except Exception:
            pass

    def release(self):
        if not is_connected:
            return
        if _active_backend == "macku":
            _macku_release()
            return
        if kmNet is None:
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
        global is_connected, _mask_applied_idx, _listener_thread, _macku_controller, _active_backend
        _mask_applied_idx = None
        is_connected = False
        if _active_backend == "kmnet" and kmNet is not None:
            monitor_port = int(getattr(config, "kmnet_monitor_port", 0))
            if monitor_port:
                try:
                    kmNet.monitor(0)
                except Exception:
                    pass
        if _active_backend == "macku" and _macku_controller is not None:
            disconnect_fn = getattr(_macku_controller, "disconnect", None)
            if callable(disconnect_fn):
                try:
                    disconnect_fn()
                except Exception:
                    pass
        _macku_controller = None
        _active_backend = None
        Mouse._instance = None
        Mouse._listener = None
        _listener_thread = None
        print("[INFO] Mouse device cleaned up.")
