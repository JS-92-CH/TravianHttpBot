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

# In‑memory state that gets pushed to the dashboard
BOT_STATE: Dict[str, Any] = {
    "accounts": [],               # List of account dicts → {username, password, server_url}
    "villages": {},               # village_id → last scraped village snapshot
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
        log.info("Configuration loaded ✔")
    except Exception as exc:
        log.warning("Could not read config.json → %s", exc)


def save_config() -> None:
    """Persist accounts + queues back to disk."""
    with state_lock:
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
        24: "Town Hall", 25: "Residence", 26: "Palace", 42: "Tournament Square",
    }
    return mapping.get(int(gid), f"GID {gid}")

# ─────────────────────────────────────────
# DASHBOARD – Flask + SocketIO
# ─────────────────────────────────────────

app = Flask(__name__)
app.config["SECRET_KEY"] = "8b8e2d43‐dashboard‐secret"
socketio = SocketIO(app, async_mode="threading")

def _ensure_index_html() -> None:
    """Drop a *very* simple dashboard template the first time we run."""
    if os.path.exists("index.html"):
        return
    tmpl = """<!DOCTYPE html><html><head><title>Travian Bot Dashboard</title><style>body{font-family:Segoe UI,Arial,sans-serif;margin:0;padding:1rem;background:#f5f4f2;color:#333}h1{margin-top:0}.flex{display:flex;gap:1rem}.card{background:#fff;padding:1rem;border-radius:8px;box-shadow:0 2px 4px rgba(0,0,0,.1);flex:1;overflow:auto}button{cursor:pointer;padding:.5rem 1rem;border:0;border-radius:4px;background:#4caf50;color:#fff;margin-right:.5rem}button.danger{background:#e74c3c}</style><script src="https://cdnjs.cloudflare.com/ajax/libs/socket.io/4.7.5/socket.io.min.js"></script></head><body><h1>Travian Bot Dashboard</h1><div class="flex"><div class="card" style="flex:0 0 250px"><h2>Controls</h2><button id="btn-start">Start</button><button id="btn-stop" class="danger">Stop</button><hr><h3>Accounts</h3><ul id="accounts"></ul></div><div class="card"><h2 id="village-title">Villages</h2><pre id="village-json" style="white-space:pre-wrap"></pre></div></div><div class="card" style="margin-top:1rem"><h2>Live log</h2><pre id="log" style="white-space:pre-wrap;height:200px;overflow:auto"></pre></div><script>const s=io();const logDiv=document.getElementById("log");const accountsUl=document.getElementById("accounts");const vJson=document.getElementById("village-json");const vTitle=document.getElementById("village-title");document.getElementById("btn-start").onclick=()=>s.emit("start_bot");document.getElementById("btn-stop").onclick=()=>s.emit("stop_bot");s.on("log",m=>{logDiv.textContent+=m+"\n";logDiv.scrollTop=logDiv.scrollHeight});s.on("state",st=>{accountsUl.innerHTML=st.accounts.map(a=>`<li>${a.username}@${a.server_url}</li>`).join("");vJson.textContent=JSON.stringify(st.villages,null,2);vTitle.textContent=`Villages (${Object.keys(st.villages).length})`;});</script></body></html>"""
    with open("index.html", "w", encoding="utf‑8") as fh:
        fh.write(tmpl)


@app.route("/")
def index_route():
    return render_template_string(open("index.html", encoding="utf‑8").read())

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
            "User-Agent": "Mozilla/5.0 (TravianBot)",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        })

    # ── authentication ──────────────────

    def login(self) -> bool:
        log.info("[%s] Logging in via API …", self.username)
        try:
            self.sess.get(f"{self.server_url}/")
        except requests.RequestException as exc:
            log.error("[%s] Cannot reach server: %s", self.username, exc)
            return False

        api_url = f"{self.server_url}/api/v1/auth/login"
        payload = {
            "name": self.username,
            "password": self.password,
            "w": "1920:1080",
            "mobileOptimizations": False,
        }
        headers = {
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Content-Type": "application/json; charset=UTF-8",
            "X-Requested-With": "XMLHttpRequest",
            "X-Version": "2554.3",
            "Referer": f"{self.server_url}/",
        }
        try:
            resp = self.sess.post(api_url, json=payload, headers=headers, timeout=15)
            resp.raise_for_status()
            if "redirectTo" not in resp.json():
                raise ValueError("API did not return a redirect → bad credentials?")
            log.info("[%s] Logged in successfully ✔", self.username)
            return True
        except Exception as exc:
            log.error("[%s] Login failed ✖ – %s", self.username, exc)
            return False

    # ── scraping helpers ────────────────

    def parse_village_page(self, html: str) -> Dict[str, Any]:
        """Extract resources, buildings & queued upgrades from village HTML."""
        soup = BeautifulSoup(html, "html.parser")
        out: Dict[str, Any] = {
            "resources": {},
            "storage": {},
            "production": {},
            "buildings": [],
            "queue": [],
            "villages": [],
        }

        # ① resources block (inside a <script>)
        try:
            script = soup.find("script", string=re.compile(r"var\s+resources\s*="))
            if script:
                m = re.search(r"var\s+resources\s*=\s*(\{.*?\});", script.string, re.DOTALL)
                if m:
                    fixed = re.sub(r"([a-zA-Z_][\w]*)\s*:", r'"\1":', m.group(1))
                    res = json.loads(fixed)
                    out["resources"] = {k: int(v) for k, v in res["storage"].items()}
                    out["storage"] = {k: int(v) for k, v in res["maxStorage"].items()}
                    out["production"] = {k: int(v) for k, v in res["production"].items()}
        except Exception as exc:
            log.debug("resource parser failed – %s", exc)

        # ② buildings
        for link in soup.select("#resourceFieldContainer a, #villageContent a"):
            if "build.php?id=" not in link.get("href", ""):
                continue
            lvl_div = link.find("div", class_="labelLayer")
            if not lvl_div:
                continue
            gid_cls = next((c for c in link.get("class", []) if c.startswith("gid")), None)
            out["buildings"].append({
                "id": int(re.search(r"id=(\d+)", link["href"]).group(1)),
                "level": int(lvl_div.text.strip()),
                "gid": int(gid_cls[3:]) if gid_cls else 0,
            })

        # ③ server build queue
        for li in soup.select(".buildingList li"):
            name_div = li.find("div", class_="name")
            if not name_div:
                continue
            out["queue"].append({
                "name": name_div.text.split("\n")[0].strip(),
                "level": li.find("span", class_="lvl").text.strip(),
                "eta": int(li.find("span", class_="timer")["value"]),
            })

        # ④ villages sidebar – so the Agent can spawn for each
        for v in soup.select("#sidebarBoxVillageList .listEntry"):
            link = v.find("a", href=re.compile(r"newdid="))
            if not link:
                continue
            out["villages"].append({
                "id": int(re.search(r"newdid=(\d+)", link["href"]).group(1)),
                "name": v.find("span", class_="name").text.strip(),
                "active": "active" in v.get("class", []),
            })
        return out

    # ── high‑level helpers ───────────────

    def fetch_v
