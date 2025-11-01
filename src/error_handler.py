# -*- coding: utf-8 -*-
"""Module xử lý lỗi và chế độ bảo trì."""
from threading import Lock
import logging
from .constants import DEFAULT_TIMING_CFG

class ErrorHandler:
    """Quản lý trạng thái lỗi/bảo trì của hệ thống."""
    
    def __init__(self, ws_manager):
        self.lock = Lock()
        self.maintenance_mode = False
        self.last_error = None
        self.ws_manager = ws_manager
        
    def _broadcast_maintenance_status(self):
        """Gửi trạng thái bảo trì qua WebSocket."""
        status_data = {
            "type": "maintenance_update", 
            "enabled": self.maintenance_mode, 
            "reason": self.last_error
        }
        # Tạm thời, dùng broadcast_log để tránh dependency vòng
        self.ws_manager.broadcast_log({
            "log_type": "error" if self.maintenance_mode else "info",
            "message": f"Hệ thống đã {'BẬT' if self.maintenance_mode else 'TẮT'} chế độ bảo trì: {self.last_error or 'Reset thành công.'}"
        })
        
    def trigger_maintenance(self, message):
        """Kích hoạt chế độ bảo trì do lỗi nghiêm trọng."""
        with self.lock:
            if self.maintenance_mode: return
            self.maintenance_mode = True
            self.last_error = message
            logging.critical("="*50 + f"\n[CHẾ ĐỘ BẢO TRÌ] Lý do: {message}\n" +
                             "Hệ thống đã dừng. Cần can thiệp thủ công.\n" + "="*50)
            self._broadcast_maintenance_status()

    def reset(self):
        """Reset lại chế độ bảo trì (thường do người dùng kích hoạt)."""
        with self.lock:
            if not self.maintenance_mode: return
            self.maintenance_mode = False
            self.last_error = None
            logging.info("[RESET BẢO TRÌ]")
            self._broadcast_maintenance_status()

    def is_maintenance(self):
        """Kiểm tra hệ thống có đang bảo trì không."""
        with self.lock:
            return self.maintenance_mode
