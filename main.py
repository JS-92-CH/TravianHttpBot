import os
import re
import json
import time
import threading
import logging
from typing import List, Dict, Any, Optional

import requests
from bs4 import BeautifulSoup
from flask import Flask, render_template_string
from flask_socketio import SocketIO

# ─────────────────────────────────────────
# CONFIG & GLOBAL STATE
# ─────────────────────────────────────────

# Keep Werkzeug quiet – our own logger is enough
logging.getLogger("werkzeug").setLevel(logging.ERROR)
log = logging.getLogger("TravianBot")
log.setLevel(logging.INFO)
stream_handler = logging.StreamHandler()
stream_handler.setFormatter(logging.Formatter('[%(levelname)s] %(message)s'))
log.addHandler(stream_handler)


# In‑memory state that gets pushed to the dashboard
BOT_STATE: Dict[str, Any] = {
    "accounts": [],               # List of account dicts → {username, password, server_url}
    "village_data": {},           # Combined data store: username -> village_list, village_id -> village_data
    "build_queues": {}            # village_id → list[queue‑item]
}
state_lock = threading.Lock()       # thread‑safe writes to BOT_STATE

# ⬇ The classic C# build order, kept as a *string* so it round‑trips through json.loads()
CSHARP_BUILD_ORDER: str = r"""
[{"Id":{"Value":0},"Position":0,"Type":0,"Content":"{\"Location\":39,\"Level\":1,\"Type\":16}"},{"Id":{"Value":0},"Position":1,"Type":0,"Content":"{\"Location\":26,\"Level\":20,\"Type\":15}"},{"Id":{"Value":0},"Position":2,"Type":0,"Content":"{\"Location\":19,\"Level\":5,\"Type\":10}"},{"Id":{"Value":0},"Position":3,"Type":0,"Content":"{\"Location\":20,\"Level\":5,\"Type\":11}"},{"Id":{"Value":0},"Position":4,"Type":0,"Content":"{\"Location\":21,\"Level\":1,\"Type\":17}"},{"Id":{"Value":0},"Position":5,"Type":0,"Content":"{\"Location\":22,\"Level\":20,\"Type\":25}"},{"Id":{"Value":0},"Position":6,"Type":1,"Content":"{\"Level\":5,\"Plan\":0}"},{"Id":{"Value":0},"Position":7,"Type":0,"Content":"{\"Location\":23,\"Level\":3,\"Type\":19}"},{"Id":{"Value":0},"Position":8,"Type":0,"Content":"{\"Location\":24,\"Level\":10,\"Type\":22}"},{"Id":{"Value":0},"Position":9,"Type":0,"Content":"{\"Location\":25,\"Level\":20,\"Type\":24}"},{"Id":{"Value":0},"Position":10,"Type":1,"Content":"{\"Level\":10,\"Plan\":0}"},{"Id":{"Value":0},"Position":11,"Type":0,"Content":"{\"Location\":19,\"Level\":20,\"Type\":10}"},{"Id":{"Value":0},"Position":12,"Type":0,"Content":"{\"Location\":20,\"Level\":20,\"Type\":11}"},{"Id":{"Value":0},"Position":13,"Type":0,"Content":"{\"Location\":21,\"Level\":20,\"Type\":17}"},{"Id":{"Value":0},"Position":14,"Type":0,"Content":"{\"Location\":23,\"Level\":20,\"Type\":19}"},{"Id":{"Value":0},"Position":15,"Type":0,"Content":"{\"Location\":24,\"Level\":20,\"Type\":22}"},{"Id":{"Value":0},"Position":16,"Type":0,"Content":"{\"Location\":27,\"Level\":20,\"Type\":13}"},{"Id":{"Value":0},"Position":17,"Type":0,"Content":"{\"Location\":28,\"Level\":20,\"Type\":20}"},{"Id":{"Value":0},"Position":18,"Type":0,"Content":"{\"Location\":29,\"Level\":20,\"Type\":46}"},{"Id":{"Value":0},"Position":19,"Type":0,"Content":"{\"Location\":39,\"Level\":20,\"Type\":16}"},{"Id":{"Value":0},"Position":20,"Type":0,"Content":"{\"Location\":30,\"Level\":20,\"Type\":14}"},{"Id":{"Value":0},"Position":21,"Type":0,"Content":"{\"Location\":40,\"Level\":20,\"Type\":42}"},{"Id":{"Value":0},"Position":22,"Type":0,"Content":"{\"Location\":31,\"Level\":20,\"Type\":21}"},{"Id":{"Value":0},"Position":23,"Type":0,"Content":"{\"Location\":32,\"Level\":5,\"Type\":5}"},{"Id":{"Value":0},"Position":24,"Type":0,"Content":"{\"Location\":33,\"Level\":5,\"Type\":6}"},{"Id":{"Value":0},"Position":25,"Type":0,"Content":"{\"Location\":34,\"Level\":5,\"Type\":7}"},{"Id":{"Value":0},"Position":26,"Type":0,"Content":"{\"Location\":35,\"Level\":5,\"Type\":8}"},{"Id":{"Value":0},"Position":27,"Type":0,"Content":"{\"Location\":36,\"Level\":5,\"Type\":9}"}]
"""

# ─────────────────────────────────────────
# UTILS – config, parsing & helpers
# ─────────────────────────────────────────

def load_config() -> None:
    """Load accounts & queues from disk into BOT_STATE."""
    if not os.path.exists("config.json"):
        log.info("No config.json found – starting fresh ✨")
        return
    try:
        with open("config.json", "r", encoding="utf‑8") as fh:
            data = json.load(fh)
        with state_lock:
            BOT_STATE["accounts"] = data.get("accounts", [])
            BOT_STATE["build_queues"] = data.get("build_queues", {})
            # Ensure village_data is initialized
            if "village_data" not in BOT_STATE:
                BOT_STATE["village_data"] = {}
        log.info("Configuration loaded ✔")
    except Exception as exc:
        log.warning("Could not read config.json → %s", exc)


def save_config() -> None:
    """Persist accounts + queues back to disk."""
    with state_lock:
        # Create a copy to avoid serializing the large village_data object
        payload = {
            "accounts": BOT_STATE["accounts"],
            "build_queues": BOT_STATE["build_queues"],
        }
    with open("config.json", "w", encoding="utf‑8") as fh:
        json.dump(payload, fh, indent=4)
    log.info("Configuration saved ✔")


def parse_csharp_build_order(raw: str) -> List[Dict[str, Any]]:
    """Convert original C# build instruction JSON → python‑friendly queue list."""
    queue: List[Dict[str, Any]] = []
    if not raw.strip():
        return queue
    try:
        items = json.loads(raw)
    except json.JSONDecodeError:
        log.error("Default build order JSON is malformed – returning empty queue ✖")
        return queue

    for node in items:
        content = json.loads(node["Content"])
        if node["Type"] == 0:  # building upgrade
            queue.append({
                "type": "building",
                "location": content["Location"],
                "level": content["Level"],
                "gid": content["Type"],
            })
        elif node["Type"] == 1:  # resource level plan
            queue.append({
                "type": "resource",
                "plan": content.get("Plan", 0),
                "level": content["Level"],
            })
    return queue


def load_default_build_queue() -> List[Dict[str, Any]]:
    """Try loading the build‑order from an external file; fall back to constant."""
    try:
        with open("default_build_order.json", "r", encoding="utf‑8") as fh:
            raw = fh.read()
        return parse_csharp_build_order(raw)
    except FileNotFoundError:
        # Fallback to baked‑in constant
        return parse_csharp_build_order(CSHARP_BUILD_ORDER)


def gid_name(gid: int) -> str:
    mapping = {
        1: "Woodcutter", 2: "Clay Pit", 3: "Iron Mine", 4: "Cropland", 5: "Sawmill", 6: "Brickyard",
        7: "Iron Foundry", 8: "Grain Mill", 9: "Bakery", 10: "Warehouse", 11: "Granary", 15: "Main Building",
        16: "Rally Point", 17: "Marketplace", 19: "Barracks", 20: "Stable", 21: "Workshop", 22: "Academy",
        24: "Town Hall", 25: "Residence", 26: "Palace", 42: "Tournament Square", 37: "Hero's Mansion",
        45: "Waterworks", 13: "Smithy", 14: "Tournament Square"
    }
    return mapping.get(int(gid), f"GID {gid}")

# ─────────────────────────────────────────
# DASHBOARD – Flask + SocketIO
# ─────────────────────────────────────────

app = Flask(__name__)
app.config["SECRET_KEY"] = "8b8e2d43‐dashboard‐secret"
socketio = SocketIO(app, async_mode="threading")

@app.route("/")
def index_route():
    try:
        with open("index.html", "r", encoding="utf-8") as f:
            return render_template_string(f.read())
    except FileNotFoundError:
        return "Error: index.html not found.", 404

# ─────────────────────────────────────────
# LOW‑LEVEL TRAVIAN CLIENT
# ─────────────────────────────────────────

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

        # ① Resources block (from <script> tag)
        try:
            script_text = soup.find("script", string=re.compile(r"var\s+resources\s*="))
            if script_text:
                match = re.search(r"var\s+resources\s*=\s*(\{.*?\});", script_text.string, re.DOTALL)
                if match:
                    json_str = match.group(1)
                    # It's not perfect JSON, needs keys quoted
                    json_str_fixed = re.sub(r"([a-zA-Z_][\w]*)\s*:", r'"\1":', json_str)
                    res_data = json.loads(json_str_fixed)
                    out["resources"] = {k: int(v) for k, v in res_data.get("storage", {}).items()}
                    out["storage"] = {k: int(v) for k, v in res_data.get("maxStorage", {}).items()}
                    out["production"] = {k: int(v) for k, v in res_data.get("production", {}).items()}
        except Exception as exc:
            log.debug("Resource parser failed – %s", exc)

        # ② Buildings (works for both dorf1 and dorf2)
        for link in soup.select("#resourceFieldContainer a, #villageContent a.buildingSlot"):
            if "build.php?id=" not in link.get("href", ""):
                 continue
            lvl_div = link.find("div", class_="labelLayer")
            if not lvl_div:
                continue
            
            gid_cls = next((c for c in link.get("class", []) if c.startswith("g") and c[1:].isdigit()), None)
            
            # For dorf2, the gid is in the parent div
            if not gid_cls:
                 parent_div = link.find_parent('div', class_='buildingSlot')
                 if parent_div:
                     gid_cls = next((c for c in parent_div.get("class", []) if c.startswith("g") and c[1:].isdigit()), None)

            if gid_cls:
                out["buildings"].append({
                    "id": int(re.search(r"id=(\d+)", link["href"]).group(1)),
                    "level": int(lvl_div.text.strip()),
                    "gid": int(gid_cls[1:]),
                })
        
        # for dorf2, some building info is directly in divs
        for b_slot in soup.select("#villageContent > .buildingSlot"):
            gid = b_slot.get('data-gid')
            loc_id = b_slot.get('data-aid')
            if gid and loc_id:
                level_tag = b_slot.select_one('a.level')
                level = 0
                if level_tag and level_tag.get('data-level'):
                    level = int(level_tag.get('data-level'))
                # avoid duplicates from previous loop
                if not any(b['id'] == int(loc_id) for b in out['buildings']):
                     out["buildings"].append({
                        "id": int(loc_id),
                        "level": level,
                        "gid": int(gid),
                    })

        # ③ Server build queue
        for li in soup.select(".buildingList li"):
            name_div = li.find("div", class_="name")
            if not name_div:
                continue
            out["queue"].append({
                "name": name_div.text.split("\n")[0].strip(),
                "level": li.find("span", class_="lvl").text.strip(),
                "eta": int(li.find("span", class_="timer")["value"]),
            })

        # ④ Villages sidebar
        for v_entry in soup.select("#sidebarBoxVillageList .listEntry"):
            link = v_entry.find("a", href=re.compile(r"newdid="))
            if not link:
                continue
            out["villages"].append({
                "id": int(re.search(r"newdid=(\d+)", link["href"]).group(1)),
                "name": v_entry.find("span", class_="name").text.strip(),
                "active": "active" in v_entry.get("class", []),
            })
        return out

    def fetch_and_parse_village(self, village_id: int) -> Optional[Dict[str, Any]]:
        """Fetch dorf1 and dorf2 for a village and return combined data."""
        log.info("[%s] Fetching data for village %d", self.username, village_id)
        
        # Fetch and parse dorf1 (resource fields)
        try:
            url_d1 = f"{self.server_url}/dorf1.php?newdid={village_id}"
            resp_d1 = self.sess.get(url_d1, timeout=15)
            resp_d1.raise_for_status()
            village_data = self.parse_village_page(resp_d1.text)
        except Exception as exc:
            log.error("[%s] Failed to fetch/parse dorf1 for village %d: %s", self.username, village_id, exc)
            return None

        # Fetch and parse dorf2 (village buildings)
        try:
            url_d2 = f"{self.server_url}/dorf2.php?newdid={village_id}"
            resp_d2 = self.sess.get(url_d2, timeout=15)
            resp_d2.raise_for_status()
            parsed_d2 = self.parse_village_page(resp_d2.text)
            
            # Merge buildings list, avoiding duplicates
            existing_building_ids = {b['id'] for b in village_data["buildings"]}
            for building in parsed_d2["buildings"]:
                if building['id'] not in existing_building_ids:
                    village_data["buildings"].append(building)

            village_data["queue"] = parsed_d2["queue"] # Overwrite with dorf2 queue
        except Exception as exc:
            log.warning("[%s] Failed to fetch/parse dorf2 for village %d: %s. Proceeding with dorf1 data only.", self.username, village_id, exc)

        return village_data

# ─────────────────────────────────────────
# HIGH-LEVEL BOT / AGENT LOGIC
# ─────────────────────────────────────────

class BotThread(threading.Thread):
    def __init__(self, app_context):
        super().__init__()
        self.daemon = True
        self.stop_event = threading.Event()
        self.app_context = app_context

    def stop(self):
        self.stop_event.set()

    def run(self):
        log.info("Bot thread started.")
        socketio.emit('log_message', {'data': 'Bot thread started.'})
        
        while not self.stop_event.is_set():
            with state_lock:
                accounts = BOT_STATE["accounts"][:]

            if not accounts:
                log.info("No accounts configured. Waiting...")
                time.sleep(15)
                continue

            for account in accounts:
                if self.stop_event.is_set():
                    break
                
                client = TravianClient(account["username"], account["password"], account["server_url"])
                if not client.login():
                    time.sleep(10) # Wait before retrying
                    continue

                try:
                    dorf1_resp = client.sess.get(f"{client.server_url}/dorf1.php", timeout=15)
                    dorf1_resp.raise_for_status()
                    sidebar_data = client.parse_village_page(dorf1_resp.text)
                    villages = sidebar_data.get("villages", [])
                except Exception as exc:
                    log.error("[%s] Could not get village list: %s", client.username, exc)
                    continue

                if not villages:
                    log.warning("[%s] No villages found for this account.", client.username)
                    continue

                with state_lock:
                    BOT_STATE["village_data"][client.username] = villages

                for village in villages:
                    if self.stop_event.is_set():
                        break
                        
                    village_id = village['id']
                    full_village_data = client.fetch_and_parse_village(village_id)
                    
                    if full_village_data:
                        with state_lock:
                            BOT_STATE["village_data"][str(village_id)] = full_village_data
                            if str(village_id) not in BOT_STATE["build_queues"]:
                                log.info("No build queue in config for village %s, creating default.", village_id)
                                BOT_STATE["build_queues"][str(village_id)] = load_default_build_queue()
                                save_config()

                        with self.app_context:
                            socketio.emit("state_update", BOT_STATE)

                    time.sleep(5) # Stagger village updates

            log.info("Finished checking all accounts. Waiting for 60 seconds.")
            self.stop_event.wait(60)
            
        log.info("Bot thread stopped.")
        socketio.emit('log_message', {'data': 'Bot thread stopped.'})


# ─────────────────────────────────────────
# SOCKET.IO EVENT HANDLERS
# ─────────────────────────────────────────
bot_thread = None

@socketio.on('connect')
def on_connect():
    log.info("Dashboard connected.")
    # Send initial state on connect
    socketio.emit("state_update", BOT_STATE)

@socketio.on('start_bot')
def handle_start_bot():
    global bot_thread
    if bot_thread is None or not bot_thread.is_alive():
        log.info("Starting bot...")
        bot_thread = BotThread(app.app_context())
        bot_thread.start()
        socketio.emit("log_message", {'data': "Bot started."})
    else:
        log.info("Bot is already running.")
        socketio.emit("log_message", {'data': "Bot is already running."})

@socketio.on('stop_bot')
def handle_stop_bot():
    global bot_thread
    if bot_thread and bot_thread.is_alive():
        log.info("Stopping bot...")
        bot_thread.stop()
        bot_thread = None
        socketio.emit("log_message", {'data': "Bot stopped."})
    else:
        log.info("Bot is not running.")
        socketio.emit("log_message", {'data': "Bot is not running."})

@socketio.on('add_account')
def handle_add_account(data):
    log.info("Adding account: %s", data['username'])
    with state_lock:
        if any(a['username'] == data['username'] for a in BOT_STATE['accounts']):
            log.warning("Account %s already exists.", data['username'])
            return
        BOT_STATE['accounts'].append({
            "username": data["username"],
            "password": data["password"],
            "server_url": data["server_url"],
        })
    save_config()
    socketio.emit("state_update", BOT_STATE)

@socketio.on('remove_account')
def handle_remove_account(data):
    username_to_remove = data.get('username')
    log.info("Removing account: %s", username_to_remove)
    with state_lock:
        BOT_STATE['accounts'] = [acc for acc in BOT_STATE['accounts'] if acc['username'] != username_to_remove]
    save_config()
    socketio.emit("state_update", BOT_STATE)

@socketio.on('update_build_queue')
def handle_update_build_queue(data):
    village_id = data.get('villageId')
    queue = data.get('queue')
    if village_id and queue is not None:
        log.info("Updating build queue for village %s", village_id)
        with state_lock:
            BOT_STATE['build_queues'][str(village_id)] = queue
        save_config()
        socketio.emit("state_update", BOT_STATE)

# ─────────────────────────────────────────
# MAIN EXECUTION
# ─────────────────────────────────────────

if __name__ == "__main__":
    load_config()
    log.info("Dashboard available at http://127.0.0.1:5000")
    socketio.run(app, host="0.0.0.0", port=5000)