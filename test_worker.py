# -*- coding: utf-8 -*-
"""Module chứa các worker test được gọi bởi ThreadPoolExecutor."""
import time
import logging

def run_test_relay_worker(system, lane_index, relay_action):
    """Worker test relay (dùng cho APIRouter)."""
    lane_info = system.state_manager.get_lane_info(lane_index)
    if not lane_info: return
    
    push_pin, pull_pin, lane_name = lane_info.get("push_pin"), lane_info.get("pull_pin"), lane_info['name']
    pin, state_key = (pull_pin, "relay_grab") if relay_action == "grab" else (push_pin, "relay_push")

    try:
        system.gpio_handler.relay_on(pin)
        system.state_manager.update_lane_status(lane_index, {state_key: 1})
        time.sleep(0.5)
        if not system.main_running.is_set(): return

        system.gpio_handler.relay_off(pin)
        system.state_manager.update_lane_status(lane_index, {state_key: 0})
        system.ws_manager.broadcast_log({"log_type": "info", "message": f"Test '{relay_action}' trên '{lane_name}' thành công."})
    except Exception as e:
        system.ws_manager.broadcast_log({"log_type": "error", "message": f"Lỗi test '{relay_action}' trên '{lane_name}' (Pin {pin}): {e}"})

def run_test_all_relays_worker(system):
    """Worker test tuần tự (dùng cho APIRouter)."""
    system.ws_manager.broadcast_log({"log_type": "info", "message": "Bắt đầu test tuần tự (Cycle) relay..."})
    lanes = system.state_manager.state['lanes']
    cfg = system.state_manager.state['timing_config']
    cycle_delay = cfg.get('cycle_delay', 0.3)
    settle_delay = cfg.get('settle_delay', 0.2)
    
    try:
        for i, lane_info in enumerate(lanes):
            push_pin, pull_pin = lane_info.get("push_pin"), lane_info.get("pull_pin")
            if push_pin is None or pull_pin is None: continue
            
            # Cycle Logic
            system.ws_manager.broadcast_log({"log_type": "info", "message": f"Testing Cycle cho '{lane_info['name']}'..."})
            system.gpio_handler.relay_off(pull_pin); system.state_manager.update_lane_status(i, {"relay_grab": 0})
            time.sleep(settle_delay)
            if not system.main_running.is_set():
                break
            system.gpio_handler.relay_on(push_pin)
            system.state_manager.update_lane_status(i, {"relay_push": 1})
            time.sleep(cycle_delay)
            if not system.main_running.is_set():
                break
            system.gpio_handler.relay_off(push_pin)
            system.state_manager.update_lane_status(i, {"relay_push": 0})
            time.sleep(settle_delay)
            if not system.main_running.is_set():
                break
            system.gpio_handler.relay_on(pull_pin)
            system.state_manager.update_lane_status(i, {"relay_grab": 1})
            time.sleep(0.5)
            if not system.main_running.is_set():
                break

        system.ws_manager.broadcast({"type": "test_sequence_complete"})
        if system.main_running.is_set():
            system.ws_manager.broadcast_log({"log_type": "info", "message": "Test tuần tự hoàn tất."})
        else:
            system.ws_manager.broadcast_log({"log_type": "warn", "message": "Test tuần tự bị dừng."})
    finally:
        system.gpio_handler.reset_all_relays()
