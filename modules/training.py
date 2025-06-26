# In modules/training.py

import re
import time
from bs4 import BeautifulSoup
from .base import BaseModule
from config import log, BOT_STATE, state_lock, GID_MAPPING

# GIDs for buildings that can train troops
TRAINING_BUILDING_GIDS = {
    19: "Barracks",
    20: "Stable",
    21: "Workshop",
    29: "Great Barracks",
    30: "Great Stable",
}

class Module(BaseModule):
    """
    Manages fetching troop information and executing training queues based on user settings.
    """

    def tick(self, village_data):
        agent = self.agent
        village_id_str = str(agent.village_id)

        # 1. Get user-defined training settings for this village
        with state_lock:
            training_settings = BOT_STATE.get("training_queues", {}).get(village_id_str, {})

        if not training_settings:
            return # No training configured for this village

        # 2. Find all existing training buildings in the current village
        existing_training_buildings = [
            b for b in village_data.get("buildings", [])
            if b.get('gid') in TRAINING_BUILDING_GIDS and b.get('id') is not None
        ]
        
        # 3. Process each training building
        for building in existing_training_buildings:
            building_type_name = TRAINING_BUILDING_GIDS.get(building['gid'])
            if not building_type_name or building_type_name not in training_settings:
                continue # No settings for this specific building type

            try:
                # Fetch fresh page data for the building
                html = agent.client.fetch_building_page(agent.village_id, building['id'])
                if not html:
                    continue
                
                # Parse available troops and current queue from the page
                trainable_units, current_queue = self.parse_training_page(html)

                # Update the global state with the parsed info for the dashboard
                with state_lock:
                    if "training_data" not in BOT_STATE:
                        BOT_STATE["training_data"] = {}
                    if village_id_str not in BOT_STATE["training_data"]:
                        BOT_STATE["training_data"][village_id_str] = {}
                    
                    BOT_STATE["training_data"][village_id_str][building_type_name] = {
                        'trainable': trainable_units,
                        'queue': current_queue
                    }
                
                # Get the user's goal for this building
                goal = training_settings[building_type_name]
                goal_troop_name = goal.get("troop_name")
                goal_duration_minutes = goal.get("queue_duration_minutes", 0)

                if not goal_troop_name or goal_duration_minutes <= 0:
                    continue
                
                # Check if the troop we want to train is actually available
                target_unit = next((u for u in trainable_units if u['name'] == goal_troop_name), None)
                if not target_unit:
                    log.warning(f"[{agent.village_name}] Configured troop '{goal_troop_name}' not found in {building_type_name}.")
                    continue

                # Calculate current queue duration
                total_queue_seconds = sum(item.get('duration_seconds', 0) for item in current_queue)

                # If the queue is already long enough, do nothing
                if total_queue_seconds >= goal_duration_minutes * 60:
                    log.info(f"[{agent.village_name}] {building_type_name} queue is full enough ({total_queue_seconds}s). Skipping.")
                    continue

                # Calculate how many troops to train
                seconds_to_fill = (goal_duration_minutes * 60) - total_queue_seconds
                amount_to_train = int(seconds_to_fill / target_unit['time_per_unit'])
                
                if amount_to_train <= 0:
                    continue

                # Check if we can afford to train this many
                current_res, _ = agent.hero_module.fetch_current_resources(agent.village_id)
                
                can_afford = True
                for res, cost in target_unit['costs'].items():
                    if current_res.get(res, 0) < cost * amount_to_train:
                        # We can't afford the full amount, calculate max affordable
                        amount_to_train = min(amount_to_train, int(current_res.get(res, 0) / cost))

                if amount_to_train <= 0:
                    log.info(f"[{agent.village_name}] Not enough resources to train '{goal_troop_name}' in {building_type_name}.")
                    continue
                
                # We have a plan. Let's train!
                log.info(f"[{agent.village_name}] Training {amount_to_train} x '{goal_troop_name}' in {building_type_name}.")
                
                troop_payload = {target_unit['input_name']: amount_to_train}
                agent.client.initiate_training(agent.village_id, building['id'], troop_payload)
                
                # Wait a bit after submitting to avoid spamming
                time.sleep(5)


            except Exception as e:
                log.error(f"[{agent.village_name}] Failed to process training for {building_type_name}: {e}", exc_info=True)


    def parse_training_page(self, html: str):
        soup = BeautifulSoup(html, 'html.parser')
        trainable_units = []
        training_queue = []

        # Parse trainable units
        for action_div in soup.select('.buildActionOverview.trainUnits > .action'):
            try:
                name = action_div.select_one('.details .tit a:nth-of-type(2)').text.strip()
                input_field = action_div.find('input', {'type': 'text'})
                if not input_field or not input_field.has_attr('name'):
                    continue
                
                duration_text = action_div.select_one('.inlineIcon.duration .value').text
                time_match = re.search(r'(\d{2}):(\d{2}):(\d{2})', duration_text)
                time_per_unit = int(time_match.group(1)) * 3600 + int(time_match.group(2)) * 60 + int(time_match.group(3))

                costs = {}
                for res_div in action_div.select('.resourceWrapper .resource'):
                    res_class = res_div.find('i')['class'][0] # e.g., 'r1Big'
                    res_key = {'r1Big': 'lumber', 'r2Big': 'clay', 'r3Big': 'iron', 'r4Big': 'crop'}.get(res_class)
                    if res_key:
                        costs[res_key] = int(res_div.select_one('.value').text.strip())

                trainable_units.append({
                    'name': name,
                    'input_name': input_field['name'], # e.g. 't1', 't6'
                    'time_per_unit': time_per_unit,
                    'costs': costs
                })
            except Exception as e:
                log.debug(f"Could not parse a trainable unit. Details: {e}")

        # Parse "in training" queue
        for row in soup.select('table.under_progress tbody tr'):
            desc_cell = row.find('td', class_='desc')
            if not desc_cell:
                continue
            try:
                name = desc_cell.find('img', class_='unit')['alt'].strip()
                text_content = desc_cell.get_text(separator=' ', strip=True)
                amount_match = re.search(r'^([\d,]+)', text_content)
                amount = int(amount_match.group(1).replace(',', '')) if amount_match else 0
                
                timer_span = row.select_one('.dur span.timer')
                duration_seconds = int(timer_span['value']) if timer_span and timer_span.has_attr('value') else 0

                training_queue.append({
                    'name': name,
                    'amount': amount,
                    'duration_seconds': duration_seconds
                })
            except Exception as e:
                log.debug(f"Could not parse an item from the training queue. Details: {e}")
                
        return trainable_units, training_queue