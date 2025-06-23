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
        log.info("[%s] Logging in...", self.username)
        try:
            self.sess.get(f"{self.server_url}/")
        except requests.RequestException as exc:
            log.error("[%s] Cannot reach server: %s", self.username, exc)
            return False

        api_url = f"{self.server_url}/api/v1/auth/login"
        payload = {"name": self.username, "password": self.password}
        try:
            resp = self.sess.post(api_url, json=payload, timeout=15)
            resp.raise_for_status()
            if "token" not in resp.json().get("data", {}):
                raise ValueError("API did not return a token → bad credentials?")
            log.info("[%s] Logged in successfully ✔", self.username)
            return True
        except Exception as exc:
            log.error("[%s] Login failed ✖ – %s", self.username, exc)
            return False

    def parse_village_page(self, html: str) -> Dict[str, Any]:
        """Extract resources, buildings & queued upgrades from a village page HTML."""
        soup = BeautifulSoup(html, "html.parser")
        out: Dict[str, Any] = {
            "resources": {}, "storage": {}, "production": {},
            "buildings": [], "queue": [], "villages": [],
        }

        try:
            script_text = soup.find("script", string=re.compile(r"var\s+resources\s*="))
            if script_text:
                match = re.search(r"var\s+resources\s*=\s*(\{.*?\});", script_text.string, re.DOTALL)
                if match:
                    json_str = re.sub(r"([a-zA-Z_][\w]*)\s*:", r'"\1":', match.group(1))
                    res_data = json.loads(json_str)
                    out["resources"] = {k: int(v) for k, v in res_data.get("storage", {}).items()}
                    out["storage"] = {k: int(v) for k, v in res_data.get("maxStorage", {}).items()}
                    out["production"] = {k: int(v) for k, v in res_data.get("production", {}).items()}
        except Exception as exc:
            log.debug("Resource parser failed – %s", exc)

        for link in soup.select("#resourceFieldContainer a, #villageContent a.buildingSlot"):
            if "build.php?id=" not in link.get("href", ""):
                 continue
            lvl_div = link.find("div", class_="labelLayer")
            if not lvl_div: continue
            
            gid_cls = next((c for c in link.get("class", []) if c.startswith("g") and c[1:].isdigit()), None)
            if not gid_cls:
                 parent_div = link.find_parent('div', class_='buildingSlot')
                 if parent_div: gid_cls = next((c for c in parent_div.get("class", []) if c.startswith("g") and c[1:].isdigit()), None)

            if gid_cls:
                out["buildings"].append({
                    "id": int(re.search(r"id=(\d+)", link["href"]).group(1)),
                    "level": int(lvl_div.text.strip()),
                    "gid": int(gid_cls[1:]),
                })

        for b_slot in soup.select("#villageContent > .buildingSlot"):
            if b_slot.get('data-gid') and b_slot.get('data-aid'):
                level_tag = b_slot.select_one('a.level')
                level = int(level_tag.get('data-level')) if level_tag and level_tag.get('data-level') else 0
                if not any(b['id'] == int(b_slot['data-aid']) for b in out['buildings']):
                     out["buildings"].append({ "id": int(b_slot['data-aid']), "level": level, "gid": int(b_slot['data-gid']) })

        for li in soup.select(".buildingList li"):
            name_div = li.find("div", class_="name")
            if not name_div: continue
            out["queue"].append({
                "name": name_div.text.split("\n")[0].strip(),
                "level": li.find("span", class_="lvl").text.strip(),
                "eta": int(li.find("span", class_="timer")["value"]),
            })

        for v_entry in soup.select("#sidebarBoxVillageList .listEntry"):
            link = v_entry.find("a", href=re.compile(r"newdid="))
            if not link: continue
            out["villages"].append({
                "id": int(re.search(r"newdid=(\d+)", link["href"]).group(1)),
                "name": v_entry.find("span", class_="name").text.strip(),
                "active": "active" in v_entry.get("class", []),
            })
        return out

    def fetch_and_parse_village(self, village_id: int) -> Optional[Dict[str, Any]]:
        """Fetch dorf1 and dorf2 for a village and return combined data."""
        log.info("[%s] Fetching data for village %d", self.username, village_id)
        
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
                    village_data["buildings"].append(building)
            village_data["queue"] = parsed_d2.get("queue", [])
        except Exception as exc:
            log.warning("[%s] Failed to fetch/parse dorf2 for village %d: %s", self.username, village_id, exc)

        return village_data