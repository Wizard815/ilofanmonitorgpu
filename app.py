"""
Flask dashboard + JSON API for fan-bridge.

The page (static/index.html) is fully client-side: it polls /api/state and
/api/history every second and saves changes through /api/settings.
"""

import hmac
import os

from flask import Flask, jsonify, request, send_from_directory

import config
import ilo_fan
import jbod
import loop
import power

app = Flask(__name__, static_folder=None)
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

# FAN_BRIDGE_TOKEN, when set, must come in the X-Fan-Token header of every
# request that changes something (settings, presets, the self-test, power).
# Reads stay open: the dashboard and Argos poll them. Without it anyone who can
# reach port 5000 can change the fan curves, and can point jbod_host at their
# own machine to be sent JBOD_TOKEN.
TOKEN = os.environ.get("FAN_BRIDGE_TOKEN", "").strip()


def _same(got, want) -> bool:
    return bool(want) and hmac.compare_digest(str(got or "").encode(), want.encode())


@app.before_request
def _writes_need_the_token():
    if request.method in ("GET", "HEAD", "OPTIONS") or not TOKEN:
        return None
    if _same(request.headers.get("X-Fan-Token"), TOKEN):
        return None
    # Argos sends only the power token, and only to /api/power.
    if request.path == "/api/power" and _same(request.headers.get("X-Power-Token"), power.TOKEN):
        return None
    return jsonify({"error": "X-Fan-Token missing or wrong", "token": True}), 401

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


@app.route("/api/power", methods=["GET"])
def api_power_get():
    """Each MI50's power cap, allowed range, clock levels and any clock limit, plus the fan state, for Argos."""
    st = loop.get_state_snapshot()
    temps = {g.get("pci"): g for g in st.get("gpus") or []}
    cards = [{**power.public(c), **{k: temps.get(c["pci"], {}).get(src) for k, src in
                                    (("edge_c", "edge_c"), ("junction_c", "junction_c"), ("mem_c", "memory_c"))}}
             for c in power.cards()]
    return jsonify({"cards": cards, "guard_c": power.GUARD_C,
                    "fans": {"phase": st.get("phase"), "floor_pct": st.get("floor_pct"), "control_c": st.get("control_c")}})


@app.route("/api/power", methods=["POST"])
def api_power_set():
    """Body {"cards": {pci: {"cap_w": W or null (stock), "sclk_mhz": MHz or null (no limit)}}}. A raise is
    refused while a GPU is at or above the guard temperature or the loop is in failsafe; a cut never is."""
    if power.TOKEN and not _same(request.headers.get("X-Power-Token"), power.TOKEN) \
            and not _same(request.headers.get("X-Fan-Token"), TOKEN):
        return jsonify({"error": "X-Power-Token missing or wrong"}), 403
    body = request.get_json(silent=True)
    if not isinstance(body, dict) or not isinstance(body.get("cards"), dict):
        return jsonify({"error": "expected {\"cards\": {pci: {cap_w, sclk_mhz}}}"}), 400
    st = loop.get_state_snapshot()
    applied, errors = power.apply(body["cards"], control_c=st.get("control_c"), failsafe=st.get("phase") == "failsafe",
                                  log=loop._log)
    return jsonify({"ok": not errors, "applied": applied, "errors": errors,
                    "cards": [power.public(c) for c in power.cards()]}), (200 if applied or not errors else 409)


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
