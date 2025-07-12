import os
import re
import json
import threading
import logging
from typing import List, Dict, Any

# ANSI escape codes for colors
class bcolors:
    HEADER = '\033[95m'
    OKBLUE = '\033[94m'
    OKCYAN = '\033[96m'
    OKGREEN = '\033[92m'
    WARNING = '\033[93m'
    FAIL = '\033[91m'
    ENDC = '\033[0m'
    BOLD = '\033[1m'
    UNDERLINE = '\033[4m'

class ColoredFormatter(logging.Formatter):
    """A custom log formatter to add color to log messages."""

    def format(self, record):
        log_message = super().format(record)
        if '[TrainingAgent]' in record.getMessage():
            return f"{bcolors.OKCYAN}{log_message}{bcolors.ENDC}"
        return log_message

# ─────────────────────────────────────────
# LOGGING
# ─────────────────────────────────────────
class SocketIOHandler(logging.Handler):
    def __init__(self, socketio_instance):
        super().__init__()
        self.socketio = socketio_instance

    def emit(self, record):
        log_entry = self.format(record)
        # Remove ANSI color codes for the web dashboard
        log_entry = re.sub(r'\x1b\[[0-9;]*m', '', log_entry)
        self.socketio.emit('log_message', {'data': log_entry})

def setup_logging(socketio_instance=None):
    """Sets up the global logger."""
    log = logging.getLogger("TravianBot")
    if not log.handlers:
        log.setLevel(logging.INFO)
        stream_handler = logging.StreamHandler()
        # Use the new ColoredFormatter for console output
        stream_handler.setFormatter(ColoredFormatter('[%(levelname)s] [%(asctime)s] %(message)s'))
        log.addHandler(stream_handler)
        
        if socketio_instance:
            socketio_handler = SocketIOHandler(socketio_instance)
            socketio_handler.setFormatter(logging.Formatter('%(message)s'))
            log.addHandler(socketio_handler)

        logging.getLogger("werkzeug").setLevel(logging.ERROR)
    return log

log = setup_logging()
# ─────────────────────────────────────────
# SHARED STATE
# ─────────────────────────────────────────
BOT_STATE: Dict[str, Any] = {
    "accounts": [],
    "village_data": {},
    "build_queues": {},
    "demolish_queues": {},
    "training_data": {},
    "training_queues": {},
    "smithy_upgrades": {},
    "smithy_data": {},
    "build_templates": {
        "Off Village": [
            {"type": "building", "location": 26, "gid": 15, "level": 20},
            {"type": "building", "location": 39, "gid": 16, "level": 1},
            {"type": "building", "location": 19, "gid": 25, "level": 10},
            {"type": "building", "location": 23, "gid": 10, "level": 5},
            {"type": "building", "location": 24, "gid": 11, "level": 5},
            {"type": "building", "location": 20, "gid": 19, "level": 3},
            {"type": "building", "location": 21, "gid": 22, "level": 10},
            {"type": "building", "location": 22, "gid": 24, "level": 10},
            {"type": "resource_plan", "level": 10},
            {"type": "building", "location": 25, "gid": 17, "level": 1},
            {"type": "building", "location": 27, "gid": 13, "level": 3},
            {"type": "building", "location": 28, "gid": 20, "level": 15},
            {"type": "building", "location": 29, "gid": 21, "level": 10},            
            {"type": "building", "location": 40, "gid": 31, "level": 20},
            {"type": "building", "location": 19, "gid": 25, "level": 20},
            {"type": "building", "location": 20, "gid": 19, "level": 20},
            {"type": "building", "location": 21, "gid": 22, "level": 20},
            {"type": "building", "location": 22, "gid": 24, "level": 20},
            {"type": "building", "location": 23, "gid": 10, "level": 20},
            {"type": "building", "location": 24, "gid": 11, "level": 20},
            {"type": "building", "location": 25, "gid": 17, "level": 20},
            {"type": "building", "location": 27, "gid": 13, "level": 20},
            {"type": "building", "location": 28, "gid": 20, "level": 20},
            {"type": "building", "location": 30, "gid": 46, "level": 20},
            {"type": "building", "location": 29, "gid": 21, "level": 20},
            {"type": "building", "location": 39, "gid": 16, "level": 20},
            {"type": "building", "location": 31, "gid": 14, "level": 20},
            {"type": "building", "location": 32, "gid": 29, "level": 20},
            {"type": "building", "location": 33, "gid": 30, "level": 20},
            {"type": "building", "location": 34, "gid": 10, "level": 20},
            {"type": "building", "location": 35, "gid": 11, "level": 20},
            {"type": "building", "location": 36, "gid": 10, "level": 20},
            {"type": "building", "location": 37, "gid": 11, "level": 20}
        ],
            "Task_Focused": [
    { "type": "building", "gid": 15, "level": 20, "location": 26 }, # Main Building
    { "type": "resource_plan", "level": 2 },                        # Upgrade all resource fields to level 2
    { "type": "building", "gid": 10, "level": 1 },                  # Warehouse
    { "type": "building", "gid": 11, "level": 1 },                  # Granary
    { "type": "building", "gid": 16, "level": 1 },                  # Rally Point
    { "type": "building", "gid": 19, "level": 10 },                 # Barracks
    { "type": "building", "gid": 22, "level": 10 },                 # Academy
    { "type": "building", "gid": 24, "level": 1 },                  # Town Hall
    { "type": "building", "gid": 17, "level": 1 },                  # Marketplace
    { "type": "building", "gid": 25, "level": 1 },                  # Residence
    { "type": "building", "gid": 31, "level": 1 },                  # City Wall
    { "type": "building", "gid": 23, "level": 10 },                 # Cranny
    { "type": "resource_plan", "level": 4 },                        # Upgrade all resource fields to level 4
    { "type": "resource_plan", "level": 7 },                        # Upgrade all resource fields to level 7
    { "type": "building", "gid": 1, "level": 10 },                  # Woodcutter
    { "type": "building", "gid": 2, "level": 10 },                  # Clay Pit
    { "type": "building", "gid": 3, "level": 10 },                  # Iron Mine
    { "type": "building", "gid": 4, "level": 10 },                  # Cropland
    { "type": "building", "gid": 18, "level": 10 },                 # Embassy
    { "type": "building", "gid": 13, "level": 10 },                 # Smithy
    { "type": "building", "gid": 20, "level": 10 },                 # Stable
    { "type": "building", "gid": 10, "level": 10 },                 # Warehouse
    { "type": "building", "gid": 11, "level": 10 },                 # Granary
    { "type": "building", "gid": 16, "level": 10 },                 # Rally Point
    { "type": "building", "gid": 17, "level": 10 },                 # Marketplace
    { "type": "building", "gid": 25, "level": 10 },                 # Residence
    { "type": "building", "gid": 31, "level": 10 },                 # City Wall
    { "type": "building", "gid": 24, "level": 10 },                  # Town Hall
    { "type": "building", "gid": 18, "level": 16 },                 # Embassy
    { "type": "building", "gid": 13, "level": 16 },                 # Smithy
    { "type": "building", "gid": 20, "level": 16 },                 # Stable
    { "type": "building", "gid": 10, "level": 16 },                 # Warehouse
    { "type": "building", "gid": 11, "level": 16 },                 # Granary
    { "type": "building", "gid": 16, "level": 20 },                 # Rally Point
    { "type": "building", "gid": 31, "level": 20 },                 # City Wall
    { "type": "building", "gid": 17, "level": 20 }                 # Marketplace
],
        "Roman": [
            {"type": "resource_plan", "level": 10},
            {"type": "building", "gid": 15, "level": 20},
            {"type": "building", "gid": 17, "level": 20},
            {"type": "building", "gid": 16, "level": 20},
            {"type": "building", "gid": 19, "level": 20},
            {"type": "building", "gid": 20, "level": 20},
            {"type": "building", "gid": 21, "level": 20},
            {"type": "building", "gid": 22, "level": 20},
        ]
    }
}
# Use a re-entrant lock to prevent deadlocks
state_lock = threading.RLock()

# ─────────────────────────────────────────
# CONFIG & DEFAULTS
# ─────────────────────────────────────────
# --- (CSHARP_BUILD_ORDER, GID_MAPPING, NAME_TO_GID, etc. remain unchanged) ---
CSHARP_BUILD_ORDER = r"""
[{"Id":{"Value":0},"Position":0,"Type":0,"Content":"{\"Location\":39,\"Level\":1,\"Type\":16}"},{"Id":{"Value":0},"Position":1,"Type":0,"Content":"{\"Location\":26,\"Level\":20,\"Type\":15}"},{"Id":{"Value":0},"Position":2,"Type":0,"Content":"{\"Location\":19,\"Level\":5,\"Type\":10}"},{"Id":{"Value":0},"Position":3,"Type":0,"Content":"{\"Location\":20,\"Level\":5,\"Type\":11}"},{"Id":{"Value":0},"Position":4,"Type":0,"Content":"{\"Location\":21,\"Level\":1,\"Type\":17}"},{"Id":{"Value":0},"Position":5,"Type":0,"Content":"{\"Location\":22,\"Level\":20,\"Type\":25}"},{"Id":{"Value":0},"Position":6,"Type":1,"Content":"{\"Level\":5,\"Plan\":0}"},{"Id":{"Value":0},"Position":7,"Type":0,"Content":"{\"Location\":23,\"Level\":3,\"Type\":19}"},{"Id":{"Value":0},"Position":8,"Type":0,"Content":"{\"Location\":24,\"Level\":10,\"Type\":22}"},{"Id":{"Value":0},"Position":9,"Type":0,"Content":"{\"Location\":25,\"Level\":20,\"Type\":24}"},{"Id":{"Value":0},"Position":10,"Type":1,"Content":"{\"Level\":10,\"Plan\":0}"},{"Id":{"Value":0},"Position":11,"Type":0,"Content":"{\"Location\":19,\"Level\":20,\"Type\":10}"},{"Id":{"Value":0},"Position":12,"Type":0,"Content":"{\"Location\":20,\"Level\":20,\"Type\":11}"},{"Id":{"Value":0},"Position":13,"Type":0,"Content":"{\"Location\":21,\"Level\":20,\"Type\":17}"},{"Id":{"Value":0},"Position":14,"Type":0,"Content":"{\"Location\":23,\"Level\":20,\"Type\":19}"},{"Id":{"Value":0},"Position":15,"Type":0,"Content":"{\"Location\":24,\"Level\":20,\"Type\":22}"},{"Id":{"Value":0},"Position":16,"Type":0,"Content":"{\"Location\":27,\"Level\":20,\"Type\":13}"},{"Id":{"Value":0},"Position":17,"Type":0,"Content":"{\"Location\":28,\"Level\":20,\"Type\":20}"},{"Id":{"Value":0},"Position":18,"Type":0,"Content":"{\"Location\":29,\"Level\":20,\"Type\":46}"},{"Id":{"Value":0},"Position":19,"Type":0,"Content":"{\"Location\":39,\"Level\":20,\"Type\":16}"},{"Id":{"Value":0},"Position":20,"Type":0,"Content":"{\"Location\":30,\"Level\":20,\"Type\":14}"},{"Id":{"Value":0},"Position":21,"Type":0,"Content":"{\"Location\":40,\"Level\":20,\"Type\":42}"},{"Id":{"Value":0},"Position":22,"Type":0,"Content":"{\"Location\":31,\"Level\":20,\"Type\":21}"},{"Id":{"Value":0},"Position":23,"Type":0,"Content":"{\"Location\":32,\"Level\":5,\"Type\":5}"},{"Id":{"Value":0},"Position":24,"Type":0,"Content":"{\"Location\":33,\"Level\":5,\"Type\":6}"},{"Id":{"Value":0},"Position":25,"Type":0,"Content":"{\"Location\":34,\"Level\":5,\"Type\":7}"},{"Id":{"Value":0},"Position":26,"Type":0,"Content":"{\"Location\":35,\"Level\":5,\"Type\":8}"},{"Id":{"Value":0},"Position":27,"Type":0,"Content":"{\"Location\":36,\"Level\":5,\"Type\":9}"}]
"""

GID_MAPPING = {
    0: 'Empty Slot', 1: 'Woodcutter', 2: 'Clay Pit', 3: 'Iron Mine', 4: 'Cropland',
    5: 'Sawmill', 6: 'Brickyard', 7: 'Iron Foundry', 8: 'Grain Mill', 9: 'Bakery',
    10: 'Warehouse', 11: 'Granary', 12: 'Armoury', 13: 'Smithy', 14: 'Tournament Square',
    15: 'Main Building', 16: 'Rally Point', 17: 'Marketplace', 18: 'Embassy', 19: 'Barracks',
    20: 'Stable', 21: 'Workshop', 22: 'Academy', 23: 'Cranny', 24: 'Town Hall',
    25: 'Residence', 26: 'Palace', 27: 'Treasury', 28: 'Trade Office', 29: 'Great Barracks',
    30: 'Great Stable', 31: 'City Wall', 32: 'Earth Wall', 33: 'Palisade', 34: 'Stonemason\'s Lodge',
    35: 'Brewery', 36: 'Trapper', 37: "Hero's Mansion", 38: 'Great Warehouse', 39: 'Great Granary',
    40: 'Wonder of the World', 41: 'Horse Drinking Trough', 42: 'Stone Wall', 43: 'Makeshift Wall', 44: 'Command Center',
    45: 'Waterworks', 46: 'Hospital'
}

# Create a reverse mapping from name to GID for prerequisite parsing
NAME_TO_GID = {name.lower(): gid for gid, name in GID_MAPPING.items()}
NAME_TO_GID["hero's mansion"] = 37 # Handle apostrophe case

def load_config() -> None:
    config_path = "config.json"
    if not os.path.exists(config_path):
        log.warning(f"{config_path} not found!")
        log.info(f"Please copy 'config.example.json' to '{config_path}' and fill in your account details.")
        return
    try:
        with open(config_path, "r", encoding="utf-8") as fh:
            data = json.load(fh)

        config_updated = False
        accounts = data.get("accounts", [])
        for account in accounts:
            if 'active' not in account:
                account['active'] = False
                config_updated = True
                log.info(f"Configuration Update: Added 'active: false' to account '{account.get('username')}'.")

            if 'proxy' not in account or not isinstance(account.get('proxy'), dict):
                account['proxy'] = {
                    "ip": "", "port": "",
                    "username": "", "password": ""
                }
                config_updated = True
                log.info(f"Configuration Update: Added default proxy object to account '{account.get('username')}'.")

        with state_lock:
            BOT_STATE["accounts"] = accounts
            BOT_STATE["build_queues"] = data.get("build_queues", {})
            BOT_STATE["demolish_queues"] = data.get("demolish_queues", {})
            BOT_STATE["training_queues"] = data.get("training_queues", {})
            BOT_STATE["smithy_upgrades"] = data.get("smithy_upgrades", {})
            BOT_STATE["loop_module_state"] = data.get("loop_module_state", {})
            BOT_STATE["build_templates"].update(data.get("build_templates", {}))
            if "village_data" not in BOT_STATE:
                BOT_STATE["village_data"] = {}
            if "training_data" not in BOT_STATE:
                BOT_STATE["training_data"] = {}
        
        if config_updated:
            log.info("Configuration was updated with new fields. Saving changes...")
            save_config()

        log.info("Configuration loaded ✔")
    except Exception as exc:
        log.warning(f"Could not read {config_path} → {exc}")

def save_config() -> None:
    with state_lock:
        # Create a deep copy to avoid modifying the state during serialization
        payload = {
            "accounts": [acc.copy() for acc in BOT_STATE["accounts"]],
            "build_queues": BOT_STATE["build_queues"].copy(),
            "demolish_queues": BOT_STATE["demolish_queues"].copy(),
            "training_queues": BOT_STATE["training_queues"].copy(),
            "smithy_upgrades": BOT_STATE["smithy_upgrades"].copy(),
            "build_templates": BOT_STATE.get("build_templates", {}).copy(),
            "loop_module_state": BOT_STATE.get("loop_module_state", {}).copy()
        }
    with open("config.json", "w", encoding="utf‑8") as fh:
        json.dump(payload, fh, indent=4)
    log.info("Configuration saved ✔")

def parse_csharp_build_order(raw: str) -> List[Dict[str, Any]]:
    queue: List[Dict[str, Any]] = []
    if not raw.strip(): return queue
    try:
        items = json.loads(raw)
    except json.JSONDecodeError:
        log.error("Default build order JSON is malformed – returning empty queue ✖")
        return queue
    for node in items:
        content = json.loads(node["Content"])
        if node["Type"] == 0:
            queue.append({"type": "building", "location": content["Location"], "level": content["Level"], "gid": content["Type"]})
        elif node["Type"] == 1:
            queue.append({"type": "resource", "plan": content.get("Plan", 0), "level": content["Level"]})
    return queue

def load_default_build_queue() -> List[Dict[str, Any]]:
    try:
        with open("default_build_order.json", "r", encoding="utf‑8") as fh:
            raw = fh.read()
        return parse_csharp_build_order(raw)
    except FileNotFoundError:
        return parse_csharp_build_order(CSHARP_BUILD_ORDER)

def gid_name(gid: int) -> str:
    return GID_MAPPING.get(int(gid), f"GID {gid}")

def is_multi_instance(gid: int) -> bool:
    """Checks if a building can have multiple instances."""
    return gid in [10, 11, 23, 36, 38, 39]