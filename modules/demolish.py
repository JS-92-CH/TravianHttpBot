# modules/demolish.py
import time
import re
import threading
from bs4 import BeautifulSoup
from config import log, BOT_STATE, state_lock, save_config, gid_name

class Module(threading.Thread):
    """
    An independent agent thread that handles demolishing buildings for a single account.
    """
    def __init__(self, account_info, client_class):
        super().__init__()
        self.stop_event = threading.Event()
        self.daemon = True
        self.account_info = account_info
        self.client_class = client_class

    def stop(self):
        self.stop_event.set()

    def run(self):
        username = self.account_info['username']
        log.info(f"[DemolishAgent][{username}] Thread started.")
        self.stop_event.wait(25)

        client = self.client_class(
            username,
            self.account_info['password'],
            self.account_info['server_url'],
            self.account_info.get('proxy')
        )
        if not client.login():
            log.error(f"[DemolishAgent][{username}] Initial login failed. Agent will stop.")
            return

        while not self.stop_event.is_set():
            try:
                with state_lock:
                    all_village_data = BOT_STATE.get("village_data", {})
                    demolish_queues = BOT_STATE.get("demolish_queues", {})
                
                villages_for_account = all_village_data.get(username, [])
                
                for village in villages_for_account:
                    if self.stop_event.is_set(): break
                    
                    village_id_str = str(village['id'])
                    if not demolish_queues.get(village_id_str):
                        continue
                    
                    queue = demolish_queues[village_id_str]
                    if not queue:
                        continue

                    task = queue[0]
                    log.info(f"[DemolishAgent][{username}] Checking task for {village['name']}: Demolish {gid_name(task['gid'])} to Lvl {task['level']}.")

                    village_details = client.fetch_and_parse_village(village['id'])
                    if not village_details:
                        continue

                    target_building = next((b for b in village_details.get('buildings', []) if b.get('id') == task['location']), None)
                    if not target_building or target_building.get('level', 0) <= task['level']:
                         log.info(f"[DemolishAgent][{username}] Task for {village['name']} complete. Removing from queue.")
                         with state_lock:
                             BOT_STATE['demolish_queues'][village_id_str].pop(0)
                         save_config()
                         continue

                    main_building = next((b for b in village_details.get('buildings', []) if b.get('gid') == 15), None)
                    if not main_building or main_building.get('level', 0) < 10:
                        log.warning(f"[DemolishAgent][{username}] Main Building in {village['name']} is < Lvl 10. Cannot demolish.")
                        continue
                        
                    demolish_info = client.get_demolish_info(village['id'])
                    if not demolish_info:
                        log.error(f"[DemolishAgent][{username}] Could not retrieve demolish info for {village['name']}.")
                        continue
                    
                    if not demolish_info.get('can_demolish'):
                        log.info(f"[DemolishAgent][{username}] A demolition is already active in {village['name']}. Skipping for now.")
                        continue

                    building_to_demolish = next((b for b in demolish_info.get('options', []) if b.get('location_id') == task['location']), None)
                    if not building_to_demolish:
                        log.error(f"[DemolishAgent][{username}] Cannot demolish GID {task['gid']} now. It may be a prerequisite.")
                        continue
                        
                    log.info(f"[DemolishAgent][{username}] Sending demolish request for {gid_name(task['gid'])} in {village['name']}.")
                    client.demolish_building(village['id'], building_to_demolish['value'], demolish_info['form_data'])
                    
                    self.stop_event.wait(1)

            except Exception as e:
                log.error(f"[DemolishAgent][{username}] CRITICAL ERROR in main loop: {e}", exc_info=True)
            
            self.stop_event.wait(10)