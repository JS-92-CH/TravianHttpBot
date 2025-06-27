# bot.py

import time
import threading
from typing import Dict, Optional
from modules.adventure import Module as AdventureModule
from modules.hero import Module as HeroModule # Import the hero module
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

                log.info(f"[{self.village_name}] Refreshing village data...")
                village_data = self.client.fetch_and_parse_village(self.village_id)

                if not village_data or not village_data.get("buildings"):
                    log.warning(f"[{self.village_name}] Data fetch was incomplete. Retrying in 60 seconds.")
                    self.next_check_time = time.time() + 60
                    continue

                with state_lock:
                    BOT_STATE["village_data"][str(self.village_id)] = village_data
                self.socketio.emit("state_update", BOT_STATE)

                # Run village-specific modules
                for module in self.modules:
                    if module == self.building_module:
                        continue
                    try:
                        module.tick(village_data)
                    except Exception as e:
                        log.error(f"[{self.village_name}] Error in module {type(module).__name__}: {e}", exc_info=True)
                
                # Aggressive building loop
                if self.building_module:
                    while not self.stop_event.is_set():
                        current_village_data = self.client.fetch_and_parse_village(self.village_id)
                        if not current_village_data:
                            log.warning(f"[{self.village_name}] Could not fetch village data for building loop. Waiting a moment.")
                            time.sleep(15)
                            continue

                        max_queue_length = 2 if self.use_dual_queue else 1
                        current_queue_length = len(current_village_data.get("queue", []))

                        if current_queue_length >= max_queue_length:
                            log.info(f"[{self.village_name}] Server build queue is full ({current_queue_length}/{max_queue_length}).")
                            break

                        log.info(f"[{self.village_name}] Queue has open slot ({current_queue_length}/{max_queue_length}). Attempting to build.")
                        build_eta = self.building_module.tick(current_village_data)

                        if build_eta <= 0:
                            log.info(f"[{self.village_name}] No more buildings to queue or an error occurred. Exiting build loop.")
                            break
                        
                        time.sleep(0.25)

                # Schedule next check
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
        self.village_agents: Dict[int, VillageAgent] = {}
        self.adventure_module = AdventureModule(self)
        self.hero_module = HeroModule(self)
        self.daemon = True

    def stop(self):
        log.info("Stopping all village agents...")
        for agent in self.village_agents.values(): agent.stop()
        for agent in self.village_agents.values(): agent.join()
        self.stop_event.set()
        log.info("All agents stopped.")

    def run(self):
        log.info("Bot Manager started.")
        while not self.stop_event.is_set():
            with state_lock: accounts = BOT_STATE["accounts"][:]
            if not accounts: self.stop_event.wait(15); continue

            for account in accounts:
                if self.stop_event.is_set(): break

                temp_client = TravianClient(
                    account["username"],
                    account["password"],
                    account["server_url"],
                    account.get("proxy")
                )

                if not temp_client.login():
                    self.stop_event.wait(60)
                    continue
                
                # --- Start of Changes ---
                # Run account-level modules here, passing the client
                self.adventure_module.tick(temp_client)
                time.sleep(1) 
                self.hero_module.tick(temp_client)
                time.sleep(1)
                # --- End of Changes ---

                try:
                    resp = temp_client.sess.get(f"{temp_client.server_url}/dorf1.php", timeout=15)
                    sidebar_data = temp_client.parse_village_page(resp.text, "dorf1")
                    villages = sidebar_data.get("villages", [])
                    with state_lock: BOT_STATE["village_data"][account['username']] = villages

                    current_ids = {v['id'] for v in villages}
                    for vid in list(self.village_agents.keys()):
                        if vid not in current_ids:
                            self.village_agents.pop(vid).stop()
                            log.info(f"Stopped and removed agent for stale village ID: {vid}")

                    for village in villages:
                        if village['id'] not in self.village_agents:
                            log.info(f"Creating new agent for {village['name']}")
                            agent = VillageAgent(account, village, self.socketio)
                            self.village_agents[village['id']] = agent
                            agent.start()
                        else:
                            existing_agent = self.village_agents[village['id']]
                            existing_agent.tribe = account.get("tribe", "roman")
                            existing_agent.building_logic = account.get("building_logic", "default")
                            existing_agent.use_dual_queue = account.get("use_dual_queue", False)
                            existing_agent.use_hero_resources = account.get("use_hero_resources", False)

                except Exception as exc:
                    log.error(f"Failed to manage agents for {account['username']}: {exc}", exc_info=True)

            self.stop_event.wait(30)
        log.info("Bot Manager stopped.")