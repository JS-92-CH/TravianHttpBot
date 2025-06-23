from flask import Flask, render_template_string
from flask_socketio import SocketIO

from config import BOT_STATE, state_lock, save_config, log
from bot import BotThread

app = Flask(__name__)
app.config["SECRET_KEY"] = "8b8e2d43‐dashboard‐secret"
socketio = SocketIO(app, async_mode="threading")

bot_thread = None

@app.route("/")
def index_route():
    try:
        with open("index.html", "r", encoding="utf-8") as f:
            return render_template_string(f.read())
    except FileNotFoundError:
        return "Error: index.html not found. Please create this file for the dashboard.", 404

@socketio.on('connect')
def on_connect():
    log.info("Dashboard connected.")
    socketio.emit("state_update", BOT_STATE)

@socketio.on('start_bot')
def handle_start_bot(data=None):
    global bot_thread
    if bot_thread is None or not bot_thread.is_alive():
        log.info("Starting bot...")
        # FIX: Pass the socketio instance to the bot thread constructor
        bot_thread = BotThread(socketio)
        bot_thread.start()
        socketio.emit("log_message", {'data': "Bot started."})
    else:
        log.info("Bot is already running.")
        socketio.emit("log_message", {'data': "Bot is already running."})

@socketio.on('stop_bot')
def handle_stop_bot(data=None):
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