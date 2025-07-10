# modules/resources.py

from .base import BaseModule
from config import log
from bs4 import BeautifulSoup
import re
import json
import time

class Module(BaseModule):
    """Provides helper routines for using hero resources."""

    def __init__(self, agent):
        super().__init__(agent)
        # This ensures the building module can access this one.
        agent.resources_module = self

    def tick(self, village_data):
        pass

    def parse_stock_bar(self, soup: BeautifulSoup):
        stock = soup.find(id="stockBar")
        if not stock:
            return None
        mapping = {"lumber": "l1", "clay": "l2", "iron": "l3", "crop": "l4"}
        out = {}
        for name, rid in mapping.items():
            span = stock.find(id=rid)
            if not span or not span.text:
                return None
            try:
                out[name] = int(re.sub(r"\D", "", span.text))
            except ValueError:
                return None
        return out

    def _get_storage_caps(self, soup: BeautifulSoup):
        def val(sel):
            el = soup.select_one(sel)
            return int(re.sub(r"\D", "", el.text)) if el and el.text else 0
        wh = val('.warehouse .capacity .value')
        gr = val('.granary .capacity .value')
        return {"lumber": wh, "clay": wh, "iron": wh, "crop": gr}

    def fetch_current_resources(self, village_id: int):
        url = f"{self.agent.client.server_url}/dorf1.php?newdid={village_id}"
        resp = self.agent.client.sess.get(url, timeout=15)
        soup = BeautifulSoup(resp.text, 'html.parser')
        res = self.parse_stock_bar(soup)
        caps = self._get_storage_caps(soup)
        return res, caps

    def parse_resource_costs(self, soup: BeautifulSoup):
        contract = soup.select_one('#contract .resourceWrapper, .upgradeBuilding .resourceWrapper')
        if not contract:
            return None
        mapping = {'r1Big': 'lumber', 'r2Big': 'clay', 'r3Big': 'iron', 'r4Big': 'crop'}
        costs = {k: 0 for k in mapping.values()}
        for div in contract.select('.inlineIcon.resource'):
            icon = div.find('i')
            val_el = div.select_one('.value')
            if not icon or not val_el:
                continue
            for cls, res in mapping.items():
                if cls in icon.get('class', []):
                    try:
                        costs[res] = int(re.sub(r"\D", "", val_el.text))
                    except ValueError:
                        pass
        return costs if any(costs.values()) else None

    def _fetch_build_page(self, village_id: int, slot_id: int, gid: int):
        base = f"{self.agent.client.server_url}/build.php?newdid={village_id}&id={slot_id}"
        for cat in [None, 1, 2, 3]:
            url = base if cat is None else f"{base}&category={cat}"
            resp = self.agent.client.sess.get(url, timeout=15)
            soup = BeautifulSoup(resp.text, 'html.parser')
            if soup.find('img', class_=f'g{gid}') or soup.find('button', class_='green'):
                return soup
        return soup

    def ensure_resources_for_build(self, village_id: int, slot_id: int, gid: int):
        soup = self._fetch_build_page(village_id, slot_id, gid)
        costs = self.parse_resource_costs(soup)
        if not costs:
            return False
        current, caps = self.fetch_current_resources(village_id)
        if not current:
            return False
        deficits = {r: max(0, costs[r] - current.get(r, 0)) for r in costs}
        if all(v <= 0 for v in deficits.values()):
            return False
        return self.use_hero_resources(village_id, deficits, current, caps)

    def use_hero_resources(self, village_id: int, deficits, current, caps) -> bool:
        # --- START OF MODIFICATIONS ---
        hero_items = {}
        resource_mapping = {
            145: "lumber", # Item ID for Lumber
            146: "clay",   # Item ID for Clay
            147: "iron",   # Item ID for Iron
            148: "crop"    # Item ID for Crop
        }

        try:
            # Fetch inventory from the reliable API endpoint
            api_url = f"{self.agent.client.server_url}/api/v1/hero/v2/screen/inventory"
            resp = self.agent.client.sess.get(api_url, timeout=15)
            
            if resp.status_code != 200:
                log.error(f"[{self.agent.client.username}] Failed to fetch hero inventory API. Status: {resp.status_code}")
                return False
                
            data = resp.json()
            inventory = data.get('viewData', {}).get('itemsInventory', [])
            
            # Map resources by their Type ID
            for item in inventory:
                type_id = item.get('typeId')
                if type_id in resource_mapping:
                    resource_name = resource_mapping[type_id]
                    hero_items[resource_name] = {'id': item['id'], 'amount': int(item['amount'])}

        except Exception as e:
            log.error(f"[{self.agent.client.username}] Failed to fetch or parse hero items API: {e}")
            return False

        used_any_item = False
        # Iterate in the correct resource order
        for res_name in ["lumber", "clay", "iron", "crop"]:
            deficit = deficits.get(res_name, 0)
            if deficit <= 0 or res_name not in hero_items:
                continue
                
            # Calculate how much space is available in storage
            room_in_storage = caps.get(res_name, 0) - current.get(res_name, 0)
            
            # Determine the amount to use
            amount_to_use = min(deficit, hero_items[res_name]['amount'], room_in_storage)
            
            if amount_to_use <= 0:
                continue

            # Prepare the API payload
            payload = {
                "action": "inventory",
                "itemId": hero_items[res_name]['id'],
                "amount": amount_to_use,
                "villageId": village_id
            }
            headers = {
                'X-Version': self.agent.client.server_version,
                'X-Requested-With': 'XMLHttpRequest',
                'Content-Type': 'application/json; charset=UTF-8',
                'Referer': f'{self.agent.client.server_url}/hero/inventory'
            }

            try:
                use_item_api = f"{self.agent.client.server_url}/api/v1/hero/v2/inventory/use-item"
                # Use PUT method as seen in network logs
                r = self.agent.client.sess.put(use_item_api, json=payload, headers=headers, timeout=15)
                
                if r.status_code == 200 or r.status_code == 204: # Success can be 200 OK or 204 No Content
                    used_any_item = True
                    current[res_name] += amount_to_use # Update local state immediately
                    log.info(f"[{self.agent.client.username}] Successfully used {amount_to_use} {res_name} from hero items.")
                    
                    # Perform follow-up requests to refresh data, mimicking the browser
                    self.agent.client.sess.get(f"{self.agent.client.server_url}/api/v1/hero/dataForHUD", timeout=10)
                    self.agent.client.sess.post(f"{self.agent.client.server_url}/api/v1/village/resources", json={}, timeout=10)
                    time.sleep(0.5) # Small delay
                else:
                    log.error(f"[{self.agent.client.username}] Hero item API failed with status {r.status_code}: {r.text}")

            except Exception as e:
                log.error(f"[{self.agent.client.username}] Network error while using hero item: {e}")
                
        return used_any_item
        # --- END OF MODIFICATIONS ---