import time
import threading
import concurrent.futures
from typing import Optional, Dict

from client import TravianClient
from dashboard import socketio
from config import log, BOT_STATE, state_lock, save_config, load_default_build_queue

class VillageAgent(threading.Thread):
    """A dedicated agent that continuously monitors and manages a single village."""
    def __init__(self, client: TravianClient, village_info: Dict, socketio_instance):
        super().__init__()
        self.client = client
        self.village_id = village_info['id']
        self.village_name = village_info['name']
        self.socketio = socketio_instance
        self.stop_event = threading.Event()
        self.daemon = True

    def stop(self):
        self.stop_event.set()

    def run(self):
        log.info(f"Agent started for village: {self.village_name} ({self.village_id})")
        while not self.stop_event.is_set():
            try:
                village_data = self.client.fetch_and_parse_village(self.village_id)
                if village_data:
                    with state_lock:
                        BOT_STATE["village_data"][str(self.village_id)] = village_data
                        if str(self.village_id) not in BOT_STATE["build_queues"]:
                            log.info(f"No build queue for village {self.village_id}, creating default.")
                            BOT_STATE["build_queues"][str(self.village_id)] = load_default_build_queue()
                            save_config()
                    
                    self.socketio.emit("state_update", BOT_STATE)
                
                # Wait for the next cycle
                self.stop_event.wait(60) # Fetch data every 60 seconds
            except Exception as e:
                log.error(f"Agent for village {self.village_id} encountered an error: {e}")
                self.stop_event.wait(300) # Wait longer on error

        log.info(f"Agent stopped for village: {self.village_name} ({self.village_id})")


class BotManager(threading.Thread):
    """Manages all accounts and spawns VillageAgents for each village."""
    def __init__(self, socketio_instance):
        super().__init__()
        self.socketio = socketio_instance
        self.stop_event = threading.Event()
        self.village_agents: Dict[int, VillageAgent] = {}
        self.daemon = True

    def stop(self):
        log.info("Stopping all village agents...")
        for agent in self.village_agents.values():
            agent.stop()
        for agent in self.village_agents.values():
            agent.join()
        self.stop_event.set()
        log.info("All agents stopped.")

    def run(self):
        log.info("Bot Manager started.")
        self.socketio.emit('log_message', {'data': 'Bot Manager started.'})

        while not self.stop_event.is_set():
            with state_lock:
                accounts = BOT_STATE["accounts"][:]
            
            if not accounts:
                log.info("No accounts configured. Manager is idle.")
                self.stop_event.wait(15)
                continue

            for account in accounts:
                if self.stop_event.is_set(): break
                
                client = TravianClient(account["username"], account["password"], account["server_url"])
                if not client.login():
                    self.stop_event.wait(60) # Wait before retrying failed login
                    continue

                try:
                    dorf1_resp = client.sess.get(f"{client.server_url}/dorf1.php", timeout=15)
                    dorf1_resp.raise_for_status()
                    sidebar_data = client.parse_village_page(dorf1_resp.text)
                    villages = sidebar_data.get("villages", [])

                    with state_lock:
                        BOT_STATE["village_data"][client.username] = villages
                    
                    # Synchronize agents - start new ones, stop old ones
                    current_village_ids = {v['id'] for v in villages}
                    agents_to_stop = set(self.village_agents.keys()) - current_village_ids
                    
                    for village_id in agents_to_stop:
                        log.info(f"Stopping agent for removed/lost village {village_id}")
                        self.village_agents[village_id].stop()
                        del self.village_agents[village_id]
                        
                    for village in villages:
                        if village['id'] not in self.village_agents:
                            log.info(f"Spawning new agent for village {village['name']}")
                            agent = VillageAgent(client, village, self.socketio)
                            self.village_agents[village['id']] = agent
                            agent.start()

                except Exception as exc:
                    log.error(f"Failed to manage agents for account {account['username']}: {exc}")

            log.info("Account sync complete. Manager sleeping for 5 minutes.")
            self.stop_event.wait(300) # Re-check accounts every 5 minutes

        log.info("Bot Manager stopped.")
        self.socketio.emit('log_message', {'data': 'Bot Manager stopped.'})