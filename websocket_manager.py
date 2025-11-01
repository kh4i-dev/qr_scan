# -*- coding: utf-8 -*-
"""Module quản lý kết nối WebSocket và broadcast."""
from threading import Lock
import json
import time
import logging

class WebSocketManager:
    def __init__(self):
        self.clients = set()
        self.clients_lock = Lock()
        self.last_broadcast_state_json = ""

    def add_client(self, ws):
        with self.clients_lock: 
            self.clients.add(ws)

    def remove_client(self, ws):
        with self.clients_lock: 
            self.clients.discard(ws)

    def _list_clients(self):
        with self.clients_lock: 
            return list(self.clients)

    def broadcast(self, data):
        """Gửi dữ liệu JSON tới tất cả client."""
        msg = json.dumps(data)
        disconnected = set()
        for client in self._list_clients():
            try:
                client.send(msg)
            except Exception:
                disconnected.add(client)
        if disconnected:
            with self.clients_lock: 
                self.clients.difference_update(disconnected)

    def broadcast_log(self, log_data):
        """Định dạng và gửi 1 tin nhắn log."""
        log_data['timestamp'] = time.strftime('%H:%M:%S')
        self.broadcast({"type": "log", **log_data})

    def broadcast_state_thread(self, state_manager, error_handler):
        """Luồng gửi state định kỳ cho client, chỉ gửi khi có thay đổi."""
        while True:
            time.sleep(0.5)
            
            current_state_snapshot = state_manager.get_state()
            
            # Cập nhật trạng thái error/maintenance từ ErrorHandler
            current_state_snapshot["maintenance_mode"] = error_handler.is_maintenance()
            current_state_snapshot["last_error"] = error_handler.last_error

            try:
                current_state_json = json.dumps({"type": "state_update", "state": current_state_snapshot})
            except TypeError as e:
                logging.error(f"[WS_BCAST] Lỗi serialize state: {e}"); continue

            if current_state_json != self.last_broadcast_state_json:
                try:
                    self.broadcast(json.loads(current_state_json))
                    self.last_broadcast_state_json = current_state_json
                except json.JSONDecodeError: 
                    pass
