# bot.py

import time
import threading
import copy
from typing import Dict, Optional, List
from modules.adventure import Module as AdventureModule
from modules.hero import Module as HeroModule
from modules.training import Module as TrainingModule
from modules.demolish import Module as DemolishModule
from modules.smithyupgrades import Module as SmithyModule
from client import TravianClient
from config import log, BOT_STATE, state_lock, save_config
from modules import load_modules

class VillageAgent(threading.Thread):
    def __init__(self, account_info: Dict, village_info: Dict, socketio_instance):
        super().__init__()
        self.client = TravianClient(
            account_info["username"],
            account_info["password"],
            account_info["server_url"],
            account_info.get("proxy")
        )
        self.village_id = village_info['id']
        self.village_name = village_info['name']
        self.socketio = socketio_instance
        self.tribe = account_info.get("tribe", "roman")
        self.building_logic = account_info.get("building_logic", "default")
        self.use_dual_queue = account_info.get("use_dual_queue", False)
        self.use_hero_resources = account_info.get("use_hero_resources", False)
        self.stop_event = threading.Event()
        self.daemon = True
        self.modules = load_modules(self)
        self.building_module = next((m for m in self.modules if 'building' in type(m).__module__), None)
        self.resources_module = next((m for m in self.modules if 'resources' in type(m).__module__), None)
        self.next_check_time = time.time()

    def stop(self):
        self.stop_event.set()

    def run(self):
        log.info(f"Agent started for village: {self.village_name} ({self.village_id})")

        if not self.client.login():
            log.error(f"Agent for {self.village_name} ({self.village_id}) failed to log in and will be stopped.")
            return

        while not self.stop_event.is_set():
            try:
                if time.time() < self.next_check_time:
                    self.stop_event.wait(1)
                    continue

                log.info(f"[{self.village_name}] Refreshing village data for logic loop...")
                village_data = self.client.fetch_and_parse_village(self.village_id)

                if not village_data or not village_data.get("buildings"):
                    log.warning(f"[{self.village_name}] Data fetch was incomplete. Retrying in 60 seconds.")
                    self.next_check_time = time.time() + 60
                    continue
                
                with state_lock:
                    if str(self.village_id) not in BOT_STATE['training_data']:
                        BOT_STATE['training_data'][str(self.village_id)] = {}
                    
                    training_gids = [19, 20, 21, 29, 30] # Barracks, Stable, etc.
                    for gid in training_gids:
                        if any(b['gid'] == gid for b in village_data.get('buildings', [])):
                            training_page_data = self.client.get_training_page(self.village_id, gid)
                            if training_page_data:
                                BOT_STATE['training_data'][str(self.village_id)][str(gid)] = training_page_data

                    if any(b['gid'] == 13 for b in village_data.get('buildings', [])):
                        smithy_page_data = self.client.get_smithy_page(self.village_id)
                        if smithy_page_data:
                            BOT_STATE['smithy_data'][str(self.village_id)] = smithy_page_data
                
                with state_lock:
                    BOT_STATE["village_data"][str(self.village_id)] = village_data

                for module in self.modules:
                    if module == self.building_module:
                        continue
                    try:
                        module.tick(village_data)
                    except Exception as e:
                        log.error(f"[{self.village_name}] Error in module {type(module).__name__}: {e}", exc_info=True)
                
                if self.building_module:
                    while not self.stop_event.is_set():
                        # Fetch another copy right before the build attempt for maximum freshness
                        current_village_data_for_build = self.client.fetch_and_parse_village(self.village_id)
                        if not current_village_data_for_build:
                            log.warning(f"[{self.village_name}] Could not fetch village data for building loop. Waiting a moment.")
                            time.sleep(15)
                            continue

                        max_queue_length = 2 if self.use_dual_queue else 1
                        current_queue_length = len(current_village_data_for_build.get("queue", []))

                        if current_queue_length >= max_queue_length:
                            log.info(f"[{self.village_name}] Server build queue is full ({current_queue_length}/{max_queue_length}).")
                            break

                        log.info(f"[{self.village_name}] Queue has open slot ({current_queue_length}/{max_queue_length}). Attempting to build.")
                        
                        # Pass the freshest data to the building module
                        build_eta = self.building_module.tick(current_village_data_for_build)

                        if build_eta <= 0:
                            log.info(f"[{self.village_name}] No more buildings to queue or an error occurred. Exiting build loop.")
                            break
                        
                        time.sleep(0.25)

                final_data = self.client.fetch_and_parse_village(self.village_id)
                if final_data and final_data.get("queue"):
                    server_eta = min([b.get('eta', 3600) for b in final_data["queue"]])
                    wait_time = max(5, server_eta + 0.5) 
                    self.next_check_time = time.time() + wait_time
                    log.info(f"[{self.village_name}] Construction active. Next main check in {wait_time:.0f}s.")
                else:
                    self.next_check_time = time.time() + 60
                    log.info(f"[{self.village_name}] No construction active. Next main check in 60s.")


            except Exception as e:
                log.error(f"Agent for {self.village_name} encountered a CRITICAL ERROR: {e}", exc_info=True)
                self.next_check_time = time.time() + 300 

        log.info(f"Agent stopped for village: {self.village_name} ({self.village_id})")

class BotManager(threading.Thread):
    def __init__(self, socketio_instance):
        super().__init__()
        self.socketio = socketio_instance
        self.stop_event = threading.Event()
        self.running_account_agents: Dict[str, List[VillageAgent]] = {}
        self.adventure_module = AdventureModule(self)
        self.hero_module = HeroModule(self)
        self.training_module = TrainingModule(self, TravianClient)
        self.demolish_module = DemolishModule(self, TravianClient)
        self.smithy_module = SmithyModule(self, TravianClient)
        self.daemon = True
        self._ui_updater_thread = threading.Thread(target=self._ui_updater, daemon=True)

    def _ui_updater(self):
        log.info("UI Updater thread started.")
        while not self.stop_event.is_set():
            with state_lock:
                try:
                    # Send a deep copy to avoid race conditions during serialization
                    self.socketio.emit("state_update", copy.deepcopy(BOT_STATE))
                except Exception as e:
                    log.error(f"Error in UI updater: {e}", exc_info=True)
            self.stop_event.wait(2) # Update UI every 2 seconds
        log.info("UI Updater thread stopped.")

    def stop(self):
        log.info("Stopping Bot Manager and all active agents...")
        self.stop_event.set()
        with state_lock:
            for acc in BOT_STATE['accounts']:
                acc['active'] = False
            save_config()
            
        for username in list(self.running_account_agents.keys()):
            self._stop_agents_for_account(username)

        log.info("Stopping training agent...")
        self.training_module.stop()
        self.training_module.join()

        log.info("Stopping demolish agent...")
        self.demolish_module.stop()
        self.demolish_module.join()

        log.info("Stopping smithy agent...")
        self.smithy_module.stop()             
        self.smithy_module.join()
        
        if self._ui_updater_thread.is_alive():
            self._ui_updater_thread.join()

        log.info("Bot Manager stopped.")

    def _stop_agents_for_account(self, username: str):
        if username in self.running_account_agents:
            log.info(f"Stopping agents for account: {username}")
            agents = self.running_account_agents.pop(username, [])
            for agent in agents:
                agent.stop()
            for agent in agents:
                agent.join()
            log.info(f"All agents for {username} stopped.")
            with state_lock:
                villages_to_clear = [v['id'] for v in BOT_STATE['village_data'].get(username, [])]
                for vid in villages_to_clear:
                    BOT_STATE['village_data'].pop(str(vid), None)
                BOT_STATE['village_data'].pop(username, None)

    def run(self):
        log.info("Bot Manager thread started and is now monitoring accounts.")
        log.info("Starting independent training agent...")
        self.training_module.start()
        log.info("Starting independent demolish agent...")
        self.demolish_module.start()
        log.info("Starting independent smithy agent...")
        self.smithy_module.start() 
        self._ui_updater_thread.start()

        while not self.stop_event.is_set():
            try:
                with state_lock:
                    accounts = [acc.copy() for acc in BOT_STATE["accounts"]]

                for account in accounts:
                    username = account['username']
                    if account.get('active') and username not in self.running_account_agents:
                        self.start_agents_for_account(account)

                for username in list(self.running_account_agents.keys()):
                    account_is_active = any(
                        acc['username'] == username and acc.get('active') for acc in accounts
                    )
                    if not account_is_active:
                        self._stop_agents_for_account(username)
                
                for account in accounts:
                     if account.get('active'):
                        temp_client = TravianClient(
                            account["username"], account["password"],
                            account["server_url"], account.get("proxy")
                        )
                        if temp_client.login():
                            try:
                                self.adventure_module.tick(temp_client)
                                time.sleep(1) 
                                self.hero_module.tick(temp_client)
                            except Exception as e:
                                log.error(f"Error in account-level module for {account['username']}: {e}", exc_info=True)
                        else:
                            log.error(f"[{account['username']}] Login failed for account-level module check.")

            except Exception as e:
                log.error(f"Critical error in BotManager loop: {e}", exc_info=True)

            self.stop_event.wait(10)

    def start_agents_for_account(self, account_info: Dict):
        username = account_info['username']
        log.info(f"Attempting to start agents for account: {username}")
        
        temp_client = TravianClient(
            account_info["username"], account_info["password"],
            account_info["server_url"], account_info.get("proxy")
        )

        if not temp_client.login():
            log.error(f"[{username}] Login failed. Cannot start agents for this account.")
            with state_lock:
                for acc in BOT_STATE['accounts']:
                    if acc['username'] == username:
                        acc['active'] = False
                        break
            save_config()
            return

        try:
            resp = temp_client.sess.get(f"{temp_client.server_url}/dorf1.php", timeout=15)
            sidebar_data = temp_client.parse_village_page(resp.text, "dorf1")
            villages = sidebar_data.get("villages", [])

            with state_lock:
                BOT_STATE["village_data"][username] = villages
                if 'training_queues' not in BOT_STATE: BOT_STATE['training_queues'] = {}
                if 'smithy_upgrades' not in BOT_STATE: BOT_STATE['smithy_upgrades'] = {}
                config_updated = False
                for v in villages:
                    if str(v['id']) not in BOT_STATE['training_queues']:
                        log.info(f"Creating default (disabled) training config for new village {v['name']}")
                        BOT_STATE['training_queues'][str(v['id'])] = {
                            "enabled": False, "min_queue_duration_minutes": 15,
                            "buildings": {
                                "barracks": {"gid":19,"enabled":False,"troop_name":""}, "stable": {"gid":20,"enabled":False,"troop_name":""},
                                "workshop": {"gid":21,"enabled":False,"troop_name":""}, "great_barracks": {"gid":29,"enabled":False,"troop_name":""},
                                "great_stable": {"gid":30,"enabled":False,"troop_name":""},
                            }
                        }

                    if str(v['id']) not in BOT_STATE['smithy_upgrades']:
                        log.info(f"Creating default (disabled) smithy upgrade config for new village {v['name']}")
                        BOT_STATE['smithy_upgrades'][str(v['id'])] = {
                            "enabled": False,
                            "priority": []
                        }
                        config_updated = True
                if config_updated: save_config()

            self.running_account_agents[username] = []
            for village in villages:
                log.info(f"Creating new agent for village {village['name']} under account {username}")
                agent = VillageAgent(account_info, village, self.socketio)
                self.running_account_agents[username].append(agent)
                agent.start()

        except Exception as exc:
            log.error(f"Failed to start village agents for {username}: {exc}", exc_info=True)
            self.running_account_agents.pop(username, None)