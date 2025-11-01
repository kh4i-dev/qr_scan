# -*- coding: utf-8 -*-
"""Module trừu tượng hóa và xử lý GPIO."""
import logging
from threading import Lock
from .constants import ACTIVE_LOW

# Thử import RPi.GPIO thật
try:
    import RPi.GPIO as RPiGPIO
except (ImportError, RuntimeError):
    RPiGPIO = None  # Đặt là None nếu thất bại (chạy trên PC)

class GPIOProvider:
    """Lớp trừu tượng (base class) để tương tác GPIO."""
    def setup(self, pin, mode, pull_up_down=None): raise NotImplementedError
    def output(self, pin, value): raise NotImplementedError
    def input(self, pin): raise NotImplementedError
    def cleanup(self): raise NotImplementedError
    def setmode(self, mode): raise NotImplementedError
    def setwarnings(self, value): raise NotImplementedError

class RealGPIO(GPIOProvider):
    """Triển khai dùng thư viện RPi.GPIO thật."""
    def __init__(self):
        if RPiGPIO is None: raise ImportError("Không thể tải thư viện RPi.GPIO.")
        self.gpio = RPiGPIO
        for attr in ['BOARD', 'BCM', 'OUT', 'IN', 'HIGH', 'LOW', 'PUD_UP']:
            setattr(self, attr, getattr(self.gpio, attr))
        self.is_real = True

    def setmode(self, mode): self.gpio.setmode(mode)
    def setwarnings(self, value): self.gpio.setwarnings(value)

    def setup(self, pin, mode, pull_up_down=None):
        if pin is None: return
        try:
            if pull_up_down: self.gpio.setup(pin, mode, pull_up_down=pull_up_down)
            else: self.gpio.setup(pin, mode)
            logging.debug(f"[GPIO] Setup pin {pin} OK.")
        except Exception as e:
            logging.error(f"[GPIO] Lỗi setup pin {pin}: {e}", exc_info=True)
            raise RuntimeError(f"Lỗi setup pin {pin}") from e

    def output(self, pin, value): 
        if pin is not None: self.gpio.output(pin, value)
    def input(self, pin): 
        if pin is not None: return self.gpio.input(pin)
        return self.HIGH
    def cleanup(self): self.gpio.cleanup()

# --- Triển khai Mock GPIO ---
class MockGPIO(GPIOProvider):
    """Triển khai GPIO giả lập (Mock) để test trên PC."""
    def __init__(self):
        for attr, val in [('BOARD', "mock_BOARD"), ('BCM', "mock_BCM"), ('OUT', "mock_OUT"),
                          ('IN', "mock_IN"), ('HIGH', 1), ('LOW', 0), ('PUD_UP', "mock_PUD_UP")]:
            setattr(self, attr, val)
        self.pin_states = {}  # Lưu trạng thái giả lập của các pin
        self.input_pins = set()
        self.override_lock = Lock()
        self.is_real = False
        logging.warning("="*50 + "\nĐANG CHẠY Ở CHẾ ĐỘ GIẢ LẬP (MOCK GPIO).\n" + "="*50)

    def setmode(self, mode): logging.info(f"[MOCK] Đặt chế độ GPIO: {mode}")
    def setwarnings(self, value): logging.info(f"[MOCK] Đặt cảnh báo: {value}")

    def setup(self, pin, mode, pull_up_down=None):
        if pin is None: return
        logging.info(f"[MOCK] Setup pin {pin} mode={mode} pull_up_down={pull_up_down}")
        with self.override_lock:
            if mode == self.OUT: self.pin_states[pin] = self.LOW
            else: self.pin_states[pin] = self.HIGH; self.input_pins.add(pin)

    def output(self, pin, value):
        if pin is None: return
        value_str = "HIGH" if value == self.HIGH else "LOW"
        logging.info(f"[MOCK] Output pin {pin} = {value_str}({value})")
        with self.override_lock:
            self.pin_states[pin] = value
            
    def input(self, pin):
        if pin is None: return self.HIGH
        with self.override_lock:
            return self.pin_states.get(pin, self.HIGH)
        
    def set_input_state(self, pin, logical_state):
        """Dùng cho API Mock (True/False -> LOW/HIGH)"""
        state = self.LOW if logical_state == 0 else self.HIGH
        with self.override_lock:
            self.pin_states[pin] = state
        return state
        
    def cleanup(self): logging.info("[MOCK] Dọn dẹp GPIO")

def get_gpio_provider():
    """Hàm factory để chọn đúng nhà cung cấp GPIO."""
    if RPiGPIO:
        logging.info("Phát hiện thư viện RPi.GPIO. Sử dụng RealGPIO.")
        return RealGPIO()
    else:
        logging.info("Không tìm thấy RPi.GPIO. Sử dụng MockGPIO.")
        return MockGPIO()

class GPIOHandler:
    def __init__(self, error_handler):
        self.gpio = get_gpio_provider()
        self.error_handler = error_handler
        self.lanes_cfg = []
        self.timing_cfg = {}
        
    def setup_pins(self, lanes_cfg, timing_cfg):
        self.lanes_cfg = lanes_cfg
        self.timing_cfg = timing_cfg
        
        gpio_mode_str = timing_cfg.get("gpio_mode", "BCM")
        mode_to_set = self.gpio.BCM if gpio_mode_str == "BCM" else self.gpio.BOARD

        try:
            self.gpio.setmode(mode_to_set)
            self.gpio.setwarnings(False)

            active_pins = self._get_active_pins(lanes_cfg)
            
            # Setup chân SENSOR (gồm cả PIN_ENTRY)
            for pin in active_pins['sensor']:
                self.gpio.setup(pin, self.gpio.IN, pull_up_down=self.gpio.PUD_UP)
            
            # Setup chân RELAY
            for pin in active_pins['relay']:
                self.gpio.setup(pin, self.gpio.OUT)
                
            logging.info(f"[GPIO] Cài đặt {len(active_pins['sensor'])} sensor và {len(active_pins['relay'])} relay hoàn tất.")
            self.reset_all_relays()

        except Exception as e:
            msg = f"Cài đặt GPIO thất bại (Mode: {gpio_mode_str}): {e}"
            logging.critical(f"[CRITICAL] {msg}", exc_info=True)
            self.error_handler.trigger_maintenance(msg)
            raise

    def _get_active_pins(self, lanes_cfg):
        sensor_pins = set()
        relay_pins = set()
        
        # Thêm các pin từ config
        for lane in lanes_cfg:
            if lane.get("sensor_pin") is not None: sensor_pins.add(lane["sensor_pin"])
            if lane.get("push_pin") is not None: relay_pins.add(lane["push_pin"])
            if lane.get("pull_pin") is not None: relay_pins.add(lane["pull_pin"])
            
        # (MỚI) Thêm PIN_ENTRY vào sensor_pins (nếu chưa có)
        from .constants import PIN_ENTRY
        sensor_pins.add(PIN_ENTRY)
        
        return {'sensor': list(sensor_pins), 'relay': list(relay_pins)}

    def relay_on(self, pin):
        if pin is None: return
        try:
            self.gpio.output(pin, self.gpio.LOW if ACTIVE_LOW else self.gpio.HIGH)
        except Exception as e:
            msg = f"Lỗi RELAY_ON pin {pin}: {e}"
            logging.error(f"[GPIO] {msg}")
            self.error_handler.trigger_maintenance(msg)
            
    def relay_off(self, pin):
        if pin is None: return
        try:
            self.gpio.output(pin, self.gpio.HIGH if ACTIVE_LOW else self.gpio.LOW)
        except Exception as e:
            msg = f"Lỗi RELAY_OFF pin {pin}: {e}"
            logging.error(f"[GPIO] {msg}")
            self.error_handler.trigger_maintenance(msg)
            
    def read_sensor(self, pin):
        if pin is None: return self.gpio.HIGH # Mặc định trả về inactive
        try:
            return self.gpio.input(pin)
        except Exception as e:
            msg = f"Lỗi đọc sensor pin {pin}: {e}"
            logging.error(f"[GPIO] {msg}")
            self.error_handler.trigger_maintenance(msg)
            return self.gpio.HIGH # Giả định là HIGH (sensor inactive) khi có lỗi

    def reset_all_relays(self):
        """Reset tất cả relay về trạng thái an toàn (Thu BẬT, Đẩy TẮT)."""
        logging.info("[GPIO] Reset tất cả relay về trạng thái mặc định (Thu BẬT, Đẩy TẮT)...")
        for lane in self.lanes_cfg:
            self.relay_on(lane.get("pull_pin"))
            self.relay_off(lane.get("push_pin"))
        logging.info("[GPIO] Reset relay hoàn tất.")

    def cleanup(self):
        logging.info("[GPIO] Dọn dẹp GPIO...")
        self.gpio.cleanup()

    def is_mock(self):
        return isinstance(self.gpio, MockGPIO)

    def mock_set_input(self, pin, logical_state):
        """API cho UI Mock."""
        if self.is_mock():
            return self.gpio.set_input_state(pin, logical_state)
        return None
