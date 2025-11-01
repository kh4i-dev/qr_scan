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
    def __init__(self, error_handler):
        self.camera = None
        self.frame_lock = threading.Lock()
        self.latest_frame = None
        self.error_handler = error_handler
        self.camera_thread = None
        self.running = threading.Event()

    def start(self):
        """Khởi tạo và chạy luồng camera."""
        if self.camera_thread and self.camera_thread.is_alive():
            logging.info("[CAMERA] Camera đã chạy.")
            return

        self.running.set()
        self.camera_thread = threading.Thread(target=self._run_camera_loop, name="CameraThread", daemon=True)
        self.camera_thread.start()
        logging.info("[CAMERA] Đã khởi động luồng camera.")

    def stop(self):
        """Dừng luồng camera an toàn."""
        self.running.clear()
        if self.camera_thread and self.camera_thread.is_alive():
            self.camera_thread.join(timeout=1)
        if self.camera:
            self.camera.release()
            
    def _run_camera_loop(self):
        """Luồng chính chạy camera, xử lý lỗi và kết nối lại."""
        retries, max_retries = 0, 5
        
        while self.running.is_set():
            if self.error_handler.is_maintenance(): 
                time.sleep(0.5); continue
                
            if self.camera is None or not self.camera.isOpened():
                if retries >= max_retries:
                    self.error_handler.trigger_maintenance("Camera lỗi vĩnh viễn (mất kết nối).")
                    break
                    
                logging.warning(f"[CAMERA] Đang thử kết nối lại camera (Lần {retries + 1})...")
                self._initialize_camera()
                retries += 1
                time.sleep(1)
                continue

            ret, frame = self.camera.read()
            if not ret:
                logging.warning(f"[CAMERA] Mất khung hình. Đang thử mở lại...")
                self._release_camera()
                time.sleep(0.5)
                continue

            retries = 0 # Reset khi thành công
            with self.frame_lock:
                self.latest_frame = frame.copy()
            time.sleep(1 / 60) # Tăng tốc độ chụp (60 FPS)

        self._release_camera()
        logging.info("[CAMERA] Luồng camera đã dừng.")


    def _initialize_camera(self):
        """Khởi tạo camera với các cài đặt tối ưu."""
        try:
            if self.camera: self.camera.release()
            self.camera = cv2.VideoCapture(CAMERA_INDEX)
            
            if not self.camera.isOpened():
                logging.error("[CAMERA] Không thể mở camera.")
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
            
            logging.info("[CAMERA] Camera sẵn sàng với cấu hình tối ưu.")
        except Exception as e:
            logging.error(f"[CAMERA] Lỗi cấu hình camera: {e}")
            self._release_camera()


    def _release_camera(self):
        if self.camera:
            self.camera.release()
            self.camera = None

    def get_frame(self):
        """Lấy frame mới nhất (thread-safe)."""
        with self.frame_lock:
            return self.latest_frame.copy() if self.latest_frame is not None else None
