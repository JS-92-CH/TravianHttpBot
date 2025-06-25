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
        log.info(f"Agent started for village: {self.village_name} ({self.village_id}) - Dual Queue: {self.use_dual_queue}")
        while not self.stop_event.is_set():
            try:
                village_data = self.client.fetch_and_parse_village(self.village_id)
                if not village_data:
                    self.stop_event.wait(300); continue

                with state_lock:
                    BOT_STATE["village_data"][str(self.village_id)] = village_data
                self.socketio.emit("state_update", BOT_STATE)

                for module in self.modules:
                    module.tick(village_data)
                    
            except Exception as e:
                log.error(f"Agent for village {self.village_name} CRITICAL ERROR: {e}", exc_info=True)
                self.stop_event.wait(300)

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
                        if agent := self.village_agents.get(village['id']):
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
