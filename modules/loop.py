# js-92-ch/travianhttpbot/TravianHttpBot-loop/modules/loop.py
import time
import math
import re
import random
import threading
from .base import BaseModule
from config import log, BOT_STATE, state_lock, save_config, gid_name

class Module(threading.Thread):
    """
    An independent agent thread that handles the entire lifecycle of settling 
    new villages, building them up, and then destroying them to start over for a single account.
    """

    def __init__(self, account_info, client_class, socketio_instance):
        super().__init__()
        self.stop_event = threading.Event()
        self.daemon = True
        self.account_info = account_info
        self.client_class = client_class
        self.socketio = socketio_instance

    def stop(self):
        self.stop_event.set()

    def _get_loop_state(self, village_id):
        """Safely gets the state for a village, initializing if not present."""
        with state_lock:
            if "loop_module_state" not in BOT_STATE:
                BOT_STATE["loop_module_state"] = {}
            if "reserved_settle_targets" not in BOT_STATE:
                BOT_STATE["reserved_settle_targets"] = []
            # Check for the top-level village config and the settlement_slots list
            if str(village_id) not in BOT_STATE["loop_module_state"] or "settlement_slots" not in BOT_STATE["loop_module_state"][str(village_id)]:
                BOT_STATE["loop_module_state"][str(village_id)] = {
                    "enabled": False,
                    "catapult_origin_village": None,
                    "settlement_slots": []
                }
            return BOT_STATE["loop_module_state"][str(village_id)]

    def run(self):
        username = self.account_info['username']
        log.info(f"[LoopAgent][{username}] Thread started.")
        self.stop_event.wait(45) # Initial delay to let other things settle

        client = self.client_class(
            username,
            self.account_info['password'],
            self.account_info['server_url'],
            self.account_info.get('proxy')
        )

        if not client.login():
            log.error(f"[LoopAgent][{username}] Initial login failed. Agent will stop.")
            return

        while not self.stop_event.is_set():
            try:
                with state_lock:
                    all_villages_for_user = BOT_STATE.get("village_data", {}).get(username, [])
                
                # Find all villages for this account that have the loop enabled.
                villages_with_loop_enabled = []
                for v_summary in all_villages_for_user:
                    v_id_str = str(v_summary['id'])
                    with state_lock:
                        loop_config = BOT_STATE.get("loop_module_state", {}).get(v_id_str, {})
                    if loop_config.get("enabled"):
                        villages_with_loop_enabled.append(v_summary)
                
                if not villages_with_loop_enabled:
                    self.stop_event.wait(60) # Wait for a minute if no villages are configured.
                    continue

                for village_info in villages_with_loop_enabled:
                    village_id = village_info['id']
                    village_name = village_info['name']
                    
                    log.info(f"[LoopAgent][{username}] Checking loop status for village: {village_name}")
                    
                    # Fetch the latest full data for this specific village
                    village_data = client.fetch_and_parse_village(village_id)
                    if not village_data:
                        log.warning(f"[LoopAgent][{username}] Could not fetch data for {village_name}. Skipping this cycle.")
                        continue

                    loop_state = self._get_loop_state(village_id)
                    
                    self.manage_settlement_slots(client, village_data, loop_state, village_name)

                    # Check if any slot is currently training settlers. If so, don't start a new training.
                    is_training_settlers = any(s.get("status") == "training_settlers" for s in loop_state.get("settlement_slots", []))

                    for i, slot_state in enumerate(loop_state.get("settlement_slots", [])):
                        log.info(f"[{village_name}] Loop slot {i+1} active. Current status: {slot_state.get('status', 'unknown')}")

                        status = slot_state.get("status")
                        if status == "idle":
                            if not is_training_settlers:
                                if self.start_settling_process(client, village_id, village_data, slot_state):
                                    if slot_state["status"] == "training_settlers":
                                        break
                                    if slot_state["status"] == "finding_village":
                                        self.find_and_send_settlers(client, village_id, village_data, slot_state)
                                        break 
                        elif status == "training_settlers":
                            self.check_settler_training(client, village_id, village_data, slot_state)
                        elif status == "finding_village":
                            self.find_and_send_settlers(client, village_id, village_data, slot_state)
                            if slot_state.get("status") == "settling":
                                break
                        elif status == "settling":
                            self.check_settlement_complete(client, slot_state)
                        elif status == "waiting_for_build_up":
                            self.check_build_up_complete(client, slot_state)
                        elif status == "destroying":
                            self.destroy_village(client, village_data, slot_state, loop_state)
                        elif status == "waiting_for_destruction":
                            self.check_destruction_complete(client, slot_state)

                    self.stop_event.wait(2) # Small delay between checking each enabled village

            except Exception as e:
                log.error(f"[LoopAgent][{username}] CRITICAL ERROR in main loop: {e}", exc_info=True)
            
            self.stop_event.wait(20) # Wait before the next full cycle for the account

    def manage_settlement_slots(self, client, village_data, loop_state, village_name):
        max_slots = 0
        buildings = village_data.get('buildings', [])
        # Simplified check for Palace (GID 26) or Residence (GID 25)
        palace = next((b for b in buildings if b.get('gid') == 26), None)
        residence = next((b for b in buildings if b.get('gid') == 25), None)

        if palace and palace.get('level', 0) >= 10:
             max_slots = (palace.get('level', 0) // 10) * 1 + (1 if palace.get('level',0) >= 15 else 0) + (1 if palace.get('level',0) >= 20 else 0)
        elif residence and residence.get('level', 0) >= 10:
             max_slots = 1 if residence.get('level',0) < 20 else 2
        
        loop_state["settlement_slots"] = [s for s in loop_state["settlement_slots"] if s.get("status") != "idle" and s.get("status") is not None]
        
        active_slots = len(loop_state["settlement_slots"])
        if max_slots > active_slots:
            needed_slots = max_slots - active_slots
            log.info(f"[{village_name}] Found {needed_slots} available expansion slot(s). Adding to queue.")
            for _ in range(needed_slots):
                loop_state["settlement_slots"].append({"status": "idle"})
            save_config()

    def start_settling_process(self, client, village_id, village_data, slot_state) -> bool:
        village_name = village_data.get('name', 'Unknown Village') # Add this line
        log.info(f"[{village_name}] Attempting to start settlement process for an idle slot.")
        # --- END OF FIX ---
        home_troops = client.get_home_troops(village_id)
        available_settlers = home_troops.get('Settler', 0)

        if available_settlers >= 3:
            log.info(f"[{village_name}] Found {available_settlers} available settlers. Skipping training.")
            slot_state["status"] = "finding_village"
            save_config()
            return True

        needed_settlers = 3 - available_settlers
        log.info(f"[{village_name}] Not enough settlers available ({available_settlers}/3). Training {needed_settlers} more.")
        
        settler_building_gid = 26 if any(b.get('gid') == 26 for b in village_data.get('buildings', [])) else 25
        
        success, duration = client.train_settlers(village_id, settler_building_gid, needed_settlers)
        if success:
            slot_state["status"] = "training_settlers"
            slot_state["settler_training_end_time"] = time.time() + duration
            log.info(f"[{village_name}] Training for {needed_settlers} settlers initiated. Will be ready in {duration} seconds.")
            save_config()
            return True
        else:
            log.error(f"[{village_name}] Failed to initiate settler training. Slot will remain idle.")
            return False

    def check_settler_training(self, client, village_id, village_data, slot_state):
        end_time = slot_state.get("settler_training_end_time")
        if end_time and time.time() > end_time:
            village_name = village_data.get('name', 'Unknown Village') # Add this line
            log.info(f"[{village_name}] Settlers have finished training for a slot. Immediately proceeding to send them.")
            # --- END OF FIX ---
            slot_state["status"] = "finding_village"
            slot_state["settler_training_end_time"] = None
            save_config()
            
            # Re-fetch data to be absolutely sure we have the latest info
            fresh_village_data = client.fetch_and_parse_village(village_id)
            if fresh_village_data:
                self.find_and_send_settlers(client, village_id, fresh_village_data, slot_state)
            else:
                log.warning(f"[{village_name}] Could not fetch village data to send settlers. Will retry on the next tick.")

    def find_and_send_settlers(self, client, village_id, village_data, slot_state):
        village_name = village_data.get('name', 'Unknown Village') # Add this line
        log.info(f"[{village_name}] Searching for a nearby empty village for a slot...")
        current_coords = village_data.get('coords')
        if not current_coords:
            log.error(f"[{village_name}] Cannot find coordinates. Aborting.")
            slot_state["status"] = "idle"
            save_config()
            return
            
        map_data = client.get_map_data(current_coords['x'], current_coords['y'])
        if not map_data or not map_data.get('tiles'):
            log.error(f"[{village_name}] Failed to fetch map data.")
            return

        empty_villages = []
        for tile in map_data['tiles']:
            if 'k.vt' in tile.get('title', ''):
                pos = tile['position']
                dist = math.sqrt((current_coords['x'] - pos['x'])**2 + (current_coords['y'] - pos['y'])**2)
                empty_villages.append({'x': pos['x'], 'y': pos['y'], 'distance': dist})
        
        if not empty_villages:
            log.warning(f"[{village_name}] No empty villages found nearby.")
            return

        empty_villages.sort(key=lambda v: v['distance'])

        # --- START OF CHANGE: Find an unreserved target ---
        target_village = None
        with state_lock:
            # Check the global list of reserved targets
            reserved_targets = BOT_STATE.get("reserved_settle_targets", [])
            for village in empty_villages:
                coord_tuple = (village['x'], village['y'])
                if coord_tuple not in reserved_targets:
                    target_village = village
                    # Immediately add the target to the reserved list to prevent other threads from picking it
                    BOT_STATE.setdefault("reserved_settle_targets", []).append(coord_tuple)
                    log.info(f"[{village_name}] Reserving target {coord_tuple}.")
                    break
        # --- END OF CHANGE ---

        if not target_village:
            log.warning(f"[{village_name}] No *unreserved* empty villages found nearby.")
            return

        target_coords = {'x': target_village['x'], 'y': target_village['y']}
        
        success, travel_time = client.send_settlers(village_id, target_coords)

        if success:
            slot_state["status"] = "settling"
            slot_state["target_coords"] = target_coords
            slot_state["settle_start_time"] = time.time()
            slot_state["settle_travel_time"] = travel_time
            log.info(f"[{village_name}] Settlers sent to ({target_coords['x']}|{target_coords['y']}). Travel time: {travel_time}s.")
        else:
            # If sending fails, we must un-reserve the target so it can be tried again later.
            slot_state["status"] = "idle"
            # --- START OF CHANGE: Un-reserve the target on failure ---
            with state_lock:
                coord_tuple = (target_coords['x'], target_coords['y'])
                if coord_tuple in BOT_STATE.get("reserved_settle_targets", []):
                    BOT_STATE["reserved_settle_targets"].remove(coord_tuple)
                    log.info(f"[{village_name}] Un-reserving target {coord_tuple} due to send failure.")
            # --- END OF CHANGE ---
        save_config()

    def check_settlement_complete(self, client, slot_state):
        elapsed_time = time.time() - slot_state.get("settle_start_time", 0)
        if elapsed_time < slot_state.get("settle_travel_time", float('inf')):
            # It's not time yet, the settlers are still traveling.
            return

        log.info(f"[LoopAgent] Settler arrival time has passed. Checking for new village...")
        
        target_coords = slot_state.get("target_coords")
        if not target_coords:
            log.error(f"[LoopAgent] No target coordinates in slot state. Resetting slot.")
            slot_state["status"] = "idle"
            save_config()
            return

        # --- START OF REVISED, MORE RELIABLE DETECTION LOGIC ---
        new_village_id = None
        # Get the absolute latest village list directly from the server
        all_villages_on_server = client.get_all_villages()

        # Instead of checking for "new" villages, we check ALL villages for a coordinate match.
        # This is immune to the race condition.
        for village_summary in all_villages_on_server:
            # We must fetch the village details to get its coordinates.
            details = client.fetch_and_parse_village(village_summary['id'])
            if details and details.get('coords') == target_coords:
                new_village_id = village_summary['id']
                log.info(f"[LoopAgent] Found village {new_village_id} at target coordinates ({target_coords['x']}|{target_coords['y']}).")
                break # Found it.
        # --- END OF REVISED LOGIC ---

        coord_tuple = (target_coords['x'], target_coords['y'])

        if new_village_id:
            log.info(f"[LoopAgent] SUCCESS! New village '{new_village_id}' has been settled.")
            
            # This correctly saves the new village's ID to the slot state,
            # permanently linking this loop slot to the new village.
            slot_state["new_village_id"] = new_village_id
            slot_state["status"] = "waiting_for_build_up"
            
            # Trigger the special agent to start the initial build-up.
            self.socketio.emit('start_special_agent', {
                'account_username': client.username,
                'village_id': new_village_id
            })
        else:
            log.error(f"[LoopAgent] Settlement failed. New village not found at target coordinates. Resetting slot.")
            slot_state["status"] = "idle"

        # Whether successful or not, we are done with this settlement attempt,
        # so un-reserve the target coordinates.
        with state_lock:
            if coord_tuple and coord_tuple in BOT_STATE.get("reserved_settle_targets", []):
                BOT_STATE["reserved_settle_targets"].remove(coord_tuple)
                log.info(f"[LoopAgent] Un-reserving target {coord_tuple} after settlement check.")
            
        save_config()

    def check_build_up_complete(self, client, slot_state):
        new_village_id = slot_state.get("new_village_id")
        
        with state_lock:
            new_village_queue = BOT_STATE.get("build_queues", {}).get(str(new_village_id), [])
            
        if not new_village_queue:
            log.info(f"[LoopAgent] Build-up of village {new_village_id} is complete. Starting destruction.")
            slot_state["status"] = "destroying"
        
        save_config()

    def destroy_village(self, client, village_data, slot_state, loop_state):
        new_village_id = slot_state.get("new_village_id")
        origin_village_id = loop_state.get("catapult_origin_village") or village_data['id']
        
        target_village_details = client.fetch_and_parse_village(new_village_id)

        if not target_village_details or 'coords' not in target_village_details:
            log.error(f"[LoopAgent] Cannot find coordinates for target village {new_village_id}. Aborting.")
            slot_state["status"] = "idle"
            save_config()
            return

        target_coords = target_village_details['coords']
        attack_troops = {'t8': 100} # Catapults

        log.info(f"[LoopAgent] Sending 17 catapult waves from village {origin_village_id} to destroy {new_village_id}.")
        
        success = client.send_catapult_waves(
            from_village_id=int(origin_village_id),
            target_x=target_coords['x'],
            target_y=target_coords['y'],
            troops=attack_troops,
            waves=17
        )

        if success:
            log.info(f"[LoopAgent] All catapult waves sent. Waiting for destruction.")
            slot_state["status"] = "waiting_for_destruction"
        else:
            log.error(f"[LoopAgent] Failed to send catapult waves. Resetting loop slot.")
            slot_state["status"] = "idle"
            
        save_config()

    def check_destruction_complete(self, client, slot_state):
        new_village_id = slot_state.get("new_village_id")
        if not new_village_id:
            slot_state["status"] = "idle"
            save_config()
            return

        log.info(f"[LoopAgent] Checking if village {new_village_id} has been destroyed...")
        
        all_villages = client.get_all_villages()
        village_exists = any(v['id'] == new_village_id for v in all_villages)

        if not village_exists:
            log.info(f"[LoopAgent] SUCCESS! Village {new_village_id} has been destroyed. Slot is now free.")
            slot_state["status"] = "idle"
        else:
            log.info(f"[LoopAgent] Village {new_village_id} still exists. Will check again on the next cycle.")

        save_config()