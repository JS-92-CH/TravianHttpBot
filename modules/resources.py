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
        # --- Start of Changes ---
        # Correctly assign the module to the agent
        agent.resources_module = self
        # --- End of Changes ---

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
        hero_items = {}
        try:
            resp = self.agent.client.sess.get(f"{self.agent.client.server_url}/hero", timeout=15)
            soup = BeautifulSoup(resp.text, 'html.parser')
            script_tag = soup.find('script', string=re.compile(r"HeroV2\.render"))
            if not script_tag:
                return False
            match = re.search(r"HeroV2\.render\s*\(\s*(\{.*\}),\s*\{\}\);", script_tag.string, re.DOTALL)
            if not match:
                return False
            data = json.loads(match.group(1))
            inventory = data.get('screenData', {}).get('viewData', {}).get('itemsInventory', [])
            for item in inventory:
                if item.get('name') in ['lumber','clay','iron','crop']:
                    hero_items[item['name']] = {'id': item['id'], 'amount': int(item['amount'])}
        except Exception as e:
            log.error(f"[{self.agent.client.username}] Failed to fetch hero items: {e}")
            return False

        used = False
        for res in ['lumber','clay','iron','crop']:
            deficit = deficits.get(res, 0)
            if deficit <= 0 or res not in hero_items:
                continue
            room = caps[res] - current.get(res, 0)
            amount = min(deficit, hero_items[res]['amount'], room)
            if amount <= 0:
                continue
            payload = {"itemId": hero_items[res]['id'], "amount": amount, "villageId": village_id}
            headers = {
                'X-Version': self.agent.client.server_version,
                'X-Requested-With': 'XMLHttpRequest',
                'Content-Type': 'application/json; charset=UTF-8'
            }
            try:
                api = f"{self.agent.client.server_url}/api/v1/hero/v2/inventory/use-item"
                r = self.agent.client.sess.post(api, json=payload, headers=headers, timeout=15)
                if r.status_code == 200:
                    used = True
                    current[res] += amount
                    log.info(f"[{self.agent.client.username}] Used {amount} {res} from hero items.")
                else:
                    log.error(f"[{self.agent.client.username}] Hero API failed with status {r.status_code}: {r.text}")
            except Exception as e:
                log.error(f"[{self.agent.client.username}] Network error using hero item: {e}")
        return used