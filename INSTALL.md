# OREI Matrix Switch — Installation Guide

Deployment target: Raspberry Pi 5, Debian Trixie, systemd.

---

## 1. Create the service user

```bash
sudo useradd --system --shell /usr/sbin/nologin --create-home --home-dir /opt/matrix --user-group matrix
```

The `nologin` shell prevents interactive login, which is recommended for a service
account. `sudo -u matrix` still works for running commands as this user during
installation. If you need to open an interactive shell as the matrix user (e.g. for
administration), you can temporarily override the shell:

```bash
sudo -u matrix -s /bin/bash
```

Or permanently change it if preferred (not required by the install):

```bash
sudo chsh -s /bin/bash matrix
```

---

## 2. Create the Python virtual environment

```bash
sudo -u matrix python3 -m venv /opt/matrix/.venv
```

---

## 3. Clone the repository

```bash
sudo -u matrix git clone https://github.com/brett-kimball/orei-matrix-controller.git /opt/matrix/orei-matrix-controller
```

---

## 4. Install Python dependencies

```bash
sudo -u matrix /opt/matrix/.venv/bin/pip install -r /opt/matrix/orei-matrix-controller/requirements.txt
```

---

## 5. Configure the application

> All `vi` commands in this guide can be substituted with your preferred editor (e.g. `nano`, `vim`, `emacs`).

```bash
sudo -u matrix cp /opt/matrix/orei-matrix-controller/config.template.json /opt/matrix/orei-matrix-controller/config.json
sudo vi /opt/matrix/orei-matrix-controller/config.json
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

## 6. Create the log directory

```bash
sudo mkdir -p /var/log/matrix-switch
sudo chown matrix:matrix /var/log/matrix-switch
```

---

## 7. Install and enable the systemd service

```bash
sudo cp /opt/matrix/orei-matrix-controller/matrix-switch.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable matrix-switch
sudo systemctl start matrix-switch
```

Check that it started successfully:

```bash
sudo systemctl status matrix-switch
```

---

## 8. Configure log rotation

Gunicorn writes access and error logs to `/var/log/matrix-switch/`. Install the
provided logrotate config to rotate them daily, keeping 14 days of compressed history:

```bash
sudo cp /opt/matrix/orei-matrix-controller/matrix-switch.logrotate /etc/logrotate.d/matrix-switch
```

You can test it immediately (dry run) with:

```bash
sudo logrotate --debug /etc/logrotate.d/matrix-switch
```

> **Note:** Python application logs (startup messages, schedule actions, errors) go to
> the systemd journal via `journalctl` and are managed automatically by journald —
> no additional configuration is needed for those.

---

## 9. Access the web interface

Open a browser and navigate to:

```
http://<pi-hostname-or-ip>:5000
```

---

## 10. Configure nginx reverse proxy (optional)

Skip this step if you only need LAN access — the app is already reachable on port 5000
from any device on the local network.

If you want to expose the app publicly through an FQDN (with HTTPS via Certbot), use
the provided nginx virtual-host file.

### Create the basic-auth password file

```bash
sudo apt install apache2-utils   # provides htpasswd
sudo htpasswd -c /etc/nginx/.htpasswd-matrix-switch <username>
# Enter and confirm the password when prompted.
# To add more users (omit -c to append): sudo htpasswd /etc/nginx/.htpasswd-matrix-switch <username2>
```

### Install the virtual-host config

```bash
sudo cp /opt/matrix/orei-matrix-controller/matrix-switch.nginx /etc/nginx/sites-available/matrix-switch
```

Edit the file and replace `matrix.example.com` with your actual FQDN:

```bash
sudo vi /etc/nginx/sites-available/matrix-switch
```

Enable the site and reload nginx:

```bash
sudo ln -s /etc/nginx/sites-available/matrix-switch /etc/nginx/sites-enabled/matrix-switch
sudo nginx -t && sudo systemctl reload nginx
```

### Add HTTPS with Certbot

```bash
sudo certbot --nginx -d matrix.example.com
```

Certbot will automatically modify the virtual-host file to add HTTPS and redirect HTTP
to HTTPS.

---

## 11. Name your inputs and outputs on the matrix switch

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
| Edit config | `sudo vi /opt/matrix/orei-matrix-controller/config.json && sudo systemctl restart matrix-switch` |

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

1. Copy your image into place.  Because `/home/<you>` is readable only by your
   own user, stage the file through `/tmp` first:
   ```bash
   cp /path/to/your/image.png /tmp/logo.png
   sudo -u matrix cp /tmp/logo.png /opt/matrix/orei-matrix-controller/static/logo.png
   rm /tmp/logo.png
   ```

2. Install Pillow if not already present:
   ```bash
   sudo -u matrix /opt/matrix/.venv/bin/pip install pillow
   ```

3. Regenerate all icon sizes:
   ```bash
   sudo -u matrix /opt/matrix/.venv/bin/python3 - << 'EOF'
   from PIL import Image
   import os
   base = "/opt/matrix/orei-matrix-controller/static"
   img = Image.open(f"{base}/logo.png").convert("RGBA")
   w, h = img.size
   size = max(w, h)
   bg = Image.new("RGBA", (size, size), (15, 17, 23, 255))
   bg.paste(img, ((size - w) // 2, (size - h) // 2), img)
   bg = bg.convert("RGB")
   os.makedirs(f"{base}/icons", exist_ok=True)
   for s, path in [
       (32,  f"{base}/favicon.png"),
       (180, f"{base}/icons/apple-touch-icon.png"),
       (192, f"{base}/icons/icon-192.png"),
       (512, f"{base}/icons/icon-512.png"),
   ]:
       bg.resize((s, s), Image.LANCZOS).save(path, "PNG", optimize=True)
       print(f"  {path}")
   EOF
   ```

4. Tell git to ignore your local overrides so they are not overwritten by future `git pull` updates:
   ```bash
   sudo -u matrix git -C /opt/matrix/orei-matrix-controller update-index --skip-worktree \
       static/logo.png static/favicon.png \
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
sudo -u matrix git -C /opt/matrix/orei-matrix-controller update-index --no-skip-worktree \
    static/logo.png static/favicon.png \
    static/icons/apple-touch-icon.png static/icons/icon-192.png static/icons/icon-512.png
sudo -u matrix git -C /opt/matrix/orei-matrix-controller checkout -- \
    static/logo.png static/favicon.png static/icons/
```

---

## Updating

```bash
sudo -u matrix git -C /opt/matrix/orei-matrix-controller pull
sudo -u matrix /opt/matrix/.venv/bin/pip install -r /opt/matrix/orei-matrix-controller/requirements.txt
sudo systemctl restart matrix-switch
```

---

## Uninstall

```bash
sudo systemctl stop matrix-switch
sudo systemctl disable matrix-switch
sudo rm /etc/systemd/system/matrix-switch.service
sudo systemctl daemon-reload
sudo rm -rf /opt/matrix
sudo rm -rf /var/log/matrix-switch
sudo rm -f /etc/logrotate.d/matrix-switch
sudo userdel matrix
```
