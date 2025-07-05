# dashboard.py

from flask import Flask, render_template_string
from flask_socketio import SocketIO
import copy
import time
from config import BOT_STATE, state_lock, save_config, log, setup_logging
from bot import BotManager
# Add this line to import the necessary classes
from client import TravianClient
from bs4 import BeautifulSoup
from datetime import datetime, timedelta
import re


app = Flask(__name__)
app.config["SECRET_KEY"] = "8b8e2d43‐dashboard‐secret"
socketio = SocketIO(app, async_mode="eventlet")

# Setup logging to include the socketio handler
setup_logging(socketio)

# The BotManager now runs continuously, managing agents based on their 'active' state.
bot_manager_thread = None

@app.route("/")
def index_route():
    try:
        # Start the BotManager on the first request
        global bot_manager_thread
        if bot_manager_thread is None or not bot_manager_thread.is_alive():
            log.info("Starting bot manager thread...")
            bot_manager_thread = BotManager(socketio)
            bot_manager_thread.start()

        with open("index.html", "r", encoding="utf-8") as f:
            return render_template_string(f.read())
    except FileNotFoundError:
        return "Error: index.html not found.", 404

@socketio.on('connect')
def on_connect():
    log.info("Dashboard connected.")
    with state_lock:
        socketio.emit("state_update", copy.deepcopy(BOT_STATE))

@socketio.on('start_account')
def handle_start_account(data):
    username = data.get('username')
    log.info(f"UI request to START account: {username}")
    with state_lock:
        for acc in BOT_STATE['accounts']:
            if acc['username'] == username:
                acc['active'] = True
                break
    save_config()

@socketio.on('stop_account')
def handle_stop_account(data):
    username = data.get('username')
    log.info(f"UI request to STOP account: {username}")
    with state_lock:
        for acc in BOT_STATE['accounts']:
            if acc['username'] == username:
                acc['active'] = False
                break
    save_config()

# dashboard.py

@socketio.on('add_build_task')
def handle_add_build_task(data):
    """
    Handles adding a single new build task from the UI and nudges the
    corresponding agent to act on it immediately.
    """
    village_id = data.get('villageId')
    task = data.get('task')
    if not village_id or not task:
        return

    log.info(f"UI request to ADD build task for village {village_id}: {task}")
    with state_lock:
        if str(village_id) not in BOT_STATE['build_queues']:
            BOT_STATE['build_queues'][str(village_id)] = []
        BOT_STATE['build_queues'][str(village_id)].append(task)
    save_config()

    # Find the running agent and wake it up by setting its next check time to now.
    if bot_manager_thread and bot_manager_thread.is_alive():
        with state_lock:
            agents_found = False
            for username, agents in bot_manager_thread.running_account_agents.items():
                for agent in agents:
                    if str(agent.village_id) == str(village_id):
                        log.info(f"Nudging agent for village {agent.village_name} to check new build task.")
                        # This now works because the `time` module is imported
                        agent.next_check_time = time.time()
                        agents_found = True
                        break
                if agents_found:
                    break

@socketio.on('add_account')
def handle_add_account(data):
    log.info("Adding account: %s", data['username'])
    
    is_sitter = data.get("is_sitter", False)
    sitter_for = data.get("sitter_for", "")
    username = data["username"]

    if is_sitter and sitter_for:
        # Create a unique username for the sitter account
        username = f"{username}_{sitter_for}"

    with state_lock:
        # Make the check case-insensitive to avoid duplicates like "zero" and "Zero"
        if any(a['username'].lower() == username.lower() for a in BOT_STATE['accounts']):
            log.warning("Account %s already exists.", username)
            return
        
        new_account = {
            "username": username, # Use the potentially new username
            "password": data["password"],
            "server_url": data["server_url"],
            "is_sitter": is_sitter,
            "sitter_for": sitter_for,
            "login_username": data["username"], # Store the original username for login
            "tribe": data.get("tribe", "roman"),
            "use_dual_queue": data.get("use_dual_queue", False),
            "use_hero_resources": data.get("use_hero_resources", False),
            "building_logic": data.get("building_logic", "default"),
            "active": False,
            "proxy": {
                "ip": data.get('proxy_ip', ''),
                "port": data.get('proxy_port', ''),
                "username": data.get('proxy_user', ''),
                "password": data.get('proxy_pass', '')
            }
        }
        
        BOT_STATE['accounts'].append(new_account)
    save_config()

@socketio.on('update_account_setting')
def handle_update_account_setting(data):
    username = data.get('username')
    key = data.get('key')
    value = data.get('value')
    log.info(f"Updating setting '{key}' for account {username} to {value}")
    with state_lock:
        for acc in BOT_STATE['accounts']:
            if acc['username'] == username:
                if key.startswith('proxy_'):
                    proxy_key = key.split('_', 1)[1]
                    if 'proxy' not in acc:
                        acc['proxy'] = {"ip": "", "port": "", "username": "", "password": ""}
                    
                    if proxy_key == 'ip': acc['proxy']['ip'] = value
                    elif proxy_key == 'port': acc['proxy']['port'] = value
                    elif proxy_key == 'user': acc['proxy']['username'] = value
                    elif proxy_key == 'pass': acc['proxy']['password'] = value
                else:
                    acc[key] = value
                break
    save_config()

@socketio.on('remove_account')
def handle_remove_account(data):
    username_to_remove = data.get('username')
    log.info("Removing account: %s", username_to_remove)
    with state_lock:
        BOT_STATE['accounts'] = [acc for acc in BOT_STATE['accounts'] if acc['username'] != username_to_remove]
    save_config()
    
@socketio.on('update_build_queue')
def handle_update_build_queue(data):
    village_id = data.get('villageId')
    queue = data.get('queue')
    if village_id and queue is not None:
        log.info("Updating build queue for village %s", village_id)
        with state_lock:
            BOT_STATE['build_queues'][str(village_id)] = queue
        save_config()

@socketio.on('move_build_queue_item')
def handle_move_build_queue_item(data):
    village_id = str(data.get('villageId'))
    index = int(data.get('index'))
    direction = data.get('direction')

    with state_lock:
        queue = BOT_STATE['build_queues'].get(village_id, [])
        if not (0 <= index < len(queue)):
            return

        item = queue.pop(index)

        if direction == 'top':
            queue.insert(0, item)
        elif direction == 'bottom':
            queue.append(item)
        elif direction == 'up':
            queue.insert(max(0, index - 1), item)
        elif direction == 'down':
            queue.insert(min(len(queue), index + 1), item)

        BOT_STATE['build_queues'][village_id] = queue
    
    save_config()

@socketio.on('update_hero_settings')
def handle_update_hero_settings(data):
    username = data.get('username')
    settings = data.get('settings')
    log.info(f"Updating hero settings for account {username}")
    with state_lock:
        for acc in BOT_STATE['accounts']:
            if acc['username'] == username:
                if 'hero_settings' not in acc:
                    acc['hero_settings'] = {}
                acc['hero_settings'].update(settings)
                break
    save_config()

@socketio.on('save_build_template')
def handle_save_build_template(data):
    template_name = data.get('templateName')
    village_id = data.get('villageId')
    with state_lock:
        if 'build_templates' not in BOT_STATE:
            BOT_STATE['build_templates'] = {}
        BOT_STATE['build_templates'][template_name] = BOT_STATE['build_queues'].get(str(village_id), [])
    save_config()

@socketio.on('load_build_template')
def handle_load_build_template(data):
    template_name = data.get('templateName')
    village_id = data.get('villageId')
    with state_lock:
        if 'build_templates' in BOT_STATE and template_name in BOT_STATE['build_templates']:
            BOT_STATE['build_queues'][str(village_id)] = BOT_STATE['build_templates'][template_name]
    save_config()

@socketio.on('delete_build_template')
def handle_delete_build_template(data):
    template_name = data.get('templateName')
    with state_lock:
        if 'build_templates' in BOT_STATE and template_name in BOT_STATE['build_templates']:
            del BOT_STATE['build_templates'][template_name]
    save_config()

@socketio.on('update_training_queues')
def handle_update_training_queues(data):
    village_id = data.get('villageId')
    settings = data.get('settings')
    if village_id and settings is not None:
        log.info(f"Updating training queue for village {village_id}")
        with state_lock:
            if 'training_queues' not in BOT_STATE:
                BOT_STATE['training_queues'] = {}
            if str(village_id) not in BOT_STATE['training_queues']:
                BOT_STATE['training_queues'][str(village_id)] = {}
            BOT_STATE['training_queues'][str(village_id)].update(settings)
        save_config()

@socketio.on('update_demolish_queue')
def handle_update_demolish_queue(data):
    village_id = data.get('villageId')
    queue = data.get('queue')
    if village_id and queue is not None:
        log.info(f"Updating demolish queue for village {village_id}")
        with state_lock:
            if 'demolish_queues' not in BOT_STATE:
                BOT_STATE['demolish_queues'] = {}
            BOT_STATE['demolish_queues'][str(village_id)] = queue
        save_config()

@socketio.on('update_smithy_upgrades')
def handle_update_smithy_upgrades(data):
    village_id = data.get('villageId')
    settings = data.get('settings')
    if village_id and settings is not None:
        log.info(f"Updating smithy upgrades for village {village_id}")
        with state_lock:
            if 'smithy_upgrades' not in BOT_STATE:
                BOT_STATE['smithy_upgrades'] = {}
            if str(village_id) not in BOT_STATE['smithy_upgrades']:
                BOT_STATE['smithy_upgrades'][str(village_id)] = {}
            BOT_STATE['smithy_upgrades'][str(village_id)].update(settings)
        save_config()
        
@socketio.on('copy_settings')
def handle_copy_settings(data):
    source_village_id = data.get('source_village_id')
    target_village_id = data.get('target_village_id')
    setting_type = data.get('setting_type') # 'training' or 'smithy'

    log.info(f"Copying {setting_type} settings from village {source_village_id} to {target_village_id}.")

    with state_lock:
        # Find the source account and all its villages
        source_account_username = None
        for username, village_list in BOT_STATE.get("village_data", {}).items():
            if isinstance(village_list, list) and any(str(v['id']) == source_village_id for v in village_list):
                source_account_username = username
                break
        
        if not source_account_username:
            log.error(f"Could not find account for source village {source_village_id}.")
            return

        source_key = 'training_queues' if setting_type == 'training' else 'smithy_upgrades'
        source_settings = BOT_STATE.get(source_key, {}).get(source_village_id)

        if not source_settings:
            log.error(f"No {setting_type} settings found for source village {source_village_id}.")
            return

        all_villages_for_account = BOT_STATE.get("village_data", {}).get(source_account_username, [])
        
        target_village_ids = []
        if target_village_id == '__ALL__':
            target_village_ids = [str(v['id']) for v in all_villages_for_account if str(v['id']) != source_village_id]
        else:
            target_village_ids.append(target_village_id)
        
        for target_id in target_village_ids:
            target_village_details = BOT_STATE.get("village_data", {}).get(target_id)
            if not target_village_details:
                log.warning(f"Skipping copy for {target_id}: no village details found.")
                continue

            log.info(f"Applying {setting_type} settings to village ID {target_id}")

            if setting_type == 'training':
                new_target_settings = copy.deepcopy(source_settings)
                new_target_settings['buildings'] = {}

                for building_key, building_setting in source_settings.get('buildings', {}).items():
                    source_gid = building_setting.get('gid')
                    
                    if any(b.get('gid') == source_gid for b in target_village_details.get('buildings', [])):
                        new_target_settings['buildings'][building_key] = building_setting
                        log.info(f"  - Copied setting for {building_key} (GID: {source_gid})")
                    else:
                        log.info(f"  - Skipped {building_key} (GID: {source_gid}) - building not found in target village.")
                
                BOT_STATE.setdefault(source_key, {})[target_id] = new_target_settings

            elif setting_type == 'smithy':
                # For smithy, we just check if the building exists and copy everything.
                if any(b.get('gid') == 13 for b in target_village_details.get('buildings', [])):
                     BOT_STATE.setdefault(source_key, {})[target_id] = copy.deepcopy(source_settings)
                     log.info(f"  - Copied smithy settings.")
                else:
                    log.info(f"  - Skipped smithy settings - building not found in target village.")


    save_config()
    log.info(f"Finished copying {setting_type} settings.")

@socketio.on('set_lowest_training_time')
def handle_set_lowest_training_time(data):
    village_id = data.get('villageId')
    log.info(f"UI request to set lowest training time for village {village_id}")

    # Find the relevant account info to create a temporary client
    with state_lock:
        account_info = None
        for acc in BOT_STATE.get("accounts", []):
            villages = BOT_STATE.get("village_data", {}).get(acc['username'], [])
            if any(str(v['id']) == village_id for v in villages):
                account_info = acc
                break
    
    if not account_info:
        log.error(f"Could not find account for village {village_id}")
        return

    client = TravianClient(
        account_info['username'],
        account_info['password'],
        account_info['server_url'],
        account_info.get('proxy')
    )
    if not client.login():
        log.error(f"Failed to log in for {account_info['username']}")
        return

    lowest_queue_duration_seconds = float('inf')
    
    with state_lock:
        village_config = BOT_STATE.get('training_queues', {}).get(str(village_id), {})
        buildings = village_config.get('buildings', {})

    for key, b_config in buildings.items():
        if b_config.get('enabled'):
            gid = b_config['gid']
            page_data = client.get_training_page(int(village_id), gid)
            if page_data and 'queue_duration_seconds' in page_data:
                lowest_queue_duration_seconds = min(lowest_queue_duration_seconds, page_data['queue_duration_seconds'])

    if lowest_queue_duration_seconds != float('inf'):
        # Convert seconds to minutes, rounding down to be safe
        lowest_time_minutes = int(lowest_queue_duration_seconds / 60)
        log.info(f"Lowest queue duration found: {lowest_queue_duration_seconds:.2f}s. Setting min queue to {lowest_time_minutes} minutes.")
        with state_lock:
            BOT_STATE['training_queues'][str(village_id)]['min_queue_duration_minutes'] = lowest_time_minutes
        save_config()


@socketio.on('set_end_time_from_infobox')
def handle_set_end_time_from_infobox(data):
    village_id = data.get('villageId')
    time_type = data.get('timeType') # 'ww' or 'artefacts'
    log.info(f"UI request to set end time from infobox ({time_type}) for village {village_id}")

    with state_lock:
        account_info = None
        for acc in BOT_STATE.get("accounts", []):
            villages = BOT_STATE.get("village_data", {}).get(acc['username'], [])
            if any(str(v['id']) == village_id for v in villages):
                account_info = acc
                break

    if not account_info:
        log.error(f"Could not find account for village {village_id}")
        return

    client = TravianClient(
        account_info['username'],
        account_info['password'],
        account_info['server_url'],
        account_info.get('proxy')
    )
    if not client.login():
        log.error(f"Failed to log in for {account_info['username']}")
        return
        
    infobox_html = client.get_infobox_html()
    if not infobox_html:
        log.error("Failed to retrieve infobox HTML.")
        return

    soup = BeautifulSoup(infobox_html, 'html.parser')
    
    search_text = "WW plans" if time_type == 'ww' else "Artifacts"
    
    target_timer = None
    all_list_items = soup.select('ul > li')
    for item in all_list_items:
        if search_text in item.get_text():
            target_timer = item.find('span', class_='timer')
            break

    if target_timer and 'value' in target_timer.attrs:
        try:
            seconds_to_add = int(target_timer['value'])
            end_time = datetime.now() + timedelta(seconds=seconds_to_add)
            formatted_end_time = end_time.strftime('%d.%m.%Y %H:%M')
            
            log.info(f"Found timer value: {seconds_to_add}s. Calculated end time: {formatted_end_time}")
            
            with state_lock:
                BOT_STATE['training_queues'][str(village_id)]['max_training_time'] = formatted_end_time
            save_config()
        except (ValueError, TypeError):
            log.error("Could not parse timer value from infobox.")
    else:
        log.warning(f"Could not find a timer for '{search_text}' in the infobox.")