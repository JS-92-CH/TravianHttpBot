# modules/marketplace.py

from .base import BaseModule
from config import log, BOT_STATE, state_lock

class Module(BaseModule):
    """
    Handles sending resources between villages based on configuration.
    This module provides the core function but requires trigger logic to be implemented
    based on specific botting needs (e.g., balancing resources, feeding a specific village).
    """

    def tick(self, village_data):
        """
        The main logic loop for the marketplace module.
        This function will check for conditions to send resources.

        The trigger logic is currently a placeholder. You would replace the
        'if False:' condition with your actual balancing or sending logic.
        """
        agent = self.agent
        village_id = agent.village_id
        village_name = agent.village_name

        # --- Example Trigger Logic ---
        # This is where you would implement your custom conditions for sending resources.
        if False: # Replace with your actual condition
            resources_to_send = {
                'lumber': 10000,
                'clay': 10000,
                'iron': 10000,
                'crop': 5000
            }
            target_village_name = "B" # The name of the village to send to
            runs = 1 # How many times the merchants should make the trip

            # --- Logic to find target coordinates ---
            target_village_info = None
            with state_lock:
                # Search through all villages of the current user to find the target by name
                all_player_villages = BOT_STATE.get("village_data", {}).get(agent.client.username, [])
                target_village_info = next((v for v in all_player_villages if v.get('name') == target_village_name), None)

            if not target_village_info or 'id' not in target_village_info:
                log.warning(f"[{agent.client.username}] Could not find target village '{target_village_name}' to send resources to.")
                return

            # Fetch the detailed data for the target village to get its coordinates
            with state_lock:
                target_village_details = BOT_STATE.get("village_data", {}).get(str(target_village_info['id']))

            if not target_village_details or 'coords' not in target_village_details:
                log.warning(f"[{agent.client.username}] Missing coordinate data for target village '{target_village_name}'. It may need to be visited by an agent first.")
                return
            
            target_coords = target_village_details['coords']
            # --- End of coordinate logic ---

            log.info(f"[{agent.client.username}] Triggering resource send from {village_name} to {target_village_name} at ({target_coords['x']}|{target_coords['y']}).")
            
            # Call the new function in the client with coordinates
            success = agent.client.send_resources(
                from_village_id=village_id,
                target_x=target_coords['x'],
                target_y=target_coords['y'],
                resources=resources_to_send,
                runs=runs
            )

            if success:
                log.info(f"[{agent.client.username}] Successfully initiated resource transfer.")
            else:
                log.error(f"[{agent.client.username}] Failed to initiate resource transfer.")

        pass
