# modules/training.py
from .base import BaseModule
from config import log, BOT_STATE, state_lock, save_config, gid_name
import re

class Module(BaseModule):
    """Handles automatic troop training."""
    
    def __init__(self, agent):
        super().__init__(agent)
        self.training_gids = [19, 20, 21, 29, 30]

    def get_trainable_troops(self, soup):
        """Parses the training page to find trainable troops."""
        trainable = []
        # Find all the sections for individual troops
        for action_div in soup.select('div.action'):
            # The troop name is in the second link inside the 'tit' div
            name_tag = action_div.select_one('div.tit a:nth-of-type(2)')
            if not name_tag or not name_tag.text:
                continue
            unit_name = name_tag.text.strip()

            # The troop ID (e.g., u1, u2) is in the class of the image tag
            img_tag = action_div.select_one('div.tit img.unit')
            if not img_tag:
                continue
            
            # Extract the unit class like 'u1'
            unit_class = next((c for c in img_tag.get('class', []) if c.startswith('u') and c[1:].isdigit()), None)
            if not unit_class:
                continue
                
            trainable.append({
                'name': unit_name,
                'id': unit_class
            })
            
        return trainable

    def tick(self, village_data):
        agent = self.agent
        village_id = agent.village_id
        
        with state_lock:
            if str(village_id) not in BOT_STATE['training_data']:
                BOT_STATE['training_data'][str(village_id)] = {}
        
        training_buildings = [b for b in village_data.get("buildings", []) if b.get('gid') in self.training_gids]

        for building in training_buildings:
            gid = building.get('gid')
            building_name = f"{gid_name(gid)} ({building.get('id')})"
            
            soup = agent.client.fetch_training_page(village_id, gid)
            if not soup:
                continue
            
            trainable_troops = self.get_trainable_troops(soup)
            with state_lock:
                BOT_STATE['training_data'][str(village_id)][building_name] = {'trainable': trainable_troops}

            # Check if there are any troops to train
            with state_lock:
                training_queues = BOT_STATE.get('training_queues', {}).get(str(village_id), {})
                
            if building_name not in training_queues:
                continue
            
            troop_to_train = training_queues[building_name].get('troop_name')
            queue_duration = training_queues[building_name].get('queue_duration_minutes', 0)
            
            if not troop_to_train or not queue_duration:
                continue

            # Get current training queue duration
            in_training_table = soup.find('table', class_='under_progress')
            current_duration = 0
            if in_training_table:
                timers = in_training_table.select('.timer')
                if timers:
                    current_duration = max(int(t.get('value', 0)) for t in timers)
            
            if current_duration < queue_duration * 60:
                # Calculate how many troops to train
                troop_info = next((t for t in trainable_troops if t['name'] == troop_to_train), None)
                if not troop_info:
                    continue
                
                duration_per_troop_tag = soup.find('img', class_=f"unit {troop_info['id']}").find_parent('div', class_='tit').find_next_sibling('div', class_='inlineIcon.duration')
                if not duration_per_troop_tag:
                    continue
                
                duration_text = duration_per_troop_tag.select_one('.value').text
                h, m, s = map(int, duration_text.split('+')[0].strip().split(':'))
                duration_per_troop = h * 3600 + m * 60 + s
                
                if duration_per_troop == 0:
                    continue
                
                needed_duration = (queue_duration * 60) - current_duration
                amount_to_train = int(needed_duration / duration_per_troop)
                
                if amount_to_train > 0:
                    log.info(f"[{agent.village_name}] Training {amount_to_train} of {troop_to_train} in {building_name}")
                    agent.client.train_troops(building.get('id'), {troop_info['id']: amount_to_train})