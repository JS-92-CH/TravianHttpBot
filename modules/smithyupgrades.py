# modules/smithyupgrades.py

import time
import threading
from config import log, BOT_STATE, state_lock

class Module(threading.Thread):
    """
    An independent agent thread that handles smithy upgrades for a single account.
    """
    def __init__(self, account_agent, client_class):
        super().__init__()
        self.account_agent = account_agent
        self.client = account_agent.client
        self.username = self.client.username
        self.stop_event = threading.Event()
        self.daemon = True
        self.next_check_times = {} # Tracks check times per village

    def stop(self):
        self.stop_event.set()

    def run(self):
        log.info(f"[{self.username}][SmithyAgent] Thread started.")
        self.stop_event.wait(30) # Initial wait

        while not self.stop_event.is_set():
            try:
                with state_lock:
                    villages_for_this_account = BOT_STATE.get("village_data", {}).get(self.username, [])
                    upgrade_configs = BOT_STATE.get("smithy_upgrades", {})

                if not villages_for_this_account:
                    self.stop_event.wait(60)
                    continue

                for village in villages_for_this_account:
                    if self.stop_event.is_set(): break

                    village_id_str = str(village['id'])
                    
                    if time.time() < self.next_check_times.get(village_id_str, 0):
                        continue

                    config = upgrade_configs.get(village_id_str)
                    if not config or not config.get("enabled") or not config.get("priority"):
                        self.next_check_times[village_id_str] = time.time() + 300 # Check disabled villages every 5 mins
                        continue

                    log.info(f"[{self.username}][SmithyAgent] Checking smithy for {village['name']}...")
                    smithy_info = self.client.get_smithy_page(village['id'])

                    if not smithy_info:
                        log.warning(f"[{self.username}][SmithyAgent] Could not retrieve smithy info for {village['name']}. Retrying in 5 mins.")
                        self.next_check_times[village_id_str] = time.time() + 300
                        continue
                    
                    with state_lock:
                        BOT_STATE.setdefault('smithy_data', {})[village_id_str] = smithy_info

                    current_queue = smithy_info.get("research_queue", [])
                    max_queue = 2 if smithy_info.get("plus_account", False) else 1

                    if len(current_queue) >= max_queue:
                        earliest_eta = min(item['eta'] for item in current_queue) if current_queue else 300
                        wait_time = max(2, earliest_eta + 1)
                        log.info(f"[{self.username}][SmithyAgent] Smithy queue is full for {village['name']}. Next check in {wait_time:.0f}s.")
                        self.next_check_times[village_id_str] = time.time() + wait_time
                        continue

                    # Attempt to fill an open queue slot
                    upgrade_triggered = False
                    for unit_name in config.get("priority", []):
                        unit_to_upgrade = next((u for u in smithy_info.get("researches", []) if u['name'] == unit_name and u.get("upgrade_url")), None)
                        if unit_to_upgrade:
                            log.info(f"[{self.username}][SmithyAgent] Found unit to upgrade in {village['name']}: {unit_name}")
                            if self.client.upgrade_unit(village['id'], unit_to_upgrade['upgrade_url']):
                                log.info(f"[{self.username}][SmithyAgent] Sent upgrade request for {unit_name} in {village['name']}.")
                                self.next_check_times[village_id_str] = time.time() + 3 # Re-check quickly for dual queue
                                upgrade_triggered = True
                            else:
                                log.error(f"[{self.username}][SmithyAgent] Failed to send upgrade request for {unit_name}.")
                                self.next_check_times[village_id_str] = time.time() + 60
                            break # Exit after one attempt
                    
                    if not upgrade_triggered:
                        log.info(f"[{self.username}][SmithyAgent] No units from the priority list were available to upgrade in {village['name']}. Checking again in 5 mins.")
                        self.next_check_times[village_id_str] = time.time() + 300
                    
                    self.stop_event.wait(0.5) # Small delay between villages

            except Exception as e:
                log.error(f"[{self.username}][SmithyAgent] CRITICAL ERROR in main loop: {e}", exc_info=True)
                self.stop_event.wait(300)
            
            self.stop_event.wait(10) # Main loop delay
