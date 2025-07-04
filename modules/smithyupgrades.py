# modules/smithyupgrades.py
import time
import re
import threading
from bs4 import BeautifulSoup
from config import log, BOT_STATE, state_lock, save_config

class Module(threading.Thread):
    def __init__(self, account_info, client_class):
        super().__init__()
        self.stop_event = threading.Event()
        self.daemon = True
        self.account_info = account_info
        self.client_class = client_class
        self.next_check_times = {}

    def stop(self):
        self.stop_event.set()

    def run(self):
        username = self.account_info['username']
        log.info(f"[SmithyAgent][{username}] Thread started.")
        self.stop_event.wait(30)

        client = self.client_class(
            username,
            self.account_info['password'],
            self.account_info['server_url'],
            self.account_info.get('proxy')
        )
        if not client.login():
            log.error(f"[SmithyAgent][{username}] Initial login failed. Agent will stop.")
            return

        while not self.stop_event.is_set():
            try:
                with state_lock:
                    all_village_data = BOT_STATE.get("village_data", {})
                    upgrade_queues = BOT_STATE.get("smithy_upgrades", {})

                villages_for_account = all_village_data.get(username, [])

                for village in villages_for_account:
                    if self.stop_event.is_set(): break

                    village_id_str = str(village['id'])
                    
                    if time.time() < self.next_check_times.get(village_id_str, 0):
                        continue

                    config = upgrade_queues.get(village_id_str)
                    if not config or not config.get("enabled"):
                        self.next_check_times[village_id_str] = time.time() + 300
                        continue

                    priority_list = config.get("priority", [])
                    if not priority_list:
                        self.next_check_times[village_id_str] = time.time() + 300
                        continue

                    log.info(f"[SmithyAgent][{username}] Checking smithy for {village['name']}...")
                    
                    with state_lock:
                        village_full_data = BOT_STATE.get("village_data", {}).get(village_id_str, {})
                    
                    smithy_building = next((b for b in village_full_data.get('buildings', []) if b.get('gid') == 13), None)

                    if not smithy_building:
                        log.debug(f"[SmithyAgent][{username}] No smithy found in {village['name']}. Skipping.")
                        self.next_check_times[village_id_str] = time.time() + 900
                        continue

                    smithy_info = client.get_smithy_page(village['id'], 13)

                    if not smithy_info:
                        log.warning(f"[SmithyAgent][{username}] Could not retrieve smithy info for {village['name']}. Retrying in 5 mins.")
                        self.next_check_times[village_id_str] = time.time() + 300
                        continue
                    
                    with state_lock:
                        BOT_STATE['smithy_data'][village_id_str] = smithy_info

                    current_queue = smithy_info.get("research_queue", [])
                    max_queue = 2 if smithy_info.get("plus_account", False) else 1

                    if len(current_queue) >= max_queue:
                        earliest_eta = min(item['eta'] for item in current_queue) if current_queue else 300
                        wait_time = max(2, earliest_eta + 1)
                        log.info(f"[SmithyAgent][{username}] Smithy queue is full for {village['name']}. Next check in {wait_time:.0f}s.")
                        self.next_check_times[village_id_str] = time.time() + wait_time
                        continue

                    upgrade_triggered = False
                    for unit_name in priority_list:
                        research_details = next((u for u in smithy_info.get("researches", []) if u['name'] == unit_name), None)
                        
                        if research_details and research_details.get('level', 0) < 20:
                            if research_details.get("upgrade_url"):
                                log.info(f"[SmithyAgent][{username}] Found unit to upgrade in {village['name']}: {unit_name}")
                                if client.upgrade_unit(village['id'], research_details['upgrade_url']):
                                    log.info(f"[SmithyAgent][{username}] Sent upgrade request for {unit_name} in {village['name']}.")
                                    self.next_check_times[village_id_str] = time.time() + 3
                                    upgrade_triggered = True
                                else:
                                    log.error(f"[SmithyAgent][{username}] Failed to send upgrade request for {unit_name}.")
                                    self.next_check_times[village_id_str] = time.time() + 60 
                                break
                    
                    if not upgrade_triggered:
                        log.info(f"[SmithyAgent][{username}] No units from the priority list were available to upgrade in {village['name']}. Checking again in 30s.")
                        self.next_check_times[village_id_str] = time.time() + 30
                    
                    self.stop_event.wait(0.5)

            except Exception as e:
                log.error(f"[SmithyAgent][{username}] CRITICAL ERROR in main loop: {e}", exc_info=True)
                self.stop_event.wait(300)
            
            self.stop_event.wait(1)