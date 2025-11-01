# -*- coding: utf-8 -*-
"""Định nghĩa các hằng số và giá trị mặc định toàn cục."""
import os
from threading import Lock

# --- Cấu hình File ---
CONFIG_FILE = 'config.json'
SORT_LOG_FILE = 'sort_log.json'
YOLO_MODEL_PATH = 'yolov8_qr.pt' # Tên file model YOLO

# --- Cấu hình Mặc định GPIO/Lanes ---
# (!!!) (SỬA) THÊM CHÂN GÁC CỔNG (ENTRY) VÀ TỐI ƯU HÓA CÁC CHÂN KHÁC
PIN_ENTRY = 6 # Chân BCM 16 cho sensor gác cổng
CAMERA_INDEX = 0
ACTIVE_LOW = True  # Relay kích hoạt bằng mức LOW
USERNAME = os.environ.get("APP_USERNAME", "admin")
PASSWORD = os.environ.get("APP_PASSWORD", "123")
AUTH_ENABLED = os.environ.get("APP_AUTH_ENABLED", "false").strip().lower() in {"1", "true", "yes", "on"}

# (SỬA) Cấu hình chân mặc định an toàn hơn
DEFAULT_LANES_CFG = [
    {"id": "A", "name": "Phân loại A (Đẩy)", "sensor_pin": 3, "push_pin": 17, "pull_pin": 27},
    {"id": "B", "name": "Phân loại B (Đẩy)", "sensor_pin": 23, "push_pin": 22, "pull_pin": 14},
    {"id": "C", "name": "Phân loại C (Đẩy)", "sensor_pin": 24, "push_pin": 4, "pull_pin": 25},
    {"id": "D", "name": "Lane D (Đi thẳng/Thoát)", "sensor_pin": 5, "push_pin": None, "pull_pin": None},
]

DEFAULT_TIMING_CFG = {
    "cycle_delay": 0.3, "settle_delay": 0.2, "sensor_debounce": 0.1,
    "push_delay": 0.0, "gpio_mode": "BCM",
    "queue_head_timeout": 15.0, "pending_trigger_timeout": 0.5 
    # pending_trigger_timeout không còn được dùng trong Gated FIFO, nhưng giữ lại để tương thích
}

