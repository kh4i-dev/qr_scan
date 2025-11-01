# -*- coding: utf-8 -*-
"""
Main Application (Orchestrator) - Logic Hybrid YOLO + Gated FIFO.
Phiên bản này áp dụng kiến trúc mô-đun (class-based) và logic Gated FIFO (có SENSOR_ENTRY).
"""
import time
import json
import threading
import logging
import os
import sys
from pathlib import Path
import functools
from concurrent.futures import ThreadPoolExecutor
from flask import Flask, render_template, Response, jsonify, request
from flask_sock import Sock
import cv2

# (SỬA) Thử import Waitress
try:
    from waitress import serve
    WAITRESS_AVAILABLE = True
except ImportError:
    serve = None
    WAITRESS_AVAILABLE = False
# --- Bổ sung PYTHONPATH để chạy được cả khi thư mục làm việc thay đổi ---
PROJECT_ROOT = Path(__file__).resolve().parent
SRC_DIR = PROJECT_ROOT / "src"
PARENT_DIR = PROJECT_ROOT.parent

for extra_path in (PROJECT_ROOT, SRC_DIR, PARENT_DIR):
    extra_str = str(extra_path)
    if extra_str not in sys.path:
        sys.path.insert(0, extra_str)


# --- Import Modules ---
from src.constants import PASSWORD, PIN_ENTRY, ACTIVE_LOW
from src.error_handler import ErrorHandler
from src.gpio_handler import GPIOHandler, get_gpio_provider
from src.system_state import SystemState
from src.config_manager import ConfigManager
from src.queue_manager import QueueManager
from src.camera_manager import CameraManager
from src.qr_scanner import QRScanner
from src.websocket_manager import WebSocketManager
from src.api_routes import APIRouter
from src.test_workers import run_test_relay_worker, run_test_all_relays_worker # (MỚI) Import Worker Test
from src.utils import canon_id # Dùng cho logic Mapping

# --- Cấu hình Logging (tối thiểu) ---
LOG_FILE = 'system.log'
log_format = '%(asctime)s [%(levelname)s] (%(threadName)s) %(message)s'
logging.basicConfig(level=logging.INFO, format=log_format,
                    handlers=[logging.FileHandler(LOG_FILE, encoding='utf-8'),
                              logging.StreamHandler()])

# =========================================================================
#             LỚP ỨNG DỤNG CHÍNH (ORCHESTRATOR)
# =========================================================================
class SortingSystem:
    def __init__(self):
        # 1. Khởi tạo Modules (Tạo đối tượng)
        self.ws_manager = WebSocketManager()
        self.error_handler = ErrorHandler(self.ws_manager)
        self.gpio_handler = GPIOHandler(self.error_handler)
        self.state_manager = SystemState(self.gpio_handler.is_mock())
        self.config_manager = ConfigManager(self.state_manager, self.error_handler, self.ws_manager)
        self.queue_manager = QueueManager(self.state_manager)
        self.camera_manager = CameraManager(self.error_handler)
        self.qr_scanner = QRScanner() # Model YOLO được tải bên trong

        # 2. Các biến Runtime & Threading
        self.executor = ThreadPoolExecutor(max_workers=5, thread_name_prefix="Worker")
        self.main_running = threading.Event()
        
        # Biến trạng thái sensor (dùng trong Sensor Monitoring Thread)
        self.last_s_state, self.last_s_trig = [], []
        self.last_entry_trigger_time = 0.0
        self.auto_test_enabled = False
        
        # 3. Cấu hình Flask
        self.app = Flask(__name__)
        self.sock = Sock(self.app)
        
        # Khởi tạo và đăng ký APIRouter
        self.api_router = APIRouter(self.app, self.sock, self)
        self.api_router.setup_routes()

    # --- Các hàm phụ trợ cho Router ---
    def _stream_frames_generator(self):
        """Generator stream video (được gọi từ APIRouter)."""
        while True: 
            if self.error_handler.is_maintenance(): 
                time.sleep(0.5); continue
            
            frame = self.camera_manager.get_frame()
            if frame is None:
                time.sleep(0.1); continue
            
            try:
                is_success, buffer = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, 70])
                if is_success:
                    yield (b'--frame\r\nContent-Type: image/jpeg\r\n\r\n' + buffer.tobytes() + b'\r\n')
            except Exception as encode_err:
                logging.error(f"[CAMERA] Lỗi encode khung hình: {encode_err}")
            time.sleep(1 / 20)  # Stream 20 FPS

    def _run_test_relay_worker(self, lane_index, relay_action):
        """Wrapper gọi worker test relay (dùng cho APIRouter)."""
        self.executor.submit(run_test_relay_worker, self, lane_index, relay_action)

    def _run_test_all_relays_worker(self):
        """Wrapper gọi worker test tuần tự (dùng cho APIRouter)."""
        self.executor.submit(run_test_all_relays_worker, self)

    # --- 2. Khởi động Hệ thống ---
    def start(self):
        logging.info("--- HỆ THỐNG ĐANG KHỞI ĐỘNG (Modular Hybrid) ---")
        self.main_running.set()

        # 1. Tải cấu hình và Setup GPIO
        lanes_cfg, timing_cfg = self.config_manager.load_config()
        self.gpio_handler.setup_pins(lanes_cfg, timing_cfg)
        self._initialize_sensor_states()
        
        # 2. Khởi động các luồng nền
        self.camera_manager.start()
        threading.Thread(target=self.ws_manager.broadcast_state_thread, name="StateBcast", daemon=True, args=(self.state_manager, self.error_handler)).start()
        threading.Thread(target=self.config_manager.periodic_save_thread, name="ConfigSave", daemon=True).start()
        
        # 3. Khởi động luồng Logic
        threading.Thread(target=self._qr_detection_loop, name="QRScannerLogic", daemon=True).start()
        threading.Thread(target=self._sensor_monitoring_thread, name="SensorMon", daemon=True).start()
        
        logging.info("="*55 + "\n HỆ THỐNG PHÂN LOẠI SẴN SÀNG \n" + "="*55)
        
        # 4. Chạy Web Server
        host = '0.0.0.0'; port = 3000
        if WAITRESS_AVAILABLE:
            logging.info(f"✅ SERVER MODE: Waitress (Production). Listening on http://{host}:{port}")
            serve(self.app, host=host, port=port, threads=8, connection_limit=200)
        else:
            logging.warning("⚠️ KHÔNG tìm thấy Waitress. Dùng Flask dev server (TẠM THỜI).")
            self.app.run(host=host, port=port, debug=False)

    def stop(self):
        self.main_running.clear()
        self.camera_manager.stop()
        self.executor.shutdown(wait=False)
        self.gpio_handler.cleanup()

    def _initialize_sensor_states(self):
        """Khởi tạo mảng trạng thái sensor."""
        num_lanes = len(self.state_manager.state['lanes'])
        self.last_s_state = [1] * num_lanes
        self.last_s_trig = [0.0] * num_lanes
        self.last_entry_trigger_time = 0.0

    # =========================================================================
    #             LOGIC HỆ THỐNG (THREADS)
    # =========================================================================

    # --- QR Detection Loop ---
    def _qr_detection_loop(self):
        """Luồng quét QR (Hybrid YOLO + Pyzbar) và thêm vào hàng chờ."""
        while self.main_running.is_set():
            if self.error_handler.is_maintenance() or self.auto_test_enabled:
                time.sleep(0.2); continue
            
            frame = self.camera_manager.get_frame()
            qr_result = self.qr_scanner.scan_frame(frame)
            
            if qr_result:
                key, raw, source, timestamp = qr_result['key'], qr_result['raw'], qr_result['source'], qr_result['timestamp']
                
                # Logic Map: Tra cứu Config Map
                mapped_index = None
                mapped_lane_id = None
                lane_map = {canon_id(lane['id']): i for i, lane in enumerate(self.state_manager.state['lanes'])}
                
                if key in lane_map:
                    mapped_index = lane_map[key]
                    mapped_lane_id = self.state_manager.state['lanes'][mapped_index]['id']

                if mapped_index is not None and mapped_lane_id is not None:
                    # Tạo Object Queue Item
                    queue_item = {
                        "lane_index": mapped_index,
                        "qr_key": key,
                        "lane_id": mapped_lane_id,
                        "timestamp": timestamp,
                        "map_source": source,
                        "data_raw": raw
                    }
                    
                    is_pending_match = self._check_pending_match(mapped_index)
                    
                    if is_pending_match:
                        # TRƯỜNG HỢP 1: Sensor đã kích hoạt TRƯỚC (Sensor-First)
                        self._process_sort_trigger(mapped_index, queue_item, "Khớp pending sensor")
                    else:
                        # TRƯỜNG HỢP 2: QR tới trước (QR-First) -> Thêm vào hàng chờ
                        self.queue_manager.add_qr_item(queue_item)
                        
                        self.state_manager.update_lane_status(mapped_index, {"status": "Đang chờ vật..."})
                        
                        self.ws_manager.broadcast_log({
                            "log_type": "qr", 
                            "data": raw, "data_key": key,
                            "message": f"QR '{raw}' ({source}) -> Thêm vào hàng chờ"
                        })
                        logging.info(f"[QR] '{raw}' (key: '{key}', src: {source}) -> lane {mapped_index} (Thêm vào hàng chờ)")

            time.sleep(0.01)

    # --- Sensor Monitoring Loop (GATED FIFO) ---
    def _sensor_monitoring_thread(self):
        """Luồng giám sát sensor với logic Gated FIFO."""
        while self.main_running.is_set():
            if self.error_handler.is_maintenance() or self.auto_test_enabled:
                time.sleep(0.2); continue

            cfg = self.state_manager.state['timing_config']
            debounce_time = cfg.get('sensor_debounce', 0.1)
            queue_timeout = cfg.get('queue_head_timeout', 15.0)
            pending_timeout = cfg.get('pending_trigger_timeout', 0.5)
            lanes = self.state_manager.state['lanes']
            num_lanes = len(lanes)
            now = time.time()
            
            # 1. LOGIC CHỐNG KẸT HÀNG CHỜ QR
            timeout_item = self.queue_manager.check_qr_timeout(queue_timeout)
            if timeout_item:
                expected_lane_name = lanes[timeout_item['lane_index']]['name']
                self.ws_manager.broadcast_log({
                    "log_type": "warn",
                    "message": f"TIMEOUT! Tự động xóa {expected_lane_name} khỏi hàng chờ (>{queue_timeout}s)."
                })
                self.state_manager.update_lane_status(timeout_item['lane_index'], {"status": "Sẵn sàng"})

            # 2. ĐỌC SENSOR ĐẦU VÀO (PIN_ENTRY)
            try:
                entry_sensor_now = self.gpio_handler.read_sensor(PIN_ENTRY)
                # Phát hiện sườn xuống (1 -> 0)
                if entry_sensor_now == 0 and (now - self.last_entry_trigger_time > debounce_time):
                    self.last_entry_trigger_time = now
                    token_count = self.queue_manager.add_entry_token()
                    self.ws_manager.broadcast_log({"log_type": "info", "message": f"Vật qua cổng (SENSOR_ENTRY, Pin {PIN_ENTRY}). Tokens: {token_count}"})
                    
                self.state_manager.update_lane_status(num_lanes, {"sensor_reading": entry_sensor_now}) # Cập nhật trạng thái sensor cổng
                # Cập nhật số token cho UI (giả định)
                self.state_manager.update_lane_status(num_lanes, {"entry_token_count": self.queue_manager.get_entry_queue_length()})

            except Exception as e:
                self.error_handler.trigger_maintenance(f"Lỗi đọc SENSOR_ENTRY: {e}")
                
            # 3. ĐỌC CÁC SENSOR PHÂN LOẠI (Lanes)
            for i in range(num_lanes):
                lane_cfg = lanes[i]
                sensor_pin, push_pin, lane_name = lane_cfg.get("sensor_pin"), lane_cfg.get("push_pin"), lane_cfg['name']

                if sensor_pin is None: continue
                
                try:
                    sensor_now = self.gpio_handler.read_sensor(sensor_pin)
                except Exception as gpio_e:
                    self.error_handler.trigger_maintenance(f"Lỗi đọc sensor {lane_name}: {gpio_e}")
                    continue

                self.state_manager.update_lane_status(i, {"sensor_reading": sensor_now})

                # Phát hiện sườn xuống (1 -> 0)
                if sensor_now == 0 and self.last_s_state[i] == 1:
                    if (now - self.last_s_trig[i]) > debounce_time:
                        self.last_s_trig[i] = now

                        # --- LOGIC GATED FIFO (2-WAY CHECK) ---
                        item_to_process = self.queue_manager.pop_qr_by_index(i)
                        
                        if item_to_process:
                            # 2. KHỚP (QR-First, hoặc Vượt Hàng) -> KIỂM TRA ENTRY TOKEN
                            if self.queue_manager.consume_entry_token():
                                # CÓ CẢ QR VÀ TOKEN ENTRY -> PROCESS SORT
                                self._process_sort_trigger(i, item_to_process, "Khớp QR + Token Entry")
                            else:
                                # CÓ QR, KHÔNG CÓ TOKEN -> BỎ QUA (False trigger)
                                self.ws_manager.broadcast_log({"log_type": "warn", "message": f"Sensor {lane_name} kích hoạt! QR có, TOKEN Entry KHÔNG. Bỏ qua (False Trigger)."})
                                
                        elif not self.queue_manager.is_entry_queue_empty():
                            # 1. HÀNG CHỜ QR RỖNG, NHƯNG ENTRY CÓ TOKEN
                            if push_pin is None:
                                # Lane đi thẳng (pass-through) -> Chỉ cần TOKEN -> PROCESS SORT
                                self.queue_manager.consume_entry_token() # Dùng Token
                                self._process_sort_trigger(i, None, "Token Entry (Pass-Through)")
                            else:
                                # Lane đẩy, chỉ có Token -> KHÔNG HÀNH ĐỘNG
                                # Lý do: Nếu QR không đến kịp (timeout QR), ta không muốn đẩy một vật không rõ loại.
                                self.ws_manager.broadcast_log({"log_type": "warn", "message": f"Sensor {lane_name} kích hoạt! TOKEN có, QR rỗng. Bỏ qua để đợi QR (Nếu QR timeout, token sẽ bị mất)."})

                        else:
                            # 3. CẢ HAI HÀNG CHỜ ĐỀU RỖNG
                            if push_pin is None:
                                # Lane đi thẳng (Pass-Through) -> KHÔNG CÓ QR, KHÔNG CÓ TOKEN
                                self.ws_manager.broadcast_log({"log_type": "warn", "message": f"Sensor {lane_name} kích hoạt! Không có Token/QR. Bỏ qua."})
                            # Nếu là lane đẩy, không làm gì.

                self.last_s_state[i] = sensor_now
            
            # Cập nhật số token cho UI sau khi quét qua các sensor lane
            self.state_manager.update_lane_status(num_lanes, {"entry_token_count": self.queue_manager.get_entry_queue_length()})
            
            time.sleep(0.01) # Quét nhanh

    def _check_pending_match(self, lane_index):
        """Kiểm tra QR vừa quét có khớp với Token đang chờ không (Sensor-First)."""
        # Trong Logic Gated FIFO, Sensor-First xảy ra khi: 
        # Token Entry đã được tạo VÀ Sensor Lane đã kích hoạt, NHƯNG QR chưa đến.
        # Khi QR đến, ta chỉ cần kiểm tra xem có token nào đang chờ không.
        
        if self.queue_manager.get_entry_queue_length() > 0:
            return True
        return False
        
    def _process_sort_trigger(self, lane_index, qr_item, log_context):
        """Khởi động tiến trình phân loại và cập nhật trạng thái."""
        lane_info = self.state_manager.get_lane_info(lane_index)
        if not lane_info: return

        lane_name = lane_info['name']
        qr_key = qr_item['qr_key'] if qr_item else None
        lane_id = lane_info['id'] # Luôn dùng ID của lane đang được xử lý

        self.ws_manager.broadcast_log({"log_type": "info", "message": f"Kích hoạt Phân loại {lane_name} ({log_context})."})
        self.state_manager.update_lane_status(lane_index, {"status": "Đang chờ đẩy"})
        
        # Gửi đến ThreadPoolExecutor để không block Sensor/QR thread
        self.executor.submit(self._sorting_process_wrapper, lane_index, qr_key, lane_id)

    def _sorting_process_wrapper(self, lane_index, qr_key, lane_id):
        """Luồng trung gian, chờ push_delay rồi mới gọi sorting_process."""
        lane_info = self.state_manager.get_lane_info(lane_index)
        if not lane_info: return
        
        push_delay = self.state_manager.state['timing_config'].get('push_delay', 0.0)
        lane_name_for_log = lane_info['name']

        if push_delay > 0:
            time.sleep(push_delay)

        if not self.main_running.is_set(): return

        # Thực hiện chu trình piston
        self._sorting_process(lane_index, lane_info)


    def _sorting_process(self, lane_index, lane_info):
        """Quy trình đẩy-thu piston."""
        
        push_pin, pull_pin = lane_info.get("push_pin"), lane_info.get("pull_pin")
        lane_name = lane_info['name']
        is_sorting_lane = not (push_pin is None or pull_pin is None)
        operation_successful = False

        try:
            cfg = self.state_manager.state['timing_config']
            delay = cfg['cycle_delay']
            settle_delay = cfg['settle_delay']
            
            self.state_manager.update_lane_status(lane_index, {"status": "Đang phân loại..." if is_sorting_lane else "Đang đi thẳng..."})

            if not is_sorting_lane:
                self.ws_manager.broadcast_log({"log_type": "info", "message": f"Vật phẩm đi thẳng qua {lane_name}"})
            else:
                # 1. Nhả Grab (Pull OFF)
                self.gpio_handler.relay_off(pull_pin)
                self.state_manager.update_lane_status(lane_index, {"relay_grab": 0})
                time.sleep(settle_delay);
                if not self.main_running.is_set(): return

                # 2. Kích hoạt Push (Push ON)
                self.gpio_handler.relay_on(push_pin)
                self.state_manager.update_lane_status(lane_index, {"relay_push": 1})
                time.sleep(delay);
                if not self.main_running.is_set(): return

                # 3. Tắt Push (Push OFF)
                self.gpio_handler.relay_off(push_pin)
                self.state_manager.update_lane_status(lane_index, {"relay_push": 0})
                time.sleep(settle_delay);
                if not self.main_running.is_set(): return

                # 4. Kích hoạt Grab (Pull ON)
                self.gpio_handler.relay_on(pull_pin)
                self.state_manager.update_lane_status(lane_index, {"relay_grab": 1})
            
            operation_successful = True

        except Exception as e:
            logging.error(f"[SORT] Lỗi trong sorting_process (lane {lane_name}): {e}")
            self.error_handler.trigger_maintenance(f"Lỗi sorting_process (Lane {lane_name}): {e}")
        finally:
            if operation_successful:
                current_count = self.state_manager.get_lane_info(lane_index)['count'] + 1
                log_type = "sort" if is_sorting_lane else "pass"
                
                self.state_manager.update_lane_status(lane_index, {
                    "count": current_count,
                    "status": "Sẵn sàng"
                })
                self.ws_manager.broadcast_log({"log_type": log_type, "name": lane_name, "count": current_count})
                
                msg = f"Hoàn tất chu trình cho {lane_name}" if is_sorting_lane else f"Hoàn tất đếm vật phẩm đi thẳng qua {lane_name}"
                self.ws_manager.broadcast_log({"log_type": "info", "message": msg})
            else:
                self.state_manager.update_lane_status(lane_index, {"status": "Lỗi/Sẵn sàng"})
