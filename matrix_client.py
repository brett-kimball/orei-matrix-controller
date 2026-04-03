"""
OREI Matrix Client
==================
- Uses HTTP API for all commands and periodic status polling.
- Maintains a persistent Telnet connection to receive real-time push
  notifications whenever routing changes (front panel, web UI, etc.).
- Thread-safe: safe to call from multiple Flask worker threads simultaneously.
- Power-aware: when the device is in standby, heavy polling is suppressed
  and only a lightweight reachability check is performed.
"""

import json
import logging
import re
import socket
import threading
import time
import urllib.request
from datetime import datetime
from typing import Callable, Optional

logger = logging.getLogger(__name__)


class MatrixClient:
    def __init__(self, config: dict):
        mc = config.get("matrix", {})
        self.host: str = mc.get("host", "172.16.16.238")
        self.http_port: int = mc.get("http_port", 80)
        self.http_user: str = mc.get("http_user", "Admin")
        self.http_password: str = mc.get("http_password", "admin")
        self.telnet_port: int = mc.get("telnet_port", 23)
        self.num_inputs: int = mc.get("num_inputs", 8)
        self.num_outputs: int = mc.get("num_outputs", 8)

        poll = config.get("polling", {})
        self.status_interval: int = poll.get("status_interval_seconds", 10)
        self.names_interval: int = poll.get("names_interval_seconds", 3600)

        # ── Schedule ─────────────────────────────────────────────────
        # Each entry: {"time": "HH:MM", "days": ["mon",...] or "all", "action": "on"|"off", "outputs": [N,...] or "all"}
        self._schedule: list[dict] = config.get("schedule", [])
        self._schedule_last_fired: dict[str, str] = {}  # key -> last date fired (YYYY-MM-DD)

        # ── State cache ──────────────────────────────────────────────
        self._lock = threading.RLock()
        self.input_names: list[str] = [f"Input {i+1}" for i in range(self.num_inputs)]
        self.output_names: list[str] = [f"Output {i+1}" for i in range(self.num_outputs)]
        self.hdbt_output_names: list[str] = [f"Output {i+1}" for i in range(self.num_outputs)]
        self.preset_names: list[str] = [f"preset{i+1}" for i in range(8)]
        self.routing: list[int] = list(range(1, self.num_outputs + 1))
        self.output_connected: list[bool] = [False] * self.num_outputs
        self.power: bool = False
        self.model: str = "Unknown"
        self._connected: bool = False
        self._last_names_fetch: float = 0.0

        # Optional callback fired after any routing change (for SSE etc.)
        self.on_state_change: Optional[Callable] = None

        # ── Background threads ───────────────────────────────────────
        self._stop = threading.Event()

    # ---------------------------------------------------------------- #
    # Lifecycle
    # ---------------------------------------------------------------- #

    def start(self):
        """Perform initial fetch then launch background threads."""
        self._stop.clear()
        self._fetch_model()
        self._fetch_all()

        threading.Thread(target=self._poll_loop,      name="matrix-poll",     daemon=True).start()
        threading.Thread(target=self._telnet_listener, name="matrix-telnet",   daemon=True).start()
        if self._schedule:
            threading.Thread(target=self._schedule_loop, name="matrix-schedule", daemon=True).start()
            logger.info("Schedule loaded: %d event(s)", len(self._schedule))

        logger.info("MatrixClient started (host=%s)", self.host)

    def stop(self):
        self._stop.set()

    # ---------------------------------------------------------------- #
    # HTTP helpers
    # ---------------------------------------------------------------- #

    def _http_post(self, payload: dict) -> Optional[dict]:
        url = f"http://{self.host}:{self.http_port}/cgi-bin/instr"
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url, data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=8) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception:
            logger.warning("HTTP request failed: comhead=%s", payload.get("comhead"))
            return None

    # ---------------------------------------------------------------- #
    # Fetch methods
    # ---------------------------------------------------------------- #

    def _fetch_model(self):
        resp = self._http_post({"comhead": "get status"})
        notify = False
        with self._lock:
            new_connected = resp is not None
            if new_connected != self._connected:
                self._connected = new_connected
                notify = True
            if resp:
                self.model = resp.get("model", self.model)
                new_power = bool(resp.get("power", self.power))
                if new_power != self.power:
                    self.power = new_power
                    logger.info("Device power state changed to: %s", "ON" if new_power else "STANDBY")
                    notify = True
        if notify and self.on_state_change:
            try:
                self.on_state_change()
            except Exception:
                logger.exception("on_state_change callback error")

    def _fetch_video_status(self):
        """Fetch routing + names from HTTP. Names only refreshed per interval."""
        resp = self._http_post({"comhead": "get video status"})
        if not resp:
            with self._lock:
                self._connected = False
            return
        with self._lock:
            self._connected = True
            new_power = bool(resp.get("power", self.power))
            if new_power != self.power:
                self.power = new_power
                logger.info("Device power state changed to: %s", "ON" if new_power else "STANDBY")
                if self.on_state_change:
                    try:
                        self.on_state_change()
                    except Exception:
                        logger.exception("on_state_change callback error")

            if "allsource" in resp:
                sources = resp["allsource"]
                self.routing = [int(sources[i]) for i in range(self.num_outputs)]

            now = time.time()
            if now - self._last_names_fetch >= self.names_interval:
                if "allinputname" in resp:
                    self.input_names = [n.strip() for n in list(resp["allinputname"])[: self.num_inputs]]
                if "alloutputname" in resp:
                    self.output_names = [n.strip() for n in list(resp["alloutputname"])[: self.num_outputs]]
                if "allhdbtoutputname" in resp:
                    self.hdbt_output_names = [n.strip() for n in list(resp["allhdbtoutputname"])[: self.num_outputs]]
                if "allname" in resp:
                    self.preset_names = [n.strip() for n in list(resp["allname"])[:8]]
                self._last_names_fetch = now
                logger.debug("Names refreshed from device")

    def _fetch_output_status(self):
        """Fetch HDMI output link/connect status."""
        resp = self._http_post({"comhead": "get output status"})
        if not resp:
            return
        with self._lock:
            if "allconnect" in resp:
                hdmi_connected = [bool(v) for v in resp["allconnect"][: self.num_outputs]]
            else:
                hdmi_connected = [False] * self.num_outputs
            if "allhdbtconnect" in resp:
                hdbt_connected = [bool(v) for v in resp["allhdbtconnect"][: self.num_outputs]]
            else:
                hdbt_connected = [False] * self.num_outputs
            self.output_connected = [
                h or b for h, b in zip(hdmi_connected, hdbt_connected)
            ]
            # Also update routing from this response as a redundancy check
            if "allsource" in resp:
                sources = resp["allsource"]
                self.routing = [int(sources[i]) for i in range(self.num_outputs)]

    def _fetch_all(self):
        self._fetch_video_status()
        self._fetch_output_status()
        if self.on_state_change:
            try:
                self.on_state_change()
            except Exception:
                logger.exception("on_state_change callback error")

    def force_config_refresh(self):
        """Immediately re-fetch names from the device in a background thread."""
        def _do():
            with self._lock:
                self._last_names_fetch = 0.0
            self._fetch_video_status()
            if self.on_state_change:
                try:
                    self.on_state_change()
                except Exception:
                    logger.exception("on_state_change callback error")
        threading.Thread(target=_do, name="matrix-names-refresh", daemon=True).start()

    def reload_config(self, config: dict) -> None:
        """Hot-reload schedule and polling intervals from a freshly parsed config dict.

        Network/flask settings are intentionally excluded — those require a restart.
        Title and other app-level settings are handled by app.py updating its own
        module-level config reference before calling this method.
        """
        poll = config.get("polling", {})
        new_status_interval = poll.get("status_interval_seconds", self.status_interval)
        new_names_interval  = poll.get("names_interval_seconds",  self.names_interval)
        new_schedule        = config.get("schedule", [])

        with self._lock:
            self.status_interval = new_status_interval
            self.names_interval  = new_names_interval
            self._schedule       = new_schedule
            # Clear last-fired cache so any matching events in the new schedule
            # aren't suppressed by stale keys from the old schedule.
            self._schedule_last_fired.clear()

        logger.info(
            "Config reloaded: %d schedule event(s), poll=%ds, names=%ds",
            len(new_schedule), new_status_interval, new_names_interval,
        )

    # ---------------------------------------------------------------- #
    # Power control
    # ---------------------------------------------------------------- #

    def apply_preset(self, index: int) -> bool:
        """Apply a preset routing configuration by 1-based index (1–8)."""
        if not (1 <= index <= 8):
            raise ValueError(f"Preset index {index} out of range (1-8)")
        resp = self._http_post({"comhead": "preset set", "language": 0, "index": index})
        if resp and resp.get("result") == 1:
            logger.info("Preset %d applied", index)
            # The device will push a routing update via telnet; also poll immediately.
            threading.Thread(target=self._fetch_all, name="matrix-preset-poll", daemon=True).start()
            return True
        logger.warning("Preset %d failed: resp=%s", index, resp)
        return False

    def set_power(self, on: bool) -> bool:
        """Turn device on (True) or standby (False) via HTTP."""
        resp = self._http_post({"comhead": "set poweronoff", "power": 1 if on else 0})
        if resp and resp.get("result") == 1:
            with self._lock:
                self.power = on
            logger.info("Power set to %s", "ON" if on else "STANDBY")
            if self.on_state_change:
                try:
                    self.on_state_change()
                except Exception:
                    logger.exception("on_state_change callback error")
            return True

    # Maps API state values to CEC index: 1=On→0, 0=Off→1, 2=Input→5
    _CEC_STATE_INDEX = {1: 0, 0: 1, 2: 5}
    _CEC_STATE_LABEL = {1: "ON", 0: "STANDBY", 2: "INPUT"}

    def set_cec_power(self, output: int, connection_type: str, state: int) -> bool:
        """Send CEC power on/off/input to a specific output via the matrix switch.

        output:          1-based output port number
        connection_type: 'hdmi' or 'hdbt'
        state:           1=On, 0=Off, 2=Input/Source

        Port array layout (16 elements):
          indices 0-7  = HDMI outputs 1-8
          indices 8-15 = HDBaseT outputs 1-8
        CEC output indices (object=1, language=0): 0=On, 1=Off, 5=Input
        """
        if not (1 <= output <= self.num_outputs):
            raise ValueError(f"Output {output} out of range")
        if connection_type not in ("hdmi", "hdbt"):
            raise ValueError(f"connection_type must be 'hdmi' or 'hdbt'")
        if state not in self._CEC_STATE_INDEX:
            raise ValueError(f"state must be 0, 1, or 2")

        port = [0] * 16
        if connection_type == "hdmi":
            port[output - 1] = 1
        else:  # hdbt
            port[output - 1 + 8] = 1

        resp = self._http_post({
            "comhead": "cec command",
            "language": 0,
            "object": 1,       # 1 = output
            "port": port,
            "index": self._CEC_STATE_INDEX[state],  # 0=On, 1=Off, 5=Input
        })
        action = self._CEC_STATE_LABEL[state]
        if resp and resp.get("result") == 1:
            logger.info("CEC %s sent to output %d (%s)", action, output, connection_type)
            return True
        logger.warning("CEC %s failed for output %d (%s): resp=%s", action, output, connection_type, resp)
        return False

    def send_cec_key(self, input_num: int, key_index: int) -> bool:
        """Send a CEC User Control key to a source device via a specific input port.

        input_num:  1-based input port number (1–8)
        key_index:  keycode index as defined by the device (1–32)

        Port array layout (16 elements, object=0 for input side):
          indices 0-7 = HDMI inputs 1-8
        Keycodes: 1=Power On, 2=Standby, 3=Up, 4=Left, 5=OK, 6=Right,
                  7=Menu, 8=Down, 9=Back, 10=Prev, 11=Play, 12=Next,
                  13=Rewind, 14=Pause, 15=FF, 16=Stop,
                  17=Mute, 18=Vol Down, 19=Vol Up,
                  20=Digit 0, 21-29=Digits 1-9,
                  30=Channel Up, 31=Channel Down, 32=Previous Channel
        """
        if not (1 <= input_num <= self.num_inputs):
            raise ValueError(f"Input {input_num} out of range")
        if not (1 <= key_index <= 32):
            raise ValueError(f"key_index {key_index} out of range (1-32)")

        port = [0] * 16
        port[input_num - 1] = 1

        resp = self._http_post({
            "comhead": "cec command",
            "language": 0,
            "object": 0,       # 0 = input
            "port": port,
            "index": key_index,
        })
        if resp and resp.get("result") == 1:
            logger.info("CEC key %d sent to input %d", key_index, input_num)
            return True
        logger.warning("CEC key %d failed for input %d: resp=%s", key_index, input_num, resp)
        return False

    # ---------------------------------------------------------------- #
    # Schedule loop
    # ---------------------------------------------------------------- #

    def _schedule_loop(self):
        """Fire scheduled actions according to the configured schedule.

        Supported actions:
          "on" / "off"              — CEC power to matching outputs
          "matrix_on"               — power on the matrix switch itself
          "matrix_standby"          — put the matrix switch into standby
          "switch"                  — route all/listed outputs to a source input
          "source_on" / "source_off" — CEC power on/standby to source devices on inputs

        Checks every 30 seconds whether any schedule event matches the current
        time.  Each event is fired at most once per minute (tracked by index +
        date) so the 30-second polling window never double-fires.
        """
        while not self._stop.is_set():
            try:
                now = datetime.now()
                current_time = now.strftime("%H:%M")
                current_day  = now.strftime("%a").lower()   # mon, tue, …
                today        = str(now.date())               # YYYY-MM-DD
                for i, event in enumerate(self._schedule):
                    if event.get("time", "") != current_time:
                        continue
                    days = event.get("days", "all")
                    if days != "all" and current_day not in [d.lower() for d in days]:
                        continue
                    # Guard: fire each event at most once per minute per day
                    key = f"{i}:{today}"
                    if self._schedule_last_fired.get(key) == current_time:
                        continue
                    self._schedule_last_fired[key] = current_time
                    action = event.get("action", "off")
                    if action == "matrix_on":
                        logger.info("Schedule event %d fired: matrix power ON", i)
                        self.set_power(True)
                    elif action == "matrix_standby":
                        logger.info("Schedule event %d fired: matrix power STANDBY", i)
                        self.set_power(False)
                    elif action == "cec_input":
                        outputs_cfg = event.get("outputs", "all")
                        source_is   = event.get("source_is", "any")
                        logger.info(
                            "Schedule event %d fired: CEC input (active source), outputs=%s, source_is=%s",
                            i, outputs_cfg, source_is,
                        )
                        self._fire_schedule_cec_input(outputs_cfg, source_is)
                    elif action == "preset":
                        preset_index = event.get("index")
                        if preset_index is None:
                            logger.warning("Schedule event %d: 'preset' action missing 'index'", i)
                        else:
                            logger.info("Schedule event %d fired: preset %s", i, preset_index)
                            self.apply_preset(int(preset_index))
                    elif action == "switch":
                        outputs_cfg = event.get("outputs", "all")
                        source      = event.get("source")
                        if source is None:
                            logger.warning("Schedule event %d: 'switch' action missing 'source'", i)
                        else:
                            logger.info(
                                "Schedule event %d fired: switch outputs=%s to source %s",
                                i, outputs_cfg, source,
                            )
                            self._fire_schedule_switch(outputs_cfg, source)
                    elif action in ("source_on", "source_off"):
                        inputs_cfg = event.get("inputs", "all")
                        on         = action == "source_on"
                        logger.info(
                            "Schedule event %d fired: source CEC %s, inputs=%s",
                            i, "ON" if on else "STANDBY", inputs_cfg,
                        )
                        self._fire_schedule_source_cec(on, inputs_cfg)
                    else:
                        on           = action == "on"
                        outputs_cfg  = event.get("outputs",   "all")
                        source_is    = event.get("source_is", "any")
                        logger.info(
                            "Schedule event %d fired: CEC %s, outputs=%s, source_is=%s",
                            i, "ON" if on else "STANDBY", outputs_cfg, source_is,
                        )
                        self._fire_schedule_cec(on, outputs_cfg, source_is)
            except Exception:
                logger.exception("Schedule loop error")
            self._stop.wait(30)
        logger.info("Schedule loop stopped")

    def _fire_schedule_source_cec(self, on: bool, inputs_config) -> None:
        """Send a CEC power on/standby key to source devices on matching inputs.

        inputs_config: ``"all"`` or a list of 1-based input numbers.
        Key index 1 = Power On, 2 = Standby.
        """
        key_index = 1 if on else 2
        for inp in self.get_state()["inputs"]:
            if inp.get("is_default"):
                continue   # unnamed/default input — no source device known
            if inputs_config != "all" and inp["number"] not in inputs_config:
                continue
            try:
                self.send_cec_key(inp["number"], key_index)
            except Exception:
                logger.exception("Schedule source CEC error for input %d", inp["number"])

    def _fire_schedule_switch(self, outputs_config, source: int) -> None:
        """Route outputs to a given source input.

        outputs_config: ``"all"`` or a list of 1-based output numbers.
        source:         1-based input number to route to.
        """
        for output in self.get_state()["outputs"]:
            if outputs_config != "all" and output["number"] not in outputs_config:
                continue
            try:
                self.set_output_source(output["number"], source)
            except Exception:
                logger.exception("Schedule switch error for output %d", output["number"])

    def _fire_schedule_cec(self, on: bool, outputs_config, source_is) -> None:
        """Send a CEC power command to each applicable output.

        outputs_config: ``"all"`` or a list of 1-based output numbers.
        source_is:      ``"any"`` or a list of 1-based input numbers; only
                        outputs currently routed to one of those inputs are sent
                        the command.
        Outputs whose connection_type is ``None`` (default-named, no TV) are
        silently skipped.
        """
        for output in self.get_state()["outputs"]:
            if output["connection_type"] is None:
                continue   # no CEC target known for this output
            if outputs_config != "all" and output["number"] not in outputs_config:
                continue
            if source_is != "any" and output["source"] not in source_is:
                continue
            try:
                self.set_cec_power(output["number"], output["connection_type"], 1 if on else 0)
            except Exception:
                logger.exception("Schedule CEC error for output %d", output["number"])

    def _fire_schedule_cec_input(self, outputs_config, source_is) -> None:
        """Send a CEC Active Source (switch to input) command to each applicable output."""
        for output in self.get_state()["outputs"]:
            if output["connection_type"] is None:
                continue
            if outputs_config != "all" and output["number"] not in outputs_config:
                continue
            if source_is != "any" and output["source"] not in source_is:
                continue
            try:
                self.set_cec_power(output["number"], output["connection_type"], 2)
            except Exception:
                logger.exception("Schedule CEC input error for output %d", output["number"])

    # ---------------------------------------------------------------- #
    # Background poll loop (HTTP safety-net)
    # ---------------------------------------------------------------- #

    def _poll_loop(self):
        """Poll device status. When in standby, only polls power state lightly."""
        while not self._stop.is_set():
            try:
                with self._lock:
                    is_on = self.power
                if is_on:
                    self._fetch_all()
                else:
                    # Device is in standby — just check if it has come back on
                    self._fetch_model()
            except Exception:
                logger.exception("Poll loop error")
            self._stop.wait(self.status_interval)
        logger.info("Poll loop stopped")

    # ---------------------------------------------------------------- #
    # Telnet push listener
    # ---------------------------------------------------------------- #

    def _telnet_listener(self):
        """Maintain a persistent Telnet connection and parse push messages."""
        reconnect_delay = 5
        while not self._stop.is_set():
            sock = None
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.settimeout(10)
                sock.connect((self.host, self.telnet_port))
                # Drain the welcome banner / IAC negotiation
                time.sleep(0.5)
                sock.settimeout(2)
                try:
                    sock.recv(4096)
                except socket.timeout:
                    pass

                logger.info(
                    "Telnet listener connected to %s:%s", self.host, self.telnet_port
                )
                reconnect_delay = 5  # reset on successful connect

                sock.settimeout(5)
                buf = b""
                while not self._stop.is_set():
                    try:
                        chunk = sock.recv(1024)
                        if not chunk:
                            break
                        # Strip Telnet IAC bytes and non-ASCII
                        chunk = bytes(b for b in chunk if b < 0x80)
                        buf += chunk
                        while b"\n" in buf:
                            line, buf = buf.split(b"\n", 1)
                            text = line.decode("ascii", errors="ignore").strip().strip(">").strip()
                            if text:
                                self._handle_push(text)
                    except socket.timeout:
                        continue
                    except OSError:
                        break
            except Exception:
                logger.warning(
                    "Telnet listener disconnected, retrying in %ds", reconnect_delay
                )
            finally:
                if sock:
                    try:
                        sock.close()
                    except Exception:
                        pass

            if not self._stop.is_set():
                self._stop.wait(reconnect_delay)
                reconnect_delay = min(reconnect_delay * 2, 60)

        logger.info("Telnet listener stopped")

    def _handle_push(self, line: str):
        """Parse and apply a push notification from the device."""
        # Format: "input N -> output M"
        m = re.match(r"input\s+(\d+)\s*->\s*output\s+(\d+)", line, re.IGNORECASE)
        if m:
            inp = int(m.group(1))
            out = int(m.group(2))
            if 1 <= out <= self.num_outputs and 1 <= inp <= self.num_inputs:
                with self._lock:
                    self.routing[out - 1] = inp
                logger.info("Push: output %d -> input %d", out, inp)
                if self.on_state_change:
                    try:
                        self.on_state_change()
                    except Exception:
                        logger.exception("on_state_change callback error")

    # ---------------------------------------------------------------- #
    # Control
    # ---------------------------------------------------------------- #

    def set_output_source(self, output: int, input_num: int) -> bool:
        """Switch output (1-based) to input (1-based) via HTTP."""
        if not (1 <= output <= self.num_outputs):
            raise ValueError(f"Output {output} out of range (1–{self.num_outputs})")
        if not (1 <= input_num <= self.num_inputs):
            raise ValueError(f"Input {input_num} out of range (1–{self.num_inputs})")

        resp = self._http_post(
            {"comhead": "video switch", "source": [input_num, output]}
        )
        if resp and resp.get("result") == 1:
            with self._lock:
                self.routing[output - 1] = input_num
            logger.info("Switched output %d to input %d", output, input_num)
            if self.on_state_change:
                try:
                    self.on_state_change()
                except Exception:
                    logger.exception("on_state_change callback error")
            return True

        logger.warning(
            "Switch failed: output=%d input=%d resp=%s", output, input_num, resp
        )
        return False

    # ---------------------------------------------------------------- #
    # State snapshot
    # ---------------------------------------------------------------- #

    # Patterns that identify factory-default names (case-insensitive).
    # Input defaults:    "input1", "Input 2", etc.
    # HDMI out defaults: "output1", "hdmi output2", etc.
    # HDBaseT defaults:  "output1", "hdbt output2", etc.
    _DEFAULT_INPUT_RE  = re.compile(r'^input\s*\d+$',               re.IGNORECASE)
    _DEFAULT_OUTPUT_RE = re.compile(r'^((hdmi|hdbt)\s+)?output\s*\d+$', re.IGNORECASE)

    # ── Device-type keyword table ───────────────────────────────────────────
    # Maps substrings (lower-case) found in an input name to a device type.
    # First match wins.  Types: "cable", "streaming", "dvd", "signage"
    # Any unmatched input defaults to type "generic".
    _DEVICE_TYPE_KEYWORDS: list[tuple[list[str], str]] = [
        (["xfinity", "comcast", "cable", "cox", "spectrum", "directv", "dish"], "cable"),
        (["appletv", "apple tv", "firetv", "fire tv", "fire stick",
          "roku", "chromecast", "shield", "streaming", "stream"], "streaming"),
        (["blu", "bluray", "blu-ray", "dvd", "oppo", "disc", "player",
          "tivo", "dvr", "pvr", "recorder", "hdhomerun"], "dvd"),
        (["brightsign", "scala", "xibo", "signage", "display", "player"], "signage"),
        (["music", "audio", "stereo", "receiver", "amp", "speaker"], "signage"),
    ]

    @classmethod
    def _infer_device_type(cls, name: str) -> str:
        """Return a device type string inferred from an input name."""
        lower = name.lower()
        for keywords, dtype in cls._DEVICE_TYPE_KEYWORDS:
            if any(kw in lower for kw in keywords):
                return dtype
        return "generic"

    def get_state(self) -> dict:
        """Return a thread-safe, JSON-serialisable snapshot of current state."""
        with self._lock:
            outputs = []
            for i in range(self.num_outputs):
                hdmi = self.output_names[i] if i < len(self.output_names) else f"Output {i+1}"
                hdbt = self.hdbt_output_names[i] if i < len(self.hdbt_output_names) else f"Output {i+1}"
                hdmi_default = bool(self._DEFAULT_OUTPUT_RE.match(hdmi))
                hdbt_default = bool(self._DEFAULT_OUTPUT_RE.match(hdbt))
                # Infer connection type from whichever side has been given a custom name.
                # If HDMI is named → HDMI connection; if HDBaseT is named → HDBaseT connection.
                # If both are custom (misconfiguration), HDMI takes precedence.
                if not hdmi_default:
                    display_name = hdmi
                    connection_type = "hdmi"
                elif not hdbt_default:
                    display_name = hdbt
                    connection_type = "hdbt"
                else:
                    display_name = hdmi  # both default — card will be hidden
                    connection_type = None
                outputs.append({
                    "number": i + 1,
                    "name": display_name,
                    "is_default": hdmi_default and hdbt_default,
                    "connection_type": connection_type,
                    "source": self.routing[i] if i < len(self.routing) else 0,
                    "connected": (
                        self.output_connected[i] if i < len(self.output_connected) else False
                    ),
                })
            return {
                "model": self.model,
                "power": self.power,
                "connected": self._connected,
                "inputs": [
                    {
                        "number": i + 1,
                        "name": (iname := self.input_names[i] if i < len(self.input_names) else f"Input {i+1}"),
                        "is_default": bool(self._DEFAULT_INPUT_RE.match(iname)),
                        "device_type": self._infer_device_type(iname),
                    }
                    for i in range(self.num_inputs)
                ],
                "outputs": outputs,
                "preset_names": list(self.preset_names),
            }
