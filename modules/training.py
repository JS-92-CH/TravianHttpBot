# modules/training.py
import time
import re
import threading
from bs4 import BeautifulSoup
from config import log, BOT_STATE, state_lock
from client import TravianClient

class Module(threading.Thread):
    """
    An independent agent thread that handles queuing troops in all villages.
    It moves the hero between villages and queues troops based on configuration.
    """
    def __init__(self, bot_manager):
        super().__init__()
        self.stop_event = threading.Event()
        self.daemon = True
        self.village_cycle_index = 0

    def stop(self):
        self.stop_event.set()

    def run(self):
        log.info("[TrainingAgent] Thread started. Waiting for initial data population...")
        self.stop_event.wait(20)

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
                client = TravianClient(account['username'], account['password'], account['server_url'], account.get('proxy'))
                if not client.login():
                    log.error("[TrainingAgent] Login failed. Retrying in 5 minutes.")
                    self.stop_event.wait(300)
                    continue

                # Get a list of villages that are enabled for training
                active_training_villages = [v for v in all_village_data.get(account['username'], []) if str(v['id']) in training_configs and training_configs.get(str(v['id']), {}).get('enabled')]
                if not active_training_villages:
                    log.info("[TrainingAgent] No villages are enabled for training. Waiting...")
                    self.stop_event.wait(60)
                    continue
                
                # --- THIS IS THE START OF THE CORRECTED LOGIC ---

                # Cycle through the active villages
                if self.village_cycle_index >= len(active_training_villages):
                    self.village_cycle_index = 0
                
                target_village = active_training_villages[self.village_cycle_index]
                target_village_id = target_village['id']
                village_name = target_village['name']
                config = training_configs.get(str(target_village_id), {})

                log.info(f"--- [TrainingAgent] Starting Cycle for Village: {village_name} ({target_village_id}) ---")

                # Find where the hero is
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

                # If hero is not in the target village, move it there and wait.
                if current_hero_location_id != target_village_id:
                    log.info(f"[TrainingAgent] Hero is at {current_hero_location_id}, needs to be at {village_name} ({target_village_id}).")
                    target_coords = all_village_data.get(str(target_village_id), {}).get('coords', {})
                    if not target_coords:
                        fresh_data = client.fetch_and_parse_village(target_village_id)
                        target_coords = fresh_data.get('coords', {}) if fresh_data else {}
                    
                    if target_coords.get('x') is not None:
                        # Use the new send_hero method
                        move_success = client.send_hero(int(target_coords['x']), int(target_coords['y']))
                        if move_success:
                            # Since we don't get travel time back from this new method, we'll wait a fixed time.
                            # You may need to adjust this wait time.
                            wait_time = 10 
                            log.info(f"[TrainingAgent] Hero reinforcement to {village_name} initiated. Waiting {wait_time}s before next check.")
                            self.stop_event.wait(wait_time)
                        else:
                            log.error(f"[TrainingAgent] Failed to send hero to {village_name}. Waiting 60s.")
                            self.stop_event.wait(60)
                    else:
                         log.warning(f"[TrainingAgent] Target village {village_name} missing coordinates. Skipping.")
                    continue # Restart the cycle to re-verify hero location after moving

                # If we reach here, the hero is in the correct village. Now, check ALL training buildings.
                log.info(f"[TrainingAgent] Hero is confirmed to be in the correct village: {village_name}. Checking all training buildings.")
                min_queue_seconds = config.get('min_queue_duration_minutes', 15) * 60
                
                for building_type, b_config in config.get('buildings', {}).items():
                    log.info(f"[TrainingAgent] Checking building: {building_type.replace('_', ' ').title()}")
                    if not b_config.get('enabled'):
                        log.debug(f"[TrainingAgent] - Skipped: {building_type} is disabled in config.")
                        continue
                    if not b_config.get('troop_name'):
                        log.debug(f"[TrainingAgent] - Skipped: No troop name configured for {building_type}.")
                        continue

                    gid = b_config['gid']
                    page_data = client.get_training_page(target_village_id, gid)
                    
                    if not page_data:
                        log.warning(f"[TrainingAgent] - Skipped: Could not get page data for {building_type} (GID: {gid}). It might not be built.")
                        continue
                    if not page_data.get('trainable'):
                        log.info(f"[TrainingAgent] - Skipped: No troops researched yet in {building_type}.")
                        continue

                    # --- Start of Changes ---
                    if page_data['queue_duration_seconds'] < (min_queue_seconds / 2):
                        log.info(f"[TrainingAgent] - Queue for {building_type} is {page_data['queue_duration_seconds']}s, which is less than half the minimum of {min_queue_seconds}s. Attempting to train.")
                    # --- End of Changes ---
                        troop_to_train = next((t for t in page_data['trainable'] if t['name'] == b_config['troop_name']), None)
                        
                        if not troop_to_train:
                            log.warning(f"[TrainingAgent] - Could not find troop '{b_config['troop_name']}' to train in {building_type}.")
                            continue
                        if troop_to_train['time_per_unit'] <= 0:
                            log.warning(f"[TrainingAgent] - Troop '{b_config['troop_name']}' has an invalid training time.")
                            continue
                        
                        time_to_fill = min_queue_seconds - page_data['queue_duration_seconds']
                        amount_to_queue = int(time_to_fill / troop_to_train['time_per_unit'])
                        
                        if amount_to_queue > 0:
                            log.info(f"[TrainingAgent] Attempting to queue {amount_to_queue} x {troop_to_train['name']} in {village_name}'s {building_type}.")
                            success = client.train_troops(target_village_id, page_data['build_id'], page_data['form_data'], {troop_to_train['id']: amount_to_queue})
                            if success:
                                log.info(f"[TrainingAgent] Successfully queued troops in {building_type}.")
                                self.stop_event.wait(2) # Brief pause after a successful action
                            else:
                                log.error(f"[TrainingAgent] Failed to queue troops in {building_type}.")
                        else:
                            log.info(f"[TrainingAgent] - Not enough time to queue even one unit in {building_type}.")
                    else:
                        log.info(f"[TrainingAgent] - {building_type} in {village_name} already has a sufficient queue ({page_data['queue_duration_seconds']}s).")

                # All buildings in the current village have been checked. Now, move to the next village for the next cycle.
                log.info(f"--- [TrainingAgent] Finished Cycle for Village: {village_name}. Moving to next village in the list. ---")
                self.village_cycle_index += 1
                
            except Exception as e:
                log.error(f"[TrainingAgent] CRITICAL ERROR in training cycle: {e}", exc_info=True)
                self.village_cycle_index += 1 # Ensure we don't get stuck on an erroring village
            
            # Wait before starting the entire process over
            self.stop_event.wait(10)