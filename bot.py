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

    def stop(self):
        self.stop_event.set()

    def run(self):
        log.info(f"Agent started for village: {self.village_name} ({self.village_id})")
        
        # --- MODIFIED LOGIC ---
        while not self.stop_event.is_set():
            try:
                # 1. Fetch core village data (dorf1 & dorf2)
                log.info(f"[{self.village_name}] Refreshing village data...")
                village_data = self.client.fetch_and_parse_village(self.village_id)
                if not village_data:
                    log.warning(f"[{self.village_name}] Failed to fetch main data. Retrying in 5 minutes.")
                    self.stop_event.wait(300)
                    continue

                # 2. Update the global state and notify the dashboard
                with state_lock:
                    BOT_STATE["village_data"][str(self.village_id)] = village_data
                self.socketio.emit("state_update", BOT_STATE)

                # 3. Run all modules (building, tasks, training, etc.)
                # Each module will now execute its logic based on the freshly fetched data.
                for module in self.modules:
                    if self.stop_event.is_set():
                        break
                    module.tick(village_data)
                
                # 4. Wait for the next cycle
                log.info(f"[{self.village_name}] Cycle complete. Next refresh in 5 minutes.")
                self.stop_event.wait(300) # Wait for 5 minutes (300 seconds)

            except Exception as e:
                log.error(f"Agent for village {self.village_name} encountered a CRITICAL ERROR: {e}", exc_info=True)
                # Wait longer after a critical error to avoid spamming logs
                self.stop_event.wait(600)

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
                if not client.login(): self.stop_event.wait(60); continue
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
                            # Update settings on existing agent if they changed
                            agent.use_dual_queue = use_dual_queue
                            agent.use_hero_resources = use_hero_resources
                        else:
                            # Create a new agent if it doesn't exist
                            log.info(f"Creating new agent for {village['name']}")
                            agent = VillageAgent(client, village, self.socketio, use_dual_queue, use_hero_resources)
                            self.village_agents[village['id']] = agent
                            agent.start()

                except Exception as exc:
                    log.error(f"Failed to manage agents for {account['username']}: {exc}", exc_info=True)
            
            # The BotManager will check for new accounts/villages every 30 seconds.
            # The individual village agents will manage their own 5-minute data refresh cycle.
            self.stop_event.wait(30)
        log.info("Bot Manager stopped.")