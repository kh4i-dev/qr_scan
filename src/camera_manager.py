# -*- coding: utf-8 -*-
"""Module quản lý luồng video và frames."""
import cv2
import threading
import logging
import time
import os
import numpy as np
from .constants import CAMERA_INDEX

class CameraManager:
    # (SỬA) Thêm main_running_event
    def __init__(self, error_handler, main_running_event):
        self.camera = None
        self.frame_lock = threading.Lock()
        self.latest_frame = None
        self.error_handler = error_handler
        self.camera_thread = None
        self.main_running = main_running_event # (SỬA) Dùng tín hiệu chung
        self.is_ready = threading.Event() # (MỚI) Tín hiệu báo camera sẵn sàng

    def start(self):
        """Khởi tạo và chạy luồng camera."""
        if self.camera_thread and self.camera_thread.is_alive():
            logging.info("[CAMERA] Camera đã chạy.")
            return

        # self.running.set() # (SỬA) Xóa (đã được set ở main.py)
        self.camera_thread = threading.Thread(target=self._run_camera_loop, name="CameraThread", daemon=True)
        self.camera_thread.start()
        logging.info("[CAMERA] Đã khởi động luồng camera.")
        
        # (MỚI) Chờ camera sẵn sàng hoặc timeout 5 giây
        logging.info("[CAMERA] Đang chờ tín hiệu sẵn sàng từ camera...")
        if not self.is_ready.wait(timeout=5.0):
            # (SỬA) Không clear main_running ở đây, để main.py xử lý
            raise RuntimeError("Khởi động Camera Timeout (Chưa nhận được tín hiệu sẵn sàng sau 5s).")
        logging.info("[CAMERA] Tín hiệu Camera Sẵn sàng đã được nhận.")


    def stop(self):
        """Dừng luồng camera an toàn."""
        # self.running.clear() # (SỬA) Xóa (đã được clear ở main.py)
        if self.camera_thread and self.camera_thread.is_alive():
            self.camera_thread.join(timeout=1)
        if self.camera:
            self.camera.release()
            
    def _run_camera_loop(self):
        """Luồng chính chạy camera, xử lý lỗi và kết nối lại."""
        retries, max_retries = 0, 5
        
        # (SỬA) Dùng self.main_running
        while self.main_running.is_set(): 
            if self.error_handler.is_maintenance(): 
                time.sleep(0.5); continue
                
            if self.camera is None or not self.camera.isOpened():
                if retries >= max_retries:
                    self.error_handler.trigger_maintenance("Camera lỗi vĩnh viễn (mất kết nối).")
                    self.is_ready.clear() # (MỚI) Đánh dấu không sẵn sàng
                    break
                    
                logging.warning(f"[CAMERA] Đang thử kết nối lại camera (Lần {retries + 1})...")
                self._initialize_camera() # Thử khởi tạo lại
                if self.camera is None or not self.camera.isOpened():
                    # Nếu khởi tạo thất bại, đợi 1s
                    retries += 1
                    time.sleep(1)
                continue

            ret, frame = self.camera.read()
            if not ret:
                logging.warning(f"[CAMERA] Mất khung hình. Đang thử mở lại...")
                self._release_camera()
                self.is_ready.clear() # (MỚI) Mất kết nối, không sẵn sàng
                time.sleep(0.5)
                continue

            retries = 0 # Reset khi thành công
            
            # (MỚI) Đặt tín hiệu sẵn sàng sau khi có frame đầu tiên
            if not self.is_ready.is_set():
                self.is_ready.set()
                logging.info("[CAMERA] Đã nhận frame đầu tiên. Camera Sẵn sàng.")
                
            with self.frame_lock:
                self.latest_frame = frame.copy()
            time.sleep(1 / 60) # Tăng tốc độ chụp (60 FPS)

        self._release_camera()
        self.is_ready.clear() # Đảm bảo clear khi luồng dừng
        logging.info("[CAMERA] Luồng camera đã dừng.")


    def _initialize_camera(self):
        """Khởi tạo camera với các cài đặt tối ưu."""
        try:
            if self.camera: self.camera.release()
            self.camera = cv2.VideoCapture(CAMERA_INDEX)
            
            if not self.camera.isOpened():
                logging.error("[CAMERA] Không thể mở camera.")
                self.is_ready.clear() # (MỚI)
                return

            # Cài đặt tối ưu (từ v5.4)
            self.camera.set(cv2.CAP_PROP_FPS, 30)
            self.camera.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
            self.camera.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
            self.camera.set(cv2.CAP_PROP_BUFFERSIZE, 1)
            # Auto-Exposure Settings (Cải thiện độ sáng)
            self.camera.set(cv2.CAP_PROP_AUTO_EXPOSURE, 0.75)  
            self.camera.set(cv2.CAP_PROP_EXPOSURE, -4)         
            self.camera.set(cv2.CAP_PROP_GAIN, 8)              
            
            logging.info("[CAMERA] Đã cấu hình camera.")
            # (SỬA) Không set ready ở đây, chờ frame đầu tiên thành công trong loop

        except Exception as e:
            logging.error(f"[CAMERA] Lỗi cấu hình camera: {e}")
            self._release_camera()
            self.is_ready.clear() # (MỚI)


    def _release_camera(self):
        if self.camera:
            self.camera.release()
            self.camera = None

    def get_frame(self):
        """Lấy frame mới nhất (thread-safe)."""
        with self.frame_lock:
            return self.latest_frame.copy() if self.latest_frame is not None else None

