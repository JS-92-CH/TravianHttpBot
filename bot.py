# bot.py

import time
import threading
import copy
from typing import Dict, Optional, List
from modules.adventure import Module as AdventureModule
from modules.hero import Module as HeroModule
from modules.training import Module as TrainingModule
# We still import it to use it
from modules.demolish import Module as DemolishModule
from modules.smithyupgrades import Module as SmithyModule
from client import TravianClient
from config import log, BOT_STATE, state_lock, save_config
from modules import load_modules
from proxy_util import test_proxy

class VillageAgent(threading.Thread):
    # ... (this class is unchanged)
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

                    # Check if a smithy (gid 13) exists in the village
                    smithy_building = next((b for b in village_data.get('buildings', []) if b.get('gid') == 13), None)

                    if smithy_building:
                        # Call with the correct GID, not the location ID
                        smithy_page_data = self.client.get_smithy_page(self.village_id, 13)
                        if smithy_page_data:
                            with state_lock:
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
        self.running_training_agents: Dict[str, TrainingModule] = {}
        self.running_smithy_agents: Dict[str, SmithyModule] = {}
        self.running_demolish_agents: Dict[str, DemolishModule] = {}
        # --- START OF CHANGES ---
        # This dictionary will hold the single, reusable, logged-in client for each account.
        self.running_account_clients: Dict[str, TravianClient] = {}
        # --- END OF CHANGES ---
        self.adventure_module = AdventureModule(self)
        self.hero_module = HeroModule(self)
        self.daemon = True
        self._ui_updater_thread = threading.Thread(target=self._ui_updater, daemon=True)

    def _ui_updater(self):
        # ... (this method is unchanged)
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
        # ... (this method is unchanged)
        log.info("Stopping Bot Manager and all active agents...")
        self.stop_event.set()
        with state_lock:
            for acc in BOT_STATE['accounts']:
                acc['active'] = False
            save_config()
            
        for username in list(self.running_account_agents.keys()):
            self._stop_agents_for_account(username)

        
        if self._ui_updater_thread.is_alive():
            self._ui_updater_thread.join()

        log.info("Bot Manager stopped.")

    def _stop_agents_for_account(self, username: str):
        # --- START OF CHANGES ---
        # Remove the persistent client for the stopped account
        if username in self.running_account_clients:
            log.info(f"Removing persistent client for account: {username}")
            self.running_account_clients.pop(username, None)
        # --- END OF CHANGES ---

        if username in self.running_training_agents:
            log.info(f"Stopping training agent for account: {username}")
            training_agent = self.running_training_agents.pop(username, None)
            if training_agent:
                training_agent.stop()
                training_agent.join()

        if username in self.running_smithy_agents:
            log.info(f"Stopping smithy agent for account: {username}")
            smithy_agent = self.running_smithy_agents.pop(username, None)
            if smithy_agent:
                smithy_agent.stop()
                smithy_agent.join()

        if username in self.running_demolish_agents:
            log.info(f"Stopping demolish agent for account: {username}")
            demolish_agent = self.running_demolish_agents.pop(username, None)
            if demolish_agent:
                demolish_agent.stop()
                demolish_agent.join()

        if username in self.running_account_agents:
            log.info(f"Stopping village agents for account: {username}")
            agents = self.running_account_agents.pop(username, [])
            for agent in agents:
                agent.stop()
            for agent in agents:
                agent.join()
            log.info(f"All village agents for {username} stopped.")
            with state_lock:
                villages_to_clear = [v['id'] for v in BOT_STATE['village_data'].get(username, [])]
                for vid in villages_to_clear:
                    BOT_STATE['village_data'].pop(str(vid), None)
                BOT_STATE['village_data'].pop(username, None)

    def run(self):
        log.info("Bot Manager thread started and is now monitoring accounts.")
        self._ui_updater_thread.start()

        while not self.stop_event.is_set():
            try:
                with state_lock:
                    accounts = [acc.copy() for acc in BOT_STATE["accounts"]]

                active_usernames = {acc['username'] for acc in accounts if acc.get('active')}
                running_usernames = set(self.running_account_agents.keys())

                # Start agents and clients for new accounts
                for username_to_start in active_usernames - running_usernames:
                    account_info = next((acc for acc in accounts if acc['username'] == username_to_start), None)
                    if account_info:
                        self.start_agents_for_account(account_info)

                # Stop agents and clients for deactivated accounts
                for username_to_stop in running_usernames - active_usernames:
                    self._stop_agents_for_account(username_to_stop)
                
                # --- START OF CHANGES ---
                # Use the persistent, logged-in clients for general tasks
                for username in active_usernames:
                    client = self.running_account_clients.get(username)
                    if client:
                        try:
                            self.adventure_module.tick(client)
                            time.sleep(1) 
                            self.hero_module.tick(client)
                        except Exception as e:
                            log.error(f"Error in account-level module for {username}: {e}", exc_info=True)
                    else:
                        # This can happen briefly while an account is starting up
                        log.debug(f"No active client found for running account {username}. It may be starting.")
                # --- END OF CHANGES ---

            except Exception as e:
                log.error(f"Critical error in BotManager loop: {e}", exc_info=True)

            self.stop_event.wait(10)

    def start_agents_for_account(self, account_info: Dict):
        username = account_info['username']
        log.info(f"Attempting to start agents for account: {username}")
        
        proxy_settings = account_info.get("proxy")
        if proxy_settings and proxy_settings.get('ip'):
            if not test_proxy(proxy_settings):
                log.error(f"[{username}] Proxy check failed. Aborting agent startup for this account.")
                with state_lock:
                    for acc in BOT_STATE['accounts']:
                        if acc['username'] == username:
                            acc['active'] = False
                            break
                save_config()
                return
        
        # --- START OF CHANGES ---
        # Create the single client instance that will be reused
        persistent_client = TravianClient(
            account_info["username"], account_info["password"],
            account_info["server_url"], account_info.get("proxy")
        )

        if not persistent_client.login():
            log.error(f"[{username}] Login failed. Cannot start agents for this account.")
            with state_lock:
                for acc in BOT_STATE['accounts']:
                    if acc['username'] == username:
                        acc['active'] = False
                        break
            save_config()
            return

        # Store the successfully logged-in client for reuse
        self.running_account_clients[username] = persistent_client
        # --- END OF CHANGES ---

        try:
            # Use the new persistent client to fetch initial data
            resp = persistent_client.sess.get(f"{persistent_client.server_url}/dorf1.php", timeout=15)
            sidebar_data = persistent_client.parse_village_page(resp.text, "dorf1")
            villages = sidebar_data.get("villages", [])

            self.socketio.emit('villages_discovered', {'username': username, 'villages': villages})

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
            
            log.info(f"Starting dedicated training agent for {username}")
            training_agent = TrainingModule(account_info, TravianClient)
            self.running_training_agents[username] = training_agent
            training_agent.start()

            log.info(f"Starting dedicated smithy agent for {username}")
            smithy_agent = SmithyModule(account_info, TravianClient)
            self.running_smithy_agents[username] = smithy_agent
            smithy_agent.start()
            
            log.info(f"Starting dedicated demolish agent for {username}")
            demolish_agent = DemolishModule(account_info, TravianClient)
            self.running_demolish_agents[username] = demolish_agent
            demolish_agent.start()

            self.running_account_agents[username] = []
            for village in villages:
                log.info(f"Creating new agent for village {village['name']} under account {username}")
                agent = VillageAgent(account_info, village, self.socketio)
                self.running_account_agents[username].append(agent)
                agent.start()

        except Exception as exc:
            log.error(f"Failed to start village agents for {username}: {exc}", exc_info=True)
            # Clean up if startup fails midway
            self.running_account_clients.pop(username, None)
            self.running_account_agents.pop(username, None)