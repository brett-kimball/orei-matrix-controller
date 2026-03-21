# OREI Matrix Switch Web Interface

A real-time web interface for the **OREI UHD88-EXB400R-K** 8×8 HDMI/HDBaseT matrix switch (and similar OREI matrix switches with the same HTTP API).

---

## Features

- Live 8-output routing grid — change any output's source with a single dropdown
- Real-time updates via Server-Sent Events (SSE) — all connected browser tabs update instantly when routing changes, including changes made from other sources (the device's own web UI, RS232, etc.)
- Signal presence indicator per output
- Power on / standby control
- Custom input and output names pulled directly from the device
- **Outputs and inputs that still have their factory-default names are automatically hidden** from the UI — only named ports are shown (see [Default Names](#default-names) below)
- Configurable via a single `config.json` file
- Suitable for running as a persistent systemd service

---

## Requirements

| Requirement | Version |
|---|---|
| Python | 3.10 or later (tested on 3.13) |
| Flask | 3.0+ |
| Gunicorn | 23.0+ (for production/systemd deployment) |
| OREI matrix switch | HTTP API enabled (factory default) |

> **Note:** Python's `telnetlib` was removed in Python 3.13. This application uses raw sockets for the telnet push-notification listener — no third-party telnet library is needed.

---

## How It Works

The application communicates with the matrix switch over two channels simultaneously:

### HTTP API (`POST /cgi-bin/instr`)
All commands are sent as JSON to the switch's built-in HTTP API. This is stateless and concurrent-safe, so multiple browser clients are supported without contention. Commands used:

| Operation | `comhead` value |
|---|---|
| Get routing + all names | `get video status` |
| Get output signal status | `get output status` |
| Get device info / power state | `get status` |
| Switch an output to an input | `video switch` |
| Power on | `set poweronoff` with `"power": 1` |
| Enter standby | `set poweronoff` with `"power": 0` |

### Telnet push listener (port 23)
The device sends unsolicited routing-change notifications to any connected telnet session in the form `input N -> output M`. The application maintains a persistent TCP connection on port 23 and uses these push events to update the UI in real time without waiting for the next poll cycle.

### Polling (safety net)
A background thread polls `get video status` every `status_interval_seconds` (default 10 s) as a fallback. When the device is in standby, heavy polling is suppressed and only a lightweight status check is performed.

### Server-Sent Events (SSE)
The Flask server streams state updates to all connected browsers over `/api/events`. No WebSocket library is required.

---

## Default Names

The OREI switch ships with factory-default names for all inputs and outputs. These defaults follow predictable patterns:

| Port | Factory default pattern | Examples |
|---|---|---|
| Inputs | `inputN` | `input1`, `input2` … `input8` |
| HDMI outputs | `hdmi outputN` | `hdmi output1`, `hdmi output2` … |
| HDBaseT outputs | `hdbt outputN` | `hdbt output1`, `hdbt output2` … |

Each physical output port number has two connections: a direct HDMI output and an HDBaseT (twisted-pair extender) output. Both share the same routing slot, so a single dropdown controls both simultaneously.

**Only rename outputs on the side that matches their physical connection — rename the HDMI output entry if the TV/display is connected by HDMI direct, or rename the HDBaseT output entry if it is connected via an HDBaseT extender.** Leave the other entry at its factory default. The app uses this to:

1. Determine the display name for the output card
2. Infer the connection type (`hdmi` or `hdbt`) for future CEC commands — no extra configuration needed

If both entries for a slot have non-default names (misconfiguration), the HDMI name takes precedence. A card is hidden only when **both** names remain at factory defaults.

**Any input or output whose name matches the default patterns is hidden in the web UI.** This keeps the display clean when only a subset of ports are in use.

To make a port appear in the UI, give its correct-side output entry a custom name using the matrix switch's own web interface (navigate to the device's IP address in a browser, go to the output settings, and rename either the HDMI or HDBaseT entry — not both). The custom names are fetched from the device and refreshed every hour (configurable via `names_interval_seconds`). Use the **↺ Refresh Names** button in the UI to force an immediate re-fetch.

The patterns detected as defaults are (case-insensitive):
- Input: matches `input` followed by optional whitespace and a number
- HDMI output: matches an optional `hdmi ` prefix followed by `output` and a number
- HDBaseT output: matches an optional `hdbt ` prefix followed by `output` and a number

---

## Quick Start (development)

```bash
# Clone / copy the project
cd /home/brett/Projects/matrix

# Create and activate a virtual environment
python3 -m venv .venv
source .venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Create your config
cp config.template.json config.json
# Edit config.json with your switch's IP and credentials

# Run
python app.py
```

Then open `http://localhost:5000` in a browser.

---

## Configuration

Copy `config.template.json` to `config.json` and edit it. The file is gitignored so credentials are never committed.

```jsonc
{
  "matrix": {
    "host": "192.168.1.100",       // IP address of the matrix switch
    "http_port": 80,               // HTTP port (almost always 80)
    "http_user": "Admin",          // HTTP login username
    "http_password": "admin",      // HTTP login password
    "telnet_port": 23,             // Telnet push-notification port
    "num_inputs": 8,               // Total number of inputs
    "num_outputs": 8               // Total number of outputs
  },
  "polling": {
    "status_interval_seconds": 10,    // Routing poll interval (seconds)
    "names_interval_seconds": 3600    // Name re-fetch interval (seconds)
  },
  "flask": {
    "host": "0.0.0.0",            // Bind address
    "port": 5000,                  // TCP port for the web UI
    "debug": false,                // Never true in production
    "secret_key": "change-me"      // Flask session secret
  },
  "logging": {
    "level": "INFO"
  },
  "schedule": [
    {
      "time": "02:30",             // HH:MM (24-hour, local time)
      "days": "all",               // "all" or an array like ["mon","tue","wed","thu","fri"]
      "action": "off",             // "on", "off", "matrix_on", "matrix_standby", "switch", "source_on", or "source_off"
      "outputs": "all",            // "all" or an array of output numbers, e.g. [1,3,5]
      "source_is": "any"           // "any" or an array of input numbers, e.g. [2,3]
    },
    {
      "time": "07:00",
      "days": "all",
      "action": "on",
      "outputs": "all",
      "source_is": [2, 3]          // only outputs routed to input 2 or 3 will power on
    },
    {
      "time": "08:00",
      "days": ["mon","tue","wed","thu","fri"],
      "action": "switch",         // route outputs to a different input
      "outputs": [1, 2],           // "all" or a list of output numbers
      "source": 3                  // input number to route to
    },
    {
      "time": "02:30",
      "days": "all",
      "action": "source_off",     // CEC standby to source devices on inputs
      "inputs": "all"              // "all" or a list of input numbers
    },
    {
      "time": "07:00",
      "days": "all",
      "action": "source_on",      // CEC power-on to source devices on inputs
      "inputs": [1, 3]             // only inputs 1 and 3
    }
  ]
}
```

### Schedule notes

* The `schedule` key is optional — omit it (or set it to `[]`) to disable scheduling entirely.
* **Actions:** `"on"` / `"off"` send CEC commands to outputs (displays); `"source_on"` / `"source_off"` send CEC power commands to source devices on inputs; `"matrix_on"` and `"matrix_standby"` control power on the matrix switch itself; `"switch"` routes outputs to a different source input. The `outputs`, `source_is` fields are ignored for matrix power actions.
* **Ordering matters:** the matrix switch must be powered on before CEC commands can be relayed to displays. Schedule `"matrix_on"` a few minutes before any `"on"` CEC event to give the switch time to fully boot (e.g. `matrix_on` at 06:55, CEC `on` at 07:00).
* **Multiple events at the same time** are supported. All matching events fire in the order they appear in the `schedule` array. If ordering between same-time events matters (e.g. `switch` before `on`), place them in that order in the list. If a guaranteed delay is needed between actions, use times one minute apart.
* Only outputs that have a **custom name** (i.e. not a default name like `"output3"`) receive CEC commands, because the connection type (`hdmi` or `hdbt`) is inferred from the naming convention.
* Each event fires **at most once per minute**: the 30-second check interval will never double-fire the same event.
* Day abbreviations match Python's `datetime.strftime("%a").lower()`: `mon`, `tue`, `wed`, `thu`, `fri`, `sat`, `sun`.
* **`source_is`** is optional (defaults to `"any"`). When set to a list of input numbers, the CEC command is only sent to outputs currently routed to one of those inputs — useful for selectively powering on signage players while leaving cable box displays alone.
* **`source`** (for `"switch"` action only) is the input number to route to. `"outputs"` follows the same `"all"`-or-list convention as CEC actions.
* **`inputs`** (for `"source_on"` / `"source_off"` only) is `"all"` or a list of input numbers. Only inputs with a custom name (i.e. a known source device) receive the command.

---

## Production Deployment (Raspberry Pi / systemd)

See **[INSTALL.md](INSTALL.md)** for full step-by-step instructions covering:

1. Creating a dedicated `matrix` system user
2. Copying files to `/opt/matrix-switch`
3. Creating the virtual environment and installing dependencies
4. Configuring `config.json`
5. Creating the log directory
6. Installing and enabling the systemd service (`matrix-switch.service`)

### Quick reference

```bash
# Install and start
sudo cp matrix-switch.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now matrix-switch

# Status / logs
sudo systemctl status matrix-switch
sudo journalctl -fu matrix-switch
```

---

## Project Structure

```
matrix/
├── app.py                  # Flask application — routes, SSE, API endpoints
├── matrix_client.py        # Matrix communication layer (HTTP + telnet)
├── templates/
│   └── index.html          # Web UI (single-page, no JS framework)
├── static/
│   └── logo.png            # Logo displayed in the header
├── config.json             # Live config (gitignored)
├── config.template.json    # Safe-to-commit config template
├── requirements.txt        # Python dependencies
├── matrix-switch.service   # systemd unit file
├── INSTALL.md              # Full deployment guide
└── .gitignore
```

---

## API Endpoints

| Method | Path | Description |
|---|---|---|
| `GET` | `/` | Web UI |
| `GET` | `/api/state` | Full JSON state snapshot |
| `GET` | `/api/events` | SSE stream of state updates |
| `POST` | `/api/switch` | Route an output: `{"output": N, "source": M}` |
| `POST` | `/api/power` | Power control: `{"state": 1}` = on, `{"state": 0}` = standby |
| `POST` | `/api/cec` | CEC power to one output: `{"output": N, "connection_type": "hdmi"\|"hdbt", "state": 0\|1}` |
| `POST` | `/api/cec-key` | CEC keypress to an input device: `{"input": N, "key": index}` (key 1–32, see below) |
| `POST` | `/api/refresh-names` | Immediately re-fetch input/output names from the device |

---

## CEC Input Remote Control

Each output card shows a remote control button when its current source is a CEC-capable device. Tapping it opens a modal popup with a button set tailored to the device type.

### Device type detection

The remote button set is selected automatically by matching the **input name** (as set on the matrix switch) against a keyword table:

| Device type | Matched keywords (case-insensitive) |
|---|---|
| `cable` | xfinity, comcast, cable, cox, spectrum, directv, dish |
| `streaming` | appletv, apple tv, firetv, fire tv, fire stick, roku, chromecast, shield, streaming, stream |
| `dvd` | blu, bluray, blu-ray, dvd, oppo, disc, player |
| `signage` | brightsign, scala, xibo, signage, display, player |
| `generic` | *(anything else)* |

The first matching keyword wins. Inputs named with the factory default pattern (e.g. `input1`) are `generic`.

**Signage** inputs have the remote button hidden entirely — no interaction is expected.

### Button profiles per device type

| Group | Buttons | cable | dvd | streaming | generic |
|---|---|:---:|:---:|:---:|:---:|
| Power | Power On / Standby | ✅ | ✅ | ✅ | ✅ |
| Navigation | ↑↓←→ OK Menu Back | ✅ | ✅ | ✅ | ✅ |
| Playback | ⏮ ▶ ⏭ ⏪ ⏸ ⏩ ⏹ | — | ✅ | — | — |
| Transport | ▶ ⏸ only | — | — | ✅ | — |
| Volume | 🔉 🔇 🔊 | ✅ | ✅ | ✅ | ✅ |
| Channel | numpad 0–9 + CH▲▼ | ✅ | — | — | — |

### CEC key index table

The matrix switch accepts indices 1–32 for the `cec-key` API (indices 20–32 were discovered by probing the device; indices 1–19 are documented in the device firmware):

| Index | Function | Index | Function |
|---|---|---|---|
| 1 | Power On | 17 | Mute |
| 2 | Power Standby | 18 | Volume Down |
| 3 | Up | 19 | Volume Up |
| 4 | Left | 20 | Digit 0 |
| 5 | OK / Select | 21–29 | Digits 1–9 |
| 6 | Right | 30 | Channel Up |
| 7 | Menu | 31 | Channel Down |
| 8 | Down | 32 | Previous Channel |
| 9 | Back | | |
| 10 | Previous | | |
| 11 | Play | | |
| 12 | Next | | |
| 13 | Rewind | | |
| 14 | Pause | | |
| 15 | Fast Forward | | |
| 16 | Stop | | |

### Future: automatic device detection via CEC queries

An alternative to keyword matching is querying the device itself over CEC:

- **`Give OSD Name` (CEC opcode 0x46)** — asks the connected device to return its human-readable name (e.g. "Fire TV Stick", "XFINITY X1"). More reliable than guessing from the user-assigned matrix input name.
- **`Give Device Vendor ID` (CEC opcode 0x8C)** — returns an IEEE OUI (3-byte manufacturer code) which can be looked up in a vendor table (e.g. 0x0018EC = Comcast/Xfinity, 0x00E091 = Amazon).

Whether the OREI matrix exposes these CEC queries through its HTTP API has not yet been tested (the device accepted undocumented key indices 20–32 when probed, so further CEC commands may also be available). If supported, these queries could replace the keyword table entirely, providing automatic detection without any dependency on naming conventions.

---

## Compatibility

Tested against the **OREI UHD88-EXB400R-K** (firmware V1.03.01). The HTTP `comhead` API and telnet push format are shared across OREI's HDBaseT matrix switch family; other models in the range are likely compatible with little or no modification.
