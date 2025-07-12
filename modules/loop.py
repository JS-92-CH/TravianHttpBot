# modules/loop.py
import time
import math
import re
import random
from .base import BaseModule
from config import log, BOT_STATE, state_lock, save_config, gid_name

class Module(BaseModule):
    """
    Handles the entire lifecycle of settling a new village, building it up,
    and then destroying it to start over.
    """

    def __init__(self, agent):
        super().__init__(agent)

    def _get_loop_state(self, village_id):
        """Safely gets the state for a village, initializing if not present."""
        with state_lock:
            if "loop_module_state" not in BOT_STATE:
                BOT_STATE["loop_module_state"] = {}
            if str(village_id) not in BOT_STATE["loop_module_state"]:
                BOT_STATE["loop_module_state"][str(village_id)] = {
                    "enabled": False,
                    "status": "idle",
                    "target_coords": None,
                    "settle_start_time": None,
                    "settle_travel_time": 0,
                    "new_village_id": None,
                    "catapult_origin_village": None
                }
            return BOT_STATE["loop_module_state"][str(village_id)]

    def tick(self, village_data):
        agent = self.agent
        village_id = agent.village_id
        loop_state = self._get_loop_state(village_id)

        if not loop_state.get("enabled"):
            return

        log.info(f"[{agent.village_name}] Loop module active. Current status: {loop_state['status']}")

        if loop_state["status"] == "idle":
            self.start_settling_process(village_data, loop_state)
        elif loop_state["status"] == "training_settlers":
            self.check_settler_training(village_data, loop_state)
        elif loop_state["status"] == "finding_village":
            self.find_and_send_settlers(village_data, loop_state)
        elif loop_state["status"] == "settling":
            self.check_settlement_complete(village_data, loop_state)
        elif loop_state["status"] == "waiting_for_build_up":
            self.check_build_up_complete(village_data, loop_state)
        elif loop_state["status"] == "destroying":
            self.destroy_village(village_data, loop_state)
        elif loop_state["status"] == "waiting_for_destruction":
            self.check_destruction_complete(village_data, loop_state)


    def start_settling_process(self, village_data, loop_state):
        """Starts the process by training settlers."""
        agent = self.agent
        log.info(f"[{agent.village_name}] Starting loop: Training settlers.")
        
        # Determine which building to use for settlers
        settler_building_gid = None
        if any(b.get('gid') == 26 for b in village_data.get('buildings', [])):
            settler_building_gid = 26 # Palace
        elif any(b.get('gid') == 25 for b in village_data.get('buildings', [])):
            settler_building_gid = 25 # Residence
        elif any(b.get('gid') == 44 for b in village_data.get('buildings', [])):
            settler_building_gid = 44 # Command Center
        else:
            log.error(f"[{agent.village_name}] No Residence, Palace, or Command Center found to train settlers.")
            loop_state["status"] = "idle" # Reset
            return

        success = agent.client.train_settlers(agent.village_id, settler_building_gid, 3)
        if success:
            loop_state["status"] = "training_settlers"
            log.info(f"[{agent.village_name}] Settler training initiated.")
        else:
            log.error(f"[{agent.village_name}] Failed to initiate settler training. Retrying next cycle.")
        save_config()

    def check_settler_training(self, village_data, loop_state):
        """Waits for settlers to be trained."""
        agent = self.agent
        # This is a simplified check. A more robust implementation would parse the training queue.
        # For now, we assume if we are in this state, we are waiting, then proceed.
        log.info(f"[{agent.village_name}] Waiting for settlers to finish training...")
        loop_state["status"] = "finding_village"
        save_config()


    def find_and_send_settlers(self, village_data, loop_state):
        """Finds the closest empty village and sends settlers."""
        agent = self.agent
        log.info(f"[{agent.village_name}] Searching for a nearby empty village...")
        
        current_coords = village_data.get('coords')
        if not current_coords:
            log.error(f"[{agent.village_name}] Cannot find coordinates for current village. Aborting loop.")
            loop_state["status"] = "idle"
            return
            
        # Fetch map data around the current village
        map_data = agent.client.get_map_data(current_coords['x'], current_coords['y'])
        if not map_data or not map_data.get('tiles'):
            log.error(f"[{agent.village_name}] Failed to fetch map data.")
            return

        empty_villages = []
        for tile in map_data['tiles']:
            # k.vt indicates an empty, settable valley
            if 'k.vt' in tile.get('title', ''):
                pos = tile['position']
                distance = math.sqrt((current_coords['x'] - pos['x'])**2 + (current_coords['y'] - pos['y'])**2)
                empty_villages.append({'x': pos['x'], 'y': pos['y'], 'distance': distance})
        
        if not empty_villages:
            log.warning(f"[{agent.village_name}] No empty villages found nearby. Will try again later.")
            return

        # Sort by distance to find the closest
        closest_village = min(empty_villages, key=lambda v: v['distance'])
        target_coords = {'x': closest_village['x'], 'y': closest_village['y']}
        log.info(f"[{agent.village_name}] Closest empty village found at ({target_coords['x']}|{target_coords['y']}).")

        success, travel_time = agent.client.send_settlers(agent.village_id, target_coords)

        if success:
            loop_state["status"] = "settling"
            loop_state["target_coords"] = target_coords
            loop_state["settle_start_time"] = time.time()
            loop_state["settle_travel_time"] = travel_time
            log.info(f"[{agent.village_name}] Settlers sent. Travel time: {travel_time}s.")
        else:
            log.error(f"[{agent.village_name}] Failed to send settlers.")
            loop_state["status"] = "idle" # Reset
        save_config()
        
    def check_settlement_complete(self, village_data, loop_state):
        """Checks if the new village appears in the sidebar."""
        agent = self.agent
        
        elapsed_time = time.time() - loop_state.get("settle_start_time", 0)
        if elapsed_time < loop_state.get("settle_travel_time", float('inf')):
            # Not yet time, just wait
            return

        log.info(f"[{agent.village_name}] Settler arrival time has passed. Checking for new village...")
        # Fetch fresh list of all villages for the account
        all_villages = agent.client.get_all_villages()
        
        new_village = None
        # Find a village that wasn't there before
        with state_lock:
            current_known_villages = BOT_STATE['village_data'][agent.client.username]
            known_ids = {v['id'] for v in current_known_villages}
            
            for v in all_villages:
                if v['id'] not in known_ids:
                    new_village = v
                    break

        if new_village:
            log.info(f"[{agent.village_name}] SUCCESS! New village '{new_village['name']}' ({new_village['id']}) has been settled.")
            loop_state["status"] = "waiting_for_build_up"
            loop_state["new_village_id"] = new_village['id']
            
            # This is the key part: trigger the special agent via the bot manager
            agent.socketio.emit('start_special_agent', {
                'account_username': agent.client.username,
                'village_id': new_village['id']
            })
        else:
            log.error(f"[{agent.village_name}] Settlement failed. New village not found. Restarting loop.")
            loop_state["status"] = "idle"
            
        save_config()
        
    def check_build_up_complete(self, village_data, loop_state):
        """Checks if the special agent for the new village is finished."""
        agent = self.agent
        new_village_id = loop_state.get("new_village_id")
        
        # The special agent should signal its completion by clearing its own build queue
        with state_lock:
            new_village_queue = BOT_STATE.get("build_queues", {}).get(str(new_village_id), [])
            
        if not new_village_queue:
            log.info(f"[{agent.village_name}] Build-up of village {new_village_id} is complete. Starting destruction phase.")
            loop_state["status"] = "destroying"
        
        save_config()

    def destroy_village(self, village_data, loop_state):
        """Initiates the catapult waves to destroy the newly built village."""
        agent = self.agent
        new_village_id = loop_state.get("new_village_id")
        origin_village_id = loop_state.get("catapult_origin_village") or agent.village_id
        
        with state_lock:
            # Re-fetch all village data to ensure we have the latest info for the new village
            all_player_villages = BOT_STATE.get("village_data", {}).get(agent.client.username, [])
            target_village_details = next((v for v in all_player_villages if v.get('id') == new_village_id), None)

        if not target_village_details:
             # If not in the main list, fetch its details directly
             target_village_details = agent.client.fetch_and_parse_village(new_village_id)

        if not target_village_details or 'coords' not in target_village_details:
            log.error(f"[{agent.village_name}] Cannot find coordinates for target village {new_village_id}. Aborting destruction.")
            loop_state["status"] = "idle"
            return

        target_coords = target_village_details['coords']
        
        # Assuming Teuton catapults (t8). Adjust if needed.
        attack_troops = {'t8': 100}

        log.info(f"[{agent.village_name}] Sending 17 catapult waves from village {origin_village_id} to {new_village_id} at ({target_coords['x']}|{target_coords['y']}).")
        
        success = agent.client.send_catapult_waves(
            from_village_id=int(origin_village_id),
            target_x=target_coords['x'],
            target_y=target_coords['y'],
            troops=attack_troops,
            waves=17
        )

        if success:
            log.info(f"[{agent.village_name}] All catapult waves sent. Now waiting for destruction.")
            loop_state["status"] = "waiting_for_destruction"
        else:
            log.error(f"[{agent.village_name}] Failed to send catapult waves. Resetting loop.")
            loop_state["status"] = "idle"
            
        save_config()


    def check_destruction_complete(self, village_data, loop_state):
        """Checks if the settled village has been destroyed by seeing if it's gone from the village list."""
        agent = self.agent
        new_village_id = loop_state.get("new_village_id")

        if not new_village_id:
            log.info(f"[{agent.village_name}] No new village ID in state. Resetting loop to idle.")
            loop_state["status"] = "idle"
            save_config()
            return

        log.info(f"[{agent.village_name}] Checking if village {new_village_id} has been destroyed...")
        
        all_villages = agent.client.get_all_villages()
        village_exists = any(v['id'] == new_village_id for v in all_villages)

        if not village_exists:
            log.info(f"[{agent.village_name}] SUCCESS! Village {new_village_id} has been destroyed. Restarting loop.")
            loop_state["status"] = "idle"
            loop_state["new_village_id"] = None
            loop_state["target_coords"] = None
        else:
            log.info(f"[{agent.village_name}] Village {new_village_id} still exists. Will check again on the next cycle.")

        save_config()