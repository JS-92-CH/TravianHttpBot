import re
import requests
import logging
import json
from typing import Dict, Any, Optional
from bs4 import BeautifulSoup

from config import log

class TravianClient:
    """Lightweight HTTP wrapper around the Travian *HTML* and JSON endpoints."""

    def __init__(self, username: str, password: str, server_url: str):
        self.username = username
        self.password = password
        self.server_url = server_url.rstrip("/")
        self.sess = requests.Session()
        self.sess.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/108.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.9",
        })

    def login(self) -> bool:
        log.info("[%s] Attempting API login to Vardom server...", self.username)
        try:
            login_page_url = f"{self.server_url}/"
            login_page_resp = self.sess.get(login_page_url, timeout=15)
            login_page_resp.raise_for_status()

            soup = BeautifulSoup(login_page_resp.text, 'html.parser')
            server_version = "2554.3"
            link_tag = soup.find("link", href=re.compile(r"gpack\.vardom\.net/([\d\.]+)/"))
            if link_tag and (match := re.search(r"gpack\.vardom\.net/([\d\.]+)/", link_tag['href'])):
                server_version = match.group(1)
            
            log.info("[%s] Using server version: %s", self.username, server_version)

            api_url = f"{self.server_url}/api/v1/auth/login"
            headers = {'X-Version': server_version, 'X-Requested-With': 'XMLHttpRequest'}
            payload = {"name": self.username, "password": self.password, "w": "1920:1080", "mobileOptimizations": False}
            
            resp = self.sess.post(api_url, json=payload, headers=headers, timeout=15)
            resp.raise_for_status()
            response_json = resp.json()

            if response_json.get("redirectTo") == "dorf1.php":
                log.info("[%s] Logged in successfully ✔", self.username)
                return True
            else:
                log.error("[%s] Login failed. Server response: %s", self.username, response_json)
                return False
        except Exception as exc:
            log.error("[%s] Login process failed with an exception: %s", self.username, exc)
            return False

    def parse_village_page(self, html: str) -> Dict[str, Any]:
        soup = BeautifulSoup(html, "html.parser")
        out: Dict[str, Any] = {"resources":{},"storage":{},"production":{},"buildings":[],"queue":[],"villages":[]}

        try:
            script_text = soup.find("script", string=re.compile(r"var\s+resources\s*="))
            if script_text and (match := re.search(r"var\s+resources\s*=\s*(\{.*?\});", script_text.string, re.DOTALL)):
                json_str = re.sub(r"([a-zA-Z_][\w]*)\s*:", r'"\1":', match.group(1))
                res_data = json.loads(json_str)
                out.update({
                    "resources": {k: int(v) for k, v in res_data.get("storage", {}).items()},
                    "storage": {k: int(v) for k, v in res_data.get("maxStorage", {}).items()},
                    "production": {k: int(v) for k, v in res_data.get("production", {}).items()}
                })
        except Exception as exc:
            log.debug(f"Resource javascript parser failed – {exc}")

        found_buildings = {}
        for slot in soup.select('#resourceFieldContainer > a.level, div.buildingSlot'):
            loc_id, gid, name = None, None, None
            
            # Method for dorf2 buildings
            if slot.has_attr('data-aid'):
                loc_id = int(slot['data-aid'])
                gid = int(slot.get('data-gid', 0))
                name = slot.get('data-name')
                level_tag = slot.select_one(f'a.aid{loc_id} .labelLayer')
                level = int(level_tag.text.strip()) if level_tag else 0
            # Method for dorf1 resource fields
            elif 'build.php?id=' in slot.get('href', ''):
                loc_id = int(re.search(r'id=(\d+)', slot['href']).group(1))
                gid_class = next((c for c in slot.get('class', []) if c.startswith('g') and c[1:].isdigit()), None)
                gid = int(gid_class[1:]) if gid_class else 0
                name = slot.get('title', '').split(' Level')[0]
                level = int(slot.text.strip()) if slot.text.strip().isdigit() else 0
            
            if loc_id and gid:
                found_buildings[loc_id] = {'id': loc_id, 'gid': gid, 'level': level, 'name': name or f"GID {gid}"}

        out['buildings'] = list(found_buildings.values())
        log.debug(f"Parser found {len(out['buildings'])} buildings/fields.")

        for li in soup.select(".buildingList li"):
            if name_div := li.find("div", class_="name"):
                out["queue"].append({
                    "name": name_div.text.split("\n")[0].strip(),
                    "level": li.find("span", class_="lvl").text.strip(),
                    "eta": int(li.find("span", class_="timer")["value"]),
                })

        for v_entry in soup.select("#sidebarBoxVillageList .listEntry"):
            if link := v_entry.find("a", href=re.compile(r"newdid=")):
                out["villages"].append({
                    "id": int(re.search(r"newdid=(\d+)", link["href"]).group(1)),
                    "name": v_entry.find("span", class_="name").text.strip(),
                    "active": "active" in v_entry.get("class", []),
                })
        return out

    def fetch_and_parse_village(self, village_id: int) -> Optional[Dict[str, Any]]:
        """Fetch dorf1 and dorf2 for a village and return combined data."""
        log.info("[%s] Fetching data for village %d", self.username, village_id)
        village_data = {}
        try:
            url_d1 = f"{self.server_url}/dorf1.php?newdid={village_id}"
            resp_d1 = self.sess.get(url_d1, timeout=15)
            resp_d1.raise_for_status()
            village_data = self.parse_village_page(resp_d1.text)
        except Exception as exc:
            log.error("[%s] Failed to fetch/parse dorf1 for village %d: %s", self.username, village_id, exc)
            return None

        try:
            url_d2 = f"{self.server_url}/dorf2.php?newdid={village_id}"
            resp_d2 = self.sess.get(url_d2, timeout=15)
            resp_d2.raise_for_status()
            parsed_d2 = self.parse_village_page(resp_d2.text)
            
            existing_ids = {b['id'] for b in village_data.get("buildings", [])}
            for building in parsed_d2.get("buildings", []):
                if building['id'] not in existing_ids:
                    village_data.setdefault("buildings", []).append(building)
            village_data["queue"] = parsed_d2.get("queue", [])
        except Exception as exc:
            log.warning("[%s] Failed to fetch/parse dorf2 for village %d: %s", self.username, village_id, exc)

        return village_data