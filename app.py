"""
Flask dashboard + JSON API for fan-bridge.

The page (static/index.html) is fully client-side: it polls /api/state and
/api/history every second and saves changes through /api/settings.
"""

import os

from flask import Flask, jsonify, request, send_from_directory

import config
import ilo_fan
import loop

app = Flask(__name__, static_folder=None)
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

# Started once at import time. gunicorn must run with --workers 1 (see
# entrypoint.sh) or several loops would fight over the iLO setting.
if os.environ.get("FAN_BRIDGE_NO_LOOP") != "1":
    loop.start_background_thread()


@app.route("/")
def dashboard():
    resp = send_from_directory(STATIC_DIR, "index.html")
    resp.headers["Cache-Control"] = "no-cache"
    return resp


@app.route("/api/state")
def api_state():
    return jsonify({"settings": config.load(), "state": loop.get_state_snapshot()})


@app.route("/api/history")
def api_history():
    since = request.args.get("since", 0, type=float)
    return jsonify({"history": loop.get_history(since)})


@app.route("/api/settings", methods=["POST"])
def api_settings():
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return jsonify({"error": "expected a JSON object"}), 400
    try:
        settings = config.save(body)
    except (ValueError, TypeError, IndexError) as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"ok": True, "settings": settings})


@app.route("/api/fans")
def api_fans():
    """Raw fan list from iLO, for diagnostics."""
    try:
        ilo = ilo_fan.IloFanController()
        return jsonify(ilo.read_status())
    except Exception as e:
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
