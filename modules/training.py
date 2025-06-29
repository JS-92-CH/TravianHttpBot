# modules/training.py
import time
import re
import threading
from bs4 import BeautifulSoup
from config import log, BOT_STATE, state_lock

class Module(threading.Thread):
    """
    An independent agent thread that handles queuing troops in all villages.
    It moves the hero between villages and queues troops based on configuration.
    """
    def __init__(self, bot_manager, client_class):
        super().__init__()
        self.stop_event = threading.Event()
        self.daemon = True
        self.village_cycle_index = 0
        self.client_class = client_class

    def stop(self):
        self.stop_event.set()

    def run(self):
        log.info("[TrainingAgent] Thread started. Waiting for initial data population...")
        self.stop_event.wait(20)

        # Hardcoded helmet IDs
        INFANTRY_HELMET_ID = 1126
        CAVALRY_HELMET_ID = 1018

        while not self.stop_event.is_set():
            try:
                with state_lock:
                    accounts = BOT_STATE.get("accounts", [])
                    all_village_data = BOT_STATE.get("village_data", {})
                    training_configs = BOT_STATE.get("training_queues", {})

                if not accounts:
                    self.stop_event.wait(60)
                    continue

                account = accounts[0]
                client = self.client_class(account['username'], account['password'], account['server_url'], account.get('proxy'))
                
                if not client.login():
                    log.error("[TrainingAgent] Login failed. Retrying in 5 minutes.")
                    self.stop_event.wait(300)
                    continue

                active_training_villages = [v for v in all_village_data.get(account['username'], []) if str(v['id']) in training_configs and training_configs.get(str(v['id']), {}).get('enabled')]
                if not active_training_villages:
                    log.info("[TrainingAgent] No villages are enabled for training. Waiting...")
                    self.stop_event.wait(60)
                    continue

                if self.village_cycle_index >= len(active_training_villages):
                    self.village_cycle_index = 0
                
                target_village = active_training_villages[self.village_cycle_index]
                target_village_id = target_village['id']
                village_name = target_village['name']
                config = training_configs.get(str(target_village_id), {})

                log.info(f"--- [TrainingAgent] Starting Cycle for Village: {village_name} ({target_village_id}) ---")

                current_hero_location_id = None
                log.info("[TrainingAgent] Searching for hero by checking all village rally points...")
                all_player_villages = all_village_data.get(account['username'], [])
                for village_to_check in all_player_villages:
                    found_in_id = client.find_hero_in_rally_point(village_to_check['id'])
                    if found_in_id:
                        current_hero_location_id = found_in_id
                        break
                    self.stop_event.wait(0.5)

                if current_hero_location_id is None:
                    log.warning("[TrainingAgent] Could not find hero in any village. Hero may be moving. Retrying in 3 minutes.")
                    self.stop_event.wait(180)
                    continue

                if current_hero_location_id != target_village_id:
                    log.info(f"[TrainingAgent] Hero is at {current_hero_location_id}, needs to be at {village_name} ({target_village_id}).")
                    target_coords = all_village_data.get(str(target_village_id), {}).get('coords', {})
                    if not target_coords:
                        fresh_data = client.fetch_and_parse_village(target_village_id)
                        target_coords = fresh_data.get('coords', {}) if fresh_data else {}
                    
                    if target_coords.get('x') is not None:
                        move_success = client.send_hero(int(target_coords['x']), int(target_coords['y']))
                        if move_success:
                            wait_time = 10 
                            log.info(f"[TrainingAgent] Hero reinforcement to {village_name} initiated. Waiting {wait_time}s before next check.")
                            self.stop_event.wait(wait_time)
                        else:
                            log.error(f"[TrainingAgent] Failed to send hero to {village_name}. Waiting 60s.")
                            self.stop_event.wait(60)
                    else:
                            log.warning(f"[TrainingAgent] Target village {village_name} missing coordinates. Skipping.")
                    continue

                log.info(f"[TrainingAgent] Hero is confirmed to be in {village_name}. Starting aggressive training loop.")
                
                while not self.stop_event.is_set():
                    all_queues_filled_for_this_village = True
                    min_queue_seconds = config.get('min_queue_duration_minutes', 15) * 60
                    
                    hero_inventory = client.get_hero_inventory()
                    if not hero_inventory:
                        log.warning("[TrainingAgent] Could not refresh hero inventory. Retrying in 3 minutes.")
                        self.stop_event.wait(180)
                        break

                    for building_type, b_config in sorted(config.get('buildings', {}).items()):
                        if self.stop_event.is_set(): break
                        
                        log.info(f"[TrainingAgent] Checking: {building_type.replace('_', ' ').title()}")
                        if not b_config.get('enabled') or not b_config.get('troop_name'):
                            continue

                        required_helmet_id = None
                        if building_type in ["barracks", "great_barracks"]:
                            required_helmet_id = INFANTRY_HELMET_ID
                        elif building_type in ["stable", "great_stable"]:
                            required_helmet_id = CAVALRY_HELMET_ID

                        if required_helmet_id:
                            equipped_helmet = next((item for item in hero_inventory.get('equipped', []) if item.get('slot') == 'helmet'), None)
                            if not equipped_helmet or equipped_helmet.get('id') != required_helmet_id:
                                log.info(f"[TrainingAgent] Incorrect helmet equipped for {building_type}. Switching...")
                                helmet_to_equip = next((item for item in hero_inventory.get('inventory', []) if item.get('id') == required_helmet_id), None)
                                
                                if helmet_to_equip:
                                    if client.equip_item(helmet_to_equip['id']):
                                        log.info(f"[TrainingAgent] Successfully equipped helmet for {building_type}. Pausing and refreshing inventory.")
                                        self.stop_event.wait(0.25)
                                        hero_inventory = client.get_hero_inventory()
                                    else:
                                        log.error(f"[TrainingAgent] Failed to equip helmet for {building_type}. Skipping.")
                                        continue
                                else:
                                    log.warning(f"[TrainingAgent] Required helmet (ID: {required_helmet_id}) not found in inventory. Skipping {building_type}.")
                                    continue

                        gid = b_config['gid']
                        page_data = client.get_training_page(target_village_id, gid)
                        
                        if not page_data or not page_data.get('trainable'):
                            continue

                        # --- Start of Changes ---
                        # Check if the queue duration is less than 95% of the target duration
                        if page_data['queue_duration_seconds'] < (min_queue_seconds * 0.95):
                            all_queues_filled_for_this_village = False
                            log.info(f"[TrainingAgent] - {building_type} queue ({page_data['queue_duration_seconds']}s) is less than 95% of target ({min_queue_seconds}s).")
                        # --- End of Changes ---
                            
                            troop_to_train = next((t for t in page_data['trainable'] if t['name'] == b_config['troop_name']), None)
                            if not troop_to_train or troop_to_train['time_per_unit'] <= 0:
                                continue
                            
                            time_to_fill = min_queue_seconds - page_data['queue_duration_seconds']
                            amount_based_on_time = int(time_to_fill / troop_to_train['time_per_unit'])
                            max_possible_by_res = troop_to_train.get('max_trainable', 0)
                            amount_to_queue = min(amount_based_on_time, max_possible_by_res)
                            
                            if amount_to_queue > 0:
                                log.info(f"[TrainingAgent] Attempting to queue {amount_to_queue} x {troop_to_train['name']}.")
                                if client.train_troops(target_village_id, page_data['build_id'], page_data['form_data'], {troop_to_train['id']: amount_to_queue}):
                                    log.info(f"[TrainingAgent] Successfully queued troops.")
                                    self.stop_event.wait(2)
                                else:
                                    log.error(f"[TrainingAgent] Failed to queue troops.")
                            else:
                                log.info(f"[TrainingAgent] - Not enough resources to queue even one unit in {building_type}.")
                        else:
                            log.info(f"[TrainingAgent] - {building_type} queue is sufficient.")
                    
                    if all_queues_filled_for_this_village:
                        log.info(f"--- [TrainingAgent] All queues in {village_name} are filled. Moving to next village. ---")
                        break
                    else:
                        log.info(f"[TrainingAgent] Not all queues in {village_name} are full. Waiting 5 seconds before re-checking...")
                        self.stop_event.wait(5)

                self.village_cycle_index += 1
                
            except Exception as e:
                log.error(f"[TrainingAgent] CRITICAL ERROR in training cycle: {e}", exc_info=True)
                self.village_cycle_index += 1
            
            self.stop_event.wait(10)
