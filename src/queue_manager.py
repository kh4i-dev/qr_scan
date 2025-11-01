# -*- coding: utf-8 -*-
"""Module quản lý hàng chờ QR và hàng chờ tín hiệu vào (Gated FIFO)."""
import logging
from threading import Lock
import time
from .system_state import SystemState

class QueueManager:
    """Quản lý hàng chờ QR (Object Queue) và Entry Queue (Token)."""
    def __init__(self, state_manager):
        self.state = state_manager
        self.qr_queue = [] # Hàng chờ QR (từ camera)
        self.entry_queue = [] # (MỚI) Hàng chờ vật lý (từ SENSOR_ENTRY)
        self.qr_queue_lock = Lock()
        self.entry_queue_lock = Lock() # (MỚI) Khóa riêng cho hàng chờ vật lý

    def add_qr_item(self, item):
        """Thêm item QR vào hàng chờ."""
        with self.qr_queue_lock:
            self.qr_queue.append(item)
            self._update_state_indices()
    
    def add_qr_item_at_head(self, item):
        """(MỚI) Thêm item QR trở lại vào ĐẦU hàng chờ (dùng khi false trigger)."""
        with self.qr_queue_lock:
            self.qr_queue.insert(0, item)
            self._update_state_indices()

    def _update_state_indices(self):
        """Cập nhật trạng thái chỉ mục (index) cho UI."""
        with self.state.state_lock:
            self.state.state["queue_indices"] = [item['lane_index'] for item in self.qr_queue]

    # (MỚI) Thêm tín hiệu (token) vật lý vào hàng chờ
    def add_entry_token(self):
        """Thêm tín hiệu vật lý (token) vào hàng chờ."""
        with self.entry_queue_lock:
            self.entry_queue.append(True)
            return len(self.entry_queue)

    def pop_qr_by_index(self, lane_index):
        """Tìm và xóa item QR khớp với index, trả về item đó."""
        with self.qr_queue_lock:
            found_item_index = next((idx for idx, item in enumerate(self.qr_queue) if item['lane_index'] == lane_index), -1)
            if found_item_index != -1:
                item = self.qr_queue.pop(found_item_index)
                self._update_state_indices()
                return item
            return None

    def check_qr_timeout(self, timeout):
        """Kiểm tra và xóa item QR bị timeout ở đầu hàng chờ."""
        now = time.time()
        timeout_occurred = None
        
        with self.qr_queue_lock:
            if self.qr_queue:
                head_item = self.qr_queue[0]
                if (now - head_item["timestamp"]) > timeout:
                    timeout_occurred = self.qr_queue.pop(0)
                    self._update_state_indices()
                    
        return timeout_occurred

    # (MỚI) Tiêu thụ (sử dụng) 1 token vật lý
    def consume_entry_token(self):
        """Lấy và xóa 1 token vật lý, nếu có."""
        with self.entry_queue_lock:
            if self.entry_queue:
                self.entry_queue.pop(0)
                return True # Trả về True nếu tiêu thụ thành công
            return False # Trả về False nếu không còn token

    # (MỚI) Kiểm tra hàng chờ token có rỗng không
    def is_entry_queue_empty(self):
        """Kiểm tra hàng chờ token có rỗng không."""
        with self.entry_queue_lock:
            return not self.entry_queue
            
    # (MỚI) Lấy độ dài hàng chờ token (cho UI)
    def get_entry_queue_length(self):
        """Lấy độ dài hàng chờ token."""
        with self.entry_queue_lock:
            return len(self.entry_queue)
            
    # (SỬA) Xóa sạch cả hai hàng chờ
    def clear_all_queues(self):
        """Xóa sạch cả hai hàng chờ (khi reset bảo trì)."""
        with self.qr_queue_lock:
            self.qr_queue.clear()
            self._update_state_indices()
        with self.entry_queue_lock:
            self.entry_queue.clear() # (MỚI)
            
        logging.info("[QUEUE] Đã xóa sạch hàng chờ QR và Entry Token.")

