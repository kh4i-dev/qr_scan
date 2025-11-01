# -*- coding: utf-8 -*-
"""Module quét QR với logic Hybrid (Pyzbar + YOLO)."""
import cv2
import logging
import time
import os
import numpy as np
from .constants import YOLO_MODEL_PATH
from .utils import canon_id

# Thử import pyzbar
try:
    import pyzbar.pyzbar as pyzbar
    PYZBAR_AVAILABLE = True
except ImportError:
    PYZBAR_AVAILABLE = False

# Thử import YOLO
try:
    from ultralytics import YOLO
    YOLO_AVAILABLE = True
except ImportError:
    YOLO_AVAILABLE = False
    
# Cấu hình ngưỡng YOLO
YOLO_CONF_THRESHOLD = 0.7

class QRScanner:
    def __init__(self):
        self.yolo_model = self._load_yolo_model()
        self.last_qr_key = ""
        self.last_time = 0.0
        self.qr_detector_cv2 = None
        self._initialize_cv2_detector()

    def _load_yolo_model(self):
        """Tải model YOLO, nếu có."""
        if not YOLO_AVAILABLE:
            logging.warning("[YOLO] Thư viện 'ultralytics' không có. KHÔNG thể dùng YOLO.")
            return None
        if not os.path.exists(YOLO_MODEL_PATH):
            logging.warning(f"[YOLO] Không tìm thấy model tại: {YOLO_MODEL_PATH}. KHÔNG thể dùng YOLO.")
            return None
        
        try:
            model = YOLO(YOLO_MODEL_PATH)
            # Khởi tạo/làm nóng model
            model.predict(np.zeros((640, 480, 3), dtype=np.uint8), verbose=False) 
            logging.info(f"✅ Đã tải và làm nóng model YOLO: {YOLO_MODEL_PATH}")
            logging.info(f"   Các class model đã học: {model.names}")
            return model
        except Exception as e:
            logging.error(f"❌ Lỗi tải model YOLO: {e}", exc_info=True)
            return None

    def _initialize_cv2_detector(self):
        """Khởi tạo detector CV2 (chỉ dùng khi Pyzbar/YOLO không có)."""
        if not PYZBAR_AVAILABLE and self.yolo_model is None:
            self.qr_detector_cv2 = cv2.QRCodeDetector()
            logging.warning("[QR] Đang dùng cv2.QRCodeDetector (chậm hơn).")

    def scan_frame(self, frame):
        """
        Quét QR từ frame với logic Hybrid:
        1. Pyzbar (Nhanh)
        2. YOLO (Mạnh, nếu có model)
        3. CV2 (Fallback)
        """
        if frame is None: return None
        
        gray_frame = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        data_key = None
        data_raw = None
        map_source = None

        # 1. Thử Pyzbar (Nhanh nhất)
        if PYZBAR_AVAILABLE:
            barcodes = pyzbar.decode(gray_frame)
            if barcodes:
                data_raw = barcodes[0].data.decode("utf-8")
                data_key = canon_id(data_raw)
                map_source = "Pyzbar"
                
        # 2. Thử YOLO (Nếu Pyzbar thất bại)
        if data_key is None and self.yolo_model is not None:
            try:
                results = self.yolo_model.predict(frame, verbose=False, conf=YOLO_CONF_THRESHOLD)
                
                if results and len(results[0].boxes) > 0:
                    best_box = results[0].boxes[0]
                    class_id = int(best_box.cls[0])
                    label = self.yolo_model.names[class_id]
                    conf = float(best_box.conf[0])
                    
                    data_key = canon_id(label)
                    data_raw = f"YOLO_Label:{label}"
                    map_source = f"YOLO:{conf:.2f}"
            except Exception as e:
                logging.error(f"[QR] Lỗi dự đoán YOLO: {e}")
                
        # 3. Thử CV2 (Fallback nếu cả 2 trên đều thất bại)
        if data_key is None and self.qr_detector_cv2 is not None:
             data_cv2, _, _ = self.qr_detector_cv2.detectAndDecode(gray_frame)
             if data_cv2:
                data_raw = data_cv2
                data_key = canon_id(data_raw)
                map_source = "CV2"
                
        if data_key:
            now = time.time()
            # Logic chống lặp QR (giống v6)
            if data_key != self.last_qr_key or (now - self.last_time > 3.0):
                self.last_qr_key = data_key
                self.last_time = now
                return {"key": data_key, "raw": data_raw, "source": map_source, "timestamp": now}
                
        return None
