# OREI Matrix Switch — Installation Guide

Deployment target: Raspberry Pi 5, Debian Trixie, systemd.

---

## 1. Create the service user

```bash
sudo useradd --system --shell /usr/sbin/nologin --create-home --home-dir /opt/matrix-switch matrix
```

---

## 2. Copy files to the deployment directory

If you cloned the repo on another machine, copy it across (adjust source path as needed):

```bash
sudo rsync -av --exclude='.venv' --exclude='__pycache__' \
    /path/to/orei-matrix-controller/ /opt/matrix-switch/
```

Or clone directly on the Pi (replace with your actual repo URL if using git):

```bash
sudo git clone <repo-url> /opt/matrix-switch
```

---

## 3. Create the Python virtual environment

```bash
cd /opt/matrix-switch
sudo python3 -m venv .venv
sudo .venv/bin/pip install -r requirements.txt
```

---

## 4. Configure the application

```bash
sudo cp /opt/matrix-switch/config.template.json /opt/matrix-switch/config.json
sudo nano /opt/matrix-switch/config.json
```

Edit the values to match your environment:

| Field | Description |
|---|---|
| `matrix.host` | IP address of the matrix switch |
| `matrix.http_port` | HTTP port (default: `80`) |
| `matrix.http_user` | HTTP login username (default: `Admin`) |
| `matrix.http_password` | HTTP login password (default: `admin`) |
| `matrix.telnet_port` | Telnet push port (default: `23`) |
| `matrix.num_inputs` | Number of inputs (default: `8`) |
| `matrix.num_outputs` | Number of outputs (default: `8`) |
| `polling.status_interval_seconds` | How often to poll for routing status (default: `10`) |
| `polling.names_interval_seconds` | How often to re-fetch input/output names (default: `3600`) |
| `flask.host` | Bind address for the web UI (default: `0.0.0.0`) |
| `flask.port` | TCP port for the web UI (default: `5000`) |
| `flask.debug` | Set to `false` in production |

---

## 5. Set correct ownership

```bash
sudo chown -R matrix:matrix /opt/matrix-switch
```

---

## 6. Create the log directory

```bash
sudo mkdir -p /var/log/matrix-switch
sudo chown matrix:matrix /var/log/matrix-switch
```

---

## 7. Install and enable the systemd service

```bash
sudo cp /opt/matrix-switch/matrix-switch.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable matrix-switch
sudo systemctl start matrix-switch
```

Check that it started successfully:

```bash
sudo systemctl status matrix-switch
```

---

## 8. Access the web interface

Open a browser and navigate to:

```
http://<pi-hostname-or-ip>:5000
```

---

## 9. Name your inputs and outputs on the matrix switch

**The app hides any port that still has its factory-default name** (e.g. `input1`, `hdmi output3`). Until you rename ports on the matrix device itself, the web UI will show an empty grid.

### How to rename

1. Open the matrix switch's own web interface in a browser (navigate to the IP address you put in `config.json`).
2. Log in (default credentials: `Admin` / `admin`).
3. Go to the input or output settings page and give each connected port a descriptive name (e.g. `Apple TV`, `Bar Sign`, `Conference Room`).

### Inputs

Rename every input that has a source device connected. Inputs left at their default (`input1`, `input2`, …) are hidden in the UI.

### Outputs — name only the primary connection side

Each physical output slot has **two entries** on the matrix: an HDMI output and an HDBaseT output. They share the same routing slot. **Only rename the entry that matches the cable actually connected to the display:**

| Connection | Rename | Leave at default |
|---|---|---|
| Direct HDMI cable to display | `hdmi outputN` entry | `hdbt outputN` entry |
| HDBaseT extender to display | `hdbt outputN` entry | `hdmi outputN` entry |

The app uses which side has a custom name to determine the display name **and** to infer the connection type for CEC commands — so naming only the correct side is important. If you accidentally name both sides, the HDMI name takes precedence.

### After renaming

Click **↺ Refresh Config** in the app (top-right of the header) to pull the new names immediately, or wait for the automatic hourly refresh.

---

## Useful commands

| Purpose | Command |
|---|---|
| View live logs | `sudo journalctl -fu matrix-switch` |
| View access log | `sudo tail -f /var/log/matrix-switch/access.log` |
| Restart the service | `sudo systemctl restart matrix-switch` |
| Stop the service | `sudo systemctl stop matrix-switch` |
| Edit config | `sudo nano /opt/matrix-switch/config.json && sudo systemctl restart matrix-switch` |

---

## Customising the Logo and Icons

The app displays `static/logo.png` in the page header and uses it as the source for
the favicon and home-screen icons. The default `logo.png` shipped with the repo is a
generic matrix-switch graphic. Replace it with your own image to personalise the app.

### Requirements
- A square PNG is ideal. Non-square images are padded to a square with the app's dark
  background colour (`#0f1117`) before being resized.
- Recommended source size: at least 512×512 px.

### Steps

1. Copy your image to `static/logo.png`.

2. Install Pillow if not already present:
   ```bash
   .venv/bin/pip install pillow
   ```

3. Regenerate all icon sizes:
   ```bash
   .venv/bin/python3 - << 'EOF'
   from PIL import Image
   import os
   img = Image.open("static/logo.png").convert("RGBA")
   w, h = img.size
   size = max(w, h)
   bg = Image.new("RGBA", (size, size), (15, 17, 23, 255))
   bg.paste(img, ((size - w) // 2, (size - h) // 2), img)
   bg = bg.convert("RGB")
   os.makedirs("static/icons", exist_ok=True)
   for s, path in [
       (32,  "static/favicon.png"),
       (180, "static/icons/apple-touch-icon.png"),
       (192, "static/icons/icon-192.png"),
       (512, "static/icons/icon-512.png"),
   ]:
       bg.resize((s, s), Image.LANCZOS).save(path, "PNG", optimize=True)
       print(f"  {path}")
   EOF
   ```

4. Tell git to ignore your local overrides so they are not overwritten by future `git pull` updates:
   ```bash
   git update-index --skip-worktree static/logo.png static/favicon.png \
       static/icons/apple-touch-icon.png static/icons/icon-192.png static/icons/icon-512.png
   ```

5. Restart the service to serve the updated files:
   ```bash
   sudo systemctl restart matrix-switch
   ```

The repo ships with generic versions of all five image files. Once you mark them as
`skip-worktree` (step 4), git will leave your custom files alone on every subsequent
`git pull`. To undo this and revert to the generic images, run:
```bash
git update-index --no-skip-worktree static/logo.png static/favicon.png \
    static/icons/apple-touch-icon.png static/icons/icon-192.png static/icons/icon-512.png
git checkout -- static/logo.png static/favicon.png static/icons/
```

---

## Updating

```bash
# Copy new files
sudo rsync -av --exclude='.venv' --exclude='__pycache__' --exclude='config.json' \
    /path/to/orei-matrix-controller/ /opt/matrix-switch/

# Fix ownership and restart
sudo chown -R matrix:matrix /opt/matrix-switch
sudo systemctl restart matrix-switch
```

---

## Uninstall

```bash
sudo systemctl stop matrix-switch
sudo systemctl disable matrix-switch
sudo rm /etc/systemd/system/matrix-switch.service
sudo systemctl daemon-reload
sudo rm -rf /opt/matrix-switch
sudo rm -rf /var/log/matrix-switch
sudo userdel matrix
```
