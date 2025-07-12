# js-92-ch/travianhttpbot/TravianHttpBot-loop/modules/loop.py
import time
import math
import re
import random
from .base import BaseModule
from config import log, BOT_STATE, state_lock, save_config, gid_name

class Module(BaseModule):
    """
    Handles the entire lifecycle of settling new villages, building them up,
    and then destroying them to start over. Now supports multiple concurrent settlements.
    """

    def __init__(self, agent):
        super().__init__(agent)

    def _get_loop_state(self, village_id):
        """Safely gets the state for a village, initializing if not present."""
        with state_lock:
            if "loop_module_state" not in BOT_STATE:
                BOT_STATE["loop_module_state"] = {}
            if "reserved_settle_targets" not in BOT_STATE:
                BOT_STATE["reserved_settle_targets"] = []
            if str(village_id) not in BOT_STATE["loop_module_state"] or "settlement_slots" not in BOT_STATE["loop_module_state"][str(village_id)]:
                BOT_STATE["loop_module_state"][str(village_id)] = {
                    "enabled": False,
                    "catapult_origin_village": None,
                    "settlement_slots": []
                }
            return BOT_STATE["loop_module_state"][str(village_id)]

    def tick(self, village_data):
        agent = self.agent
        village_id = agent.village_id
        loop_state = self._get_loop_state(village_id)

        if not loop_state.get("enabled"):
            return

        self.manage_settlement_slots(village_data, loop_state)

        # Check if any slot is currently training settlers. If so, don't start a new training.
        is_training_settlers = any(s.get("status") == "training_settlers" for s in loop_state.get("settlement_slots", []))

        for i, slot_state in enumerate(loop_state.get("settlement_slots", [])):
            log.info(f"[{agent.village_name}] Loop slot {i+1} active. Current status: {slot_state.get('status', 'unknown')}")

            status = slot_state.get("status")
            if status == "idle":
                if not is_training_settlers:
                    if self.start_settling_process(agent, village_data, slot_state):
                        # If we just started training, break the loop to not start another one in the same tick.
                        if slot_state["status"] == "training_settlers":
                            break
                        if slot_state["status"] == "finding_village":
                            self.find_and_send_settlers(agent, village_data, slot_state)
                            break # also break after sending settlers
            elif status == "training_settlers":
                self.check_settler_training(agent, slot_state)
            elif status == "finding_village":
                self.find_and_send_settlers(agent, village_data, slot_state)
                # break after sending settlers to not send for another slot in the same tick
                if slot_state.get("status") == "settling":
                    break
            elif status == "settling":
                self.check_settlement_complete(agent, slot_state)
            elif status == "waiting_for_build_up":
                self.check_build_up_complete(agent, slot_state)
            elif status == "destroying":
                self.destroy_village(agent, village_data, slot_state, loop_state)
            elif status == "waiting_for_destruction":
                self.check_destruction_complete(agent, slot_state)

    def manage_settlement_slots(self, village_data, loop_state):
        agent = self.agent
        max_slots = 0
        buildings = village_data.get('buildings', [])
        if any(b.get('gid') == 26 and b.get('level', 0) >= 10 for b in buildings):
             max_slots = 3
        elif any(b.get('gid') == 44 and b.get('level', 0) >= 10 for b in buildings):
             max_slots = 3
        elif any(b.get('gid') == 25 and b.get('level', 0) >= 10 for b in buildings):
             max_slots = 2
        
        loop_state["settlement_slots"] = [s for s in loop_state["settlement_slots"] if s.get("status") != "idle" and s.get("status") is not None]
        
        active_slots = len(loop_state["settlement_slots"])
        if max_slots > active_slots:
            needed_slots = max_slots - active_slots
            log.info(f"[{agent.village_name}] Found {needed_slots} available expansion slot(s). Adding to queue.")
            for _ in range(needed_slots):
                loop_state["settlement_slots"].append({"status": "idle"})
            save_config()

    def start_settling_process(self, agent, village_data, slot_state) -> bool:
        log.info(f"[{agent.village_name}] Attempting to start settlement process for an idle slot.")
        home_troops = agent.client.get_home_troops(agent.village_id)
        available_settlers = home_troops.get('Settler', 0)

        if available_settlers >= 3:
            log.info(f"[{agent.village_name}] Found {available_settlers} available settlers. Skipping training.")
            slot_state["status"] = "finding_village"
            save_config()
            return True

        needed_settlers = 3 - available_settlers
        log.info(f"[{agent.village_name}] Not enough settlers available ({available_settlers}/3). Training {needed_settlers} more.")
        
        settler_building_gid = 26 if any(b.get('gid') == 26 for b in village_data.get('buildings', [])) else 25
        
        success, duration = agent.client.train_settlers(agent.village_id, settler_building_gid, needed_settlers)
        if success:
            slot_state["status"] = "training_settlers"
            slot_state["settler_training_end_time"] = time.time() + duration
            log.info(f"[{agent.village_name}] Training for {needed_settlers} settlers initiated. Will be ready in {duration} seconds.")
            save_config()
            return True
        else:
            log.error(f"[{agent.village_name}] Failed to initiate settler training. Slot will remain idle.")
            return False

    def check_settler_training(self, agent, slot_state):
        end_time = slot_state.get("settler_training_end_time")
        if end_time and time.time() > end_time:
            log.info(f"[{agent.village_name}] Settlers have finished training for a slot. Immediately proceeding to send them.")
            slot_state["status"] = "finding_village"
            slot_state["settler_training_end_time"] = None
            save_config()
            
            village_data = agent.client.fetch_and_parse_village(agent.village_id)
            if village_data:
                self.find_and_send_settlers(agent, village_data, slot_state)
            else:
                log.warning(f"[{agent.village_name}] Could not fetch village data to send settlers. Will retry on the next tick.")

    def find_and_send_settlers(self, agent, village_data, slot_state):
        log.info(f"[{agent.village_name}] Searching for a nearby empty village for a slot...")
        current_coords = village_data.get('coords')
        if not current_coords:
            log.error(f"[{agent.village_name}] Cannot find coordinates. Aborting.")
            slot_state["status"] = "idle"
            save_config()
            return
            
        map_data = agent.client.get_map_data(current_coords['x'], current_coords['y'])
        if not map_data or not map_data.get('tiles'):
            log.error(f"[{agent.village_name}] Failed to fetch map data.")
            return

        empty_villages = []
        for tile in map_data['tiles']:
            if 'k.vt' in tile.get('title', ''):
                pos = tile['position']
                dist = math.sqrt((current_coords['x'] - pos['x'])**2 + (current_coords['y'] - pos['y'])**2)
                empty_villages.append({'x': pos['x'], 'y': pos['y'], 'distance': dist})
        
        if not empty_villages:
            log.warning(f"[{agent.village_name}] No empty villages found nearby.")
            return

        empty_villages.sort(key=lambda v: v['distance'])

        # --- START OF CHANGE: Find an unreserved target ---
        target_village = None
        with state_lock:
            for village in empty_villages:
                coord_tuple = (village['x'], village['y'])
                if coord_tuple not in BOT_STATE["reserved_settle_targets"]:
                    target_village = village
                    BOT_STATE["reserved_settle_targets"].append(coord_tuple)
                    log.info(f"[{agent.village_name}] Reserving target {coord_tuple}.")
                    break
        # --- END OF CHANGE ---

        if not target_village:
            log.warning(f"[{agent.village_name}] No *unreserved* empty villages found nearby.")
            return

        target_coords = {'x': target_village['x'], 'y': target_village['y']}
        
        success, travel_time = agent.client.send_settlers(agent.village_id, target_coords)

        if success:
            slot_state["status"] = "settling"
            slot_state["target_coords"] = target_coords
            slot_state["settle_start_time"] = time.time()
            slot_state["settle_travel_time"] = travel_time
            log.info(f"[{agent.village_name}] Settlers sent to ({target_coords['x']}|{target_coords['y']}). Travel time: {travel_time}s.")
        else:
            slot_state["status"] = "idle"
            # --- START OF CHANGE: Un-reserve the target on failure ---
            with state_lock:
                coord_tuple = (target_coords['x'], target_coords['y'])
                if coord_tuple in BOT_STATE["reserved_settle_targets"]:
                    BOT_STATE["reserved_settle_targets"].remove(coord_tuple)
                    log.info(f"[{agent.village_name}] Un-reserving target {coord_tuple} due to send failure.")
            # --- END OF CHANGE ---
        save_config()

    def check_settlement_complete(self, agent, slot_state):
        elapsed_time = time.time() - slot_state.get("settle_start_time", 0)
        if elapsed_time < slot_state.get("settle_travel_time", float('inf')):
            return

        log.info(f"[{agent.village_name}] Settler arrival time has passed. Checking for new village...")
        
        target_coords = slot_state.get("target_coords")
        if not target_coords:
            log.error(f"[{agent.village_name}] No target coordinates in slot state. Resetting slot.")
            slot_state["status"] = "idle"
            save_config()
            return

        # --- START OF MORE RELIABLE NEW VILLAGE DETECTION ---
        new_village_id = None
        all_villages = agent.client.get_all_villages()
        with state_lock:
            # Get a set of all known village IDs for the current user
            current_known_villages = BOT_STATE.get("village_data", {}).get(agent.client.username, [])
            known_ids = {v['id'] for v in current_known_villages}

        # Find any village that is not in our known list
        for v in all_villages:
            if v['id'] not in known_ids:
                # To be sure, let's check its coordinates
                details = agent.client.fetch_and_parse_village(v['id'])
                if details and details.get('coords') == target_coords:
                    new_village_id = v['id']
                    break
        # --- END OF DETECTION ---

        coord_tuple = (target_coords['x'], target_coords['y'])

        if new_village_id:
            log.info(f"[{agent.village_name}] SUCCESS! New village '{new_village_id}' has been settled.")
            
            # --- START OF STATE PERSISTENCE FIX ---
            # Save the new village ID directly into the slot state for persistence
            slot_state["new_village_id"] = new_village_id
            slot_state["status"] = "waiting_for_build_up"
            # --- END OF STATE PERSISTENCE FIX ---
            
            # Trigger the special agent to start the build-up
            agent.socketio.emit('start_special_agent', {
                'account_username': agent.client.username,
                'village_id': new_village_id
            })
        else:
            log.error(f"[{agent.village_name}] Settlement failed. New village not found. Resetting slot.")
            slot_state["status"] = "idle"

        # Un-reserve the target coordinates
        with state_lock:
            if coord_tuple and coord_tuple in BOT_STATE.get("reserved_settle_targets", []):
                BOT_STATE["reserved_settle_targets"].remove(coord_tuple)
                log.info(f"[{agent.village_name}] Un-reserving target {coord_tuple} after settlement check.")
            
        save_config()

    def check_build_up_complete(self, agent, slot_state):
        new_village_id = slot_state.get("new_village_id")
        
        with state_lock:
            new_village_queue = BOT_STATE.get("build_queues", {}).get(str(new_village_id), [])
            
        if not new_village_queue:
            log.info(f"[{agent.village_name}] Build-up of village {new_village_id} is complete. Starting destruction.")
            slot_state["status"] = "destroying"
        
        save_config()

    def destroy_village(self, agent, village_data, slot_state, loop_state):
        new_village_id = slot_state.get("new_village_id")
        origin_village_id = loop_state.get("catapult_origin_village") or agent.village_id
        
        target_village_details = agent.client.fetch_and_parse_village(new_village_id)

        if not target_village_details or 'coords' not in target_village_details:
            log.error(f"[{agent.village_name}] Cannot find coordinates for target village {new_village_id}. Aborting.")
            slot_state["status"] = "idle"
            save_config()
            return

        target_coords = target_village_details['coords']
        attack_troops = {'t8': 100}

        log.info(f"[{agent.village_name}] Sending 17 catapult waves from village {origin_village_id} to destroy {new_village_id}.")
        
        success = agent.client.send_catapult_waves(
            from_village_id=int(origin_village_id),
            target_x=target_coords['x'],
            target_y=target_coords['y'],
            troops=attack_troops,
            waves=17
        )

        if success:
            log.info(f"[{agent.village_name}] All catapult waves sent. Waiting for destruction.")
            slot_state["status"] = "waiting_for_destruction"
        else:
            log.error(f"[{agent.village_name}] Failed to send catapult waves. Resetting loop slot.")
            slot_state["status"] = "idle"
            
        save_config()

    def check_destruction_complete(self, agent, slot_state):
        new_village_id = slot_state.get("new_village_id")
        if not new_village_id:
            slot_state["status"] = "idle"
            save_config()
            return

        log.info(f"[{agent.village_name}] Checking if village {new_village_id} has been destroyed...")
        
        all_villages = agent.client.get_all_villages()
        village_exists = any(v['id'] == new_village_id for v in all_villages)

        if not village_exists:
            log.info(f"[{agent.village_name}] SUCCESS! Village {new_village_id} has been destroyed. Slot is now free.")
            slot_state["status"] = "idle"
        else:
            log.info(f"[{agent.village_name}] Village {new_village_id} still exists. Will check again on the next cycle.")

        save_config()