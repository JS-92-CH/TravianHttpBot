# modules/training.py
import time
import re
import threading
from bs4 import BeautifulSoup
from config import log, BOT_STATE, state_lock
from client import TravianClient # Import client for direct use

class Module(threading.Thread):
    """
    An independent agent thread that handles queuing troops in all villages.
    It moves the hero between villages and queues troops based on configuration.
    """
    def __init__(self, bot_manager):
        super().__init__()
        self.bot_manager = bot_manager
        self.stop_event = threading.Event()
        self.daemon = True
        self.village_cycle_index = 0

    def stop(self):
        self.stop_event.set()

    def _get_current_state(self):
        """Safely gets a copy of the needed state from the global BOT_STATE."""
        with state_lock:
            accounts = BOT_STATE.get("accounts", [])
            all_village_data = BOT_STATE.get("village_data", {})
            training_configs = BOT_STATE.get("training_queues", {})
            hero_location = BOT_STATE.get("hero_location", {})

        return accounts, all_village_data, training_configs, hero_location

    def run(self):
        log.info("[TrainingAgent] Thread started.")
        time.sleep(15) # Initial delay to allow village agents to start and populate data

        while not self.stop_event.is_set():
            try:
                accounts, all_village_data, training_configs, hero_location = self._get_current_state()

                if not accounts:
                    self.stop_event.wait(30)
                    continue
                
                # This agent will work for the first configured account.
                account = accounts[0]
                
                villages = []
                account_villages = all_village_data.get(account['username'], [])
                for v_summary in account_villages:
                    v_details = all_village_data.get(str(v_summary['id']))
                    if v_details:
                        v_summary['x'] = v_details.get('coords', {}).get('x')
                        v_summary['y'] = v_details.get('coords', {}).get('y')
                        villages.append(v_summary)
                
                if not training_configs or not villages:
                    log.info("[TrainingAgent] No training configurations or villages found. Waiting...")
                    self.stop_event.wait(60)
                    continue

                active_villages = [v for v in villages if str(v['id']) in training_configs and training_configs.get(str(v['id']), {}).get('enabled')]
                if not active_villages:
                    self.stop_event.wait(60)
                    continue

                # Create a client for this cycle
                client = TravianClient(account['username'], account['password'], account['server_url'], account.get('proxy'))
                if not client.login():
                    log.error("[TrainingAgent] Could not log in. Waiting for the next cycle.")
                    self.stop_event.wait(300)
                    continue
                
                # Cycle through villages
                if self.village_cycle_index >= len(active_villages):
                    self.village_cycle_index = 0

                target_village = active_villages[self.village_cycle_index]
                village_id = target_village['id']
                village_name = target_village['name']
                config = training_configs.get(str(village_id), {})
                log.info(f"[TrainingAgent] Starting cycle for village: {village_name}")

                # 1. Check hero location using the most recent data
                current_hero_location_id = hero_location.get(account['username'])
                
                if current_hero_location_id != village_id:
                   target_x = target_village.get('x')
                   target_y = target_village.get('y')
                   if not target_x or not target_y:
                       log.warning(f"[TrainingAgent] Target village {village_name} has no coordinates. Skipping.")
                   else:
                       log.info(f"[TrainingAgent] Hero is in village {current_hero_location_id}, needs to be in {village_id}. Moving...")
                       move_success, travel_time = client.move_hero(current_hero_location_id, target_x, target_y)
                       if move_success:
                           log.info(f"[TrainingAgent] Hero move to {village_name} initiated. ETA: {travel_time}s.")
                           self.stop_event.wait(travel_time + 5)
                       else:
                           log.error(f"[TrainingAgent] Failed to move hero to {village_name}. Skipping for now.")
                           self.stop_event.wait(60)
                   self.village_cycle_index += 1
                   continue

                # 2. Hero is in the correct village, proceed with training
                log.info(f"[TrainingAgent] Hero is in {village_name}. Checking buildings.")
                min_queue_seconds = config.get('min_queue_duration_minutes', 15) * 60
                building_configs = config.get('buildings', {})

                for building_type, b_config in building_configs.items():
                    if not b_config.get('enabled') or not b_config.get('troop_name'):
                        continue

                    gid = b_config['gid']
                    page_data = client.get_training_page(village_id, gid)
                    
                    if not page_data:
                        log.warning(f"[TrainingAgent] Could not get page data for GID {gid} in {village_name}.")
                        continue
                    
                    if not page_data.get('trainable'):
                        log.info(f"[TrainingAgent] No trainable units in GID {gid} for {village_name}.")
                        continue

                    if page_data['queue_duration_seconds'] < min_queue_seconds:
                        target_troop_name = b_config['troop_name']
                        troop_to_train = next((t for t in page_data['trainable'] if t['name'] == target_troop_name), None)
                        if not troop_to_train: continue
                        
                        time_needed = min_queue_seconds - page_data['queue_duration_seconds']
                        if time_needed <= 0 or troop_to_train['time_per_unit'] <= 0: continue
                        
                        amount_to_queue = int(time_needed / troop_to_train['time_per_unit'])
                        if amount_to_queue == 0: continue
                        
                        # A full implementation would check resources here
                        # For now, we just try to queue
                        
                        log.info(f"[TrainingAgent] Queuing {amount_to_queue} x {troop_to_train['name']} in {village_name}")
                        success = client.train_troops(village_id, page_data['build_id'], page_data['form_data'], {troop_to_train['id']: amount_to_queue})
                        if success:
                            log.info(f"[TrainingAgent] Successfully queued troops in {building_type}.")
                            self.stop_event.wait(2) # Small delay after a successful action
                        else:
                            log.error(f"[TrainingAgent] Failed to queue troops in {building_type}.")
                    else:
                        log.info(f"[TrainingAgent] {building_type} in {village_name} already has a sufficient queue.")

                # Move to next village in the next cycle
                self.village_cycle_index += 1
                self.stop_event.wait(60) # Wait 1 minute before checking the next village

            except Exception as e:
                log.error(f"[TrainingAgent] Critical error in main training cycle: {e}", exc_info=True)
                self.stop_event.wait(300)