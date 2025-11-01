# -*- coding: utf-8 -*-
"""Module định nghĩa các HTTP và WebSocket routes."""
import functools
import json
import logging
from flask import Response, jsonify, request, render_template
from flask_sock import Sock
from .constants import USERNAME, PASSWORD, PIN_ENTRY, SORT_LOG_FILE
import os

class APIRouter:
    def __init__(self, app, sock, system_instance):
        self.app = app
        self.sock = sock
        # Truyền toàn bộ instance của SortingSystem để truy cập các modules
        self.system = system_instance

    # --- Decorators Auth ---
    def check_auth(self, username, password):
        return username == USERNAME and password == PASSWORD
        
    def authenticate(self):
        return Response('Yêu cầu đăng nhập.', 401, {'WWW-Authenticate': 'Basic realm="Login Required"'})
        
    def requires_auth(self, f):
        @functools.wraps(f)
        def decorated(*args, **kwargs):
            if not self.system.state_manager.auth_enabled: return f(*args, **kwargs)
            auth = request.authorization
            if not auth or not self.check_auth(auth.username, auth.password): return self.authenticate()
            return f(*args, **kwargs)
        return decorated

    def setup_routes(self):
        """Đăng ký tất cả các routes Flask và WebSocket."""
        # Flask Routes
        self.app.route('/')(self.requires_auth(self.route_index))
        self.app.route('/video_feed')(self.requires_auth(self.route_video_feed))
        self.app.route('/config', methods=['GET'])(self.requires_auth(self.route_get_config))
        self.app.route('/update_config', methods=['POST'])(self.requires_auth(self.route_update_config))
        self.app.route('/api/sort_log')(self.requires_auth(self.route_api_sort_log))
        self.app.route('/api/reset_maintenance', methods=['POST'])(self.requires_auth(self.route_reset_maintenance))
        self.app.route('/api/queue/reset', methods=['POST'])(self.requires_auth(self.route_api_queue_reset))
        self.app.route('/api/mock_gpio', methods=['POST'])(self.requires_auth(self.route_api_mock_gpio))
        
        # WebSocket Route
        self.sock.route('/ws')(self.requires_auth(self.route_ws))

    # =================================================================
    # --- HTTP Route Handlers ---
    # =================================================================

    def route_index(self):
        return render_template('index.html')

    def route_video_feed(self):
        # Yêu cầu generator stream từ CameraManager
        return Response(self.system._stream_frames_generator(), mimetype='multipart/x-mixed-replace; boundary=frame')
        
    def route_get_config(self):
        return jsonify(self.system.state_manager.get_config_snapshot())

    def route_update_config(self):
        """API để POST config."""
        data = request.json
        if not data: return jsonify({"error": "Thiếu dữ liệu JSON"}), 400

        new_timing_config = data.get('timing_config', {})
        new_lanes_config = data.get('lanes_config')

        config_to_save = {}
        restart_required = False

        with self.system.state_manager.state_lock:
            # 1. Cập nhật Timing
            current_timing = self.system.state_manager.state['timing_config']
            current_gpio_mode = current_timing.get('gpio_mode', 'BCM')
            current_timing.update(new_timing_config)
            new_gpio_mode = current_timing.get('gpio_mode', 'BCM')

            if new_gpio_mode != current_gpio_mode:
                restart_required = True
                self.system.ws_manager.broadcast_log({"log_type": "warn", "message": "Chế độ GPIO thay đổi. Cần khởi động lại!"})
            
            config_to_save['timing_config'] = current_timing.copy()

            # 2. Cập nhật Lanes Config
            if isinstance(new_lanes_config, list):
                 self.system.state_manager.update_lanes_config(new_lanes_config)
                 config_to_save['lanes_config'] = new_lanes_config
                 restart_required = True
                 self.system.ws_manager.broadcast_log({"log_type": "warn", "message": "Cấu hình lanes thay đổi. Cần khởi động lại!"})
            else:
                 config_to_save['lanes_config'] = self.system.state_manager.get_config_snapshot()['lanes_config']

        # 3. Lưu file
        if self.system.config_manager.save_config(config_to_save):
            msg = "Đã lưu cấu hình." + (" Yêu cầu khởi động lại!" if restart_required else "")
            log_type = "warn" if restart_required else "success"
            self.system.ws_manager.broadcast_log({"log_type": log_type, "message": msg})
            return jsonify({"message": msg, "config": config_to_save, "restart_required": restart_required})
        else:
            self.system.ws_manager.broadcast_log({"log_type": "error", "message": "Lỗi lưu config server."})
            return jsonify({"error": "Lỗi lưu config."}), 500

    def route_api_sort_log(self):
        """API lấy log đếm."""
        with self.system.config_manager.sort_log_lock:
            try:
                full_data = {}
                if os.path.exists(SORT_LOG_FILE):
                    with open(SORT_LOG_FILE, 'r', encoding='utf-8') as f:
                        content = f.read()
                        if content: full_data = json.loads(content)
                return jsonify(full_data)
            except Exception as e:
                return jsonify({"error": f"Lỗi xử lý sort log: {e}"}), 500

    def route_reset_maintenance(self):
        """API reset chế độ bảo trì."""
        if self.system.error_handler.is_maintenance():
            self.system.error_handler.reset()
            self.system.queue_manager.clear_all_queues()
            self.system.ws_manager.broadcast_log({"log_type": "success", "message": "Reset bảo trì thành công. Hàng chờ đã xóa."})
            return jsonify({"message": "Đã reset chế độ bảo trì."})
        else:
            return jsonify({"message": "Hệ thống không ở chế độ bảo trì."})

    def route_api_queue_reset(self):
        """API (POST) để xóa hàng chờ QR."""
        if self.system.error_handler.is_maintenance():
            return jsonify({"error": "Hệ thống đang bảo trì."}), 403

        self.system.queue_manager.clear_all_queues()
        
        self.system.ws_manager.broadcast_log({"log_type": "warn", "message": "Hàng chờ QR & Token Entry đã được reset thủ công."})
        return jsonify({"message": "Hàng chờ đã được reset."})

    def route_api_mock_gpio(self):
        """API Mock sensor."""
        if not self.system.gpio_handler.is_mock():
            return jsonify({"error": "Chức năng chỉ khả dụng ở chế độ mô phỏng."}), 400
            
        payload = request.get_json(silent=True) or {}; lane_index = payload.get('lane_index')
        requested_state = payload.get('state')
        
        # Kiểm tra nếu là PIN_ENTRY
        lanes_snapshot = self.system.state_manager.get_config_snapshot()['lanes_config']
        is_entry_pin = lane_index == len(lanes_snapshot)
        
        if is_entry_pin:
             pin = PIN_ENTRY
        else:
            lane_info = self.system.state_manager.get_lane_info(lane_index)
            if not lane_info: return jsonify({"error": "lane_index không hợp lệ."}), 400
            pin = lane_info.get('sensor_pin')
        
        if pin is None:
             return jsonify({"error": "Lane/Pin này không có chân sensor để mô phỏng."}), 400

        try:
            # 0=Active/LOW, 1=Inactive/HIGH
            logical_state = 0 if requested_state is True else 1
            
            # Gửi lệnh mock
            self.system.gpio_handler.mock_set_input(pin, logical_state)
            
            # Cập nhật trạng thái sensor trong state
            self.system.state_manager.update_lane_status(lane_index, {"sensor_reading": logical_state})
            
            state_label = 'ACTIVE (LOW)' if logical_state == 0 else 'INACTIVE (HIGH)'
            name = "Gác Cổng (ENTRY)" if is_entry_pin else self.system.state_manager.get_lane_info(lane_index)['name']
            message = f"[MOCK] Sensor pin {pin} -> {state_label} ({name})"
            self.system.ws_manager.broadcast_log({"log_type": "info", "message": message})
            return jsonify({"pin": pin, "state": logical_state, "lane": name})
        
        except Exception as e:
            return jsonify({"error": str(e)}), 500


    # =================================================================
    # --- WebSocket Route Handler ---
    # =================================================================
    
    def route_ws(self, ws):
        """Xử lý kết nối WebSocket."""
        user = "guest"
        self.system.ws_manager.add_client(ws)
        logging.info(f"[WS] Client '{user}' đã kết nối. Tổng: {len(self.system.ws_manager._list_clients())}")

        try:
            # Gửi state ban đầu
            initial_state_snapshot = self.system.state_manager.get_state()
            initial_state_snapshot["maintenance_mode"] = self.system.error_handler.is_maintenance()
            initial_state_snapshot["last_error"] = self.system.error_handler.last_error
            ws.send(json.dumps({"type": "state_update", "state": initial_state_snapshot}))
            
            while True:
                message = ws.receive()
                if message is None: break

                data = json.loads(message)
                action = data.get('action')
                if self.system.error_handler.is_maintenance() and action not in ["reset_maintenance"]:
                    self.system.ws_manager.broadcast_log({"log_type": "error", "message": "Hành động bị chặn: Hệ thống đang bảo trì."}); continue

                if action == 'reset_count':
                    lane_idx = data.get('lane_index')
                    if lane_idx == 'all':
                        for i in range(len(self.system.state_manager.state['lanes'])):
                            self.system.state_manager.update_lane_status(i, {'count': 0})
                        self.system.ws_manager.broadcast_log({"log_type": "info", "message": "Reset toàn bộ số đếm."})
                    elif isinstance(lane_idx, int) and 0 <= lane_idx < len(self.system.state_manager.state['lanes']):
                        lane_name = self.system.state_manager.state['lanes'][lane_idx]['name']
                        self.system.state_manager.update_lane_status(lane_idx, {'count': 0})
                        self.system.ws_manager.broadcast_log({"log_type": "info", "message": f"Reset đếm '{lane_name}'."})
                
                elif action == "test_relay":
                    idx, act = data.get("lane_index"), data.get("relay_action")
                    if idx is not None and act in ["grab", "push"]:
                        lane_info = self.system.state_manager.get_lane_info(idx)
                        if lane_info and lane_info.get('push_pin') is not None and lane_info.get('pull_pin') is not None:
                             self.system.executor.submit(self.system._run_test_relay_worker, idx, act)
                        else:
                             self.system.ws_manager.broadcast_log({"log_type": "error", "message": f"Lane {idx+1} không có relay để test."})

                elif action == "test_all_relays":
                    self.system.executor.submit(self.system._run_test_all_relays_worker)

                elif action == "toggle_auto_test":
                    self.system.auto_test_enabled = data.get("enabled", False)
                    status_msg = "BẬT" if self.system.auto_test_enabled else "TẮT"
                    self.system.ws_manager.broadcast_log({"log_type": "warn", "message": f"Chế độ Auto-Test đã {status_msg}."})
                    if not self.system.auto_test_enabled: self.system.gpio_handler.reset_all_relays()

                elif action == "reset_maintenance":
                    self.route_reset_maintenance() # Dùng lại logic API
            
        except Exception as conn_err:
            if "close" not in str(conn_err).lower():
                logging.warning(f"[WS] Kết nối WebSocket lỗi/đóng cho '{user}': {conn_err}")
        finally:
            self.system.ws_manager.remove_client(ws)
            logging.info(f"[WS] Client '{user}' đã ngắt kết nối. Tổng: {len(self.system.ws_manager._list_clients())}")
