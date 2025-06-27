# File: modules/hero.py

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

        try:
            hero_page_resp = client.sess.get(f"{client.server_url}/hero.php?t=1", timeout=15)
            soup = BeautifulSoup(hero_page_resp.text, 'html.parser')

            if not soup.select_one('i.levelUp.show'):
                return 

            script_tag = soup.find("script", string=re.compile(r"window\.Travian\.React\.HeroV2\.render"))
            if not script_tag:
                log.warning(f"[{username}] Could not find hero data script.")
                return

            script_content = script_tag.string
            match = re.search(r'HeroV2\.render\s*\((.*)\);', script_content, re.DOTALL | re.MULTILINE)
            if not match:
                log.warning(f"[{username}] Could not extract hero data from script.")
                return
            
            json_str = ""
            open_braces = 0
            start_index = match.start(1)
            for i, char in enumerate(script_content[start_index:]):
                if char == '{':
                    open_braces += 1
                elif char == '}':
                    open_braces -= 1
                json_str += char
                if open_braces == 0 and char == '}':
                    break
            
            hero_data = json.loads(json_str)
            
            free_points = hero_data.get("screenData", {}).get("hero", {}).get("freePoints", 0)

            if free_points > 0:
                log.info(f"[{username}] Hero has {free_points} attribute points to distribute.")
                self.distribute_points(client, hero_data, free_points, account_config["hero_settings"])

        except Exception as e:
            log.error(f"[{username}] An error occurred while checking hero attributes: {e}", exc_info=True)

    def distribute_points(self, client, hero_data, free_points, hero_settings):
        """
        Distributes the available hero points based on the user's configuration.
        """
        username = client.username
        distribution = hero_settings.get("point_distribution", {})
        
        if free_points < 4:
            log.info(f"[{username}] Not enough points to distribute based on the 4-point-per-level logic.")
            return

        payload = {
            "fightingStrength": hero_data["screenData"]["hero"]["attributes"]["fightingStrength"]["usedPoints"],
            "offBonus": hero_data["screenData"]["hero"]["attributes"]["offBonus"]["usedPoints"],
            "defBonus": hero_data["screenData"]["hero"]["attributes"]["defBonus"]["usedPoints"],
            "resourceProduction": hero_data["screenData"]["hero"]["attributes"]["resourceProduction"]["usedPoints"],
            "checksum": hero_data["screenData"]["checksum"]
        }
        
        points_to_distribute = 4
        
        payload["fightingStrength"] += distribution.get("fightingStrength", 0)
        payload["offBonus"] += distribution.get("offBonus", 0)
        payload["defBonus"] += distribution.get("defBonus", 0)
        payload["resourceProduction"] += distribution.get("resourceProduction", 0)

        log.info(f"[{username}] Attempting to set hero points to: {payload}")
        
        try:
            api_url = f"{client.server_url}/api/v1/hero/v2/attributes"
            headers = {
                'X-Version': client.server_version,
                'X-Requested-With': 'XMLHttpRequest',
                'Content-Type': 'application/json; charset=UTF-8'
            }
            
            resp = client.sess.put(api_url, json=payload, headers=headers, timeout=15)
            
            if resp.status_code == 200 and resp.json().get('success'):
                log.info(f"[{username}] Successfully distributed hero points.")
            else:
                log.error(f"[{username}] Failed to distribute hero points. Response: {resp.text}")

        except Exception as e:
            log.error(f"[{username}] An error occurred while distributing hero points: {e}", exc_info=True)