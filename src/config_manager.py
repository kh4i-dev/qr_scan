# -*- coding: utf-8 -*-
"""Module quản lý việc tải và lưu cấu hình."""
import json
import os
import logging
from threading import Lock, RLock 
from .constants import CONFIG_FILE, SORT_LOG_FILE, DEFAULT_LANES_CFG, DEFAULT_TIMING_CFG
from .system_state import SystemState

class ConfigManager:
    # (SỬA) Thêm main_running_event
    def __init__(self, state_manager, error_handler, ws_manager, main_running_event):
        self.state = state_manager
        self.error_handler = error_handler
        self.ws_manager = ws_manager
        self.main_running = main_running_event # (SỬA) Lưu tín hiệu
        
        # (SỬA) Dùng RLock để tránh Deadlock khi load_config gọi save_config
        self.config_file_lock = RLock() 
        self.sort_log_lock = RLock() 

    def _ensure_lane_ids(self, lanes_list):
        """Đảm bảo mỗi lane có một ID cố định."""
        default_ids = ['A', 'B', 'C', 'D', 'E', 'F', 'G', 'H', 'I', 'J']
        for i, lane in enumerate(lanes_list):
            if 'id' not in lane or not lane['id']:
                if i < len(default_ids): lane['id'] = default_ids[i]
                else: lane['id'] = f"LANE_{i+1}"
                logging.warning(f"[CONFIG] Lane {i+1} thiếu ID. Đã gán ID: {lane['id']}")
        return lanes_list

    def load_config(self):
        """Tải cấu hình timing và lanes từ JSON."""
        loaded_cfg = {
            "timing_config": DEFAULT_TIMING_CFG.copy(),
            "lanes_config": [l.copy() for l in DEFAULT_LANES_CFG],
        }

        with self.config_file_lock: # <--- Bây giờ an toàn
            if os.path.exists(CONFIG_FILE):
                try:
                    with open(CONFIG_FILE, 'r', encoding='utf-8') as f: content = f.read()
                    if content:
                        file_cfg = json.loads(content)
                        # Merge Timing: File ghi đè lên mặc định
                        loaded_cfg["timing_config"].update(file_cfg.get('timing_config', {}))
                        
                        lanes_from_file = file_cfg.get('lanes_config')
                        if isinstance(lanes_from_file, list):
                            loaded_cfg["lanes_config"] = self._ensure_lane_ids(lanes_from_file)
                        else:
                            loaded_cfg["lanes_config"] = self._ensure_lane_ids(loaded_cfg["lanes_config"])

                    else: logging.warning(f"[CONFIG] File {CONFIG_FILE} rỗng, dùng mặc định.")
                except Exception as e:
                    logging.error(f"[CONFIG] Lỗi đọc {CONFIG_FILE}: {e}. Dùng mặc định.", exc_info=True)
                    self.error_handler.trigger_maintenance(f"Lỗi file {CONFIG_FILE}: {e}")
            else:
                logging.warning(f"[CONFIG] Không tìm thấy {CONFIG_FILE}, tạo file mới với cấu hình mặc định.")
                self.save_config(loaded_cfg) # <--- Lệnh gọi này giờ đã an toàn

        # Cập nhật trạng thái trung tâm
        with self.state.state_lock:
            self.state.state['timing_config'] = loaded_cfg['timing_config']
            self.state.state['gpio_mode'] = loaded_cfg['timing_config'].get("gpio_mode", "BCM")
            # Cập nhật lanes config vào state (sẽ giữ lại count nếu có)
            self.state.update_lanes_config(loaded_cfg['lanes_config'])

        logging.info(f"[CONFIG] Đã tải cấu hình cho {len(loaded_cfg['lanes_config'])} lanes.")
        return loaded_cfg['lanes_config'], loaded_cfg['timing_config']

    def atomic_save_json(self, data, filepath, lock):
        """Ghi file JSON một cách an toàn (atomic write)."""
        with lock: # <--- Bây giờ an toàn
            tmp_file = f"{filepath}.tmp"
            try:
                with open(tmp_file, 'w', encoding='utf-8') as f:
                    json.dump(data, f, indent=4, ensure_ascii=False)
                os.replace(tmp_file, filepath)
                return True
            except Exception as e:
                # (SỬA) Thêm exc_info=True để gỡ lỗi quyền truy cập
                logging.error(f"[SAVE] Lỗi atomic save file {filepath}: {e}", exc_info=True)
                if os.path.exists(tmp_file):
                    try: os.remove(tmp_file)
                    except Exception: pass
                return False

    def save_config(self, config_data=None):
        """Lưu cấu hình hiện tại hoặc cấu hình được truyền vào."""
        if config_data is None:
            config_data = self.state.get_config_snapshot()
        
        return self.atomic_save_json(config_data, CONFIG_FILE, self.config_file_lock)

    def periodic_save_thread(self):
        """Tự động lưu config và log đếm định kỳ."""
        from datetime import datetime
        
        # (SỬA) Dùng tín hiệu main_running để dừng luồng
        while self.main_running.is_set(): 
            
            # (SỬA) Dùng wait(60) thay vì sleep(60)
            # Luồng sẽ "ngủ" 60 giây, nhưng sẽ tỉnh dậy ngay nếu main_running.clear() được gọi
            stopped_early = self.main_running.wait(60) 
            if not self.main_running.is_set() or stopped_early:
                break # Thoát luồng nếu hệ thống đang tắt

            if self.error_handler.is_maintenance(): continue
            
            config_snapshot = self.state.get_config_snapshot()
            today = datetime.now().strftime('%Y-%m-%d')
            
            try:
                # 1. Lưu Config (Atomic)
                if self.save_config(config_snapshot):
                    logging.debug("[CONFIG] Đã tự động lưu config.")

                # 2. Lưu Sort Log (Atomic)
                counts_snapshot = {lane['name']: lane['count'] for lane in self.state.state['lanes']}
                
                sort_log_data = {}
                with self.sort_log_lock:
                    if os.path.exists(SORT_LOG_FILE):
                        try:
                            with open(SORT_LOG_FILE, 'r', encoding='utf-8') as f:
                                sort_log_data = json.load(f)
                        except Exception:
                            sort_log_data = {}
                
                sort_log_data[today] = counts_snapshot
                
                if self.atomic_save_json(sort_log_data, SORT_LOG_FILE, self.sort_log_lock):
                    logging.debug(f"[SORT_LOG] Đã tự động lưu số đếm vào {SORT_LOG_FILE}.")

            except Exception as e:
                logging.error(f"[SAVE] Lỗi khi tự động lưu state: {e}")
        
        logging.info("[ConfigSave] Luồng lưu tự động đã dừng.") # Log khi thoát

