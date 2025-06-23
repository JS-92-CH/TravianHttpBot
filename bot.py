import time
import threading
from typing import Optional

from client import TravianClient
from dashboard import socketio
from config import log, BOT_STATE, state_lock, save_config, load_default_build_queue

class BotThread(threading.Thread):
    def __init__(self):
        super().__init__()
        self.daemon = True
        self.stop_event = threading.Event()

    def stop(self):
        self.stop_event.set()

    def run(self):
        log.info("Bot thread started.")
        socketio.emit('log_message', {'data': 'Bot thread started.'})
        
        while not self.stop_event.is_set():
            with state_lock:
                accounts = BOT_STATE["accounts"][:]

            if not accounts:
                log.info("No accounts configured. Waiting...")
                time.sleep(15)
                continue

            for account in accounts:
                if self.stop_event.is_set(): break
                
                client = TravianClient(account["username"], account["password"], account["server_url"])
                if not client.login():
                    time.sleep(10)
                    continue

                try:
                    dorf1_resp = client.sess.get(f"{client.server_url}/dorf1.php", timeout=15)
                    dorf1_resp.raise_for_status()
                    sidebar_data = client.parse_village_page(dorf1_resp.text)
                    villages = sidebar_data.get("villages", [])
                except Exception as exc:
                    log.error("[%s] Could not get village list: %s", client.username, exc)
                    continue

                if not villages:
                    log.warning("[%s] No villages found for account.", client.username)
                    continue

                with state_lock:
                    BOT_STATE["village_data"][client.username] = villages

                for village in villages:
                    if self.stop_event.is_set(): break
                        
                    village_id = village['id']
                    full_village_data = client.fetch_and_parse_village(village_id)
                    
                    if full_village_data:
                        with state_lock:
                            BOT_STATE["village_data"][str(village_id)] = full_village_data
                            if str(village_id) not in BOT_STATE["build_queues"]:
                                log.info("No build queue for village %s, creating default.", village_id)
                                BOT_STATE["build_queues"][str(village_id)] = load_default_build_queue()
                                save_config()
                        socketio.emit("state_update", BOT_STATE)

                    time.sleep(5)

            log.info("Finished checking all accounts. Waiting for 60 seconds.")
            self.stop_event.wait(60)
            
        log.info("Bot thread stopped.")
        socketio.emit('log_message', {'data': 'Bot thread stopped.'})