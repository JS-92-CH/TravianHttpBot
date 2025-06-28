# dashboard.py

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
        
        # Add proxy settings if they exist
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
    """Updates a specific setting for an account."""
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
    """Updates the hero settings for an account."""
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

@socketio.on('update_training_queues')
def handle_update_training_queues(data):
    """Updates the training queues for a village."""
    village_id = data.get('villageId')
    settings = data.get('settings')
    log.info(f"Updating training queues for village {village_id}")
    with state_lock:
        if str(village_id) not in BOT_STATE['training_queues']:
            BOT_STATE['training_queues'][str(village_id)] = {}
        BOT_STATE['training_queues'][str(village_id)] = settings
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
            BOT_STATE['build_queues'][str(village_id)] = list(BOT_STATE['build_templates'][template_name])
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