import re
import time
import json
from datetime import timedelta

from bs4 import BeautifulSoup

from .base import BaseModule
from config import log

class Module(BaseModule):
    """
    Handles automatically sending the hero on adventures.
    This module manages its own timing to avoid spamming requests.
    """
    def __init__(self, agent):
        super().__init__(agent)
        self.next_check_time = {}

    def tick(self, client):
        """
        Checks for available adventures and sends the hero on the shortest one,
        but only if the cooldown period has passed.
        """
        username = client.username
        
        # Check if it's time to run for this account
        if time.time() < self.next_check_time.get(username, 0):
            return

        try:
            dorf1_resp = client.sess.get(f"{client.server_url}/dorf1.php", timeout=15)
            soup = BeautifulSoup(dorf1_resp.text, 'html.parser')

            if not soup.select_one('.heroStatus i.heroHome'):
                # Hero is not home. We can't get the exact return time from this page,
                # so we'll just check back in a minute.
                self.next_check_time[username] = time.time() + 60
                return

            adventure_button = soup.select_one('a.adventure.attention .content')
            if not adventure_button or int(adventure_button.text.strip()) == 0:
                # No adventures available. Check again in 15 minutes.
                self.next_check_time[username] = time.time() + 900
                return

        except Exception as e:
            log.error(f"[{username}] Failed to check for adventures on dorf1: {e}")
            self.next_check_time[username] = time.time() + 300 # Retry in 5 minutes on error
            return

        log.info(f"[{username}] Hero is home and adventures are available. Checking adventure list...")

        try:
            adv_page_resp = client.sess.get(f"{client.server_url}/hero.php?t=3", timeout=15)
            adv_soup = BeautifulSoup(adv_page_resp.text, 'html.parser')
            
            script_tag = adv_soup.find("script", string=re.compile(r"window\.Travian\.React\.HeroAdventure\.render"))
            if not script_tag:
                log.warning(f"[{username}] Could not find the adventure data script on the page.")
                self.next_check_time[username] = time.time() + 300
                return
            
            script_content = script_tag.string
            view_data_start = script_content.find('viewData:')
            if view_data_start == -1:
                log.warning(f"[{username}] Could not find 'viewData:' in the script.")
                self.next_check_time[username] = time.time() + 300
                return

            # Find the opening brace of the viewData object
            json_start = script_content.find('{', view_data_start)
            if json_start == -1:
                log.warning(f"[{username}] Could not find opening brace for viewData JSON.")
                self.next_check_time[username] = time.time() + 300
                return
            
            # Find the matching closing brace
            open_braces = 0
            json_end = -1
            for i, char in enumerate(script_content[json_start:]):
                if char == '{':
                    open_braces += 1
                elif char == '}':
                    open_braces -= 1
                    if open_braces == 0:
                        json_end = json_start + i + 1
                        break
            
            if json_end == -1:
                log.warning(f"[{username}] Could not find matching closing brace for viewData JSON.")
                self.next_check_time[username] = time.time() + 300
                return

            json_str = script_content[json_start:json_end]
            adventure_data = json.loads(json_str)
            adventures_list = adventure_data.get("data", {}).get("ownPlayer", {}).get("hero", {}).get("adventures", [])

            if not adventures_list:
                log.info(f"[{username}] No adventures available at the moment.")
                self.next_check_time[username] = time.time() + 900
                return

            adventures = [
                {
                    'duration': adv['travelingDuration'],
                    'mapId': adv['mapId']
                }
                for adv in adventures_list
            ]

            shortest_adventure = min(adventures, key=lambda x: x['duration'])
            
            log.info(f"[{username}] Shortest adventure found (duration: {shortest_adventure['duration']}s, mapId: {shortest_adventure['mapId']}). Starting...")
            
            if client.start_adventure(shortest_adventure['mapId']):
                log.info(f"[{username}] Successfully sent hero on adventure.")
                cooldown = (shortest_adventure['duration'] * 2) + 10
                self.next_check_time[username] = time.time() + cooldown
                log.info(f"[{username}] Next adventure check scheduled in {cooldown:.0f} seconds.")
            else:
                log.warning(f"[{username}] Failed to send hero on adventure.")
                self.next_check_time[username] = time.time() + 60 # Retry in 1 minute

        except Exception as e:
            log.error(f"[{username}] An error occurred while starting an adventure: {e}", exc_info=True)
            self.next_check_time[username] = time.time() + 300