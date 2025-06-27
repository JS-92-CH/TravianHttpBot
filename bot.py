import time
import threading
from typing import Dict, Optional
from modules.adventure import Module as AdventureModule
from client import TravianClient
from config import log, BOT_STATE, state_lock, save_config
from modules import load_modules

class VillageAgent(threading.Thread):
    def __init__(self, client: TravianClient, village_info: Dict, socketio_instance, use_dual_queue: bool = False, use_hero_resources: bool = False):
        super().__init__()
        self.client = client
        self.village_id = village_info['id']
        self.village_name = village_info['name']
        self.socketio = socketio_instance
        self.use_dual_queue = use_dual_queue
        self.use_hero_resources = use_hero_resources
        self.stop_event = threading.Event()
        self.daemon = True
        self.modules = load_modules(self)
        self.building_module = next((m for m in self.modules if type(m).__name__ == 'Module' and 'building' in type(m).__module__), None)
        self.next_check_time = time.time()

    def stop(self):
        self.stop_event.set()

    def run(self):
        log.info(f"Agent started for village: {self.village_name} ({self.village_id})")
        
        while not self.stop_event.is_set():
            try:
                if time.time() < self.next_check_time:
                    self.stop_event.wait(1)
                    continue

                log.info(f"[{self.village_name}] Refreshing village data...")
                village_data = self.client.fetch_and_parse_village(self.village_id)
                if not village_data:
                    log.warning(f"[{self.village_name}] Failed to fetch main data. Retrying in 5 minutes.")
                    self.next_check_time = time.time() + 10
                    continue

                with state_lock:
                    BOT_STATE["village_data"][str(self.village_id)] = village_data
                self.socketio.emit("state_update", BOT_STATE)

                if self.building_module:
                    max_queue_length = 2 if self.use_dual_queue else 1
                    # Loop to try and fill the build queue
                    while True:
                        current_village_data = self.client.fetch_and_parse_village(self.village_id)
                        if not current_village_data:
                            log.warning(f"[{self.village_name}] Failed to refresh data before building. Aborting build checks for this cycle.")
                            break
                        
                        active_builds = current_village_data.get("queue", [])
                        if len(active_builds) >= max_queue_length:
                            log.info(f"[{self.village_name}] Build queue is full ({len(active_builds)}/{max_queue_length}).")
                            break # Build queue is full

                        # Try to queue one item
                        build_eta = self.building_module.tick(current_village_data)
                        
                        if build_eta <= 0:
                            # The building module decided not to or failed to build.
                            log.info(f"[{self.village_name}] No further build tasks could be queued.")
                            break
                        
                        log.info(f"[{self.village_name}] Successfully queued a build. Next check in {build_eta + 1:.0f} seconds. Checking for another available slot...")
                        self.stop_event.wait(1) # Wait a few seconds for server state to update before trying again

                # After attempting to fill the queue, set the next check time based on the final state.
                final_village_data = self.client.fetch_and_parse_village(self.village_id)
                if final_village_data:
                    active_builds = final_village_data.get("queue", [])
                    if active_builds:
                        server_eta = min([b.get('eta', 300) for b in active_builds])
                        self.next_check_time = time.time() + server_eta + 1
                        log.info(f"[{self.village_name}] Construction is active. Next check in {server_eta + 1:.0f} seconds.")
                    else:
                        self.next_check_time = time.time() + 3
                        log.info(f"[{self.village_name}] No construction queued. Checking for new tasks in 3 seconds.")
                else:
                    self.next_check_time = time.time() + 5
                
            except Exception as e:
                log.error(f"Agent for village {self.village_name} encountered a CRITICAL ERROR: {e}", exc_info=True)
                self.next_check_time = time.time() + 10

        log.info(f"Agent stopped for village: {self.village_name} ({self.village_id})")


class BotManager(threading.Thread):
    def __init__(self, socketio_instance):
        super().__init__()
        self.socketio = socketio_instance
        self.stop_event = threading.Event()
        self.village_agents: Dict[int, VillageAgent] = {}
        self.adventure_module = AdventureModule(self)
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
                client = TravianClient(
                    account["username"],
                    account["password"],
                    account["server_url"],
                    account.get("proxy")
                )
                if not client.login(): self.stop_event.wait(10); continue
                self.adventure_module.tick(client)
                time.sleep(2)
                
                try:
                    resp = client.sess.get(f"{client.server_url}/dorf1.php", timeout=15)
                    sidebar_data = client.parse_village_page(resp.text, "dorf1")
                    villages = sidebar_data.get("villages", [])
                    with state_lock: BOT_STATE["village_data"][client.username] = villages
                    
                    current_ids = {v['id'] for v in villages}
                    for vid in list(self.village_agents.keys()):
                        if vid not in current_ids:
                            self.village_agents.pop(vid).stop()
                            log.info(f"Stopped and removed agent for stale village ID: {vid}")

                    use_dual_queue = account.get("use_dual_queue", False)
                    use_hero_resources = account.get("use_hero_resources", False)
                    for village in villages:
                        agent = self.village_agents.get(village['id'])
                        if agent:
                            agent.use_dual_queue = use_dual_queue
                            agent.use_hero_resources = use_hero_resources
                        else:
                            log.info(f"Creating new agent for {village['name']}")
                            agent = VillageAgent(client, village, self.socketio, use_dual_queue, use_hero_resources)
                            self.village_agents[village['id']] = agent
                            agent.start()

                except Exception as exc:
                    log.error(f"Failed to manage agents for {account['username']}: {exc}", exc_info=True)
            
            self.stop_event.wait(30)
        log.info("Bot Manager stopped.")