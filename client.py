# js-92-ch/travianhttpbot/TravianHttpBot-loop/client.py

import re
import json
import requests
import time
import logging
from typing import Dict, Any, Optional, List, Tuple
from bs4 import BeautifulSoup
from urllib.parse import urljoin, parse_qs, urlencode

from config import log, gid_name, NAME_TO_GID, state_lock, BOT_STATE

class TravianClient:
    """Lightweight HTTP wrapper around the Travian *HTML* and JSON endpoints."""

    def __init__(self, username: str, password: str, server_url: str, proxy: Optional[Dict[str, Any]] = None):
        self.username = username
        self.password = password
        self.server_url = server_url.rstrip("/")
        self.sess = requests.Session()
        self.sess.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/108.0.0.0 Safari/537.36",
        })
        self.server_version = None

        if proxy and proxy.get('ip') and proxy.get('port'):
            proxy_user = proxy.get('username')
            proxy_pass = proxy.get('password')
            proxy_ip = proxy.get('ip')
            proxy_port = proxy.get('port')
            
            proxy_auth = ""
            if proxy_user and proxy_pass:
                proxy_auth = f"{proxy_user}:{proxy_pass}@"
            
            proxy_url = f"http://{proxy_auth}{proxy_ip}:{proxy_port}"
            
            self.sess.proxies = {
                "http": proxy_url,
                "https": proxy_url,
            }
            log.info(f"[{self.username}] Using proxy: {proxy_ip}:{proxy_port}")

    def send_hero(self, current_village_id: int, x: int, y: int) -> tuple[bool, int]:
        """
        Sends the hero to the specified coordinates by simulating the browser's
        multi-step process, parsing the travel time, and returning both
        success status and the duration in seconds.
        """
        log.info(f"[{self.username}] Sending hero from village {current_village_id} to ({x}|{y}) as reinforcement.")
        try:
            # Step 1: GET the rally point page to get the troop form
            rp_url = f"{self.server_url}/build.php?newdid={current_village_id}&gid=16&tt=2"
            rp_resp = self.sess.get(rp_url, timeout=15)
            rp_soup = BeautifulSoup(rp_resp.text, 'html.parser')

            troop_form = rp_soup.find('form', action=re.compile(r'build\.php'))
            if not troop_form:
                log.error(f"[{self.username}] Could not find troop form on rally point page.")
                return False, 0

            # Step 2: Prepare and send the first POST request to get the confirmation page
            first_post_data = {i['name']: i['value'] for i in troop_form.find_all('input', {'name': True})}
            first_post_data.update({
                'troop[t11]': '1',
                'x': str(x),
                'y': str(y),
                'redeployHero': '1',
                'eventType': '2', # 2 for reinforcement
                'ok': 'ok'
            })

            form_action = urljoin(self.server_url, troop_form['action'])
            sel_resp = self.sess.post(form_action, data=first_post_data, timeout=15)
            sel_soup = BeautifulSoup(sel_resp.text, 'html.parser')

            # Step 3: Parse travel time from the confirmation page
            travel_time = 0
            arrival_info_div = sel_soup.find('div', class_='in')
            if arrival_info_div:
                time_match = re.search(r'(\d+):(\d{2}):(\d{2})', arrival_info_div.text)
                if time_match:
                    h, m, s = map(int, time_match.groups())
                    travel_time = h * 3600 + m * 60 + s
                    log.info(f"[{self.username}] Parsed travel time: {travel_time} seconds.")
            
            if travel_time == 0:
                log.warning(f"[{self.username}] Could not parse travel time. Defaulting to a safe wait of 180 seconds.")
                travel_time = 180

            # Step 4: Prepare and send the final POST request with the checksum
            second_post_form = sel_soup.find('form', id='troopSendForm')
            if not second_post_form:
                log.error(f"[{self.username}] Could not find the confirmation form. The hero might be busy or another issue occurred.")
                return False, 0
            
            second_post_data = {i['name']: i['value'] for i in second_post_form.find_all('input', {'name': True})}

            # --- ROBUST CHECKSUM EXTRACTION ---
            checksum_button = sel_soup.find('button', id='confirmSendTroops')
            if checksum_button and checksum_button.has_attr('onclick'):
                # This regex is more robust and handles variations in spacing and quotes.
                match = re.search(r"value\s*=\s*'([^']+)'", checksum_button['onclick'])
                if match:
                    second_post_data['checksum'] = match.group(1)
            
            if not second_post_data.get('checksum'):
                log.error(f"[{self.username}] CRITICAL: Could not extract checksum from the confirmation button. Aborting hero send.")
                return False, 0
            
            final_action_url = urljoin(self.server_url, second_post_form['action'])
            final_resp = self.sess.post(final_action_url, data=second_post_data, timeout=15)

            if final_resp.status_code == 200 and ("Reinforcement for" in final_resp.text or "troops are on their way" in final_resp.text):
                log.info(f"[{self.username}] Hero successfully sent to ({x}|{y}).")
                return True, travel_time
            else:
                log.error(f"[{self.username}] Final hero send request failed. The server did not confirm the movement. Status: {final_resp.status_code}.")
                return False, 0

        except Exception as e:
            log.error(f"[{self.username}] An unhandled error occurred during send_hero: {e}", exc_info=True)
            return False, 0

    def start_adventure(self, target_map_id: int) -> bool:
        """Sends the hero on a specific adventure."""
        try:
            api_url = f"{self.server_url}/api/v1/troop/send"
            payload = {
                "action": "troopsSend",
                "targetMapId": int(target_map_id),
                "eventType": 50,
                "troops": [{"t11": 1}]
            }
            headers = {
                'X-Version': self.server_version,
                'X-Requested-With': 'XMLHttpRequest',
                'Content-Type': 'application/json; charset=UTF-8'
            }

            self.sess.put(api_url, json=payload, headers=headers, timeout=15)
            post_resp = self.sess.post(api_url, json=payload, headers=headers, timeout=15)

            return post_resp.status_code == 200 and 'dialog' in post_resp.json()

        except requests.RequestException as e:
            log.error(f"[{self.username}] Network error sending hero on adventure: {e}")
            return False
        except Exception as e:
            log.error(f"[{self.username}] Unexpected error during start_adventure: {e}", exc_info=True)
            return False
        
    def collect_task_reward(self, payload: Dict[str, Any], village_id: int, village_name: str) -> bool:
        """Collects the reward for a completed task."""
        try:
            api_url = f"{self.server_url}/api/v1/progressive-tasks/collectReward?villageId={village_id}"
            headers = {
                'X-Version': self.server_version,
                'X-Requested-With': 'XMLHttpRequest',
                'Content-Type': 'application/json; charset=UTF-8'
            }

            resp = self.sess.post(api_url, json=payload, headers=headers, timeout=15)
            
            if resp.status_code == 200 and (resp.json().get('rewards') or resp.json().get('success')):
                log.info(f"[{self.username}] Successfully collected reward for task in {village_name}: {payload.get('questType')} - Level {payload.get('targetLevel')}")
                return True
            else:
                log.warning(f"[{self.username}] Failed to collect reward for task in {village_name}: {payload.get('questType')}. Response: {resp.text}")
                return False

        except requests.RequestException as e:
            log.error(f"[{self.username}] Network error collecting task reward for {village_name}: {e}")
            return False
        except Exception as e:
            log.error(f"[{self.username}] Unexpected error during collect_task_reward for {village_name}: {e}", exc_info=True)
            return False

    def login(self) -> bool:
        with state_lock:
            account_config = next((acc for acc in BOT_STATE.get("accounts", []) if acc['username'] == self.username), None)

        login_username = self.username
        if account_config and account_config.get("is_sitter"):
            login_username = account_config.get("login_username", self.username)
        
        log.info("[%s] Attempting API login to Vardom server...", login_username)
        try:
            login_page_url = f"{self.server_url}/"
            login_page_resp = self.sess.get(login_page_url, timeout=15)
            soup = BeautifulSoup(login_page_resp.text, 'html.parser')
            link_tag = soup.find("link", href=re.compile(r"gpack\.vardom\.net/([\d\.]+)/"))
            if link_tag and (match := re.search(r"gpack\.vardom\.net/([\d\.]+)/", link_tag['href'])):
                self.server_version = match.group(1)
            else: self.server_version = "2554.3"
            api_url = f"{self.server_url}/api/v1/auth/login"
            headers = {'X-Version': self.server_version, 'X-Requested-With': 'XMLHttpRequest'}
            payload = {"name": login_username, "password": self.password, "w": "1920:1080", "mobileOptimizations": False}
            resp = self.sess.post(api_url, json=payload, headers=headers, timeout=15)
            if resp.json().get("redirectTo") == "dorf1.php":
                log.info("[%s] Logged in successfully ✔", login_username)

                if account_config and account_config.get("is_sitter") and account_config.get("sitter_for"):
                    return self.switch_to_sitter(account_config["sitter_for"])

                return True
        except Exception as exc:
            log.error("[%s] Login process failed with an exception: %s", login_username, exc)
        return False

    def initiate_build(self, village_id: int, slot_id: int, gid: int, is_new_build: bool) -> Dict[str, Any]:
        log.info(f"[{self.username}] Attempting to build GID {gid_name(gid)} ({gid}) at slot {slot_id} (New Build: {is_new_build})")
        action_url, build_page_url = None, f"{self.server_url}/build.php?newdid={village_id}&id={slot_id}"
        
        try:
            if is_new_build:
                WALL_GIDS = {31, 32, 33, 42, 43}
                for category in [1, 2, 3]:
                    if action_url: break
                    resp = self.sess.get(f"{build_page_url}&category={category}", timeout=15)
                    soup = BeautifulSoup(resp.text, 'html.parser')
                    search_gids = [gid]
                    if gid in WALL_GIDS: search_gids.extend(g for g in WALL_GIDS if g != gid)
                    for search_gid in search_gids:
                        if img_tag := soup.find('img', class_=f'g{search_gid}'):
                            wrapper = img_tag.find_parent('div', class_='buildingWrapper')
                            if wrapper and (button := wrapper.find('button', class_='green', disabled=False)):
                                if match := re.search(r"window\.location\.href\s*=\s*'([^']+)'", button.get('onclick', '')):
                                    action_url = urljoin(self.server_url, match.group(1).replace('&amp;', '&'))
                                    if 'newdid=' not in action_url:
                                        action_url = f"{action_url}{'&' if '?' in action_url else '?'}{village_id}"
                                    break
                if not action_url: return {'status': 'error', 'reason': f'Could not find build button for GID {gid}'}
            else: # Upgrade
                resp = self.sess.get(build_page_url, timeout=15)
                soup = BeautifulSoup(resp.text, 'html.parser')
                if err_msg := soup.select_one(".upgradeBlocked .errorMessage"):
                    return {'status': 'error', 'reason': err_msg.text.strip()}
                if button := soup.find('button', class_=re.compile(r'\b(green|build)\b'), disabled=False):
                    if match := re.search(r"window\.location\.href\s*=\s*'([^']+)'", button.get('onclick', '')):
                        action_url = urljoin(self.server_url, match.group(1).replace('&amp;', '&'))
                        if 'newdid=' not in action_url:
                           action_url = f"{action_url}{'&' if '?' in action_url else '?'}{village_id}"
                if not action_url: return {'status': 'error', 'reason': 'Could not find upgrade button'}

            confirmation_resp = self.sess.get(action_url, timeout=15)
            conf_soup = BeautifulSoup(confirmation_resp.text, 'html.parser')
            
            # Find the timer for the *last* item in the queue
            timers = conf_soup.select(".buildingList .timer")
            if timers:
                last_timer = timers[-1]
                eta = int(last_timer.get('value', 0))
                log.info(f"[{self.username}] Successfully initiated build for {gid_name(gid)}. ETA: {eta}s")
                return {'status': 'success', 'eta': eta}
            else:
                log.warning(f"[{self.username}] Build command sent, but couldn't confirm in response page.")
                return {'status': 'error', 'reason': 'confirmation_failed_or_res_low'}
        except requests.RequestException as e:
            return {'status': 'error', 'reason': f'Network error: {e}'}

    def fetch_and_parse_village(self, village_id: int) -> Optional[Dict[str, Any]]:
        log.info("[%s] Fetching data for village %d", self.username, village_id)
        try:
            url_d1 = f"{self.server_url}/dorf1.php?newdid={village_id}"
            resp_d1 = self.sess.get(url_d1, timeout=15)
            village_data = self.parse_village_page(resp_d1.text, "dorf1")
            url_d2 = f"{self.server_url}/dorf2.php?newdid={village_id}"
            resp_d2 = self.sess.get(url_d2, timeout=15)
            parsed_d2 = self.parse_village_page(resp_d2.text, "dorf2")
            final_buildings = {b['id']: b for b in village_data.get("buildings", [])}
            for building in parsed_d2.get("buildings", []):
                final_buildings[building['id']] = building
            village_data["buildings"] = list(final_buildings.values())
            village_data["queue"] = parsed_d2.get("queue", [])
            village_data["merchants"] = self.parse_merchants(resp_d1.text)
            return village_data
        except requests.RequestException as e:
            log.error(f"Network error fetching village data for {village_id}: {e}")
        except Exception as e:
            log.error(f"An unexpected error occurred in fetch_and_parse_village for {village_id}: {e}", exc_info=True)
        return None

    def parse_merchants(self, html: str) -> Dict[str, Any]:
        """Parses merchant information from dorf1."""
        soup = BeautifulSoup(html, "html.parser")
        merchants_data = {"total": 0, "capacity": 0}
        merchants_info = soup.find(id="merchants")
        if merchants_info:
            try:
                text = merchants_info.get_text(strip=True)
                match = re.search(r"(\d+)\s*x\s*(\d+)", text)
                if match:
                    merchants_data["total"] = int(match.group(1))
                    merchants_data["capacity"] = int(match.group(2))
            except (ValueError, AttributeError):
                pass
        return merchants_data

    def parse_village_page(self, html: str, page_type: str) -> Dict[str, Any]:
        soup = BeautifulSoup(html, "html.parser")
        out: Dict[str, Any] = {"resources": {}, "storage": {}, "production": {}, "buildings": [], "queue": [], "villages": [], "coords": {}}
        try:
            # Get coordinates from the active village list
            active_village_entry = soup.select_one("#sidebarBoxVillageList .listEntry.active")
            if active_village_entry:
                coord_span = active_village_entry.select_one(".coordinates")
                if coord_span:
                    x = coord_span.select_one(".coordinateX").text.strip('()')
                    y = coord_span.select_one(".coordinateY").text.strip('()')
                    out["coords"] = {'x': int(x), 'y': int(y)}

            if script_text := soup.find("script", string=re.compile(r"var\s+resources\s*=")):
                if match := re.search(r"var\s+resources\s*=\s*(\{.*?\});", script_text.string, re.DOTALL):
                    json_str = re.sub(r'([a-zA-Z_][\w]*)\s*:', r'"\1":', match.group(1))
                    res_data = json.loads(json_str)
                    out.update({
                        "resources": {k: int(v) for k, v in res_data.get("storage", {}).items()},
                        "storage": {k: int(v) for k, v in res_data.get("maxStorage", {}).items()},
                        "production": {k: int(v) for k, v in res_data.get("production", {}).items()}
                    })
        except Exception as exc: log.debug(f"Resource javascript parser failed: {exc}")
        found_buildings = {}
        if container := soup.find(id="resourceFieldContainer"):
            for slot in container.select('a[href*="build.php?id="]'):
                try:
                    loc_id = int(re.search(r'id=(\d+)', slot['href']).group(1))
                    gid_class = next((c for c in slot.get('class', []) if c.startswith('gid') and c[3:].isdigit()), None)
                    if not gid_class: continue
                    gid = int(gid_class[3:])
                    level = int(slot.find('div', class_='labelLayer').text.strip() or 0)
                    name = BeautifulSoup(slot.get('title', ''), 'html.parser').get_text().split('||')[0].strip()
                    found_buildings[loc_id] = {'id': loc_id, 'gid': gid, 'level': level, 'name': name}
                except Exception: continue
        for slot in soup.select('#villageContent > .buildingSlot'):
            try:
                if not (slot.has_attr('data-aid') and slot.has_attr('data-gid')): continue
                loc_id, gid = int(slot['data-aid']), int(slot.get('data-gid', 0))
                level = int(slot.select_one('a.level[data-level]')['data-level']) if slot.select_one('a.level[data-level]') else 0
                found_buildings[loc_id] = {'id': loc_id, 'gid': gid, 'level': level, 'name': slot.get('data-name')}
            except Exception: continue
        out['buildings'] = list(found_buildings.values())
        
        for li in soup.select(".buildingList li"):
            if (name_div := li.find("div", class_="name")) and (lvl_span := li.find("span", class_="lvl")) and (timer_span := li.find("span", class_="timer")):
                level_text = lvl_span.text.strip()
                level_match = re.search(r'\d+', level_text)
                level = int(level_match.group(0)) if level_match else 0
                out["queue"].append({
                    "name": name_div.text.strip(),
                    "level": level,
                    "eta": int(timer_span.get("value", 0))
                })
                
        for v_entry in soup.select("#sidebarBoxVillageList .listEntry"):
            if link := v_entry.find("a",href=re.compile(r"newdid=")):
                out["villages"].append({"id":int(re.search(r"newdid=(\d+)",link["href"]).group(1)),"name":v_entry.find("span",class_="name").text.strip(),"active":"active" in v_entry.get("class",[])})
        return out
    
    def get_hero_inventory(self) -> Dict[str, Any]:
        """
        Fetches the hero's inventory and equipped items.
        """
        log.info(f"[{self.username}] Fetching hero inventory...")
        try:
            url = f"{self.server_url}/hero/inventory"
            resp = self.sess.get(url, timeout=15)
            soup = BeautifulSoup(resp.text, 'html.parser')

            script_tag = soup.find("script", string=re.compile(r"window\.Travian\.React\.HeroV2\.render"))
            if not script_tag:
                log.warning(f"[{self.username}] Could not find hero data script on inventory page.")
                return {}

            script_content = script_tag.string
            match = re.search(r"screenData:\s*(\{.*\})\s*,\s*favouriteTab:", script_content, re.DOTALL)
            if not match:
                log.warning(f"[{self.username}] Could not extract screenData JSON from the script.")
                return {}

            json_str = match.group(1)
            open_braces = 0
            json_end = -1
            for i, char in enumerate(json_str):
                if char == '{':
                    open_braces += 1
                elif char == '}':
                    open_braces -= 1
                    if open_braces == 0:
                        json_end = i + 1
                        break
            
            if json_end == -1:
                log.warning(f"[{self.username}] Could not find the closing brace for the screenData JSON.")
                return {}

            final_json_str = json_str[:json_end]
            data = json.loads(final_json_str)

            return {
                'inventory': data.get('viewData', {}).get('itemsInventory', []),
                'equipped': data.get('viewData', {}).get('itemsEquipped', [])
            }

        except Exception as e:
            log.error(f"[{self.username}] An error occurred while fetching hero inventory: {e}", exc_info=True)
            return {}

    def equip_item(self, item_id: int) -> bool:
        """
        Equips a specific item from the hero's inventory.
        """
        log.info(f"[{self.username}] Attempting to equip item with ID: {item_id}")
        try:
            api_url = f"{self.server_url}/api/v1/hero/v2/inventory/move-item"
            payload = {
                "action": "inventory",
                "itemId": item_id,
                "amount": 1,
                "targetPlaceId": -1
            }
            headers = {
                'X-Version': self.server_version,
                'X-Requested-With': 'XMLHttpRequest',
                'Content-Type': 'application/json; charset=UTF-8',
                'Referer': f'{self.server_url}/hero/inventory'
            }
            
            resp = self.sess.post(api_url, json=payload, headers=headers, timeout=15)
            
            if resp.status_code == 200 or resp.status_code == 204:
                log.info(f"[{self.username}] Successfully sent request to equip item {item_id} (Status: {resp.status_code}).")
                try:
                    self.sess.get(f"{self.server_url}/api/v1/hero/dataForHUD", timeout=15)
                    self.sess.get(f"{self.server_url}/api/v1/hero/v2/screen/inventory", timeout=15)
                    log.info(f"[{self.username}] Performed follow-up GET requests to refresh hero data.")
                except Exception as e:
                    log.warning(f"[{self.username}] Follow-up GET requests after equipping failed: {e}")
                return True
            else:
                log.error(f"[{self.username}] Failed to equip item {item_id}. Status: {resp.status_code}, Response: {resp.text}")
                return False
                
        except Exception as e:
            log.error(f"[{self.username}] An error occurred while equipping item {item_id}: {e}", exc_info=True)
            return False

    def get_hero_initial_location(self) -> Optional[int]:
        """
        Determines the hero's current home village ID. This now primarily relies on get_hero_status.
        """
        log.info(f"[{self.username}] --- STARTING HERO LOCATION CHECK (v3) ---")
        
        try:
            hero_status = self.get_hero_status()
            if hero_status and hero_status.get('village_id') and not hero_status.get('is_moving'):
                village_id = hero_status['village_id']
                log.info(f"[{self.username}] SUCCESS: Hero location confirmed at village ID: {village_id}")
                log.info(f"[{self.username}] --- ENDING HERO LOCATION CHECK (SUCCESS) ---")
                return village_id
            else:
                log.warning(f"[{self.username}] Hero is moving or status is unavailable. Status: {hero_status}")

        except Exception as e:
            log.error(f"[{self.username}] An exception occurred during hero location check: {e}", exc_info=True)

        log.error(f"[{self.username}] Could not determine hero's home village.")
        log.info(f"[{self.username}] --- ENDING HERO LOCATION CHECK (FAILURE) ---")
        return None
    
    def find_hero_in_rally_point(self, village_id: int) -> Optional[int]:
        """
        Checks the rally point of a specific village to see if the hero is present.
        Returns the village_id if the hero is found, otherwise None.
        """
        log.info(f"[{self.username}] Checking for hero in rally point of village {village_id}...")
        try:
            url = f"{self.server_url}/build.php?newdid={village_id}&gid=16&tt=1&filter=3"
            resp = self.sess.get(url, timeout=15)
            if resp.status_code != 200:
                log.warning(f"[{self.username}] Failed to fetch rally point for village {village_id}. Status: {resp.status_code}")
                return None

            soup = BeautifulSoup(resp.text, 'html.parser')
            
            troop_tables = soup.find_all('table', class_='troop_details')
            if not troop_tables:
                log.debug(f"[{self.username}] No troop_details table found in village {village_id}.")
                return None

            for table in troop_tables:
                header_row = table.find('tbody', class_='units').find('tr')
                if not header_row:
                    continue

                unit_icons = header_row.find_all('td', class_='uniticon')
                hero_column_index = -1
                for i, icon_cell in enumerate(unit_icons):
                    if icon_cell.find('img', class_='uhero'):
                        hero_column_index = i
                        break
                
                if hero_column_index == -1:
                    continue

                count_row = table.find('tbody', class_='units last').find('tr')
                if not count_row:
                    continue

                count_cells = count_row.find_all('td', class_='unit')
                
                if len(count_cells) > hero_column_index:
                    hero_count_text = count_cells[hero_column_index].text.strip()
                    if hero_count_text.isdigit() and int(hero_count_text) == 1:
                        log.info(f"[{self.username}] SUCCESS: Hero found in village {village_id} via rally point.")
                        return village_id

            log.debug(f"[{self.username}] Hero not found in any troop table in village {village_id}.")
            return None

        except Exception as e:
            log.error(f"[{self.username}] An error occurred checking rally point for village {village_id}: {e}", exc_info=True)
            return None

    def get_hero_status(self) -> Dict[str, Any]:
        """
        Fetches hero status from the hero.php page by parsing the embedded React JSON data.
        This is the most reliable method for getting hero location and status.
        """
        log.info(f"[{self.username}] Getting hero status from hero.php...")
        try:
            resp = self.sess.get(f"{self.server_url}/hero.php", timeout=15)
            if resp.status_code != 200:
                log.error(f"[{self.username}] Failed to fetch hero.php. Status: {resp.status_code}")
                return {}

            soup = BeautifulSoup(resp.text, 'html.parser')
            script_tag = soup.find("script", string=re.compile(r"window\.Travian\.React\.HeroV2\.render"))
            
            if not script_tag:
                log.warning(f"[{self.username}] Could not find the HeroV2 render script on hero.php.")
                return {}

            script_content = script_tag.string
            match = re.search(r"screenData:\s*(\{.*\})\s*,\s*favouriteTab:", script_content, re.DOTALL)
            if not match:
                log.warning(f"[{self.username}] Could not extract screenData JSON from the script.")
                return {}

            json_str = match.group(1)
            open_braces = 0
            json_end = -1
            for i, char in enumerate(json_str):
                if char == '{':
                    open_braces += 1
                elif char == '}':
                    open_braces -= 1
                    if open_braces == 0:
                        json_end = i + 1
                        break
            
            if json_end == -1:
                log.warning(f"[{self.username}] Could not find the closing brace for the screenData JSON.")
                return {}

            final_json_str = json_str[:json_end]
            data = json.loads(final_json_str)
            
            hero_state = data.get('heroState', {})
            status_info = hero_state.get('status', {})
            
            if status_info.get('status') == 100:
                village_id = int(hero_state.get('homeVillage', {}).get('id', 0))
                if village_id > 0:
                    log.info(f"[{self.username}] Hero is home at village ID: {village_id}")
                    return {
                        'village_id': village_id,
                        'is_moving': False,
                        'travel_time': 0
                    }

            log.info(f"[{self.username}] Hero is not home. Status: {status_info}")
            return {'village_id': None, 'is_moving': True, 'travel_time': 60}

        except json.JSONDecodeError as e:
            log.error(f"[{self.username}] Failed to decode hero JSON from hero.php: {e}")
        except Exception as e:
            log.error(f"[{self.username}] Could not get hero status from hero.php: {e}", exc_info=True)
        
        return {}

    def move_hero(self, current_village_id: int, target_x: int, target_y: int) -> Tuple[bool, int]:
        """
        Moves the hero by accurately simulating the browser's multi-step process.
        """
        try:
            rally_point_url = f"{self.server_url}/build.php?newdid={current_village_id}&gid=16&tt=2"
            
            log.info(f"[{self.username}] Step 1/4: Accessing initial send troops page: {rally_point_url}")
            self.sess.get(rally_point_url, timeout=15)

            initial_post_url = f"{self.server_url}/build.php?id=39&tt=2"
            initial_payload = {
                'troop[t11]': '1',
                'x': str(target_x),
                'y': str(target_y),                
                'villagename': '',
                'eventType': '2',
                'redeployHero': '1',
                'ok': 'ok'
            }
            log.info(f"[{self.username}] Step 2/4: Posting initial move request to {initial_post_url}")
            confirmation_page_resp = self.sess.post(
                initial_post_url,
                data=initial_payload,
                timeout=15,
                headers={'Referer': rally_point_url},
            )
            
            soup = BeautifulSoup(confirmation_page_resp.text, 'html.parser')
            confirm_form = soup.find('form', id='troopSendForm')
            if not confirm_form:
                log.error(f"[{self.username}] PARSING FAILED: Could not find the final confirmation form (troopSendForm).")
                return False, 0

            final_payload = {i['name']: i['value'] for i in confirm_form.find_all('input', {'type': 'hidden'})}
            
            if not final_payload.get('checksum'):
                checksum_script = soup.find('script', id='confirmSendTroops_script')
                if checksum_script:
                    m = re.search(r"name=['\"]?checksum['\"]?\].*?\.value\s*=\s*'([^']+)'", checksum_script.text)
                    if m:
                        final_payload['checksum'] = m.group(1)
            
            travel_time = 0
            if (arrival_info := soup.select_one("tbody.infos .in")) and (time_match := re.search(r'(\d+):(\d{2}):(\d{2})', arrival_info.text)):
                h, m, s = map(int, time_match.groups())
                travel_time = h * 3600 + m * 60 + s
                log.info(f"[{self.username}] Step 3/4: Parsed travel time: {travel_time} seconds.")
            else:
                log.warning(f"[{self.username}] Could not parse travel time from confirmation page. Aborting.")
                return False, 0
            
            final_action_url = urljoin(self.server_url, confirm_form['action'])
            
            log.info(f"[{self.username}] Step 4/4: Sending final hero move confirmation to {final_action_url}")
            final_resp = self.sess.post(final_action_url, data=final_payload, timeout=15, headers={'Referer': initial_post_url})

            success = (
                "troop_details" in final_resp.text
                and (
                    "troops are on their way" in final_resp.text
                    or "Reinforcement for" in final_resp.text
                    or "Changed successfully" in final_resp.text
                )
            )

            if success:
                log.info(f"[{self.username}] SUCCESS: Hero movement to ({target_x}|{target_y}) was successfully confirmed by the server.")
                return True, travel_time
            else:
                log.error(f"[{self.username}] FAILED: The final confirmation POST did not result in a success page.")
                return False, 0

        except Exception as e:
            log.error(f"[{self.username}] A critical error occurred during the hero move process: {e}", exc_info=True)
            return False, 0

    def get_training_page(self, village_id: int, gid: int) -> Optional[Dict]:
        """Fetches and parses a troop training page (barracks, stable, etc.)."""
        try:
            url = f"{self.server_url}/build.php?newdid={village_id}&gid={gid}"
            resp = self.sess.get(url, timeout=15)
            soup = BeautifulSoup(resp.text, 'html.parser')

            if "no troops for this building have been researched yet" in resp.text.lower():
                return {'trainable': [], 'training_queue': [], 'queue_duration_seconds': 0}

            form = soup.find('form', {'name': 'snd'})
            if not form: return None

            build_id_match = re.search(r'build\.php\?id=(\d+)', form['action'])
            build_id = int(build_id_match.group(1)) if build_id_match else 0
            form_data = {i['name']: i['value'] for i in form.find_all('input') if i.has_attr('name')}
            
            training_queue = []
            queue_duration_seconds = 0
            if queue_table := soup.find('table', class_='under_progress'):
                all_rows = queue_table.select('tbody tr')
                training_rows = [row for row in all_rows if 'next' not in row.get('class', [])]

                if training_rows:
                    last_row = training_rows[-1]
                    if dur_cell := last_row.find('td', class_='dur'):
                        if timer := dur_cell.find('span', class_='timer'):
                            queue_duration_seconds = int(timer.get('value', 0))

                for row in training_rows:
                    desc_cell = row.find('td', class_='desc')
                    dur_cell = row.find('td', class_='dur')
                    if desc_cell and dur_cell:
                        unit_img = desc_cell.find('img', class_='unit')
                        if not unit_img: continue
                        
                        desc_text = desc_cell.get_text(separator=' ', strip=True)
                        amount_match = re.search(r'(\d{1,3}(?:,\d{3})*)', desc_text)
                        amount = int(amount_match.group(1).replace(',', '')) if amount_match else 0
                        
                        unit_name = desc_text.replace(amount_match.group(1), '').strip() if amount_match else 'Unknown'

                        timer = dur_cell.find('span', class_='timer')
                        duration = int(timer.get('value', 0)) if timer else 0
                        training_queue.append({'name': unit_name, 'amount': amount, 'duration': duration})
            
            trainable_units = []
            for action_div in soup.select(".buildActionOverview .action"):
                details = action_div.find('div', class_='details')
                if not details: continue

                name_link = details.select_one(".tit > a:nth-of-type(2)")
                if not name_link: continue
                unit_name = name_link.text.strip()

                input_tag = details.find('input', {'type': 'text'})
                if not input_tag: continue
                unit_id_match = re.search(r't(\d+)', input_tag.get('name', ''))
                if not unit_id_match: continue
                unit_id = int(unit_id_match.group(1))

                max_amount = 0
                if max_link := details.select_one('.cta a'):
                    if max_match := re.search(r'(\d{1,3}(?:,?\d{3})*)', max_link.text):
                         max_amount = int(max_match.group(1).replace(',', ''))
                
                time_str = "0:0:0"
                milliseconds = 0
                if dur_span := details.find('div', class_='duration'):
                    if val_span := dur_span.find('span', class_='value'):
                        full_time_str = val_span.text.strip()
                        time_parts = full_time_str.split('(')
                        time_str = time_parts[0].strip()
                        if len(time_parts) > 1:
                            ms_match = re.search(r'(\d+)\s*ms', time_parts[1])
                            if ms_match:
                                milliseconds = int(ms_match.group(1))

                h,m,s = map(int, time_str.split(':'))
                time_per_unit = (h*3600 + m*60 + s) + (milliseconds / 1000.0)

                trainable_units.append({'id': unit_id, 'name': unit_name, 'max_trainable': max_amount, 'time_per_unit': time_per_unit})

            return {
                'build_id': build_id, 
                'form_data': form_data,
                'queue_duration_seconds': queue_duration_seconds, 
                'trainable': trainable_units,
                'training_queue': training_queue
            }
        except Exception as e:
            log.error(f"[{self.username}] Failed to get/parse GID {gid}: {e}", exc_info=True)
            return None


    def train_troops(self, village_id: int, build_id: int, form_data: Dict, troops_to_train: Dict[int, int]) -> bool:
        """Submits the form to train troops."""
        try:
            url = f"{self.server_url}/build.php?newdid={village_id}&id={build_id}"
            payload = form_data.copy()
            for troop_id, amount in troops_to_train.items():
                payload[f"t{troop_id}"] = str(amount)
            payload['s1'] = 'ok'

            resp = self.sess.post(url, data=payload, headers={'Referer': f"{self.server_url}/build.php?newdid={village_id}&gid=19"})
            return "in training" in resp.text.lower() or "duration" in resp.text.lower()
        except Exception as e:
            log.error(f"[{self.username}] Failed to train troops: {e}")
            return False

    def get_demolish_info(self, village_id: int) -> Optional[Dict]:
        """
        Fetches the main building page to get data needed for demolition.
        """
        try:
            main_building_slot = None
            # We need to find the location ID of the main building first
            dorf2_resp = self.sess.get(f"{self.server_url}/dorf2.php?newdid={village_id}", timeout=15)
            dorf2_soup = BeautifulSoup(dorf2_resp.text, 'html.parser')
            mb_slot = dorf2_soup.select_one('.buildingSlot.g15')
            if not mb_slot or not mb_slot.has_attr('data-aid'):
                log.error(f"[{self.username}] Could not find Main Building slot in village {village_id}.")
                return None
            main_building_slot = mb_slot['data-aid']

            url = f"{self.server_url}/build.php?newdid={village_id}&id={main_building_slot}"
            resp = self.sess.get(url, timeout=15)
            soup = BeautifulSoup(resp.text, 'html.parser')

            # Check if a demolition is already in progress
            if soup.find('table', id='demolish', class_='under_progress'):
                return {'can_demolish': False}

            form = soup.find('form', class_='demolish_building')
            if not form:
                return {'can_demolish': False, 'options': []}

            form_data = {i['name']: i['value'] for i in form.find_all('input', {'type': 'hidden'}) if i.has_attr('name')}
            
            options = []
            for option in form.select('select#demolish option'):
                text = option.text
                value = option['value']
                
                # Extract details like "19 . Warehouse 20"
                match = re.search(r'(\d+)\s*\.\s*(.*?)\s*(\d+)', text)
                if match:
                    location_id = int(match.group(1).strip())
                    name = match.group(2).strip()
                    level = int(match.group(3).strip())
                    options.append({'location_id': location_id, 'name': name, 'level': level, 'value': value})

            return {
                'can_demolish': True,
                'form_data': form_data,
                'options': options
            }
        except Exception as e:
            log.error(f"[{self.username}] Failed to get demolition info for village {village_id}: {e}", exc_info=True)
            return None

    def demolish_building(self, village_id: int, abriss_value: str, form_data: Dict) -> int:
        """
        Sends the POST request to demolish a building and returns the duration.
        """
        try:
            # Find the main building's own location ID to build the referrer URL
            dorf2_resp = self.sess.get(f"{self.server_url}/dorf2.php?newdid={village_id}", timeout=15)
            dorf2_soup = BeautifulSoup(dorf2_resp.text, 'html.parser')
            mb_slot_div = dorf2_soup.select_one('.buildingSlot.g15')
            if not mb_slot_div or not mb_slot_div.has_attr('data-aid'):
                log.error(f"[{self.username}] Could not find Main Building slot ID for referrer.")
                return 0
            
            mb_location_id = mb_slot_div['data-aid']
            
            url = f"{self.server_url}/build.php?gid=15"
            payload = form_data.copy()
            payload['abriss'] = abriss_value

            headers = {'Referer': f"{self.server_url}/build.php?id={mb_location_id}"}
            
            resp = self.sess.post(url, data=payload, headers=headers, timeout=15, params={'newdid': village_id})
            soup = BeautifulSoup(resp.text, 'html.parser')
            
            # We look for a table with id="demolish" that is also "under_progress"
            demolish_table = soup.select_one("table#demolish.transparent")
            if demolish_table:
                timer_span = demolish_table.find("span", class_="timer")
                if timer_span and timer_span.has_attr('value'):
                    duration = int(timer_span['value'])
                    log.info(f"[{self.username}] Successfully initiated demolition. Duration: {duration}s")
                    return duration
            
            log.warning(f"[{self.username}] Could not find demolition timer after request.")
            return 0
            
        except Exception as e:
            log.error(f"[{self.username}] Failed to execute demolition: {e}", exc_info=True)
            return 0
        
    def get_smithy_page(self, village_id: int, gid: int) -> Optional[Dict]:
        """Fetches and parses a building page using its GID."""
        try:
            # Construct the URL using gid instead of the specific location id
            url = f"{self.server_url}/build.php?newdid={village_id}&gid={gid}"
            resp = self.sess.get(url, timeout=15)
            soup = BeautifulSoup(resp.text, 'html.parser')

            researches = []
            for research_div in soup.select('.build_details.researches .research'):
                title_div = research_div.select_one('.information .title')
                if not title_div: continue

                name_anchor = title_div.select_one('a:nth-of-type(2)')
                if not name_anchor: continue
                
                name = name_anchor.text.strip()
                level_span = title_div.select_one('.level')
                
                level = 0
                if level_span:
                    level_text = level_span.text
                    level_match = re.search(r'\d+', level_text)
                    if level_match:
                        level = int(level_match.group(0))

                upgrade_url = None
                button = research_div.select_one('.cta button.green')
                if button and button.has_attr('onclick'):
                    match = re.search(r"window\.location\.href\s*=\s*'([^']+)'", button['onclick'])
                    if match:
                        upgrade_url = urljoin(self.server_url, match.group(1).replace('&amp;', '&'))

                researches.append({'name': name, 'level': level, 'upgrade_url': upgrade_url})
            
            research_queue = []
            if queue_table := soup.find('table', class_='under_progress'):
                for row in queue_table.select('tbody tr'):
                    name_cell = row.find('td', class_='desc')
                    duration_cell = row.find('td', class_='fin')
                    if name_cell and duration_cell:
                        name = name_cell.get_text(strip=True).split('level')[0].strip()
                        timer = duration_cell.find('span', class_='timer')
                        if timer and timer.has_attr('value'):
                            eta = int(timer['value'])
                            research_queue.append({'name': name, 'eta': eta})
            
            plus_account = soup.select_one('.upgradeButtonsContainer .section2') is not None

            return {
                'researches': researches,
                'research_queue': research_queue,
                'plus_account': plus_account
            }

        except Exception as e:
            log.error(f"[{self.username}] Failed to get/parse Smithy page for village {village_id}: {e}", exc_info=True)
            return None

    def upgrade_unit(self, village_id: int, upgrade_url: str) -> bool:
        """Sends the GET request to upgrade a unit in the smithy."""
        try:
            log.info(f"[{self.username}] Sending upgrade request: {upgrade_url}")
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:140.0) Gecko/20100101 Firefox/140.0",
                "Referer": f"{self.server_url}/build.php?newdid={village_id}&gid=13"
            }
            resp = self.sess.get(upgrade_url, headers=headers, timeout=15)
            return resp.status_code == 200
        except Exception as e:
            log.error(f"[{self.username}] Failed to execute unit upgrade: {e}", exc_info=True)
            return False
        
    def switch_to_sitter(self, sitter_for_name):
        """
        Switches to a sitter account after logging in.
        """
        log.info(f"[{self.username}] Attempting to switch to sitter account for: {sitter_for_name}")
        try:
            # 1. Get sittings info
            graphql_url = f"{self.server_url}/api/v1/graphql"
            graphql_payload = {
                "query": "query{account{sittings{sittingId player{id name}loggedIn loginIsPossible}}}"
            }
            headers = {
                'X-Version': self.server_version,
                'X-Requested-With': 'XMLHttpRequest',
                'Content-Type': 'application/json; charset=UTF-8'
            }
            sittings_resp = self.sess.post(graphql_url, json=graphql_payload, headers=headers, timeout=15)
            sittings_data = sittings_resp.json()

            sittings = sittings_data.get("data", {}).get("account", {}).get("sittings", [])
            target_sitter = next((s for s in sittings if s.get("player", {}).get("name", "").lower() == sitter_for_name.lower()), None)

            if not target_sitter:
                log.error(f"[{self.username}] Could not find sitter information for '{sitter_for_name}'.")
                return False

            if not target_sitter.get("loginIsPossible"):
                log.error(f"[{self.username}] Login to sitter account '{sitter_for_name}' is not possible at this time.")
                return False

            player_id_to_switch = target_sitter.get("player", {}).get("id")

            # 2. Initiate the switch
            switch_url = f"{self.server_url}/api/v1/auth/switch"
            switch_payload = {"uid": player_id_to_switch, "w": "1920:1080"}
            switch_resp = self.sess.post(switch_url, json=switch_payload, headers=headers, timeout=15)
            switch_data = switch_resp.json()

            if "redirectTo" not in switch_data:
                log.error(f"[{self.username}] Failed to get redirection URL for sitter switch.")
                return False

            # 3. Finalize the switch
            redirect_url = f"{self.server_url}{switch_data['redirectTo']}"
            final_resp = self.sess.get(redirect_url, timeout=15)

            if final_resp.status_code == 200:
                log.info(f"[{self.username}] Successfully switched to sitter account for: {sitter_for_name}")
                return True
            else:
                log.error(f"[{self.username}] Failed to finalize sitter switch. Status code: {final_resp.status_code}")
                return False

        except Exception as e:
            log.error(f"[{self.username}] An error occurred while switching to sitter account: {e}", exc_info=True)
            return False
        
    def get_infobox_html(self) -> Optional[str]:
        """
        Fetches the HTML content of the sidebar's infobox.
        """
        log.info(f"[{self.username}] Fetching infobox HTML...")
        try:
            # The infobox is typically on dorf1
            resp = self.sess.get(f"{self.server_url}/dorf1.php", timeout=15)
            soup = BeautifulSoup(resp.text, 'html.parser')
            infobox = soup.find(id="sidebarBoxInfobox")
            if infobox:
                return str(infobox)
            else:
                log.warning(f"[{self.username}] Could not find infobox on page.")
                return None
        except Exception as e:
            log.error(f"[{self.username}] An error occurred while fetching infobox HTML: {e}", exc_info=True)
            return None

    def send_resources(self, from_village_id: int, target_x: int, target_y: int, resources: Dict[str, int], runs: int = 1) -> bool:
        """
        Sends resources to another village using the marketplace via the REST API.
        `resources` should be a dict like {'lumber': 1000, 'clay': 1000, ...}
        """
        log.info(f"[{self.username}] Attempting to send {runs} run(s) of resources from village {from_village_id} to ({target_x}|{target_y}).")

        payload_resources = {
            "lumber": resources.get('lumber', 0),
            "clay": resources.get('clay', 0),
            "iron": resources.get('iron', 0),
            "crop": resources.get('crop', 0)
        }

        payload = {
            "action": "marketPlace",
            "resources": payload_resources,
            "destination": {
                "x": int(target_x),
                "y": int(target_y)
            },
            "runs": int(runs),
            "useTradeShips": False
        }

        try:
            api_url = f"{self.server_url}/api/v1/marketplace/resources/send"
            headers = {
                'X-Version': self.server_version,
                'X-Requested-With': 'XMLHttpRequest',
                'Content-Type': 'application/json; charset=UTF-8',
                'Accept': 'application/json, text/javascript, */*; q=0.01',
                'Referer': f"{self.server_url}/build.php?t=5"
            }
            
            resp = self.sess.put(api_url, json=payload, headers=headers, timeout=15)
            resp_data = resp.json()

            if resp.status_code == 200 and "duration" in resp_data:
                duration = resp_data.get('duration')
                log.info(f"[{self.username}] Successfully sent resources to ({target_x}|{target_y}). Travel time: {duration}s.")
                return True
            else:
                log.error(f"[{self.username}] Failed to send resources. Server responded with status {resp.status_code}: {resp.text}")
                return False

        except Exception as e:
            log.error(f"[{self.username}] A critical error occurred while sending resources: {e}", exc_info=True)
            return False

    def get_map_data(self, x: int, y: int) -> Optional[Dict[str, Any]]:
            """Fetches map data for a 7x7 grid centered on the given coordinates."""
            log.info(f"[{self.username}] Fetching map data around ({x}|{y}).")
            try:
                api_url = f"{self.server_url}/api/v1/map/position"
                payload = {
                    "data": {
                        "x": str(x),
                        "y": str(y),
                        "zoomLevel": 3,
                        "ignorePositions": []
                    }
                }
                headers = {
                    'X-Version': self.server_version,
                    'X-Requested-With': 'XMLHttpRequest',
                    'Content-Type': 'application/json; charset=UTF-8'
                }
                resp = self.sess.post(api_url, json=payload, headers=headers, timeout=20)
                return resp.json()
            except Exception as e:
                log.error(f"[{self.username}] Failed to fetch map data: {e}", exc_info=True)
                return None
            
    def get_all_villages(self) -> List[Dict[str, Any]]:
        """Fetches the complete list of villages from the sidebar."""
        try:
            resp = self.sess.get(f"{self.server_url}/dorf1.php", timeout=15)
            parsed_data = self.parse_village_page(resp.text, "dorf1")
            return parsed_data.get("villages", [])
        except Exception as e:
            log.error(f"[{self.username}] Failed to get all villages: {e}")
            return []

    def train_settlers(self, village_id: int, building_gid: int, amount: int) -> Tuple[bool, int]:
        """Trains a specific amount of settlers and returns the total queue time."""
        log.info(f"[{self.username}] Attempting to train {amount} settlers in village {village_id} from GID {building_gid}.")
        try:
            # Find the location ID of the building (Residence/Palace)
            dorf2_resp = self.sess.get(f"{self.server_url}/dorf2.php?newdid={village_id}", timeout=15)
            dorf2_soup = BeautifulSoup(dorf2_resp.text, 'html.parser')
            building_slot = dorf2_soup.select_one(f'.buildingSlot.g{building_gid}')
            if not building_slot or not building_slot.has_attr('data-aid'):
                log.error(f"[{self.username}] Could not find building GID {building_gid} in village {village_id}.")
                return False, 0
            location_id = building_slot['data-aid']

            # Get the training page to find the settler info
            train_page_url = f"{self.server_url}/build.php?newdid={village_id}&gid={building_gid}&s=1"
            train_page_resp = self.sess.get(train_page_url, timeout=15)
            train_soup = BeautifulSoup(train_page_resp.text, 'html.parser')

            form = train_soup.find('form', {'name': 'snd'})
            if not form:
                log.error(f"[{self.username}] Could not find training form for settlers.")
                return False, 0

            # Find the time per settler
            time_per_settler = 0
            duration_div = train_soup.find('div', class_='duration')
            if duration_div:
                time_str_match = re.search(r'(\d{2}):(\d{2}):(\d{2})', duration_div.text)
                if time_str_match:
                    h, m, s = map(int, time_str_match.groups())
                    time_per_settler = h * 3600 + m * 60 + s

            if time_per_settler == 0:
                log.warning(f"[{self.username}] Could not parse time per settler. Cannot calculate total duration.")
                # We can proceed but the wait time will be incorrect
            
            # Prepare and send the training request
            payload = {i['name']: i['value'] for i in form.find_all('input', {'type': 'hidden'})}
            settler_input = train_soup.find('input', {'name': re.compile(r't\d0')}) # Matches t10, t20, t30
            if not settler_input:
                log.error(f"[{self.username}] Could not find settler input field on training page.")
                return False, 0

            payload[settler_input['name']] = str(amount)
            payload['s1'] = 'ok'

            post_url = urljoin(self.server_url, form['action'])
            resp = self.sess.post(post_url, data=payload, headers={'Referer': train_page_url})
            
            # Check for success and calculate total duration
            if "in training" in resp.text.lower() or "duration" in resp.text.lower():
                log.info(f"[{self.username}] Successfully queued {amount} settlers for training.")
                
                # Get current queue duration
                soup = BeautifulSoup(resp.text, 'html.parser')
                current_queue_duration = 0
                if queue_table := soup.find('table', class_='under_progress'):
                    timers = queue_table.select('td.dur span.timer')
                    if timers:
                        # The last timer is the total duration of the queue
                        current_queue_duration = int(timers[-1].get('value', 0))

                # This is a fallback calculation in case the queue parsing is tricky
                calculated_duration = time_per_settler * amount
                
                # The most reliable duration is the one parsed from the updated queue
                final_duration = current_queue_duration if current_queue_duration > 0 else calculated_duration

                if final_duration > 0:
                    log.info(f"[{self.username}] Total training time for settlers is now {final_duration}s.")
                else:
                    log.warning(f"[{self.username}] Could not determine settler training time, will use a default wait.")
                    final_duration = 300 # Default to 5 minutes as a safe fallback

                return True, final_duration
            else:
                log.error(f"[{self.username}] Failed to queue settlers. Response might indicate lack of resources or other issues.")
                return False, 0

        except Exception as e:
            log.error(f"[{self.username}] An error occurred during train_settlers: {e}", exc_info=True)
            return False, 0

    def send_settlers(self, from_village_id: int, target_coords: Dict[str, int]) -> Tuple[bool, int]:
        """Sends 3 settlers to found a new village at the given coordinates."""
        log.info(f"[{self.username}] Initiating settlement at ({target_coords['x']}|{target_coords['y']}) from village {from_village_id}.")
        try:
            # --- START OF CORRECTION: Use the correct map size and formula for kid calculation ---
            map_size = 200 
            width = 2 * map_size + 1
            # This is the standard formula used by Travian to convert coordinates to a map ID (kid)
            kid = (map_size - target_coords['y']) * width + (target_coords['x'] + map_size) + 1
            # --- END OF CORRECTION ---

            # Step 1: GET the rally point confirmation page with the correct kid
            confirm_url = f"{self.server_url}/build.php?id=39&tt=2&kid={kid}&a=6"
            confirm_resp = self.sess.get(confirm_url, timeout=15)
            confirm_soup = BeautifulSoup(confirm_resp.text, 'html.parser')

            form = confirm_soup.find('form', {'action': re.compile(r'build\.php')})
            if not form:
                log.error(f"[{self.username}] Could not find settlement confirmation form. This may be due to an incorrect kid, or not enough resources/settlers.")
                return False, 0
            
            payload = {i['name']: i['value'] for i in form.find_all('input', {'type': 'hidden'})}
            payload['s1'] = 'Send'
            
            travel_time = 0
            arrival_info = confirm_soup.select_one('.in')
            if arrival_info:
                time_match = re.search(r'(\d+):(\d{2}):(\d{2})', arrival_info.text)
                if time_match:
                    h, m, s = map(int, time_match.groups())
                    travel_time = h * 3600 + m * 60 + s

            # Step 2: POST to send the settlers
            post_url = urljoin(self.server_url, form['action'])
            final_resp = self.sess.post(post_url, data=payload, headers={'Referer': confirm_url})

            if "troops are on their way" in final_resp.text:
                log.info(f"[{self.username}] Settlement mission successfully sent. Travel time: {travel_time}s")
                return True, travel_time
            else:
                log.error(f"[{self.username}] Final settlement send request failed.")
                return False, 0

        except Exception as e:
            log.error(f"[{self.username}] A critical error occurred during send_settlers: {e}", exc_info=True)
            return False, 0
    
    def send_catapult_waves(self, from_village_id: int, target_x: int, target_y: int, troops: Dict[str, int], waves: int, target_gid: int = 27) -> bool:
        """
        Sends multiple waves of catapults to a specific target. The first wave includes 200 rams.
        """
        log.info(f"[{self.username}] Preparing to send {waves} catapult waves from village {from_village_id} to ({target_x}|{target_y}).")

        try:
            for i in range(waves):
                log.info(f"[{self.username}] Preparing wave {i + 1}/{waves}...")

                current_wave_troops = troops.copy()
                if i == 0:
                    current_wave_troops['t7'] = current_wave_troops.get('t7', 0) + 200
                    log.info(f"[{self.username}] This is the first wave, adding 200 rams.")

                first_post_params = {'x': str(target_x), 'y': str(target_y), 'eventType': '3', 'ok': 'ok'}
                for troop_type, count in current_wave_troops.items():
                    first_post_params[f"troop[{troop_type}]"] = str(count)

                confirm_resp = self.sess.post(f"{self.server_url}/build.php?id=39&tt=2", data=first_post_params, timeout=15)
                confirm_soup = BeautifulSoup(confirm_resp.text, 'html.parser')

                form = confirm_soup.find('form', id='troopSendForm')
                if not form:
                    log.error(f"[{self.username}] Wave {i + 1}: Could not find confirmation form. Aborting.")
                    return False

                final_payload = {inp.get('name'): inp.get('value') for inp in form.find_all('input', {'name': True})}

                final_payload['troops[0][catapultTarget1]'] = str(target_gid)
                if confirm_soup.select_one('select[name="troops[0][catapultTarget2]"]'):
                    final_payload['troops[0][catapultTarget2]'] = str(target_gid)

                final_action_url = urljoin(self.server_url, form['action'])
                final_resp = self.sess.post(final_action_url, data=final_payload, timeout=15)

                if "troops are on their way" not in final_resp.text:
                    log.error(f"[{self.username}] Wave {i + 1} failed to send. Server response did not confirm attack.")
                    error_msg = confirm_soup.select_one('.error, .warning')
                    if error_msg:
                        log.error(f"[{self.username}] Server error message: {error_msg.text.strip()}")
                    return False

                log.info(f"[{self.username}] Wave {i + 1}/{waves} sent successfully.")

                if i < waves - 1:
                    time.sleep(0.25)

            log.info(f"[{self.username}] All {waves} waves have been sent successfully.")
            return True

        except Exception as e:
            log.error(f"[{self.username}] A critical error occurred during send_catapult_waves: {e}", exc_info=True)
            return False
        
    def get_home_troops(self, village_id: int) -> Dict[str, int]:
        """
        Fetches the rally point to get a list of all troops currently in the village.
        Returns a dictionary mapping troop name (e.g., 'Clubswinger', 'Settler') to its count.
        """
        log.info(f"[{self.username}] Checking for home troops in village {village_id}.")
        troops = {}
        try:
            url = f"{self.server_url}/build.php?newdid={village_id}&gid=16&tt=1"
            resp = self.sess.get(url, timeout=15)
            soup = BeautifulSoup(resp.text, 'html.parser')

            troop_table = soup.find('table', class_='troop_details')
            if not troop_table:
                log.warning(f"[{self.username}] Could not find troop_details table in rally point for village {village_id}.")
                return {}

            unit_row = troop_table.select_one('tbody.units')
            count_row = troop_table.select_one('tbody.units.last')

            if not unit_row or not count_row:
                log.warning(f"[{self.username}] Could not find unit/count rows in troop_details table for village {village_id}.")
                return {}

            unit_icons = unit_row.select('img.unit')
            unit_counts = count_row.select('td.unit')

            for i, icon in enumerate(unit_icons):
                troop_name = icon.get('alt', '').strip()
                if not troop_name:
                    continue
                
                if i < len(unit_counts):
                    count_text = unit_counts[i].text.strip().replace(',', '')
                    if count_text.isdigit():
                        troops[troop_name] = int(count_text)
                    else:
                        troops[troop_name] = 0
            
            log.info(f"[{self.username}] Found troops: {troops}")
            return troops

        except Exception as e:
            log.error(f"[{self.username}] An error occurred while getting home troops for village {village_id}: {e}", exc_info=True)
            return {}