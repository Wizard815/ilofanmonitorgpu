"""
Flask UI for mi50-fan-bridge.

Single-page dashboard: live GPU temps, current fan floor, recent log,
and an editable settings form (persisted via config.py to a JSON file
on a mounted volume -- no env var / compose editing needed day-to-day).
"""

from flask import Flask, jsonify, render_template_string, request

import config
import ilo_fan
import loop

app = Flask(__name__)

# Started once at import time -- safe as long as we run gunicorn with a
# single worker (--workers 1), which is plenty for a small local dashboard
# and avoids multiple copies of the poll loop fighting over iLO/state.
loop.start_background_thread()


PAGE = """
<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <title>MI50 Fan Bridge</title>
  <meta http-equiv="refresh" content="10">
  <style>
    body { font-family: -apple-system, sans-serif; max-width: 900px; margin: 2rem auto; padding: 0 1rem; background: #14161a; color: #e6e6e6; }
    h1 { font-size: 1.4rem; }
    h2 { font-size: 1.1rem; margin-top: 2rem; border-bottom: 1px solid #333; padding-bottom: 0.3rem; }
    .card { background: #1e2126; border-radius: 8px; padding: 1rem 1.2rem; margin: 1rem 0; }
    .stat { display: inline-block; margin-right: 2rem; }
    .stat .label { color: #999; font-size: 0.8rem; display: block; }
    .stat .value { font-size: 1.6rem; font-weight: 600; }
    .hot { color: #ff6b6b; }
    .ok { color: #6bcf7f; }
    table { width: 100%; border-collapse: collapse; }
    th, td { text-align: left; padding: 0.4rem 0.6rem; border-bottom: 1px solid #2a2d33; }
    input[type=text], input[type=number] { background: #14161a; border: 1px solid #333; color: #eee; padding: 0.3rem 0.5rem; border-radius: 4px; width: 100px; }
    input[type=text] { width: 200px; }
    button { background: #3a6df0; color: white; border: none; padding: 0.5rem 1.2rem; border-radius: 6px; cursor: pointer; font-size: 0.95rem; }
    button.secondary { background: #444; }
    button.danger { background: #b33; }
    label { display: block; margin: 0.6rem 0 0.4rem; color: #ccc; font-size: 0.9rem; max-width: 500px; }
    .log { background: #0e1013; border-radius: 6px; padding: 0.6rem 0.8rem; height: 220px; overflow-y: auto; font-family: monospace; font-size: 0.8rem; white-space: pre-wrap; }
    .flash { background: #234; padding: 0.5rem 1rem; border-radius: 6px; margin-bottom: 1rem; }
    .gpu-block { margin-bottom: 1.1rem; }
    .gpu-block:last-child { margin-bottom: 0; }
    .gpu-name { font-weight: 600; margin-bottom: 0.3rem; }
    .sensor-row { display: flex; justify-content: space-between; padding: 0.2rem 0 0.2rem 1rem; font-size: 0.95rem; border-bottom: 1px solid #2a2d33; }
    .sensor-row:last-child { border-bottom: none; }
    .sensor-label { color: #999; }
    .curve-wrap { margin-top: 0.5rem; }
  </style>
</head>
<body>
  <h1>MI50 Fan Bridge</h1>

  {% if flash %}<div class="flash">{{ flash }}</div>{% endif %}

  <div class="card">
    <div class="stat">
      <span class="label">Hottest GPU</span>
      <span class="value {{ 'hot' if hottest and hottest >= settings.target_max_c else 'ok' }}">
        {{ "%.1f"|format(hottest) if hottest is not none else "--" }}&deg;C
      </span>
    </div>
    <div class="stat">
      <span class="label">Trigger</span>
      <span class="value">{{ settings.target_max_c }}&deg;C</span>
    </div>
    <div class="stat">
      <span class="label">Fan Floor</span>
      <span class="value">{{ state.current_floor_pct if state.current_floor_pct is not none else "--" }}%</span>
    </div>
    <div class="stat">
      <span class="label">Status</span>
      <span class="value">
        {% if settings.paused %}Paused
        {% elif state.phase == 'active' %}Script control
        {% elif state.phase == 'cooldown' %}Cooldown ({{ state.cooldown_remaining_sec }}s)
        {% elif state.phase == 'held' %}Held ({{ state.release_remaining_sec }}s to release)
        {% else %}iLO control
        {% endif %}
      </span>
    </div>
    <div class="stat">
      <span class="label">GPU Perf</span>
      <span class="value">{{ state.perf_mode or "--" }}{% if state.perf_mode == 'high' and settings.perf_control %} ({{ state.perf_idle_remaining_sec }}s to auto){% endif %}</span>
    </div>
    {% if state.last_error %}<p style="color:#ff6b6b">{{ state.last_error }}</p>{% endif %}
  </div>

  <h2>GPU Temps</h2>
  <div class="card">
    {% for t in state.last_temps %}
    <div class="gpu-block">
      <div class="gpu-name">{{ t.card }}</div>
      <div class="sensor-row"><span class="sensor-label">{{ "GPU" if t.fan_curve_kind == "gpu" else "Edge" }}</span><span>{{ "%.1f"|format(t.edge_c) + "&deg;C" if t.edge_c is not none else "--" }}</span></div>
      <div class="sensor-row"><span class="sensor-label">Junction</span><span class="{{ 'hot' if t.junction_c is not none and t.junction_c >= settings.target_max_c else 'ok' }}">{{ "%.1f"|format(t.junction_c) + "&deg;C" if t.junction_c is not none else "--" }}</span></div>
      <div class="sensor-row"><span class="sensor-label">Memory</span><span>{{ "%.1f"|format(t.memory_c) + "&deg;C" if t.memory_c is not none else "--" }}</span></div>
      <div class="sensor-row"><span class="sensor-label">MCLK top / now</span><span>{{ "%d"|format(t.mclk_top_mhz) if t.mclk_top_mhz is not none else "--" }} / {{ "%.0f"|format(t.mclk_mhz) if t.mclk_mhz is not none else "--" }} MHz</span></div>
      <div class="sensor-row"><span class="sensor-label">Power / busy</span><span>{{ "%.0f W"|format(t.power_w) if t.power_w is not none else "--" }} / {{ "%.0f%%"|format(t.busy_pct) if t.busy_pct is not none else "--" }}</span></div>
    </div>
    {% else %}
    <p>No data.</p>
    {% endfor %}
    <p style="color:#999; font-size:0.8rem;">MCLK step (FAN_BRIDGE_MCLK={{ state.mclk.target if state.mclk.target else "off" }}): {{ state.mclk.status }}{% for pci, c in state.mclk.cards.items() %} | {{ pci }}: {{ c.result or "-" }}{% endfor %}</p>
  </div>

  <h2>Settings</h2>
  <div class="card">
    <form method="post" action="/settings" id="settingsForm">
      <label>Fan curve. Blue points: temp/fan% ramp. Red line: trigger, drag its handle at top. Independent of each other.</label>
      <div class="curve-wrap">
        <svg id="curveSvg" viewBox="0 0 560 220" width="100%" style="max-width:560px; touch-action:none;"></svg>
        <p style="color:#999; font-size:0.8rem; margin-top:0.3rem;">
          <span id="curveStatus">{{ "Curve edited." if settings.thresholds_customized else "Default curve." }}</span>
          &nbsp;<button type="button" id="resetCurveBtn" class="secondary" style="padding:0.2rem 0.7rem; font-size:0.8rem;">Reset curve</button>
        </p>
      </div>

      <label style="margin-top:1rem;">Cooldown (sec) &mdash; delay before the script stops actively adjusting once temp drops below trigger.</label>
      <input type="number" name="cooldown_sec" value="{{ settings.cooldown_sec }}">

      <label style="margin-top:1rem;">Release delay (sec) &mdash; after cooldown, how long the floor stays held before resetting to the value below. If temp climbs back above trigger during either delay, both reset.</label>
      <input type="number" name="release_sec" value="{{ settings.release_sec }}">

      <label style="margin-top:1rem;">Fan floor on release (%) &mdash; what the global minimum resets to once fully released back to iLO. 0 means no floor at all.</label>
      <input type="number" name="min_fan_pct" value="{{ settings.min_fan_pct }}">

      <p style="color:#999; font-size:0.8rem; margin-top:1rem;">This fan floor applies to all chassis fans at once -- this iLO firmware doesn't support per-fan overrides.</p>

      <label style="margin-top:1rem;">Poll interval below trigger (sec)</label>
      <input type="number" name="idle_poll_sec" value="{{ settings.idle_poll_sec }}">

      <label>Poll interval above trigger (sec)</label>
      <input type="number" name="active_poll_sec" value="{{ settings.active_poll_sec }}">

      <label style="margin-top:1rem;">Load trigger &mdash; any GPU at or above this power (W) or busy (%) pushes the load floor right away, before the heat arrives.</label>
      <input type="number" name="load_power_w" value="{{ settings.load_power_w }}">
      <input type="number" name="load_busy_pct" value="{{ settings.load_busy_pct }}">

      <label>Load floor (%) &mdash; fan floor while under load (the curve wins if it asks for more). 0 turns the load trigger off.</label>
      <input type="number" name="load_floor_pct" value="{{ settings.load_floor_pct }}">

      <label style="margin-top:1rem;">Step down &mdash; lower the floor only after the lower value has held this long (sec), by at most this much (%) per step.</label>
      <input type="number" name="step_down_sec" value="{{ settings.step_down_sec }}">
      <input type="number" name="step_down_pct" value="{{ settings.step_down_pct }}">

      <label style="margin-top:1rem;"><input type="checkbox" name="perf_control" value="1" {{ 'checked' if settings.perf_control }}> GPU perf on demand &mdash; "high" (top clocks) while the GPUs are in use, "auto" (lower idle power) after the idle time below. Off = always high.</label>
      <label>Idle time before auto (sec)</label>
      <input type="number" name="perf_idle_sec" value="{{ settings.perf_idle_sec }}">
      <label>Activity = any GPU busy (%) or power (W) at or above</label>
      <input type="number" name="perf_busy_pct" value="{{ settings.perf_busy_pct }}">
      <input type="number" name="perf_power_w" value="{{ settings.perf_power_w }}">

      <div style="margin-top:1rem;">
        <button type="submit">Save</button>
        <button type="submit" name="pause" value="{{ 'unpause' if settings.paused else 'pause' }}" class="secondary" formaction="/pause">
          {{ "Resume" if settings.paused else "Pause" }}
        </button>
      </div>
    </form>
  </div>

  <script>
  (function() {
    const svg = document.getElementById('curveSvg');

    // Fixed axes -- never rescale based on data, so dragging can't blow
    // the chart out to absurd values.
    const T_MIN = 40, T_MAX = 110;
    const P_MIN = 0, P_MAX = 100;

    const W = 560, H = 220, padL = 40, padR = 15, padT = 40, padB = 30;
    const plotW = W - padL - padR, plotH = H - padT - padB;
    const MIN_GAP_C = 1;  // curve points can't cross a neighbor closer than this

    // Two COMPLETELY independent pieces of state:
    //   thresholds -- 7 fan-curve points, nothing to do with the trigger.
    //   triggerC   -- the standalone vertical marker. Dragging it never
    //                 touches thresholds; dragging a curve point never
    //                 touches triggerC. They just happen to share axes.
    let thresholds = {{ settings.thresholds|tojson }};
    let triggerC = {{ settings.target_max_c }};

    function clamp(v, lo, hi) { return Math.max(lo, Math.min(hi, v)); }
    function xFor(t) { return padL + (clamp(t, T_MIN, T_MAX) - T_MIN) / (T_MAX - T_MIN) * plotW; }
    function tFromX(x) { return clamp(T_MIN + (x - padL) / plotW * (T_MAX - T_MIN), T_MIN, T_MAX); }
    function yFor(p) { return padT + (1 - clamp(p, P_MIN, P_MAX) / 100) * plotH; }
    function pFromY(y) { return Math.round(clamp((1 - (y - padT) / plotH) * 100, P_MIN, P_MAX)); }

    function render() {
      let s = '';
      // fixed y-axis gridlines, 0-100%, never recalculated from data
      [0, 25, 50, 75, 100].forEach(pct => {
        const y = yFor(pct);
        s += `<line x1="${padL}" y1="${y}" x2="${W - padR}" y2="${y}" stroke="#2a2d33" stroke-width="1"/>`;
        s += `<text x="${padL - 8}" y="${y + 4}" fill="#999" font-size="10" text-anchor="end">${pct}%</text>`;
      });
      // fixed x-axis end labels so the 40-110 window is always visible
      s += `<text x="${padL}" y="${H - padB + 16}" fill="#666" font-size="9" text-anchor="start">${T_MIN}&deg;</text>`;
      s += `<text x="${W - padR}" y="${H - padB + 16}" fill="#666" font-size="9" text-anchor="end">${T_MAX}&deg;</text>`;

      // Trigger marker: a plain vertical line spanning the whole plot
      // height, completely separate from the curve below. Its own small
      // draggable handle sits at the TOP of the chart, well clear of the
      // curve points, so the two can never be grabbed by mistake.
      const tx = xFor(triggerC);
      s += `<line x1="${tx}" y1="${padT}" x2="${tx}" y2="${H - padB}" stroke="#ff6b6b" stroke-width="2" stroke-dasharray="5,4"/>`;
      s += `<circle cx="${tx}" cy="${padT - 15}" r="7" fill="#ff6b6b" stroke="#14161a" stroke-width="2" style="cursor:ew-resize" class="triggerhandle"/>`;
      s += `<text x="${tx}" y="${padT - 27}" fill="#ff6b6b" font-size="11" text-anchor="middle" font-weight="600">${triggerC}&deg;C</text>`;

      // Fan curve: 7 independent points, free X+Y drag, nothing to do
      // with the trigger above.
      const pts = thresholds.map(([t, p]) => `${xFor(t).toFixed(1)},${yFor(p).toFixed(1)}`).join(' ');
      s += `<polyline points="${pts}" fill="none" stroke="#3a6df0" stroke-width="2.5"/>`;
      thresholds.forEach(([t, p], i) => {
        const x = xFor(t), y = yFor(p);
        s += `<circle cx="${x}" cy="${y}" r="7" fill="#3a6df0" stroke="#14161a" stroke-width="2" style="cursor:grab" data-idx="${i}" class="dragdot"/>`;
        s += `<text x="${x}" y="${y - 14}" fill="#eee" font-size="10" text-anchor="middle">${Math.round(t)}&deg; ${p}%</text>`;
      });

      svg.setAttribute('viewBox', `0 0 ${W} ${H}`);
      svg.innerHTML = s;
      svg.querySelectorAll('.dragdot').forEach(dot => dot.addEventListener('pointerdown', onCurveDragStart));
      const handle = svg.querySelector('.triggerhandle');
      if (handle) handle.addEventListener('pointerdown', onTriggerDragStart);
    }

    // --- Fan curve point dragging (free X+Y, independent of trigger) ---
    let draggingIdx = null;

    function onCurveDragStart(e) {
      draggingIdx = parseInt(e.target.getAttribute('data-idx'));
      e.target.setPointerCapture(e.pointerId);
      svg.addEventListener('pointermove', onCurveDragMove);
      svg.addEventListener('pointerup', onCurveDragEnd);
    }

    function onCurveDragMove(e) {
      if (draggingIdx === null) return;
      const rect = svg.getBoundingClientRect();
      const svgX = (e.clientX - rect.left) / rect.width * W;
      const svgY = (e.clientY - rect.top) / rect.height * H;

      // Clamp temp so this point can't cross past its left/right neighbor
      // (keeps the curve a valid function -- standard fan-curve editor
      // behavior). This has nothing to do with the trigger marker.
      const leftBound = draggingIdx > 0 ? thresholds[draggingIdx - 1][0] + MIN_GAP_C : T_MIN;
      const rightBound = draggingIdx < thresholds.length - 1 ? thresholds[draggingIdx + 1][0] - MIN_GAP_C : T_MAX;
      const newT = Math.round(clamp(tFromX(svgX), leftBound, rightBound) * 10) / 10;
      const newP = pFromY(svgY);

      thresholds[draggingIdx] = [newT, newP];
      document.getElementById('curveStatus').textContent = 'Editing (not saved).';
      render();
    }

    function onCurveDragEnd(e) {
      if (draggingIdx === null) return;
      draggingIdx = null;
      svg.removeEventListener('pointermove', onCurveDragMove);
      svg.removeEventListener('pointerup', onCurveDragEnd);
      fetch('/api/thresholds', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({thresholds: thresholds})
      }).then(r => r.json()).then(() => {
        document.getElementById('curveStatus').textContent = 'Curve edited.';
      }).catch(() => {
        document.getElementById('curveStatus').textContent = 'Save failed.';
      });
    }

    // --- Trigger marker dragging (horizontal only, independent of curve) ---
    let draggingTrigger = false;

    function onTriggerDragStart(e) {
      draggingTrigger = true;
      e.target.setPointerCapture(e.pointerId);
      svg.addEventListener('pointermove', onTriggerDragMove);
      svg.addEventListener('pointerup', onTriggerDragEnd);
    }

    function onTriggerDragMove(e) {
      if (!draggingTrigger) return;
      const rect = svg.getBoundingClientRect();
      const svgX = (e.clientX - rect.left) / rect.width * W;
      triggerC = Math.round(tFromX(svgX) * 10) / 10;
      render();
    }

    function onTriggerDragEnd(e) {
      if (!draggingTrigger) return;
      draggingTrigger = false;
      svg.removeEventListener('pointermove', onTriggerDragMove);
      svg.removeEventListener('pointerup', onTriggerDragEnd);
      fetch('/api/thresholds', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({trigger_c: triggerC})
      }).catch(() => {});
    }

    document.getElementById('resetCurveBtn').addEventListener('click', function() {
      // Reset only re-seeds the 7 curve points around a fixed default
      // shape -- it does NOT move the trigger marker, since the two are
      // unrelated. The seed uses a fixed 78C center rather than the
      // current trigger, since curve points don't take direction from
      // the trigger at all.
      const seedCenter = 78;
      const offsets = {{ seed_offsets|tojson }};
      const pcts = {{ default_seed_pcts|tojson }};
      thresholds = offsets.map((off, i) => [Math.round(clamp(seedCenter + off, T_MIN, T_MAX) * 10) / 10, pcts[i]]);
      render();
      fetch('/api/thresholds', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({thresholds: thresholds, reset: true})
      }).then(r => r.json()).then(() => {
        document.getElementById('curveStatus').textContent = 'Default curve.';
      }).catch(() => {
        document.getElementById('curveStatus').textContent = 'Reset failed.';
      });
    });

    render();
  })();
  </script>

  <h2>Log</h2>
  <div class="card">
    <div class="log" id="log">{{ state.recent_log|reverse|join('\\n') }}</div>
  </div>

  <p style="color:#666; font-size:0.8rem;">Refreshes every 10s.</p>
</body>
</html>
"""


@app.route("/", methods=["GET"])
def dashboard():
    settings = config.load()
    state = loop.get_state_snapshot()
    temps = state["last_temps"]
    hottest = max((t["temp_c"] for t in temps), default=None)
    flash = request.args.get("flash")
    return render_template_string(
        PAGE, settings=settings, state=state, hottest=hottest, flash=flash,
        seed_offsets=config.SEED_OFFSETS,
        default_seed_pcts=config.DEFAULT_SEED_PCTS,
    )


@app.route("/settings", methods=["POST"])
def update_settings():
    form = request.form
    # Trigger temp and max fan % are set entirely by dragging the curve
    # (see /api/thresholds). This form covers cooldown, release delay,
    # the release floor, and poll rates. target_fan_ids is no longer
    # settable from the UI -- this firmware only supports one global fan
    # floor, not per-fan control, so the setting is inert. (hysteresis_c
    # keeps its existing value; edit /data/settings.json directly or via
    # /api/state if you ever need to change it.)
    new_settings = {
        "cooldown_sec": int(form.get("cooldown_sec", 60)),
        "release_sec": int(form.get("release_sec", 120)),
        "min_fan_pct": int(form.get("min_fan_pct", 0)),
        "idle_poll_sec": float(form.get("idle_poll_sec", 1)),
        "active_poll_sec": float(form.get("active_poll_sec", 1)),
        "load_power_w": float(form.get("load_power_w", 100)),
        "load_busy_pct": float(form.get("load_busy_pct", 50)),
        "load_floor_pct": int(form.get("load_floor_pct", 40)),
        "step_down_sec": float(form.get("step_down_sec", 20)),
        "step_down_pct": int(form.get("step_down_pct", 5)),
        "perf_control": form.get("perf_control") == "1",
        "perf_idle_sec": float(form.get("perf_idle_sec", 600)),
        "perf_busy_pct": float(form.get("perf_busy_pct", 20)),
        "perf_power_w": float(form.get("perf_power_w", 80)),
    }
    config.save(new_settings)
    return dashboard_redirect("Settings saved.")


@app.route("/pause", methods=["POST"])
def toggle_pause():
    action = request.form.get("pause")
    config.save({"paused": action == "pause"})
    return dashboard_redirect("Paused." if action == "pause" else "Resumed.")


def dashboard_redirect(msg: str):
    from flask import redirect, url_for
    return redirect(url_for("dashboard", flash=msg))


# --- Simple JSON API, handy for curl/automation or a future mobile check ---

@app.route("/api/thresholds", methods=["POST"])
def api_save_thresholds():
    """
    Called by the drag-to-edit curve in the Settings card. Two
    completely independent modes -- the trigger and the curve never
    affect each other:
      - Curve drag (any of the 7 fan-curve points): body includes
        "thresholds", a list of 7 [temp, pct] pairs. Persists them
        (clamped/reordered server-side) and marks thresholds_customized.
      - Trigger drag (the standalone vertical marker): body includes
        "trigger_c" only -- updates target_max_c, does not touch
        thresholds at all.
      - reset=true alongside "thresholds" (the "Reset to auto curve"
        button): stores the freshly-seeded curve but leaves
        thresholds_customized cleared.
    """
    body = request.get_json(force=True, silent=True) or {}

    if "trigger_c" in body and "thresholds" not in body:
        try:
            new_trigger = float(body["trigger_c"])
        except (ValueError, TypeError):
            return jsonify({"error": "invalid trigger_c"}), 400
        config.save_trigger_drag(new_trigger)
        return jsonify({"ok": True})

    thresholds = body.get("thresholds")
    expected_len = len(config.SEED_OFFSETS)
    if not isinstance(thresholds, list) or len(thresholds) != expected_len:
        return jsonify({"error": f"thresholds must be a list of {expected_len} [temp, pct] pairs"}), 400
    try:
        cleaned = [[config._clamp_temp(float(t)), max(0, min(100, int(p)))] for t, p in thresholds]
    except (ValueError, TypeError):
        return jsonify({"error": "invalid threshold values"}), 400

    is_reset = bool(body.get("reset"))
    config.save_curve_drag(cleaned)
    if is_reset:
        config.set_customized_flag(False)

    return jsonify({"ok": True})



@app.route("/api/state", methods=["GET"])
def api_state():
    return jsonify({"settings": config.load(), "state": loop.get_state_snapshot()})


@app.route("/api/fans", methods=["GET"])
def api_list_fans():
    """Read-only fan list from iLO -- for diagnostics. On this firmware
    these entries have no writable per-fan fields (see README)."""
    try:
        ilo = ilo_fan.IloFanController()
    except (FileNotFoundError, KeyError) as e:
        return jsonify({"error": str(e)}), 500
    return jsonify({"fans": ilo.list_fans()})


@app.route("/api/set-fan", methods=["POST"])
def api_set_fan():
    """Manual one-off push of the GLOBAL minimum fan speed (applies to
    every chassis fan at once -- this firmware has no per-fan control)."""
    body = request.get_json(force=True, silent=True) or {}
    pct = body.get("pct")
    if pct is None:
        return jsonify({"error": "requires pct"}), 400
    try:
        ilo = ilo_fan.IloFanController()
    except (FileNotFoundError, KeyError) as e:
        return jsonify({"error": str(e)}), 500
    ok = ilo.set_fan_floor_pct(int(pct))
    return jsonify({"ok": ok})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
