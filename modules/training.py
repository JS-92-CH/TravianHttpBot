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
        self.stop_event.wait(20) # Initial delay for village agents to start

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

                villages = []
                account_villages = all_village_data.get(account['username'], [])
                for v_summary in account_villages:
                    v_details = all_village_data.get(str(v_summary['id']))
                    if v_details:
                        v_summary['x'] = v_details.get('coords', {}).get('x')
                        v_summary['y'] = v_details.get('coords', {}).get('y')
                        villages.append(v_summary)

                active_villages = [v for v in villages if str(v['id']) in training_configs and training_configs.get(str(v['id']), {}).get('enabled')]
                if not active_villages:
                    self.stop_event.wait(60)
                    continue

                if self.village_cycle_index >= len(active_villages):
                    self.village_cycle_index = 0

                target_village = active_villages[self.village_cycle_index]
                village_id = target_village['id']
                village_name = target_village['name']
                config = training_configs.get(str(village_id), {})
                log.info(f"[TrainingAgent] Starting cycle for village: {village_name}")

                current_hero_location_id = client.get_hero_initial_location()

                if current_hero_location_id is None:
                     log.warning("[TrainingAgent] Could not determine hero location. Skipping cycle.")
                     self.stop_event.wait(60)
                     continue

                if current_hero_location_id != village_id:
                    target_x = target_village.get('x')
                    target_y = target_village.get('y')
                    if not target_x or not target_y:
                        log.warning(f"[TrainingAgent] Target village {village_name} missing coordinates. Skipping.")
                    else:
                        log.info(f"[TrainingAgent] Hero is at village {current_hero_location_id}, needs to be at {village_id}. Moving...")
                        move_success, travel_time = client.move_hero(current_hero_location_id, int(target_x), int(target_y))
                        if move_success:
                            log.info(f"[TrainingAgent] Hero move to {village_name} initiated. Waiting {travel_time}s.")
                            self.stop_event.wait(travel_time + 5)
                        else:
                            log.error(f"[TrainingAgent] Failed to move hero to {village_name}. Waiting 60s.")
                            self.stop_event.wait(60)
                    self.village_cycle_index += 1
                    continue
                
                # Fetch fresh village data for resource check
                current_village_resources = client.fetch_and_parse_village(village_id)
                if not current_village_resources:
                    log.warning(f"[TrainingAgent] Could not fetch village resources for {village_name}. Skipping.")
                    self.village_cycle_index += 1
                    continue

                log.info(f"[TrainingAgent] Hero is in {village_name}. Checking buildings.")
                min_queue_seconds = config.get('min_queue_duration_minutes', 15) * 60
                
                for building_type, b_config in config.get('buildings', {}).items():
                    if not b_config.get('enabled') or not b_config.get('troop_name'):
                        continue

                    gid = b_config['gid']
                    page_data = client.get_training_page(village_id, gid)
                    if not page_data or not page_data.get('trainable'):
                        continue

                    if page_data['queue_duration_seconds'] < min_queue_seconds:
                        target_troop_name = b_config['troop_name']
                        troop_to_train = next((t for t in page_data['trainable'] if t['name'] == target_troop_name), None)
                        if not troop_to_train: continue

                        time_to_fill = min_queue_seconds - page_data['queue_duration_seconds']
                        if time_to_fill <= 0 or troop_to_train['time_per_unit'] <= 0: continue
                        
                        amount_to_queue = int(time_to_fill / troop_to_train['time_per_unit'])
                        if amount_to_queue <= 0: continue

                        log.info(f"[TrainingAgent] Attempting to queue {amount_to_queue} x {troop_to_train['name']} in {village_name}")
                        success = client.train_troops(village_id, page_data['build_id'], page_data['form_data'], {troop_to_train['id']: amount_to_queue})
                        if success:
                            log.info(f"[TrainingAgent] Successfully queued troops in {building_type}.")
                            self.stop_event.wait(2)
                        else:
                            log.error(f"[TrainingAgent] Failed to queue troops in {building_type}.")
                    else:
                        log.info(f"[TrainingAgent] {building_type} in {village_name} already has a sufficient queue.")

                self.village_cycle_index += 1
                
            except Exception as e:
                log.error(f"[TrainingAgent] CRITICAL ERROR in training cycle: {e}", exc_info=True)
            
            self.stop_event.wait(10)