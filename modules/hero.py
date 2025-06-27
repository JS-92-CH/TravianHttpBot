# modules/hero.py

import re
import time
import json
from bs4 import BeautifulSoup
from .base import BaseModule
from config import log, BOT_STATE, state_lock, save_config

class Module(BaseModule):
    """
    Handles hero attributes, including automatic point distribution.
    """

    def tick(self, client):
        """
        Checks for hero level ups and distributes points.
        """
        username = client.username
        account_config = next((acc for acc in BOT_STATE.get("accounts", []) if acc['username'] == username), None)

        if not account_config or not account_config.get("hero_settings", {}).get("auto_distribute_points"):
            return

        json_str = "" # Initialize for error logging
        try:
            hero_page_resp = client.sess.get(f"{client.server_url}/hero/attributes", timeout=15)
            soup = BeautifulSoup(hero_page_resp.text, 'html.parser')

            if not soup.select_one('i.levelUp.show'):
                return

            script_tag = soup.find("script", string=re.compile(r"window\.Travian\.React\.HeroV2\.render"))
            if not script_tag:
                log.warning(f"[{username}] Could not find hero data script on attributes page.")
                return

            script_content = script_tag.string
            
            screen_data_start = script_content.find('screenData:')
            if screen_data_start == -1:
                log.warning(f"[{username}] Could not find 'screenData:' in hero script.")
                return

            json_start = script_content.find('{', screen_data_start)
            if json_start == -1:
                log.warning(f"[{username}] Could not find opening brace for screenData JSON.")
                return
            
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
                log.warning(f"[{username}] Could not find matching closing brace for screenData JSON.")
                return

            json_str = script_content[json_start:json_end]
            screen_data = json.loads(json_str)
            
            free_points = screen_data.get("hero", {}).get("freePoints", 0)

            if free_points > 0:
                log.info(f"[{username}] Hero has {free_points} attribute points to distribute.")
                self.distribute_points(client, screen_data, free_points, account_config["hero_settings"])

        except json.JSONDecodeError as e:
            log.error(f"[{username}] Failed to decode hero JSON: {e}. Raw string causing error (first 200 chars): {json_str[:200]}")
        except Exception as e:
            log.error(f"[{username}] An error occurred while checking hero attributes: {e}", exc_info=True)


    def distribute_points(self, client, screen_data, free_points, hero_settings):
        """
        Distributes the available hero points based on the user's configuration.
        """
        username = client.username
        distribution = hero_settings.get("point_distribution", {})
        
        if free_points < 4:
            log.info(f"[{username}] Not enough points to distribute ({free_points}/4). Waiting for next level.")
            return

        points_to_distribute = 4 
        
        if sum(distribution.values()) != points_to_distribute:
            log.warning(f"[{username}] Hero point distribution in config does not sum to 4. Aborting distribution.")
            return

        # --- Start of Changes ---
        # This payload matches the one from the user's network logs
        payload = {
            "resource": "0",  # Default value
            "attackBehaviour": "hide", # Default value
            "attributes": {
                "power": distribution.get("fightingStrength", 0),
                "offBonus": distribution.get("offBonus", 0),
                "defBonus": distribution.get("defBonus", 0),
                "productionPoints": distribution.get("resourceProduction", 0)
            }
        }
        # --- End of Changes ---

        log.info(f"[{username}] Attempting to set hero points with payload: {payload}")
        
        try:
            api_url = f"{client.server_url}/api/v1/hero/v2/attributes"
            headers = {
                'X-Version': client.server_version,
                'X-Requested-With': 'XMLHttpRequest',
                'Content-Type': 'application/json; charset=UTF-8'
            }
            
            # --- Start of Changes ---
            # Changed from PUT to POST to match the network logs
            resp = client.sess.post(api_url, json=payload, headers=headers, timeout=15)
            
            # The server returns an empty list on success for this POST request
            if resp.status_code == 200 and resp.json() == []:
                log.info(f"[{username}] Successfully distributed {points_to_distribute} hero points.")
            else:
                log.error(f"[{username}] Failed to distribute hero points. Response: {resp.text}")
            # --- End of Changes ---

        except Exception as e:
            log.error(f"[{username}] An error occurred while distributing hero points: {e}", exc_info=True)