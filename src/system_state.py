# -*- coding: utf-8 -*-
"""Quản lý trạng thái trung tâm của hệ thống."""
from threading import Lock
import json
from .constants import DEFAULT_TIMING_CFG, DEFAULT_LANES_CFG, AUTH_ENABLED

class SystemState:
    """Đối tượng quản lý trạng thái chung, bao gồm các locks và dữ liệu runtime."""
    def __init__(self, is_mock):
        self.state_lock = Lock()
        self.state = {
            "lanes": [],            # Cấu hình Lane + Trạng thái runtime
            "timing_config": DEFAULT_TIMING_CFG.copy(),
            "is_mock": is_mock,
            "maintenance_mode": False,
            "gpio_mode": "BCM",
            "last_error": None,
            "queue_indices": []     # Hàng chờ (chỉ lưu index cho UI)
        }
        self.auth_enabled = AUTH_ENABLED
        self._initialize_lanes(DEFAULT_LANES_CFG)

    def _initialize_lanes(self, lanes_cfg):
        """Khởi tạo cấu trúc dữ liệu lane ban đầu."""
        new_lanes_state = []
        for i, cfg in enumerate(lanes_cfg):
            new_lanes_state.append({
                "name": cfg.get("name", f"Lane {i+1}"),
                "id": cfg.get("id", f"ID_{i+1}"),
                "status": "Sẵn sàng", "count": 0,
                "sensor_pin": cfg.get("sensor_pin"), 
                "push_pin": cfg.get("push_pin"), 
                "pull_pin": cfg.get("pull_pin"),
                "sensor_reading": 1, "relay_grab": 0, "relay_push": 0
            })
        self.state['lanes'] = new_lanes_state

    def update_lanes_config(self, lanes_cfg):
        """Cập nhật cấu hình lanes, giữ lại số đếm hiện tại."""
        with self.state_lock:
            current_counts = {lane['name']: lane['count'] for lane in self.state['lanes']}
            new_lanes_state = []
            
            for i, cfg in enumerate(lanes_cfg):
                name = cfg.get("name", f"Lane {i+1}")
                new_lanes_state.append({
                    "name": name,
                    "id": cfg.get("id"),
                    "status": "Sẵn sàng",
                    "count": current_counts.get(name, 0), # Giữ lại count
                    "sensor_pin": cfg.get("sensor_pin"), 
                    "push_pin": cfg.get("push_pin"), 
                    "pull_pin": cfg.get("pull_pin"),
                    "sensor_reading": 1, "relay_grab": 0, "relay_push": 0
                })
            self.state['lanes'] = new_lanes_state
            
    def get_state(self):
        """Lấy bản sao của trạng thái hiện tại (thread-safe)."""
        with self.state_lock:
            # Dùng deep copy an toàn (json.loads(json.dumps))
            state_copy = json.loads(json.dumps(self.state))
            # Cập nhật các trường runtime không cần lock riêng
            state_copy["auth_enabled"] = self.auth_enabled
            return state_copy

    def get_lane_info(self, index):
        """Lấy thông tin chi tiết của một lane (thread-safe)."""
        with self.state_lock:
            if 0 <= index < len(self.state['lanes']):
                return self.state['lanes'][index].copy()
            return None

    def update_lane_status(self, index, updates):
        """Cập nhật trạng thái runtime của một lane (status, count, relay...)."""
        with self.state_lock:
            if 0 <= index < len(self.state['lanes']):
                self.state['lanes'][index].update(updates)
                return True
            return False

    def get_config_snapshot(self):
        """Lấy snapshot của Timing và Lanes config để lưu file."""
        with self.state_lock:
            config_snapshot = {}
            config_snapshot['timing_config'] = self.state['timing_config'].copy()
            config_snapshot['lanes_config'] = [
                {"id": l.get('id'), "name": l.get('name'), 
                 "sensor_pin": l.get('sensor_pin'), "push_pin": l.get('push_pin'), 
                 "pull_pin": l.get('pull_pin')}
                for l in self.state['lanes']
            ]
            return config_snapshot
