import re
import time
from bs4 import BeautifulSoup
from .base import BaseModule
from config import log, BOT_STATE, state_lock

# GIDs for buildings that can train troops, mapped to their names
TRAINING_BUILDING_GIDS = {
    19: "Barracks",
    20: "Stable",
    21: "Workshop",
    29: "Great Barracks",
    30: "Great Stable",
}

class Module(BaseModule):
    """
    Manages fetching troop information from training buildings and executing
    the training strategy defined in the main configuration.
    """

    def tick(self, village_data):
        agent = self.agent
        village_id_str = str(agent.village_id)

        try:
            # 1. Get the user-defined training strategy for this village
            with state_lock:
                training_strategy = BOT_STATE.get("training_queues", {}).get(village_id_str, {})

            # 2. Find all existing training buildings in the current village
            existing_training_buildings = {
                b.get('gid'): b for b in village_data.get("buildings", [])
                if b.get('gid') in TRAINING_BUILDING_GIDS and b.get('id') is not None
            }

            # This will hold all parsed data for the dashboard
            all_parsed_data = {}

            # 3. Iterate through ALL possible training building types
            for gid, building_type_name in TRAINING_BUILDING_GIDS.items():
                building = existing_training_buildings.get(gid)
                
                # If the building exists in the village, parse its data
                if building:
                    html = agent.client.fetch_building_page(agent.village_id, building['id'])
                    if not html:
                        log.warning(f"[{agent.village_name}] Could not fetch page for {building_type_name} (ID: {building['id']}).")
                        continue
                    
                    trainable_units, current_queue = self.parse_training_page(html)
                    all_parsed_data[building_type_name] = {
                        'trainable': trainable_units,
                        'queue': current_queue
                    }
                    
                    # --- TRAINING LOGIC ---
                    self.execute_training_for_building(
                        agent, training_strategy, building, building_type_name, trainable_units, current_queue
                    )
                else:
                    # **CRUCIAL FIX**: If building doesn't exist, create a placeholder
                    # This ensures the dashboard doesn't break when looking for this building type.
                    all_parsed_data[building_type_name] = {
                        'trainable': [],
                        'queue': []
                    }

            # 4. Finally, update the global state for the dashboard with all collected data
            with state_lock:
                if "training_data" not in BOT_STATE:
                    BOT_STATE["training_data"] = {}
                BOT_STATE["training_data"][village_id_str] = all_parsed_data
        
        except Exception as e:
            log.error(f"[{agent.village_name}] A critical error occurred in the training module tick: {e}", exc_info=True)


    def execute_training_for_building(self, agent, strategy, building, building_name, trainable, queue):
        """Contains the logic to queue troops for a single building."""
        goal = strategy.get(building_name)
        if not goal or not goal.get("troop_name") or not goal.get("queue_duration_minutes"):
            return

        goal_troop_name = goal["troop_name"]
        goal_duration_minutes = int(goal["queue_duration_minutes"])

        target_unit = next((u for u in trainable if u['name'] == goal_troop_name), None)
        if not target_unit:
            return

        total_queue_seconds = sum(item.get('duration_seconds', 0) for item in queue)
        if total_queue_seconds >= goal_duration_minutes * 60:
            return

        seconds_to_fill = (goal_duration_minutes * 60) - total_queue_seconds
        amount_to_train = int(seconds_to_fill / target_unit['time_per_unit']) if target_unit['time_per_unit'] > 0 else 0
        
        if amount_to_train <= 0: return

        max_possible = target_unit.get('max_trainable', 0)
        if amount_to_train > max_possible:
            amount_to_train = max_possible
        
        if amount_to_train <= 0: return
        
        log.info(f"[{agent.village_name}] Training {amount_to_train} x '{goal_troop_name}' in {building_name}.")
        troop_payload = {target_unit['input_name']: amount_to_train}
        if agent.client.initiate_training(agent.village_id, building['id'], troop_payload):
            time.sleep(3)


    def parse_training_page(self, html: str):
        soup = BeautifulSoup(html, 'html.parser')
        trainable_units = []
        training_queue = []

        for action_div in soup.select('.buildActionOverview.trainUnits > .action'):
            try:
                details_div = action_div.find('div', class_='details')
                if not details_div: continue

                name = details_div.select_one('.tit > a:nth-of-type(2)').text.strip()
                input_field = details_div.find('input', type='text')
                if not (input_field and input_field.has_attr('name')): continue
                
                max_amount_link = details_div.select_one('.cta > a')
                max_trainable = 0
                if max_amount_link and max_amount_link.has_attr('onclick'):
                    max_amount_match = re.search(r"val\('?([\d,]+)'?\)", max_amount_link['onclick'])
                    if max_amount_match:
                        max_trainable = int(max_amount_match.group(1).replace(',', ''))

                duration_text = details_div.select_one('.inlineIcon.duration .value').text
                time_match = re.search(r'(\d{2}):(\d{2}):(\d{2})', duration_text)
                time_per_unit = int(time_match.group(1))*3600 + int(time_match.group(2))*60 + int(time_match.group(3)) if time_match else 1
                
                trainable_units.append({
                    'name': name, 'input_name': input_field['name'],
                    'time_per_unit': time_per_unit, 'max_trainable': max_trainable,
                })
            except Exception:
                continue

        for row in soup.select('table.under_progress tbody tr'):
            try:
                desc_cell = row.find('td', class_='desc')
                if not desc_cell: continue
                
                name = desc_cell.find('img', class_='unit')['alt'].strip()
                amount_match = re.search(r'^([\d,]+)', desc_cell.get_text(strip=True))
                amount = int(amount_match.group(1).replace(',', '')) if amount_match else 0
                
                timer_span = row.select_one('.dur span.timer')
                duration_seconds = int(timer_span['value']) if timer_span and timer_span.has_attr('value') else 0

                training_queue.append({
                    'name': name, 'amount': amount,
                    'duration_seconds': duration_seconds,
                    'duration_str': time.strftime('%H:%M:%S', time.gmtime(duration_seconds)),
                })
            except Exception:
                continue
                
        return trainable_units, training_queue