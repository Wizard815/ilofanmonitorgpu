"""
Flask dashboard + JSON API for fan-bridge.

The page (static/index.html) is fully client-side: it polls /api/state and
/api/history every second and saves changes through /api/settings.
"""

import os

from flask import Flask, jsonify, request, send_from_directory

import config
import ilo_fan
import jbod
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


def _settings_payload(s: dict) -> dict:
    return {**s, "active_preset": config.active_preset(s),
            "preset_list": [{"name": n, "builtin": n in config.BUILTIN_PRESETS, **p}
                            for n, p in config.all_presets(s).items()]}


@app.route("/api/state")
def api_state():
    return jsonify({"settings": _settings_payload(config.load()), "state": loop.get_state_snapshot(),
                    "jbod": jbod.snapshot()})


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
    return jsonify({"ok": True, "settings": _settings_payload(settings)})


@app.route("/api/presets/<action>", methods=["POST"])
def api_presets(action):
    """apply / save / delete, body {"name": "..."}. save stores the current
    curve + load minimum + timing under that name."""
    body = request.get_json(silent=True) or {}
    fn = {"apply": config.apply_preset, "save": config.save_preset, "delete": config.delete_preset}.get(action)
    if fn is None:
        return jsonify({"error": "unknown action"}), 404
    try:
        settings = fn(str(body.get("name", "")))
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"ok": True, "settings": _settings_payload(settings)})


@app.route("/api/jbod/selftest", methods=["POST"])
def api_jbod_selftest():
    """Start the ESP32's fan wiring self-test (~36 s; results in state.jbod.esp.selftest)."""
    try:
        return jsonify({"ok": True, "esp": jbod.start_selftest(config.load()["jbod_host"])})
    except Exception as e:
        return jsonify({"error": f"ESP32 not reachable: {e}"}), 502


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
