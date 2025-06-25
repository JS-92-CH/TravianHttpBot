import re
import json
import requests
import logging
from typing import Dict, Any, Optional, List
from bs4 import BeautifulSoup
from urllib.parse import urljoin

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

    def get_prerequisites(self, village_id: int, slot_id: int, target_gid: int) -> List[Dict[str, Any]]:
        prereqs = []
        build_page_url = f"{self.server_url}/build.php?newdid={village_id}&id={slot_id}"
        try:
            resp = self.sess.get(build_page_url, timeout=15)
            soup = BeautifulSoup(resp.text, 'html.parser')
            
            contract_div = soup.find('div', id=f'contract_building{target_gid}') or soup.find('div', class_='upgradeBuilding')
            if contract_div:
                error_spans = contract_div.select('.contractLink span.error, .upgradeBlocked span.error')
                for span in error_spans:
                    text = span.get_text(separator=' ', strip=True).lower()
                    level_match = re.search(r'(?:level|stufe)\s(\d+)', text)
                    if not level_match: continue
                    
                    required_level = int(level_match.group(1))
                    building_name = re.sub(r'(?:level|stufe)\s\d+', '', text).strip()
                    required_gid = NAME_TO_GID.get(building_name)
                    
                    if required_gid:
                        prereqs.append({'gid': required_gid, 'level': required_level})
        except requests.RequestException: pass
        return prereqs

    def initiate_build(self, village_id: int, slot_id: int, gid: int, is_new_build: bool) -> Dict[str, Any]:
        log.info(f"[{self.username}] Attempting to build GID {gid_name(gid)} ({gid}) at slot {slot_id} (New Build: {is_new_build})")
        action_url, build_page_url = None, f"{self.server_url}/build.php?newdid={village_id}&id={slot_id}"
        
        try:
            if is_new_build:
                for category in [1, 2, 3]:
                    if action_url: break
                    resp = self.sess.get(f"{build_page_url}&category={category}", timeout=15)
                    soup = BeautifulSoup(resp.text, 'html.parser')
                    
                    # --- Start of fix ---
                    wrapper = None
                    if img_tag := soup.find('img', class_=f'g{gid}'):
                        wrapper = img_tag.find_parent('div', class_='buildingWrapper')
                    
                    if wrapper:
                    # --- End of fix ---
                        if button := wrapper.find('button', class_='green', disabled=False):
                            if match := re.search(r"window\.location\.href\s*=\s*'([^']+)'", button.get('onclick', '')):
                                action_url = urljoin(self.server_url, match.group(1).replace('&amp;', '&'))
                                if 'newdid=' not in action_url:
                                    sep = '&' if '?' in action_url else '?'
                                    action_url = f"{action_url}{sep}newdid={village_id}"
                if not action_url: return {'status': 'error', 'reason': f'Could not find build button for GID {gid}'}
            else: # Upgrade
                resp = self.sess.get(build_page_url, timeout=15)
                soup = BeautifulSoup(resp.text, 'html.parser')
                if err_msg := soup.select_one(".upgradeBlocked .errorMessage"):
                    log.warning(f"Build blocked by message: {err_msg.text.strip()}")
                    return {'status': 'error', 'reason': 'prerequisites_not_met_or_res_low'}
                if button := soup.find('button', class_=re.compile(r'\b(green|build)\b'), disabled=False):
                    if match := re.search(r"window\.location\.href\s*=\s*'([^']+)'", button.get('onclick', '')):
                        action_url = urljoin(self.server_url, match.group(1).replace('&amp;', '&'))
                        if 'newdid=' not in action_url:
                            sep = '&' if '?' in action_url else '?'
                            action_url = f"{action_url}{sep}newdid={village_id}"
                if not action_url: return {'status': 'error', 'reason': 'Could not find upgrade button'}

            log.info(f"[{self.username}] Executing build action URL: {action_url}")
            confirmation_resp = self.sess.get(action_url, timeout=15)
            conf_soup = BeautifulSoup(confirmation_resp.text, 'html.parser')
            
            if conf_soup.select_one(".buildingList .timer"):
                    log.info(f"[{self.username}] Successfully initiated build for {gid_name(gid)}.")
                    return {'status': 'success'}
            else:
                    log.warning(f"[{self.username}] Build command sent, but couldn't confirm in response page.")
                    debug_filename = f"debug_fail_{village_id}_{gid_name(gid).replace(' ','')}.html"
                    with open(debug_filename, "w", encoding="utf-8") as f: f.write(conf_soup.prettify())
                    log.error(f"Saved failed confirmation page to '{debug_filename}' for inspection.")
                    return {'status': 'error', 'reason': 'confirmation_failed_or_res_low'}
        except requests.RequestException as e:
            return {'status': 'error', 'reason': f'Network error: {e}'}
        
    def use_hero_resources_for_shortfall(self, village_id: int, required_resources: Dict[str, int]) -> bool:
        """
        The core logic to automatically use hero resources to cover a deficit.
        """
        log.info(f"[{self.username}] Checking hero inventory to cover resource shortfall for village {village_id}.")

        # 1. Get current village state
        village_data = self.fetch_and_parse_village(village_id)
        if not village_data:
            log.error(f"[{self.username}] Could not get village data for hero resource logic.")
            return False

        current_resources = village_data.get('resources', {})
        storage_capacity = village_data.get('storage', {})
        # Map resources to l1, l2, etc. format if needed
        resource_map = {'lumber': 'l1', 'clay': 'l2', 'iron': 'l3', 'crop': 'l4'}
        current_res_mapped = {
            'lumber': current_resources.get('l1', 0),
            'clay': current_resources.get('l2', 0),
            'iron': current_resources.get('l3', 0),
            'crop': current_resources.get('l4', 0)
        }
        storage_cap_mapped = {
            'lumber': storage_capacity.get('l1', 0),
            'clay': storage_capacity.get('l2', 0),
            'iron': storage_capacity.get('l3', 0),
            'crop': storage_capacity.get('l4', 0)
        }


        # 2. Fetch and Parse Hero Inventory Data
        hero_items = {}
        try:
            hero_url = f"{self.server_url}/hero"
            resp = self.sess.get(hero_url, timeout=15)
            soup = BeautifulSoup(resp.text, 'html.parser')
            script_tag = soup.find("script", string=re.compile(r"HeroV2\.render"))
            if not script_tag:
                log.warning(f"[{self.username}] Could not find HeroV2.render script tag.")
                return False

            match = re.search(r"HeroV2\.render\s*\(\s*(\{.*\}),\s*\{\}\);", script_tag.string, re.DOTALL)
            if not match:
                log.warning(f"[{self.username}] Could not extract hero JSON from script.")
                return False

            hero_json_str = match.group(1)
            hero_data = json.loads(hero_json_str)
            inventory = hero_data.get("screenData", {}).get("viewData", {}).get("itemsInventory", [])

            resource_types = ['lumber', 'clay', 'iron', 'crop']
            for item in inventory:
                if item.get('name') in resource_types:
                    hero_items[item['name']] = {'id': item['id'], 'amount': int(item['amount'])}
            log.info(f"[{self.username}] Found hero items: {hero_items}")

        except Exception as e:
            log.error(f"[{self.username}] Failed to fetch or parse hero inventory: {e}", exc_info=True)
            return False

        # 3. Calculate Deficit and Execute API Calls
        used_items = False
        for res_name, res_code in resource_map.items():
            deficit = required_resources.get(res_name, 0) - current_res_mapped.get(res_name, 0)
            if deficit <= 0:
                continue

            if res_name in hero_items:
                hero_has = hero_items[res_name]['amount']
                room_in_storage = storage_cap_mapped[res_name] - current_res_mapped[res_name]

                amount_to_use = min(deficit, hero_has, room_in_storage)

                if amount_to_use > 0:
                    log.info(f"[{self.username}] Using {amount_to_use} {res_name} from hero inventory.")
                    api_url = f"{self.server_url}/api/v1/hero/v2/inventory/use-item"
                    payload = {
                        "itemId": hero_items[res_name]['id'],
                        "amount": amount_to_use,
                        "villageId": village_id
                    }
                    headers = {
                        'X-Version': self.server_version,
                        'X-Requested-With': 'XMLHttpRequest',
                        'Content-Type': 'application/json; charset=UTF-8'
                    }
                    try:
                        api_resp = self.sess.post(api_url, json=payload, headers=headers, timeout=15)
                        if api_resp.status_code == 200:
                            log.info(f"[{self.username}] Successfully used hero item.")
                            used_items = True
                        else:
                            log.error(f"[{self.username}] API call to use hero item failed with status {api_resp.status_code}: {api_resp.text}")
                    except Exception as e:
                        log.error(f"[{self.username}] Exception during hero item API call: {e}")

        return used_items

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
            return village_data
        except requests.RequestException as e:
            log.error(f"Network error fetching village data for {village_id}: {e}")
        except Exception as e:
            log.error(f"An unexpected error occurred in fetch_and_parse_village for {village_id}: {e}", exc_info=True)
        return None

    def parse_village_page(self, html: str, page_type: str) -> Dict[str, Any]:
        soup = BeautifulSoup(html, "html.parser")
        out: Dict[str, Any] = {"resources": {}, "storage": {}, "production": {}, "buildings": [], "queue": [], "villages": []}
        try:
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