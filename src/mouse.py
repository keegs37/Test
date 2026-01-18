import threading
import time

try:
    from makcu import create_controller, MouseButton
except Exception:  # makcu must be installed for mouse support
    create_controller = None
    MouseButton = None

makcu = None
button_states = {i: False for i in range(5)}
button_states_lock = threading.Lock()
is_connected = False
_listener_thread = None


def _set_button_state(idx: int, pressed: bool):
    with button_states_lock:
        button_states[idx] = bool(pressed)


def _setup_button_monitoring():
    if makcu is None or MouseButton is None:
        return False

    mapping = {
        MouseButton.LEFT: 0,
        MouseButton.RIGHT: 1,
        MouseButton.MIDDLE: 2,
        MouseButton.MOUSE4: 3,
        MouseButton.MOUSE5: 4,
    }

    def _on_button_event(button, pressed):
        idx = mapping.get(button)
        if idx is None and hasattr(button, "name"):
            name = str(button.name).upper()
            name_map = {"LEFT": 0, "RIGHT": 1, "MIDDLE": 2, "MOUSE4": 3, "MOUSE5": 4}
            idx = name_map.get(name)
        if idx is not None:
            _set_button_state(idx, pressed)

    set_callback = getattr(makcu, "set_button_callback", None)
    enable_monitoring = getattr(makcu, "enable_button_monitoring", None)
    if callable(set_callback) and callable(enable_monitoring):
        try:
            set_callback(_on_button_event)
            enable_monitoring(True)
            return True
        except Exception:
            return False
    return False


def _poll_button_states():
    if makcu is None:
        return
    get_states = getattr(makcu, "get_button_states", None)
    is_pressed = getattr(makcu, "is_pressed", None)
    if callable(get_states):
        try:
            states = get_states()
        except Exception:
            return
        if isinstance(states, dict):
            for key, idx in (("LEFT", 0), ("RIGHT", 1), ("MIDDLE", 2), ("MOUSE4", 3), ("MOUSE5", 4)):
                if key in states:
                    _set_button_state(idx, states[key])
        elif isinstance(states, (list, tuple)):
            for i, value in enumerate(states[:5]):
                _set_button_state(i, value)
        return
    if callable(is_pressed) and MouseButton is not None:
        try:
            _set_button_state(0, is_pressed(MouseButton.LEFT))
            _set_button_state(1, is_pressed(MouseButton.RIGHT))
            _set_button_state(2, is_pressed(MouseButton.MIDDLE))
            _set_button_state(3, is_pressed(MouseButton.MOUSE4))
            _set_button_state(4, is_pressed(MouseButton.MOUSE5))
        except Exception:
            return


def connect_to_makcu():
    global makcu, is_connected
    if create_controller is None:
        print("[ERROR] makcu library not installed. Please install 'makcu'.")
        return False
    try:
        makcu = create_controller(debug=False, auto_reconnect=True)
    except TypeError:
        makcu = create_controller()
    except Exception as e:
        print(f"[ERROR] makcu create_controller failed: {e}")
        return False

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
            if not callback_enabled:
                _poll_button_states()
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
    if is_connected and makcu is not None:
        try:
            makcu.move(100, 100)
        except Exception:
            pass


def lock_button_idx(idx: int):
    if not is_connected or makcu is None or MouseButton is None:
        return
    lock_fn = getattr(makcu, "lock", None)
    if not callable(lock_fn):
        return
    mapping = [MouseButton.LEFT, MouseButton.RIGHT, MouseButton.MIDDLE, MouseButton.MOUSE4, MouseButton.MOUSE5]
    if 0 <= idx < len(mapping):
        try:
            lock_fn(mapping[idx])
        except Exception:
            pass


def unlock_button_idx(idx: int):
    if not is_connected or makcu is None or MouseButton is None:
        return
    unlock_fn = getattr(makcu, "unlock", None)
    if not callable(unlock_fn):
        return
    mapping = [MouseButton.LEFT, MouseButton.RIGHT, MouseButton.MIDDLE, MouseButton.MOUSE4, MouseButton.MOUSE5]
    if 0 <= idx < len(mapping):
        try:
            unlock_fn(mapping[idx])
        except Exception:
            pass


def unlock_all_locks():
    for i in range(5):
        unlock_button_idx(i)


_mask_applied_idx = None


def mask_manager_tick(selected_idx: int, aimbot_running: bool):
    global _mask_applied_idx
    if not is_connected:
        _mask_applied_idx = None
        return

    if not isinstance(selected_idx, int) or not (0 <= selected_idx <= 4):
        selected_idx = None

    if not aimbot_running:
        if _mask_applied_idx is not None:
            unlock_button_idx(_mask_applied_idx)
            _mask_applied_idx = None
        return

    if selected_idx is None:
        if _mask_applied_idx is not None:
            unlock_button_idx(_mask_applied_idx)
            _mask_applied_idx = None
        return

    if _mask_applied_idx != selected_idx:
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
                Mouse._listener = _listener_thread
            self._inited = True

    def move(self, x: float, y: float):
        if not is_connected or makcu is None:
            return
        try:
            makcu.move(int(x), int(y))
        except Exception:
            pass

    def move_bezier(self, x: float, y: float, segments: int, ctrl_x: float, ctrl_y: float):
        if not is_connected or makcu is None:
            return
        move_bezier = getattr(makcu, "move_bezier", None)
        if callable(move_bezier):
            try:
                move_bezier(int(x), int(y), int(segments), int(ctrl_x), int(ctrl_y))
                return
            except Exception:
                pass
        move_smooth = getattr(makcu, "move_smooth", None)
        if callable(move_smooth):
            try:
                move_smooth(int(x), int(y), int(segments))
                return
            except Exception:
                pass

    def click(self):
        if not is_connected or makcu is None:
            return
        try:
            makcu.click(MouseButton.LEFT if MouseButton else None)
        except Exception:
            try:
                makcu.click()
            except Exception:
                pass

    def press(self):
        if not is_connected or makcu is None or MouseButton is None:
            return
        press_fn = getattr(makcu, "press", None)
        if callable(press_fn):
            try:
                press_fn(MouseButton.LEFT)
            except Exception:
                pass

    def release(self):
        if not is_connected or makcu is None or MouseButton is None:
            return
        release_fn = getattr(makcu, "release", None)
        if callable(release_fn):
            try:
                release_fn(MouseButton.LEFT)
            except Exception:
                pass

    @staticmethod
    def mask_manager_tick(selected_idx: int, aimbot_running: bool):
        mask_manager_tick(selected_idx, aimbot_running)

    @staticmethod
    def cleanup():
        global is_connected, makcu, _mask_applied_idx, _listener_thread
        try:
            unlock_all_locks()
        except Exception:
            pass
        _mask_applied_idx = None

        is_connected = False
        if makcu is not None:
            disconnect_fn = getattr(makcu, "disconnect", None)
            if callable(disconnect_fn):
                try:
                    disconnect_fn()
                except Exception:
                    pass
        makcu = None
        Mouse._instance = None
        Mouse._listener = None
        _listener_thread = None
        print("[INFO] Mouse serial cleaned up.")
