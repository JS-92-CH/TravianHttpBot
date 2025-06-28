from flask import Flask, render_template_string
from flask_socketio import SocketIO

from config import BOT_STATE, state_lock, save_config, log
from bot import BotManager

app = Flask(__name__)
app.config["SECRET_KEY"] = "8b8e2d43‐dashboard‐secret"
socketio = SocketIO(app, async_mode="threading")

bot_manager_thread = None

@app.route("/")
def index_route():
    try:
        with open("index.html", "r", encoding="utf-8") as f:
            return render_template_string(f.read())
    except FileNotFoundError:
        return "Error: index.html not found.", 404

@socketio.on('connect')
def on_connect():
    log.info("Dashboard connected.")
    with state_lock:
        socketio.emit("state_update", BOT_STATE)

@socketio.on('start_bot')
def handle_start_bot(data=None):
    global bot_manager_thread
    if bot_manager_thread is None or not bot_manager_thread.is_alive():
        log.info("Starting bot manager...")
        bot_manager_thread = BotManager(socketio)
        bot_manager_thread.start()
    else:
        log.info("Bot is already running.")

@socketio.on('stop_bot')
def handle_stop_bot(data=None):
    global bot_manager_thread
    if bot_manager_thread and bot_manager_thread.is_alive():
        log.info("Stopping bot manager...")
        bot_manager_thread.stop()
        bot_manager_thread.join()
        bot_manager_thread = None
    else:
        log.info("Bot is not running.")

@socketio.on('add_account')
def handle_add_account(data):
    log.info("Adding account: %s", data['username'])
    with state_lock:
        if any(a['username'] == data['username'] for a in BOT_STATE['accounts']):
            log.warning("Account %s already exists.", data['username'])
            return
        
        new_account = {
            "username": data["username"],
            "password": data["password"],
            "server_url": data["server_url"],
            "tribe": data.get("tribe", "roman"),
            "use_dual_queue": data.get("use_dual_queue", False),
            "use_hero_resources": data.get("use_hero_resources", False),
            "building_logic": data.get("building_logic", "default")
        }
        
        if data.get('proxy_ip') and data.get('proxy_port'):
            new_account['proxy'] = {
                "ip": data.get('proxy_ip'),
                "port": data.get('proxy_port'),
                "username": data.get('proxy_user', ''),
                "password": data.get('proxy_pass', '')
            }
        
        BOT_STATE['accounts'].append(new_account)
    save_config()
    socketio.emit("state_update", BOT_STATE)

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
    socketio.emit("state_update", BOT_STATE)

@socketio.on('remove_account')
def handle_remove_account(data):
    username_to_remove = data.get('username')
    log.info("Removing account: %s", username_to_remove)
    with state_lock:
        BOT_STATE['accounts'] = [acc for acc in BOT_STATE['accounts'] if acc['username'] != username_to_remove]
    save_config()
    socketio.emit("state_update", BOT_STATE)
    
@socketio.on('add_build_task')
def handle_add_build_task(data):
    village_id = data.get('villageId')
    task = data.get('task')
    if village_id and task:
        log.info(f"Adding task to build queue for village {village_id}: {task}")
        with state_lock:
            if str(village_id) not in BOT_STATE['build_queues']:
                BOT_STATE['build_queues'][str(village_id)] = []
            BOT_STATE['build_queues'][str(village_id)].append(task)
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

@socketio.on('move_build_queue_item')
def handle_move_build_queue_item(data):
    village_id = str(data.get('villageId'))
    index = int(data.get('index'))
    direction = data.get('direction')

    with state_lock:
        queue = BOT_STATE['build_queues'].get(village_id, [])
        if not queue or not (0 <= index < len(queue)):
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
    socketio.emit("state_update", BOT_STATE)

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
    socketio.emit("state_update", BOT_STATE)

@socketio.on('save_build_template')
def handle_save_build_template(data):
    template_name = data.get('templateName')
    village_id = data.get('villageId')
    with state_lock:
        if 'build_templates' not in BOT_STATE:
            BOT_STATE['build_templates'] = {}
        BOT_STATE['build_templates'][template_name] = BOT_STATE['build_queues'].get(str(village_id), [])
    save_config()
    socketio.emit("state_update", BOT_STATE)

@socketio.on('load_build_template')
def handle_load_build_template(data):
    template_name = data.get('templateName')
    village_id = data.get('villageId')
    with state_lock:
        if 'build_templates' in BOT_STATE and template_name in BOT_STATE['build_templates']:
            BOT_STATE['build_queues'][str(village_id)] = BOT_STATE['build_templates'][template_name]
    save_config()
    socketio.emit("state_update", BOT_STATE)

@socketio.on('delete_build_template')
def handle_delete_build_template(data):
    template_name = data.get('templateName')
    with state_lock:
        if 'build_templates' in BOT_STATE and template_name in BOT_STATE['build_templates']:
            del BOT_STATE['build_templates'][template_name]
    save_config()
    socketio.emit("state_update", BOT_STATE)

@socketio.on('update_training_queues')
def handle_update_training_queues(data):
    village_id = data.get('villageId')
    settings = data.get('settings')
    if village_id and settings is not None:
        log.info(f"Updating training queue for village {village_id}")
        with state_lock:
            if 'training_queues' not in BOT_STATE:
                BOT_STATE['training_queues'] = {}
            BOT_STATE['training_queues'][str(village_id)] = settings
        save_config()
        socketio.emit("state_update", BOT_STATE)

@socketio.on('copy_training_settings')
def handle_copy_training_settings(data):
    source_village_id_str = data.get('villageId')
    log.info(f"Copying training settings from village {source_village_id_str} to all others.")

    with state_lock:
        # Find the source account and all its villages
        source_account_username = None
        for username, village_list in BOT_STATE.get("village_data", {}).items():
            if isinstance(village_list, list) and any(str(v['id']) == source_village_id_str for v in village_list):
                source_account_username = username
                break
        
        if not source_account_username:
            log.error(f"Could not find account for source village {source_village_id_str}.")
            return

        source_settings = BOT_STATE.get("training_queues", {}).get(source_village_id_str)
        if not source_settings:
            log.error(f"No training settings found for source village {source_village_id_str}.")
            return

        all_villages_for_account = BOT_STATE.get("village_data", {}).get(source_account_username, [])
        
        for target_village in all_villages_for_account:
            target_village_id_str = str(target_village['id'])
            if target_village_id_str == source_village_id_str:
                continue

            # Get details of the target village to check for buildings
            target_village_details = BOT_STATE.get("village_data", {}).get(target_village_id_str)
            if not target_village_details:
                continue

            log.info(f"Applying settings to village {target_village['name']} ({target_village_id_str})")
            
            # Start with a fresh copy of the source settings
            new_target_settings = source_settings.copy()
            new_target_settings['buildings'] = {}

            # Iterate through building types (barracks, stable, etc.) from the source
            for building_key, building_setting in source_settings.get('buildings', {}).items():
                source_gid = building_setting.get('gid')
                
                # Check if the target village has a building with the same GID
                if any(b.get('gid') == source_gid for b in target_village_details.get('buildings', [])):
                    new_target_settings['buildings'][building_key] = building_setting
                    log.info(f"  - Copied setting for {building_key} (GID: {source_gid})")
                else:
                    log.info(f"  - Skipped {building_key} (GID: {source_gid}) - building not found in target village.")

            BOT_STATE['training_queues'][target_village_id_str] = new_target_settings

    save_config()
    socketio.emit("state_update", BOT_STATE)
    log.info("Finished copying training settings.")