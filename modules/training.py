# modules/training.py
import time
import re
import threading
from datetime import datetime, timezone
from bs4 import BeautifulSoup
from config import log, BOT_STATE, state_lock, save_config

class Module(threading.Thread):
    """
    An independent agent thread that handles queuing troops for a single account.
    It moves the hero between villages and queues troops based on configuration.
    """
    def __init__(self, account_info, client_class):
        super().__init__()
        self.stop_event = threading.Event()
        self.daemon = True
        self.account_info = account_info
        self.client_class = client_class
        self.village_cycle_index = 0
        self.current_hero_location_id = None

    def stop(self):
        self.stop_event.set()

    def run(self):
        username = self.account_info['username']
        log.info(f"[TrainingAgent][{username}] Thread started. Waiting for initial data population...")
        self.stop_event.wait(10)

        # Create the client instance ONCE for the agent's entire lifecycle
        client = self.client_class(
            self.account_info['username'],
            self.account_info['password'],
            self.account_info['server_url'],
            self.account_info.get('proxy')
        )

        # Log in ONCE at the beginning of the run method
        if not client.login():
            log.error(f"[TrainingAgent][{username}] Initial login failed. Agent will stop.")
            return

        HELMET_TYPE_IDS = {
            "barracks": [15, 14, 13],
            "stable":   [12, 11, 10],
        }

        while not self.stop_event.is_set():
            try:
                # The agent now works on its own account using its persistent client
                with state_lock:
                    all_village_data = BOT_STATE.get("village_data", {})
                    training_configs = BOT_STATE.get("training_queues", {})

                active_training_villages = [
                    v for v in all_village_data.get(username, [])
                    if str(v['id']) in training_configs and training_configs.get(str(v['id']), {}).get('enabled')
                ]
                if not active_training_villages:
                    log.info(f"[TrainingAgent][{username}] No villages are enabled for training. Waiting...")
                    self.stop_event.wait(60)
                    continue

                if self.village_cycle_index >= len(active_training_villages):
                    self.village_cycle_index = 0

                target_village = active_training_villages[self.village_cycle_index]
                target_village_id = target_village['id']
                village_name = target_village['name']
                config = training_configs.get(str(target_village_id), {})

                log.info(f"--- [TrainingAgent][{username}] Starting Cycle for Village: {village_name} ({target_village_id}) ---")

                if self.current_hero_location_id is None:
                    log.info(f"[TrainingAgent][{username}] Hero location is unknown. Performing a full search...")
                    all_player_villages = all_village_data.get(username, [])
                    for village_to_check in all_player_villages:
                        found_in_id = client.find_hero_in_rally_point(village_to_check['id'])
                        if found_in_id:
                            self.current_hero_location_id = found_in_id
                            log.info(f"[TrainingAgent][{username}] Hero found in {village_to_check['name']} ({found_in_id}).")
                            break
                        self.stop_event.wait(0.5)

                    if self.current_hero_location_id is None:
                        log.warning(f"[{username}] Could not find hero in any village. Hero may be moving. Retrying in 1 minutes.")
                        self.stop_event.wait(60)
                        continue

                if self.current_hero_location_id != target_village_id:
                    log.info(f"[TrainingAgent][{username}] Hero is at {self.current_hero_location_id}, needs to be at {village_name} ({target_village_id}).")

                    with state_lock:
                        # Attempt to get details from the global state first
                        target_village_details = BOT_STATE.get("village_data", {}).get(str(target_village_id))
                    
                    # If not found or incomplete, fetch from the server
                    if not target_village_details or 'coords' not in target_village_details:
                        target_village_details = client.fetch_and_parse_village(target_village_id)

                    target_coords = target_village_details.get('coords', {}) if target_village_details else {}

                    if target_coords.get('x') is not None:
                        move_success, travel_time = client.send_hero(self.current_hero_location_id, int(target_coords['x']), int(target_coords['y']))
                        if move_success:
                            wait_time = travel_time + 0.5
                            log.info(f"[TrainingAgent][{username}] Hero reinforcement to {village_name} initiated. Waiting {wait_time:.1f}s for arrival.")
                            self.stop_event.wait(wait_time)
                            self.current_hero_location_id = target_village_id
                        else:
                            log.error(f"[TrainingAgent][{username}] Failed to send hero to {village_name}. Resetting hero location for next cycle. Waiting 60s.")
                            self.current_hero_location_id = None
                            self.stop_event.wait(60)
                    else:
                        log.warning(f"[TrainingAgent][{username}] Target village {village_name} missing coordinates. Skipping.")

                    continue

                log.info(f"[TrainingAgent][{username}] Hero is confirmed to be in {village_name}. Starting aggressive training loop.")

                while not self.stop_event.is_set():
                    all_queues_filled_for_this_village = True
                    troops_were_queued = False
                    min_queue_seconds = config.get('min_queue_duration_minutes', 15) * 60

                    max_time_str = config.get('max_training_time')
                    remaining_time_cap = float('inf')
                    if max_time_str:
                        try:
                            max_datetime = datetime.strptime(max_time_str, "%d.%m.%Y %H:%M")
                            now_datetime = datetime.now()
                            remaining_time_cap = (max_datetime - now_datetime).total_seconds()
                            if remaining_time_cap <= 0:
                                log.info(f"[TrainingAgent] Max training time for {village_name} has passed. Halting training for this cycle.")
                                break
                            log.info(f"[TrainingAgent] Max training time is set. Remaining time: {remaining_time_cap:.0f}s")
                        except ValueError:
                            log.warning(f"[TrainingAgent] Invalid max_training_time format: '{max_time_str}'. Ignoring.")


                    hero_inventory = client.get_hero_inventory()
                    if not hero_inventory:
                        log.warning("[TrainingAgent] Could not refresh hero inventory. Retrying in 3 minutes.")
                        self.stop_event.wait(180)
                        break

                    processing_order = ['barracks', 'great_barracks', 'stable', 'great_stable', 'workshop']

                    buildings_config = config.get('buildings', {})
                    sorted_buildings = sorted(
                        buildings_config.items(),
                        key=lambda item: processing_order.index(item[0]) if item[0] in processing_order else len(processing_order)
                    )

                    for building_type_key, b_config in sorted_buildings:
                        if self.stop_event.is_set(): break

                        log.info(f"[TrainingAgent] Checking: {building_type_key.replace('_', ' ').title()}")
                        if not b_config.get('enabled') or not b_config.get('troop_name'):
                            continue

                        helmet_category = None
                        if building_type_key in ["barracks", "great_barracks"]:
                            helmet_category = "barracks"
                        elif building_type_key in ["stable", "great_stable"]:
                            helmet_category = "stable"

                        if helmet_category:
                            preferred_type_ids = HELMET_TYPE_IDS.get(helmet_category, [])
                            
                            best_helmet_owned = None
                            for type_id in preferred_type_ids:
                                found = next((item for item in hero_inventory.get('inventory', []) if item.get('typeId') == type_id), None)
                                if found:
                                    best_helmet_owned = found
                                    break

                            if best_helmet_owned:
                                equipped_helmet = next((item for item in hero_inventory.get('equipped', []) if item.get('slot') == 'helmet'), None)
                                
                                if not equipped_helmet or equipped_helmet.get('id') != best_helmet_owned.get('id'):
                                    log.info(f"[TrainingAgent] Equipping best {helmet_category} helmet: {best_helmet_owned.get('name')}")
                                    if client.equip_item(best_helmet_owned.get('id')):
                                        log.info("[TrainingAgent] Successfully equipped helmet. Pausing and refreshing inventory.")
                                        self.stop_event.wait(2)
                                        hero_inventory = client.get_hero_inventory()
                                    else:
                                        log.error("[TrainingAgent] Failed to equip helmet. Skipping.")
                                        continue
                            else:
                                log.info(f"[TrainingAgent] No suitable {helmet_category} helmet found in inventory. Proceeding without one.")


                        gid = b_config['gid']
                        page_data = client.get_training_page(target_village_id, gid)

                        if not page_data or not page_data.get('trainable'):
                            continue

                        current_queue_duration = page_data['queue_duration_seconds']

                        if current_queue_duration >= (remaining_time_cap * 0.98):
                            log.info(f"[TrainingAgent] - {building_type_key} queue ({current_queue_duration}s) is at 98% of the max training time limit. Skipping.")
                            continue

                        if current_queue_duration < (min_queue_seconds * 0.95):
                            all_queues_filled_for_this_village = False
                            log.info(f"[TrainingAgent] - {building_type_key} queue ({current_queue_duration}s) is less than 95% of target ({min_queue_seconds}s).")

                            troop_to_train = next((t for t in page_data['trainable'] if t['name'] == b_config['troop_name']), None)
                            if not troop_to_train or troop_to_train['time_per_unit'] <= 0:
                                continue

                            time_to_fill = min(min_queue_seconds - current_queue_duration, remaining_time_cap - current_queue_duration)

                            if time_to_fill <= 0:
                                continue

                            amount_based_on_time = int(time_to_fill / troop_to_train['time_per_unit'])
                            max_possible_by_res = troop_to_train.get('max_trainable', 0)

                            if amount_based_on_time <= 0:
                                log.info(f"[TrainingAgent] - Not enough time remaining in the configured 'max_training_time' to queue even one {troop_to_train['name']}. (Time needed: {troop_to_train['time_per_unit']:.2f}s, Time available: {time_to_fill:.2f}s)")
                                continue

                            if max_possible_by_res <= 0:
                                log.info(f"[TrainingAgent] - Not enough resources to queue even one {troop_to_train['name']} according to the game's training page.")
                                continue

                            amount_to_queue = min(amount_based_on_time, max_possible_by_res)

                            if amount_to_queue > 0:
                                log.info(f"[TrainingAgent] Attempting to queue {amount_to_queue} x {troop_to_train['name']}.")
                                if client.train_troops(target_village_id, page_data['build_id'], page_data['form_data'], {troop_to_train['id']: amount_to_queue}):
                                    log.info(f"[TrainingAgent] Successfully queued troops. Re-checking village.")
                                    self.stop_event.wait(2)
                                    troops_were_queued = True
                                    break # Exit the building check loop to re-evaluate immediately
                                else:
                                    log.error(f"[TrainingAgent] Failed to queue troops.")
                        else:
                            log.info(f"[TrainingAgent] - {building_type_key} queue is sufficient.")

                    # If we queued troops, restart the loop for this village immediately
                    if troops_were_queued:
                        continue

                    # --- START OF CHANGES ---
                    # If a max training time is set, check if all enabled buildings have reached it.
                    if max_time_str and remaining_time_cap < float('inf'):
                        all_enabled_buildings_at_max_time = True
                        is_any_building_enabled = False

                        # Re-check all buildings with fresh data to confirm they are all at the end time.
                        for building_type_key, b_config in sorted_buildings:
                            if b_config.get('enabled'):
                                is_any_building_enabled = True
                                gid = b_config['gid']
                                page_data = client.get_training_page(target_village_id, gid)
                                
                                if not page_data:
                                    # To be safe, if we can't get data, assume it's not at the max time.
                                    all_enabled_buildings_at_max_time = False
                                    break
                                
                                current_queue_duration = page_data.get('queue_duration_seconds', 0)
                                
                                # If any enabled building's queue is not yet at the time limit, the village is not finished.
                                if current_queue_duration < (remaining_time_cap * 0.98):
                                    all_enabled_buildings_at_max_time = False
                                    break
                        
                        if is_any_building_enabled and all_enabled_buildings_at_max_time:
                            log.info(f"[TrainingAgent] All enabled buildings in {village_name} have reached the end time duration. Disabling training for this village.")
                            with state_lock:
                                if str(target_village_id) in BOT_STATE['training_queues']:
                                    BOT_STATE['training_queues'][str(target_village_id)]['enabled'] = False
                                    save_config()
                            # Exit the aggressive training loop for this village as it's now disabled.
                            break
                    # --- END OF CHANGES ---

                    # If all queues are full, move to the next village
                    if all_queues_filled_for_this_village:
                        auto_increment_enabled = config.get('auto_increment_enabled', False)
                        
                        if auto_increment_enabled:
                            with state_lock:
                                current_duration = config.get('min_queue_duration_minutes', 15)
                                step_size = config.get('auto_increment_step_size', 10)
                                new_duration = current_duration + step_size
                                
                                log.info(f"[TrainingAgent] Increasing max queue duration for {village_name} by {step_size} to {new_duration} minutes for the next cycle.")

                                if str(target_village_id) in BOT_STATE['training_queues']:
                                    BOT_STATE['training_queues'][str(target_village_id)]['min_queue_duration_minutes'] = new_duration
                                    save_config()
                        else:
                            log.info(f"[TrainingAgent] Auto-increment is disabled for {village_name}. Keeping queue time the same.")
                        
                        log.info(f"--- [TrainingAgent][{username}] All queues in {village_name} are filled. Moving to next village. ---")
                        break # Exit the aggressive training loop for this village
                    else:
                        # This part is now only reached if queues aren't full but no action could be taken (e.g., lack of resources)
                        log.info(f"[TrainingAgent][{username}] Not all queues in {village_name} are full, but no action could be taken. Waiting 20 seconds before re-checking...")
                        self.stop_event.wait(20)

                self.village_cycle_index += 1
            except Exception as e:
                log.error(f"[TrainingAgent][{username}] CRITICAL ERROR in training cycle: {e}", exc_info=True)
                self.village_cycle_index += 1

            self.stop_event.wait(4)