from dashboard import app, socketio
from config import load_config, log

if __name__ == "__main__":
    load_config()
    log.info("Dashboard available at http://127.0.0.1:5000")
    socketio.run(app, host="0.0.0.0", port=5000)