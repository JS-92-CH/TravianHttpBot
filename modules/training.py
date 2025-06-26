import re
from bs4 import BeautifulSoup
from .base import BaseModule
from config import log, BOT_STATE, state_lock

# GIDs for buildings that can train troops
TRAINING_BUILDING_GIDS = [19, 20, 21, 29, 30]  # Barracks, Stable, Workshop, Great Barracks, Great Stable

class Module(BaseModule):
    """Fetches troop training queue information from a village."""

    def tick(self, village_data):
        """
        This method is called periodically for each village. 
        It finds all training buildings and aggregates their training queues.
        """
        agent = self.agent
        village_id = str(agent.village_id)

        # This will hold the combined training queue from all buildings in the village
        combined_training_queue = []

        # Find all existing training buildings in the current village
        existing_training_buildings = [
            b for b in village_data.get("buildings", [])
            if b.get('gid') in TRAINING_BUILDING_GIDS and b.get('id') is not None
        ]

        # If there are no training buildings, there's nothing to do
        if not existing_training_buildings:
            with state_lock:
                # Ensure the state is clean for this village
                if "training_data" not in BOT_STATE:
                    BOT_STATE["training_data"] = {}
                BOT_STATE["training_data"][village_id] = {'training_queue': []}
            return

        # Fetch the page for each training building and parse its queue
        for building in existing_training_buildings:
            try:
                html = agent.client.fetch_building_page(agent.village_id, building['id'])
                if html:
                    parsed_queue = self.parse_training_queue(html)
                    combined_training_queue.extend(parsed_queue)
            except Exception as e:
                log.error(f"AGENT({agent.village_name}): Failed to parse training data for GID {building['gid']}: {e}")

        # Update the global state with the combined queue for the village
        with state_lock:
            if "training_data" not in BOT_STATE:
                BOT_STATE["training_data"] = {}
            BOT_STATE["training_data"][village_id] = {'training_queue': combined_training_queue}

    def parse_training_queue(self, html: str):
        """
        Parses the 'in training' table from a building's HTML content to extract
        the name, amount, and duration of each training batch.
        """
        soup = BeautifulSoup(html, 'html.parser')
        training_queue = []
        
        # The training queue is within a table with the class 'under_progress'
        progress_table = soup.find('table', class_='under_progress')
        if not progress_table:
            return training_queue

        # Each `tr` with a `td.desc` represents one batch of troops in training
        for row in progress_table.select('tbody tr'):
            desc_cell = row.find('td', class_='desc')
            if not desc_cell:
                continue

            try:
                # The troop name is in the 'alt' attribute of the image tag
                name = desc_cell.find('img', class_='unit')['alt'].strip()
                
                # The text of the cell contains the amount, e.g., "316,414 Slave Militia"
                # We extract the number from the beginning of the string.
                text_content = desc_cell.get_text(separator=' ', strip=True)
                amount_match = re.search(r'^([\d,]+)', text_content)
                
                if not amount_match:
                    continue # Skip if no amount is found
                    
                amount_str = amount_match.group(1)
                amount = int(amount_str.replace(',', ''))

                # The duration is in a specific cell with a timer span
                duration_cell = row.find('td', class_='dur')
                duration = duration_cell.find('span', class_='timer').text.strip() if duration_cell else 'N/A'
                
                training_queue.append({
                    'name': name,
                    'amount': amount,
                    'duration': duration,
                })
            except (AttributeError, TypeError, ValueError) as e:
                log.debug(f"Could not parse a training queue item from row. Details: {e}")
                
        return training_queue