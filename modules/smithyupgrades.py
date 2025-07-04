# modules/smithyupgrades.py

import time
import re
import threading
from bs4 import BeautifulSoup
from config import log, BOT_STATE, state_lock, save_config

class Module(threading.Thread):
    def __init__(self, bot_manager, client_class):
        super().__init__()
        self.stop_event = threading.Event()
        self.daemon = True
        self.client_class = client_class
        # Tracks the next time we should check a specific village
        self.next_check_times = {}

    def stop(self):
        self.stop_event.set()

    def run(self):
        log.info("[SmithyAgent] Thread started.")
        self.stop_event.wait(30)

        while not self.stop_event.is_set():
            active_account = None
            try:
                with state_lock:
                    active_account = next((acc for acc in BOT_STATE.get("accounts", []) if acc.get('active')), None)
                    if not active_account:
                        self.stop_event.wait(60)
                        continue

                    all_village_data = BOT_STATE.get("village_data", {})
                    upgrade_queues = BOT_STATE.get("smithy_upgrades", {})

                client = self.client_class(active_account['username'], active_account['password'], active_account['server_url'], active_account.get('proxy'))
                if not client.login():
                    log.error(f"[SmithyAgent] Login failed for {active_account['username']}. Retrying in 1 minute.")
                    self.stop_event.wait(60)
                    continue

                villages_for_account = all_village_data.get(active_account['username'], [])

                for village in villages_for_account:
                    if self.stop_event.is_set(): break

                    village_id_str = str(village['id'])
                    
                    # Check if it's time to process this specific village
                    if time.time() < self.next_check_times.get(village_id_str, 0):
                        continue

                    config = upgrade_queues.get(village_id_str)
                    if not config or not config.get("enabled"):
                        self.next_check_times[village_id_str] = time.time() + 300 # Check disabled villages every 5 mins
                        continue

                    priority_list = config.get("priority", [])
                    if not priority_list:
                        self.next_check_times[village_id_str] = time.time() + 300
                        continue

                    log.info(f"[SmithyAgent] Checking smithy for {village['name']}...")
                    smithy_info = client.get_smithy_page(village['id'])

                    if not smithy_info:
                        log.warning(f"[SmithyAgent] Could not retrieve smithy info for {village['name']}. Retrying in 5 mins.")
                        self.next_check_times[village_id_str] = time.time() + 300
                        continue
                    
                    with state_lock:
                        BOT_STATE['smithy_data'][village_id_str] = smithy_info

                    current_queue = smithy_info.get("research_queue", [])
                    max_queue = 2 if smithy_info.get("plus_account", False) else 1

                    if len(current_queue) >= max_queue:
                        earliest_eta = min(item['eta'] for item in current_queue) if current_queue else 300
                        wait_time = max(2, earliest_eta + 1) # Wait 1s after completion
                        log.info(f"[SmithyAgent] Smithy queue is full for {village['name']}. Next check in {wait_time:.0f}s.")
                        self.next_check_times[village_id_str] = time.time() + wait_time
                        continue

                    # If we reach here, there's a free slot. Let's try to fill it.
                    upgrade_triggered = False
                    for unit_name in priority_list:
                        unit_to_upgrade = next((u for u in smithy_info.get("researches", []) if u['name'] == unit_name and u.get("upgrade_url")), None)
                        if unit_to_upgrade:
                            log.info(f"[SmithyAgent] Found unit to upgrade in {village['name']}: {unit_name}")
                            if client.upgrade_unit(village['id'], unit_to_upgrade['upgrade_url']):
                                log.info(f"[SmithyAgent] Sent upgrade request for {unit_name} in {village['name']}.")
                                # Set a very short delay to re-check immediately for dual queue
                                self.next_check_times[village_id_str] = time.time() + 3
                                upgrade_triggered = True
                            else:
                                log.error(f"[SmithyAgent] Failed to send upgrade request for {unit_name}.")
                                self.next_check_times[village_id_str] = time.time() + 60 
                            break 
                    
                    if not upgrade_triggered:
                        log.info(f"[SmithyAgent] No units from the priority list were available to upgrade in {village['name']}. Checking again in 0.5 min.")
                        self.next_check_times[village_id_str] = time.time() + 30
                    
                    # Small delay between processing each village in the main loop
                    self.stop_event.wait(0.5)

            except Exception as e:
                log.error(f"[SmithyAgent] CRITICAL ERROR in main loop: {e}", exc_info=True)
                self.stop_event.wait(300)
            
            # This is the agent's main loop delay, preventing it from spinning too fast
            # if all villages have long ETAs.
            self.stop_event.wait(1)