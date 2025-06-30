# js-92-ch/travianhttpbot/JS-92-CH-TravianHttpBot-fb2cb2cd128978521c7b03d3e5bab6bb001c5307/modules/demolish.py

import time
import re
import threading
from bs4 import BeautifulSoup
from config import log, BOT_STATE, state_lock, save_config, gid_name

class Module(threading.Thread):
    """
    An independent agent thread that handles demolishing buildings across all villages.
    """
    def __init__(self, bot_manager, client_class):
        super().__init__()
        self.stop_event = threading.Event()
        self.daemon = True
        self.client_class = client_class

    def stop(self):
        self.stop_event.set()

    def run(self):
        log.info("[DemolishAgent] Thread started.")
        self.stop_event.wait(25)

        while not self.stop_event.is_set():
            active_account = None
            try:
                with state_lock:
                    # Find the first active account
                    active_account = next((acc for acc in BOT_STATE.get("accounts", []) if acc.get('active')), None)
                    if not active_account:
                        self.stop_event.wait(60)
                        continue

                    all_village_data = BOT_STATE.get("village_data", {})
                    demolish_queues = BOT_STATE.get("demolish_queues", {})
                
                client = self.client_class(active_account['username'], active_account['password'], active_account['server_url'], active_account.get('proxy'))
                if not client.login():
                    log.error(f"[DemolishAgent] Login failed for {active_account['username']}. Retrying in 1 minute.")
                    self.stop_event.wait(60)
                    continue

                villages_for_account = all_village_data.get(active_account['username'], [])
                
                # Check all villages in one loop
                for village in villages_for_account:
                    if self.stop_event.is_set(): break
                    
                    village_id_str = str(village['id'])
                    if not demolish_queues.get(village_id_str):
                        continue
                    
                    queue = demolish_queues[village_id_str]
                    if not queue:
                        continue

                    task = queue[0]
                    log.info(f"[DemolishAgent] Checking task for {village['name']}: Demolish {gid_name(task['gid'])} to Lvl {task['level']}.")

                    village_details = client.fetch_and_parse_village(village['id'])
                    if not village_details:
                        continue

                    target_building = next((b for b in village_details.get('buildings', []) if b.get('id') == task['location']), None)
                    if not target_building or target_building.get('level', 0) <= task['level']:
                         log.info(f"[DemolishAgent] Task for {village['name']} complete. Removing from queue.")
                         with state_lock:
                             BOT_STATE['demolish_queues'][village_id_str].pop(0)
                         save_config()
                         continue # Move to the next village immediately

                    # Check if Main Building is sufficient
                    main_building = next((b for b in village_details.get('buildings', []) if b.get('gid') == 15), None)
                    if not main_building or main_building.get('level', 0) < 10:
                        log.warning(f"[DemolishAgent] Main Building in {village['name']} is < Lvl 10. Cannot demolish.")
                        continue
                        
                    # Check if something is already being demolished in this village
                    demolish_info = client.get_demolish_info(village['id'])
                    if not demolish_info:
                        log.error(f"[DemolishAgent] Could not retrieve demolish info for {village['name']}.")
                        continue
                    
                    if not demolish_info.get('can_demolish'):
                        log.info(f"[DemolishAgent] A demolition is already active in {village['name']}. Skipping for now.")
                        continue

                    building_to_demolish = next((b for b in demolish_info.get('options', []) if b.get('location_id') == task['location']), None)
                    if not building_to_demolish:
                        log.error(f"[DemolishAgent] Cannot demolish GID {task['gid']} now. It may be a prerequisite for another build/demolish task.")
                        continue
                        
                    log.info(f"[DemolishAgent] Sending demolish request for {gid_name(task['gid'])} in {village['name']}.")
                    client.demolish_building(village['id'], building_to_demolish['value'], demolish_info['form_data'])
                    
                    # Short pause after sending a request to avoid being too aggressive
                    self.stop_event.wait(1)

            except Exception as e:
                log.error(f"[DemolishAgent] CRITICAL ERROR in main loop: {e}", exc_info=True)
            
            # Main sleep interval between full checks of all villages
            self.stop_event.wait(15)