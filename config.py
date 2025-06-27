import os
import re
import json
import threading
import logging
from typing import List, Dict, Any

# ─────────────────────────────────────────
# LOGGING
# ─────────────────────────────────────────
class SocketIOHandler(logging.Handler):
    def __init__(self, socketio_instance):
        super().__init__()
        self.socketio = socketio_instance

    def emit(self, record):
        log_entry = self.format(record)
        self.socketio.emit('log_message', {'data': log_entry})

def setup_logging(socketio_instance=None):
    """Sets up the global logger."""
    log = logging.getLogger("TravianBot")
    if not log.handlers:
        log.setLevel(logging.INFO)
        stream_handler = logging.StreamHandler()
        stream_handler.setFormatter(logging.Formatter('[%(levelname)s] [%(asctime)s] %(message)s'))
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
    "training_data": {},
    "training_queues": {},
    "build_templates": {
        "Off Village": [
            {"type": "building", "location": 26, "gid": 15, "level": 20},
            {"type": "building", "location": 39, "gid": 16, "level": 1},
            {"type": "building", "location": 19, "gid": 25, "level": 10},
            {"type": "resource_plan", "level": 5},
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
            {"type": "building", "location": 30, "gid": 46, "level": 20},
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
state_lock = threading.Lock()

# ─────────────────────────────────────────
# CONFIG & DEFAULTS
# ─────────────────────────────────────────

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
    if not os.path.exists("config.json"):
        # --- Start of Changes ---
        log.warning("config.json not found!")
        log.info("Please copy 'config.example.json' to 'config.json' and fill in your account details.")
        # --- End of Changes ---
        return
    try:
        with open("config.json", "r", encoding="utf‑8") as fh:
            data = json.load(fh)
        with state_lock:
            BOT_STATE["accounts"] = data.get("accounts", [])
            BOT_STATE["build_queues"] = data.get("build_queues", {})
            BOT_STATE["build_templates"].update(data.get("build_templates", {})) # Update instead of overwrite
            if "village_data" not in BOT_STATE:
                BOT_STATE["village_data"] = {}
                BOT_STATE["training_queues"] = data.get("training_queues", {})
            if "training_data" not in BOT_STATE:
                BOT_STATE["training_data"] = {}

        log.info("Configuration loaded ✔")
    except Exception as exc:
        log.warning("Could not read config.json → %s", exc)

def save_config() -> None:
    with state_lock:
        payload = {
            "accounts": BOT_STATE["accounts"],
            "build_queues": BOT_STATE["build_queues"],
            "training_queues": BOT_STATE["training_queues"],
            "build_templates": BOT_STATE.get("build_templates", {})
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