import re
import json
import requests
import time
import logging
from typing import Dict, Any, Optional, List, Tuple
from bs4 import BeautifulSoup
from urllib.parse import urljoin, parse_qs, urlencode

from config import log, gid_name, NAME_TO_GID

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
        log.info("[%s] Attempting API login to Vardom server...", self.username)
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
            payload = {"name": self.username, "password": self.password, "w": "1920:1080", "mobileOptimizations": False}
            resp = self.sess.post(api_url, json=payload, headers=headers, timeout=15)
            if resp.json().get("redirectTo") == "dorf1.php":
                log.info("[%s] Logged in successfully ✔", self.username)
                return True
        except Exception as exc:
            log.error("[%s] Login process failed with an exception: %s", self.username, exc)
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
                out["queue"].append({"name":name_div.text.strip(),"level":lvl_span.text.strip(),"eta":int(timer_span.get("value",0))})
        for v_entry in soup.select("#sidebarBoxVillageList .listEntry"):
            if link := v_entry.find("a",href=re.compile(r"newdid=")):
                out["villages"].append({"id":int(re.search(r"newdid=(\d+)",link["href"]).group(1)),"name":v_entry.find("span",class_="name").text.strip(),"active":"active" in v_entry.get("class",[])})
        return out

    # --- NEW METHODS FOR TRAINING AGENT ---

    def get_hero_initial_location(self) -> Optional[int]:
        """Fetches /hero/attributes to find the hero's current village ID on startup."""
        try:
            hero_page_url = f"{self.server_url}/hero/attributes"
            resp = self.sess.get(hero_page_url, timeout=15)
            soup = BeautifulSoup(resp.text, 'html.parser')
            state_div = soup.find('div', class_='heroState')
            if state_div:
                link = state_div.find('a', href=re.compile(r'd=\d+'))
                if link and (match := re.search(r'd=(\d+)', link['href'])):
                    village_id = int(match.group(1))
                    log.info(f"[{self.username}] Hero initial location found in village: {village_id}")
                    return village_id
            log.warning(f"[{self.username}] Could not determine hero's initial location from attributes page.")
            return None
        except Exception as e:
            log.error(f"[{self.username}] Error fetching hero's initial location: {e}")
            return None

    def get_hero_status(self) -> Dict[str, Any]:
        """Fetches hero status, including current location and movement status."""
        try:
            resp = self.sess.get(f"{self.server_url}/hero.php", timeout=15)
            soup = BeautifulSoup(resp.text, 'html.parser')
            
            # Find the script containing the hero data
            script_tag = soup.find("script", string=re.compile(r"window\.Travian\.React\.HeroV2\.render"))
            if not script_tag:
                # Fallback for when hero is not home, check sidebar
                sidebar_hero = soup.select_one("#sidebarBoxHero")
                if sidebar_hero and sidebar_hero.select_one(".heroStatusMessage .duration"):
                     timer = sidebar_hero.select_one(".heroStatusMessage .duration .timer")
                     return {'village_id': None, 'is_moving': True, 'travel_time': int(timer.get('value', 60))}
                return {'village_id': None, 'is_moving': True, 'travel_time': 60} # Default assumption

            json_str = re.search(r"screenData:\s*(\{.*\})\s*,\s*viewData:", script_tag.string, re.DOTALL)
            if not json_str: return {}
            
            data = json.loads(json_str.group(1))
            hero_data = data.get('hero', {})
            
            return {
                'village_id': int(hero_data.get('villageId', 0)),
                'is_moving': hero_data.get('status') != 'home',
                'travel_time': 0 # Cannot get from here, assume 0 if at home
            }
        except Exception as e:
            log.error(f"[{self.username}] Could not get hero status: {e}")
            return {'village_id': None, 'is_moving': False}

    def move_hero(self, current_village_id: int, target_x: int, target_y: int) -> Tuple[bool, int]:
        """Moves the hero from its current village to the target coordinates."""
        try:
            rally_point_url = f"{self.server_url}/build.php?newdid={current_village_id}&gid=16&tt=2"
            self.sess.get(rally_point_url, timeout=15) # Establish referer

            initial_post_url = f"{self.server_url}/build.php?gid=16&tt=2&newdid={current_village_id}"
            initial_payload = {
                'troop[t11]': '1', 'x': str(target_x), 'y': str(target_y),
                'eventType': '2', 'redeployHero': '1', 'ok': 'ok'
            }
            resp1 = self.sess.post(initial_post_url, data=initial_payload, timeout=15, allow_redirects=True)
            soup1 = BeautifulSoup(resp1.text, 'html.parser')

            confirm_form = soup1.find('form', id='troopSendForm')
            if not confirm_form:
                log.error(f"[{self.username}] Could not find hero move confirmation form.")
                return False, 0

            confirm_url = urljoin(self.server_url, confirm_form['action'])
            confirm_payload = {i['name']: i['value'] for i in confirm_form.find_all('input') if i.has_attr('name')}

            arrival_timer = soup1.select_one("#content .at .timer")
            travel_time = (int(arrival_timer['value']) - int(time.time())) if arrival_timer else 60

            self.sess.post(confirm_url, data=confirm_payload, timeout=15)
            # The final confirmation doesn't give a clear success message,
            # so we assume success if we get this far.
            log.info(f"[{self.username}] Hero movement confirmation sent.")
            return True, travel_time

        except Exception as e:
            log.error(f"[{self.username}] An error occurred while moving the hero: {e}", exc_info=True)
            return False, 0

    def get_training_page(self, village_id: int, gid: int) -> Optional[Dict]:
        """Fetches and parses a troop training page (barracks, stable, etc.)."""
        try:
            url = f"{self.server_url}/build.php?newdid={village_id}&gid={gid}"
            resp = self.sess.get(url, timeout=15)
            soup = BeautifulSoup(resp.text, 'html.parser')

            if "no troops for this building have been researched yet" in resp.text.lower():
                return {'trainable': []} # Return empty but valid dict

            form = soup.find('form', {'name': 'snd'})
            if not form: return None

            build_id_match = re.search(r'build\.php\?id=(\d+)', form['action'])
            build_id = int(build_id_match.group(1)) if build_id_match else 0
            form_data = {i['name']: i['value'] for i in form.find_all('input') if i.has_attr('name')}
            total_queue_duration = sum(int(timer.get('value',0)) for timer in soup.select("table.under_progress .timer"))

            trainable_units = []
            for action_div in soup.select(".buildActionOverview .action"):
                details = action_div.find('div', class_='details')
                if not details: continue

                name_link = details.select_one(".tit > a:nth-of-type(2)")
                if not name_link: continue
                unit_name = name_link.text.strip()

                input_tag = details.find('input', {'type': 'text'})
                if not input_tag: continue
                unit_id = int(re.sub(r'\D', '', input_tag['name']))

                max_link = details.select_one('.cta a')
                max_amount = int(re.sub(r'\D', '', max_link.text)) if max_link else 0

                time_str = "0:0:0"
                if (dur_span := details.find('div', class_='duration')):
                     if (val_span := dur_span.find('span', class_='value')):
                         time_str = val_span.text.split('(')[0].strip()
                h,m,s = map(int, time_str.split(':'))
                time_per_unit = h*3600 + m*60 + s

                trainable_units.append({'id': unit_id, 'name': unit_name, 'max_trainable': max_amount, 'time_per_unit': time_per_unit})

            return {
                'build_id': build_id, 'form_data': form_data,
                'queue_duration_seconds': total_queue_duration, 'trainable': trainable_units
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