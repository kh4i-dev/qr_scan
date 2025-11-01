# -*- coding: utf-8 -*-
"""
Main Application (Orchestrator) - Logic Hybrid YOLO + Gated FIFO.
(SỬA) Phiên bản này áp dụng logic Gated FIFO (có SENSOR_ENTRY).
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
# (SỬA) Import USERNAME, PASSWORD, AUTH_ENABLED từ constants
from src.constants import USERNAME, PASSWORD, PIN_ENTRY, ACTIVE_LOW, AUTH_ENABLED
from src.error_handler import ErrorHandler
from src.gpio_handler import GPIOHandler, get_gpio_provider
from src.system_state import SystemState
from src.config_manager import ConfigManager
from src.queue_manager import QueueManager
from src.camera_manager import CameraManager
from src.qr_scanner import QRScanner
from src.websocket_manager import WebSocketManager
from src.api_routes import APIRouter
from src.test_workers import run_test_relay_worker, run_test_all_relays_worker 
from src.utils import canon_id 

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
        
        # (SỬA) Khởi tạo self.main_running TRƯỚC khi dùng
        self.main_running = threading.Event()
        
        self.gpio_handler = GPIOHandler(self.error_handler)
        self.state_manager = SystemState(self.gpio_handler.is_mock())
        
        # (SỬA) Truyền main_running vào ConfigManager và CameraManager
        self.config_manager = ConfigManager(self.state_manager, self.error_handler, self.ws_manager, self.main_running)
        self.queue_manager = QueueManager(self.state_manager) 
        self.camera_manager = CameraManager(self.error_handler, self.main_running)
        
        self.qr_scanner = QRScanner() 

        # 2. Các biến Runtime & Threading
        self.executor = ThreadPoolExecutor(max_workers=5, thread_name_prefix="Worker")
        # self.main_running = threading.Event() # (SỬA) Đã chuyển lên trên
        
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
        while self.main_running.is_set(): # (SỬA) Dùng main_running.is_set()
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
        try:
            logging.info("--- HỆ THỐNG ĐANG KHỞI ĐỘNG (Modular Gated FIFO) ---")
            self.main_running.set()

            # 1. Tải cấu hình và Setup GPIO (Giai đoạn dễ bị treo)
            logging.info("[START] Đang tải cấu hình...")
            lanes_cfg, timing_cfg = self.config_manager.load_config()
            
            logging.info("[START] Đang thiết lập chân GPIO...")
            self.gpio_handler.setup_pins(lanes_cfg, timing_cfg)
            self._initialize_sensor_states()
            
            # 2. Khởi động các luồng nền (Camera, WebSocket)
            logging.info("[START] Đang khởi động Camera và WebSocket...")
            self.camera_manager.start()
            threading.Thread(target=self.ws_manager.broadcast_state_thread, name="StateBcast", daemon=True, args=(self.state_manager, self.error_handler)).start()
            
            # 3. Khởi động luồng Logic (QR, Sensor)
            logging.info("[START] Đang khởi động luồng Logic (QR và Sensor)...")
            threading.Thread(target=self._qr_detection_loop, name="QRScannerLogic", daemon=True).start()
            threading.Thread(target=self._sensor_monitoring_thread, name="SensorMon", daemon=True).start()
            
            # In log báo cáo (sau khi GPIO và Config đã OK)
            self._print_startup_log()         
            
            # 4. (SỬA) Khởi động luồng ConfigSave CUỐI CÙNG (Tránh Deadlock)
            logging.info("[START] Đang khởi động luồng lưu tự động...")
            threading.Thread(target=self.config_manager.periodic_save_thread, name="ConfigSave", daemon=True).start()

            # 5. Chạy Web Server (Blocking)
            host = '0.0.0.0'; port = 3000
            if WAITRESS_AVAILABLE:
                logging.info(f"✅ SERVER MODE: Waitress (Production). Listening on http://{host}:{port}")
                serve(self.app, host=host, port=port, threads=8, connection_limit=200)
            else:
                logging.warning("⚠️ KHÔNG tìm thấy Waitress. Dùng Flask dev server (TẠM THỜI).")
                self.app.run(host=host, port=port, debug=False)
                
        except Exception as e:
            logging.critical(f"Lỗi khởi động hệ thống: {e}", exc_info=True)
            self.stop()
            # (SỬA) Ném lỗi ra ngoài để khối __main__ bắt được
            raise 

    def stop(self):
        # 1. Phát tín hiệu dừng
        self.main_running.clear()
        
        # (SỬA) Thêm độ trễ ngắn để các luồng (daemon) kịp thoát
        import time
        time.sleep(0.5) 

        # 2. Dừng các tài nguyên
        self.camera_manager.stop()
        self.executor.shutdown(wait=False, cancel_futures=True) # (SỬA) Thêm cancel_futures
        self.gpio_handler.cleanup()
        logging.info("Đã gọi cleanup cho các module.")


    def _initialize_sensor_states(self):
        """Khởi tạo mảng trạng thái sensor."""
        # (SỬA) Số lượng lanes bao gồm cả lane Gác Cổng (dummy lane)
        # (SỬA) Logic Gated FIFO không cần dummy lane trong state, chỉ cần num_lanes
        num_lanes = len(self.state_manager.state['lanes'])
        self.last_s_state = [1] * num_lanes
        self.last_s_trig = [0.0] * num_lanes
        self.last_entry_trigger_time = 0.0

    def _print_startup_log(self):
        """In log trạng thái chi tiết khi khởi động thành công."""
        # (SỬA) Import hằng số từ scope ngoài
        global WAITRESS_AVAILABLE
        
        is_real_gpio = not self.gpio_handler.is_mock()
        gpio_mode = self.state_manager.state['timing_config'].get("gpio_mode", "BCM")
        WAITRESS_STATUS = "Waitress (Production)" if WAITRESS_AVAILABLE else "Flask Dev (TẠM THỜI)"

        logging.info("="*55)
        logging.info("  HỆ THỐNG PHÂN LOẠI SẴN SÀNG (Modular Hybrid / Gated FIFO)")
        logging.info(f"  Logic: Gated FIFO (SENSOR_ENTRY & QR Match)") 
        logging.info(f"  GPIO Mode: {'REAL' if is_real_gpio else 'MOCK'} (Config: {gpio_mode})")
        logging.info(f"  Web Server: {WAITRESS_STATUS}")
        logging.info(f"  API State: http://<IP_CUA_PI>:3000")
        
        if AUTH_ENABLED:
            logging.info(f"  Truy cập: http://<IP_CUA_PI>:3000 (User: {USERNAME} / Pass: {PASSWORD})")
        else:
            logging.info("  Truy cập: http://<IP_CUA_PI>:3000 (KHÔNG yêu cầu đăng nhập)")
        logging.info("="*55)    

    # =========================================================================
    #             LOGIC HỆ THỐNG (THREADS)
    # =========================================================================

    # --- (SỬA) QR Detection Loop (Logic Gated FIFO) ---
    def _qr_detection_loop(self):
        """Luồng quét QR (Hybrid YOLO + Pyzbar) và chỉ thêm vào hàng chờ."""
        while self.main_running.is_set():
            if self.error_handler.is_maintenance() or self.auto_test_enabled:
                time.sleep(0.2); continue
            
            frame = self.camera_manager.get_frame()
            qr_result = self.qr_scanner.scan_frame(frame)
            
            if qr_result:
                key, raw, source, timestamp = qr_result['key'], qr_result['raw'], qr_result['source'], qr_result['timestamp']
                
                # Logic Map: Tra cứu Config Map
                lanes_config = self.state_manager.state['lanes'] # Lấy config lanes hiện tại
                mapped_index = None
                mapped_lane_id = None
                # (SỬA) Đảm bảo lane_map dùng index 'i' chính xác
                lane_map = {canon_id(lane['id']): i for i, lane in enumerate(lanes_config)}
                
                if key in lane_map:
                    mapped_index = lane_map[key]
                    mapped_lane_id = lanes_config[mapped_index]['id']

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
                    
                    # (SỬA) Logic Gated FIFO: Chỉ thêm vào hàng chờ.
                    # Luồng sensor sẽ xử lý việc khớp với tín hiệu gác cổng.
                    self.queue_manager.add_qr_item(queue_item)
                    self.state_manager.update_lane_status(mapped_index, {"status": "Đang chờ vật..."})
                    
                    self.ws_manager.broadcast_log({
                        "log_type": "qr", 
                        "data": raw, "data_key": key,
                        "message": f"QR '{raw}' ({source}) -> Thêm vào hàng chờ"
                    })
                    logging.info(f"[QR] '{raw}' (key: '{key}', src: {source}) -> lane {mapped_index} (Thêm vào hàng chờ)")

            time.sleep(0.01) # Quét nhanh

    # --- (SỬA) Sensor Monitoring Loop (Logic Gated FIFO MỚI) ---
    def _sensor_monitoring_thread(self):
        """Luồng giám sát sensor với logic Gated FIFO (Logic 2 tín hiệu)."""
        while self.main_running.is_set():
            if self.error_handler.is_maintenance() or self.auto_test_enabled:
                time.sleep(0.2); continue
            
            try:
                cfg = self.state_manager.state['timing_config']
                debounce_time = cfg.get('sensor_debounce', 0.1)
                queue_timeout = cfg.get('queue_head_timeout', 15.0)
                lanes = self.state_manager.state['lanes']
                num_lanes = len(lanes) # Chỉ các lane phân loại
                
                now = time.time()
                
                # 1. LOGIC CHỐNG KẸT HÀNG CHỜ QR (Giữ nguyên)
                timeout_item = self.queue_manager.check_qr_timeout(queue_timeout)
                if timeout_item:
                    expected_lane_name = lanes[timeout_item['lane_index']]['name']
                    self.ws_manager.broadcast_log({
                        "log_type": "warn",
                        "message": f"TIMEOUT! Tự động xóa {expected_lane_name} khỏi hàng chờ (>{queue_timeout}s)."
                    })
                    self.state_manager.update_lane_status(timeout_item['lane_index'], {"status": "Sẵn sàng"})

                # 2. (MỚI) ĐỌC SENSOR ĐẦU VÀO (PIN_ENTRY)
                try:
                    entry_sensor_now = self.gpio_handler.read_sensor(PIN_ENTRY)
                    # Phát hiện sườn xuống (1 -> 0)
                    if entry_sensor_now == 0 and (now - self.last_entry_trigger_time > debounce_time):
                        self.last_entry_trigger_time = now
                        token_count = self.queue_manager.add_entry_token()
                        
                        msg = f"Vật qua cổng (SENSOR_ENTRY, Pin {PIN_ENTRY}). Tokens: {token_count}"
                        self.ws_manager.broadcast_log({"log_type": "info", "message": msg})
                        logging.info(f"[SENSOR] {msg}")
                        
                    # (SỬA) Cập nhật trạng thái sensor cổng cho UI (dùng index = num_lanes)
                    # Giả định UI sẽ render thêm 1 lane cho Gác Cổng
                    self.state_manager.update_lane_status(num_lanes, {"sensor_reading": entry_sensor_now})

                except Exception as e:
                    # Nếu SENSOR_ENTRY lỗi, dừng hệ thống
                    self.error_handler.trigger_maintenance(f"Lỗi đọc SENSOR_ENTRY (Pin {PIN_ENTRY}): {e}")
                    time.sleep(1); continue
                    
                # 3. ĐỌC CÁC SENSOR PHÂN LOẠI (Lanes)
                for i in range(num_lanes): # Chỉ lặp qua các lane thật
                    lane_cfg = lanes[i]
                    sensor_pin, push_pin, lane_name = lane_cfg.get("sensor_pin"), lane_cfg.get("push_pin"), lane_cfg['name']

                    if sensor_pin is None: continue # Bỏ qua lane không có sensor
                    
                    try:
                        sensor_now = self.gpio_handler.read_sensor(sensor_pin)
                    except Exception as gpio_e:
                        self.error_handler.trigger_maintenance(f"Lỗi đọc sensor {lane_name}: {gpio_e}")
                        continue # Bỏ qua lane này

                    self.state_manager.update_lane_status(i, {"sensor_reading": sensor_now})

                    # Phát hiện sườn xuống (1 -> 0)
                    if sensor_now == 0 and self.last_s_state[i] == 1:
                        if (now - self.last_s_trig[i]) > debounce_time:
                            self.last_s_trig[i] = now

                            # --- LOGIC GATED FIFO (2-WAY CHECK) ---
                            # Kiểm tra xem có QR khớp cho lane này không
                            item_to_process = self.queue_manager.pop_qr_by_index(i)
                            
                            if item_to_process:
                                # TRƯỜNG HỢP 1: CÓ QR KHỚP
                                # Kiểm tra xem có tín hiệu gác cổng (token) không
                                if self.queue_manager.consume_entry_token():
                                    # CÓ CẢ QR VÀ TOKEN ENTRY -> PROCESS SORT
                                    self._process_sort_trigger(i, item_to_process, "Khớp QR + Token Entry")
                                else:
                                    # CÓ QR, KHÔNG CÓ TOKEN -> BỎ QUA (False trigger)
                                    msg = f"Sensor {lane_name} kích hoạt! QR có, TOKEN Entry KHÔNG. Bỏ qua (False Trigger)."
                                    self.ws_manager.broadcast_log({"log_type": "warn", "message": msg})
                                    logging.warning(f"[LOGIC] {msg}")
                                    # (SỬA) Trả lại item vào đầu hàng chờ vì nó chưa được xử lý
                                    self.queue_manager.add_qr_item_at_head(item_to_process)
                                    
                            elif not self.queue_manager.is_entry_queue_empty():
                                # TRƯỜN HỢP 2: KHÔNG CÓ QR, NHƯNG CÓ TOKEN (Vật lạ)
                                if push_pin is None:
                                    # Lane đi thẳng (pass-through) -> Chỉ cần TOKEN -> PROCESS SORT
                                    self.queue_manager.consume_entry_token() # Dùng Token
                                    self._process_sort_trigger(i, None, "Token Entry (Pass-Through)")
                                else:
                                    # Lane đẩy (Sorting Lane), chỉ có Token (Vật lạ) -> KHÔNG HÀNH ĐỘNG
                                    # Không dùng token, chờ QR (nếu QR đến trễ hoặc timeout)
                                    msg = f"Sensor {lane_name} kích hoạt! TOKEN có, QR rỗng. Bỏ qua (Chờ QR)."
                                    self.ws_manager.broadcast_log({"log_type": "warn", "message": msg})
                                    logging.warning(f"[LOGIC] {msg}")

                            else:
                                # TRƯỜNG HỢP 3: CẢ HAI HÀNG CHỜ ĐỀU RỖNG (KÍCH HOẠT NHẦM)
                                msg = f"Sensor {lane_name} kích hoạt! Không có Token/QR. Bỏ qua (Kích hoạt nhầm)."
                                self.ws_manager.broadcast_log({"log_type": "warn", "message": msg})
                                logging.warning(f"[LOGIC] {msg}")

                    self.last_s_state[i] = sensor_now
                
                # 4. Cập nhật số token cho UI sau khi quét qua các sensor lane
                # (SỬA) Đổi tên 'count' thành 'entry_token_count' cho rõ ràng
                self.state_manager.update_lane_status(num_lanes, {"entry_token_count": self.queue_manager.get_entry_queue_length()})
            
            except Exception as loop_e:
                logging.error(f"[SensorMon] Lỗi không mong muốn trong vòng lặp: {loop_e}", exc_info=True)
                
            time.sleep(0.005) # Quét nhanh

    # (SỬA) Xóa bỏ _check_pending_match

    def _process_sort_trigger(self, lane_index, qr_item, log_context):
        """Khởi động tiến trình phân loại và cập nhật trạng thái."""
        lane_info = self.state_manager.get_lane_info(lane_index)
        if not lane_info: return

        lane_name = lane_info['name']
        # (SỬA) Xử lý trường hợp đi thẳng (qr_item là None)
        qr_key = qr_item['qr_key'] if qr_item else "N/A"
        lane_id = lane_info['id'] 

        logging.info(f"[LOGIC] Kích hoạt Phân loại {lane_name} (Context: {log_context}, QR: {qr_key}).")
        
        # (SỬA) Lane đi thẳng (pass-through) không cần chờ đẩy
        is_pass_through = lane_info.get("push_pin") is None
        if is_pass_through:
            self.state_manager.update_lane_status(lane_index, {"status": "Đang đi thẳng..."})
        else:
            self.state_manager.update_lane_status(lane_index, {"status": "Đang chờ đẩy"})
        
        # Gửi đến ThreadPoolExecutor để không block Sensor/QR thread
        self.executor.submit(self._sorting_process_wrapper, lane_index, qr_key, lane_id)

    def _sorting_process_wrapper(self, lane_index, qr_key, lane_id):
        """Luồng trung gian, chờ push_delay rồi mới gọi sorting_process."""
        lane_info = self.state_manager.get_lane_info(lane_index)
        if not lane_info: return
        
        # (SỬA) Lane đi thẳng không cần push_delay
        is_pass_through = lane_info.get("push_pin") is None
        if not is_pass_through:
            push_delay = self.state_manager.state['timing_config'].get('push_delay', 0.0)
            if push_delay > 0:
                time.sleep(push_delay)

        if not self.main_running.is_set(): return

        # Thực hiện chu trình piston (hoặc chỉ đếm nếu là pass-through)
        self._sorting_process(lane_index, lane_info)


    def _sorting_process(self, lane_index, lane_info):
        """Quy trình đẩy-thu piston (hoặc chỉ đếm)."""
        
        push_pin, pull_pin = lane_info.get("push_pin"), lane_info.get("pull_pin")
        lane_name = lane_info['name']
        is_sorting_lane = not (push_pin is None or pull_pin is None)
        operation_successful = False

        try:
            cfg = self.state_manager.state['timing_config']
            delay = cfg['cycle_delay']
            settle_delay = cfg['settle_delay']
            
            if not is_sorting_lane:
                # (SỬA) Lane đi thẳng (Pass-Through)
                self.state_manager.update_lane_status(lane_index, {"status": "Đang đi thẳng..."})
                self.ws_manager.broadcast_log({"log_type": "info", "message": f"Vật phẩm đi thẳng qua {lane_name}"})
                logging.info(f"[SORT] Vật phẩm đi thẳng qua {lane_name}") # Thêm log server
                # Giả lập thời gian vật đi qua (hoặc sleep 0.1)
                time.sleep(0.1) 
            else:
                # (LOGIC CŨ) Lane Phân loại (Sorting Lane)
                logging.info(f"[SORT] Bắt đầu chu trình Piston cho {lane_name}...") # Thêm log
                self.state_manager.update_lane_status(lane_index, {"status": "Đang phân loại..."})
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
            logging.error(f"[SORT] Lỗi trong sorting_process (lane {lane_name}): {e}", exc_info=True) # (SỬA) Thêm exc_info
            self.error_handler.trigger_maintenance(f"Lỗi sorting_process (Lane {lane_name}): {e}")
        finally:
            if operation_successful:
                # (SỬA) Cập nhật số đếm và log (dùng state_lock để đảm bảo)
                with self.state_manager.state_lock:
                    current_count = self.state_manager.state['lanes'][lane_index]['count'] + 1
                    self.state_manager.state['lanes'][lane_index]['count'] = current_count
                    self.state_manager.state['lanes'][lane_index]['status'] = "Sẵn sàng"
                
                log_type = "sort" if is_sorting_lane else "pass"
                # (SỬA) Gửi data trong broadcast_log (ĐÃ SỬA: data.name -> name)
                self.ws_manager.broadcast_log({"log_type": log_type, "name": lane_name, "count": current_count})
                
                msg = f"Hoàn tất chu trình cho {lane_name}" if is_sorting_lane else f"Hoàn tất đếm vật phẩm đi thẳng qua {lane_name}"
                logging.info(f"[SORT] {msg} (Tổng: {current_count})") # Thêm log server
                self.ws_manager.broadcast_log({"log_type": "info", "message": f"{msg} (Tổng: {current_count})"})
            else:
                # Nếu lỗi, reset về Sẵn sàng
                self.state_manager.update_lane_status(lane_index, {"status": "Sẵn sàng"})

# (SỬA) Khối thực thi chính (Main execution block)
if __name__ == "__main__":
    app_system = None 
    try:
        # 1. Khởi tạo đối tượng (chạy __init__)
        app_system = SortingSystem()
        
        # 2. Khởi động toàn bộ logic (chạy start())
        app_system.start() 

    except KeyboardInterrupt:
        logging.info("\n🛑 Dừng hệ thống (Ctrl+C)...")
        
    except Exception as main_e:
        # Lỗi này đã được ghi log bên trong start() hoặc gpio_handler
        logging.critical(f"[CRITICAL] Không thể khởi động hệ thống. Đang thoát.")

    finally:
        # Khối dọn dẹp
        if app_system is not None:
            logging.info("Đang thực hiện dọn dẹp và tắt hệ thống...")
            app_system.stop()
            logging.info("✅ Cleanup hoàn tất. Tạm biệt!")
        else:
            logging.info("👋 Tạm biệt! (Hệ thống chưa kịp khởi tạo hoàn chỉnh)")

