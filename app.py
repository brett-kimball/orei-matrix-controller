"""
Matrix Switch Web Interface
Flask application entry point.
"""

import json
import logging
import os
import queue
import threading
from pathlib import Path

from flask import Flask, Response, jsonify, make_response, render_template, request

from matrix_client import MatrixClient

# ── Config ─────────────────────────────────────────────────────────────────
CONFIG_PATH = Path(__file__).parent / "config.json"

def load_config() -> dict:
    if not CONFIG_PATH.exists():
        raise FileNotFoundError(
            f"config.json not found at {CONFIG_PATH}. "
            "Copy config.template.json to config.json and edit it."
        )
    with open(CONFIG_PATH) as f:
        return json.load(f)

config = load_config()

# ── Logging ─────────────────────────────────────────────────────────────────
log_level = config.get("logging", {}).get("level", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, log_level, logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# ── Matrix client ────────────────────────────────────────────────────────────
matrix = MatrixClient(config)

# ── SSE broadcast ────────────────────────────────────────────────────────────
_sse_clients: list[queue.SimpleQueue] = []
_sse_lock = threading.Lock()

def _broadcast_state():
    state = matrix.get_state()
    data = json.dumps(state)
    with _sse_lock:
        dead = []
        for q in _sse_clients:
            try:
                q.put_nowait(data)
            except queue.Full:
                dead.append(q)
        for q in dead:
            _sse_clients.remove(q)

matrix.on_state_change = _broadcast_state

# ── Flask app ────────────────────────────────────────────────────────────────
flask_cfg = config.get("flask", {})
app = Flask(__name__)
app.secret_key = flask_cfg.get("secret_key", os.urandom(24).hex())


@app.route("/")
def index():
    title = config.get("title", "Matrix Switch")
    return render_template("index.html", state=matrix.get_state(), title=title)


@app.route("/manifest.json")
def manifest():
    title = config.get("title", "Matrix Switch")
    data = {
        "name": title,
        "short_name": title,
        "description": "OREI Matrix Switch Controller",
        "start_url": "/",
        "display": "standalone",
        "background_color": "#0f1117",
        "theme_color": "#0f1117",
        "icons": [
            {"src": "/static/icons/icon-192.png", "sizes": "192x192", "type": "image/png"},
            {"src": "/static/icons/icon-512.png", "sizes": "512x512", "type": "image/png"},
        ],
    }
    resp = make_response(jsonify(data))
    resp.headers["Content-Type"] = "application/manifest+json"
    return resp


@app.route("/api/state")
def api_state():
    return jsonify(matrix.get_state())


@app.route("/api/schedule")
def api_schedule():
    """Return the active schedule with next-fire timestamps for each event."""
    from datetime import datetime, timedelta
    now = datetime.now()
    entries = []
    for i, event in enumerate(matrix._schedule):
        e = {k: v for k, v in event.items() if not k.startswith("_comment")}
        # Calculate next fire datetime
        t = event.get("time", "")
        try:
            h, m = map(int, t.split(":"))
            candidate = now.replace(hour=h, minute=m, second=0, microsecond=0)
            days = event.get("days", "all")
            # Walk forward up to 7 days to find the next matching day
            for delta in range(8):
                d = candidate + timedelta(days=delta)
                if days == "all" or d.strftime("%a").lower() in [x.lower() for x in days]:
                    if d > now:
                        e["next_fire"] = d.strftime("%Y-%m-%d %H:%M")
                        break
            else:
                e["next_fire"] = None
        except Exception:
            e["next_fire"] = None
        entries.append(e)
    return jsonify({"schedule": entries, "count": len(entries)})


@app.route("/api/switch", methods=["POST"])
def api_switch():
    data = request.get_json(force=True, silent=True) or {}
    output = data.get("output")
    source = data.get("source")
    if not isinstance(output, int) or not isinstance(source, int):
        return jsonify({"error": "output and source must be integers"}), 400
    try:
        ok = matrix.set_output_source(output, source)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    if ok:
        return jsonify({"ok": True})
    return jsonify({"error": "Switch command failed"}), 502


@app.route("/api/power", methods=["POST"])
def api_power():
    data = request.get_json(force=True, silent=True) or {}
    state = data.get("state")
    if state not in (0, 1, True, False):
        return jsonify({"error": "state must be 0 or 1"}), 400
    ok = matrix.set_power(bool(state))
    if ok:
        return jsonify({"ok": True, "power": bool(state)})
    return jsonify({"error": "Power command failed"}), 502


@app.route("/api/preset", methods=["POST"])
def api_preset():
    """Apply a preset routing configuration.
    Body: {"index": N}  where N is 1-8.
    """
    data = request.get_json(force=True, silent=True) or {}
    index = data.get("index")
    if not isinstance(index, int) or not (1 <= index <= 8):
        return jsonify({"error": "index must be an integer between 1 and 8"}), 400
    try:
        ok = matrix.apply_preset(index)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    if ok:
        return jsonify({"ok": True})
    return jsonify({"error": "Preset command failed"}), 502


@app.route("/api/refresh-config", methods=["POST"])
def api_refresh_config():
    global config
    try:
        fresh_config = load_config()
        config = fresh_config
        matrix.reload_config(fresh_config)
    except Exception:
        logger.exception("Failed to reload config.json")
    matrix.force_config_refresh()
    return jsonify({"ok": True, "message": "Config reloading now"})


@app.route("/api/cec", methods=["POST"])
def api_cec():
    """Send a CEC power/input command to a specific output.
    Body: {"output": N, "connection_type": "hdmi"|"hdbt", "state": 0|1|2}
    state: 1=On, 0=Off, 2=Input/Source
    """
    data = request.get_json(force=True, silent=True) or {}
    output = data.get("output")
    connection_type = data.get("connection_type")
    state = data.get("state")
    if not isinstance(output, int) or not (1 <= output <= matrix.num_outputs):
        return jsonify({"error": "invalid output"}), 400
    if connection_type not in ("hdmi", "hdbt"):
        return jsonify({"error": "connection_type must be 'hdmi' or 'hdbt'"}), 400
    if state not in (0, 1, 2):
        return jsonify({"error": "state must be 0, 1, or 2"}), 400
    ok = matrix.set_cec_power(output, connection_type, state)
    if ok:
        return jsonify({"ok": True})
    return jsonify({"error": "CEC command failed or no response from device"}), 502


@app.route("/api/cec-key", methods=["POST"])
def api_cec_key():
    """Send a CEC keypress to a specific input source device.
    Body: {"input": N, "key": index}  (input 1-based, key 1-19)
    """
    data = request.get_json(force=True, silent=True) or {}
    input_num = data.get("input")
    key = data.get("key")
    if not isinstance(input_num, int) or not (1 <= input_num <= matrix.num_inputs):
        return jsonify({"error": "invalid input"}), 400
    if not isinstance(key, int) or not (1 <= key <= 32):
        return jsonify({"error": "key must be an integer between 1 and 32"}), 400
    try:
        ok = matrix.send_cec_key(input_num, key)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    if ok:
        return jsonify({"ok": True})
    return jsonify({"error": "CEC key command failed or no response from device"}), 502


@app.route("/api/cec-raw", methods=["POST"])
def api_cec_raw():
    """Send an arbitrary CEC payload directly to the matrix switch for diagnostics.
    Body: any dict — forwarded verbatim with comhead='cec command' added if absent.
    Only available in debug/development use; do not expose publicly.
    """
    data = request.get_json(force=True, silent=True) or {}
    if "comhead" not in data:
        data["comhead"] = "cec command"
    logger.info("CEC raw probe: %s", data)
    resp = matrix._http_post(data)
    return jsonify({"sent": data, "response": resp})


@app.route("/api/events")
def api_events():
    """Server-Sent Events stream — pushes JSON state on every device change."""
    q: queue.SimpleQueue = queue.SimpleQueue()
    with _sse_lock:
        _sse_clients.append(q)

    def generate():
        # Send current state immediately on connect
        yield f"data: {json.dumps(matrix.get_state())}\n\n"
        try:
            while True:
                try:
                    data = q.get(timeout=25)
                    yield f"data: {data}\n\n"
                except queue.Empty:
                    # Heartbeat to keep connection alive through proxies
                    yield ": heartbeat\n\n"
        except GeneratorExit:
            pass
        finally:
            with _sse_lock:
                try:
                    _sse_clients.remove(q)
                except ValueError:
                    pass

    return Response(
        generate(),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


# ── Startup ───────────────────────────────────────────────────────────────────
matrix.start()
logger.info(
    "Matrix web interface starting on %s:%s",
    flask_cfg.get("host", "0.0.0.0"),
    flask_cfg.get("port", 5000),
)

if __name__ == "__main__":
    app.run(
        host=flask_cfg.get("host", "0.0.0.0"),
        port=flask_cfg.get("port", 5000),
        debug=flask_cfg.get("debug", False),
        use_reloader=False,  # reloader conflicts with background threads
    )
