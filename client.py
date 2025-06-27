import re
import json
import requests
import time
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